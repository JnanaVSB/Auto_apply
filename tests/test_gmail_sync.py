"""One Gmail sync pass against a stand-in Gmail built from made-up e-mails (no HTTP, no keychain)."""

from datetime import UTC, datetime

import pytest
from helpers import add_job, one
from test_tracker import version

from app import tracker
from app.gmail import sync
from app.gmail.client import GmailError, HistoryTooOld

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def email(message_id, sender, subject, snippet="", thread=None, received="2026-11-04T10:00:00+00:00"):
    return {"id": message_id, "thread_id": thread or f"t-{message_id}", "received_on": received, "from": sender,
            "subject": subject, "snippet": snippet}  # fmt: skip


class FakeGmail:
    """Messages in a mailbox with a position that grows with each new one, like Gmail's history."""

    def __init__(self, *messages):
        self.messages, self.added, self.position = {}, [], 100
        self.queries, self.arrives_during_search, self.too_old, self.broken = [], None, False, set()
        for message in messages:
            self.deliver(message)

    def deliver(self, message):
        self.position += 1
        self.messages[message["id"]] = message
        self.added.append((self.position, message["id"]))

    def profile(self):
        return "jordan@example.com", str(self.position)

    def search(self, query):
        self.queries.append(query)
        found = list(self.messages)
        if self.arrives_during_search:  # arrives after the search looked, before it answered
            self.deliver(self.arrives_during_search)
            self.arrives_during_search = None
        return found

    def history(self, start):
        if self.too_old:
            raise HistoryTooOld("too old")
        return [message_id for position, message_id in self.added if position > int(start)], str(self.position)

    def message(self, message_id):
        if message_id in self.broken:
            raise GmailError("Gmail is busy; the next sync tries again.")
        return self.messages[message_id]


@pytest.fixture
def acme(conn):
    """A Ready card for Acme (a finished version, nothing moved yet)."""
    with conn:
        job = add_job(conn, "ML Intern", company="Acme")
        version(conn, job)
    return job


def row(conn, message_id):
    return one(conn, "SELECT label, job_id, moved, to_check, subject, candidates FROM emails WHERE message_id = ?",
               message_id)  # fmt: skip


def test_the_first_sync_reads_back_sorts_matches_and_keeps_only_what_needs_you(conn, acme):
    gmail = FakeGmail(
        email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"),
        email("m2", "Initrode <jobs@initrode.example>", "Your application", "Unfortunately we will not proceed."),
        email("m3", "Shop <news@shop.example>", "Our autumn sale"),
    )
    result = sync.run(conn, gmail, 60, NOW)
    assert result == "Read 3 new e-mails: 1 card moved, 1 to check."
    assert gmail.queries[0].startswith("newer_than:60d {from:greenhouse.io ")
    assert tracker.stage(conn, acme) == "applied"
    assert row(conn, "m1") == ("confirmation", acme, 1, 0, None, "[]")
    assert row(conn, "m2") == ("rejection", None, 0, 1, "Your application", "[]")
    assert row(conn, "m3") == ("other", None, 0, 0, None, "[]")
    assert one(conn, "SELECT history_id, last_sync_on, last_result FROM gmail_sync") == ("103", NOW.isoformat(), result)


def test_the_next_sync_reads_only_new_e_mails_from_the_saved_position(conn, acme):
    gmail = FakeGmail(email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"))
    sync.run(conn, gmail, 60, NOW)
    gmail.deliver(email("m2", "Acme <no-reply@greenhouse.io>", "Acme: we would like to schedule an interview"))
    gmail.added.append((gmail.position, "m1"))  # Gmail reporting an old one again: skipped
    assert sync.run(conn, gmail, 60, NOW) == "Read 1 new e-mail: 1 card moved, 0 to check."
    assert len(gmail.queries) == 1 and tracker.stage(conn, acme) == "interview"
    assert one(conn, "SELECT history_id FROM gmail_sync")[0] == "102"


def test_a_reply_in_the_same_thread_is_about_the_same_job(conn, acme):
    gmail = FakeGmail(
        email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme", thread="t1"),
        email("m2", "Sam <sam@gmail.example>", "Re: next week", "We would like to invite you to interview.", "t1",
              "2026-11-04T11:00:00+00:00"),
    )  # fmt: skip
    sync.run(conn, gmail, 60, NOW)
    assert row(conn, "m2")[:3] == ("interview", acme, 1) and tracker.stage(conn, acme) == "interview"


def test_the_company_writing_with_no_phrase_is_unclear_for_the_local_model(conn, acme):
    tracker.move(conn, acme, "ready", "applied", NOW.date())
    sync.run(conn, FakeGmail(email("m1", "Acme Team <team@acme.example>", "A quick note")), 60, NOW)
    assert row(conn, "m1") == ("unclear", acme, 0, 0, None, "[]") and tracker.stage(conn, acme) == "applied"
    assert one(conn, "SELECT last_email_on FROM tracker WHERE job_id = ?", acme)[0] == "2026-11-04T10:00:00+00:00"


def test_two_jobs_the_e_mail_could_be_are_kept_to_check_with_both(conn, acme):
    with conn:
        other = add_job(conn, "Robotics Intern", company="Acme")
        version(conn, other)
    sync.run(
        conn, FakeGmail(email("m1", "Acme <jobs@acme.example>", "Your application", "Thanks for applying")), 60, NOW
    )
    assert row(conn, "m1") == ("confirmation", None, 0, 1, "Your application", f"[{acme}, {other}]")


def test_a_history_gmail_no_longer_has_searches_from_the_last_sync(conn, acme):
    gmail = FakeGmail()
    sync.run(conn, gmail, 60, NOW)
    gmail.too_old = True
    gmail.deliver(email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"))
    sync.run(conn, gmail, 60, datetime(2026, 11, 20, tzinfo=UTC))
    assert gmail.queries[1].startswith("after:2026/11/03 {") and tracker.stage(conn, acme) == "applied"
    assert one(conn, "SELECT history_id FROM gmail_sync")[0] == "101"


def test_an_e_mail_arriving_during_the_first_search_is_read_next_time(conn, acme):
    gmail = FakeGmail()
    gmail.arrives_during_search = email("late", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme")
    sync.run(conn, gmail, 60, NOW)
    sync.run(conn, gmail, 60, NOW)
    assert row(conn, "late")[0] == "confirmation" and tracker.stage(conn, acme) == "applied"


def test_a_gmail_error_half_way_keeps_what_was_done_and_the_old_position(conn, acme):
    gmail = FakeGmail(email("m1", "Shop <news@shop.example>", "Sale"), email("m2", "Shop <news@shop.example>", "Sale"))
    gmail.broken.add("m2")
    with pytest.raises(GmailError, match="busy"):
        sync.run(conn, gmail, 60, NOW)
    assert one(conn, "SELECT history_id, last_result FROM gmail_sync") == (
        None,
        "Gmail is busy; the next sync tries again.",
    )
    assert row(conn, "m1")[0] == "other" and row(conn, "m2") is None


def test_the_same_sync_twice_reads_nothing_again(conn, acme):
    gmail = FakeGmail(email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"))
    sync.run(conn, gmail, 60, NOW)
    with conn:
        conn.execute("UPDATE gmail_sync SET history_id = NULL")  # e.g. Disconnect and Connect again: a fresh search
    assert sync.run(conn, gmail, 60, NOW) == "Read 0 new e-mails: 0 cards moved, 0 to check."
    assert one(conn, "SELECT count(*) FROM emails")[0] == 1 and len(tracker.history(conn, acme)) == 1
