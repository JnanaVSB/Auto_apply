"""Every wording the second review tried, kept like the table in test_submit_wordings.py: what each must do on a form
for a job in the US. Nothing here fills without a full match."""

import random

import pytest
from test_submit_wordings import (
    ASKED,
    CONSENT,
    EVERY_CONSENT,
    EYE,
    LEFT,
    OURS,
    YES_NO,
    outcome,
    person,
    q,
)

from app.submit import Choice, Field, Form, match
from app.submit.match import LEGAL, ORDINARY, THIS_COMPANY

L, T, C = "long_text", "text", "consent"


def probed(**sections):
    """The reviewer's made-up person, with changes."""
    return person(**{
        "contact": {"full_name": "Robin Sample", "email": "r@example.com", "phone": "5550100200", "city": "Mesa",
                    "linkedin": "https://linkedin.com/in/robin", "portfolio": "https://robin.example.com"},
        "availability": {"earliest_start": "2027-02-01"},
        "salary": {"minimum": 90000, "maximum": 110000},
        "education": {"highest_degree": "Master's", "graduation": "2026-12", "gpa": "3.7"},
        "other": {"heard_about": "LinkedIn", "clearance": "None"},
        "eeo": {"gender": "Female", "race": "Asian", "veteran": "I am not a protected veteran",
                "disability": "No, I do not have a disability"},
    } | sections)  # fmt: skip


F1 = probed()
GC = probed(work={"status": "permanent resident", "sponsorship_now": "no", "sponsorship_future": "no"})
REUSED = probed(more=[{"question": "Why this company?", "answer": "Acme's mission is the reason",
                       "asked_by": "Acme Corp", "asked_on": "2026-11-01"}])  # fmt: skip


def r(label, kind="single", key="question_7", answers=F1, **more):
    return q(label, kind, key=key, answers=answers, **more)


