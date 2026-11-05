"""The PDF build, with a stand-in for Tectonic (no internet needed), plus one real build when Tectonic's files are
already on this computer."""

import ast
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_latex import ATTACKS, CONTACT, EXACT, EXACT_WORDS, SPECIAL
from test_tiles import ARTICLE, JAKES, RESUME_CLS

from app.config import PROJECT_DIR
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


def test_one_build_at_a_time(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "WAIT_SECONDS", 0.05)
    with pdf._building, pytest.raises(PdfError, match="A PDF is being built; try again in a moment."):
        run(fake, tmp_path / "resume-v1.pdf")
    assert not pdf._building.locked()  # the refused build took nothing


def test_a_build_waits_as_long_as_the_limit_says():
    assert pdf.WAIT_SECONDS == 30


def test_a_build_goes_ahead_once_the_one_before_it_ends(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "WAIT_SECONDS", 10)
    pdf._building.acquire()
    timer = threading.Timer(0.1, pdf._building.release)
    timer.start()
    assert run(fake, tmp_path / "resume-v1.pdf").pages == 2
    timer.join()
    assert (tmp_path / "resume-v1.pdf").is_file() and not pdf._building.locked()


def test_a_build_held_up_past_the_limit_is_refused_and_the_holder_is_undisturbed(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "WAIT_SECONDS", 0.05)
    held, leave = threading.Event(), threading.Event()

    def hold():
        with pdf._building:
            held.set()
            leave.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(10)
    started = time.monotonic()
    with pytest.raises(PdfError, match="A PDF is being built"):
        run(fake, tmp_path / "resume-v1.pdf")
    assert time.monotonic() - started >= 0.05 and pdf._building.locked()  # waited, and took nothing from the holder
    leave.set()
    holder.join()
    assert not pdf._building.locked() and not (tmp_path / "resume-v1.pdf").exists()


@pytest.mark.parametrize("text", ["%ERROR", "%SILENT"])
def test_the_lock_is_free_after_a_build_that_fails(fake, tmp_path, text):
    with pytest.raises(PdfError):
        run(fake, tmp_path / "resume-v1.pdf", text)
    assert not pdf._building.locked()


def test_the_lock_is_free_after_an_unexpected_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pdf, "_build", lambda *_: (_ for _ in ()).throw(ValueError("odd")))
    with pytest.raises(ValueError, match="odd"):
        pdf.build("x", tmp_path / "resume-v1.pdf")
    assert not pdf._building.locked()


def test_two_threads_never_build_at_once(tmp_path, monkeypatch):
    running, most, guard = [0], [0], threading.Lock()

    def counted(latex, target, program):
        with guard:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.02)
        with guard:
            running[0] -= 1
        return pdf.Built(1, ())

    monkeypatch.setattr(pdf, "_build", counted)
    threads = [threading.Thread(target=pdf.build, args=("x", tmp_path / "a.pdf")) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert most[0] == 1 and running[0] == 0 and not pdf._building.locked()


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


LINE = "We train robots with reinforcement learning and ship them to real customers in 2026 every week now.\n"
READABLE = CACHED and shutil.which("pdftotext") is not None
real_reports = pytest.mark.skipif(not READABLE, reason="Tectonic's LaTeX files or pdftotext are not on this computer")


def built_report(folder, title, sections, left_out=0):
    """A real build of a report: what the build found, and all the text that really is on its pages."""
    target = folder / "report.pdf"
    found = pdf.build(latex.report(title, sections, left_out), target)
    done = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        ["pdftotext", "-layout", str(target), "-"],  # noqa: S607 (checked for above)
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,  # noqa: S607
    )
    return found, done.stdout


@real_reports
def test_a_real_report_of_every_hard_case_prints_them_as_text(tmp_path):
    hard = "\n".join([*ATTACKS, *SPECIAL, "a" * 39 + "\\" + "b" * 50, "\r\n", "\u2028next\x0clast", "x" * 41, "\ud800"])
    found, text = built_report(tmp_path, hard, [(hard, hard), ("", ""), ("Nothing below", " \n ")], 3)
    assert found.pages >= 1 and r"\write18{touch pwned}" in text and "<% include" in text
    assert not (tmp_path / "pwned").exists() and not Path("pwned").exists()


