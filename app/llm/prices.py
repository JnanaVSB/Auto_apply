"""The price list shipped with the app (prices.json, never fetched online), and what one request cost.

Ollama on this computer is free; your own address costs what you typed in Settings, else it is not counted (None).
Half price is Phase 4c's Batch and Flex sending; Phase 4b sends at the normal price."""

import json
from functools import cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from app.config import OPENAI_HOST, AIRole
from app.llm import Usage


class Price(NamedTuple):  # dollars per 1M tokens
    input: float
    cached: float
    output: float


@cache
def _list() -> dict:
    return json.loads((Path(__file__).parent / "prices.json").read_text(encoding="utf-8"))


def checked() -> str:
    """When the prices were last checked against the services' own price pages."""
    return _list()["checked"]


def models(service: str) -> dict[str, str]:
    """The ready-made models of "anthropic" or "openai": their names as the app sends them, and as people say them."""
    return {model: found["name"] for model, found in _list()["models"].items() if found["service"] == service}


def price(role: AIRole, prompt_tokens: int = 0, half: bool = False) -> Price | None:
    """The role's price for a request reading `prompt_tokens` (cached ones too); None when it is not known (your own
    address without prices, or a model missing from the list)."""
    if role.service == "ollama":
        return Price(0, 0, 0)
    if role.service == "openai-compatible" and urlsplit(role.address).hostname != OPENAI_HOST:  # your own address
        if role.price_input is None or role.price_output is None:
            return None
        return Price(role.price_input, role.price_input, role.price_output)  # no cache price known: the full one
    found = _list()["models"].get(role.model)
    if found is None:
        return None
    long = prompt_tokens > found.get("long_over", float("inf"))
    return Price(*found[("long_" if long else "") + ("half" if half else "normal")])


def cost(price: Price, usage: Usage) -> float:
    """Dollars for one request's tokens."""
    return (usage.input * price.input + usage.cached * price.cached + usage.output * price.output) / 1_000_000
