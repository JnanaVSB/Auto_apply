import html
import json

import httpx
import pytest
from helpers import FakeWeb

from app.fetch import Fetcher
from app.submit import Choice, FormClosed, FormError, lever

FORM_URL = "https://jobs.lever.co/acme/0a1b-2c3d/apply"


def question(label, control, required=False):
    """One of Lever's own questions, laid out as its apply page does."""
    mark = '<span class="required">✱</span>' if required else ""
    return (
        f'<li class="application-question"><label><div class="application-label">{label}{mark}</div>'
        f'<div class="application-field">{control}</div></label></li>'
    )


def boxes(name, kind, values):
    return "<ul>" + "".join(
        f'<li><label><input type="{kind}" name="{name}" value="{value}"><span>{value}</span></label></li>'
        for value in values
    ) + "</ul>"  # fmt: skip


def card(group, card_id, title, fields):
    """A custom question card (or survey) with its template, the JSON copy of itself the page carries."""
    template = html.escape(json.dumps({"id": card_id, "text": title, "fields": fields}))
    inputs = "".join(
        f'<li class="application-question custom-question"><div class="application-label"><div class="text">'
        f'{field.get("text")}</div></div><div class="application-field"><input type="text" '
        f'name="{group}[{card_id}]{"[responses]" if group == "surveysResponses" else ""}[field{number}]"></div></li>'
        for number, field in enumerate(fields)
    )
    return f'<input type="hidden" name="{group}[{card_id}][baseTemplate]" value="{template}"><ul>{inputs}</ul>'


def options(*texts):
    return [{"text": text, "optionId": f"o-{number}"} for number, text in enumerate(texts)]


CARD_FIELDS = [
    {"type": "text", "text": "Preferred name", "required": False, "description": ""},
    {"type": "textarea", "text": "Why Acme?", "required": True, "description": ""},
    {"type": "multiple-choice", "text": "Need sponsorship?", "required": True, "options": options("Yes", "No")},
    {"type": "multiple-select", "text": "Which teams?", "required": False, "options": options("Robotics", "Search")},
    {"type": "dropdown", "text": "How did you hear about us?", "required": True, "options": options("Friend", "Ad")},
    {"type": "file-upload", "text": "Writing sample", "required": False},
    {"type": "text", "text": "", "required": False},  # no label: skipped
]
LOCATION = '<input type="text" name="location" required><input type="hidden" name="selectedLocation">'
GENDER = (
    '<select name="eeo[gender]"><option value="">Select ...</option><option value="Female">Female</option>'
    '<option value="Decline to self-identify">Decline to self-identify</option></select>'
)
SURVEY = [{"type": "multiple-choice", "text": "Your gender?", "required": False, "options": options("Woman", "Man")}]
PAGE = f"""<html><head><script>var form = '<input name="from_a_script">';</script><style>.x {{}}</style></head>
<body><form id="application-form">
<div class="section page-centered application-form"><h4>Submit your application</h4><ul>
<li class="application-question resume"><label><div class="application-label">Resume/CV<span class="required">✱</span>
</div><div class="application-field"><a><span>ATTACH RESUME/CV</span>
<input type="file" name="resume" accept=".pdf, .docx"></a></div></label></li>
{question("Full name", '<input type="text" name="name" required>', required=True)}
<li class="application-question"><div class="application-label multiple-select">Pronouns</div>
<div class="application-field">{boxes("pronouns", "checkbox", ["She/her", "They/them"])}
<li><label><input type="checkbox" value="Custom"><span>Custom</span></label></li>
<input type="text" name="pronouns"></div></li>
{question("Email", '<input type="email" name="email" required>', required=True)}
{question("Phone", '<input type="text" name="phone">')}
{question("Current location", LOCATION, required=True)}
{question("Current company", '<input type="text" name="org">')}
</ul></div>
<div class="section page-centered application-form"><h4>Links</h4><ul>
{question("LinkedIn URL", '<input type="text" name="urls[LinkedIn]">')}</ul></div>
<div class="section page-centered application-form" data-qa="additional-cards"><h4>Acme questions</h4>
{card("cards", "c-1", "Acme questions", CARD_FIELDS)}
{card("cards", "c-2", "Why Acme?", [{"type": "textarea", "text": "Why Acme?", "required": False}])}
<input type="hidden" name="cards[c-3][baseTemplate]" value="{{not json">
{card("cards", "c-1", "Acme questions", CARD_FIELDS[:1])}
</div>
<div class="section page-centered application-form"><label for="additional-information">
<h4>Additional information</h4></label><div class="application-additional">
<textarea name="comments" id="additional-information"></textarea></div></div>
<div class="section page-centered application-form"><h4>U.S. Equal Employment Opportunity information</h4><ul>
{question("Gender", GENDER)}</ul></div>
<div class="section page-centered application-form hidden" id="countrySurvey_US_CA&#10;&#10;"><h4>Voluntary survey</h4>
{card("surveysResponses", "s-1", "Voluntary survey", SURVEY)}
<input type="hidden" name="surveysResponses[s-1][surveyId]" value="survey-9"></div>
<input type="hidden" name="h-captcha-response" value=""><input type="hidden" name="origin">
<div class="section page-centered application-form" data-qa="consent-section"><ul><li class="application-question">
<div class="application-field full-width"><p>Your data is processed as Acme's Privacy Policy says.</p><ul><li><label>
<span>Acme may contact me about future job opportunities.</span>
<input type="hidden" name="consent[marketing]" value="0"><input type="checkbox" name="consent[marketing]" value="1">
</label></li></ul></div></li></ul></div>
<button id="btn-submit" type="button">Submit application</button></form></body></html>"""


