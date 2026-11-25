import os
from typing import Any

from .llm_config import has_model_auth, resolve_api_key, resolve_base_url, resolve_model


def _career_facts(profile: dict[str, Any]) -> str:
    facts = [
        str(profile.get("experience_summary", "")).strip(),
        *[
            str(highlight).strip()
            for job in profile.get("work_history", [])
            if isinstance(job, dict)
            for highlight in job.get("highlights", [])
            if str(highlight).strip()
        ],
    ]
    return "\n".join(fact for fact in facts if fact)


async def draft_cover_letter(
    profile: dict[str, Any],
    company: str,
    role: str,
    job_description: str,
    model: str | None = None,
) -> str:
    if not has_model_auth():
        raise RuntimeError("OPENAI_API_KEY is required for API model calls.")

    from openai import AsyncOpenAI

    response = await AsyncOpenAI(
        api_key=resolve_api_key(), base_url=resolve_base_url()
    ).chat.completions.create(
        model=resolve_model(model),
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "Write a concise, professional cover letter under 250 words. "
                    "Use only facts in the candidate background; never invent metrics, "
                    "skills, employers, dates, or qualifications. Return letter text only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Candidate name: {profile.get('personal', {}).get('first_name', '')} "
                    f"{profile.get('personal', {}).get('last_name', '')}\n"
                    f"Candidate background:\n{_career_facts(profile)}\n\n"
                    f"Company: {company}\nRole: {role}\n"
                    f"Job description:\n{job_description[:8000]}"
                ),
            },
        ],
    )
    letter = response.choices[0].message.content
    if not letter:
        raise RuntimeError("The language model returned an empty cover letter.")
    return letter.strip()


async def draft_open_question(
    profile: dict[str, Any],
    question: str,
    job_context: str,
    model: str | None = None,
) -> str:
    if not has_model_auth():
        raise RuntimeError(
            "OPENAI_API_KEY is required for API model calls. "
            "You can answer the question manually without this feature."
        )

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=resolve_api_key(), base_url=resolve_base_url())
    response = await client.chat.completions.create(
        model=resolve_model(model),
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "Draft a concise first-person job application answer. Use only "
                    "facts in the supplied candidate background. Do not invent "
                    "employers, dates, credentials, metrics, or qualifications. "
                    "If the facts are insufficient, say what detail the candidate "
                    "needs to add. The candidate reviews every draft."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Candidate background:\n{_career_facts(profile)}\n\n"
                    f"Job context:\n{job_context}\n\nQuestion:\n{question}"
                ),
            },
        ],
    )
    answer = response.choices[0].message.content
    if not answer:
        raise RuntimeError("The language model returned an empty answer.")
    return answer.strip()
