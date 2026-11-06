"""Lever application forms, from the job's public apply page (jobs.lever.co/<board>/<id>/apply): Lever has no
official way to read its questions. Only the form's elements, their labels and each custom question's own copy of
itself (a JSON template in the page) are read; the page's scripts are skipped and nothing in it ever runs."""

import json
import re
from html.parser import HTMLParser

from app.fetch import Fetcher, FetchError, Moved, NotFound
from app.submit import BROKEN, Choice, Drawn, Field, Form, FormClosed, FormError, distinct

FORM_PAGE = "https://jobs.lever.co/{}/{}/apply"
DRAWN = Drawn()
STANDARD = {  # Lever's own inputs, by name
    "name": "text", "email": "text", "phone": "text", "location": "location", "org": "text", "comments": "long_text",
    "resume": "file", "pronouns": "multi",
}  # fmt: skip
BOT_CHECKS = frozenset({"h-captcha-response", "g-recaptcha-response"})  # the site's own, whatever element holds it
PREFIXES = {"urls[": ("text", "standard"), "consent[": ("consent", "privacy"), "eeo[": ("single", "eeo")}
CARD_KINDS = {
    "text": "text", "textarea": "long_text", "multiple-choice": "single", "multiple-select": "multi",
    "dropdown": "single",
}  # fmt: skip
TEMPLATE = re.compile(r"(cards|surveysResponses)\[([^\]]+)\]\[baseTemplate\]")
CARD_INPUT = re.compile(r"(?:cards|surveysResponses)\[[^\]]+\](?:\[responses\])?\[field\d+\]")  # read from templates
SURVEY = "countrySurvey_"  # a voluntary survey's section, followed by the countries it is for
UNREADABLE = "Lever's page could not be read: try Read again later, or apply by hand with the lines below."
CLOSED = "This posting is closed or was removed (Lever says not found)."
MOVED = "Lever sent this posting somewhere else; it may be closed: check the job page."


def read(fetcher: Fetcher, board: str, job_id: str) -> tuple[Form, int]:
    """The job's form, and how many of its questions could not be read; `board` and `job_id` from
    sources.posting_of."""
    try:
        page = fetcher.get_text(FORM_PAGE.format(board, job_id))
    except NotFound:
        raise FormClosed(CLOSED) from None
    except Moved:
        raise FormError(MOVED) from None
    except FetchError as error:
        raise FormError(f"Lever did not answer: {error}") from None
    return parse(page)


def parse(page: str) -> tuple[Form, int]:
    """The form on Lever's apply page, and how many questions were skipped; FormError when it shows no question."""
    found = _Page()
    found.feed(page)
    found.close()
    fields: list[Field] = []
    skipped = 0
    for item in found.items:
        if isinstance(item, str):
            fields.append(_control(item, found.controls[item]))
            continue
        name, template, countries = item
        try:
            card = json.loads(template)
            entries = card["fields"]
            if not isinstance(entries, list):
                raise TypeError("no list of fields")
        except (*BROKEN, RecursionError):  # RecursionError: nested too deep to read
            skipped += 1
            continue
        for number, entry in enumerate(entries):
            try:
                fields.append(_card_field(name, card, number, entry, countries))
            except BROKEN:
                skipped += 1
    if not any(field.kind != "hidden" for field in fields):
        raise FormError(UNREADABLE)
    kept, repeated = distinct(fields)
    return Form(kept), skipped + repeated


def _control(name: str, controls: list[dict]) -> Field:
    """One of the page's own inputs, from every element with its name (a group of boxes, or a hidden default beside
    a box, is one field); the first visible element gives its kind, label, accepted file types and help, the text
    beside it inside its field."""
    shown = [control for control in controls if control["type"] != "hidden"] or controls
    first = shown[0]
    kind, part = _kind(name, first)
    if kind == "hidden":  # never filled or asked: it gets no question's label or mark
        return Field(name, name, kind, False, part)
    choices = ()
    if kind in ("single", "multi"):
        values = [value for control in shown for value in control["options"]]
        choices = tuple(Choice(value, value) for value in dict.fromkeys(values))
    accept = tuple(filter(None, map(str.strip, first["accept"].split(",")))) if kind == "file" else ()
    required = first["marked"] or any(control["required"] for control in shown)
    help_ = " ".join("".join(first["beside"]).split())
    return Field(name, first["label"] or name, kind, required, part, help_, choices, accept)


def _kind(name: str, control: dict) -> tuple[str, str]:
    if name in BOT_CHECKS:
        return "hidden", "standard"
    if name in STANDARD:
        return STANDARD[name], "location" if name == "location" else "standard"
    for prefix, found in PREFIXES.items():
        if name.startswith(prefix):
            return found
    return "hidden" if control["type"] == "hidden" else "unknown", "standard"


