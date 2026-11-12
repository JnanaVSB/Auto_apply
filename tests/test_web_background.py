"""The person's own background picture: a private file in the data folder shown behind every signed-in page, served
to the signed-in person only, with its credit; the fallbacks when it is missing, not a picture, or removed."""

import gc
import importlib
import io
import logging
import os
import threading
import time

import pytest
from helpers import csrf, log_in
from PIL import Image, ImageFile, WebPImagePlugin

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
    (folder / "own.webp").write_bytes(PNG)  # the name says WebP, the bytes say PNG: the bytes win
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


# ---------- Uploading a picture on Settings ----------
# Pictures made here in code, never a photograph: a plain colour with one brighter corner, so a crop or a turn shows.


def picture(width=3000, height=1500, mode="RGB"):
    image = Image.new(mode, (width, height), (20, 60, 40) if mode == "RGB" else 0)
    image.paste((230, 200, 120) if mode == "RGB" else 1, (0, 0, width // 4, height // 4))
    return image


def encoded(image, kind="JPEG", **options):
    out = io.BytesIO()
    image.save(out, kind, **options)
    return out.getvalue()


def upload(client, data, name="secret-holiday.jpg", credit=CREDIT, token=True, kind="image/jpeg"):
    form = {"credit": credit} | ({"csrf_token": csrf(client)} if token else {})
    files = {"picture": (name, data, kind)}
    return client.post(background.UPLOAD, data=form, files=files, follow_redirects=False)


def stored(folder):
    return Image.open(folder / "own.webp"), Image.open(folder / background.PHONE)


@pytest.mark.parametrize("kind", ["JPEG", "PNG", "WEBP"])
def test_a_jpeg_png_or_webp_is_shrunk_cropped_for_phones_and_shown_with_its_credit(client, folder, kind):
    answer = upload(client, encoded(picture(), kind))
    assert (answer.status_code, answer.headers["location"]) == (303, "/settings?look=uploaded#look")
    big, phone = stored(folder)
    assert (big.format, big.size, big.mode) == ("WEBP", (2560, 1280), "RGB")
    assert (phone.format, phone.size) == ("WEBP", (1080, 1280))  # the middle of the picture, as tall as it has
    assert (folder / "own.txt").read_text(encoding="utf-8") == f"{CREDIT}\n"
    assert sorted(path.name for path in folder.iterdir()) == ["own-phone.webp", "own.txt", "own.webp"]
    text = client.get(answer.headers["location"]).text
    assert "Your picture is in use on every page." in text and f'value="{CREDIT}"' in text
    assert SWITCH in text and f'<p class="credit">{CREDIT}</p>' in text
    assert client.get(background.ROUTE).headers["Content-Type"] == "image/webp"
    assert client.get(background.PHONE_ROUTE).content == (folder / background.PHONE).read_bytes()


def test_a_small_picture_is_never_enlarged_and_the_phone_copy_takes_what_there_is(client, folder):
    upload(client, encoded(picture(800, 600), "PNG"))
    big, phone = stored(folder)
    assert big.size == (800, 600) and phone.size == (800, 600)


def test_every_hidden_detail_is_dropped(client, folder):
    source = picture()
    exif = Image.Exif()
    exif[0x0112] = 1  # orientation: as it is
    exif[0x010F] = "Made-up Camera Works"  # make
    exif[0x8825] = {1: "N", 2: (33.0, 25.0, 12.5)}  # a GPS block
    profile = b"\x00\x00\x02\x00ADBE" + b"\x00" * 120  # stands in for a colour profile
    data = encoded(source, "JPEG", exif=exif.tobytes(), icc_profile=profile, comment=b"a secret comment",
                   xmp=b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>where it was taken</x:xmpmeta>")  # fmt: skip
    assert b"Made-up Camera" in data and b"ADBE" in data and b"secret comment" in data and b"xmpmeta" in data
    assert upload(client, data).headers["location"].endswith("uploaded#look")
    for image in stored(folder):
        assert not (image.info.keys() & {"exif", "icc_profile", "xmp", "comment", "dpi"})
        assert dict(image.getexif()) == {} and image.getexif().get_ifd(0x8825) == {}
    written = (folder / "own.webp").read_bytes() + (folder / background.PHONE).read_bytes()
    for mark in (b"Made-up Camera", b"ADBE", b"secret comment", b"xmpmeta", b"EXIF", b"ICCP", b"XMP "):
        assert mark not in written


@pytest.mark.parametrize("mode", ["RGBA", "P", "RGB"])
def test_see_through_parts_land_on_the_dark_floor_and_a_big_picture_keeps_its_colours(client, folder, mode):
    gold, floor = (230, 200, 120), background.FLOOR
    if mode == "P":  # a palette picture whose entry 0 is see-through
        source = Image.new("P", (5200, 600), 0)
        source.putpalette([0, 0, 0, *gold])
        source.paste(1, (2600, 0, 5200, 600))
        data = encoded(source, "PNG", transparency=0)
    else:
        source = Image.new(mode, (5200, 600), (0, 0, 0, 0) if mode == "RGBA" else floor)  # wholly see-through, or floor
        source.paste((*gold, 255) if mode == "RGBA" else gold, (2600, 0, 5200, 600))
        data = encoded(source, "PNG")
    assert upload(client, data).headers["location"].endswith("uploaded#look")
    main, phone = stored(folder)
    assert main.size == (2560, 295) and main.mode == "RGB" and phone.size == (1080, 295)
    spots = {(2, 2): floor, (1270, 150): floor, (10, 292): floor, (1290, 2): gold, (2550, 150): gold, (1300, 292): gold}
    for spot, expected in spots.items():  # the left half is floor, the right half gold, to a little lossy noise
        assert max(abs(a - b) for a, b in zip(main.getpixel(spot), expected, strict=True)) <= 6, spot
    assert main.getpixel((1281, 150)) != main.getpixel((1270, 150))  # the edge is where it was, not shifted by a strip


def test_a_camera_picture_is_turned_the_way_it_was_meant(client, folder):
    exif = Image.Exif()
    exif[0x0112] = 6  # stored lying on its side: turn 90 degrees to show
    upload(client, encoded(picture(300, 200), "JPEG", exif=exif.tobytes()))
    big, phone = stored(folder)
    assert big.size == (200, 300) and big.getpixel((199, 0)) != big.getpixel((0, 299))  # the bright corner moved
    assert dict(big.getexif()) == {}


def test_the_first_frame_of_an_animation_is_taken_and_nothing_is_sought(client, folder, monkeypatch):
    frames = [picture(400, 300), Image.new("RGB", (400, 300), (255, 0, 0))]
    data = encoded(frames[0], "PNG", save_all=True, append_images=frames[1:], duration=100)
    assert Image.open(io.BytesIO(data)).n_frames == 2
    monkeypatch.setattr(Image.Image, "seek", lambda self, frame: (_ for _ in ()).throw(AssertionError("sought")))
    assert upload(client, data, "moving.png", kind="image/png").headers["location"].endswith("uploaded#look")
    big, _ = stored(folder)
    assert getattr(big, "n_frames", 1) == 1 and big.getpixel((390, 290)) != (255, 0, 0)  # frame 0, green not red


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"not a picture at all" * 100, "words.jpg"),
        (b"", "empty.jpg"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "drawing.jpg"),
        (None, "picture.tiff"),  # a real TIFF under a JPEG name: a decoder the app never asks
        (b"GIF89a" + b"\x00" * 100, "moving.gif"),
    ],
)
def test_what_is_not_a_jpeg_png_or_webp_is_refused_whatever_its_name(client, folder, data, name):
    data = encoded(picture(200, 100), "TIFF") if data is None else data
    answer = upload(client, data, name.replace(".tiff", ".jpg"))
    assert answer.headers["location"] == "/settings?look=refused#look"
    assert not folder.exists() or list(folder.iterdir()) == []
    text = client.get(answer.headers["location"]).text
    assert "Not uploaded: choose a JPEG, PNG or WebP picture of up to 15 MB." in text and PLAIN in text


