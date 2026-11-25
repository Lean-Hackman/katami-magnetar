import os


def has_model_auth() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def resolve_model(model: str | None = None) -> str:
    selected = (model or os.getenv("OPENAI_MODEL", "")).strip()
    if not selected:
        raise RuntimeError(
            "Choose a model with --model or set OPENAI_MODEL. "
            "Any model ID available to your configured API account is accepted."
        )
    return selected


def resolve_base_url() -> str | None:
    return os.getenv("OPENAI_BASE_URL") or None


def resolve_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Set OPENAI_API_KEY for API model calls. Codex device login is separate "
            "and is available with `job-agent auth --device-code`."
        )
    return key
