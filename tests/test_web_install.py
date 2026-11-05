"""The installable frame: the manifest, its icons, the page tags and the room for a full-screen phone's status bar."""

import json
import re
import struct

from helpers import log_in

from app.web import main

STATIC = main.HERE / "static"
MANIFEST = "/static/manifest.webmanifest"


def _size(png: bytes) -> tuple[int, int]:
    """Width and height from the file's own header, so a wrong picture can't pass on its name."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR"
    return struct.unpack(">II", png[16:24])


def _parts(png: bytes) -> set[bytes]:
    names, at = set(), 8
    while at < len(png):
        names.add(png[at + 4 : at + 8])
        at += 12 + struct.unpack(">I", png[at : at + 4])[0]
    return names


def test_the_manifest_opens_without_a_login_and_only_under_static(client):
    response = client.get(MANIFEST)
    assert (response.status_code, response.headers["Cache-Control"]) == (200, "no-cache")
    assert response.headers["Content-Type"] == "application/manifest+json"
    assert "Set-Cookie" not in response.headers
    outside = client.get("/manifest.webmanifest", follow_redirects=False)
    assert (outside.status_code, outside.headers["location"]) == (303, "/login")


def test_the_manifest_says_exactly_what_was_decided(client):
    text = client.get(MANIFEST).text
    manifest = json.loads(text)
    assert manifest.pop("icons")
    assert manifest == {  # exact: no service worker
        "name": "auto_apply",
        "short_name": "auto_apply",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f3f5f7",
        "theme_color": "#ffffff",
        "share_target": {
            "action": "/add",
            "method": "GET",
            "enctype": "application/x-www-form-urlencoded",
            "params": {"url": "link", "text": "text"},
        },
    }
    assert "http" not in text  # no address of any computer


def test_every_icon_is_a_real_picture_of_the_size_it_claims(client):
    icons = json.loads(client.get(MANIFEST).text)["icons"]
    assert sorted((icon["sizes"], icon.get("purpose", "any")) for icon in icons) == [
        ("192x192", "any"),
        ("512x512", "any"),
        ("512x512", "maskable"),
    ]
    for icon in icons:
        response = client.get(icon["src"])  # not logged in
        assert (response.status_code, response.headers["Content-Type"]) == (200, "image/png")
        assert "x".join(map(str, _size(response.content))) == icon["sizes"]


def test_icons_are_solid_and_carry_nothing_but_the_picture():
    icons = sorted(STATIC.glob("*.png"))
    assert [icon.name for icon in icons] == [
        "apple-touch-icon.png",
        "icon-192.png",
        "icon-512.png",
        "icon-maskable-512.png",
    ]
    for icon in icons:
        png = icon.read_bytes()
        assert png[24:26] == b"\x08\x02"  # 8-bit colour with no see-through channel
        assert _parts(png) == {b"IHDR", b"IDAT", b"IEND"}  # no text, dates or colour profile from the making


def test_every_kind_of_page_has_the_install_tags_and_they_work(client):
    before_login = client.get("/login")
    log_in(client)
    for response in (before_login, client.get("/"), client.get("/nowhere")):
        text = response.text
        assert text.count('rel="manifest"') == 1 and text.count('rel="apple-touch-icon"') == 1
        assert text.count('name="theme-color"') == 2
        assert '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#ffffff">' in text
        assert '<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#1a1e23">' in text
        links = dict(re.findall(r'<link rel="(manifest|apple-touch-icon)" href="([^"]+)">', text))
        assert {client.get(link).status_code for link in links.values()} == {200}
        assert _size(client.get(links["apple-touch-icon"]).content) == (180, 180)


def test_no_program_file_starts_a_service_worker():
    for path in STATIC.rglob("*"):
        assert not path.is_file() or b"serviceworker" not in path.read_bytes().lower()


def test_the_frame_keeps_clear_of_a_full_screen_phones_status_bar():
    css = (STATIC / "app.css").read_text()
    assert re.search(r"^body \{[^}]*padding-top: env\(safe-area-inset-top\);", css, re.M)
    assert re.search(r"^\.app \{[^}]*min-height: calc\(100dvh - env\(safe-area-inset-top\)\);", css, re.M)
    rail = r"^\.rail \{[^}]*top: env\(safe-area-inset-top\);[^}]*height: calc\(100dvh - env\(safe-area-inset-top\)\);"
    assert re.search(rail, css, re.M)
