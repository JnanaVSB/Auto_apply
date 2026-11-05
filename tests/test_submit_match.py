import json
from datetime import date

import pytest
from helpers import add_job

from app import questionnaire
from app.documents import pdf
from app.questionnaire import DISABILITY, NO_ANSWER, VETERAN
from app.submit import Choice, Field, Form, match

ANSWERS = questionnaire.check({
    "contact": {
        "full_name": "Sam Lee Park", "email": "sam@example.com", "phone": "+1 555 0100", "city": "Austin",
        "state": "TX", "country": "United States", "linkedin": "https://example.com/in/sam",
        "github": "https://example.com/sam", "portfolio": "https://example.com",
    },
    "work": {"authorized_us": "yes", "status": "F-1 OPT", "sponsorship_now": "no", "sponsorship_future": "yes"},
    "availability": {"earliest_start": "2027-01-15", "notice_period": "Two weeks", "relocate": "yes"},
    "salary": {"minimum": 120_000, "maximum": 150_000},
    "education": {"highest_degree": "Master's", "graduation": "2027-05", "gpa": "3.9"},
    "eeo": {"gender": "Female", "race": "Asian", "veteran": VETERAN[1], "disability": DISABILITY[1]},
    "other": {"over_18": "yes"},
    "more": [{"question": "Why do you want to join [company]?", "answer": "Robots."}, {"question": "Favourite model?"}],
}).model_dump(mode="json")  # fmt: skip
YES_NO = ("Yes", "No")


def field(label, kind="text", required=False, part="company", key=None, choices=(), **more):
    """A made-up field; each choice's value is its place in the list."""
    options = tuple(Choice(text, str(number)) for number, text in enumerate(choices))
    return Field(key or label, label, kind, required, part, choices=options, **more)


def line(asked, answers=ANSWERS, company="Acme", **given):
    return match.match(Form((asked,)), answers, company, **given)[0][0]


def changed(section, **values):
    return ANSWERS | {section: ANSWERS[section] | values}


def chosen(found):
    """The labels of the choices a line picked."""
    return [found.field.choices[int(value)].label for value in found.value]


def said(found):
    return (found.status, found.decision, found.note)


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("Why do you want to join ACME?!", "why do you want to join [company]"),
        ("Why [Company]?", "why [company]"),
        ("  Résumé/CV\n(PDF) ", "résumé cv pdf"),
        ("Acmeville office", "acmeville office"),  # the company's name only as a whole word
    ],
)
def test_wording_is_cleaned_with_the_company_as_a_marker(text, cleaned):
    assert match.clean(text, "Acme") == cleaned


def test_every_field_gets_a_line_in_the_form_order():
    form = Form((field("Email", key="email", part="standard"), field("Session", "hidden", True)))
    lines, warnings = match.match(form, ANSWERS, "Acme")
    assert [(found.field.key, found.status, found.value) for found in lines] == [
        ("email", "fill", "sam@example.com"),
        ("Session", "hidden", ""),
    ]
    assert warnings == ()


def test_a_hidden_field_is_never_filled_even_when_kept():
    hidden = field("token", "hidden", required=True)
    assert line(hidden, kept={"token": ("abc", "you")}) == match.Line(hidden, "hidden")


def test_an_answer_kept_for_this_job_beats_the_questionnaire():
    email = field("Email", key="email", part="standard")
    found = line(email, kept={"email": ("sam.park@example.com", "you")})
    assert (found.value, found.source) == ("sam.park@example.com", "your answer for this job")
    letter = field("Why Acme?", "long_text", required=True)
    assert line(letter, kept={"Why Acme?": ("I like robots.", "writer")}).source == "Writer draft"


