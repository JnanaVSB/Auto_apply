"""What a version can cost: every call at the round limit with every discussion turn, from the research's token model
(.claude plan research: council-FINAL), and from your own calls' measured averages once a member has finished 20
versions with the same model. The laptop is free; your own address without a price can't be estimated (None)."""

import sqlcipher3

from app.config import AIRole
from app.llm import Usage, prices

FIRST, REVISION, REVIEW = (6500, 3000), (11500, 3000), (9300, 500)  # tokens read and written per call
LATER, DISCUSSION = 2000, 1500  # more to read: the debate from round 2, the others' objections in a discussion turn
THINKING = {"off": 0, "low": 500, "medium": 1000, "high": 3000, "xhigh": 5000}  # hidden thinking, billed as written
MEASURED_AFTER = 20  # finished versions in which that member used that model
MEASURED = """SELECT avg(s.input_tokens + s.cached_tokens), avg(s.output_tokens), count(DISTINCT s.preparation_id)
    FROM steps s JOIN preparations p ON p.id = s.preparation_id WHERE s.member = ? AND s.model = ? AND s.status = 'done'
    AND p.status = 'done'"""


def version(
    conn: sqlcipher3.Connection, writer: AIRole, members: dict[str, AIRole], rounds: int, turns: int, half: bool
) -> float | None:
    """The most a version can cost, in dollars: the Writer and each member in every round and discussion turn."""
    costs = [member(conn, "writer", writer, rounds, turns, half),
             *(member(conn, name, role, rounds, turns, half) for name, role in members.items())]  # fmt: skip
    return None if None in costs else sum(costs)


def member(conn: sqlcipher3.Connection, name: str, role: AIRole, rounds: int, turns: int, half: bool) -> float | None:
    """The most one member (or the Writer) can cost in a version."""
    if name == "writer":
        calls = [FIRST if number == 1 else REVISION for number in range(1, rounds + 1)]
    else:
        calls = [(REVIEW[0] + (LATER if number > 1 else 0) + (DISCUSSION if turn else 0), REVIEW[1])
                 for number in range(1, rounds + 1) for turn in range(turns + 1)]  # fmt: skip
    total = 0.0
    for reading, writing in calls:
        if (cost := _call(conn, name, role, reading, writing, half)) is None:
            return None
        total += cost
    return total


def _call(
    conn: sqlcipher3.Connection, member: str, role: AIRole, reading: int, writing: int, half: bool
) -> float | None:
    measured = conn.execute(MEASURED, (member, role.model)).fetchone()
    if measured[2] >= MEASURED_AFTER:
        reading, writing = round(measured[0]), round(measured[1])  # your own calls, thinking included
    else:
        writing += THINKING[role.thinking]
    price = prices.price(role, reading, half)
    return None if price is None else prices.cost(price, Usage(reading, 0, writing))
