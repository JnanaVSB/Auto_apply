"""Where an export may go, what its folders are called and how its files are written: all on a made-up home, project
and data folder in a temporary folder, never the real ones."""

import hashlib
import os
import stat
import unicodedata

import pytest

from app.export import folder

OLD = b"2026-10-17  Applied\n"
NEW = OLD + b"2026-11-02  Interview, Round 1\n"
TODAY = "2026-11-05"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A made-up home holding the project, its data folder inside, and the tests' own working room."""
    found = tmp_path / "people" / "sam"
    (found / "code" / "auto_apply" / "data").mkdir(parents=True)
    (found / "code" / "auto_apply" / ".git").mkdir()
    monkeypatch.setenv("HOME", str(found))
    monkeypatch.setenv("USERPROFILE", str(found))
    monkeypatch.setattr(folder, "PROJECT_DIR", found / "code" / "auto_apply")
    return found


@pytest.fixture
def project(home):
    return home / "code" / "auto_apply"


@pytest.fixture
def data(project):
    return project / "data"


@pytest.fixture
def loose_umask():
    """Anything the code leaves to the system's default would come out open to everyone."""
    old = os.umask(0)
    yield
    os.umask(old)


@pytest.fixture
def root(tmp_path):
    found = tmp_path / "exports"
    found.mkdir()
    return found


def rights(path):
    return stat.S_IMODE(path.lstat().st_mode)


def names(root):
    return sorted(path.name for path in root.iterdir())


def refused(text, data, check=folder.chosen, words=""):
    with pytest.raises(folder.Refused, match=words or None) as refusal:
        check(str(text), data)
    assert str(text) not in str(refusal.value) or not str(text).strip()  # never the path


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_nothing_typed_says_choose_a_folder_first(text, data):
    with pytest.raises(folder.Refused, match="Choose a folder first"):
        folder.chosen(text, data)


@pytest.mark.parametrize("text", ["relative/path", "./x", "..", "x", " /starts/with/a/space"])
def test_a_path_that_isnt_full_is_refused(text, data):
    refused(text, data, words="full path")


@pytest.mark.parametrize("text", ["/srv/a\x00b", "/srv/a\nb", "/srv/a\rb", "/srv/a\x7fb", "/srv/a\tb", "/srv/a\n"])
def test_a_control_character_is_refused(text, data):
    refused(text, data, words="control character")


@pytest.mark.parametrize("text", ["~", "~/", "~/."])
def test_your_home_folder_is_refused_however_it_is_typed(text, home, data):
    refused(text, data, words="home folder")


def test_an_unknown_persons_home_is_refused_not_a_crash(data):
    refused("~nobody-here/x", data)


def test_everything_above_home_and_home_itself_is_refused(home, data):
    for found in (home, home.parent, home.parent.parent, "/"):
        refused(found, data, words="home folder")


def test_the_project_inside_it_and_above_it_are_refused(project, data):
    (project / "app" / "deep").mkdir(parents=True)
    for found in (project, project / "app", project / "app" / "deep", project / "app" / "new" / "x", project / ".git"):
        refused(found, data, words="project")
    refused(project.parent, data, words="home folder")  # holds the project


def test_the_data_folder_inside_it_and_above_it_are_refused(tmp_path, home):
    outside = tmp_path / "elsewhere" / "private" / "data"  # outside the project and home
    (outside / "documents").mkdir(parents=True)
    for found in (outside, outside / "documents", outside / "new"):
        refused(found, outside, words="data folder")
    (home / "into-data").symlink_to(outside / "documents")
    (home / "link-to-link").symlink_to(home / "into-data")
    refused(home / "link-to-link", outside, words="data folder")
    refused(outside.parent, outside, words="home folder")
    refused(outside.parent.parent, outside, words="home folder")


def test_a_git_project_is_refused_at_every_depth(home, data):
    other = home / "other-repo"
    (other / ".git").mkdir(parents=True)
    (other / "a" / "b" / "c").mkdir(parents=True)
    for found in (other, other / "a" / "b" / "c", other / "a" / "new"):
        refused(found, data, words="git project")


