"""Encrypted database: daily unlock from the OS keychain, recovery with a master password.

Files in the data folder:
- app.db: the SQLCipher-encrypted database, opened with a random 256-bit key.
- recovery.key: a tiny SQLCipher file locked with the master password; it holds only that key.
"""

import argparse
import getpass
import re
import secrets
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import sqlcipher3

from app.config import ConfigError, load_config
from app.keychain import KeychainError, get_secret, set_secret

DB_FILE = "app.db"
RECOVERY_FILE = "recovery.key"
KEY_NAME = "db_key"
MIN_PASSWORD_LENGTH = 12
RECOVERY_KDF_ITER = 1_024_000  # 4x SQLCipher's default, so every master-password guess is slow
SQLITE_NOTADB = 26  # SQLCipher's "file is not a database": what a wrong key or password produces
KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
SIMPLE_SEQUENCES = ("0123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiop", "asdfghjkl", "zxcvbnm")

# UPGRADES[n - 1] turns a version n - 1 database into version n. Append new steps; never edit old ones.
# "REFERENCES" is documentation only: SQLite leaves foreign keys unenforced unless asked, which the table
# rebuilds in steps 3 to 5 rely on.
UPGRADES = (
    "",  # version 1: the empty Phase 1 database
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,  -- tracking parameters removed
        match_key TEXT NOT NULL,  -- company + title + location, for the same job under another link
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,  -- JSON list
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,  -- JSON list, e.g. ["Summer 2027"]
        description TEXT,  -- plain text; NULL until fetched
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0  -- the app must never open this link itself
    );
    CREATE INDEX jobs_match_key ON jobs (match_key);
    CREATE TABLE job_sources (
        source TEXT NOT NULL,  -- a Simplify list or a board, e.g. greenhouse:acme
        source_job_id TEXT NOT NULL,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        listed INTEGER NOT NULL,  -- 1 while the source still lists the job as open
        PRIMARY KEY (source, source_job_id)
    );
    CREATE INDEX job_sources_job ON job_sources (job_id);
    CREATE TABLE sources (
        name TEXT PRIMARY KEY,
        etag TEXT,  -- GitHub's version tag, so an unchanged list is not downloaded again
        last_ok TEXT
    );
    CREATE TABLE collect_runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,  -- done, cancelled or stopped
        new_jobs INTEGER NOT NULL DEFAULT 0,
        updated_jobs INTEGER NOT NULL DEFAULT 0,
        closed_jobs INTEGER NOT NULL DEFAULT 0,
        failures TEXT NOT NULL DEFAULT '[]'  -- JSON list of "source: reason"
    );
    """,
    """
    ALTER TABLE sources ADD COLUMN misses INTEGER NOT NULL DEFAULT 0;  -- runs in a row a board was not found
    ALTER TABLE sources ADD COLUMN retired_on TEXT;  -- the day a dead board stopped being read
    -- us_location may now also be 'no' (clearly not in the US). SQLite cannot change a CHECK rule in place,
    -- so the table is copied into one with the new rule, column for column.
    CREATE TABLE jobs_v3 (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,
        match_key TEXT NOT NULL,
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear', 'no')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,
        description TEXT,
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO jobs_v3 SELECT * FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_v3 RENAME TO jobs;
    CREATE INDEX jobs_match_key ON jobs (match_key);
    """,
    """
    -- A run never hides or closes a job. Status 'no longer listed' replaces 'closed' ('closed' stays allowed for
    -- a later phase); two new marks. Jobs labelled 'no' become 'unclear' with the check mark, so none stays hidden.
    -- collect_runs.closed_jobs now counts jobs that became 'no longer listed'.
    CREATE TABLE jobs_v4 (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,
        match_key TEXT NOT NULL,
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear', 'no')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,
        description TEXT,
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'no longer listed', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0,
        added_by_you INTEGER NOT NULL DEFAULT 0,  -- picked with `add`: later runs never change its US label
        check_location INTEGER NOT NULL DEFAULT 0  -- a later run judged it outside the US: please check by hand
    );
    INSERT INTO jobs_v4 SELECT *, id IN (SELECT job_id FROM job_sources WHERE source = 'manual'), us_location = 'no'
        FROM jobs;
    UPDATE jobs_v4 SET status = 'no longer listed' WHERE status = 'closed';
    UPDATE jobs_v4 SET us_location = 'unclear' WHERE us_location = 'no';
    DROP TABLE jobs;
    ALTER TABLE jobs_v4 RENAME TO jobs;
    CREATE INDEX jobs_match_key ON jobs (match_key);
    """,
    """
    -- Every job that is not open says why, and each source entry says since when and why it stopped listing the
    -- job. 'no longer listed' becomes 'no longer available'. What the saved data can't tell is 'reason not recorded
    -- (before 2026-10-21)'; the next successful read of each source fills in the real reason.
    ALTER TABLE sources ADD COLUMN empty_runs INTEGER NOT NULL DEFAULT 0;  -- runs in a row a board with jobs sent none
    ALTER TABLE job_sources ADD COLUMN unlisted_on TEXT;  -- the day the app noticed this source stopped listing it
    ALTER TABLE job_sources ADD COLUMN unlisted_reason TEXT;  -- why, in the words of jobs.status_reason
    UPDATE job_sources SET unlisted_reason = CASE
        WHEN source IN (SELECT name FROM sources WHERE retired_on IS NOT NULL) THEN 'board shut down'
        ELSE 'reason not recorded (before 2026-10-21)' END
        WHERE listed = 0;
    CREATE TABLE jobs_v5 (
        id INTEGER PRIMARY KEY,
        apply_url TEXT NOT NULL UNIQUE,
        match_key TEXT NOT NULL,
        company TEXT NOT NULL,
        title TEXT NOT NULL,
        locations TEXT NOT NULL,
        us_location TEXT NOT NULL CHECK (us_location IN ('yes', 'unclear', 'no')),
        remote TEXT NOT NULL CHECK (remote IN ('remote', 'hybrid', 'on-site', 'unknown')),
        level TEXT NOT NULL CHECK (level IN ('intern', 'new grad', 'entry', 'mid', 'senior', 'staff+', 'unknown')),
        role_category TEXT,
        sponsorship TEXT NOT NULL CHECK (sponsorship IN ('offers', 'no', 'citizens only', 'unknown')),
        terms TEXT NOT NULL,
        description TEXT,
        salary TEXT,
        date_posted TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('open', 'no longer available', 'closed')),
        open_manually_only INTEGER NOT NULL DEFAULT 0,
        added_by_you INTEGER NOT NULL DEFAULT 0,
        check_location INTEGER NOT NULL DEFAULT 0,
        status_reason TEXT CHECK (status_reason IN ('removed from its board', 'board shut down',
            'company stopped hiring', 'removed from Simplify', 'inactive on Simplify',
            'reason not recorded (before 2026-10-21)'))  -- why it is not open; NULL while open
    );
    -- Renamed while copied, since the new table refuses the old name. A retired board gives the reason only when
    -- no board that is still read has the job.
    INSERT INTO jobs_v5 SELECT id, apply_url, match_key, company, title, locations, us_location, remote, level,
        role_category, sponsorship, terms, description, salary, date_posted, first_seen, last_seen,
        CASE status WHEN 'no longer listed' THEN 'no longer available' ELSE status END,
        open_manually_only, added_by_you, check_location,
        CASE WHEN status != 'no longer listed' THEN NULL
            WHEN EXISTS (SELECT 1 FROM job_sources WHERE job_id = jobs.id AND unlisted_reason = 'board shut down')
                AND NOT EXISTS (SELECT 1 FROM job_sources s LEFT JOIN sources r ON r.name = s.source
                    WHERE s.job_id = jobs.id AND s.source NOT LIKE 'simplify:%' AND r.retired_on IS NULL)
            THEN 'board shut down' ELSE 'reason not recorded (before 2026-10-21)' END
        FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_v5 RENAME TO jobs;
    CREATE INDEX jobs_match_key ON jobs (match_key);
    """,
    """
    -- Phase 3 dashboard. "Not interested" only hides a job (never deletes it); a US label you set by hand is never
    -- changed by a run; the dashboard's own password is kept only as a slow hash, never as itself.
    ALTER TABLE jobs ADD COLUMN not_interested INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN location_by_you INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE dashboard_login (
        id INTEGER PRIMARY KEY CHECK (id = 1),  -- one dashboard password per database
        password_hash TEXT NOT NULL,
        set_on TEXT NOT NULL
    );
    -- Everything the jobs list filters, sorts or shows, so listing never reads the long descriptions.
    CREATE INDEX jobs_list ON jobs (not_interested, status, status_reason, us_location, check_location, level, remote,
        added_by_you, role_category, sponsorship, terms, date_posted, first_seen, company, title, locations);
    """,
    """
    -- A board you stopped is never read again (and no longer decides whether its jobs are open) until you say so;
    -- nothing is deleted.
    ALTER TABLE sources ADD COLUMN stopped_on TEXT;  -- the day you stopped it; NULL while it is read
    """,
    """
    -- Phase 3.5 phone book. Nothing is deleted. A board in `sources` is read on every collection; a name the phone
    -- book finds waits in `phonebook` until its site's own API has confirmed it.
    ALTER TABLE sources ADD COLUMN found_by TEXT;  -- who found it first: simplify, you, commoncrawl, list:<name> or
        -- cross-check; NULL = found before the phone book (by Simplify or by you)
    ALTER TABLE sources ADD COLUMN company TEXT;  -- the company name the board itself reports (only Greenhouse does)
    CREATE TABLE phonebook (
        board TEXT PRIMARY KEY,  -- e.g. lever:acme
        found_by TEXT NOT NULL,  -- commoncrawl, list:<name> or cross-check
        found_on TEXT NOT NULL,
        cross_of TEXT,  -- for a cross-check: the board whose name was tried on this site
        checked_on TEXT,  -- NULL until its site's API was asked
        result TEXT CHECK (result IN ('added', 'empty', 'not found', 'to confirm', 'declined')),
        company TEXT  -- the name the board reports, when it reports one
    );
    CREATE TABLE phonebook_done (
        part TEXT PRIMARY KEY,  -- a finished piece of a refresh, e.g. one index page; a rerun skips it
        done_on TEXT NOT NULL
    );
    CREATE TABLE phonebook_runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,  -- done, partial (a source failed), paused (for a collection), cancelled or stopped
        summary TEXT NOT NULL DEFAULT '',
        continues INTEGER NOT NULL DEFAULT 0  -- 1 when it continued an unfinished round instead of starting one
    );
    """,
    """
    -- Phase 3b scoring. Nothing is deleted: a job's results are only replaced by newer ones for the same job. Its
    -- status (ready, low score, ...) is worked out when shown, so a new threshold applies at once.
    CREATE TABLE stage_a (  -- the quick check on this computer, one row per job
        job_id INTEGER PRIMARY KEY REFERENCES jobs (id),
        setup TEXT NOT NULL,  -- fingerprint of your resume, keywords, skills list and meaning model at the time
        changed INTEGER NOT NULL DEFAULT 0,  -- 1 once the job's title or description changed (trigger below)
        keywords TEXT NOT NULL,  -- JSON list of your keywords found
        job_skills INTEGER NOT NULL,  -- skills from the list found in the ad
        shared_skills TEXT NOT NULL,  -- JSON list of those also in your resume
        meaning REAL NOT NULL,  -- meaning match, 0 to 1
        title_only INTEGER NOT NULL,  -- the job had no description, so only its title was checked
        trick TEXT,  -- a phrase like "ignore previous instructions" found in the ad; NULL when none
        passed INTEGER NOT NULL,
        checked_on TEXT NOT NULL
    );
    CREATE INDEX stage_a_queue ON stage_a (passed, changed);
    CREATE TABLE ai_scores (  -- the local AI's score, one row per job per model
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        model TEXT NOT NULL,
        score INTEGER CHECK (score BETWEEN 0 AND 100),  -- NULL when scoring failed
        reasons TEXT NOT NULL DEFAULT '[]',  -- JSON list of up to 3 short strings
        failed TEXT,  -- why the AI score failed, e.g. a bad answer twice; NULL when scored
        odd TEXT,  -- why the score looks odd; NULL when it doesn't
        resume TEXT NOT NULL,  -- fingerprint of the resume it read
        changed INTEGER NOT NULL DEFAULT 0,  -- 1 once the job's title or description changed
        scored_on TEXT NOT NULL,
        PRIMARY KEY (job_id, model),
        CHECK ((score IS NULL) != (failed IS NULL))  -- either a score or the reason it failed
    );
    CREATE INDEX ai_scores_model ON ai_scores (model, score);
    CREATE TABLE answer_key (  -- your marks, used to choose the thresholds and the model
        job_id INTEGER PRIMARY KEY REFERENCES jobs (id),
        label TEXT NOT NULL CHECK (label IN ('yes', 'maybe', 'no')),
        marked_on TEXT NOT NULL
    );
    CREATE TABLE scoring_days (
        day TEXT PRIMARY KEY,  -- the date on this computer's clock
        seconds REAL NOT NULL DEFAULT 0  -- automatic AI scoring time used that day ("Score these" does not count)
    );
    ALTER TABLE jobs ADD COLUMN prepare_anyway INTEGER NOT NULL DEFAULT 0;  -- you sent it on despite its score
    -- A changed title or description makes the job's results out of date, without touching the collection code.
    -- A later upgrade that rebuilds the jobs table must create this trigger again.
    CREATE TRIGGER job_text_changed AFTER UPDATE OF title, description ON jobs
        WHEN old.title IS NOT new.title OR old.description IS NOT new.description
    BEGIN
        UPDATE stage_a SET changed = 1 WHERE job_id = new.id;
        UPDATE ai_scores SET changed = 1 WHERE job_id = new.id;
    END;
    """,
    """
    -- A failed new try on a job that already has a score never replaces that score: it is noted here instead, and
    -- the older score and its reasons stay shown until a new score replaces both.
    CREATE TABLE ai_failures (  -- the latest failed new try, one row per job per model
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        model TEXT NOT NULL,
        failed TEXT NOT NULL,  -- why it failed
        resume TEXT NOT NULL,  -- fingerprint of the resume it read
        changed INTEGER NOT NULL DEFAULT 0,  -- 1 once the job's title or description changed
        tried_on TEXT NOT NULL,
        PRIMARY KEY (job_id, model)
    );
    -- The same trigger, now also for ai_failures. A later upgrade that rebuilds the jobs table must create it again.
    DROP TRIGGER job_text_changed;
    CREATE TRIGGER job_text_changed AFTER UPDATE OF title, description ON jobs
        WHEN old.title IS NOT new.title OR old.description IS NOT new.description
    BEGIN
        UPDATE stage_a SET changed = 1 WHERE job_id = new.id;
        UPDATE ai_scores SET changed = 1 WHERE job_id = new.id;
        UPDATE ai_failures SET changed = 1 WHERE job_id = new.id;
    END;
    """,
    """
    -- Phase 4a documents. Nothing is deleted: confirming tiles again adds a new set and keeps the older ones, so a
    -- resume prepared from older tiles can always be traced back to them.
    CREATE TABLE questionnaire (
        id INTEGER PRIMARY KEY CHECK (id = 1),  -- one set of your standard answers
        answers TEXT NOT NULL,  -- JSON, checked by the app before saving
        saved_on TEXT NOT NULL
    );
    CREATE TABLE tile_sets (  -- one row each time you press "Confirm tiles"
        id INTEGER PRIMARY KEY,
        resume TEXT NOT NULL,  -- fingerprint of the resume the tiles were read from
        confirmed_on TEXT NOT NULL
    );
    CREATE TABLE tiles (
        set_id INTEGER NOT NULL REFERENCES tile_sets (id),
        tile_id TEXT NOT NULL,  -- stable id from the kind and fixed facts, e.g. exp-3f2a
        position INTEGER NOT NULL,  -- order on your master resume
        kind TEXT NOT NULL CHECK (kind IN ('experience', 'project', 'education', 'publication', 'skills', 'award',
            'summary', 'other')),
        section TEXT NOT NULL,  -- the heading it appears under, e.g. Research Experience
        facts TEXT NOT NULL,  -- JSON object: the fixed facts the Writer can never change
        bullets TEXT NOT NULL DEFAULT '[]',  -- JSON list of the bullets' text
        PRIMARY KEY (set_id, tile_id),
        UNIQUE (set_id, position)
    );
    ALTER TABLE jobs ADD COLUMN prepare_requested_on TEXT;  -- when you pressed Prepare; NULL until you do
    """,
    """
    -- Uploaded resumes and CVs, encrypted like everything else. Nothing is deleted: Remove only stops a file being
    -- offered, so a resume prepared from it can always be traced back.
    CREATE TABLE resume_files (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,  -- the name you gave it, e.g. Robotics CV
        latex TEXT NOT NULL,  -- the .tex file as uploaded
        fingerprint TEXT NOT NULL,  -- of its plain text, worked out as for resume.tex
        uploaded_on TEXT NOT NULL,
        removed_on TEXT  -- when you pressed Remove; NULL while it is offered
    );
    ALTER TABLE tile_sets ADD COLUMN file_id INTEGER REFERENCES resume_files (id);  -- NULL: your Resume Profile
    CREATE TABLE writer_sources (  -- what the Writer works from, unless a job says otherwise
        id INTEGER PRIMARY KEY CHECK (id = 1),
        sources TEXT NOT NULL  -- JSON list of "profile" and uploaded file ids, at least one
    );
    ALTER TABLE jobs ADD COLUMN writer_sources TEXT;  -- this job's own choice, as above; NULL: the default
    """,
    """
    -- Phase 4b, the Writer. Nothing is overwritten: Request changes makes a new version, and every AI call is a saved
    -- step, so a call is never sent twice by itself. Spending per day and month is added up from the steps.
    CREATE TABLE preparations (  -- one row per version of a job's tailored resume or cover letter
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        kind TEXT NOT NULL CHECK (kind IN ('resume', 'cover_letter')),
        version INTEGER NOT NULL CHECK (version >= 1),  -- 1, 2, ... per job and kind
        based_on INTEGER REFERENCES preparations (id),  -- the version Request changes started from
        feedback TEXT,  -- your words from Request changes
        sources TEXT NOT NULL,  -- JSON: each source used with its confirmed tile set
        writer TEXT NOT NULL,  -- JSON: the Writer's settings at the start (never a key)
        status TEXT NOT NULL CHECK (status IN ('writing', 'done', 'by_hand', 'stopped')),
        problem TEXT,  -- why it stopped or needs checking by hand; never your text
        draft TEXT,  -- JSON: the checked answer with placeholders, and "details": their real text, kept only here
        flags TEXT NOT NULL DEFAULT '[]',  -- JSON: e.g. numbers not in the original bullets
        created_on TEXT NOT NULL,
        finished_on TEXT,
        UNIQUE (job_id, kind, version),
        CHECK (status != 'done' OR draft IS NOT NULL)
    );
    -- At most one version of a job's resume (or cover letter) is being written at a time
    CREATE UNIQUE INDEX preparations_writing ON preparations (job_id, kind) WHERE status = 'writing';
    CREATE TABLE steps (  -- one row per AI call a preparation needs
        id INTEGER PRIMARY KEY,
        preparation_id INTEGER NOT NULL REFERENCES preparations (id),
        round INTEGER NOT NULL DEFAULT 1 CHECK (round >= 1),  -- the council's rounds come in Phase 4c
        member TEXT NOT NULL DEFAULT 'writer',  -- and its other members
        service TEXT NOT NULL,
        model TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('sent', 'done', 'failed', 'interrupted')),
        tries INTEGER NOT NULL DEFAULT 1 CHECK (tries >= 1),
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,  -- thinking included
        cost REAL CHECK (cost >= 0),  -- dollars, from the tokens the service reported; NULL: not counted (no price)
        reserved REAL NOT NULL DEFAULT 0 CHECK (reserved >= 0),  -- the largest possible cost of the call waited for
        day TEXT NOT NULL,  -- the date on this computer's clock when it was sent
        answer TEXT,  -- the checked answer
        error TEXT,  -- a short reason; never your text or the answer
        sent_on TEXT NOT NULL,
        done_on TEXT,
        UNIQUE (preparation_id, round, member),
        CHECK (status = 'sent' OR reserved = 0)  -- once answered or interrupted, the reservation is cost or gone
    );
    CREATE INDEX steps_day ON steps (day);
    """,
    # 14: every failed try of a step, kept whole for troubleshooting (the Failed tries page)
    """
    CREATE TABLE failed_tries (
        id INTEGER PRIMARY KEY,
        step_id INTEGER NOT NULL REFERENCES steps (id),
        try_number INTEGER NOT NULL CHECK (try_number >= 1),  -- 1, or 2 for the retry
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost REAL CHECK (cost >= 0),  -- this try's dollars; NULL: not counted (no price)
        reason TEXT NOT NULL,  -- why it failed, whole; never your text
        reply TEXT,  -- what came back, if anything: details still as placeholders, at most 100,000 characters
        reply_length INTEGER,  -- set only when the reply was cut: how long it was
        saved_on TEXT NOT NULL
    );
    CREATE INDEX failed_tries_step ON failed_tries (step_id);
    """,
    # 15: Phase 4c, the council. preparations and steps are rebuilt (SQLite can't change a table's checks), every row
    # kept; versions from Part 4b were Writer-only and sent at the normal price.
    """
    CREATE TABLE preparations_v15 (  -- one row per version of a job's tailored resume or cover letter, or a pilot's
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        kind TEXT NOT NULL CHECK (kind IN ('resume', 'cover_letter', 'pilot')),
        version INTEGER NOT NULL CHECK (version >= 1),  -- 1, 2, ... per job and kind
        based_on INTEGER REFERENCES preparations (id),  -- the version Request changes started from
        feedback TEXT,  -- your words from Request changes
        asked TEXT NOT NULL DEFAULT '[]',  -- JSON: your words sent while the council worked, each with its round
        sources TEXT NOT NULL,  -- JSON: each source used with its confirmed tile set
        writer TEXT NOT NULL,  -- JSON: the Writer's settings at the start (never a key)
        council TEXT,  -- JSON: the reviewers' settings at the start (never a key); NULL: a Writer-only version (4b)
        status TEXT NOT NULL CHECK (status IN ('writing', 'done', 'by_hand', 'stopped')),
        round INTEGER NOT NULL DEFAULT 1 CHECK (round BETWEEN 1 AND 6),  -- at most 5, plus one for your words
        agreed INTEGER CHECK (agreed IN (0, 1)),  -- the council's result; NULL: not finished, or no council
        wording TEXT CHECK (wording IN ('original', 'writer')),  -- no agreement: the wording of objected entries
        mode TEXT NOT NULL DEFAULT 'half' CHECK (mode IN ('half', 'normal')),  -- normal: "Now, normal price"
        waived TEXT NOT NULL DEFAULT '[]',  -- JSON: the doubts you proceeded anyway with (skipped, closed, not-ready)
        automatic INTEGER NOT NULL DEFAULT 0 CHECK (automatic IN (0, 1)),  -- started by automatic preparation
        pilot_id INTEGER REFERENCES pilots (id),
        lineup TEXT,  -- the pilot line-up it was made with
        planted TEXT,  -- JSON: the pilot's made-up claim in its first draft; never in a PDF
        problem TEXT,  -- why it stopped, paused or needs checking by hand; never your text
        draft TEXT,  -- JSON: the checked answer with placeholders, and "details": their real text, kept only here
        flags TEXT NOT NULL DEFAULT '[]',  -- JSON: e.g. numbers not in the original bullets
        created_on TEXT NOT NULL,
        finished_on TEXT,
        UNIQUE (job_id, kind, version),
        CHECK (status != 'done' OR draft IS NOT NULL),
        CHECK ((kind = 'pilot') = (pilot_id IS NOT NULL) AND (kind = 'pilot') = (lineup IS NOT NULL))
    );
    INSERT INTO preparations_v15 (id, job_id, kind, version, based_on, feedback, sources, writer, status, mode, problem,
        draft, flags, created_on, finished_on)
        SELECT id, job_id, kind, version, based_on, feedback, sources, writer, status, 'normal', problem, draft, flags,
        created_on, finished_on FROM preparations;
    DROP TABLE preparations;
    ALTER TABLE preparations_v15 RENAME TO preparations;
    -- At most one version of a job's resume (or cover letter, or one pilot line-up's resume) is being written at a time
    CREATE UNIQUE INDEX preparations_writing ON preparations (job_id, kind, coalesce(lineup, ''))
        WHERE status = 'writing';
    CREATE TABLE steps_v15 (  -- one row per AI call a preparation needs
        id INTEGER PRIMARY KEY,
        preparation_id INTEGER NOT NULL REFERENCES preparations (id),
        round INTEGER NOT NULL DEFAULT 1 CHECK (round >= 1),
        member TEXT NOT NULL DEFAULT 'writer'
            CHECK (member IN ('writer', 'truth', 'hiring', 'editor', 'ats', 'domain', 'x1', 'x2', 'x3')),
        turn INTEGER NOT NULL DEFAULT 0 CHECK (turn BETWEEN 0 AND 3),  -- 0: the draft or a member's own review;
        -- 1 to 3: the council's discussion turns
        service TEXT NOT NULL,
        model TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT 'direct' CHECK (mode IN ('direct', 'batch', 'flex')),  -- batch, flex: half price
        status TEXT NOT NULL CHECK (status IN ('sent', 'waiting', 'done', 'failed', 'interrupted')),
        tries INTEGER NOT NULL DEFAULT 1 CHECK (tries >= 1),
        waits INTEGER NOT NULL DEFAULT 0 CHECK (waits >= 0),  -- times the service said "busy, try later": not a try
        retry_after TEXT,  -- waiting: not sent again before this time (NULL: with the next batch)
        batch_id TEXT,  -- the Anthropic batch it was sent in, saved the moment the batch is created
        input_tokens INTEGER NOT NULL DEFAULT 0,
        cached_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,  -- thinking included
        cost REAL CHECK (cost >= 0),  -- dollars, from the tokens the service reported; NULL: not counted (no price)
        reserved REAL NOT NULL DEFAULT 0 CHECK (reserved >= 0),  -- the largest possible cost of the call waited for
        day TEXT NOT NULL,  -- the date on this computer's clock when it was last sent
        answer TEXT,  -- the checked answer
        error TEXT,  -- a short reason; never your text or the answer
        sent_on TEXT NOT NULL,
        done_on TEXT,
        UNIQUE (preparation_id, round, member, turn),
        CHECK (member != 'writer' OR turn = 0),
        CHECK (status = 'sent' OR reserved = 0),  -- once answered or interrupted, the reservation is cost or gone
        CHECK (status = 'waiting' OR retry_after IS NULL),
        CHECK (mode = 'batch' OR batch_id IS NULL)
    );
    INSERT INTO steps_v15 (id, preparation_id, round, member, service, model, status, tries, input_tokens,
        cached_tokens, output_tokens, cost, reserved, day, answer, error, sent_on, done_on)
        SELECT id, preparation_id, round, member, service, model, status, tries, input_tokens, cached_tokens,
        output_tokens, cost, reserved, day, answer, error, sent_on, done_on FROM steps;
    DROP TABLE steps;
    ALTER TABLE steps_v15 RENAME TO steps;
    CREATE INDEX steps_day ON steps (day);
    CREATE TABLE batches (  -- one row per Anthropic batch (half price)
        id INTEGER PRIMARY KEY,
        batch_id TEXT NOT NULL UNIQUE,  -- Anthropic's id
        status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'saved', 'deleted')),  -- results saved here,
        -- then deleted at Anthropic
        adopted INTEGER NOT NULL DEFAULT 0 CHECK (adopted IN (0, 1)),  -- found again after a crash lost its id
        sent_on TEXT NOT NULL,
        saved_on TEXT,
        deleted_on TEXT
    );
    CREATE TABLE pilots (  -- the same jobs through several line-ups, to compare them
        id INTEGER PRIMARY KEY,
        jobs TEXT NOT NULL,  -- JSON: the jobs' ids
        planted TEXT NOT NULL,  -- JSON: the made-up claim slipped into the first draft of some of them, by job id
        rounds INTEGER NOT NULL CHECK (rounds BETWEEN 1 AND 5),
        turns INTEGER NOT NULL CHECK (turns BETWEEN 0 AND 3),
        created_on TEXT NOT NULL
    );
    CREATE TABLE objection_marks (  -- your mark on a council member's objection: useful or not
        step_id INTEGER NOT NULL REFERENCES steps (id),
        objection TEXT NOT NULL,  -- its number, e.g. TC-1
        useful INTEGER NOT NULL CHECK (useful IN (0, 1)),
        marked_on TEXT NOT NULL,
        PRIMARY KEY (step_id, objection)
    );
    """,
    # 16: Phase 5, the Tracker. A job's place on the board, and every move. A job with a finished resume version and
    # no row (or stage 'ready') is in Ready. Rows are never deleted: the history stays even if a card goes back to
    # Ready.
    """
    CREATE TABLE tracker (
        job_id INTEGER PRIMARY KEY REFERENCES jobs (id),
        stage TEXT NOT NULL CHECK (stage IN ('ready', 'applied', 'assessment', 'interview', 'offer', 'rejected')),
        round TEXT CHECK (round IN ('1', '2', '3', '4', '5', 'final')),  -- Interview only, else NULL
        applied_on TEXT,  -- the local date (YYYY-MM-DD) the day counter counts from; NULL in Ready
        changed_on TEXT NOT NULL,  -- the last move or round change (UTC, ISO), for greying out
        CHECK ((stage = 'interview') = (round IS NOT NULL)),
        CHECK ((stage = 'ready') = (applied_on IS NULL))
    );
    CREATE TABLE tracker_moves (  -- the history the job page shows
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs (id),
        from_stage TEXT NOT NULL
            CHECK (from_stage IN ('ready', 'applied', 'assessment', 'interview', 'offer', 'rejected')),
        to_stage TEXT NOT NULL CHECK (to_stage IN ('ready', 'applied', 'assessment', 'interview', 'offer', 'rejected')),
        round TEXT CHECK (round IN ('1', '2', '3', '4', '5', 'final')),  -- the round after the move, else NULL
        moved_by TEXT NOT NULL CHECK (moved_by IN ('you', 'gmail')),  -- Phase 6 writes 'gmail'
        moved_on TEXT NOT NULL
    );
    CREATE INDEX tracker_moves_job ON tracker_moves (job_id, id);
    """,
    # 17: Phase 6, Gmail sync. One row per e-mail looked at (never its body; its subject only while you check it), where
    # the next sync starts, and each card's latest matched e-mail for greying out.
    """
    CREATE TABLE emails (  -- one row per Gmail message ever looked at; never the body
        message_id TEXT PRIMARY KEY,  -- Gmail's id: read once
        thread_id TEXT NOT NULL,
        received_on TEXT NOT NULL,  -- UTC, ISO
        sender_domain TEXT NOT NULL,
        label TEXT NOT NULL CHECK (label IN ('confirmation', 'assessment', 'interview', 'offer', 'rejection', 'other',
            'unclear')),
        labelled_by TEXT NOT NULL CHECK (labelled_by IN ('rules', 'local model', 'you')),
        job_id INTEGER REFERENCES jobs (id),  -- the matched job, else NULL
        moved INTEGER NOT NULL DEFAULT 0 CHECK (moved IN (0, 1)),  -- it moved a card
        to_check INTEGER NOT NULL DEFAULT 0 CHECK (to_check IN (0, 1)),  -- on "E-mails to check"
        subject TEXT,  -- kept only while to_check = 1
        candidates TEXT NOT NULL DEFAULT '[]',  -- JSON: job ids it might be, while to_check = 1
        CHECK (to_check = 1 OR subject IS NULL)
    );
    CREATE INDEX emails_job ON emails (job_id, received_on);
    CREATE TABLE gmail_sync (  -- one row: where the next sync starts
        id INTEGER PRIMARY KEY CHECK (id = 1),
        history_id TEXT,  -- Gmail's position after the last sync; NULL before the first
        connected_on TEXT,  -- when you last connected (testing mode: Google asks again after 7 days)
        last_sync_on TEXT, last_result TEXT  -- plain words, never e-mail text or a token
    );
    ALTER TABLE tracker ADD COLUMN last_email_on TEXT;  -- the latest matched e-mail (UTC, ISO), for greying out
    """,
)
SCHEMA_VERSION = len(UPGRADES)


class DatabaseError(Exception):
    """The database can't be set up or unlocked. Messages never include keys or passwords."""


