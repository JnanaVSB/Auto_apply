"""The privacy filter, with a made-up person: hiding, putting back, the last check, and refusing without a name."""

import re
import unicodedata

import pytest
from test_tiles import RESUME_CLS

from app.documents import privacy
from app.documents.privacy import PrivacyError, PrivacyStop
from app.documents.tiles import Tile, read_tiles

CONTACT = {
    "full_name": "Alex Example", "preferred_name": "Lex", "email": "alex@example.com", "phone": "+1 (555) 010-0199",
    "street": "12 Fake Hill Rd", "github": "https://github.com/alex-example", "linkedin": "",
}  # fmt: skip
PLACEHOLDERS = re.compile(r"\[ME-[0-9]+\] and \[ME-[0-9]+\] built it")
PAPER = Tile("pub-0001", "publication", "Publications", {"name": "A. Example and S. Sample. Fake Paper. 2025.",
    "link": "https://example.org/paper"})  # fmt: skip


def shield():
    return privacy.shield(CONTACT, [RESUME_CLS], [*read_tiles(RESUME_CLS).tiles, PAPER])


def test_contact_details_become_placeholders():
    hidden = shield().hide(
        "Alex Example (alex@EXAMPLE.com, 555.010.0199, 5550100199) lives at 12 fake hill rd; see "
        "github.com/alex-example/ and https://www.example.org/paper. Authors: A. Example, Example, Alex; Lex wrote it."
    )
    assert hidden == (  # [ME-3] is "Example, Alex", [ME-4] "A. Example", [ME-9] the preferred name alone
        "[ME] ([EMAIL], [PHONE], [PHONE]) lives at [ADDRESS]; see [LINK] and [LINK-2]. "
        "Authors: [ME-4], [ME-3]; [ME-9] wrote it."
    )


def test_each_detail_always_gets_the_same_placeholder():
    first, again = shield(), shield()
    text = "Alex Example, alex@example.com, (555) 010-0199, https://github.com/alex-example"
    assert first.hide(text) == again.hide(text) == "[ME], [EMAIL], [PHONE], [LINK]"


def test_a_draft_keeps_what_each_of_its_placeholders_stood_for():
    assert shield().details("Mail [EMAIL], see [LINK-9]; [EMAIL] again") == {"[EMAIL]": "alex@example.com"}
    assert privacy.restore("[EMAIL] or [ME]", {"[EMAIL]": "old@example.com"}) == "old@example.com or [ME]"


def test_putting_back_restores_the_exact_text():
    text = "A. Example and Lex built it; mail alex@example.com or call +1 (555) 010-0199; https://example.org/paper"
    filter_ = shield()
    hidden = filter_.hide(text)
    assert "Example" not in hidden and filter_.restore(hidden) == text
    assert filter_.restore("[LINK-9] and [ME]") == "[LINK-9] and Alex Example"  # an unknown placeholder stays


def test_the_resume_header_details_are_hidden_too():
    filter_ = privacy.shield({"full_name": "Alex Example"}, [RESUME_CLS], [])
    assert filter_.hide("alex@example.com, (555) 010-0199, https://example.com/alex") == "[EMAIL], [PHONE], [LINK]"


def test_the_headers_of_every_source_are_hidden():
    uploaded = RESUME_CLS.replace("alex@example.com", "lex.work@example.org").replace("(555) 010-0199", "480 555 0123")
    filter_ = privacy.shield({"full_name": "Alex Example"}, [RESUME_CLS, uploaded], [])
    assert filter_.hide("alex@example.com or lex.work@example.org, 480-555-0123") == "[EMAIL] or [EMAIL-2], [PHONE-2]"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Alex Li", "Li, A., Doe, B. (2023) and A. J. Li and B. Doe; A.J. Li; Li, A. J.; Alex J. Li built it"),
        ("Jose Example", "José Example and JOSÉ EXAMPLE"),
        ("José Example", "Jose Example and J. Exámple"),
        ("François Dvořák", "Francois Dvorak and Dvorak, F."),
        ("Søren Kierkegaard-Test", "Soren Kierkegaard-Test"),
    ],
)
def test_short_surnames_middle_initials_and_accents_are_hidden(name, text):
    hidden = privacy.shield({"full_name": name}, [], []).hide(text)
    assert not {"Li", "Jose", "José", "JOSÉ", "Exámple", "Dvorak", "Soren"} & set(hidden.replace(",", " ").split())