def test_a_kept_choice_is_matched_and_a_lost_one_is_yours_to_decide():
    teams = field("Which teams?", "multi", choices=("Robotics", "Search", "Ads"))
    assert chosen(line(teams, kept={"Which teams?": ("robotics\nSEARCH", "you")})) == ["Robotics", "Search"]
    sponsor = field("Pick one", "single", choices=YES_NO)
    assert said(line(sponsor, kept={"Pick one": ("Maybe", "you")})) == (
        "decide", "unknown", "your saved answer is no longer one of the choices",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("key", "label", "value"),
    [
        ("first_name", "First Name", "Sam Lee"),
        ("last_name", "Last Name", "Park"),
        ("name", "Full name", "Sam Lee Park"),
        ("email", "Email", "sam@example.com"),
        ("phone", "Phone", "+1 555 0100"),
        ("location", "Current location", "Austin, TX, United States"),
        ("urls[LinkedIn]", "LinkedIn URL", "https://example.com/in/sam"),
        ("question_7", "Your GitHub profile", "https://example.com/sam"),  # by its wording
        ("question_8", "Personal website", "https://example.com"),
    ],
)
def test_standard_fields_come_from_the_contact_section(key, label, value):
    found = line(field(label, key=key, part="standard"))
    assert (found.status, found.value, found.source) == ("fill", value, "Questionnaire: Contact")


def test_first_and_last_name_say_they_were_split():
    assert line(field("Last Name", key="last_name", part="standard")).note == "split from your full name"
    one_word = changed("contact", full_name="Sam")
    assert line(field("First Name", key="first_name", part="standard"), one_word).value == "Sam"
    assert said(line(field("Last Name", key="last_name", required=True), one_word)) == (
        "decide", "unknown", "your full name is one word",
    )  # fmt: skip


def test_a_missing_contact_answer_says_what_to_fill():
    no_email = changed("contact", email="")
    note = "fill Contact in your Questionnaire first"
    assert said(line(field("Email", key="email", required=True), no_email)) == ("decide", "unknown", note)
    assert said(line(field("Email", key="email"), no_email)) == ("empty", "", note)


def test_two_link_wordings_are_no_guess_and_lever_extras_are_left():
    assert line(field("GitHub or LinkedIn", key="question_9")).status == "empty"
    assert line(field("Additional information", "long_text", key="comments", part="standard")).status == "empty"
    assert line(field("Current company", key="org", required=True, part="standard")).status == "decide"


RESUME = ("Resume v3", "/data/documents/7/resume-v3.pdf")
LETTER = ("Cover letter v1", "/data/documents/7/cover-letter-v1.pdf")


@pytest.mark.parametrize(
    ("upload", "files", "result"),
    [
        (field("Resume/CV", "file", True, key="resume"), {"resume": RESUME}, ("fill", *RESUME[::-1], "", "")),
        (field("Cover Letter", "file", key="cover_letter"), {"cover_letter": LETTER}, ("fill", *LETTER[::-1], "", "")),
        (field("CV", "file", accept=("application/pdf",)), {"resume": RESUME}, ("fill", *RESUME[::-1], "", "")),
        (field("Resume", "file", True, key="resume", accept=(".doc", ".docx")), {"resume": RESUME},
         ("decide", "", "", "non_pdf", "")),
        (field("Resume", "file", True, key="resume"), {"resume": None},
         ("decide", "", "", "choose_file", "press Prepare first")),
        (field("Cover letter", "file", True), {}, ("decide", "", "", "cover_letter", "")),
        (field("Cover letter", "file"), {}, ("empty", "", "", "", "")),
        (field("Writing sample", "file", True), {"resume": RESUME}, ("decide", "", "", "choose_file", "")),
        (field("Writing sample", "file"), {}, ("empty", "", "", "", "")),
    ],
)  # fmt: skip
def test_uploads_take_the_newest_pdf_or_say_why_not(upload, files, result):
    found = line(upload, files=files)
    assert (found.status, found.value, found.source, found.decision, found.note) == result


@pytest.mark.parametrize(
    ("label", "value", "section"),
    [
        ("Are you legally authorized to work in the United States?", "Yes", "Work authorization"),
        ("Will you now or in the future require visa sponsorship?", "Yes", "Work authorization"),
        ("Do you currently require sponsorship?", "No", "Work authorization"),
        ("Are you authorized to work in New Jersey?", "Yes", "Work authorization"),  # a state, not Jersey
        ("Are you open to relocation?", "Yes", "Availability"),
        ("What is your earliest start date?", "January 15, 2027", "Availability"),
        ("Notice period", "Two weeks", "Availability"),
        ("What are your salary expectations?", "120,000-150,000 USD per year", "Salary"),
        ("Highest level of education completed", "Master's", "Education"),
        ("Expected graduation date", "May 2027", "Education"),
        ("Cumulative GPA", "3.9", "Education"),
        ("Are you at least 18 years of age?", "Yes", "Other"),
        ("Do you hold a security clearance?", "none", "Other"),
        ("How did you hear about Acme?", "Company website", "Other"),
    ],
)
def test_known_questions_take_the_questionnaire_answer(label, value, section):
    found = line(field(label, required=True))
    assert (found.status, found.value, found.source) == ("fill", value, f"Questionnaire: {section}")


