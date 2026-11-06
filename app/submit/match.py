"""Each form field's line for the preview (Phase 7), an allow-list: nothing is filled without your yes for this job
unless it is on a short positive list, and every list here is a full match but ESSAY, which only says how an essay
starts: `_essay` holds the rest of its shape. A wording that doesn't fit goes to you: never widen a list to make a real
wording pass. `care` (app/submit/wordings.py) only says who may save, suggest or write an answer; it decides nothing is
safe to fill, so a word it misses costs one visible suggestion at worst.

The rules, in order, for each field:
1. Hidden: left alone.
2. A promise: left for you, whatever is kept.
3. Your answer for this job, when its wording is today's; a Writer draft is only suggested.
4. A kind of box the app can't fill goes to the last rule.
5. The site's own boxes by key, uploads, plain links, plain legal questions and facts, EEO, remembered consents.
6. Your saved answer to the same wording: only suggested, with your "Use" to keep it.
7. A long question of an essay's shape: the Writer's.
8. The last rule: a required question is yours to decide, an optional one is left empty."""

import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from app import places
from app.questionnaire import DISABILITY, MONTH, NO_ANSWER, OLD_MARKER, RACES, SECTIONS, VETERAN, clean, place, spelled
from app.submit import Choice, Field, Form
from app.submit.wordings import (
    KEYS,
    KNOWN,
    LEGAL,
    ORDINARY,
    PROMISE,
    SURVEY_COUNTRIES,
    THIS_COMPANY,
    bare,
    care,
    consent_choice,
    consent_kind,
    names,
    no_writer,
    promise,
    sentence,
    wording,
)

