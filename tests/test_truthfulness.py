"""Truthfulness validator.

This is the guard that stops a tailored resume claiming something the candidate never
did. A false negative here means walking into an interview behind a fabricated
bullet, so the fabrication cases matter more than anything else in the suite.
"""

from __future__ import annotations

import pytest

from jobhunt.resume.profile import Experience, Profile, Project, Skills

PROFILE = Profile(
    experience=[
        Experience(
            company="Northwind",
            title="Software Engineer",
            location="Bengaluru, India",
            start="July 2025",
            end="Present",
            bullets=[
                "Delivered five full-stack integrations using React, TypeScript, Java "
                "and Spring Boot.",
                "Diagnosed and resolved 87 defects in one year, including 9 long-pending issues.",
                "Engineered a GitLab CI/CD pipeline replacing 20+ manual commands.",
            ],
            tech=["React", "TypeScript", "Java", "Spring Boot"],
        ),
        Experience(
            company="Contoso",
            title="Technical Intern",
            bullets=["Integrated Kerberos authorization, reducing unauthorized access by 90%."],
            tech=["Kotlin", "SQLite"],
        ),
    ],
    projects=[
        Project(
            name="AI-Assisted Engineering Agent Platform",
            tech=["Python", "Node.js", "React"],
            bullets=["Built hybrid code retrieval with RAG and approval-gated tool execution."],
        )
    ],
    skills=Skills(
        languages=["Python", "Java", "TypeScript"],
        frameworks=["React", "Spring Boot", "Node.js"],
        tools=["Playwright", "Cypress", "Docker", "Kubernetes", "GitLab"],
        domains=["AI Agents", "RAG", "Vector Embeddings"],
    ),
)


@pytest.fixture(scope="module")
def facts():
    return PROFILE.facts()


# --- must pass -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Diagnosed and resolved 87 defects in one year, including 9 long-pending issues.",
        "Resolved 87 production defects across frontend, backend and CI/CD at Northwind.",
        "Spearheaded five full-stack integrations using React and Spring Boot.",
        "Shipped a GitLab CI/CD pipeline that replaced 20+ manual commands.",
        "Reduced unauthorized access by 90% at Contoso using Kerberos.",
        "Built RAG pipelines with Vector Embeddings in Python.",
        # Regression: slash-joined compounds were flagged as invented entities
        # even though both halves are in the profile. This blocked every
        # tailored resume the model produced.
        "Delivered integrations using React/TypeScript and Java/Spring Boot.",
        "Ship Playwright/Cypress automation in GitLab CI/CD.",
        # Regression: the token regex allowed a bare dot, so a sentence-ending
        # period glued the following sentence on as one entity.
        "Containerised services with Docker and Kubernetes.\nEngineered a pipeline.",
        "Built services in Node.js and Spring Boot.",
    ],
)
def test_faithful_rewrites_pass(facts, text):
    """The Tailor may rephrase freely, including with new action verbs."""
    assert facts.check(text) == [], f"false positive on: {text}"


# --- must fail -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("Led a team of engineers at Google on distributed systems.", "Google"),
        ("Built microservices using Rust and Kafka at Northwind.", "Rust"),
        ("Holds a Master of Science from Stanford University.", "Stanford"),
        ("Diagnosed and resolved 940 defects in one year.", "940"),
        ("Reduced unauthorized access by 99% at Contoso.", "99"),
        ("Managed a team of 15 engineers.", "15"),
    ],
)
def test_fabrications_are_caught(facts, text, expect):
    violations = facts.check(text)
    assert violations, f"FABRICATION SLIPPED THROUGH: {text}"
    assert any(expect in v for v in violations), f"{expect!r} not flagged in {violations}"


def test_sentence_initial_verb_is_not_an_entity(facts):
    """Regression: 'Resolved ...' was flagged as a fabricated proper noun."""
    assert facts.check("Resolved 87 defects.") == []


def test_midsentence_unknown_proper_noun_is_still_caught(facts):
    """The sentence-start exemption must not become a loophole."""
    assert facts.check("Worked at Google.") != []


def test_empty_text_is_clean(facts):
    assert facts.check("") == []


def test_bullet_opening_with_a_fabricated_technology_is_caught(facts):
    """The sentence-start exemption is for action verbs, not a blanket pass.

    Resume bullets often open with a technology name, so exempting every
    sentence-initial word would let an invented one through unexamined.
    """
    assert facts.check("Rust microservices shipped at Northwind.") != []
    assert facts.check("Kafka pipelines built for Northwind.") != []


def test_bullet_opening_with_a_bare_verb_is_still_exempt(facts):
    assert facts.check("Ship Playwright automation weekly.") == []
    assert facts.check("Build RAG pipelines in Python.") == []


@pytest.mark.parametrize(
    "text",
    [
        # Regression: hyphenated compounds were read as one invented entity.
        # The lowercase half is an adjective, not a claim.
        "Built AI-driven tooling for Northwind.",
        "Delivered agent-based workflows in Python.",
        # Regression: irregular past tenses are not caught by the -ed rule.
        "Troubleshot production defects across CI/CD.",
        "Led delivery of five integrations at Northwind.",
        "Wrote Playwright suites for Northwind.",
    ],
)
def test_compound_and_irregular_verb_forms_pass(facts, text):
    assert facts.check(text) == [], f"false positive on: {text}"


# --- prose: cover letters and application answers ---------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Regression: every one of these rejected a correctly-written cover
        # letter. Prose capitalises by grammar far more than a resume bullet
        # does, and each was a different word class.
        "At Northwind I shipped five full-stack integrations.",
        "That system now runs Playwright suites on every merge.",
        "For this role the relevant work is the RAG tooling.",
        "Separately, I built six AI-assisted SDLC skills.",
        "Cross-functionally, I worked with product and platform teams.",
        "Having shipped those integrations, the next step is clear.",
        "Given the scope, I owned delivery end to end.",
    ],
)
def test_prose_capitalisation_is_not_a_fabrication(facts, text):
    assert facts.check(text) == [], f"false positive on: {text}"


@pytest.mark.parametrize(
    "text",
    [
        # The exemption must not cover an invented technology, even when it
        # opens the sentence - that is exactly the claim worth catching.
        "Rust services at Northwind handled the load.",
        "Kafka pipelines fed the Northwind platform.",
        "PostgreSQL tuning was my responsibility at Northwind.",
    ],
)
def test_sentence_initial_technology_is_still_checked(facts, text):
    assert facts.check(text) != [], f"FABRICATION SLIPPED THROUGH: {text}"
