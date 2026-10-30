"""Money for paid AI calls, added up from the saved steps (one per call) and kept within your daily and monthly limits.

Before a paid call, its largest possible cost is set aside; it is sent only if that still fits within both limits.
Once it answers, the amount set aside becomes what it really cost. Ollama on this computer is free and never limited;
your own address without a price is "not counted" (its cost is NULL), so the limits can't cover it."""

import math
from datetime import date, datetime, timedelta
from typing import NamedTuple

import sqlcipher3

from app.config import AIRole, SpendingSettings
from app.llm import Usage, most_output, prices

BYTES_PER_TOKEN = 2  # a token is usually about 4 English letters: 2 bytes over-estimates on purpose
CENT = 0.005  # rounding: a total that shows as the limit is still within it
SPENT = """SELECT coalesce(sum(coalesce(cost, 0) + reserved) FILTER (WHERE day = ?), 0),
    coalesce(sum(coalesce(cost, 0) + reserved), 0) FROM steps WHERE substr(day, 1, 7) = ?"""
OF_JOB = """SELECT coalesce(sum(coalesce(cost, 0) + reserved), 0), coalesce(max(cost IS NULL), 0)
    FROM steps JOIN preparations ON preparations.id = steps.preparation_id WHERE preparations.job_id = ?"""
DAYS = """SELECT day, count(*), sum(input_tokens), sum(cached_tokens), sum(output_tokens),
    sum(coalesce(cost, 0) + reserved), max(cost IS NULL) FROM steps GROUP BY day ORDER BY day DESC"""


class Spent(NamedTuple):
    today: float
    month: float


class Period(NamedTuple):
    start: str  # its first day: the day itself, the Monday of a week, the 1st of a month, January 1st
    calls: int  # AI calls, local and paid
    input: int  # tokens read, at the normal price
    cached: int  # tokens read from a service's cache
    output: int  # tokens written, thinking included
    dollars: float  # spent, and set aside for calls still waiting
    uncounted: bool  # some calls were not counted (your own address without a price): dollars is "at least"


def today() -> str:
    """The date on this computer's clock: spending is counted per day and month in your own time zone."""
    return datetime.now().astimezone().date().isoformat()


def spent(conn: sqlcipher3.Connection, day: str) -> Spent:
    """Dollars spent or set aside on `day` and in its month."""
    return Spent(*conn.execute(SPENT, (day, day[:7])).fetchone())


def largest(role: AIRole, texts: str, answer_limit: int | None = None) -> float | None:
    """The most a request sending `texts` can cost: generously counted, at the price for that length; None when the
    price isn't known."""
    reading = math.ceil(len(texts.encode()) / BYTES_PER_TOKEN)
    price = prices.price(role, reading)
    return None if price is None else prices.cost(price, Usage(reading, 0, most_output(role, answer_limit)))


def charged(role: AIRole, usage: Usage) -> float | None:
    """What a request really cost, from the tokens the service reported; None when the price isn't known."""
    price = prices.price(role, usage.input + usage.cached)
    return None if price is None else prices.cost(price, usage)


def refusal(
    conn: sqlcipher3.Connection, role: AIRole, cost: float | None, limits: SpendingSettings, day: str
) -> str | None:
    """Why a request that could cost `cost` must not be sent; None when it may. Ask inside the transaction that saves
    its step, so that two requests can't both fit in the same room."""
    if role.service == "ollama":
        return None
    if cost is None:
        if role.own:
            return None  # your own address without a price: not counted (Settings warns)
        return (
            f"{role.model} is not on the app's price list, so its cost can't be counted and the spending limits "
            "can't protect you: choose a model from the list in Settings."
        )
    found = spent(conn, day)
    if found.today + cost > limits.daily + CENT:
        return (
            f"Paused: today's spending limit of ${limits.daily:.2f} would be passed (${found.today:.2f} spent today; "
            f"this call could cost up to ${cost:.2f}). It continues tomorrow, or raise the limit in Settings."
        )
    if found.month + cost > limits.monthly + CENT:
        return (
            f"Paused: this month's spending limit of ${limits.monthly:.2f} would be passed (${found.month:.2f} spent "
            f"this month; this call could cost up to ${cost:.2f}). It continues next month, or raise the limit in "
            "Settings."
        )
    return None


def of_job(conn: sqlcipher3.Connection, job_id: int) -> tuple[float, bool]:
    """Dollars spent on a job so far, and whether some of it was not counted (so the total is "at least")."""
    dollars, uncounted = conn.execute(OF_JOB, (job_id,)).fetchone()
    return dollars, bool(uncounted)


def history(conn: sqlcipher3.Connection, period: str) -> list[Period]:
    """Your AI use for every day, week (Monday to Sunday), month or year that had calls, newest first. Steps are
    never deleted, so the history goes back to the first call."""
    found: dict[str, list] = {}
    for day, *numbers in conn.execute(DAYS):
        totals = found.setdefault(_start(day, period), [0, 0, 0, 0, 0.0, False])
        totals[:5] = [total + number for total, number in zip(totals[:5], numbers[:5], strict=True)]
        totals[5] = totals[5] or bool(numbers[5])
    return [Period(start, *totals) for start, totals in found.items()]


def _start(day: str, period: str) -> str:
    if period == "week":
        found = date.fromisoformat(day)
        return (found - timedelta(days=found.weekday())).isoformat()
    return {"day": day, "month": f"{day[:7]}-01", "year": f"{day[:4]}-01-01"}[period]
