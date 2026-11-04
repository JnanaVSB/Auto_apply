"""E-mails to check (/tracker/emails), kept apart from main.py: the e-mails the sync couldn't place on a card by itself.
For each you say which card it is about and what it is, or dismiss it; its subject is forgotten either way."""

import re
from collections.abc import Callable

import sqlcipher3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException

from app.gmail import apply, classify, match

PAGE = "/tracker/emails"
MESSAGE_ID = re.compile(r"[0-9a-zA-Z_-]{1,64}")  # Gmail's ids are hex
DONE = {  # ?done=…: fixed words only
    "linked": "Linked to its card.",
    "moved": "Linked, and its card moved.",
    "dismissed": "Dismissed: it stays off the list.",
}


def add_routes(app: FastAPI, page: Callable[..., HTMLResponse]) -> None:
    @app.get(PAGE)
    async def emails_page(request: Request) -> Response:
        conn = request.app.state.conn
        cards = {card["id"]: card for card in match.cards(conn)}
        found = apply.to_check(conn)
        for email in found:  # a card no longer on the board isn't offered
            email["candidates"] = [cards[job] for job in email["candidates"] if job in cards]
        return page(request, "emails.html", emails=found, cards=list(cards.values()), labels=classify.LABELS,
                    notice=DONE.get(request.query_params.get("done", "")))  # fmt: skip

    @app.post(PAGE + "/{message_id}/link")
    async def link(request: Request, message_id: str) -> Response:
        conn, form = request.app.state.conn, request.state.form
        _check(conn, message_id)
        job, label = form.get("job", ""), form.get("label")
        if not job.isascii() or not job.isdigit() or int(job) not in {card["id"] for card in match.cards(conn)}:
            raise HTTPException(400, "Choose a card on the Tracker.")
        if label not in classify.LABELS:
            raise HTTPException(400, "Choose what the e-mail is from the list.")
        moved = apply.decide(conn, message_id, int(job), label)
        return RedirectResponse(f"{PAGE}?done={'moved' if moved else 'linked'}", 303)

    @app.post(PAGE + "/{message_id}/dismiss")
    async def dismiss(request: Request, message_id: str) -> Response:
        _check(request.app.state.conn, message_id)
        apply.dismiss(request.app.state.conn, message_id)
        return RedirectResponse(f"{PAGE}?done=dismissed", 303)


def _check(conn: sqlcipher3.Connection, message_id: str) -> None:
    """An e-mail that isn't on the list (unknown, already decided, or not an id at all) is "Page not found"."""
    if not MESSAGE_ID.fullmatch(message_id) or not apply.on_list(conn, message_id):
        raise HTTPException(404)