def test_too_many_pixels_or_too_long_a_side_is_refused_before_decoding(client, folder, monkeypatch):
    wide = encoded(Image.new("1", (12001, 10)), "PNG")
    many = encoded(Image.new("1", (8000, 5001)), "PNG")  # just over 40 million pixels, a few KB as a file
    loads = []  # the decodes the app asks for: JPEG and PNG load through ImageFile, WebP through its own class
    for owner in (ImageFile.ImageFile, WebPImagePlugin.WebPImageFile):
        original = owner.load
        monkeypatch.setattr(owner, "load", lambda self, original=original: loads.append(self.size) or original(self))
    for data in (wide, many):
        assert upload(client, data, "big.png", kind="image/png").headers["location"].endswith("refused#look")
    assert loads == [] and list(folder.iterdir()) == []
    assert upload(client, encoded(picture(200, 100), "WEBP")).headers["location"].endswith("uploaded#look")
    assert loads[0] == (200, 100)  # a sound picture is decoded, so the hook does see the app's decodes


def test_too_many_bytes_are_refused_before_any_decoder_sees_them(client, folder, monkeypatch):
    stores = []
    monkeypatch.setattr(background, "_store", lambda *args: stores.append(args) or True)
    just_over = b"\xff\xd8\xff" + b"\x00" * (background.MAX_BYTES - 2)  # one byte over the cap
    assert upload(client, just_over).headers["location"].endswith("refused#look")
    far_over = b"\x00" * (background.MAX_BYTES + 64 * 1024)  # over the address's own limit: refused as it arrives
    assert upload(client, far_over).status_code == 413
    assert stores == [] and list(folder.iterdir()) == []


