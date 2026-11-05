"""Each form field matched to an answer for the preview (Phase 7): what you kept for this job, your Questionnaire,
your saved More answers, your remembered consents and the job's newest PDFs. The first rule that fits wins, and
nothing is guessed: a choice is picked only when exactly one fits, else the field is yours to decide."""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import sqlcipher3

from app import places, questionnaire
from app.council import runs
from app.documents import pdf
from app.questionnaire import DISABILITY, MAX_PAIRS, MONTH, NO_ANSWER, RACES, SECTIONS, VETERAN, QuestionnaireError
from app.submit import Field, Form

STATUSES = ("fill", "empty", "hidden", "write", "decide", "left")  # left: you tick it yourself in the window
DECISIONS = {  # the "Decision required" reasons, as the preview words them
    "unknown": "Unknown question", "non_pdf": "Non-PDF upload", "choose_file": "A file you need to choose",
    "consent": "A promise or consent", "cover_letter": "Cover letter required",
}  # fmt: skip
WORDS = re.compile(r"[^\W_]+")  # a run of letters or digits
COMPANY = "[company]"  # stands for the company's name, so a saved answer fits the same question from any company
TICKED = "yes"  # a consent box to tick
IN_WINDOW, LEFT_EMPTY = "[in the window]", "[left empty]"  # your choice for one job's question, kept as its answer
FILL = "fill {} in your Questionnaire first"
NOT_A_CHOICE = "your {} is not one of the choices"
KEYS = {  # the sites' own fields, by key: (Questionnaire section, answer)
    "name": ("contact", "full_name"), "preferred_name": ("contact", "preferred_name"), "email": ("contact", "email"),
    "phone": ("contact", "phone"), "urls[LinkedIn]": ("contact", "linkedin"), "urls[GitHub]": ("contact", "github"),
    "urls[Portfolio]": ("contact", "portfolio"), "urls[Other]": ("contact", "other_link"),
}  # fmt: skip
WORDED = {  # a text field asked by its wording (exactly one must fit) -> its Contact answer
    "linkedin": places.whole_words("linkedin"), "github": places.whole_words("github"),
    "scholar": places.whole_words("scholar"), "portfolio": places.whole_words("portfolio|website|personal site|blog"),
    "preferred_name": places.whole_words("preferred (?:first )?name"),
}  # fmt: skip
RESUME = places.whole_words("resume|cv")
KNOWN = {  # a known question, by its cleaned wording
    "work": r"(?=.*\b(?:authori[sz]\w*|eligible|legally|right to work)\b)(?=.*\bwork\b)",
    "sponsorship": r"\bsponsor", "relocation": r"\brelocat",
    "start": r"\b(?:start date|earliest|available to start|when can you start)\b", "notice": r"\bnotice period\b",
    "salary": r"(?=.*\b(?:salary|compensation|pay)\b)(?=.*\b(?:expect\w*|desired|requirements?)\b)",
    "degree": r"\bhighest\b.*\b(?:degree|education)\b", "graduation": r"\bgraduat", "gpa": r"\b(?:gpa|grade point)\b",
    "over_18": r"\b(?:18 years|over 18|at least 18)\b", "clearance": r"\bclearance\b",
    "heard": r"\b(?:how did you hear|where did you hear|how did you find|referral source)\b",
}  # fmt: skip
KNOWN = {name: re.compile(pattern) for name, pattern in KNOWN.items()}
NOW, FUTURE = re.compile(r"\b(?:current|currently|now)\b"), re.compile(r"\bfuture\b")
US_NAMES = frozenset({"united states", "usa", "us"})
SURVEY_COUNTRIES = re.compile(r"\(for candidates in: ([^)]*)\)$")  # how app/submit/lever.py words a survey's help
TOPICS = (  # an EEO or voluntary question, by its key and wording; Hispanic before race ("hispanic ethnicity")
    ("hispanic", re.compile(r"\b(?:hispanic|latin[aox]?)\b")), ("race", re.compile(r"\b(?:race|racial|ethnic\w*)\b")),
    ("gender", re.compile(r"\b(?:gender|sex)\b")), ("veteran", re.compile(r"\bveteran")),
    ("disability", re.compile(r"\bdisabilit")),
)  # fmt: skip
DECLINE = re.compile(  # "do not want": the US disability form's own words
    r"\b(?:decline|don t (?:wish|want)|do not (?:wish|want)|prefer not|not to answer|not to disclose|choose not)\b"
)
GENDERS = {"Male": r"\b(?:male|man)\b", "Female": r"\b(?:female|woman)\b", "Non-binary": r"\bnon ?binary\b"}
GENDERS = {answer: re.compile(pattern) for answer, pattern in GENDERS.items()}
CONSENT_WORDS = re.compile(r"\b(?:consent\w*|agree|acknowledge|attest|certify)\b")
CONSENTS = (  # kind, as the preview names it, and its wording: the first that fits
    ("ai", "AI use", r"\b(?:ai|artificial intelligence|attest)\b"), ("arbitration", "arbitration", r"\barbitrat"),
    ("privacy", "privacy", r"\bprivacy (?:notice|policy)\b|\bdata processing\b"
     r"|\bprocessing of (?:my|your) (?:personal )?data\b"),
    ("retention", "retention", r"\b(?:retain|retention|gdpr|store)\b"),  # store: Lever's consent[store]
    ("texting", "texting", r"\b(?:text message|sms|whatsapp)"),
    ("marketing", "marketing", r"\b(?:marketing|newsletter|talent community)\b"),
)  # fmt: skip
CONSENTS = tuple((kind, name, re.compile(pattern)) for kind, name, pattern in CONSENTS)
ALWAYS_ASK = frozenset({"ai", "arbitration"})  # never answered from a remembered consent or a saved answer
PROMISES = tuple(pattern for kind, _, pattern in CONSENTS if kind in ALWAYS_ASK)
FREE_TEXT = frozenset({"salary", "start", "notice", "heard"})  # known questions a long-text box may ask
NO_AI = re.compile(  # on cleaned wording: "must be your own", "not AI-generated", "use of AI ... is prohibited"
    r"\b(?:must|should) be (?:entirely |solely )?(?:your|the applicant s|the candidate s) own\b"
    r"|\bnot (?:be )?(?:ai|artificial intelligence) (?:generated|written)\b"
    r"|\b(?:ai|artificial intelligence)\b(?: \w+){0,5} (?:is |are )?"
    r"(?:prohibited|not permitted|not allowed|forbidden)\b"
)
NO_AI_WARNING = ("This form asks that application materials be your own and not AI-written: write the long answers "
                 "yourself")  # fmt: skip
