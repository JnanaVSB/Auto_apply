"""Shared job rules and saving, on a throwaway encrypted database with made-up jobs."""

from contextlib import closing
from dataclasses import replace

import pytest
import sqlcipher3

from app import db, jobs
from app.sources import Job

ROLES = jobs.Roles.from_settings(["AI/ML/Data"], ["machine learning", "AI", "RL"])
NOW, LATER = "2026-10-20T10:00:00+00:00", "2026-10-21T10:00:00+00:00"
SIMPLIFY = "simplify:Summer2027-Internships"


def job(**changes) -> Job:
    base = Job(
        source="greenhouse:acme",
        source_job_id="123",
        company="Acme",
        title="Machine Learning Engineer",
        url="https://job-boards.greenhouse.io/acme/jobs/123",
        locations=["San Francisco, CA"],
    )
    return replace(base, **changes)


def simplify_job(**changes) -> Job:
    return job(**{"source": SIMPLIFY, "source_job_id": "s-1", "category": "AI/ML/Data", "level": "intern"} | changes)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "RECOVERY_KDF_ITER", 1_000)
    db.setup(tmp_path / "data", "fake-master-password-1")
    with closing(db.connect(tmp_path / "data")) as connection:
        yield connection


def rows(conn, columns="title, status"):
    return conn.execute(f"SELECT {columns} FROM jobs ORDER BY id").fetchall()  # noqa: S608 (test helper)


@pytest.mark.parametrize(
    ("changes", "matches"),
    [
        ({}, True),
        ({"title": "Product Designer", "category": "AI/ML/Data"}, True),
        ({"title": "Applied ai Engineer"}, True),
        ({"title": "Data Engineer", "description": "You will use RL daily."}, True),
        ({"title": "Maintain the RLHF-free payroll"}, False),  # whole words only
        ({"title": "Accountant", "description": "We maintain books."}, False),
    ],
)
def test_role_matching(changes, matches):
    assert ROLES.match(job(**changes)) is matches
    assert not jobs.Roles.from_settings([], []).match(job())


@pytest.mark.parametrize(
    ("locations", "place_checks", "expected"),
    [
        (["Austin, TX"], [], "yes"),
        (["NYC"], [], "yes"),
        (["Remote in USA"], [], "yes"),
        (["Boston, Massachusetts"], [], "yes"),
        (["London, UK", "Seattle, WA"], [], "yes"),
        (["Toronto, ON, Canada"], [], "no"),
        (["London, UK"], [], "no"),
        (["Remote"], [], "unclear"),
        ([], [], "unclear"),
        (["Remote"], [("Remote", "US")], "yes"),
        (["Toronto"], [("Toronto", "CA")], "no"),  # Lever's code for Canada, not California
        (["Berlin, DE"], [("Berlin, DE", "DE")], "no"),  # the country field wins over "DE" looking like Delaware
        (["New York"], [("New York", "Remote")], "yes"),  # "Remote" is no country: judged by the text
        (["New York, NY"], [("New York, NY", None), ("New York, NY", None)], "yes"),  # a Greenhouse office
        (["San Francisco"], [("San Francisco, California", None)], "yes"),
        (["New York", "London"], [("New York", None), ("London", "United Kingdom")], "yes"),  # Ashby, mixed
        (["London"], [("London", "United Kingdom")], "no"),
        (["São Paulo, Brasil"], [], "no"),
        (["Seattle"], [], "yes"),
        (["Toronto, CA", "Bengaluru, IN"], [], "no"),  # every location clearly foreign
        (["Toronto, CA", "Vancouver"], [], "unclear"),  # one could be in Washington
        (["Qwertyville Base 7"], [], "unclear"),  # not recognised: kept, never silently dropped
    ],
)
def test_us_location(locations, place_checks, expected):
    assert jobs.us_location(job(locations=locations, place_checks=place_checks)) == expected


