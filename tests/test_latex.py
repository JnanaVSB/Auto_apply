"""The resume LaTeX: every special character escaped, LaTeX injection printed as text, links made safe, and the
template's layout, with made-up people only."""

import re
from pathlib import Path

import pytest
from test_tiles import ARTICLE, JAKES, RESUME_CLS

from app.documents import latex
from app.documents.tiles import Tile, read_tiles

CONTACT = {
    "full_name": "Alex Example", "email": "alex_ex@example.com", "phone": "(555) 010-0199", "city": "Phoenix",
    "state": "AZ", "github": "https://github.com/alex-example", "linkedin": "javascript:alert(1)",
}  # fmt: skip


@pytest.mark.parametrize(
    ("text", "escaped"),
    [
        ("\\", r"\textbackslash{}"), ("{", r"\{"), ("}", r"\}"), ("$", r"\$"), ("&", r"\&"), ("#", r"\#"),
        ("_", r"\_"), ("%", r"\%"), ("^", r"\textasciicircum{}"), ("~", r"\textasciitilde{}"), ("[", "{[}"),
        ("]", "{]}"), ("40% of $5 & more", r"40\% of \$5 \& more"), ("café — naïve", "café — naïve"),
        ("a\x00b\x1fc\nd", "abc d"),  # invisible characters removed, a line break is a space
    ],
)  # fmt: skip
def test_every_special_character_is_escaped(text, escaped):
    assert latex.escape(text) == escaped


def test_latex_in_a_bullet_is_printed_never_run():
    attack = r"\input{/etc/passwd} \write18{touch x} \end{document} \\[2pt]"
    tile = Tile("exp-0001", "experience", "Work", {"name": "Fake Corp"}, (attack,))
    source = latex.resume(CONTACT, [tile])
    assert r"\item \textbackslash{}input\{/etc/passwd\} \textbackslash{}write18\{touch x\}" in source
    assert r"\input{" not in source and r"\write18" not in source and source.count(r"\end{document}") == 1


def test_links_are_made_safe_and_only_web_links_are_shown():
    assert latex.href("https://example.com/a b?x=1&y=2#top~") == r"https://example.com/a\%20b?x=1\&y=2\#top\%7E"
    assert latex.href("https://example.com/}{\\evil") == r"https://example.com/\%7D\%7B\%5Cevil"
    source = latex.resume(CONTACT, [])
    assert r"\href{https://github.com/alex-example}{github.com/alex-example}" in source
    assert "javascript" not in source  # a link that isn't http(s) is left out


def test_the_header_comes_from_the_questionnaire():
    source = latex.resume(CONTACT, [])
    assert r"{\LARGE\bfseries Alex Example}" in source
    assert r"alex\_ex@example.com\enspace\textbullet\enspace (555) 010-0199\enspace\textbullet\enspace Phoenix, AZ" in (
        source
    )


def test_the_layouts_of_the_three_resume_styles():
    tiles = [*read_tiles(RESUME_CLS).tiles, *read_tiles(JAKES).tiles, *read_tiles(ARTICLE).tiles]
    source = latex.resume(CONTACT, tiles)
    assert source.count(r"\resumesection{Technical Skills}") == 2  # one heading for tiles in a row, per resume
    assert r"\textbf{ML \& Robotics:} Fake learning, pretend vision\par" in source
    assert r"\textbf{Example State University} --- M.S. Pretend Robotics, Fake Concentration\hfill May 2027\par" in (
        source
    )
    assert r"\textit{ML Intern}\hfill \textit{Phoenix, AZ}\par" in source
    assert "Made-up engineer who builds pretend robots.\\par" in source  # a summary is a paragraph
    publications = source.split(r"\resumesection{Publications}")[1].split(r"\resumesection")[0]
    assert publications.count(r"\item") == 2 and publications.count(r"\begin{itemize}") == 1


def test_a_summary_is_always_a_paragraph_whatever_its_name_box_holds():
    named = Tile("sum-1", "summary", "Summary", {"name": "Alex Example"}, ("Made-up engineer who builds robots.",))
    body = latex.resume(CONTACT, [named]).split(r"\resumesection{Summary}")[1]
    assert body.lstrip().startswith("Made-up engineer who builds robots.\\par") and "Alex Example" not in body


SPECIAL = "\\{}$&#_%^~[]"
BREAK = "\\allowbreak{}%\n"
ATTACKS = (r"\input{/etc/passwd}", r"\write18{touch pwned}", r"\end{document}", r"\immediate", r"\def\x{y}",
           "\\\\[2pt]", "<< x >>", "<% x %>", "<# x #>", "<< 1 / 0 >>", "<% include 'resume.tex' %>", "%", "$$", "^^M",
           "~")  # fmt: skip


