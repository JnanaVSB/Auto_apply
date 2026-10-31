"""The resume LaTeX: every special character escaped, LaTeX injection printed as text, links made safe, and the
template's layout, with made-up people only."""

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