@pytest.mark.parametrize(
    ("title", "hint", "expected"),
    [
        ("Machine Learning Intern", None, "intern"),
        ("Software Engineer Co-op", None, "intern"),
        ("Internal Communications Manager", None, "unknown"),
        ("New Grad ML Engineer", None, "new grad"),
        ("Member of Technical Staff", None, "staff+"),
        ("Associate Director, AI", None, "staff+"),
        ("Sr. Research Scientist", None, "senior"),
        ("Machine Learning Engineer II", None, "mid"),
        ("Machine Learning Engineer I", None, "entry"),
        ("Junior Data Scientist", None, "entry"),
        ("Research Scientist", None, "unknown"),
        ("Senior Engineer", "new grad", "new grad"),  # the Simplify list decides
    ],
)
def test_level(title, hint, expected):
    assert jobs.level(job(title=title, level=hint)) == expected


def test_remote_label():
    assert jobs.remote(job(remote="hybrid")) == "hybrid"
    assert jobs.remote(job(locations=["Remote in USA"])) == "remote"
    assert jobs.remote(job()) == "unknown"


@pytest.mark.parametrize(
    ("source", "url", "expected"),
    [
        ("lever:globex", "https://anything.example/x", "https://jobs.lever.co/globex/123"),
        (SIMPLIFY, "https://boards.greenhouse.io/Acme/jobs/9?gh_jid=9", "https://job-boards.greenhouse.io/acme/jobs/9"),
        (
            SIMPLIFY,
            "http://Careers.Example.com/jobs/7/?utm_source=Simplify&ref=x&jobId=7#apply",
            "https://careers.example.com/jobs/7?jobId=7",
        ),
        (SIMPLIFY, "https://careers.example.com/#/jobs/123", "https://careers.example.com#/jobs/123"),
        (SIMPLIFY, "https://careers.example.com/#!/jobs/9", "https://careers.example.com#!/jobs/9"),
        (SIMPLIFY, "https://careers.example.com/jobs/7#apply", "https://careers.example.com/jobs/7"),
        (SIMPLIFY, "https://Careers.Example.com./jobs/7", "https://careers.example.com/jobs/7"),
        (SIMPLIFY, "javascript:alert(1)", None),
        (SIMPLIFY, "https://user@careers.example.com/jobs/7", None),
        (SIMPLIFY, "not a link", None),
    ],
)
def test_clean_link(source, url, expected):
    assert jobs.clean_link(job(source=source, url=url)) == expected


def test_new_jobs_are_stored_once_and_rerunning_changes_nothing(conn):
    first = jobs.save(conn, "greenhouse:acme", [job(), job(source_job_id="124", title="Accountant")], ROLES, NOW)
    second = jobs.save(conn, "greenhouse:acme", [job(), job(source_job_id="124", title="Accountant")], ROLES, LATER)
    assert (first.new, first.kept, second.new, second.updated, second.kept) == (1, 1, 0, 0, 1)
    assert rows(conn, "title, status, first_seen, last_seen") == [("Machine Learning Engineer", "open", NOW, LATER)]


def test_non_us_and_inactive_new_jobs_are_not_stored(conn):
    jobs.save(
        conn, "greenhouse:acme", [job(locations=["London, UK"]), job(source_job_id="2", listed=False)], ROLES, NOW
    )
    assert rows(conn) == []


def test_simplify_and_board_copies_become_one_merged_job(conn):
    simplify = simplify_job(company="Acme Corp", sponsorship="no", terms=["Summer 2027"])
    jobs.save(conn, SIMPLIFY, [simplify], ROLES, NOW)
    board = job(company="acme", description="Train models.", salary="$50/hour", remote="hybrid", title="ML Intern")
    counts = jobs.save(conn, "greenhouse:acme", [board], ROLES, LATER)
    assert (counts.new, counts.updated) == (0, 1)
    assert rows(conn, "company, title, sponsorship, terms, description, salary, remote, level") == [
        ("Acme Corp", "ML Intern", "no", '["Summer 2027"]', "Train models.", "$50/hour", "hybrid", "intern")
    ]
    assert conn.execute("SELECT count(*) FROM job_sources").fetchone()[0] == 2


def test_each_source_owns_its_details_so_reruns_change_nothing(conn):
    simplify = simplify_job(title="ML Intern (Summer)", locations=["SF"], sponsorship="no")
    board = job(company="acme", title="ML Intern", locations=["San Francisco, CA"], description="Train models.")
    for _ in range(2):
        simplify_counts = jobs.save(conn, SIMPLIFY, [simplify], ROLES, NOW)
        board_counts = jobs.save(conn, "greenhouse:acme", [board], ROLES, NOW)
    assert (simplify_counts.updated, board_counts.updated) == (0, 0)
    assert rows(conn, "company, title, locations, sponsorship, description") == [
        ("Acme", "ML Intern", '["San Francisco, CA"]', "no", "Train models.")
    ]