def body(source):
    return source.split(r"\begin{document}")[1].split(r"\end{document}")[0]


def commands(source):
    return set(re.findall(r"\\[A-Za-z@]+", body(source)))


@pytest.mark.parametrize("character", SPECIAL)
def test_a_report_escapes_every_special_character_wherever_it_stands(character):
    safe = latex.escape(character)
    source = latex.report(f"{character}title", [(f"{character}heading", f"{character}line\nmid{character}dle")])
    assert all(f"{safe}{word}" in source for word in ("title", "heading", "line"))
    assert f"mid{safe}dle" in source


@pytest.mark.parametrize("character", SPECIAL)
@pytest.mark.parametrize("before", [38, 39, 40, 41])
def test_a_cut_never_splits_an_escape(character, before):
    word = "a" * before + character + "b" * 100
    pieces = [word[start : start + 40] for start in range(0, len(word), 40)]
    expected = BREAK.join(latex.escape(piece) for piece in pieces)
    for source in (latex.report(word, []), latex.report("T", [(word, "")]), latex.report("T", [("", word)])):
        assert expected in source


@pytest.mark.parametrize("attack", ATTACKS)
def test_nothing_in_a_report_s_text_can_be_a_command(attack):
    plain = latex.report("Title", [("Heading", "Line")])
    assert commands(latex.report(attack, [(attack, f"{attack}\n{attack} words")])) <= commands(plain) | {
        r"\textbackslash", r"\textasciicircum", r"\textasciitilde", r"\textquotesingle", r"\textquotedbl",
        r"\textasciigrave",
    }  # fmt: skip
    assert latex.report(attack, []).count(r"\end{document}") == 1


@pytest.mark.parametrize("delimiters", ["<< x >>", "<% x %>", "<# x #>"])
def test_the_template_s_own_marks_in_a_text_print_as_text(delimiters):
    assert "".join(latex.escape(delimiters).split()) in plain(latex.report("T", [("", f"see {delimiters} here")]))


def test_text_marked_as_safe_elsewhere_is_escaped_here_all_the_same():
    attack = latex.Raw(r"\input{x}")
    assert r"\input" not in latex.report(attack, [(attack, attack)])


@pytest.mark.parametrize(("length", "breaks"), [(40, 0), (41, 1), (80, 1), (81, 2), (3000, 74)])
@pytest.mark.parametrize("letter", ["Q", "字", "😀", "é"])
def test_a_long_word_gets_a_place_to_break_every_40_characters(length, breaks, letter):
    source = latex.report("T", [("", letter * length)])
    assert source.count(BREAK) == breaks and source.count(letter) == length
    assert "\n\n" not in body(source).strip()  # a blank line would end the paragraph inside the word


@pytest.mark.parametrize(
    "text",
    [
        "word " * 60_000,
        "x" * 300_000,
        "\\" * 300_000,
        "字" * 300_000,
        "😀" * 300_000,
        "a-" * 150_000,
        "%$&#_^~{}[]" * 30_000,
    ],
)
def test_no_line_of_a_report_comes_near_what_tectonic_can_read(text):
    """Tectonic can't read a source line of over 200,000 bytes, and a paragraph on one line easily is one."""
    source = latex.report(text, [(text, text), ("", f"{text}\n{text}")])
    assert max(len(line.encode()) for line in source.splitlines()) < 1000


def test_only_words_too_long_get_breaks():
    source = latex.report("T", [("", "short " * 30 + "x" * 41 + " short")])
    assert source.count(BREAK) == 1 and "short\nshort" in source


def test_control_characters_go_before_the_cut():
    assert "a" * 30 + "b" * 10 + BREAK + "b" * 10 in latex.report("T", [("", "a" * 30 + "\x00" * 20 + "b" * 20)])


@pytest.mark.parametrize(
    "nothing", ["", " ", "   \n  \n", "\r\n\r\n", "\x00\x07", "\u2028\u2029", "\t", "\x0c", " \x00 \n"]
)
def test_a_text_of_nothing_makes_no_paragraph(nothing):
    assert latex.report("T", [("H", nothing)]) == latex.report("T", [("H", "")])


@pytest.mark.parametrize("line_end", ["\n", "\r\n", "\r", "\x0c", "\x0b", "\x85", "\u2028", "\u2029", "\n\n\n"])
def test_each_kind_of_line_end_makes_its_own_paragraph(line_end):
    assert re.search(r"first\\par\s+second\\par", latex.report("T", [("", f"first{line_end}second")]))


