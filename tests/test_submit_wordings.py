"""The permanent table of question wordings: what the sorting step and the rules must do with each one. Like the 45
e-mails Gmail's rules are checked against, it stays as the record. Every row says what you would want, not what the code
happens to do; a wording that doesn't fit a plain form goes to you (allow-list: one more question beats a wrong legal
answer)."""

import pytest

from app import questionnaire
from app.questionnaire import DISABILITY, VETERAN
from app.submit import Choice, Field, Form, match, record
from app.submit.match import LEGAL, ORDINARY, PROMISE, THIS_COMPANY


def person(**sections):
    """A made-up person: the reviewer's F-1 OPT example, with changes."""
    answers = questionnaire.Answers().model_dump(mode="json")
    answers["contact"].update(
        full_name="Alex Example", email="a@example.com", phone="5551234567", city="Tempe", state="AZ",
        country="United States", linkedin="https://linkedin.com/in/alex", portfolio="https://alex.example.com",
    )  # fmt: skip
    answers["work"].update(authorized_us="yes", sponsorship_now="no", sponsorship_future="yes", status="F-1 OPT")
    answers["availability"].update(relocate="yes", earliest_start="2027-01-15", notice_period="2 weeks")
    answers["salary"].update(minimum=120000, maximum=150000)
    answers["other"].update(over_18="yes")
    for section, values in sections.items():
        answers[section] = values if section == "more" else answers[section] | values
    return answers


F1 = person()
CITIZEN = person(work={"status": "citizen", "sponsorship_now": "no", "sponsorship_future": "no"})
EMPTY = questionnaire.Answers().model_dump(mode="json")
GRADUATE = person(education={"highest_degree": "Master's", "graduation": "2027-05", "gpa": "3.9"})
YES_NO = ("Yes", "No")
EVERY_CONSENT = dict.fromkeys(("privacy", "retention", "texting", "marketing"), "accept")


def row(question, answer):
    return {"question": question, "answer": answer, "asked_by": "Acme", "asked_on": "2026-11-01"}


SAVED = person(more=[
    row("Have you worked at [company] before?", "Yes"), row("Why [company]?", "Acme's mission is the reason"),
    row("Are you related to anyone at [company]?", "No"), row("Do you have a non-compete with [company]?", "No"),
    row("I authorize a background check", "Yes"), row("Please confirm you have read our Privacy Policy", "Yes"),
    row("Which is your favourite robot?", "The small one"),
    row("What is your favourite colour at work?", "I love Acme's blue"), row("Have you interviewed here before?", "No"),
])  # fmt: skip

# The outcomes: class, status, value shown, decision
LEFT = (PROMISE, "left", "", "")
EYE = (LEGAL, "decide", "", "unknown")  # a legal question that isn't on a plain form, or a long one: yours
ASKED = (ORDINARY, "decide", "", "unknown")
OURS = (THIS_COMPANY, "decide", "", "unknown")
CONSENT = (LEGAL, "decide", "", "consent")
EMPTY_LINE = (ORDINARY, "empty", "", "")


def q(label, kind="single", choices=YES_NO, help="", company="Acme", key="question_1", part="company", required=True,
      answers=F1, consents=None, kept=None):  # fmt: skip
    return {"label": label, "kind": kind, "choices": choices if kind in ("single", "multi") else (), "help": help,
            "company": company, "key": key, "part": part, "required": required, "answers": answers,
            "consents": consents, "kept": kept}  # fmt: skip


def text(label, **more):
    return q(label, "text", **more)


def long(label, **more):
    return q(label, "long_text", **more)


ACKNOWLEDGE = ("Please review and acknowledge Cloudflare's Candidate Privacy Policy "
               "(cloudflare.com/candidate-privacy-notice/).")  # fmt: skip
