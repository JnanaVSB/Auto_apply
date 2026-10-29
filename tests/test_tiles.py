"""The tile reader, on made-up resumes in three common styles (fake people and fake work only)."""

from app.documents.tiles import NO_SECTIONS, read_tiles

RESUME_CLS = r"""\documentclass{resume}
\usepackage[left=0.4in,right=0.4in]{geometry}
\name{Alex Example}
\address{alex@example.com \\ (555) 010-0199 \\ \href{https://example.com/alex}{example.com/alex}}
\begin{document}
% SUMMARY
\begin{rSection}{Summary}
Made-up engineer who builds pretend robots.
\end{rSection}

\begin{rSection}{Education}
\textbf{Example State University} --- M.S. Pretend Robotics,
Fake Concentration \hfill May 2027
\vspace{-0.1}
\textbf{Sample Institute} --- B.S. Imaginary Science \hfill 2020 -- 2024
\end{rSection}

\begin{rSection}{Technical Skills}
\renewcommand{\arraystretch}{0.84}
\begin{tabular}{@{} >{\bfseries}l @{\hspace{2ex}} p{13.8cm}}
Languages & Python, C++ (17, 20) \\
ML \& Robotics & Fake learning, pretend vision
\end{tabular}
\renewcommand{\arraystretch}{1}
\end{rSection}

\begin{rSection}{Research Experience}
\textbf{Pretend Policy Search}
\hfill \textit{Ongoing Research}

\begin{itemize}
    \item Built a made-up pipeline with PI\textsuperscript{2} and 40\% fewer steps.
    \item Compared it with
    \textbf{nothing real}, so this bold line is not an entry.
\end{itemize}
\end{rSection}

\begin{rSection}{Experience}
\begin{rSubsection}{Acme Robotics}{June 2025 -- Aug 2025}{ML Intern}{Phoenix, AZ}
\item Trained fake models.
\end{rSubsection}
\end{rSection}

\begin{rSection}{Projects}
A stray sentence the reader can't place.
\textbf{Toy Driver} \hfill \textit{2-Person Project}
\begin{itemize}
    \item Drove a toy car.
\end{itemize}
Another stray line, after the bullets.
\end{rSection}
\end{document}
"""

JAKES = r"""\documentclass[letterpaper,11pt]{article}
\begin{document}
\begin{center}
    \textbf{\Huge \scshape Sam Sample} \\ \vspace{1pt}
    \small 555-010-0100 $|$ \href{mailto:sam@example.com}{\underline{sam@example.com}}
\end{center}
\section{Experience}
  \resumeSubHeadingListStart
    \resumeSubheading
      {Fake Corp}{Jan 2024 -- Present}
      {Software Engineer}{Remote}
      \resumeItemListStart
        \resumeItem{Wrote pretend code in \textbf{Python}.}
        \resumeItem{Shipped a made-up feature.}
      \resumeItemListEnd
  \resumeSubHeadingListEnd
\section{Projects}
    \resumeSubHeadingListStart
      \resumeProjectHeading
          {\textbf{Fake Planner} $|$ \emph{Python, Flask}}{2023}
          \resumeItemListStart
            \resumeItem{Planned fake trips.}
          \resumeItemListEnd
    \resumeSubHeadingListEnd
\section{Technical Skills}
 \begin{itemize}[leftmargin=0.15in, label={}]
    \small{\item{
     \textbf{Languages}{: Python, Java} \\
     \textbf{Tools}{: Git, Docker}
    }}
 \end{itemize}
\end{document}
"""

ARTICLE = r"""\documentclass{article}
\begin{document}
Jo Placeholder, jo@example.com
\section*{Publications}
\begin{enumerate}
  \item J. Placeholder and A. Example. A Fake Paper. In FakeConf, 2025. \href{https://example.org/paper}{[pdf]}
  \item J. Placeholder. Another Fake Paper. 2024.
\end{enumerate}
\section*{Honors and Awards}
\begin{itemize}\item Pretend Award, 2023\end{itemize}
\section*{Interests}
Chess and made-up hobbies.
\end{document}
"""


def summary(reading):
    return [(tile.kind, tile.section, tile.facts, list(tile.bullets)) for tile in reading.tiles]


