import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from job_agent.browser import ApplicationOutcome
from job_agent.cli import _process_application
from job_agent.telegram_approval import request_telegram_review
from job_agent.tracker import ApplicationTracker


class ApplicationTrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_workflow_records_outcome_and_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = ApplicationTracker(Path(directory) / "apps.db")
            args = Namespace(output=directory, model="test-model", browser_profile=directory, cdp_url=None)
            kwargs = dict(url="https://jobs.test/1", company="Example", role="Engineer", description="", screenshot="review.png")
            with patch("job_agent.cli._prepare_assets", new=AsyncMock(return_value=(Path("resume.pdf"), Path("letter.pdf"), "letter"))), patch("job_agent.cli.run_assisted_application", new=AsyncMock(return_value=ApplicationOutcome(4, "REVIEWED", "Verify receipt."))):
                await _process_application(args, {}, tracker, **kwargs)
            record = tracker.list_applications()[0]
            self.assertEqual(record["status"], "REVIEWED")
            self.assertEqual(record["notes"], "4 agent steps. Verify receipt.")
            with patch("job_agent.cli._prepare_assets", new=AsyncMock(side_effect=RuntimeError("missing resume"))):
                with self.assertRaisesRegex(RuntimeError, "missing resume"):
                    await _process_application(args, {}, tracker, **kwargs)
            self.assertEqual(tracker.list_applications()[0]["status"], "FAILED")

    async def test_telegram_does_not_discard_an_immediate_approval(self):
        import json
        from types import SimpleNamespace

        class Client:
            def __init__(self):
                self.sent = False
                self.consumed = False
                self.callback_data = ""
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def response(self, payload):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

            async def get(self, url, **kwargs):
                self.calls.append("getUpdates")
                updates = []
                if self.sent and not self.consumed:
                    self.consumed = True
                    updates = [{"update_id": 1, "callback_query": {
                        "id": "callback", "data": self.callback_data,
                        "message": {"chat": {"id": 123}, "message_id": 9},
                    }}]
                return self.response({"ok": True, "result": updates})

            async def post(self, url, **kwargs):
                if url.endswith("/sendPhoto"):
                    self.calls.append("sendPhoto")
                    self.sent = True
                    markup = json.loads(kwargs["data"]["reply_markup"])
                    self.callback_data = markup["inline_keyboard"][0][0]["callback_data"]
                    return self.response({"ok": True, "result": {"message_id": 9}})
                return self.response({"ok": True})

        client = Client()
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "review.png"
            screenshot.write_bytes(b"fixture")
            with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "123"}), patch("job_agent.telegram_approval.httpx.AsyncClient", return_value=client):
                approved = await request_telegram_review(screenshot, "Example", "Engineer", timeout_seconds=1)
        self.assertTrue(approved)
        self.assertEqual(client.calls[:2], ["getUpdates", "sendPhoto"])
