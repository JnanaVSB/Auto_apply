"""Settings → Scoring model, on a temporary settings file and the fake keychain (never the real ones)."""

from html import unescape

import pytest
import yaml
from helpers import csrf, log_in
from keyring.errors import KeyringLocked
from markupsafe import escape
from test_web_settings import ORIGINAL

from app import config
from app.config import AISettings, load_config
from app.keychain import SERVICE
from app.llm import prices
from app.scoring import stage_b
from app.web import scoring_settings

FAKE_KEY = "fake-key-123"  # tests only
PAGE = scoring_settings.PAGE
OWN = {"address": "https://scoring.example.net/v1", "model": "my-model"}


@pytest.fixture
def settings_file(tmp_path, data_dir, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(ORIGINAL.format(data_dir=data_dir, list="https://raw.githubusercontent.com/a/b/c.json"), "utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


@pytest.fixture
def client(client, settings_file):
    log_in(client)
    return client


def post(client, path, /, **fields):
    return client.post(path, data=fields | {"csrf_token": csrf(client)})


def scoring(settings_file):
    found = load_config(settings_file)
    return found.ai.evaluator, found.scoring.cloud_ok


def test_a_new_install_scores_on_this_computer_and_nothing_is_chosen_or_ticked(client):
    text = client.get(PAGE).text
    assert "on this computer (free; nothing leaves it)" in text
    assert 'name="address" type="url" maxlength="300" value=""' in text and 'name="confirm" value="on">' in text
    assert "Send my resume to this address to score jobs." in " ".join(text.split())  # never ticked for you
    assert 'href="/settings/scoring-model"' in client.get("/settings").text


def test_your_own_address_is_saved_with_the_tick_and_writes_the_ticked_address_too(client, settings_file):
    text = post(client, PAGE + "/own", **OWN).text
    assert "Not saved: tick the box to confirm that your resume (contact details hidden) and job ads go to" in text
    assert "scoring.example.net." in text and 'value="https://scoring.example.net/v1"' in text  # what was typed
    assert scoring(settings_file)[1] == ""
    text = post(client, PAGE + "/own", **OWN | {"address": "https://scoring.example.net/v1/"}, confirm="on").text
    role, ticked = scoring(settings_file)
    assert (role.service, role.model, role.address, role.key_name, role.thinking) == (
        "openai-compatible", "my-model", "https://scoring.example.net/v1", None, "off"
    )  # fmt: skip
    assert ticked == "https://scoring.example.net/v1" and "Saved: jobs are scored through your own address." in text
    assert 'name="confirm" value="on" checked' in text  # shown as ticked for the same address
    post(client, PAGE + "/own", **OWN | {"model": "other-model"})  # the same address: not asked again
    assert scoring(settings_file)[0].model == "other-model"
    post(client, PAGE + "/own", **OWN | {"address": "https://other.example.net/v1"})  # a new address asks again
    assert scoring(settings_file) == (role.model_copy(update={"model": "other-model"}), ticked)


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"address": scoring_settings.PRESETS["cloudflare"][1], "model": "@cf/meta/llama-3.1-8b-instruct"},
         "put your own Cloudflare account id where the address says YOUR-ACCOUNT-ID"),
        (OWN | {"model": "OpenRouter/free"}, "the model OpenRouter/free lets OpenRouter choose the model"),
        (OWN | {"address": "http://scoring.example.net/v1"}, "must start with https://"),
        (OWN | {"price_input": "0.2"}, "give both prices"),
        (OWN | {"model": ""}, "ai.evaluator.model"),
    ],
)  # fmt: skip
def test_what_the_settings_refuse_is_not_saved_and_keeps_what_was_typed(client, settings_file, fields, problem):
    text = post(client, PAGE + "/own", **fields, confirm="on").text
    assert escape(problem) in text and scoring(settings_file) == (AISettings().evaluator, "")
    assert f'value="{escape(fields["address"])}"' in text


