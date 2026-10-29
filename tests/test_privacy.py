"""The privacy filter, with a made-up person: hiding, putting back, the last check, and refusing without a name."""

import pytest
from test_tiles import RESUME_CLS

from app.documents import privacy
from app.documents.privacy import PrivacyError, PrivacyStop
from app.documents.tiles import Tile, read_tiles

CONTACT = {
    "full_name": "Alex Example", "preferred_name": "Lex", "email": "alex@example.com", "phone": "+1 (555) 010-0199",
    "street": "12 Fake Hill Rd", "github": "https://github.com/alex-example", "linkedin": "",
}  # fmt: skip
PAPER = Tile("pub-0001", "publication", "Publications", {"name": "A. Example and S. Sample. Fake Paper. 2025.",
    "link": "https://example.org/paper"})  # fmt: skip


def shield():
    return privacy.shield(CONTACT, RESUME_CLS, [*read_tiles(RESUME_CLS).tiles, PAPER])


def test_contact_details_become_placeholders():
    hidden = shield().hide(
        "Alex Example (alex@EXAMPLE.com, 555.010.0199, 5550100199) lives at 12 fake hill rd; see "
        "github.com/alex-example/ and https://www.example.org/paper. Authors: A. Example, Example, Alex; Lex wrote it."
    )
    assert hidden == (  # [ME-3] is "Example, Alex", [ME-4] "A. Example", [ME-8] the preferred name alone
        "[ME] ([EMAIL], [PHONE], [PHONE]) lives at [ADDRESS]; see [LINK] and [LINK-2]. "
        "Authors: [ME-4], [ME-3]; [ME-8] wrote it."
    )


def test_each_detail_always_gets_the_same_placeholder():
    first, again = shield(), shield()
    text = "Alex Example, alex@example.com, (555) 010-0199, https://github.com/alex-example"
    assert first.hide(text) == again.hide(text) == "[ME], [EMAIL], [PHONE], [LINK]"


def test_putting_back_restores_the_exact_text():
    text = "A. Example and Lex built it; mail alex@example.com or call +1 (555) 010-0199; https://example.org/paper"
    filter_ = shield()
    hidden = filter_.hide(text)
    assert "Example" not in hidden and filter_.restore(hidden) == text
    assert filter_.restore("[LINK-9] and [ME]") == "[LINK-9] and Alex Example"  # an unknown placeholder stays


def test_the_resume_header_details_are_hidden_too():
    filter_ = privacy.shield({"full_name": "Alex Example"}, RESUME_CLS, [])
    assert filter_.hide("alex@example.com, (555) 010-0199, https://example.com/alex") == "[EMAIL], [PHONE], [LINK]"


def test_no_name_in_the_questionnaire_means_no_filter():
    with pytest.raises(PrivacyError, match="Fill in your full name in the Questionnaire first"):
        privacy.shield({"email": "alex@example.com"}, RESUME_CLS, [])


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
    ],
)
def test_ordinary_resume_text_passes_the_last_check(text):
    privacy.last_check(text)
