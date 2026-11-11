"""The person's own background picture: a private file in the data folder shown behind every signed-in page, served
to the signed-in person only, with its credit; the fallbacks when it is missing, not a picture, or removed."""

import gc
import importlib
import logging
import os
import threading

import pytest
from helpers import csrf, log_in

from app.web import background

# Made-up pictures: only their first bytes matter to the app, which never decodes them
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
WEBP = b"RIFF\x00\x01\x00\x00WEBP" + b"\x00" * 200
PLAIN = '<html lang="en" data-palette="G">'  # nothing chosen on Settings
SWITCH = '<html lang="en" data-palette="G" data-own-field data-ink="dark">'
CREDIT = "Background: a made-up picture · copyright its author · private copy."
PROBLEM = "own.jpg in your data folder"  # the start of the problem sentence (its apostrophe is escaped on the page)


@pytest.fixture
def folder(data_dir):
    path = data_dir / background.FOLDER
    path.mkdir()
    return path


@pytest.fixture
def client(client):
    log_in(client)
    return client


def within(seconds, action):
    """Runs the action in a helper thread and fails, instead of hanging the suite, if it has not finished in time."""
    done = []
    worker = threading.Thread(target=lambda: done.append(action()), daemon=True)
    worker.start()
    worker.join(seconds)
    assert done, "the request did not finish in time: something blocked"
    return done[0]


def test_without_a_file_the_built_in_pictures_show_and_the_route_answers_404(client):
    text = client.get("/").text
    assert PLAIN in text and "credit-corner" not in text
    assert client.get(background.ROUTE).status_code == 404
    settings = client.get("/settings").text
    assert "Your own picture" in settings and "Remove my picture" not in settings and "own.jpg" in settings


