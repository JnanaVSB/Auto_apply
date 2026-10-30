"""The privacy gate every cloud AI call must pass: text leaves this computer only through Shield.outgoing. Your contact
details become placeholders before anything is sent and are put back only on this computer, when the PDF is built. A
last check stops a call whose text still holds anything that looks like an email, phone number, web address or street
address. Values are never logged."""

import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from itertools import pairwise

from app.documents.tiles import HREF, Tile, header_latex
from app.resume import GROUP, plain_text

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SEPARATORS = r" ().\-\u00a0\u2000-\u200a\u2010-\u2013\u202f\u205f\u3000"  # spaces, dots, brackets, hyphens, dashes
PHONE = re.compile(rf"(?<![\w.])\+?\(?\d[\d{SEPARATORS}]{{8,}}\d(?!\w|\.\d)")  # a phone number with 10 to 15 digits
# Endings of personal web addresses; file types such as .sh, .rs, .pl or .cc are left out on purpose
ENDINGS = (
    "com|org|net|io|ai|dev|edu|gov|co|me|app|info|xyz|us|uk|ca|de|in|eu|tech|fr|nl|jp|cn|au|ch|kr|nz|ie|sg|hk|tw|il|br"
    "|ac|site|online|page|blog|cloud|tv"
)
# A web address: https:// or www. in any case, else a name.ending with the ending in lower case or followed by "/", so
# tech names such as ASP.NET or Socket.IO are not addresses
WEB = re.compile(rf"(?i:https?://|www\.)|\b[\w-]+\.(?:(?:{ENDINGS})\b|(?i:{ENDINGS})/)")
TECH_NAMES = {"asp.net", "vb.net", "ado.net", "socket.io", "fast.ai"}  # also websites, but never yours: allowed alone
STREET_WORDS = "Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Parkway|Pkwy"
STREET = re.compile(rf"\b\d{{1,6}}\s+(?:[A-Z][\w.'-]*\s+){{1,4}}(?:{STREET_WORDS})\b")  # e.g. 12 Fake Hill Rd
PLACEHOLDER = re.compile(r"\[(?:ME|EMAIL|PHONE|ADDRESS|LINK)(?:-[0-9]+)?\]")
LINKS = ("linkedin", "github", "portfolio", "scholar", "other_link")
NO_NAME = "Fill in your full name in the Questionnaire first: the privacy filter needs it to hide your name."
# The name at the top of a source: \name{..} or \author{..}, else its first big line, as in {\Huge Alex Example}
HEADER_NAME = re.compile(rf"\\(?:name|author)\s*({GROUP})|\\(?:Huge|huge|LARGE|Large)(?![A-Za-z@])([^\n}}]*)")
NAME_WORD = re.compile(r"[^\W\d_]+(?:[.'’-][^\W\d_]*)*")  # letters, with dots, apostrophes or hyphens inside
LATIN = [chr(code) for code in range(0xC0, 0x250)]  # accented letters: Latin-1, Latin Extended-A and -B
STROKES = str.maketrans("øØłŁđĐıħĦ", "oOlLdDihH")  # letters Unicode doesn't split into a plain letter and a mark


class PrivacyError(Exception):
    """The filter can't be set up. Messages never include your details."""


class PrivacyStop(Exception):
    """Text that must not be sent; the message names only the kind of item found."""


@dataclass(frozen=True)
class Shield:
    """Your details, each with how it is found and its placeholder, in the order they are hidden."""

    values: tuple[tuple[re.Pattern, str, str], ...]  # (how it is found, the real text, its placeholder)

    def outgoing(self, text: str) -> str:
        """The only way text leaves this computer: your details hidden, then the last check (else PrivacyStop)."""
        hidden = self.hide(text)
        last_check(hidden)
        return hidden

    def hide(self, text: str) -> str:
        text = unicodedata.normalize("NFC", text)  # one way of writing each accented letter
        for pattern, _, placeholder in self.values:
            text = pattern.sub(placeholder, text)
        return text

    def restore(self, text: str) -> str:
        """Only on this computer: each placeholder back to its exact text (an unknown one is left as it is)."""
        real = {placeholder: value for _, value, placeholder in self.values}
        return PLACEHOLDER.sub(lambda match: real.get(match[0], match[0]), text)


def shield(contact: dict, sources: list[str], tiles: list[Tile]) -> Shield:
    """The placeholders for your questionnaire's contact details, the headers of every source the Writer uses (your
    resume.tex and any uploaded files, as LaTeX; app/documents/writer_sources.py), and your tiles' links."""
    name = " ".join(str(contact.get("full_name") or "").split())
    if not name:
        raise PrivacyError(NO_NAME)
    header = "\n".join(map(header_latex, sources))
    links = [contact.get(field) or "" for field in LINKS] + [tile.facts.get("link", "") for tile in tiles]
    links += [link for link in HREF.findall(header) if not link.startswith("mailto:")]
    emails = [contact.get("email") or "", *EMAIL.findall(header)]
    phones = [contact.get("phone") or "", *(match[0] for match in PHONE.finditer(header) if _is_phone(match[0]))]
    names = _names(name, str(contact.get("preferred_name") or ""))
    names += [form for other in map(_header_name, sources) if other for form in _names(other, "")]
    # Each spelling of your name in your sources gets its own placeholder, so it comes back exactly as written
    tile_texts = ("\n".join([*map(str, tile.facts.values()), *tile.bullets]) for tile in tiles)
    texts = unicodedata.normalize("NFC", "\n".join([*map(plain_text, sources), *tile_texts]))
    names += [match[0] for form in names if form for match in _name_pattern(form).finditer(texts)]
    kinds = (  # emails and links before names, so a name inside an address never breaks the address up
        _numbered("EMAIL", emails, lambda email: re.compile(re.escape(email), re.I), str.lower),
        _numbered("LINK", links, _link_pattern, _link_core),
        _numbered("PHONE", phones, _phone_pattern, lambda phone: _digits(phone)[-10:]),
        _numbered("ADDRESS", [" ".join(str(contact.get("street") or "").split())], _name_pattern, str.lower),
        _numbered("ME", names, _name_pattern, lambda form: " ".join(form.split())),
    )
    # Longest first; at equal length the later one, so a spelling found in your sources goes before the general form
    return Shield(tuple(value for kind in kinds for value in sorted(kind[::-1], key=lambda value: -len(value[1]))))


