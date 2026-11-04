"""One Gmail sync pass against a stand-in Gmail built from made-up e-mails (no HTTP, no keychain)."""

from datetime import UTC, datetime

import pytest
from helpers import add_job, one
from test_tracker import version

from app import tracker
from app.config import AIRole
from app.gmail import model, sync
from app.gmail.client import GmailError, Gone, HistoryTooOld

NOW = datetime(2026, 11, 4, 18, 0, tzinfo=UTC)


def email(
    message_id, sender, subject, snippet="", thread=None, received="2026-11-04T10:00:00+00:00", labels=("INBOX",)
):
    return {"id": message_id, "thread_id": thread or f"t-{message_id}", "received_on": received, "from": sender,
            "subject": subject, "snippet": snippet, "labels": list(labels)}  # fmt: skip


class FakeGmail:
    """Messages in a mailbox with a position that grows with each new one, like Gmail's history."""

    def __init__(self, *messages):
        self.messages, self.added, self.position = {}, [], 100
        self.queries, self.arrives_during_search, self.too_old, self.broken, self.bodies = [], None, False, set(), []
        self.gone = set()
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

    def body(self, message_id):
        if message_id in self.gone:
            raise Gone("gone")
        self.bodies.append(message_id)
        return self.messages[message_id].get("body", "")

    def message(self, message_id):
        if message_id in self.gone:
            raise Gone("gone")
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
        email(
            "m2", "Initrode <jobs@initrode.example>", "Your application", "Unfortunately we will not be moving forward."
        ),
        email("m3", "Shop <news@shop.example>", "Our autumn sale"),
    )
    result = sync.run(conn, gmail, 60, NOW)
    assert result == "Read 3 new e-mails: 1 card moved, 1 to check."
    assert gmail.queries[0].startswith(
        "newer_than:60d -in:sent -in:drafts -in:spam -in:trash -in:chats {from:greenhouse.io "
    )
    assert tracker.stage(conn, acme) == "applied"
    assert row(conn, "m1") == ("confirmation", acme, 1, 0, None, "[]")
    assert row(conn, "m2") == ("rejection", None, 0, 1, "Your application", "[]")
    assert row(conn, "m3") is None  # a newsletter leaves nothing behind
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
    sync.run(
        conn, FakeGmail(email("m1", "Acme Team <team@acme.example>", "A quick note about your application")), 60, NOW
    )
    assert row(conn, "m1") == (
        "unclear",
        acme,
        0,
        1,
        "A quick note about your application",
        f"[{acme}]",
    )  # on the list until decided
    assert tracker.stage(conn, acme) == "applied"
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
    assert gmail.queries[1].startswith("after:2026/11/03 -in:sent") and tracker.stage(conn, acme) == "applied"
    assert one(conn, "SELECT history_id FROM gmail_sync")[0] == "101"