def test_a_git_worktree_whose_git_is_a_file_is_refused(home, data):
    worktree = home / "worktree"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    refused(worktree / "src", data, words="git project")


def test_a_broken_link_named_git_still_marks_a_git_project(home, data):
    other = home / "other"
    other.mkdir()
    (other / ".git").symlink_to(home / "gone")
    refused(other, data, words="git project")


def test_links_are_followed_before_the_check(home, project, data):
    (home / "into-project").symlink_to(project / "docs")
    (home / "to-home").symlink_to(home)
    (home / "into-data").symlink_to(data / "documents")
    (home / "link-to-link").symlink_to(home / "into-data")
    refused(home / "into-project", data, words="project")  # even though nothing is there yet
    refused(home / "to-home", data, words="home folder")
    refused(home / "link-to-link", data, words="project")  # this data folder is inside the project
    refused(home / "link-to-link" / "new", data, words="project")


def test_a_broken_link_and_a_link_loop_are_refused(home, data):
    (home / "broken").symlink_to(home / "gone")
    (home / "one").symlink_to(home / "two")
    (home / "two").symlink_to(home / "one")
    refused(home / "broken", data)
    refused(home / "one", data)
    refused(home / "one" / "x", data)


def test_a_file_and_a_path_through_a_file_are_refused(home, data):
    (home / "file.txt").write_text("x", encoding="utf-8")
    refused(home / "file.txt", data, words="file")
    refused(home / "file.txt" / "x", data, words="file")


def test_a_path_too_long_is_refused_not_a_crash(home, data):
    refused(home / ("x" * 5000), data)
    refused("/".join(["", *["long-name"] * 900]), data)


@pytest.mark.parametrize("closed", [0, 0o300, 0o100])
def test_a_folder_you_have_no_rights_to_is_refused_not_a_crash(home, data, closed):
    locked = home / "locked"
    (locked / "inside").mkdir(parents=True)
    locked.chmod(closed)
    try:
        refused(locked, data, words="isn't open to you")
        if closed == 0:
            refused(locked / "inside", data, words="isn't open to you")
            refused(locked / "new", data, words="isn't open to you")
    finally:
        locked.chmod(0o700)


def test_the_same_folder_under_another_name_is_refused(home, project, data, monkeypatch):
    """A disk that ignores case, or a folder mounted twice: the names differ, the folder on disk is the same one."""
    other = home / "PROJECT-ON-ANOTHER-MOUNT"
    other.mkdir()
    real = os.path.samefile
    pair = {str(other), str(project)}
    monkeypatch.setattr(folder.os.path, "samefile", lambda a, b: real(a, b) or {str(a), str(b)} == pair)
    refused(other, data, words="project")
    refused(other / "new", data, words="project")


def test_ordinary_folders_are_accepted(home, data):
    (home / "plain").mkdir()
    assert folder.chosen(str(home / "plain"), data) == home / "plain"
    assert folder.chosen(str(home / "code" / "exports"), data) == home / "code" / "exports"  # beside the project, new
    assert folder.chosen("~/my exports 导出", data) == home / "my exports 导出"
    assert folder.chosen(str(home / "code" / "auto_apply" / ".." / "exports"), data) == home / "code" / "exports"


def test_a_link_to_an_ordinary_folder_is_accepted_as_the_real_folder(home, data):
    (home / "real").mkdir()
    (home / "shortcut").symlink_to(home / "real")
    assert folder.chosen(str(home / "shortcut"), data) == home / "real"


def test_saving_makes_a_new_last_part_for_you_only(home, data, loose_umask):
    made = folder.make(str(home / "exports"), data)
    assert made == home / "exports" and made.is_dir() and rights(made) == 0o700


def test_saving_twice_is_fine(home, data):
    assert folder.make(str(home / "exports"), data) == folder.make(str(home / "exports"), data)


def test_saving_refuses_a_deeper_new_path_and_makes_nothing(home, data):
    refused(home / "new" / "deeper", data, check=folder.make, words="last part")
    assert not (home / "new").exists()


