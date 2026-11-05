"""The job box's Application form part: reading a form from its site (a fake web, never the network), the preview and
what is left for you, with a made-up person, job and forms."""

from datetime import date

import httpx
import pytest
from helpers import add_job, csrf, log_in, one
from test_submit_greenhouse import REPLY, question
from test_submit_lever import PAGE

from app import config, questionnaire, submit, tracker
from app.council import answers
from app.documents import pdf
from app.submit import greenhouse, match, record
from app.web import submit as form_part

GREENHOUSE = "https://job-boards.greenhouse.io/acme/jobs/123"
GREENHOUSE_FORM = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123?questions=true"
LEVER = "https://jobs.lever.co/acme/0a1b-2c3d"
LEVER_FORM = "https://jobs.lever.co/acme/0a1b-2c3d/apply"
DONE = """INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on)
    VALUES (?, 'resume', 3, '[]', '{}', 'done', '{}', 'now')"""
FORM = REPLY | {
    "questions": [
        *REPLY["questions"],
        question("Favourite <script>alert(1)</script> paper?", "question_110", "input_text", required=True),
        question("Hobby?", "question_111", "input_text"),
        question("I consent to Acme's use of AI tools to review my application", "question_112",
                 "multi_value_single_select", required=True, values=[("Yes", 1), ("No", 0)]),
    ]
}  # fmt: skip


@pytest.fixture
def client(client):
    log_in(client)
    return client


def made_up_job(conn, data_dir, link=GREENHOUSE, resume=True):
    """A made-up job at `link`, with a Questionnaire and (`resume`) a finished resume version 3 and its PDF."""
    questionnaire.save(conn, questionnaire.check({
        "contact": {"full_name": "Sam Lee Park", "email": "sam@example.com"},
        "work": {"sponsorship_now": "no", "sponsorship_future": "no"},
    }))  # fmt: skip
    with conn:
        job = add_job(conn, "ML Intern", apply_url=link)
        if resume:
            conn.execute(DONE, (job,))
    if resume:
        target = pdf.tailored(data_dir, job, 3)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.7 fake")
    return job


def read(client, job):
    return client.post(f"/jobs/{job}/form/read", data={"csrf_token": csrf(client)})


def test_a_greenhouse_form_is_read_once_the_resume_is_ready_and_previewed(client, conn, data_dir, web):
    web.pages[GREENHOUSE_FORM] = FORM
    job = made_up_job(conn, data_dir)
    text = client.get(f"/jobs/{job}/form").text
    assert one(conn, "SELECT read_from, problem FROM forms WHERE job_id = ?", job) == (
        "api", "3 of this form's questions could not be read: check the form on the site before you submit.",
    )  # fmt: skip
    assert "from Greenhouse&#39;s public job board" in text and ">Read again</button>" in text
    assert "Resume v3" in text and str(data_dir) not in text  # never the file's path
    assert "Favourite &lt;script&gt;alert(1)&lt;/script&gt; paper?" in text and "<script>alert" not in text
    assert "3 hidden boxes, never filled." in text  # a hidden question, the latitude and the longitude
    assert "Left for you" in text and "You tick this yourself in the window." in text
    found = form_part.preview(conn, data_dir, job)
    assert f"Decision required ({found.open})</h3>" in text
    assert "question_112" not in [line.field.key for line in found.lines if line.status == "decide"]


def test_the_preview_shows_a_chosen_choices_label_never_its_value(conn, data_dir):
    job = made_up_job(conn, data_dir)
    sponsor = question("Do you require visa sponsorship?", "question_9", "multi_value_single_select", True,
                       [("Yes", 1), ("No", 0)])  # fmt: skip
    submit.save(conn, job, greenhouse.parse(FORM | {"questions": [*FORM["questions"], sponsor]})[0], "api")
    lines = {line.field.key: line for line in form_part.preview(conn, data_dir, job).lines}
    assert (lines["question_9"].value, record.shown(lines["question_9"])) == (("0",), "No")
    assert record.shown(lines["resume"]) == "Resume v3"


