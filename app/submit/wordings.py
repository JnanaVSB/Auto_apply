"""Who may answer a form's question (Phase 7): the four classes of `care` and the words they rest on, whether a form has
a rule about AI, what kind of consent a question is, and what an answer is an answer to. The class decides who may save,
suggest or write an answer, never that one is safe to fill: that is app/submit/match.py's short lists."""

import re
from functools import lru_cache

from app.questionnaire import clean
from app.submit import Choice, Field, Form

# The four classes of `care`: who may answer a question.
PROMISE = "promise"  # AI-use and arbitration: nothing answers it, you do it in the window
LEGAL = "legal"  # law and personal facts: never saved, suggested or the Writer's
THIS_COMPANY = "company"  # names or is about this company: never saved or suggested; an essay may be the Writer's
ORDINARY = ""  # may be saved and suggested

COMPANY_FORM = re.compile(  # not part of a name, when compared
    r"(?: (?:inc|corp|corporation|llc|ltd|labs|co|technologies|systems|group|holdings|limited|gmbh|plc|ai|health"
    r"|software))+$"
)
COMMON_WORDS = frozenset({  # a company's first word that names nothing by itself
    "american", "best", "blue", "data", "digital", "east", "first", "general", "global", "great", "green",
    "international", "national", "next", "north", "open", "south", "true", "united", "west", "world",
})  # fmt: skip
KEYS = {  # the sites' own fields, by key: (Questionnaire section, answer)
    "name": ("contact", "full_name"), "preferred_name": ("contact", "preferred_name"), "email": ("contact", "email"),
    "phone": ("contact", "phone"), "urls[LinkedIn]": ("contact", "linkedin"), "urls[GitHub]": ("contact", "github"),
    "urls[Portfolio]": ("contact", "portfolio"), "urls[Other]": ("contact", "other_link"),
    "_systemfield_name": ("contact", "full_name"), "_systemfield_email": ("contact", "email"),
}  # fmt: skip
SITE_BOXES = frozenset({*KEYS, "first_name", "last_name", "location", "city", "comments", "resume", "cover_letter"})
SURVEY_COUNTRIES = re.compile(r"\(for candidates in: ([^)]*)\)$")  # how app/submit/lever.py words a survey's help
KNOWN = {  # a legal topic, by its cleaned wording: filled only on match's PLAIN, under its legal conditions
    "work": re.compile(r"(?=.*\b(?:authori[sz]\w*|eligib\w*|legally|lawfully|permitted|able to|right to work"
                       r"|can you)\b)(?=.*\b(?:work|employment)\b)"),
    "sponsorship": re.compile(r"\bsponsor"), "relocation": re.compile(r"\brelocat"),
    "clearance": re.compile(r"\bclearance\b"),
    "over_18": re.compile(r"\b(?:18 years|over 18|at least 18|are you 18)\b"),
}  # fmt: skip
CONSENT_WORDS = re.compile(  # also in a choice: "Applicant Terms" with "I agree"
    r"\b(?:agree|agrees|agreed|acknowledg\w*|attest\w*|certify|certifies|consent\w*|opt in|have read|confirm|accept"
    r"|declare|swear|understand)\b|\bauthori[sz]e\b(?! to work)"
)
PLAIN_CONSENTS = {  # the only sentences a remembered consent answers, whole, with a site's name as "company"
    "privacy": re.compile(r"consent to the privacy notice gdpr"
                          r"|i(?: have read and)? (?:agree to|accept|acknowledge|consent to) the privacy "
                          r"(?:policy|notice)"
                          r"|please review and acknowledge(?: company s)?(?: candidate)? privacy (?:policy|notice)"
                          r"(?: company com candidate privacy notice)?"),
    "processing": re.compile(r"consent to data processing gdpr"),
    "retention": re.compile(r"consent to keeping your data gdpr"),
    "texting": re.compile(r"do you opt in to(?: receive)? whatsapp messages(?: from company(?: recruiting)?)?"),
    "marketing": re.compile(r"company (?:has my consent to|may) contact me about future job opportunities"),
}  # fmt: skip
GIVES = frozenset({"yes", "i agree", "agree", "i accept", "accept", "i acknowledge", "acknowledge",
                   "acknowledge confirm", "i consent", "i confirm", "confirm"})  # fmt: skip
REFUSES = re.compile(r"(?:no|not|decline\w*|do not|don t|i do not|i don t|disagree|reject\w*|refuse\w*)\b")

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
NO_WORD = (
    r"not|no|never|refrain\w*|prohibit\w*|reject\w*|avoid\w*|without|discourag\w*|forbid\w*|unacceptable|disqualif\w*"
    r"|banned|disallow\w*|isn t|won t|aren t"
)
NO_AI = re.compile(  # on cleaned wording: when unsure, warn
    rf"\b(?:{AI_WORD})\b(?: \w+){{0,7}} (?:{NO_WORD})\b|\b(?:{NO_WORD})\b(?: \w+){{0,7}} (?:{AI_WORD})\b"
    r"|\b(?:own work|my own|your own(?! words)|the candidate s own|the applicant s own|written by you"
    r"|write this yourself|on (?:your|their) own|answer personally)\b"
)


