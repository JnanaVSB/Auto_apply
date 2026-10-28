from pathlib import Path

import httpx
import pytest
from test_fetch import CONTENT, SHA256, fake_web

from app.fetch import SETUP_HOSTS
from app.scoring import files

FAKE = files.SetupFile("fake.onnx", "the fake model", "https://huggingface.co/fake.onnx", len(CONTENT), SHA256)


@pytest.fixture(autouse=True)
def one_fake_file(monkeypatch, no_waiting):
    monkeypatch.setattr(files, "FILES", (FAKE,))


def test_missing_file_is_downloaded_once(tmp_path):
    fetcher, _ = fake_web(httpx.Response(200, content=CONTENT))
    lines = []
    folder = files.ensure_files(fetcher, lines.append, tmp_path / "cache")
    assert (folder / FAKE.name).read_bytes() == CONTENT
    assert lines == ["Downloading the fake model (0.0 MB), only this once..."]
    fetcher, requests = fake_web()  # a second run asks the web for nothing
    files.ensure_files(fetcher, lines.append, folder)
    assert requests == []
    assert len(lines) == 1


def test_damaged_file_is_downloaded_again(tmp_path):
    (tmp_path / FAKE.name).write_bytes(b"x" * len(CONTENT))  # right size, wrong content
    fetcher, requests = fake_web(httpx.Response(200, content=CONTENT))
    files.ensure_files(fetcher, lambda line: None, tmp_path)
    assert (tmp_path / FAKE.name).read_bytes() == CONTENT
    assert len(requests) == 1


def test_download_problem_names_the_file(tmp_path):
    fetcher, _ = fake_web(httpx.Response(302, headers={"Location": "https://cdn.example.net/x"}))
    with pytest.raises(files.SetupError, match="Could not download the fake model: it was sent to cdn.example.net"):
        files.ensure_files(fetcher, lambda line: None, tmp_path)


def test_disk_full_says_what_to_do_without_a_full_path(tmp_path, monkeypatch):
    fetcher, _ = fake_web()

    def disk_full(*args):
        raise OSError(28, "No space left on device", str(tmp_path / FAKE.name))

    monkeypatch.setattr(fetcher, "download_file", disk_full)
    with pytest.raises(
        files.SetupError, match=r"cache folder \(No space left on device\). Free some disk space"
    ) as error:
        files.ensure_files(fetcher, lambda line: None, tmp_path)
    assert str(tmp_path) not in str(error.value)


def test_a_file_where_the_cache_folder_should_be_is_reported(tmp_path):
    (tmp_path / "cache").write_text("not a folder")
    fetcher, _ = fake_web()
    with pytest.raises(files.SetupError, match="Could not save the fake model in the app's cache folder"):
        files.ensure_files(fetcher, lambda line: None, tmp_path / "cache")


@pytest.mark.parametrize(
    ("platform", "variables", "expected"),
    [
        ("linux", {}, "home/.cache/auto_apply"),
        ("linux", {"XDG_CACHE_HOME": "/elsewhere"}, "/elsewhere/auto_apply"),
        ("linux", {"XDG_CACHE_HOME": "relative"}, "home/.cache/auto_apply"),  # only a full path counts
        ("darwin", {}, "home/Library/Caches/auto_apply"),
        ("win32", {"LOCALAPPDATA": "/appdata"}, "/appdata/auto_apply/Cache"),
    ],
)
def test_cache_folder_is_the_system_standard_one(monkeypatch, platform, variables, expected):
    monkeypatch.setattr(files.sys, "platform", platform)
    monkeypatch.setattr(files.Path, "home", lambda: Path("home"))
    for name in ("XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    assert files.cache_dir() == Path(expected)


@pytest.mark.parametrize("file", [files.MODEL, files.TOKENIZER, files.SKILLS])
def test_real_files_are_pinned_to_approved_https_addresses(file):
    url = httpx.URL(file.url)
    assert url.scheme == "https" and url.host in SETUP_HOSTS
    assert len(file.sha256) == 64 and file.size > 0