POLARITY = (
    "Can you work in the U.S. without sponsorship?",
    "Are you able to work in the United States without visa sponsorship now or in the future?",
    "Will you be able to work for us without requiring sponsorship?",
    "We are unable to sponsor visas for this role. Do you acknowledge?",
    "This role is not eligible for visa sponsorship. Do you understand?",
    "Do you need work authorization to work in the United States?",
    "Do you currently require work authorization?",
    "Do you have unrestricted authorization to work in the United States?",
    "Are you authorized to work in the U.S. on a permanent basis?",
    "Are you legally authorized to work for any employer in the U.S. without restriction?",
    "Are you legally authorized to work in the country in which this job is located?",
    "Are you authorized to work in the country where you are applying?",
    "Are you authorized to work in the UK?",
    "Are you authorized to work in the EU?",
    "Do you have the right to work in Europe?",
    "Are you authorized to work in Ontario?",
    "Are you eligible to work in EMEA?",
    "Do you have work authorization in LATAM?",
    "Do you require sponsorship to work in the UK?",
    "Do you hold the right to work in APAC?",
    "Do you currently have a work permit or visa that is tied to a sponsor?",
    "Are you currently sponsored by your employer?",
    "Would you need relocation assistance?",
    "Do you require relocation assistance?",
    "Are you under 18 years of age?",
    "Are you willing to obtain a security clearance?",
    "Have you ever held a security clearance?",
)
ROWS = [
    # The research's real AI-use and arbitration wordings, and the reviewer's m1.py
    (q("AI Policy for Application"), *LEFT),
    (q("Candidate AI Usage Attestation", choices=("I agree",)), *LEFT),
    (q("Please read the arbitration agreement below", help="Arbitration text...", choices=("Yes",)), *LEFT),
    (q("Agreement to Arbitrate", choices=("I agree",)), *LEFT),
    (long("Agreement to Arbitrate"), *LEFT),
    (long("Please read the arbitration agreement below"), *LEFT),
    (q("Do you opt in to WhatsApp messages"), *CONSENT),
    (q("Consent to Privacy Notice (Japan)"), *CONSENT),
    (q("Did you use A.I. tools to prepare this application?"), *LEFT),
    (q("Did you use ChatGPT or a similar tool for this application?"), *LEFT),
    (q("Have you used LLMs in preparing your materials?"), *LEFT),
    (q("Generative tools policy"), *LEFT),
    (q("GenAI usage policy"), *LEFT),
    (q("I confirm that I have read the AI partnership guidelines for candidates"), *LEFT),
    (q("Use of artificial-intelligence tools"), *LEFT),
    (q("I will not use generative tools during interviews, I attest"), *LEFT),
    (q("Attestation"), *CONSENT),
    (q("I understand that the use of AI in interviews is prohibited"), *LEFT),
    (q("AI Usage Acknowledgement"), *LEFT),
    (q("Acknowledgment of AI policy"), *LEFT),
    (q("Acknowledged: AI guidelines"), *LEFT),
    (q("Mutual Arbitration Agreement"), *LEFT),
    (q("Arbitration Acknowledgement"), *LEFT),
    (q("Dispute resolution agreement"), *LEFT),
    (q("I agree to binding arbitration"), *LEFT),
    (q("Class action waiver"), *LEFT),
    # A skill question about AI is ordinary; anything about how you applied is a promise
    (long("Describe your experience with AI"), ORDINARY, "write", "", ""),
    (long("What AI projects have you built?"), *ASKED),
    (long("Describe your experience using AI on this application"), *LEFT),
    (long("What interests you about AI?"), ORDINARY, "write", "", ""),
    (long("What excites you most in generative AI research?"), ORDINARY, "write", "", ""),
    # A company's name and the AI check: dropped only when that stays honest
    (long("Why Scale AI?", company="Scale AI"), THIS_COMPANY, "write", "", ""),
    (long("Why Mistral AI?", company="Mistral AI"), THIS_COMPANY, "write", "", ""),
    (q("AI policy for applicants at Scale AI", company="Scale AI"), *LEFT),
    (long("Why AI?", company="AI"), *LEFT),
    (long("Why GPT?", company="GPT"), *LEFT),
    (q("Agree to the AI policy", company="AI"), *LEFT),
    (q("I agree to arbitrate", company="Arbitrate"), *LEFT),
    # A promise can sit in the help, or in the only choice
    (q("I agree", help="I will not use AI tools during the interview process.", choices=("I agree",)), *LEFT),
    (q("I acknowledge the terms above", "consent", help="By ticking you agree to binding arbitration of all disputes."),
     *LEFT),
    (q("Please confirm", "consent", help="AI attestation", part="privacy", key="consent[custom]"), *LEFT),
    (q("Please choose", choices=("I will not use AI tools", "Other")), *LEFT),
    # The plain forms: the only wordings a legal topic is answered on
    (q("Are you legally authorized to work in the United States?"), LEGAL, "fill", "Yes", ""),
    (q("Are you authorized to work in the U.S.?"), LEGAL, "fill", "Yes", ""),
    (q("Are you legally authorized to work in the United States? Yes/No"), LEGAL, "fill", "Yes", ""),
    (q("Will you now or in the future require visa sponsorship?"), LEGAL, "fill", "Yes", ""),
    (q("Do you currently require sponsorship?"), LEGAL, "fill", "No", ""),
    (q("Will you now or in the future require visa sponsorship?", answers=CITIZEN), LEGAL, "fill", "No", ""),
    (q("Are you open to relocation?"), LEGAL, "fill", "Yes", ""),
    (q("Are you at least 18 years of age?"), LEGAL, "fill", "Yes", ""),
    (text("Security clearance level"), LEGAL, "fill", "none", ""),
    (q("Are you authorized to work in the United States?", help="Without sponsorship, that is."), *EYE),
    # Company names can't hide a word
    (q("Do you require visa sponsorship?", company="Visa"), LEGAL, "fill", "Yes", ""),
    (q("Are you authorized to work in the United States?", company="Work"), LEGAL, "fill", "Yes", ""),
    (long("Why Visa?", company="Visa"), *EYE),
    # The reviewer's m2.py: wordings that flip or narrow the meaning, for someone who needs sponsorship and someone who
    # doesn't: both go to you
    *((q(label), *EYE) for label in POLARITY),
    *((q(label, answers=CITIZEN), *EYE) for label in POLARITY),
    (q("Are you legally eligible to work and at least 18 years old?"), *EYE),
    (q("Do you meet the salary requirements listed?"), *EYE),
    (q("Have you ever been convicted of a felony?"), *EYE),
    (q("Have you worked at Acme before?"), *OURS),
    (text("Can you work in the U.S. without sponsorship?"), *EYE),
    (text("Are you willing to obtain a security clearance?"), *EYE),
    (text("Do you require visa sponsorship? If yes, which visa?"), *EYE),
    (text("Have you ever worked with an AI tool"), *OURS),  # "ever worked": this company's, on the safe side
    (text("Please provide the LinkedIn profile of your referrer"), *ASKED),
    (text("Website of your most recent employer"), *ASKED),
    (text("Link to your company's website"), *ASKED),
    (text("What is your blog's name?"), *ASKED),
    (text("Portfolio password (if any)"), *ASKED),
    # A fact never answers a yes-or-no question, whatever it is about
    (text("Do you meet the salary requirements?"), *EYE),
    (text("Are you a recent graduate?", answers=GRADUATE), *ASKED),
    (text("Do you have a GPA above 3.5?", answers=GRADUATE), *ASKED),
    (text("Can you start in January?"), *ASKED),
    (text("Is your notice period under a month?"), *ASKED),
    (q("Have you graduated?", answers=GRADUATE), *ASKED),
    (q("Salary expectations met"), *EYE),
    (text("What are your salary expectations?"), LEGAL, "fill", "120,000-150,000 USD per year", ""),
    (text("Expected graduation date", answers=GRADUATE), ORDINARY, "fill", "May 2027", ""),
    (text("When can you start?"), ORDINARY, "fill", "January 15, 2027", ""),
    (text("How did you hear about us?"), THIS_COMPANY, "fill", "Company website", ""),
    (text("What is your GPA?", answers=GRADUATE), ORDINARY, "fill", "3.9", ""),
    # Long boxes on legal matters are yours to write, never the Writer's
    (long("Are you authorized to work in the US? Please explain your visa status."), *EYE),
    (long("Describe your visa situation and whether you need sponsorship."), *EYE),
    (long("Have you ever been convicted of a crime? If so, explain."), *EYE),
    (long("Please disclose any non-compete agreements you have signed."), *EYE),
    (long("How did you use AI when preparing this application?"), *LEFT),
    (long("Do you certify the above is true?"), *EYE),
    (long("What is your gender and race?"), *EYE),
    (long("Why do you want to relocate?"), *EYE),
    (long("Tell us about a project you are proud of"), THIS_COMPANY, "write", "", ""),
    # The reviewer's m3.py: a remembered accept, on sentences that say the opposite or go further
    (q("I do not agree to receive marketing emails", "consent", consents=EVERY_CONSENT), *CONSENT),
    (q("I do not consent to text messages", consents=EVERY_CONSENT), *CONSENT),
    (q("Do you opt in to WhatsApp?", choices=("I do not consent",), consents=EVERY_CONSENT), *EYE),
    (q("I consent to the privacy policy", choices=("Decline",), consents=EVERY_CONSENT), *EYE),
    (q("I consent to the privacy policy of the third-party vendor and to sharing my data with advertisers",
       consents=EVERY_CONSENT), *CONSENT),  # fmt: skip
    (q("Please confirm you have read our Privacy Policy", consents=EVERY_CONSENT), *CONSENT),
    (q("I have read the privacy notice", "consent", consents=EVERY_CONSENT), *CONSENT),
    (q("I authorize a background check", consents=EVERY_CONSENT), *CONSENT),
    # m3.py: saved answers across companies, and an answer naming the company that gave it
    (q("Have you worked at Beta before?", company="Beta", answers=SAVED), *OURS),
    (text("Why Beta?", company="Beta", answers=SAVED), *OURS),
    (q("Are you related to anyone at Beta?", company="Beta", answers=SAVED), *OURS),
    (q("Do you have a non-compete with Beta?", company="Beta", answers=SAVED), *EYE),
    (q("I authorize a background check", company="Beta", answers=SAVED), *CONSENT),
    (q("Please confirm you have read our Privacy Policy", company="Beta", answers=SAVED), *CONSENT),
    (text("Which is your favourite robot?", company="Beta", answers=SAVED), ORDINARY, "suggested", "The small one", ""),
    (text("What is your favourite colour at work?", company="Beta", answers=SAVED), *ASKED),
    (q("Have you interviewed here before?", company="Beta", answers=SAVED), *OURS),
    # m3.py: your own pick must close the question
    (q("Your favourite?", choices=("Yes", "Yes, with conditions", "No"), kept={"question_1": ("Yes", "you")}),
     ORDINARY, "fill", "Yes", ""),
    (q("Your favourite?", choices=("Remote", "remote"), kept={"question_1": ("remote", "you")}),
     ORDINARY, "fill", "remote", ""),
    # m3.py and m4.py: an empty Questionnaire, and saved answers with no EEO values
    (q("Are you legally authorized to work in the United States?", answers=EMPTY), *EYE),
    (q("Will you require sponsorship?", answers=EMPTY), *EYE),
    (q("Do you have a disability?", answers=EMPTY), *EYE),
    (q("How did you hear about us?", choices=("Company website", "Other"), answers=EMPTY),
     THIS_COMPANY, "fill", "Company website", ""),
    (q("Disability status", part="eeo", key="disability_status", answers=EMPTY, required=False,
       choices=("Yes, I have a disability", "No, I don't have a disability", "I don't wish to answer")),
     LEGAL, "fill", "I don't wish to answer", ""),
    (q("Disability status", part="eeo", key="disability_status", answers={"contact": {}}, required=False,
       choices=("Yes, I have a disability", "No, I don't have a disability", "I don't wish to answer")), *EYE),
    (q("Are you Hispanic/Latino?", part="eeo", key="hispanic_ethnicity", answers={"eeo": {"race": ""}},
       required=False, choices=("Yes", "No", "Decline to self-identify")), *EYE),
    # The research's other real questions
    (q("Have you interviewed here before?"), *OURS),
    (long("Why Acme?"), THIS_COMPANY, "write", "", ""),
    (q("Are you subject to a non-compete agreement?"), *EYE),  # a fact about you, with a normal answer box
    (q("Terms agreement"), *EYE),
    (q("Do you hold a PMP certification?"), *ASKED),
    (q("Do you have a visa?", required=False), LEGAL, "empty", "", ""),
    (text("What is your earliest start date?"), ORDINARY, "fill", "January 15, 2027", ""),
    # Stripe's public form (job 8172510): every question, labels only
    (text("First Name", key="first_name", part="standard"), ORDINARY, "fill", "Alex", ""),
    (text("Last Name", key="last_name", part="standard"), ORDINARY, "fill", "Example", ""),
    (text("Email", key="email", part="standard"), ORDINARY, "fill", "a@example.com", ""),
    (text("Phone", key="phone", part="standard"), ORDINARY, "fill", "5551234567", ""),
    (q("Resume/CV", "file", key="resume", part="standard", required=False), *EMPTY_LINE),
    (q("Cover Letter", "file", key="cover_letter", part="standard", required=False), *EMPTY_LINE),
    (text("Who is your current or previous employer?", company="Stripe"), *ASKED),
    (text("What is your current or previous job title?", company="Stripe"), *ASKED),
    (q("Please select the country where you currently reside.", company="Stripe", choices=("Canada", "United States"),
       help="If you don't see your country in the list, please select 'Other'."), *ASKED),
    (q("Please select the country or countries you anticipate working in for the role in which you are applying.",
       "multi", company="Stripe", choices=("Canada", "United States")), *ASKED),
    (q("Are you authorized to work in the location(s) you selected in your previous response?", company="Stripe"),
     *EYE),
    (q("Will you require Stripe to sponsor you for a work permit now or in the future for the location(s) you selected "
       "in in your previous response?", company="Stripe"), *EYE),
    (q("If this role offers the option to work from a remote location, do you plan to work remotely?", company="Stripe",
       help="Stripe does not cover the cost of relocating to a remote location.",
       choices=("Yes, I intend to work remotely.", "No, I intend to work from an office location.")), *EYE),
    (q("Have you ever been employed by Stripe or a Stripe affiliate?", company="Stripe"), *OURS),
    (q("Do you opt-in to receive WhatsApp messages from Stripe Recruiting?", company="Stripe",
       help="Messages will only be sent regarding your candidacy with Stripe."), *CONSENT),
    (text("If located in the US, in what city and state do you reside?", company="Stripe"), *OURS),
    (q("Location", "location", key="location", part="location", company="Stripe"),
     ORDINARY, "fill", "Tempe, AZ, United States", ""),
    (q("VeteranStatus", key="veteran_status", part="eeo", required=False, company="Stripe",
       choices=("I don't wish to answer", "I am not a protected veteran")),
     LEGAL, "fill", "I don't wish to answer", ""),
    (q("Race", key="race", part="eeo", required=False, company="Stripe",
       choices=("Decline To Self Identify", "White", "Asian")), LEGAL, "fill", "Decline To Self Identify", ""),
    (q("Gender", key="gender", part="eeo", required=False, company="Stripe",
       choices=("Decline To Self Identify", "Female", "Male")), LEGAL, "fill", "Decline To Self Identify", ""),
    (q("School", key="school", part="education", company="Stripe", choices=()), *ASKED),
    (q("Degree", key="degree", part="education", company="Stripe", choices=()), *ASKED),
    (q("Discipline", key="discipline", part="education", company="Stripe", choices=()), *ASKED),
    # Cloudflare's public form (job 7695702): the questions Stripe's didn't have
    (text("Legal Name (if different than above)", company="Cloudflare", required=False), *EMPTY_LINE),
    (text("How did you hear about this job?", company="Cloudflare"), ORDINARY, "fill", "Company website", ""),
    (text("Would you like to include your LinkedIn profile, personal website or blog?", company="Cloudflare",
          required=False), *EMPTY_LINE),
    (q("Do you now or will you in the future require immigration sponsorship to work at Cloudflare?",
       company="Cloudflare"), *EYE),
    (q(ACKNOWLEDGE, "multi", company="Cloudflare", choices=("Acknowledge/Confirm",), help="Candidate Privacy Policy"),
     *CONSENT),
    (q(ACKNOWLEDGE, "multi", company="Cloudflare", choices=("Acknowledge/Confirm",), help="Candidate Privacy Policy",
       consents={"privacy": "accept"}), LEGAL, "fill", "Acknowledge/Confirm", ""),
    # Spotify's public Lever form (posting 2a55d6c9): the questions the others didn't have
    (text("Full name", key="name", part="standard", company="Spotify"), ORDINARY, "fill", "Alex Example", ""),
    (q("Pronouns", "multi", key="pronouns", part="standard", required=False, company="Spotify",
       choices=("He/him", "She/her", "They/them")), *EMPTY_LINE),
    (text("Current company", key="org", part="standard", company="Spotify"), *ASKED),
    (text("LinkedIn URL", key="urls[LinkedIn]", part="standard", required=False, company="Spotify"),
     ORDINARY, "fill", "https://linkedin.com/in/alex", ""),
    (text("Portfolio URL", key="urls[Portfolio]", part="standard", required=False, company="Spotify"),
     ORDINARY, "fill", "https://alex.example.com", ""),
    (text("Other website", key="urls[Other]", part="standard", required=False, company="Spotify"), *EMPTY_LINE),
    (q("Have you ever been previously employed by Spotify?", "multi", company="Spotify",
       help="Previous Spotify Employment", choices=("No", "Yes - Intern", "Yes - Full Time Employment")), *OURS),
    (q("What best describes your gender?", key="surveysResponses[s1][responses][field0]", part="demographic",
       required=False, company="Spotify", help="Demographic Questions for Spotify (for candidates in: US)",
       choices=("Woman", "Man", "Prefer not to disclose")), LEGAL, "fill", "Prefer not to disclose", ""),
    (q("Are you a veteran/have you served in the military?", key="surveysResponses[s1][responses][field2]",
       part="demographic", required=False, company="Spotify",
       help="Demographic Questions for Spotify (for candidates in: US)",
       choices=("Yes", "No", "Prefer not to disclose")), LEGAL, "fill", "Prefer not to disclose", ""),
    # The country list is the app's own: "AI" there is Anguilla, never a promise
    (q("What best describes your gender?", key="surveysResponses[s2][responses][field0]", part="demographic",
       required=False, company="Spotify", help="Demographic Question for Spotify (for candidates in: AL, DZ, AI, AQ)",
       choices=("Woman", "Man")), LEGAL, "empty", "", ""),
    (q("Spotify has my consent to contact me about future job opportunities.", "consent", key="consent[marketing]",
       part="privacy", required=False, company="Spotify"), *CONSENT),
]  # fmt: skip