def test_a_lever_form_is_read_from_its_application_page(client, conn, data_dir, web):
    web.pages[LEVER_FORM] = httpx.Response(200, text=PAGE)
    job = made_up_job(conn, data_dir, LEVER)
    assert "from Lever&#39;s application page" in client.get(f"/jobs/{job}/form").text
    assert one(conn, "SELECT read_from FROM forms WHERE job_id = ?", job) == ("page",)


@pytest.mark.parametrize(
    ("reply", "message"),
    [(404, "This posting is closed or was removed"), (503, "Greenhouse did not answer: busy or failing (status 503)")],
)
def test_a_site_that_fails_says_why_and_nothing_is_saved(client, conn, data_dir, web, no_waiting, reply, message):
    web.pages[GREENHOUSE_FORM] = reply
    job = made_up_job(conn, data_dir)
    assert message in client.get(f"/jobs/{job}/form").text
    assert one(conn, "SELECT count(*) FROM forms")[0] == 0
    asked = len(web.requests)
    assert message not in client.get(f"/jobs/{job}/form").text and len(web.requests) == asked  # tried once only
    assert message in read(client, job).text and len(web.requests) > asked  # Read again always reads


@pytest.mark.parametrize(
    ("link", "line"),
    [
        ("https://jobs.ashbyhq.com/initech/9f8e-7d6c", "Ashby forms are read in the browser"),
        ("https://careers.example.com/jobs/7", "To copy"),
    ],
)
def test_other_sites_forms_are_never_downloaded(client, conn, data_dir, web, link, line):
    job = made_up_job(conn, data_dir, link)
    text = client.get(f"/jobs/{job}/form").text
    assert line in text and "Read the form" not in text
    assert line in read(client, job).text and web.requests == []


def test_without_a_finished_resume_the_form_waits_for_a_press(client, conn, data_dir, web):
    web.pages[GREENHOUSE_FORM] = FORM
    job = made_up_job(conn, data_dir, resume=False)
    text = client.get(f"/jobs/{job}/form").text
    assert "The form is read once the resume is prepared" in text and ">Read the form</button>" in text
    assert web.requests == []
    assert "from Greenhouse&#39;s public job board" in read(client, job).text


def test_a_second_press_while_reading_waits(client, conn, data_dir, web):
    job = made_up_job(conn, data_dir)
    client.app.state.forms_reading.add(job)
    assert "Already reading this form" in read(client, job).text and web.requests == []


def test_a_required_question_no_rule_answers_is_kept_to_answer_once(client, conn, data_dir, web):
    web.pages[GREENHOUSE_FORM] = FORM
    client.get(f"/jobs/{made_up_job(conn, data_dir)}/form")
    more = questionnaire.load(conn)[0]["more"]
    assert [(row["question"], row["answer"], row["asked_by"]) for row in more] == [
        ("Favourite <script>alert(1)</script> paper?", "", "Acme"),  # not the optional "Hobby?"
    ]


def test_a_full_more_answers_is_said_and_the_form_still_saved(client, conn, data_dir, web):
    web.pages[GREENHOUSE_FORM] = FORM
    job = made_up_job(conn, data_dir)
    full = [{"question": f"Question {number}?"} for number in range(questionnaire.MAX_PAIRS)]
    questionnaire.save(conn, questionnaire.check({"more": full}))
    assert "More answers is full (300 rows)" in client.get(f"/jobs/{job}/form").text
    assert one(conn, "SELECT count(*) FROM forms")[0] == 1


def test_unknown_jobs_are_not_found(client):
    assert client.get("/jobs/999/form").status_code == 404
    assert client.post("/jobs/999/form/read", data={"csrf_token": csrf(client)}).status_code == 404


