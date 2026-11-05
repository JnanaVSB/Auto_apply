"""Hacker News' monthly "Who is hiring?" threads, through its official API: which threads, which comments, which links.

Only reads replies; the phone book makes the requests. Nothing from a comment is kept but the board names it links to.
"""

import html
import re
from contextlib import suppress
from typing import Any

from app.fetch import HN_API
from app.sources import SourceError, board_of

POSTS = f"https://{HN_API}/v0/user/whoishiring.json"
POSTS_TO_READ = 12  # three kinds of thread are posted each month
THREADS = 2  # the newest hiring threads: a thread grows all month, so the one before it is still read
MAX_COMMENTS = 1000  # per thread
HIRING_TITLE = "Ask HN: Who is hiring?"
HREF = re.compile(r'href="([^"]*)"')  # the visible text of a link is shortened, so only its address is read


def item(number: int) -> str:
    return f"https://{HN_API}/v0/item/{number}.json"


def _whole_numbers(values: list) -> list[int]:
    """Only these may go into an address; true and false are not numbers."""
    return [value for value in values if type(value) is int and value > 0]


def _gone(entry: Any) -> bool:
    return not isinstance(entry, dict) or bool(entry.get("deleted") or entry.get("dead"))


def newest_posts(user: Any) -> list[int]:
    """The ids of the posts to look at, newest first, from the whoishiring user's reply."""
    submitted = user.get("submitted") if isinstance(user, dict) else None
    if not isinstance(submitted, list):
        raise SourceError("its list of threads looks different from usual")
    return sorted(_whole_numbers(submitted), reverse=True)[:POSTS_TO_READ]


def is_hiring(post: Any) -> bool:
    if _gone(post) or post.get("type") != "story":
        return False
    title = post.get("title")
    return isinstance(title, str) and title.startswith(HIRING_TITLE)


def comments_of(thread: dict) -> list[int]:
    """The ids of a thread's top-level comments; replies to them are never read."""
    kids = thread.get("kids", [])
    if not isinstance(kids, list) or (kids and not _whole_numbers(kids)):
        raise SourceError("its list of comments looks different from usual")
    return _whole_numbers(kids)[:MAX_COMMENTS]


def looks_changed(comment: Any) -> bool:
    """A reply that is there but isn't a comment as we know it; `None` is an id with nothing behind it."""
    if isinstance(comment, dict):
        return not (comment.get("deleted") or comment.get("dead") or isinstance(comment.get("text"), str))
    return comment is not None


def boards_in(comment: Any) -> set[str]:
    """The boards a comment links to; a comment that is gone, or has no text, gives none."""
    text = None if _gone(comment) else comment.get("text")
    if not isinstance(text, str):
        return set()
    boards = set()
    for address in HREF.findall(text):
        with suppress(ValueError):  # a broken address like https://[x is no link
            boards.add(board_of(html.unescape(address)))
    return boards - {None}