def label_of(row):
    return row["label"][:60] if isinstance(row, dict) else None


def outcome(asked):
    """What the app does with a question, on a form for a job in the US: class, status, value shown, decision."""
    choices = tuple(Choice(text_, str(number)) for number, text_ in enumerate(asked["choices"]))
    field = Field(asked["key"], asked["label"], asked["kind"], asked["required"], asked["part"], asked["help"], choices)
    kept = {key: (*row, match.wording(field)) for key, row in (asked["kept"] or {}).items()}
    line = match.match(
        Form((field,)), asked["answers"], asked["company"], kept=kept, consents=asked["consents"], us_job=True
    )[0][0]
    return match.care(field, asked["company"]), line.status, record.shown(line), line.decision


@pytest.mark.parametrize(("asked", "kind", "status", "value", "decision"), ROWS, ids=label_of)
def test_every_wording_gets_the_answer_you_would_want(asked, kind, status, value, decision):
    assert outcome(asked) == (kind, status, value, decision)


NO_AI_HELP = [  # the reviewer's m3.py: every way of saying it must warn
    "AI tools are strictly prohibited during the interview process",
    "I confirm the materials I submit are my own work",
    "All submitted materials are the candidate's own.",
    "You must not use AI to write your answers",
    "Do not use AI when answering these questions.",
    "Applications written with AI will be rejected.",
    "We do not accept AI-generated applications.",
    "Please refrain from using generative AI in your application.",
    "Your answers should be written by you, without the help of ChatGPT.",
    "Answers must be your own.",
    "Answers must be written by you, not by an AI.",
    "No AI, please.",
    "The use of AI for these answers is not acceptable",
    "AI assistance is discouraged",
    "We ask that you not use AI tools",
]