@lru_cache(maxsize=2048)  # the Ready page matches every job on it, and every rule and route asks again
def care(field: Field, company: str = "") -> str:
    """The question's class: who may save, suggest or write its answer. Reads the label, help, key and choices, with
    the company's name left in, so a company called Visa can't hide a word. Your own boxes are never in question."""
    if field.key in SITE_BOXES:
        return ORDINARY
    if promise(field, company, help_too=field.kind != "long_text"):
        return PROMISE
    said = _said(field)
    legal = any(pattern.search(said) for pattern in KNOWN.values()) or LEGAL_WORDS.search(_with_choices(field))
    if legal or consent_kind(field, company) is not None or promise(field, company):  # an essay under an AI rule
        return LEGAL
    return THIS_COMPANY if names(said, company) or ABOUT_THIS_COMPANY.search(said) else ORDINARY


@lru_cache(maxsize=2048)  # as for `care`
def promise(field: Field, company: str, help_too: bool = True) -> str:
    """An AI-use promise's or an arbitration agreement's name as the preview says it, else "". A mention of AI is one
    unless it is positively a skill question."""
    said = _promise_words(field, company, help_too)
    if ARBITRATION.search(said):
        return "arbitration"
    return "AI use" if _ai_use(said) else ""


def _promise_words(field: Field, company: str, help_too: bool = True) -> str:
    """Everything a promise can hide in: with the key and choices, without the name `_label` hides or the survey's
    country list; `help_too` False for a long box, where help is a rule for the answer, not a box to tick."""
    help_ = SURVEY_COUNTRIES.sub("", field.help) if help_too else ""
    return clean(" ".join([_label(field, company), help_, field.key, *(choice.label for choice in field.choices)]))


def _ai_use(said: str) -> bool:
    return AI.search(said) is not None and (not SKILL.search(said) or PROCESS.search(said) is not None)


def _label(field: Field, company: str) -> str:
    """The label, for the promise words: "Why Scale AI?" says nothing but the name. A company called just AI hides
    nothing, and no other wording hides the name."""
    label, name = COMPANY_FORM.sub("", clean(field.label)), bare(company)
    is_ai = AI.fullmatch(name) or AI.fullmatch(clean(company))
    only_the_name = label in (f"why {name}", f"why do you want to work at {name}") and not is_ai
    return "" if only_the_name else clean(field.label)


def _said(field: Field) -> str:
    """The question's words. A survey's country list is ours, not the form's: "AI" there is Anguilla."""
    return clean(f"{field.label} {SURVEY_COUNTRIES.sub('', field.help)} {field.key}")


def _with_choices(field: Field) -> str:
    """`_said` and the choices, for LEGAL_WORDS only: "Citizen" in a choice is legal, and a choice names no company."""
    return " ".join([_said(field), *(clean(choice.label) for choice in field.choices)])


def sentence(words: str, company: str) -> str:
    """The cleaned label with the company's name as the word "company", for the lists that hold a site's name."""
    name = bare(company)
    return re.sub(rf"(?<!\S){re.escape(name)}(?!\S)", "company", words) if name else words


def no_writer(form: Form, company: str) -> bool:
    """Whether the form holds an AI-use promise or says no to AI: then it has no Write answers."""
    return any(_ai_use(_promise_words(field, company)) or _says_no_ai(field, company) for field in form.fields)


def _says_no_ai(field: Field, company: str) -> bool:
    """`_promise_words` with no key: NO_AI reads a sentence, and a key is none."""
    choices = (choice.label for choice in field.choices)
    help_ = SURVEY_COUNTRIES.sub("", field.help)
    return NO_AI.search(clean(" ".join([_label(field, company), help_, *choices]))) is not None


def consent_kind(field: Field, company: str = "") -> str | None:
    """The kind a remembered consent may answer: a whole sentence on PLAIN_CONSENTS with no help, whose box or giving
    choice is plain; "" for any other consent, yours for this job only; None for a field that is no consent."""
    said = clean(" ".join([field.label, *(choice.label for choice in field.choices)]))  # not help: a survey's own words
    if field.kind != "consent" and field.part != "privacy" and not CONSENT_WORDS.search(said):
        return None
    words = sentence(clean(field.label), company)
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


def names(text: str, company: str) -> bool:
    """Whether the text holds the company's name, as whole words, without COMPANY_FORM's endings, or its first word
    when that says more than COMMON_WORDS."""
    name, said = bare(company), f" {clean(text)} "
    first = name.partition(" ")[0]
    return bool(name) and (
        f" {name} " in said or (len(first) >= 4 and first not in COMMON_WORDS and f" {first} " in said)
    )


def bare(company: str) -> str:
    return COMPANY_FORM.sub("", clean(company))


def wording(field: Field) -> str:
    """What an answer is an answer to: a changed question, choices or kind of box, or a box now required, is asked
    again. Whole: the help as the form words it, country list and all, and no key, which never changes."""
    needed = "required" if field.required else "optional"
    return clean(" ".join([field.kind, needed, field.label, field.help, *(choice.label for choice in field.choices)]))
