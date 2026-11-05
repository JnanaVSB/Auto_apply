"""The Settings page, on a temporary settings file (never the real data/config.yaml)."""

import stat
from contextlib import closing

import httpx
import pytest
import yaml
from helpers import LIST, FakeWeb, csrf, log_in

from app import config, db, fetch, keychain, phonebook
from app.config import ExportSettings, load_config
from app.sources import usajobs
from app.web import actions

ORIGINAL = """# my own comment
data_dir: {data_dir}
roles:
  simplify_categories: ["AI/ML/Data"]
  keywords: ["ML", "deep learning"]
sources:
  simplify_lists:
    - url: {list}
      level: intern
  extra_boards: ["greenhouse:acme"]
"""


@pytest.fixture
def settings_file(tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(ORIGINAL.format(data_dir=data_dir, list=LIST), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)  # the whole app reads this file
    return path


@pytest.fixture
def client(client, settings_file):
    log_in(client)
    return client


TICK = '<input type="checkbox" name="hacker_news" value="on"'
UNTICKED = '<input type="hidden" name="hacker_news" value="off">'  # an unticked box sends nothing by itself
TICKED = ["off", "on"]  # what a browser sends for the ticked box: the last value counts


def test_saving_settings_keeps_the_phone_address_and_never_sets_it(client, settings_file):
    settings_file.write_text(
        f"{settings_file.read_text(encoding='utf-8')}phone_address: https://laptop.tail0000.ts.net\n", encoding="utf-8"
    )
    assert client.post("/settings", data=form(phone_address="") | {"csrf_token": csrf(client)}).status_code == 200
    assert load_config().phone_address == "https://laptop.tail0000.ts.net"


def test_saving_settings_adds_no_phone_address(client, settings_file):
    assert client.post("/settings", data=form() | {"csrf_token": csrf(client)}).status_code == 200
    assert "phone_address" not in settings_file.read_text(encoding="utf-8")


def form(**changes):
    values = {
        "simplify_categories": "AI/ML/Data", "keywords": "ML\ndeep learning", "extra_boards": "greenhouse:acme",
        "collect_every_hours": "0", "phonebook_every_days": "30", "tracker_stale_days": "21",
        "gmail_lookback_days": "60", "gmail_every_minutes": "30",
        "requests_per_second": "1",
        "list_url_0": LIST, "list_level_0": "intern", "list_url_1": "",
        "list_level_1": "intern", "phonebook_lists": "",
    }  # fmt: skip
    return values | changes


def save(client, **changes):
    return client.post("/settings", data=form(**changes) | {"csrf_token": csrf(client)})


def test_the_page_shows_the_current_settings(client, data_dir):
    text = client.get("/settings").text
    assert ">ML\ndeep learning</textarea>" in text and ">greenhouse:acme</textarea>" in text
    assert f'name="list_url_0" type="url" maxlength="500" value="{LIST}"' in text
    assert 'name="list_url_1" type="url" maxlength="500" value=""' in text  # one empty row to add a list
    assert f"<code>{data_dir}</code>" in text


def test_a_valid_save_rewrites_the_file_all_at_once(client, settings_file, data_dir):
    text = save(client, keywords="ML\n\n  reinforcement learning \nML", collect_every_hours="6",
                extra_boards="greenhouse:acme\nashby:anthropic").text  # fmt: skip
    assert "Saved. Used from the next collection or action." in text
    config = load_config(settings_file)
    assert config.roles.keywords == ["ML", "reinforcement learning"]  # spaces, blank lines and repeats cleaned
    assert (config.collect_every_hours, config.sources.extra_boards) == (6, ["greenhouse:acme", "ashby:anthropic"])
    assert config.data_dir == data_dir
    written = settings_file.read_text(encoding="utf-8")
    assert written.startswith(actions.SETTINGS_HEADER) and "my own comment" not in written
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600
    assert not settings_file.with_name("config.yaml.new").exists()


def test_saving_the_form_keeps_the_scoring_and_ai_settings_it_does_not_show(client, settings_file):
    with settings_file.open("a", encoding="utf-8") as file:
        file.write("scoring:\n  meaning_threshold: 0.7\n  extra_skills: [MuJoCo]\n")
        file.write("ai:\n  evaluator:\n    model: qwen3.6:35b-a3b\n")
        file.write("documents:\n  one_page: false\n")
        file.write("spending:\n  daily: 5\n  monthly: 80\n")
        file.write("council:\n  rounds: 4\n  daily_jobs: 7\n")
    assert "Saved." in save(client, collect_every_hours="6").text
    config = load_config(settings_file)
    assert (config.scoring.meaning_threshold, config.scoring.extra_skills) == (0.7, ["MuJoCo"])
    assert config.ai.evaluator.model == "qwen3.6:35b-a3b" and config.documents.one_page is False
    assert (config.spending.daily, config.spending.monthly) == (5, 80)
    assert (config.council.rounds, config.council.daily_jobs) == (4, 7)


@pytest.mark.parametrize(
    "folder",
    [
        '/home/sam/my exports #1: "old" 导出\n',  # a space, #, a colon, quotes, Chinese and a last line break
        "C:\\Users\\Sam\\Export # 1",
        "~/exports",
        "- not a list",
        "'quoted'",
        "null",
        "2026-11-05",
        "0o7",
        " spaces round it ",
        "/line\u2028break\n\n",
    ],
)
def test_saving_the_form_keeps_the_export_settings_exactly(client, settings_file, folder):
    with settings_file.open("a", encoding="utf-8") as file:
        file.write(yaml.safe_dump({"export": {"folder": folder, "every_days": 7}}, allow_unicode=True))
    for _ in range(2):  # the second save reads what the first one wrote
        assert "Saved." in save(client, collect_every_hours="6").text
    assert load_config(settings_file).export == ExportSettings(folder=folder, every_days=7)


def test_saving_the_form_keeps_the_export_settings_and_the_phone_address_together(client, settings_file):
    with settings_file.open("a", encoding="utf-8") as file:
        file.write(yaml.safe_dump({"export": {"folder": "/srv/exports", "every_days": 7},
                                   "phone_address": "https://laptop.example.ts.net"}))  # fmt: skip
    assert "Saved." in save(client, collect_every_hours="6").text
    config = load_config(settings_file)
    assert config.export == ExportSettings(folder="/srv/exports", every_days=7)
    assert config.phone_address == "https://laptop.example.ts.net"


@pytest.mark.parametrize("section", ["", "export:\n", "export: {}\n"])
def test_saving_the_form_adds_no_empty_export_section(client, settings_file, section):
    with settings_file.open("a", encoding="utf-8") as file:
        file.write(section)
    assert "Saved." in save(client, collect_every_hours="6").text
    assert "export" not in yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    assert load_config(settings_file).export == ExportSettings()


@pytest.mark.parametrize(
    ("changes", "problem", "shown"),
    [
        ({"list_url_0": "https://example.com/list.json"}, "sources.simplify_lists.0.url",
         'value="https://example.com/list.json"'),
        ({"list_level_0": "senior"}, "sources.simplify_lists.0.level", "Not saved"),  # the list offers no "senior"
        ({"extra_boards": "acme"}, "sources.extra_boards", ">acme</textarea>"),
        ({"collect_every_hours": "200"}, "collect_every_hours", 'value="200"'),
        ({"collect_every_hours": "soon"}, "collect_every_hours", 'value="soon"'),
        ({"collect_every_hours": "²"}, "collect_every_hours", 'value="²"'),
        ({"phonebook_every_days": "366"}, "phonebook_every_days", 'value="366"'),
        ({"requests_per_second": "0"}, "requests_per_second", 'value="0"'),
        ({"requests_per_second": "6"}, "requests_per_second", 'value="6"'),
    ],
)  # fmt: skip
def test_a_refused_save_changes_nothing_and_keeps_what_was_typed(client, settings_file, changes, problem, shown):
    before = settings_file.read_bytes()
    text = save(client, **changes).text
    assert "Not saved: Invalid settings in data/config.yaml" in text and problem in text and shown in text
    assert settings_file.read_bytes() == before
    assert not settings_file.with_name("config.yaml.new").exists()


def test_the_phone_book_schedule_and_requests_per_second_are_saved_and_shown(client, settings_file):
    assert (
        'name="phonebook_every_days" type="number" min="0" max="365" step="1" value="30"'
        in client.get("/settings").text
    )  # not in the file yet: the default, monthly
    assert 'name="requests_per_second" type="number" min="1" max="5" step="1" value="1"' in client.get("/settings").text
    save(client, phonebook_every_days="7", requests_per_second="2", tracker_stale_days="30")
    config = load_config(settings_file)
    assert (config.phonebook_every_days, config.requests_per_second, config.tracker_stale_days) == (7, 2, 30)
    assert 'name="requests_per_second" type="number" min="1" max="5" step="1" value="2"' in client.get("/settings").text


def test_reading_all_e_mails_is_saved_and_shown(client, settings_file):
    assert not load_config(settings_file).gmail_read_all
    assert "Saved." in save(client, gmail_read_all="on").text
    assert load_config(settings_file).gmail_read_all
    assert '<option value="on" selected>all my e-mails' in client.get("/settings").text


def test_phone_book_lists_are_saved_and_shown(client, settings_file):
    other = "https://raw.githubusercontent.com/someone/jobs/main/README.md"
    save(client, phonebook_lists=f" {other} \n\n{other}")
    assert load_config(settings_file).sources.phonebook_lists == [other]  # spaces, blank lines and repeats cleaned
    assert f">{other}</textarea>" in client.get("/settings").text


def test_hacker_news_is_ticked_when_the_file_does_not_say(client):
    page = client.get("/settings").text
    assert f"{TICK} checked>" in page
    assert page.index(UNTICKED) < page.index(TICK)
    assert "Hacker News' monthly Who is hiring (only the company boards it links to)" in page
    assert "Read by the phone book. Only board names are kept, never what people wrote." in page


@pytest.mark.parametrize(("sent", "kept"), [(TICKED, True), ("off", False)])
def test_the_hacker_news_box_is_saved_and_shown(client, settings_file, sent, kept):
    assert "Saved." in save(client, hacker_news=sent).text
    assert load_config(settings_file).sources.hacker_news is kept
    assert f"hacker_news: {str(kept).lower()}" in settings_file.read_text(encoding="utf-8")
    assert (f"{TICK} checked>" in client.get("/settings").text) is kept


def test_the_hacker_news_box_leaves_the_other_sources_as_they_are(client, settings_file):
    other = "https://raw.githubusercontent.com/someone/jobs/main/README.md"
    save(client, hacker_news=TICKED, keywords="RL", phonebook_lists=other)
    sources = load_config(settings_file).sources
    assert [listing.url for listing in sources.simplify_lists] == [LIST]
    assert (sources.extra_boards, sources.phonebook_lists) == (["greenhouse:acme"], [other])
    assert sources.hacker_news is True


def test_a_refused_save_keeps_the_box_as_you_left_it(client):
    text = save(client, hacker_news="off", extra_boards="not a board").text
    assert "Not saved" in text and f"{TICK}>" in text


@pytest.mark.usefixtures("no_waiting")
@pytest.mark.parametrize(("sent", "asked"), [(TICKED, True), ("off", False)])
def test_the_next_refresh_does_what_the_box_says(client, conn, settings_file, sent, asked):
    save(client, hacker_news=sent)
    web = FakeWeb({})
    with closing(fetch.Fetcher(httpx.MockTransport(web))) as fetcher:
        phonebook.refresh(conn, load_config(settings_file), fetcher, [].append)
    assert (fetch.HN_API in {request.url.host for request in web.requests}) is asked


FEED_BOX = '<input type="checkbox" name="feed_remoteok" value="on"'
NO_FEED = '<input type="hidden" name="feed_remoteok" value="off">'


def test_a_feed_is_ticked_when_the_file_does_not_say(client):
    page = client.get("/settings").text
    assert f"{FEED_BOX} checked> Remote OK</label>" in page
    assert page.index(NO_FEED) < page.index(FEED_BOX)


@pytest.mark.parametrize(("sent", "feeds"), [(TICKED, ["remoteok"]), ("off", [])])
def test_a_feeds_box_is_saved_and_shown(client, settings_file, sent, feeds):
    assert "Saved." in save(client, feed_remoteok=sent).text
    assert load_config(settings_file).sources.feeds == feeds
    assert (f"{FEED_BOX} checked>" in client.get("/settings").text) is bool(feeds)


def test_a_feeds_box_leaves_the_other_sources_as_they_are(client, settings_file):
    other = "https://raw.githubusercontent.com/someone/jobs/main/README.md"
    save(client, feed_remoteok="off", hacker_news=TICKED, phonebook_lists=other)
    sources = load_config(settings_file).sources
    assert [listing.url for listing in sources.simplify_lists] == [LIST]
    assert (sources.extra_boards, sources.phonebook_lists) == (["greenhouse:acme"], [other])
    assert (sources.hacker_news, sources.feeds) == (True, [])


def test_a_refused_save_keeps_a_feeds_box_as_you_left_it(client):
    text = save(client, feed_remoteok="off", extra_boards="not a board").text
    assert "Not saved" in text and f"{FEED_BOX}>" in text


USAJOBS_BOX = '<input type="checkbox" name="feed_usajobs" value="on"'


def test_usajobs_is_off_until_you_tick_it_and_the_page_says_what_it_needs(client):
    page = " ".join(client.get("/settings").text.split())
    assert f"{USAJOBS_BOX}>" in page and f"{USAJOBS_BOX} checked" not in page
    assert "(needs your own free key; almost all jobs need US citizenship)" in page
    assert 'href="/settings/usajobs"' in page
    assert NO_USAJOBS_KEY not in page


NO_USAJOBS_KEY = "Ticked, but USAJobs is skipped until you save your"


@pytest.mark.parametrize(
    ("saved", "missing"),
    [({}, "key and e-mail"), ({usajobs.KEY: "fake-key-1234"}, "e-mail"), ({usajobs.EMAIL: "sam@example.com"}, "key")],
)
def test_ticked_with_something_missing_says_which(client, saved, missing):
    for name, value in saved.items():
        keychain.set_secret(name, value)
    assert f"{NO_USAJOBS_KEY} {missing}." in " ".join(save(client, feed_usajobs=TICKED).text.split())


def test_ticked_with_a_key_says_nothing_more(client):
    keychain.set_secret(usajobs.KEY, "fake-key-1234")
    keychain.set_secret(usajobs.EMAIL, "sam.fake@example.com")
    text = save(client, feed_usajobs=TICKED).text
    assert NO_USAJOBS_KEY not in text
    assert "fake-key-1234" not in text


def test_adding_and_removing_simplify_lists(client, settings_file):
    second = LIST.replace("Summer2027-Internships", "New-Grad-Positions")
    save(client, list_url_1=second, list_level_1="new grad")
    assert [listing.level for listing in load_config(settings_file).sources.simplify_lists] == ["intern", "new grad"]
    assert 'name="list_url_2"' in client.get("/settings").text  # still one empty row
    save(client, list_url_0="", list_url_1=second, list_level_1="new grad")
    assert [listing.url for listing in load_config(settings_file).sources.simplify_lists] == [second]


def test_special_characters_survive_saving(client, settings_file):
    words = "yes\n# not a comment\nkey: value\n\"quoted\" 'single'\nIngénieur IA\n100%"
    save(client, keywords=words)
    assert load_config(settings_file).roles.keywords == words.split("\n")


def test_an_interrupted_save_keeps_the_old_file(client, settings_file, monkeypatch):
    before = settings_file.read_bytes()

    def power_cut(self, target):
        raise OSError(28, "No space left on device", str(self))

    monkeypatch.setattr(actions.Path, "replace", power_cut)
    text = save(client, keywords="AI").text
    assert "The settings could not be saved (No space left on device: config.yaml.new)." in text
    error = next(line for line in text.splitlines() if "Not saved" in line)
    assert str(settings_file.parent) not in error  # the file name only, never the folder path
    assert settings_file.read_bytes() == before
    assert not settings_file.with_name("config.yaml.new").exists()


def test_saving_needs_the_anti_forgery_token(client, settings_file):
    before = settings_file.read_bytes()
    assert client.post("/settings", data=form(keywords="AI")).status_code == 403
    assert settings_file.read_bytes() == before


def test_a_broken_settings_file_is_shown_as_a_problem(client, settings_file):
    settings_file.write_text("data_dir: [unclosed\n", encoding="utf-8")
    text = client.get("/settings").text
    assert "data/config.yaml is not valid YAML" in text and "Fix <code>data/config.yaml</code> by hand" in text
    assert "Save settings" not in text


def test_every_simplify_list_is_shown_and_kept(client, settings_file):
    many = [LIST.replace("Summer2027", f"Summer{year}") for year in range(2000, 2030)]  # 30 lists
    settings_file.write_text(
        ORIGINAL.split("sources:")[0].format(data_dir=load_config(settings_file).data_dir, list=LIST)
        + "sources:\n  simplify_lists:\n"
        + "".join(f"    - url: {url}\n      level: intern\n" for url in many),
        encoding="utf-8",
    )
    page = client.get("/settings").text
    assert 'name="list_url_29"' in page and 'name="list_url_30"' in page  # all 30, plus one empty row
    typed = {f"list_url_{number}": url for number, url in enumerate(many)} | {"list_url_1": "", "list_url_0": ""}
    assert "Saved." in save(client, **typed, **{f"list_level_{number}": "intern" for number in range(30)}).text
    assert [listing.url for listing in load_config(settings_file).sources.simplify_lists] == many[2:]


def test_settings_lists_stopped_boards_with_read_again(client, data_dir):
    with closing(db.connect(data_dir)) as conn, conn:
        conn.execute("INSERT INTO sources (name, stopped_on) VALUES ('greenhouse:wrongco', '2026-10-22')")
    text = client.get("/settings").text
    assert "greenhouse:wrongco" in text and "(since 2026-10-22)" in text and "Read again" in text
    form = {"board": "greenhouse:wrongco", "value": "0", "csrf_token": csrf(client)}
    response = client.post("/boards/stop", data=form, follow_redirects=False)
    assert response.headers["location"] == "/settings"
    assert "greenhouse:wrongco" not in client.get("/settings").text
