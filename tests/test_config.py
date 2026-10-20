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
"""


def test_roles_and_sources_are_read(tmp_path):
    config = load_config(write(tmp_path, SECTIONS))
    assert config.roles.keywords == ["machine learning", "RL"]
    assert config.sources.simplify_lists[0].level == "intern"
    assert config.sources.extra_boards == ["greenhouse:acme", "ashby:globex"]


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