@pytest.mark.parametrize("help_", NO_AI_HELP)
def test_every_way_of_saying_no_ai_warns(help_):
    form = Form((Field("question_1", "Policy", "long_text", True, "company", help_),))
    assert match.match(form, F1, "Acme")[1] == (match.NO_AI_WARNING,)


@pytest.mark.parametrize(
    ("label", "help_", "company"),
    [
        ("Why Scale AI?", "Not more than 100 words.", "Scale AI"),
        ("Tell us about your project", "", "Acme"),
    ],
)
def test_a_form_that_doesnt_forbid_ai_doesnt_warn(label, help_, company):
    assert match.match(Form((Field("question_1", label, "long_text", True, "company", help_),)), F1, company)[1] == ()


def test_the_veteran_and_disability_tables_still_answer_what_you_said():
    choices = tuple(Choice(text_, text_) for text_ in ("I am not a protected veteran", "I don't wish to answer"))
    field = Field("veteran_status", "VeteranStatus", "single", False, "eeo", choices=choices)
    answers = person(eeo={"veteran": VETERAN[1], "disability": DISABILITY[1]})
    assert record.shown(match.match(Form((field,)), answers, "Acme")[0][0]) == "I am not a protected veteran"


def test_your_own_words_warns_and_blocks_nothing():
    form = Form((Field("question_1", "Why Acme?", "long_text", True, "company", "In your own words."),))
    assert match.match(form, F1, "Acme")[1] == (match.YOUR_WORDS_WARNING,)
