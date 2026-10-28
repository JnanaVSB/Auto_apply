"""The one AI connector: ask a model for a JSON answer of a given shape, through Ollama on this computer, an
OpenAI-compatible service or the Anthropic API, as one role's settings say. Keys come only from the keychain.

Errors name the cause and the fix; they never include a key, the texts sent or the answer.
"""

from urllib.parse import urlsplit

import httpx

from app.config import AIRole
from app.keychain import get_secret

TIMEOUT_SECONDS = 120  # per answer; the first answer of the day also loads the model (about 20 s)


class LLMError(Exception):
    """One request failed; the next one may work."""


class StopScoring(LLMError):  # noqa: N818 (an instruction, not just an error)
    """Every request will fail until this is fixed: Ollama not running, model missing, out of memory, key refused."""


class TooLong(LLMError):  # noqa: N818
    """The texts are longer than the model can read at once."""


class TimedOut(LLMError):  # noqa: N818
    """No answer in time; one failed try for this request only."""


def ask(
    role: AIRole,
    system: str,
    user: str,
    schema: dict,
    *,
    retry: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """One request; returns the model's answer, meant to be JSON of the requested shape.

    `retry` asks a little less strictly: the first try is fully predictable, so asking the same way again would give
    the same answer."""
    from app.llm import anthropic, ollama, openai_compatible  # the services import the errors above

    service = {"ollama": ollama, "openai-compatible": openai_compatible, "anthropic": anthropic}[role.service]
    key = None if role.service == "ollama" else _key(role)
    with httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        try:  # trust_env=False: no proxy settings or other surprises from the environment
            return service.answer(client, role, key, system, user, schema, retry)
        except httpx.TimeoutException:
            raise TimedOut(f"no answer within {TIMEOUT_SECONDS} seconds") from None
        except httpx.TransportError:
            if role.service == "ollama":
                raise StopScoring("Ollama isn't running: start it with `ollama serve`.") from None
            raise StopScoring(
                f"Could not reach {urlsplit(role.address).hostname}: check your internet connection."
            ) from None


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
        return LLMError(f"{service} is busy or failing (status {status}); try again later.")
    return LLMError(f"{service} refused the request (status {status}).")