def test_the_credit_typed_at_upload_is_cleaned_and_a_blank_one_gets_the_default(client, folder):
    upload(client, encoded(picture(200, 100)), credit="  mine‮ <b>x</b>\nmore")
    assert (folder / "own.txt").read_text(encoding="utf-8") == "mine <b>x</b>\n"
    assert "mine &lt;b&gt;x&lt;/b&gt;" in client.get("/").text
    upload(client, encoded(picture(200, 100)), credit="   ")
    assert (folder / "own.txt").read_text(encoding="utf-8") == f"{background.DEFAULT_CREDIT}\n"


def test_an_upload_replaces_a_file_placed_by_hand_and_remove_takes_everything(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    (folder / "own.txt").write_text("the old line", encoding="utf-8")
    upload(client, encoded(picture(200, 100), "PNG"))
    assert sorted(path.name for path in folder.iterdir()) == ["own-phone.webp", "own.txt", "own.webp"]
    assert client.get(background.ROUTE).headers["Content-Type"] == "image/webp"
    client.post("/background/remove", data={"csrf_token": csrf(client)})
    assert list(folder.iterdir()) == [] and PLAIN in client.get("/").text


def test_a_phone_gets_the_main_picture_when_there_is_no_phone_copy(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    phone = client.get(background.PHONE_ROUTE)
    assert (phone.status_code, phone.content, phone.headers["Content-Type"]) == (200, JPEG, "image/jpeg")
    assert client.get(background.PHONE_ROUTE, headers={"If-None-Match": phone.headers["ETag"]}).status_code == 304


def test_the_phone_copy_is_kept_privately_and_checked_each_time(client, folder):
    upload(client, encoded(picture(400, 300), "PNG"))
    phone = client.get(background.PHONE_ROUTE)
    assert (phone.status_code, phone.headers["Cache-Control"]) == (200, "private, no-cache")
    again = client.get(background.PHONE_ROUTE, headers={"If-None-Match": phone.headers["ETag"]})
    assert (again.status_code, again.content, again.headers["Cache-Control"]) == (304, b"", "private, no-cache")


def test_removing_waits_while_a_picture_is_being_written(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    busy = client.app.state.background_busy
    assert busy.acquire(blocking=False)  # an upload is being prepared
    try:
        answer = client.post("/background/remove", data={"csrf_token": csrf(client)}, follow_redirects=False)
    finally:
        busy.release()
    assert answer.headers["location"] == "/settings?look=busy#look" and (folder / "own.jpg").exists()
    answer = client.post("/background/remove", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert answer.headers["location"] == "/settings?look=removed#look" and list(folder.iterdir()) == []  # free again


def test_uploading_needs_a_login_and_the_safety_token(client, folder):
    data = encoded(picture(200, 100))
    assert upload(client, data, token=False).status_code == 403
    client.cookies.clear()
    answer = upload(client, data, token=False)
    assert (answer.status_code, answer.headers["location"]) == (303, "/login")
    assert list(folder.iterdir()) == []


def test_one_picture_is_prepared_at_a_time(client, folder):
    busy = client.app.state.background_busy
    assert busy.acquire(blocking=False)  # another upload is being prepared
    try:
        answer = upload(client, encoded(picture(200, 100)))
    finally:
        busy.release()
    assert answer.headers["location"] == "/settings?look=busy#look"
    assert "Another picture is being prepared; try again in a moment." in client.get(answer.headers["location"]).text
    assert list(folder.iterdir()) == []
    assert upload(client, encoded(picture(200, 100))).headers["location"].endswith("uploaded#look")  # free again


def test_a_failed_write_leaves_no_half_file_behind(client, folder, monkeypatch):
    (folder / "own.jpg").write_bytes(JPEG)

    def refuse(source, target):
        raise OSError("made-up full disk")

    monkeypatch.setattr(background.os, "replace", refuse)
    answer = upload(client, encoded(picture(200, 100)))
    assert answer.headers["location"] == "/settings?look=failed#look"
    assert sorted(path.name for path in folder.iterdir()) == ["own.jpg"]  # no .own-*.partial, the old file kept
    assert "Not saved: your data folder" in client.get(answer.headers["location"]).text


def test_nothing_about_an_upload_reaches_the_apps_log(client, folder, caplog):
    caplog.set_level(logging.DEBUG)
    upload(client, b"not a picture", "secret-holiday.jpg")
    upload(client, encoded(picture(200, 100)), "secret-holiday.jpg", credit="my secret credit")
    for word in ("secret", "holiday", "own.", str(folder), "Traceback"):
        assert word not in caplog.text


def test_the_file_upload_form_is_read_for_this_address_only(client):
    form = {"csrf_token": csrf(client)}
    files = {"picture": ("x.jpg", b"\xff\xd8\xff", "image/jpeg")}
    assert client.post("/background/remove", data=form, files=files).status_code == 400  # plain forms only there
    multipart = {"Content-Type": "multipart/form-data; boundary=x"}
    broken = client.post(background.UPLOAD, content=b"--x\r\nno headers", headers=multipart)
    assert broken.status_code == 400  # unreadable: refused before anything else, never a crash


def raw(client, body, boundary="x"):
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    return client.post(background.UPLOAD, content=body, headers=headers, follow_redirects=False)


@pytest.mark.parametrize(
    ("body", "boundary", "status"),
    [
        (b'--x\nContent-Disposition: form-data; name="a"\n\nvalue\n--x--\n', "x", 400),  # bare new lines
        (b'--\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n----\r\n', "", 400),  # no boundary
        (b"--" + b"y" * 71 + b"\r\n", "y" * 71, 400),  # longer than the standard allows
        (b"--x\r\n" + b"X-Long: " + b"a" * 9000 + b"\r\n\r\nv\r\n--x--\r\n", "x", 400),  # headers past the cap
        (b'--x\r\nContent-Disposition: form-data; name="a"\r\n\r\nv\r\n' * 25 + b"--x--\r\n", "x", 400),  # 25 parts
        (b"--x\r\n" * (background.MAX_BYTES // 5), "x", 400),  # 15 MB of delimiters: millions of parts, if made
        (b"--x--x--x" * (background.MAX_BYTES // 9), "x", 403),  # the same, read as one closing delimiter: no token
        (b"no delimiter at all", "x", 400),
    ],
)
def test_a_malformed_file_upload_form_is_refused_quickly(client, body, boundary, status):
    started = time.monotonic()
    assert raw(client, body, boundary).status_code == status
    assert time.monotonic() - started < 2


def test_a_token_sent_as_a_file_does_not_count(client, folder):
    files = {"csrf_token": ("t.txt", csrf(client).encode(), "text/plain")}
    files["picture"] = ("a.jpg", encoded(picture(200, 100)), "image/jpeg")
    assert client.post(background.UPLOAD, data={"credit": "c"}, files=files).status_code == 403
    assert list(folder.iterdir()) == []


def test_the_lock_is_free_again_after_a_fault_while_preparing(client, folder, monkeypatch):
    faults = [RuntimeError("made-up fault")]
    prepare = background._prepare

    def faulty(data):
        if faults:
            raise faults.pop()
        return prepare(data)

    monkeypatch.setattr(background, "_prepare", faulty)
    assert upload(client, encoded(picture(200, 100))).headers["location"].endswith("refused#look")
    assert not client.app.state.background_busy.locked()
    assert upload(client, encoded(picture(200, 100))).headers["location"].endswith("uploaded#look")


def test_the_phone_route_needs_a_login_too(client, folder):
    (folder / "own.jpg").write_bytes(JPEG)
    client.cookies.clear()
    answer = client.get(background.PHONE_ROUTE, follow_redirects=False)
    assert (answer.status_code, answer.headers["location"]) == (303, "/login")


def test_a_write_that_fails_part_way_leaves_a_set_the_phone_route_can_use(client, folder, monkeypatch):
    upload(client, encoded(picture(400, 300), "PNG"), credit="first")
    old_phone = (folder / background.PHONE).read_bytes()
    os.utime(folder / background.PHONE, ns=(1, 1))  # plainly older than anything written next
    renames, replace = [], background.os.replace

    def second_fails(source, target):
        renames.append(target)
        if len(renames) == 2:
            raise OSError("made-up full disk")
        replace(source, target)

    monkeypatch.setattr(background.os, "replace", second_fails)
    answer = upload(client, encoded(picture(600, 300), "PNG"), credit="second")
    assert answer.headers["location"] == "/settings?look=partly#look"  # not "failed": the picture is in use
    assert sorted(path.name for path in folder.iterdir()) == ["own-phone.webp", "own.txt", "own.webp"]  # no partials
    assert Image.open(folder / "own.webp").size == (600, 300)  # the main picture is the new one
    assert (folder / background.PHONE).read_bytes() == old_phone and (folder / "own.txt").read_text() == "first\n"
    assert client.get(background.PHONE_ROUTE).content == (folder / "own.webp").read_bytes()  # the stale copy is skipped
    text = client.get(answer.headers["location"]).text
    assert "Your picture is in use, but its phone copy or its credit could not be written" in text
    assert "Upload the picture again, or remove it." in text and SWITCH in text


def test_a_credit_that_cannot_be_written_still_leaves_the_picture_in_use_and_says_so(client, folder):
    (folder / "own.txt").mkdir()  # a folder under the credit's name: the picture and phone copy are renamed, it is not
    answer = upload(client, encoded(picture(400, 300), "PNG"))
    assert answer.headers["location"] == "/settings?look=partly#look"
    assert sorted(path.name for path in folder.iterdir()) == ["own-phone.webp", "own.txt", "own.webp"]
    assert (folder / "own.txt").is_dir() and Image.open(folder / background.PHONE).size == (400, 300)
    text = client.get(answer.headers["location"]).text
    assert "its phone copy or its credit could not be written" in text and background.DEFAULT_CREDIT in text


def test_a_stale_or_broken_phone_copy_gives_way_to_the_main_picture(client, folder):
    upload(client, encoded(picture(400, 300), "PNG"))
    os.utime(folder / background.PHONE, ns=(1, 1))
    (folder / "own.webp").write_bytes(PNG)  # placed by hand, newer than the phone copy
    assert client.get(background.PHONE_ROUTE).content == PNG
    os.utime(folder / background.PHONE, ns=(2**62, 2**62))  # newer again
    (folder / background.PHONE).write_bytes(b"not a picture")
    assert client.get(background.PHONE_ROUTE).content == PNG  # broken: the main picture, not a 404


def test_leftover_partial_files_are_swept_by_remove_and_by_the_next_upload(client, folder):
    for name in (".own-abc.partial", ".own-def.partial"):
        (folder / name).write_bytes(b"cut off")
    (folder / "own.jpg").write_bytes(JPEG)
    client.post("/background/remove", data={"csrf_token": csrf(client)})
    assert list(folder.iterdir()) == []
    (folder / ".own-ghi.partial").write_bytes(b"cut off")
    upload(client, encoded(picture(200, 100)))
    assert sorted(path.name for path in folder.iterdir()) == ["own-phone.webp", "own.txt", "own.webp"]


def test_without_pillow_the_dashboard_runs_and_settings_says_what_to_install(client, folder, monkeypatch):
    monkeypatch.setattr(background, "Image", None)
    text = client.get("/settings").text
    assert "needs Pillow" in text and "pip install --require-hashes --no-deps -r requirements-pip.txt" in text
    assert 'name="picture"' not in text
    assert upload(client, encoded(picture(200, 100))).headers["location"] == "/settings?look=missing#look"
    assert list(folder.iterdir()) == []
