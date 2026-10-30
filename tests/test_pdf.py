"""The PDF build, with a stand-in for Tectonic (no internet needed), plus one real build when Tectonic's files are
already on this computer."""

import errno
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from test_latex import CONTACT
from test_tiles import ARTICLE, JAKES, RESUME_CLS

from app.documents import latex, pdf
from app.documents.pdf import PdfError
from app.documents.tiles import read_tiles

FAKE = """#!{python} -I
import json, sys, time
from pathlib import Path
args = sys.argv[1:]
out, source = Path(args[args.index("--outdir") + 1]), Path(args[-1])
Path({record!r}).write_text(json.dumps(args))
text = source.read_text()
if "%ERROR" in text:
    (out / "resume.log").write_text("! Undefined control sequence.")
    sys.exit(print("error: resume.tex:3: Undefined control sequence\\nl.3 \\\\secret resume words", file=sys.stderr))
if "%OFFLINE" in text:
    sys.exit(print("error: we couldn't get it from the internet: https://data1b.fullyjustified.net/x", file=sys.stderr))
if "%SILENT" in text:
    sys.exit(1)
if "%SLOW" in text:
    time.sleep(5)
(out / "resume.pdf").write_bytes(b"%PDF-fake")
(out / "resume.log").write_text("Missing character: There is no \\u2603 (U+2603) in font x;!\\n"
    "Output written on resume.xdv (2 pages, 10 bytes).\\n")
"""


def fake_tectonic(folder: Path) -> Path:
    """The stand-in program: a good build gives 2 pages with one character the font lacks."""
    program = folder / "tectonic"
    program.write_text(FAKE.format(python=sys.executable, record=str(folder / "args.json")))
    program.chmod(0o700)
    return program


@pytest.fixture
def fake(tmp_path):
    return fake_tectonic(tmp_path)


def run(fake, target, text="%OK"):
    return pdf.build(rf"\documentclass{{article}}{text}", target, fake)


def args(fake):
    return json.loads((fake.parent / "args.json").read_text())


def test_a_good_build_moves_the_pdf_in_and_reads_the_log(fake, tmp_path):
    target = tmp_path / "documents" / "master" / "resume-v1.pdf"
    target.parent.parent.mkdir()
    assert run(fake, target) == pdf.Built(2, ("☃",))
    assert target.read_bytes() == b"%PDF-fake" and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert [path.name for path in target.parent.iterdir()] == ["resume-v1.pdf"]  # no build folder left behind
    used = args(fake)
    assert used[:4] == ["--untrusted", "--bundle", pdf.BUNDLE, "--outdir"] and "--keep-logs" in used


def test_a_latex_error_names_its_kind_and_line_and_keeps_the_files(fake, tmp_path):
    target = tmp_path / "resume-v1.pdf"
    target.write_bytes(b"older")
    with pytest.raises(PdfError) as error:
        run(fake, target, "%ERROR")
    assert "Undefined control sequence, line 3 of the generated file" in str(error.value)
    assert "secret" not in str(error.value)  # Tectonic's own output quotes the resume; the message never does
    kept = tmp_path / pdf.FAILED
    assert (kept / "resume.tex").read_text().endswith("%ERROR") and (kept / "resume.log").is_file()
    assert stat.S_IMODE(kept.stat().st_mode) == 0o700 and target.read_bytes() == b"older"  # never half-replaced
    run(fake, target)
    assert not kept.exists()  # deleted at the next good build


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("%OFFLINE", "Tectonic needs to download its LaTeX files once from data1b.fullyjustified.net"),
        ("%SILENT", "Tectonic stopped without saying why"),
    ],
)
def test_a_failed_download_and_a_silent_failure_say_what_to_do(fake, tmp_path, text, problem):
    with pytest.raises(PdfError, match=problem):
        run(fake, tmp_path / "resume-v1.pdf", text)
    assert not (tmp_path / pdf.FAILED).exists()


def test_a_build_over_the_time_limit_is_stopped(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "TIMEOUT_SECONDS", 1)
    with pytest.raises(PdfError, match="was stopped"):
        run(fake, tmp_path / "resume-v1.pdf", "%SLOW")
    assert list(tmp_path.glob(f"{pdf.BUILDING}*")) == []


def test_no_tectonic_says_how_to_install_it(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "tectonic", lambda: None)
    with pytest.raises(PdfError, match="install it with: conda install -n auto_apply -c conda-forge tectonic=0.17.0"):
        pdf.build("x", tmp_path / "resume-v1.pdf")


def test_one_build_at_a_time(fake, tmp_path):
    with pdf._building, pytest.raises(PdfError, match="A PDF is being built"):
        run(fake, tmp_path / "resume-v1.pdf")


def test_an_interrupted_builds_folder_is_cleaned_up_but_not_one_still_running(fake, tmp_path):
    old, busy = tmp_path / f"{pdf.BUILDING}old", tmp_path / f"{pdf.BUILDING}busy"  # busy: another dashboard's build
    old.mkdir()
    busy.mkdir()
    long_ago = time.time() - pdf.LEFT_OVER_SECONDS - 1
    os.utime(old, (long_ago, long_ago))
    (tmp_path / f"{pdf.BUILDING}gone").symlink_to(tmp_path / "removed meanwhile")  # gone before it is looked at
    run(fake, tmp_path / "resume-v1.pdf")
    assert sorted(tmp_path.glob(f"{pdf.BUILDING}*")) == [busy, tmp_path / f"{pdf.BUILDING}gone"]


def test_the_master_pdf_remembers_what_its_build_found(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "tectonic", lambda: fake)
    assert pdf.built(pdf.master(tmp_path, 3)) is None
    pdf.build_master(tmp_path, 3, "%OK")
    assert pdf.built(pdf.master(tmp_path, 3)) == pdf.Built(2, ("☃",))
    assert stat.S_IMODE((tmp_path / "documents").stat().st_mode) == 0o700


CACHED = pdf.tectonic() is not None and any(
    (Path.home() / folder).is_dir() for folder in (".cache/tectonic", "Library/Caches/Tectonic")
)


@pytest.mark.skipif(not CACHED, reason="Tectonic or its downloaded LaTeX files are not on this computer yet")
def test_a_real_build_of_a_resume_full_of_special_characters(tmp_path):
    tiles = [*read_tiles(RESUME_CLS).tiles, *read_tiles(JAKES).tiles, *read_tiles(ARTICLE).tiles]
    built = pdf.build(latex.resume(CONTACT | {"full_name": "Alex $Ex&mple_%#"}, tiles), tmp_path / "resume.pdf")
    assert built.pages >= 1 and (tmp_path / "resume.pdf").read_bytes().startswith(b"%PDF")
    assert not (tmp_path / "pwned").exists()


def test_a_folder_that_cannot_be_written_or_a_full_disk_names_the_folder(fake, tmp_path, monkeypatch):
    (tmp_path / "documents").write_text("a file where the folder should be")
    with pytest.raises(PdfError, match=r"Could not write in .*documents \("):
        run(fake, tmp_path / "documents" / "resume-v1.pdf")

    real_open = pdf.os.open

    def full(path, *args, **kwargs):  # the disk is full for the files the build writes
        if str(path).endswith(".partial"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(pdf.os, "open", full)
    with pytest.raises(PdfError, match=r"Could not save resume.tex in .* \(No space left on device\)"):
        run(fake, tmp_path / "resume-v1.pdf")
    assert list(tmp_path.glob(f"{pdf.BUILDING}*")) == [] and list(tmp_path.glob("*.partial")) == []
