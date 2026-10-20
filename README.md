# auto_apply

A personal job-application helper. Each person runs their own copy with their own data;
nothing personal is ever stored in this repository.

> **Status:** early development. Phase 1 (foundation): private settings, secrets in the OS keychain
> and an encrypted database. Phase 2: job collection from Simplify's public GitHub lists and the
> Greenhouse, Lever and Ashby job boards. Dashboard and applying come in later phases.

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
- Back up your `data/` folder regularly. It is encrypted, so a cloud or USB backup is fine.
- Lost keychain entry: `python -m app.db recover`
- New laptop: copy your backed-up `data/` folder into the project first, then run `python -m app.db recover`
- Change the master password: `python -m app.db change-password`
- Lost or damaged recovery.key, or forgot the master password while the app still opens:
  `python -m app.db new-recovery-key`

## Collect jobs
```bash
python -m app.collect
```
Reads Simplify's two public job lists on GitHub and every Greenhouse, Lever and Ashby board they link
to (plus your `extra_boards`), one request per second per site. It keeps only US jobs matching your
`roles`, stores each job once, and marks jobs closed when their board or list drops them. A full run
takes about 15–25 minutes; running it again is safe. Sources that fail are listed at the end and close
nothing. If your settings file is from before Phase 2, copy the `roles` and `sources` sections from
`config.example.yaml`.

To save one job you found yourself (on a company site, or by browsing simplify.jobs and clicking Apply):
```bash
python -m app.collect add "<job link>"
```
Greenhouse, Lever and Ashby links are read from the company's board; for other sites you type the
company and job title. simplify.jobs links themselves are refused: the app never opens that website.

## Privacy and security
- `data/` (settings, database, recovery file) and `exports/` are git-ignored.
- Secrets live only in your OS keychain; the database is encrypted with SQLCipher.
- Collecting jobs sends nothing about you: only public read requests, identified as
  "auto_apply personal job search". Job descriptions are stored as plain text.

## Development
- Run all tests: `python -m pytest`
- Format and lint: `ruff format .` and `ruff check .`
- After an approved upgrade (change `environment.yml`, rebuild the environment), refresh the Linux lock file:
  ```bash
  conda list --explicit --sha256 -n auto_apply > conda-linux-64.lock
  ```

## License
MIT. See [LICENSE](LICENSE).
