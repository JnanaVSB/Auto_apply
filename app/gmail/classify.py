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
# The first label with a phrase in the e-mail wins, so the order matters: a rejection often thanks you for applying
# and may mention an interview; an interview invite often thanks you for applying too.
RULES = (
    ("rejection", ("unfortunately", "not moving forward", "not to move forward", "will not be moving forward",
                   "won't be moving forward", "decided to pursue other candidates", "regret to inform",
                   "position has been filled", "not been selected")),
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
SPACES = re.compile(r"\s+")
QUOTES = str.maketrans("\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}", "''")  # as phones type them


def job_system(domain: str) -> bool:
    """Whether the sender's domain is a job system's (or one of its subdomains), never a lookalike."""
    return any(domain == system or domain.endswith("." + system) for system in JOB_SYSTEMS)


def classify(domain: str, subject: str, snippet: str) -> str:
    """The e-mail's label from its subject and preview (as Gmail sends it, HTML-escaped); with no phrase, "unclear"
    from a job system, else "other"."""
    text = SPACES.sub(" ", f"{subject} {html.unescape(snippet)}".casefold().translate(QUOTES))
    for label, phrases in RULES:
        if any(phrase in text for phrase in phrases):
            return label
    return "unclear" if job_system(domain) else "other"


def sender(from_header: str) -> tuple[str, str]:
    """The From line's display name and its address's domain (lower case); ("", "") when there is no usable address."""
    name, address = parseaddr(from_header)
    _, at, domain = address.rpartition("@")
    return (name, domain.lower()) if at and domain else ("", "")