def test_an_ai_use_promise_is_left_for_you_and_never_an_open_decision(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    promise = submit.Field("question_1", "I consent to Acme's use of AI tools", "consent", True, "privacy")
    submit.save(conn, job, submit.Form((promise,)), "api")
    found = form_part.preview(conn, data_dir, job)
    assert ([line.status for line in found.lines], found.open) == (["left"], 0)
    assert "Decision required" not in client.get(f"/jobs/{job}/form").text


YES_NO = (submit.Choice("Yes", "1"), submit.Choice("No", "0"))
PAPER = submit.Field("question_1", "What do you like about Acme?", "text", True, "company")
ROBOTICS = submit.Choice("Robotics <script>x</script>", "7")
TEAM = submit.Field(
    "question_2", "Which team?", "single", True, "company", choices=(ROBOTICS, submit.Choice("Search", "8"))
)
TOOLS = submit.Field("question_3", "Which tools?", "multi", False, "company",
                     choices=(submit.Choice("Python", "p"), submit.Choice("Rust", "r")))  # fmt: skip
WHY = submit.Field("question_4", "Why Acme? In 100 words or less.", "long_text", True, "company")
CONDUCT = submit.Field("question_5", "I acknowledge the code of conduct", "consent", False, "privacy")
RULES = submit.Field("question_6", "I acknowledge the house rules", "consent", True, "privacy")
TEXTS = submit.Field("question_7", "I agree to receive text messages", "consent", True, "privacy")
AI = submit.Field("question_8", "I consent to Acme's use of AI tools", "consent", True, "privacy")
SAMPLE = submit.Field("question_9", "Writing sample", "file", True, "company")
HOBBY = submit.Field("question_10", "Hobby?", "text", False, "company")
QUESTIONS = (PAPER, TEAM, TOOLS, WHY, CONDUCT, RULES, TEXTS, AI, SAMPLE, HOBBY)
KEPT_ROW = "SELECT answer, given_by FROM form_answers WHERE job_id = ? AND field = ?"


@pytest.fixture
def job(conn, data_dir):
    job = made_up_job(conn, data_dir)
    submit.save(conn, job, submit.Form(QUESTIONS), "api")
    return job


def press(client, job, action, **fields):
    return client.post(f"/jobs/{job}/form/{action}", data=fields | {"csrf_token": csrf(client)})


def status(conn, data_dir, job, key):
    return next(line for line in form_part.preview(conn, data_dir, job).lines if line.field.key == key)


def test_a_short_answer_is_kept_for_the_job_and_with_save_for_every_form(client, conn, data_dir, job):
    press(client, job, "answer", field=HOBBY.key, answer="  Chess.  ")
    assert one(conn, KEPT_ROW, job, HOBBY.key) == ("Chess.", "you") and questionnaire.load(conn)[0]["more"] == []
    press(client, job, "answer", field=HOBBY.key, answer="Chess, again.", save="1")
    assert [(row["question"], row["answer"]) for row in questionnaire.load(conn)[0]["more"]] == [
        ("Hobby?", "Chess, again."),
    ]  # fmt: skip
    assert status(conn, data_dir, job, HOBBY.key).status == "fill"


def test_save_is_ignored_for_a_question_that_isnt_ordinary(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    about_us = submit.Field("question_1", "What do you like about Acme?", "text", True, "company")
    legal = submit.Field("question_2", "Have you ever been convicted of a felony?", "text", True, "company")
    submit.save(conn, job, submit.Form((about_us, legal)), "api")
    assert "Save to my Questionnaire" not in client.get(f"/jobs/{job}/form").text
    for asked in (about_us, legal):
        press(client, job, "answer", field=asked.key, answer="Something", save="1")
        assert one(conn, KEPT_ROW, job, asked.key) == ("Something", "you")
    assert questionnaire.load(conn)[0]["more"] == []


def test_an_answer_naming_the_company_is_kept_for_this_job_only(client, conn, data_dir, job):
    text = press(client, job, "answer", field=HOBBY.key, answer="Reading about Acme", save="1").text
    assert "This answer names Acme, so it is kept for this job only" in text
    assert one(conn, KEPT_ROW, job, HOBBY.key)[0] == "Reading about Acme" and questionnaire.load(conn)[0]["more"] == []


def test_a_promise_is_refused_by_every_route_but_the_window(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    promise = submit.Field("question_1", "AI Policy for Application", "single", True, "company", choices=YES_NO)
    submit.save(conn, job, submit.Form((promise,)), "api")
    for action, fields in (("answer", {"answer": "1"}), ("consent", {"choice": "accept"}), ("empty", {})):
        assert "the app never answers it" in press(client, job, action, field=promise.key, **fields).text
    assert one(conn, "SELECT count(*) FROM form_answers")[0] == 0
    press(client, job, "window", field=promise.key)
    assert status(conn, data_dir, job, promise.key).status == "left"


def test_accepting_a_sentence_that_goes_further_never_changes_your_remembered_consent(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    further = submit.Field("question_1", "I consent to the privacy policy and to sharing my data", "consent", True,
                           "privacy")  # fmt: skip
    submit.save(conn, job, submit.Form((further,)), "api")
    assert "for this job only" in client.get(f"/jobs/{job}/form").text
    press(client, job, "consent", field=further.key, choice="accept")
    assert questionnaire.load(conn)[0]["consents"]["privacy"] == "ask"
    assert one(conn, KEPT_ROW, job, further.key) == (match.TICKED, "you")


def test_a_reworded_question_is_asked_again_and_your_old_answer_stays(client, conn, data_dir, job):
    press(client, job, "answer", field=TEAM.key, answer="8")
    reworded = submit.Field(TEAM.key, "Which team would you rather not join?", "single", True, "company",
                            choices=TEAM.choices)  # fmt: skip
    submit.save(conn, job, submit.Form((reworded,)), "api")
    found = status(conn, data_dir, job, TEAM.key)
    assert (found.status, found.note) == ("decide", "the question changed since you answered it")
    assert one(conn, KEPT_ROW, job, TEAM.key) == ("Search", "you")


def test_choices_are_kept_as_their_labels_and_filled_by_the_rules(client, conn, data_dir, job):
    text = press(client, job, "answer", field=TEAM.key, answer="7").text
    assert one(conn, KEPT_ROW, job, TEAM.key)[0] == "Robotics <script>x</script>"
    assert status(conn, data_dir, job, TEAM.key).value == ("7",)
    assert "Robotics &lt;script&gt;x&lt;/script&gt;" in text and "<script>x" not in text
    press(client, job, "answer", field=TOOLS.key, **{"choice.0": "p", "choice.1": "r"})
    assert one(conn, KEPT_ROW, job, TOOLS.key)[0] == "Python\nRust"
    assert status(conn, data_dir, job, TOOLS.key).value == ("p", "r")


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"field": "question_99", "answer": "x"}, "This question is no longer on the form: read it again"),
        ({"field": TEAM.key, "answer": "9"}, "Choose from the form&#39;s own choices"),
        ({"field": TOOLS.key}, "Choose from the form&#39;s own choices"),
        ({"field": PAPER.key, "answer": "x" * 2001}, "An answer here is at most 2,000 characters"),
        ({"field": PAPER.key, "answer": "Hi\x07there"}, "The answer holds an invisible character"),
        ({"field": PAPER.key, "answer": match.IN_WINDOW}, "That text is one the app keeps for itself"),
        ({"field": PAPER.key, "answer": "   "}, "Type an answer, or press Leave it empty"),
        ({"field": SAMPLE.key, "answer": "x"}, "This question isn&#39;t answered here"),
        ({"field": AI.key, "answer": "yes"}, "You tick this yourself in the window: the app never answers it"),
    ],
)
def test_an_answer_that_cant_be_kept_says_why_and_saves_nothing(client, conn, job, fields, problem):
    assert problem in press(client, job, "answer", **fields).text
    assert one(conn, "SELECT count(*) FROM form_answers")[0] == 0