LIMIT_WARNING = "This company limits applications (its words: {})"
FULL = f"More answers is full ({MAX_PAIRS} rows): delete some on the Questionnaire page"
BROKEN = "Your saved Questionnaire breaks a rule; fix it on the Questionnaire page first: {}"


@dataclass(frozen=True)
class Line:
    """One preview line, in the form's order."""

    field: Field
    status: str  # one of STATUSES
    value: str | tuple[str, ...] = ""  # text, the chosen choices' values, or an upload's path
    source: str = ""  # where the value came from, e.g. "Questionnaire: Work authorization", "Resume v3"
    decision: str = ""  # a DECISIONS key when status is "decide"
    note: str = ""  # e.g. "split from your full name"


def clean(text: str, company: str = "") -> str:
    """Wording to compare: lower case, the company's name (or a written [company]) as [company], every run of other
    characters one space."""
    cleaned = f" {COMPANY} ".join(" ".join(WORDS.findall(part)) for part in text.lower().split(COMPANY))
    if name := " ".join(WORDS.findall(company.lower())):
        cleaned = re.sub(rf"(?<!\S){re.escape(name)}(?!\S)", COMPANY, cleaned)
    return " ".join(cleaned.split())


def match(
    form: Form,
    answers: dict,
    company: str,
    *,
    kept: dict[str, tuple[str, str]] | None = None,
    consents: dict[str, str] | None = None,
    files: dict[str, tuple[str, Path] | None] | None = None,
) -> tuple[tuple[Line, ...], tuple[str, ...]]:
    """Every field's line, and the warnings (not blocking). `answers` as questionnaire.load gives them, `kept` this
    job's own answers (key -> answer, 'you' or 'writer'), `consents` kind -> 'accept', 'decline' or 'ask', `files`
    from documents()."""
    matcher = _Matcher(answers, company, kept or {}, consents or {}, files or {})
    return tuple(map(matcher.line, form.fields)), _warnings(form, company)


