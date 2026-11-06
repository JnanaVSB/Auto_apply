"""The compare and the pack: what the browser says the form shows, field by field against the saved read, and what
goes out. Made-up forms only."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_submit_lever import PAGE as LEVER_PAGE

from app.submit import Choice, Field, Form, ashby, compare, greenhouse, lever, match

# What the browser says the form shows


GONE, NEW = "no longer on the form", "new on the form"
WORDS, KIND, REQUIRED = "the question's words changed", "the kind of box changed", "whether it's required changed"
CHOICES, CHOICES_UNCHECKED = "its choices changed", "its choices weren't checked"
FILE_UNCHECKED, UNCHECKED = "the app only checked that it's a file box", "wasn't checked"
RESUME_PDF = "/fake/folder/resume-v3.pdf"
DOCUMENTS = {"resume": ("Resume v3", Path(RESUME_PDF)), "cover_letter": None}
GOOD_BOX = {"key": "email", "label": "Email", "kind": "text", "required": True}


def box(key, label, kind="text", required=False, **rest):
    return {"key": key, "label": label, "kind": kind, "required": required} | rest


def offered(*labels):
    return [{"label": label, "value": f"site-{number}"} for number, label in enumerate(labels)]


def lines_of(form, values):
    """A line for each field: filled with `values[key]`, else empty, and a hidden one hidden."""
    return tuple(
        match.Line(field, "hidden") if field.kind == "hidden"
        else match.Line(field, "fill", values[field.key]) if field.key in values
        else match.Line(field, "empty")
        for field in form.fields
    )  # fmt: skip


def compared(lines, boxes, board=lever.DRAWN):
    return compare.compare(lines, compare.fields_from(boxes), board)


def keys(passed):
    return [line.field.key for line, _ in passed]


def said(changes):
    return {change["question"]: change["what"] for change in changes}


# Greenhouse, as the plan's "Real forms read early" found it: the app's read, then what the browser draws
GREENHOUSE_REPLY = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Email", "required": True, "fields": [{"name": "email", "type": "input_text"}]},
        {"label": "Resume/CV", "required": True, "fields": [
            {"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"},
        ]},
        {"label": "Need sponsorship?", "required": True, "fields": [
            {"name": "question_1", "type": "multi_value_single_select",
             "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]},
        ]},
    ],
    "compliance": [{"questions": [{"label": "Race", "required": False, "fields": [
        {"name": "race", "type": "multi_value_single_select", "values": [{"label": "Decline", "value": 9}]},
    ]}]}],
    "demographic_questions": {"questions": [
        {"id": 7, "label": "Pronouns", "type": "multi_value_single_select",
         "answer_options": [{"id": 1, "label": "He"}]},
    ]},
    "data_compliance": [{"type": "gdpr", "requires_consent": True}],
    "education": "education_optional",
}  # fmt: skip
GREENHOUSE_DRAWN = [
    box("first_name", "First Name*", required=True),
    box("email", "Email*", required=True),
    box("resume", "Attach", "file"),
    box("country", "Country*", "single", True),
    box("question_1", "Need sponsorship?*", "single", True),
    box("hispanic_ethnicity", "Are you Hispanic or Latino?", "single"),
]
GREENHOUSE_VALUES = {
    "first_name": "Fake", "email": "person@example.com", "resume": RESUME_PDF, "question_1": ("1",), "race": ("9",),
    "demographic_7": ("1",), "data_compliance[gdpr][requires_consent]": "yes", "school": ("1",),
}  # fmt: skip


def test_a_greenhouse_form_as_it_is_really_drawn():
    form, _ = greenhouse.parse(GREENHOUSE_REPLY)
    passed, changes = compared(lines_of(form, GREENHOUSE_VALUES), GREENHOUSE_DRAWN, greenhouse.DRAWN)
    assert keys(passed) == ["first_name", "email", "resume", "question_1"]
    assert compare.pack(passed, DOCUMENTS) == [
        {"key": "first_name", "kind": "text", "value": "Fake"},
        {"key": "email", "kind": "text", "value": "person@example.com"},
        {"key": "resume", "kind": "file", "value": {"slot": "resume"}},
        {"key": "question_1", "kind": "single", "value": "Yes"},
    ]
    assert said(changes) == {
        "Resume/CV": FILE_UNCHECKED,
        "Need sponsorship?": CHOICES_UNCHECKED,
        "Race": UNCHECKED,
        "Pronouns": UNCHECKED,
        "Consent to the privacy notice (GDPR)": UNCHECKED,
        "School": UNCHECKED,
        "Degree": UNCHECKED,
        "Discipline": UNCHECKED,
        "Country*": UNCHECKED,  # as the page words it, like anything the saved read doesn't have
        "Are you Hispanic or Latino?": UNCHECKED,
    }


def test_greenhouse_s_undrawn_fields_don_t_count_as_a_match():
    form, _ = greenhouse.parse(GREENHOUSE_REPLY)
    drawn_only_unlisted = [box("country", "Country*", "single", True)]
    passed, changes = compared(lines_of(form, GREENHOUSE_VALUES), drawn_only_unlisted, greenhouse.DRAWN)
    assert passed == []
    assert said(changes)["First Name"] == GONE and said(changes)["Race"] == UNCHECKED


def test_a_lever_form_as_it_is_really_drawn():
    form, _ = lever.parse(LEVER_PAGE)
    values = {
        "resume": RESUME_PDF, "name": "Fake Person", "email": "person@example.com",
        "urls[LinkedIn]": "https://linkedin.example/fake", "cards[c-1][field2]": ("Yes",),
        "cards[c-1][field3]": ("Robotics", "Search"),
    }  # fmt: skip
    boxes = [
        box("resume", "Resume/CV ✱", "file"),
        box("name", "Full name ✱", required=True),
        box("email", "Email ✱", required=True),
        box("urls[LinkedIn]", "LinkedIn URL"),
        box("cards[c-1][field2]", "Need sponsorship?", "single", True, choices=offered("Yes", "No")),
        box("cards[c-1][field3]", "Which teams?", "multi", choices=offered("Robotics", "Search")),
    ]
    passed, changes = compared(lines_of(form, values), boxes)
    assert compare.pack(passed, DOCUMENTS) == [
        {"key": "resume", "kind": "file", "value": {"slot": "resume"}},
        {"key": "name", "kind": "text", "value": "Fake Person"},
        {"key": "email", "kind": "text", "value": "person@example.com"},
        {"key": "urls[LinkedIn]", "kind": "text", "value": "https://linkedin.example/fake"},
        {"key": "cards[c-1][field2]", "kind": "single", "value": "Yes"},
        {"key": "cards[c-1][field3]", "kind": "multi", "value": ["Robotics", "Search"]},
    ]
    assert said(changes)["Resume/CV"] == FILE_UNCHECKED
    assert said(changes)["Phone"] == GONE  # Lever draws everything it has
    assert "selectedLocation" not in said(changes) and "origin" not in said(changes)  # hidden: never looked at


def test_an_ashby_form_as_it_is_really_drawn():
    form = Form((
        Field("_systemfield_name", "Name", "text", True, "standard"),
        Field("_systemfield_resume", "Resume", "file", True, "standard"),
    ))  # fmt: skip
    boxes = [box("_systemfield_name", "Name*", required=True), box("_systemfield_resume", "Upload File", "file")]
    values = {"_systemfield_name": "Fake Person", "_systemfield_resume": RESUME_PDF}
    passed, changes = compared(lines_of(form, values), boxes, ashby.DRAWN)
    assert keys(passed) == ["_systemfield_name", "_systemfield_resume"]
    assert said(changes) == {"Resume": FILE_UNCHECKED}


# One field at a time

FOUR = Form((
    Field("name", "Full name", "text", True, "standard"),
    Field("why", "Why Acme?", "long_text", False, "company"),
    Field("sponsor", "Need sponsorship?", "single", True, "company", choices=(Choice("Yes", "1"), Choice("No", "0"))),
    Field("teams", "Which teams?", "multi", False, "company", choices=(Choice("Robotics", "r"), Choice("Search", "s"))),
))  # fmt: skip
FOUR_VALUES = {"name": "Fake Person", "why": "Fake words", "sponsor": ("0",), "teams": ("s",)}
FOUR_DRAWN = {
    "name": box("name", "Full name", required=True),
    "why": box("why", "Why Acme?", "long_text"),
    "sponsor": box("sponsor", "Need sponsorship?", "single", True, choices=offered("Yes", "No")),
    "teams": box("teams", "Which teams?", "multi", choices=offered("Robotics", "Search")),
}


def test_a_form_that_is_the_same_passes_whole():
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(FOUR_DRAWN.values()))
    assert keys(passed) == list(FOUR_DRAWN) and changes == []
    assert compare.pack(passed, {}) == [
        {"key": "name", "kind": "text", "value": "Fake Person"},
        {"key": "why", "kind": "long_text", "value": "Fake words"},
        {"key": "sponsor", "kind": "single", "value": "No"},
        {"key": "teams", "kind": "multi", "value": ["Search"]},
    ]


@pytest.mark.parametrize(
    ("key", "change", "question", "what"),
    [
        ("why", {"label": "Why us?"}, "Why Acme?", WORDS),
        ("why", {"kind": "text"}, "Why Acme?", KIND),
        ("why", {"required": True}, "Why Acme?", REQUIRED),
        ("sponsor", {"choices": offered("Yes", "No", "Maybe")}, "Need sponsorship?", CHOICES),
        ("sponsor", {"choices": offered("Yes")}, "Need sponsorship?", CHOICES),
    ],
)
def test_one_difference_leaves_out_that_field_alone(key, change, question, what):
    boxes = FOUR_DRAWN | {key: FOUR_DRAWN[key] | change}
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    assert keys(passed) == [name for name in FOUR_DRAWN if name != key]
    assert changes == [{"question": question, "what": what}]


def test_a_field_gone_from_the_form_leaves_out_that_field_alone():
    boxes = [item for name, item in FOUR_DRAWN.items() if name != "why"]
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), boxes)
    assert keys(passed) == ["name", "sponsor", "teams"]
    assert changes == [{"question": "Why Acme?", "what": GONE}]


def test_a_field_added_to_the_form_changes_nothing_else():
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), [*FOUR_DRAWN.values(), box("extra", "Anything else?")])
    assert keys(passed) == list(FOUR_DRAWN)
    assert changes == [{"question": "Anything else?", "what": NEW}]


def test_each_difference_of_one_field_is_said():
    boxes = FOUR_DRAWN | {"why": box("why", "Why us?", "text", True)}
    _, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    assert [change["what"] for change in changes] == [WORDS, KIND, REQUIRED]


def test_a_saved_box_that_is_a_file_is_compared_by_key_and_kind_only():
    form = Form((Field("resume", "Resume/CV", "file", True, "standard"),))
    lines = lines_of(form, {"resume": RESUME_PDF})
    assert keys(compared(lines, [box("resume", "Attach", "file")])[0]) == ["resume"]
    passed, changes = compared(lines, [box("resume", "Attach", "text")])
    assert passed == [] and said(changes) == {"Resume/CV": KIND}


def test_a_drawn_box_on_a_saved_hidden_field_is_not_new():
    form = Form((FOUR.fields[0], Field("token", "Token", "hidden", False, "standard")))
    passed, changes = compared(lines_of(form, {"name": "Fake"}), [FOUR_DRAWN["name"], box("token", "Token")])
    assert keys(passed) == ["name"] and changes == []


# Choices


def test_choices_in_another_order_or_capitals_pass():
    boxes = FOUR_DRAWN | {"sponsor": FOUR_DRAWN["sponsor"] | {"choices": offered("NO", "yes")}}
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    assert "sponsor" in keys(passed) and changes == []


def test_the_pack_holds_the_label_as_drawn():
    boxes = FOUR_DRAWN | {
        "sponsor": FOUR_DRAWN["sponsor"] | {"choices": offered("YES", "NO")},
        "teams": FOUR_DRAWN["teams"] | {"choices": offered("robotics", "search")},
    }
    passed, _ = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()))
    values = {item["key"]: item["value"] for item in compare.pack(passed, {})}
    assert (values["sponsor"], values["teams"]) == ("NO", ["search"])


@pytest.mark.parametrize(
    ("text", "as_read"),
    [
        ("Phone", "phone"),
        ("Phone*", "phone"),
        ("Phone *", "phone"),
        ("Resume/CV ✱", "resume/cv"),
        (" Phone*  ", "phone"),
        ("Phone**", "phone"),
        ("Phone* ✱", "phone"),
        ("Years? * *", "years?"),
        ("*Phone", "*phone"),
        ("Phone?", "phone?"),
        ("", ""),
        ("*", ""),
    ],
)
def test_a_question_loses_its_required_marks_at_the_end(text, as_read):
    assert compare._question(text) == as_read


@pytest.mark.parametrize(
    ("saved", "shown"),
    [
        ("Phone", "Phone*"),
        ("Resume/CV", "Resume/CV ✱"),
        ("Phone", "Phone "),
        ("Your phone", "Your\xa0phone"),
        ("Need sponsorship?", "Need sponsorship?*"),
        ("(Optional) Pronouns", "(Optional) Pronouns"),
        ("Years?*", "Years?**"),
        ("Years? *", "Years? * *"),
        ("Phone", "Phone**"),
    ],
)
def test_a_question_s_case_spacing_and_required_mark_don_t_count(saved, shown):
    form = Form((Field("phone", saved, "text", True, "standard"),))
    passed, changes = compared(lines_of(form, {"phone": "Fake"}), [box("phone", shown, required=True)])
    assert keys(passed) == ["phone"] and changes == []


@pytest.mark.parametrize(("saved", "shown"), [("Phone", "Phone?"), ("<1 year", ">1 year"), ("Pay in $", "Pay in €")])
def test_a_question_s_symbols_count(saved, shown):
    form = Form((Field("phone", saved, "text", True, "standard"),))
    passed, changes = compared(lines_of(form, {"phone": "Fake"}), [box("phone", shown, required=True)])
    assert passed == [] and said(changes) == {saved: "the question's words changed"}


def test_choices_are_counted_so_one_gone_is_a_change():
    form = Form((Field("level", "Level", "single", True, "company", choices=(Choice("A", "1"), Choice("a", "2"))),))
    drawn_once = [box("level", "Level", "single", True, choices=offered("A"))]
    passed, changes = compared(lines_of(form, {"level": ("1",)}), drawn_once)
    assert passed == [] and said(changes) == {"Level": CHOICES}


def test_a_choice_that_can_t_be_told_apart_passes_and_stays_out_of_the_pack():
    form = Form((Field("level", "Level", "single", True, "company", choices=(Choice("A", "1"), Choice("a", "2"))),))
    drawn_twice = [box("level", "Level", "single", True, choices=offered("A", "a"))]
    passed, changes = compared(lines_of(form, {"level": ("1",)}), drawn_twice)
    assert keys(passed) == ["level"] and said(changes) == {"Level": compare.ALIKE}
    assert compare.pack(passed, {}) == []


@pytest.mark.parametrize("status", ["decide", "left", "empty"])
def test_alike_choices_are_only_said_for_a_line_that_would_be_filled(status):
    form = Form((Field("level", "Level", "single", True, "company", choices=(Choice("A", "1"), Choice("a", "2"))),))
    line = replace(lines_of(form, {"level": ("1",)})[0], status=status)
    assert compared((line,), [box("level", "Level", "single", True, choices=offered("A", "a"))])[1] == []


def test_no_drawn_choices_is_a_change_on_lever_and_ashby():
    boxes = FOUR_DRAWN | {"sponsor": box("sponsor", "Need sponsorship?", "single", True)}
    for board in (lever.DRAWN, ashby.DRAWN):
        passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()), board)
        assert "sponsor" not in keys(passed) and said(changes) == {"Need sponsorship?": CHOICES}


def test_no_drawn_choices_passes_on_greenhouse_and_says_so():
    boxes = FOUR_DRAWN | {
        "sponsor": box("sponsor", "Need sponsorship?", "single", True),
        "teams": box("teams", "Which teams?", "multi"),
    }
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()), greenhouse.DRAWN)
    assert keys(passed) == list(FOUR_DRAWN)
    assert said(changes) == {"Need sponsorship?": CHOICES_UNCHECKED, "Which teams?": CHOICES_UNCHECKED}
    values = {item["key"]: item["value"] for item in compare.pack(passed, {})}
    assert (values["sponsor"], values["teams"]) == ("No", ["Search"])  # the saved label, as nothing else is drawn


def test_other_drawn_choices_are_still_a_change_on_greenhouse():
    boxes = FOUR_DRAWN | {"sponsor": FOUR_DRAWN["sponsor"] | {"choices": offered("Maybe")}}
    passed, changes = compared(lines_of(FOUR, FOUR_VALUES), list(boxes.values()), greenhouse.DRAWN)
    assert "sponsor" not in keys(passed) and said(changes) == {"Need sponsorship?": CHOICES}


@pytest.mark.parametrize(
    ("drawn_as", "what"),
    [
        (box("race", "Race", "single", choices=offered("Decline")), None),
        (box("race", "Your race", "single", choices=offered("Decline")), WORDS),
        (box("race", "Race", "multi", choices=offered("Decline")), KIND),
    ],
)
def test_a_saved_key_that_may_not_be_drawn_is_compared_when_it_is(drawn_as, what):
    form = Form((Field("race", "Race", "single", False, "eeo", choices=(Choice("Decline", "9"),)),))
    passed, changes = compared(lines_of(form, {"race": ("9",)}), [drawn_as], greenhouse.DRAWN)
    assert keys(passed) == ([] if what else ["race"])
    assert said(changes) == ({"Race": what} if what else {})


def test_a_key_beginning_like_an_undrawn_one_is_said_unchecked():
    form = Form((FOUR.fields[0], Field("demographic_12", "Age", "single", False, "demographic")))
    _, changes = compared(lines_of(form, {"name": "Fake"}), [FOUR_DRAWN["name"]], greenhouse.DRAWN)
    assert said(changes) == {"Age": UNCHECKED}


# The pack


@pytest.mark.parametrize("status", ["suggested", "decide", "left", "empty", "write", "hidden"])
def test_only_lines_to_fill_go_out(status):
    field = FOUR.fields[0]
    passed, changes = compared((match.Line(field, status, "Fake draft"),), [FOUR_DRAWN["name"]])
    assert keys(passed) == ["name"] and changes == []  # it still counts as a match
    assert compare.pack(passed, {}) == []


def test_a_value_in_the_body_never_reaches_the_pack():
    boxes = [FOUR_DRAWN["name"] | {"value": "from the page"}, FOUR_DRAWN["why"] | {"answer": "from the page"}]
    passed, _ = compared(lines_of(FOUR, FOUR_VALUES), boxes)
    assert "from the page" not in json.dumps(compare.pack(passed, {}))


def test_a_pack_never_holds_a_path():
    form = Form((
        Field("resume", "Resume/CV", "file", True, "standard"),
        Field("cover_letter", "Cover Letter", "file", False, "standard"),
    ))  # fmt: skip
    letter = "/fake/folder/letter-v1.pdf"
    lines = lines_of(form, {"resume": RESUME_PDF, "cover_letter": letter})
    passed, _ = compared(lines, [box("resume", "Attach", "file"), box("cover_letter", "Attach", "file")])
    both = DOCUMENTS | {"cover_letter": ("Cover letter v1", Path(letter))}
    found = compare.pack(passed, both)
    assert [item["value"] for item in found] == [{"slot": "resume"}, {"slot": "cover_letter"}]
    assert "/fake" not in json.dumps(found)


def test_a_file_that_is_no_document_stays_out():
    form = Form((Field("resume", "Resume/CV", "file", True, "standard"),))
    passed, _ = compared(lines_of(form, {"resume": "/fake/folder/other.pdf"}), [box("resume", "Attach", "file")])
    assert compare.pack(passed, DOCUMENTS) == []


def test_a_consent_is_a_tick_and_nothing_else_is():
    form = Form((Field("consent[a]", "I agree", "consent", False, "privacy"),))
    drawn_box = [box("consent[a]", "I agree", "consent")]
    passed, _ = compared(lines_of(form, {"consent[a]": match.TICKED}), drawn_box)
    assert compare.pack(passed, {}) == [{"key": "consent[a]", "kind": "consent", "value": "yes"}]
    passed, _ = compared(lines_of(form, {"consent[a]": "no thanks"}), drawn_box)
    assert compare.pack(passed, {}) == []


# A hostile page


@pytest.mark.parametrize(
    "item",
    [
        None, 1, "email", [GOOD_BOX],
        {"label": "Email", "kind": "text", "required": True},
        {"key": "email", "kind": "text", "required": True},
        {"key": "email", "label": "Email", "required": True},
        {"key": "email", "label": "Email", "kind": "text"},
        GOOD_BOX | {"key": ""}, GOOD_BOX | {"key": 1}, GOOD_BOX | {"key": None}, GOOD_BOX | {"key": ["email"]},
        GOOD_BOX | {"key": "x" * 1001},
        GOOD_BOX | {"label": 1}, GOOD_BOX | {"label": None}, GOOD_BOX | {"label": ["Email"]},
        GOOD_BOX | {"kind": "dropdown"}, GOOD_BOX | {"kind": 1}, GOOD_BOX | {"kind": None},
        GOOD_BOX | {"kind": ["text"]}, GOOD_BOX | {"kind": {"a": 1}},
        GOOD_BOX | {"required": 1}, GOOD_BOX | {"required": "true"}, GOOD_BOX | {"required": None},
        GOOD_BOX | {"choices": None}, GOOD_BOX | {"choices": "Yes"}, GOOD_BOX | {"choices": {"label": "Yes"}},
        GOOD_BOX | {"choices": ["Yes"]}, GOOD_BOX | {"choices": [None]}, GOOD_BOX | {"choices": [{"label": "Yes"}]},
        GOOD_BOX | {"choices": [{"value": "1"}]}, GOOD_BOX | {"choices": [{"label": 1, "value": "1"}]},
        GOOD_BOX | {"choices": [{"label": "Yes", "value": 1}]},
        GOOD_BOX | {"accept": None}, GOOD_BOX | {"accept": ".pdf"}, GOOD_BOX | {"accept": [1]},
    ],
)  # fmt: skip
def test_anything_wrong_in_a_drawn_field_refuses_the_whole_list(item):
    with pytest.raises(ValueError):  # noqa: PT011 (the only thing the route needs to know)
        compare.fields_from([GOOD_BOX, item])


def test_every_kind_of_box_is_known():
    kinds = ("text", "long_text", "single", "multi", "file", "location", "consent", "unknown")
    found = compare.fields_from([box(kind, "Label", kind) for kind in kinds])
    assert [field.kind for field in found] == list(kinds)


def test_a_key_of_1000_characters_is_read_and_help_is_not():
    (found,) = compare.fields_from([GOOD_BOX | {"key": "x" * 1000, "help": 1}])
    assert found.key == "x" * 1000 and found.help == ""


def test_choices_and_file_types_may_be_left_out_or_given():
    (found,) = compare.fields_from([GOOD_BOX | {"choices": offered("Yes"), "accept": [".pdf"]}])
    assert found.choices == (Choice("Yes", "site-0"),) and found.accept == (".pdf",)


def test_hidden_boxes_are_dropped_and_a_repeated_key_keeps_the_first():
    found = compare.fields_from([GOOD_BOX, GOOD_BOX | {"label": "Second"}, box("token", "Token", "hidden")])
    assert [(field.key, field.label) for field in found] == [("email", "Email")]


def test_a_long_label_is_cut_in_what_is_said():
    _, changes = compared(lines_of(Form(()), {}), [box("extra", "x" * 2000)])
    assert changes == [{"question": "x" * 1000, "what": NEW}]


def test_invisible_characters_are_taken_out_of_a_drawn_label():
    (found,) = compare.fields_from([box("extra", "Fake\x00 \x07label\n")])
    assert found.label == "Fake label"


@pytest.mark.parametrize(("extra", "more"), [(100, None), (101, 1), (150, 50)])
def test_at_most_100_changes_and_then_how_many_more(extra, more):
    _, changes = compared(lines_of(Form(()), {}), [box(f"key_{number}", f"Label {number}") for number in range(extra)])
    assert len(changes) == (100 if more is None else 101)
    assert changes[99] == {"question": "Label 99", "what": NEW}
    if more:
        assert changes[100] == {"question": f"{more:,} more", "what": "not shown here"}