def test_the_name_at_the_top_of_each_source_is_hidden_too():
    jake = r"\begin{document}\begin{center}\textbf{\Huge \scshape Sam Sample-Person}\end{center}\section{Work}"
    filter_ = privacy.shield({"full_name": "Alexander Example"}, [RESUME_CLS, jake], [])
    assert PLACEHOLDERS.fullmatch(filter_.hide("Alex Example and S. Sample-Person built it"))


@pytest.mark.parametrize("form", ["NFC", "NFD"])  # an accent as one character, or as a letter and a mark
def test_a_spelling_found_in_the_sources_comes_back_exactly(form):
    authors = unicodedata.normalize(form, "A. J. Example and J. Exámple. Fake. 2025.")
    filter_ = privacy.shield(
        CONTACT, [RESUME_CLS], [Tile("pub-0002", "publication", "Publications", {"name": authors})]
    )
    hidden = filter_.hide(authors)
    assert "Ex" not in hidden and filter_.restore(hidden) == unicodedata.normalize("NFC", authors)


def test_an_accent_written_as_a_letter_and_a_mark_is_one_name():
    assert privacy.shield({"full_name": "José Example"}, [], []).hide("Jose\u0301 Example") == "[ME]"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Contact alexander.example@tum.de", "an email address"),
        ("write to example@lab.fake-uni.ac.jp", "an email address"),
        ("Contact alexander.example@uni.museum", "an email address"),  # an ending the web check doesn't know
        ("write to example@LAB.UNI.AC.JP", "an email address"),
        ("Alex Example, 480–555–0100", "a phone number"),
    ],
)
def test_text_leaves_only_hidden_and_checked(text, kind):
    with pytest.raises(PrivacyStop) as stop:  # the placeholder inside the address must not hide it from the last check
        shield().outgoing(text)
    assert str(stop.value) == kind


def test_outgoing_text_is_the_hidden_text():
    assert shield().outgoing("Alex Example built it with Lex.") == "[ME] built it with [ME-9]."


def test_no_name_in_the_questionnaire_means_no_filter():
    with pytest.raises(PrivacyError, match="Fill in your full name in the Questionnaire first"):
        privacy.shield({"email": "alex@example.com"}, [RESUME_CLS], [])


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Reach me at sam@sample.org for details.", "an email address"),
        ("Call 480-555-0123 any time.", "a phone number"),
        ("Call +44 20 7946 0958.", "a phone number"),
        ("Code at https://example.net/x", "a web address"),
        ("Demo on www.sample.dev", "a web address"),
        ("See fake-project.io/demo", "a web address"),
        ("Moved to 221 Baker Street last year.", "a street address"),
        ("Call 480–555–0123 any time.", "a phone number"),  # en dashes
        ("Call 480\u2011555\u20110123 any time.", "a phone number"),  # non-breaking hyphens
        ("Call 480\u2009555\u00a00123 any time.", "a phone number"),  # thin and non-breaking spaces
        ("Site: sam-sample.de", "a web address"),
        ("Blog at sam.tech and sam-sample.ca", "a web address"),
        ("See Example.com for more", "a web address"),
        ("See SAMPLE.ORG/sam", "a web address"),
        ("Contact x@lab.fake-uni.ac.jp", "an email address"),
    ],
)
def test_the_last_check_stops_contact_like_items_and_names_only_the_kind(text, kind):
    with pytest.raises(PrivacyStop) as stop:
        privacy.last_check(text)
    assert str(stop.value) == kind


@pytest.mark.parametrize(
    "text",
    [
        "Built it in 2020 – 2024 with 40% fewer steps and 10,000,000 samples.",
        "Used Node.js, PyTorch 2.5.1 and CUDA 12.4 on 8 GPUs, e.g. for [LINK-1] by [ME].",
        "Improved 3 metrics by 25 percent along the lane.",
        "Ran from 2019 - 2023 (4 years) on 2 sites.",  # a year range with plain dashes is not a phone
        "Built REST APIs in ASP.NET Core and VB.NET, with ADO.NET and ML.NET.",  # tech names, not web addresses
        "Real-time chat with Socket.IO and socket.io; took the fast.ai course.",
        "Wrote run.sh, main.rs and lib.cc scripts from 2020–2024.",
    ],
)
def test_ordinary_resume_text_passes_the_last_check(text):
    privacy.last_check(text)
