"""The price list shipped with the app, and the cost of one request."""

import pytest

from app.config import AIRole
from app.llm import Usage, prices

LOCAL = AIRole(model="gemma4:26b-a4b-it-q8_0")
OPUS = AIRole(service="anthropic", model="claude-opus-5-5", key_name="anthropic")
HAIKU = AIRole(service="anthropic", model="claude-haiku-5-5", key_name="anthropic")
SOL = AIRole(service="openai-compatible", model="gpt-6.1-sol", address="https://api.openai.com/v1", key_name="openai")
OWN = AIRole(service="openai-compatible", model="my-model", address="https://models.example.org/v1")


def test_the_list_is_dated_and_names_its_models():
    assert prices.checked() == "2026-10-30"
    assert prices.models("anthropic") == {
        "claude-opus-5-5": ("Claude Opus 5.5", (4, 0.2, 20)), "claude-sonnet-5-5": ("Claude Sonnet 5.5", (2, 0.1, 10)),
        "claude-haiku-5-5": ("Claude Haiku 5.5", (0.1, 0.01, 0.5)),
    }  # fmt: skip
    assert prices.models("openai") == {
        "gpt-6.1-sol": ("GPT-6.1 Sol", (2, 0.1, 10)), "gpt-6.1-terra": ("GPT-6.1 Terra", (1, 0.05, 5))
    }  # fmt: skip


@pytest.mark.parametrize(
    ("role", "prompt", "half", "price"),
    [
        (LOCAL, 50_000, False, (0, 0, 0)),  # free
        (OPUS, 10_000, False, (4, 0.2, 20)),
        (OPUS, 10_000, True, (2, 0.1, 10)),
        (HAIKU, 100_000, False, (0.1, 0.01, 0.5)),
        (HAIKU, 100_001, False, (0.5, 0.05, 2.5)),  # over 100,000 tokens: the higher price
        (HAIKU, 100_001, True, (0.25, 0.025, 1.25)),
        (SOL, 272_000, False, (2, 0.1, 10)),
        (SOL, 272_001, False, (4, 0.2, 15)),
        (OWN.model_copy(update={"price_input": 0.3, "price_output": 0.9}), 10, False, (0.3, 0.3, 0.9)),
    ],
)
def test_each_model_has_its_price(role, prompt, half, price):
    assert prices.price(role, prompt, half) == prices.Price(*price)


@pytest.mark.parametrize("role", [OWN, OPUS.model_copy(update={"model": "claude-unknown-9"})])
def test_an_unknown_price_is_none_never_zero(role):
    assert prices.price(role) is None


def test_a_request_costs_its_tokens_at_their_prices():
    usage = Usage(input=1_000_000, cached=500_000, output=250_000)
    assert prices.cost(prices.Price(4, 0.2, 20), usage) == pytest.approx(4 + 0.1 + 5)
    assert prices.cost(prices.Price(0, 0, 0), usage) == 0
