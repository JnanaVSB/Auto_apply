import pytest

from app.resume import MAX_BYTES, RESUME_FILE, ResumeError, plain_text, read_resume

# A made-up resume: no real person, employer or address.
FAKE_RESUME = r"""\documentclass[11pt]{article}
\newcommand{\resumeItem}[1]{\item\small{#1}}
\begin{document}
\begin{center}
  {\Huge \textbf{Alex Example}} \\ \vspace{2pt}
  \href{mailto:alex@example.com}{alex@example.com} $|$ \url{https://example.com/alex}
\end{center}
\section{Experience} % private note: never shown
\begin{itemize}[leftmargin=0.15in]
  \resumeItem{Trained \textbf{reinforcement learning} agents in \emph{PyTorch}, cutting cost by 30\%}
  \resumeItem{Built R\&D tools for Caf\'e Analytics --- 2024--2025}
\end{itemize}
\section{Skills}
Python, C++, Kubernetes~and Docker
\section{Projects}
\textbf{Gridworld planner}: a made-up project that plans routes for warehouse robots with model-based RL.
\end{document}
This text after the end is ignored.
"""


def write(tmp_path, content: bytes) -> None:
    (tmp_path / RESUME_FILE).write_bytes(content)


def test_plain_text_keeps_words_and_drops_layout():
    assert plain_text(FAKE_RESUME).splitlines() == [
        "Alex Example",
        "alex@example.com |",  # what is inside math signs stays
        "Experience",
        "Trained reinforcement learning agents in PyTorch, cutting cost by 30%",
        "Built R&D tools for Cafe Analytics - 2024-2025",
        "Skills",
        "Python, C++, Kubernetes and Docker",
        "Projects",
        "Gridworld planner: a made-up project that plans routes for warehouse robots with model-based RL.",
    ]


def test_comments_go_but_escaped_percent_and_line_break_before_a_comment_stay():
    assert plain_text("50\\% done % hidden\nend\\\\% hidden too\nnext") == "50% done\nend\nnext"


def test_text_after_a_space_following_a_layout_command_is_kept():
    assert plain_text(r"\vspace{2pt} {\Large Alex}") == "Alex"


def test_accent_commands_keep_a_word_whole():
    assert plain_text(
        r"Fran\c{c}ois Dvo\v{r}\'ak, \O rsted, Stra\ss e, \aa{}ngstr\"om, \l{}\'od\'z, \H{o}\k{e}\u{a}\r{u}"
    ) == ("Francois Dvorak, Orsted, Strasse, angstrom, lodz, oeau")


def test_without_document_markers_the_whole_text_is_read():
    assert plain_text(r"Just \textit{words}") == "Just words"


def test_null_and_control_characters_are_removed():
    assert plain_text("Py\x00thon\x07 and\tRL") == "Python and RL"


def test_broken_latex_never_raises():
    assert plain_text(r"\textbf{unclosed \begin{itemize} \item } } { \\[") == "unclosed\n["


def test_read_resume_returns_text_and_fingerprint(tmp_path):
    write(tmp_path, FAKE_RESUME.encode())
    resume = read_resume(tmp_path)
    assert resume.text == plain_text(FAKE_RESUME)
    assert len(resume.fingerprint) == 64


def test_fingerprint_changes_only_when_the_words_change(tmp_path):
    write(tmp_path, FAKE_RESUME.encode())
    first = read_resume(tmp_path).fingerprint
    write(tmp_path, FAKE_RESUME.replace("% private note", "% another note").encode())
    assert read_resume(tmp_path).fingerprint == first
    write(tmp_path, FAKE_RESUME.replace("Docker", "Podman").encode())
    assert read_resume(tmp_path).fingerprint != first


def test_odd_bytes_are_replaced_not_fatal(tmp_path):
    write(tmp_path, b"\xef\xbb\xbf" + b"caf\xe9 " * 100)  # a byte-order mark and Latin-1 text
    assert read_resume(tmp_path).text.startswith("caf�")


def test_missing_resume_says_where_to_put_it(tmp_path):
    with pytest.raises(ResumeError, match=r"Resume not found: save your LaTeX resume as resume.tex"):
        read_resume(tmp_path)


def test_a_folder_instead_of_a_file_is_reported(tmp_path):
    (tmp_path / RESUME_FILE).mkdir()
    with pytest.raises(ResumeError, match="Could not read resume.tex"):
        read_resume(tmp_path)


def test_huge_file_is_refused(tmp_path):
    write(tmp_path, b"a" * (MAX_BYTES + 1))
    with pytest.raises(ResumeError, match="over 1 MB"):
        read_resume(tmp_path)


def test_empty_looking_resume_is_refused_without_showing_its_text(tmp_path):
    write(tmp_path, rb"\begin{document}\textbf{Secret Name}\end{document}")
    with pytest.raises(ResumeError, match="looks empty") as error:
        read_resume(tmp_path)
    assert "Secret" not in str(error.value)
