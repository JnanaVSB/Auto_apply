"""The Settings page, on a temporary settings file (never the real data/config.yaml)."""

import stat
from contextlib import closing

import pytest
from helpers import LIST, csrf, log_in

from app import config, db
from app.config import load_config
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


def test_phone_book_lists_are_saved_and_shown(client, settings_file):
    other = "https://raw.githubusercontent.com/someone/jobs/main/README.md"
    save(client, phonebook_lists=f" {other} \n\n{other}")
    assert load_config(settings_file).sources.phonebook_lists == [other]  # spaces, blank lines and repeats cleaned
    assert f">{other}</textarea>" in client.get("/settings").text


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
