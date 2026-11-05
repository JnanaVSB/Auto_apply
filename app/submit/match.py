"""Each form field matched to an answer for the preview (Phase 7): what you kept for this job, your Questionnaire,
your saved More answers, your remembered consents and the job's newest PDFs. The first rule that fits wins, and
nothing is guessed: a choice is picked only when exactly one fits, else the field is yours to decide.

Before any rule answers a question, `care` sorts it, and everything that fills, saves or writes asks that one step.
An allow-list: the safe mistake is the default, so one more question to you beats a wrong legal answer. A wording that
doesn't fit a plain form below goes to you; never widen a form to make a real wording pass."""

import re
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from pathlib import Path

import sqlcipher3

from app import places, questionnaire
from app.council import runs
from app.documents import pdf
from app.questionnaire import (
    DISABILITY,
    MAX_PAIRS,
    MONTH,
    NO_ANSWER,
    RACES,
    SECTIONS,
    VETERAN,
    QuestionnaireError,
    clean,
)
from app.submit import Choice, Field, Form

STATUSES = ("fill", "empty", "hidden", "write", "decide", "left")  # left: you tick it yourself in the window
DECISIONS = {  # the "Decision required" reasons, as the preview words them
    "unknown": "Unknown question", "non_pdf": "Non-PDF upload", "choose_file": "A file you need to choose",
    "consent": "A promise or consent", "cover_letter": "Cover letter required",
}  # fmt: skip
OLD_MARKER = "[company]"  # in a More answers row from before questions naming a company became this-job-only
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
CONSENT_WORDS = re.compile(
    r"\b(?:agree|agrees|agreed|acknowledg\w*|attest\w*|certify|certifies|consent\w*|opt in|have read|confirm)\b"
    r"|\bauthori[sz]e\b(?! to work)"
)
CONSENTS = (  # kind, as the preview names it, and its wording: the first that fits
    ("privacy", "privacy", r"\bprivacy (?:notice|policy)\b|\bdata processing\b"
     r"|\bprocessing of (?:my|your) (?:personal )?data\b"),
    ("retention", "retention", r"\b(?:retain|retention|gdpr|store)\b"),  # store: Lever's consent[store]
    ("texting", "texting", r"\b(?:text message|sms|whatsapp)"),
    ("marketing", "marketing", r"\b(?:marketing|newsletter|talent community)\b"),
)  # fmt: skip
CONSENTS = tuple((kind, name, re.compile(pattern)) for kind, name, pattern in CONSENTS)
NOT_PLAIN = re.compile(  # a consent that says no, or goes further than its kind: never from a remembered accept
    r"\b(?:not|don t|never|decline|opt out|withdraw|unsubscribe|refuse|reject"
    r"|share|sharing|third part\w*|advertis\w*|affiliate\w*|sell|partner\w*)\b"
)
FREE_TEXT = frozenset({"salary", "start", "notice", "heard"})  # known questions a long-text box may ask

# The four classes of `care`: who may answer a question.
PROMISE = "promise"  # AI-use and arbitration: nothing answers it, you tick it in the window
LEGAL = "legal"  # law and personal facts: a plain form below, or an answer for this job; never saved, never the Writer
THIS_COMPANY = "company"  # names or is about this company: an answer for this job, or the Writer's; never saved
ORDINARY = ""  # the usual rules

