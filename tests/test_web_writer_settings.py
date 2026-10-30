"""The Writer settings page, on a temporary settings file and the fake keychain (never the real ones)."""

import pytest
from helpers import csrf, log_in
from keyring.errors import KeyringLocked
from markupsafe import escape
from test_web_settings import ORIGINAL

from app import config
from app.config import load_config
from app.keychain import SERVICE
from app.web import writer_settings

FAKE_KEY = "fake-key-123"  # tests only
DOWNLOADED = ["gemma4:26b-a4b-it-q8_0", "qwen3:14b"]
OWN = {"kind": "own", "address": "https://models.example.org/v1", "model": "my-model", "thinking": "off"}


@pytest.fixture
def settings_file(tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(ORIGINAL.format(data_dir=data_dir, list="https://raw.githubusercontent.com/a/b/c.json"), "utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(writer_settings.ollama, "installed", lambda role: DOWNLOADED)
    return path


@pytest.fixture
def client(client, settings_file):
    log_in(client)
    return client


def post(client, path, /, **fields):
    return client.post(path, data=fields | {"csrf_token": csrf(client)})


def writer(settings_file):
    return load_config(settings_file).ai.writer


def test_a_new_install_has_no_writer_and_shows_the_four_kinds(client):
    text = client.get("/settings/writer").text
    assert "None chosen yet" in text and 'type="submit" disabled>Test' in text
    assert text.count('name="kind" value=') == 4
    assert "Claude Opus 5.5: $4 / $20" in text and "GPT-6.1 Sol: $2 / $10" in text and "checked 2026-10-30" in text
    assert "<option selected>gemma4:26b-a4b-it-q8_0</option>" in text  # the scorer's model first
    assert 'href="/settings/writer"' in client.get("/settings").text


def test_the_local_model_is_saved_and_a_different_one_warns_about_switching(client, settings_file):
    response = post(client, "/settings/writer", kind="local", model="gemma4:26b-a4b-it-q8_0", thinking="off",
                    context="16384")  # fmt: skip
    assert response.history and "Writer saved." in response.text and escape(writer_settings.SWAP) not in response.text
    role = writer(settings_file)
    assert (role.service, role.model, role.context) == ("ollama", "gemma4:26b-a4b-it-q8_0", 16384)
    for changes in ({"model": "qwen3:14b"}, {"context": "32768"}):
        fields = {"kind": "local", "model": "gemma4:26b-a4b-it-q8_0", "thinking": "off", "context": "16384"}
        assert escape(writer_settings.SWAP) in post(client, "/settings/writer", **fields | changes).text
    assert "keywords" in settings_file.read_text(encoding="utf-8")  # the rest of the file is kept


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"kind": "local", "model": "llama9:70b", "context": "16384"}, "Choose a model Ollama has downloaded"),
        ({"kind": "local", "model": "qwen3:14b", "context": "99"}, "ai.writer.context"),
        ({"kind": "anthropic", "model": "claude-unknown-9"}, "Choose one of the listed models."),
        ({"kind": "openai", "model": "gpt-4"}, "Choose one of the listed models."),
        ({"kind": "cloud"}, "Choose one of the four kinds"),
        (
            OWN,
            "Tick the box to confirm that your entries (contact details hidden) and job ads go to models.example.org",
        ),
        (
            OWN | {"address": "http://8.8.8.8/v1", "confirm": "on"},
            escape("ai.writer: Value error, a cloud service's address"),
        ),
        (OWN | {"price_input": "0.5", "confirm": "on"}, "give both prices"),
        (OWN | {"price_input": "lots", "price_output": "1", "confirm": "on"}, "ai.writer.price_input"),
    ],
)
def test_a_writer_that_breaks_the_rules_is_not_saved(client, settings_file, fields, problem):
    before = settings_file.read_text(encoding="utf-8")
    text = post(client, "/settings/writer", **{"thinking": "off"} | fields).text
    assert problem in text and settings_file.read_text(encoding="utf-8") == before
    if fields.get("kind") == "own":
        assert f'value="{fields["address"]}"' in text  # what was typed stays on the page


