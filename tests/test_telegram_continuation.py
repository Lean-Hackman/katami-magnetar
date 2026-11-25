import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from job_agent.campaign import CampaignOutcome
from job_agent.telegram_tasks import Transcript, _poll


class TelegramContinuationTests(unittest.IsolatedAsyncioTestCase):
    def test_transcript_preserves_original_browser_upload(self):
        with sqlite3.connect(":memory:") as db, tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extracted = root / "resume.pdf.txt"
            upload = root / "resume.pdf"
            extracted.write_text("resume text")
            upload.write_bytes(b"%PDF-1.4 resume")
            transcript = Transcript(db, "1")
            transcript.append("apply", "ready", [extracted], [upload])
            restored = root / "restored"
            self.assertEqual(transcript.restore_files(restored)[0].read_text(), "resume text")
            self.assertEqual(transcript.restore_uploads(restored)[0].read_bytes(), b"%PDF-1.4 resume")

    async def test_chat_correction_in_same_update_batch(self):
        with sqlite3.connect(":memory:") as db:
            transcript = Transcript(db, "1")
            client = AsyncMock()
            messages = ["/browse https://example.org Review", "Use the second field"]
            updates = [{"update_id": i, "message": {"chat": {"type": "private", "id": 1}, "text": text}}
                       for i, text in enumerate(messages)]
            delivered = []

            async def post(method, json):
                if method == "getUpdates":
                    if delivered:
                        raise StopAsyncIteration
                    result = updates
                else:
                    delivered.append(json["text"])
                    result = {"message_id": len(delivered)}
                return MagicMock(is_success=True, json=lambda: {"result": result})

            async def campaign(spec, *, on_blocked, **kwargs):
                change = await on_blocked("Which field?", spec.url, None)
                self.assertEqual(change, messages[1])
                self.assertIn(change, transcript.paused()[1])
                return CampaignOutcome(2, True, "COMPLETED", "Second field selected")

            client.post.side_effect = post
            with patch("job_agent.telegram_tasks.httpx.AsyncClient") as factory, patch(
                "job_agent.campaign.run_browser_campaign", side_effect=campaign
            ):
                factory.return_value.__aenter__.return_value = client
                with self.assertRaises(StopAsyncIteration):
                    await _poll("codex", [], "test", "1", transcript)
            self.assertTrue(any("Browser paused" in line for line in delivered))
            self.assertIsNone(transcript.paused())
            self.assertIn("Second field selected", delivered[-1])