def documents(conn: sqlcipher3.Connection, data_dir: Path, job_id: int) -> dict[str, tuple[str, Path] | None]:
    """The job's newest finished resume and cover letter, each (label, PDF), or None without one or without its
    file."""
    found: dict[str, tuple[str, Path] | None] = {}
    for kind, name in ((runs.KIND, "Resume"), (runs.LETTER, "Cover letter")):
        done = runs.last_done(conn, job_id, kind)
        path = pdf.tailored(data_dir, job_id, done["version"], kind) if done else None
        found[kind] = (f"{name} v{done['version']}", path) if path and path.is_file() else None
    return found


def remember(
    conn: sqlcipher3.Connection, label: str, company: str, answer: str = "", today: date | None = None
) -> None:
    """Keep a form's question in your More answers, the company's name saved as [company] so the same question from
    any company finds it, with `answer` if given. One row per wording: a row keeps the first company and day that
    asked it, and an answer it holds is never changed."""
    values, _, problem = questionnaire.load(conn)
    if problem:
        raise QuestionnaireError(BROKEN.format(problem))
    wording, answer = clean(label, company), answer.strip()
    if row := next((row for row in values["more"] if clean(row["question"], company) == wording), None):
        if row["answer"] or not answer:
            return
        row["answer"] = answer
    elif len(values["more"]) >= MAX_PAIRS:
        raise QuestionnaireError(FULL)
    else:
        asked_on = (today or date.today()).isoformat()
        values["more"].append({"question": _marked(label, company), "answer": answer, "asked_by": company[:100],
                               "asked_on": asked_on})  # fmt: skip
    questionnaire.save(conn, questionnaire.check(values))


def _marked(label: str, company: str) -> str:
    """The label with the company's name (whole words, any capitals and punctuation between) as [company]."""
    if not (words := WORDS.findall(company)):
        return label
    name = r"[\W_]+".join(map(re.escape, words))
    return re.sub(rf"(?<!\w){name}(?!\w)", COMPANY, label, flags=re.IGNORECASE)


def _warnings(form: Form, company: str) -> tuple[str, ...]:
    said = (clean(f"{field.label} {field.help}", company) for field in form.fields)
    found = [NO_AI_WARNING] if any(NO_AI.search(text) for text in said) else []
    if form.limit_note:
        found.append(LIMIT_WARNING.format(form.limit_note))
    return tuple(found)


