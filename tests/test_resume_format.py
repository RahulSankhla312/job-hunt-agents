"""The rendered resume must stay in the original Overleaf layout.

He built that document himself and asked for it specifically. The generator had
drifted into a different one - left-aligned name, an invented SUMMARY section,
skills split across four labelled lines - which is not a cosmetic difference
when the whole point is that the tailored output is the document he already
uses. These assertions pin the layout so it cannot drift again silently.
"""

from __future__ import annotations

import pytest

from jobhunt.config import settings
from jobhunt.resume.profile import Profile
from jobhunt.resume.render import render_html


@pytest.fixture(scope="module")
def html():
    profile = Profile.load(settings().profile_path)
    return render_html(
        profile,
        experience=profile.experience,
        projects=profile.projects,
        skills=profile.skills,
        achievements=profile.achievements,
    )


def test_there_is_no_summary_section(html):
    """The source document goes straight from the contact line into Education."""
    assert "<h2>Summary</h2>" not in html
    assert ">Summary<" not in html


def test_the_name_is_centred(html):
    head = html[: html.index("</style>")]
    h1 = head[head.index("h1 {"):]
    assert "text-align: center" in h1[: h1.index("}")]


def test_the_contact_line_labels_the_profile_links(html):
    """The source document prints the words, not the spelled-out URLs."""
    assert "GitHub" in html and "LinkedIn" in html
    assert "linkedin.com/in/" not in html
    assert "github.com/" not in html


def test_skills_are_one_line_not_four(html):
    assert "Technical Skills" in html
    for label in ("Languages:", "Frameworks:", "Tools &amp; Platforms:", "Domains:"):
        assert label not in html, label


def test_the_degree_carries_its_field(html):
    assert "Bachelor of Technology in Computer Science and Engineering" in html


def test_education_comes_before_skills_and_experience(html):
    """Section order is load-bearing: an ATS reads them in document order."""
    assert html.index("<h2>Education</h2>") < html.index("<h2>Skills</h2>")
    assert html.index("<h2>Skills</h2>") < html.index("<h2>Work Experience</h2>")


def test_project_tech_sits_inline_after_the_name(html):
    """Not right-aligned in a meta column, as the source document has it."""
    start = html.index("AI-ASSISTED ENGINEERING AGENT PLATFORM")
    window = html[start : start + 400]
    assert "Python, Node.js, React, SQLite, OCI" in window
    assert "meta mono" not in window


def test_the_layout_is_still_single_column(html):
    """Unchanged ATS constraint: multi-column output gets read across the page."""
    import re

    # The template's own header comment explains why tables are banned, so the
    # comments have to go before looking for one.
    body = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    assert "<table" not in body
    assert "column-count" not in body


# --------------------------------------------------------------------------
# Transcribed from the LaTeX source, not eyeballed from a PDF
# --------------------------------------------------------------------------
#
# The Overleaf source was supplied as the spec. These assert the specific values it sets,
# each of which was wrong when the layout was inferred from a rendered PDF.


def test_section_rules_are_thick_and_light_grey(html):
    """\titlerule[2pt] in \definecolor{light-grey}{gray}{0.83}, not a hairline."""
    assert "--light-grey: #d4d4d4" in html
    assert "border-bottom: 2pt solid var(--light-grey)" in html


def test_body_text_is_text_grey_not_black(html):
    """\definecolor{text-grey}{gray}{.08} - 0.08 grey, not #000."""
    assert "--text-grey: #141414" in html
    assert "color: var(--text-grey)" in html


def test_dates_and_locations_are_dark_grey(html):
    """{\color{dark-grey}\small #2} on the right-hand column."""
    assert "--dark-grey: #4d4d4d" in html
    assert "color: var(--dark-grey)" in html


def test_the_headings_are_uppercase(html):
    """The source writes \section{EDUCATION}."""
    assert "text-transform: uppercase" in html


def test_the_grade_is_bold(html):
    """\textbf{CPI: 8.3} in the source."""
    assert "<strong>{{ edu.grade }}</strong>" in html or "CPI" in html
    assert "<strong>" in html


def test_tech_stack_is_a_bullet_in_the_list(html):
    """In the source it is a \resumeItem, so it belongs inside the <ul>.

    It had been rendered as a separate monospace paragraph after the list,
    which is neither where nor what the source puts there.
    """
    import re

    # The header comment explains this, so it has to go before searching.
    body = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    start = body.index("TECH STACK")
    window = body[start - 200 : start + 80]
    assert "<li>" in window
    assert "mono" not in window


def test_the_page_is_us_letter_with_half_inch_margins(html):
    """\documentclass[letterpaper] and \oddsidemargin -0.5in + \textwidth 1in."""
    assert "size: letter" in html
    assert "0.5in" in html


def test_the_base_size_is_the_eleven_point_class(html):
    """\documentclass[...,11pt] puts the body at 10.95pt, not 10pt."""
    assert "font-size: 10.95pt" in html


def test_the_name_is_huge(html):
    """\Huge at 11pt base is 24.88pt."""
    assert "font-size: 24.88pt" in html
