"""Ashby application forms: where a form is, how its drawn form differs, and what the plugin's read of one gives. Only a
browser that draws the form can see its questions."""

from dataclasses import replace

from app.submit import Drawn, Field, Form

FORM_PAGE = "https://jobs.ashbyhq.com/{}/{}/application"
DRAWN = Drawn()


def form(fields: tuple[Field, ...]) -> Form:
    """The form as the plugin read it. A file box with no label is the site's own top box, never a question."""
    kept = [field for field in fields if field.kind != "file" or field.label.strip()]
    return Form(tuple(replace(field, part=_part(field.key)) for field in kept))


def _part(key: str) -> str:
    return "standard" if key.startswith("_systemfield_") else "company"