class _Matcher:
    """The rules, in order, for one form's fields."""

    def __init__(self, answers: dict, company: str, kept: dict, consents: dict, files: dict) -> None:
        self.answers, self.company, self.kept, self.consents, self.files = answers, company, kept, consents, files
        self.saved: dict[str, str] = {}  # your More answers, by their cleaned question
        for pair in answers.get("more") or []:
            if isinstance(pair, dict) and isinstance(pair.get("answer"), str) and pair["answer"].strip():
                self.saved.setdefault(clean(str(pair.get("question", "")), company), pair["answer"])

    def line(self, field: Field) -> Line:
        if field.kind == "hidden":
            return Line(field, "hidden")
        words = clean(field.label, self.company)
        if found := self._kept(field, words):
            return found
        if field.kind == "unknown":  # a kind of field the app can't fill
            return _other(field)
        for rule in (self._standard, self._upload, self._known, self._eeo, self._saved, self._consent):
            if found := rule(field, words):
                return found
        if field.kind == "long_text" and field.part == "company":
            return Line(field, "write")
        return _other(field)

    def _kept(self, field: Field, _: str) -> Line | None:
        answer, given_by = self.kept.get(field.key, ("", ""))
        if not answer.strip():
            return None
        source = "your answer for this job" if given_by == "you" else "Writer draft"
        if answer == IN_WINDOW:
            return Line(field, "left", source=source, note="you do this in the window")
        if answer == LEFT_EMPTY:
            return Line(field, "empty", source=source, note="you chose to leave it empty")
        return self._given(field, answer, source, "your saved answer is no longer one of the choices")

    def _standard(self, field: Field, words: str) -> Line | None:
        if field.key in ("first_name", "last_name"):
            return self._name_part(field)
        if field.key == "comments":  # Lever's "Additional information": yours to write, or leave
            return Line(field, "empty")
        if field.kind == "location" or field.key in ("location", "city"):
            place = (self._answer("contact", name) for name in ("city", "state", "country"))
            return self._fill(field, ", ".join(filter(None, place)), "contact")
        if field.key in KEYS:
            return self._fill(field, self._answer(*KEYS[field.key]), KEYS[field.key][0])
        if field.kind != "text" or any(pattern.search(words) for pattern in KNOWN.values()):  # "hear (LinkedIn, ...)"
            return None
        fits = [name for name, pattern in WORDED.items() if pattern.search(words)]
        return self._fill(field, self._answer("contact", fits[0]), "contact") if len(fits) == 1 else None

    def _name_part(self, field: Field) -> Line:
        """First name: your full name up to its last space; last name: the rest."""
        full = self._answer("contact", "full_name")
        if not full:
            return _other(field, FILL.format(SECTIONS["contact"]))
        first, _, last = full.rpartition(" ")
        if field.key == "last_name" and not first:
            return Line(field, "decide", decision="unknown", note="your full name is one word")
        value = (first or last) if field.key == "first_name" else last
        return Line(field, "fill", value, f"Questionnaire: {SECTIONS['contact']}", note="split from your full name")

    def _upload(self, field: Field, words: str) -> Line | None:
        if field.kind != "file":
            return None
        if field.accept and not any("pdf" in kind.lower() for kind in field.accept):  # ".pdf" or "application/pdf"
            return Line(field, "decide", decision="non_pdf")
        if field.key == "cover_letter" or "cover letter" in words:
            slot = "cover_letter"
        elif field.key == "resume" or RESUME.search(words):
            slot = "resume"
        else:
            return Line(field, "decide", decision="choose_file") if field.required else Line(field, "empty")
        if found := self.files.get(slot):
            return Line(field, "fill", str(found[1]), found[0])
        if not field.required:
            return Line(field, "empty")
        if slot == "cover_letter":
            return Line(field, "decide", decision="cover_letter")
        return Line(field, "decide", decision="choose_file", note="press Prepare first")

    def _known(self, field: Field, words: str) -> Line | None:
        if field.kind not in ("text", "long_text", "single", "multi"):
            return None
        asked = [name for name, pattern in KNOWN.items() if pattern.search(words)]
        if len(asked) != 1:  # two known questions in one: never a guess
            return Line(field, "decide", decision="unknown", note="this asks more than one thing") if asked else None
        if field.kind == "long_text" and asked[0] not in FREE_TEXT:  # "Why do you want to relocate?": for the Writer
            return None
        if asked[0] in ("work", "sponsorship") and places.FOREIGN_NAMES.search(
            places.STATE_NAMES.sub(" ", field.label)
        ):
            return Line(field, "decide", decision="unknown", note="this asks about another country")
        section, answer = ANSWERS[asked[0]]
        value = answer(self._section(section), words)
        if value is None:
            return Line(field, "decide", decision="unknown", note="your Questionnaire says it depends")
        return self._fill(field, value, section)

    def _eeo(self, field: Field, words: str) -> Line | None:
        if field.part not in ("eeo", "demographic") or field.kind not in ("single", "multi"):
            return None
        if (survey := SURVEY_COUNTRIES.search(field.help)) and self._country() not in survey[1].split(", "):
            return Line(field, "empty", note=f"only for candidates in: {survey[1]}")
        about = f"{clean(field.key)} {words}"
        topic = next((name for name, pattern in TOPICS if pattern.search(about)), None)
        if topic is None:
            return None
        answer = self._answer("eeo", "race" if topic == "hispanic" else topic)
        found = [choice.value for choice in field.choices if _eeo_fits(topic, clean(choice.label), answer)]
        if len(found) != 1:
            return Line(field, "decide", decision="unknown", note=NOT_A_CHOICE.format("EEO answer"))
        return Line(field, "fill", (found[0],), f"Questionnaire: {SECTIONS['eeo']}")

    def _saved(self, field: Field, words: str) -> Line | None:
        about = f"{clean(field.key)} {words}"
        answer = None if any(pattern.search(about) for pattern in PROMISES) else self.saved.get(words)
        return self._given(field, answer, "your saved answer", NOT_A_CHOICE.format("saved answer")) if answer else None

    def _consent(self, field: Field, _: str) -> Line | None:
        if (found := consent_kind(field, self.company)) is None:
            return None
        kind, name = found
        if kind in ALWAYS_ASK:  # an AI-use promise or an arbitration agreement: you tick it yourself, never the app
            return Line(field, "left", note=name)
        choice = self.consents.get(kind, "ask") if kind else "ask"  # "": no known kind
        if choice == "decline" and not field.required:
            return Line(field, "empty", note=name)
        if choice == "accept" and (value := self._tick(field)):
            return Line(field, "fill", value, "remembered consent", note=name)
        note = f"{name}: you decline this, but the form requires it" if choice == "decline" else name
        return Line(field, "decide", decision="consent", note=note)

    def _tick(self, field: Field) -> str | tuple[str, ...]:
        """A consent given: its box ticked, or its yes choice; nothing for a box to type in."""
        if field.kind == "consent":
            return TICKED
        return self._choose(field, "yes") if field.kind in ("single", "multi") else ""

    def _fill(self, field: Field, value: str, section: str, note: str = "") -> Line:
        """A Questionnaire answer; an empty one leaves the field to the last rule, with what to fill in."""
        if not value:
            return _other(field, FILL.format(SECTIONS[section]))
        source = f"Questionnaire: {SECTIONS[section]}"
        return self._given(field, value, source, NOT_A_CHOICE.format("Questionnaire answer"), note)

    def _given(self, field: Field, answer: str, source: str, unmatched: str, note: str = "") -> Line:
        """An answer filled in as it is, or for a choice field as the one choice it names."""
        if field.kind not in ("single", "multi"):
            return Line(field, "fill", answer, source, note=note)
        if chosen := self._choose(field, answer):
            return Line(field, "fill", chosen, source, note=note)
        return Line(field, "decide", decision="unknown", note=unmatched)

    def _choose(self, field: Field, answer: str) -> tuple[str, ...]:
        """The values of the choices the answer names (for a multi field, one a line); () unless each names exactly
        one choice."""
        chosen = []
        for wanted in filter(None, (clean(text, self.company) for text in answer.splitlines() or [answer])):
            found = [choice.value for choice in field.choices if fits(clean(choice.label, self.company), wanted)]
            if len(found) != 1:
                return ()
            chosen.append(found[0])
        if field.kind == "single" and len(chosen) > 1:
            return ()
        return tuple(dict.fromkeys(chosen))

    def _section(self, name: str) -> dict:
        section = self.answers.get(name)
        return section if isinstance(section, dict) else {}

    def _answer(self, section: str, name: str) -> str:
        value = self._section(section).get(name)
        return value.strip() if isinstance(value, str) else ""

    def _country(self) -> str:
        """Your country's two-letter code, or "" when it isn't known."""
        name = self._answer("contact", "country").lower()
        return "US" if name in US_NAMES else places.COUNTRIES.get(name, "")


