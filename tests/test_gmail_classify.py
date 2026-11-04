"""The rules that sort an e-mail by its sender, subject and preview (made-up e-mails only)."""

import pytest

from app.gmail.classify import classify, job_like, job_system, sender

BOARD = "boards.greenhouse.io"  # a job system's sender
SHOP = "news.example.com"  # anyone else


@pytest.mark.parametrize(
    "subject, snippet, label",
    [
        ("Your application to Acme", "Unfortunately we will not be moving forward.", "rejection"),
        ("Offer from Acme", "We are pleased to offer you the role of ML Intern.", "offer"),
        ("Next steps", "We would like to schedule an interview with you next week.", "interview"),
        ("Acme coding challenge", "Please complete the HackerRank test within 5 days.", "assessment"),
        ("Thank you for applying to Acme", "We have received your application.", "confirmation"),
    ],
)
def test_each_label_has_its_phrases(subject, snippet, label):
    assert classify(BOARD, subject, snippet) == label


def test_a_rejection_wins_over_thanks_and_an_interview_mention():
    snippet = "Thank you for applying and for your interview. Unfortunately we decided to pursue other candidates."
    assert classify(BOARD, "Your application", snippet) == "rejection"


def test_an_interview_invite_that_thanks_you_for_applying_is_an_interview():
    assert classify(BOARD, "Thank you for applying", "We would like to invite you to interview.") == "interview"


def test_offering_benefits_is_not_an_offer():
    assert classify(SHOP, "Join us", "We offer great benefits and an assessment of your skills.") == "other"


@pytest.mark.parametrize("domain, label", [(BOARD, "unclear"), ("acme.myworkdayjobs.com", "unclear"), (SHOP, "other")])
def test_with_no_phrase_a_job_system_is_unclear_and_anyone_else_other(domain, label):
    assert classify(domain, "Update on your candidacy", "Please log in to see the status.") == label


@pytest.mark.parametrize("domain, label", [(BOARD, "unclear"), (SHOP, "other")])
def test_another_language_is_left_to_the_local_model_only_from_a_job_system(domain, label):
    assert classify(domain, "Tu solicitud", "Lamentablemente no seguiremos con tu candidatura.") == label


def test_case_spacing_and_curly_apostrophes_do_not_matter():
    assert classify(BOARD, "UPDATE", "We’re  sorry:\nwe are NOT   MOVING\tFORWARD.") == "rejection"
    assert classify(BOARD, "Update", "We won’t be moving forward.") == "rejection"


def test_the_preview_is_read_as_gmail_sends_it_html_escaped():
    assert classify(BOARD, "Update", "we won&#39;t be moving forward &amp; wish you well") == "rejection"


@pytest.mark.parametrize(
    "domain, found",
    [("greenhouse.io", True), ("us.greenhouse.io", True), ("evilgreenhouse.io", False),
     ("greenhouse.io.example.com", False), ("us.greenhouse-mail.io", False), ("", False)],
)  # fmt: skip
def test_a_job_system_is_its_own_domain_or_under_it_never_a_lookalike(domain, found):
    assert job_system(domain) is found


@pytest.mark.parametrize(
    "header, found",
    [("Acme Recruiting <no-reply@Boards.Greenhouse.io>", ("Acme Recruiting", "boards.greenhouse.io")),
     ('"Doe, Jane" <jane@example.com>', ("Doe, Jane", "example.com")),
     ("not an address", ("", "")), ("", ("", "")), ("Someone <@>", ("", ""))],
)  # fmt: skip
def test_the_sender_is_the_display_name_and_the_address_s_domain(header, found):
    assert sender(header) == found


@pytest.mark.parametrize(
    "snippet, label",
    [("Thank you for your interest in Acme. We have decided to move forward with other candidates.", "rejection"),
     ("Thank you for applying. We have decided not to proceed with your application.", "rejection"),
     ("Thanks for applying. We will not be advancing your application.", "rejection"),
     ("Unfortunately our interviewer is sick; can we move it to Thursday?", "unclear")],
)  # fmt: skip
def test_common_ways_of_saying_no_are_rejections_and_unfortunately_alone_is_unclear(snippet, label):
    assert classify(SHOP, "Your application", snippet) == label


@pytest.mark.parametrize(
    "snippet, label",
    [("Congratulations! After meeting many other candidates, we are pleased to offer you the role.", "offer"),
     ("Quick note: we will not be moving the time, see you at 10.", "other"),
     ("You will join other candidates for a group exercise.", "other"),
     ("We would like to schedule an interview. If you prefer not to proceed, just reply.", "interview"),
     ("We are pleased to offer you the role, and regret to inform you the start date moved.", "unclear")],
)  # fmt: skip
def test_good_news_is_never_read_as_a_rejection_and_mixed_news_is_unclear(snippet, label):
    assert classify(SHOP, "Next steps", snippet) == label


@pytest.mark.parametrize(
    "snippet",
    ["Unfortunately, you did not pass to the next round of our process.",
     "Unfortunately we won't be able to invite you to interview at this time.",
     "Unfortunately the coding challenge results did not meet our bar.",
     "Thank you for applying. Unfortunately, we have filled the role.",
     "Unfortunately we cannot extend an offer at this time."],
)  # fmt: skip
def test_unfortunately_without_a_clear_no_is_unclear_whatever_else_it_says(snippet):
    assert classify(SHOP, "Your application", snippet) == "unclear"


@pytest.mark.parametrize(
    "subject, job_like_",
    [("Your applications", True), ("Two interviews next week", True), ("Thanks for applying", True),
     ("Your job offer", True), ("Today's offer: 30% off", False), ("Your take-home pay for October", False)],
)  # fmt: skip
def test_job_words_include_plurals_but_not_a_shop_s_offer(subject, job_like_):
    assert job_like(subject, "") is job_like_
