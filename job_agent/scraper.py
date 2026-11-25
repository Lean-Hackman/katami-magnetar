import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx


def _plain_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value or "")).strip()


def _matches(title: str, location: str, include: list[str], exclude: list[str]) -> bool:
    title_lower = title.casefold()
    location_lower = location.casefold()
    return (
        (not include or any(term.casefold() in title_lower for term in include))
        and not any(term.casefold() in title_lower for term in exclude)
        and (not location or not location_lower.startswith("unknown"))
    )


async def _fetch_board(
    client: httpx.AsyncClient, board: dict[str, str]
) -> list[dict[str, str]]:
    ats = board["ats"].casefold()
    slug = board["slug"].strip("/")
    company = board["company"]
    if ats == "greenhouse":
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    elif ats == "lever":
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    elif ats == "ashby":
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    else:
        raise ValueError(f"Unsupported ATS '{ats}' for {company}")

    response = await client.get(url)
    response.raise_for_status()
    payload: Any = response.json()
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
    results = []
    for item in jobs:
        if ats == "greenhouse":
            title = item.get("title", "")
            location = item.get("location", {}).get("name", "")
            description = item.get("content", "")
            job_url = item.get("absolute_url", "")
        elif ats == "lever":
            title = item.get("text", "")
            categories = item.get("categories") or {}
            location = categories.get("location", "")
            description = item.get("descriptionPlain", "")
            job_url = item.get("hostedUrl", "")
        else:
            title = item.get("title", "")
            location = item.get("location", "")
            description = item.get("descriptionPlainText", "") or item.get(
                "descriptionHtml", ""
            )
            job_url = item.get("jobUrl", "")

        if not title or not job_url or not _matches(
            title, location, board.get("include_keywords", []), board.get("exclude_keywords", [])
        ):
            continue
        results.append(
            {
                "job_url": job_url,
                "company_name": company,
                "role_title": title,
                "location": location,
                "ats": ats,
                "job_description": _plain_text(description)[:8000],
            }
        )
    return results


async def scrape_boards(
    boards: list[dict[str, str]], timeout: float = 20.0
) -> list[dict[str, str]]:
    headers = {"User-Agent": "job-application-assistant/0.1 (personal job search)"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        tasks = [_fetch_board(client, board) for board in boards]
        results = await asyncio.gather(*tasks)
    unique: dict[str, dict[str, str]] = {}
    for group in results:
        for job in group:
            unique.setdefault(job["job_url"], job)
    return list(unique.values())


def load_boards(path: str | Path) -> list[dict[str, str]]:
    boards = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(boards, list):
        raise ValueError("Boards file must contain a JSON array.")
    for board in boards:
        if not all(board.get(key) for key in ("ats", "company", "slug")):
            raise ValueError("Every board needs 'ats', 'company', and 'slug'.")
    return boards


def merge_queue(
    existing: list[dict[str, str]],
    discovered: list[dict[str, str]],
    processed_urls: set[str],
) -> list[dict[str, str]]:
    merged = list(existing)
    known = {job.get("job_url", "").strip() for job in existing} | processed_urls
    for job in discovered:
        url = job["job_url"].strip()
        if url not in known:
            merged.append(job)
            known.add(url)
    return merged