def last_check(text: str) -> None:
    """Stop a call whose text (your part, never the public job ad) still holds a contact-like item."""
    text = PLACEHOLDER.sub("x", text)  # a placeholder inside an address (alexander.[ME-6]@uni.de) must not hide it
    if EMAIL.search(text):
        raise PrivacyStop("an email address")
    if any(_is_phone(match[0]) for match in PHONE.finditer(text)):
        raise PrivacyStop("a phone number")
    if any(match[0].lower() not in TECH_NAMES for match in WEB.finditer(text)):
        raise PrivacyStop("a web address")
    if STREET.search(text):
        raise PrivacyStop("a street address")


def _numbered(kind: str, found: list[str], pattern, same) -> list[tuple[re.Pattern, str, str]]:
    """[KIND], [KIND-2], ... for each different value, in the order found; empty values and repeats left out."""
    seen, values = set(), []
    for value in (text.strip() for text in found):
        if value and same(value) and same(value) not in seen:
            seen.add(same(value))
            values.append((pattern(value), value, f"[{kind}]" if not values else f"[{kind}-{len(values) + 1}]"))
    return values


def _names(name: str, preferred: str) -> list[str]:
    """Your name as it is usually written: in full, in author lists ("A. Example", "Example, A.", "Example, Alex"),
    and its parts of 3 or more letters. Middle initials and accents are left to the pattern (_name_pattern)."""
    parts = name.split()
    first, last = parts[0], parts[-1]
    forms = [name, f"{preferred} {last}" if preferred else ""]
    if len(parts) > 1:
        forms += [
            f"{last}, {first}",
            f"{first[0]}. {last}",
            f"{last} {first[0]}.",
            f"{first} {last}",
            f"{last}, {first[0]}.",
        ]
    return forms + [part for part in (first, last, preferred) if len(part) >= 3]


def _header_name(latex: str) -> str:
    """The name at the top of a source (2 to 5 words of letters), or "" when there is none."""
    for match in HEADER_NAME.finditer(header_latex(latex)):
        words = plain_text(match[1] or match[2]).split()
        if 2 <= len(words) <= 5 and all(NAME_WORD.fullmatch(word) for word in words):
            return " ".join(words)
    return ""


def _name_pattern(text: str) -> re.Pattern:
    """Whole words in any case, with or without accents, and with any middle initials ("A. J. Li", "Li, A. J.")."""
    words = ["".join(map(_letter, word)) for word in _fold(text).split()]
    pattern = words[0]
    for before, word in pairwise(words):
        pattern += (r"(?:\s*[A-Z]\.)*\s*" if before.endswith(r"\.") else r"(?:\s+[A-Z]\.)*\s+") + word
    if pattern.endswith(r"\."):
        pattern += r"(?:\s*[A-Z]\.)*"
    return re.compile(rf"(?<!\w){pattern}(?!\w)", re.I)


@cache
def _letter(char: str) -> str:
    """A plain letter with its accented forms (e: [eéèêë...]), so "Jose" also finds "José"; anything else as it is."""
    if not (char.isascii() and char.isalpha()):
        return re.escape(char)
    forms = [other.lower() for other in LATIN if _fold(other).lower() == char.lower() and len(other.lower()) == 1]
    return f"[{char.lower()}{''.join(forms)}]"


def _fold(text: str) -> str:
    """Without accents: é -> e, ø -> o."""
    letters = unicodedata.normalize("NFKD", text.translate(STROKES))
    return "".join(char for char in letters if not unicodedata.combining(char))


def _phone_pattern(phone: str) -> re.Pattern:
    """The same last 10 digits with any spaces, dashes, dots or brackets between them, and an optional +1."""
    digits = _digits(phone)[-10:]
    return re.compile(rf"(?<![\w.])(?:\+?1[{SEPARATORS}]*)?\(?" + f"[{SEPARATORS}]*".join(digits) + r"(?![\w])")


def _link_pattern(link: str) -> re.Pattern:
    return re.compile(rf"(?<![\w/.])(?:https?://)?(?:www\.)?{re.escape(_link_core(link))}/?(?![\w/-])", re.I)


def _link_core(link: str) -> str:
    """The address without https://, www. and a final /, so all its spellings are the same link."""
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", link.strip(), flags=re.I).rstrip("/").lower()


def _is_phone(text: str) -> bool:
    return 10 <= len(_digits(text)) <= 15


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)
