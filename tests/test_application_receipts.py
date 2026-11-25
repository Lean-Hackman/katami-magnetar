import unittest
from unittest.mock import AsyncMock, MagicMock

from job_agent.application_receipts import SubmissionReceipts, requested_submissions


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_new_site_confirmation_counts_once_per_form(self):
        receipt = SubmissionReceipts()
        tab = MagicMock()
        browser = MagicMock(get_or_create_cdp_session=AsyncMock(return_value=tab))
        evaluate = AsyncMock()
        tab.cdp_client.send.Runtime.evaluate = evaluate

        def page(text, url="https://jobs.example/form"):
            evaluate.return_value = {"result": {"value": {"url": url, "text": text}}}

        page("Thank you for applying")
        await receipt.before(browser)
        await receipt.observe(browser)
        self.assertFalse(receipt.verified)
        for _ in range(2):
            page("Submit application")
            await receipt.before(browser)
            page("How to tell if your application was submitted")
            await receipt.observe(browser)
            page("Application submitted!", "https://jobs.example/receipt")
            await receipt.observe(browser)
        self.assertEqual(len(receipt.verified), 1)

    def test_retry_keeps_batch_scope(self):
        history = [{"role": "user", "content": "Submit both job applications"}]
        self.assertEqual(requested_submissions("Retry", "Fill out this form", history), 2)
