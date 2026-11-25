"""Low-level browser helpers shared by every campaign mode."""

from typing import Any
from urllib.parse import urlsplit


RESTRICTED_ACTIONS = (
    "click", "navigate", "search", "go_back", "send_keys", "evaluate",
    "write_file", "replace_file", "read_file",
)


def node_text(node: Any) -> str:
    attributes = getattr(node, "attributes", {}) or {}
    parts = [
        str(getattr(node, "node_value", "") or ""),
        str(attributes.get("aria-label", "") or ""),
        str(attributes.get("value", "") or ""),
    ]
    parts.extend(node_text(child) for child in getattr(node, "children_nodes", None) or [])
    normalized = (" ".join(part.casefold().split()) for part in parts)
    return " ".join(dict.fromkeys(part for part in normalized if part))


def valid_web_url(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
    )
