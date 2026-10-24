"""Resume PDF -> structured :class:`Profile`.

Two paths. The LLM path produces a far better structuring and is used whenever
the backend is reachable. The heuristic path is a deterministic fallback so
onboarding is never blocked on LLM auth - it produces a rougher draft that the
interactive review step then corrects.
"""

from __future__ import annotations

from pathlib import Path

from jobhunt.llm.adapter import MODEL_JUDGE, LLMError, complete_json
from jobhunt.resume.heuristic import structure_heuristically
from jobhunt.resume.profile import Profile


def pdf_to_text(path: Path) -> str:
    """Extract the text layer. Raises if the PDF is a scan with no text."""
    from pdfminer.high_level import extract_text

    text = extract_text(str(path))
    if len(text.strip()) < 200:
        raise ValueError(
            f"{path.name} yielded only {len(text.strip())} characters of text. "
            "If this is a scanned image, export a text-based PDF instead."
        )
    return text


_SYSTEM = """You convert resume text into structured JSON.

Rules:
- Copy facts verbatim. Never invent, embellish, infer or round anything.
- Preserve every metric exactly as written ("87 defects", "90%", "20+", "750+").
- If a field is not present in the text, use an empty string or empty list.
- Split each bullet into its own array entry, with the leading marker removed.
- The source PDF may be two-column, so dates and grades can appear out of
  order. Attach each to the entry it belongs to, not the one it sits beside.
"""

_SCHEMA_HINT = """Return JSON with exactly this shape:

{
  "identity": {"name","email","phone","location","linkedin","github","portfolio"},
  "education": [{"institution","degree","field","start","end","grade"}],
  "experience": [{"company","title","location","start","end","bullets":[],"tech":[]}],
  "projects": [{"name","tech":[],"bullets":[],"url"}],
  "skills": {"languages":[],"frameworks":[],"tools":[],"domains":[]},
  "achievements": []
}"""


def structure_with_llm(text: str) -> Profile:
    data = complete_json(
        f"{_SCHEMA_HINT}\n\nResume text:\n\n{text}",
        system=_SYSTEM,
        model=MODEL_JUDGE,
        max_tokens=8000,
    )
    return Profile.model_validate(data)


def pdf_links(path: Path) -> list[str]:
    """Every hyperlink target in the PDF (https:, mailto:, tel:).

    Resumes usually show "GitHub | LinkedIn" as clickable words, so the text
    layer holds only the words and the real URLs live in link annotations.
    Reading only the text put the word "GitHub" where the URL belonged.
    """
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1

    out: list[str] = []
    try:
        with path.open("rb") as fh:
            doc = PDFDocument(PDFParser(fh))
            for page in PDFPage.create_pages(doc):
                for annot in resolve1(page.annots) or []:
                    a = resolve1(annot) or {}
                    action = resolve1(a.get("A")) if isinstance(a, dict) else None
                    uri = resolve1(action.get("URI")) if isinstance(action, dict) else None
                    if isinstance(uri, bytes):
                        uri = uri.decode("utf-8", "replace")
                    if isinstance(uri, str) and uri.strip():
                        out.append(uri.strip())
    except Exception:  # noqa: BLE001 - links are a bonus, never a failure
        return []
    return list(dict.fromkeys(out))


def looks_like_url(value: str) -> bool:
    """A link, as opposed to link text such as "LinkedIn"."""
    v = (value or "").strip().lower()
    return "." in v and " " not in v


def apply_links(profile: Profile, links: list[str]) -> Profile:
    """Fill contact fields from the PDF's link targets where the text had none."""
    ident = profile.identity
    by_host = {
        "linkedin": next((u for u in links if "linkedin.com/" in u.lower()), ""),
        "github": next((u for u in links if "github.com/" in u.lower()), ""),
    }
    for key, url in by_host.items():
        if url and not looks_like_url(getattr(ident, key)):
            setattr(ident, key, url)
    if not ident.email:
        ident.email = next((u[7:] for u in links if u.lower().startswith("mailto:")), "")
    if not ident.phone:
        ident.phone = next((u[4:] for u in links if u.lower().startswith("tel:")), "")
    # Anything left that is only link text is not a usable value.
    for key in ("linkedin", "github", "portfolio"):
        if getattr(ident, key) and not looks_like_url(getattr(ident, key)):
            setattr(ident, key, "")
    return profile


def extract_profile(path: Path, *, prefer_llm: bool = True) -> tuple[Profile, str]:
    """Return ``(profile, method)`` where method is ``"llm"`` or ``"heuristic"``."""
    text = pdf_to_text(path)
    links = pdf_links(path)
    if prefer_llm:
        try:
            return apply_links(structure_with_llm(text), links), "llm"
        except LLMError:
            pass  # fall through - onboarding must not be blocked on LLM auth
    return apply_links(structure_heuristically(text), links), "heuristic"
