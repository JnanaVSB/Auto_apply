# auto_apply

A personal job-application helper. Each person runs their own copy with their own data;
nothing personal is ever stored in this repository.

> **Status:** early development. Phase 1 (foundation): private settings, secrets in the OS keychain
> and an encrypted database. Phase 2: job collection from Simplify's public GitHub lists and the
> Greenhouse, Lever and Ashby job boards. Phase 3: a jobs dashboard on this computer. Phase 3b: scoring
> jobs against your resume, on this computer only. Applying comes in later phases.

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
public API: a board that lists jobs is read on every collection from then on (after the boards that already had jobs
for you), and its matching jobs are saved at once; one that does not exist is remembered and never asked again. A
board that exists but lists no jobs (often an old account a company no longer uses) is not read by collections: each
refresh round asks it again and adds it as soon as it lists a job, the same rule as Add company. The trade-off: a job
posted on such a board is found at the next round, so a shorter refresh interval means less delay. Each new
name is also tried once on the other sites; a board found that way is added only if it shares at least 3 job titles
with the first one, otherwise it waits for you on the dashboard's Boards page.

It is slow on purpose (Common Crawl asks for pauses between requests and is often overloaded): often an hour or more.
It says which part of the index it is reading. When the index is overloaded (two site parts in a row fail), it
leaves the rest for the next refresh and goes on to check the names it already has.
Collections never wait for it: when a collection starts (from the dashboard or a terminal), a running refresh pauses
at its next request and continues where it stopped once the collection has finished. It is safe to stop with Ctrl+C;
the next refresh continues where it stopped. Nothing is ever deleted.

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
  started from a terminal, except that a collection always goes first: a refresh pauses for it and continues
  afterwards. While the dashboard is open, it can collect every few hours and start a new phone-book round every few
  days (monthly unless you change it; see Settings), counted from when the last round began. A round that was paused
  or stopped continues at the next chance; one that a failing site left unfinished continues the next day.
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

## Scoring jobs
Scoring tells you which collected jobs fit your resume. **Nothing leaves this computer:** both stages run here, and
your resume is never logged or sent anywhere.
- **Quick check** (every open job, free, on the processor): it passes a job with one of your `roles` keywords in its
  title, or a job that shares skills with your resume (at least one, and at least `skills_threshold` of the skills
  it names) and is close enough in meaning (`meaning_threshold`). Skill names come from O*NET® software skills data,
  plus your own `extra_skills`; the meaning match uses the small bge-small model.
