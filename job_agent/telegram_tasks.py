import asyncio
import json
import os
import sqlite3
import tempfile
from time import monotonic
from collections import deque
from .application_receipts import claims_submission
from .telegram_files import AttachmentError, download, original_upload
from contextlib import closing
from pathlib import Path

import httpx

from .auth import CodexTaskError, codex_models
from .llm_config import resolve_api_key, resolve_base_url
from .harness import run_task
from .receipt import measure, progress, report


def progress_line(message):
    icons = {"Starting": "🚀", "Requesting": "💭", "Opening": "🌐", "Browser": "🧭",
             "Web": "🔎", "Workspace": "💻", "File": "📄", "Tool": "🛠️", "Running": "🛠️",
             "Run": "🏁"}
    return f"{icons.get(message.partition(' ')[0], '🔹')} {message}"


class Transcript:
    def __init__(self, db, chat):
        self.db, self.chat = db, chat
        db.execute("CREATE TABLE IF NOT EXISTS turns(chat TEXT, question TEXT, answer TEXT)")

        db.execute("CREATE TABLE IF NOT EXISTS chat_files(chat TEXT, position INTEGER, name TEXT, data BLOB, PRIMARY KEY(chat, position))")
        db.execute("CREATE TABLE IF NOT EXISTS chat_uploads(chat TEXT, position INTEGER, name TEXT, data BLOB, PRIMARY KEY(chat, position))")

        db.execute("CREATE TABLE IF NOT EXISTS paused_campaigns(chat TEXT PRIMARY KEY, url TEXT, goal TEXT, result TEXT)")

    def checkpoint(self, url, goal, result):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO paused_campaigns VALUES(?,?,?,?)", (self.chat, url, goal, result))

    def paused(self):
        return self.db.execute("SELECT url, goal, result FROM paused_campaigns WHERE chat=?", (self.chat,)).fetchone()

    def forget_campaign(self):
        with self.db:
            self.db.execute("DELETE FROM paused_campaigns WHERE chat=?", (self.chat,))

    def restore_files(self, directory):
        paths = []
        for position, name, data in self.db.execute(
            "SELECT position, name, data FROM chat_files WHERE chat=? ORDER BY position", (self.chat,)
        ):
            folder = directory / str(position)
            folder.mkdir(parents=True)
            path = folder / Path(name).name
            path.write_bytes(data)
            paths.append(path)
        return paths

    def restore_uploads(self, directory):
        paths = []
        for position, name, data in self.db.execute(
            "SELECT position, name, data FROM chat_uploads WHERE chat=? ORDER BY position", (self.chat,)
        ):
            folder = directory / f"upload-{position}"
            folder.mkdir(parents=True)
            path = folder / Path(name).name
            path.write_bytes(data)
            paths.append(path)
        return paths

    def history(self):
        rows = self.db.execute("SELECT question, answer FROM turns WHERE chat=? ORDER BY rowid DESC LIMIT 24", (self.chat,))
        recent = []
        remaining = 32768
        for question, answer in rows:
            size = len(question) + len(answer)
            if recent and size > remaining:
                break
            recent.append((question, answer))
            remaining -= size
        return [dict(role=role, content=text) for question, answer in reversed(recent)
                for role, text in (("user", question), ("assistant", answer))]

    def append(self, question, answer, files=(), uploads=()):
        with self.db:
            if files:
                self.db.execute("DELETE FROM chat_files WHERE chat=?", (self.chat,))
                self.db.execute("DELETE FROM chat_uploads WHERE chat=?", (self.chat,))
                self.db.executemany("INSERT INTO chat_files VALUES(?,?,?,?)",
                                    ((self.chat, i, path.name, path.read_bytes()) for i, path in enumerate(files)))
                self.db.executemany("INSERT INTO chat_uploads VALUES(?,?,?,?)",
                                    ((self.chat, i, path.name, path.read_bytes()) for i, path in enumerate(uploads)))
            self.db.execute("INSERT INTO turns VALUES(?,?,?)", (self.chat, question[:4096], answer[:8192]))
            self.db.execute("""DELETE FROM turns WHERE chat=? AND rowid NOT IN
                (SELECT rowid FROM turns WHERE chat=? ORDER BY rowid DESC LIMIT 24)""", (self.chat, self.chat))

    def clear(self):
        self.forget_campaign()
        with self.db:
            self.db.execute("DELETE FROM chat_files WHERE chat=?", (self.chat,))
            self.db.execute("DELETE FROM chat_uploads WHERE chat=?", (self.chat,))
            self.db.execute("DELETE FROM turns WHERE chat=?", (self.chat,))


async def available_models(auth):
    if auth == "codex":
        return await codex_models()
    from openai import AsyncOpenAI

    async with AsyncOpenAI(api_key=resolve_api_key(), base_url=resolve_base_url()) as client:
        return sorted({model.id async for model in client.models.list()})


