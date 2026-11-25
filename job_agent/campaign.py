"""General-purpose DOM-first browser campaigns with reusable workflow skills."""


import asyncio
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .browser_primitives import RESTRICTED_ACTIONS, node_text, valid_web_url
from .llm_config import has_model_auth, resolve_api_key, resolve_base_url, resolve_model
from .skills import SkillStore, WorkflowSkill, record_observation
from .receipt import browser_model, current, report

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

ApprovalPolicy = Literal["never", "final", "always"]

@dataclass(frozen=True)
class BrowserCampaign:
    name: str
    url: str
    instructions: str
    mode: str = "general"
    required_submissions: int = 0
    approval_policy: ApprovalPolicy = "final"
    permitted_files: tuple[str | Path, ...] = ()
    skills: tuple[WorkflowSkill, ...] = ()
    max_steps: int = 30
    browser_profile_dir: str | Path = "browser_profile"
    screenshot_path: str | Path = "output/campaign-review.png"
    cdp_url: str | None = None
    model: str | None = None
    observation_dir: str | Path | None = "campaign_observations"

    def __post_init__(self) -> None:
        if not valid_web_url(self.url):
            raise ValueError("Campaign URL must be HTTP or HTTPS.")
        if not self.instructions.strip():
            raise ValueError("Campaign instructions cannot be empty.")
        if self.max_steps < 1:
            raise ValueError("Campaign max_steps must be positive.")


@dataclass(frozen=True)
class CampaignOutcome:
    steps: int
    success: bool
    status: str
    result: str
    matched_skills: tuple[str, ...] = ()
    observation_path: Path | None = None


def _skill_prompt(skills: tuple[WorkflowSkill, ...]) -> str:
    if not skills:
        return "No matching workflow skill was found; inspect the current DOM carefully."
    blocks = []
    for skill in skills:
        hints = ", ".join(f"{label} -> {source}" for label, source in skill.field_hints.items())
        blocks.append(
            f"Skill {skill.name} v{skill.version}:\n"
            f"{skill.instructions}\n"
            f"Known field hints: {hints or 'none'}\n"
            f"Known completion markers: {', '.join(skill.confirmation_markers) or 'none'}"
        )
    return "\n\n".join(blocks)