def test_the_first_simplify_list_owns_a_job_found_in_both(conn):
    new_grad = "simplify:New-Grad-Positions"
    for _ in range(2):
        jobs.save(conn, SIMPLIFY, [simplify_job(title="ML Engineer")], ROLES, NOW)
        counts = jobs.save(
            conn, new_grad, [simplify_job(source=new_grad, title="ML Trainee", level="new grad")], ROLES, NOW
        )
    assert (counts.updated, rows(conn, "title, level")) == (0, [("ML Engineer", "intern")])


def test_two_entries_of_one_list_for_the_same_job_do_not_overwrite_each_other(conn):
    entries = [simplify_job(title="ML Intern"), simplify_job(source_job_id="s-2", title="ML Intern (copy)")]
    jobs.save(conn, SIMPLIFY, entries, ROLES, NOW)
    counts = jobs.save(conn, SIMPLIFY, entries, ROLES, LATER)
    assert (counts.updated, rows(conn)) == (0, [("ML Intern", "open")])


def test_a_missing_value_never_replaces_a_known_one(conn):
    jobs.save(conn, "greenhouse:acme", [job(description="Train models.", remote="hybrid")], ROLES, NOW)
    jobs.save(conn, "greenhouse:acme", [job(description=None, remote=None)], ROLES, LATER)
    assert rows(conn, "description, remote") == [("Train models.", "hybrid")]


def test_the_other_source_fills_gaps_but_changes_nothing_known(conn):
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, NOW)  # the board does not say whether it is remote
    jobs.save(conn, SIMPLIFY, [simplify_job(locations=["Remote in USA"])], ROLES, LATER)
    assert rows(conn, "remote, locations") == [("remote", '["San Francisco, CA"]')]


def test_a_saved_job_judged_foreign_later_stays_visible_and_is_marked_for_checking(conn, monkeypatch):
    toronto = job(locations=["Toronto, CA"])
    with monkeypatch.context() as old_rule:
        old_rule.setattr(jobs, "us_location", lambda job: "yes")  # how the old rule judged it
        jobs.save(conn, "greenhouse:acme", [toronto], ROLES, NOW)
    counts = jobs.save(conn, "greenhouse:acme", [toronto], ROLES, LATER)
    assert (counts.updated, rows(conn, "us_location, check_location")) == (1, [("yes", 1)])  # never hidden


def test_only_the_owning_source_asks_for_a_location_check(conn):
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, NOW)  # the company's own board: San Francisco
    jobs.save(conn, SIMPLIFY, [simplify_job(locations=["Toronto, CA"])], ROLES, LATER)  # Simplify disagrees
    assert rows(conn, "us_location, check_location") == [("yes", 0)]


def test_a_job_you_picked_is_marked_as_yours(conn):
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, NOW)
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, LATER, complete=False, chosen=True)
    assert rows(conn, "added_by_you") == [(1,)]


def test_a_closed_job_is_left_alone(conn):
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, NOW)
    conn.execute("UPDATE jobs SET status = 'closed'")  # set by hand in a later phase
    jobs.save(conn, "greenhouse:acme", [], ROLES, LATER)
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, LATER)
    assert rows(conn) == [("Machine Learning Engineer", "closed")]


def test_unclear_never_replaces_a_known_us_label(conn):
    board, simplify = job(locations=["Remote"]), simplify_job(locations=["Austin, TX"])
    for _ in range(2):
        board_counts = jobs.save(conn, "greenhouse:acme", [board], ROLES, NOW)
        simplify_counts = jobs.save(conn, SIMPLIFY, [simplify], ROLES, NOW)
    assert (board_counts.updated, simplify_counts.updated, rows(conn, "us_location")) == (0, 0, [("yes",)])


def test_simplify_only_job_takes_simplify_changes(conn):
    jobs.save(conn, SIMPLIFY, [simplify_job()], ROLES, NOW)
    jobs.save(conn, SIMPLIFY, [simplify_job(title="Machine Learning Engineer, Vision")], ROLES, LATER)
    assert rows(conn) == [("Machine Learning Engineer, Vision", "open")]


