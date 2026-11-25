"""The page's colors and structure against the Hennepin County Design System's accessibility rules.

The county standard is WCAG 2.1 AA: text at least 4.5:1 against its background, and the edges of
controls and focus rings at least 3:1. The colors are read from the CSS variables in index.html, in
both the light and the dark scheme, so a change to the palette that breaks contrast fails here.
"""

from __future__ import annotations

import re
from importlib import resources

import pytest

PAGE = resources.files("legistutor").joinpath("static", "index.html").read_text(encoding="utf-8")

# (foreground, background, minimum ratio). Text is 4.5:1; borders and focus rings are 3:1.
TEXT = 4.5
UI = 3.0
PAIRS = [
    *[(ink, bg, TEXT) for ink in ("--ink", "--ink-2", "--muted", "--link", "--visited", "--heading", "--error-ink")
      for bg in ("--page", "--surface")],
    ("--ink", "--wash", TEXT), ("--ink-2", "--wash", TEXT),
    ("--on-brand", "--brand", TEXT), ("--on-brand", "--brand-hover", TEXT),
    ("--on-header", "--header", TEXT),
    ("--ink", "--warn-bg", TEXT), ("--ink", "--error-bg", TEXT), ("--ink", "--info-bg", TEXT), ("--ink", "--ins-bg", TEXT),
    ("--surface", "--ink", TEXT),  # the tooltip: page color on ink
    ("--field", "--page", UI), ("--field", "--surface", UI),
    ("--focus", "--page", UI), ("--focus", "--surface", UI),
]


def _variables(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


def _schemes() -> dict[str, dict[str, str]]:
    light = _variables(PAGE[PAGE.index(":root {"): PAGE.index("@media (prefers-color-scheme: dark)")])
    dark_block = PAGE[PAGE.index("@media (prefers-color-scheme: dark)"):]
    dark = {**light, **_variables(dark_block[: dark_block.index("\n  }\n")])}
    return {"light": light, "dark": dark}


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i: i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted([_luminance(a), _luminance(b)], reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_helper_matches_known_values():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)  # the usual lightest AA gray
    assert contrast("#0058a4", "#ffffff") == pytest.approx(7.16, abs=0.01)  # Hennepin blue on white


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg", "minimum"), PAIRS)
def test_colors_meet_wcag_aa(scheme, fg, bg, minimum):
    colors = _schemes()[scheme]
    ratio = contrast(colors[fg], colors[bg])
    assert ratio >= minimum, f"{scheme}: {fg} {colors[fg]} on {bg} {colors[bg]} is {ratio:.2f}:1, needs {minimum}:1"


def test_page_script_parses():
    # One stray bracket stops the whole script: no button works and the page never leaves its first section.
    quickjs = pytest.importorskip("quickjs")
    start = PAGE.index("<script>") + len("<script>")
    script = PAGE[start: PAGE.index("</script>", start)]
    before = PAGE[:start].count("\n")  # the script's line n is the page's line n + before
    try:
        quickjs.Context().eval("(function () {" + script + "\n})")  # compiled, never run
    except quickjs.JSException as error:
        found = re.search(r"<input>:(\d+)", str(error))
        where = f"near line {int(found[1]) + before} of index.html" if found else "in index.html"
        pytest.fail(f"The page's script does not parse, {where}: {str(error).splitlines()[0]}")


def test_page_structure_follows_the_design_system():
    assert '<html lang="en">' in PAGE
    assert PAGE.count("<h1") == 1, "one H1 per page"
    assert 'class="skip" href="#main"' in PAGE and '<main id="main"' in PAGE
    assert "placeholder=" not in PAGE, "fields need visible labels, not placeholder text"
    assert ".disabled =" not in PAGE, "use aria-disabled, not the disabled attribute"
    assert "text-transform:uppercase" not in PAGE.replace(" ", ""), "no all caps"
    assert 'month: "short"' not in PAGE, "dates spell out the month"