def test_saving_leaves_a_folder_you_made_as_it_is(home, data):
    (home / "mine").mkdir(mode=0o755)
    (home / "mine").chmod(0o755)
    folder.make(str(home / "mine"), data)
    assert rights(home / "mine") == 0o755


def test_saving_refuses_a_file(home, data):
    (home / "file.txt").write_text("x", encoding="utf-8")
    refused(home / "file.txt", data, check=folder.make, words="file")
    assert (home / "file.txt").read_text(encoding="utf-8") == "x"


def test_saving_where_it_cant_write_gives_the_reason(home, data):
    (home / "read-only").mkdir()
    (home / "read-only").chmod(0o500)
    try:
        refused(home / "read-only" / "new", data, check=folder.make, words="Permission denied")
    finally:
        (home / "read-only").chmod(0o700)


def test_a_run_finds_the_folder_and_never_makes_it(home, data):
    (home / "plain").mkdir()
    assert folder.ready(str(home / "plain"), data) == home / "plain"
    refused(home / "unplugged", data, check=folder.ready, words="plug the drive in")
    assert not (home / "unplugged").exists()


def test_a_run_refuses_a_file_and_a_folder_git_init_reached_later(home, data):
    (home / "file.txt").write_text("x", encoding="utf-8")
    refused(home / "file.txt", data, check=folder.ready)
    folder.make(str(home / "exports"), data)
    (home / "exports" / ".git").mkdir()  # after it was saved
    refused(home / "exports", data, check=folder.ready, words="git project")


def test_a_link_to_an_unplugged_drive_says_to_plug_it_in(home, data):
    (home / "usb").symlink_to(home / "media" / "usb")
    refused(home / "usb", data, check=folder.ready, words="plug the drive in")


ODD_NAMES = [
    ("../../etc/passwd", "Engineer"), ("..", ".."), (".", "."), ("/", "/"), ("\\", "\\"), ("C:\\x", "D:\\y"),
    ("\"Acme\" 'Inc'", "`ML`"), ("<>|?*:", "a<b>c|d?e*f:g"), ("Acme. . .", "Engineer...   "), ("   ", "\t"),
    ("Acme\x00Inc", "ML\x00"), ("Acme\tInc", "ML\nEngineer\r\n"), ("Acme\u202eInc", "\u202eML"), ("A\u200dB", "\u200d"),
    ("Acme 🚀", "🤖 ML"), ("字节跳动", "机器学习工程师"),
    ("x" * 500, "y" * 500), ("字" * 500, "节" * 500), ("😀" * 500, ""),
    ("", ""), ("!!!", "???"), ("Acme", ""), ("", "Engineer"), ("a" * 54 + "-b", "c"), ("a" * 54 + " " + "b" * 30, "c"),
    ("e\u0301" * 200, "\u0301" * 200), ("\u0301", "\u0301"), ("/\u0338", ".\u0301"), ("..\u0301", "\u0301/\u0301"),
    ("\u0301..", "\\\u0338"), ("\u0301/etc/passwd", "\u0300\\"), ("CON", ""), ("Bell\x07", "\x1b[31m"),
    ("Fullwidth／Slash", "．．"), ("\ud800", "\udfff"),
]  # fmt: skip


@pytest.mark.parametrize(("company", "role"), ODD_NAMES)
def test_every_name_is_safe_on_every_computer(company, role):
    found = folder.name(company, role)
    assert found and len(found.encode()) <= 110
    assert found not in (".", "..") and found[0] not in ". -_" and found[-1] not in ". -"
    assert not set(found) & set('/\\:*?"<>|')
    assert not [character for character in found if unicodedata.category(character)[0] in "CZ"]
    assert unicodedata.category(found[0])[0] in "LN"  # a mark never stands first, with nothing to belong to
    assert found == unicodedata.normalize("NFC", found)


