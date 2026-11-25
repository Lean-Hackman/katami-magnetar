"""Observe compact, reusable browser paths without changing agent behavior."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


_VOLATILE_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{24,})$",
    re.IGNORECASE,
)


def path_pattern(url: str) -> str:
    """Remove query data and replace likely record or session IDs."""
    segments = ["*" if _VOLATILE_SEGMENT.fullmatch(part) else part.casefold()
                for part in urlsplit(url).path.split("/") if part]
    return "/" + "/".join(segments) if segments else "/"


def semantic_actions(actions: list[str]) -> tuple[str, ...]:
    """Keep action names only, collapse repeats, and exclude observation noise."""
    ignored = {"done", "screenshot", "request_human_approval", "handoff_to_user"}
    result: list[str] = []
    for action in actions:
        normalized = re.sub(r"[^a-z0-9_]+", "_", action.casefold()).strip("_")
        if not normalized or normalized in ignored or result[-1:] == [normalized]:
            continue
        result.append(normalized)
    return tuple(result[:32])


class WorkflowHintObserver:
    """Store successful path candidates for later review and promotion."""

    def __init__(self, database: str | Path = "output/workflow_hints.sqlite3") -> None:
        self.database = Path(database).expanduser()

    def observe_success(self, *, url: str, mode: str, actions: list[str]) -> bool:
        normalized = semantic_actions(actions)
        if not normalized:
            return False
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        if not host:
            return False
        pattern = path_pattern(url)
        fingerprint = hashlib.sha256(
            json.dumps([mode.casefold(), host, pattern, normalized], separators=(",", ":")).encode()
        ).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS workflow_hint_candidates(
                    fingerprint TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    path_pattern TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    actions_json TEXT NOT NULL,
                    successes INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    promoted INTEGER NOT NULL DEFAULT 0
                )"""
            )
            connection.execute(
                """INSERT INTO workflow_hint_candidates(
                       fingerprint, host, path_pattern, mode, actions_json,
                       successes, first_seen, last_seen, promoted
                   ) VALUES(?,?,?,?,?,1,?,?,0)
                   ON CONFLICT(fingerprint) DO UPDATE SET
                       successes=successes+1,
                       last_seen=excluded.last_seen""",
                (fingerprint, host, pattern, mode.casefold(), json.dumps(normalized), now, now),
            )
        return True
