import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx


async def request_telegram_review(
    screenshot_path: str | Path,
    company: str,
    role: str,
    summary: str = "",
    timeout_seconds: int = 900,
) -> bool | None:
    """Optionally send a review screenshot; return None if Telegram is not configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token and not chat_id:
        return None
    if not token or not chat_id:
        raise ValueError("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")

    request_id = uuid.uuid4().hex[:12]
    approve_data = f"review:approve:{request_id}"
    reject_data = f"review:reject:{request_id}"
    api = f"https://api.telegram.org/bot{token}"
    markup = {
        "inline_keyboard": [[
            {"text": "Approve review", "callback_data": approve_data},
            {"text": "Reject review", "callback_data": reject_data},
        ]]
    }

    async with httpx.AsyncClient(timeout=35) as client:
        updates = await client.get(
            f"{api}/getUpdates", params={"offset": -1, "timeout": 0}
        )
        updates.raise_for_status()
        latest = updates.json().get("result", [])
        offset = latest[-1]["update_id"] + 1 if latest else 0
        with Path(screenshot_path).open("rb") as photo:
            response = await client.post(
                f"{api}/sendPhoto",
                data={
                    "chat_id": chat_id,
                    "caption": (
                        f"Application review\n{company} — {role}\n"
                        f"{summary[:500]}\nApprove allows one protected browser click."
                    ),
                    "reply_markup": json.dumps(markup),
                },
                files={"photo": (Path(screenshot_path).name, photo, "image/png")},
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram sendPhoto failed: {result}")
        message_id = result["result"]["message_id"]

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            response = await client.get(
                f"{api}/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": '["callback_query"]',
                },
            )
            response.raise_for_status()
            for update in response.json().get("result", []):
                offset = update["update_id"] + 1
                callback = update.get("callback_query", {})
                callback_data = callback.get("data")
                sender_chat_id = str(
                    callback.get("message", {}).get("chat", {}).get("id", "")
                )
                if (
                    callback_data not in {approve_data, reject_data}
                    or sender_chat_id != str(chat_id)
                ):
                    continue
                await client.post(
                    f"{api}/answerCallbackQuery",
                    json={"callback_query_id": callback["id"]},
                )
                await client.post(
                    f"{api}/editMessageReplyMarkup",
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
                return callback_data == approve_data
    return False
