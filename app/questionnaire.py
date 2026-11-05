"""Your standard answers for application forms, kept in the encrypted database. Not sent to any AI in Phase 4."""

import json
import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal

import sqlcipher3
import yaml
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from app.resume import CONTROL

NO_ANSWER = "I prefer not to answer"
GENDERS = ("Male", "Female", "Non-binary", NO_ANSWER)
RACES = (
    "Hispanic or Latino", "White", "Black or African American", "Asian", "Native Hawaiian or Other Pacific Islander",
    "American Indian or Alaska Native", "Two or more races", NO_ANSWER,
)  # fmt: skip
VETERAN = ("I am a protected veteran", "I am not a protected veteran", NO_ANSWER)
DISABILITY = ("Yes, I have a disability (or had one in the past)", "No, I do not have a disability", NO_ANSWER)
STATUSES = ("citizen", "permanent resident", "F-1 OPT", "STEM OPT", "H-1B", "other")
MAX_PAIRS = 300
OLD_MARKER = "[company]"  # in a More answers row from before questions naming a company became this-job-only
FULL = f"More answers is full ({MAX_PAIRS} rows): delete some on the Questionnaire page"
BROKEN = "Your saved Questionnaire breaks a rule; fix it on the Questionnaire page first: {}"
PAIR_ROW = re.compile(r"more\.(question|answer)\.([0-9]{1,3})")  # one "More answers" row on the form
WORDS = re.compile(r"[^\W_]+")  # a run of letters or digits
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE = re.compile(r"[0-9 +().-]+")
MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
SECTIONS = {
    "contact": "Contact", "work": "Work authorization", "availability": "Availability", "salary": "Salary",
    "education": "Education", "eeo": "EEO", "other": "Other", "consents": "Consents", "more": "More answers",
}  # fmt: skip
SAVE = """INSERT INTO questionnaire (id, answers, saved_on) VALUES (1, ?, ?)
    ON CONFLICT (id) DO UPDATE SET answers = excluded.answers, saved_on = excluded.saved_on"""
FILE_HEADER = "# Your auto_apply questionnaire answers. Keep this file somewhere safe: it is not encrypted.\n"


class QuestionnaireError(Exception):
    """Answers that break the rules. Messages name the field, never the value."""


def clean(text: str) -> str:
    """Wording to compare: lower case, every run of other characters one space."""
    return " ".join(WORDS.findall(text.lower()))


def _visible(text: str) -> str:
    if CONTROL.search(text):
        raise ValueError("contains an invisible (control) character")
    return text.strip()


def _email(text: str) -> str:
    if text and not EMAIL.fullmatch(text):
        raise ValueError("does not look like an email address")
    return text


def _phone(text: str) -> str:
    if text and not (PHONE.fullmatch(text) and 7 <= sum(c.isdigit() for c in text) <= 15):
        raise ValueError("needs 7 to 15 digits (spaces and + - ( ) . are fine)")
    return text


def _link(text: str) -> str:
    if text and (not text.startswith("https://") or any(c.isspace() for c in text)):
        raise ValueError("must be an https:// address without spaces")
    return text


def _month(text: str) -> str:
    if text and not MONTH.fullmatch(text):
        raise ValueError("must be a year and month, like 2027-05")
    return text


def _text(length: int) -> type[str]:
    return Annotated[str, Field(max_length=length), AfterValidator(_visible)]


Empty = BeforeValidator(lambda value: None if value == "" else value)  # an empty form field means "no answer"
Short, Line, Long = _text(100), _text(200), _text(2000)
Email = Annotated[str, Field(max_length=200), AfterValidator(_visible), AfterValidator(_email)]
Phone = Annotated[str, Field(max_length=30), AfterValidator(_visible), AfterValidator(_phone)]
Link = Annotated[str, Field(max_length=300), AfterValidator(_visible), AfterValidator(_link)]
Month = Annotated[str, AfterValidator(_visible), AfterValidator(_month)]
Day = Annotated[date | None, Empty]
Money = Annotated[Annotated[int, Field(ge=0, le=100_000_000)] | None, Empty]
YesNo = Literal["", "yes", "no"]
Consent = Literal["ask", "accept", "decline"]  # how a form's consent box is answered: asked each time, or remembered
Ticked = Annotated[bool, BeforeValidator(lambda value: value not in ("", "0", False))]  # a ticked box sends "1"


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contact(Section):
    full_name: Short = ""
    preferred_name: Short = ""
    email: Email = ""
    phone: Phone = ""
    city: Short = ""
    state: Short = ""
    country: Short = ""
    street: Line = ""
    linkedin: Link = ""
    github: Link = ""
    portfolio: Link = ""
    scholar: Link = ""
    other_link: Link = ""


class Work(Section):
    authorized_us: YesNo = ""
    status: Literal["", *STATUSES] = ""
    status_other: Short = ""
    sponsorship_now: YesNo = ""
    sponsorship_future: YesNo = ""
    permit_end: Day = None


class Availability(Section):
    earliest_start: Day = None
    notice_period: Short = ""
    relocate: Literal["", "yes", "no", "depends"] = ""
    remote: Ticked = False
    hybrid: Ticked = False
    on_site: Ticked = False
    locations: Long = ""  # one per line


class Salary(Section):
    minimum: Money = None
    maximum: Money = None
    per: Literal["year", "hour"] = "year"
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] = "USD"
    prefer_to_discuss: Ticked = False

    @model_validator(mode="after")
    def _in_order(self) -> "Salary":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("the minimum is above the maximum")
        return self


