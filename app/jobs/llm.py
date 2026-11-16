from typing import Any, Literal

import httpx
from pydantic import Field

from app.jobs.handlers import Payload
from app.jobs.registry import JobContext, PermanentJobError, register

ModelId = Literal["qwen2.5-0.5b-instruct"]
MODEL_ID: ModelId = "qwen2.5-0.5b-instruct"
DEADLINE_SECONDS = 300.0
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ModelServerError(RuntimeError):
    pass


class LlmPayload(Payload):
    model: ModelId = MODEL_ID
    prompt: str = Field(min_length=1, max_length=2_000)
    max_tokens: int = Field(default=64, ge=1, le=256)


@register(
    "llm_generate",
    "Asks the local language model to answer a prompt.",
    LlmPayload,
    deadline_seconds=DEADLINE_SECONDS,
)
def llm_generate(p: LlmPayload, ctx: JobContext) -> dict[str, Any]:
    s = ctx.config
    body = {
        "model": p.model,
        "messages": [{"role": "user", "content": p.prompt}],
        "max_tokens": p.max_tokens,
        "temperature": 0,
        "stream": False,
    }
    timeout = httpx.Timeout(s.llm_read_timeout_seconds, connect=s.llm_connect_timeout_seconds)
    try:
        response = httpx.post(
            s.llm_server_url.rstrip("/") + "/v1/chat/completions",
            json=body,
            timeout=timeout,
            headers={"Connection": "close"},
        )
    except httpx.TimeoutException:
        raise ModelServerError("the model server did not answer in time") from None
    except httpx.TransportError as exc:
        raise ModelServerError(f"cannot reach the model server ({type(exc).__name__})") from None
    if response.status_code in RETRYABLE_STATUS:
        raise ModelServerError(f"the model server answered {response.status_code}")
    if response.status_code >= 400:
        raise PermanentJobError(f"the model server refused the request ({response.status_code})")
    try:
        data = response.json()
        text = str(data["choices"][0]["message"]["content"])
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelServerError("the model server sent an answer that could not be read") from None
    limit = s.llm_max_text_chars
    return {
        "model": p.model,
        "text": text[:limit],
        "truncated": len(text) > limit,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
