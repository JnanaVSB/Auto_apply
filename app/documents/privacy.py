"""The privacy gate every cloud AI call must pass. Your contact details become placeholders before anything is sent
and are put back only on this computer, when the PDF is built. A last check stops a call whose text still holds
anything that looks like an email, phone number, web address or street address. Values are never logged."""

import re
from dataclasses import dataclass

from app.documents.tiles import HREF, Tile, header_latex

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w.])\+?\(?\d[\d ().-]{8,}\d(?!\w|\.\d)")  # a phone number when it has 10 to 15 digits
WEB = re.compile(
    r"https?://|www\.|\b[\w-]+\.(?:com|org|net|io|ai|dev|edu|gov|co|me|app|info|xyz|us|uk)\b(?:/\S*)?", re.I
)
STREET_WORDS = "Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Parkway|Pkwy"
STREET = re.compile(rf"\b\d{{1,6}}\s+(?:[A-Z][\w.'-]*\s+){{1,4}}(?:{STREET_WORDS})\b")  # e.g. 12 Fake Hill Rd
PLACEHOLDER = re.compile(r"\[(?:ME|EMAIL|PHONE|ADDRESS|LINK)(?:-[0-9]+)?\]")
LINKS = ("linkedin", "github", "portfolio", "scholar", "other_link")
NO_NAME = "Fill in your full name in the Questionnaire first: the privacy filter needs it to hide your name."


class PrivacyError(Exception):
    """The filter can't be set up. Messages never include your details."""


class PrivacyStop(Exception):
    """Text that must not be sent; the message names only the kind of item found."""


@dataclass(frozen=True)
class Shield:
    """Your details, each with how it is found and its placeholder, in the order they are hidden."""

    values: tuple[tuple[re.Pattern, str, str], ...]  # (how it is found, the real text, its placeholder)

    def hide(self, text: str) -> str:
        for pattern, _, placeholder in self.values:
            text = pattern.sub(placeholder, text)
        return text

    def restore(self, text: str) -> str:
        """Only on this computer: each placeholder back to its exact text (an unknown one is left as it is)."""
        real = {placeholder: value for _, value, placeholder in self.values}
        return PLACEHOLDER.sub(lambda match: real.get(match[0], match[0]), text)


def shield(contact: dict, latex: str, tiles: list[Tile]) -> Shield:
    """The placeholders for your questionnaire's contact details, your resume header's, and your tiles' links."""
    name = " ".join(str(contact.get("full_name") or "").split())
    if not name:
        raise PrivacyError(NO_NAME)
    header = header_latex(latex)
    links = [contact.get(field) or "" for field in LINKS] + [tile.facts.get("link", "") for tile in tiles]
    links += [link for link in HREF.findall(header) if not link.startswith("mailto:")]
    emails = [contact.get("email") or "", *EMAIL.findall(header)]
    phones = [contact.get("phone") or "", *(match[0] for match in PHONE.finditer(header) if _is_phone(match[0]))]
    kinds = (  # emails and links before names, so a name inside an address never breaks the address up
        _numbered("EMAIL", emails, lambda email: re.compile(re.escape(email), re.I), str.lower),
        _numbered("LINK", links, _link_pattern, _link_core),
        _numbered("PHONE", phones, _phone_pattern, lambda phone: _digits(phone)[-10:]),
        _numbered("ADDRESS", [" ".join(str(contact.get("street") or "").split())], _name_pattern, str.lower),
        _numbered("ME", _names(name, str(contact.get("preferred_name") or "")), _name_pattern, str.lower),
    )
    return Shield(tuple(value for kind in kinds for value in sorted(kind, key=lambda value: -len(value[1]))))


def last_check(text: str) -> None:
    """Stop a call whose text (your part, never the public job ad) still holds a contact-like item."""
    if EMAIL.search(text):
        raise PrivacyStop("an email address")
    if any(_is_phone(match[0]) for match in PHONE.finditer(text)):
        raise PrivacyStop("a phone number")
    if WEB.search(text):
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
    """Your name as it is usually written: in full, in author lists ("A. Example", "Example, Alex"), and its parts."""
    parts = name.split()
    first, last = parts[0], parts[-1]
    forms = [name, f"{preferred} {last}" if preferred else ""]
    if len(parts) > 1:
        forms += [f"{last}, {first}", f"{first[0]}. {last}", f"{last} {first[0]}.", f"{first} {last}"]
    return forms + [part for part in (first, last, preferred) if len(part) >= 3]


def _name_pattern(text: str) -> re.Pattern:
    words = r"\s+".join(map(re.escape, text.split()))
    return re.compile(rf"(?<!\w){words}(?!\w)", re.I)


def _phone_pattern(phone: str) -> re.Pattern:
    """The same last 10 digits with any spaces, dashes, dots or brackets between them, and an optional +1."""
    digits = _digits(phone)[-10:]
    return re.compile(r"(?<![\w.])(?:\+?1[ .-]*)?\(?" + r"[ ().-]*".join(digits) + r"(?![\w])")


def _link_pattern(link: str) -> re.Pattern:
    return re.compile(rf"(?<![\w/.])(?:https?://)?(?:www\.)?{re.escape(_link_core(link))}/?(?![\w/-])", re.I)


def _link_core(link: str) -> str:
    """The address without https://, www. and a final /, so all its spellings are the same link."""
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", link.strip(), flags=re.I).rstrip("/").lower()


def _is_phone(text: str) -> bool:
    return 10 <= len(_digits(text)) <= 15


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)
