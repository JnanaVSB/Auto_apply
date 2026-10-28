"""The meaning model's reading in parts, with a tiny real tokenizer and a stand-in model (no downloaded files)."""

import numpy as np
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from app.scoring import meaning

VOCAB = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "robot": 4, "tax": 5}


class FakeModel:
    """Each part's summary is (number of "robot", number of "tax") among its word-pieces; records every batch."""

    def __init__(self) -> None:
        self.batches: list[np.ndarray] = []

    def run(self, outputs, feeds):
        ids = feeds["input_ids"]
        self.batches.append(ids)
        assert (feeds["attention_mask"] == (ids != 0)).all()
        hidden = np.zeros((*ids.shape, 2), np.float32)
        hidden[:, 0] = np.stack([(ids == 4).sum(1), (ids == 5).sum(1)], axis=1) + 1e-6
        return [hidden]


def model():
    tokenizer = Tokenizer(WordLevel(VOCAB, unk_token="[UNK]"))  # noqa: S106 (a word-piece, not a password)
    tokenizer.pre_tokenizer = Whitespace()
    fake = FakeModel()
    return meaning.Meaning(tokenizer, fake), fake


def test_long_text_is_read_in_parts_covering_all_of_it(monkeypatch):
    monkeypatch.setattr(meaning, "WINDOW", 3)
    reader, fake = model()
    reader.fingerprints(["robot robot robot tax tax"])
    parts = [list(row[row != 0]) for batch in fake.batches for row in batch]
    assert sorted(parts) == [[2, 4, 4, 4, 3], [2, 5, 5, 3]]  # every word-piece, each part with start and end marks


def test_a_fingerprint_is_the_average_of_its_parts_with_length_one(monkeypatch):
    monkeypatch.setattr(meaning, "WINDOW", 3)
    reader, _ = model()
    robots, taxes, half = reader.fingerprints(["robot robot", "tax", "robot robot robot tax tax tax"])
    assert np.allclose(robots, [1, 0], atol=1e-5) and np.allclose(taxes, [0, 1], atol=1e-5)
    assert np.allclose(half, [2**-0.5, 2**-0.5], atol=1e-5)  # one robot part and one tax part, equally


def test_empty_text_still_gets_a_fingerprint():
    reader, _ = model()
    assert reader.fingerprints([""]).shape == (1, 2)


def test_parts_are_read_in_groups(monkeypatch):
    monkeypatch.setattr(meaning, "WINDOW", 1)
    monkeypatch.setattr(meaning, "BATCH", 2)
    reader, fake = model()
    reader.fingerprints(["robot tax robot", "tax"])
    assert [len(batch) for batch in fake.batches] == [2, 2]
