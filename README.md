# auto_apply

A personal job-application helper. Each person runs their own copy with their own data;
nothing personal is ever stored in this repository.

> **Status:** early development. Phase 1 (foundation): private settings, secrets in the OS keychain
> and an encrypted database. Phase 2: job collection from Simplify's public GitHub lists and the
> Greenhouse, Lever and Ashby job boards. Phase 3: a jobs dashboard on this computer. Applying comes
> in later phases.

## Requirements
- [Miniconda](https://docs.anaconda.com/miniconda/) (or any conda)
- An OS keychain: macOS Keychain, Windows Credential Manager, or on Linux GNOME Keyring / KWallet

## Setup
1. Create and activate the environment.
   On Linux (exact, locked versions of every package):
   ```bash
   conda create -n auto_apply --file conda-linux-64.lock
   conda activate auto_apply
   pip install --require-hashes --no-deps -r requirements-pip.txt
   ```
   On macOS or Windows:
   ```bash
   conda env create -f environment.yml
   conda activate auto_apply
   ```
2. Create your private settings file (the `data/` folder is never committed), then set your own
   `roles` (Simplify categories and keywords) in `data/config.yaml`:
   ```bash
   mkdir -p data && cp config.example.yaml data/config.yaml
   ```
3. Create the encrypted database and choose a master password (12+ characters):
   ```bash
   python -m app.db setup
   ```
   Keep the master password in a password manager. **If both the master password and the
   keychain entry are lost, the data cannot be recovered.**
4. Recommended: scan for secrets before every commit (needs the `auto_apply` env active):
   ```bash
   pre-commit install
   ```
   The first commit afterwards builds gitleaks once, which takes about a minute.

## Day to day
- The database unlocks automatically from your OS keychain; you never type the master password.
- Back up your `data/` folder regularly. It is encrypted, so a cloud or USB backup is fine. Close the dashboard and
  any collection first: while they run, the latest changes are kept in `app.db-wal` and `app.db-shm` next to the
  database (also encrypted); SQLite folds them back into `app.db` when the last one closes.
- Lost keychain entry: `python -m app.db recover`
- New laptop: copy your backed-up `data/` folder into the project first, then run `python -m app.db recover`
- Change the master password: `python -m app.db change-password`
- Lost or damaged recovery.key, or forgot the master password while the app still opens:
  `python -m app.db new-recovery-key`

## Collect jobs
```bash
python -m app.collect
```
Reads Simplify's two public job lists on GitHub, then every Greenhouse, Lever and Ashby board the app knows (the
"phone book", below, plus your `extra_boards`). Each site has its own queue and all sites are read at the same time,
at most `requests_per_second` new requests to each site (1 by default). It keeps only US jobs matching your
`roles` and stores each job once. Jobs are never deleted or hidden: a job its board or list drops becomes
"no longer available" with the reason ("removed from its board", "board shut down", "company stopped hiring",
"removed from Simplify" or "inactive on Simplify"), and a saved job later judged to be outside the US keeps its
label and gets a "please check location" mark. A run takes about one second per board on the site with the most
boards (about 5 minutes for 700 boards, 1–1.5 hours for 10,000); its first line says how long. Running it again is
safe.

At the end it prints a summary:
- **failed**: sources that could not be downloaded. They change nothing. If a site does not answer at all, the
  message says the site may be down or your network may be blocking it; with no internet at all the run stops
  at once.
- **Needs investigation**: a source whose data looks wrong (for example none of its entries can be read, or a
  board suddenly lists no jobs at all). Nothing is changed for it, so no application is missed; please check.
- **Stopped reading**: a board that answered "not found" three runs in a row. It is read again automatically if
  Simplify links a newer job there, or if you add it to `extra_boards`.
- **Company stopped hiring**: a board that listed jobs and then sent none three runs in a row (the first two show
  under "Needs investigation"). Its jobs are kept, marked "company stopped hiring".

If your settings file is from before Phase 2, copy the `roles` and `sources` sections from `config.example.yaml`.

To save one job you found yourself (on a company site, or by browsing simplify.jobs and clicking Apply):
```bash
python -m app.collect add "<job link>"
```
Greenhouse, Lever and Ashby links are read from the company's board; for other sites you type the
company and job title. A job you add is marked "added by you", and later runs never change its US label.
simplify.jobs links themselves are refused: the app never opens that website.

To see one saved job's status and every board or list it appears on (changes nothing):
```bash
python -m app.collect show "<job link>"
```

