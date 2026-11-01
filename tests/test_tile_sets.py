"""Confirmed tiles: the form's checks, saving a new set, "nothing changed", what changed, and the contact check."""

import pytest
from test_tiles import RESUME_CLS

from app.documents import tile_sets
from app.documents.tile_sets import TilesError
from app.documents.tiles import header_text, read_tiles

TILES = read_tiles(RESUME_CLS).tiles


def form_of(tiles, **changes):
    """The Resume Profile page's form for these tiles, with some boxes changed (e.g. tile_2_name="...")."""
    form = {
        f"tile.{number}.{name}": value
        for number, tile in enumerate(tiles)
        for name, value in tile_sets.as_row(tile, number + 1).items()
    }
    return form | {name.replace("_", ".", 2): value for name, value in changes.items()}


def confirmed(**changes):
    return tile_sets.check(tile_sets.typed(form_of(TILES, **changes)))


def test_unchanged_boxes_give_back_the_same_tiles():
    assert confirmed() == TILES


def test_edits_reorder_leave_out_and_add_a_tile():
    tiles = confirmed(
        tile_0_drop="1",  # the summary is left out
        tile_7_position="1", tile_7_name="Toy Driver, renamed",  # the last project moves first, keeping its id
        tile_8_kind="award", tile_8_name="Made-up Award", tile_8_bullets="Won it.\n\n",  # the new-tile box
    )  # fmt: skip
    assert [tile.facts["name"] for tile in tiles[:2]] == ["Toy Driver, renamed", "Example State University"]
    assert tiles[0].tile_id == TILES[7].tile_id and all(tile.kind != "summary" for tile in tiles)
    assert tiles[-1].kind == "award" and tiles[-1].tile_id.startswith("award-") and tiles[-1].bullets == ("Won it.",)


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"tile_1_name": ""}, "Entry 2, name: is empty"),
        ({"tile_1_kind": "hobby"}, "Entry 2, kind: choose one from the list"),
        ({"tile_1_right": "May\x002027"}, "Entry 2, right: contains an invisible (control) character"),
        ({"tile_1_rest": "x" * 301}, "Entry 2, rest: is longer than 300 characters"),
        ({"tile_7_bullets": "\n".join(["Bullet."] * 41)}, "Entry 8, bullets: at most 40 lines"),
        ({"tile_7_link": "javascript:alert(1)"}, "Entry 8, link: must start with http:// or https://"),
        ({"tile_3_skills": ""}, "Entry 4, skills: a skills entry needs its skills"),
        ({"tile_7_skills": "Python"}, "Entry 8, skills: only a skills entry has a skills list"),
        ({"tile_7_id": "../../etc"}, "Entry 8, its id is damaged"),
        ({"tile_7_id": TILES[6].tile_id}, "two entries have the same id"),
        ({"tile_7_position": "first"}, "Entry 8, position: must be a whole number"),
        ({f"tile_{n}_drop": "1" for n in range(8)}, "there are no entries to confirm"),
    ],
)
def test_broken_boxes_name_the_tile_and_box_never_the_text(changes, problem):
    with pytest.raises(TilesError) as error:
        confirmed(**changes)
    assert problem in str(error.value)
    assert "x" * 50 not in str(error.value) and "alert" not in str(error.value)


def test_more_than_35_tiles_are_refused():
    many = form_of(TILES) | {
        f"tile.{n}.{box}": value for n in range(8, 40) for box, value in (("name", f"Extra {n}"), ("kind", "other"))
    }
    with pytest.raises(TilesError, match="at most 35 entries can be confirmed"):
        tile_sets.check(tile_sets.typed(many))


def test_confirming_saves_a_new_set_and_keeps_the_older_ones(conn):
    first = tile_sets.confirm(conn, TILES, "fp1")
    assert tile_sets.confirm(conn, TILES, "fp1") is None  # the same tiles again: nothing saved
    renamed = confirmed(tile_7_name="Toy Pilot")
    second = tile_sets.confirm(conn, renamed, "fp1")
    assert second == first + 1 and tile_sets.latest(conn).tiles == renamed
    assert tile_sets.confirm(conn, renamed, "fp2") == second + 1  # the same tiles from a changed resume: a new set
    assert tile_sets.latest_id(conn) == second + 1 and tile_sets.latest_id(conn, 12345) is None  # the number only
    assert conn.execute("SELECT count(*) FROM tiles WHERE set_id = ?", (first,)).fetchone()[0] == len(TILES)


def test_what_changed_lists_new_changed_and_gone_tiles():
    newer = read_tiles(RESUME_CLS.replace("Drove a toy car.", "Drove a toy car fast.").replace("Toy Driver", "Kite"))
    kinds = [(change, (old or new).facts["name"]) for change, old, new in tile_sets.changes(TILES, newer.tiles)]
    assert kinds == [("new", "Kite"), ("gone", "Toy Driver")]
    reworded = read_tiles(RESUME_CLS.replace("Trained fake models.", "Trained fake models well."))
    assert [change for change, *_ in tile_sets.changes(TILES, reworded.tiles)] == ["changed"]


def test_the_contact_check_names_what_the_header_does_not_show():
    header = header_text(RESUME_CLS)
    assert "Alex Example" in header and "Summary" not in header
    same = {"full_name": "alex  EXAMPLE", "email": "alex@example.com", "phone": "+1 (555) 010-0199"}
    assert tile_sets.contact_check(same, header) == []
    other = {"full_name": "Sam Sample", "email": "sam@example.com", "phone": "555 999 0000"}
    assert tile_sets.contact_check(other, header) == ["name", "email", "phone"]
    assert tile_sets.contact_check({}, header) == []