def build_campaign_controller(
    campaign: BrowserCampaign,
    browser: Any,
    state: dict[str, bool],
    live=None,
    unattended=False,
    receipts=None,
    allow_handoff=False,
) -> Any:
    try:
        from browser_use import ActionResult, Controller
        from browser_use.browser.events import ClickElementEvent, NavigateToUrlEvent
        from browser_use.browser.session import BrowserSession
        from browser_use.tools.views import ClickElementAction, NavigateAction
    except ImportError as exc:
        raise RuntimeError("Install project dependencies with: pip install -e .") from exc

    controller = Controller(exclude_actions=list(RESTRICTED_ACTIONS))
    pending: tuple | None = None
    approved: tuple | None = None
    final_labels = tuple(
        label.casefold() for skill in campaign.skills for label in skill.final_action_labels
    )

    def target_key(node: Any) -> tuple:
        return (live.control_epoch if live else 0, node.target_id, node.frame_id, node.backend_node_id, node_text(node))

    def clear_approval() -> None:
        nonlocal pending, approved
        pending = approved = None

    if allow_handoff:
        @controller.action("Hand the current tab to the user when the next step needs their input or control. "
                           "Describe the visible blocker and the specific action needed.")
        async def handoff_to_user(reason: str, next_step: str) -> ActionResult:
            clear_approval()
            state["handoff"] = True
            state["finished"] = True
            return ActionResult(is_done=True, success=False, extracted_content=f"{reason}\n{next_step}")

    @controller.action("Navigate to a safe HTTP or HTTPS URL", param_model=NavigateAction)
    async def safe_navigate(params: NavigateAction, browser_session: BrowserSession) -> ActionResult:
        if state.get("finished"):
            return ActionResult(error="Campaign is finished.")
        if not valid_web_url(params.url):
            return ActionResult(error="Use an HTTP or HTTPS URL without embedded credentials.")
        clear_approval()
        event = browser_session.event_bus.dispatch(
            NavigateToUrlEvent(url=params.url, new_tab=params.new_tab)
        )
        await event
        await event.event_result(raise_if_any=True, raise_if_none=False)
        return ActionResult(extracted_content=f"Navigated to {params.url}")

    class CampaignClickAction(ClickElementAction):
        consequential: bool = False
        final_action: bool = False

    @controller.action(
        "Click a control. Set consequential=True for external side effects and final_action=True for the campaign's irreversible final action.",
        param_model=CampaignClickAction,
    )
    async def safe_click(params: CampaignClickAction, browser_session: BrowserSession) -> ActionResult:
        nonlocal pending, approved
        if state.get("finished"):
            return ActionResult(error="Campaign is finished.")
        node = await browser_session.get_element_by_index(params.index)
        if node is None:
            clear_approval()
            return ActionResult(error="Element unavailable. Refresh browser state.")
        target = target_key(node)
        text = target[-1]
        inferred_final = any(label in text for label in final_labels) or bool(receipts and text == "submit")
        gated = (
            campaign.approval_policy == "always"
            or campaign.approval_policy == "final" and (params.final_action or inferred_final)
        )
        if gated and approved != target:
            pending, approved = target, None
            return ActionResult(
                error="Call request_human_approval, then retry this exact target."
            )
        clear_approval()
        if params.final_action or inferred_final:
            if receipts:
                await receipts.before(browser_session)
            state["final_action_attempted"] = True
        event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
        await event
        metadata = await event.event_result(raise_if_any=True, raise_if_none=False)
        if isinstance(metadata, dict) and "validation_error" in metadata:
            if receipts:
                receipts.pending = None
            state["final_action_attempted"] = False
            return ActionResult(error=str(metadata["validation_error"]))
        return ActionResult(extracted_content=f"Clicked element {params.index}")

    @controller.action("Request approval for the pending browser action")
    async def request_human_approval(summary: str) -> ActionResult:
        nonlocal approved
        if pending is None:
            return ActionResult(error="Call safe_click on the intended target first.")
        if unattended:
            state["finished"] = True
            state["awaiting_review"] = True
            return ActionResult(is_done=True, success=False, extracted_content=
                                "Stopped before the final action. Configure BROWSER_PUBLIC_URL for Telegram approval.")
        screenshot = Path(campaign.screenshot_path).expanduser().resolve()
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        await browser.take_screenshot(path=str(screenshot), full_page=True)
        if receipt := current.get():
            receipt.approvals += 1
        if live:
            accepted = await live.approve(f"{pending[-1]}\n{summary}")
        else:
            print(f"\nCampaign: {campaign.name}\nPending action: {pending[-1]}\n{summary}")
            print(f"Review screenshot: {screenshot}")
            response = await asyncio.to_thread(input, "Type 'approve' to allow this action: ")
            accepted = response.strip().casefold() == "approve"
        if accepted is None:
            state["awaiting_review"] = True
            return ActionResult(is_done=True, success=False, extracted_content="Waiting for manual takeover.")
        if not accepted:
            state["finished"] = True
            state["rejected"] = True
            clear_approval()
            return ActionResult(is_done=True, success=False, extracted_content="Action rejected.")
        approved = pending
        return ActionResult(extracted_content="Approved one click on the pending target.")

    for name in ("input", "upload_file", "select_dropdown", "switch", "close"):
        action = controller.registry.registry.actions.get(name)
        if action is None:
            continue

        def guard(function):
            async def guarded(**kwargs):
                if state.get("finished"):
                    return ActionResult(error="Campaign is finished.")
                clear_approval()
                return await function(**kwargs)
            return guarded

        action.function = guard(action.function)

    return controller