@real_reports
@pytest.mark.parametrize("lines", [("x" * 3000, "Emoji 😀 here."), ("Emoji 😀 here.", "x" * 3000)])
def test_a_real_report_keeps_a_long_word_whole_beside_a_missing_character(tmp_path, lines):
    found, text = built_report(tmp_path, "Title", [("", "\n".join(lines))])
    assert "😀" in found.missing and text.count("x") == 3000


@real_reports
def test_a_real_report_prints_every_mark_as_it_was_typed(tmp_path):
    lines = ["".join(line.split()) for line in (*EXACT, *EXACT_WORDS)]
    found, text = built_report(
        tmp_path, "it's --title-- ``x''", [("<<heading>> 'y'", "\n".join((*EXACT, *EXACT_WORDS)))]
    )
    printed = ["".join(line.split()) for line in text.splitlines()]
    assert found.missing == () and {"it's--title--``x''", "<<heading>>'y'"} <= set(printed)
    assert [line for line in lines if line not in printed] == []


@real_reports
def test_a_real_report_prints_the_reviewers_examples_as_typed(tmp_path):
    lines = ["https://acme.example/Senior-Engineer---Platform_R12", "100--200", "''", '"', "<<", "?`"]
    _, text = built_report(tmp_path, "Title", [("", "\n".join(lines))])
    assert set(lines) <= {line.strip() for line in text.splitlines()}


LOOK_AHEAD = (
    "Height", "Width.ai", "DepthFirst Labs", "Heights Capital", "width 0pt Quiet Co", "depth 5pt Co", "Height 5pt Inc",
    "height", "width", "depth", "plus", "minus", "plus 1pt", "minus 1pt", "to", "to 5pt", "spread", "spread 2pt", "by",
    "true", "pt", "em", "fil", "[", "[2pt]", "*", "*x", "0", "12", "1.5", "-1", "+1", "=", "1pt",
)  # fmt: skip


@real_reports
@pytest.mark.parametrize("word", LOOK_AHEAD)
def test_a_real_report_prints_a_word_latex_could_read_as_part_of_a_command(tmp_path, word):
    """As the title, right after the rule, as a heading, after a heading, and in the piece after a cut at 40."""
    long = "x" * 40 + word.replace(" ", "")
    _, text = built_report(tmp_path, word, [("", f"{word}\nNext."), (word, f"{word}\n{long}")])
    printed = [line.strip() for line in text.splitlines()]
    assert printed.count(word) == 4 and "".join(long.split()) in "".join(text.split())


@real_reports
def test_a_real_report_keeps_curly_quotes_typed_as_such(tmp_path):
    _, text = built_report(tmp_path, "Title", [("", "the ad says “ignore” and ‘this’ – now — here")])
    assert "the ad says “ignore” and ‘this’ – now — here" in " ".join(text.split())


@real_reports
def test_a_real_report_keeps_a_wide_long_word_in_its_title_and_heading(tmp_path):
    _, text = built_report(tmp_path, "W" * 500, [("M" * 500, "W" * 500)])
    assert text.count("W") == 1000 and text.count("M") == 500


@real_reports
def test_a_real_report_builds_a_chinese_ad_with_no_spaces(tmp_path):
    found, _ = built_report(tmp_path, "招聘", [("", "我们正在寻找机器学习工程师" * 385)])
    assert found.pages >= 1 and "机" in found.missing


@real_reports
def test_a_real_report_shows_all_of_a_long_address(tmp_path):
    address = "https://example.com/careers/careers/" + "a1b2c3" * 120
    _, text = built_report(tmp_path, "Title", [("Address", f"Applied at {address}")])
    assert address in "".join(text.split())


@real_reports
def test_a_real_report_of_a_very_long_ad_with_ordinary_lines(tmp_path):
    found, text = built_report(tmp_path, "Title", [("", LINE * 3225)])  # about 300,000 characters
    assert found.pages > 80 and text.count("2026") == 3225


