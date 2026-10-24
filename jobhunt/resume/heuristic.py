"""Deterministic resume structuring.

This is the fallback for when the LLM backend is unavailable. It has to cope
with the fact that ``pdfminer`` linearises a two-column resume by emitting the
left column then the right column, so dates and grades routinely surface under
the *following* heading rather than their own. The strategy is therefore:

1. bucket lines by heading,
2. harvest stray right-column tokens (date ranges, CPI/GPA) from anywhere,
3. parse experience in *blocks* delimited by bullets, rather than line by line.
"""

from __future__ import annotations

import re

from jobhunt.resume.profile import (
    Education,
    Experience,
    Identity,
    Profile,
    Project,
    Skills,
)

SECTION_ALIASES = {
    "education": {"education", "academics", "academic background"},
    "skills": {"skills", "technical skills", "core competencies"},
    "experience": {"work experience", "experience", "professional experience", "employment"},
    "projects": {"projects", "personal projects", "selected projects"},
    "achievements": {"achievements", "awards", "honors", "accomplishments"},
}

_BULLET = re.compile(r"^\s*[•\-–•\*●·∗]\s+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\+?\d[\d\s\-()]{8,}\d")
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DATE_RANGE = re.compile(
    rf"({_MONTH}\s*\d{{4}}|\d{{4}})\s*[–—\-]{{1,2}}\s*({_MONTH}\s*\d{{4}}|Present|Current|\d{{4}})",
    re.IGNORECASE,
)
_GRADE = re.compile(r"\b(CPI|CGPA|GPA|Percentage)\s*:?\s*([\d.]+)", re.IGNORECASE)
_LOCATION = re.compile(
    r"^[A-Z][A-Za-z. ]+,\s*[A-Z][A-Za-z. ]+$|remote|hybrid|onsite", re.IGNORECASE
)
_TECH_PREFIX = re.compile(r"^\s*tech\s*stack\s*:?\s*", re.IGNORECASE)
_DEGREE = re.compile(r"bachelor|master|b\.?\s?tech|m\.?\s?tech|b\.?\s?e\b|ph\.?d", re.IGNORECASE)


def split_sections(text: str) -> tuple[dict[str, list[str]], list[str]]:
    """Bucket lines by heading.

    Also returns the headings in the order they appeared, which the stray
    harvester needs to know which section a column-bled token came from.
    """
    lookup = {alias: key for key, aliases in SECTION_ALIASES.items() for alias in aliases}
    sections: dict[str, list[str]] = {k: [] for k in SECTION_ALIASES}
    sections["_header"] = []
    current = "_header"
    order: list[str] = ["_header"]

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = lookup.get(line.lower().strip(":"))
        if key and len(line) < 40:
            current = key
            if key not in order:
                order.append(key)
            continue
        sections[current].append(line)
    return sections, order


