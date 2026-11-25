import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ai import draft_open_question
from .browser_primitives import RESTRICTED_ACTIONS, node_text, valid_web_url
from .form_fill import fill_known_fields
from .llm_config import has_model_auth, resolve_api_key, resolve_base_url, resolve_model
from .telegram_approval import request_telegram_review
from .skills import SkillStore
from .receipt import browser_model, current

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")


@dataclass(frozen=True)
class ApplicationOutcome:
    steps: int
    status: str
    notes: str


def _agent_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in profile.items()
        if key not in {"demographics_optional", "files"} and value
    }


def _node_text(node: Any) -> str:
    return node_text(node)


def _click_needs_approval(node: Any) -> bool:
    text = _node_text(node)
    final_labels = (
        "submit application", "submit my application", "send application",
        "send my application", "complete application", "finish application",
        "confirm and submit",
    )
    return text == "submit" or any(label in text for label in final_labels)


def build_controller(
    profile: dict[str, Any],
    browser: Any = None,
    *,
    company: str = "",
    role: str = "",
    screenshot_path: str | Path = "output/review.png",
    model: str | None = None,
    state: dict[str, bool] | None = None,
) -> Any:
    try:
        from browser_use import ActionResult, Controller
        from browser_use.browser.events import ClickElementEvent, NavigateToUrlEvent
        from browser_use.browser.session import BrowserSession
        from browser_use.tools.views import ClickElementAction, NavigateAction
    except ImportError as exc:
        raise RuntimeError("Install the project dependencies with: pip install -e .") from exc

    controller = Controller(exclude_actions=list(RESTRICTED_ACTIONS))
    state = state if state is not None else {}
    pending_click = None
    approved_click = None

    def target_key(node: Any) -> tuple:
        return (node.target_id, node.frame_id, node.backend_node_id, _node_text(node))

    def clear_approval() -> None:
        nonlocal pending_click, approved_click
        pending_click = approved_click = None

    @controller.action("Get candidate facts, excluding files and optional demographics")
    def get_candidate_profile() -> ActionResult:
        return ActionResult(extracted_content=json.dumps(_agent_profile(profile), separators=(",", ":")))

    @controller.action("Fill known blank contact fields directly through the DOM; call on each form step")
    async def fill_profile_fields(browser_session: BrowserSession) -> ActionResult:
        if state.get("aborted") or state.get("submission_attempted"):
            return ActionResult(error="Application session is finished.")
        clear_approval()
        count = await fill_known_fields(browser_session, profile)
        return ActionResult(extracted_content=f"Filled {count} known blank contact fields.")

    @controller.action("Draft a factual answer for an open-ended application question")
    async def answer_open_question(question: str, job_context: str) -> ActionResult:
        answer = await draft_open_question(profile, question, job_context, model=model)
        return ActionResult(extracted_content=answer)

    @controller.action("Navigate to an HTTP or HTTPS application page", param_model=NavigateAction)
    async def safe_navigate(params: NavigateAction, browser_session: BrowserSession) -> ActionResult:
        if state.get("aborted") or state.get("submission_attempted"):
            return ActionResult(error="Application session is finished.")
        if not valid_web_url(params.url):
            return ActionResult(error="Use an HTTP or HTTPS URL without credentials.")
        clear_approval()
        event = browser_session.event_bus.dispatch(NavigateToUrlEvent(url=params.url, new_tab=params.new_tab))
        await event
        await event.event_result(raise_if_any=True, raise_if_none=False)
        return ActionResult(extracted_content=f"Navigated to {params.url}")

    class ApplicationClickAction(ClickElementAction):
        final_submission: bool = False

    @controller.action(
        "Click a control. Set final_submission=True when this sends the completed application, regardless of label. Next/Continue steps run automatically.",
        param_model=ApplicationClickAction,
    )
    async def safe_click(params: ApplicationClickAction, browser_session: BrowserSession) -> ActionResult:
        nonlocal pending_click, approved_click
        if state.get("aborted") or state.get("submission_attempted"):
            return ActionResult(error="Application session is finished; do not submit again.")
        node = await browser_session.get_element_by_index(params.index)
        if node is None:
            clear_approval()
            return ActionResult(error="Element unavailable. Refresh browser state.")
        target = target_key(node)
        final = params.final_submission or _click_needs_approval(node) or pending_click == target
        if final and approved_click != target:
            pending_click, approved_click = target, None
            return ActionResult(error="Call request_human_approval before final submission, then retry this target.")
        clear_approval()
        if final:
            # Never retry an uncertain submission automatically, even after a timeout.
            state["submission_attempted"] = True
        event = browser_session.event_bus.dispatch(ClickElementEvent(node=node))
        await event
        metadata = await event.event_result(raise_if_any=True, raise_if_none=False)
        if isinstance(metadata, dict) and "validation_error" in metadata:
            if final:
                state["submission_attempted"] = False
            return ActionResult(error=str(metadata["validation_error"]))
        return ActionResult(extracted_content=f"Clicked element {params.index}")

    @controller.action("Review a screenshot and approve one pending final-submission click")
    async def request_human_approval(filled_summary: str) -> ActionResult:
        nonlocal approved_click
        if state.get("aborted") or state.get("submission_attempted"):
            return ActionResult(error="Application session is finished.")
        if pending_click is None or browser is None:
            return ActionResult(error="Call safe_click on the final submission target first; a browser is required.")
        screenshot = Path(screenshot_path).expanduser().resolve()
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        await browser.take_screenshot(path=str(screenshot), full_page=True)
        if receipt := current.get():
            receipt.approvals += 1
        summary = f"Final submission target: {pending_click[-1]}\n{filled_summary}"
        print(f"\n{company} — {role}\n{summary}\nReview screenshot: {screenshot}")
        decision = await request_telegram_review(screenshot, company, role, summary=summary)
        if decision is None:
            response = await asyncio.to_thread(input, "Type 'approve' to submit, or anything else to stop: ")
            decision = response.strip().casefold() == "approve"
        if not decision:
            clear_approval()
            state["aborted"] = True
            return ActionResult(is_done=True, success=False, extracted_content="Candidate rejected submission. Stop.")
        approved_click = pending_click
        return ActionResult(extracted_content="Candidate approved one click on the pending final submission target.")

    # Default mutating tools must invalidate approval too. Read-only inspection
    # and screenshots remain available after submission to verify the result.
    for name in ("input", "upload_file", "select_dropdown", "switch", "close"):
        action = controller.registry.registry.actions.get(name)
        if action is None:
            continue

        def guard(function):
            async def guarded(**kwargs):
                if state.get("aborted") or state.get("submission_attempted"):
                    return ActionResult(error="Application session is finished.")
                clear_approval()
                return await function(**kwargs)
            return guarded

        action.function = guard(action.function)

    return controller