def test_a_known_question_with_choices_picks_the_one_that_fits():
    asked = field("Are you legally authorized to work in the US?", "single", choices=("Yes, I am authorized", "No"))
    assert chosen(line(asked)) == ["Yes, I am authorized"]


@pytest.mark.parametrize(
    ("label", "note"),
    [
        ("Are you legally authorized to work in the US without sponsorship?", "this asks more than one thing"),
        ("Are you legally authorized to work in Canada?", "this asks about another country"),
        ("Will you require sponsorship to work in the EMEA region?", "this asks about another country"),
    ],
)
def test_a_question_the_rules_cannot_answer_alone_is_yours_to_decide(label, note):
    assert said(line(field(label))) == ("decide", "unknown", note)


@pytest.mark.parametrize(
    ("now", "future", "value"), [("no", "no", "No"), ("yes", "no", "Yes"), ("no", "", ""), ("", "", "")]
)
def test_sponsorship_later_is_yes_if_either_is_yes_and_no_only_if_both_are(now, future, value):
    found = line(
        field("Will you require sponsorship?"), changed("work", sponsorship_now=now, sponsorship_future=future)
    )
    assert found.value == value
    assert found.note == ("" if value else "fill Work authorization in your Questionnaire first")


@pytest.mark.parametrize(
    ("salary", "value"),
    [
        ({"maximum": None}, "120,000 USD per year"),
        ({"minimum": 40, "maximum": 50, "per": "hour"}, "40-50 USD per hour"),
        ({"prefer_to_discuss": True}, "Open to discussion"),
    ],
)
def test_salary_is_written_out(salary, value):
    assert line(field("Desired salary"), changed("salary", **salary)).value == value


def test_relocation_that_depends_is_yours_to_decide():
    found = line(field("Are you willing to relocate?"), changed("availability", relocate="depends"))
    assert said(found) == ("decide", "unknown", "your Questionnaire says it depends")


def test_two_choices_that_both_fit_are_no_guess():
    asked = field("Are you at least 18?", "single", choices=("Yes", "yes", "No"))
    assert said(line(asked)) == ("decide", "unknown", "your Questionnaire answer is not one of the choices")


GENDER = ("Man", "Woman", "Non-binary", "I don't wish to answer")
RACE = (
    "Asian (Not Hispanic or Latino)",
    "White (Not Hispanic or Latino)",
    "Hispanic or Latino",
    "Decline to self-identify",
)
HISPANIC = ("Yes", "No", "Decline To Self Identify")
VETERANS = (
    "I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
    "I don't wish to answer",
)  # fmt: skip
DISABLED = (
    "Yes, I have a disability, or have had one in the past",
    "No, I do not have a disability",
    "I do not want to answer",
)


@pytest.mark.parametrize(
    ("key", "label", "choices", "eeo", "picked"),
    [
        ("eeo[gender]", "Gender", GENDER, {}, "Woman"),
        ("gender", "Gender", ("Male", "Female", "Decline to self-identify"), {"gender": "Male"}, "Male"),
        ("demographic_1", "How do you describe your gender?", ("Nonbinary", "Woman"), {"gender": "Non-binary"},
         "Nonbinary"),
        ("eeo[gender]", "Gender", GENDER, {"gender": NO_ANSWER}, "I don't wish to answer"),
        ("race", "Please identify your race", RACE, {}, "Asian (Not Hispanic or Latino)"),
        ("hispanic_ethnicity", "Are you Hispanic/Latino?", HISPANIC, {"race": "Hispanic or Latino"}, "Yes"),
        ("hispanic_ethnicity", "Are you Hispanic/Latino?", HISPANIC, {}, "No"),
        ("hispanic_ethnicity", "Are you Hispanic/Latino?", HISPANIC, {"race": NO_ANSWER}, "Decline To Self Identify"),
        ("veteran_status", "Veteran Status", VETERANS, {}, "I am not a protected veteran"),
        ("veteran_status", "Veteran Status", VETERANS, {"veteran": VETERAN[0]}, VETERANS[1]),
        ("disability_status", "Disability Status", DISABLED, {"disability": DISABILITY[0]}, DISABLED[0]),
        ("disability_status", "Disability Status", DISABLED, {}, "No, I do not have a disability"),
        ("disability_status", "Disability Status", DISABLED, {"disability": NO_ANSWER}, "I do not want to answer"),
    ],
)  # fmt: skip
def test_eeo_answers_fit_each_forms_wording(key, label, choices, eeo, picked):
    found = line(field(label, "single", key=key, part="eeo", choices=choices), changed("eeo", **eeo))
    assert (chosen(found), found.source) == ([picked], "Questionnaire: EEO")


