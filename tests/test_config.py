from pathlib import Path

import pytest

from app.config import PROJECT_DIR, ConfigError, load_config

FAKE_VALUE = "fake-private-value-123"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_relative_data_dir_counts_from_project_folder(tmp_path):
    assert load_config(write(tmp_path, "data_dir: data\n")).data_dir == PROJECT_DIR / "data"


def test_absolute_data_dir_is_kept(tmp_path):
    assert load_config(write(tmp_path, f"data_dir: {tmp_path}\n")).data_dir == tmp_path.resolve()


def test_missing_file_gives_clear_message(tmp_path):
    with pytest.raises(ConfigError, match="Copy config.example.yaml"):
        load_config(tmp_path / "missing.yaml")


def test_missing_data_dir_is_reported_by_name(tmp_path):
    with pytest.raises(ConfigError, match="data_dir: Field required"):
        load_config(write(tmp_path, ""))


def test_unknown_setting_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="data_dri: Extra inputs are not permitted"):
        load_config(write(tmp_path, "data_dir: data\ndata_dri: data\n"))


@pytest.mark.parametrize("text", [f"data_dir: data\nsurprise: {FAKE_VALUE}\n", f"data_dir: [{FAKE_VALUE}\n"])
def test_errors_never_contain_values(tmp_path, text):
    with pytest.raises(ConfigError) as error:
        load_config(write(tmp_path, text))
    assert FAKE_VALUE not in str(error.value)
    assert error.value.__suppress_context__  # the original error, which may hold values, is not shown


@pytest.mark.parametrize("value", ['""', ".", '"~"', '"~/"', "..", '"/"'])
def test_data_dir_cannot_be_the_project_or_home_folder_or_above(tmp_path, value):
    with pytest.raises(ConfigError, match="a folder of its own"):
        load_config(write(tmp_path, f"data_dir: {value}\n"))


SECTIONS = """data_dir: data
roles:
  simplify_categories: ["AI/ML/Data"]
  keywords: ["machine learning", "RL"]
sources:
  simplify_lists:
    - url: https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/listings.json
      level: intern
  extra_boards: [" Greenhouse:Acme ", "ashby:globex"]
  phonebook_lists: ["https://raw.githubusercontent.com/someone/jobs/main/README.md"]
"""


def test_roles_and_sources_are_read(tmp_path):
    config = load_config(write(tmp_path, SECTIONS))
    assert config.roles.keywords == ["machine learning", "RL"]
    assert config.sources.simplify_lists[0].level == "intern"
    assert config.sources.extra_boards == ["greenhouse:acme", "ashby:globex"]
    assert config.sources.phonebook_lists == ["https://raw.githubusercontent.com/someone/jobs/main/README.md"]


def test_roles_and_sources_are_optional(tmp_path):
    config = load_config(write(tmp_path, "data_dir: data\n"))
    assert (config.roles.keywords, config.sources.simplify_lists, config.sources.extra_boards) == ([], [], [])


@pytest.mark.parametrize(
    ("old", "new", "where"),
    [
        ("https://raw.githubusercontent.com", "http://raw.githubusercontent.com", "simplify_lists.0.url"),
        ("https://raw.githubusercontent.com", f"https://{FAKE_VALUE}.example", "simplify_lists.0.url"),
        ("level: intern", "level: senior", "simplify_lists.0.level"),
        ('"ashby:globex"', f'"workday:{FAKE_VALUE}"', "extra_boards"),
        ('"ashby:globex"', '"lever:../admin"', "extra_boards"),
        ("https://raw.githubusercontent.com/someone", f"https://{FAKE_VALUE}.example/someone", "phonebook_lists.0"),
    ],
)
def test_bad_source_settings_are_named_without_their_value(tmp_path, old, new, where):
    with pytest.raises(ConfigError, match=f"sources.{where}") as error:
        load_config(write(tmp_path, SECTIONS.replace(old, new)))
    assert FAKE_VALUE not in str(error.value)


