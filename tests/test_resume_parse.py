"""Resume structuring.

The two-column bleed handling is subtle and was wrong twice during
development - once rotating every date by one entry, once swapping title and
company. Both regressions are pinned here.
"""

from __future__ import annotations

import pytest

from jobhunt.resume.heuristic import structure_heuristically

# Mirrors what pdfminer emits for a two-column LaTeX resume: the right column
# (dates, grade) is flushed *after* the left column, so it lands under the
# following heading.
TWO_COLUMN_TEXT = """\
Aarav Mehta

+91 9000012345 | aarav.mehta@example.com | GitHub | LinkedIn

EDUCATION

Indian Institute of Information Technology, Pune
Bachelor of Technology in Computer Science and Engineering
SKILLS

2021 - 2025

CPI: 8.1

Technical Skills : Python, Java, TypeScript, React, Spring Boot, Node.js

WORK EXPERIENCE

Software Engineer
Northwind

July 2025 - Present
Bengaluru, India

• Delivered five full-stack integrations using React, TypeScript and Java.

• Diagnosed and resolved 87 defects in one year.

Technical Intern
Contoso

October 2023 - January 2024
Pune, India

• Built a Kotlin Android application with multi-factor authentication.

• TECH STACK: Kotlin, Java, SQLite, Python

ACHIEVEMENTS

• Won first place at CodeSprint 2024 for Best Product.

• Solved 600+ data-structure and algorithm problems.

PROJECTS

AI-ASSISTED ENGINEERING AGENT PLATFORM | Python, Node.js, React

• Built an AI engineering-agent platform with hybrid code retrieval and RAG.
"""


@pytest.fixture(scope="module")
def profile():
    return structure_heuristically(TWO_COLUMN_TEXT)


def test_identity(profile):
    assert profile.identity.name == "Aarav Mehta"
    assert profile.identity.email == "aarav.mehta@example.com"
    assert "9000012345" in profile.identity.phone


def test_education_keeps_its_own_bled_date_and_grade(profile):
    """The date and CPI appear under SKILLS but belong to EDUCATION."""
    assert len(profile.education) == 1
    edu = profile.education[0]
    assert edu.institution.startswith("Indian Institute of Information Technology")
    assert edu.degree.startswith("Bachelor of Technology")
    assert edu.grade == "CPI: 8.1"
    assert (edu.start, edu.end) == ("2021", "2025")


def test_grade_does_not_leak_into_skills(profile):
    """Regression: 'CPI: 8.1' once became the first skill token."""
    assert not any("8.1" in s or "CPI" in s for s in profile.skills.languages)
    assert "Python" in profile.skills.languages
    assert "Spring Boot" in profile.skills.languages


def test_title_and_company_are_not_swapped(profile):
    """Regression: location was being read as the company."""
    northwind = profile.experience[0]
    assert northwind.title == "Software Engineer"
    assert northwind.company == "Northwind"
    assert northwind.location == "Bengaluru, India"


def test_dates_are_not_rotated_between_entries(profile):
    """Regression: every job inherited the *previous* entry's date range."""
    northwind, contoso = profile.experience[0], profile.experience[1]
    assert (northwind.start, northwind.end) == ("July 2025", "Present")
    assert (contoso.start, contoso.end) == ("October 2023", "January 2024")


def test_tech_stack_bullet_becomes_tech_not_a_bullet(profile):
    contoso = profile.experience[1]
    assert contoso.tech == ["Kotlin", "Java", "SQLite", "Python"]
    assert not any("TECH STACK" in b for b in contoso.bullets)


def test_bullets_are_captured_without_markers(profile):
    northwind = profile.experience[0]
    assert len(northwind.bullets) == 2
    assert northwind.bullets[0].startswith("Delivered five full-stack")
    assert not any(b.startswith("•") for b in northwind.bullets)


def test_projects_and_achievements(profile):
    assert len(profile.projects) == 1
    proj = profile.projects[0]
    assert proj.name == "AI-ASSISTED ENGINEERING AGENT PLATFORM"
    assert proj.tech == ["Python", "Node.js", "React"]
    assert len(proj.bullets) == 1
    assert len(profile.achievements) == 2