def test_a_known_consent_is_remembered_for_every_form(client, conn, data_dir, job):
    press(client, job, "consent", field=TEXTS.key, choice="accept")
    assert questionnaire.load(conn)[0]["consents"]["texting"] == "accept"
    with conn:
        other = add_job(conn, "Data Intern", apply_url=LEVER)
    submit.save(conn, other, submit.Form((TEXTS,)), "api")
    found = status(conn, data_dir, other, TEXTS.key)
    assert (found.status, found.source) == ("fill", "remembered consent")


def test_a_consent_of_no_known_kind_is_for_this_job_only(client, conn, data_dir, job):
    press(client, job, "consent", field=CONDUCT.key, choice="accept")
    assert one(conn, KEPT_ROW, job, CONDUCT.key) == (match.TICKED, "you")
    press(client, job, "consent", field=CONDUCT.key, choice="decline")
    assert status(conn, data_dir, job, CONDUCT.key).status == "empty"
    assert "This form requires it: decide in the window" in press(client, job, "consent", field=RULES.key,
                                                                    choice="decline").text  # fmt: skip
    assert "the app never answers it" in press(client, job, "consent", field=AI.key, choice="accept").text
    assert questionnaire.load(conn)[0]["consents"] == {
        k: "ask" for k in ("privacy", "retention", "texting", "marketing")
    }


