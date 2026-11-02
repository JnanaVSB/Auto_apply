"""The cover letter: 3 to 5 paragraphs from the Writer about one job, from your entries (app/council/writer.py builds
the data the same way, through the privacy gate). Its answer is checked here before anything is saved; this computer
adds your name, contact line, the date, the greeting and the sign-off when the PDF is built."""

import json

from app.config import PROJECT_DIR
from app.council import writer
from app.council.writer import (
    DATA_RULE,
    LONG_DASH,
    BadAnswer,
    Checked,
    Entry,
    data,
    new_numbers,
    original,
    responses,
    revision,
)
from app.documents.privacy import PLACEHOLDER, PrivacyStop, Shield, last_check
from app.resume import CONTROL

SKILL_FILE = PROJECT_DIR / "skills" / "cover-letter.md"
RULES = DATA_RULE + """
Write only the letter's paragraphs: no greeting, sign-off, name, date or contact details, and never a placeholder such
as [ME] or [LINK-1]. Never write a long dash (— or –) or two hyphens (--): use a plain hyphen (-) or rewrite the
sentence. Answer only with JSON of this shape: {"paragraphs": ["...", "...", "..."], "from": ["E1", "E3"], "notes":
"...", "responses": []}. "from" lists the entries the letter draws on."""  # fmt: skip
SHAPE = {
    "type": "object",
    "properties": {
        "paragraphs": {"type": "array", "items": {"type": "string"}},
        "from": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "responses": writer.SHAPE["properties"]["responses"],  # replies to the council, as the resume's
    },
    "required": ["paragraphs", "from", "notes", "responses"],
    "additionalProperties": False,
}
FEWEST, MOST = 3, 5  # paragraphs
MAX_PARAGRAPH, MAX_NOTES = 2000, 2000  # only against a broken answer


def chosen(entries: list[Entry], keys: set | None) -> list[Entry]:
    """The entries a letter draws on: those your resume version chose (by source and entry id), relabelled E1, E2...
    in your resume's order; all of them when there is no such version (keys None)."""
    found = [entry for entry in entries if keys is None or (entry.source, entry.tile.tile_id) in keys]
    return [Entry(f"E{number}", entry.source, entry.tile) for number, entry in enumerate(found, 1)]


def request(
    title: str, company: str, location: str, description: str, entries: list[Entry], shield: Shield,
    previous: dict | None = None, feedback: str = "", council: str = "",
) -> tuple[str, str]:  # fmt: skip
    """The Writer's instructions for a letter, and its data (the job ad and your entries, wrapped as data); for a
    revision also the previous letter, your request and the council's objections."""
    instructions = f"{SKILL_FILE.read_text(encoding='utf-8').strip()}\n\n{RULES}"
    user = data(title, company, location, description, entries, shield, hints=False)
    if previous is None:
        return instructions, user
    text = "\n".join(f"P{number}: {paragraph}" for number, paragraph in enumerate(previous["paragraphs"], 1))
    return revision(instructions, user, shield, text, feedback, council, "letter")


def checked(text: str, entries: list[Entry], ad: str, objections: dict[str, str] | None = None) -> Checked:
    """The answer, checked, with its replies to the council's `objections` if it was sent any; raises BadAnswer saying
    what is wrong. Numbers in neither the entries it draws on nor the job ad are flagged."""
    try:
        found = json.loads(text)
    except (ValueError, RecursionError):  # RecursionError: nested too deep to read
        raise BadAnswer("the answer is not JSON") from None
    if not isinstance(found, dict) or set(found) != {"paragraphs", "from", "notes", "responses"}:
        raise BadAnswer('the answer must have exactly the fields "paragraphs", "from", "notes" and "responses"')
    replies, missed = responses(found["responses"], objections or {})
    paragraphs, cited, notes = found["paragraphs"], found["from"], found["notes"]
    if not isinstance(paragraphs, list) or not FEWEST <= len(paragraphs) <= MOST or not isinstance(notes, str):
        raise BadAnswer(f'"paragraphs" must hold {FEWEST} to {MOST} paragraphs, and "notes" must be text')
    labels = {entry.label: entry for entry in entries}
    known_labels = isinstance(cited, list) and all(isinstance(label, str) and label in labels for label in cited)
    if not known_labels or not cited or len(set(cited)) != len(cited):
        raise BadAnswer('"from" must list the labels (E1, E2, ...) of the entries the letter draws on, each once')
    letter = [_paragraph(number, paragraph) for number, paragraph in enumerate(paragraphs, 1)]
    known = " ".join([ad, *(original(labels[label]) for label in cited)])
    flags = [
        {"paragraph": number, "numbers": new}
        for number, paragraph in enumerate(letter)
        if (new := new_numbers(paragraph, known))
    ]
    draw = [{"source": labels[label].source, "tile_id": labels[label].tile.tile_id} for label in cited]
    notes = " ".join(CONTROL.sub("", notes).split())[:MAX_NOTES]
    return Checked({"paragraphs": letter, "from": draw, "notes": notes, "responses": replies, "missed": missed}, flags)


def _paragraph(number: int, paragraph: object) -> str:
    where = f"paragraph {number}"
    if not isinstance(paragraph, str):
        raise BadAnswer(f"{where} must be text")
    text = " ".join(paragraph.split())
    if not text or len(text) > MAX_PARAGRAPH or CONTROL.search(paragraph):
        raise BadAnswer(f"{where} is empty, far too long, or holds invisible characters")
    if LONG_DASH.search(text):
        raise BadAnswer(f"{where} uses a long dash (— or –) or '--': use a plain '-' or rewrite the sentence")
    if PLACEHOLDER.search(text):
        raise BadAnswer(f"{where} uses a placeholder: write no name or contact details, they are added separately")
    try:
        last_check(text)
    except PrivacyStop as stop:
        raise BadAnswer(f"{where} holds {stop}: never add one") from None
    return text
