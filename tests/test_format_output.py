"""Tests that the HTML formatter changes presentation only.

Asserts that every word of the markdown appears in the page, that findings keep their numbering,
and that the markup is well-formed. Runs against the real committed reports.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPORTS_DIR = Path(__file__).parent.parent / "reports"
sys.path.insert(0, str(REPORTS_DIR))

from format_output import (  # noqa: E402
    _emphasis,
    build_page,
    missing_words,
    render_markdown,
)

REPORTS = sorted(REPORTS_DIR.joinpath("generated").rglob("*_report.md"))
VOID_ELEMENTS = {"img", "br", "hr", "meta", "link", "input", "source"}


class _Nesting(HTMLParser):
    """Checks every element opened is closed in order, which browsers otherwise repair silently."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes {self.stack[-1] if self.stack else 'nothing'} "
                               f"at line {self.getpos()[0]}")
            while self.stack and self.stack.pop() != tag:
                pass
            return
        self.stack.pop()


def _page(report: Path) -> str:
    # Charts are not embedded here: the base64 payload is irrelevant to the assertions below and
    # inlining three PNGs per report slows the suite down.
    return build_page(report.read_text(encoding="utf-8"), report, embed_images=False)


@pytest.mark.parametrize("report", REPORTS, ids=lambda p: p.stem)
def test_no_word_of_the_report_is_lost(report: Path) -> None:
    """Reformatting drops no content."""
    assert missing_words(report.read_text(encoding="utf-8"), _page(report)) == []


@pytest.mark.parametrize("report", REPORTS, ids=lambda p: p.stem)
def test_page_is_well_formed(report: Path) -> None:
    checker = _Nesting()
    checker.feed(_page(report))
    assert checker.errors == []
    assert checker.stack == []


@pytest.mark.parametrize("report", REPORTS, ids=lambda p: p.stem)
def test_every_section_and_chart_survives(report: Path) -> None:
    """Headings, charts and tables are counted directly, since the word check alone would not
    catch one whose words appear elsewhere."""
    markdown = report.read_text(encoding="utf-8")
    page = _page(report)
    headings = re.findall(r"^## +(.+?)\s*$", markdown, re.MULTILINE)
    assert len(re.findall(r"<h2 ", page)) == len(headings)
    assert page.count("<figure>") == len(re.findall(r"^!\[", markdown, re.MULTILINE))
    assert page.count("<table>") == markdown.count("\n|---")  # every evidence table renders


@pytest.mark.parametrize("report", REPORTS, ids=lambda p: p.stem)
def test_findings_keep_their_own_numbering(report: Path) -> None:
    """The CSS counter starts where the markdown does, so findings are not renumbered."""
    markdown = report.read_text(encoding="utf-8")
    first_numbers = [int(n) for n in re.findall(r"^(\d+)\. ", markdown, re.MULTILINE)[:1]]
    for start, reset in re.findall(r'<ol start="(\d+)" style="counter-reset:item (\d+)"',
                                   _page(report)):
        assert int(reset) == int(start) - 1
    if first_numbers:
        assert f'<ol start="{first_numbers[0]}"' in _page(report)


def test_missing_words_actually_catches_a_loss() -> None:
    """missing_words reports a real omission, so the check can fail."""
    markdown = "# Title\n\nA finding worth keeping.\n"
    assert missing_words(markdown, "<h1>Title</h1><p>A finding worth</p>") == ["keeping."]
    assert missing_words(markdown, "<h1>Title</h1><p>A finding worth keeping.</p>") == []


@pytest.mark.parametrize("markdown, expected", [
    ("**bold**", "<strong>bold</strong>"),
    ("*italic*", "<em>italic</em>"),
    ("***both***", "<em><strong>both</strong></em>"),
    # The real line from training_productivity that crossed its tags under regex emphasis.
    ("**Outlier detection on *output_points***",
     "<strong>Outlier detection on <em>output_points</em></strong>"),
    ("unmatched **open", "unmatched **open"),
    # Column names are full of underscores and none of them are emphasis.
    ("ad_spend_usd vs avg_order_value_usd", "ad_spend_usd vs avg_order_value_usd"),
])
def test_emphasis_pairs_delimiters_correctly(markdown: str, expected: str) -> None:
    assert _emphasis(markdown) == expected


def test_tables_and_code_survive_a_round_trip() -> None:
    markdown = ("| # | analysis | result |\n|---|---|---|\n"
                "| 1 | `compute_correlation(col_a=a, col_b=b)` | r = +0.995 |\n")
    body, _ = render_markdown(markdown)
    assert "<th>analysis</th>" in body
    assert "<code>compute_correlation(col_a=a, col_b=b)</code>" in body
    assert body.count("<tr>") == 2


def test_the_formatter_never_writes_outside_its_output_file(tmp_path: Path) -> None:
    """The formatter imports nothing from the toolkit, the guardrails, the agent or the API."""
    source = Path(__file__).parent.parent / "reports" / "format_output.py"
    text = source.read_text(encoding="utf-8")
    for forbidden in ("import pandas", "from toolkit", "from guardrails", "from agent",
                      "import openai", "generate_report"):
        assert forbidden not in text.replace("generate_report.py", ""), forbidden