def test_every_part_of_the_page_is_read_and_broken_questions_are_counted():
    form, skipped = lever.parse(PAGE)
    assert [(field.key, field.kind, field.part, field.required, field.label) for field in form.fields] == [
        ("resume", "file", "standard", True, "Resume/CV"),
        ("name", "text", "standard", True, "Full name"),
        ("pronouns", "multi", "standard", False, "Pronouns"),
        ("email", "text", "standard", True, "Email"),
        ("phone", "text", "standard", False, "Phone"),
        ("location", "location", "location", True, "Current location"),
        ("selectedLocation", "hidden", "standard", False, "selectedLocation"),
        ("org", "text", "standard", False, "Current company"),
        ("urls[LinkedIn]", "text", "standard", False, "LinkedIn URL"),
        ("cards[c-1][field0]", "text", "company", False, "Preferred name"),
        ("cards[c-1][field1]", "long_text", "company", True, "Why Acme?"),
        ("cards[c-1][field2]", "single", "company", True, "Need sponsorship?"),
        ("cards[c-1][field3]", "multi", "company", False, "Which teams?"),
        ("cards[c-1][field4]", "single", "company", True, "How did you hear about us?"),
        ("cards[c-1][field5]", "unknown", "company", False, "Writing sample"),
        ("cards[c-2][field0]", "long_text", "company", False, "Why Acme?"),
        ("comments", "long_text", "standard", False, "Additional information"),
        ("eeo[gender]", "single", "eeo", False, "Gender"),
        ("surveysResponses[s-1][responses][field0]", "single", "demographic", False, "Your gender?"),
        ("surveysResponses[s-1][surveyId]", "hidden", "standard", False, "surveysResponses[s-1][surveyId]"),
        ("h-captcha-response", "hidden", "standard", False, "h-captcha-response"),
        ("origin", "hidden", "standard", False, "origin"),
        ("consent[marketing]", "consent", "privacy", False, "Acme may contact me about future job opportunities."),
    ]
    assert skipped == 3  # a field without a label, a template that isn't JSON, a card repeated