class _WrongKey(Exception):
    """The key or password does not open the file (unlike a locked or unreadable file)."""


def _raw_key(key_hex: str) -> str:
    # The key comes from the keychain; checking its shape stops anything else reaching the PRAGMA.
    if not KEY_PATTERN.fullmatch(key_hex):
        raise DatabaseError("The database key in the keychain is malformed. Run: python -m app.db recover")
    return f"\"x'{key_hex}'\""


def _passphrase(password: str) -> str:
    if "\0" in password:
        raise DatabaseError("The password contains an invalid (null) character.")
    return "'" + password.replace("'", "''") + "'"


def check_password(password: str, name: str = "master password") -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise DatabaseError(f"The {name} must be at least {MIN_PASSWORD_LENGTH} characters.")
    text = password.lower()
    runs = (run * (len(text) // len(run) + 2) for seq in SIMPLE_SEQUENCES for run in (seq, seq[::-1]))
    if len(set(text)) == 1 or any(text in run for run in runs):
        raise DatabaseError(
            f"The {name} is too easy to guess (one repeated character or a simple sequence). Try a 4-5 word passphrase."
        )


def _missing(data_dir: Path) -> DatabaseError:
    # Without app.db there is nothing to rescue, so every command points to the settings or a fresh setup.
    if not (data_dir / DB_FILE).is_file():
        return DatabaseError(
            "app.db not found in the data folder. "
            "Check data_dir in data/config.yaml; for a brand-new install run: python -m app.db setup"
        )
    return DatabaseError(
        "recovery.key not found in the data folder. "
        "If your database still opens, make a new one with: python -m app.db new-recovery-key"
    )


def _connect(path: Path, key: str, kdf_iter: int | None = None, *, create: bool = False) -> sqlcipher3.Connection:
    if not create and not path.is_file():  # connecting would otherwise create an empty file
        raise _missing(path.parent)
    conn = None
    try:
        conn = sqlcipher3.connect(path)
        conn.execute("PRAGMA cipher_log_level = NONE")  # a wrong key would otherwise print SQLCipher's internal log
        conn.execute(f"PRAGMA key = {key}")
        if kdf_iter:
            conn.execute(f"PRAGMA kdf_iter = {kdf_iter}")
        conn.execute("SELECT count(*) FROM sqlite_master")  # SQLCipher only checks the key on first read
        return conn
    except BaseException as error:
        if conn is not None:
            conn.close()
        if not isinstance(error, sqlcipher3.DatabaseError):
            raise
        if error.sqlite_errorcode == SQLITE_NOTADB:
            raise _WrongKey from None
        raise DatabaseError(f"{path.name} could not be read ({error}).") from None


def _open_recovery(data_dir: Path, master_password: str) -> tuple[sqlcipher3.Connection, str]:
    """Unlock recovery.key and return it, still open, with the database key it holds."""
    try:
        conn = _connect(data_dir / RECOVERY_FILE, _passphrase(master_password), RECOVERY_KDF_ITER)
    except _WrongKey:
        raise DatabaseError("Wrong master password.") from None
    try:
        row = conn.execute("SELECT db_key FROM recovery").fetchone()
    except sqlcipher3.DatabaseError:  # e.g. the table is missing, or the file is empty
        row = None
    key_hex = str(row[0]) if row else ""
    if not KEY_PATTERN.fullmatch(key_hex):
        conn.close()
        raise DatabaseError(
            "recovery.key is damaged; the keychain was not changed. "
            "If your database still opens, make a new one with: python -m app.db new-recovery-key"
        )
    return conn, key_hex


def _upgrade(conn: sqlcipher3.Connection, version: int) -> None:
    """Bring the database from `version` to SCHEMA_VERSION; each step is saved all-or-nothing."""
    for number in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(f"BEGIN; {UPGRADES[number - 1]}; PRAGMA user_version = {number}; COMMIT;")


def _backup(conn: sqlcipher3.Connection, data_dir: Path, version: int) -> None:
    """Copy app.db, still encrypted, to app.db.bak-v<version>; swapped in only once complete.

    SQLite makes the copy itself (VACUUM INTO), so it is consistent even if another program has the database open.
    """
    backup = data_dir / f"{DB_FILE}.bak-v{version}"
    partial = data_dir / f"{backup.name}.partial"
    try:
        partial.unlink(missing_ok=True)  # VACUUM INTO refuses to overwrite a leftover from an interrupted copy
        conn.execute("VACUUM INTO ?", (str(partial),))
        partial.chmod(0o600)
        partial.replace(backup)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _open_db(data_dir: Path, key_hex: str) -> sqlcipher3.Connection:
    conn = _connect(data_dir / DB_FILE, _raw_key(key_hex))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                "app.db was made by a newer version of auto_apply. Update this copy (git pull) and try again."
            )
        if version < 1:  # an emptied file opens with any key
            raise DatabaseError("app.db is empty or damaged. Restore the data folder from a backup.")
        if version < SCHEMA_VERSION:
            _backup(conn, data_dir, version)
            _upgrade(conn, version)
        _use_wal(conn)  # only after the checks: a refused database is left untouched
    except BaseException:
        conn.close()
        raise
    return conn


def _use_wal(conn: sqlcipher3.Connection) -> None:
    """WAL: readers never wait for a writer, so the dashboard keeps working while a collection saves.

    Kept in the file; while it is open SQLite adds app.db-wal and app.db-shm beside it (encrypted too).
    """
    conn.execute("PRAGMA journal_mode = WAL")


def _refuse_if_set_up(data_dir: Path, replace_key: bool) -> None:
    if (data_dir / DB_FILE).exists():
        raise DatabaseError(
            "A database already exists in the data folder; setup never overwrites it. "
            "To restore the keychain key run: python -m app.db recover. "
            "To change the master password run: python -m app.db change-password."
        )
    if not replace_key and get_secret(KEY_NAME) is not None:  # one key per user, so a new setup would replace it
        raise DatabaseError(
            "A database key from an earlier setup is already in the keychain; setup won't replace it. "
            "If your database is in another folder, check data_dir in data/config.yaml. "
            "To start fresh on purpose, run: python -m app.db setup --replace-key"
        )


def _write_recovery(path: Path, master_password: str, key_hex: str) -> None:
    with closing(_connect(path, _passphrase(master_password), RECOVERY_KDF_ITER, create=True)) as conn:
        conn.execute("CREATE TABLE recovery (db_key TEXT NOT NULL)")
        conn.execute("INSERT INTO recovery VALUES (?)", (key_hex,))
        conn.commit()
    path.chmod(0o600)


def setup(data_dir: Path, master_password: str, *, replace_key: bool = False) -> None:
    check_password(master_password)
    _refuse_if_set_up(data_dir, replace_key)
    db_path, recovery_path = data_dir / DB_FILE, data_dir / RECOVERY_FILE
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir.chmod(0o700)  # also when the folder already existed (e.g. created for config.yaml)
    if recovery_path.exists():  # left by an interrupted setup; set aside rather than deleted
        recovery_path.replace(data_dir / f"old-{RECOVERY_FILE}")
    key_hex = secrets.token_hex(32)
    try:
        _write_recovery(recovery_path, master_password, key_hex)
        with closing(_connect(db_path, _raw_key(key_hex), create=True)) as conn:
            _upgrade(conn, 0)
            _use_wal(conn)
        db_path.chmod(0o600)
        set_secret(KEY_NAME, key_hex)  # last, so a failed setup leaves nothing in the keychain
    except BaseException:
        db_path.unlink(missing_ok=True)  # no data yet, so removing a half-finished setup is safe
        recovery_path.unlink(missing_ok=True)
        raise


def connect(data_dir: Path) -> sqlcipher3.Connection:
    if not (data_dir / DB_FILE).is_file():  # checked before the keychain, so a fresh install isn't sent to recover
        raise _missing(data_dir)
    key_hex = get_secret(KEY_NAME)
    if key_hex is None:
        raise DatabaseError("Database key not found in the keychain. Run: python -m app.db recover")
    try:
        return _open_db(data_dir, key_hex)
    except _WrongKey:
        raise DatabaseError(
            "app.db could not be opened with the keychain key: the key is wrong or the file is damaged. "
            "If the key is wrong, run: python -m app.db recover. "
            "If the file is damaged, restore the data folder from a backup."
        ) from None


def recover(data_dir: Path, master_password: str) -> None:
    conn, key_hex = _open_recovery(data_dir, master_password)
    conn.close()
    try:
        _open_db(data_dir, key_hex).close()
    except _WrongKey:
        raise DatabaseError(
            "The key in recovery.key does not open app.db: they come from different setups, or app.db is damaged. "
            "The keychain was not changed."
        ) from None
    set_secret(KEY_NAME, key_hex)


def change_master_password(data_dir: Path, old_password: str, new_password: str) -> None:
    check_password(new_password)
    conn, _ = _open_recovery(data_dir, old_password)
    with closing(conn):
        conn.execute(f"PRAGMA rekey = {_passphrase(new_password)}")


def new_recovery_key(data_dir: Path, master_password: str) -> None:
    check_password(master_password)
    connect(data_dir).close()  # proves the keychain key opens app.db before it is copied
    key_hex = get_secret(KEY_NAME)
    new_path = data_dir / f"new-{RECOVERY_FILE}"
    new_path.unlink(missing_ok=True)  # leftover from an interrupted earlier run
    try:
        _write_recovery(new_path, master_password, key_hex)
        new_path.replace(data_dir / RECOVERY_FILE)  # swapped in only once complete
    except BaseException:
        new_path.unlink(missing_ok=True)
        raise


def ask_new_password(
    prompt: str = (
        f"New master password ({MIN_PASSWORD_LENGTH}+ characters; a 4-5 word passphrase is easiest to remember): "
    ),
    name: str = "master password",
) -> str:
    """Ask for a new password twice; a weak one is refused before it is typed again."""
    password = getpass.getpass(prompt)
    check_password(password, name)
    if password != getpass.getpass("Re-enter the password: "):
        raise DatabaseError("The two passwords do not match.")
    return password


def error_line(error: BaseException) -> str:
    """A terminal command's one-line message for an error: never a traceback, never a full path."""
    if isinstance(error, sqlcipher3.Error | sqlite3.Error):  # anything unexpected from the database library
        return f"Error: the database could not be read or written ({error})."
    if isinstance(error, OSError):  # only the file name, since full paths include the user's name
        where = f": {Path(error.filename).name}" if error.filename else ""
        return f"Error: {error.strerror or error}{where}"
    if isinstance(error, EOFError | KeyboardInterrupt):
        return "\nCancelled."
    return f"Error: {error}"  # our own errors, whose messages are written for the user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db", description="Set up or recover the encrypted database.")
    parser.add_argument("command", choices=["setup", "recover", "change-password", "new-recovery-key"])
    parser.add_argument("--replace-key", action="store_true", help="setup only: replace a key from an earlier setup")
    args = parser.parse_args(argv)
    command = args.command
    if args.replace_key and command != "setup":
        parser.error("--replace-key only works with setup")
    try:
        data_dir = load_config().data_dir
        if command == "setup":
            _refuse_if_set_up(data_dir, args.replace_key)  # before asking for a password that could not be used
            setup(data_dir, ask_new_password(), replace_key=args.replace_key)
            print(
                "Database created. Store your master password somewhere safe (e.g. a password manager): "
                "if both it and the keychain entry are lost, the data cannot be recovered."
            )
        elif command == "recover":
            recover(data_dir, getpass.getpass("Master password: "))
            print("Database key restored to the keychain.")
        elif command == "new-recovery-key":
            connect(data_dir).close()  # check the keychain key before asking for a password
            new_recovery_key(data_dir, ask_new_password())
            print("New recovery.key written. Store the new master password safely; any old one no longer works.")
        elif command == "change-password":
            old_password = getpass.getpass("Current master password: ")
            _open_recovery(data_dir, old_password)[0].close()  # check it before asking for the new one
            change_master_password(data_dir, old_password, ask_new_password())
            print("Master password changed.")
    except (
        ConfigError,
        KeychainError,
        DatabaseError,
        sqlcipher3.DatabaseError,
        OSError,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        print(error_line(error), file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