@pytest.mark.parametrize(
    ("company", "role", "expected"),
    [
        ("Acme", "ML Engineer", "Acme_ML-Engineer"),
        ("Acme, Inc.", "ML / AI Engineer (Remote)", "Acme-Inc_ML-AI-Engineer-Remote"),
        ("under_score", "a_b", "under-score_a-b"),
        ("Acme", "", "Acme"),
        ("", "Engineer", "Engineer"),
        ("", "", "application"),
        ("!!!", "???", "application"),
        ("../../etc/passwd", "x", "etc-passwd_x"),
        ("字节跳动", "机器学习工程师", "字节跳动_机器学习工程师"),
        ("Café", "Rôle", "Café_Rôle"),
        ("हिन्दी कंपनी", "इंजीनियर", "हिन्दी-कंपनी_इंजीनियर"),
        ("தமிழ்", "பொறியாளர்", "தமிழ்_பொறியாளர்"),
        ("CON", "", "CON_"),
        ("con", "", "con_"),
        ("", "Nul", "Nul_"),
        ("LPT9", "", "LPT9_"),
        ("COM1", "", "COM1_"),
        ("aux", "", "aux_"),
        ("PRN", "", "PRN_"),
        ("CON", "Engineer", "CON_Engineer"),
        ("COM0", "", "COM0"),
        ("CONSOLE", "", "CONSOLE"),
    ],
)
def test_names(company, role, expected):
    assert folder.name(company, role) == expected


def test_both_ways_of_writing_an_accent_give_one_name():
    assert folder.name("Caf\u00e9", "") == folder.name("Cafe\u0301", "") == "Caf\u00e9"


def test_a_mark_with_nothing_to_belong_to_is_dropped():
    assert folder.name("\u0301Acme", "") == "Acme"
    assert folder.name("a-\u0301b", "") == "a-b"


def test_a_long_company_leaves_room_for_the_role():
    company, role = folder.name("x" * 500, "y" * 500).split("_")
    assert len(company) == 55 and len(company) + 1 + len(role) == 110


def test_a_short_company_gives_its_room_to_the_role():
    assert folder.name("Acme", "y" * 500) == "Acme_" + "y" * 105


def test_a_cut_never_breaks_a_character_or_ends_on_a_dash():
    assert folder.name("字" * 500, "") == "字" * 18  # 54 of the company's 55 bytes
    assert folder.name("a" * 54 + "-b", "c") == "a" * 54 + "_c"
    assert folder.name("", "a" * 109 + "-b") == "a" * 109


def test_a_free_name_is_the_first_with_nothing_there(root):
    assert folder.free(root, TODAY, "Acme_ML", []) == f"{TODAY}/Acme_ML"
    (root / TODAY / "Acme_ML").mkdir(parents=True)
    assert folder.free(root, TODAY, "Acme_ML", []) == f"{TODAY}/Acme_ML_2"
    (root / TODAY / "Acme_ML_2").mkdir()
    assert folder.free(root, TODAY, "Acme_ML", []) == f"{TODAY}/Acme_ML_3"


def test_a_name_kept_for_another_application_isnt_free_though_its_folder_is_gone(root):
    assert folder.free(root, TODAY, "Acme_ML", [f"{TODAY}/Acme_ML"]) == f"{TODAY}/Acme_ML_2"
    taken = [f"{TODAY}/Acme_ML", f"{TODAY}/Acme_ML_2"]
    assert folder.free(root, TODAY, "Acme_ML", taken) == f"{TODAY}/Acme_ML_3"


def test_a_name_kept_in_another_case_isnt_free(root):
    assert folder.free(root, TODAY, "Acme_ML", [f"{TODAY}/ACME_ml"]) == f"{TODAY}/Acme_ML_2"
    assert folder.free(root, TODAY, "STRASSE", [f"{TODAY}/straße"]) == f"{TODAY}/STRASSE_2"


def test_the_same_name_on_another_day_is_free(root):
    assert folder.free(root, TODAY, "Acme_ML", ["2026-11-04/Acme_ML"]) == f"{TODAY}/Acme_ML"