def test_an_eeo_answer_the_choices_do_not_hold_is_yours_to_decide():
    asked = field("Race", "single", key="eeo[race]", part="eeo", choices=("East Asian", "South Asian"))
    assert said(line(asked)) == ("decide", "unknown", "your EEO answer is not one of the choices")


def survey(countries):
    help_ = f"Voluntary survey (for candidates in: {countries})" if countries else "Voluntary survey"
    key = "surveysResponses[s-1][responses][field0]"
    return field("Your gender?", "single", key=key, part="demographic", choices=GENDER, help=help_)


@pytest.mark.parametrize("countries", ["US", "GB, US, CA", ""])
def test_a_lever_survey_for_your_country_is_answered(countries):
    assert chosen(line(survey(countries))) == ["Woman"]


@pytest.mark.parametrize("country", ["United States", "Canada", ""])
def test_a_lever_survey_for_other_countries_is_left_empty(country):
    found = line(survey("GB, IE"), changed("contact", country=country))
    assert said(found) == ("empty", "", "only for candidates in: GB, IE")
    assert said(line(survey("CA"), changed("contact", country="Canada")))[0] == "fill"


def test_a_saved_more_answer_fits_the_same_question_from_any_company():
    found = line(field("Why do you want to join Globex?", "long_text", True), company="Globex")
    assert (found.status, found.value, found.source) == ("fill", "Robots.", "your saved answer")


def test_an_empty_saved_answer_does_not_count():
    assert said(line(field("Favourite model?", required=True))) == ("decide", "unknown", "")


@pytest.mark.parametrize(
    ("asked", "kind"),
    [
        (field("Consent to the privacy notice (GDPR)", "consent", True, "privacy"), "privacy"),
        (field("Consent to keeping your data (GDPR)", "consent", True, "privacy"), "retention"),
        (field("Consent to data processing (GDPR)", "consent", True, "privacy"), "privacy"),
        (field("I agree to the processing of my personal data", "consent", True), "privacy"),
        (field("Acme may store my data for future opportunities.", "consent", key="consent[store]"), "retention"),
        (field("I agree to receive text messages about my application", "single", choices=YES_NO), "texting"),
        (field("Acme may contact me about future jobs.", "consent", key="consent[marketing]"), "marketing"),
    ],
)  # fmt: skip
def test_a_remembered_consent_is_given_and_otherwise_asked(asked, kind):
    found = line(asked, consents={kind: "accept"})
    assert (found.status, found.source, found.note) == ("fill", "remembered consent", kind)
    assert found.value == (("0",) if asked.kind == "single" else "yes")
    assert said(line(asked, consents={kind: "ask"})) == ("decide", "consent", kind)
    assert said(line(asked)) == ("decide", "consent", kind)


@pytest.mark.parametrize(
    ("label", "kind", "name"),
    [
        ("I consent to Acme's use of AI tools to review my application", "ai", "AI use"),
        ("I attest that all information I provide is true", "ai", "AI use"),
        ("I agree to resolve any dispute through arbitration", "arbitration", "arbitration"),
    ],
)
def test_ai_use_and_arbitration_are_always_left_for_you_to_tick(label, kind, name):
    assert said(line(field(label, "consent", True), consents={kind: "accept"})) == ("left", "", name)


