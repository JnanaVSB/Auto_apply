"""This browser's Table | Cards choice for the job lists and its Look (colours and mode), kept apart from main.py."""

from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from starlette.exceptions import HTTPException

VIEWS = ("table", "cards")  # the job lists' two views; Cards until one is chosen
VIEW_COOKIE = "auto_apply_view"  # named for this app, like the session cookie: cookies on 127.0.0.1 are shared
LISTS = ("/", "/ready")  # the pages with the Table | Cards switch
LOOK_COOKIE = "auto_apply_look"  # this browser's colours and mode, "<colours>.<mode>", e.g. "E.dark"
SCHEMES = {  # the stylesheet's colour schemes (data-palette); the first is the default and needs no attribute
    "today": "Teal (default)",
    "A": "A · Olive and walnut",
    "B": "B · Sage and espresso",
    "C": "C · Moss and cream",
    "D": "D · Mix (A, warmer brown titles)",
    "E": "E · Forest rail and cognac",
    "F": "F · Espresso rail and olive",
    "G": "G · Olive-black and brass",
}
MODES = {"auto": "Auto (as this device)", "light": "Light", "dark": "Dark"}  # data-theme; the first is the default


def add_routes(app: FastAPI) -> None:
    @app.post("/view")
    async def choose_view(request: Request) -> Response:
        """Table or Cards for the job lists, kept in this browser; back to the same list, shown that way."""
        choice = request.state.form.get("view")
        if choice not in VIEWS:
            raise HTTPException(400, "Choose Table or Cards.")
        response = RedirectResponse(_list_page(request.state.form.get("back", "")), 303)
        response.set_cookie(VIEW_COOKIE, choice, max_age=365 * 24 * 60 * 60, path="/", samesite="lax", httponly=True)
        return response

    @app.post("/look")
    async def choose_look(request: Request) -> Response:
        """Colours and mode for every page, kept in this browser; back to the Look section on Settings."""
        scheme, mode = request.state.form.get("scheme"), request.state.form.get("mode")
        if scheme not in SCHEMES or mode not in MODES:
            raise HTTPException(400, "Choose colours and a mode from the lists.")
        response = RedirectResponse("/settings?look=changed#look", 303)
        response.set_cookie(
            LOOK_COOKIE, f"{scheme}.{mode}", max_age=365 * 24 * 60 * 60, path="/", samesite="lax", httponly=True
        )
        return response


def of(request: Request) -> tuple[str, tuple[str, str]]:
    """This browser's list view and look, as (view, (colours, mode)); an unknown or damaged cookie gives the
    defaults: Cards, and teal as the device."""
    view = request.cookies.get(VIEW_COOKIE)
    scheme, _, mode = request.cookies.get(LOOK_COOKIE, "").partition(".")
    look = (scheme, mode) if scheme in SCHEMES and mode in MODES else ("today", "auto")
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