def test_a_broken_link_or_a_file_with_that_name_isnt_free(root):
    (root / TODAY).mkdir()
    (root / TODAY / "Acme_ML").symlink_to(root / "gone")
    (root / TODAY / "Acme_ML_2").write_text("x", encoding="utf-8")
    assert folder.free(root, TODAY, "Acme_ML", []) == f"{TODAY}/Acme_ML_3"


def test_the_longest_name_stays_within_120_bytes_with_its_number(root):
    base = folder.name("字" * 500, "节" * 500)
    taken = [f"{TODAY}/{base}"] + [f"{TODAY}/{base}_{number}" for number in range(2, 12)]
    found = folder.free(root, TODAY, base, taken).split("/")[1]
    assert found.endswith("_12") and len(found.encode()) <= 120


def test_entering_makes_both_folders_for_you_only(root, loose_umask):
    made = folder.enter(root, f"{TODAY}/Acme_ML")
    assert made == root / TODAY / "Acme_ML" and made.is_dir()
    assert rights(made) == 0o700 and rights(made.parent) == 0o700


def test_entering_again_keeps_what_is_there_and_clears_partials_only(root):
    made = folder.enter(root, f"{TODAY}/Acme_ML")
    (made / "resume.pdf").write_bytes(b"whole")
    (made / "resume.pdf.partial").write_bytes(b"ha")
    (made / "status_history.txt.partial").symlink_to(root / "gone")
    folder.enter(root, f"{TODAY}/Acme_ML")
    assert sorted(path.name for path in made.iterdir()) == ["resume.pdf"]
    assert (made / "resume.pdf").read_bytes() == b"whole"


def test_entering_clears_only_the_app_s_own_partials(root):
    made = folder.enter(root, f"{TODAY}/Acme_ML")
    (made / "notes.partial").write_text("yours", encoding="utf-8")
    (made / "report.docx.partial").write_text("yours", encoding="utf-8")
    (made / "old.pdf.partial").mkdir()  # a folder is never the app's
    (made / "resume.pdf.partial").write_bytes(b"ha")
    (made / "status_history-2026-11-05.txt.partial").write_bytes(b"ha")
    folder.enter(root, f"{TODAY}/Acme_ML")
    assert names(made) == ["notes.partial", "old.pdf.partial", "report.docx.partial"]


