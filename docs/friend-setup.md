# Setting up auto_apply on your computer

One command sets up the app: it makes the app's own Python environment, your private settings file, the encrypted
database and the scoring files, then offers to start the dashboard. It asks before anything big, and running it again
is safe.

Everything stays on your computer. Your data lives in the project's `data/` folder, and your secrets in your computer's
keychain, never in a file.

## What you need
- **A computer:** Linux on an x86-64 processor, with a desktop; or a Mac with Apple silicon (M1 or newer) on macOS 11 or
  newer. macOS is untested so far. Intel Macs are not supported for now. On Windows this command doesn't run: use the
  by-hand steps in the [README](../README.md#setup).
- **`git` and `curl`**, which most systems already have. On a Mac, the first `git` may offer to install Apple's
  command line tools: accept, wait for it to finish, then run the command again.
- **At least 3 GB free in your home folder.**
- **A keychain.** A Mac has one. On Linux it is GNOME Keyring or KWallet, which a desktop usually has; a server with no
  desktop has none, and the app can't run there.

## 1. Set it up
Open a terminal and run:

```bash
git clone https://github.com/JnanaVSB/Auto_apply.git && cd Auto_apply && ./setup.sh
```

## 2. What it asks, in order
Every question is No unless you type `y` (or `yes`). Some only come up when they're needed.

1. **Install Miniforge?** Only if no conda is found on your computer, or the one found is older than 26.1 or doesn't
   run (it is left as it is). Miniforge is the free tool that holds the app's
   Python. It goes in `~/miniforge3`, changes no shell settings, and the download (about 130 MB, or 90 MB on a Mac) is
   checked against its fingerprint before anything runs.
2. **Rebuild the environment?** Only if an earlier `auto_apply` environment is broken or half-built. Your `data/`
   folder is not touched.
3. **Your master password, twice** (12 or more characters; a 4-5 word passphrase is easiest). You won't type it day to
   day: the database unlocks from your keychain. Keep it in a password manager. **If both the master password and the
   keychain entry are lost, your data can't be recovered.**
4. **Download the scoring files?** About 137 MB, once, only if they aren't there yet.
5. **Start the dashboard now?**

On a Mac, macOS may ask whether Python may use your keychain: choose **Always Allow**.

At the end, setup prints the exact command that opens the dashboard later. Keep it: it names the app's own Python, so
it works without any other step.

## 3. After setup
1. **The dashboard password.** The first time the dashboard starts, it asks you in the terminal to choose a dashboard
   password (12 or more characters, different from your master password).
2. **Your roles.** Choose the kinds of jobs you want on the dashboard's **Settings** page. Nobody needs to edit the
   settings file by hand.
3. **Your resume.** Put it, as a LaTeX file, at `data/resume.tex`.
4. **The AI score.** Either install Ollama and its model on this computer, as the README's
   [Scoring jobs](../README.md#scoring-jobs) section says, or choose a cloud service on **Settings → Scoring model**
   (see [docs/own-ai-address.md](own-ai-address.md)).
5. **The browser plugin**, which fills job forms for you to check and submit. Read
   [Read this before your first fill](submitting.md#read-this-before-your-first-fill) first, then follow
   [Install](submitting.md#install).

The README's commands start with `python`: put the Python path from setup's start command in its place. If setup
installed Miniforge, `conda` isn't on your PATH either (setup changes no shell settings), so where the README says
`conda`, type `~/miniforge3/bin/conda`.

## 4. Running it again
Running `./setup.sh` again is safe: nothing is overwritten, and whatever is done already is skipped. It asks "Start the
dashboard now?" again, and "Download the scoring files?" again if you said no last time.

Stopped midway, by Ctrl+C, a closed window or a lost connection? Run `./setup.sh` again: it continues where it can.

## 5. If setup stops
Each stop is one line saying why. Find yours here:

- **"…runs under Rosetta"**: open Terminal without Rosetta (in Finder, Get Info on Terminal and untick **Open using
  Rosetta**), then run `./setup.sh` again.
- **"Intel Macs are not supported for now"**: one of the app's packages has no Intel Mac build at the pinned version.
- **"This needs macOS 11 or newer"**: update macOS first, if your Mac can.
- **"Only Linux on x86-64 processors…"**, **"This Mac's processor…"** or **"This system… is not supported"**: this
  computer can't run the one-command setup.
- **"Setup needs at least 3 GB free in…"**: free some space in the folder it names, then run it again. **"Could not
  check the free space…"** means the home folder couldn't be read.
- **"Nothing installed…"**: you answered no to Miniforge. Install it yourself in `~/miniforge3` from the link it gives,
  or update your conda to 26.1 or newer, or run `./setup.sh` again and answer yes.
- **"…miniforge3 exists but has no conda in it"**: an earlier install was probably cut off. Setup never overwrites a
  folder: move or remove `~/miniforge3`, then run it again.
- **"The Miniforge in ~/miniforge3 is version …"** or **"…doesn't run"**: it is too old to build the environment, or an
  earlier install was cut off. Setup never deletes it: move or remove `~/miniforge3`, then run it again, and it
  installs a fresh one.
- **"curl is needed…"**: install curl, then run it again.
- **"Downloading Miniforge failed"**: check your internet connection, then run it again.
- **"The download's fingerprint is wrong; nothing was installed"**: the file that arrived isn't the one setup expects.
  Try again later. Never work around this check.
- **"Installing Miniforge failed"**: remove the folder `~/miniforge3`, then run it again.
- **"…can't load the app. Run ./setup.sh again and answer yes to rebuild it"**: you answered no to the rebuild.
- **"Removing the broken environment… failed"**: if `auto_apply` is the active environment, run `conda deactivate`
  first. Or remove it yourself with `conda env remove -n auto_apply` (see section 3 for which `conda`), then run setup
  again.
- **"Building the environment failed"**: check the free space and your internet connection, then run it again.
- **"…was built but can't load the app"** or **"Could not find the Python of environment auto_apply"**: run it again;
  if it stops the same way, ask whoever gave you this.
- **"No secure OS keychain available"** with **"This computer has no keychain the app can use"**: on a Linux desktop,
  install GNOME Keyring (or KWallet) and log in to the desktop. A server with no desktop has no keychain.
- **"The keychain is locked or refused"**: unlock it (log out and back in, or open **Passwords and Keys**), then run
  it again.
- **"macOS asked for permission and it was refused…"**: run it again and choose **Always Allow** when macOS asks.
- **"The project's data folder is a file"**: rename or move the file named `data` in the project folder, then run it
  again.
- **"A database key from an earlier setup is already in the keychain"**: the line says what to do. If your database
  is somewhere else, it names the setting to check.

## 6. If you already have Miniconda or Anaconda
Setup uses the conda you already have, as it is. If your `auto_apply` environment already works, setup keeps it,
whatever your conda's version. To build the environment it needs conda 26.1 or newer. If yours is older or doesn't
run, setup says so ("The conda at … setup needs 26.1 or newer, so it can install its own Miniforge"), leaves it as it
is, and offers its own Miniforge in `~/miniforge3`, which it then uses from that run on. Setup itself only names
conda-forge, the free community channel. If your conda still asks you to accept Anaconda's terms, that comes from
your conda's own settings: read them and decide, or ask whoever gave you this.

## 7. What setup never does
- It never changes your shell settings.
- It never overwrites your settings file or your database, and it never touches `data/` when it rebuilds the
  environment.
- It downloads only from conda-forge, from PyPI (the database library, checked by its fingerprint) and from Miniforge's
  GitHub release (checked by its fingerprint). If you say yes to the scoring files, those come from Hugging Face and
  O*NET, each checked by its fingerprint too.
- It never runs a download straight from the internet: Miniforge is saved first and runs only once its fingerprint
  matches.