ARBITRATION = re.compile(r"\b(?:arbitrat\w*|dispute resolution|class action)\b")
AI_WORD = r"ai|a i|artificial intelligence|genai|gen ai|generative|chatgpt|gpt|copilot|llms?|large language models?"
AI = re.compile(rf"\b(?:{AI_WORD})\b")
SKILL = re.compile(  # a mention of AI is a promise unless it is positively about your skills, with none of PROCESS
    r"\b(?:experience|projects?|skills?|background|familiar\w*|expertise|research|worked (?:on|with)|built"
    r"|interests? you|excites? you|passionate)\b"
)
PROCESS = re.compile(
    r"\b(?:application|applying|interview\w*|assessment|materials|resume|cover letter|answers?|responses?|submission"
    r"|form|prepar\w*|polic\w*|guidelines?|attest\w*|agree\w*|acknowledg\w*|certif\w*|confirm\w*|promise"
    r"|prohibit\w*|use of|using|usage)\b"
)
LEGAL_TOPICS = ("work", "sponsorship", "relocation", "over_18", "clearance")  # KNOWN questions filled only on PLAIN
LEGAL_WORDS = re.compile(
    r"\b(?:convict\w*|criminal|felon\w*|misdemeanou?r|arrest\w*|background check|drug (?:test|screen)\w*|non compete"
    r"|non solicit\w*|visa|citizen\w*|immigration|sponsor\w*|work authori\w*|right to work|gender|race|ethnic\w*"
    r"|disabilit\w*|veteran\w*|military|sexual orientation|date of birth|social security|ssn|passport|salary"
    r"|compensation|agreements?)\b"
)
ABOUT_THIS_COMPANY = re.compile(
    r"\b(?:worked (?:here|for us|with us|at)|previously (?:worked|employed|applied|interviewed)"
    r"|ever (?:worked|applied|interviewed)|interviewed (?:here|with us|at|before)|related to|relatives?"
    r"|family member|know anyone|referred by)\b"
)
US = r"(?:us|u s|usa|united states(?: of america)?)"
PLAIN = {  # the only wordings a legal topic is answered on: the whole cleaned label, US forms only
    "work": rf"(?:are|do) you(?: currently| legally)* (?:authorized|eligible|permitted|have the (?:legal )?right)"
            rf" to work(?: in the {US})?",
    "sponsorship": rf"(?:will|do|would) you(?: now or in the future| now| currently| in the future)* (?:need|require)"
                   rf"(?: now or in the future)?(?: (?:visa|immigration|employment|work))? sponsorship"
                   rf"(?: for employment visa status)?(?: e g h 1b(?: visa)?(?: status)?)?(?: to work in the {US})?",
    "relocation": r"(?:are you|would you be) (?:open|willing|able) to relocat(?:e|ion)"
                  r"(?: for this (?:role|position|job))?",
    "over_18": r"are you(?: at least| over)? 18(?: years(?: of age| old)?)?(?: or older)?",
    "clearance": r"do you(?: currently)? (?:hold|have)(?: an active| a)?(?: security)? clearance"
                 r"|security clearance(?: level)?",
}  # fmt: skip
PLAIN = {topic: re.compile(pattern) for topic, pattern in PLAIN.items()}
LABEL_TAIL = re.compile(r"(?: yes no| required| please select)+$")  # ignored after a plain form
TWIST = re.compile(  # in a plain form's help: its meaning may not be the usual one
    r"\b(?:without|not|unable|cannot|unrestricted|restriction|permanent|any employer|acknowledge|understand|country"
    r"|location|assistance|under|if yes|which)\b"
)
YES_OR_NO = re.compile(r"(?:(?:do|are|can|have|will|would|did) you|is your)\b")  # at the start of a cleaned label
EYE = "the wording needs your eye"
YOURS_TO_WRITE = "this one is yours to write"
CHANGED = "the question changed since you answered it"
LOST = "your saved answer is no longer one of the choices"
TWO_FIT = "your saved answer fits more than one of the choices"

NO_WORD = r"not|no|never|refrain\w*|prohibit\w*|reject\w*|avoid\w*|without|discourag\w*|forbid\w*|unacceptable"
NO_AI = re.compile(  # on cleaned wording: when unsure, warn
    rf"\b(?:{AI_WORD})\b(?: \w+){{0,7}} (?:{NO_WORD})\b|\b(?:{NO_WORD})\b(?: \w+){{0,7}} (?:{AI_WORD})\b"
    r"|\b(?:own work|my own|your own(?! words)|the candidate s own|the applicant s own|written by you)\b"
)
YOUR_WORDS = re.compile(r"\byour own words\b")  # common essay wording, no AI ban: a warning that blocks nothing
NO_AI_WARNING = ("This form asks that application materials be your own and not AI-written: write the long answers "
                 "yourself")  # fmt: skip