def test_the_example_settings_are_valid():
    config = load_config(PROJECT_DIR / "config.example.yaml")
    assert config.roles.keywords
    assert [listing.level for listing in config.sources.simplify_lists] == ["intern", "new grad"]


@pytest.mark.parametrize(("value", "ok"), [("0", True), ("168", True), ("-1", False), ("169", False),
                                            ('"6"', False), ("true", False), ("1.5", False)])  # fmt: skip
def test_the_dashboard_schedule_is_0_to_168_hours(tmp_path, value, ok):
    path = write(tmp_path, f"data_dir: data\ncollect_every_hours: {value}\n")
    if ok:
        assert load_config(path).collect_every_hours == int(value)
    else:
        with pytest.raises(ConfigError, match="collect_every_hours"):
            load_config(path)


@pytest.mark.parametrize(
    ("setting", "value", "ok"),
    [
        ("phonebook_every_days", "0", True), ("phonebook_every_days", "365", True),
        ("phonebook_every_days", "366", False), ("phonebook_every_days", "-1", False),
        ("requests_per_second", "1", True), ("requests_per_second", "5", True),
        ("requests_per_second", "0", False), ("requests_per_second", "6", False),
        ("requests_per_second", "0.5", False), ("requests_per_second", '"2"', False),
    ],
)  # fmt: skip
def test_the_phone_book_schedule_and_requests_per_second_have_limits(tmp_path, setting, value, ok):
    path = write(tmp_path, f"data_dir: data\n{setting}: {value}\n")
    if ok:
        assert getattr(load_config(path), setting) == int(value)
    else:
        with pytest.raises(ConfigError, match=setting):
            load_config(path)


def test_without_them_the_phone_book_is_monthly_at_one_request_per_second(tmp_path):
    config = load_config(write(tmp_path, "data_dir: data\n"))
    assert (config.phonebook_every_days, config.requests_per_second) == (30, 1)


def test_scoring_settings_have_defaults_and_limits(tmp_path):
    assert load_config(write(tmp_path, "data_dir: data\n")).scoring.meaning_threshold == 0.5
    with pytest.raises(ConfigError, match="scoring.skills_threshold: Input should be less than or equal to 1"):
        load_config(write(tmp_path, "data_dir: data\nscoring:\n  skills_threshold: 1.5\n"))


AI_ROLE = "data_dir: data\nai:\n  evaluator:\n"


@pytest.mark.parametrize(
    ("role", "problem"),
    [
        ("    model: m\n    address: http://192.168.1.5:11434\n", "Ollama must run on this computer"),
        ("    model: m\n    address: http://0.0.0.0:11434\n", "Ollama must run on this computer"),
        ("    service: anthropic\n    model: m\n    address: http://api.anthropic.com\n    key_name: k\n", "https://"),
        ("    service: anthropic\n    model: m\n", "needs key_name"),
        ("    service: openai-compatible\n    model: m\n    key_name: k\n", "https://"),  # no default address
        ("    model: rm -rf /\n", "String should match pattern"),
        ("    model: m\n    thinking: off\n", "thinking: Input should be 'off'"),  # YAML reads a bare off as false
    ],
)
def test_ai_role_settings_are_checked(tmp_path, role, problem):
    with pytest.raises(ConfigError, match=problem):
        load_config(write(tmp_path, AI_ROLE + role))


def test_ai_roles_have_safe_defaults(tmp_path):
    role = load_config(write(tmp_path, "data_dir: data\n")).ai.evaluator
    assert (role.service, role.model, role.address, role.thinking) == (
        "ollama", "gemma4:26b-a4b-it-q8_0", "http://127.0.0.1:11434", "off"
    )  # fmt: skip
    claude = load_config(write(tmp_path, AI_ROLE + "    service: anthropic\n    model: m\n    key_name: k\n"))
    assert claude.ai.evaluator.address == "https://api.anthropic.com"
