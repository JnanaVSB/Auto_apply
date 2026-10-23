# Adding a new job-board type

auto_apply reads jobs from Simplify's GitHub lists and from three kinds of company job boards: Greenhouse, Lever
and Ashby. Each kind has one small "translator" file in `app/sources/` that turns that site's public data into the
app's shared job shape. Adding another kind (for example Workable or SmartRecruiters) means adding one such file
plus its tests; nothing else in the app has to know how that site's data looks.

Adding one more **company** of a kind that already exists is just one settings line, see the README.

## Before you start
- Use only the site's **official public job-board API**: read-only, no login, no scraping of web pages. Check the
  site's terms of use first. Never add simplify.jobs: its terms forbid automated access.
- The download itself is shared (`app/fetch.py`): it only contacts the hosts listed in `ALLOWED_HOSTS` there
  (collection sites in `COLLECT_HOSTS`, plus Common Crawl's index for the phone book).
  A new board type needs its API host added to that list, which is a security decision for the owner.

## The template: `app/sources/<name>.py`
Copy this, then fill in the field names from the site's real data. Keep it this small: no downloading, no
database, no settings in this file.

```python
"""<Name> public job boards (<api host>), with full descriptions."""

from typing import Any

from app.sources import Job, day_from_iso, html_to_text, parse_each, required_text, workplace

BOARD_PAGE = "https://<public host>/{}"  # the board's public page, to check by hand
JOB_LINK = "https://<public host>/{}/{}"  # one standard link per job: board, then job id


def api_url(board: str) -> str:
    return f"https://<api host>/<path to the board's job list>/{board}"


def parse(data: Any, board: str) -> tuple[list[Job], int]:
    """Turn one board's reply into jobs; also return how many broken entries were skipped."""
    # parse_each refuses anything but a list of jobs; pick the list out of the shape the site really sends
    return parse_each(data.get("jobs") if isinstance(data, dict) else None, lambda entry: _job(entry, board))


def _job(entry: dict, board: str) -> Job | None:
    # Return None for an entry to leave out on purpose (e.g. an unlisted job). Any KeyError, TypeError,
    # ValueError or AttributeError here counts the entry as broken; it is skipped, never a crash.
    location = entry.get("location")
    return Job(
        source=f"<name>:{board}",
        source_job_id=required_text(entry, "id"),
        company=board,  # or the site's own company-name field, if it has one
        title=required_text(entry, "title"),
        url=required_text(entry, "url"),
        locations=[location] if isinstance(location, str) else [],
        # each location with the site's own country field for it, or None to judge it by its text
        place_checks=[(location, entry.get("country"))] if isinstance(location, str) else [],
        remote=workplace(entry.get("workplace")),
        description=html_to_text(entry.get("descriptionHtml") or "") or None,
        date_posted=day_from_iso(entry.get("publishedAt")),
    )
```

Then connect it. These are all the places a new kind needs:
1. **In the new file, next to `api_url`:** `BOARD_PAGE` (the board's public page, `{}` = board name) and `JOB_LINK`
   (one job's public page: board name, then job id). The app uses them to show the board to the user and to store
   one standard link per job.
2. **`app/sources/boards.py` (`BOARDS`):** add the new file under its kind name, for example `"workable": workable`.
   Every other part of the app takes its list of kinds from here: the settings check for `extra_boards`,
   collection, Add job, Add company, and the jobs-list Source filter.
3. **`app/sources/__init__.py` (`BOARD_HOSTS`):** the board's public web host, so job links in Simplify's lists or
   pasted into Add job are recognised. `posting_of` reads such a link as `https://<host>/<board>/<job id>`; a site
   whose links look different needs its own case there, as Greenhouse has. The phone book also looks for the
   new kind's boards in Common Crawl under this host, and tries each new name on it (the cross-check).
4. **`app/fetch.py` (`COLLECT_HOSTS`):** the API host from `api_url`. This list is separate on purpose: it is the
   security list of sites the app may contact, so adding to it is the owner's decision.
5. **Hand-written wording that names the kinds:** the Add company tip and its "not found" message in
   `app/web/actions.py`, and the first paragraph of this guide.

The test `test_every_board_kind_is_connected` fails until items 1, 3 and 4 are done.

## Tests: `tests/test_boards.py`
Use a small made-up reply in the site's real shape, with fake companies only; tests never use the internet.
Add at least:
- a normal job, with every field read correctly;
- a description containing HTML and a `<script>` (stored as plain text, the script dropped);
- broken entries are skipped and counted, and a board where *every* entry is broken fails
  ("none of its N entries could be read");
- a reply in the wrong shape fails the board;
- an empty board means "no openings";
- `api_url` gives the exact address.

Run everything before asking for review:

```bash
python -m pytest
```
```bash
ruff format . && ruff check .
```