DRAFTS_WARNING = "This form has Writer drafts: replace them with your own words"
YOUR_WORDS_WARNING = "This form asks for your own words: think about writing the long answers yourself"
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
    askable: bool = False  # optional and left empty only because no rule answers it (never a promise): you still may


def names(text: str, company: str) -> bool:
    """Whether the text holds the company's name, as whole words."""
    name = clean(company)
    return bool(name) and f" {name} " in f" {clean(text)} "


def wording(field: Field) -> str:
    """What an answer is an answer to: a changed question is asked again."""
    return clean(f"{field.label} {field.help}")


@lru_cache(maxsize=2048)
def care(field: Field, company: str = "") -> str:
    """The question's class: who may answer it. Reads the label, help and key, with the company's name left in, so a
    company called Visa can't hide a word. The one exception is the AI check, in `_promise`."""
    if _promise(field, company):
        return PROMISE
    said = _said(field)
    legal = any(KNOWN[topic].search(said) for topic in LEGAL_TOPICS) or LEGAL_WORDS.search(said)
    if legal or consent_kind(field) is not None:
        return LEGAL
    return THIS_COMPANY if names(said, company) or ABOUT_THIS_COMPANY.search(said) else ORDINARY


@lru_cache(maxsize=2048)
def _promise(field: Field, company: str) -> str:
    """An AI-use promise's or an arbitration agreement's name as the preview says it, else "". The choices count too.
    The company's name is left out of the AI check when that stays honest: "Why Scale AI?" is no promise, but a company
    called just AI or GPT hides nothing."""
    said = " ".join([_said(field), *(clean(choice.label) for choice in field.choices)])
    if ARBITRATION.search(said):
        return "arbitration"
    text = _without_name(said, company)
    return "AI use" if AI.search(text) and (not SKILL.search(text) or PROCESS.search(text)) else ""


def _without_name(said: str, company: str) -> str:
    name = clean(company)
    if not name or (" " not in name and AI.fullmatch(name)):
        return said
    return re.sub(rf"(?<!\S){re.escape(name)}(?!\S)", " ", said)


def _said(field: Field) -> str:
    """The question's words. A survey's country list is ours, not the form's: "AI" there is Anguilla."""
    return clean(f"{field.label} {SURVEY_COUNTRIES.sub('', field.help)} {field.key}")


def match(
    form: Form,
    answers: dict,
    company: str,
    *,
    kept: dict[str, tuple[str, str, str]] | None = None,
    consents: dict[str, str] | None = None,
    files: dict[str, tuple[str, Path] | None] | None = None,
) -> tuple[tuple[Line, ...], tuple[str, ...]]:
    """Every field's line, and the warnings (not blocking). `answers` as questionnaire.load gives them, `kept` this
    job's own answers (key -> answer, 'you' or 'writer', its `wording`), `consents` kind -> 'accept', 'decline' or
    'ask', `files` from documents()."""
    matcher = _Matcher(answers, company, kept or {}, consents or {}, files or {})
    lines = tuple(map(matcher.line, form.fields))
    return lines, _warnings(form, company, lines)


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
    """Keep a form's ordinary question in your More answers, with `answer` if given. One row per wording: a row keeps
    the first company and day that asked it, and an answer it holds is never changed. Only for a question of class
    ORDINARY: the caller asks `care`."""
    values, _, problem = questionnaire.load(conn)
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
    questionnaire.save(conn, questionnaire.check(values))


def _warnings(form: Form, company: str, lines: tuple[Line, ...]) -> tuple[str, ...]:
    said = [_without_name(wording(field), company) for field in form.fields]
    found = []
    if any(YOUR_WORDS.search(text) for text in said):
        found.append(YOUR_WORDS_WARNING)
    if any(NO_AI.search(text) for text in said):
        found.append(NO_AI_WARNING)
        if any(line.source == "Writer draft" for line in lines):
            found.append(DRAFTS_WARNING)
    if form.limit_note:
        found.append(LIMIT_WARNING.format(form.limit_note))
    return tuple(found)


