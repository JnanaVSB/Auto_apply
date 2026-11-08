# Adding a new job-board type

auto_apply reads jobs from Simplify's GitHub lists and from three kinds of company job boards: Greenhouse, Lever
and Ashby. Each kind has one small "translator" file in `app/sources/` that turns that site's public data into the
app's shared job shape. Adding another kind (for example Workable or SmartRecruiters) means adding one such file
plus its tests; nothing else in the app has to know how that site's data looks.

Adding one more **company** of a kind that already exists is just one settings line, see the README.

## A feed is not a board
A feed is a job list read as one file, such as Remote OK's: it is listed in `FEEDS` in `app/sources/feeds.py`, never in
`BOARDS`, and has no row in the `sources` table or on the Boards page. Its file gives `LABEL`, `HOST`, `SOURCE` and
`read(fetcher, keywords)`, which returns three things: the jobs, how many entries were skipped, and whether the list is
whole (a list cut short closes nothing). A feed decides a job only when no company board lists it, as a Simplify list
does. The rest of this guide is about boards.

## Before you start
- Use only the site's **official public job-board API**: read-only, no login, no scraping of web pages. Check the
  site's terms of use first. Never add simplify.jobs: its terms forbid automated access.
- The download itself is shared (`app/fetch.py`): it only contacts the hosts listed in `ALLOWED_HOSTS` there
  (collection sites in `COLLECT_HOSTS`, plus Common Crawl's index and Hacker News' API for the phone book, and
  Lever's application pages for a job's form).
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

## A site's Submit
This part is for a site the browser plugin can fill, not for a new board. The plugin notices your press of Submit only
on a real `type=submit` control of the form. A site where that can't work, such as Ashby with no `<form>`, is never
noticed until its file in `plugin/boards/` names the control as `submit`, a selector inside the form. Take that only
from a real form you saw yourself, never from a button's words: a page chooses those.

## A site's drop-downs
The plugin leaves every custom drop-down for you until the site's file in `plugin/boards/` says how it shows a pick, as
`dropdown`: selectors for the part round the text box (`control`), the words of a pick (`picked`) and the words shown
with nothing picked (`empty`). The drop-down must open when its text changes, and its text box must name its list with
`aria-controls`. Take these only from a real form you saw yourself.

## A site's thank-you address
The app can record an application by itself once it knows the address a site thanks you at. Every site ships with this
off. To switch a site on, set `thanks` on `DRAWN` in the site's own file in `app/submit/`, to how the address ends after
a real Submit, on the same host:

```python
DRAWN = Drawn(thanks="/thanks")
```

Take it only from a real application you saw yourself, never from a guess: a wrong sign can write a record that can't be
erased. The test `test_every_site_ships_off` holds all three sites off, so switching one on means changing that test
too.

### Before any site is switched on
Don't set `thanks` on any site until all four of these are done. A page writes its own address, and can set up a real
press, so today a site switched on can reach a permanent record with nothing sent.

1. **Take the automatic write away.** The sign should only say the thank-you page showed, with one press of **I
   submitted it** beside it. Or make that record erasable. Either way, match the whole path, tied to the job, not just
   how the address ends: every company on a board shares its host.
2. **A key isn't a press.** Someone with the key and a live login can report a press and a thank-you page with no
   browser at all. The first condition closes this. Otherwise refuse `thanked` on toolbar tickets.
3. **Say only what is known.** The record's and the popup's words must say this address looks like the site's thank-you
   page, not that it showed one.
4. **Keep a value in its place.** In the paper, a value with a line break can pose as a `From:` line. Put a value on one
   line, or mark where it ends.
