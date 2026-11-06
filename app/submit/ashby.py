"""Ashby application forms: only where a form is and how its drawn form differs. The app doesn't read Ashby's yet."""

from app.submit import Drawn

FORM_PAGE = "https://jobs.ashbyhq.com/{}/{}/application"
DRAWN = Drawn(frozenset(), (), frozenset(), hidden_choices=False, file_checks=("key", "kind"))