## Phone book of company boards
```bash
python -m app.phonebook refresh
```
Greenhouse, Lever and Ashby publish no list of the companies that use them, so the app keeps its own "phone book".
A refresh takes company board names from every job-board link in Simplify's lists (closed jobs too), from the other
public GitHub job lists in your `phonebook_lists` setting (add only lists whose licence allows it), and from
[Common Crawl](https://commoncrawl.org)'s public index of the web. Each new name is checked once with its site's own
public API: a board that exists is read on every collection from then on (after the boards that already had jobs for
you), and its matching jobs are saved at once; one that does not exist is remembered and never asked again. Each new
name is also tried once on the other sites; a board found that way is added only if it shares at least 3 job titles
with the first one, otherwise it waits for you on the dashboard's Boards page.

It is slow on purpose (Common Crawl asks for pauses between requests and is often overloaded): often an hour or more.
It is safe to stop with Ctrl+C; the next refresh continues where it stopped. It does not run while a collection runs,
and the other way round. Nothing is ever deleted.

## Dashboard
```bash
python -m app.web
```
Opens the jobs dashboard in your browser at http://127.0.0.1:8765. It works on this computer only. The first time,
it asks you to choose a dashboard password in the terminal (12+ characters; use a different one from your master
password). Press Ctrl+C in the terminal to stop it.
- **Jobs:** every saved job, 50 per page, with filters (source, level, US location, remote, date posted, role category,
  term, sponsorship, status and reason, added by you, not interested) and sorting. Filters stay in the page address, so
  you can bookmark a view. Search ignores capital letters only for English letters (A–Z): "société" does not find
  "SOCIÉTÉ". Open a job to see its details, its description and every board or list it is on.
- **On a job's page:** mark it "not interested" (this only hides it; nothing is ever deleted), or set its location to
  US or not US. Later collections keep your choice until you click "Let collections decide again".
- **Add:** save a job by its link (the same rules as `collect add`), or find a company's boards on Greenhouse, Lever and
  Ashby by typing its name: every likely board name is tried on every site, and every board found is shown for you to
  check and add.
- **Boards:** every company board the app knows, with who found it, whether it is active or discovered (found by the
  phone book, no matching job yet), the company name it reports and when it was last read. Search, stop reading a
  board or read it again, and add or decline the boards the phone book could not confirm on its own.
- **Collect:** "Collect now" runs the same collection as `python -m app.collect`, and "Refresh phone book" the same
  refresh as `python -m app.phonebook refresh`, with live progress. Only one of them runs at a time, including one
  started from a terminal. While the dashboard is open, it can collect every few hours and refresh the phone book every
  few days (monthly unless you change it; see Settings).
- **Settings:** roles, Simplify lists, extra boards, phone-book lists, the automatic collection and phone-book refresh,
  and requests per second to each job site. Saving checks them with the same rules as the file and rewrites
  `data/config.yaml` (comments in that file are not kept).

Forgot the dashboard password? Stop the dashboard and run:
```bash
python -m app.web reset-password
```
If another program is using the address, start it with `python -m app.web --port 8766`.

Security: the dashboard only listens on this computer and refuses requests addressed to any other name. Its login locks
for 15 minutes after 5 wrong passwords and logs you out after 30 idle minutes. Every change carries an anti-forgery
token, pages load nothing from the internet, and job text is always shown as plain text.

## Adding companies and board types
- **Another company** on Greenhouse, Lever or Ashby: add one line to `extra_boards` in `data/config.yaml`, using the
  name from the board's web address, e.g. `greenhouse:openai` for `job-boards.greenhouse.io/openai`. Boards linked
  from Simplify's lists, other approved job lists and Common Crawl are found automatically by the phone book. Or use
  **Add company** in the dashboard.
- **Another kind of job board** (for example Workable): one small translator file in `app/sources/` plus its tests.
  See [docs/adding-a-board-type.md](docs/adding-a-board-type.md) for the template and the test checklist.

## Privacy and security
- `data/` (settings, database, recovery file) and `exports/` are git-ignored.
- Secrets live only in your OS keychain; the database is encrypted with SQLCipher.
- Collecting jobs sends nothing about you: only public read requests, identified as
  "auto_apply personal job search". Job descriptions are stored as plain text.

## Credits
US and foreign place names (used to decide whether a job is in the US) come from
[GeoNames](https://www.geonames.org), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
They are stored in `app/places.json`; `tools/make_places.py` explains how to rebuild it.

The dashboard uses [HTMX](https://htmx.org) (BSD Zero-Clause licence), stored in `app/web/static/` so nothing is loaded
from the internet: version 2.0.11, file `dist/htmx.min.js` from the npm package `htmx.org@2.0.11`, SHA-256
`d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` (a test checks it).

## Development
- Run all tests: `python -m pytest`
- Format and lint: `ruff format .` and `ruff check .`
- After an approved upgrade (change `environment.yml`, rebuild the environment), refresh the Linux lock file:
  ```bash
  conda list --explicit --sha256 -n auto_apply > conda-linux-64.lock
  ```

## License
MIT. See [LICENSE](LICENSE).
