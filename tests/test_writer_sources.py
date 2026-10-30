"""What the Writer uses: the default, a job's own choice, the confirmed check, and Remove cleaning the choices."""

import pytest
from helpers import add_job
from test_tiles import RESUME_CLS
from test_uploads import OTHER, form

from app.documents import tile_sets, uploads, writer_sources
from app.documents.tiles import read_tiles
from app.documents.writer_sources import SourcesError


@pytest.fixture
def file(conn):
    uploads.upload(conn, form(("robotics-cv.tex", OTHER)))
    return uploads.offered(conn)[0]


def test_the_default_is_the_profile_until_you_choose(conn, file):
    assert writer_sources.default(conn) == ["profile"]
    writer_sources.save_default(conn, [file.id])
    writer_sources.save_default(conn, ["profile", file.id])
    assert writer_sources.default(conn) == ["profile", file.id]
    offered = writer_sources.offered(conn, None)
    assert writer_sources.names(offered, [*writer_sources.default(conn), 999]) == [
        "Profile",
        "robotics-cv",
        "a removed file",
    ]


def test_ticked_boxes_offered_files_only_and_at_least_one(conn, file):
    assert writer_sources.ticked(conn, {f"source.{file.id}": "1", "source.999": "1"}) == [file.id]
    with pytest.raises(SourcesError, match="Tick at least one"):
        writer_sources.ticked(conn, {"source.999": "1"})


def test_a_job_uses_its_own_choice_or_the_default(conn, file):
    with conn:
        job_id = add_job(conn, "Fake Job")
    assert writer_sources.of_job(conn, job_id) == (["profile"], False)
    assert writer_sources.save_job(conn, job_id, [file.id]) and writer_sources.of_job(conn, job_id) == ([file.id], True)
    writer_sources.save_job(conn, job_id, None)
    assert writer_sources.of_job(conn, job_id) == (["profile"], False) and not writer_sources.save_job(conn, 999, None)


def test_every_chosen_source_must_be_confirmed_for_its_current_text(conn, file):
    def unconfirmed(sources, resume):
        return writer_sources.unconfirmed(writer_sources.offered(conn, resume), sources)

    assert unconfirmed(["profile", file.id], "fp-now") == ["Profile", "robotics-cv"]
    tile_sets.confirm(conn, read_tiles(RESUME_CLS).tiles, "fp-before")  # the Profile, from an older resume
    tile_sets.confirm(conn, read_tiles(OTHER).tiles, "fp-older", file.id)
    tile_sets.confirm(conn, read_tiles(OTHER).tiles, file.fingerprint, file.id)  # the latest set counts
    assert unconfirmed(["profile", file.id], "fp-now") == ["Profile"]
    assert unconfirmed(["profile", file.id], "fp-before") == []
    assert unconfirmed(["profile"], None) == ["Profile"] and unconfirmed([999], "fp-before") == ["a removed file"]
    tile_sets.confirm(conn, read_tiles(RESUME_CLS).tiles, "fp-now")  # a newer Profile set replaces the older one
    assert unconfirmed(["profile"], "fp-before") == ["Profile"] and unconfirmed(["profile"], "fp-now") == []


def test_removing_a_file_cleans_every_choice_that_used_it(conn, file):
    with conn:
        alone, both = add_job(conn, "Alone"), add_job(conn, "Both")
    writer_sources.save_job(conn, alone, [file.id])
    writer_sources.save_job(conn, both, ["profile", file.id])
    writer_sources.save_default(conn, [file.id])
    writer_sources.remove(conn, file.id)
    assert writer_sources.of_job(conn, alone) == (["profile"], False)  # nothing left: your default
    assert writer_sources.of_job(conn, both) == (["profile"], True)
    assert writer_sources.default(conn) == ["profile"]
