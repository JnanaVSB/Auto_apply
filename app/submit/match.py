"""Each form field matched to an answer for the preview (Phase 7), an allow-list: nothing is filled without your yes for
this job unless it is on a short positive list, and every list here is a full match but ESSAY, which only says how an
essay starts: `_essay` holds the rest of its shape. Your contact details and PDFs by
the site's own keys, your answer for this job, then plain links, legal questions, facts, EEO and remembered consents. A
saved answer to the same wording is only suggested, with your "Use" to keep it; anything else is yours. A wording that
doesn't fit goes to you: never widen a list to make a real wording pass. `care` only says who may save, suggest or write
an answer; it decides nothing is safe to fill, so a word it misses costs one visible suggestion at worst."""

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

# left: you do it yourself in the window; suggested: shown, open until you press Use
STATUSES = ("fill", "empty", "hidden", "write", "decide", "left", "suggested")
DECISIONS = {  # the "Decision required" reasons, as the preview words them
    "unknown": "Unknown question", "non_pdf": "Non-PDF upload", "choose_file": "A file you need to choose",
    "consent": "A promise or consent", "cover_letter": "Cover letter required",
}  # fmt: skip
COMPANY_FORM = re.compile(  # not part of a name, when compared
    r"(?: (?:inc|corp|corporation|llc|ltd|labs|co|technologies|systems|group|holdings|limited|gmbh|plc|ai|health"
    r"|software))+$"
)
COMMON_WORDS = frozenset({  # a company's first word that names nothing by itself
    "american", "best", "blue", "data", "digital", "east", "first", "general", "global", "great", "green",
    "international", "national", "next", "north", "open", "south", "true", "united", "west", "world",
})  # fmt: skip
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
SITE_BOXES = frozenset({*KEYS, "first_name", "last_name", "location", "city", "comments", "resume", "cover_letter"})
UPLOADS = {"resume": "resume", "resume cv": "resume", "cv": "resume", "cover letter": "cover_letter"}  # by whole label
LINKS = {  # a text box asked for one of your links, by its whole cleaned label
    "linkedin": r"linkedin(?: profile| url| profile url)?", "github": r"github(?: profile| url| profile url)?",
    "portfolio": r"portfolio(?: url| link| website)?|(?:personal )?website(?: url)?",
    "scholar": r"google scholar(?: profile| url)?",
}  # fmt: skip
LINKS = {name: re.compile(pattern) for name, pattern in LINKS.items()}
KNOWN = {  # a legal topic, by its cleaned wording: filled only on PLAIN's whole label, with `_plain_legal`
    "work": r"(?=.*\b(?:authori[sz]\w*|eligib\w*|legally|lawfully|permitted|able to|right to work|can you)\b)"
            r"(?=.*\b(?:work|employment)\b)",
    "sponsorship": r"\bsponsor", "relocation": r"\brelocat", "clearance": r"\bclearance\b",
    "over_18": r"\b(?:18 years|over 18|at least 18|are you 18)\b",
}  # fmt: skip
KNOWN = {name: re.compile(pattern) for name, pattern in KNOWN.items()}
NOW, FUTURE = re.compile(r"\b(?:current|currently|now)\b"), re.compile(r"\bfuture\b")
US_NAMES = frozenset({"united states", "usa", "us"})
SURVEY_COUNTRIES = re.compile(r"\(for candidates in: ([^)]*)\)$")  # how app/submit/lever.py words a survey's help
EEO_KEYS = {  # the sites' own fixed EEO keys -> the topic
    "gender": "gender", "race": "race", "veteran_status": "veteran", "disability_status": "disability",
    "hispanic_ethnicity": "hispanic", "eeo[gender]": "gender", "eeo[race]": "race", "eeo[veteran]": "veteran",
    "eeo[disability]": "disability",
}  # fmt: skip
EEO_LABELS = {  # or the whole cleaned label, in a form's EEO or voluntary part
    "gender": "gender", "your gender": "gender", "what is your gender": "gender", "how do you describe your gender":
    "gender", "what best describes your gender": "gender", "race": "race", "what is your race": "race",
    "what is your race ethnicity": "race", "race ethnicity": "race", "please identify your race": "race",
    "veteran status": "veteran", "veteranstatus": "veteran", "are you a veteran have you served in the military":
    "veteran", "disability status": "disability", "are you hispanic latino": "hispanic",
    "are you hispanic or latino": "hispanic",
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
CONSENT_WORDS = re.compile(  # also in a choice: "Applicant Terms" with "I agree"
    r"\b(?:agree|agrees|agreed|acknowledg\w*|attest\w*|certify|certifies|consent\w*|opt in|have read|confirm|accept"
    r"|declare|swear|understand)\b|\bauthori[sz]e\b(?! to work)"
)
PLAIN_CONSENTS = {  # the only sentences a remembered consent answers, whole, with a site's name as "company"
    "privacy": r"consent to the privacy notice gdpr"
               r"|i(?: have read and)? (?:agree to|accept|acknowledge|consent to) the privacy (?:policy|notice)"
               r"|please review and acknowledge(?: company s)?(?: candidate)? privacy (?:policy|notice)"
               r"(?: company com candidate privacy notice)?",
    "processing": r"consent to data processing gdpr",
    "retention": r"consent to keeping your data gdpr",
    "texting": r"do you opt in to(?: receive)? whatsapp messages(?: from company(?: recruiting)?)?",
    "marketing": r"company (?:has my consent to|may) contact me about future job opportunities",
}  # fmt: skip
PLAIN_CONSENTS = {kind: re.compile(pattern) for kind, pattern in PLAIN_CONSENTS.items()}
GIVES = frozenset({"yes", "i agree", "agree", "i accept", "accept", "i acknowledge", "acknowledge",
                   "acknowledge confirm", "i consent", "i confirm", "confirm"})  # fmt: skip
REFUSES = re.compile(r"(?:no|not|decline\w*|do not|don t|i do not|i don t|disagree|reject\w*|refuse\w*)\b")

# The four classes of `care`: who may answer a question.
PROMISE = "promise"  # AI-use and arbitration: nothing answers it, you do it in the window
LEGAL = "legal"  # law and personal facts: never saved, suggested or the Writer's
THIS_COMPANY = "company"  # names or is about this company: never saved or suggested; an essay may be the Writer's
ORDINARY = ""  # may be saved and suggested

ARBITRATION = re.compile(
    r"\b(?:arbitrat\w*|dispute resolution|class action|collective action|jury|waive\w*|right to sue|mediation|jams|aaa"
    r"|resolve\w* (?:\w+ )?disputes?|outside (?:of )?court|neutral third party|representative proceeding)\b"
)
AI_WORD = (
    r"ai|a i|artificial intelligence|genai|gen ai|generative|chatgpt|gpt|copilot|llms?|(?:large )?language models?"
    r"|chatbots?|bots?|claude|gemini|openai|grammarly|writing assistant|machine generated|automated (?:\w+ )?tools?"
    r"|authorship|original work|human written"
)
AI = re.compile(rf"\b(?:{AI_WORD})\b")
SKILL = re.compile(  # a mention of AI is a promise unless it is positively about your skills, with none of PROCESS
    r"\b(?:experience|projects?|skills?|background|familiar\w*|expertise|research|worked (?:on|with)|built"
    r"|interests? you|excites? you|passionate)\b"
)
PROCESS = re.compile(
    r"\b(?:application|applying|interview\w*|assessment|materials|resume|cover letter|answers?|responses?|submission"
    r"|form|prepar\w*|polic\w*|guidelines?|attest\w*|agree\w*|acknowledg\w*|certif\w*|confirm\w*|promise"
    r"|prohibit\w*|use of|using|usage|involved|writ\w+)\b"
)
LEGAL_WORDS = re.compile(
    r"\b(?:convict\w*|criminal|felon\w*|misdemeanou?r|arrest\w*|background check|drug (?:test|screen)\w*|non compete"
    r"|non solicit\w*|visa|citizen\w*|immigration|sponsor\w*|work authori\w*|right to work|gender|race|ethnic\w*"
    r"|disabilit\w*|veteran\w*|military|sexual orientation|date of birth|social security|ssn|passport|salary"
    r"|compensation|agreements?|eligib\w*|employment authori\w*|able to work|work permit|green card|permanent resident"
    r"|h 1b|opt|cpt|ead|export control|itar|u s person|terminated|fired|charges|guilty|signature|terms|pay|hourly rate"
    r"|base pay|religion|pregnan\w*|marital status|nationality|medical|accommodations?|lgbtq\w*|transgender"
    r"|year were you born|how old are you|sued|restrictive covenants?|conflict of interest|government official"
    r"|credit check|fingerprint\w*|settled status|leave to remain|work rights)\b"
)
ABOUT_THIS_COMPANY = re.compile(
    r"\b(?:worked (?:here|for us|with us|at)|previously (?:worked|employed|applied|interviewed)"
    r"|ever (?:worked|applied|interviewed)|interviewed (?:here|with us|at|before)|related to|relatives?"
    r"|family member|know anyone|know someone|referred|here|this company|this role|the company|us|our|ours"
    r"|current (?:or former )?employee|former employee)\b"
)
MAX_ESSAY_WORDS = 25
SENTENCE_END = re.compile(r"[.?!;]")
A_LIMIT = (
    r"(?:please )?(?:keep it )?(?:in |up to |at most |maximum(?: of)? |max |no more than |not more than |under "
    r"|fewer than |less than )*\d[\d ]* ?(?:words?|characters?|chars?)(?: or (?:less|fewer)| maximum| max| limit)?"
)
OWN_WORDS = r"(?:please )?(?:explain )?in your own words(?: please)?"
ESSAY_HELP = re.compile(  # an essay's help, whole and cleaned, may say this and nothing else
    rf"{A_LIMIT}|{OWN_WORDS}(?: {A_LIMIT})?|{A_LIMIT} {OWN_WORDS}"
)
ESSAY = re.compile(  # the start of the cleaned label: only these may be the Writer's
    r"why (?:do you want|are you interested|would you like|should we|this (?:company|role|position|job|team)|us\b)"
    r"|tell (?:us|me) about (?:yourself|a (?:project|time)|your (?:experience|background|interest))"
    r"|describe a (?:project|time|situation|challenge)"
    r"|describe your (?:experience|background|approach|interest|motivation)"
    r"|what (?:interests|excites|motivates) you|cover letter"
)
US = r"(?:us|u s|usa|united states(?: of america)?)"
PLAIN = {  # the only wordings a known question is filled on: the whole cleaned label, US forms only
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
    "salary": r"(?:what (?:are|is) your )?(?:desired salary|expected salary|salary expectations?|salary requirements?)",
    "start": r"(?:what is your )?(?:earliest )?(?:possible )?start date|earliest start|when can you start"
             r"|when are you available to start",
    "notice": r"(?:what is your )?notice period",
    "degree": r"(?:what is your )?highest (?:degree|level of education)(?: completed| earned)?",
    "graduation": r"(?:what is your )?(?:expected )?graduation (?:date|month)",
    "gpa": r"(?:what is your )?(?:cumulative |overall )?(?:gpa|grade point average)",
    "heard": r"(?:how|where) did you (?:hear|find out) about (?:us|company|this (?:job|role|position|opportunity))"
             r"|referral source",
}  # fmt: skip
PLAIN = {topic: re.compile(pattern) for topic, pattern in PLAIN.items()}
LEGAL_LABEL = re.compile(r"(?:[^\W_]|[\s?.,\-/'()])*")  # a symbol `clean` removes can turn a plain form around: "< 18"
LABEL_TAIL = re.compile(r"(?: yes no| required| please select)+$")  # ignored after a plain form
EYE = "the wording needs your eye"
YOURS_TO_WRITE = "this one is yours to write"
CHANGED = "the question changed since you answered it"
LOST = "your saved answer is no longer one of the choices"
TWO_FIT = "your saved answer fits more than one of the choices"

NO_WORD = (
    r"not|no|never|refrain\w*|prohibit\w*|reject\w*|avoid\w*|without|discourag\w*|forbid\w*|unacceptable|disqualif\w*"
    r"|banned|disallow\w*|isn t|won t|aren t"
)
NO_AI = re.compile(  # on cleaned wording: when unsure, warn
    rf"\b(?:{AI_WORD})\b(?: \w+){{0,7}} (?:{NO_WORD})\b|\b(?:{NO_WORD})\b(?: \w+){{0,7}} (?:{AI_WORD})\b"
    r"|\b(?:own work|my own|your own(?! words)|the candidate s own|the applicant s own|written by you"
    r"|write this yourself|on (?:your|their) own|answer personally)\b"
)
YOUR_WORDS = re.compile(r"\byour own words\b")  # common essay wording, no AI ban: a warning that blocks nothing
NO_AI_WARNING = "This form has a rule about AI: read it, and write the long answers yourself"
DRAFT_UNDER_RULE = "only your own words close this one: edit it under Long questions"
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
    askable: bool = False  # optional and left empty only because no rule answers it: you still may


def names(text: str, company: str) -> bool:
    """Whether the text holds the company's name, as whole words, with or without its Inc, Corp, LLC, Ltd or Labs."""
    name, said = _bare(company), f" {clean(text)} "
    first = name.partition(" ")[0]
    return bool(name) and (
        f" {name} " in said or (len(first) >= 4 and first not in COMMON_WORDS and f" {first} " in said)
    )


def _bare(company: str) -> str:
    return COMPANY_FORM.sub("", clean(company))


def wording(field: Field) -> str:
    """What an answer is an answer to: a changed question, choices or kind of box, or a box now required, is asked
    again."""
    needed = "required" if field.required else "optional"
    return clean(" ".join([field.kind, needed, field.label, field.help, *(choice.label for choice in field.choices)]))


@lru_cache(maxsize=2048)
def care(field: Field, company: str = "") -> str:
    """The question's class: who may save, suggest or write its answer. Reads the label, help, key and choices, with
    the company's name left in, so a company called Visa can't hide a word. Your own boxes are never in question."""
    if field.key in SITE_BOXES:
        return ORDINARY
    if _promise(field, company, help_too=field.kind != "long_text"):
        return PROMISE
    said = _said(field)
    legal = any(pattern.search(said) for pattern in KNOWN.values()) or LEGAL_WORDS.search(_with_choices(field))
    if legal or consent_kind(field, company) is not None or _promise(field, company):  # an essay under an AI rule
        return LEGAL
    return THIS_COMPANY if names(said, company) or ABOUT_THIS_COMPANY.search(said) else ORDINARY


@lru_cache(maxsize=2048)
def _promise(field: Field, company: str, help_too: bool = True) -> str:
    """An AI-use promise's or an arbitration agreement's name as the preview says it, else "". A mention of AI is one
    unless it is positively a skill question."""
    said = _promise_words(field, company, help_too)
    if ARBITRATION.search(said):
        return "arbitration"
    return "AI use" if _ai_use(said) else ""


def _promise_words(field: Field, company: str, help_too: bool = True) -> str:
    help_ = SURVEY_COUNTRIES.sub("", field.help) if help_too else ""
    return clean(" ".join([_label(field, company), help_, field.key, *(choice.label for choice in field.choices)]))


def _ai_use(said: str) -> bool:
    return AI.search(said) is not None and (not SKILL.search(said) or PROCESS.search(said) is not None)


def _label(field: Field, company: str) -> str:
    """The label, for the promise words: "Why Scale AI?" says nothing but the name. A company called just AI hides
    nothing, and no other wording hides the name."""
    label, name = COMPANY_FORM.sub("", clean(field.label)), _bare(company)
    is_ai = AI.fullmatch(name) or AI.fullmatch(clean(company))
    only_the_name = label in (f"why {name}", f"why do you want to work at {name}") and not is_ai
    return "" if only_the_name else clean(field.label)


def _said(field: Field) -> str:
    """The question's words. A survey's country list is ours, not the form's: "AI" there is Anguilla."""
    return clean(f"{field.label} {SURVEY_COUNTRIES.sub('', field.help)} {field.key}")


def _with_choices(field: Field) -> str:
    return " ".join([_said(field), *(clean(choice.label) for choice in field.choices)])


def _sentence(words: str, company: str) -> str:
    """The cleaned label with the company's name as the word "company", for the lists that hold a site's name."""
    name = _bare(company)
    return re.sub(rf"(?<!\S){re.escape(name)}(?!\S)", "company", words) if name else words


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
    'ask', `files` from documents(), `us_job` whether the job is marked as in the US ('unclear' is not)."""
    matcher = _Matcher(answers, company, kept or {}, consents or {}, files or {}, us_job, _no_writer(form, company))
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


def _no_writer(form: Form, company: str) -> bool:
    """Whether the form holds an AI-use promise or says no to AI: then it has no Write answers."""
    return any(_ai_use(_promise_words(field, company)) or _says_no_ai(field, company) for field in form.fields)


def _says_no_ai(field: Field, company: str) -> bool:
    choices = (choice.label for choice in field.choices)
    help_ = SURVEY_COUNTRIES.sub("", field.help)
    return NO_AI.search(clean(" ".join([_label(field, company), help_, *choices]))) is not None


def _warnings(form: Form, company: str, lines: tuple[Line, ...]) -> tuple[str, ...]:
    found = []
    if any(YOUR_WORDS.search(wording(field)) for field in form.fields):
        found.append(YOUR_WORDS_WARNING)
    if _no_writer(form, company):
        found.append(NO_AI_WARNING)
        if any(line.source == "Writer draft" for line in lines):
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
            return Line(field, "left", note=_promise(field, self.company, help_too=field.kind != "long_text"))
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
        name = _bare(self.company)
        shaped = ESSAY.match(words) is not None or bool(name) and f"{words} ".startswith(f"why {name} ")
        one_sentence = not SENTENCE_END.search(field.label.strip().rstrip(".?!;"))
        short = len(field.label.split()) <= MAX_ESSAY_WORDS
        bare = not field.help.strip() or ESSAY_HELP.fullmatch(clean(field.help)) is not None
        return (shaped and one_sentence and short and bare and field.part == "company"
                and kind in (ORDINARY, THIS_COMPANY) and not self.no_writer)  # fmt: skip

    def _kept(self, field: Field, answer: str, given_by: str) -> Line:
        if given_by == "writer":  # never a fill by itself: you keep it, or edit it
            return Line(field, "suggested", answer, "Writer draft", note=DRAFT_UNDER_RULE if self.no_writer else "")
        source = "your answer for this job"
        if answer == IN_WINDOW:
            return Line(field, "left", source=source, note="you do this in the window")
        if answer == LEFT_EMPTY:
            return Line(field, "empty", source=source, note="you chose to leave it empty")
        texts = list(filter(str.strip, answer.splitlines()))
        lost = field.kind in ("single", "multi") and not all(_named(field, text) for text in texts)
        return self._given(field, answer, source, LOST if lost else TWO_FIT, own=True)

    def _site_box(self, field: Field, _: str) -> Line | None:
        if field.key in ("first_name", "last_name"):
            return self._name_part(field)
        if field.key == "comments":  # Lever's "Additional information": yours to write, or leave
            return Line(field, "empty")
        if field.key in ("location", "city") and field.kind in ("text", "location") and not field.choices:
            place = (self._answer("contact", name) for name in ("city", "state", "country"))
            return self._fill(field, ", ".join(filter(None, place)), "contact")
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
        slot = field.key if field.key in ("resume", "cover_letter") else UPLOADS.get(words)
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
        asked = (asked, _sentence(asked, self.company))
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
            found = _named(field, text) if own else [c for c in field.choices if clean(c.label) == clean(text)]
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


def consent_kind(field: Field, company: str = "") -> str | None:
    """The kind a remembered consent may answer: a whole sentence on PLAIN_CONSENTS with no help, whose box or giving
    choice is plain; "" for any other consent, yours for this job only; None for a field that is no consent."""
    said = clean(" ".join([field.label, *(choice.label for choice in field.choices)]))  # not help: a survey's own words
    if field.kind != "consent" and field.part != "privacy" and not CONSENT_WORDS.search(said):
        return None
    words = _sentence(clean(field.label), company)
    kind = next((kind for kind, pattern in PLAIN_CONSENTS.items() if pattern.fullmatch(words)), "")
    if field.help.strip():
        return ""
    chosen = consent_choice(field)
    return kind if field.kind == "consent" or (chosen and clean(chosen.label) in GIVES) else ""


def consent_choice(field: Field) -> Choice | None:
    """The choice that gives a consent: the Yes, else the only one that isn't a refusal; None when there isn't one."""
    labels = [(choice, clean(choice.label)) for choice in field.choices]
    giving = [choice for choice, label in labels if label == "yes"]
    giving = giving or [choice for choice, label in labels if not REFUSES.match(label)]
    return giving[0] if len(giving) == 1 else None


def _named(field: Field, text: str) -> list[Choice]:
    """The choices your pick names: the same label, else the same words, else for yes or no, one starting with it."""
    wanted, labels = clean(text), [(choice, clean(choice.label)) for choice in field.choices]
    exact = [choice for choice in field.choices if choice.label == text.strip()]
    same = [choice for choice, label in labels if label == wanted]
    return exact or same or [c for c, label in labels if wanted in ("yes", "no") and label.startswith(f"{wanted} ")]


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