def test_an_ad_s_lines_stay_lines():
    source = latex.report("T", [("", "We offer:\n- pay\n- lunch\n\nApply now.")])
    assert re.search(r"We\s+offer:\\par\s+-\s+pay\\par\s+-\s+lunch\\par\s+Apply\s+now\.\\par", source)


def test_a_lone_surrogate_never_stops_the_file_being_written():
    source = latex.report("\ud800Title", [("\udfffHeading", "a\ud800b\n\ud800")])
    assert source.encode() and "ab" in source and "Title" in source and "Heading" in source


def test_a_report_with_no_sections_or_no_title_is_still_a_document():
    for source in (latex.report("T", []), latex.report("", []), latex.report("", [("", "")])):
        assert source.count(r"\begin{document}") == source.count(r"\end{document}") == 1


def test_a_heading_stays_with_its_text_but_headings_with_none_can_part():
    with_text, alone = latex.report("T", [("Heading", "Line")]), latex.report("T", [("Heading", "")])
    assert r"\nopagebreak" in with_text and r"\nopagebreak" not in alone
    assert r"\textbf{Heading}" in alone


def test_a_section_with_no_heading_prints_no_empty_bold():
    assert body(latex.report("T", [("", "Line")])).count(r"\textbf") == 1  # the title's


@pytest.mark.parametrize(
    ("count", "line"),
    [(0, None), (1, "1 kind of character could not be shown here and was left out."),
     (3, "3 kinds of character could not be shown here and were left out.")],
)  # fmt: skip
def test_the_closing_line_says_how_many_kinds_of_character_were_left_out(count, line):
    source = latex.report("T", [("", "Line")], count)
    assert "left out" not in source if line is None else source.index("Line") < source.index(line)


def test_a_report_needs_nothing_the_cover_letter_doesnt_load():
    folder = Path(latex.__file__).parent / "templates"
    packages = [set(re.findall(r"\\usepackage(?:\[.*?\])?\{(.*?)\}", (folder / name).read_text(encoding="utf-8")))
                for name in ("report.tex", "cover-letter.tex")]  # fmt: skip
    assert packages[0] <= packages[1]


EXACT = (
    '"', "'", "`", "--", "---", "----", "``", "''", ",,", "<<", ">>", "?`", "!`", '""', "'''", "```", ",,,", "<<<",
    ">>>",
)  # fmt: skip
EXACT_WORDS = (
    "--version", "it's", "`code`", "a<<b>>c", "1,,2", 'say "hi"', '\\"{x}', '{"a": "b"}', "-" * 100, "a--", "--a",
    "“curly” ‘quotes’ – en — em", "rock 'n' roll", "x" * 39 + "--" + "y" * 39,
)  # fmt: skip


def plain(source):
    """What a report's body says once its marks are read back, with its source lines taken out."""
    words = "".join(body(source).split()).replace(r"\allowbreak{}%", "")
    back = {r"\textquotedbl{}": '"', r"\textquotesingle{}": "'", r"\textasciigrave{}": "`", r"\textbackslash{}": "\\"}
    for mark, text in (*back.items(), ("{}", ""), (r"\{", "{"), (r"\}", "}")):
        words = words.replace(mark, text)
    return words


@pytest.mark.parametrize("marks", EXACT)
def test_no_marks_are_left_for_latex_to_turn_into_another_character(marks):
    source = latex.report(f"a{marks}b{marks}", [(f"{marks}c", f"{marks}\nd{marks}e")])
    assert not re.findall(r"""["'`]|--|,,|<<|>>""", body(source))


@pytest.mark.parametrize("text", [*EXACT, *EXACT_WORDS])
def test_marks_in_a_report_read_back_as_typed(text):
    assert "".join(text.split()) in plain(latex.report("T", [("", text)]))


def test_a_word_cut_between_two_hyphens_stays_two_hyphens():
    assert "x" * 39 + "-" + BREAK + "-y" in latex.report("T", [("", "x" * 39 + "--y")])


def test_escapes_and_marks_side_by_side_stay_apart():
    assert r"\textbackslash{}\textquotedbl{}\{\textquotedbl{}" in latex.report("T", [("", '\\"{"')])


def test_the_resume_and_the_letter_are_printed_as_before():
    text = 'it\'s -- "good" `x` <<y>> ,,z'
    assert latex.escape(text) == text
    letter = latex.cover_letter(CONTACT, "ML", "Acme", "5 Nov", [text])
    assert text in letter and r"\textquote" not in letter and r"\textasciigrave" not in letter


def test_nothing_after_the_rule_can_be_read_as_part_of_it():
    assert "\\hrule\\relax\n" in latex.report("T", [("", "Height")])
