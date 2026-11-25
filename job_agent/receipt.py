from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic


@dataclass
class Receipt:
    searches: int | None = 0
    browsers: int = 0
    images: int = 0
    approvals: int = 0
    started: float = field(default_factory=monotonic)

    def __str__(self):
        searches = "search count unavailable" if self.searches is None else f"{self.searches} web searches"
        return (f"{searches} · {self.browsers} browser sessions · {self.images} model screenshots"
                f" · {self.approvals} approval screenshots · {monotonic() - self.started:.1f}s")


progress = ContextVar("task_progress", default=None)


async def report(message):
    if callback := progress.get():
        await callback(message)


current = ContextVar("campaign_receipt", default=None)


@contextmanager
def measure():
    receipt = Receipt()
    token = current.set(receipt)
    try:
        yield receipt
    finally:
        current.reset(token)


def browser_model(**options):
    from browser_use.llm import ChatOpenAI

    class MeasuredChat(ChatOpenAI):
        async def ainvoke(self, messages, output_format=None):
            if receipt := current.get():
                receipt.images += sum(
                    getattr(part, "type", None) == "image_url"
                    for message in messages if isinstance(message.content, list)
                    for part in message.content
                )
            return await super().ainvoke(messages, output_format)

    return MeasuredChat(**options)