def test_in_the_window_and_leave_it_empty_close_a_decision_and_change_brings_it_back(client, conn, data_dir, job):
    before = form_part.preview(conn, data_dir, job).open
    text = press(client, job, "window", field=SAMPLE.key).text
    assert status(conn, data_dir, job, SAMPLE.key).status == "left" and "You do this yourself in the window." in text
    press(client, job, "empty", field=HOBBY.key)
    assert status(conn, data_dir, job, HOBBY.key).note == "you chose to leave it empty"
    assert "This question is required" in press(client, job, "empty", field=PAPER.key).text
    assert form_part.preview(conn, data_dir, job).open == before - 1  # Hobby? was optional: left empty already
    press(client, job, "forget", field=SAMPLE.key)
    assert status(conn, data_dir, job, SAMPLE.key).status == "decide"


def test_your_edit_of_a_writer_draft_is_yours_and_a_later_draft_leaves_it(client, conn, data_dir, job):
    with conn:
        conn.execute("INSERT INTO form_answers VALUES (?, ?, 'A draft.', 'writer', 5, 'then', ?)",
                     (job, WHY.key, match.wording(WHY)))  # fmt: skip
    text = client.get(f"/jobs/{job}/form").text
    assert "(up to 100 words; Writer draft)" in text and ">A draft.</textarea>" in text
    press(client, job, "answer", field=WHY.key, answer="My own words.\r\nTwo lines.")
    with conn:
        answers.save(conn, job, 6, {"answers": [{"field": WHY.key, "text": "A newer draft."}]})
    assert one(conn, KEPT_ROW, job, WHY.key) == ("My own words.\nTwo lines.", "you")


def test_write_answers_needs_a_writer(client, job):
    assert "Choose the Writer&#39;s model" in press(client, job, "write").text


@pytest.fixture
def writer(tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {data_dir}\nai:\n  writer:\n    model: gemma4:26b-a4b-it-q8_0\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)


def test_write_answers_starts_the_writer_unless_busy(client, conn, job, writer, monkeypatch):
    woken = []
    monkeypatch.setattr(client.app.state.preparer, "wake", lambda: woken.append(1))
    assert press(client, job, "write").headers["hx-refresh"] == "true"  # the box above says so at once
    assert one(conn, "SELECT kind, status FROM preparations WHERE kind = 'answers'") == ("answers", "writing")
    assert woken == [1] and "The Writer is writing these answers" in client.get(f"/jobs/{job}/form").text
    assert "The Writer is still working on this job" in press(client, job, "write").text
    assert "The Writer is writing" in client.get(f"/jobs/{job}/form/writing").text
    with conn:
        conn.execute(
            "UPDATE preparations SET status = 'stopped', problem = 'Read the form first' WHERE kind = 'answers'"
        )
    assert client.get(f"/jobs/{job}/form/writing").headers["hx-refresh"] == "true"  # done: the page reloads
    assert "The last Write answers stopped: Read the form first" in client.get(f"/jobs/{job}/form").text


def test_write_answers_on_a_form_that_wants_your_own_words_says_so(client, conn, data_dir, writer):
    job = made_up_job(conn, data_dir)
    own = submit.Field("question_1", "Why Acme?", "long_text", True, "company", help="Answers must be your own.")
    submit.save(conn, job, submit.Form((own,)), "api")
    text = client.get(f"/jobs/{job}/form").text
    assert (
        "Write these answers yourself" in text and "to be written by you" in text and "Write answers, above" not in text
    )
    assert "write these answers yourself" in press(client, job, "write").text
    assert one(conn, "SELECT count(*) FROM preparations WHERE kind = 'answers'")[0] == 0