class _Matcher:
    """The rules, in order, for one form's fields."""

    def __init__(self, answers: dict, company: str, kept: dict, consents: dict, files: dict) -> None:
        self.answers, self.company, self.kept, self.consents, self.files = answers, company, kept, consents, files
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
        if kind == PROMISE:  # whatever is kept: you tick it yourself, never the app
            return Line(field, "left", note=_promise(field, self.company))
        answer, given_by, asked = self.kept.get(field.key, ("", "", ""))
        if not answer.strip() or (given_by == "writer" and kind == LEGAL):
            return self._rules(field, kind)
        if asked == wording(field):
            return self._kept(field, answer, given_by)
        found = self._rules(
            field, kind
        )  # your old answer stays until you replace it, but never lands on a new question
        return replace(found, note=f"{CHANGED}; {found.note}" if found.note else CHANGED)

    def _rules(self, field: Field, kind: str) -> Line:
        if field.kind == "unknown":  # a kind of field the app can't fill
            return self._other(field)
        words = clean(field.label)
        for rule in (self._standard, self._upload, self._known, self._eeo, self._saved, self._consent):
            if found := rule(field, words):
                return found
        if field.kind == "long_text" and kind == LEGAL:
            return Line(field, "decide", decision="unknown", note=YOURS_TO_WRITE)
        if field.kind == "long_text" and field.part == "company":
            return Line(field, "write")
        return self._other(field)

    def _kept(self, field: Field, answer: str, given_by: str) -> Line:
        source = "your answer for this job" if given_by == "you" else "Writer draft"
        if answer == IN_WINDOW:
            return Line(field, "left", source=source, note="you do this in the window")
        if answer == LEFT_EMPTY:
            return Line(field, "empty", source=source, note="you chose to leave it empty")
        texts = list(filter(str.strip, answer.splitlines()))
        lost = field.kind in ("single", "multi") and not all(_named(field, text) for text in texts)
        return self._given(field, answer, source, LOST if lost else TWO_FIT, own=True)

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
        if asked[0] in LEGAL_TOPICS and (field.kind == "long_text" or not _plain(asked[0], field)):
            return Line(field, "decide", decision="unknown", note=EYE)
        if field.kind == "long_text" and asked[0] not in FREE_TEXT:  # "Why do you want to relocate?": for the Writer
            return None
        if asked[0] not in LEGAL_TOPICS and _yes_or_no(field, words):  # a fact is no answer to "Is your GPA above 3.5?"
            return Line(field, "decide", decision="unknown", note=EYE)
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
        answer = self.saved.get(words) if care(field, self.company) == ORDINARY else None
        return self._given(field, answer, "your saved answer", NOT_A_CHOICE.format("saved answer")) if answer else None

    def _consent(self, field: Field, _: str) -> Line | None:
        if field.kind in ("text", "long_text") or (found := consent_kind(field)) is None:  # a box to type in: yours
            return None
        kind, name = found
        choice = self.consents.get(kind, "ask") if kind else "ask"  # "": no known kind
        if choice == "decline" and not field.required:
            return Line(field, "empty", note=name)
        if choice == "accept" and (value := self._tick(field)):
            return Line(field, "fill", value, "remembered consent", note=name)
        note = f"{name}: you decline this, but the form requires it" if choice == "decline" else name
        return Line(field, "decide", decision="consent", note=note)

    def _tick(self, field: Field) -> str | tuple[str, ...]:
        """A remembered consent: its box ticked, or the choice that gives it, on a plain sentence only."""
        if field.kind == "consent":
            return TICKED if plain_consent(field) else ""
        choice = consent_choice(field)
        return (choice.value,) if choice else ""

    def _fill(self, field: Field, value: str, section: str, note: str = "") -> Line:
        """A Questionnaire answer; an empty one leaves the field to the last rule, with what to fill in."""
        if not value:
            return self._other(field, FILL.format(SECTIONS[section]))
        source = f"Questionnaire: {SECTIONS[section]}"
        return self._given(field, value, source, NOT_A_CHOICE.format("Questionnaire answer"), note)

    def _given(self, field: Field, answer: str, source: str, unmatched: str, note: str = "", own: bool = False) -> Line:
        """An answer filled in as it is, or for a choice field as the one choice it names; `own`: your pick for this
        job, stored as the choice's label."""
        if field.kind not in ("single", "multi"):
            return Line(field, "fill", answer, source, note=note)
        if chosen := self._choose(field, answer, own):
            return Line(field, "fill", chosen, source, note=note)
        return Line(field, "decide", decision="unknown", note=unmatched)

    def _choose(self, field: Field, answer: str, own: bool = False) -> tuple[str, ...]:
        """The values of the choices the answer names (for a multi field, one a line); () unless each names exactly
        one choice. Your own pick is found by its exact label first."""
        chosen = []
        for text in filter(clean, answer.splitlines() or [answer]):
            wanted = clean(text)
            found = _named(field, text) if own else [c for c in field.choices if fits(clean(c.label), wanted)]
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
        "Work authorization": _yes_no(work.get("authorized_us")),
        "Sponsorship now": _yes_no(work.get("sponsorship_now")),
        "Sponsorship later": _yes_no(work.get("sponsorship_future")),
        "Earliest start": _day(availability.get("earliest_start")),
        "Salary": _salary(answers.get("salary") or {}, ""),
    }


