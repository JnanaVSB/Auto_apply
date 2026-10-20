# auto_apply

A personal job-application helper. Each person runs their own copy with their own data;
nothing personal is ever stored in this repository.

> **Status:** early development. Phase 1 (foundation) is done: private settings, secrets in the
> OS keychain and an encrypted database. Job features come in later phases.

## Requirements
- [Miniconda](https://docs.anaconda.com/miniconda/) (or any conda)
- An OS keychain: macOS Keychain, Windows Credential Manager, or on Linux GNOME Keyring / KWallet

## Setup
1. Create and activate the environment:
   ```bash
   conda env create -f environment.yml
   conda activate auto_apply
   ```
2. Create your private settings file (the `data/` folder is never committed):
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
- New laptop or lost keychain entry: `python -m app.db recover`
- Change the master password: `python -m app.db change-password`

## Privacy and security
- `data/` (settings, database, recovery file) and `exports/` are git-ignored.
- Secrets live only in your OS keychain; the database is encrypted with SQLCipher.

## Development
- Run all tests: `python -m pytest`
- Format and lint: `ruff format .` and `ruff check .`

## License
MIT. See [LICENSE](LICENSE).