def _other(field: Field, note: str = "") -> Line:
    """The last rule: a required field is yours to decide, an optional one is left empty."""
    return Line(field, "decide", decision="unknown", note=note) if field.required else Line(field, "empty", note=note)


def written(answers: dict) -> dict[str, str]:
    """Your work, start date and salary answers as the rules write them on a form ("" for one you left empty)."""
    work, availability = answers.get("work") or {}, answers.get("availability") or {}
    return {
        "Work authorization": _yes_no(work.get("authorized_us")),
        "Sponsorship now": _yes_no(work.get("sponsorship_now")),
        "Sponsorship later": _yes_no(work.get("sponsorship_future")),
        "Earliest start": _day(availability.get("earliest_start")),
        "Salary": _salary(answers.get("salary") or {}, ""),
    }


def consent_kind(field: Field, company: str = "") -> tuple[str, str] | None:
    """A consent's kind (a CONSENTS kind, "" for none known) and its name as the preview says it; None for a field
    that is no consent."""
    words = clean(field.label, company)
    if field.kind != "consent" and field.part != "privacy" and not CONSENT_WORDS.search(words):
        return None
    about = f"{clean(field.key)} {words}"
    return next(((kind, name) for kind, name, pattern in CONSENTS if pattern.search(about)), ("", "consent"))