class Education(Section):
    highest_degree: Short = ""
    graduation: Month = ""
    gpa: Annotated[str, Field(max_length=20), AfterValidator(_visible)] = ""


class EEO(Section):
    gender: Literal[GENDERS] = NO_ANSWER
    race: Literal[RACES] = NO_ANSWER
    veteran: Literal[VETERAN] = NO_ANSWER
    disability: Literal[DISABILITY] = NO_ANSWER


class Other(Section):
    over_18: YesNo = ""
    clearance: Short = "none"
    heard_about: Short = "Company website"


class Consents(Section):
    """The consent kinds app/submit/wordings.py knows; AI-use promises and arbitration are always asked."""

    privacy: Consent = "ask"
    processing: Consent = "ask"
    retention: Consent = "ask"
    texting: Consent = "ask"
    marketing: Consent = "ask"


class Pair(Section):
    question: Annotated[str, Field(min_length=1, max_length=1_000), AfterValidator(_visible)]  # a form label's limit
    answer: Annotated[str, Field(max_length=2000), AfterValidator(_visible)] = ""  # empty: needs an answer
    asked_by: Short = ""  # the company whose form first asked it
    asked_on: Day = None


class Answers(Section):
    contact: Contact = Contact()
    work: Work = Work()
    availability: Availability = Availability()
    salary: Salary = Salary()
    education: Education = Education()
    eeo: EEO = EEO()
    other: Other = Other()
    consents: Consents = Consents()
    more: list[Pair] = Field(default=[], max_length=MAX_PAIRS)


def typed(form: dict[str, str]) -> dict:
    """The form's values as the page shows them: section -> field -> text, and the rows of More answers."""
    values: dict = {name: {} for name in SECTIONS if name != "more"}
    rows: dict[int, dict[str, str]] = {}
    for name, value in form.items():
        value = value.replace("\r\n", "\n")  # a browser sends a text area's new lines as \r\n
        if match := PAIR_ROW.fullmatch(name):
            rows.setdefault(int(match[2]), {})[match[1]] = value
        elif (section := name.partition(".")[0]) in values:
            values[section][name.partition(".")[2]] = value
    values["more"] = [  # a row counts when its question or answer has text
        row for _, row in sorted(rows.items()) if row.get("question", "").strip() or row.get("answer", "").strip()
    ]
    return values


def with_askers(values: dict, saved: list[dict]) -> dict:
    """The rows as typed, each with the company and day that first asked it, from the saved row with its wording: the
    page sends only questions and answers."""
    asked = {clean(str(row.get("question", ""))): row for row in reversed(saved)}
    for row in values["more"]:
        if found := asked.get(clean(row.get("question", ""))):
            row |= {"asked_by": found.get("asked_by") or "", "asked_on": found.get("asked_on") or ""}
    return values


def check(values: dict) -> Answers:
    try:
        return Answers.model_validate(values)
    except ValidationError as error:
        raise QuestionnaireError("; ".join(_problem(problem) for problem in error.errors())) from None


def _problem(problem: dict) -> str:
    where = [SECTIONS.get(str(part), str(part).replace("_", " ")) for part in problem["loc"]]
    if len(where) > 1 and problem["loc"][0] == "more":
        where[1] = f"row {int(where[1]) + 1}"
    return f"{', '.join(where)}: {problem['msg'].removeprefix('Value error, ')}"  # pydantic never puts the value in


def load(conn: sqlcipher3.Connection) -> tuple[dict, str | None, str | None]:
    """Your saved answers as the page shows them, when they were saved, and a problem if they break today's rules.

    Saved answers that break the rules (a rule made stricter by an update) are shown as they are, never changed.
    """
    row = conn.execute("SELECT answers, saved_on FROM questionnaire").fetchone()
    if row is None:
        return Answers().model_dump(mode="json"), None, None
    values = json.loads(row[0])
    try:
        return check(values).model_dump(mode="json"), row[1], None
    except QuestionnaireError as error:
        return values, row[1], str(error)


def save(conn: sqlcipher3.Connection, answers: Answers) -> None:
    with conn:  # all or nothing
        conn.execute(SAVE, (answers.model_dump_json(), datetime.now(UTC).isoformat(timespec="seconds")))


def remember(
    conn: sqlcipher3.Connection, label: str, company: str, answer: str = "", today: date | None = None
) -> None:
    """Keep a form's ordinary question in your More answers, with `answer` if given. One row per wording: a row keeps
    the first company and day that asked it, and an answer it holds is never changed. Only for a question of class
    ORDINARY: the caller asks `wordings.care`."""
    values, _, problem = load(conn)
    if problem:
        raise QuestionnaireError(BROKEN.format(problem))
    asked, answer = clean(label), answer.strip()
    rows = [row for row in values["more"] if OLD_MARKER not in row["question"]]
    if row := next((row for row in rows if clean(row["question"]) == asked), None):
        if row["answer"] or not answer:
            return
        row["answer"] = answer
    elif len(values["more"]) >= MAX_PAIRS:
        raise QuestionnaireError(FULL)
    else:
        asked_on = (today or date.today()).isoformat()
        values["more"].append({"question": label, "answer": answer, "asked_by": company[:100], "asked_on": asked_on})
    save(conn, check(values))


def place(contact: dict) -> str:
    """Your city, state and country on one line, those you gave."""
    found = (contact.get(name) for name in ("city", "state", "country"))
    return ", ".join(text.strip() for text in found if isinstance(text, str) and text.strip())


def as_file(values: dict) -> str:
    return FILE_HEADER + yaml.safe_dump(values, sort_keys=False, allow_unicode=True)