def test_resume_cls_written_by_hand():
    reading = read_tiles(RESUME_CLS)
    assert summary(reading) == [
        ("summary", "Summary", {"name": "Summary"}, ["Made-up engineer who builds pretend robots."]),
        ("education", "Education", {"name": "Example State University",
            "rest": "M.S. Pretend Robotics, Fake Concentration", "right": "May 2027"}, []),
        ("education", "Education", {"name": "Sample Institute", "rest": "B.S. Imaginary Science",
            "right": "2020 – 2024"}, []),
        ("skills", "Technical Skills", {"name": "Languages", "skills": ["Python", "C++ (17, 20)"]}, []),
        ("skills", "Technical Skills", {"name": "ML & Robotics", "skills": ["Fake learning", "pretend vision"]}, []),
        ("experience", "Research Experience", {"name": "Pretend Policy Search", "right": "Ongoing Research"}, [
            "Built a made-up pipeline with PI2 and 40% fewer steps.",
            "Compared it with nothing real, so this bold line is not an entry.",
        ]),
        ("experience", "Experience", {"name": "Acme Robotics", "right": "June 2025 – Aug 2025",
            "second": "ML Intern", "second_right": "Phoenix, AZ"}, ["Trained fake models."]),
        ("project", "Projects", {"name": "Toy Driver", "right": "2-Person Project"}, ["Drove a toy car."]),
    ]  # fmt: skip
    assert reading.unplaced == [
        ("Projects", "A stray sentence the reader can't place."),
        ("Projects", "Another stray line, after the bullets."),
    ]
    assert reading.problem is None


def test_the_header_with_contact_details_is_never_a_tile():
    for latex, name in ((RESUME_CLS, "Alex Example"), (JAKES, "Sam Sample"), (ARTICLE, "Jo Placeholder")):
        reading = read_tiles(latex)
        text = repr(summary(reading)) + repr(reading.unplaced)
        assert name not in text and "@example.com" not in text and "010-01" not in text


def test_jakes_resume():
    reading = read_tiles(JAKES)
    assert summary(reading) == [
        ("experience", "Experience", {"name": "Fake Corp", "right": "Jan 2024 – Present", "second": "Software Engineer",
            "second_right": "Remote"}, ["Wrote pretend code in Python.", "Shipped a made-up feature."]),
        ("project", "Projects", {"name": "Fake Planner | Python, Flask", "right": "2023"}, ["Planned fake trips."]),
        ("skills", "Technical Skills", {"name": "Languages", "skills": ["Python", "Java"]}, []),
        ("skills", "Technical Skills", {"name": "Tools", "skills": ["Git", "Docker"]}, []),
    ]  # fmt: skip
    assert reading.unplaced == []


def test_plain_article_with_publications_awards_and_other():
    reading = read_tiles(ARTICLE)
    assert summary(reading) == [
        ("publication", "Publications", {"name": "J. Placeholder and A. Example. A Fake Paper. In FakeConf, 2025. "
            "[pdf]", "link": "https://example.org/paper"}, []),
        ("publication", "Publications", {"name": "J. Placeholder. Another Fake Paper. 2024."}, []),
        ("award", "Honors and Awards", {"name": "Pretend Award, 2023"}, []),
        ("other", "Interests", {"name": "Interests"}, ["Chess and made-up hobbies."]),
    ]  # fmt: skip


def test_a_resume_without_sections_goes_to_unplaced_text():
    reading = read_tiles(r"\begin{document}Just some words, no sections.\end{document}")
    assert reading.tiles == [] and reading.problem == NO_SECTIONS
    assert reading.unplaced == [("", "Just some words, no sections.")]


def test_tile_ids_survive_wording_and_date_changes():
    ids = [tile.tile_id for tile in read_tiles(RESUME_CLS).tiles]
    changed = RESUME_CLS.replace("Drove a toy car.", "Drove a toy car fast.").replace("May 2027", "Dec 2027")
    assert [tile.tile_id for tile in read_tiles(changed).tiles] == ids
    assert ids[-1].startswith("proj-") and ids[1].startswith("edu-") and len(set(ids)) == len(ids)
    renamed = RESUME_CLS.replace("Toy Driver", "Toy Pilot")
    assert read_tiles(renamed).tiles[-1].tile_id != ids[-1]


def test_two_entries_with_the_same_name_get_different_ids():
    twice = RESUME_CLS.replace(r"\textbf{Sample Institute}", r"\textbf{Example State University}")
    first, second = (tile.tile_id for tile in read_tiles(twice).tiles if tile.kind == "education")
    assert second == f"{first}-2"