def test_choices_file_types_and_help_come_from_the_page():
    fields = {field.key: field for field in lever.parse(PAGE)[0].fields}
    assert fields["resume"].accept == (".pdf", ".docx")
    assert fields["pronouns"].choices == (Choice("She/her", "She/her"), Choice("They/them", "They/them"))
    assert fields["eeo[gender]"].choices == (
        Choice("Female", "Female"), Choice("Decline to self-identify", "Decline to self-identify"),
    )  # fmt: skip
    assert fields["cards[c-1][field2]"].choices == (Choice("Yes", "Yes"), Choice("No", "No"))
    assert fields["cards[c-1][field0]"].help == "Acme questions"
    assert fields["cards[c-2][field0]"].help == ""  # the card's title is the question itself
    assert fields["surveysResponses[s-1][responses][field0]"].help == "Voluntary survey (for candidates in: US, CA)"
    assert fields["comments"].choices == () and fields["comments"].accept == ()


def test_bot_check_boxes_are_never_a_question_whatever_element_holds_them():
    boxes = '<textarea name="h-captcha-response"></textarea><textarea name="g-recaptcha-response"></textarea>'
    page = PAGE.replace('<input type="hidden" name="h-captcha-response" value="">', boxes)
    fields = {field.key: field for field in lever.parse(page)[0].fields}
    assert fields["h-captcha-response"].kind == fields["g-recaptcha-response"].kind == "hidden"


def card_page(title, instructions, description):
    entry = {"type": "textarea", "text": "Why Acme?", "required": True, "description": description}
    template = {"id": "c-1", "text": title, "instructions": instructions, "fields": [entry]}
    value = html.escape(json.dumps(template))
    return f'<form><input type="hidden" name="cards[c-1][baseTemplate]" value="{value}"></form>'


@pytest.mark.parametrize(
    ("title", "instructions", "description", "help_"),
    [
        ("About you", "Do not use AI.", "Answers written with ChatGPT are rejected.",
         "About you Do not use AI. Answers written with ChatGPT are rejected."),
        ("Why Acme?", "", "Temporary visa holders should select No.", "Temporary visa holders should select No."),
        ("", "Be brief.", "", "Be brief."), ("Why Acme?", None, None, ""),
    ],
)  # fmt: skip
def test_a_cards_title_instructions_and_a_fields_description_are_its_help(title, instructions, description, help_):
    assert lever.parse(card_page(title, instructions, description))[0].fields[0].help == help_


def test_a_survey_outside_a_country_section_is_for_everyone():
    survey = card("surveysResponses", "s-2", "Second survey", SURVEY)
    page = PAGE.replace("</form>", f'<div class="section page-centered application-form">{survey}</div></form>')
    fields = {field.key: field for field in lever.parse(page)[0].fields}
    assert fields["surveysResponses[s-2][responses][field0]"].help == "Second survey"


def test_a_file_slot_without_its_own_list_takes_any_file():
    page = PAGE.replace(' accept=".pdf, .docx"', "")
    assert next(field for field in lever.parse(page)[0].fields if field.key == "resume").accept == ()


@pytest.mark.parametrize(
    "page",
    [
        "",
        "<html><body><p>This job is no longer accepting applications.</p></body></html>",
        '<form><input type="hidden" name="origin"><input type="hidden" name="h-captcha-response"></form>',
    ],
)
def test_a_page_without_a_question_is_refused(page):
    with pytest.raises(FormError, match="Lever's page could not be read: the browser reads it"):
        lever.parse(page)


def test_the_page_is_read_through_the_downloader():
    web = FakeWeb({FORM_URL: httpx.Response(200, text=PAGE)})
    assert lever.read(Fetcher(httpx.MockTransport(web)), "acme", "0a1b-2c3d") == lever.parse(PAGE)


@pytest.mark.parametrize(
    ("reply", "problem", "message"),
    [
        (404, FormClosed, r"closed or was removed \(Lever says not found\)"),
        (302, FormError, "Lever sent this posting somewhere else; it may be closed: check the job page."),
        (503, FormError, r"Lever did not answer: busy or failing \(status 503\)"),
    ],
)
def test_a_page_that_cannot_be_had_says_why(no_waiting, reply, problem, message):
    with pytest.raises(problem, match=message):
        lever.read(Fetcher(httpx.MockTransport(FakeWeb({FORM_URL: reply}))), "acme", "0a1b-2c3d")