TICKED = "yes"  # a consent box to tick
IN_WINDOW, LEFT_EMPTY = "[in the window]", "[left empty]"  # your choice for one job's question, kept as its answer
MINE, WRITER = "your answer for this job", "Writer draft"  # where a job's own answer came from
UPLOADS = {"resume": "resume", "resume cv": "resume", "cv": "resume", "cover letter": "cover_letter"}  # by whole label
LINKS = {  # a text box asked for one of your links, by its whole cleaned label
    "linkedin": re.compile(r"linkedin(?: profile| url| profile url)?"),
    "github": re.compile(r"github(?: profile| url| profile url)?"),
    "portfolio": re.compile(r"portfolio(?: url| link| website)?|personal website(?: url)?"),
    "scholar": re.compile(r"google scholar(?: profile| url)?"),
}  # fmt: skip
NOW, FUTURE = re.compile(r"\b(?:current|currently|now)\b"), re.compile(r"\bfuture\b")
US_NAMES = frozenset({"united states", "usa", "us"})
EEO_KEYS = {  # the sites' own fixed EEO keys -> the topic
    "gender": "gender", "race": "race", "veteran_status": "veteran", "disability_status": "disability",
    "hispanic_ethnicity": "hispanic", "eeo[gender]": "gender", "eeo[race]": "race", "eeo[veteran]": "veteran",
    "eeo[disability]": "disability",
}  # fmt: skip
EEO_LABELS = {  # or the whole cleaned label, in a form's EEO or voluntary part
    label: topic
    for topic, labels in {
        "gender": ("gender", "your gender", "what is your gender", "how do you describe your gender",
                   "what best describes your gender"),
        "race": ("race", "what is your race", "what is your race ethnicity", "race ethnicity",
                 "please identify your race"),
        "veteran": ("veteran status", "veteranstatus", "are you a veteran have you served in the military"),
        "disability": ("disability status",),
        "hispanic": ("are you hispanic latino", "are you hispanic or latino"),
    }.items()
    for label in labels
}  # fmt: skip
DECLINES = frozenset({  # a choice that declines, whole and cleaned
    "i don t wish to answer", "i do not wish to answer", "i don t want to answer", "i do not want to answer",
    "decline to self identify", "decline to answer", "decline to state", "prefer not to say", "prefer not to answer",
    "i prefer not to answer", "prefer not to disclose", "choose not to disclose",
})  # fmt: skip
EEO_CHOICES = {  # your answer -> the choices that say it, whole and cleaned
    "gender": {"Male": ("male", "man"), "Female": ("female", "woman"), "Non-binary": ("non binary", "nonbinary")},
    "veteran": {
        VETERAN[0]: ("i am a protected veteran",
                     "i identify as one or more of the classifications of a protected veteran",
                     "i identify as one or more of the classifications of protected veteran"),
        VETERAN[1]: ("i am not a protected veteran",),
    },
    "disability": {
        DISABILITY[0]: ("yes i have a disability or have had one in the past",
                        "yes i have a disability or have a history record of having a disability"),
        DISABILITY[1]: ("no i do not have a disability", "no i don t have a disability",
                        "no i do not have a disability and have not had one in the past",
                        "no i don t have a disability and have not had one in the past"),
    },
}  # fmt: skip
MAX_ESSAY_WORDS = 25
SENTENCE_END = re.compile(r"[.?!;]")
A_LIMIT = (
    r"(?:please )?(?:keep it )?(?:in |up to |at most |maximum(?: of)? |max |no more than |not more than |under "
    r"|fewer than |less than )*\d[\d ]* ?(?:words?|characters?|chars?)(?: or (?:less|fewer)| maximum| max| limit)?"
)
IN_OWN_WORDS = r"(?:please )?(?:explain )?in your own words(?: please)?"
ESSAY_HELP = re.compile(  # an essay's help, whole and cleaned, may say this and nothing else
    rf"{A_LIMIT}|{IN_OWN_WORDS}(?: {A_LIMIT})?|{A_LIMIT} {IN_OWN_WORDS}"
)
ESSAY = re.compile(  # the start of the cleaned label: only these may be the Writer's
    r"why (?:do you want|are you interested|would you like|should we|this (?:company|role|position|job|team)|us\b)"
    r"|tell (?:us|me) about (?:yourself|a (?:project|time)|your (?:experience|background|interest))"
    r"|describe a (?:project|time|situation|challenge)"
    r"|describe your (?:experience|background|approach|interest|motivation)"
    r"|what (?:interests|excites|motivates) you|cover letter"
)
US = r"(?:us|u s|usa|united states(?: of america)?)"
PLAIN = {  # the only wordings a known question is filled on: the whole cleaned label; a legal topic on US forms only
    "work": re.compile(rf"(?:are|do) you(?: currently| legally)* "
                       rf"(?:authorized|eligible|permitted|have the (?:legal )?right) to work(?: in the {US})?"),
    "sponsorship": re.compile(rf"(?:will|do|would) you(?: now or in the future| now| currently| in the future)* "
                              rf"(?:need|require)(?: now or in the future)?"
                              rf"(?: (?:visa|immigration|employment|work))? sponsorship"
                              rf"(?: for employment visa status)?(?: e g h 1b(?: visa)?(?: status)?)?"
                              rf"(?: to work in the {US})?"),
    "relocation": re.compile(r"(?:are you|would you be) (?:open|willing|able) to relocat(?:e|ion)"
                             r"(?: for this (?:role|position|job))?"),
    "over_18": re.compile(r"are you(?: at least| over)? 18(?: years(?: of age| old)?)?(?: or older)?"),
    "clearance": re.compile(r"do you(?: currently)? (?:hold|have)(?: an active| a)?(?: security)? clearance"
                            r"|security clearance(?: level)?"),
    "salary": re.compile(r"(?:what (?:are|is) your )?"
                         r"(?:desired salary|expected salary|salary expectations?|salary requirements?)"),
    "start": re.compile(r"(?:what is your )?(?:earliest |possible |earliest possible )start date|earliest start"
                        r"|when can you start"
                        r"|when are you available to start"),
    "notice": re.compile(r"(?:what is your )?notice period"),
    "degree": re.compile(r"(?:what is your )?highest (?:degree|level of education)(?: completed| earned)?"),
    "graduation": re.compile(r"(?:what is your )?(?:expected )?graduation (?:date|month)"),
    "gpa": re.compile(r"(?:what is your )?(?:cumulative |overall )?(?:gpa|grade point average)"),
    "heard": re.compile(r"(?:how|where) did you (?:hear|find out) about "
                        r"(?:us|company|this (?:job|role|position|opportunity))|referral source"),
}  # fmt: skip
LEGAL_LABEL = re.compile(r"(?:[^\W_]|[\s?.,\-/'()])*")  # a symbol `clean` removes can turn a plain form around: "< 18"
LABEL_TAIL = re.compile(r"(?: yes no| required| please select)+$")  # ignored after a plain form
YOUR_WORDS = re.compile(r"\byour own words\b")  # common essay wording, no AI ban: a warning that blocks nothing