def test_i_submitted_it_records_what_the_app_prepared_and_never_a_second_time(client, conn, data_dir, job):
    press(client, job, "answer", field=PAPER.key, answer="The robots.")
    text = press(client, job, "submitted", on="2026-11-04").text
    assert "What the app prepared (you applied by hand)" in text and "What was sent" not in text
    assert "The site may hold different answers if you changed them there." in text
    assert "Applied 2026-11-04" in text and "The robots." in text and "answered by you on the site" in text
    assert "Resume v3" in text and ">I submitted it</button>" not in text and 'name="answer"' not in text
    assert ">Write answers</button>" not in text and "Read again" not in text
    assert "Already recorded on 2026-11-04" in press(client, job, "submitted", on="2026-11-05").text


@pytest.mark.parametrize("day", ["yesterday", "", "1999-12-31", "2999-01-01"])
def test_a_bad_date_records_nothing(client, conn, job, day):
    assert "Choose the date you applied" in press(client, job, "submitted", on=day).text
    assert one(conn, "SELECT count(*) FROM submissions")[0] == 0


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (GREENHOUSE, '<a href="https://job-boards.greenhouse.io/acme/jobs/123" target="_blank" rel="noopener">'),
        ("https://www.linkedin.com/jobs/view/7", '<a href="https://www.linkedin.com/jobs/view/7" target="_blank"'),
    ],
)
def test_the_copy_panel_is_on_every_job(client, conn, data_dir, link, expected):
    job = made_up_job(conn, data_dir, link)
    client.app.state.forms_tried.add(job)
    text = client.get(f"/jobs/{job}/form").text
    assert expected in text and "sam@example.com" in text and "Sponsorship later" in text
    assert f'<a href="/documents/{job}/resume-v3.pdf"' in text and ">I submitted it</button>" in text


def test_a_simplify_job_never_gets_a_link(client, conn, data_dir):
    job = made_up_job(conn, data_dir, "https://simplify.jobs/p/7")
    text = client.get(f"/jobs/{job}/form").text
    assert '<code class="address">https://simplify.jobs/p/7</code>' in text and 'href="https://simplify' not in text
    assert "copy this into your browser yourself" in text


SELECTED = "Are you authorized to work in the location(s) you selected in your previous response?"
DEPENDS = submit.Field("question_1", SELECTED, "single", True, "company", choices=YES_NO)


