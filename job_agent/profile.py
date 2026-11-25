import json
from pathlib import Path
from typing import Any


class ProfileError(ValueError):
    """Raised when a candidate profile is missing or malformed."""


def load_profile(path: str | Path) -> dict[str, Any]:
    profile_path = Path(path).expanduser()
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileError(
            f"Profile not found: {profile_path}. Copy candidate_profile.example.json "
            "to candidate_profile.json and fill it in."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProfileError(f"Invalid JSON in {profile_path}: {exc}") from exc

    if not isinstance(profile, dict) or not isinstance(profile.get("personal"), dict):
        raise ProfileError("Profile must be an object with a 'personal' object.")
    return profile


def profile_value(profile: dict[str, Any], key: str) -> str | None:
    """Resolve a profile field using dotted paths such as 'personal.email'."""
    value: Any = profile
    for component in key.split("."):
        if not isinstance(value, dict) or component not in value:
            return None
        value = value[component]
    if value is None or isinstance(value, (dict, list)):
        return None
    result = str(value).strip()
    return result or None


def field_values(profile: dict[str, Any]) -> dict[str, str]:
    values = {
        "first name": profile_value(profile, "personal.first_name"),
        "last name": profile_value(profile, "personal.last_name"),
        "full name": " ".join(
            value
            for value in (
                profile_value(profile, "personal.first_name"),
                profile_value(profile, "personal.last_name"),
            )
            if value
        )
        or None,
        "email": profile_value(profile, "personal.email"),
        "phone": profile_value(profile, "personal.phone"),
        "city": profile_value(profile, "personal.location.city"),
        "state": profile_value(profile, "personal.location.state"),
        "country": profile_value(profile, "personal.location.country"),
        "zip": profile_value(profile, "personal.location.zip_code"),
        "postal": profile_value(profile, "personal.location.zip_code"),
        "linkedin": profile_value(profile, "links.linkedin"),
        "github": profile_value(profile, "links.github"),
        "portfolio": profile_value(profile, "links.portfolio"),
        "authorized to work": profile_value(
            profile, "work_authorization.authorized_to_work_us"
        ),
        "sponsorship": profile_value(
            profile, "work_authorization.requires_sponsorship"
        ),
        "relocate": profile_value(
            profile, "work_authorization.willing_to_relocate"
        ),
    }
    return {label: value for label, value in values.items() if value}