def test_a_cloudflare_model_name_and_prices_are_saved_and_read_back(client, settings_file):
    address = "https://api.cloudflare.com/client/v4/accounts/fake-account-0000/ai/v1"
    fields = {"model": "@cf/meta/llama-3.1-8b-instruct", "price_input": "0.2", "price_output": "0.6"}
    post(client, PAGE + "/own", address=address, **fields, needs_key="on", confirm="on")
    role, ticked = scoring(settings_file)
    assert (role.model, role.price_input, role.price_output, role.key_name, ticked) == (
        "@cf/meta/llama-3.1-8b-instruct", 0.2, 0.6, "own-scoring", address
    )  # fmt: skip
    written = settings_file.read_text(encoding="utf-8")
    assert "model: '@cf/meta/llama-3.1-8b-instruct'" in written  # quoted: "@" can't start a bare value
    assert yaml.safe_load(written)["ai"]["evaluator"]["model"] == "@cf/meta/llama-3.1-8b-instruct"
    text = client.get(PAGE).text
    assert 'value="0.2"' in text and 'name="needs_key" value="on" checked' in text and "fake-account-0000" in text


def test_a_preset_fills_in_only_the_address_and_none_is_chosen_by_default(client, settings_file):
    for name, (_, address) in scoring_settings.PRESETS.items():
        text = client.get(f"{PAGE}?preset={name}").text
        assert f'name="address" type="url" maxlength="300" value="{address}"' in text
        assert 'name="model" maxlength="100" value=""' in text and 'name="confirm" value="on">' in text
    assert 'value=""' in client.get(f"{PAGE}?preset=nothing-like-it").text
    assert scoring(settings_file) == (AISettings().evaluator, "")  # a preset saves nothing


def test_scoring_on_this_computer_goes_back_to_the_usual_model_and_forgets_the_tick(client, settings_file):
    post(client, PAGE + "/own", **OWN, confirm="on")
    text = post(client, PAGE + "/local").text
    assert scoring(settings_file) == (AISettings().evaluator, "")
    assert "Saved: jobs are scored on this computer." in text
    post(client, PAGE + "/own", **OWN)
    assert scoring(settings_file)[0].service == "ollama"  # the address must be ticked again


def test_a_save_lets_a_run_the_old_setting_stopped_start_again(client):
    client.app.state.scorer.failed_at = "a minute ago"
    post(client, PAGE + "/local")
    assert client.app.state.scorer.failed_at is None


def test_the_key_is_saved_only_in_the_keychain_under_its_own_name_and_never_shown(client, settings_file, fake_keychain):
    text = post(client, PAGE + "/key", key=FAKE_KEY).text
    assert fake_keychain.store[(SERVICE, "own-scoring")] == FAKE_KEY
    assert (SERVICE, "own-address") not in fake_keychain.store  # the Writer's own-address key is a different one
    assert "Key saved." in text and FAKE_KEY not in text and FAKE_KEY not in settings_file.read_text(encoding="utf-8")
    assert "Remove key" in text
    for bad in ("", "has spaces in it", "x" * 501):
        assert "Not saved: paste the whole key" in post(client, PAGE + "/key", key=bad).text
    assert "Key removed." in post(client, PAGE + "/key", remove="1").text
    assert (SERVICE, "own-scoring") not in fake_keychain.store
    assert client.post(PAGE + "/key", data={"key": FAKE_KEY}).status_code == 403  # no form token


def test_keychain_trouble_is_named_never_the_key(client, fake_keychain, monkeypatch):
    def locked(*args):
        raise KeyringLocked(f"locked while handling {FAKE_KEY}")

    monkeypatch.setattr(fake_keychain, "get_password", locked)
    text = unescape(client.get(PAGE).text)
    assert text.count("the keychain can't be read") == 3 and FAKE_KEY not in text  # yours, OpenAI's, Anthropic's


def test_the_test_button_checks_the_scoring_model(client, monkeypatch):
    checked = []
    monkeypatch.setattr(scoring_settings.llm, "check", lambda role: checked.append(role.model) or (True, "It works."))
    assert '<p class="result" role="status">It works.</p>' in post(client, PAGE + "/test").text
    post(client, PAGE + "/own", **OWN, confirm="on")
    post(client, PAGE + "/test")
    assert checked == [AISettings().evaluator.model, "my-model"]


