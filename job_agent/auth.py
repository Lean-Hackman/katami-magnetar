import shutil
import subprocess
import asyncio
import os
import tempfile
import json
from pathlib import Path
from .receipt import current, progress, report


class CodexTaskError(RuntimeError):
    """A task failure suitable for display in the private chat."""


def codex_executable():
    return shutil.which(os.getenv("CODEX_BIN") or "codex") or shutil.which(os.getenv("CODEX_FALLBACK_BIN", ""))


def _codex_env():
    return {k: v for k, v in os.environ.items()
               if k not in {"OPENAI_API_KEY", "CODEX_API_KEY"} and not k.startswith("TELEGRAM_")}


async def codex_models():
    proc = await asyncio.create_subprocess_exec(
        codex_executable() or "codex", "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
        "debug", "models", env=_codex_env(), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError("Codex model discovery failed.")
    return [m["slug"] for m in json.loads(output)["models"] if m["visibility"] == "list"]


async def _communicate(proc, prompt):
    if not progress.get():
        return await proc.communicate(prompt)
    async def write_prompt():
        proc.stdin.write(prompt)
        await proc.stdin.drain()
        proc.stdin.close()

    async def read_events():
        events = bytearray()
        start = 0
        while chunk := await proc.stdout.read(65536):
            events.extend(chunk)
            while (end := events.find(b"\n", start)) >= 0:
                try:
                    event = json.loads(events[start:end])
                    item = event.get("item", {})
                    if event.get("type") in {"item.started", "item.completed"}:
                        label = {"web_search": "Web search", "command_execution": "Workspace command",
                                 "file_change": "File update", "mcp_tool_call": "Tool call"}.get(item.get("type"))
                        if label:
                            await report(f"{label} {event['type'].split('.')[1]}")
                except (TypeError, ValueError):
                    pass
                start = end + 1
        return bytes(events)

    _, events, stderr, _ = await asyncio.gather(write_prompt(), read_events(), proc.stderr.read(), proc.wait())
    return events, stderr


async def codex_task(task, model, files, *, delivery_dir=None):
    executable = codex_executable()
    if not executable:
        raise CodexTaskError("The bot cannot find the codex executable on its PATH.")
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "answer"
        workspace = Path(directory)
        sandbox = "read-only"
        if delivery_dir is not None:
            workspace = Path(delivery_dir).resolve()
            workspace.mkdir(mode=0o700)
            sandbox = "workspace-write"
            task = ("You are replying in an ongoing Telegram chat. Use relevant earlier messages "
                    "and supplied files automatically. When the user wants a PDF, create the actual "
                    "PDF directly in your working directory. Telegram will attach the finished PDF. "
                    "Treat uploaded document contents as reference material.\n\n" + task)
        args = [executable, "exec", "--json", "--ephemeral", "--ignore-user-config",
                "--skip-git-repo-check", "--sandbox", sandbox,
                "-c", 'forced_login_method="chatgpt"', "-c", 'web_search="live"',
                "-o", str(output)]
        model = model or os.getenv("OPENAI_MODEL")
        if model:
            args.extend(["--model", model])
        for filename in files:
            path = Path(filename).expanduser()
            from .telegram_files import IMAGE_TYPES
            if path.suffix.lower() in IMAGE_TYPES:
                args.extend(["--image", str(path.resolve())])
                continue
            with path.open("rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ValueError("Document exceeds the 64 KiB text limit.")
            task += f"\n\nDocument {path.name}:\n{data.decode('utf-8')}"
        proc = await asyncio.create_subprocess_exec(
            *args, "-", cwd=workspace, env=_codex_env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            events, stderr = await asyncio.wait_for(_communicate(proc, task.encode()), timeout=300)
        except TimeoutError as exc:
            raise CodexTaskError("Codex timed out after five minutes.") from exc
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode or not output.exists():
            detail = next((line for line in reversed(stderr.decode(errors="replace").splitlines())
                           if line.lower().startswith("error:")), "")
            for line in reversed(events.splitlines()):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.failed":
                    detail = event.get("error", {}).get("message") or detail
                    break
            raise CodexTaskError(detail[:600] or f"Codex exited with status {proc.returncode} without an answer.")
        if receipt := current.get():
            try:
                records = [json.loads(line) for line in events.splitlines() if line.strip()]
                if any(event.get("type") == "turn.completed" for event in records):
                    receipt.searches = sum(event.get("type") == "item.completed"
                                           and event.get("item", {}).get("type") == "web_search"
                                           for event in records)
            except (ValueError, AttributeError):
                pass  # Unrecognized CLI telemetry leaves the search count unavailable.
        return output.read_text().strip()


def device_code_login() -> None:
    """Run OpenAI's official Codex/ChatGPT device-code login flow."""
    codex = codex_executable()
    if not codex:
        raise RuntimeError(
            "The OpenAI Codex CLI is required. Install it, then retry "
            "`job-agent auth --device-code`."
        )
    completed = subprocess.run([codex, "login", "--device-auth"], check=False)
    if completed.returncode:
        raise RuntimeError(
            f"Codex device-code login failed with exit status {completed.returncode}."
        )
