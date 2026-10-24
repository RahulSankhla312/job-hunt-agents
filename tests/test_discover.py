"""Board discovery.

The slug generator has to guess, so the ownership check is what stops a guess
becoming a wrong employer's jobs in the pipeline. Every "must reject" case here
was a real false positive from running discovery over a 131-company list.
"""

from __future__ import annotations

import pytest

from jobhunt.sources.discover import board_belongs_to, slug_candidates

# --------------------------------------------------------------------------
# Slug generation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Scale AI", "scaleai"),
        ("Red Hat", "redhat"),
        ("Remote.com", "remotecom"),
        ("Palo Alto Networks", "paloaltonetworks"),
    ],
)
def test_concatenated_slug_is_tried_first(name, expected):
    assert slug_candidates(name)[0] == expected


def test_corporate_suffixes_are_dropped():
    """"Tekion India Private limited" is just `tekion` on its board."""
    assert "tekion" in slug_candidates("Tekion India Private limited")


def test_a_name_of_only_suffixes_yields_nothing_rather_than_garbage():
    assert slug_candidates("Pvt Ltd") == []


# --------------------------------------------------------------------------
# Ownership - must reject
# --------------------------------------------------------------------------


def test_test_boards_are_rejected():
    """greenhouse/linkedin is "LI Test Company" - 53 postings that are not
    LinkedIn jobs at all."""
    assert not board_belongs_to(
        "LinkedIn", "linkedin", "linkedin LI Test Company greenhouse.io/linkedin/jobs/1"
    )


@pytest.mark.parametrize(
    ("company", "slug", "evidence"),
    [
        # The slug is only the first word of the name, and nothing else in the
        # name shows up - it is a different company that happens to own it.
        ("Air Asia", "air", "air Air job-boards.greenhouse.io/air/jobs/1"),
        ("CASA RETAIL AI", "casa", "casa Casa job-boards.greenhouse.io/casa/jobs/1"),
        ("Human Holdings", "human", "human jobs.ashbyhq.com/human/abc"),
    ],
)
def test_first_word_slug_needs_corroboration(company, slug, evidence):
    assert not board_belongs_to(company, slug, evidence)


# --------------------------------------------------------------------------
# Ownership - must accept
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("company", "slug", "evidence"),
    [
        ("Slice", "slice", "slice Slice slice.careers/careers-listing"),
        ("Meesho", "meesho", "meesho jobs.lever.co/meesho/abc"),
        ("Rubrik", "rubrik", "rubrik Rubrik Job Board rubrik.com/company/careers"),
        # Suffix-only noise removed, so the single significant word matches.
        ("Tekion India Private limited", "tekion", "tekion jobs.ashbyhq.com/tekion/abc"),
        # The concatenated spelling is proof on its own.
        ("Scale AI", "scaleai", "scaleai greenhouse.io/scaleai/jobs/1"),
        # First-word slug, but the rest of the name corroborates it.
        ("Air Asia", "air", "air AirAsia Berhad careers.airasia.com"),
    ],
)
def test_genuine_boards_are_accepted(company, slug, evidence):
    assert board_belongs_to(company, slug, evidence)