def test_whatever_was_typed_is_shown_as_text(client):
    sneaky = '"><script>alert(1)</script>'
    text = post(client, PAGE + "/own", address=f"https://scoring.example.net/{sneaky}", model=sneaky).text
    assert "<script>alert(1)</script>" not in text and escape(sneaky) in text


@pytest.mark.parametrize(
    ("kind", "service", "address"),
    [
        ("openai", "openai-compatible", "https://api.openai.com/v1"),
        ("anthropic", "anthropic", "https://api.anthropic.com"),
    ],
)
def test_openai_and_anthropic_are_saved_with_the_tick_and_the_scoring_gate_agrees(
    client, settings_file, kind, service, address
):
    model, host = next(iter(prices.models(kind))), address.split("/")[2]
    text = post(client, PAGE + "/paid", kind=kind, model=model).text
    assert f"job ads go to {host}." in text and f'<option value="{model}" selected>' in text  # what was chosen
    assert scoring(settings_file) == (AISettings().evaluator, "")
    text = post(client, PAGE + "/paid", kind=kind, model=model, confirm="on").text
    role, ticked = scoring(settings_file)
    assert (role.service, role.model, role.address, role.key_name, role.thinking, ticked) == (
        service, model, address, kind, "off", address
    )  # fmt: skip
    assert f"Saved: jobs are scored through {scoring_settings.PAID[kind][0]}." in text
    assert f"<strong>{model}</strong> through <code>{host}</code>" in text
    assert f'<option value="{model}" selected>' in text
    assert text.count('<option value="">Choose a model</option>') == 1  # the other service's only
    stage_b.gate(role, load_config(settings_file).scoring)  # the page and the gate agree on the address


def test_the_same_service_is_not_asked_again_but_another_one_is(client, settings_file):
    first, second, *_ = prices.models("openai")
    post(client, PAGE + "/own", **OWN, confirm="on")
    post(client, PAGE + "/paid", kind="openai", model=first)  # from your own address: asked again
    assert scoring(settings_file)[0].model == "my-model"
    post(client, PAGE + "/paid", kind="openai", model=first, confirm="on")
    post(client, PAGE + "/paid", kind="openai", model=second)  # the same address: not asked again
    assert (scoring(settings_file)[0].model, scoring(settings_file)[1]) == (second, "https://api.openai.com/v1")
    text = post(client, PAGE + "/paid", kind="anthropic", model=next(iter(prices.models("anthropic")))).text
    assert "job ads go to api.anthropic.com." in text and scoring(settings_file)[0].model == second


@pytest.mark.parametrize(
    ("kind", "model"),
    [("openai", "gpt-made-up"), ("openai", next(iter(prices.models("anthropic")))), ("anthropic", ""),
     ("made-up", "gpt-made-up"), ("", next(iter(prices.models("openai")))), ("ollama", AISettings().evaluator.model)],
)  # fmt: skip
def test_only_a_listed_model_of_openai_or_anthropic_is_saved(client, settings_file, kind, model):
    text = post(client, PAGE + "/paid", kind=kind, model=model, confirm="on").text
    assert "Not saved: choose one of the listed models." in text
    assert scoring(settings_file) == (AISettings().evaluator, "")


def test_no_model_is_chosen_for_you_and_the_writers_keys_are_named_never_asked_for(client, fake_keychain):
    text = client.get(PAGE).text
    assert text.count('<option value="">Choose a model</option>') == 2 and " selected>" not in text
    link = 'no key saved yet: add it in <a href="/settings/writer">Settings → Writer</a>'
    assert " ".join(text.split()).count(link) == 2
    fake_keychain.store[(SERVICE, "openai")] = FAKE_KEY
    text = " ".join(client.get(PAGE).text.split())
    assert "OpenAI key: saved" in text and "Anthropic key: no key saved yet" in text and FAKE_KEY not in text
    assert text.count('name="key"') == 1  # only your own address's key box: one key per service, on the Writer page
