"""Ashby application forms: where a form is, how its drawn form differs, and what the plugin's read of one gives. Only a
browser that draws the form can see its questions."""

import re
from dataclasses import replace

from app.questionnaire import clean
from app.submit import Drawn, Field, Form

FORM_PAGE = "https://jobs.ashbyhq.com/{}/{}/application"
DRAWN = Drawn()
# The page writes this form, so only these go out by themselves, each in its own kind of box and under its own plain
# words: every other answer waits for your decision. `form` drops a box wearing one on another kind, and
# `preview.held_back` checks again through `own_box`, on purpose
OWN_KEYS = {
    "_systemfield_name": ("text", re.compile(r"(full |legal |your )?name")),
    "_systemfield_email": ("text", re.compile(r"(your )?e ?mail( address)?")),
    "_systemfield_resume": ("file", re.compile(r"resume( cv)?|cv")),
}


def form(fields: tuple[Field, ...]) -> Form:
    """The form as the plugin read it. A file box with no label is the site's own top box, never a question, and a box
    wearing one of the site's own keys on another kind isn't that box."""
    kept = [
        field for field in fields
        if (field.kind != "file" or field.label.strip()) and OWN_KEYS.get(field.key, (field.kind,))[0] == field.kind
    ]  # fmt: skip
    return Form(tuple(replace(field, part=_part(field.key)) for field in kept))


def own_box(field: Field) -> bool:
    """Whether this is one of the site's own boxes: key, kind and whole words all fit."""
    kind, words = OWN_KEYS.get(field.key, ("", None))
    return kind == field.kind and words.fullmatch(clean(field.label)) is not None


def _part(key: str) -> str:
    return "standard" if key.startswith("_systemfield_") else "company"
