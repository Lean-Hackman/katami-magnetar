"""Reusable, data-only browser workflow skills and campaign observations."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "skill"


@dataclass(frozen=True)
class WorkflowSkill:
    name: str
    mode: str
    hosts: tuple[str, ...] = ()
    instructions: str = ""
    final_action_labels: tuple[str, ...] = ()
    confirmation_markers: tuple[str, ...] = ()
    field_hints: dict[str, str] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowSkill":
        required = {"name", "mode"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Workflow skill is missing: {', '.join(sorted(missing))}")
        return cls(
            name=str(data["name"]),
            mode=str(data["mode"]),
            hosts=tuple(str(value).casefold() for value in data.get("hosts", [])),
            instructions=str(data.get("instructions", "")),
            final_action_labels=tuple(data.get("final_action_labels", [])),
            confirmation_markers=tuple(data.get("confirmation_markers", [])),
            field_hints=dict(data.get("field_hints", {})),
            version=int(data.get("version", 1)),
        )

    def matches(self, url: str, mode: str) -> bool:
        if self.mode.casefold() != mode.casefold():
            return False
        host = (urlsplit(url).hostname or "").casefold()
        return not self.hosts or any(host == item or host.endswith(f".{item}") for item in self.hosts)


class SkillStore:
    def __init__(self, roots: list[str | Path] | None = None) -> None:
        package_root = Path(__file__).with_name("workflow_skills")
        self.roots = list(dict.fromkeys([package_root, Path("workflow_skills"),
                                       *(Path(root).expanduser() for root in roots or [])]))

    def load(self) -> list[WorkflowSkill]:
        skills: dict[tuple[str, str], WorkflowSkill] = {}
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.json")):
                skill = WorkflowSkill.from_dict(json.loads(path.read_text(encoding="utf-8")))
                key = (skill.mode.casefold(), skill.name.casefold())
                # Later roots are user supplied and intentionally override built-ins.
                skills[key] = skill
        return list(skills.values())

    def match(self, url: str, mode: str) -> list[WorkflowSkill]:
        return [skill for skill in self.load() if skill.matches(url, mode)]

    def save(self, skill: WorkflowSkill, root: str | Path) -> Path:
        destination = Path(root).expanduser() / _slug(skill.mode) / f"{_slug(skill.name)}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(asdict(skill), indent=2), encoding="utf-8")
        return destination.resolve()


def record_observation(
    *,
    root: str | Path,
    campaign: str,
    mode: str,
    url: str,
    success: bool,
    matched_skills: list[str],
) -> Path:
    """Record sanitized campaign metadata; never persist DOM, prompts, or entered values."""
    host = urlsplit(url).hostname or "unknown-host"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = Path(root).expanduser() / _slug(mode) / f"{timestamp}-{_slug(host)}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "campaign": campaign,
        "mode": mode,
        "host": host,
        "success": success,
        "outcome": "success" if success else "failure",
        "matched_skills": matched_skills,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return destination.resolve()