DECISIONS = {  # the "Decision required" reasons, as the preview words them
    "unknown": "Unknown question", "non_pdf": "Non-PDF upload", "choose_file": "A file you need to choose",
    "consent": "A promise or consent", "cover_letter": "Cover letter required",
}  # fmt: skip
FILL = "fill {} in your Questionnaire first"
NOT_A_CHOICE = "your {} is not one of the choices"
EYE = "the wording needs your eye"
YOURS_TO_WRITE = "this one is yours to write"
CHANGED = "the question changed since you answered it"
LOST = "your saved answer is no longer one of the choices"
TWO_FIT = "your saved answer fits more than one of the choices"
DRAFT_UNDER_RULE = "only your own words close this one: edit it under Long questions"
NO_AI_WARNING = "This form has a rule about AI: read it, and write the long answers yourself"
DRAFTS_WARNING = "This form has Writer drafts: replace them with your own words"
YOUR_WORDS_WARNING = "This form asks for your own words: think about writing the long answers yourself"
LIMIT_WARNING = "This company limits applications (its words: {})"


@dataclass(frozen=True)
class Line:
    """One preview line, in the form's order."""

    field: Field
    status: str  # fill, empty, hidden, write, decide, left (yours, in the window), suggested (open until you press Use)
    value: str | tuple[str, ...] = ""  # text, the chosen choices' values, or an upload's path
    source: str = ""  # where the value came from, e.g. "Questionnaire: Work authorization", "Resume v3"
    decision: str = ""  # a DECISIONS key when status is "decide"
    note: str = ""  # e.g. "split from your full name"
    askable: bool = False  # optional and left empty only because no rule answers it: you still may


def match(
    form: Form,
    answers: dict,
    company: str,
    *,
    kept: dict[str, tuple[str, str, str]] | None = None,
    consents: dict[str, str] | None = None,
    files: dict[str, tuple[str, Path] | None] | None = None,
    us_job: bool = False,
) -> tuple[tuple[Line, ...], tuple[str, ...]]:
    """Every field's line, and the warnings (not blocking). `answers` as questionnaire.load gives them, `kept` this
    job's own answers (key -> answer, 'you' or 'writer', its `wording`), `consents` kind -> 'accept', 'decline' or
    'ask', `files` from record.documents(), `us_job` whether the job is marked as in the US ('unclear' is not)."""
    matcher = _Matcher(answers, company, kept or {}, consents or {}, files or {}, us_job, no_writer(form, company))
    lines = tuple(map(matcher.line, form.fields))
    return lines, _warnings(form, company, lines)


def _warnings(form: Form, company: str, lines: tuple[Line, ...]) -> tuple[str, ...]:
    found = []
    if any(YOUR_WORDS.search(wording(field)) for field in form.fields):
        found.append(YOUR_WORDS_WARNING)
    if no_writer(form, company):
        found.append(NO_AI_WARNING)
        if any(line.source == WRITER for line in lines):
            found.append(DRAFTS_WARNING)
    if form.limit_note:
        found.append(LIMIT_WARNING.format(form.limit_note))
    return tuple(found)


