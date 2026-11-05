"""One job on a feed and elsewhere: the fourth duplicate check and the link upgrade. Made-up jobs only."""

from dataclasses import replace

import pytest

from app import jobs, tracker
from app.config import RoleSettings
from app.sources import Job

ROLES = jobs.Roles.from_settings(RoleSettings(keywords=["ML"]))
NOW, LATER = "2026-10-20T10:00:00+00:00", "2026-10-21T10:00:00+00:00"
FEED, BOARD, SIMPLIFY = "feed:remoteok", "greenhouse:acme", "simplify:Summer2027-Internships"
LISTING = "https://remoteok.com/remote-jobs/remote-ml-engineer-acme-1"
BOARD_LINK = "https://job-boards.greenhouse.io/acme/jobs/123"


def board_job(number=123, **changes):
    url = f"https://job-boards.greenhouse.io/acme/jobs/{number}"
    base = Job(source=BOARD, source_job_id=str(number), company="Acme", title="ML Engineer", url=url,
               locations=["Austin, TX"], description="From the board.")  # fmt: skip
    return replace(base, **changes)


def feed_job(number=1, **changes):
    link = f"https://remoteok.com/remote-jobs/remote-ml-engineer-acme-{number}"
    base = Job(source=FEED, source_job_id=link, company="Acme", title="ML Engineer", url=link, locations=[],
               remote="remote", description="From the feed.")  # fmt: skip
    return replace(base, **changes)


def simplify_job(**changes):
    base = Job(source=SIMPLIFY, source_job_id="s-1", company="Acme", title="ML Engineer", url=BOARD_LINK,
               locations=["SF"])  # fmt: skip
    return replace(base, **changes)


def manual_job(**changes):
    link = "https://acme.example/careers/1"
    base = Job(source="manual", source_job_id=link, company="Acme", title="ML Engineer", url=link, locations=[])
    return replace(base, **changes)


def save(conn, *found, source=None, when=NOW, chosen=False):
    return jobs.save(conn, source or found[0].source, list(found), ROLES, when, complete=False, chosen=chosen)


def stored(conn, columns="id, apply_url"):
    return conn.execute(f"SELECT {columns} FROM jobs ORDER BY id").fetchall()  # noqa: S608 (test helper)


def entries(conn, job_id=1):
    rows = conn.execute("SELECT source, source_job_id FROM job_sources WHERE job_id = ? ORDER BY rowid", (job_id,))
    return rows.fetchall()


def everything(conn):
    return (conn.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM job_sources ORDER BY rowid").fetchall())  # fmt: skip


@pytest.mark.parametrize("order", [(board_job, feed_job), (feed_job, board_job)])
def test_a_job_on_a_board_and_a_feed_is_one_job_with_the_boards_link_and_text(conn, order):
    for make in order:
        save(conn, make())
    assert stored(conn, "apply_url, description") == [(BOARD_LINK, "From the board.")]
    assert {source for source, _ in entries(conn)} == {BOARD, FEED}


def test_the_feeds_own_link_stays_on_its_entry_after_the_upgrade(conn):
    save(conn, feed_job())
    save(conn, board_job())
    assert (FEED, LISTING) in entries(conn)


def test_one_feeds_two_postings_stay_two_jobs(conn):
    save(conn, feed_job(1), feed_job(2), feed_job(3))
    assert len(stored(conn)) == 3


def test_a_feed_joins_the_oldest_of_a_boards_postings_and_they_stay_apart(conn):
    cities = ["Austin, TX", "Denver, CO", "Boston, MA"]
    postings = [board_job(number, locations=[city]) for number, city in enumerate(cities, 1)]
    save(conn, *postings)
    save(conn, feed_job())
    assert len(stored(conn)) == 3
    assert (FEED, LISTING) in entries(conn, 1)
    before = everything(conn)
    save(conn, *postings)
    save(conn, feed_job())
    assert everything(conn)[1] == before[1]


def test_a_second_posting_of_the_feed_takes_the_next_job_only_if_it_is_not_on_this_feed(conn):
    save(conn, board_job())
    save(conn, feed_job(1), feed_job(2))
    assert len(stored(conn)) == 2  # the board's job holds one listing; the other has nowhere to go


def test_a_boards_three_postings_and_a_feeds_one_the_other_way_round(conn):
    save(conn, feed_job())
    cities = ["Austin, TX", "Denver, CO", "Boston, MA"]
    save(conn, *[board_job(number, locations=[city]) for number, city in enumerate(cities, 1)])
    assert len(stored(conn)) == 3  # the first joined the feed's job; after that it is no longer feeds only


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (simplify_job(locations=["SF"], url="https://acme.example/1"), board_job()),  # also on Simplify
        (board_job(source="lever:acme", url="https://jobs.lever.co/acme/1", locations=["Reno, NV"]), board_job()),
        (manual_job(), board_job()),  # added by you
    ],
)
def test_a_board_job_joins_only_a_job_that_is_on_feeds_and_nothing_else(conn, first, second):
    save(conn, first)
    save(conn, second)
    assert len(stored(conn)) == 2