def _card_field(name: str, card: dict, number: int, entry: dict, countries: str) -> Field:
    """Field `number` of a custom question card (part company) or a voluntary survey (part demographic)."""
    group, card_id = TEMPLATE.fullmatch(name).groups()
    label = entry["text"]
    if not isinstance(label, str) or not label.strip():
        raise ValueError("a question without a label")
    options = [option["text"] for option in entry.get("options") or []]
    if not all(isinstance(option, str) and option.strip() for option in options):
        raise ValueError("a choice without a label")
    title = _text(card.get("text"))
    said = (title if title != label.strip() else "", _text(card.get("instructions")), _text(entry.get("description")))
    help_ = " ".join(filter(None, said))
    if group == "cards":
        key, part = f"cards[{card_id}][field{number}]", "company"
    else:
        key, part = f"surveysResponses[{card_id}][responses][field{number}]", "demographic"
        help_ += f" (for candidates in: {countries})" if countries else ""
    choices = tuple(Choice(option, option) for option in options)
    kind = CARD_KINDS.get(entry["type"], "unknown")
    return Field(key, label, kind, entry.get("required") is True, part, help_.strip(), choices)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


class _Page(HTMLParser):
    """The form's named elements in page order, each with the label of the question it sits in, and the custom
    questions' templates. Text inside scripts and styles is skipped."""

    CONTROLS = frozenset({"input", "select", "textarea"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # attribute values arrive unescaped
        self.items: list[str | tuple[str, str, str]] = []  # an element's name, or (template name, JSON, countries)
        self.controls: dict[str, list[dict]] = {}
        self.label, self.marked = "", False  # the current question's label, and whether it shows the required mark
        self.beside: list[str] = []  # the current question's text inside its field, in no label of a box
        self.in_field, self.in_label = False, 0
        self.reading: list[str] | None = None  # a label's text while it is read
        self.mark = False
        self.select: dict | None = None  # the drop-down whose options are being read
        self.countries = ""
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self.skip += 1
            return
        found = {key: value or "" for key, value in attrs}
        classes = found.get("class", "").split()
        if "application-question" in classes or "application-form" in classes:  # a new question or section
            self.label, self.marked, self.reading = "", False, None
            self.beside, self.in_field = [], False
        if "application-form" in classes:  # a survey's countries hold for its own section only
            section = found.get("id", "")
            codes = section.removeprefix(SURVEY).replace("_", " ").split() if section.startswith(SURVEY) else []
            self.countries = ", ".join(codes)
        if "application-field" in classes:
            self.in_field, self.in_label = True, 0
        elif tag == "label":
            self.in_label += 1
        if "application-label" in classes or (tag == "label" and self.reading is None):
            self.reading, self.mark = [], False
        elif "required" in classes and self.reading is not None:
            self.mark = True
        if "application-field" in classes or "application-additional" in classes or tag in self.CONTROLS:
            self._end_label()
        if tag == "option" and self.select is not None and found.get("value", "").strip():
            self.select["options"].append(found["value"])
        if tag in self.CONTROLS:
            self._control(tag, found)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "label":
            self.in_label = max(0, self.in_label - 1)
            self._end_label()
        elif tag == "select":
            self.select = None

    def handle_data(self, data: str) -> None:
        if self.reading is not None and not self.skip:
            self.reading.append(data)
        elif self.in_field and not self.in_label and self.select is None and not self.skip:
            self.beside.append(data)

    def _end_label(self) -> None:
        """A label read with text becomes the current question's; an empty one (a box's own label) changes nothing."""
        if self.reading is not None and (text := " ".join("".join(self.reading).replace("✱", "").split())):
            self.label, self.marked = text, self.mark
        self.reading = None

    def _control(self, tag: str, found: dict[str, str]) -> None:
        name = found.get("name", "")
        if not name or CARD_INPUT.fullmatch(name):  # an element without a name is never sent
            return
        if TEMPLATE.fullmatch(name):
            self.items.append((name, found.get("value", ""), self.countries))
            return
        kind = found.get("type", "text").lower() if tag == "input" else tag
        boxes = [found["value"]] if kind in ("checkbox", "radio") and found.get("value", "").strip() else []
        control = {"type": kind, "required": "required" in found, "accept": found.get("accept", ""), "options": boxes,
                   "label": self.label, "marked": self.marked, "beside": self.beside}  # fmt: skip
        if name not in self.controls:
            self.items.append(name)
        self.controls.setdefault(name, []).append(control)
        if tag == "select":
            self.select = control