class _Matcher:
    """The rules, in order, for one form's fields."""

    def __init__(
        self, answers: dict, company: str, kept: dict, consents: dict, files: dict, us_job: bool, no_writer: bool
    ) -> None:
        self.answers, self.company, self.kept, self.consents, self.files = answers, company, kept, consents, files
        self.us_job, self.no_writer = us_job, no_writer
        self.saved: dict[str, str] = {}  # your More answers, by their cleaned question
        for pair in answers.get("more") or []:
            if not isinstance(pair, dict) or not isinstance(pair.get("answer"), str) or not pair["answer"].strip():
                continue
            question, asked_by = str(pair.get("question", "")), str(pair.get("asked_by") or "")
            if OLD_MARKER not in question and not names(pair["answer"], asked_by):
                self.saved.setdefault(clean(question), pair["answer"])

    def line(self, field: Field) -> Line:
        if field.kind == "hidden":
            return Line(field, "hidden")
        kind = care(field, self.company)
        if kind == PROMISE:  # whatever is kept: you do it yourself, never the app
            return Line(field, "left", note=promise(field, self.company, help_too=field.kind != "long_text"))
        answer, given_by, asked = self.kept.get(field.key, ("", "", ""))
        if not answer.strip() or (given_by == "writer" and kind == LEGAL):
            return self._rules(field, kind)
        if asked == wording(field):
            return self._kept(field, answer, given_by)
        found = self._rules(field, kind)  # your old answer stays until replaced, but never lands on a new question
        return replace(found, note=f"{CHANGED}; {found.note}" if found.note else CHANGED)

    def _rules(self, field: Field, kind: str) -> Line:
        if field.kind == "unknown":  # a kind of field the app can't fill
            return self._other(field)
        words = clean(field.label)
        for rule in (self._site_box, self._upload, self._link, self._known, self._eeo, self._consent, self._suggest):
            if found := rule(field, words):
                return found
        if field.kind != "long_text":
            return self._other(field)
        if self._essay(field, words, kind):
            return Line(field, "write")
        return self._other(field, YOURS_TO_WRITE)

    def _essay(self, field: Field, words: str, kind: str) -> bool:
        """Only a long company question of the shape of an essay, on a form with no AI rule, is the Writer's: one short
        sentence, read in the raw label, as `clean` removes its stops, with no help but a limit or "in your own words".
        A class of legal means LEGAL_WORDS holds none."""
        name = bare(self.company)
        shaped = ESSAY.match(words) is not None or bool(name) and f"{words} ".startswith(f"why {name} ")
        one_sentence = not SENTENCE_END.search(field.label.strip().rstrip(".?!;"))
        short = len(field.label.split()) <= MAX_ESSAY_WORDS
        plain_help = not field.help.strip() or ESSAY_HELP.fullmatch(clean(field.help)) is not None
        return (shaped and one_sentence and short and plain_help and field.part == "company"
                and kind in (ORDINARY, THIS_COMPANY) and not self.no_writer)  # fmt: skip

    def _kept(self, field: Field, answer: str, given_by: str) -> Line:
        if given_by == "writer":  # never a fill by itself: you keep it, or edit it
            return Line(field, "suggested", answer, WRITER, note=DRAFT_UNDER_RULE if self.no_writer else "")
        if answer == IN_WINDOW:
            return Line(field, "left", source=MINE, note="you do this in the window")
        if answer == LEFT_EMPTY:
            return Line(field, "empty", source=MINE, note="you chose to leave it empty")
        texts = list(filter(str.strip, answer.splitlines()))
        lost = field.kind in ("single", "multi") and not all(_named(field, text) for text in texts)
        return self._given(field, answer, MINE, LOST if lost else TWO_FIT, own=True)

    def _site_box(self, field: Field, _: str) -> Line | None:
        if field.key in ("first_name", "last_name"):
            return self._name_part(field)
        if field.key == "comments":  # Lever's "Additional information": yours to write, or leave
            return Line(field, "empty")
        if field.key in ("location", "city") and field.kind in ("text", "location") and not field.choices:
            return self._fill(field, place(self._section("contact")), "contact")
        if field.key in KEYS:
            return self._fill(field, self._answer(*KEYS[field.key]), KEYS[field.key][0])
        return None

    def _name_part(self, field: Field) -> Line:
        """First name: your full name up to its last space; last name: the rest."""
        full = self._answer("contact", "full_name")
        if not full:
            return self._other(field, FILL.format(SECTIONS["contact"]))
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
        by_label = None if field.help.strip() else UPLOADS.get(words)
        slot = field.key if field.key in ("resume", "cover_letter") else by_label
        if slot is None:
            return Line(field, "decide", decision="choose_file") if field.required else Line(field, "empty")
        if found := self.files.get(slot):
            return Line(field, "fill", str(found[1]), found[0])
        if not field.required:
            return Line(field, "empty")
        if slot == "cover_letter":
            return Line(field, "decide", decision="cover_letter")
        return Line(field, "decide", decision="choose_file", note="press Prepare first")

    def _link(self, field: Field, words: str) -> Line | None:
        link = next((name for name, pattern in LINKS.items() if pattern.fullmatch(words)), None)
        if not link or field.kind != "text" or field.help.strip():
            return None
        return self._fill(field, self._answer("contact", link), "contact")

    def _known(self, field: Field, words: str) -> Line | None:
        if field.kind not in ("text", "long_text", "single", "multi"):
            return None
        legal = [name for name, pattern in KNOWN.items() if pattern.search(words)]
        if len(legal) > 1:  # two legal questions in one: never a guess
            return Line(field, "decide", decision="unknown", note="this asks more than one thing")
        asked = LABEL_TAIL.sub("", words)
        asked = (asked, sentence(asked, self.company))
        topic = next((name for name, pattern in PLAIN.items() if any(map(pattern.fullmatch, asked))), None)
        if (legal or topic in KNOWN) and not (topic and self._plain_legal(field)):
            return Line(field, "decide", decision="unknown", note=EYE)
        if topic is None or field.help.strip():
            return None
        section, answer = ANSWERS[topic]
        value = answer(self._section(section), words)
        if value is None:
            return Line(field, "decide", decision="unknown", note="your Questionnaire says it depends")
        return self._fill(field, value, section)

    def _plain_legal(self, field: Field) -> bool:
        """A legal topic's other conditions: no help at all, no symbol in the label, a free box or exactly Yes and No,
        and a job in the US."""
        labels = sorted(clean(choice.label) for choice in field.choices)
        box = field.kind == "text" or (field.kind == "single" and labels == ["no", "yes"])
        return box and not field.help.strip() and self.us_job and LEGAL_LABEL.fullmatch(field.label) is not None

    def _eeo(self, field: Field, words: str) -> Line | None:
        if field.part not in ("eeo", "demographic") or field.kind not in ("single", "multi"):
            return None
        if (survey := SURVEY_COUNTRIES.search(field.help)) and self._country() not in survey[1].split(", "):
            return Line(field, "empty", note=f"only for candidates in: {survey[1]}")
        topic = EEO_KEYS.get(field.key) or ("" if field.help.strip() else EEO_LABELS.get(words))
        if not topic:
            return None
        answer = self._answer("eeo", "race" if topic == "hispanic" else topic)
        found = [choice.value for choice in field.choices if _eeo_fits(topic, clean(choice.label), answer)]
        if len(found) != 1:
            return Line(field, "decide", decision="unknown", note=NOT_A_CHOICE.format("EEO answer"))
        return Line(field, "fill", (found[0],), f"Questionnaire: {SECTIONS['eeo']}")

    def _consent(self, field: Field, _: str) -> Line | None:
        if field.kind in ("text", "long_text") or (kind := consent_kind(field, self.company)) is None:  # a box to type
            return None
        if field.kind != "consent" and consent_choice(field) is None:  # nothing here gives it: an answer, not Accept
            return None
        name, choice = kind or "consent", self.consents.get(kind, "ask") if kind else "ask"
        if choice == "decline" and not field.required:
            return Line(field, "empty", note=name)
        if choice == "accept":
            return Line(field, "fill", self._tick(field), "remembered consent", note=name)
        note = f"{name}: you decline this, but the form requires it" if choice == "decline" else name
        return Line(field, "decide", decision="consent", note=note)

    def _tick(self, field: Field) -> str | tuple[str, ...]:
        """A remembered consent: its box ticked, or the choice that gives it."""
        return TICKED if field.kind == "consent" else (consent_choice(field).value,)

    def _suggest(self, field: Field, words: str) -> Line | None:
        """Your saved answer to the same wording: shown, and yours to Use. Never for a promise, a legal question or one
        about this company."""
        answer = self.saved.get(words)
        if not answer or care(field, self.company) != ORDINARY:
            return None
        value = self._choose(field, answer) if field.kind in ("single", "multi") else answer
        return Line(field, "suggested", value, "your saved answer") if value else None

    def _fill(self, field: Field, value: str, section: str) -> Line:
        """A Questionnaire answer; an empty one leaves the field to the last rule, with what to fill in."""
        if not value:
            return self._other(field, FILL.format(SECTIONS[section]))
        source = f"Questionnaire: {SECTIONS[section]}"
        return self._given(field, value, source, NOT_A_CHOICE.format("Questionnaire answer"))

    def _given(self, field: Field, answer: str, source: str, unmatched: str, own: bool = False) -> Line:
        """An answer filled in as it is, or for a choice field as the one choice it names; `own`: your pick for this
        job, stored as the choice's label."""
        if field.kind not in ("single", "multi"):
            return Line(field, "fill", answer, source)
        if chosen := self._choose(field, answer, own):
            return Line(field, "fill", chosen, source)
        return Line(field, "decide", decision="unknown", note=unmatched)

    def _choose(self, field: Field, answer: str, own: bool = False) -> tuple[str, ...]:
        """The values of the choices the answer names (for a multi field, one a line); () unless each names exactly
        one choice. Your own pick is found by its exact label first."""
        chosen = []
        for text in filter(str.strip, answer.splitlines() or [answer]):
            found = _named(field, text) if own else [c for c in field.choices if spelled(c.label) == spelled(text)]
            found = [choice.value for choice in found]
            if len(found) != 1:
                return ()
            chosen.append(found[0])
        if field.kind == "single" and len(chosen) > 1:
            return ()
        return tuple(dict.fromkeys(chosen))

    def _other(self, field: Field, note: str = "") -> Line:
        """The last rule: a required field is yours to decide, an optional one is left empty."""
        if field.required:
            return Line(field, "decide", decision="unknown", note=note)
        return Line(field, "empty", note=note, askable=True)

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