@pytest.mark.parametrize(
    "other",
    [
        board_job(title="ML Researcher"),
        board_job(company="Globex"),
        board_job(company="ACME, Inc."),  # the known limit: names must match once capitals and punctuation go
    ],
)
def test_another_company_or_title_never_merges(conn, other):
    save(conn, feed_job())
    save(conn, other)
    assert len(stored(conn)) == 2


@pytest.mark.parametrize(("company", "title"), [("—", "ML Engineer"), ("Acme", "!!!"), ("—", "!!!")])
def test_a_name_with_nothing_left_after_cleaning_merges_with_nothing(conn, company, title):
    save(conn, feed_job(company=company, title=title, description="ML"))
    save(conn, board_job(company=company, title=title, locations=["Austin, TX"], description="ML"))
    assert len(stored(conn)) == 2


def test_names_in_other_scripts_merge_when_identical(conn):
    save(conn, feed_job(company="株式会社ロボ", title="ML 技術者"))
    save(conn, board_job(company="株式会社ロボ", title="ML 技術者"))
    assert len(stored(conn)) == 1


@pytest.mark.parametrize("title", ["ML%", "ML_", "ML*", "ML[a-z]", "ML|x", "%", "M%"])
def test_odd_characters_cannot_widen_the_match(conn, title):
    save(conn, feed_job(title=title, description="ML"))
    save(conn, board_job())
    assert len(stored(conn)) == 2


@pytest.mark.parametrize("longer", ["ML Engineer II", "ML Engineers", "ML Engineer é", "ML Engineer 2"])
def test_the_range_stops_exactly_at_the_title_whichever_comes_first(conn, longer):
    save(conn, feed_job())
    save(conn, board_job(title=longer))
    assert len(stored(conn)) == 2
    save(conn, feed_job(2, title=longer, company="Globex"))
    save(conn, board_job(7, company="Globex", title=longer.replace("ML Engineer", "ML Engineer Lead")))
    assert len(stored(conn)) == 4


def test_a_feed_with_a_shorter_title_does_not_join_a_longer_one(conn):
    save(conn, board_job(title="ML Engineer II"))
    save(conn, feed_job())
    assert len(stored(conn)) == 2


def test_reading_the_same_sources_again_changes_nothing(conn):
    save(conn, feed_job())
    save(conn, board_job())
    before = everything(conn)
    save(conn, board_job(), when=LATER)
    save(conn, feed_job(), when=LATER)
    assert [row[:16] for row in everything(conn)[0]] == [row[:16] for row in before[0]]  # last_seen moves on
    assert everything(conn)[1] == before[1]


def test_an_unlisted_simplify_entry_joins_nothing_and_changes_no_link(conn):
    save(conn, feed_job())
    save(conn, simplify_job(listed=False))
    assert stored(conn) == [(1, LISTING)]
    assert entries(conn) == [(FEED, LISTING)]


