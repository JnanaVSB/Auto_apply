"""What the export's made files say: each PDF as a title and sections for `latex.report`, which escapes every value, and
the status history's text. Reads only. Damaged JSON raises: the run says so for that file and carries on."""

import json
from datetime import datetime

import sqlcipher3

from app import tracker
from app.council import runs
from app.submit import record
from app.tracker import ROUNDS, STAGES

NOT_RECORDED = "this copy is not the file recorded"
GONE = "the app no longer holds this file: it can't show which PDF you sent"
NO_RECORD = (
    "No record of what was sent: this application wasn't recorded with I submitted it. Any resume here is the newest "
    "the app prepared and may not be what you sent."
)
NO_FILES = "No files were recorded"
POSTING = "The posting's text as the app last read it"
COUNCIL_TITLE = "Scores and council notes"
SCORES = "SELECT model, score, reasons, failed, scored_on FROM ai_scores WHERE job_id = ? ORDER BY scored_on, model"
QUICK = "SELECT passed, title_only, keywords, job_skills, shared_skills, meaning, trick FROM stage_a WHERE job_id = ?"


def history(conn: sqlcipher3.Connection, job_id: int, recorded: dict | None) -> str:
    """One line a move, oldest first, so a later move only adds lines at the end. The opening Applied line stands for
    the oldest Ready → Applied move, dated when you applied, which then isn't listed again. With no record it needs
    that move, and the card's date: nobody said you applied to a card dragged from Ready to Rejected."""
    moves = tracker.history(conn, job_id)[::-1]
    first = next((move for move in moves if (move["from_stage"], move["to_stage"]) == ("ready", "applied")), None)
    card = conn.execute("SELECT applied_on FROM tracker WHERE job_id = ?", (job_id,)).fetchone()
    applied = recorded["submitted_on"] if recorded else card and first and card[0]
    lines = [] if recorded else [NO_RECORD]
    lines += [f"{applied}  {STAGES['applied']}"] if applied else []
    lines += [f"{day(move['moved_on'])}  {_move(move)}" for move in moves if move is not first or not applied]
    return "".join(f"{line}\n" for line in lines)


def filled_answers(job: dict, recorded: dict, moved_back: str | None, files: dict[str, str]) -> tuple[str, list]:
    """The frozen record as a paper. `moved_back` is a local date; `files` says of each slot's copy in the export
    folder whether it is the "same" as recorded, "different" or "gone"."""
    title = record.heading(recorded)
    opening = [job["company"], job["title"], f"Applied {recorded['submitted_on']}", recorded["address"]]
    opening += [record.caution(recorded)]
    opening += [record.MOVED_BACK.format(moved_back)] if moved_back else []
    answers = [(row["question"], _answer(row)) for row in recorded["fields"]]
    return title, [("", "\n".join(opening)), *answers, ("Files", _files(recorded["files"], files))]


def job_description(job: dict) -> tuple[str, list] | None:
    """The posting as the app holds it; None with no saved text."""
    if not (job["description"] or "").strip():
        return None
    details = (job["company"], ", ".join(json.loads(job["locations"])), job["apply_url"])
    return job["title"], [("", "\n".join(filter(None, details))), (POSTING, job["description"])]


def council_notes(conn: sqlcipher3.Connection, job_id: int, versions: list[tuple[str, dict | None]]) -> tuple | None:
    """The scores, the quick check and each version's last round, as the job's page words them; None with nothing to
    say. Placeholders such as [ME] stay as they are, so this paper holds fewer of your details."""
    sections = [*_scores(conn, job_id), *_quick(conn, job_id)]
    for label, version in versions:
        if version is not None:
            sections.append(_council(conn, label, version))
    return (COUNCIL_TITLE, sections) if sections else None


def file_notes(row: dict, state: str | None) -> list[str]:
    """What is true of a recorded file now, in words: for the paper and the run's problem lines alike."""
    notes = [record.ANOTHER_FILE] if row.get("changed") else []
    notes += [record.NO_COPY] if row["note"] else [GONE] if state == "gone" else []
    return notes + [NOT_RECORDED] if state == "different" else notes