def consent_kind(field: Field) -> tuple[str, str] | None:
    """A consent's kind (a CONSENTS kind, "" for none known) and its name as the preview says it; None for a field
    that is no consent. An AI-use promise or arbitration never comes here: `care` stops it first."""
    said = _said(field)
    if field.kind != "consent" and field.part != "privacy" and not CONSENT_WORDS.search(said):
        return None
    return next(((kind, name) for kind, name, pattern in CONSENTS if pattern.search(said)), ("", "consent"))


def plain_consent(field: Field) -> bool:
    """Whether the sentence simply gives its kind of consent: no negative, and nothing beyond the kind."""
    return not NOT_PLAIN.search(wording(field))


def consent_choice(field: Field) -> Choice | None:
    """The choice that gives a plain consent: its one yes choice, else the only choice there is, unless that says no."""
    labels = [(choice, clean(choice.label)) for choice in field.choices]
    yes = [(choice, label) for choice, label in labels if fits(label, "yes")]
    found = labels if len(labels) == 1 else yes if len(yes) == 1 else []
    if not found or not plain_consent(field) or fits(found[0][1], "no") or NOT_PLAIN.search(found[0][1]):
        return None
    return found[0][0]


def _yes_or_no(field: Field, words: str) -> bool:
    """Whether a question wants a yes or a no: by how it opens, or by its choices."""
    labels = [clean(choice.label) for choice in field.choices]
    return YES_OR_NO.match(words) is not None or all(
        any(fits(label, word) for label in labels) for word in ("yes", "no")
    )


def _plain(topic: str, field: Field) -> bool:
    """A legal topic asked in one of its plain forms, its help saying nothing that turns the meaning."""
    return PLAIN[topic].fullmatch(LABEL_TAIL.sub("", clean(field.label))) is not None and not TWIST.search(
        clean(field.help)
    )


def _named(field: Field, text: str) -> list[Choice]:
    """The choices your pick names: the same label, else the same words, else the yes or no rule."""
    wanted, labels = clean(text), [(choice, clean(choice.label)) for choice in field.choices]
    exact = [choice for choice in field.choices if choice.label == text.strip()]
    return exact or [c for c, label in labels if label == wanted] or [c for c, label in labels if fits(label, wanted)]


def fits(label: str, wanted: str) -> bool:
    """A choice (cleaned) names the answer: the same words, or for yes or no, a choice starting with it."""
    return label == wanted or (wanted in ("yes", "no") and label.startswith(f"{wanted} "))


def _eeo_fits(topic: str, label: str, answer: str) -> bool:
    """Whether a choice (cleaned) says the same as your EEO answer, however the form words it. No answer fits nothing:
    never an invented No."""
    if not answer:
        return False
    declines = DECLINE.search(label) is not None
    if declines or answer == NO_ANSWER:
        return declines and answer == NO_ANSWER
    if topic == "race":
        wanted, label = clean(answer), label.replace("alaskan native", "alaska native")  # Greenhouse's wording
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
