"""Sort an e-mail by its sender, subject and Gmail's short preview, with fixed phrases: no AI, nothing sent anywhere.
A job system's e-mail that no phrase fits is left "unclear" for the local model (Phase 6 step 8)."""

import html
import re
from email.utils import parseaddr

LABELS = ("confirmation", "assessment", "interview", "offer", "rejection", "other")  # "unclear" waits for a decision
JOB_SYSTEMS = (  # where application e-mails come from; a sender counts if its domain is one of these or under one
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkday.com", "myworkdayjobs.com", "smartrecruiters.com",
    "icims.com", "jobvite.com", "workable.com", "bamboohr.com",
)  # fmt: skip
# Whole phrases only: a card moves only on a clear signal. A rejection phrase wins over a thank-you, but with an
# offer, interview or assessment phrase as well the e-mail is mixed and waits as "unclear". Otherwise the first label
# in this order wins: an interview invite often thanks you for applying too.
RULES = (
    ("rejection", ("not moving forward", "not to move forward", "will not be moving forward",
                   "won't be moving forward", "decided to pursue other candidates",
                   "move forward with other candidates", "decided to move forward with other",
                   "not to proceed with your application", "not be proceeding with your application",
                   "not be advancing your application", "regret to inform", "position has been filled",
                   "not been selected")),
    ("offer", ("pleased to offer", "offer letter", "extend an offer", "extend you an offer", "job offer")),
    ("interview", ("schedule an interview", "invite you to interview", "invitation to interview",
                   "interview invitation", "would like to interview", "phone screen", "next round",
                   "your availability for")),
    ("assessment", ("online assessment", "coding challenge", "coding assessment", "technical assessment",
                    "complete the assessment", "hackerrank", "codesignal", "codility", "take-home")),
    ("confirmation", ("thank you for applying", "thanks for applying", "application received",
                      "received your application", "application has been received",
                      "thank you for your application", "thank you for your interest in")),
)  # fmt: skip
# Words that make a company's own e-mail worth a closer look (whole words); without one it is just their mail
JOB_WORDS = re.compile(
    r"\b(applications?|applied|applying|candidates?|positions?|roles?|recruiters?|interviews?|assessments?|job offers?|"
    r"offer letters?)\b"
)
GOOD_NEWS = {"offer", "interview", "assessment"}
SPACES = re.compile(r"\s+")
QUOTES = str.maketrans("\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}", "''")  # as phones type them


def job_system(domain: str) -> bool:
    """Whether the sender's domain is a job system's (or one of its subdomains), never a lookalike."""
    return any(domain == system or domain.endswith("." + system) for system in JOB_SYSTEMS)


def job_like(subject: str, snippet: str) -> bool:
    """Whether the subject or preview has one of the job words."""
    return bool(JOB_WORDS.search(_text(subject, snippet)))


def classify(domain: str, subject: str, snippet: str) -> str:
    """The e-mail's label from its subject and preview (as Gmail sends it, HTML-escaped). Mixed news, or
    "unfortunately" without a clear no, is "unclear" (the local model or you decide); with no phrase, "unclear" from a
    job system, else "other"."""
    text = _text(subject, snippet)
    found = [label for label, phrases in RULES if any(phrase in text for phrase in phrases)]
    if "rejection" in found:
        return "unclear" if GOOD_NEWS.intersection(found) else "rejection"
    if "unfortunately" in text:  # it can open a no as well as a reschedule: never a move on its own
        return "unclear"
    if found:
        return found[0]
    return "unclear" if job_system(domain) else "other"


def _text(subject: str, snippet: str) -> str:
    """The subject and preview as the rules read them: lower case, plain apostrophes, single spaces."""
    return SPACES.sub(" ", f"{subject} {html.unescape(snippet)}".casefold().translate(QUOTES))


def sender(from_header: str) -> tuple[str, str]:
    """The From line's display name and its address's domain (lower case); ("", "") when there is no usable address."""
    name, address = parseaddr(from_header)
    _, at, domain = address.rpartition("@")
    return (name, domain.lower()) if at and domain else ("", "")