def test_a_placed_picture_switches_every_page_and_is_served_to_the_signed_in_person(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    for page in ("/", "/settings", "/nowhere"):
        assert SWITCH in client.get(page).text, page
    assert f'<p class="credit credit-corner">{background.DEFAULT_CREDIT}</p>' in client.get("/").text
    assert "credit-corner" not in client.get("/nowhere").text  # a problem page has no frame, so no caption
    picture = client.get(background.ROUTE)
    assert (picture.status_code, picture.content) == (200, JPEG)
    assert picture.headers["Content-Type"] == "image/jpeg"
    assert picture.headers["Content-Length"] == str(len(JPEG))
    assert picture.headers["X-Content-Type-Options"] == "nosniff"
    assert picture.headers["Cache-Control"] == "private, no-cache"
    again = client.get(background.ROUTE, headers={"If-None-Match": picture.headers["ETag"]})
    assert (again.status_code, again.content, again.headers["Cache-Control"]) == (304, b"", "private, no-cache")


@pytest.mark.parametrize(("name", "content", "kind"), [("own.png", PNG, "image/png"), ("own.webp", WEBP, "image/webp")])
def test_the_type_comes_from_the_bytes(client, folder, name, content, kind):
    (folder / name).write_bytes(content)
    assert client.get(background.ROUTE).headers["Content-Type"] == kind
    (folder / "own.jpg").write_bytes(PNG)  # the name says JPEG, the bytes say PNG: the bytes win
    assert client.get(background.ROUTE).headers["Content-Type"] == "image/png"


@pytest.mark.parametrize("content", [b"", b"not a picture at all", b"\xff\xd8", b"RIFF\x00\x00\x00\x00WAVE"])
def test_a_file_that_is_not_a_picture_is_reported_with_a_way_out(client, folder, content):
    (folder / "own.jpg").write_bytes(content)
    assert PLAIN in client.get("/").text
    assert client.get(background.ROUTE).status_code == 404
    text = client.get("/settings").text
    assert PROBLEM in text and "shown meanwhile" in text and "Remove my picture" in text  # a wrong file can be removed


def test_a_link_a_folder_or_a_huge_file_is_refused(client, folder, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere.jpg"
    elsewhere.write_bytes(JPEG)
    (folder / "own.jpg").symlink_to(elsewhere)  # a link out of the folder is never followed
    assert client.get(background.ROUTE).status_code == 404
    assert PROBLEM in client.get("/settings").text
    (folder / "own.jpg").unlink()
    (folder / "own.png").mkdir()
    assert client.get(background.ROUTE).status_code == 404 and PLAIN in client.get("/").text
    (folder / "own.png").rmdir()
    monkeypatch.setattr(background, "MAX_BYTES", len(JPEG) - 1)
    (folder / "own.webp").write_bytes(JPEG)
    assert client.get(background.ROUTE).status_code == 404 and PLAIN in client.get("/").text


def test_a_data_folder_that_is_not_a_folder_means_no_picture(client, data_dir):
    (data_dir / background.FOLDER).write_bytes(JPEG)  # a file where the folder should be
    assert PLAIN in client.get("/").text and client.get(background.ROUTE).status_code == 404
    assert "shown meanwhile" not in client.get("/settings").text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this system")
@pytest.mark.parametrize("name", ["own.jpg", background.CREDIT])
def test_a_pipe_under_a_name_never_holds_the_dashboard(client, folder, name):
    if name == background.CREDIT:
        (folder / "own.jpg").write_bytes(JPEG)
    os.mkfifo(folder / name)  # nobody ever writes to it: a plain open for reading would wait forever
    try:
        page = within(5, lambda: client.get("/").text)
        picture = within(5, lambda: client.get(background.ROUTE))
        if name == background.CREDIT:
            assert SWITCH in page and background.DEFAULT_CREDIT in page and picture.status_code == 200
        else:
            assert PLAIN in page and picture.status_code == 404
            assert PROBLEM in within(5, lambda: client.get("/settings").text)
    finally:  # a reader left blocked by a regression is released, so the suite can still end
        try:
            os.close(os.open(folder / name, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:  # no reader is waiting
            pass


def test_the_picture_is_served_where_the_system_lacks_the_guarding_flags(client, folder):
    """Windows has no O_NOFOLLOW or O_NONBLOCK: the module must still load, open and serve, each flag counting 0."""
    saved = {name: getattr(os, name) for name in ("O_NOFOLLOW", "O_NONBLOCK")}
    for name in saved:
        delattr(os, name)
    try:
        importlib.reload(background)  # the module's own first lines run again, without the flags
        assert background.OPEN_FLAGS == os.O_RDONLY | getattr(os, "O_BINARY", 0)
        (folder / "own.jpg").write_bytes(JPEG)
        picture = client.get(background.ROUTE)
        assert (picture.status_code, picture.content) == (200, JPEG) and SWITCH in client.get("/").text
    finally:
        for name, value in saved.items():
            setattr(os, name, value)
        importlib.reload(background)


def test_an_error_after_the_open_still_closes_the_file(client, folder, monkeypatch):
    (folder / "own.jpg").write_bytes(JPEG)
    opened = []
    first = background._open

    def remembered(path):
        found = first(path)
        if found:
            opened.append(found[0])
        return found

    faults = [ValueError("made-up fault")]  # once, in the route; the problem page drawn after it must still work

    def broken(head):
        if faults:
            raise faults.pop()
        return "image/jpeg"

    monkeypatch.setattr(background, "_open", remembered)
    monkeypatch.setattr(background, "_kind", broken)
    assert client.get(background.ROUTE).status_code == 500
    assert opened and all(file.closed for file in opened)
    assert client.get(background.ROUTE).status_code == 200
    gc.collect()
    assert len(opened) == 3 and all(file.closed for file in opened)  # the route's, the page's, the route's again


def test_what_was_checked_is_what_is_sent(client, folder, tmp_path, monkeypatch):
    """The name is opened once; swapping it for a link or a bigger file after the check changes nothing."""
    path, elsewhere = folder / "own.jpg", tmp_path / "elsewhere.png"
    path.write_bytes(JPEG)
    elsewhere.write_bytes(PNG * 50)
    kind = background._kind

    def swap_then_judge(head):
        path.unlink()
        path.symlink_to(elsewhere)  # another file, another type, bigger
        return kind(head)

    monkeypatch.setattr(background, "_kind", swap_then_judge)
    picture = client.get(background.ROUTE)
    assert (picture.status_code, picture.headers["Content-Type"], picture.content) == (200, "image/jpeg", JPEG)
    monkeypatch.setattr(background, "_kind", kind)
    assert client.get(background.ROUTE).status_code == 404  # the link that is there now is refused on its own


def test_a_file_that_grows_while_it_is_sent_is_cut_at_the_size_that_was_checked(client, folder, monkeypatch):
    path = folder / "own.jpg"
    path.write_bytes(JPEG)
    kind = background._kind

    def grow_then_judge(head):
        with path.open("ab") as file:
            file.write(b"\x00" * 5000)
        return kind(head)

    monkeypatch.setattr(background, "_kind", grow_then_judge)
    picture = client.get(background.ROUTE)
    assert (picture.headers["Content-Length"], picture.content) == (str(len(JPEG)), JPEG)


def test_the_credit_is_the_typed_first_line_escaped_cleaned_and_cut(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    (folder / "own.txt").write_text(f"  {CREDIT} <b>x</b>\nsecond line is not shown\n", encoding="utf-8")
    text = client.get("/settings").text
    assert f"{CREDIT} &lt;b&gt;x&lt;/b&gt;" in text and "second line" not in text and "<b>x</b>" not in text
    (folder / "own.txt").write_text("a‮​b\x07c\td \x1b[31me", encoding="utf-8")  # bidi, zero-width, controls
    assert '<p class="credit">abcd [31me</p>' in client.get("/settings").text
    (folder / "own.txt").write_text("y" * 1000, encoding="utf-8")
    text = client.get("/").text
    assert "y" * background.MAX_CREDIT in text and "y" * (background.MAX_CREDIT + 1) not in text
    (folder / "own.txt").write_text("\n\n", encoding="utf-8")
    assert background.DEFAULT_CREDIT in client.get("/").text


def test_a_credit_that_is_a_link_or_a_folder_gives_the_default(client, folder, tmp_path):
    (folder / "own.jpg").write_bytes(JPEG)
    elsewhere = tmp_path / "secret.txt"
    elsewhere.write_text("not for the page", encoding="utf-8")
    (folder / "own.txt").symlink_to(elsewhere)
    text = client.get("/").text
    assert background.DEFAULT_CREDIT in text and "not for the page" not in text
    (folder / "own.txt").unlink()
    (folder / "own.txt").mkdir()
    assert background.DEFAULT_CREDIT in client.get("/").text


def test_a_changed_file_counts_at_once_and_an_unchanged_one_is_not_read_again(client, folder, monkeypatch):
    (folder / "own.jpg").write_bytes(JPEG)
    assert SWITCH in client.get("/").text
    reads = []
    monkeypatch.setattr(background, "_look", lambda folder: reads.append(folder) or background.Own(in_use=True))
    client.get("/")
    client.get("/settings")
    assert reads == []  # nothing changed: the sizes and times say so, no file is opened
    (folder / "own.txt").write_text(CREDIT, encoding="utf-8")
    client.get("/")
    assert reads == [folder]


def test_only_a_whole_page_looks_for_the_picture(client, folder, monkeypatch):
    looks = []
    of = background.of
    monkeypatch.setattr(background, "of", lambda folder: looks.append(folder) or of(folder))
    client.get("/collect/progress")  # a box redrawn every two seconds
    client.get("/score/progress")
    assert looks == []
    client.get("/")
    assert looks == [folder]


def test_the_picture_needs_a_login_and_the_login_page_never_shows_it(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    assert PLAIN in client.get("/login").text
    client.cookies.clear()
    answer = client.get(background.ROUTE, follow_redirects=False)
    assert (answer.status_code, answer.headers["location"]) == (303, "/login")
    assert client.post("/background/remove", follow_redirects=False).status_code == 303  # to the login page
    assert (folder / "own.jpg").read_bytes() == JPEG


def test_removing_needs_the_safety_token_and_deletes_only_the_private_copies(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    (folder / "own.txt").write_text(CREDIT, encoding="utf-8")
    (folder / "unrelated.txt").write_text("stays", encoding="utf-8")
    assert client.post("/background/remove").status_code == 403
    assert (folder / "own.jpg").exists()
    form = {"csrf_token": csrf(client)}
    answer = client.post("/background/remove", data=form, follow_redirects=False)
    assert (answer.status_code, answer.headers["location"]) == (303, "/settings?look=removed#look")
    assert sorted(path.name for path in folder.iterdir()) == ["unrelated.txt"]
    text = client.get(answer.headers["location"]).text
    assert "the built-in pictures are back" in text and PLAIN in text
    assert client.post("/background/remove", data=form, follow_redirects=False).status_code == 303  # twice: fine


def test_removing_never_deletes_a_folder_and_says_what_was_left(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    (folder / "own.png").mkdir()
    (folder / "own.png" / "keep.txt").write_text("kept", encoding="utf-8")
    answer = client.post("/background/remove", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert answer.headers["location"] == "/settings?look=left#look"
    assert sorted(path.name for path in folder.iterdir()) == ["own.png"] and (folder / "own.png" / "keep.txt").exists()
    assert "could not be removed" in client.get(answer.headers["location"]).text


def test_nothing_about_the_picture_reaches_the_apps_log(client, folder, caplog):
    """The app's own log lines: the web server's request log is off (see app/web/__main__.py), and a request line
    would name only the fixed address anyway."""
    caplog.set_level(logging.DEBUG)  # every level, not only warnings
    (folder / "own.jpg").write_bytes(JPEG)
    (folder / "own.txt").write_text(CREDIT, encoding="utf-8")
    client.get("/")
    client.get(background.ROUTE)
    client.post("/background/remove", data={"csrf_token": csrf(client)})
    assert "own." not in caplog.text and CREDIT not in caplog.text and str(folder) not in caplog.text
