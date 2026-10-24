"""Master profile - the single source of truth for every tailored resume.

The Tailor agent may reorder, re-weight and rephrase what is in here. It may
never introduce a company, number, technology or date that is not. The
:class:`FactRegistry` built from this profile is what makes that enforceable
rather than aspirational.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Identity(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Preferences(BaseModel):
    """Facts the resume does not contain but every application needs."""

    current_ctc_lpa: float | None = None
    expected_min_ctc_lpa: float | None = None
    notice_period_days: int | None = None
    preferred_locations: list[str] = Field(default_factory=list)
    open_to_remote: bool = True
    willing_to_relocate: bool = False
    work_authorization: str = ""      # e.g. "Indian citizen; needs sponsorship for US roles"
    years_experience: float | None = None


class ApplicationAnswers(BaseModel):
    """Answers to the questions application forms ask over and over.

    Collected once so a form fills completely instead of stalling on the same
    seven fields every time. Anything left empty is never guessed at - the
    Applicant reports it as needing a human rather than inventing a value.
    """

    # Work authorisation. Answered per posting: sponsorship is needed abroad
    # but not in India, and a blanket answer is wrong in one direction or the
    # other.
    needs_sponsorship_abroad: bool = True
    needs_sponsorship_india: bool = False
    work_auth_statement: str = ""
    # Citizenship is its own fact, not an inference from sponsorship. Forms ask
    # "are you a US citizen or permanent resident?" and the honest answer does
    # not follow from whether sponsorship is needed - a green-card holder needs
    # no sponsorship and is not a citizen either.
    citizenship: str = "India"

    # Location and mobility
    preferred_locations: list[str] = Field(default_factory=list)
    willing_to_relocate_domestic: bool = True
    willing_to_relocate_international: bool = False
    can_work_hybrid: bool = True
    open_to_remote: bool = True

    # Timing and money
    earliest_start: str = ""
    notice_period_days: int | None = None
    expected_ctc_inr_lpa: float | None = None
    expected_base_usd: float | None = None
    salary_negotiable: bool = True

    # Prior relationships with the employer
    previously_employed_here: bool = False
    family_employed_here: bool = False
    flag_companies: list[str] = Field(default_factory=list)

    # Voluntary EEO/demographic questions, US forms
    gender: str = ""
    race_ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""
    hispanic_latino: str = ""
    pronouns: str = ""

    # Miscellaneous recurring fields
    how_did_you_hear: str = ""
    portfolio_url: str = ""
    other_url: str = ""
    requires_accommodation: str = ""
    consent_to_sms: bool = False
    background_check_consent: bool = True
    references_available: str = ""

    _INDIA_WORDS = (
        "india", "bengaluru", "bangalore", "hyderabad", "pune",
        "gurgaon", "gurugram", "noida", "chennai", "mumbai", "delhi",
    )

    def _posting_is_india(self, location: str) -> bool:
        return any(k in (location or "").lower() for k in self._INDIA_WORDS)

    def sponsorship_answer(self, location: str) -> str:
        """Yes/No for "will you require sponsorship", judged by posting location."""
        india = self._posting_is_india(location)
        needed = self.needs_sponsorship_india if india else self.needs_sponsorship_abroad
        return "Yes" if needed else "No"

    def work_authorised_answer(self, location: str) -> str:
        """Yes/No for "are you legally authorised to work in <country>".

        The opposite polarity of :meth:`sponsorship_answer`, and kept as its own
        method because the two questions share every keyword a pattern could
        match on. Answering this one from the sponsorship value would put "yes,
        I am authorised to work in the United States" on a real application,
        which is false and is the kind of thing that ends a hiring process at
        the reference stage.
        """
        india = self._posting_is_india(location)
        needs = self.needs_sponsorship_india if india else self.needs_sponsorship_abroad
        return "No" if needs else "Yes"

    def citizenship_answer(self, location: str) -> str:
        """Yes/No for "are you a citizen or permanent resident of <country>".

        Judged against the posting's country, because that is the country the
        form means, and compared with the stated citizenship. An Indian citizen
        answers Yes for an India posting and No everywhere else - and "no" here
        is a plain fact, not something to be inferred from the sponsorship
        answer.
        """
        home = (self.citizenship or "").strip().lower()
        if not home:
            return "No"
        if home in ("india", "indian"):
            return "Yes" if self._posting_is_india(location) else "No"
        return "Yes" if home in (location or "").lower() else "No"


class Education(BaseModel):
    institution: str = ""
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    grade: str = ""


class Experience(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str = ""
    tech: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    url: str = ""


class Skills(BaseModel):
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


_MONTHS = {m: i for i, m in enumerate(
    ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'], start=1)}


def _parse_month(text: str):
    """"July 2025" -> date(2025, 7, 1). None for "Present" or anything unparsed."""
    import re as _re
    from datetime import date

    low = (text or '').strip().lower()
    if not low or low in ('present', 'current', 'now', 'ongoing'):
        return None
    year = _re.search(r'(19|20)\d{2}', low)
    if not year:
        return None
    month = next((n for name, n in _MONTHS.items() if name in low), 1)
    return date(int(year.group(0)), month, 1)


class Profile(BaseModel):
    identity: Identity = Field(default_factory=Identity)
    preferences: Preferences = Field(default_factory=Preferences)
    answers: ApplicationAnswers = Field(default_factory=ApplicationAnswers)
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    achievements: list[str] = Field(default_factory=list)

    # ---- persistence --------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> Profile:
        with path.open(encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh) or {})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                self.model_dump(mode="json"),
                fh,
                sort_keys=False,
                allow_unicode=True,
                width=100,
            )

    # ---- derived ------------------------------------------------------

    def all_skills(self) -> list[str]:
        s = self.skills
        return [*s.languages, *s.frameworks, *s.tools, *s.domains]

    def all_bullets(self) -> list[str]:
        out: list[str] = []
        for e in self.experience:
            out.extend(e.bullets)
        for p in self.projects:
            out.extend(p.bullets)
        return out

    def facts(self) -> FactRegistry:
        return FactRegistry.from_profile(self)

    def years_of_experience(self) -> str:
        """Full-time years, counted from the employment dates.

        Derived rather than stored: a number written into a config file is
        correct on the day it is written and quietly wrong every day after.
        Internships are excluded, because the forms asking this say "not
        counting internships".
        """
        from datetime import date

        months = 0
        for role in self.experience:
            if "intern" in (role.title or "").lower():
                continue
            start = _parse_month(role.start)
            if start is None:
                continue
            end = _parse_month(role.end) or date.today()
            months += max(0, (end.year - start.year) * 12 + end.month - start.month)

        if months <= 0:
            return ""
        years = months / 12
        # Forms want a plain number; "1.2" reads better than "1.2083".
        return str(int(years)) if years >= 2 and years == int(years) else f"{years:.1f}"

    def years_text(self) -> str:
        """Years of experience for a prompt: what you stated, else the dates.

        Never a made-up default - a prompt that says "0 years" or invents a
        number misleads the model scoring or writing on your behalf.
        """
        stated = self.preferences.years_experience
        if stated is not None:
            return f"{stated:g}"
        return self.years_of_experience() or "under 1"

    def current_role(self) -> str:
        """"Software Engineer at Acme, Pune" for the most recent role, or ""."""
        if not self.experience:
            return ""
        e = self.experience[0]
        role = " at ".join(x for x in (e.title, e.company) if x)
        return ", ".join(x for x in (role, e.location or self.identity.location) if x)

    def is_complete(self) -> tuple[bool, list[str]]:
        """Whether enough is filled in for the agents to act on your behalf."""
        missing = []
        ident = self.identity
        for label, value in (
            ("full name", ident.name),
            ("email", ident.email),
            ("phone", ident.phone),
            ("current location", ident.location),
        ):
            if not (value or "").strip():
                missing.append(label)
        if not self.experience and not self.education:
            missing.append("at least one job or degree")
        if not self.all_skills():
            missing.append("skills")
        return not missing, missing


# --------------------------------------------------------------------------
# Fact registry - the basis of the truthfulness validator
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"\b\d+(?:\.\d+)?%?\+?\b")

# Multi-word proper nouns ("Spring Boot", "Northwind Digital Assistant") and
# single capitalised tokens. Deliberately greedy: over-collecting allowed
# entities is safe, under-collecting causes false fabrication alarms.
#
# Two details that were wrong and blocked every legitimate resume:
#   * a dot is only part of a token when a letter follows it, so "Node.js"
#     stays whole while "Kubernetes." ends at the period. Allowing a bare dot
#     glued sentences together into 'Kubernetes.\nEngineered'.
#   * words are joined by literal spaces, never \s, so a phrase cannot run
#     across a line break into the next sentence.
_TOKEN = r"[A-Z][A-Za-z0-9+#-]*(?:\.[A-Za-z0-9+#-]+)*"
_PROPER = re.compile(rf"\b{_TOKEN}(?:[ ]+{_TOKEN})*")

# Resumes routinely join related technologies ("React/TypeScript",
# "Playwright/Cypress", "AI-driven"). Each side is checked separately.
_SPLIT_COMPOUND = re.compile(r"[/&+-]")

# Words that get capitalised by grammar rather than by being entities, and so
# are never a claim about the candidate.
#
# The list has to cover prose, not just resume bullets. Cover letters and
# application answers are full sentences, so they capitalise prepositions,
# pronouns and determiners constantly - "At Northwind I built ...", "That system
# ...", "For this role ...". Treating those as invented entities rejected every
# correctly-written letter.
_STOPWORDS = {
    # Action verbs that open a bullet
    "Built", "Delivered", "Engineered", "Created", "Diagnosed", "Optimized",
    "Led", "Integrated", "Automated", "Containerized", "Designed", "Developed",
    "Implemented", "Improved", "Reduced", "Achieved", "Received", "Recognized",
    "Won", "Solved", "Managed", "Owned", "Drove", "Shipped", "Migrated",
    "Refactored", "Architected", "Collaborated", "Partnered", "Wrote", "Ran",
    "Spearheaded", "Troubleshot", "Rebuilt", "Oversaw", "Maintained",
    # Determiners, pronouns, conjunctions
    "The", "A", "An", "This", "That", "These", "Those", "Their", "Our", "My",
    "I", "We", "It", "They", "He", "She", "You", "Your", "His", "Her", "Its",
    "And", "But", "Or", "Nor", "So", "Yet", "If", "Then", "Than", "As",
    "Both", "Each", "Every", "Either", "Neither", "All", "Any", "Some", "Most",
    "More", "Less", "Many", "Few", "Much", "Other", "Another", "Such", "Same",
    # Prepositions and adverbs that open sentences
    "At", "In", "On", "To", "For", "From", "With", "Without", "Within",
    "Into", "Onto", "Over", "Under", "Between", "Across", "Through", "During",
    "Before", "After", "Since", "Because", "Although", "However", "While",
    "When", "Where", "What", "Why", "How", "Here", "There", "Now", "Today",
    "Also", "Only", "Just", "Even", "Still", "Already", "Currently",
    "Recently", "Beyond", "Besides", "Throughout", "Alongside", "Toward",
    "Towards", "Given", "Having", "Being", "Once", "Whether", "Which", "Who",
    # Generic nouns that carry no claim
    "Software", "Engineer", "Engineering", "Technical", "Intern", "Present",
    "Tech", "Stack", "Skills", "Education", "Experience", "Projects",
    "Achievements", "Team", "Teams", "Role", "Work", "Company", "Product",
    "Platform", "System", "Systems", "Service", "Services", "Code", "Data",
    "Time", "Year", "Years", "Day", "Days", "Month", "Months", "Week",
    "Development", "Delivery", "Design", "Support", "Quality", "Performance",
    "Scale", "Growth", "Impact", "Value", "Users", "User", "Customers",
    "Customer", "Business", "Sincerely", "Regards", "Dear", "Hello", "Hi",
}


class FactRegistry:
    """Every entity and number the Tailor is allowed to use."""

    def __init__(self, entities: set[str], numbers: set[str]) -> None:
        self.entities = {e.lower() for e in entities}
        self.numbers = numbers

    @classmethod
    def from_profile(cls, profile: Profile) -> FactRegistry:
        entities: set[str] = set()
        numbers: set[str] = set()

        def ingest(text: str) -> None:
            if not text:
                return
            for m in _PROPER.finditer(text):
                token = m.group(0).strip()
                if token and token not in _STOPWORDS:
                    entities.add(token)
                    # Also register each word, so "Spring Boot" licenses "Spring".
                    for part in token.split():
                        if part not in _STOPWORDS and len(part) > 1:
                            entities.add(part)
            numbers.update(m.group(0) for m in _NUMBER.finditer(text))

        ingest(profile.identity.name)
        ingest(profile.identity.location)
        for edu in profile.education:
            for field in (edu.institution, edu.degree, edu.field, edu.grade, edu.start, edu.end):
                ingest(field)
        for exp in profile.experience:
            for field in (exp.company, exp.title, exp.location, exp.start, exp.end):
                ingest(field)
            for b in exp.bullets:
                ingest(b)
            for t in exp.tech:
                entities.add(t.lower())
                ingest(t)
        for proj in profile.projects:
            ingest(proj.name)
            for b in proj.bullets:
                ingest(b)
            for t in proj.tech:
                entities.add(t.lower())
                ingest(t)
        for ach in profile.achievements:
            ingest(ach)
        for skill in profile.all_skills():
            entities.add(skill.lower())
            # Also decompose: a multi-word skill like "AI Agents" has to
            # license "AI" on its own, or "AI-driven tooling" reads as a
            # fabricated entity.
            ingest(skill)

        return cls(entities, numbers)

    # ---- validation ----------------------------------------------------

    def check(self, text: str) -> list[str]:
        """Return every claim in ``text`` not traceable to the profile.

        An empty list means the text is safe to render.

        A single capitalised word at the start of a sentence is exempt. The
        Tailor rewrites bullets with whatever action verb fits the JD, so
        "Resolved ..." or "Spearheaded ..." must not read as a fabricated
        entity - and no stopword list stays complete against that. Fabricated
        employers and technologies overwhelmingly appear mid-sentence
        ("... at Google", "... using Rust"), or as multi-word proper nouns
        ("Stanford University"), both of which are still checked everywhere.
        """
        violations: list[str] = []

        normalised = {_normalise_number(x) for x in self.numbers}
        for m in _NUMBER.finditer(text):
            n = m.group(0)
            if n not in self.numbers and _normalise_number(n) not in normalised:
                violations.append(f"number not in profile: {n!r}")

        sentence_starts = _sentence_start_offsets(text)
        for m in _PROPER.finditer(text):
            token = m.group(0).strip()
            if not token or token in _STOPWORDS:
                continue
            if self._known(token):
                continue
            # A multi-word phrase is fine if every word is known.
            words = token.split()
            parts = [p for p in words if p not in _STOPWORDS]
            if parts and all(self._known(p) for p in parts):
                continue

            # A word opening a sentence is capitalised by grammar, so it is
            # usually not an entity at all - "Resolved ...", "Separately ...",
            # "At Northwind ...". Exempting all of them would however let a
            # sentence opening with a fabricated technology ("Rust services at
            # Northwind...") through unchecked, so the exemption is withheld from
            # anything that is recognisably a technology name.
            if m.start() in sentence_starts and not _is_technology(words[0]):
                if len(words) == 1:
                    continue
                tail = [p for p in words[1:] if p not in _STOPWORDS]
                if tail and all(self._known(p) for p in tail):
                    continue

            violations.append(f"entity not in profile: {token!r}")

        return violations

    def _known(self, token: str) -> bool:
        """Is this token traceable to the profile?

        Slash- and ampersand-joined compounds are split first: a resume that
        says "React/TypeScript" or "Playwright/Cypress" is naming two
        technologies the profile lists, not inventing a third.
        """
        if token.lower() in self.entities:
            return True
        pieces = [p.strip() for p in _SPLIT_COMPOUND.split(token) if p.strip()]
        if len(pieces) > 1:
            return all(
                # A lowercase piece of a compound is an ordinary adjective, not
                # a claim: "AI-driven" names AI and then describes it. Only the
                # capitalised pieces have to be traceable to the profile.
                piece[0].islower()
                or piece.lower() in self.entities
                or piece in _STOPWORDS
                for piece in pieces
            )

        # An adjective built from a known noun is the same claim, not a new
        # one: a profile listing "AI Agents" licenses "agentic", "Kubernetes"
        # licenses "Kubernetes-based". Rejecting these blocked a cover letter
        # for the word "Agentic" on a profile whose domains say "AI Agents".
        low = token.lower()
        for suffix in ("ic", "ics", "al", "ian", "ish", "less", "like", "based", "driven"):
            if low.endswith(suffix):
                stem = low[: -len(suffix)].rstrip("-")
                if stem and (stem in self.entities or f"{stem}s" in self.entities):
                    return True
        # And the plural/singular of a known noun.
        if low.endswith("s") and low[:-1] in self.entities:
            return True
        if f"{low}s" in self.entities:
            return True
        return False


# Bare-form action verbs that open resume bullets. The -ed/-ing forms are
# caught by the suffix rule below, so only these need listing.
_BARE_VERBS = frozenset(
    {"ship", "lead", "drive", "build", "design", "develop", "deliver", "own",
     "manage", "create", "improve", "reduce", "scale", "maintain", "support",
     "write", "test", "deploy", "automate", "optimize", "optimise", "migrate",
     "refactor", "architect", "mentor", "run", "grow", "launch", "partner",
     # Irregular past tenses, which the -ed suffix rule cannot catch.
     "led", "built", "wrote", "drove", "grew", "ran", "oversaw", "rebuilt",
     "troubleshot", "took", "made", "set", "met", "kept", "held", "brought",
     "taught", "sought", "began", "chose", "cut", "put", "sent", "won"}
)


def _looks_like_verb(word: str) -> bool:
    """Whether a capitalised word is plausibly an action verb."""
    w = word.lower().rstrip(".,;:")
    if w in _BARE_VERBS or w in {s.lower() for s in _STOPWORDS} or w.endswith(("ed", "ing")):
        return True
    # Third-person present, which some resume styles use throughout:
    # "Ships REST APIs weekly", "Owns the release pipeline".
    return w.endswith("s") and w[:-1] in _BARE_VERBS


def _is_technology(word: str) -> bool:
    """Whether a word is recognisably the name of a technology.

    This is what withholds the sentence-start exemption from a claim that
    matters. Enumerating ordinary English is hopeless - prose kept producing
    new word classes ("Separately", "Cross-functionally", "At") - but
    recognising a *technology* is tractable, and a fabricated technology is
    the thing worth catching.
    """
    from jobhunt.resume.vocab import TECH_VOCAB

    w = word.lower().rstrip(".,;:")
    if w in TECH_VOCAB:
        return True
    bare = word.rstrip(".,;:")
    if not bare or _looks_like_verb(bare):
        return False
    # Shapes English words do not have: internal capitals, digits, or the
    # +/# suffixes ("PostgreSQL", "Node.js", "C++", "K8s").
    return bool(
        re.search(r"[A-Z0-9].*[A-Z]", bare[1:] + bare[:1])
        or re.search(r"\d", bare)
        or bare.endswith(("+", "#"))
    )


def _normalise_number(n: str) -> str:
    return n.rstrip("+%").rstrip("0").rstrip(".") if "." in n else n.rstrip("+%")


_SENTENCE_BREAK = re.compile(r"(?:^|(?<=[.!?;:])\s+|\n+|^\s*[-•*]\s*)", re.MULTILINE)


def _sentence_start_offsets(text: str) -> set[int]:
    """Character offsets where a new sentence or bullet begins."""
    offsets = {0}
    for m in _SENTENCE_BREAK.finditer(text):
        offsets.add(m.end())
    # Also treat the first word after a bullet marker as sentence-initial.
    for m in re.finditer(r"[-•*]\s*", text):
        offsets.add(m.end())
    return offsets
