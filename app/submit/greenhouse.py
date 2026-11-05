"""Greenhouse application forms, from the public job-board API (boards-api.greenhouse.io, ?questions=true): every
question with its choices, the location, EEO and voluntary questions, data consents and education. No browser."""

import html
from typing import Any

from app.fetch import Fetcher, FetchError, NotFound
from app.sources import html_to_text
from app.submit import Choice, Field, Form, FormClosed, FormError, distinct

FORM_API = "https://boards-api.greenhouse.io/v1/boards/{}/jobs/{}?questions=true"
KINDS = {
    "input_text": "text", "textarea": "long_text", "input_file": "file", "input_hidden": "hidden",
    "multi_value_single_select": "single", "multi_value_multi_select": "multi",
}  # fmt: skip
STANDARD = frozenset({"first_name", "last_name", "email", "phone", "resume", "cover_letter"})  # Greenhouse's own
TWINS = frozenset({"resume_text", "cover_letter_text"})  # typing the file's text instead: the app always uploads
ACCEPT = (".pdf", ".doc", ".docx", ".txt", ".rtf")  # the file types Greenhouse's documentation says it takes
CONSENTS = {
    "requires_consent": "Consent to the privacy notice ({})",
    "requires_processing_consent": "Consent to data processing ({})",
    "requires_retention_consent": "Consent to keeping your data ({})",
}
EDUCATION = {"school": "School", "degree": "Degree", "discipline": "Discipline"}
UNREADABLE = "Greenhouse's form for this job could not be read."
CLOSED = "This posting is closed or was removed (Greenhouse says not found)."
BROKEN = (AttributeError, KeyError, TypeError, ValueError)


def read(fetcher: Fetcher, board: str, job_id: str) -> tuple[Form, int]:
    """The job's form, and how many of its questions could not be read; `board` and `job_id` from
    sources.posting_of."""
    try:
        data, _ = fetcher.get_json(FORM_API.format(board, job_id))
    except NotFound:
        raise FormClosed(CLOSED) from None
    except FetchError as error:
        raise FormError(f"Greenhouse did not answer: {error}") from None
    return parse(data)


def parse(data: Any) -> tuple[Form, int]:
    """The form in Greenhouse's reply, and how many broken questions were skipped; FormError when it holds none."""
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list):
        raise FormError(UNREADABLE)
    eeo = [
        question
        for block in _list(data.get("compliance"))
        for question in _list(block.get("questions") if isinstance(block, dict) else None)
    ]
    voluntary = data.get("demographic_questions")
    sections = (
        (questions, _question, None),
        (data.get("location_questions"), _question, "location"),
        (eeo, _question, "eeo"),
        (voluntary.get("questions") if isinstance(voluntary, dict) else None, _voluntary, "demographic"),
        (data.get("data_compliance"), _consents, "privacy"),
    )
    fields: list[Field] = []
    skipped = 0
    for entries, read_one, part in sections:
        for entry in _list(entries):
            try:
                fields += read_one(entry, part)
            except BROKEN:
                skipped += 1
    if not any(field.part in ("standard", "company") for field in fields):
        raise FormError(UNREADABLE)
    kept, repeated = distinct(fields + _education(data.get("education")) + _ai_disclaimer(data.get("ai_disclaimer")))
    return Form(kept), skipped + repeated


def _question(entry: dict, part: str | None) -> list[Field]:
    """One question's fields (a question can hold two, such as an upload and its typed-text twin, which is left out).
    `part` None: Greenhouse's own fields are standard, the rest the company's."""
    label, required = _label(entry), entry.get("required") is True
    help_ = html_to_text(html.unescape(entry.get("description") or ""))
    found = []
    for item in entry["fields"]:
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("a field without a name")
        if name in TWINS:
            continue
        kind = KINDS.get(item["type"], "unknown")
        if part == "location" and kind != "hidden":
            kind = "location"  # a place picked from the site's suggestions; its coordinates stay hidden
        choices = tuple(_choice(value["label"], value["value"]) for value in item.get("values") or [])
        place = part or ("standard" if name in STANDARD else "company")
        found.append(Field(name, label, kind, required, place, help_, choices, ACCEPT if kind == "file" else ()))
    return found


def _voluntary(entry: dict, part: str) -> list[Field]:
    """A voluntary (demographic) question; an option you'd describe in your own words is still one choice."""
    choices = tuple(_choice(option["label"], option["id"]) for option in entry["answer_options"])
    kind = KINDS.get(entry["type"], "unknown")
    return [Field(f"demographic_{entry['id']}", _label(entry), kind, entry.get("required") is True, part, "", choices)]


def _consents(entry: dict, part: str) -> list[Field]:
    """The consents a data-protection entry (such as GDPR) really asks for: one field each."""
    rule = str(entry["type"])
    return [
        Field(f"data_compliance[{rule}][{flag}]", label.format(rule.upper()), "consent", True, part)
        for flag, label in CONSENTS.items()
        if entry.get(flag) is True
    ]


def _education(setting: Any) -> list[Field]:
    """School, degree and discipline, when the form has an education section; their choices come from the site."""
    if setting not in ("education_optional", "education_required"):
        return []
    required = setting == "education_required"
    return [Field(key, label, "single", required, "education") for key, label in EDUCATION.items()]


def _ai_disclaimer(text: Any) -> list[Field]:
    """The site's own words about AI, kept as a hidden box: a form's rule about AI is read from its fields."""
    words = html_to_text(html.unescape(text)) if isinstance(text, str) else ""
    return [Field("ai_disclaimer", "AI disclaimer", "hidden", False, "standard", words)] if words.strip() else []


def _choice(label: Any, value: Any) -> Choice:
    """A choice with a label and a value, else its question is broken (never a choice shown as "None")."""
    if not isinstance(label, str) or not label.strip() or value is None:
        raise ValueError("a choice without a label or value")
    return Choice(label, str(value))


def _label(entry: dict) -> str:
    label = entry["label"]
    if not isinstance(label, str) or not label.strip():
        raise ValueError("a question without a label")
    return label


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []
