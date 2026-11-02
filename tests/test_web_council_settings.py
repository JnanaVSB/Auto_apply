"""The council settings page, on a temporary settings file and the fake keychain (never the real ones)."""

import pytest
from helpers import csrf, log_in
from test_web_settings import ORIGINAL

from app import config
from app.config import load_config
from app.keychain import SERVICE
from app.web import council_settings, writer_settings

DOWNLOADED = ["gemma4:26b-a4b-it-q8_0", "qwen3.6:35b-a3b"]


@pytest.fixture
def settings_file(tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(ORIGINAL.format(data_dir=data_dir, list="https://raw.githubusercontent.com/a/b/c.json"), "utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(writer_settings.ollama, "installed", lambda role: DOWNLOADED)
    return path


@pytest.fixture
def client(client, settings_file, monkeypatch):
    log_in(client)
    resumed = []
    monkeypatch.setattr(client.app.state.preparer, "resume", lambda: resumed.append(True))
    client.resumed = resumed
    return client


def council_page(client):
    """The page below its top bar (which shows the line-up and its costs too)."""
    return client.get("/settings/council").text.split("<h1>", 1)[1]


def post(client, path, /, **fields):
    return client.post(path, data=fields | {"csrf_token": csrf(client)})


def test_a_preset_fills_in_all_six_and_a_change_makes_it_custom(client, settings_file):
    assert "Line-up: None chosen" in council_page(client)
    post(client, "/settings/council/preset", preset="laptop")
    found = load_config(settings_file).ai
    assert (found.writer.model, found.truth.model, found.ats.model, found.tier) == (
        "gemma4:26b-a4b-it-q8_0", "qwen3.6:35b-a3b", "gemma4:26b-a4b-it-q8_0", "laptop",
    )  # fmt: skip
    assert found.truth.context == 32768 and found.council_chosen and client.resumed == [True]
    text = council_page(client)
    assert "Line-up: Laptop" in text and text.count(">free<") == 6
    post(client, "/settings/writer", member="truth", kind="local", model="gemma4:26b-a4b-it-q8_0", thinking="off",
         context="32768")  # fmt: skip
    assert load_config(settings_file).ai.truth.model == "gemma4:26b-a4b-it-q8_0"
    text = council_page(client)
    assert "Line-up: Custom (based on Laptop)" in text
    assert "The Truth checker uses the same laptop model (gemma4:26b-a4b-it-q8_0) as the Writer" in text


def test_paid_presets_estimate_each_member_and_name_missing_keys(client, settings_file, fake_keychain):
    post(client, "/settings/council/preset", preset="recommended")
    ai = load_config(settings_file).ai
    assert (ai.writer.model, ai.writer.thinking, ai.hiring.model, ai.domain.model) == (
        "claude-opus-5-5", "high", "claude-sonnet-5-5", "claude-sonnet-5-5",
    )  # fmt: skip
    text = council_page(client)
    assert "Line-up: Recommended" in text and text.count("up to $") == 6 and "A whole job at most: $" in text
    assert "No key is saved under &#39;anthropic&#39;" in text and "No key is saved under &#39;openai&#39;" in text
    assert "same company" not in text  # Opus writes, Sol checks the truth
    post(client, "/settings/council/preset", preset="high")
    assert load_config(settings_file).ai.writer.thinking == "xhigh"
    assert client.get("/settings/writer?member=writer").text.count("<option selected>xhigh</option>") == 1
    fake_keychain.store[(SERVICE, "anthropic")] = "fake-key"
    assert "under &#39;anthropic&#39;" not in council_page(client)


def test_the_council_form_saves_its_settings_with_the_usual_checks(client, settings_file):
    form = {"rounds": "4", "turns": "2", "start": "21:00", "end": "07:30", "daily_jobs": "12"}
    post(client, "/settings/council", **form)
    council = load_config(settings_file).council
    assert (council.rounds, council.turns, council.start, council.end, council.daily_jobs) == (
        4,
        2,
        "21:00",
        "07:30",
        12,
    )
    before = settings_file.read_bytes()
    for wrong, field in (({"rounds": "6"}, "council.rounds"), ({"turns": "x"}, "council.turns"),
                         ({"start": "25:00"}, "council.start")):  # fmt: skip
        text = post(client, "/settings/council", **form | wrong).text
        assert field in text and settings_file.read_bytes() == before  # nothing saved
    text = post(client, "/settings/council", **form, automatic="on").text
    assert "council.automatic needs the Writer and all five council members chosen first" in text


def test_automatic_preparation_needs_the_paid_members_keys(client, settings_file, fake_keychain):
    post(client, "/settings/council/preset", preset="low")
    form = {"rounds": "3", "turns": "1", "start": "22:00", "end": "08:00", "daily_jobs": "20", "automatic": "on"}
    assert "needs the key &#39;anthropic&#39; saved first" in post(client, "/settings/council", **form).text
    fake_keychain.store[(SERVICE, "anthropic")] = "fake-key"
    fake_keychain.store[(SERVICE, "openai")] = "fake-key"
    post(client, "/settings/council", **form)
    assert load_config(settings_file).council.automatic


def test_a_member_you_add_keeps_its_instructions_in_your_data_folder_only(client, settings_file, data_dir):
    post(
        client, "/settings/council/members", name="Recruiter", letters="on", instructions="Read it as a\x00 recruiter."
    )
    added = load_config(settings_file).ai.extra
    assert [(m.id, m.name, m.letters, m.role.model) for m in added] == [
        ("x1", "Recruiter", True, "gemma4:26b-a4b-it-q8_0")
    ]
    path = data_dir / "skills" / "x1.md"
    assert path.read_text(encoding="utf-8") == "Read it as a recruiter.\n" and path.stat().st_mode & 0o777 == 0o600
    assert (data_dir / "skills").stat().st_mode & 0o777 == 0o700
    text = council_page(client)
    assert "Recruiter <span" in text and "Read it as a recruiter." in text and "/settings/writer?member=x1" in text
    post(
        client, "/settings/writer", member="x1", kind="local", model="qwen3.6:35b-a3b", thinking="off", context="32768"
    )
    assert load_config(settings_file).ai.extra[0].role.model == "qwen3.6:35b-a3b"  # its own model
    post(client, "/settings/council/members", id="x1", name="Scout", instructions="Look for leadership.")
    assert load_config(settings_file).ai.extra[0].name == "Scout" and path.read_text() == "Look for leadership.\n"
    assert load_config(settings_file).ai.extra[0].role.model == "qwen3.6:35b-a3b"  # kept
    post(client, "/settings/council/members/x1/remove")
    assert load_config(settings_file).ai.extra == [] and path.is_file()  # its instructions stay


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"name": "Recruiter", "instructions": ""}, "Write the member&#39;s instructions"),
        ({"name": "Recruiter", "instructions": "x" * 20001}, "at most 20,000 characters"),
        ({"name": "<b>Bad</b>", "instructions": "Read it."}, "ai.extra.0.name"),
    ],
)
def test_a_member_that_breaks_the_rules_is_not_added(client, settings_file, data_dir, fields, problem):
    assert problem in post(client, "/settings/council/members", **fields).text
    assert load_config(settings_file).ai.extra == [] and not (data_dir / "skills" / "x1.md").exists()


def test_at_most_three_members_can_be_added_and_unknown_ones_are_not_found(client, settings_file):
    for name in ("One", "Two", "Three"):
        post(client, "/settings/council/members", name=name, instructions="Read it.")
    assert "At most 3 members" in post(client, "/settings/council/members", name="Four", instructions="Read it.").text
    assert post(client, "/settings/council/members/x9/remove").status_code == 404
    assert "ai.extra" in post(client, "/settings/council/members", id="../x1", name="Bad", instructions="Read.").text
    assert client.get("/settings/writer?member=judge").status_code == 404
    assert post(client, "/settings/council/preset", preset="cheapest").status_code == 404


def test_the_line_up_names_itself():
    names = council_settings.NAMES
    assert set(names) == set(council_settings.PRESETS) and all(
        len(roles) == 6 for roles in council_settings.PRESETS.values()
    )
