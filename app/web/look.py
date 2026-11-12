"""This browser's Table | Cards choice for the job lists and its Look (colours, mode and background picture), kept
apart from main.py."""

from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

VIEWS = ("table", "cards")  # the job lists' two views; Cards until one is chosen
VIEW_COOKIE = "auto_apply_view"  # named for this app, like the session cookie: cookies on 127.0.0.1 are shared
LISTS = ("/", "/ready")  # the pages with the Table | Cards switch
LOOK_COOKIE = "auto_apply_look"  # this browser's look, "<colours>.<mode>.<picture>", e.g. "E.dark.pines"
SCHEMES = {  # the stylesheet's colour schemes (data-palette); "today" is the stylesheet's own and needs no
    # attribute; G is what a browser gets until it chooses (see `of`)
    "today": "Teal",
    "A": "A · Olive and walnut",
    "B": "B · Sage and espresso",
    "C": "C · Moss and cream",
    "D": "D · Mix (A, warmer brown titles)",
    "E": "E · Forest rail and cognac",
    "F": "F · Espresso rail and olive",
    "G": "G · Forest and ochre (default)",
}
MODES = {"auto": "Auto (as this device)", "light": "Light", "dark": "Dark"}  # data-theme; the first is the default


class Picture(NamedTuple):
    """One choice: its words on Settings, and which words it takes: "dark" (a dark picture: white words, whatever the
    mode) or "mode" (a pair of files, the mode picks one, as the stylesheet's mountains do)."""

    words: str
    ink: str


# The background pictures a browser can choose on Settings, in the list's order; the first is what a browser gets
# until it chooses. Every signed-in page carries the key as data-field on <html>, with data-ink, and the stylesheet
# has one file rule per key (the mountains are its own pair, picked by the mode); a test checks every key has its
# rules. The person's own picture, when set, is shown instead of any of these (app/web/background.py).
PICTURES = {
    "pines": Picture("Pines in fog (default)", "dark"),
    "mountains": Picture("Blue mountains: a misty morning in light mode, dusk ridges in dark mode", "mode"),
    "lake": Picture("Snowy peaks over a lake, at dusk", "dark"),
    "river": Picture("Snowy peaks above a river, at dusk", "dark"),
    "ice": Picture("Peaks and a half-frozen river, at dusk", "dark"),
    "storm": Picture("Storm clouds over snowy peaks, at dusk", "dark"),
    "clouds": Picture("Dusk clouds over snowy peaks", "dark"),
    "haze": Picture("Peaks over an evergreen forest in haze, at dusk", "dark"),
    "forest": Picture("Three peaks over a sunlit forest, at dusk", "dark"),
    "ink": Picture("Still ink: the sign-in page's picture (words are harder to read)", "dark"),
    "hillside": Picture("Pine hillside in mist", "dark"),
    "mist": Picture("Pines in grey mist", "dark"),
}
DEFAULT_LOOK = ("G", "auto", next(iter(PICTURES)))  # colours, mode and picture until a browser chooses


def add_routes(app: FastAPI) -> None:
    @app.post("/view")
    async def choose_view(request: Request) -> Response:
        """Table or Cards for the job lists, kept in this browser; back to the same list, shown that way."""
        choice = request.state.form.get("view")
        if choice not in VIEWS:
            raise HTTPException(400, "Choose Table or Cards.")
        response = RedirectResponse(_list_page(request.state.form.get("back", "")), 303)
        response.set_cookie(
            VIEW_COOKIE, choice, max_age=365 * 24 * 60 * 60, path="/", samesite="lax", httponly=True,
            secure=request.state.remote,
        )  # fmt: skip
        return response

    @app.post("/look")
    async def choose_look(request: Request) -> Response:
        """Colours, mode and picture for every page, kept in this browser; back to the Look section on Settings."""
        scheme, mode, picture = (request.state.form.get(name) for name in ("scheme", "mode", "picture"))
        if scheme not in SCHEMES or mode not in MODES or picture not in PICTURES:
            raise HTTPException(400, "Choose colours, a mode and a picture from the lists.")
        response = RedirectResponse("/settings?look=changed#look", 303)
        response.set_cookie(
            LOOK_COOKIE, f"{scheme}.{mode}.{picture}", max_age=365 * 24 * 60 * 60, path="/", samesite="lax",
            httponly=True, secure=request.state.remote,
        )  # fmt: skip
        return response


def of(request: Request) -> tuple[str, tuple[str, str, str]]:
    """This browser's list view and look, as (view, (colours, mode, picture)); an unknown or damaged cookie gives
    the defaults: Cards, and forest and ochre (G) as the device, on the pines."""
    view = request.cookies.get(VIEW_COOKIE)
    parts = request.cookies.get(LOOK_COOKIE, "").split(".")
    if len(parts) == 2:  # a cookie from before the picture choice: the default picture
        parts.append(DEFAULT_LOOK[2])
    scheme, mode, picture = parts if len(parts) == 3 else ("", "", "")
    known = scheme in SCHEMES and mode in MODES and picture in PICTURES
    look = (scheme, mode, picture) if known else DEFAULT_LOOK
    return view if view in VIEWS else "cards", look


def _list_page(address: str) -> str:
    """The list page to go back to: only the dashboard's own list pages, else the Jobs list. The query is rebuilt
    (an old bookmark's view= dropped), so nothing typed into the form reaches the redirect as it was."""
    try:
        parts = urlsplit(address)
    except ValueError:  # not an address at all (e.g. "//[x"): the Jobs list
        return "/"
    if parts.scheme or parts.netloc or parts.path not in LISTS or len(address) > 4000:
        return "/"
    pairs = [pair for pair in parse_qsl(parts.query, keep_blank_values=True) if pair[0] != "view"]
    return f"{parts.path}?{urlencode(pairs)}" if pairs else parts.path