def fits(label: str, wanted: str) -> bool:
    """A choice (cleaned) names the answer: the same words, or for yes or no, a choice starting with it."""
    return label == wanted or (wanted in ("yes", "no") and label.startswith(f"{wanted} "))


def _eeo_fits(topic: str, label: str, answer: str) -> bool:
    """Whether a choice (cleaned) says the same as your EEO answer, however the form words it."""
    declines = DECLINE.search(label) is not None
    if declines or answer == NO_ANSWER:
        return declines and answer == NO_ANSWER
    if topic == "race":
        wanted = clean(answer)
        return label == wanted or label.startswith(f"{wanted} ")
    if topic == "gender":
        return answer in GENDERS and GENDERS[answer].search(label) is not None
    if topic == "veteran":
        if "not a protected veteran" in label:
            return answer == VETERAN[1]
        protected = "identify as one or more" in label or "protected veteran" in label
        return answer == VETERAN[0] and protected and not re.search(r"\bnot\b", label)
    yes = answer == (RACES[0] if topic == "hispanic" else DISABILITY[0])  # the yes or no of the question
    return label.partition(" ")[0] == ("yes" if yes else "no")


def _yes_no(value: object) -> str:
    return {"yes": "Yes", "no": "No"}.get(value, "") if isinstance(value, str) else ""


def _text(name: str):
    return lambda section, _: section.get(name) if isinstance(section.get(name), str) else ""


def _sponsorship(work: dict, words: str) -> str:
    """Sponsorship now, when the question asks only about now; else yes if you need it now or later."""
    now, future = work.get("sponsorship_now"), work.get("sponsorship_future")
    if NOW.search(words) and not FUTURE.search(words):
        return _yes_no(now)
    return "Yes" if "yes" in (now, future) else "No" if now == future == "no" else ""


def _relocation(availability: dict, _: str) -> str | None:
    relocate = availability.get("relocate")
    return None if relocate == "depends" else _yes_no(relocate)


def _day(value: object) -> str:
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    return f"{day:%B} {day.day}, {day.year}"


def _month(value: object) -> str:
    if not isinstance(value, str) or not MONTH.fullmatch(value):
        return ""
    year, month = map(int, value.split("-"))
    return f"{date(year, month, 1):%B} {year}"


def _salary(salary: dict, _: str) -> str:
    if salary.get("prefer_to_discuss") is True:
        return "Open to discussion"
    low, high = salary.get("minimum"), salary.get("maximum")
    if not isinstance(low, int):
        return ""
    amount = f"{low:,}-{high:,}" if isinstance(high, int) and high != low else f"{low:,}"
    return f"{amount} {salary.get('currency') or 'USD'} per {salary.get('per') or 'year'}"


ANSWERS = {  # a known question -> (its Questionnaire section, its answer from that section and the question's words)
    "work": ("work", lambda work, _: _yes_no(work.get("authorized_us"))), "sponsorship": ("work", _sponsorship),
    "relocation": ("availability", _relocation),
    "start": ("availability", lambda availability, _: _day(availability.get("earliest_start"))),
    "notice": ("availability", _text("notice_period")), "salary": ("salary", _salary),
    "degree": ("education", _text("highest_degree")),
    "graduation": ("education", lambda education, _: _month(education.get("graduation"))),
    "gpa": ("education", _text("gpa")), "over_18": ("other", lambda other, _: _yes_no(other.get("over_18"))),
    "clearance": ("other", _text("clearance")), "heard": ("other", _text("heard_about")),
}  # fmt: skip
