"""The meaning match: the small bge-small model turns a text into a meaning fingerprint (384 numbers), on the
processor, and two fingerprints are compared (0 to 1). Long texts are read in parts covering the whole text."""

from pathlib import Path

import numpy as np
import onnxruntime
from tokenizers import Tokenizer

from app.scoring.files import MODEL, TOKENIZER

WINDOW = 254  # word-pieces per part; with the model's start and end markers 256, which it reads fastest
BATCH = 16  # parts read together


class Meaning:
    def __init__(self, tokenizer: Tokenizer, session: onnxruntime.InferenceSession) -> None:
        self._tokenizer, self._session = tokenizer, session
        self._start, self._end = tokenizer.token_to_id("[CLS]"), tokenizer.token_to_id("[SEP]")

    @classmethod
    def load(cls, folder: Path) -> "Meaning":
        """The model and its word-piece list from the cache folder (checked by files.ensure_files)."""
        tokenizer = Tokenizer.from_file(str(folder / TOKENIZER.name))
        tokenizer.no_truncation()  # the parts are cut here, so the whole text is read
        tokenizer.no_padding()
        session = onnxruntime.InferenceSession(str(folder / MODEL.name), providers=["CPUExecutionProvider"])
        return cls(tokenizer, session)

    def fingerprints(self, texts: list[str]) -> np.ndarray:
        """One fingerprint per text, of length 1: the average of its parts, so a closeness is a dot product."""
        parts, owners = [], []
        for owner, text in enumerate(texts):
            ids = self._tokenizer.encode(text, add_special_tokens=False).ids
            for start in range(0, max(len(ids), 1), WINDOW):  # an empty text is one empty part
                parts.append([self._start, *ids[start : start + WINDOW], self._end])
                owners.append(owner)
        order = sorted(range(len(parts)), key=lambda part: len(parts[part]))  # similar lengths together: less padding
        vectors = [np.empty(0)] * len(parts)
        for start in range(0, len(order), BATCH):
            chosen = order[start : start + BATCH]
            for part, vector in zip(chosen, self._read([parts[part] for part in chosen]), strict=True):
                vectors[part] = vector
        sums = np.zeros((len(texts), len(vectors[0])), np.float32)
        np.add.at(sums, owners, np.array(vectors))
        return sums / np.linalg.norm(sums, axis=1, keepdims=True)

    def _read(self, parts: list[list[int]]) -> np.ndarray:
        ids = np.zeros((len(parts), max(map(len, parts))), np.int64)  # 0 is the padding word-piece
        for row, part in enumerate(parts):
            ids[row, : len(part)] = part
        feeds = {"input_ids": ids, "attention_mask": (ids != 0).astype(np.int64), "token_type_ids": np.zeros_like(ids)}
        first = self._session.run(None, feeds)[0][:, 0]  # the model's summary of each part sits at its first position
        return first / np.linalg.norm(first, axis=1, keepdims=True)
