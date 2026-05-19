"""AI article generator for designservice.group blog.

Adapted from services.ai.bamboodom.BamboodomArticleService:
  - Bypasses AIOrchestrator (DB-backed PromptEngine not needed)
  - Direct httpx call to OpenRouter chat/completions
  - Strict JSON parsing + retry-with-feedback loop
  - Model chain: anthropic/claude-sonnet-4.5 → anthropic/claude-opus-4-6

Validator + image generation live in separate modules:
  - services/ai/designservice_validator.py (PR 5)
  - services/designservice_images/cover.py (PR 6)

Pipeline orchestrator lives in api/designservice_publish.py (PR 7).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml

log = structlog.get_logger()

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_MODEL_CHAIN = ("anthropic/claude-sonnet-4.5", "anthropic/claude-opus-4-6")
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_GENERATION_TIMEOUT = 180.0
_MAX_VALIDATION_RETRIES = 2  # = 3 total attempts


@dataclass(slots=True)
class DesignserviceArticleDraft:
    """Output of DesignserviceArticleService.generate.

    body_html — raw HTML fragment (no <html>/<head>/<body>). Will be wrapped
    into full document by services.seo.designservice_html.render_article.
    """

    body_html: str
    word_count: int = 0
    h2_count: int = 0
    internal_links_count: int = 0
    external_links_count: int = 0
    faq_count: int = 0
    model_used: str = ""
    attempts: int = 1
    retry_feedback: list[str] = field(default_factory=list)


class DesignserviceGenerationError(Exception):
    """Raised when LLM cannot produce valid JSON after all retries."""


def _load_prompt_template() -> dict[str, str]:
    """Load and parse designservice_article_v1.yaml."""
    path = _PROMPTS_DIR / "designservice_article_v1.yaml"
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "system" not in data or "user" not in data:
        raise DesignserviceGenerationError(
            "designservice_article_v1.yaml: missing system/user keys"
        )
    return data


def _build_messages(
    template: dict[str, str],
    article: Any,
    current_date_iso: str,
    retry_feedback: list[str] | None = None,
) -> list[dict[str, str]]:
    """Substitute placeholders + optionally append retry feedback to user."""
    system = template["system"]
    user = template["user"]
    meta = template.get("meta", {})

    # Build kw_secondary list bullets
    kw_secondary_list = (
        "\n".join(f"  - {k}" for k in (article.kw_secondary or []))
        or "  (нет дополнительных запросов)"
    )
    answer_points_list = (
        "\n".join(f"  - {p}" for p in (article.answer_points or []))
        or "  (не задано)"
    )

    replacements = {
        "<<h1>>": article.h1,
        "<<kw_primary>>": article.kw_primary or "",
        "<<kw_secondary_list>>": kw_secondary_list,
        "<<intent>>": article.intent or "info",
        "<<kind>>": article.kind or "info_basic",
        "<<service_label>>": article.service_label or "",
        "<<service_url>>": article.service_url or "",
        "<<geo>>": article.geo or "Крым",
        "<<city_slug>>": article.city_slug or "",
        "<<zhk_slug>>": article.zhk_slug or "",
        "<<target_words>>": str(article.target_words or 1500),
        "<<faq_entries>>": str(article.faq_entries or 3),
        "<<customer_pain>>": article.customer_pain or "",
        "<<answer_points_list>>": answer_points_list,
        "<<llm_brief>>": article.llm_brief or "",
        "<<current_date>>": current_date_iso,
    }

    for placeholder, value in replacements.items():
        user = user.replace(placeholder, value)
        system = system.replace(placeholder, value)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if retry_feedback:
        feedback_block = (
            "Твой ответ не прошёл валидацию (попытка ранее). Исправь следующее, "
            "пересоздай ВЕСЬ JSON заново:\n\n"
            + "\n".join(f"  - {item}" for item in retry_feedback)
        )
        messages.append({"role": "user", "content": feedback_block})

    return messages, meta


def _parse_draft(raw_reply: str) -> DesignserviceArticleDraft:
    """Strict JSON parsing — strip code fences if present, then json.loads."""
    text = raw_reply.strip()
    # Remove ```json ... ``` wrapping if any
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DesignserviceGenerationError(
            f"LLM returned non-JSON: {exc.msg} at pos {exc.pos}"
        ) from exc
    if "body_html" not in payload or not isinstance(payload["body_html"], str):
        raise DesignserviceGenerationError(
            "JSON missing required field 'body_html' or wrong type"
        )
    return DesignserviceArticleDraft(
        body_html=payload["body_html"],
        word_count=int(payload.get("word_count", 0) or 0),
        h2_count=int(payload.get("h2_count", 0) or 0),
        internal_links_count=int(payload.get("internal_links_count", 0) or 0),
        external_links_count=int(payload.get("external_links_count", 0) or 0),
        faq_count=int(payload.get("faq_count", 0) or 0),
    )


async def _call_openrouter(
    http_client: httpx.AsyncClient,
    api_key: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
) -> tuple[str, str]:
    """Try models in _MODEL_CHAIN until one succeeds. Returns (model_used, reply_text)."""
    last_err: Exception | None = None
    for model in _MODEL_CHAIN:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            r = await http_client.post(
                _OPENROUTER_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://designservice.group",
                    "X-Title": "DesignService blog (seo-master)",
                    "Content-Type": "application/json",
                },
                timeout=_GENERATION_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            last_err = exc
            log.warning("designservice.openrouter.http_error", model=model, err=str(exc))
            continue
        if r.status_code != 200:
            last_err = DesignserviceGenerationError(
                f"OpenRouter HTTP {r.status_code}: {r.text[:200]}"
            )
            log.warning(
                "designservice.openrouter.bad_status",
                model=model,
                status=r.status_code,
            )
            continue
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            last_err = DesignserviceGenerationError(
                f"OpenRouter empty choices: {data!r:.200}"
            )
            continue
        msg = choices[0].get("message", {})
        content = msg.get("content")
        if not content:
            last_err = DesignserviceGenerationError("OpenRouter empty content")
            continue
        return model, content
    raise DesignserviceGenerationError(
        f"All models in chain failed: last error = {last_err}"
    )


@dataclass
class DesignserviceArticleService:
    """Async article generator. Adapts BamboodomArticleService pattern.

    Args:
        http_client: shared httpx.AsyncClient (preferred for connection reuse).
        openrouter_api_key: from Settings.openrouter_api_key.
    """

    http_client: httpx.AsyncClient
    openrouter_api_key: str

    async def generate(
        self,
        article: Any,
        *,
        current_date_iso: str,
        retry_feedback: list[str] | None = None,
    ) -> DesignserviceArticleDraft:
        """Generate ONE article body. No internal retry — caller orchestrates.

        Args:
            article: integrations.designservice.Article (or dict-like).
            current_date_iso: e.g. "2026-05-19".
            retry_feedback: list of human-readable issues from previous attempt;
                appended as user message before regeneration.

        Returns:
            DesignserviceArticleDraft with body_html + self-reported counts.

        Raises:
            DesignserviceGenerationError on parse/network/all-models failure.
        """
        template = _load_prompt_template()
        messages, meta = _build_messages(
            template, article, current_date_iso, retry_feedback
        )
        max_tokens = int(meta.get("max_tokens", 12000))
        temperature = float(meta.get("temperature", 0.55))

        model_used, raw = await _call_openrouter(
            self.http_client,
            self.openrouter_api_key,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        draft = _parse_draft(raw)
        draft.model_used = model_used
        if retry_feedback:
            draft.retry_feedback = list(retry_feedback)
            draft.attempts = 1 + len(retry_feedback)
        log.info(
            "designservice.generate.ok",
            model=model_used,
            article_id=getattr(article, "id", "?"),
            word_count=draft.word_count,
        )
        return draft