async def send_pdfs(client, chat, directory):
    delivered = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() != ".pdf" or path.is_symlink() or not path.is_file():
            continue
        if path.stat().st_nlink != 1 or path.stat().st_size > 50_000_000:
            delivered.append(f"Could not attach {path.name}.")
            continue
        with path.open("rb") as pdf:
            if pdf.read(5) != b"%PDF-":
                delivered.append(f"{path.name} is not a valid PDF.")
                continue
            pdf.seek(0)
            try:
                response = await client.post("sendDocument", data={"chat_id": chat},
                                             files={"document": (path.name, pdf, "application/pdf")})
                sent = response.is_success and response.json().get("ok")
            except httpx.TransportError:
                sent = False
        delivered.append(f"Attached {path.name}." if sent else f"Delivery failed for {path.name}.")
    return "".join("\n" + line for line in delivered)


async def serve(auth, models):
    token = os.getenv("TELEGRAM_TASK_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise ValueError("Set TELEGRAM_CHAT_ID and a Telegram bot token.")
    path = Path(os.getenv("TELEGRAM_HISTORY_DB", "output/telegram.sqlite3")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        await _poll(auth, models, token, chat, Transcript(db, chat))


async def _poll(auth, models, token, chat, transcript):
    selected = models[0] if models else os.getenv("OPENAI_MODEL") or None
    pending = []
    offset = 0
    async with httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}/", timeout=40) as client:
        async def api(method, **params):
            response = await client.post(method, json=params)
            if not response.is_success:
                raise RuntimeError(f"Telegram {method} returned HTTP {response.status_code}.")
            return response.json()["result"]

        backlog = deque()
        async def next_message(timeout=25):
            nonlocal offset
            while not backlog:
                try:
                    updates = await api("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
                except httpx.TransportError:
                    await asyncio.sleep(2)
                    return None
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    sender = msg.get("chat", {})
                    if sender.get("type") == "private" and str(sender.get("id")) == chat:
                        backlog.append(msg)
                if not backlog:
                    return None
            return backlog.popleft()

        lines = deque(maxlen=6)
        progress_id = None
        last_edit = 0.0
        async def update_progress(message):
            nonlocal progress_id, last_edit
            message = progress_line(message)
            if not lines or lines[-1] != message:
                lines.append(message)
            if progress_id and monotonic() - last_edit < 2:
                return
            text = "\n".join(lines)
            try:
                if progress_id:
                    await api("editMessageText", chat_id=chat, message_id=progress_id, text=text)
                else:
                    sent = await api("sendMessage", chat_id=chat, text=text)
                    progress_id = sent["message_id"]
            except (RuntimeError, httpx.TransportError):
                pass
            last_edit = monotonic()

        application_outcome = None
        browser_files: list[Path] = []

        async def browse(url, goal):
            nonlocal application_outcome
            from .campaign import campaign_with_skills, run_browser_campaign
            from .live_browser import ChatBrowser
            from .application_receipts import requested_submissions

            viewer_button = {}

            async def announce(viewer_url):
                viewer_button["reply_markup"] = {"inline_keyboard": [[{"text": "Open live browser", "web_app": {"url": viewer_url}}]]}
                await api("sendMessage", chat_id=chat, text="Browser session started.",
                          **viewer_button)

            instructions = "Earlier conversation: " + json.dumps(transcript.history()) + "\nCurrent task: " + goal
            required = requested_submissions(task, goal, transcript.history())
            campaign = campaign_with_skills(
                mode="jobs" if required else "general",
                required_submissions=required,
                name="Telegram browser",
                url=url,
                instructions=instructions,
                model=selected,
                permitted_files=tuple(browser_files),
                observation_dir=None,
            )
            async def say(text):
                return await api("sendMessage", chat_id=chat, text=text, **viewer_button)
            live = ChatBrowser(next_message, say, token=token, owner=chat, announce=announce)

            async def wait_for_direction(reason, current_url, viewer):
                nonlocal goal
                transcript.checkpoint(current_url, goal, reason)
                if viewer_button:
                    viewer_button["reply_markup"]["inline_keyboard"][0][0]["text"] = "Control browser"
                await say(f"Browser paused. {reason[:2500]}\nOpen the browser to take over, or reply with instructions.")
                if viewer:
                    reply = await viewer.interact(allow_instruction=True)
                else:
                    await say("Reply with instructions, /resume to continue, or /stop to close.")
                    while True:
                        msg = await next_message()
                        reply = (msg or {}).get("text", "").strip()
                        if reply:
                            break
                if reply == "/stop":
                    return None
                if reply == "/resume":
                    return "Inspect the current tab after handback and continue the task."
                goal += "\nUser correction: " + reply
                transcript.checkpoint(current_url, goal, reason)
                return reply
            await report("Opening Chromium")
            result = await run_browser_campaign(campaign, live=live, auth=auth, headless=True,
                                                on_blocked=wait_for_direction)
            if result.status in {"COMPLETED", "REJECTED", "STOPPED"}:
                transcript.forget_campaign()
            summary = f"{result.status}: {result.result}"
            if required or "site-confirmed" in result.result:
                application_outcome = summary
            return summary

        while True:
            msg = await next_message()
            if not msg:
                continue
            item = msg.get("document") or (msg.get("photo") or [None])[-1]
            task = (msg.get("text") or msg.get("caption") or "").strip()
            if item:
                if len(pending) >= 6:
                    await api("sendMessage", chat_id=chat, text="Six attachments are queued. Send a task or /new.")
                    continue
                pending.append(item)
                if msg.get("media_group_id"):
                    if task:
                        item = dict(item, caption=task)
                        pending[-1] = item
                    await api("sendMessage", chat_id=chat, text="Attachment queued. Send a message when ready.")
                    continue
                if not task:
                    task = ("Use this attachment in the context of our conversation. Continue the "
                            "current task if clear, otherwise briefly describe what you received.")
            elif not task:
                continue
            command = "" if item else task.split()[0].split("@")[0]
            saved = transcript.paused()
            if saved and command == "/stop":
                transcript.forget_campaign()
                await api("sendMessage", chat_id=chat, text="Saved browser task cleared.")
                continue
            if saved and not item and (command == "/resume" or not command.startswith("/")):
                url, goal, reason = saved
                if command != "/resume":
                    goal += "\nUser correction: " + task
                task = f"/browse {url} {goal}\nRecovered after restart. Previous result: {reason}. Inspect current state before acting."
                command = "/browse"
            elif saved and (command == "/browse" or not command.startswith("/")):
                await api("sendMessage", chat_id=chat, text="A browser task is saved. Send /resume to reopen it or /stop to discard it.")
                continue
            application_outcome = None
            progress_id = None
            lines.clear()
            if command == "/browse" or not command.startswith("/"):
                progress.set(update_progress)
                await report("Starting task")
            if command == "/browse":
                parts = task.split(maxsplit=2)
                if len(parts) != 3:
                    answer = "Use /browse URL task to open a browser."
                else:
                    try:
                        with tempfile.TemporaryDirectory() as directory:
                            browser_files[:] = transcript.restore_uploads(Path(directory))
                            answer = await browse(parts[1], parts[2])
                        transcript.append(task, answer)
                    except (ValueError, RuntimeError, OSError) as exc:
                        answer = f"Browser session could not start ({type(exc).__name__}). Check the URL and host configuration."
            elif command == "/model":
                try:
                    catalog = await available_models(auth)
                except Exception as exc:
                    answer = f"Could not load the {auth} model catalog ({type(exc).__name__}). Check authentication and retry /model."
                else:
                    parts = task.split(maxsplit=1)
                    if not catalog:
                        answer = "The provider returned no selectable models."
                    elif len(parts) == 1:
                        active = selected or ("Codex default" if auth == "codex" else "none selected")
                        answer = f"Active model: {active}\nSelect with /model MODEL_ID\n\n" + "\n".join(catalog)
                    elif parts[1] in catalog:
                        selected = parts[1]
                        answer = f"Active model: {selected}"
                    else:
                        answer = "Unknown model ID. Send /model to see available models."
            elif command == "/new":
                pending.clear()
                transcript.clear()
                answer = "Conversation cleared."
            elif command.startswith("/"):
                answer = f"Active model: {selected or auth}. Use /model to list models, /new to clear history, /browse URL task for live handoff, or send a task."
            else:
                with measure() as receipt:
                    try:
                        with tempfile.TemporaryDirectory() as directory:
                            files = []
                            for index, upload in enumerate(pending):
                                files.append(await download(client, api, token, upload, Path(directory) / str(index)))
                            upload_files = [original_upload(path) for path in files]
                            if not pending:
                                files = transcript.restore_files(Path(directory))
                                upload_files = transcript.restore_uploads(Path(directory))
                            browser_files[:] = upload_files
                            task = "\n".join([u["caption"] for u in pending if u.get("caption")] + [task])
                            delivery_dir = Path(directory) / "deliverables" if auth == "codex" else None
                            answer = await run_task(task, model=selected, auth=auth, history=transcript.history(),
                                                    files=files, delivery_dir=delivery_dir, browser_handler=browse)
                            if application_outcome:
                                answer = application_outcome
                            elif claims_submission(answer):
                                answer = "Submission unverified. This task produced no verified browser receipt. Check the application status before submitting again."
                            if delivery_dir is not None:
                                answer += await send_pdfs(client, chat, delivery_dir)
                            transcript.append(
                                task,
                                answer,
                                files if pending else (),
                                upload_files if pending else (),
                            )
                        pending.clear()
                    except AttachmentError as exc:
                        pending.clear()
                        answer = f"{exc} Please resend the attachments for this task."
                    except CodexTaskError as exc:
                        answer = f"Codex task failed: {exc}"
                    except Exception as exc:
                        answer = f"Task failed using {selected or auth} ({type(exc).__name__}). Check authentication and model access."
                    answer += f"\n\n{receipt}"
            if progress_id:
                last_edit = 0.0
                await report("Run ended")
            progress.set(None)
            for start in range(0, len(answer), 2000):
                await api("sendMessage", chat_id=chat, text=answer[start:start + 2000])
