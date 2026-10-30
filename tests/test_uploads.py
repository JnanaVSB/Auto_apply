"""Uploaded resumes and CVs: saving all or nothing, the checks (named by file, never quoting it), and Remove."""

import json

import pytest
from test_tiles import RESUME_CLS

from app.documents import uploads
from app.documents.uploads import UploadError

OTHER = RESUME_CLS.replace("Toy Driver", "Kite")  # another made-up resume, long enough to be one


def form(*files):
    return {
        f"file.{n}.{part}": value
        for n, file in enumerate(files)
        for part, value in zip(("name", "latex"), file, strict=True)
    }


def test_files_are_saved_and_offered_by_their_name(conn):
    assert uploads.upload(conn, form(("robotics-cv.tex", RESUME_CLS), ("Short Resume.tex", OTHER))) == 2
    files = uploads.offered(conn)
    assert [file.name for file in files] == ["robotics-cv", "Short Resume"] and files[0].latex == RESUME_CLS
    assert uploads.get(conn, files[1].id) == files[1] and uploads.get(conn, 999) is None


@pytest.mark.parametrize(
    ("files", "problem"),
    [
        ((), "Choose one or more .tex files first"),
        ((("big.tex", "x" * (uploads.MAX_BYTES + 1)),), "big: is over 1 MB"),
        ((("empty.tex", r"\begin{document}Hi\end{document}"),), "empty: looks empty"),
        ((("\x01.tex", RESUME_CLS),), "a file name must be 1 to 100 visible characters"),
        (tuple((f"cv{n}.tex", RESUME_CLS + "%" * n) for n in range(6)), "Upload at most 5 files at a time"),
        ((("one.tex", RESUME_CLS), ("two.tex", RESUME_CLS)), "two: already uploaded as one"),
        ((("cv.tex", RESUME_CLS), ("CV.tex", OTHER)), "CV: a file with this name is already uploaded"),
        ((("cv.pdf", RESUME_CLS),), "cv.pdf: only .tex files can be uploaded"),
        ((("cv", RESUME_CLS),), "cv: only .tex files can be uploaded"),
        ((("cv.tex", RESUME_CLS + "\ufffd" * (len(RESUME_CLS) // 50)),), "cv: doesn't look like a LaTeX text file"),
    ],
)
def test_a_bad_file_means_nothing_is_saved(conn, files, problem):
    with pytest.raises(UploadError, match=problem):
        uploads.upload(conn, form(*files))
    assert uploads.offered(conn) == []


def test_a_tex_name_in_capitals_and_a_few_odd_characters_are_fine(conn):
    assert uploads.upload(conn, form(("Robotics CV.TEX", RESUME_CLS.replace("Toy", "Toy\ufffd")))) == 1
    assert [file.name for file in uploads.offered(conn)] == ["Robotics CV"]


def test_a_file_over_the_size_limit_is_refused_before_it_is_read(conn, monkeypatch):
    monkeypatch.setattr(uploads, "plain_text", lambda latex: pytest.fail("read before the size check"))
    with pytest.raises(UploadError, match="big: is over 1 MB"):
        uploads.upload(conn, form(("big.tex", "x" * (uploads.MAX_BYTES + 1))))


def test_the_same_file_or_name_twice_across_uploads_is_refused(conn):
    uploads.upload(conn, form(("cv.tex", RESUME_CLS)))
    with pytest.raises(UploadError, match="other: already uploaded as cv"):
        uploads.upload(conn, form(("other.tex", RESUME_CLS)))
    with pytest.raises(UploadError, match="cv: a file with this name is already uploaded"):
        uploads.upload(conn, form(("cv.tex", OTHER)))


def test_remove_hides_a_file_keeps_it_and_takes_it_out_of_the_default(conn):
    uploads.upload(conn, form(("cv.tex", RESUME_CLS), ("short.tex", OTHER)))
    cv, short = uploads.offered(conn)
    with conn:
        conn.execute("INSERT INTO writer_sources VALUES (1, ?)", (json.dumps([cv.id]),))
    assert uploads.remove(conn, cv.id) and not uploads.remove(conn, cv.id)  # a second press changes nothing
    assert uploads.offered(conn) == [short]
    assert conn.execute("SELECT count(*) FROM resume_files").fetchone()[0] == 2  # kept
    assert json.loads(conn.execute("SELECT sources FROM writer_sources").fetchone()[0]) == ["profile"]
    uploads.upload(conn, form(("cv.tex", RESUME_CLS)))  # a removed file's name and text can be uploaded again
