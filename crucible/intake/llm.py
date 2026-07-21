"""LLM clients for intake extraction (design §6.1, §15).

One small interface — `complete_json(prompt, images)` — with three backends:
Anthropic (Claude), OpenAI (GPT), and a FakeClient for tests. Both real clients
support vision (figure images passed as base64) and request structured JSON.
Keys come from the environment; SDKs are imported lazily so the package installs
without them.

All calls are meant to be logged to the trace (planner/intake are part of the
experiment record, design §6.5) — the caller wires that.
"""

from __future__ import annotations

import json
import os
from typing import Protocol


class LLMClient(Protocol):
    def complete_json(
        self, prompt: str, images: list[tuple[str, str]] | None = None
    ) -> dict:
        """Return parsed JSON. `images` is a list of (media_type, base64) pairs."""
        ...


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    return t.strip()


class AnthropicClient:
    def __init__(self, model: str = "claude-opus-4-8", max_tokens: int = 4096) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def complete_json(self, prompt: str, images: list[tuple[str, str]] | None = None) -> dict:
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        content: list[dict] = [{"type": "text", "text": prompt}]
        for media_type, b64 in images or []:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                }
            )
        msg = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system="You extract structured data. Respond with a single JSON object, no prose.",
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in msg.content if block.type == "text")
        return json.loads(_strip_code_fence(text))


class OpenAIClient:
    def __init__(self, model: str = "gpt-4o", max_tokens: int = 4096) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def complete_json(self, prompt: str, images: list[tuple[str, str]] | None = None) -> dict:
        from openai import OpenAI

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        content: list[dict] = [{"type": "text", "text": prompt}]
        for media_type, b64 in images or []:
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}}
            )
        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You extract structured data. Respond with JSON only."},
                {"role": "user", "content": content},
            ],
        )
        return json.loads(resp.choices[0].message.content or "{}")


class LoggingLLMClient:
    """Wraps any client and records each call to the trace (design §6.5: the
    planner/extractor are part of the experiment record). Transparent — it
    implements the same LLMClient interface."""

    def __init__(self, inner: LLMClient, recorder, trace_id: str, role: str) -> None:
        self.inner = inner
        self.recorder = recorder
        self.trace_id = trace_id
        self.role = role

    def complete_json(self, prompt: str, images: list[tuple[str, str]] | None = None) -> dict:
        output = self.inner.complete_json(prompt, images)
        try:
            self.recorder.record_llm_call(
                self.trace_id, self.role, {"prompt": prompt, "n_images": len(images or [])}, output
            )
        except Exception:
            pass
        return output


class FakeClient:
    """Returns queued responses. For tests and offline development."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, int]] = []

    def complete_json(self, prompt: str, images: list[tuple[str, str]] | None = None) -> dict:
        self.calls.append((prompt, len(images or [])))
        return self._responses.pop(0)


def default_client() -> LLMClient:
    """Pick a client from whatever API key is present in the environment."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIClient()
    raise RuntimeError(
        "No LLM API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
        "or pass an explicit client to Intake(...)."
    )