def _harvest_strays(
    sections: dict[str, list[str]], order: list[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Pull column-bleed tokens out of every section and re-home them.

    ``pdfminer`` emits the left column then the right column, so the tail of one
    section's right column ("2021 - 2025", "CPI: 8.3") surfaces *above* the next
    section's own content. The giveaway is position, not content:

    * a bare date/grade appearing **before** its section has produced any real
      content is bleed from the previous section;
    * one appearing **after** real content belongs where it sits (job dates are
      interleaved with job headers and are not bleed).

    Attributing purely by "previous section" instead rotates every date by one
    entry, which is the bug this replaced.
    """
    dates: dict[str, list[str]] = {k: [] for k in sections}
    grades: dict[str, list[str]] = {k: [] for k in sections}

    def previous(key: str) -> str:
        i = order.index(key) if key in order else 0
        return order[i - 1] if i > 0 else key

    for key in list(sections):
        keep: list[str] = []
        prev = previous(key)
        seen_content = False
        for line in sections[key]:
            stripped = line.strip()
            owner = key if seen_content else prev
            if _DATE_RANGE.fullmatch(stripped):
                dates[owner].append(stripped)
                continue
            if _GRADE.fullmatch(stripped):
                grades[owner].append(stripped)
                continue
            if m := _GRADE.search(stripped):
                if stripped.lower().startswith(("cpi", "cgpa", "gpa")):
                    grades[owner].append(m.group(0))
                    stripped = stripped[m.end() :].strip()
                    if not stripped:
                        continue
            keep.append(stripped)
            seen_content = True
        sections[key] = keep
    return dates, grades


def _parse_identity(header: list[str]) -> Identity:
    ident = Identity(name=header[0].strip() if header else "")
    blob = "\n".join(header)
    if m := _EMAIL.search(blob):
        ident.email = m.group(0)
    if m := _PHONE.search(blob):
        ident.phone = m.group(0).strip()
    for line in header:
        low = line.lower()
        if "linkedin.com" in low:
            ident.linkedin = line.strip()
        elif "github.com" in low:
            ident.github = line.strip()
    return ident


def _parse_experience(lines: list[str], stray_dates: list[str]) -> list[Experience]:
    """Parse in blocks: a run of heading lines followed by its bullets."""
    entries: list[Experience] = []
    pending: list[str] = []
    current: Experience | None = None

    def flush_header() -> None:
        nonlocal current
        if not pending:
            return
        exp = Experience()
        leftover: list[str] = []
        for line in pending:
            if m := _DATE_RANGE.search(line):
                exp.start, exp.end = m.group(1).strip(), m.group(2).strip()
                remainder = (line[: m.start()] + line[m.end() :]).strip(" |-,")
                if remainder:
                    leftover.append(remainder)
                continue
            if _LOCATION.search(line) and not exp.location:
                exp.location = line.strip()
                continue
            leftover.append(line)
        if leftover:
            exp.title = leftover[0]
        if len(leftover) > 1:
            exp.company = leftover[1]
        if exp.title or exp.company:
            entries.append(exp)
            current = exp
        pending.clear()

    for line in lines:
        if _BULLET.match(line):
            flush_header()
            body = _BULLET.sub("", line).strip()
            if current is None:
                continue
            if _TECH_PREFIX.match(body):
                current.tech = [
                    t.strip() for t in _TECH_PREFIX.sub("", body).split(",") if t.strip()
                ]
            else:
                current.bullets.append(body)
        else:
            # A continuation of the previous bullet (pdfminer wraps long lines)
            # rather than a new header, if we are already inside an entry.
            if current is not None and current.bullets and not pending and line[:1].islower():
                current.bullets[-1] = f"{current.bullets[-1]} {line}".strip()
            elif current is not None and current.tech and line[:1].isupper() and len(line) < 60:
                pending.append(line)
            else:
                pending.append(line)
    flush_header()

    # Re-attach any date ranges that column-bleed stole from these entries.
    for exp in entries:
        if not exp.start and stray_dates:
            m = _DATE_RANGE.search(stray_dates.pop(0))
            if m:
                exp.start, exp.end = m.group(1).strip(), m.group(2).strip()
    return entries


def _parse_education(lines: list[str], dates: list[str], grades: list[str]) -> list[Education]:
    if not lines:
        return []
    edu = Education()
    for line in lines:
        if _DEGREE.search(line) and not edu.degree:
            edu.degree = line.strip()
        elif not edu.institution:
            edu.institution = line.strip()
    if grades:
        edu.grade = grades[0].strip()
    if dates:
        if m := _DATE_RANGE.search(dates[0]):
            edu.start, edu.end = m.group(1).strip(), m.group(2).strip()
    return [edu] if (edu.institution or edu.degree) else []


def _parse_projects(lines: list[str]) -> list[Project]:
    projects: list[Project] = []
    current: Project | None = None
    for line in lines:
        if _BULLET.match(line):
            body = _BULLET.sub("", line).strip()
            if current is not None:
                if _TECH_PREFIX.match(body):
                    current.tech += [
                        t.strip() for t in _TECH_PREFIX.sub("", body).split(",") if t.strip()
                    ]
                else:
                    current.bullets.append(body)
            continue
        # Wrapped continuation of the previous bullet, not a new project.
        if current is not None and current.bullets and line[:1].islower():
            current.bullets[-1] = f"{current.bullets[-1]} {line}".strip()
            continue
        if current is not None:
            projects.append(current)
        name, _, tech = line.partition("|")
        current = Project(
            name=name.strip(),
            tech=[t.strip() for t in tech.split(",") if t.strip()],
        )
    if current is not None:
        projects.append(current)
    return projects


def _parse_skills(lines: list[str]) -> Skills:
    blob = " ".join(lines)
    blob = re.sub(r"^\s*technical\s+skills\s*:?\s*", "", blob, flags=re.IGNORECASE)
    if ":" in blob[:40]:
        blob = blob.split(":", 1)[1]
    tokens = [t.strip(" .") for t in re.split(r"[,•|]", blob)]
    return Skills(languages=[t for t in tokens if 1 < len(t) < 40])


def structure_heuristically(text: str) -> Profile:
    sections, order = split_sections(text)
    dates, grades = _harvest_strays(sections, order)

    profile = Profile()
    profile.identity = _parse_identity(sections["_header"])
    profile.skills = _parse_skills(sections["skills"])
    profile.experience = _parse_experience(sections["experience"], dates["experience"])
    profile.education = _parse_education(
        sections["education"], dates["education"], grades["education"]
    )
    profile.projects = _parse_projects(sections["projects"])
    profile.achievements = [
        _BULLET.sub("", line).strip() for line in sections["achievements"] if line.strip()
    ]
    return profile