def written(answers: dict) -> dict[str, str]:
    """Your work, start date and salary answers as the rules write them on a form ("" for one you left empty)."""
    work, availability = answers.get("work") or {}, answers.get("availability") or {}
    return {
        "Work authorization (US)": _yes_no(work.get("authorized_us")),
        "Sponsorship now": _yes_no(work.get("sponsorship_now")),
        "Sponsorship later": _yes_no(work.get("sponsorship_future")),
        "Earliest start": _day(availability.get("earliest_start")),
        "Salary": _salary(answers.get("salary") or {}, ""),
    }


def _named(field: Field, text: str) -> list[Choice]:
    """The choices your pick names: the same label, else the same label in another case or spacing."""
    exact = [choice for choice in field.choices if choice.label == text.strip()]
    return exact or [choice for choice in field.choices if spelled(choice.label) == spelled(text)]


def _eeo_fits(topic: str, label: str, answer: str) -> bool:
    """Whether a choice (cleaned) is, whole, one that says your EEO answer. No answer fits nothing: never an invented
    No."""
    if not answer:
        return False
    if answer == NO_ANSWER:
        return label in DECLINES
    if topic == "race":
        wanted, label = clean(answer), label.replace("alaskan native", "alaska native")  # Greenhouse's wording
        return label in (wanted, f"{wanted} not hispanic or latino")
    if topic == "hispanic":
        return label == ("yes" if answer == RACES[0] else "no")
    return label in EEO_CHOICES[topic].get(answer, ())


def _yes_no(value: object) -> str:
    return {"yes": "Yes", "no": "No"}.get(value, "") if isinstance(value, str) else ""


def _text(name: str):
    return lambda section, _: section.get(name) if isinstance(section.get(name), str) else ""


def _sponsorship(work: dict, words: str) -> str:
    """Your answer for now or for later, when the question asks only about that; else yes if you need it either way."""
    now, future = work.get("sponsorship_now"), work.get("sponsorship_future")
    if NOW.search(words) and not FUTURE.search(words):
        return _yes_no(now)
    if FUTURE.search(words) and not NOW.search(words):
        return _yes_no(future)
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
