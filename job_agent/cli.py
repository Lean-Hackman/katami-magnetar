import argparse
import asyncio
import json
import time
from .receipt import measure
from pathlib import Path
from typing import Any

from .browser import run_assisted_application
from .ai import draft_cover_letter
from .auth import device_code_login
from .campaign import campaign_with_skills, run_browser_campaign
from .documents import prepare_assets
from .profile import load_profile
from .scraper import load_boards, merge_queue, scrape_boards
from .tracker import ApplicationTracker
from .llm_config import has_model_auth
from .skills import SkillStore, WorkflowSkill, record_observation


def _read_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array.")
    return data


def _write_queue(path: Path, jobs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


async def _prepare_assets(
    profile: dict[str, Any],
    company: str,
    role: str,
    description: str,
    output: str | Path,
    model: str | None = None,
) -> tuple[Path, Path, str]:
    letter = None
    if has_model_auth():
        letter = await draft_cover_letter(
            profile, company, role, description, model
        )
    return prepare_assets(profile, company, role, description, output, letter)


def command_scrape(args: argparse.Namespace) -> None:
    queue_path = Path(args.queue)
    tracker = ApplicationTracker(args.database)
    discovered = asyncio.run(scrape_boards(load_boards(args.boards)))
    existing = _read_queue(queue_path)
    queue = merge_queue(existing, discovered, tracker.processed_urls())
    added = len(queue) - len(existing)
    _write_queue(queue_path, queue)
    print(f"Added {added} jobs; {len(queue)} jobs in {queue_path}.")


def command_prepare(args: argparse.Namespace) -> None:
    profile = load_profile(args.profile)
    resume, cover_letter, _ = asyncio.run(
        _prepare_assets(
            profile, args.company, args.role, args.description, args.output, args.model
        )
    )
    print(f"Resume: {resume}")
    print(f"Cover letter: {cover_letter}")


async def _process_application(
    args: argparse.Namespace,
    profile: dict[str, Any],
    tracker: ApplicationTracker,
    *,
    url: str,
    company: str,
    role: str,
    description: str,
    screenshot: str | Path,
) -> None:
    def record(status: str, notes: str = "") -> None:
        tracker.record(
            job_url=url, company_name=company, role_title=role,
            status=status, notes=notes,
        )

    record("IN_PROGRESS")
    try:
        resume, cover_letter, _ = await _prepare_assets(
            profile, company, role, description, args.output, args.model
        )
        result = await run_assisted_application(
            job_url=url,
            profile=profile,
            document_paths=[resume, cover_letter],
            browser_profile_dir=args.browser_profile,
            screenshot_path=screenshot,
            company=company,
            role=role,
            cdp_url=args.cdp_url,
            model=args.model,
        )
        record(result.status, f"{result.steps} agent steps. {result.notes}")
        matched = SkillStore().match(url, "jobs")
        record_observation(
            root=Path(args.output) / "campaign_observations",
            campaign=f"{company} — {role}",
            mode="jobs",
            url=url,
            success=result.status == "REVIEWED",
            matched_skills=[skill.name for skill in matched],
        )
    except Exception as exc:
        record("FAILED", str(exc)[:500])
        raise


def command_apply(args: argparse.Namespace) -> None:
    asyncio.run(_process_application(
        args, load_profile(args.profile), ApplicationTracker(args.database),
        url=args.url, company=args.company, role=args.role,
        description=args.description, screenshot=args.screenshot,
    ))


def command_batch(args: argparse.Namespace) -> None:
    if args.limit < 1 or args.delay < 0:
        raise ValueError("Batch limit must be positive and delay must be nonnegative.")
    tracker = ApplicationTracker(args.database)
    processed = tracker.processed_urls()
    pending = [
        job for job in _read_queue(Path(args.queue))
        if job.get("job_url") not in processed
    ][:args.limit]
    if not pending:
        print("No unprocessed jobs in the queue.")
        return
    profile = load_profile(args.profile)
    for index, job in enumerate(pending):
        try:
            asyncio.run(_process_application(
                args, profile, tracker,
                url=job["job_url"], company=job["company_name"],
                role=job["role_title"], description=job.get("job_description", ""),
                screenshot=Path(args.output) / "review.png",
            ))
        except Exception as exc:
            print(f"Failed {job.get('company_name', 'unknown company')}: {exc}")
        if index + 1 < len(pending) and args.delay:
            print(f"Waiting {args.delay} seconds before the next queued job.")
            time.sleep(args.delay)


def command_status(args: argparse.Namespace) -> None:
    records = ApplicationTracker(args.database).list_applications()
    if not records:
        print("No applications tracked.")
        return
    for item in records:
        print(
            f"{item['status']:12} {item['company_name']} — {item['role_title']} "
            f"({item['updated_at']})\n  {item['job_url']}"
        )


def command_auth(args: argparse.Namespace) -> None:
    device_code_login()
    print("Signed in. Use job-agent run --auth codex or job-agent telegram --auth codex.")


def command_campaign(args: argparse.Namespace) -> None:
    instructions = args.instructions
    if args.instructions_file:
        instructions = Path(args.instructions_file).read_text(encoding="utf-8")
    files = tuple(args.file or [])
    campaign = campaign_with_skills(
        name=args.name,
        url=args.url,
        instructions=instructions,
        mode=args.mode,
        skill_dirs=args.skill_dir,
        approval_policy=args.approval,
        permitted_files=files,
        max_steps=args.max_steps,
        browser_profile_dir=args.browser_profile,
        screenshot_path=args.screenshot,
        cdp_url=args.cdp_url,
        model=args.model,
        observation_dir=None if args.no_observation else args.observation_dir,
    )
    result = asyncio.run(run_browser_campaign(campaign))
    print(f"{result.status}: {result.result}")
    if result.matched_skills:
        print(f"Skills: {', '.join(result.matched_skills)}")
    if result.observation_path:
        print(f"Observation: {result.observation_path}")


def command_skill_save(args: argparse.Namespace) -> None:
    instructions = args.instructions
    if args.instructions_file:
        instructions = Path(args.instructions_file).read_text(encoding="utf-8")
    field_hints: dict[str, str] = {}
    for item in args.field_hint or []:
        label, separator, source = item.partition("=")
        if not separator or not label.strip() or not source.strip():
            raise ValueError("Each --field-hint must use LABEL=SOURCE format.")
        field_hints[label.strip()] = source.strip()
    skill = WorkflowSkill(
        name=args.name,
        mode=args.mode,
        hosts=tuple(args.host or []),
        instructions=instructions,
        final_action_labels=tuple(args.final_label or []),
        confirmation_markers=tuple(args.confirmation or []),
        field_hints=field_hints,
    )
    destination = SkillStore().save(skill, args.skill_dir)
    print(f"Saved workflow skill: {destination}")


def command_skill_list(args: argparse.Namespace) -> None:
    skills = SkillStore(args.skill_dir).load()
    for skill in skills:
        hosts = ", ".join(skill.hosts) if skill.hosts else "all hosts"
        print(f"{skill.mode:12} {skill.name} ({hosts}) v{skill.version}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scrape = subparsers.add_parser("scrape", help="Fetch matching jobs from ATS boards.")
    scrape.add_argument("--boards", default="boards.json")
    scrape.add_argument("--queue", default="job_queue.json")
    scrape.add_argument("--database", default="applications.sqlite3")
    scrape.set_defaults(func=command_scrape)

    prepare = subparsers.add_parser("prepare", help="Draft a factual cover letter PDF.")
    prepare.add_argument("--profile", default="candidate_profile.json")
    prepare.add_argument("--company", required=True)
    prepare.add_argument("--role", required=True)
    prepare.add_argument("--description", default="")
    prepare.add_argument("--output", default="output")
    prepare.add_argument(
        "--model",
        default=None,
        help="Any model ID available to your OpenAI API account; defaults to OPENAI_MODEL.",
    )
    prepare.set_defaults(func=command_prepare)

    apply = subparsers.add_parser(
        "apply", help="Open a visible persistent browser and assist with the current form."
    )
    apply.add_argument("--profile", default="candidate_profile.json")
    apply.add_argument("--url", required=True)
    apply.add_argument("--company", required=True)
    apply.add_argument("--role", required=True)
    apply.add_argument("--database", default="applications.sqlite3")
    apply.add_argument("--browser-profile", default="browser_profile")
    apply.add_argument("--screenshot", default="output/review.png")
    apply.add_argument("--description", default="")
    apply.add_argument("--output", default="output")
    apply.add_argument(
        "--model",
        default=None,
        help="Any model ID available to your OpenAI API account; defaults to OPENAI_MODEL.",
    )
    apply.add_argument(
        "--cdp-url",
        default=None,
        help="Attach to a locally running, visible Chrome instance (for example http://localhost:9222).",
    )
    apply.set_defaults(func=command_apply)

    batch = subparsers.add_parser(
        "batch", help="Process queued jobs sequentially; approve each final submission."
    )
    batch.add_argument("--queue", default="job_queue.json")
    batch.add_argument("--profile", default="candidate_profile.json")
    batch.add_argument("--database", default="applications.sqlite3")
    batch.add_argument("--browser-profile", default="browser_profile")
    batch.add_argument("--output", default="output")
    batch.add_argument("--limit", type=int, default=5)
    batch.add_argument("--delay", type=int, default=30)
    batch.add_argument("--cdp-url", default=None)
    batch.add_argument(
        "--model",
        default=None,
        help="Any model ID available to your OpenAI API account; defaults to OPENAI_MODEL.",
    )
    batch.set_defaults(func=command_batch)

    status = subparsers.add_parser("status", help="List tracked application states.")
    status.add_argument("--database", default="applications.sqlite3")
    status.set_defaults(func=command_status)

    auth = subparsers.add_parser(
        "auth", help="Authenticate the OpenAI Codex/ChatGPT client."
    )
    auth.add_argument(
        "--device-code",
        action="store_true",
        required=True,
        help="Run `codex login --device-auth`.",
    )
    auth.set_defaults(func=command_auth)

    campaign = subparsers.add_parser(
        "campaign", help="Run a general DOM-first browser campaign."
    )
    campaign.add_argument("--name", required=True)
    campaign.add_argument("--url", required=True)
    campaign.add_argument("--instructions", default="")
    campaign.add_argument("--instructions-file")
    campaign.add_argument("--mode", default="general")
    campaign.add_argument("--skill-dir", action="append", default=[])
    campaign.add_argument("--approval", choices=("never", "final", "always"), default="final")
    campaign.add_argument("--file", action="append")
    campaign.add_argument("--max-steps", type=int, default=30)
    campaign.add_argument("--browser-profile", default="browser_profile")
    campaign.add_argument("--screenshot", default="output/campaign-review.png")
    campaign.add_argument("--observation-dir", default="campaign_observations")
    campaign.add_argument("--no-observation", action="store_true")
    campaign.add_argument("--cdp-url")
    campaign.add_argument("--model")
    campaign.set_defaults(func=command_campaign)

    skill = subparsers.add_parser("skill", help="Manage reusable browser workflow skills.")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_list = skill_commands.add_parser("list", help="List built-in and local skills.")
    skill_list.add_argument("--skill-dir", action="append", default=[])
    skill_list.set_defaults(func=command_skill_list)
    skill_save = skill_commands.add_parser("save", help="Save stable workflow knowledge.")
    skill_save.add_argument("--name", required=True)
    skill_save.add_argument("--mode", required=True)
    skill_save.add_argument("--host", action="append")
    skill_save.add_argument("--instructions", default="")
    skill_save.add_argument("--instructions-file")
    skill_save.add_argument("--final-label", action="append")
    skill_save.add_argument("--confirmation", action="append")
    skill_save.add_argument("--field-hint", action="append", help="Stable LABEL=SOURCE mapping.")
    skill_save.add_argument("--skill-dir", default="workflow_skills")
    skill_save.set_defaults(func=command_skill_save)
    run = subparsers.add_parser("run", help="Answer a task with native OpenAI tools.")
    run.add_argument("task")
    run.add_argument("--auth", choices=("api", "codex"), default="api")
    run.add_argument("--model")
    run.add_argument("--file", action="append", default=[], help="Permit reading a UTF-8 document.")
    run.add_argument("--browser", action="store_true", help="Permit one interactive browser campaign.")
    run.set_defaults(func=command_run)
    telegram = subparsers.add_parser("telegram", help="Run tasks and select models in a private Telegram chat.")
    telegram.add_argument("--auth", choices=("api", "codex"), default="codex")
    telegram.add_argument("--models", nargs="+", help="Legacy startup list. The first ID sets the initial model.")
    from .telegram_tasks import serve
    telegram.set_defaults(func=lambda args: asyncio.run(serve(args.auth, args.models)))
    return parser


def command_run(args):
    from .harness import run_task
    print(asyncio.run(run_task(args.task, model=args.model, files=args.file, browser=args.browser, auth=args.auth)))


def main() -> None:
    args = build_parser().parse_args()
    if args.command in {"run", "apply", "batch", "campaign"}:
        with measure() as receipt:
            try:
                args.func(args)
            finally:
                print(receipt)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