def test_clearing_a_partial_link_never_touches_what_it_points_at(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("yours", encoding="utf-8")
    made = folder.enter(root, f"{TODAY}/Acme_ML")
    (made / "resume.pdf.partial").symlink_to(outside)
    folder.enter(root, f"{TODAY}/Acme_ML")
    assert outside.read_text(encoding="utf-8") == "yours" and not (made / "resume.pdf.partial").is_symlink()


def test_a_date_folder_that_is_a_link_is_refused_and_nothing_made_through_it(root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / TODAY).symlink_to(elsewhere)
    with pytest.raises(folder.Refused):
        folder.enter(root, f"{TODAY}/Acme_ML")
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("link", ["gone", "2026-11-05"])
def test_a_date_folder_that_is_a_broken_link_or_a_loop_is_refused_not_a_crash(root, link):
    (root / TODAY).symlink_to(root / link)
    with pytest.raises(folder.Refused):
        folder.enter(root, f"{TODAY}/Acme_ML")


@pytest.mark.parametrize("inside", [True, False])
def test_an_application_folder_that_is_a_link_is_refused_inside_the_root_or_out(root, tmp_path, inside):
    target = (root / "other") if inside else (tmp_path / "elsewhere")
    target.mkdir()
    (target / "old.partial").write_bytes(b"not yours to clear")
    (root / TODAY).mkdir()
    (root / TODAY / "Acme_ML").symlink_to(target)
    with pytest.raises(folder.Refused):
        folder.enter(root, f"{TODAY}/Acme_ML")
    assert (target / "old.partial").exists()


def test_a_file_where_a_folder_should_be_is_refused(root):
    (root / TODAY).write_text("x", encoding="utf-8")
    with pytest.raises(folder.Refused):
        folder.enter(root, f"{TODAY}/Acme_ML")


@pytest.mark.parametrize(
    "relative", ["", ".", "./", "/", "/srv/absolute", "../outside", "2026-11-05/../../outside", "a/../../b", "a/.."]
)
def test_a_saved_folder_that_isnt_a_plain_path_inside_is_refused_with_nothing_made_or_cleared(root, tmp_path, relative):
    (root / "resume.pdf.partial").write_bytes(b"left in the chosen folder itself")
    before = sorted(tmp_path.rglob("*"))
    with pytest.raises(folder.Refused, match="saved folder"):
        folder.enter(root, relative)
    assert sorted(tmp_path.rglob("*")) == before


def test_a_saved_folder_written_as_a_full_path_is_refused_even_inside_the_chosen_folder(root):
    with pytest.raises(folder.Refused, match="saved folder"):
        folder.enter(root, str(root / TODAY / "Acme_ML"))
    assert list(root.iterdir()) == []


def test_a_chosen_folder_reached_through_a_link_is_fine(root, tmp_path):
    (tmp_path / "shortcut").symlink_to(root)
    made = folder.enter(tmp_path / "shortcut", f"{TODAY}/Acme_ML")
    assert (root / TODAY / "Acme_ML").is_dir() and made.resolve() == root / TODAY / "Acme_ML"


def test_missing_means_nothing_at_all_there(root):
    assert folder.missing(root / "nothing")
    (root / "broken").symlink_to(root / "gone")
    (root / "a-folder").mkdir()
    (root / "a-file").write_bytes(b"")
    assert not folder.missing(root / "broken")
    assert not folder.missing(root / "a-folder")
    assert not folder.missing(root / "a-file")


def test_a_file_is_written_for_you_only_from_the_first_moment(root, loose_umask, monkeypatch):
    seen = []
    swap = os.replace

    def replace(source, target):
        seen.append(rights(source))
        swap(source, target)

    monkeypatch.setattr(folder.os, "replace", replace)
    folder.write(root / "a.txt", b"text")
    folder.write(root / "b.txt", b"text")
    assert seen == [0o600, 0o600] and rights(root / "a.txt") == 0o600 and rights(root / "b.txt") == 0o600
    assert (root / "b.txt").read_bytes() == b"text"
    assert sorted(path.name for path in root.iterdir()) == ["a.txt", "b.txt"]


def test_rights_never_come_from_a_chmod_afterwards(root, monkeypatch):
    monkeypatch.setattr(folder.os, "chmod", lambda *_: pytest.fail("rights are set on creation"))
    monkeypatch.setattr(folder.Path, "chmod", lambda *_: pytest.fail("rights are set on creation"))
    folder.write(root / "a.txt", b"text")


def test_a_planted_partial_link_is_never_written_through(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("yours", encoding="utf-8")
    (root / "a.txt.partial").symlink_to(outside)
    with pytest.raises(OSError):
        folder.write(root / "a.txt", b"new")
    assert outside.read_text(encoding="utf-8") == "yours" and folder.missing(root / "a.txt")


def test_a_planted_broken_partial_link_makes_no_file_at_its_target(root, tmp_path):
    (root / "a.txt.partial").symlink_to(tmp_path / "made-by-accident")
    with pytest.raises(OSError):
        folder.write(root / "a.txt", b"new")
    assert not (tmp_path / "made-by-accident").exists()


@pytest.fixture
def old_file(root):
    path = root / "a.txt"
    path.write_bytes(b"old")
    os.utime(path, (1_000_000, 1_000_000))
    return path


def unchanged(path):
    return (
        path.read_bytes() == b"old"
        and path.stat().st_mtime == 1_000_000
        and folder.missing(path.with_name(f"{path.name}.partial"))
    )


def test_a_failed_swap_leaves_the_old_file_and_no_partial(old_file, monkeypatch):
    monkeypatch.setattr(folder.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError(13, "in use")))
    with pytest.raises(PermissionError):
        folder.write(old_file, b"new")
    assert unchanged(old_file)


def test_a_full_disk_leaves_the_old_file_and_no_partial(old_file, monkeypatch):
    monkeypatch.setattr(folder.os, "fsync", lambda _: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError, match="No space"):
        folder.write(old_file, b"new")
    assert unchanged(old_file)


def test_an_unexpected_failure_leaves_no_partial_either(old_file, monkeypatch):
    monkeypatch.setattr(folder.os, "fsync", lambda _: (_ for _ in ()).throw(ValueError("odd")))
    with pytest.raises(ValueError, match="odd"):
        folder.write(old_file, b"new")
    assert unchanged(old_file)


def test_the_data_reaches_the_disk_before_the_swap(root, monkeypatch):
    order = []
    sync, swap = os.fsync, os.replace
    monkeypatch.setattr(folder.os, "fsync", lambda number: (order.append("synced"), sync(number))[1])
    monkeypatch.setattr(folder.os, "replace", lambda *names: (order.append("swapped"), swap(*names))[1])
    folder.write(root / "a.txt", b"text")
    assert order == ["synced", "swapped"]


def test_a_digest_is_the_file_s_sha_256(root):
    (root / "resume.pdf").write_bytes(b"abc")
    (root / "empty.pdf").write_bytes(b"")
    assert folder.digest(root / "resume.pdf") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert folder.digest(root / "empty.pdf") == hashlib.sha256(b"").hexdigest()


def test_a_digest_reads_a_piece_at_a_time(root, monkeypatch):
    data = os.urandom(3_000_000)
    (root / "big.pdf").write_bytes(data)
    monkeypatch.setattr(folder.Path, "read_bytes", lambda _: pytest.fail("the whole file at once"))
    assert folder.digest(root / "big.pdf") == hashlib.sha256(data).hexdigest()


def test_there_is_no_digest_of_a_link_a_folder_or_nothing(root, tmp_path):
    (tmp_path / "outside.pdf").write_bytes(b"abc")
    (root / "link.pdf").symlink_to(tmp_path / "outside.pdf")
    (root / "broken.pdf").symlink_to(root / "gone")
    (root / "folder.pdf").mkdir()
    os.mkfifo(root / "pipe.pdf")  # opening one would wait for ever
    for name in ("link.pdf", "broken.pdf", "folder.pdf", "pipe.pdf", "nothing.pdf"):
        assert folder.digest(root / name) is None


def test_there_is_no_digest_of_a_file_you_cant_read(root):
    (root / "closed.pdf").write_bytes(b"abc")
    (root / "closed.pdf").chmod(0)
    assert folder.digest(root / "closed.pdf") is None


def history(root, text=OLD, today=TODAY):
    return folder.history(root, text.decode(), today)


def test_a_first_history_is_made_for_you_only(root, loose_umask):
    assert history(root) == "made" and (root / "status_history.txt").read_bytes() == OLD
    assert rights(root / "status_history.txt") == 0o600


def test_the_same_history_changes_nothing_not_even_its_time(root):
    history(root)
    os.utime(root / "status_history.txt", (1_000_000, 1_000_000))
    assert history(root) == "same" and (root / "status_history.txt").stat().st_mtime == 1_000_000


def test_lines_added_are_swapped_in(root):
    history(root)
    assert history(root, NEW) == "refreshed"
    assert (root / "status_history.txt").read_bytes() == NEW and names(root) == ["status_history.txt"]


def test_non_english_text_is_written_as_utf_8_with_plain_line_ends(root):
    assert folder.history(root, "2026-10-17  应用\n", TODAY) == "made"
    assert (root / "status_history.txt").read_bytes() == "2026-10-17  应用\n".encode()


@pytest.mark.parametrize(
    "old",
    [
        b"",  # emptied
        b"2026-10-17  App",  # cut in the middle of a line
        OLD[:-1],  # only its last line break gone
        b"2026-10-17  Applied, and my own note\n",
        b"2026-11-02  Interview, Round 1\n",  # a line removed
        OLD + b"2026-11-02  Interview, Round 1\nmy note\n",  # a line of yours added
        OLD.replace(b"\n", b"\r\n"),  # Windows' line ends
        b"\xff\xfe not UTF-8\n",
    ],
)
def test_a_history_that_isnt_the_app_s_own_start_is_left_and_the_new_one_goes_beside_it(root, old):
    (root / "status_history.txt").write_bytes(old)
    assert history(root, NEW) == "beside"
    assert (root / "status_history.txt").read_bytes() == old
    assert (root / f"status_history-{TODAY}.txt").read_bytes() == NEW


def test_the_beside_file_is_for_you_only(root, loose_umask):
    (root / "status_history.txt").write_bytes(b"edited\n")
    history(root, NEW)
    assert rights(root / f"status_history-{TODAY}.txt") == 0o600


def test_an_edited_history_gets_one_beside_file_however_many_runs(root):
    (root / "status_history.txt").write_bytes(b"edited\n")
    assert [history(root, NEW, day) for day in (TODAY, TODAY, "2026-11-06", "2026-11-07")] == ["beside", *["same"] * 3]
    assert names(root) == [f"status_history-{TODAY}.txt", "status_history.txt"]


def test_a_new_line_after_an_edit_makes_a_second_beside_file(root):
    (root / "status_history.txt").write_bytes(b"edited\n")
    history(root, OLD)
    assert history(root, NEW, "2026-11-06") == "beside"
    assert names(root) == [f"status_history-{TODAY}.txt", "status_history-2026-11-06.txt", "status_history.txt"]
    assert (root / "status_history-2026-11-06.txt").read_bytes() == NEW


def test_a_taken_beside_name_moves_on_to_the_next_number(root):
    (root / "status_history.txt").write_bytes(b"edited\n")
    (root / f"status_history-{TODAY}.txt").write_bytes(b"yours\n")
    assert history(root) == "beside" and (root / f"status_history-{TODAY}-2.txt").read_bytes() == OLD
    assert history(root, NEW) == "beside" and (root / f"status_history-{TODAY}-3.txt").read_bytes() == NEW
    assert (root / f"status_history-{TODAY}.txt").read_bytes() == b"yours\n"


def test_a_folder_with_a_beside_name_is_left_alone(root):
    (root / "status_history.txt").write_bytes(b"edited\n")
    (root / f"status_history-{TODAY}.txt").mkdir()
    assert history(root) == "beside" and (root / f"status_history-{TODAY}-2.txt").read_bytes() == OLD
    assert (root / f"status_history-{TODAY}.txt").is_dir()


def test_a_beside_link_is_never_read_as_the_same_text(root, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(OLD)
    (root / "status_history.txt").write_bytes(b"edited\n")
    (root / "status_history-2026-01-01.txt").symlink_to(outside)
    assert history(root) == "beside" and (root / f"status_history-{TODAY}.txt").read_bytes() == OLD


def test_a_folder_named_status_history_is_left_and_the_text_goes_beside_it(root):
    (root / "status_history.txt").mkdir()
    assert history(root) == "beside" and (root / "status_history.txt").is_dir()
    assert history(root) == "same"  # and only once


@pytest.mark.parametrize("old", [OLD, b"edited\n"])
def test_a_link_named_status_history_is_never_read_through_or_replaced(root, tmp_path, old):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(old)
    (root / "status_history.txt").symlink_to(outside)
    assert history(root, NEW) == "beside"
    assert outside.read_bytes() == old and (root / "status_history.txt").is_symlink()
    assert (root / f"status_history-{TODAY}.txt").read_bytes() == NEW


def test_a_broken_link_named_status_history_makes_no_file_at_its_target(root, tmp_path):
    (root / "status_history.txt").symlink_to(tmp_path / "made-by-accident")
    assert history(root) == "beside"
    assert not (tmp_path / "made-by-accident").exists() and (root / "status_history.txt").is_symlink()


def test_a_failed_refresh_leaves_the_old_history(root, monkeypatch):
    history(root)
    monkeypatch.setattr(folder.os, "replace", lambda *_: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    with pytest.raises(OSError):
        history(root, NEW)
    assert (root / "status_history.txt").read_bytes() == OLD and names(root) == ["status_history.txt"]