def test_a_declined_consent_is_left_empty_unless_the_form_requires_it():
    asked = field("Subscribe to our newsletter", "consent")
    assert said(line(asked, consents={"marketing": "decline"})) == ("empty", "", "marketing")
    assert said(line(field("Join our talent community", "consent", True), consents={"marketing": "decline"})) == (
        "decide", "consent", "marketing: you decline this, but the form requires it",
    )  # fmt: skip


def test_a_consent_of_no_known_kind_is_asked():
    assert said(line(field("I acknowledge the code of conduct", "consent"), consents={"": "accept"})) == (
        "decide", "consent", "consent",
    )  # fmt: skip


@pytest.mark.parametrize("kind", ["consent", "single"])
def test_a_saved_answer_never_answers_ai_use_or_arbitration(kind):
    for label, name in [
        ("Do you agree to our AI use policy?", "AI use"),
        ("Do you agree to arbitration?", "arbitration"),
    ]:
        asked = field(label, kind, choices=YES_NO if kind == "single" else ())
        saved = ANSWERS | {"more": [{"question": label, "answer": "Yes"}]}
        assert said(line(asked, saved)) == ("left", "", name)


def test_a_saved_answer_may_answer_a_consent_of_no_known_kind():
    asked = field("I acknowledge the code of conduct", "consent")
    found = line(asked, ANSWERS | {"more": [{"question": asked.label, "answer": "yes"}]})
    assert (found.status, found.value, found.source) == ("fill", "yes", "your saved answer")


@pytest.mark.parametrize("label", ["Why do you want to relocate?", "Tell us about your graduation project"])
def test_a_long_question_on_a_yes_no_or_education_answer_is_for_the_writer(label):
    assert line(field(label, "long_text", True)).status == "write"


def test_a_long_salary_question_still_gets_the_questionnaire_salary():
    found = line(field("What are your salary expectations?", "long_text", True))
    assert (found.status, found.value, found.source) == (
        "fill",
        "120,000-150,000 USD per year",
        "Questionnaire: Salary",
    )


def test_a_known_question_naming_a_link_site_gets_no_link():
    found = line(field("How did you hear about us (LinkedIn, a friend, ...)?", key="question_10"))
    assert (found.value, found.source) == ("Company website", "Questionnaire: Other")


def test_long_free_text_is_for_the_writer_and_anything_else_is_left_or_yours():
    assert line(field("Tell us about a project you are proud of", "long_text")).status == "write"
    assert said(line(field("What is your favourite paper?", required=True))) == ("decide", "unknown", "")
    assert said(line(field("Pronouns", "multi", key="pronouns", part="standard", choices=("She/her",)))) == (
        "empty", "", "",
    )  # fmt: skip
    assert said(line(field("Writing sample", "unknown", True))) == ("decide", "unknown", "")


@pytest.mark.parametrize(
    ("label", "help_"),
    [
        ("Why Acme?", "Your answers must be your own and not AI-generated."),
        ("Use of AI tools to complete this application is prohibited.", ""),
        ("Cover letter", "Materials should be the applicant's own work."),
    ],
)
def test_a_form_that_forbids_ai_writing_warns(label, help_):
    warnings = match.match(Form((field(label, "long_text", help=help_),)), ANSWERS, "Acme")[1]
    assert warnings == (match.NO_AI_WARNING,)


def test_an_ai_notetaker_consent_or_a_limit_note_is_no_ai_warning():
    notetaker = field("Acme uses an AI Notetaker in interviews. Do you consent?", "consent")
    form = Form((notetaker,), "Candidates may apply at most 3 times in 180 days.")
    assert match.match(form, ANSWERS, "Acme")[1] == (
        "This company limits applications (its words: Candidates may apply at most 3 times in 180 days.)",
    )  # fmt: skip


