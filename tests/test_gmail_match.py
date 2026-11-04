"""Matching an e-mail to a job on the board (made-up companies and e-mails)."""

import pytest
from helpers import add_job
from test_tracker import DAY, version

from app import tracker
from app.gmail.match import cards, match

ACME_ML = {"id": 1, "company": "Acme", "title": "ML Intern"}
ACME_ROBOTS = {"id": 2, "company": "Acme", "title": "Robotics Intern"}
GLOBEX = {"id": 3, "company": "Globex Inc.", "title": "Data Scientist"}
LOOKALIKE = "us.greenhouse-mail.io"  # not a job system
GREENHOUSE = "boards.greenhouse.io"


def test_the_company_s_own_domain_matches():
    assert match([ACME_ML, GLOBEX], "Recruiting", "acme.example", "Your application", None) == [1]


@pytest.mark.parametrize(
    "company, domain, found",
    [("Acme", "pacme.example", []), ("Acme Robotics", "recruiting.acme-robotics.example", [7]),
     ("Lever", "jobs.lever.co", [])],
)  # fmt: skip
def test_the_company_s_own_domain_is_a_whole_part_of_the_address_never_a_job_system(company, domain, found):
    assert match([{"id": 7, "company": company, "title": "ML Intern"}], "", domain, "Your application", None) == found


def test_a_job_system_naming_the_company_matches():
    assert match([ACME_ML, GLOBEX], "Greenhouse", GREENHOUSE, "Thank you for applying to Acme", None) == [1]


@pytest.mark.parametrize("domain", ["phish.example", LOOKALIKE])
def test_a_display_name_or_subject_from_an_unrelated_domain_never_decides(domain):
    assert match([ACME_ML, GLOBEX], "Acme Careers", domain, "Your Acme application", None) == []


def test_the_same_company_twice_is_decided_by_the_role_in_the_subject():
    assert match([ACME_ML, ACME_ROBOTS], "", "acme.example", "Your Robotics Intern application", None) == [2]


def test_the_same_company_twice_with_no_role_named_gives_both():
    assert match([ACME_ML, ACME_ROBOTS], "", "acme.example", "Your application", None) == [1, 2]


def test_a_thread_already_matched_keeps_its_job():
    assert match([ACME_ML, GLOBEX], "", "elsewhere.example", "Re: next week", 3) == [3]


def test_a_legal_ending_is_ignored():
    assert match([ACME_ML, GLOBEX], "", GREENHOUSE, "Globex: your application", None) == [3]


def test_a_two_letter_company_never_matches_a_domain_by_accident():
    assert match([{"id": 4, "company": "AI", "title": "ML Intern"}], "", "mail.example", "Hello", None) == []


def test_accents_do_not_matter():
    societe = {"id": 5, "company": "Société Générale", "title": "Quant Intern"}
    assert match([societe], "", GREENHOUSE, "Your application to Societe Generale", None) == [5]


def test_nothing_on_the_board_matches_nothing():
    assert match([], "Acme", "acme.example", "Acme", None) == []


def test_the_cards_are_the_jobs_the_board_shows(conn):
    with conn:
        ready, applied, skipped = (add_job(conn, title) for title in ("Ready", "Applied", "Skipped"))
        version(conn, ready)
        version(conn, skipped)
        conn.execute("UPDATE jobs SET not_interested = 1 WHERE id = ?", (skipped,))
        add_job(conn, "Never prepared")
    tracker.move(conn, applied, "ready", "applied", DAY)
    assert sorted(card["id"] for card in cards(conn)) == [ready, applied]
    assert {"id", "company", "title"} == set(cards(conn)[0])


def test_a_role_sharing_a_word_with_another_is_told_apart_by_its_other_words():
    assert match([ACME_ML, ACME_ROBOTS], "", "acme.example", "Acme: your ML Intern application", None) == [1]