def day(moment: str) -> str:
    return datetime.fromisoformat(moment).astimezone().date().isoformat()


def _move(move: dict) -> str:
    words = STAGES[move["to_stage"]]
    words += f", {ROUNDS[move['round']]}" if move["round"] else ""
    return f"{words} (by Gmail)" if move["moved_by"] == "gmail" else words


def _answer(row: dict) -> str:
    """The value, or its "from" words if there is none, and the note: a record with no form read has no note."""
    value, source = row["value"], row["from"]
    lines = [source] if not value.strip() else [value, *([f"From: {source}"] if source and source != value else [])]
    return "\n".join([*lines, *filter(None, [row.get("note")])])


def _files(rows: list[dict], states: dict[str, str]) -> str:
    lines = []
    for row in rows:
        notes = file_notes(row, states.get(row["slot"]))
        lines += [
            row["label"],
            *([f"SHA-256: {row['sha256']}"] if row["sha256"] else []),
            *notes,
        ]
    return "\n".join(lines) or NO_FILES


def _scores(conn: sqlcipher3.Connection, job_id: int) -> list[tuple[str, str]]:
    lines = []
    for model, score, reasons, failed, scored_on in conn.execute(SCORES, (job_id,)).fetchall():
        when = day(scored_on)
        lines.append(
            f"{model}: no score ({failed}), tried on {when}" if score is None else f"{model}: {score}, scored on {when}"
        )
        lines += json.loads(reasons)
    return [("AI scores", "\n".join(lines))] if lines else []


def _quick(conn: sqlcipher3.Connection, job_id: int) -> list[tuple[str, str]]:
    row = conn.execute(QUICK, (job_id,)).fetchone()
    if row is None:
        return []
    passed, title_only, keywords, job_skills, shared, meaning, trick = row
    shared = json.loads(shared)
    result = "Passed" if passed else "Did not pass"
    result += " (title only: the job has no description)" if title_only else ""
    skills = f"{len(shared)} of the job's {job_skills} skills are on your resume"
    skills += f" ({', '.join(shared)})" if shared else ""
    line = (f"{result}. Your keywords in the title: {', '.join(json.loads(keywords)) or 'none'}. {skills}. "
            f"Meaning match {meaning:.2f} (0 to 1).")  # fmt: skip
    return [("Quick check", line + (f"\nAimed at AI tools: score looks odd: the ad says “{trick}”" if trick else ""))]


def _council(conn: sqlcipher3.Connection, label: str, version: dict) -> tuple[str, str]:
    notes = runs.notes(conn, version)
    if notes is None:
        return "", f"{label}: made by the Writer alone"
    lines = []
    for member in notes["members"]:
        lines += _member(member, notes["votes"])
    if notes["replies"]:
        replies = (f"{reply['objection']} {reply['action']} ({reply['why']})" for reply in notes["replies"])
        lines.append(f"The Writer's replies: {'; '.join(replies)}")
    if notes["missed"]:
        missed = (f"{item['objection']} ({item['severity']})" for item in notes["missed"])
        lines.append(f"No reply from the Writer: {', '.join(missed)}")
    for name, slips in notes["renumbered"]:
        changed = (f"{slip['wrote']} to {slip['now']}" for slip in slips)
        lines.append(f"{name} wrote numbers not its own; the app renumbered: {', '.join(changed)}")
    return f"{label} (round {notes['round']})", "\n".join(lines)


def _member(member: dict, votes: dict) -> list[str]:
    answer = member["answer"]
    if answer is None:
        return [f"{member['name']}: no valid answer"]
    summary = f". {answer['summary']}" if answer.get("summary") else ""
    lines = [f"{member['name']}: {answer['verdict']}{summary}"]
    for item in answer["objections"]:
        where = f", {item['where']}" if item.get("where") else ""
        quote = f" Your original: “{item['quote']}”" if item.get("quote") else ""
        lines.append(f"{item['id']} ({item['severity']}{where}): {item['problem']} Fix: {item['fix']}{quote}")
        lines += [
            f"{voter} {'agrees' if agree else 'disagrees'}: {why}" for voter, agree, why in votes.get(item["id"], [])
        ]
    return lines