PREPARATION = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on)
    VALUES (?, ?, ?, '[]', '{}', ?, '{}', 'now')"""


def test_documents_are_the_newest_finished_versions_with_their_file(conn, data_dir):
    job = add_job(conn, "ML Intern")
    for kind, number, status in [("resume", 1, "done"), ("resume", 2, "done"), ("resume", 3, "stopped"),
                                 ("cover_letter", 1, "done")]:  # fmt: skip
        conn.execute(PREPARATION, (job, kind, number, status))
    resume = pdf.tailored(data_dir, job, 2)
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF-1.7 fake")
    assert match.documents(conn, data_dir, job) == {"resume": ("Resume v2", resume), "cover_letter": None}
    assert match.documents(conn, data_dir, add_job(conn, "Data Intern")) == {"resume": None, "cover_letter": None}


def remembered(conn):
    return questionnaire.load(conn)[0]["more"]


def test_remember_keeps_one_row_per_wording_with_its_first_asker(conn):
    match.remember(conn, "Have you worked at Acme before?", "Acme", today=date(2026, 11, 4))
    match.remember(conn, "Have you worked at GLOBEX before?", "Globex", "No", today=date(2026, 11, 5))
    match.remember(conn, "Have you worked at Initech before?", "Initech", "Yes", today=date(2026, 11, 6))
    assert remembered(conn) == [
        {
            "question": "Have you worked at [company] before?",
            "answer": "No",
            "asked_by": "Acme",
            "asked_on": "2026-11-04",
        }
    ]


def test_remember_marks_the_company_however_it_is_written():
    assert match._marked("Why ACME, Inc? Acmeville is near.", "Acme Inc.") == "Why [company]? Acmeville is near."
    assert match._marked("Why us?", "") == "Why us?"


def test_a_remembered_question_answers_the_same_question_from_another_company(conn):
    match.remember(conn, "Have you worked at Acme before?", "Acme", "No")
    asked = field("Have you worked at Globex before?", "single", choices=YES_NO)
    found = match.match(Form((asked,)), questionnaire.load(conn)[0], "Globex")[0][0]
    assert (chosen(found), found.source) == (["No"], "your saved answer")


def test_remember_refuses_a_full_list_and_saves_nothing(conn):
    full = [{"question": f"Question {number}?", "answer": ""} for number in range(questionnaire.MAX_PAIRS)]
    questionnaire.save(conn, questionnaire.check({"more": full}))
    before = conn.execute("SELECT answers, saved_on FROM questionnaire").fetchone()
    with pytest.raises(questionnaire.QuestionnaireError, match=r"More answers is full \(300 rows\): delete some"):
        match.remember(conn, "Have you worked at Acme before?", "Acme", "No")
    assert conn.execute("SELECT answers, saved_on FROM questionnaire").fetchone() == before
    match.remember(conn, "Question 7?", "Acme", "Yes")  # a row already there still takes its answer
    assert remembered(conn)[7]["answer"] == "Yes" and len(remembered(conn)) == 300


def test_remember_changes_nothing_when_the_saved_answers_break_a_rule(conn):
    stored = questionnaire.Answers().model_dump(mode="json")
    stored["contact"]["phone"] = "12"
    with conn:
        conn.execute("INSERT INTO questionnaire VALUES (1, ?, 'then')", (json.dumps(stored),))
    with pytest.raises(
        questionnaire.QuestionnaireError, match="fix it on the Questionnaire page first: Contact, phone"
    ):
        match.remember(conn, "Have you worked at Acme before?", "Acme", "No")
    assert json.loads(conn.execute("SELECT answers FROM questionnaire").fetchone()[0]) == stored


@pytest.mark.parametrize(
    ("asked", "marker", "result"),
    [
        (field("Favourite paper?", required=True), match.IN_WINDOW, ("left", "", "you do this in the window")),
        (field("Writing sample", "unknown", True), match.IN_WINDOW, ("left", "", "you do this in the window")),
        (field("Hobby?"), match.LEFT_EMPTY, ("empty", "", "you chose to leave it empty")),
    ],
)
def test_your_choice_for_one_jobs_question_is_kept_as_a_marker(asked, marker, result):
    found = line(asked, kept={asked.key: (marker, "you")})
    assert (*said(found), found.source) == (*result, "your answer for this job")


def test_a_consents_kind_is_the_same_for_the_rule_and_the_page():
    assert match.consent_kind(field("I agree to receive text messages", "consent")) == ("texting", "texting")
    assert match.consent_kind(field("I acknowledge the code of conduct", "consent")) == ("", "consent")
    assert match.consent_kind(field("Favourite paper?")) is None
