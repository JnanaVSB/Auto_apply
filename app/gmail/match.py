"""Which job on the board an e-mail is about: the same thread as before, else the company by its sender's domain, or by
name when a job system sent it. A display name or subject alone, from any other domain, never decides (anyone can
write "Acme Careers")."""

import re
import unicodedata

import sqlcipher3

from app import tracker
from app.gmail.classify import job_system

LEGAL = ("inc", "llc", "ltd", "corp", "corporation", "co", "company")  # the same list as actions.LEGAL_ENDINGS
CARDS = f"SELECT j.id, j.company, j.title {tracker.CARDS} WHERE {tracker.SHOWN}"  # noqa: S608 (fixed text)
WORD = re.compile(r"[a-z0-9]+")


def cards(conn: sqlcipher3.Connection) -> list[dict]:
    """The jobs an e-mail may be about: every card the board shows."""
    return [{"id": job_id, "company": company, "title": title} for job_id, company, title in conn.execute(CARDS)]


def words(text: str) -> list[str]:
    """Plain lower-case words, accents dropped ("Société" is "societe")."""
    return WORD.findall(unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower())


def company_key(company: str) -> list[str]:
    """The company's words without a legal ending ("Acme Inc." is "acme")."""
    found = words(company)
    return found[:-1] if len(found) > 1 and found[-1] in LEGAL else found


def match(cards: list[dict], name: str, domain: str, subject: str, thread_job: int | None) -> list[int]:
    """The ids of the jobs the e-mail may be about, in card order; one id is a confident match."""
    if any(card["id"] == thread_job for card in cards):
        return [thread_job]
    named_in = (words(subject), words(name))
    # The sender's own domain in parts ("recruiting.acme-robotics.example": recruiting, acmerobotics, example); a job
    # system's domain is no company's own
    own = set() if job_system(domain) else {part.replace("-", "") for part in domain.split(".")}
    counted = []
    for card in cards:
        key = company_key(card["company"])
        if len("".join(key)) >= 3 and "".join(key) in own:
            counted.append(card)
        elif key and job_system(domain) and any(_runs(key, text) for text in named_in):
            counted.append(card)
    if len(counted) > 1:  # the same company twice: the role the subject names best decides, if one does
        found = {card["id"]: _title_words_in(card["title"], set(named_in[0])) for card in counted}
        most = max(hits for hits, _ in found.values())
        top = [card for card in counted if found[card["id"]][0] == most]
        if len(top) == 1 and most * 2 >= found[top[0]["id"]][1] > 0:  # one role, at least half its words named
            counted = top
    return [card["id"] for card in counted]


def _runs(key: list[str], text: list[str]) -> bool:
    """Whether the key's words appear in the text as whole, consecutive words."""
    return any(text[start : start + len(key)] == key for start in range(len(text) - len(key) + 1))


def _title_words_in(title: str, subject_words: set[str]) -> tuple[int, int]:
    """How many of the title's words (2+ letters, so "ML" counts) are in the subject, and how many it has."""
    title_words = [word for word in words(title) if len(word) >= 2]
    return sum(word in subject_words for word in title_words), len(title_words)