def test_an_e_mail_arriving_during_the_first_search_is_read_next_time(conn, acme):
    gmail = FakeGmail()
    gmail.arrives_during_search = email("late", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme")
    sync.run(conn, gmail, 60, NOW)
    sync.run(conn, gmail, 60, NOW)
    assert row(conn, "late")[0] == "confirmation" and tracker.stage(conn, acme) == "applied"


def test_a_gmail_error_half_way_keeps_what_was_done_and_the_old_position(conn, acme):
    gmail = FakeGmail(
        email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"),
        email("m2", "Acme <no-reply@greenhouse.io>", "Acme: schedule an interview"),
    )
    gmail.broken.add("m2")
    with pytest.raises(GmailError, match="busy"):
        sync.run(conn, gmail, 60, NOW)
    assert one(conn, "SELECT history_id, last_sync_on, last_result FROM gmail_sync") == (
        None, None, "Gmail is busy; the next sync tries again."
    )  # fmt: skip
    assert row(conn, "m1")[0] == "confirmation" and row(conn, "m2") is None


def test_the_same_sync_twice_reads_nothing_again(conn, acme):
    gmail = FakeGmail(email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"))
    sync.run(conn, gmail, 60, NOW)
    with conn:
        conn.execute("UPDATE gmail_sync SET history_id = NULL")  # e.g. Disconnect and Connect again: a fresh search
    assert sync.run(conn, gmail, 60, NOW) == "Read 0 new e-mails: 0 cards moved, 0 to check."
    assert one(conn, "SELECT count(*) FROM emails")[0] == 1 and len(tracker.history(conn, acme)) == 1


LOCAL = AIRole(model="gemma4:26b-a4b-it-q8_0")
CLOUD = AIRole(service="anthropic", model="claude-sonnet-5-5", key_name="anthropic")
PRIVATE_TEXT = "Dear Jordan, your made-up interview code is 4321"


@pytest.fixture
def answers(monkeypatch):
    """The local model's answer for each e-mail it is asked about (by subject); records each question."""
    found = {"asked": []}

    def fake(role, subject, snippet, body):
        found["asked"].append(subject)
        return found.get(subject)

    monkeypatch.setattr(model, "label", fake)
    return found


def unclear_mailbox():
    return FakeGmail(
        email("m1", "Acme Team <team@acme.example>", "A quick note about your application")
        | {"body": PRIVATE_TEXT},  # Acme's card
        email("m2", "Jobs <no-reply@greenhouse.io>", "An update") | {"body": PRIVATE_TEXT},  # no card named
    )


def test_the_local_model_sorts_an_unclear_e_mail_with_its_card_off_the_list(conn, acme, answers):
    answers["A quick note about your application"] = "interview"
    result = sync.run(conn, unclear_mailbox(), 60, NOW, LOCAL)
    assert row(conn, "m1") == ("interview", acme, 1, 0, None, "[]") and tracker.stage(conn, acme) == "interview"
    assert one(conn, "SELECT labelled_by FROM emails WHERE message_id = 'm1'")[0] == "local model"
    assert result.endswith(", 1 sorted by the local model.")


def test_other_takes_it_off_the_list_without_a_move(conn, acme, answers):
    answers["A quick note about your application"] = "other"
    sync.run(conn, unclear_mailbox(), 60, NOW, LOCAL)
    assert row(conn, "m1") == ("other", acme, 0, 0, None, "[]") and tracker.stage(conn, acme) is None


def test_an_e_mail_with_no_card_keeps_waiting_for_you_with_its_new_label(conn, acme, answers):
    answers["An update"] = "rejection"
    sync.run(conn, unclear_mailbox(), 60, NOW, LOCAL)
    assert row(conn, "m2") == ("rejection", None, 0, 1, "An update", "[]")
    assert row(conn, "m1")[0] == "unclear"  # no answer for it: it stays unclear


@pytest.mark.parametrize("role, scoring", [(LOCAL, True), (None, False), (CLOUD, False)])
def test_the_model_waits_while_scoring_runs_or_when_none_is_set(conn, acme, answers, role, scoring):
    answers["A quick note about your application"] = "interview"
    gmail = unclear_mailbox()
    sync.run(conn, gmail, 60, NOW, role, lambda: scoring)
    assert not answers["asked"] and not gmail.bodies and row(conn, "m1")[0] == "unclear"


def test_at_most_a_batch_of_e_mails_is_sorted_per_pass_newest_first(conn, acme, answers):
    gmail = FakeGmail(*(
        email(f"m{n}", "Jobs <no-reply@greenhouse.io>", f"Update {n}", received=f"2026-11-04T10:{n:02}:00+00:00")
        for n in range(25)
    ))  # fmt: skip
    sync.run(conn, gmail, 60, NOW, LOCAL)
    assert answers["asked"] == [f"Update {n}" for n in range(24, 4, -1)]


def test_an_e_mail_s_text_is_never_saved(conn, acme, answers):
    answers["A quick note about your application"] = "interview"
    gmail = unclear_mailbox()
    sync.run(conn, gmail, 60, NOW, LOCAL)
    assert gmail.bodies  # it was read for the model
    saved = " ".join(str(value) for found in conn.execute("SELECT * FROM emails") for value in found)
    assert "Jordan" not in saved and "4321" not in saved


def test_an_e_mail_gone_from_gmail_is_skipped_and_the_pass_carries_on(conn, acme):
    gmail = FakeGmail(
        email("draft", "Me <me@gmail.example>", "Re: interview"),
        email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"),
    )
    gmail.gone.add("draft")
    assert sync.run(conn, gmail, 60, NOW) == "Read 1 new e-mail: 1 card moved, 0 to check."
    assert row(conn, "draft") is None and one(conn, "SELECT history_id FROM gmail_sync")[0] == "102"


def test_failed_passes_never_move_the_restart_point(conn, acme):
    gmail = FakeGmail()
    sync.run(conn, gmail, 60, NOW)  # a good pass on 4 Nov
    gmail.broken.add("m1")
    gmail.deliver(email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme"))
    for day in (10, 15, 24):  # the sign-in or Gmail fails for weeks
        with pytest.raises(GmailError):
            sync.run(conn, gmail, 60, datetime(2026, 11, day, tzinfo=UTC))
    assert one(conn, "SELECT last_sync_on FROM gmail_sync")[0] == NOW.isoformat()
    gmail.broken.clear()
    gmail.too_old = True
    sync.run(conn, gmail, 60, datetime(2026, 11, 25, tzinfo=UTC))
    assert gmail.queries[-1].startswith("after:2026/11/03 ") and tracker.stage(conn, acme) == "applied"


@pytest.mark.parametrize("label", ["SENT", "DRAFT", "SPAM", "TRASH", "CHAT"])
def test_your_own_mail_drafts_spam_bin_and_chats_are_never_read(conn, acme, label):
    gmail = FakeGmail(email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme", thread="t1"))
    sync.run(conn, gmail, 60, NOW)
    gmail.deliver(email("mine", "Me <me@gmail.example>", "Re: Acme", "Unfortunately I can't make Tuesday.", "t1",
                        labels=("INBOX", label)))  # fmt: skip
    sync.run(conn, gmail, 60, NOW)
    assert row(conn, "mine") is None and tracker.stage(conn, acme) == "applied"


def test_a_company_s_ordinary_mail_leaves_nothing_behind_but_its_job_mail_is_looked_at(conn, acme):
    tracker.move(conn, acme, "ready", "applied", NOW.date())
    gmail = FakeGmail(
        email("shop", "Acme Store <orders@acme.example>", "Your package has shipped"),
        email("news", "Shop <news@shop.example>", "Our autumn sale"),
        email("job", "Acme Team <team@acme.example>", "About your interview"),
    )
    sync.run(conn, gmail, 60, NOW)
    assert row(conn, "shop") is None and row(conn, "news") is None
    assert row(conn, "job")[0] == "unclear"
    assert one(conn, "SELECT count(*) FROM emails")[0] == 1


def test_a_model_that_can_t_answer_stops_the_batch_after_one_try(conn, acme, monkeypatch):
    def off(*_):
        raise model.Unavailable

    monkeypatch.setattr(model, "label", off)
    gmail = FakeGmail(*(email(f"m{n}", "Jobs <no-reply@greenhouse.io>", f"Update {n}") for n in range(5)))
    sync.run(conn, gmail, 60, NOW, LOCAL)
    assert len(gmail.bodies) == 1 and one(conn, "SELECT count(*) FROM emails WHERE label = 'unclear'")[0] == 5


def test_scoring_is_checked_before_each_e_mail_the_model_reads(conn, acme, answers):
    gmail = FakeGmail(*(email(f"m{n}", "Jobs <no-reply@greenhouse.io>", f"Update {n}") for n in range(5)))
    checks = iter([False, False, True])  # a scoring run starts after the second e-mail
    sync.run(conn, gmail, 60, NOW, LOCAL, lambda: next(checks))
    assert len(answers["asked"]) == 2


def test_an_unclear_e_mail_gone_from_gmail_stays_on_the_list_without_failing(conn, acme, answers):
    gmail = unclear_mailbox()
    sync.run(conn, gmail, 60, NOW)
    gmail.gone.add("m1")
    answers["An update"] = "rejection"
    assert sync.run(conn, gmail, 60, NOW, LOCAL).endswith("1 sorted by the local model.")
    assert row(conn, "m1")[0] == "unclear"


def test_thank_you_for_applying_on_a_card_already_past_it_is_left_for_a_closer_look(conn, acme):
    tracker.move(conn, acme, "ready", "applied", NOW.date())
    sync.run(conn, FakeGmail(email("m1", "Acme <no-reply@greenhouse.io>", "Thank you for applying to Acme")), 60, NOW)
    assert row(conn, "m1")[:4] == ("unclear", acme, 0, 1) and tracker.stage(conn, acme) == "applied"


def test_a_shop_s_unfortunately_about_no_job_leaves_nothing(conn, acme):
    sync.run(
        conn,
        FakeGmail(email("m1", "Shop <news@shop.example>", "Delivery", "Unfortunately your parcel is late.")),
        60,
        NOW,
    )
    assert row(conn, "m1") is None
