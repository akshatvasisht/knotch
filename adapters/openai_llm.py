"""Real coordinator brain: any OpenAI-compatible vLLM endpoint.

Implements the same `CoordinatorLLM.decide(request) -> dict` interface as the fake,
so the engine never changes — flip KNOTCH_LLM=nemotron. The
endpoint + model come from env (KNOTCH_LLM_URL / KNOTCH_LLM_MODEL).

One plain async OpenAI client call per routable turn. Thinking is disabled for
low latency. Responses are parsed defensively; any error propagates so the
coordinator fail-safes to no-route.
"""
from __future__ import annotations

import json
import os
import re

from engine.interfaces import CoordinatorLLM, RoleSpec, RoutingRequest, Utterance

# Endpoints come from the environment (.env), never hardcoded.
DEFAULT_LLM_MODEL = ""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class OpenAICoordinatorLLM(CoordinatorLLM):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str = "EMPTY",
        timeout: float = 30.0,
        enable_thinking: bool = False,
    ) -> None:
        # Lazy import so the engine/tests don't require `openai` unless this
        # adapter is actually selected.
        from openai import AsyncOpenAI

        self.base_url = base_url or os.getenv("KNOTCH_LLM_URL")
        if not self.base_url:
            raise RuntimeError(
                "KNOTCH_LLM_URL is not set. Provide it via .env — "
                "endpoints are never hardcoded."
            )
        self.model = model or os.getenv("KNOTCH_LLM_MODEL") or DEFAULT_LLM_MODEL
        if not self.model:
            raise RuntimeError(
                "KNOTCH_LLM_MODEL is not set. Provide it via .env."
            )
        self.enable_thinking = (
            os.getenv("KNOTCH_LLM_THINKING", str(enable_thinking)).lower() == "true"
        )
        self._client = AsyncOpenAI(
            base_url=self.base_url, api_key=api_key, timeout=timeout
        )

    async def decide(self, request: RoutingRequest) -> dict:
        user_msg = self._format_turn(request)
        kwargs: dict = dict(
            model=self.model,
            temperature=0,
            messages=[
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": user_msg},
            ],
        )
        if self.enable_thinking:
            # NIM-specific extended reasoning — only sent when explicitly enabled.
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": True}}
        resp = await self._client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        return self._parse(content)

    # -- prompt assembly ------------------------------------------------------ #
    def _format_turn(self, request: RoutingRequest) -> str:
        names = {p.role_id: p.display_name for p in request.participants}
        roster = ", ".join(f"{p.role_id} ({names[p.role_id]})" for p in request.participants)
        recent = "\n".join(
            f"  - {u.participant}: {u.text}" for u in request.recent[-5:]
        ) or "  (none)"
        state = json.dumps(request.state, ensure_ascii=False) if request.state else "{}"
        u: Utterance = request.utterance
        return (
            f"Active participants: {roster}\n"
            f"Shared state: {state}\n"
            f"Recent turns:\n{recent}\n\n"
            f"NEW turn — source={u.participant} ({names.get(u.participant, u.participant)}), "
            f"triage_class={u.triage_class}:\n"
            f'  "{u.text}"\n\n'
            "Decide routing. Output ONLY the JSON object."
        )

    # -- defensive JSON parse ------------------------------------------------- #
    @staticmethod
    def _parse(content: str) -> dict:
        text = content.strip()
        # Strip ```json fences if present.
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("\n") + 1 :] if "\n" in text else text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = _JSON_RE.search(content)
            if m:
                return json.loads(m.group(0))
            raise