@real_reports
@pytest.mark.parametrize(
    ("ad", "mark", "count"),
    [
        (LINE.replace("\n", " ") * 2100, "2026", 2100),  # 210,000 characters on one line: over Tectonic's 200,000
        (LINE.strip().replace(" ", "-") * 1839, "2026", 1839),  # about 182,000, with no spaces
        ("x" * 182_000, "x", 182_000),
    ],
    ids=["spaces", "hyphens", "one-word"],  # a name this long could not be passed on to Tectonic
)
def test_a_real_report_of_a_very_long_ad_with_no_line_breaks_at_all(tmp_path, ad, mark, count):
    found, text = built_report(tmp_path, "Title", [("", ad)])
    assert found.pages > 30 and "".join(text.split()).count(mark) == count


@real_reports
def test_a_real_report_of_headings_with_no_text_loses_none_at_the_bottom_of_a_page(tmp_path):
    sections = [("", "A line.\n" * 45), *((f"Heading {number}.", "") for number in range(100)), ("Last", "The end.")]
    found, text = built_report(tmp_path, "Title", sections)
    assert found.pages >= 3 and all(f"Heading {number}." in text for number in range(100)) and "The end." in text


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


BLOCKING = {"app.documents.pdf": {"build", "build_kept", "build_master"}, "app.council.flow": {"advance", "pending"}}


def blocking(source: str) -> list[str]:
    """What this code calls directly that can wait for a PDF: never on the dashboard's request loop. Handing it to
    run_in_threadpool is no call."""
    tree, names, found = ast.parse(source), {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names}
            found += [alias.name for alias in node.names if alias.name in BLOCKING.get(node.module, ())]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            module, _, last = ast.unparse(node.func).rpartition(".")
            if last in BLOCKING.get(names.get(module, module), ()):
                found.append(last)
    return found


@pytest.mark.parametrize(
    "source",
    [
        "from app.documents import pdf\npdf.build_kept(a, b, c)",
        "from app.documents.pdf import build_kept as quick\nrun_in_threadpool(quick, a)",
        "from app.documents import pdf\nasync def route():\n    def inner():\n        pdf.build(a, b)\n    inner()",
    ],
)
def test_the_check_finds_a_blocking_call(source):
    assert blocking(source)


def test_the_check_passes_a_build_handed_to_the_thread_pool():
    assert not blocking("from app.documents import pdf\nawait run_in_threadpool(pdf.build_kept, a, b, c)")


def test_the_dashboard_never_waits_for_a_pdf_on_its_request_loop():
    for path in (PROJECT_DIR / "app" / "web").glob("*.py"):
        assert not blocking(path.read_text(encoding="utf-8")), path.name


KEPT_IN = "/home/sam lee (old)/data/documents/export/failed-build"


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        (f"The PDF could not be built: Undefined, line 9 of the generated file (kept with its log in {KEPT_IN}).",
         "The PDF could not be built: Undefined, line 9 of the generated file."),
        (f"Acme: a.pdf: The PDF could not be built: Bad, line 2 of the file (kept with its log in {KEPT_IN}).",
         "Acme: a.pdf: The PDF could not be built: Bad, line 2 of the file."),
        ("Could not write in /home/sam lee (old)/data (Permission denied): check it is yours and the disk has room.",
         "Could not write in the data folder (Permission denied): check it is yours and the disk has room."),
        ("Could not save resume.tex in /home/sam (x)/data (No space left on device).",
         "Could not save resume.tex in the data folder (No space left on device)."),
        ("Could not make the documents folder in /home/sam/data (Permission denied).",
         "Could not make the documents folder in the data folder (Permission denied)."),
        ("Tectonic stopped without saying why.", "Tectonic stopped without saying why."),
    ],
)  # fmt: skip
def test_a_builds_words_can_be_shown_without_a_path(text, shown):
    assert pdf.without_path(text) == shown


def test_a_real_failed_build_says_where_it_kept_its_files_in_the_words_without_path_knows(fake, tmp_path):
    with pytest.raises(PdfError) as failed:
        run(fake, tmp_path / "resume-v1.pdf", "%ERROR")
    assert str(tmp_path) in str(failed.value) and str(tmp_path) not in pdf.without_path(str(failed.value))