async def run_browser_campaign(campaign: BrowserCampaign, *, live=None, auth="api", headless=False, on_blocked=None) -> CampaignOutcome:
    if auth == "api" and not has_model_auth():
        raise RuntimeError("Set OPENAI_API_KEY to run a browser campaign.")
    selected_model = resolve_model(campaign.model) if auth == "api" else campaign.model
    try:
        from browser_use import Agent, Browser, BrowserProfile
    except ImportError as exc:
        raise RuntimeError("Install project dependencies with: pip install -e .") from exc

    files = tuple(Path(path).expanduser().resolve() for path in campaign.permitted_files)
    for path in files:
        if not path.is_file():
            raise FileNotFoundError(f"Permitted upload file not found: {path}")
    profile_dir = Path(campaign.browser_profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "keep_alive": bool(on_blocked),
        "headless": headless or live is not None,
        "wait_between_actions": 0.6,
        "minimum_wait_page_load_time": 0.5,
        "highlight_elements": True,
        "enable_default_extensions": False,
    }
    options["cdp_url" if campaign.cdp_url else "user_data_dir"] = (
        campaign.cdp_url or str(profile_dir)
    )
    browser = Browser(browser_profile=BrowserProfile(**options))
    state: dict[str, bool] = {}
    from .live_browser import ChatStop
    from .application_receipts import SubmissionReceipts
    required = campaign.required_submissions or (1 if campaign.mode == "jobs" else 0)
    receipts = SubmissionReceipts() if required else None
    if live:
        live.receipts = receipts
    controller = build_campaign_controller(campaign, browser, state, live, unattended=headless and not live,
                                           receipts=receipts, allow_handoff=bool(live and on_blocked))
    if auth == "codex":
        from .codex_browser_model import CodexBrowserModel
        llm = CodexBrowserModel(selected_model)
    else:
        llm = browser_model(
            model=selected_model,
            api_key=resolve_api_key(),
            base_url=resolve_base_url(),
            temperature=0.1,
            max_retries=2,
            timeout=45,
        )
    prompt = f"""
Run browser campaign: {campaign.name}
Mode: {campaign.mode}
Goal:
{campaign.instructions}

Matched workflow knowledge:
{_skill_prompt(campaign.skills)}

Use DOM text, accessibility labels, element indices, and structured page state first.
Request a screenshot only when DOM information remains insufficient.
Use safe_navigate and safe_click. Mark external side effects as consequential and
the campaign's irreversible completion click as final_action. Follow the configured
approval policy ({campaign.approval_policy}). Never upload files outside the permitted
list. Verify the final page against known completion markers and report the outcome.
"""
    if live and on_blocked:
        prompt += ("\nUse handoff_to_user as soon as the next step needs the user: a sign-in or verification "
                   "challenge, a required answer absent from the supplied information, or a control still "
                   "unusable after inspecting the DOM and viewport. State what you see and what the user "
                   "can do next. Continue routine steps autonomously. Use request_human_approval for final "
                   "submission. Never ask for passwords or one-time codes in chat.")
    agent_options = dict(
        directly_open_url=False,
        llm=llm,
        browser=browser,
        controller=controller,
        available_file_paths=[str(path) for path in files],
        use_vision="auto",
        vision_detail_level="low",
        max_actions_per_step=1 if live else 3,
        max_failures=2,
    )
    history = None
    steps = 0
    observed_actions: list[str] = []
    seen_run_actions = 0
    try:
        await browser.start()
        if receipt := current.get():
            receipt.browsers += 1
        await browser.navigate_to(campaign.url)
        if live:
            await live.open(browser)

        async def step_end(active_agent):
            nonlocal seen_run_actions
            if receipts:
                await receipts.observe(browser)
            if live:
                await live.step_end(active_agent)
            actions = active_agent.history.action_names()
            if actions:
                observed_actions.extend(actions[seen_run_actions:])
                seen_run_actions = len(actions)
                await report(f"Browser step {steps + len(active_agent.history)} · {actions[-1]}")

        while True:
            seen_run_actions = 0
            agent = Agent(task=prompt, **agent_options)
            history = await agent.run(max_steps=campaign.max_steps,
                                      on_step_start=live.step_start if live else None, on_step_end=step_end)
            result = history.final_result() or "The browser stopped before confirming completion."
            success = bool(history.is_successful()) and not state.get("rejected", False)
            if not receipts and on_blocked:
                from .application_receipts import submission_count
                inferred = submission_count(result)
                if inferred:
                    required = inferred
                    receipts = SubmissionReceipts()
                    if live:
                        live.receipts = receipts
                    controller = build_campaign_controller(campaign, browser, state, live, receipts=receipts, allow_handoff=bool(live and on_blocked))
                    agent_options["controller"] = controller
            handoff_reason = result if state.get("handoff") else None
            if receipts:
                await receipts.observe(browser)
                count = len(receipts.verified)
                success = success and required > 0 and count >= required
                if success:
                    result = f"Verified {count} site-confirmed application submission(s)."
                elif not state.get("rejected"):
                    total = str(required) if required > 0 else "an unspecified number of"
                    result = (f"{count} site-confirmed submissions for {total} requested applications. "
                              "Inspect the site for remaining work before retrying. " +
                              ("Reply with the total, for example ‘2 applications’, to verify the batch." if required < 0 else ""))
            if handoff_reason:
                result = handoff_reason if not receipts else f"{handoff_reason}\n{result}"
                success = False
            status = "REJECTED" if state.get("rejected") else "COMPLETED" if success else "INCOMPLETE"
            if state.get("awaiting_review"):
                status = "AWAITING_REVIEW"
            steps += len(history)
            if not on_blocked or status not in {"INCOMPLETE", "AWAITING_REVIEW"}:
                break
            if live:
                live.in_step = False
            instruction = await on_blocked(result, await browser.get_current_page_url(), live)
            if instruction is None:
                status, result = "STOPPED", "Browser session closed at your request."
                break
            if required < 0:
                from .application_receipts import submission_count
                revised = submission_count(instruction)
                if revised > 0:
                    required = revised
            state.clear()
            prompt += (f"\nPrevious run has ended: {result}\nContinue on the current tab. "
                       f"The latest user instruction supersedes earlier instructions: {instruction}")
    except ChatStop:
        result, success, status = "Browser session closed at your request.", False, "STOPPED"
    except Exception as exc:
        result, success, status, steps = str(exc), False, "FAILED", len(history) if history else 0
    finally:
        if live:
            await live.close()
        try:
            if on_blocked and not campaign.cdp_url:
                await browser.kill()
            else:
                await browser.stop()
        except Exception:
            pass

    observation = None
    if campaign.observation_dir:
        observation = record_observation(
            root=campaign.observation_dir,
            campaign=campaign.name,
            mode=campaign.mode,
            url=campaign.url,
            success=success,
            matched_skills=[skill.name for skill in campaign.skills],
        )
    # Observe only. This does not alter prompts or campaign behavior. Runs with
    # manual browser control are skipped because their full path is not captured.
    if success and not (live and live.control_epoch):
        try:
            from .workflow_hints import WorkflowHintObserver
            WorkflowHintObserver(
                os.getenv("WORKFLOW_HINT_DB", "output/workflow_hints.sqlite3")
            ).observe_success(url=campaign.url, mode=campaign.mode, actions=observed_actions)
        except (OSError, sqlite3.Error, ValueError):
            pass
    return CampaignOutcome(
        steps=steps,
        success=success,
        status=status,
        result=result,
        matched_skills=tuple(skill.name for skill in campaign.skills),
        observation_path=observation,
    )


def campaign_with_skills(
    *,
    name: str,
    url: str,
    instructions: str,
    mode: str = "general",
    skill_dirs: list[str | Path] | None = None,
    **kwargs: Any,
) -> BrowserCampaign:
    skills = tuple(SkillStore(skill_dirs).match(url, mode))
    return BrowserCampaign(
        name=name,
        url=url,
        instructions=instructions,
        mode=mode,
        skills=skills,
        **kwargs,
    )
