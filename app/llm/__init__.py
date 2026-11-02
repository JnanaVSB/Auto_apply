"""The one AI connector: ask a model for a JSON answer of a given shape, through Ollama on this computer, an
OpenAI-compatible service or the Anthropic API, as one role's settings say. Keys come only from the keychain.

Errors name the cause and the fix; they never include a key, the texts sent or the answer (a failure keeps what came
back apart, in `reply`, for the Failed tries page). Every answer comes with
the tokens the service counted, so its cost can be worked out (app/llm/prices.py), even for a failed answer that was
charged.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple
from urllib.parse import urlsplit

import httpx

from app.config import AIRole
from app.keychain import get_secret

TIMEOUT_SECONDS = 120  # per answer; the first answer of the day also loads the model (about 20 s)
CHECK_SECONDS = 10


class Usage(NamedTuple):
    input: int = 0  # tokens read at the normal price
    cached: int = 0  # tokens read from the service's cache, at its lower price
    output: int = 0  # tokens written, thinking included


class Reply(NamedTuple):
    text: str  # meant to be JSON of the requested shape
    usage: Usage


NOTHING = Usage()  # a request the service refused, or never answered


class LLMError(Exception):
    """One request failed; the next one may work. `usage` is what the service still charged for it."""

    def __init__(self, message: str, usage: Usage = NOTHING) -> None:
        super().__init__(message)
        self.usage = usage
        self.reply: str | None = None  # what the service sent back, if anything (any key in it shown as [KEY])


class Busy(LLMError):  # noqa: N818
    """The service is busy or failing for now (OpenAI's half-price "resource unavailable" too): not charged; the
    council waits and sends it again, never at full price."""


class Expired(LLMError):  # noqa: N818
    """Anthropic didn't answer a batch request in time (24 hours), or no longer has its batch: send it again."""


class StopScoring(LLMError):  # noqa: N818 (an instruction, not just an error)
    """Every request will fail until this is fixed: Ollama not running, model missing, out of memory, key refused."""


class TooLong(LLMError):  # noqa: N818
    """The texts are longer than the model can read at once."""


class TimedOut(LLMError):  # noqa: N818
    """The request was sent, but no whole answer came (none in time, or the connection broke): one failed try for this
    request only, which a paid service may still have charged."""


def ask(
    role: AIRole,
    system: str,
    user: str,
    schema: dict,
    *,
    retry: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """One request with the usual limits; returns only the model's answer (see call)."""
    return call(role, system, user, schema, retry=retry, transport=transport).text


def call(
    role: AIRole,
    system: str,
    user: str,
    schema: dict,
    *,
    retry: bool = False,
    answer_limit: int | None = None,
    timeout: int = TIMEOUT_SECONDS,
    half: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> Reply:
    """One request; returns the model's answer and the tokens counted for it.

    `retry` asks a little less strictly: the first try is fully predictable, so asking the same way again would give
    the same answer. `answer_limit` is the answer's room in tokens (thinking gets its own room on top), else the
    service's usual one; `timeout` is in seconds. `half`: OpenAI's half price (Flex); Anthropic's is its batches."""
    with connected(role, timeout, transport) as (client, key):
        return _service(role).answer(client, role, key, system, user, schema, retry, answer_limit, half)


@contextmanager
def connected(
    role: AIRole, timeout: int = TIMEOUT_SECONDS, transport: httpx.BaseTransport | None = None
) -> Iterator[tuple[httpx.Client, str | None]]:
    """A connection to the role's service and its key (None: Ollama, or your own address without one). Whatever
    fails inside comes out as one of the errors above, naming the cause and the fix, never the key."""
    key = None if role.service == "ollama" or role.key_name is None else _key(role)
    came: list[httpx.Response] = []  # what the service sent back, read once the answer fails
    with httpx.Client(
        transport=transport, timeout=timeout, follow_redirects=False, trust_env=False,
        event_hooks={"response": [came.append]},
    ) as client:  # fmt: skip
        try:  # trust_env=False: no proxy settings or other surprises from the environment
            yield client, key
        except LLMError as error:
            if came:
                error.reply = came[-1].text.replace(key, "[KEY]") if key else came[-1].text
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):  # never sent, so never charged
            if role.service == "ollama":
                raise StopScoring("Ollama isn't running: start it with `ollama serve`.") from None
            raise StopScoring(
                f"Could not reach {urlsplit(role.address).hostname}: check your internet connection."
            ) from None
        except (httpx.InvalidURL, httpx.UnsupportedProtocol):  # never sent either
            raise StopScoring("The model's address in the settings can't be used: check it there.") from None
        except httpx.TimeoutException:
            raise TimedOut(f"no answer within {timeout} seconds") from None
        except httpx.HTTPError:  # sent, then the connection broke or the answer came garbled
            raise TimedOut("the connection broke before the whole answer came") from None


def check(role: AIRole, transport: httpx.BaseTransport | None = None) -> tuple[bool, str]:
    """Whether the role's model can be used, and one line saying so or giving the fix. Free: it only asks whether
    the model exists for your key, and sends nothing of yours."""
    if role.service == "ollama":
        from app.llm import ollama

        return ollama.status(role, transport)
    try:
        key = None if role.key_name is None else _key(role)
    except StopScoring as error:
        return False, str(error)
    with httpx.Client(transport=transport, timeout=CHECK_SECONDS, follow_redirects=False, trust_env=False) as client:
        try:
            return _service(role).check(client, role, key)
        except httpx.TransportError:  # time-outs too
            return False, f"Could not reach {urlsplit(role.address).hostname}: check the address and your connection."


def most_output(role: AIRole, answer_limit: int | None = None) -> int:
    """The most tokens a request can write (its answer and thinking), for reserving its largest possible cost."""
    return _service(role).most_output(role, answer_limit)


def _service(role: AIRole):
    from app.llm import anthropic, ollama, openai_compatible  # the services import the errors above

    return {"ollama": ollama, "openai-compatible": openai_compatible, "anthropic": anthropic}[role.service]


def tokens(data: object, *names: str) -> int:
    """A token count in a service's answer, e.g. tokens(data, "usage", "output_tokens"); 0 when it isn't there."""
    for name in names:
        data = data.get(name) if isinstance(data, dict) else None
    return data if type(data) is int and data > 0 else 0


def answer_data(reply: httpx.Response) -> dict:
    """A cloud service's answer as JSON; anything else is one failed try."""
    try:
        data = reply.json()
    except (ValueError, RecursionError):  # RecursionError: nested too deep to read
        data = None
    if not isinstance(data, dict):
        raise LLMError("the service did not answer as expected")
    return data


def _key(role: AIRole) -> str:
    key = get_secret(role.key_name)
    if not key:
        raise StopScoring(f"No key is saved in the keychain under the name '{role.key_name}'.")
    return key


def refused(service: str, status: int, model: str) -> LLMError:
    """The error for a cloud service's refusal, by its status code."""
    if status in (401, 403):
        return StopScoring(f"{service} refused the key (status {status}): check the key saved in the keychain.")
    if status == 404:
        return StopScoring(f"{service} does not know the model '{model}': check the model name in the settings.")
    if status in (429, 529) or status >= 500:
        return Busy(f"{service} is busy or failing (status {status}); try again later.")
    return LLMError(f"{service} refused the request (status {status}).")
