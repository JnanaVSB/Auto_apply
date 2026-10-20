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