def test_same_title_from_another_source_is_merged_but_not_within_one_source(conn):
    workday = "https://acme.wd1.myworkdayjobs.com/jobs/1"
    jobs.save(conn, SIMPLIFY, [simplify_job(url=workday)], ROLES, NOW)
    jobs.save(conn, "greenhouse:acme", [job(company="ACME"), job(company="ACME", source_job_id="777")], ROLES, NOW)
    assert len(rows(conn)) == 2  # the board's first posting merged with Simplify's; its second is a separate job


def test_stored_job_is_updated_even_when_this_copy_would_not_match(conn):
    jobs.save(conn, SIMPLIFY, [simplify_job()], ROLES, NOW)
    jobs.save(conn, "greenhouse:acme", [job(title="Product Designer", description="Design.")], ROLES, LATER)
    assert rows(conn, "title, description") == [("Product Designer", "Design.")]


def test_company_board_decides_when_a_job_closes(conn):
    jobs.save(conn, SIMPLIFY, [simplify_job()], ROLES, NOW)
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, NOW)
    counts = jobs.save(conn, "greenhouse:acme", [], ROLES, LATER)  # gone from the board, still on Simplify
    assert (counts.unlisted, rows(conn)) == (1, [("Machine Learning Engineer", "no longer listed")])
    counts = jobs.save(conn, "greenhouse:acme", [job()], ROLES, LATER)  # back on the board
    assert (counts.updated, rows(conn)) == (1, [("Machine Learning Engineer", "open")])
    jobs.save(conn, SIMPLIFY, [simplify_job(listed=False)], ROLES, LATER)  # Simplify's inactive mark is overruled
    assert rows(conn) == [("Machine Learning Engineer", "open")]


def test_simplify_decides_for_jobs_only_it_knows(conn):
    workday = "https://acme.wd1.myworkdayjobs.com/jobs/1"
    jobs.save(
        conn, SIMPLIFY, [simplify_job(url=workday), simplify_job(source_job_id="s-2", url=workday + "2")], ROLES, NOW
    )
    counts = jobs.save(conn, SIMPLIFY, [simplify_job(url=workday)], ROLES, LATER)
    assert (counts.unlisted, [status for _, status in rows(conn)]) == (1, ["open", "no longer listed"])


def test_partial_list_closes_nothing(conn):
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, NOW)
    jobs.save(conn, "greenhouse:acme", [], ROLES, LATER, complete=False)
    assert rows(conn) == [("Machine Learning Engineer", "open")]


def test_simplify_jobs_links_are_open_manually_only_and_bad_links_skipped(conn):
    link = "https://simplify.jobs./p/5"  # a trailing dot reaches the same site
    counts = jobs.save(
        conn, SIMPLIFY, [simplify_job(url=link), simplify_job(source_job_id="x", url="ftp://x")], ROLES, NOW
    )
    assert (counts.skipped, rows(conn, "apply_url, open_manually_only")) == (1, [("https://simplify.jobs/p/5", 1)])


def test_failed_save_leaves_the_database_unchanged(conn, monkeypatch):
    jobs.save(conn, "greenhouse:acme", [job()], ROLES, NOW)

    def disk_full(*args):
        raise sqlcipher3.OperationalError("database or disk is full")

    monkeypatch.setattr(jobs, "_refresh_status", disk_full)
    with pytest.raises(sqlcipher3.OperationalError):
        jobs.save(conn, "greenhouse:acme", [job(), job(title="AI Engineer", source_job_id="9")], ROLES, LATER)
    assert rows(conn, "title, last_seen") == [("Machine Learning Engineer", NOW)]
    assert conn.execute("SELECT count(*) FROM job_sources").fetchone()[0] == 1


def test_non_english_text_and_quotes_are_stored_as_is(conn):
    odd = job(title="Ingénieur ML « IA » 機械学習 O'Brien", description='He said "RL"; DROP TABLE jobs;--')
    jobs.save(conn, "greenhouse:acme", [odd], ROLES, NOW)
    assert rows(conn, "title, description") == [(odd.title, odd.description)]
