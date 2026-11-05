import httpx
import pytest
from helpers import FakeWeb

from app.fetch import Fetcher
from app.submit import Choice, Field, FormClosed, FormError, greenhouse
from app.submit.greenhouse import ACCEPT

FORM_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123?questions=true"


def question(label, name, kind, required=False, values=(), description=None):
    field = {"name": name, "type": kind, "values": [{"label": text, "value": value} for text, value in values]}
    return {"label": label, "required": required, "description": description, "fields": [field]}


REPLY = {
    "id": 123,
    "title": "ML Intern",
    "questions": [
        question("First Name", "first_name", "input_text", required=True),
        {
            "label": "Resume/CV",
            "required": True,
            "fields": [{"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"}],
        },
        question("Why Acme?", "question_101", "textarea", description="&lt;p&gt;A few sentences.&lt;/p&gt;"),
        question("Need sponsorship?", "question_102", "multi_value_single_select", True, [("Yes", 1), ("No", 0)]),
        question("Which teams?", "question_103[]", "multi_value_multi_select", values=[("Robotics", 7)]),
        question("Referral code", "question_104", "input_hidden"),
        question("Sign here", "question_105", "signature_pad"),
        "not a question",
        {"required": True, "fields": [{"name": "question_106", "type": "input_text"}]},  # no label
    ],
    "location_questions": [
        {
            "label": "Location (City)",
            "required": True,
            "fields": [
                {"name": "location", "type": "input_text"},
                {"name": "latitude", "type": "input_hidden"},
                {"name": "longitude", "type": "input_hidden"},
            ],
        }
    ],
    "compliance": [
        {
            "type": "eeoc",
            "description": "<p>Voluntary.</p>",
            "questions": [
                question("Veteran Status", "veteran_status", "multi_value_single_select", values=[("I decline", 3)])
            ],
        }
    ],
    "demographic_questions": {
        "header": "Voluntary questions",
        "description": "<p>Optional.</p>",
        "questions": [
            {
                "id": 51,
                "label": "How do you describe your gender?",
                "required": False,
                "type": "multi_value_multi_select",
                "answer_options": [
                    {"id": 1, "label": "Woman", "free_form": False},
                    {"id": 2, "label": "Let me describe it", "free_form": True},
                ],
            },
            {"id": 52, "required": False, "type": "multi_value_single_select", "answer_options": []},  # no label
        ],
    },
    "data_compliance": [
        {
            "type": "gdpr",
            "requires_consent": True,
            "requires_processing_consent": False,
            "requires_retention_consent": True,
            "retention_period": 365,
        }
    ],
    "education": "education_required",
}


def test_every_part_of_the_form_is_read_and_broken_questions_are_counted():
    form, skipped = greenhouse.parse(REPLY)
    assert [(field.key, field.kind, field.part, field.required) for field in form.fields] == [
        ("first_name", "text", "standard", True),
        ("resume", "file", "standard", True),  # its typed-text twin is left out
        ("question_101", "long_text", "company", False),
        ("question_102", "single", "company", True),
        ("question_103[]", "multi", "company", False),
        ("question_104", "hidden", "company", False),
        ("question_105", "unknown", "company", False),
        ("location", "location", "location", True),
        ("latitude", "hidden", "location", True),
        ("longitude", "hidden", "location", True),
        ("veteran_status", "single", "eeo", False),
        ("demographic_51", "multi", "demographic", False),
        ("data_compliance[gdpr][requires_consent]", "consent", "privacy", True),
        ("data_compliance[gdpr][requires_retention_consent]", "consent", "privacy", True),
        ("school", "single", "education", True),
        ("degree", "single", "education", True),
        ("discipline", "single", "education", True),
    ]
    assert skipped == 3  # "not a question", a question and a voluntary question without a label
    fields = {field.key: field for field in form.fields}
    assert fields["resume"].accept == ACCEPT and fields["first_name"].accept == ()
    assert fields["question_101"].help == "A few sentences."
    assert fields["question_102"].choices == (Choice("Yes", "1"), Choice("No", "0"))
    assert fields["demographic_51"].choices == (Choice("Woman", "1"), Choice("Let me describe it", "2"))
    assert fields["data_compliance[gdpr][requires_consent]"].label == "Consent to the privacy notice (GDPR)"
    assert form.limit_note is None


@pytest.mark.parametrize(
    "values",
    [[{"label": "", "value": 1}], [{"label": None, "value": 1}], [{"label": "Yes", "value": None}], [{"value": 1}]],
)
def test_a_choice_without_a_label_or_value_makes_its_question_unreadable(values):
    broken = {"label": "Need sponsorship?", "fields": [{"name": "question_9", "type": "input_text", "values": values}]}
    form, skipped = greenhouse.parse(REPLY | {"questions": [*REPLY["questions"], broken]})
    assert skipped == 4 and "question_9" not in {field.key for field in form.fields}


def test_a_field_name_seen_before_is_skipped_and_the_first_kept():
    again = question("First name again", "first_name", "textarea")
    form, skipped = greenhouse.parse(REPLY | {"questions": [*REPLY["questions"], again]})
    assert skipped == 4
    assert [(field.label, field.kind) for field in form.fields if field.key == "first_name"] == [("First Name", "text")]


def test_an_ai_disclaimer_is_kept_as_a_hidden_box_only_when_it_says_something():
    form = greenhouse.parse(REPLY | {"ai_disclaimer": "<p>Do not use AI in your application.</p>"})[0]
    assert form.fields[-1] == Field(
        "ai_disclaimer", "AI disclaimer", "hidden", False, "standard", "Do not use AI in your application."
    )
    for empty in (None, "", "  "):
        assert "ai_disclaimer" not in {
            field.key for field in greenhouse.parse(REPLY | {"ai_disclaimer": empty})[0].fields
        }


def test_no_consent_or_education_fields_when_the_form_asks_for_none():
    reply = REPLY | {
        "data_compliance": [{"type": "gdpr", "requires_consent": False, "requires_retention_consent": False}],
        "education": "education_hidden",
    }
    parts = {field.part for field in greenhouse.parse(reply)[0].fields}
    assert "privacy" not in parts and "education" not in parts


def test_an_optional_education_section_is_not_required():
    form, _ = greenhouse.parse(REPLY | {"education": "education_optional"})
    assert [field.required for field in form.fields if field.part == "education"] == [False, False, False]


@pytest.mark.parametrize(
    "reply",
    [None, [], {}, {"questions": "First Name"}, {"questions": []}, {"questions": ["not a question"]}],
)
def test_a_reply_without_a_readable_question_is_refused(reply):
    with pytest.raises(FormError, match="Greenhouse's form for this job could not be read"):
        greenhouse.parse(reply)


def test_the_form_is_read_through_the_downloader():
    form, skipped = greenhouse.read(Fetcher(httpx.MockTransport(FakeWeb({FORM_URL: REPLY}))), "acme", "123")
    assert form == greenhouse.parse(REPLY)[0] and skipped == 3


def test_a_posting_that_is_gone_says_it_is_closed():
    with pytest.raises(FormClosed, match="closed or was removed"):
        greenhouse.read(Fetcher(httpx.MockTransport(FakeWeb({}))), "acme", "123")


def test_a_site_that_fails_says_so_plainly(no_waiting):
    with pytest.raises(FormError, match=r"Greenhouse did not answer: busy or failing \(status 503\)"):
        greenhouse.read(Fetcher(httpx.MockTransport(FakeWeb({FORM_URL: 503}))), "acme", "123")
