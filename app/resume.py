"""Your resume as plain text, read on this computer only: never logged, never put in a message, never sent anywhere."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

RESUME_FILE = "resume.tex"  # in the data folder
MAX_BYTES = 1024 * 1024  # a one-page LaTeX resume is about 5-15 KB
MIN_CHARACTERS = 300  # less plain text than this is not a usable resume
WHERE = "resume.tex in your data folder (normally data/resume.tex)"

COMMENT = re.compile(r"(?<!\\)((?:\\\\)*)%.*")  # % to the end of the line; \% is a real percent sign
GROUP = r"\{(?:[^{}]|\{[^{}]*\})*\}"  # one {...} argument, which may hold one more level of braces
OPTION = r"\[[^\]\n]*\]"  # one [...] option
LAYOUT = r"vspace|hspace|setlength|includegraphics|color|url|label"
# Layout and links, not words: an environment's name, and layout commands with the arguments right after them.
# Arguments after a space are kept, so a name written as \vspace{2pt} {\Large Name} is never lost.
DROPPED = re.compile(
    rf"\\(?:begin|end)\s*\{{\w+\*?\}}(?:{OPTION})?|\\(?:{LAYOUT})(?![A-Za-z@])\*?(?:{OPTION}|{GROUP})*"
)
FIRST_DROPPED = re.compile(rf"\\(?:href|textcolor)(?![A-Za-z@])\s*{GROUP}")  # the link or colour; the text stays
LINE_BREAK = re.compile(rf"\\\\\*?(?:{OPTION})?")
ACCENT = re.compile(r"\\['`^\"~=.]\{?([A-Za-z])\}?")  # \'e -> e
COMMAND = re.compile(rf"\\[A-Za-z@]+\*?(?:{OPTION})?")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # null and other invisible characters (keeps tabs, new lines)
SPACES = re.compile(r"[ \t]+")


class ResumeError(Exception):
    """The resume is missing, unreadable or unusable. Messages never include its text."""


@dataclass(frozen=True)
class Resume:
    text: str
    fingerprint: str  # SHA-256 of the text: a changed resume means scores made with the old one are out of date


def read_resume(data_dir: Path) -> Resume:
    try:
        with (data_dir / RESUME_FILE).open("rb") as file:
            raw = file.read(MAX_BYTES + 1)
    except FileNotFoundError:
        raise ResumeError(f"Resume not found: save your LaTeX resume as {WHERE}.") from None
    except OSError as error:  # no permission, a folder of that name, ...
        raise ResumeError(f"Could not read {WHERE} ({error.strerror or type(error).__name__}).") from None
    if len(raw) > MAX_BYTES:
        raise ResumeError(f"{WHERE} is over {MAX_BYTES // 2**20} MB, which does not look like a resume.")
    text = plain_text(raw.decode("utf-8-sig", errors="replace"))  # odd bytes become a placeholder, never a crash
    if len(text) < MIN_CHARACTERS:
        raise ResumeError(f"Your resume looks empty (almost no text after removing LaTeX); check {WHERE}.")
    return Resume(text, hashlib.sha256(text.encode()).hexdigest())


def plain_text(latex: str) -> str:
    """LaTeX to plain words, without compiling it: broken LaTeX leaves a few stray symbols, never an error."""
    text = COMMENT.sub(r"\1", latex)
    text = text.partition(r"\begin{document}")[2] or text  # the preamble holds settings and home-made commands
    text = text.partition(r"\end{document}")[0]
    text = FIRST_DROPPED.sub("", DROPPED.sub(" ", text))
    text = LINE_BREAK.sub("\n", text)
    text = re.sub(r"(?<!\\)[$&]", " ", text)  # math signs and table columns, before \$ and \& become plain
    text = re.sub(r"\\([&%$#_])", r"\1", text)
    text = ACCENT.sub(r"\1", text)
    text = COMMAND.sub(" ", text)
    text = text.replace("~", " ").replace("---", "-").replace("--", "-").replace("{", "").replace("}", "")
    text = CONTROL.sub("", text)
    lines = (SPACES.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)