async def run_assisted_application(
    *,
    job_url: str,
    profile: dict[str, Any],
    document_paths: list[str | Path],
    browser_profile_dir: str | Path = "browser_profile",
    screenshot_path: str | Path = "output/review.png",
    company: str = "",
    role: str = "",
    cdp_url: str | None = None,
    model: str | None = None,
) -> ApplicationOutcome:
    if not has_model_auth():
        raise RuntimeError(
            "Set OPENAI_API_KEY to run the browser-use agent. "
            "Profile setup, scraping, and tracking work without it."
        )
    selected_model = resolve_model(model)
    workflow_skills = tuple(SkillStore().match(job_url, "jobs"))
    print("Skills:", ", ".join(skill.name for skill in workflow_skills) or "none")
    workflow_knowledge = "\n\n".join(
        f"Skill {skill.name} v{skill.version}: {skill.instructions}\n"
        f"Confirmation markers: {', '.join(skill.confirmation_markers)}"
        for skill in workflow_skills
    )

    try:
        from browser_use import Agent, Browser, BrowserProfile
    except ImportError as exc:
        raise RuntimeError("Install the project dependencies with: pip install -e .") from exc

    profile_dir = Path(browser_profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    screenshot = Path(screenshot_path).expanduser().resolve()
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    for path in document_paths:
        if not Path(path).expanduser().is_file():
            raise FileNotFoundError(f"Upload file not found: {path}")

    browser_options: dict[str, Any] = {
        "headless": False,
        "wait_between_actions": 0.6,
        "minimum_wait_page_load_time": 0.5,
        "highlight_elements": True,
        "enable_default_extensions": False,
    }
    if cdp_url:
        browser_options["cdp_url"] = cdp_url
    else:
        browser_options["user_data_dir"] = str(profile_dir)

    browser = Browser(browser_profile=BrowserProfile(**browser_options))
    state: dict[str, bool] = {}
    controller = build_controller(
        profile,
        browser,
        company=company,
        role=role,
        screenshot_path=screenshot,
        model=selected_model,
        state=state,
    )

    prompt = f"""
Complete the application for {role} at {company} using only candidate facts.
Reusable job workflow knowledge:
{workflow_knowledge or 'No matching job skill was found.'}

Call fill_profile_fields first on each new form step. It fills known blank fields
locally. Use get_candidate_profile only for remaining fields. Never overwrite
existing values or invent facts. Leave optional demographic fields blank.
Use answer_open_question for free-response drafts. Use the built-in upload_file
for these permitted documents: {', '.join(map(str, document_paths))}.
Use safe_navigate and safe_click. Next, Continue, and ordinary controls run automatically.
Use DOM text, accessibility labels and element indices first. Refresh/inspect DOM
if a control is unclear. Request screenshot only as the final fallback when DOM
information is insufficient. Do not request screenshots for routine steps.
Before the final action that sends the application, call safe_click with
final_submission=True regardless of its label, then request_human_approval with
an accurate summary. Only after approval retry that same target. Stop on rejection.
If a button's effect remains uncertain after inspection, do not guess that it is
an intermediate step. Register it as final_submission=True for review.
After an approved submission, inspect for receipt confirmation and report the
result without clicking again. Stop on authentication challenges and explain
what the candidate must complete; do not create accounts or retrieve codes.
"""
    llm = browser_model(
        model=selected_model,
        api_key=resolve_api_key(),
        base_url=resolve_base_url(),
        temperature=0.1,
        max_retries=2,
        timeout=45,
    )
    agent = Agent(
        task=prompt,
        llm=llm,
        browser=browser,
        controller=controller,
        available_file_paths=[str(Path(path).expanduser().resolve()) for path in document_paths],
        use_vision="auto",
        vision_detail_level="low",
        max_actions_per_step=3,
        max_failures=2,
        extend_system_message=(
            "This is a human-supervised job application. Do not invent candidate facts. "
            "Do not read arbitrary local files; only upload the "
            "provided resume and cover letter. Require approval before final submission."
        ),
    )

    try:
        await browser.start()
        if receipt := current.get():
            receipt.browsers += 1
        await browser.navigate_to(job_url)
        print("Browser opened visibly with a persistent local profile.")
        history = await agent.run(max_steps=24)
        if state.get("aborted"):
            return ApplicationOutcome(len(history), "SKIPPED", "Candidate rejected final submission.")
        if state.get("submission_attempted"):
            return ApplicationOutcome(
                len(history), "REVIEWED",
                "Approved submission attempted; verify receipt before applying again. "
                + (history.final_result() or ""),
            )
        return ApplicationOutcome(
            len(history), "FAILED",
            "No approved submission occurred. " + (history.final_result() or ""),
        )
    except Exception as exc:
        if state.get("submission_attempted"):
            return ApplicationOutcome(
                0, "REVIEWED",
                f"Approved submission has an uncertain outcome; verify receipt before retrying: {exc}",
            )
        raise
    finally:
        try:
            await browser.stop()
        except Exception as exc:
            print(f"Browser cleanup failed: {exc}")