def test_an_unlisted_entry_that_matches_by_places_changes_no_link(conn):
    save(conn, feed_job(locations=["SF"]))
    save(conn, simplify_job(listed=False))
    assert stored(conn) == [(1, LISTING)]
    assert len(entries(conn)) == 2  # today's third check still joins it


def test_a_simplify_entry_upgrades_the_link_so_the_board_can_find_the_job_later(conn):
    save(conn, feed_job())
    save(conn, simplify_job())
    assert stored(conn) == [(1, BOARD_LINK)]
    save(conn, board_job())
    assert len(stored(conn)) == 1


def test_a_simplify_jobs_link_never_replaces_the_feeds(conn):
    save(conn, feed_job())
    save(conn, simplify_job(url="https://simplify.jobs/p/123"))
    assert stored(conn) == [(1, LISTING)]
    assert len(entries(conn)) == 2


def test_a_link_that_is_already_another_jobs_finds_that_job(conn):
    save(conn, simplify_job(title="ML Older Title"))  # holds the board's link
    save(conn, feed_job())
    save(conn, board_job())
    assert stored(conn) == [(1, BOARD_LINK), (2, LISTING)]
    assert (BOARD, "123") in entries(conn, 1)


@pytest.mark.parametrize("places", [["Worldwide"], []])  # with no places it would match by the third check too
def test_add_job_gives_you_your_own_job_and_link_beside_a_feed_job(conn, places):
    save(conn, feed_job(locations=places))
    jobs.save(conn, "manual", [manual_job()], ROLES, NOW, complete=False, chosen=True)
    assert stored(conn) == [(1, LISTING), (2, "https://acme.example/careers/1")]


def test_add_job_still_joins_a_boards_job_with_the_same_places(conn):
    save(conn, board_job(locations=[]))
    jobs.save(conn, "manual", [manual_job()], ROLES, NOW, complete=False, chosen=True)
    assert stored(conn) == [(1, BOARD_LINK)]
    assert ("manual", "https://acme.example/careers/1") in entries(conn)


def test_a_board_link_added_by_hand_counts_as_the_board(conn):
    save(conn, feed_job())
    jobs.save(conn, BOARD, [board_job()], ROLES, NOW, complete=False, chosen=True)
    assert stored(conn) == [(1, BOARD_LINK)]


def closed(conn):
    conn.execute("UPDATE jobs SET status = 'no longer available', status_reason = 'removed from its board'")


def hidden(conn):
    conn.execute("UPDATE jobs SET not_interested = 1")


def on_the_tracker(stage):
    def mark(conn):
        interview_round, applied = "1" if stage == "interview" else None, None if stage == "ready" else "2026-10-17"
        conn.execute("INSERT INTO tracker VALUES (1, ?, ?, ?, 'then')", (stage, interview_round, applied))

    return mark


def submitted(conn):
    conn.execute("INSERT INTO submissions (job_id, submitted_on, saved_on, how, address, fields, files) "
                 "VALUES (1, '2026-10-17', 'then', 'by hand', 'x', '[]', '[]')")  # fmt: skip


def finished_resume(conn):
    conn.execute("INSERT INTO preparations (job_id, kind, version, sources, writer, status, draft, created_on) "
                 "VALUES (1, 'resume', 1, '[]', '{}', 'done', '{}', 'now')")  # fmt: skip


NOT_FREE = [closed, hidden, *map(on_the_tracker, tracker.STAGES), submitted, finished_resume]


@pytest.mark.parametrize("mark", NOT_FREE)
def test_a_feed_listing_never_joins_a_job_that_is_not_free(conn, mark):
    save(conn, board_job())
    with conn:
        mark(conn)
    before = conn.execute("SELECT apply_url, description FROM jobs").fetchall()
    save(conn, feed_job())
    assert stored(conn, "apply_url")[1:] == [(LISTING,)]  # a new job of its own, open and visible
    assert stored(conn, "status, not_interested")[1:] == [("open", 0)]
    assert conn.execute("SELECT apply_url, description FROM jobs WHERE id = 1").fetchall() == before
    assert entries(conn) == [(BOARD, "123")]