ROWS = [
    # AI use, other words
    (r("Did you use Claude or Gemini to write any part of this?"), *LEFT),
    (r("Was any part of this submission machine-generated?"), *LEFT),
    (r("Were automated writing tools used in preparing these answers?"), *LEFT),
    (r("I certify that no automated tools were used to complete this form", choices=("I certify",)), *LEFT),
    (r("Do you use OpenAI products when applying for jobs?"), *LEFT),
    (r("Did you use Grammarly or any similar writing assistant?"), *LEFT),
    (r("I confirm a human wrote this", C), *CONSENT),
    (r("Human authorship statement", choices=("I confirm",)), *LEFT),
    (r("All answers here are my own work", C), *CONSENT),
    (r("Statement of original work", choices=("Yes",)), *LEFT),
    (r("Did a bot help you with this?"), *LEFT),
    (r("Did you have any assistance completing this application?"), *ASKED),
    (r("What tools did you use to prepare your application?", L), *ASKED),
    (r("Which tools helped you write this?", L), *ASKED),
    (r("Did any language model write this?", L), *LEFT),
    (r("Bots and scripts policy", choices=("I understand",)), *LEFT),
    (r("My experience is described honestly and no generative AI was involved"), *LEFT),
    (r("Describe your background. Was AI involved in writing this?", L), *LEFT),
    (r("Have you built projects with AI?", L), *ASKED),
    (r("Tell us about your research in machine learning and AI", L), *OURS),
    (r("What excites you about AI safety?", L), ORDINARY, "write", "", ""),
    (r("What is your view on the ethics of AI?", L), *LEFT),
    (r("Plz don't use A.I.", C), *LEFT),
    (r("No A.I. please", T), *LEFT),
    (r("Candidate attestation", C, help="I did not use Gemini or Claude for this"), *LEFT),
    (r("Candidate attestation", C, help="I did not use any automated writing tool for this"), *LEFT),
    # arbitration, other names
    (r("I waive my right to a jury trial", C), *LEFT),
    (r("Jury trial waiver", choices=("I accept",)), *LEFT),
    (r("Mutual Agreement to Resolve Disputes", choices=("I accept",)), *LEFT),
    (r("Any dispute shall be resolved by JAMS under its employment rules. Do you accept?"), *LEFT),
    (r("I agree to resolve all employment disputes through binding mediation", C), *LEFT),
    (r("I give up my right to sue in court", C), *LEFT),
    (r("Waiver of right to sue", choices=("I agree",)), *LEFT),
    (r("Dispute Resolution Program", choices=("I agree",)), *LEFT),
    (r("Conflict resolution agreement", C), *CONSENT),
    (r("Collective action waiver", choices=("I accept",)), *LEFT),
    (r("Employment Dispute Policy", C), *CONSENT),
    (r("Claims must be brought individually, not as part of any class or representative proceeding. I agree.", C),
       *LEFT),
    (r("AAA Employment Rules acknowledgment", choices=("I acknowledge",)), *LEFT),
    (r("Are you OK with resolving legal claims outside court?"), *LEFT),
    (r("Please read this: we settle all claims outside of court", L), *LEFT),
    (r("Terms", C, help="any dispute goes to a neutral third party whose decision is final"), *LEFT),
    # work: unrecognised topics (should be legal)
    (r("Can you work in the United States?"), *EYE),
    (r("Are you able to work in the US?"), *EYE),
    (r("Are you lawfully able to work in the US?"), *EYE),
    (r("Do you have employment authorization?"), *EYE),
    (r("Are you eligible for employment in the United States?"), *EYE),
    (r("Proof of eligibility to work in the U.S. can be provided on my first day"), *EYE),
    (r("Do you have a valid work permit?"), *EYE),
    (r("Do you hold a green card?"), *EYE),
    (r("Are you a U.S. person under export control laws (ITAR)?"), *EYE),
    (r("Are you a U.S. person?"), *EYE),
    (r("Are you on OPT or CPT?"), *EYE),
    (r("Do you hold an H-1B?"), *EYE),
    (r("Do you have an EAD?"), *EYE),
    (r("Are you a lawful permanent resident?"), *EYE),
    (r("Which best describes you?", choices=("US citizen", "Green card holder", "Visa holder", "Other")), *EYE),
    (r("Your status", choices=("I am authorized to work in the US", "I will need sponsorship")), *EYE),
    (r("Describe your employment authorization status", L), *EYE),
    (r("Please describe your work permit situation", L), *EYE),
    (r("Can you work in the United States? Please explain.", L), *EYE),
    (r("Explain your OPT and H-1B timeline", L), *EYE),
    (r("Is your employment eligibility time-limited? Explain.", L), *EYE),
    (r("Are you a U.S. person? Please explain.", L), *EYE),
    # sponsorship flipped / plain with twists
    (r("Will you NOT require sponsorship?"), *EYE),
    (r("Do you require no sponsorship?"), *EYE),
    (r("Would you need sponsorship from a future employer?"), *EYE),
    (r("Are you authorized to work in the US?", choices=("Yes, I am a US citizen or green card holder", "No")), *EYE),
    (r("Are you authorized to work in the US?", choices=("Yes, without any restrictions", "No, I need "
       "sponsorship")), *EYE),
    (r("Do you require sponsorship?", choices=("Yes, I need H-1B sponsorship", "No, I am a US citizen")), *EYE),
    (r("Will you now or in the future require sponsorship?", choices=("Yes, I hold an F-1 visa and need a "
       "transfer", "No")), *EYE),
    (r("Are you willing to relocate?", choices=("Yes, at my own expense", "No")), *EYE),
    (r("Are you 18 years or older?", choices=("Yes, and I can provide proof", "No")), *EYE),
    (r("Are you authorized to work in the US?", help="Answer Yes only if you are a citizen or green card holder."),
       *EYE),
    (r("Are you authorized to work in the US?", help="Excludes students on OPT or CPT."), *EYE),
    (r("Are you authorized to work in the US?", help="Selecting Yes means you hold permanent work rights."), *EYE),
    (r("Are you authorized to work in the US?", help="Temporary visa holders should select No."), *EYE),
    (r("Do you require sponsorship?", help="Select No only if you will never need it, including OPT extensions."),
       *EYE),
    (r("Do you require sponsorship?", help="STEM OPT counts as sponsorship here."), *EYE),
    (r("Are you eligible to work?", help="This role is based in Germany."), *EYE),
    (r("Are you eligible to work?", help="This job is in Toronto."), *EYE),
    (r("Are you authorized to work?"), LEGAL, "fill", "Yes", ""),
    (r("Are you legally authorized to work?"), LEGAL, "fill", "Yes", ""),
    (r("Do you have the right to work?"), LEGAL, "fill", "Yes", ""),
    (r("Do you require sponsorship?"), LEGAL, "fill", "Yes", ""),
    (r("Are you willing to relocate?", help="This role is in London."), *EYE),
    (r("Are you willing to relocate?", help="Relocation is at your own cost."), *EYE),
    (r("Do you hold a clearance?", help="TS/SCI only"), *EYE),
    # other countries
    (r("Do you have the right to work in Germany?"), *EYE),
    (r("Are you authorized to work in Canada?"), *EYE),
    (r("Are you authorized to work in Australia?"), *EYE),
    (r("Do you require sponsorship for a Blue Card?"), *EYE),
    (r("Do you need a Tier 2 visa?"), *EYE),
    (r("Do you hold a Schengen permit?"), *ASKED),
    (r("Do you have a Skilled Worker visa?"), *EYE),
    (r("Do you have Indefinite Leave to Remain?"), *EYE),
    (r("Do you have settled status in the UK?"), *EYE),
    (r("Are you an Indian citizen?"), *EYE),
    (r("Do you have a Canadian PR?"), *ASKED),
    (r("Do you have a valid SIN?"), *ASKED),
    (r("Can you legally work in Singapore?"), *EYE),
    # legal/personal not in the list
    (r("Have you ever been fired from a job?"), *EYE),
    (r("Have you ever been terminated for cause?"), *EYE),
    (r("Are there any pending charges against you?"), *EYE),
    (r("Have you ever pleaded guilty to an offence?"), *EYE),
    (r("Have you ever been sued by an employer?"), *EYE),
    (r("Do you have any restrictive covenants?"), *EYE),
    (r("Do you have a conflict of interest?"), *EYE),
    (r("Are you a government official?"), *EYE),
    (r("Are you pregnant?"), *EYE),
    (r("What is your religion?", T), *EYE),
    (r("What is your marital status?", T), *EYE),
    (r("How old are you?", T), *EYE),
    (r("What year were you born?", T), *EYE),
    (r("Are you transgender?"), *EYE),
    (r("Do you identify as LGBTQ+?"), *EYE),
    (r("Do you have any medical conditions?"), *EYE),
    (r("Do you need any accommodations?"), *EYE),
    (r("What is your nationality?", T), *EYE),
    (r("What is your current base pay?", T), *EYE),
    (r("What is your hourly rate?", T), *EYE),
    (r("Type your full legal name as your electronic signature", T), *EYE),
    (r("Signature", T), *EYE),
    (r("I accept the Terms and Conditions"), *CONSENT),
    (r("I declare the above is true and complete"), *CONSENT),
    (r("I understand that employment is at-will", choices=("I understand",)), *CONSENT),
    (r("Applicant Terms", choices=("I agree",)), *CONSENT),
    (r("I accept the Terms of Service", choices=("Accept",)), *CONSENT),
    (r("By submitting I swear under penalty of perjury that this is true", choices=("Yes",)), *CONSENT),
    (r("Have you ever been fired? If so, explain.", L), *EYE),
    (r("Explain any pending charges against you", L), *EYE),
    (r("Have you ever been sued by an employer? Explain.", L), *EYE),
    (r("Describe any medical conditions that may affect your work", L), *EYE),
    (r("Describe any accommodation you need", L), *EYE),
    (r("What is your target base pay?", L), *EYE),
    (r("What pay range are you looking for?", L), *EYE),
    (r("What hourly rate would you accept?", L), *EYE),
    (r("What is your nationality?", L), *EYE),
    (r("Have you ever been terminated? Please explain the circumstances.", L), *EYE),
    (r("Are you willing to take a drug test?"), *EYE),
    (r("Do you consent to a credit check?"), *CONSENT),
    (r("Do you consent to fingerprinting?"), *CONSENT),
    (r("Will you submit to a credit check?"), *EYE),
    (r("Will you submit to fingerprinting?"), *EYE),
    (r("Do you have a driver's license?"), *ASKED),
    (r("Do you own a car?"), *ASKED),
    # consents with everything remembered as accept
    (r("I consent to the privacy policy", C, consents=EVERY_CONSENT), LEGAL, "fill", "ticked", ""),
    (r("I authorize a background check and acknowledge the privacy notice", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I consent to the processing of my biometric data", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I consent to the processing of my personal data, including health and criminal records", C,
       consents=EVERY_CONSENT), *CONSENT),
    (r("I agree you may retain my data forever", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I agree you may retain my data indefinitely and transfer it outside the EU", C, consents=EVERY_CONSENT),
       *CONSENT),
    (r("I consent to Acme storing my data for 10 years", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I agree to the privacy policy and terms of service", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I agree to the privacy policy and to the recording of my interviews", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I acknowledge the privacy notice and that my employment is at-will", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I acknowledge the privacy policy and waive any claims", C, consents=EVERY_CONSENT), *LEFT),
    (r("I disagree with the privacy notice", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I object to the privacy notice", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I won't accept the privacy policy", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I can't agree to the privacy policy", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I cannot accept the privacy policy", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I revoke my consent to data retention", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I have not read the privacy notice", C, consents=EVERY_CONSENT), *CONSENT),
    (r("No, I do not want marketing emails", C, consents=EVERY_CONSENT), *CONSENT),
    (r("Stop sending me marketing emails", C, consents=EVERY_CONSENT), *CONSENT),
    (r("Please remove me from the newsletter", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I consent to SMS, autodialed calls and prerecorded messages", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I consent to text messages from recruiters and to disclosure of my number to vendors", C,
       consents=EVERY_CONSENT), *CONSENT),
    (r("I consent to marketing by any company in our group", C, consents=EVERY_CONSENT), *CONSENT),
    (r("I agree my data may be disclosed to service providers per the privacy policy", C, consents=EVERY_CONSENT),
       *CONSENT),
    (r("I agree my data may be transferred to our subsidiaries per the privacy policy", C, consents=EVERY_CONSENT),
       *CONSENT),
    (r("I agree to the privacy policy", choices=("I agree", "I do not agree"), consents=EVERY_CONSENT), LEGAL,
       "fill", "I agree", ""),
    (r("Privacy notice", choices=("Accept", "Decline"), consents=EVERY_CONSENT), *CONSENT),
    (r("Do you consent to data retention?", choices=("No thanks", "Yes please"), consents=EVERY_CONSENT), *CONSENT),
    (r("Do you consent to marketing?", choices=("Yes, send me everything", "No"), consents=EVERY_CONSENT), *CONSENT),
    (r("Do you consent to marketing?", choices=("Yes", "No", "Only by email"), consents=EVERY_CONSENT), *CONSENT),
    (r("May we text you?", C, consents=EVERY_CONSENT), *CONSENT),
    (r("Do you agree to us contacting you via WhatsApp?", consents=EVERY_CONSENT), *CONSENT),
    (r("Would you like to join our talent community?", consents=EVERY_CONSENT), *OURS),
    (r("Do you acknowledge you've read the Privacy Policy?", T, consents=EVERY_CONSENT), *EYE),
    (r("GDPR", C, key="gdpr", part="privacy", consents=EVERY_CONSENT), *CONSENT),
    (r("I confirm the data above is correct, and I consent to retention for 2 years", C, consents=EVERY_CONSENT),
       *CONSENT),
    (r("I acknowledge that a false answer here is grounds for termination, and that you will retain my data", C,
       consents=EVERY_CONSENT), *CONSENT),
    # company-specific without naming the company
    (r("Have you applied here before?"), *OURS),
    (r("Have you been employed here before?"), *OURS),
    (r("Are you a current employee?"), *OURS),
    (r("Are you a current or former employee?"), *OURS),
    (r("Former employee of this company or its subsidiaries?"), *OURS),
    (r("Do you own shares in this company?"), *OURS),
    (r("Why do you want to work here?", T), *OURS),
    (r("Why this company?", T), *OURS),
    (r("Why us?", T), *OURS),
    (r("Have you used our product?"), *OURS),
    (r("What is your favourite product of ours?", T), *OURS),
    (r("Do you have friends who work here?"), *OURS),
    (r("Which of our offices would you prefer?", choices=("Phoenix", "Austin")), *OURS),
    (r("Were you referred by an employee?"), *OURS),
    (r("Who referred you?", T), *OURS),
    (r("Are you related to a current employee?"), *OURS),
    (r("Do you know someone at the company?"), *OURS),
    (r("What do you love about this role?", T), *OURS),
    (r("How does this role fit your goals?", T), *OURS),
    (r("Have you interviewed with us in the past 6 months?"), *OURS),
    (r("Were you employed by us in the last year?"), *OURS),
    (r("Why are you interested in Acme?", T), *OURS),
    (r("Why Acme?", L), THIS_COMPANY, "write", "", ""),
    (r("Do you know the Acme CEO?"), *OURS),
    (r("Please name your Acme recruiter", T), *OURS),
    # yes/no-shaped facts
    (r("Is your GPA at least 3.5?"), *ASKED),
    (r("Does your highest degree match this role?"), *OURS),
    (r("Was your GPA above 3.0?"), *ASKED),
    (r("Were you a graduate of a US university?"), *OURS),
    (r("Has your notice period ended?"), *ASKED),
    (r("Must your start date be after June?", T), *ASKED),
    (r("Have you got a notice period?", T), *ASKED),
    (r("GPA above 3.5?", T), *ASKED),
    (r("Earliest start before January?", T), *ASKED),
    (r("Your start date is flexible, right?", T), *ASKED),
    (r("Would a salary of $70,000 meet your expectations?"), *EYE),
    (r("Is $80k within your salary requirements?", T), *EYE),
    (r("Salary expectations below 100k?", T), *EYE),
    (r("Your desired salary in GBP?", T), *EYE),
    (r("What is your current salary and salary expectation?", T), *EYE),
    (r("What was your salary expectation at your last job?", T), *EYE),
    (r("Is a pay cut compatible with your expected pay?", T), *EYE),
    (r("What is your start date at your current company?", T), *ASKED),
    (r("What university did you graduate from?", T), *ASKED),
    (r("Graduate degree field of study", T), *ASKED),
    (r("What was your high school GPA?", T), *ASKED),
    (r("Highest level of education of your parents", T), *ASKED),
    (r("Earliest date you could take our paid assessment", T), *OURS),
    (r("How did you hear about this role? Name the person", T), *OURS),
    (r("How did you find your last job?", T), *ASKED),
    (r("Notice period at your last employer, in months", T), *ASKED),
    (r("When can you start, given your visa status?", T), *EYE),
    (r("Earliest start date if we sponsor your visa", T), *EYE),
    (r("What salary do you expect, given your visa restrictions?", T), *EYE),
    (r("Website where your criminal record can be checked", T), *EYE),
    (r("Do you agree we may contact references on your LinkedIn?", T), *EYE),
    (r("Do you have a personal website?", T), *ASKED),
    # company 'Scale AI'
    (r("Why Scale AI?", L, company="Scale AI"), THIS_COMPANY, "write", "", ""),
    (r("Scale AI Policy for Applicants", L, company="Scale AI"), *LEFT),
    (r("Scale AI Use Attestation", L, company="Scale AI"), *LEFT),
    (r("Scale AI may not be used in this application", L, company="Scale AI"), *LEFT),
    (r("Have you used Scale AI to write this?", L, company="Scale AI"), *LEFT),
    (r("Have you worked at Scale AI before?", L, company="Scale AI"), *LEFT),
    # company 'Applied AI'
    (r("Applied AI use is prohibited in this form", L, company="Applied AI"), *LEFT),
    (r("Do you agree not to use Applied AI?", L, company="Applied AI"), *LEFT),
    (r("Why Applied AI?", L, company="Applied AI"), THIS_COMPANY, "write", "", ""),
    # company 'Open AI'
    (r("Open AI tools must not be used to write your answers", L, company="Open AI"), *LEFT),
    # company 'Gen AI'
    (r("Gen AI Use Attestation", L, company="Gen AI"), *LEFT),
    (r("Why Gen AI?", L, company="Gen AI"), *LEFT),
    # company 'AI'
    (r("Why AI?", L, company="AI"), *LEFT),
    (r("AI use policy", L, company="AI"), *LEFT),
    # company 'A.I. Labs'
    (r("Why A.I. Labs?", L, company="A.I. Labs"), *LEFT),
    (r("A.I. Labs attestation", L, company="A.I. Labs"), *LEFT),
    # company 'OpenAI'
    (r("Why OpenAI?", L, company="OpenAI"), *LEFT),
    (r("Did you use OpenAI to write this?", L, company="OpenAI"), *LEFT),
    # company 'C3.ai'
    (r("Why C3.ai?", L, company="C3.ai"), *OURS),  # the dot reads as a second sentence
    (r("Have you worked at C3.ai before?", L, company="C3.ai"), *LEFT),
    # company 'GPT'
    (r("Why GPT?", L, company="GPT"), *LEFT),
    (r("GPT use policy", L, company="GPT"), *LEFT),
    # company 'Copilot Inc'
    (r("Why Copilot?", L, company="Copilot Inc"), *LEFT),
    (r("Did you use Copilot?", L, company="Copilot Inc"), *LEFT),
    # company 'Generative Labs'
    (r("Why Generative Labs?", L, company="Generative Labs"), *LEFT),
    (r("Why Generative?", L, company="Generative Labs"), *LEFT),
    # company 'Arbitration Forums'
    (r("Why Arbitration Forums?", L, company="Arbitration Forums"), THIS_COMPANY, "write", "", ""),
    # company 'Visa'
    (r("Why Visa?", L, company="Visa"), *EYE),
    (r("Have you worked at Visa before?", L, company="Visa"), *EYE),
    (r("Do you require visa sponsorship?", L, company="Visa"), *EYE),
    # company 'Citizens Bank'
    (r("Why Citizens Bank?", L, company="Citizens Bank"), *EYE),
    # company 'Race Capital'
    (r("Why Race Capital?", L, company="Race Capital"), *EYE),
    # company 'Class Action Inc'
    (r("Why Class Action Inc?", L, company="Class Action Inc"), THIS_COMPANY, "write", "", ""),
    # company 'Agreement Co'
    (r("Why Agreement Co?", L, company="Agreement Co"), *EYE),
    # company 'Legal & General'
    (r("Why Legal & General?", L, company="Legal & General"), THIS_COMPANY, "write", "", ""),
    # company 'Work'
    (r("Are you authorized to Work?", L, company="Work"), *EYE),
    # company 'US Bank'
    (r("Are you authorized to work in the US?", L, company="US Bank"), *EYE),
    (r("Are you authorized to work at US Bank?", L, company="US Bank"), *EYE),
    (r("Why US Bank?", L, company="US Bank"), THIS_COMPANY, "write", "", ""),
    # company 'The Right To Work Co'
    (r("Why The Right To Work Co?", L, company="The Right To Work Co"), *EYE),
    # company 'Yes'
    (r("Are you authorized to work in the US?", L, company="Yes"), *EYE),
    # company 'I'
    (r("Why I?", L, company="I"), THIS_COMPANY, "write", "", ""),
    # company 'a'
    (r("Have you used a chatbot?", L, company="a"), *LEFT),
    # company 'use'
    (r("AI use policy", L, company="use"), *LEFT),
    (r("How do you use AI?", L, company="use"), *LEFT),
    # company 'policy'
    (r("AI policy", L, company="policy"), *LEFT),
    # company 'AI use'
    (r("AI use policy", L, company="AI use"), *LEFT),
    # company 'AI policy'
    (r("AI policy attestation", L, company="AI policy"), *LEFT),
    # company 'Application'
    (r("Application AI usage", L, company="Application"), *LEFT),
    # w5
    (r("Why do you want this job?", L, help="AI-generated content is grounds for disqualification."), *EYE),
    (r("Why do you want this job?", L, help="Applications created with AI tools won't be considered."), *EYE),
    (r("Why do you want this job?", L, help="Using ChatGPT isn't allowed."), *EYE),
    (r("Why do you want this job?", L, help="AI assistance is disallowed."), *EYE),
    (r("Why do you want this job?", L, help="Submissions produced by a chatbot will be discarded."), *EYE),
    (r("Why do you want this job?", L, help="Write this yourself, please."), *ASKED),
    (r("Why do you want this job?", L, help="We want to hear from you, not a bot."), *EYE),
    (r("Why do you want this job?", L, help="Human-written answers only."), *EYE),
    (r("Why do you want this job?", L, help="Please answer personally."), *ASKED),
    (r("Why do you want this job?", L, help="Originality matters: AI-assisted responses will be disqualified."),
       *EYE),
    (r("Why do you want this job?", L, help="We screen answers for AI use."), *EYE),
    (r("Why do you want this job?", L, help="AI detectors are used on every answer."), *EYE),
    (r("Why do you want this job?", L, help="Applicants must complete this on their own."), *ASKED),
    (r("Why do you want this job?", L, help="This must be your original work."), *EYE),
    (r("Why do you want this job?", L, help="Do not use Claude or Gemini."), *EYE),
    (r("Why do you want this job?", L, help="Use of LLMs is banned."), *EYE),
    (r("Why do you want this job?", L, help="Banned: ChatGPT"), *EYE),
    (r("Why do you want this job?", L, help="Answers from a language model will be rejected."), *EYE),
    (r("Why do you want this job?", L, help="Acme does not use AI to make hiring decisions."), *EYE),
    (r("Why do you want this job?", L, help="We never use AI in screening."), *EYE),
    (r("Why do you want this job?", L, help="No AI is used in our hiring process."), *EYE),
    (r("Why do you want this job?", L, help="You may use AI to fix spelling."), *EYE),
    (r("Why do you want this job?", L, help="AI is welcome here."), *EYE),
    (r("Why do you want this job?", L, help="Use your own judgement."), *ASKED),
    (r("Why do you want this job?", L, help="Upload your own resume here."), *OURS),
    (r("Why do you want this job?", L, help="Do you have your own transportation?"), *ASKED),
    (r("Why do you want this job?", L, help="If you have your own website, list it."), *ASKED),
    (r("Why do you want this job?", L, help="I am at my own expense."), *ASKED),
    (r("Why do you want this job?", L, help="Explain in your own words."), ORDINARY, "write", "", ""),
    # w6
    (r("Why do you want this job?", L, help="You must not use AI to write your answers"), *EYE),
    (r("Why do you want this job?", L, help="Do not use AI when answering these questions."), *EYE),
    (r("Why do you want this job?", L, help="Applications written with AI will be rejected."), *EYE),
    (r("Why do you want this job?", L, help="We do not accept AI-generated applications."), *EYE),
    (r("Why do you want this job?", L, help="Please refrain from using generative AI in your application."), *EYE),
    (r("Why do you want this job?", L, help="I confirm the materials I submit are my own work"), *ASKED),
    (r("Why do you want this job?", L, help="All submitted materials are the candidate's own."), *ASKED),
    (r("Why do you want this job?", L, help="Answers must be written by you, not by an AI."), *EYE),
    # eeo
    (r("Do you have a relative with a disability?", key="q1", part="demographic"), *EYE),
    (r("Do you care for someone with a disability?", key="q2", part="demographic"), *EYE),
    (r("Do you require an accommodation for a disability?", key="q3", part="demographic"), *EYE),
    (r("Do you NOT have a disability?", key="q4", part="eeo"), *EYE),
    (r("Are you a veteran's spouse?", key="q5", part="demographic"), *EYE),
    (r("Is your spouse Hispanic?", key="q6", part="demographic"), *ASKED),
    (r("What gender is your manager?", choices=("Male", "Female"), key="q7", part="demographic"), *EYE),
    (r("Do you identify as transgender?", key="q8", part="demographic"), *EYE),
    (r("Sexual orientation", choices=("Straight", "Gay", "Decline to self-identify"), key="q9",
       part="demographic"), *EYE),
    (r("Which pronouns?", choices=("she/her", "he/him"), key="gender_pronouns", part="demographic"), *EYE),
    (r("Race of your parents", choices=("Asian", "White"), key="q10", part="demographic"), *EYE),
    (r("Should we consider disability when grading the test?", key="q11", part="demographic"), *EYE),
    # co
    (r("Are you related to anyone at Acme? Please describe.", L), *OURS),
    (r("Have you previously worked at Acme? If so, explain.", L), *OURS),
    (r("Do you know anyone at Acme? List them.", L), *OURS),
    (r("Who at Acme referred you?", L), *OURS),
    (r("Do you own Acme stock?", L), *OURS),
    (r("Have you ever applied to Acme before? When?", L), *OURS),
    # files
    (r("Resume/CV", "file", help="AI-generated resumes are rejected", key="resume", part="standard"), ORDINARY,
       "decide", "", "choose_file"),
    (r("Email", T, help="We use AI to screen", key="email", part="standard"), ORDINARY, "fill", "r@example.com", ""),
    (r("Phone", T, help="We may send text messages", key="phone", part="standard"), ORDINARY, "fill", "5550100200",
       ""),
    # non-English
    (r("¿Está autorizado para trabajar en Estados Unidos?"), *ASKED),
    (r("Benötigen Sie ein Visum?"), *ASKED),
    (r("Acceptez-vous l'arbitrage obligatoire ?"), *ASKED),
    (r("Haben Sie KI verwendet?"), *ASKED),
    (r("Soft hyphen arbi\u00adtration agreement"), *EYE),
    (r("A\u200bI policy"), *LEFT),
    (r("\u0410rbitration agreement (Cyrillic A)"), *EYE),
    # company suffix
    (r("Why Acme?", L, company="Acme, Inc."), THIS_COMPANY, "write", "", ""),
    (r("Have you used Acme before?", company="Acme, Inc."), *OURS),
    (r("Do you use Acme products?", T, company="Acme, Inc."), *OURS),
    # yes with statement, permanent resident
    (r("Do you require sponsorship?", choices=("Yes", "No, I am a US citizen"), answers=GC), *EYE),
    (r("Are you authorized to work in the US?", choices=("Yes, I am a US citizen", "Yes, with a visa", "No"),
       answers=GC), *EYE),
    (r("Are you authorized to work in the US?", choices=("Yes, I am a US citizen", "No, I am not"), answers=GC),
       *EYE),
    # tick
    (r("Scale AI Use Attestation", C, company="Scale AI"), *LEFT),
    # acc
    (r("I acknowledge the privacy notice", choices=("Accept", "Decline"), consents=EVERY_CONSENT), LEGAL, "fill",
       "Accept", ""),
]  # fmt: skip


@pytest.mark.parametrize(("asked", "kind", "status", "value", "decision"), ROWS, ids=lambda row: _name(row))
def test_every_wording_the_review_tried(asked, kind, status, value, decision):
    assert outcome(asked) == (kind, status, value, decision)


def _name(row):
    return row["label"][:60] if isinstance(row, dict) else None


def test_an_answer_saved_at_another_company_about_this_company_is_never_reused():
    assert outcome(r("Why this company?", T, company="Beta", answers=REUSED)) == OURS


HELPS = [
    "AI-generated content is grounds for disqualification.",
    "Applications created with AI tools won't be considered.",
    "Using ChatGPT isn't allowed.",
    "AI assistance is disallowed.",
    "Submissions produced by a chatbot will be discarded.",
    "Write this yourself, please.",
    "We want to hear from you, not a bot.",
    "Human-written answers only.",
    "Please answer personally.",
    "Originality matters: AI-assisted responses will be disqualified.",
    "We screen answers for AI use.",
    "AI detectors are used on every answer.",
    "Applicants must complete this on their own.",
    "This must be your original work.",
    "Do not use Claude or Gemini.",
    "Use of LLMs is banned.",
    "Banned: ChatGPT",
    "Answers from a language model will be rejected.",
    "Acme does not use AI to make hiring decisions.",
    "We never use AI in screening.",
    "No AI is used in our hiring process.",
    "You may use AI to fix spelling.",
    "AI is welcome here.",
    "Use your own judgement.",
    "Upload your own resume here.",
    "Do you have your own transportation?",
    "If you have your own website, list it.",
    "I am at my own expense.",
    "You must not use AI to write your answers",
    "Do not use AI when answering these questions.",
    "Applications written with AI will be rejected.",
    "We do not accept AI-generated applications.",
    "Please refrain from using generative AI in your application.",
    "I confirm the materials I submit are my own work",
    "All submitted materials are the candidate's own.",
    "Answers must be written by you, not by an AI.",
]


@pytest.mark.parametrize("help_", HELPS)
def test_a_form_saying_this_has_no_write_answers_and_says_so(help_):
    essay = Field("question_7", "Why do you want this job?", L, True, "company", help_)
    lines, warnings = match.match(Form((essay,)), F1, "Acme")
    assert lines[0].status == "decide" and match.NO_AI_WARNING in warnings


@pytest.mark.parametrize(
    "promise",
    [
        Field("q1", "AI Policy for Application", "single", True, "company", choices=(Choice("Yes", "1"),)),
        Field("q2", "Candidate AI Usage Attestation", "single", True, "company", choices=(Choice("I agree", "1"),)),
        Field("q3", "Attestation", C, True, "company", "Use of LLMs is banned."),
        Field("q4", "Attestation", C, True, "company", "AI-generated content is grounds for disqualification."),
        Field("q5", "Note", C, True, "company", "Applications created with AI tools won't be considered."),
    ],
    ids=lambda field: field.key,
)
def test_a_promise_on_the_form_stops_the_writer_on_every_other_question(promise):
    essay = Field("q9", "Why this role?", L, True, "company")
    lines, warnings = match.match(Form((promise, essay)), F1, "Acme")
    assert [line.status for line in lines] == ["left", "decide"] and match.NO_AI_WARNING in warnings


def test_a_writer_draft_on_a_form_that_later_forbids_ai_stays_open_with_the_warning():
    essay = Field("q9", "Why this role?", L, True, "company")
    rule = Field("q1", "Rule", C, True, "company", "Do not use AI.")
    kept = {"q9": ("draft", "writer", match.wording(essay))}
    lines, warnings = match.match(Form((rule, essay)), F1, "Acme", kept=kept)
    assert (lines[1].status, lines[1].source, lines[1].note) == ("suggested", "Writer draft", match.DRAFT_UNDER_RULE)
    assert match.DRAFTS_WARNING in warnings


def test_a_writer_draft_on_a_question_that_is_now_legal_is_dropped():
    essay = Field("q9", "Why this role? Mention any visa needs", L, True, "company")
    kept = {"q9": ("draft", "writer", match.wording(essay))}
    assert match.match(Form((essay,)), F1, "Acme", kept=kept)[0][0].status == "decide"


WORDS = ("lantern", "copper", "meadow", "quartz", "harbor", "velvet", "thimble", "juniper", "sparrow", "gravel",
         "zorblat", "minqet", "drovash", "plenth", "skivver", "tornel", "ubrix", "wenkle")  # fmt: skip
EVERY_KIND = (
    "text",
    L,
    "single",
    "multi",
    C,
    "file",
    "unknown",
)  # never "location": that box is the site's own, by kind


def sentence(rng, most):
    return " ".join(rng.choices(WORDS, k=rng.randint(1, most)))


def made_up_field(rng, kind, part):
    labels = rng.choice([YES_NO, [sentence(rng, 3) for _ in range(rng.randint(2, 5))]])
    choices = tuple(Choice(label, str(number)) for number, label in enumerate(labels))
    return Field(f"question_{rng.randint(1, 9999)}", sentence(rng, 9), kind, rng.random() < 0.5, part,
                 rng.choice(["", sentence(rng, 12)]), choices if kind in ("single", "multi") else ())  # fmt: skip


@pytest.mark.parametrize("kind", EVERY_KIND)
@pytest.mark.parametrize("part", ["company", "standard", "privacy", "eeo", "demographic"])
def test_random_words_are_never_filled_written_or_suggested(kind, part):
    rng = random.Random(f"{kind} {part}")  # noqa: S311 (made-up wordings, the same on every run)
    for _ in range(100):
        field = made_up_field(rng, kind, part)
        line = match.match(Form((field,)), F1, "Acme", consents=EVERY_CONSENT, us_job=True)[0][0]
        assert line.status in ("decide", "empty") and not line.value and not line.source, field


def test_a_question_kept_to_answer_once_suggests_nothing_until_you_answer_it():
    waiting = probed(more=[{"question": "Thimble meadow?", "answer": "", "asked_by": "Acme", "asked_on": "2026-11-01"}])
    assert outcome(r("Thimble meadow?", T, answers=waiting)) == ASKED
