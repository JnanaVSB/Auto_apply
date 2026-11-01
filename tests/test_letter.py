"""The cover letter's texts and the checks on its answers, with a made-up person, entries and job."""

import json
import re

import pytest
from test_privacy import CONTACT
from test_tiles import RESUME_CLS
from test_writer import DRONE, JOB, LAB, PROFILE, SKILLS, labelled

from app.council import letter
from app.council.writer import BadAnswer
from app.documents import latex, privacy

AD = "We build pretend robots in 3 cities."
GOOD = [
    "I would love to join Acme.",
    "At the lab I trained a robot arm with PPO to 90% success.",
    "I hope to hear back.",
]


@pytest.fixture
def shield():
    return privacy.shield(CONTACT, [RESUME_CLS], PROFILE)


def answer(paragraphs=GOOD, cited=("E1",), notes="Robot learning first."):
    return json.dumps({"paragraphs": list(paragraphs), "from": list(cited), "notes": notes})


def test_a_letter_draws_on_the_resumes_entries_relabelled_or_on_all():
    entries = labelled(*PROFILE)
    found = letter.chosen(entries, {("profile", "exp-1"), ("profile", "proj-1")})
    assert [(entry.label, entry.tile.tile_id) for entry in found] == [("E1", "exp-1"), ("E2", "proj-1")]
    assert [entry.label for entry in letter.chosen(entries, None)] == [f"E{number}" for number in range(1, 7)]


def test_the_letter_is_sent_its_own_instructions_and_plain_entries(shield):
    system, user = letter.request(*JOB, labelled(LAB, SKILLS), shield)
    assert system.startswith("You are the Writer: you write the body of a cover letter") and letter.RULES in system
    assert "E1 | experience" in user and "Bullets:\n0: Trained" in user and "Skills:\n- Python" in user
    assert '"bullets": []' not in user and "Alex" not in user and "[LINK]" in user


def test_a_good_letter_is_kept_with_new_numbers_flagged():
    entries = labelled(LAB, DRONE)
    text = answer([*GOOD[:2], "In 3 cities and 12 labs, I would\n help.  Thanks."], ("E2", "E1"), "Short.\x00")
    draft, flags = letter.checked(text, entries, AD)
    assert draft == {
        "paragraphs": [GOOD[0], GOOD[1], "In 3 cities and 12 labs, I would help. Thanks."],
        "from": [{"source": "profile", "tile_id": "proj-1"}, {"source": "profile", "tile_id": "exp-1"}],
        "notes": "Short.",
    }
    assert flags == [{"paragraph": 2, "numbers": ["12"]}]  # 90 is in the lab's bullets, 3 in the ad


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("letter", "not JSON"),
        ("[" * 100_000, "not JSON"),  # nested too deep to read
        (json.dumps({"paragraphs": GOOD, "from": ["E1"]}), 'exactly the fields "paragraphs", "from" and "notes"'),
        (answer(GOOD[:2]), "3 to 5 paragraphs"),
        (answer(GOOD * 2), "3 to 5 paragraphs"),
        (answer(notes=["x"]), '"notes" must be text'),
        (answer(cited=()), '"from" must list the labels'),
        (answer(cited=("E9",)), '"from" must list the labels'),
        (answer(cited=("E1", "E1")), "each once"),
        (answer(cited=(1,)), '"from" must list the labels'),
        (answer([GOOD[0], 7, GOOD[2]]), "paragraph 2 must be text"),
        (answer([GOOD[0], "  ", GOOD[2]]), "paragraph 2 is empty"),
        (answer([GOOD[0], "x" * 2001, GOOD[2]]), "far too long"),
        (answer([GOOD[0], "Hi\x07there", GOOD[2]]), "invisible characters"),
        (answer([GOOD[0], "I am [ME].", GOOD[2]]), "paragraph 2 uses a placeholder"),
        (answer([GOOD[0], "See https://example.org.", GOOD[2]]), "paragraph 2 holds a web address"),
        (answer([GOOD[0], "Mail sam@example.org.", GOOD[2]]), "holds an email address"),
        (answer([GOOD[0], "Call 555-010-0123.", GOOD[2]]), "holds a phone number"),
    ],
)
def test_a_bad_letter_names_the_rule(text, problem):
    with pytest.raises(BadAnswer, match=re.escape(problem)):
        letter.checked(text, labelled(LAB, DRONE), AD)


def test_the_letters_latex_adds_your_details_and_escapes_everything():
    source = latex.cover_letter(CONTACT, "Robot Intern & More", "Acme_Co", "October 30, 2026",
                                ["I scored 100% on \\input{/etc/passwd}.", "Second.", "Third."])  # fmt: skip
    body = source.split(r"\begin{document}")[1]
    assert "{\\LARGE\\bfseries Alex Example}" in body and "alex@example.com" in body
    assert "October 30, 2026\\par\nRe: Robot Intern \\& More, Acme\\_Co\\par\nDear Hiring Manager,\\par" in body
    assert "I scored 100\\% on \\textbackslash{}input\\{/etc/passwd\\}.\\par" in body
    assert body.rstrip().endswith("Sincerely,\\par\nAlex Example\n\\end{document}")
    assert body.split(r"\vspace{1em}")[0] in latex.resume(CONTACT, [])  # the same header as the resume