@pytest.mark.parametrize("mark", NOT_FREE)
def test_a_board_posting_never_joins_a_feed_job_that_is_not_free(conn, mark):
    save(conn, feed_job())
    with conn:
        mark(conn)
    save(conn, board_job())
    assert stored(conn, "apply_url")[1:] == [(BOARD_LINK,)]
    assert stored(conn, "status, not_interested")[1:] == [("open", 0)]
    assert entries(conn) == [(FEED, LISTING)]


@pytest.mark.parametrize("mark", NOT_FREE)
def test_a_feed_with_the_same_places_never_joins_a_job_that_is_not_free_either(conn, mark):
    save(conn, board_job())
    with conn:
        mark(conn)
    save(conn, feed_job(locations=["Austin, TX"]))
    assert stored(conn, "apply_url")[1:] == [(LISTING,)]
    assert entries(conn) == [(BOARD, "123")]


def test_a_feed_with_the_same_places_joins_an_open_free_job(conn):
    save(conn, board_job())
    save(conn, feed_job(locations=["Austin, TX"]))
    assert len(stored(conn)) == 1
    assert (FEED, LISTING) in entries(conn)


def test_a_simplify_list_keeps_todays_rule_beside_a_closed_job(conn):
    save(conn, board_job())
    with conn:
        closed(conn)
    save(conn, simplify_job(url="https://acme.example/1", locations=["Austin, TX"]))
    assert len(stored(conn)) == 1


def test_a_feed_joins_the_open_posting_not_an_older_closed_one(conn):
    save(conn, board_job(1), board_job(2, locations=["Denver, CO"]))
    with conn:
        conn.execute("UPDATE jobs SET status = 'no longer available', status_reason = 'removed from its board' "
                     "WHERE id = 1")  # fmt: skip
    save(conn, feed_job())
    assert (FEED, LISTING) in entries(conn, 2)
    assert (FEED, LISTING) not in entries(conn, 1)


def test_a_job_you_applied_to_keeps_its_link_ad_and_places_when_the_board_posts_it_elsewhere(conn):
    save(conn, feed_job(description="The ad I applied to.", locations=["Austin, TX"]))
    with conn:
        on_the_tracker("applied")(conn)
    before = conn.execute("SELECT apply_url, description, locations, us_location FROM jobs").fetchall()
    save(conn, board_job(locations=["Berlin, Germany"], description="ML in Berlin."), chosen=True)
    assert (
        conn.execute("SELECT apply_url, description, locations, us_location FROM jobs WHERE id = 1").fetchall()
        == before
    )
    assert stored(conn, "description")[1:] == [("ML in Berlin.",)]


def test_a_board_that_joins_also_gives_the_job_its_places_for_later_checks(conn):
    save(conn, feed_job())
    save(conn, board_job(locations=["Austin, TX"]))
    assert stored(conn, "match_key") == [(jobs.match_key(board_job()),)]
    save(conn, simplify_job(url="https://acme.example/careers/1", locations=["Austin, TX"]))
    assert len(stored(conn)) == 1


@pytest.mark.parametrize("places", [[], ["Worldwide"]])
def test_a_feed_never_joins_a_job_you_added(conn, places):
    jobs.save(conn, "manual", [manual_job()], ROLES, NOW, complete=False, chosen=True)
    save(conn, feed_job(locations=places))
    assert len(stored(conn)) == 2
    assert entries(conn) == [("manual", "https://acme.example/careers/1")]


@pytest.mark.parametrize(
    ("sql", "values"),
    [
        (jobs.FEED_JOINS, ("a", "b", FEED)),
        (jobs.JOINS_A_FEED, ("a", "b")),
        (jobs.SAME_PLACES, {"key": "a", "source": FEED}),
    ],
)
def test_the_fourth_check_uses_the_index_and_never_reads_the_whole_table(conn, sql, values):
    plan = " ".join(row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", values))
    assert "INDEX jobs_match_key" in plan and "SCAN" not in plan
