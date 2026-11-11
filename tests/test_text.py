from __future__ import annotations

from legistutor import text as tx

STANDARD = """Item Description:
..title
Set maximum 2027 property tax levy
..end

Resolution:
..body
BE IT RESOLVED, that the levy be adopted; and

BE IT FURTHER RESOLVED, that it be certified.
..end

Background:
Pursuant to Minnesota Statutes, section 275.065, subdivision 1, counties must act.

Current Request: adopt the levy.

..recommendation
Recommendation from County Administrator: Recommend Approval
"""


def test_standard_sections():
    sections = tx.split_sections(STANDARD)
    assert list(sections) == ["title", "resolution", "background", "recommendation"]
    assert sections["title"] == "Set maximum 2027 property tax levy"
    assert sections["resolution"].startswith("BE IT RESOLVED") and sections["resolution"].endswith("certified.")
    assert "Current Request: adopt the levy." in sections["background"]
    assert tx.administrator_recommendation(sections["recommendation"]) == "Recommend Approval"


def test_resolution_after_an_empty_body_block():
    plain = "Item Description:\n..title\nBudget\n..end\n\nResolution:\n..body\n..end\nBE IT RESOLVED, that x.\n\nBackground:\nWhy."
    assert tx.split_sections(plain) == {"title": "Budget", "resolution": "BE IT RESOLVED, that x.", "background": "Why."}


def test_whereas_preamble_glued_to_directive():
    plain = "Item Description:\n..title\nZero waste\n..end\n\nWHEREAS:..body\nWHEREAS, a; and\nResolution:\nBE IT RESOLVED, b."
    sections = tx.split_sections(plain)
    assert sections["resolution"] == "WHEREAS, a; and\nBE IT RESOLVED, b."
    assert "other" not in sections


def test_unmarked_proclamation_and_inline_background():
    plain = "Item Description:\n..title\nProclamation\n..end\n\nWHEREAS, the land; and\n\nBackground: Some history."
    assert tx.split_sections(plain) == {
        "title": "Proclamation",
        "resolution": "WHEREAS, the land; and",
        "background": "Some history.",
    }


def test_title_only_and_unmarked_text():
    assert tx.split_sections("Item Description:\n..title\nMinutes\n..end") == {"title": "Minutes"}
    assert tx.split_sections("just some words") == {"other": "just some words"}
    assert tx.split_sections(None) == {}


def test_placeholder_recommendation_is_dropped():
    assert tx.administrator_recommendation("Recommendation from County Administrator: Choose an item.") is None
    assert tx.administrator_recommendation("") is None


def test_statute_citation_forms():
    cases = {
        "Pursuant to Minnesota Statutes, section 275.065, subdivision 1, all counties": ["275.065"],
        "under Minn. Stat. § 383B.158 and other law": ["383B.158"],
        "Minnesota Statutes §275.07.": ["275.07"],
        "Minnesota Statute 375.18": ["375.18"],
        "MINNESOTA STATUTES, CHAPTER 462C, AS AMENDED": ["462C"],
        "Minnesota Statutes, Chapters 462C and 469": ["462C", "469"],
        "Minnesota Statutes, Sections 469.001 to 469.047": ["469.001", "469.047"],
        "Minnesota Statutes, sections 383B.80 and 383B.81": ["383B.80", "383B.81"],
        "Minnesota Statutes, Section 373.40 and Chapter 475": ["373.40", "475"],
        "Minnesota Statutes 2022, section 13D.021": ["13D.021"],
        "M.S. 373": ["373"],
    }
    for text, expected in cases.items():
        assert [number for number, _ in tx.extract_statute_citations(text)] == expected, text


def test_statute_citations_ignore_non_citations():
    assert tx.extract_statute_citations("Minnesota Statutes, as detailed in Contract Report 2308") == []
    assert tx.extract_statute_citations("Laws of Minnesota 2023, Chapter 52") == []
    # a count after a dotted section is not another section
    assert [n for n, _ in tx.extract_statute_citations("Minn. Stat. § 383B.77 and 3 agreements")] == ["383B.77"]


def test_citation_text_is_kept_and_deduplicated():
    found = tx.extract_statute_citations("Minn. Stat. 471.59 ... later, Minnesota Statutes,\nsection 471.59 again")
    assert found == [("471.59", "Minn. Stat. 471.59")]
    assert tx.statute_chapter("383B.158") == "383B"


def test_file_numbers():
    text = "Rescind Resolution 26-0319; see 26-0333 R1, 23N-0018, 24-HCRRA-0003 and 23-HHS-0041R1. Not TMP-26-0917 or PW 36-44-26."
    assert tx.extract_file_numbers(text) == ["26-0319", "26-0333R1", "23N-0018", "24-HCRRA-0003", "23-HHS-0041R1"]
    assert tx.base_file_number("25-0312 R1 S1") == "25-0312"
    assert tx.base_file_number("26-0333") == "26-0333"


def test_rtf_notes_become_plain_text():
    rtf = r"{\rtf1\ansi\deff0{\fonttbl{\f0\fnil Arial;}}\pard Several individuals commented:\par Eddie Ellem\par}"
    assert tx.rtf_to_text(rtf) == "Several individuals commented:\nEddie Ellem"
    assert tx.rtf_to_text("Special meeting") == "Special meeting"
    assert tx.rtf_to_text(None) == ""


def test_clean_normalises_odd_spaces():
    assert tx.clean("Convey surplus property\r\n\r\n\r\n\r\nnow ") == "Convey surplus property\n\nnow"


def test_chunk_text_keeps_short_sections_whole():
    assert tx.chunk_text(None) == [] and tx.chunk_text("  ") == []
    assert tx.chunk_text("One paragraph.\n\nAnother.", max_chars=100) == ["One paragraph.\n\nAnother."]
    slightly_over = "word " * 23  # 115 chars: within the 25% allowance
    assert len(tx.chunk_text(slightly_over, max_chars=100)) == 1


def test_chunk_text_splits_between_paragraphs_into_even_passages():
    paragraphs = [f"Paragraph {i} " + "x" * 80 for i in range(10)]  # about 930 chars in all
    chunks = tx.chunk_text("\n".join(paragraphs), max_chars=400)
    assert len(chunks) == 3
    assert all(len(chunk) <= 400 for chunk in chunks)
    assert max(map(len, chunks)) - min(map(len, chunks)) < 200  # no orphaned tail
    assert "\n".join(chunks).split("\n") == paragraphs  # nothing lost, nothing split mid-paragraph


def test_chunk_text_breaks_a_giant_paragraph_at_sentences_then_words():
    sentences = "WHEREAS, the county finds that " + "; and WHEREAS, the county further finds that ".join(["x" * 60] * 12)
    chunks = tx.chunk_text(sentences, max_chars=300)
    assert all(len(chunk) <= 300 for chunk in chunks) and len(chunks) >= 4
    assert all(chunk.rstrip().endswith((";", "x")) for chunk in chunks)
    unbroken = tx.chunk_text("y" * 700 + " " + "z" * 500, max_chars=300)
    assert all(len(chunk) <= 300 for chunk in unbroken)
    assert "".join(unbroken).replace("\n", "").replace(" ", "") == "y" * 700 + "z" * 500
