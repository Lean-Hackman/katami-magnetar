"""Track newly visible site acknowledgements after a submission attempt."""
import re
from urllib.parse import urldefrag


def submission_count(text):
    """Return zero for unrelated tasks, -1 for an unspecified batch."""
    text = text.lower()
    if not re.search(r"\b(apply|applying|submit(?:ting)?|applications?|apps)\b", text):
        return 0
    if not re.search(r"\b(jobs?|roles?|positions?|applications?|apps|apply to)\b", text):
        return 0
    counts = {"both": 2, "two": 2, "three": 3, "four": 4, "five": 5}
    match = re.search(r"\b(\d+|both|two|three|four|five)\s+(?:(?:job|these|the|of the)\s+)*(?:applications?|apps|jobs?|roles?|positions?)\b", text)
    if match:
        number = match[1]
        return max(1, int(number)) if number.isdigit() else counts[number]
    return -1 if re.search(r"\b(applications|apps|jobs|roles|positions)\b", text) else 1


def requested_submissions(task, goal, history):
    current = submission_count(task)
    if current:
        return current
    if re.match(r"^\s*(?:retry|resume|continue|finish|yes|go ahead|try again|keep going)\b", task, re.I):
        prior = [submission_count(turn["content"]) for turn in history if turn["role"] == "user"]
        for count in reversed(prior):
            if count:
                return count
        # A short continuation can also refer to a batch described in the last report.
        prior = [submission_count(turn["content"]) for turn in history[-4:] if turn["role"] == "assistant"]
        if any(prior):
            return -1 if -1 in prior else max(prior)
    return submission_count(goal)


def claims_submission(text):
    return bool(re.search(r"\b(?:I(?:'ve| have)?|we(?:'ve| have)?) (?:successfully )?submitted\b"
                          r"|\b(?:applications?|apps) (?:has been |have been |were |was |successfully )*submitted\b",
                          text, re.I))


class SubmissionReceipts:
    def __init__(self):
        self.pending = None
        self.verified = set()

    async def snapshot(self, browser):
        session = await browser.get_or_create_cdp_session()
        response = await session.cdp_client.send.Runtime.evaluate(params={
            "expression": "({url:location.href, text:document.body?.innerText || ''})",
            "returnByValue": True}, session_id=session.session_id)
        page = response["result"]["value"]
        confirmation = re.compile(
            r"^(?:thank you for applying(?:[.!]| to | for |$)|"
            r"(?:your |the )?application (?:has been |was |is )?(?:successfully )?(?:received|submitted)(?:[.!]| to |$)|"
            r"we(?: have|'ve) received your application(?:[.!]| for | to |$))", re.I)
        markers = {line.strip().casefold() for line in page["text"].splitlines()
                   if confirmation.match(line.strip())}
        return urldefrag(page["url"]).url, markers

    async def before(self, browser):
        self.pending = await self.snapshot(browser)

    async def observe(self, browser):
        if self.pending:
            url, markers = await self.snapshot(browser)
            source, before = self.pending
            if markers - before:
                self.verified.add(source)
                self.pending = None
