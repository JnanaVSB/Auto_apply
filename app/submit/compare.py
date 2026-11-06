"""What goes out to a form (Phase 7): the form as the browser shows it is compared field by field with the saved read,
and only your own answers to what passed go in the pack. A value never comes from the page, except a choice's spelling,
taken only once it has matched yours exactly, so the compare can only remove lines. It holds no database, ticket or
clock."""

from collections import Counter
from dataclasses import replace
from pathlib import Path

from app.questionnaire import spelled
from app.submit import FIELD_KINDS, Choice, Drawn, Field, distinct, match

MAX_KEY = 1_000  # characters
MAX_CHANGES = 100
GONE, ADDED, UNCHECKED = "no longer on the form", "new on the form", "wasn't checked"
FILE_UNCHECKED = "the app only checked that it's a file box"
CHOICES_UNCHECKED = "its choices weren't checked"
ALIKE = "two choices look the same, so pick yours on the form"
# Each check of a saved field against the drawn one with the same key, and what is said if it fails. Help, file types,
# the form's part and choices' values aren't here: they aren't reliably drawn, and a value is the site's own code
CHECKS = {
    "label": (lambda saved, drawn: _question(saved.label) == _question(drawn.label), "the question's words changed"),
    "kind": (lambda saved, drawn: saved.kind == drawn.kind, "the kind of box changed"),
    "required": (lambda saved, drawn: saved.required == drawn.required, "whether it's required changed"),
    "choices": (lambda saved, drawn: _choices(saved) == _choices(drawn), "its choices changed"),
}


def fields_from(items: list) -> tuple[Field, ...]:
    """The fields a browser says the form shows, hostile until checked: anything wrong raises ValueError for the whole
    list. Built as Fields, so their limits apply; hidden ones are dropped, and a repeated key keeps the first."""
    fields = [_drawn_field(item) for item in items]
    return distinct([found for found in fields if found.kind != "hidden"])[0]


def compare(
    lines: tuple[match.Line, ...], fields: tuple[Field, ...], board: Drawn
) -> tuple[list[tuple[match.Line, Field]], list[dict]]:
    """The lines whose saved field passed, each with its drawn field (given the saved choices if drawn without any),
    and what to tell you about the rest: each difference as {"question", "what"}. Any saved field that fails is left for
    you, that field alone."""
    shown = {found.key: found for found in fields}
    passed, changes = [], []
    for line in lines:
        saved = line.field
        if saved.kind == "hidden":
            continue
        found = shown.get(saved.key)
        if found is None:
            undrawn = saved.key in board.undrawn or saved.key.startswith(board.undrawn_starts)
            changes.append(_change(saved, UNCHECKED if undrawn else GONE))
            continue
        unseen = board.hidden_choices and found.kind in ("single", "multi") and not found.choices
        as_seen = replace(found, choices=saved.choices) if unseen else found
        names = ("kind",) if saved.kind == "file" else CHECKS
        wrong = [CHECKS[name][1] for name in names if not CHECKS[name][0](saved, as_seen)]
        changes += [_change(saved, what) for what in wrong]
        if wrong:
            continue
        passed.append((line, as_seen))
        if saved.kind == "file" or unseen:
            changes.append(_change(saved, FILE_UNCHECKED if saved.kind == "file" else CHOICES_UNCHECKED))
        if line.status == "fill" and saved.kind in ("single", "multi") and _labels(line, as_seen) is None:
            changes.append(_change(saved, ALIKE))
    saved_keys = {line.field.key for line in lines}
    changes += [
        _change(found, UNCHECKED if found.key in board.unlisted else ADDED)
        for found in fields
        if found.key not in saved_keys
    ]
    return passed, _at_most(changes)


def pack(passed: list[tuple[match.Line, Field]], documents: dict[str, tuple[str, Path] | None]) -> list[dict]:
    """What goes to the browser: only what passed and is yours to fill, and a PDF only by its slot, never its path.
    `documents` is record.documents()."""
    values = [(line.field, _value(line, found, documents)) for line, found in passed if line.status == "fill"]
    return [{"key": saved.key, "kind": saved.kind, "value": value} for saved, value in values if value is not None]


def _drawn_field(item: object) -> Field:
    if not isinstance(item, dict):
        raise ValueError("not an object")
    key, label, kind, required = (item.get(name) for name in ("key", "label", "kind", "required"))
    choices, accept = item.get("choices", []), item.get("accept", [])
    if not (isinstance(key, str) and 1 <= len(key) <= MAX_KEY and isinstance(label, str)):
        raise ValueError("a bad key or label")
    if not (isinstance(kind, str) and kind in FIELD_KINDS and isinstance(required, bool)):
        raise ValueError("a bad kind or required mark")
    if not (isinstance(choices, list) and all(map(_is_choice, choices))):
        raise ValueError("bad choices")
    if not (isinstance(accept, list) and all(isinstance(text, str) for text in accept)):
        raise ValueError("bad file types")
    return Field(
        key,
        label,
        kind,
        required,
        "drawn",
        choices=tuple(Choice(c["label"], c["value"]) for c in choices),
        accept=accept,
    )


def _is_choice(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("label"), str) and isinstance(item.get("value"), str)


def _question(text: str) -> str:
    """A question's words as the compare reads them: required marks at the end don't count, where on a choice a star is
    part of the choice."""
    return spelled(text).rstrip(" *✱")


def _choices(found: Field) -> Counter[str]:
    return Counter(spelled(choice.label) for choice in found.choices)


def _labels(line: match.Line, found: Field) -> list[str] | None:
    """The chosen labels as drawn; None unless each one is exactly one drawn choice."""
    labels = []
    for value in line.value:
        wanted = spelled(line.field.labels[value])
        alike = [choice.label for choice in found.choices if spelled(choice.label) == wanted]
        if len(alike) != 1:
            return None
        labels += alike
    return labels


def _change(found: Field, what: str) -> dict:
    return {"question": found.label, "what": what}


def _at_most(changes: list[dict]) -> list[dict]:
    """The first MAX_CHANGES, then how many more, so the list never looks complete when it isn't."""
    if len(changes) <= MAX_CHANGES:
        return changes
    return [*changes[:MAX_CHANGES], {"question": f"{len(changes) - MAX_CHANGES:,} more", "what": "not shown here"}]


def _value(
    line: match.Line, found: Field, documents: dict[str, tuple[str, Path] | None]
) -> str | list[str] | dict[str, str] | None:
    """The line's value as it goes out, None if it can't go; a choice by its label as drawn, and only one that can be
    told from the others."""
    saved = line.field
    if saved.kind == "file":
        slots = [slot for slot, document in documents.items() if document and str(document[1]) == line.value]
        return {"slot": slots[0]} if slots else None
    if saved.kind == "consent":
        return match.TICKED if line.value == match.TICKED else None
    if saved.kind not in ("single", "multi"):
        return line.value
    labels = _labels(line, found)
    return labels[0] if labels and saved.kind == "single" else labels
