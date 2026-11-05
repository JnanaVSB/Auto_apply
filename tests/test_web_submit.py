"""The job box's Application form part: reading a form from its site (a fake web, never the network), the preview and
what is left for you, with a made-up person, job and forms."""

import httpx
import pytest
from helpers import add_job, csrf, log_in, one
from test_submit_greenhouse import REPLY, question
from test_submit_lever import PAGE

from app import questionnaire, submit
from app.documents import pdf
from app.submit import greenhouse
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
    assert "Resume v3" in text and str(data_dir) not in text and "resume-v3.pdf" not in text  # never the file's path
    assert "Favourite &lt;script&gt;alert(1)&lt;/script&gt; paper?" in text and "<script>alert" not in text
    assert "3 hidden boxes, never filled." in text  # a hidden question, the latitude and the longitude
    assert "Left for you" in text and "You tick this yourself in the window." in text
    found = form_part.preview(conn, data_dir, job)
    assert f"Decision required ({found.open})</h3>" in text
    assert "question_112" not in [line.field.key for line in found.lines if line.status == "decide"]


def test_the_preview_shows_a_chosen_choices_label_never_its_value(conn, data_dir):
    job = made_up_job(conn, data_dir)
    submit.save(conn, job, greenhouse.parse(FORM)[0], "api")
    lines = {line.field.key: line for line in form_part.preview(conn, data_dir, job).lines}
    assert (lines["question_102"].value, form_part._shown(lines["question_102"])) == (("0",), "No")
    assert form_part._shown(lines["resume"]) == "Resume v3"


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
        ("https://careers.example.com/jobs/7", "This site&#39;s form isn&#39;t read by the app"),
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
