from dataclasses import replace

import pytest
from helpers import add_job, one

from app import submit
from app.submit import MAX_CHOICE, MAX_CHOICES, MAX_FIELDS, MAX_HELP, MAX_LABEL, Choice, Field, Form, FormError

SPONSOR = Field(
    "question_102", "Will you require visa sponsorship?", "single", True, "company",
    choices=(Choice("Yes", "1"), Choice("No", "0")),
)  # fmt: skip
FORM = Form((Field("first_name", "First Name", "text", True, "standard"), SPONSOR))


def test_the_fingerprint_is_the_same_for_the_same_form():
    again = Form((Field("first_name", "First Name", "text", True, "standard"), replace(SPONSOR)))
    assert submit.fingerprint(again) == submit.fingerprint(FORM)


@pytest.mark.parametrize(
    "changed",
    [
        Form((FORM.fields[0], replace(SPONSOR, choices=(Choice("Yes, I will", "1"), Choice("No", "0"))))),
        Form((*FORM.fields, Field("question_103", "Why Acme?", "long_text", False, "company"))),
        Form(FORM.fields, "Candidates may apply at most 3 times in 180 days."),
        Form(FORM.fields[::-1]),
    ],
)
def test_any_change_changes_the_fingerprint(changed):
    assert submit.fingerprint(changed) != submit.fingerprint(FORM)


def test_saving_the_same_form_twice_keeps_one_row_and_only_updates_when(conn, monkeypatch):
    job = add_job(conn, "ML Intern")
    monkeypatch.setattr(submit, "now", lambda: "2026-11-05T10:00:00+00:00")
    submit.save(conn, job, FORM, "api")
    first = one(conn, "SELECT fields, fingerprint FROM forms WHERE job_id = ?", job)
    monkeypatch.setattr(submit, "now", lambda: "2026-11-06T09:00:00+00:00")
    submit.save(conn, job, FORM, "api")
    assert one(conn, "SELECT count(*) FROM forms")[0] == 1
    assert one(conn, "SELECT fields, fingerprint FROM forms WHERE job_id = ?", job) == first
    assert submit.load(conn, job)[1] == "2026-11-06T09:00:00+00:00"


def test_a_saved_form_loads_back_as_it_was(conn):
    job = add_job(conn, "ML Intern")
    form = Form(
        (*FORM.fields, Field("resume", "Resume/CV", "file", True, "standard", "Up to 5 MB.", accept=(".pdf", ".docx"))),
        "At most 3 applications in 180 days.",
    )
    submit.save(conn, job, form, "page")
    found, read_on, read_from, problem = submit.load(conn, job)
    assert (found, read_from, problem) == (form, "page", None) and read_on
    assert submit.fingerprint(found) == one(conn, "SELECT fingerprint FROM forms")[0]


def test_questions_that_could_not_be_read_are_kept_with_the_form_until_a_full_read(conn):
    job = add_job(conn, "ML Intern")
    submit.save(conn, job, FORM, "api", unread=2)
    assert submit.load(conn, job)[3] == (
        "2 of this form's questions could not be read: check the form on the site before you submit."
    )
    submit.save(conn, job, FORM, "api")
    assert submit.load(conn, job)[3] is None


def test_a_job_without_a_form_read_loads_nothing(conn):
    assert submit.load(conn, add_job(conn, "ML Intern")) is None


def test_text_is_cleaned_and_cut_to_the_limits_never_refused():
    field = Field(
        "question_1", "Why\x00 Acme?\n  " + "a" * MAX_LABEL, "single", False, "company", "Say\x07 why.\n" * MAX_HELP,
        tuple(Choice(f"Option\t{number} " + "b" * MAX_CHOICE, str(number)) for number in range(MAX_CHOICES + 1)),
    )  # fmt: skip
    assert field.label.startswith("Why Acme? aaa") and len(field.label) == MAX_LABEL
    assert field.help.startswith("Say why.\nSay why.") and len(field.help) == MAX_HELP
    assert len(field.choices) == MAX_CHOICES
    assert field.choices[0].label.startswith("Option 0 bbb") and len(field.choices[0].label) == MAX_CHOICE


def test_the_limit_note_is_cleaned_and_cut_too():
    note = Form(FORM.fields, "At most\x00 3 tries\n in 180 days. " + "c" * MAX_HELP).limit_note
    assert note.startswith("At most 3 tries in 180 days. ccc") and len(note) == MAX_HELP
    assert Form(FORM.fields).limit_note is None


def test_far_too_many_fields_is_refused():
    Form((FORM.fields[0],) * MAX_FIELDS)
    with pytest.raises(FormError, match="far more fields than a form has"):
        Form((FORM.fields[0],) * (MAX_FIELDS + 1))
