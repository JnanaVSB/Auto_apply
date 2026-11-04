"""The rules that sort an e-mail by its sender, subject and preview (made-up e-mails only)."""

import pytest

from app.gmail.classify import classify, job_like, job_system, sender

BOARD = "boards.greenhouse.io"  # a job system's sender
SHOP = "news.example.com"  # anyone else


@pytest.mark.parametrize(
    "subject, snippet, label",
    [
        ("Your application to Acme", "Unfortunately we will not be moving forward with your application.", "rejection"),
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
    assert classify(BOARD, "Update", "We won’t be moving forward with your application.") == "rejection"


def test_the_preview_is_read_as_gmail_sends_it_html_escaped():
    assert (
        classify(BOARD, "Update", "we won&#39;t be moving forward with your application &amp; wish you well")
        == "rejection"
    )


@pytest.mark.parametrize(
    "domain, found",
    [("greenhouse.io", True), ("us.greenhouse.io", True), ("evilgreenhouse.io", False),
     ("greenhouse.io.example.com", False), ("us.greenhouse-mail.io", True), ("greenhouse-mail.io.example", False),
     ("", False)],
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


GH, WD, LEVER, ACME = "greenhouse.io", "acme.myworkdayjobs.com", "jobs.lever.co", "acme.example"
# The quality review's made-up e-mails (Phase 6, Part A) and the label each must get: a wrong Rejected is the worst
# outcome, so anything soft, mixed or unsure is "unclear" and waits for the local model or you.
REVIEW_EMAILS = [
    ("c1", GH, "Thank you for applying to Acme", "We have received your application", "confirmation"),
    ("c2", WD, "Application Received", "Thank you for your application to the ML Intern position", "confirmation"),
    ("c3", ACME, "We received your application", "we've received your application", "confirmation"),
    ("c4", LEVER, "Your application to Acme", "Thanks for your interest in Acme!", "unclear"),
    ("a1", "hackerrank.com", "Acme | Online assessment invitation", "", "assessment"),
    ("a2", GH, "Please complete the CodeSignal assessment", "", "assessment"),
    ("a3", GH, "Your coding challenge results did not meet our bar", "", "unclear"),
    ("i1", ACME, "We'd like to invite you to interview", "", "interview"),
    ("i2", "calendar.google.example", "Invitation: Acme interview @ Tue", "", "other"),
    ("i3", ACME, "Unfortunately our interviewer is out sick; could we move your interview to Thursday?", "", "unclear"),
    ("i4", ACME, "Rescheduling your interview", "Could you share your availability for next week?", "interview"),
    ("i5", ACME, "We regret to inform you that we need to reschedule your interview to Friday", "", "unclear"),
    ("i6", ACME, "We regret to inform you that the hiring decision has been delayed by two weeks", "", "unclear"),
    ("i7", ACME, "We won't be moving forward until the hiring manager is back next week", "", "other"),
    ("i8", ACME, "We will not be moving forward until the hiring manager is back next week", "", "other"),
    ("o1", ACME, "Offer letter", "please find attached your offer letter", "offer"),
    ("o2", ACME, "We are excited to extend an offer", "", "offer"),
    ("o3", ACME, "We regret to inform you that we have to rescind the offer letter", "", "unclear"),
    ("o4", ACME, "After meeting many other candidates, we are pleased to offer you the role", "", "offer"),
    ("r1", GH, "Your application", "we have decided not to move forward with your application", "rejection"),
    ("r2", ACME, "We have decided to go in a different direction", "", "unclear"),
    ("r3", ACME, "Your candidacy", "We will not be moving forward with your candidacy", "rejection"),
    ("r4", GH, "Thank you for your interest in Acme", "we have chosen candidates whose experience more closely matches",
     "unclear"),
    ("r5", GH, "Thank you for applying", "We're sorry to let you know you were not selected", "unclear"),
    ("r6", GH, "We will not be inviting you to the next round", "", "unclear"),
    ("r7", GH, "We have decided not to advance you to the next round", "", "unclear"),
    ("r8", ACME, "Thanks for the phone screen", "decided to go with someone whose skills fit better", "unclear"),
    ("r9", GH, "Update", "decided to move forward with other applicants", "rejection"),
    ("r10", GH, "We are unable to offer you a position at this time", "", "unclear"),
    ("r11", ACME, "Following up", "Thank you for interviewing with us. We regret to inform you we will not extend an "
     "offer", "unclear"),
    ("r12", ACME, "The position has been filled. We will keep your resume on file", "", "unclear"),
    ("x1", "talentco.example", "Acme role", "would like to interview you this week", "interview"),
    ("n1", "careertips.example", "How to ace your next round of interviews", "", "other"),
    ("n2", "careertips.example", "Top 10 interview tips", "", "other"),
    ("p1", "amazon.example", "Your order of Cracking the Coding Interview", "", "other"),
    ("p3", "bank.example", "Good news about your application", "We are pleased to offer you a credit line", "other"),
    ("p5", "family.example", "Re: dinner", "Good luck with the interview tomorrow!", "other"),
    ("p6", "friends.example", "Did you hear back on your application?", "Hope you make the next round!", "other"),
    ("p7", "citybank.example", "Unfortunately your payment did not go through", "", "unclear"),
    ("p8", "state-u.example", "Your application status",
     "We regret to inform you that you have not been selected for admission", "unclear"),
    ("t1", "gmail.example", "Re: next week", "Thanks Jordan, talk soon!", "other"),
    ("t2", "gmail.example", "Re: summer", "the position has been filled for the summer cohort, but fall is open",
     "unclear"),
    ("s1", ACME, "Update", "We regret to inform you that our office is closed Monday, so your interview moves to "
     "Tuesday", "unclear"),
    ("s2", ACME, "Update", "We regret to inform you that our review is taking longer than expected", "unclear"),
    ("s3", ACME, "Update", "Thanks for completing the online assessment. You did not pass to the next round",
     "unclear"),
    ("s5", ACME, "Update", "You were not chosen for the next round", "unclear"),
    ("s7", ACME, "Update", "The position has been filled internally, but we'd love to consider you for a similar role",
     "unclear"),
    ("s8", ACME, "Thank you for applying", "Your application was not selected for further consideration", "unclear"),
    # The final check's 30 e-mails (f01-f30); a bank's "loan application" (f29) is accepted as a confirmation
    ("f01", "myworkday.com", "Brightwave - Application Submitted", "Hi Jordan, Your application for Data Analyst "
     "Intern (R-10423) has been submitted successfully.", "confirmation"),
    ("f02", "pinecrestanalytics.example", "Acknowledgement of your application", "We acknowledge the receipt of your "
     "application for the post of Junior ML Engineer. Kindly note that only shortlisted candidates will be intimated.",
     "other"),
    ("f03", "hire.lever.co", "Thanks for your application, Jordan", "Hi Jordan, thanks for applying to Kestrel Labs!",
     "confirmation"),
    ("f04", "us.greenhouse-mail.io", "Your application to Orbital Health", "After careful review, we have decided to "
     "move forward with candidates whose qualifications better align with our needs.", "unclear"),
    ("f05", "zentrix.example", "Data Scientist Intern - Application Outcome", "Having carefully considered all "
     "candidates, I am sorry to say that on this occasion you have not been successful.", "unclear"),
    ("f06", "ashbyhq.com", "Northwind application update", "We've decided not to move forward with your application "
     "at this time.", "rejection"),
    ("f07", "pinecrestanalytics.example", "Status of your candidature", "We regret that your profile has not been "
     "shortlisted for the further rounds.", "unclear"),
    ("f08", "myworkday.com", "Regarding your application for Clinical Data Intern", "Thank you for your interest in "
     "Orbital Health. We have decided to proceed with other applicants for this position.", "unclear"),
    ("f09", "zentrix.example", "Your offer from Zentrix", "We're delighted to offer you the position of Data Scientist "
     "Intern. Your offer letter is attached.", "offer"),
    ("f10", "pinecrestanalytics.example", "Offer Letter - Junior ML Engineer", "We are pleased to share your offer "
     "letter.", "offer"),
    ("f11", "zentrix.example", "Interview rescheduled: Data Scientist Intern", "we've moved your second-round "
     "interview to Thursday 3pm.", "other"),
    ("f12", "kestrellabs.example", "Interview cancelled", "the role has been put on hold due to a budget freeze.",
     "other"),
    ("f13", "northwind.example", "Next steps - Northwind", "Thanks for your application. We'd love to arrange a "
     "30-minute video call with you.", "other"),
    ("f14", "greenhouse.io", "Northwind: Please select a time for your phone screen", "pick a slot with our recruiter.",
     "interview"),
    ("f15", "codesignal.com", "Brightwave has invited you to take a CodeSignal assessment", "", "assessment"),
    ("f16", "orbitalhealth.example", "Take-home exercise for the Data Science Intern role", "please complete the "
     "attached take-home exercise.", "assessment"),
    ("f17", "hackerrankforwork.com", "Reminder: Kestrel Labs test expires in 24 hours", "", "unclear"),
    ("f18", "codility.com", "Northwind: your coding assessment results", "Thank you for completing the coding "
     "assessment for Northwind. We will not be progressing your application further.", "unclear"),
    ("f19", "zentrix.example", "Update on your offer", "Due to a hiring freeze we have to withdraw the offer of "
     "employment we made last week.", "unclear"),
    ("f20", "northwind.example", "Another role at Northwind you might like", "our platform team is hiring a Data "
     "Engineering Intern. If you're interested we can schedule an interview.", "unclear"),
    ("f21", "talentbridge.example", "Exciting Data Engineer role at a Series B fintech", "Open to a quick phone screen "
     "this week?", "interview"),
    ("f22", "linkedin.example", "30+ new Machine Learning Intern jobs in Austin, TX", "Be an early applicant.",
     "other"),
    ("f23", "linkedin.example", "Your application was viewed by Northwind", "viewed by the hiring team.", "other"),
    ("f24", "indeed.example", "Interview tips: 5 questions recruiters always ask", "", "other"),
    ("f25", "glassdoor.example", "See what candidates say about interviewing at Zentrix", "", "other"),
    ("f26", "family.example", "Sunday call", "how did the interview go? Dad says the offer letter from your cousin's "
     "company finally came through!", "offer"),
    ("f27", "gmail.example", "Referral?", "can you refer me for the analyst position at your company?", "other"),
    ("f28", "shop.example", "Your order has shipped", "Unfortunately one item in your order is delayed by two days.",
     "unclear"),
    ("f29", "bank.example", "Your loan application has been received", "", "confirmation"),
    ("f30", "icims.com", "Kestrel Labs - Software Engineering Intern", "we have selected other candidates whose "
     "experience is a closer fit.", "unclear"),
]  # fmt: skip


@pytest.mark.parametrize("name, domain, subject, snippet, label", REVIEW_EMAILS, ids=[e[0] for e in REVIEW_EMAILS])
def test_the_review_s_made_up_e_mails_get_their_labels(name, domain, subject, snippet, label):
    assert classify(domain, subject, snippet) == label


@pytest.mark.parametrize(
    "subject, snippet",
    [("Your candidacy", ""), ("Following up", "Thank you for interviewing with us."),
     ("Update", "The hiring manager will call you."), ("Update", "You interviewed with Sam last week.")],
)  # fmt: skip
def test_a_company_s_job_mail_without_the_old_job_words_still_counts(subject, snippet):
    assert job_like(subject, snippet)


@pytest.mark.parametrize("domain", ["hackerrank.com", "hackerrankforwork.com", "codesignal.com", "codility.com"])
def test_assessment_sites_count_as_job_systems(domain):
    assert job_system(domain)


@pytest.mark.parametrize("phrase", ["another role", "different role", "other opening", "other roles"])
def test_another_opening_at_the_company_never_moves_the_card_you_have(phrase):
    assert classify(ACME, "Hi", f"We'd like to schedule an interview for a {phrase}.") == "unclear"
