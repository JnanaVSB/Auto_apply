"""Phase 4 pages, kept apart from main.py: the questionnaire and the tiles (later also Ready to Apply)."""

from collections.abc import Callable
from pathlib import Path

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import questionnaire
from app.documents import tile_sets
from app.documents.tiles import PREFIXES, header_text, read_tiles
from app.resume import ResumeError, from_latex, read_latex

CHOICES = {  # the questionnaire's drop-down choices
    "STATUSES": questionnaire.STATUSES, "GENDERS": questionnaire.GENDERS, "RACES": questionnaire.RACES,
    "VETERAN": questionnaire.VETERAN, "DISABILITY": questionnaire.DISABILITY, "MAX_PAIRS": questionnaire.MAX_PAIRS,
}  # fmt: skip


RESUME_CHANGED = "Your resume changed while this page was open: reload the page (nothing was confirmed)."


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse], data_dir: Path) -> None:
    @app.get("/questionnaire")
    async def questionnaire_page(request: Request) -> Response:
        values, saved_on, problem = questionnaire.load(request.app.state.conn)
        return page(
            request, "questionnaire.html", **CHOICES, values=values, saved_on=saved_on, stale=problem,
            saved="saved" in request.query_params,
        )  # fmt: skip

    @app.post("/questionnaire")
    async def save_questionnaire(request: Request) -> Response:
        values = questionnaire.typed(request.state.form)
        try:
            questionnaire.save(request.app.state.conn, questionnaire.check(values))
        except questionnaire.QuestionnaireError as error:  # nothing saved; what was typed stays on the page
            return page(request, "questionnaire.html", **CHOICES, values=values, problem=str(error))
        return RedirectResponse("/questionnaire?saved=1", 303)  # so reloading the page never saves twice

    @app.get("/questionnaire/download")
    async def download_questionnaire(request: Request) -> Response:
        values, _, _ = questionnaire.load(request.app.state.conn)
        return Response(
            questionnaire.as_file(values), media_type="application/yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="questionnaire.yaml"', "Cache-Control": "no-store"},
        )  # fmt: skip

    @app.get("/tiles")
    async def tiles_page(request: Request) -> Response:
        view = _tiles_view(request.app.state.conn, data_dir)
        notice = {"confirmed": "Tiles confirmed.", "unchanged": "Nothing changed, so nothing was saved."}
        return page(
            request, "tiles.html", **view, notice=next((notice[n] for n in notice if n in request.query_params), None)
        )

    @app.post("/tiles")
    async def confirm_tiles(request: Request) -> Response:
        form, conn = request.state.form, request.app.state.conn
        rows, view = tile_sets.typed(form), _tiles_view(conn, data_dir)
        try:
            if view.get("resume") != form.get("resume"):
                raise tile_sets.TilesError(RESUME_CHANGED)
            set_id = tile_sets.confirm(conn, tile_sets.check(rows), view["resume"])
        except tile_sets.TilesError as error:  # nothing saved; what was typed stays on the page
            return page(
                request, "tiles.html", **view | {"rows": rows, "resume": form.get("resume")}, problem=str(error)
            )
        return RedirectResponse(f"/tiles?{'confirmed' if set_id else 'unchanged'}=1", 303)  # reloading never confirms


def _tiles_view(conn: sqlcipher3.Connection, data_dir: Path) -> dict:
    """What the Tiles page shows: your confirmed tiles if made from the current resume, else a fresh reading."""
    try:
        latex = read_latex(data_dir)
        resume = from_latex(latex).fingerprint
    except ResumeError as error:
        return {"resume_problem": str(error), "rows": [], "kinds": PREFIXES}
    reading, confirmed = read_tiles(latex), tile_sets.latest(conn)
    current = confirmed is not None and confirmed.resume == resume
    tiles = confirmed.tiles if current else reading.tiles
    contact = questionnaire.load(conn)[0]["contact"]
    return {
        "resume": resume, "confirmed": confirmed, "current": current, "kinds": PREFIXES,
        "rows": [tile_sets.as_row(tile, position) for position, tile in enumerate(tiles, 1)],
        "changes": tile_sets.changes(confirmed.tiles, reading.tiles) if confirmed and not current else [],
        "unplaced": reading.unplaced, "reading_problem": reading.problem,
        "contact_empty": not any(contact.get(field) for field, _ in tile_sets.CONTACT),
        "contact_missing": tile_sets.contact_check(contact, header_text(latex)),
    }  # fmt: skip
