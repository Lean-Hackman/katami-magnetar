import re
from pathlib import Path
from textwrap import wrap
from typing import Any

def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned or "application"


def draft_cover_letter(
    profile: dict[str, Any],
    company: str,
    role: str,
    job_description: str,
) -> str:
    """Create a factual draft using only user-provided profile details."""
    name = " ".join(
        part
        for part in (
            profile.get("personal", {}).get("first_name", ""),
            profile.get("personal", {}).get("last_name", ""),
        )
        if part
    ).strip()
    summary = str(profile.get("experience_summary", "")).strip()
    highlights: list[str] = []
    for job in profile.get("work_history", []):
        if isinstance(job, dict):
            highlights.extend(
                str(item).strip()
                for item in job.get("highlights", [])
                if str(item).strip()
            )
    matching_terms = _matching_terms(job_description, summary, *highlights)

    paragraphs = [
        f"Dear {company} Hiring Team,",
        f"I am applying for the {role} position at {company}. {summary}".strip(),
    ]
    if highlights:
        paragraphs.append("Relevant experience includes " + " ".join(highlights[:2]))
    if matching_terms:
        paragraphs.append(
            "The role's focus on "
            + ", ".join(matching_terms[:5])
            + " aligns with the experience described above."
        )
    paragraphs.append(
        "Thank you for your consideration. I would welcome the opportunity to discuss "
        "how my background may contribute to your team."
    )
    if name:
        paragraphs.extend(["Sincerely,", name])
    return "\n\n".join(paragraphs)


def _matching_terms(job_description: str, *sources: str) -> list[str]:
    common = {
        "about", "after", "also", "and", "are", "for", "from", "have", "into",
        "more", "our", "that", "the", "their", "this", "with", "you", "your",
        "will", "years", "work", "role", "team", "using", "ability", "experience",
        "skills", "strong", "including", "required", "preferred",
    }
    source_text = " ".join(sources).lower()
    terms = re.findall(r"[a-z][a-z+#.-]{2,}", job_description.lower())
    seen: set[str] = set()
    matches = []
    for term in terms:
        if term not in common and term in source_text and term not in seen:
            matches.append(term)
            seen.add(term)
    return matches


def write_pdf(text: str, output_path: str | Path) -> Path:
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError("Install the project dependencies with: pip install -e .") from exc
    destination = Path(output_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(destination), pagesize=LETTER)
    page_width, page_height = LETTER
    y = page_height - 72
    pdf.setFont("Helvetica", 11)
    for paragraph in text.splitlines():
        if not paragraph.strip():
            y -= 14
            continue
        for line in wrap(paragraph, width=88):
            if y < 72:
                pdf.showPage()
                pdf.setFont("Helvetica", 11)
                y = page_height - 72
            pdf.drawString(72, y, line)
            y -= 16
    pdf.save()
    return destination.resolve()


def prepare_assets(
    profile: dict[str, Any],
    company: str,
    role: str,
    job_description: str,
    output_dir: str | Path = "output",
    cover_letter_text: str | None = None,
) -> tuple[Path, Path, str]:
    text = cover_letter_text or draft_cover_letter(
        profile, company, role, job_description
    )
    filename = f"{_safe_filename(company)}_{_safe_filename(role)}_cover_letter.pdf"
    cover_letter = write_pdf(text, Path(output_dir) / filename)
    resume = Path(profile.get("files", {}).get("base_resume_pdf", "")).expanduser()
    if not resume.is_file():
        raise FileNotFoundError(f"Resume PDF not found: {resume}")
    return resume.resolve(), cover_letter, text