def test_anthropic_and_openai_use_their_own_keys(client, settings_file):
    with settings_file.open("a", encoding="utf-8") as file:
        file.write("ai:\n  evaluator:\n    model: qwen3:14b\n")
    text = post(client, "/settings/writer", kind="anthropic", model="claude-sonnet-5-5", thinking="high").text
    role = writer(settings_file)
    assert (role.service, role.model, role.key_name, role.thinking) == ("anthropic", "claude-sonnet-5-5", "anthropic",
                                                                        "high")  # fmt: skip
    assert "No key is saved under &#39;anthropic&#39;" in text and "In use: <strong>Claude Sonnet 5.5</strong>" in text
    post(client, "/settings/writer", kind="openai", model="gpt-6.1-terra", thinking="off")
    role = writer(settings_file)
    assert (role.address, role.key_name, role.own) == ("https://api.openai.com/v1", "openai", False)
    assert load_config(settings_file).ai.evaluator.model == "qwen3:14b"  # the scorer's settings are kept


def test_your_own_address_is_confirmed_once_and_may_have_a_key_and_prices(client, settings_file):
    text = post(client, "/settings/writer", **OWN, confirm="on").text
    role = writer(settings_file)
    assert (role.address, role.key_name, role.price_input) == ("https://models.example.org/v1", None, None)
    assert escape(writer_settings.NOT_COUNTED) in text
    text = post(client, "/settings/writer", **OWN, needs_key="on", price_input="0.2", price_output="0.6").text
    role = writer(settings_file)  # the same address: no new confirmation needed
    assert (role.key_name, role.price_input, role.price_output) == ("own-address", 0.2, 0.6)
    assert escape(writer_settings.NOT_COUNTED) not in text and 'name="needs_key" value="on" checked' in text
    changed = OWN | {"address": "http://192.168.1.5:8000/v1"}
    assert "Tick the box" in post(client, "/settings/writer", **changed).text
    post(client, "/settings/writer", **changed, confirm="on")
    assert writer(settings_file).address == "http://192.168.1.5:8000/v1"


def test_a_key_is_saved_only_in_the_keychain_and_never_shown(client, settings_file, fake_keychain):
    text = post(client, "/settings/keys/anthropic", key=FAKE_KEY).text
    assert fake_keychain.store[(SERVICE, "anthropic")] == FAKE_KEY
    assert "Key saved." in text and FAKE_KEY not in text and FAKE_KEY not in settings_file.read_text(encoding="utf-8")
    assert "Remove key" in text
    for bad in ("", "has spaces in it", "x" * 501, "tab\there"):
        assert "Not saved: paste the whole key" in post(client, "/settings/keys/openai", key=bad).text
    assert "Key removed." in post(client, "/settings/keys/anthropic", remove="1").text
    assert (SERVICE, "anthropic") not in fake_keychain.store
    assert post(client, "/settings/keys/db_key", key=FAKE_KEY).status_code == 404  # never the database's own key
    assert client.post("/settings/keys/openai", data={"key": FAKE_KEY}).status_code == 403  # no form token


def test_keychain_trouble_is_named_never_the_key(client, fake_keychain, monkeypatch):
    def locked(*args):
        raise KeyringLocked(f"locked while handling {FAKE_KEY}")

    monkeypatch.setattr(fake_keychain, "set_password", locked)
    text = post(client, "/settings/keys/openai", key=FAKE_KEY).text
    assert "Could not save &#39;openai&#39; in the OS keychain (KeyringLocked)" in text and FAKE_KEY not in text
    monkeypatch.setattr(fake_keychain, "get_password", locked)
    assert text.count("the keychain can&#39;t be read") == 0
    assert client.get("/settings/writer").text.count("the keychain can&#39;t be read") == 3


def test_the_test_button_checks_the_chosen_writer(client, monkeypatch):
    assert "Choose a model first." in post(client, "/settings/writer/test").text
    post(client, "/settings/writer", kind="anthropic", model="claude-opus-5-5", thinking="high")
    checked = []
    monkeypatch.setattr(writer_settings.llm, "check", lambda role: checked.append(role.model) or (True, "It works."))
    assert '<p class="result" role="status">It works.</p>' in post(client, "/settings/writer/test").text
    assert checked == ["claude-opus-5-5"]


def test_the_limits_and_the_one_page_switch_are_saved_and_checked(client, settings_file):
    text = post(client, "/settings/limits", daily="5", monthly="80.5", one_page="off").text
    found = load_config(settings_file)
    assert "Saved." in text
    assert (found.spending.daily, found.spending.monthly, found.documents.one_page) == (5, 80.5, False)
    before = settings_file.read_text(encoding="utf-8")
    text = post(client, "/settings/limits", daily="900", monthly="80", one_page="on").text
    assert "spending.daily" in text and 'value="900"' in text and settings_file.read_text(encoding="utf-8") == before