- **AI score** (jobs that passed, on your graphics card through [Ollama](https://ollama.com)): a score from 0 to 100
  with up to three short reasons. A score of `ready_threshold` (70) or more makes a job "ready for preparation"; a
  lower one puts it on the Low score tab. Newest jobs are scored first (posted in the last 7 days, then 8-30, then
  31-90 days ago), strongest quick-check match first. Older jobs are scored only with "Score these" on the
  dashboard.

**One-time setup**
1. Put your resume, as a LaTeX file, at `data/resume.tex` (git-ignored like the rest of `data/`; at most 1 MB).
2. Install Ollama in its own environment, start it, and download the AI model (about 28 GB). Leave the terminal
   running `ollama serve` open while scoring:
   ```bash
   conda create -n ollama -c conda-forge ollama=0.32.15
   ```
   ```bash
   conda run --no-capture-output -n ollama ollama serve
   ```
   ```bash
   conda run -n ollama ollama pull gemma4:26b-a4b-it-q8_0
   ```
   **Never change Ollama's `OLLAMA_HOST` setting.** Ollama has no password, so it must only listen on this computer
   (its default); the app refuses any other Ollama address.
3. Download the quick-check files once and check that Ollama and the model are ready:
   ```bash
   python -m app.score setup
   ```
   It downloads three public files (about 137 MB) from approved addresses into this computer's cache folder
   (`~/.cache/auto_apply` on Linux), checks each file's fingerprint, and never downloads a good file again.

**Scoring**
```bash
python -m app.score
```
Runs the quick check on every new or changed job, then the AI score on the jobs that passed, within
`minutes_per_day` of AI time a day (120 by default); the rest continue the next day. The first quick check of all
jobs takes about 1.5-2 hours, later ones a minute or so. Every job's result is saved as soon as it is done, so it is
safe to stop with Ctrl+C. It pauses while a collection runs. A new resume, new keywords or extra skills, or a changed
job ad make the affected results out of date, and they are checked again.

On the dashboard:
- **Collect:** "Score now" does the same as `python -m app.score`, with live progress. While the dashboard is open,
  it also scores by itself whenever there is something to score (`automatic`, on by default); after a failed run it
  waits 30 minutes before trying again. Only one scoring run happens at a time, including one started from a
  terminal.
- **Jobs:** each job's scoring status, score and reasons, filters by status and score, counts, and "Score these" for
  up to 500 jobs of the current list (any age; it does not use up the day's time). **Low score** lists the jobs the
  quick check or the AI turned down, with a "Prepare anyway" button.
- **A job's page:** the score, its reasons, the quick-check details, "Score this job", and the answer-key marks.
- **Answer key:** mark about 40 jobs yes, maybe or no on their pages. This page then shows how often each AI model
  and each quick-check threshold agrees with your marks, lets you score your marked jobs with another downloaded
  model to compare, and shows how many jobs each route through the quick check sends to the AI and how many of
  those the AI finds ready. Choose the model and the thresholds from these numbers.
- **Settings:** the scoring settings, the AI model, and whether Ollama is running with the model downloaded (or the
  exact command that fixes it).

"Score looks odd" marks a score worth checking by hand: for example a reason copied word for word from the ad, or an
ad with phrases aimed at AI screening tools ("ignore previous instructions"). The AI is always told that job ads are
data, never instructions.

## The council
A job's resume and cover letter are written by the Writer and then checked by a council of five reviewers: the Truth
checker (nothing made up), the Hiring manager, the Writing editor, the ATS screener and the Domain expert. You can add
up to three members of your own with private instructions; those instructions stay in `data/skills/`. In each round
the reviewers read the draft and may object, talk it over in a few discussion turns, and then the Writer answers every
objection. This repeats until everyone agrees or the round limit is reached (3 by default). When they don't agree, the
PDF keeps your original wording wherever the Truth checker objected. The job's page lets you switch to the Writer's
wording.

**Choosing the council** (Settings → Council). Pick a line-up, or set each member's model yourself:
- **Laptop:** free. Every member runs on your graphics card through Ollama. Download the second model once:
  ```bash
  conda run -n ollama ollama pull qwen3.6:35b-a3b
  ```
- **Low, Recommended, High:** paid members on Anthropic and OpenAI. Each member shows its estimated cost per job.
  Save the keys "anthropic" and "openai" under Settings → Writer → Keys. They are kept only in your keychain.
  Paid calls go at half price (Anthropic batches, OpenAI Flex). When a service is busy the app waits rather than
  paying full price, and nothing passes your daily and monthly limits.

**Preparing a job.** On a ready job's page, **Prepare** runs the Writer and the council. **Resume only** runs the
Writer alone, without reviewers. **Now, normal price** sends the rest of that one job at once, at the full price.
The job's page shows a status line while it works, then the council's notes, with the Truth checker first. A job
that closes, or that you mark not interested, stops; **Continue anyway** carries on from the saved steps.

**Running the council.** It runs while the dashboard is open, or from a terminal:
```bash
python -m app.prepare
```
Only one of them runs it at a time. Every step is saved as it finishes, so Ctrl+C loses nothing. With **automatic
preparation** on (Settings → Council), new ready jobs start by themselves only within your council hours
(22:00 to 08:00 by default), up to your daily job cap (20 by default), and only when their estimated cost fits the
money left under your limits. Laptop calls take turns with scoring on the graphics card.

**Pilot** (the Pilot page). It tries several line-ups on the same jobs (up to 20; it suggests the newest ready ones,
one per company), so you can compare them before choosing. Five of the jobs get a made-up claim slipped into the first
draft, to count how often each line-up's Truth checker catches it. Pilot versions never become PDFs and never show as
your versions. Mark each objection useful or not, and **Use this one** copies a result without a made-up claim into
its job.

**Usage page.** Laptop calls and cloud calls for every day, week, month and year, with tokens and dollars.

The first start after updating upgrades the database once. A copy of the old one is kept next to it first
(`data/app.db.bak-v<old version>`, still encrypted).

## Adding companies and board types
- **Another company** on Greenhouse, Lever or Ashby: add one line to `extra_boards` in `data/config.yaml`, using the
  name from the board's web address, e.g. `greenhouse:openai` for `job-boards.greenhouse.io/openai`. Boards linked
  from Simplify's lists, other approved job lists and Common Crawl are found automatically by the phone book. Or use
  **Add company** in the dashboard.
- **Another kind of job board** (for example Workable): one small translator file in `app/sources/` plus its tests.
  See [docs/adding-a-board-type.md](docs/adding-a-board-type.md) for the template and the test checklist.

## Privacy and security
- `data/` (settings, database, recovery file) and `exports/` are git-ignored.
- Gmail is optional and read-only: it never sends or changes mail, and an e-mail's text stays on this computer.
  To connect your own, see [docs/gmail-setup.md](docs/gmail-setup.md).
- Secrets live only in your OS keychain; the database is encrypted with SQLCipher.
- Collecting jobs sends nothing about you: only public read requests, identified as
  "auto_apply personal job search". Job descriptions are stored as plain text.
- Scoring sends nothing anywhere: the quick check and the AI (Ollama, on this computer only) both run here. Its
  only downloads are the three public quick-check files, once, checked by their fingerprints.
- The Laptop council sends nothing anywhere. A paid council sends the job ad and your resume and Profile text to
  Anthropic or OpenAI, with your name and contact details replaced by placeholders first; they are put back only
  on this computer, when the PDF is built.

## Credits
US and foreign place names (used to decide whether a job is in the US) come from
[GeoNames](https://www.geonames.org), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
They are stored in `app/places.json`; `tools/make_places.py` explains how to rebuild it.

The dashboard uses [HTMX](https://htmx.org) (BSD Zero-Clause licence), stored in `app/web/static/` so nothing is loaded
from the internet: version 2.0.11, file `dist/htmx.min.js` from the npm package `htmx.org@2.0.11`, SHA-256
`d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` (a test checks it).

The dashboard's fonts are [Source Sans 3](https://github.com/adobe-fonts/source-sans) (version 3.052) and
[Source Serif 4](https://github.com/adobe-fonts/source-serif) (version 4.005) by Adobe, under the SIL Open Font License
1.1, stored in `app/web/static/fonts/` with their licences. Its icons are 22 icons from [Lucide](https://lucide.dev)
version 1.57.0 (ISC licence; a few come from the Feather icons, MIT licence), stored in `app/web/static/icons.svg` with
`icons-LICENSE.txt`. Dragging a card on the Tracker uses [SortableJS](https://github.com/SortableJS/Sortable) version
1.15.7 (MIT licence), the file `Sortable.min.js` from the npm package `sortablejs@1.15.7`, stored as
`app/web/static/sortable.min.js` with `sortable-LICENSE.txt`, SHA-256
`bf4241bc73fef7f11c59a283a69fe8051cdd31c6d8ff5a2b9ba219e7831fcf76` (a test checks it).

Job scoring includes information from the O*NET 31.0 Database by the U.S. Department of Labor, Employment and
Training Administration (USDOL/ETA). Used under the [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
license. O*NET® is a trademark of USDOL/ETA. auto_apply has modified all or some of this information: it uses only
the software names in the
[Software Skills file](https://www.onetcenter.org/dl_files/database/db_31_0_csv/software_skills.csv), and leaves out
names of two characters or fewer (except names with + or #, such as C#) and names longer than six words. USDOL/ETA
has not approved, endorsed, or tested these modifications. The file is downloaded by `python -m app.score setup`, not
stored in this repository.

The meaning match uses [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) (MIT licence): its
ONNX model and tokenizer at revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`, also downloaded by setup and checked
by their SHA-256 fingerprints (listed in `app/scoring/files.py`).

## Development
- Run all tests: `python -m pytest`
- Format and lint: `ruff format .` and `ruff check .`
- After an approved upgrade (change `environment.yml`, rebuild the environment), refresh the Linux lock file:
  ```bash
  conda list --explicit --sha256 -n auto_apply > conda-linux-64.lock
  ```

## License
MIT. See [LICENSE](LICENSE).
