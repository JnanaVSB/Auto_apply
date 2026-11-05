"""The questionnaire's rules, saving and file, with a made-up person only."""

import json
from datetime import date

import pytest
import yaml

from app import questionnaire
from app.questionnaire import NO_ANSWER, QuestionnaireError

FORM = {
    "csrf_token": "x", "contact.full_name": "Alex Example", "contact.email": "alex@example.com",
    "contact.phone": "+1 (555) 010-0199", "contact.github": "https://github.com/alex-example",
    "work.authorized_us": "yes", "work.status": "F-1 OPT", "work.permit_end": "2028-06-30",
    "availability.remote": "1", "availability.hybrid": "1", "availability.locations": "Phoenix, AZ\nRemote",
    "salary.minimum": "90000", "salary.maximum": "120000", "salary.per": "year", "salary.currency": "USD",
    "education.graduation": "2027-05", "eeo.gender": NO_ANSWER,
    "more.question.0": "Why this company?", "more.answer.0": "Made-up answer.",
    "more.question.1": "", "more.answer.1": "",  # the empty row for a new answer
}  # fmt: skip


def answers(**changes):
    return questionnaire.check(questionnaire.typed(FORM | changes))


def test_a_filled_in_form_becomes_answers():
    saved = answers()
    assert saved.contact.phone == "+1 (555) 010-0199" and saved.work.permit_end.isoformat() == "2028-06-30"
    assert (saved.availability.remote, saved.availability.hybrid, saved.availability.on_site) == (True, True, False)
    assert saved.salary.minimum == 90000 and saved.education.graduation == "2027-05"
    assert [(pair.question, pair.answer) for pair in saved.more] == [("Why this company?", "Made-up answer.")]


def test_every_field_is_optional_and_eeo_defaults_to_no_answer():
    empty = questionnaire.check(questionnaire.typed({}))
    assert empty.contact.full_name == "" and empty.salary.minimum is None and empty.more == []
    assert {empty.eeo.gender, empty.eeo.race, empty.eeo.veteran, empty.eeo.disability} == {NO_ANSWER}
    assert empty.other.heard_about == "Company website"


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("contact.email", "alex.example.com", "Contact, email: does not look like an email address"),
        ("contact.phone", "555-01", "Contact, phone: needs 7 to 15 digits"),
        ("contact.phone", "call 5550100199", "Contact, phone: needs 7 to 15 digits"),
        ("contact.github", "http://github.com/alex", "Contact, github: must be an https:// address"),
        ("contact.full_name", "A" * 101, "Contact, full name: String should have at most 100 characters"),
        ("contact.city", "Phoenix\x00", "Contact, city: contains an invisible (control) character"),
        ("work.status", "tourist", "Work authorization, status: Input should be"),
        ("work.permit_end", "next year", "Work authorization, permit end: Input should be a valid date"),
        ("salary.minimum", "130000", "Salary: the minimum is above the maximum"),
        ("salary.currency", "dollars", "Salary, currency: String should match pattern"),
        ("education.graduation", "May 2027", "Education, graduation: must be a year and month"),
        ("eeo.race", "Martian", "EEO, race: Input should be"),
        ("more.question.1", "", None),  # an empty row is simply left out
        ("more.answer.1", "An answer with no question", "More answers, row 2, question: String should have at least"),
        ("more.question.0", "Q" * 1001, "More answers, row 1, question: String should have at most 1000 characters"),
        ("consents.texting", "always", "Consents, texting: Input should be 'ask', 'accept' or 'decline'"),
    ],
)
def test_broken_rules_name_the_field_never_the_value(field, value, problem):
    if problem is None:
        assert len(answers(**{field: value}).more) == 1
        return
    with pytest.raises(QuestionnaireError) as error:
        answers(**{field: value})
    assert problem in str(error.value) and value not in str(error.value)


def test_too_many_extra_answers_are_refused():
    rows = {f"more.question.{n}": f"Question {n}?" for n in range(questionnaire.MAX_PAIRS + 1)}
    assert len(questionnaire.check(questionnaire.typed(rows | {"more.question.300": ""})).more) == 300
    with pytest.raises(QuestionnaireError, match="More answers: List should have at most 300 items"):
        questionnaire.check(questionnaire.typed(rows))


def test_consents_are_asked_each_time_until_you_choose():
    assert answers().consents.model_dump() == {
        "privacy": "ask",
        "retention": "ask",
        "texting": "ask",
        "marketing": "ask",
    }
    assert answers(**{"consents.marketing": "decline"}).consents.marketing == "decline"


def test_who_first_asked_a_question_comes_back_from_the_saved_rows_by_its_wording():
    typed = questionnaire.typed(FORM | {"more.question.0": "WHY this company", "more.question.1": "Your shoe size?"})
    stored = [{"question": "Why this company?", "answer": "", "asked_by": "Acme", "asked_on": "2026-11-04"}]
    saved = questionnaire.check(questionnaire.with_askers(typed, stored))
    assert [(pair.question, pair.asked_by, pair.asked_on) for pair in saved.more] == [
        ("WHY this company", "Acme", date(2026, 11, 4)), ("Your shoe size?", "", None),
    ]  # fmt: skip


def test_the_page_sends_no_more_than_questions_and_answers():
    typed = questionnaire.typed(FORM | {"more.asked_by.0": "Globex", "more.asked_by.1": "Globex"})
    assert typed["more"] == [{"question": "Why this company?", "answer": "Made-up answer."}]


def test_saving_twice_keeps_one_row_with_the_newest_answers(conn):
    questionnaire.save(conn, answers())
    questionnaire.save(conn, answers(**{"contact.full_name": "Sam Example"}))
    values, saved_on, problem = questionnaire.load(conn)
    assert conn.execute("SELECT count(*) FROM questionnaire").fetchone()[0] == 1
    assert values["contact"]["full_name"] == "Sam Example" and saved_on and problem is None


def test_nothing_saved_shows_the_defaults(conn):
    values, saved_on, problem = questionnaire.load(conn)
    assert values["eeo"]["gender"] == NO_ANSWER and saved_on is None and problem is None


def test_saved_answers_that_break_a_newer_rule_are_shown_unchanged(conn):
    stored = questionnaire.Answers().model_dump(mode="json")
    stored["contact"]["phone"] = "12"  # as if an older version had allowed it
    with conn:
        conn.execute("INSERT INTO questionnaire VALUES (1, ?, 'then')", (json.dumps(stored),))
    values, _, problem = questionnaire.load(conn)
    assert values == stored and "Contact, phone: needs 7 to 15 digits" in problem
    assert json.loads(conn.execute("SELECT answers FROM questionnaire").fetchone()[0]) == stored  # never changed


def test_answers_saved_before_consents_and_asked_by_load_with_the_defaults(conn):
    stored = questionnaire.Answers().model_dump(mode="json")
    del stored["consents"]
    stored["more"] = [{"question": "Why this company?", "answer": "Made-up answer."}]
    with conn:
        conn.execute("INSERT INTO questionnaire VALUES (1, ?, 'then')", (json.dumps(stored),))
    values, _, problem = questionnaire.load(conn)
    assert problem is None and values["consents"]["privacy"] == "ask"
    assert values["more"] == [
        {"question": "Why this company?", "answer": "Made-up answer.", "asked_by": "", "asked_on": None}
    ]


def test_the_file_holds_every_saved_answer(conn):
    questionnaire.save(conn, answers())
    values, _, _ = questionnaire.load(conn)
    text = questionnaire.as_file(values)
    assert text.startswith("# Your auto_apply questionnaire answers. Keep this file somewhere safe")
    assert yaml.safe_load(text) == values