def test_a_question_pointing_at_another_answer_is_never_saved_for_every_form(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    submit.save(conn, job, submit.Form((DEPENDS,)), "api")
    assert "Save to my Questionnaire" not in client.get(f"/jobs/{job}/form").text
    text = press(client, job, "answer", field=DEPENDS.key, answer="0", save="1").text
    assert questionnaire.load(conn)[0]["more"] == [] and one(conn, KEPT_ROW, job, DEPENDS.key) == ("No", "you")
    assert "your answer for this job" in text


def test_a_question_pointing_at_another_answer_is_not_kept_as_needs_answer(client, conn, data_dir, web):
    asked = question(DEPENDS.label, "question_1", "multi_value_single_select", True, [("Yes", 1), ("No", 0)])
    web.pages[GREENHOUSE_FORM] = REPLY | {"questions": [asked]}
    client.get(f"/jobs/{made_up_job(conn, data_dir)}/form")
    assert DEPENDS.label not in [row["question"] for row in questionnaire.load(conn)[0]["more"]]


def test_a_one_choice_consent_of_no_known_kind_is_given_for_this_job(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    rules = submit.Field("question_1", "I acknowledge the house rules", "multi", True, "company",
                         choices=(submit.Choice("Acknowledge/Confirm", "9"),))  # fmt: skip
    submit.save(conn, job, submit.Form((rules,)), "api")
    press(client, job, "consent", field=rules.key, choice="accept")
    assert one(conn, KEPT_ROW, job, rules.key)[0] == "Acknowledge/Confirm"
    assert status(conn, data_dir, job, rules.key).value == ("9",)


def test_every_consent_decision_can_be_left_for_the_window(client, conn, data_dir, job):
    text = client.get(f"/jobs/{job}/form").text
    assert text.count(f'name="field" value="{RULES.key}"') >= 3  # Accept, Decline and in the window
    press(client, job, "window", field=RULES.key)
    assert status(conn, data_dir, job, RULES.key).status == "left"


def test_an_optional_question_no_rule_answers_has_a_folded_box(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    survey = submit.Field("surveysResponses[s][responses][field0]", "Your age?", "single", False, "demographic",
                          help="A survey (for candidates in: GB)", choices=YES_NO)  # fmt: skip
    submit.save(conn, job, submit.Form((HOBBY, survey)), "api")
    text = client.get(f"/jobs/{job}/form").text
    assert text.count("<summary>Answer it</summary>") == 1 and "Hobby?" in text
    assert f'value="{survey.key}"' not in text
    press(client, job, "answer", field=HOBBY.key, answer="Chess", save="1")
    assert status(conn, data_dir, job, HOBBY.key).value == "Chess"
    assert [(row["question"], row["answer"]) for row in questionnaire.load(conn)[0]["more"]] == [("Hobby?", "Chess")]


def test_an_optional_legal_question_has_a_folded_box_without_the_save_tick(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    visa = submit.Field("question_1", "Do you have a visa?", "single", False, "company", choices=YES_NO)
    submit.save(conn, job, submit.Form((visa,)), "api")
    text = client.get(f"/jobs/{job}/form").text
    assert "<summary>Answer it</summary>" in text and "Save to my Questionnaire" not in text
    press(client, job, "answer", field=visa.key, answer="1", save="1")
    assert one(conn, KEPT_ROW, job, visa.key) == ("Yes", "you") and questionnaire.load(conn)[0]["more"] == []


def test_your_own_answer_over_a_stated_limit_is_saved_with_a_note(client, conn, job):
    text = press(client, job, "answer", field=WHY.key, answer="word " * 101).text
    assert one(conn, KEPT_ROW, job, WHY.key)[1] == "you" and "over the form&#39;s stated limit of 100 words" in text


def test_a_huge_number_in_a_help_text_breaks_nothing(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    huge = submit.Field("question_1", "Why Acme?", "long_text", True, "company", help="9" * 4400 + " words")
    submit.save(conn, job, submit.Form((huge,)), "api")
    answer = client.get(f"/jobs/{job}/form")
    assert answer.status_code == 200 and "(up to 1,200 characters)" in answer.text


def test_a_card_gmail_moved_first_gets_its_record_and_the_part_says_so(client, conn, data_dir, job):
    tracker.move(conn, job, "ready", "applied", date(2026, 11, 3))
    tracker.move(conn, job, "applied", "interview", date(2026, 11, 4))
    text = press(client, job, "submitted", on="2026-11-04").text
    assert "Recorded; the card stays in Interview" in text and tracker.stage(conn, job) == "interview"


def test_a_card_moved_back_after_the_record_is_said_above_it(client, conn, job):
    press(client, job, "submitted", on="2026-11-04")
    tracker.move(conn, job, "applied", "ready", date(2026, 11, 5))
    assert "Your card was moved back on " in client.get(f"/jobs/{job}/form").text


def test_a_records_upload_shows_its_version_once(client, conn, data_dir):
    job = made_up_job(conn, data_dir)
    submit.save(conn, job, submit.Form((submit.Field("resume", "Resume/CV", "file", True, "standard"),)), "api")
    text = press(client, job, "submitted", on="2026-11-04").text
    assert '<td>Resume v3</td><td class="muted"></td>' in text


def test_no_needs_answer_row_for_a_question_that_isnt_ordinary(client, conn, data_dir, web):
    asked = [
        question("AI Policy for Application", "question_1", "input_text", required=True),
        question("Have you ever been convicted of a felony?", "question_2", "input_text", required=True),
        question("What do you like about Acme?", "question_3", "input_text", required=True),
        question("I acknowledge the code of conduct", "question_4", "input_text", required=True),
        question("What is your favourite paper?", "question_5", "input_text", required=True),
    ]
    web.pages[GREENHOUSE_FORM] = REPLY | {"questions": asked, "education": None}
    client.get(f"/jobs/{made_up_job(conn, data_dir)}/form")
    assert [row["question"] for row in questionnaire.load(conn)[0]["more"]] == ["What is your favourite paper?"]
