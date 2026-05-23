"""AI article generation for bamboodom.ru blog (Session 4A).

Architecture rationale:
- Bypasses `PromptEngine` (DB-backed) — bamboodom uses a self-contained YAML
  loaded directly from disk, because our prompt shape (JSON blocks, not
  Markdown/HTML) is incompatible with the existing prompt contract.
- Bypasses `AIOrchestrator` — we use a minimal httpx-based OpenRouter client
  because we need tight control over JSON parsing + retry on malformed output.
  Reusing the orchestrator would require a prompt registered in DB + a
  retry strategy that doesn't match our needs.
- Keeps `ArticleService`, `PromptEngine`, `ContentValidator` untouched (per
  side B directive in SESSION_4A_ANSWERS.md).

Flow:
    context = await _load_context(redis)              # blog_context + codes + catalog
    prompt = _build_messages(material, keyword, ctx)
    raw = await _call_openrouter(prompt, MODEL_CHAIN)  # with 1 JSON-retry
    draft = _parse_draft(raw)                          # strict validation
    issues = validator.validate(draft, forbidden)      # regex layers 1+2
    if issues: retry once (auto). still issues → manual.

4B.1.1 hotfix (after FEEDBACK_ITERATION_4B_1 from side B, 2026-04-24):
- regex layer 1+2 now scans article codes in ALL text-bearing blocks
  (p/h2/h3/list/callout) + title + excerpt, not only product-blocks.
  Fixes a hole where AI mentioned TK042A in prose and validator passed.

4B.1.8 (after side B shipped blog_article_index_full, 2026-04-25):
- Catalog now carries series, texture_type, name, description, price hints,
  sizes — fed to AI instead of bare code lists. AI picks articles that match
  the topic semantically, not by extrapolating from neighbouring numbers.
- Lighting category (466 ERDU items) is fetched/cached but NOT shown to AI;
  we are not writing lighting articles yet (Alex 2026-04-25).
- Old `_format_codes_sample` is kept as a graceful fallback for the case
  where the new endpoint is down — bot still generates, just with the v9
  prompt's article-list view.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
import sentry_sdk
import structlog
import yaml

from integrations.bamboodom import BamboodomClient
from services.ai.bamboodom_catalog import (
    CatalogFetchError,
    CatalogPayload,
    collect_codes_for_validator,
    fetch_catalog,
    format_catalog_for_prompt,
)

log = structlog.get_logger()

# Model chains for bamboodom pipeline. Production pays premium for quality
# (article) and uses Haiku for cheap validation checks (layer 3 in 4B).
_MODEL_CHAIN_ARTICLE: tuple[str, ...] = (
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-opus-4-6",
)
_MODEL_CHAIN_VALIDATE: tuple[str, ...] = (
    "anthropic/claude-haiku-4-5",
    "deepseek/deepseek-v3.2",
)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_GENERATION_TIMEOUT = 120.0  # seconds — article generation can take ~30-60s
_MAX_JSON_RETRIES = 1  # strict JSON parse retry budget (per side B §Б1)
_MAX_VALIDATION_RETRIES = 2  # v14 (2026-04-26): up to 2 retries (3 attempts) for full checklist

# Word-count policy from ARTICLE_LENGTH_POLICY.md (v5 prompt)
_MIN_WORDS_HARD = 1150
_MIN_WORDS_SOFT = 1050  # 4M: soft warning, статья публикуется но с пометкой
_MAX_WORDS_HARD = 2200
_TARGET_WORDS_MIN = 1700
_TARGET_WORDS_MAX = 1900
_MAX_LENGTH_RETRIES = 1  # if draft is below _MIN_WORDS_HARD, retry once

# Prompt template file — loaded once into module-level cache.
_PROMPT_PATH = Path(__file__).parent / "prompts" / "bamboodom_article_v1.yaml"
_KNOWLEDGE_BASE_PATH = Path(__file__).parent.parent.parent / "docs" / "bamboodom" / "knowledge_base.md"

MaterialCategory = Literal["wpc", "flex", "reiki", "profiles"]

# 4B.1.4: progress callback signature. Router passes a coroutine that writes
# the current stage into a shared dict; the router's background loop reads
# that dict every ~3s and re-renders the progress-bar message in Telegram.
ProgressCallback = Callable[[str, int], Awaitable[None]]

# Block types the server accepts. v14 (2026-04-26): extended with img, h4, ul,
# ol, quote per BAMBOODOM_ARTICLE_TEMPLATES.md spec from Side B. Old `list`
# kept for backward-compat (Side B renders it the same as ul).
_ALLOWED_BLOCK_TYPES: frozenset[str] = frozenset(
    {"h2", "h3", "h4", "p", "list", "ul", "ol", "product", "callout", "cta", "table", "img", "quote"}
)

# v14 image slots — must match BAMBOODOM_ARTICLE_TEMPLATES.md section 2.
_ALLOWED_IMG_SLOTS: frozenset[str] = frozenset(
    {
        "hero",
        "wide-1",
        "wide-2",
        "landscape-1",
        "landscape-2",
        "square-1",
        "square-2",
        "square-3",
        "portrait-1",
        "portrait-2",
    }
)

# v14 categories — must match BAMBOODOM_ARTICLE_TEMPLATES.md section 1.
_ALLOWED_CATEGORIES: frozenset[str] = frozenset({"wpc", "flex", "reiki", "prof", "spc", "magnez", "lighting"})

# v14 template names by id (1-10), per BAMBOODOM_ARTICLE_TEMPLATES.md section 3.
_TEMPLATE_NAMES: dict[int, str] = {
    1: "technical_guide",
    2: "buyers_guide",
    3: "comparison",
    4: "case_study",
    5: "educational",
    6: "room_focused",
    7: "problem_solution",
    8: "trends",
    9: "geo",
    10: "magazine",
}

# v14 minimum image-slots per template, per BAMBOODOM_ARTICLE_TEMPLATES.md.
_TEMPLATE_MIN_IMG: dict[int, int] = {
    1: 7,  # technical_guide
    2: 5,  # buyers_guide
    3: 5,  # comparison
    4: 7,  # case_study
    5: 5,  # educational
    6: 6,  # room_focused
    7: 5,  # problem_solution
    8: 5,  # trends
    9: 4,  # geo
    10: 7,  # magazine
}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BamboodomArticleDraft:
    """Validated draft — what gets shown in FSM preview and published via API.

    v14 (2026-04-26): added template_id (1-10), template_name, category, tags,
    cover per BAMBOODOM_ARTICLE_TEMPLATES.md from Side B. The old `format`
    field (1-7) is kept for backward-compat with v12 reads but not produced
    by the v14 pipeline.
    """

    title: str
    excerpt: str
    blocks: list[dict[str, Any]]
    seo: dict[str, str] = field(default_factory=dict)
    format: int | None = None  # legacy v5-v12 (1-7), unused in v14
    template_id: int | None = None  # v14: 1-10 from 10-templates library
    template_name: str | None = None  # v14: matching name (technical_guide etc)
    category: str | None = None  # v14: wpc/flex/reiki/prof/spc/magnez/lighting
    tags: list[str] = field(default_factory=list)  # v14: optional tags
    cover: str = ""  # v14: optional path to hero image
    word_count: int = 0  # server-side word count (not the model's self-report)

    def to_publish_payload(self) -> dict[str, Any]:
        """Shape the draft for blog_publish.

        v14: adds template_id, template_name, category, tags, cover when set.
        Side B validates template_id ∈ {1..10} and category ∈ allowed set,
        returns HTTP 422 otherwise (BAMBOODOM_ARTICLE_TEMPLATES.md section 5).
        """
        payload: dict[str, Any] = {
            "title": self.title,
            "excerpt": self.excerpt,
            # In sandbox mode we want instant preview (no draft gate). When we
            # move to production in 4B this flag flips back to True.
            "draft": False,
            "blocks": self.blocks,
        }
        if self.seo:
            payload["seo"] = self.seo
        if self.template_id is not None:
            payload["template_id"] = self.template_id
        if self.template_name:
            payload["template_name"] = self.template_name
        if self.category:
            payload["category"] = self.category
        if self.tags:
            payload["tags"] = self.tags
        if self.cover:
            payload["cover"] = self.cover
        return payload


@dataclass(slots=True)
class ValidationIssue:
    kind: Literal[
        "forbidden_claim",
        "price_in_text",
        "bad_block_type",
        "bad_article",
        "too_short",
        "list_of_two",
        "short_article",  # 4M: soft warning 1400-1499, не блокирует ok
    ]
    detail: str
    block_index: int | None = None


@dataclass(slots=True)
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # 4M: short_article (soft warning 1400-1499) не блокирует публикацию.
        return not [i for i in self.issues if i.kind != "short_article"]


# ---------------------------------------------------------------------------
# Validator — regex layers 1 (forbidden_claims) + 2 (prices). Layer 3 in 4B.
# ---------------------------------------------------------------------------


class BamboodomValidator:
    """Regex-based content checks. Mirrors side B expectations from SESSION_4A_ANSWERS."""

    # Prices: "2500 руб", "от 1000 ₽", "около 500 р.", "~750 RUB". Any integer
    # close to a currency marker is suspect — product-block is the only legal
    # way to mention prices.
    _PRICE_RE = re.compile(
        r"\b\d{2,6}\s*(?:руб\.?|₽|RUB|р\.)\b",
        flags=re.IGNORECASE,
    )

    # Article codes in prose (4B.1.1 hotfix). AI sometimes mentions articles in
    # paragraph text without a product-block (e.g. "На проекте TK042A..."). The
    # product-block path already checks .article field; this regex catches the
    # prose path. WPC series are 1–3 uppercase letters (BJL is 3); flex=F###;
    # reiki=R###; profiles=XHS-*. We deliberately avoid BJ?\d{2} — BJ/BJL suffixes
    # are handled by the generic [A-Z]{1,3} tail after TK\d{3}.
    _ARTICLE_CODE_RE = re.compile(
        r"\b(?:TK\d{3}[A-Z]{1,3}|F\d{3}|R\d{3}|XHS-[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)\b",
    )

    # 4B.1.9 v12 hotfix: F\d{3} also matches frost-resistance grades for
    # ceramic/granite (F200 = 200 freeze cycles, F300 = 300 cycles). When a
    # frost/grade keyword appears immediately before the code, treat it as a
    # technical grade, not an article code. Russian declensions covered with
    # \w* suffix (морозостойкость / морозостойкости / морозостойкостью / ...).
    _FROST_GRADE_CONTEXT_RE = re.compile(
        r"(?:морозостойкост\w*|класс\w*|марк\w*|grade)[\s\-:,]+(?:F\d{3})\b",
        flags=re.IGNORECASE,
    )

    def __init__(self, forbidden_claims: list[str]) -> None:
        # Normalize all forbidden claims to lowercase for case-insensitive match.
        self._forbidden = [c.strip().lower() for c in forbidden_claims if c and c.strip()]

    def _is_frost_grade(self, text: str, match_start: int, code: str) -> bool:
        """v12 hotfix: F\\d{3} preceded by frost/grade keyword is NOT an article."""
        if not code.startswith("F") or len(code) != 4 or not code[1:].isdigit():
            return False
        # Look back ~40 chars in the same text for the keyword pattern.
        window = text[max(0, match_start - 40) : match_start + len(code)]
        return bool(self._FROST_GRADE_CONTEXT_RE.search(window))

    def validate(  # noqa: C901  — tight regex-by-block check; splitting up harms readability
        self,
        draft: BamboodomArticleDraft,
        *,
        valid_article_codes: frozenset[str] | None = None,
    ) -> ValidationResult:
        """Run all regex checks on a draft. Returns a list of issues."""
        result = ValidationResult()

        for idx, block in enumerate(draft.blocks):
            btype = block.get("type")

            # Block type whitelist
            if btype not in _ALLOWED_BLOCK_TYPES:
                result.issues.append(
                    ValidationIssue(
                        kind="bad_block_type",
                        detail=f"Block #{idx} has type={btype!r}, allowed: {sorted(_ALLOWED_BLOCK_TYPES)}",
                        block_index=idx,
                    )
                )
                continue

            # Product-block: article must be in known codes
            if btype == "product":
                article = str(block.get("article", "")).strip()
                if valid_article_codes is not None and article and article not in valid_article_codes:
                    result.issues.append(
                        ValidationIssue(
                            kind="bad_article",
                            detail=f"Unknown article code {article!r} in block #{idx}",
                            block_index=idx,
                        )
                    )
                continue  # don't check text content on product blocks

            # CTA-block: `link` is required (per side B blog_publish schema).
            # We also auto-normalize `href` -> `link` if the model forgot.
            if btype == "cta":
                if "href" in block and "link" not in block:
                    block["link"] = block.pop("href")  # normalize in-place
                link = str(block.get("link", "")).strip()
                if not link:
                    result.issues.append(
                        ValidationIssue(
                            kind="bad_block_type",
                            detail=f"CTA block #{idx} missing link",
                            block_index=idx,
                        )
                    )

            # Gather text content from block (may be in text/title/items).
            text_parts: list[str] = []
            for field_name in ("text", "title"):
                v = block.get(field_name)
                if isinstance(v, str):
                    text_parts.append(v)
            items = block.get("items")
            if isinstance(items, list):
                text_parts.extend(str(i) for i in items if isinstance(i, (str, int)))
            combined = "\n".join(text_parts).strip()
            if not combined:
                continue

            # Check forbidden claims (case-insensitive substring match)
            lowered = combined.lower()
            for claim in self._forbidden:
                if claim and claim in lowered:
                    result.issues.append(
                        ValidationIssue(
                            kind="forbidden_claim",
                            detail=f"Forbidden phrase {claim!r} in block #{idx}",
                            block_index=idx,
                        )
                    )

            # Check price-like patterns
            for m in self._PRICE_RE.finditer(combined):
                result.issues.append(
                    ValidationIssue(
                        kind="price_in_text",
                        detail=f"Price-like token {m.group(0)!r} in block #{idx} — use product-block",
                        block_index=idx,
                    )
                )

            # Check article codes mentioned in prose (4B.1.1 hotfix).
            # Deduplicate per block — one nag per unknown code, not per occurrence.
            # 4B.1.9 v12: skip F\d{3} hits that are clearly frost-resistance
            # grades ("морозостойкость F300", "класс F200"), not article codes.
            if valid_article_codes is not None:
                seen_unknown_in_block: set[str] = set()
                for m in self._ARTICLE_CODE_RE.finditer(combined):
                    code = m.group(0)
                    if code in valid_article_codes or code in seen_unknown_in_block:
                        continue
                    if self._is_frost_grade(combined, m.start(), code):
                        continue
                    seen_unknown_in_block.add(code)
                    result.issues.append(
                        ValidationIssue(
                            kind="bad_article",
                            detail=(
                                f"Unknown article code {code!r} mentioned in block #{idx} text "
                                f"(not a product-block — check the paragraph)"
                            ),
                            block_index=idx,
                        )
                    )

        # Sanity-check title/excerpt against forbidden claims too
        title_low = draft.title.lower()
        excerpt_low = draft.excerpt.lower()
        for claim in self._forbidden:
            if claim and (claim in title_low or claim in excerpt_low):
                result.issues.append(
                    ValidationIssue(
                        kind="forbidden_claim",
                        detail=f"Forbidden phrase {claim!r} in title/excerpt",
                    )
                )

        # …and article codes in title/excerpt (4B.1.1).
        # v12: same frost-grade exception as in body blocks.
        if valid_article_codes is not None:
            seen_unknown_meta: set[str] = set()
            for text_src, where in ((draft.title, "title"), (draft.excerpt, "excerpt")):
                for m in self._ARTICLE_CODE_RE.finditer(text_src):
                    code = m.group(0)
                    if code in valid_article_codes or code in seen_unknown_meta:
                        continue
                    if self._is_frost_grade(text_src, m.start(), code):
                        continue
                    seen_unknown_meta.add(code)
                    result.issues.append(
                        ValidationIssue(
                            kind="bad_article",
                            detail=f"Unknown article code {code!r} mentioned in {where}",
                        )
                    )

        # RULE 1 (post-review): list with <=3 items should be a table.
        for idx, block in enumerate(draft.blocks):
            if block.get("type") == "list":
                items = block.get("items") or []
                if isinstance(items, list) and len(items) < 4:
                    result.issues.append(
                        ValidationIssue(
                            kind="list_of_two",
                            detail=(
                                f"list block #{idx} has only {len(items)} items; "
                                f"for 2-3 parallel positions use type=table"
                            ),
                            block_index=idx,
                        )
                    )

        return result


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class BamboodomGenerationError(Exception):
    """Raised when AI generation fails after all retries / fallbacks."""


class BamboodomArticleService:
    """Generates blog articles for bamboodom.ru using Claude Sonnet via OpenRouter.

    NOT thread-safe (holds no mutable state). Cheap to construct per-call.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        openrouter_api_key: str,
        bamboodom_client: BamboodomClient,
    ) -> None:
        self._http = http_client
        self._api_key = openrouter_api_key
        self._bamboodom = bamboodom_client
        self._progress_cb: ProgressCallback | None = None

    async def _emit(self, stage: str, attempt: int = 1) -> None:
        """Fire progress callback if one is registered (4B.1.4). Best-effort."""
        cb = self._progress_cb
        if cb is None:
            return
        try:
            await cb(stage, attempt)
        except Exception as exc:
            log.debug("bamboodom_ai_progress_cb_failed", stage=stage, error=str(exc))

    # ----- public API -------------------------------------------------

    async def generate_and_validate(
        self,
        *,
        material: MaterialCategory,
        keyword: str,
        current_date_iso: str,
        progress_cb: ProgressCallback | None = None,
    ) -> tuple[BamboodomArticleDraft, ValidationResult]:
        """High-level: fetch context → generate draft → validate → (auto-retry once).

        Returns the final draft + validation result. Caller decides what to do
        with residual issues (show to operator, publish anyway, etc).
        """
        self._progress_cb = progress_cb
        try:
            return await self._generate_and_validate_impl(
                material=material,
                keyword=keyword,
                current_date_iso=current_date_iso,
            )
        finally:
            self._progress_cb = None

    async def _generate_and_validate_impl(
        self,
        *,
        material: MaterialCategory,
        keyword: str,
        current_date_iso: str,
    ) -> tuple[BamboodomArticleDraft, ValidationResult]:
        """Inner implementation — kept separate so generate_and_validate can
        manage self._progress_cb lifecycle cleanly via try/finally (4B.1.4)."""
        # 1. Load cached context (falls back to fresh fetch if cache is cold)
        await self._emit("context", 1)
        context, codes, catalog = await self._load_context()
        await self._emit("build", 1)

        # 4B.1.8: prefer catalog-derived code set if available (covers all
        # categories including lighting), fall back to codes_obj otherwise.
        if catalog is not None:
            valid_codes = collect_codes_for_validator(catalog) | _collect_valid_codes(codes)
        else:
            valid_codes = _collect_valid_codes(codes)

        forbidden = list(getattr(context, "forbidden_claims", None) or [])

        validator = BamboodomValidator(forbidden)
        system, user = self._build_messages(
            material=material,
            keyword=keyword,
            current_date=current_date_iso,
            context_obj=context,
            codes_obj=codes,
            catalog=catalog,
        )

        draft: BamboodomArticleDraft | None = None
        validation_issues: list[str] = []
        length_retry_used = False

        for attempt in range(_MAX_VALIDATION_RETRIES + 1):
            messages = self._augment_messages_after_validation(system, user, validation_issues)
            await self._emit("call_primary", attempt + 1)
            raw_reply = await self._call_with_json_retry(messages)
            await self._emit("parse", attempt + 1)
            draft = _parse_draft(raw_reply)

            # Overwrite model's self-reported word_count with our honest count.
            draft.word_count = count_words(draft)

            # v14 (2026-04-26): full-checklist retry. On every attempt except
            # the last, run the v14 quality checklist; if any item fails,
            # feed the bullet list back to the model and retry.
            # _MAX_VALIDATION_RETRIES is the cap — after the last attempt we
            # ship as-is with warnings so we don't burn AI tokens forever.
            if attempt < _MAX_VALIDATION_RETRIES:
                v14_failures = _v14_quality_checklist(draft, valid_article_codes=valid_codes)
                if v14_failures:
                    v14_feedback = (
                        f"Твой ответ не прошёл чек-лист качества (попытка "
                        f"{attempt + 1}/{_MAX_VALIDATION_RETRIES + 1}). "
                        "Исправь следующее и перегенерируй JSON ЦЕЛИКОМ:\n\n"
                        + "\n".join(f"  • {item}" for item in v14_failures)
                        + "\n\nВерни тот же формат JSON, но с этими исправлениями."
                    )
                    validation_issues.append(v14_feedback)
                    log.info(
                        "bamboodom_ai_v14_retry_scheduled",
                        attempt=attempt,
                        failures_count=len(v14_failures),
                        failures=v14_failures[:5],
                        template_id=draft.template_id,
                        block_count=len(draft.blocks),
                        word_count=draft.word_count,
                    )
                    await self._emit("v14_retry", attempt + 2)
                    continue  # retry with full checklist feedback
            else:
                # Last attempt — record final checklist state for UI warning.
                final_failures = _v14_quality_checklist(draft, valid_article_codes=valid_codes)
                if final_failures:
                    log.warning(
                        "bamboodom_ai_v14_checklist_unmet",
                        attempt=attempt,
                        failures_count=len(final_failures),
                        failures=final_failures,
                        template_id=draft.template_id,
                    )
                    for f in final_failures:
                        validation_issues.append(f"v14: {f}")

            await self._emit("validate", attempt + 1)
            result = validator.validate(draft, valid_article_codes=valid_codes)

            # Length check (v5) — treat under-minimum as an auto-retry trigger
            # on the first pass only. Report as an advisory issue afterwards.
            if draft.word_count < _MIN_WORDS_HARD:
                if not length_retry_used and attempt == 0:
                    length_retry_used = True
                    length_feedback = (
                        f"Твой ответ слишком короткий — {draft.word_count} слов, "
                        f"минимум {_MIN_WORDS_HARD}. Расширь самый короткий "
                        "раздел ФАКТИЧЕСКИМ содержанием из KNOWLEDGE_BASE "
                        "(примеры, технические детали, сценарии). НЕ добавляй воды."
                    )
                    validation_issues.append(length_feedback)
                    log.info(
                        "bamboodom_ai_length_retry_scheduled",
                        word_count=draft.word_count,
                        target_min=_MIN_WORDS_HARD,
                    )
                    await self._emit("length_retry", attempt + 2)
                    continue  # retry with length nudge
                # 4M: soft 1400-1499 — статья пройдёт но с пометкой 'short_article'.
                #     <1400 — старое поведение: too_short critical.
                if draft.word_count >= _MIN_WORDS_SOFT:
                    result.issues.append(
                        ValidationIssue(
                            kind="short_article",
                            detail=(
                                f"Draft is {draft.word_count} words; soft warning "
                                f"(below target {_MIN_WORDS_HARD}, above min {_MIN_WORDS_SOFT})"
                            ),
                        )
                    )
                else:
                    result.issues.append(
                        ValidationIssue(
                            kind="too_short",
                            detail=(f"Draft is {draft.word_count} words; below hard min {_MIN_WORDS_SOFT}"),
                        )
                    )
            elif draft.word_count > _MAX_WORDS_HARD:
                # Going over max is less critical — operator can trim in moderation.
                log.info(
                    "bamboodom_ai_over_max_length",
                    word_count=draft.word_count,
                    target_max=_MAX_WORDS_HARD,
                )

            if result.ok:
                log.info(
                    "bamboodom_ai_generate_ok",
                    material=material,
                    keyword=keyword,
                    blocks=len(draft.blocks),
                    word_count=draft.word_count,
                    format=draft.format,
                    attempt=attempt,
                    catalog_used=catalog is not None,
                )
                return draft, result

            # Failed validation — log every attempt (per §В2 guidance).
            log.warning(
                "bamboodom_ai_validation_failed",
                material=material,
                keyword=keyword,
                attempt=attempt,
                word_count=draft.word_count,
                issues=[i.detail for i in result.issues],
            )
            sentry_sdk.capture_message(
                "bamboodom AI validation failed",
                level="warning",
                extras={
                    "material": material,
                    "keyword": keyword,
                    "attempt": attempt,
                    "word_count": draft.word_count,
                    "issues": [i.detail for i in result.issues],
                },
            )

            if attempt >= _MAX_VALIDATION_RETRIES:
                # Give up — return last draft + issues; operator decides.
                return draft, result

            # Accumulate issues for next attempt's prompt augmentation.
            validation_issues.extend(i.detail for i in result.issues)
            await self._emit("validation_retry", attempt + 2)

        # Unreachable (loop always returns), but keep type-checker happy.
        if draft is None:  # pragma: no cover
            raise BamboodomGenerationError("no draft produced")
        return draft, validator.validate(draft, valid_article_codes=valid_codes)

    # ----- internals --------------------------------------------------

    async def _load_context(self) -> tuple[Any, Any, CatalogPayload | None]:
        """Fetch blog_context + blog_article_codes + (NEW 4B.1.8) full catalog.

        Catalog fetch is best-effort: if the new endpoint is down, we log a
        warning and proceed with codes-only — the prompt then falls back to the
        v9 article-code list view via _format_codes_sample.
        """
        ctx_resp, _ = await self._bamboodom.get_context(force_refresh=False)
        codes_resp, _ = await self._bamboodom.get_article_codes(force_refresh=False)
        catalog: CatalogPayload | None
        try:
            catalog, _hit = await fetch_catalog(http_client=self._http)
        except CatalogFetchError as exc:
            log.warning("bamboodom_catalog_unavailable_falling_back", error=str(exc))
            catalog = None
        return ctx_resp, codes_resp, catalog

    def _build_messages(
        self,
        *,
        material: MaterialCategory,
        keyword: str,
        current_date: str,
        context_obj: Any,
        codes_obj: Any,
        catalog: CatalogPayload | None,
    ) -> tuple[str, str]:
        """Render the YAML prompt template with live context.

        Two placeholders coexist for backward compatibility:
        - <<articles_catalog>> — v10 rich catalog (preferred).
        - <<article_codes_sample>> — v9 bare code list (fallback if catalog
          fetch failed AND the YAML still references this placeholder).
        """
        template = _load_prompt_template()
        kb_text = _load_knowledge_base()
        forbidden_lines = (
            "\n".join(f"- {c}" for c in (getattr(context_obj, "forbidden_claims", None) or [])) or "(из knowledge base)"
        )
        codes_sample = _format_codes_sample(codes_obj, material)

        if catalog is not None:
            catalog_text = format_catalog_for_prompt(
                catalog,
                primary_material=material,
                shuffle_seed=keyword,  # v14.1: per-topic deterministic shuffle
            )
        else:
            catalog_text = (
                "(полный каталог временно недоступен — используй список артикулов ниже как источник истины)\n\n"
                + codes_sample
            )

        # 5N (2026-04-28): geo-focus block. If keyword mentions a city,
        # inject explicit instructions to make the article LOCALLY DIFFERENT
        # so 16 city-variants don't end up as near-duplicates.
        geo_focus = self._build_geo_focus_block(keyword)

        def _fill(text: str) -> str:
            return (
                text.replace("<<knowledge_base>>", kb_text)
                .replace("<<forbidden_claims_list>>", forbidden_lines)
                .replace("<<material_category>>", material)
                .replace("<<keyword>>", keyword)
                .replace("<<current_date>>", current_date)
                .replace("<<article_codes_sample>>", codes_sample)
                .replace("<<articles_catalog>>", catalog_text)
                .replace("<<geo_focus>>", geo_focus)
            )

        return _fill(template["system"]), _fill(template["user"])

    # 5N (2026-04-28): per-city profiles for Crimean cities. Used to inject
    # local context so geo-variants are substantively different.
    _CITY_PROFILES = {
        "Симферополь": "столица Крыма, континентальный климат, центр строительной торговли; основной канал - самовывоз и доставка по городу",
        "Севастополь": "город федерального значения, побережье Чёрного моря, повышенная влажность и солевая нагрузка на фасады; крупный частный сектор",
        "Ялта": "ЮБК, субтропический микроклимат, влажность и интенсивный УФ; курортная застройка, гостиницы и виллы",
        "Феодосия": "восточное побережье, степной приморский климат, ветровая нагрузка; перепады влажности",
        "Евпатория": "западное побережье, песчаные пляжи, ветры и солевая взвесь; курортный жилой фонд",
        "Керчь": "восточная оконечность Крыма, Керченский пролив, ветры и резкие температурные перепады",
        "Алушта": "ЮБК между Симферополем и Ялтой, защищённая бухта, мягкий климат; частные дома и пансионаты",
        "Судак": "юго-восток ЮБК, степной приморский климат, виноградарский регион; гостевые дома и виллы",
        "Бахчисарай": "предгорье, континентальный климат, исторический центр; реставрация и частное домостроение",
        "Джанкой": "север Крыма, степная зона, континентальный климат с большой амплитудой температур",
        "Саки": "западное побережье, лиманный климат, грязелечебный курорт; санаторный фонд",
        "Армянск": "север Крыма у Перекопа, степь, ветры и низкая влажность; промышленная зона",
        "Красноперекопск": "север Крыма, степь, повышенная солёность почв и воздуха",
        "Старый Крым": "предгорье, мягкий микроклимат, исторический город",
        "Белогорск": "центральный Крым, предгорье, континентальный климат",
        "Щёлкино": "Керченский полуостров, побережье Азовского моря, ветры; курортный посёлок",
    }

    def _build_geo_focus_block(self, keyword: str) -> str:
        """Detect a Crimean city in the keyword. If found, return a focused
        instruction block requiring genuinely city-specific content. Empty
        string when no city — generic article continues unchanged.
        """
        kw_lower = (keyword or "").lower()
        matched_city = None
        matched_profile = None
        for city, profile in self._CITY_PROFILES.items():
            # Match on stem to catch inflected forms (Симферополь, Симферополе…)
            stem = city[:-2] if len(city) > 4 else city
            if stem.lower() in kw_lower:
                matched_city = city
                matched_profile = profile
                break
        if not matched_city:
            return ""

        return (
            f"=== GEO-FOCUS: статья под город {matched_city} ===\n"
            f"Эта статья - один из 16 гео-вариантов под разные города Крыма. "
            f"Её задача - НЕ повторять текст других вариантов. ОБЯЗАТЕЛЬНО:\n"
            f"  1. Title и H1 содержат '{matched_city}' (например: '...в {matched_city}').\n"
            f"  2. Slug содержит название города в конце (через дефис).\n"
            f"  3. В первом lead-абзаце упомяни {matched_city} явно. Контекст: "
            f"{matched_profile}.\n"
            f"  4. Отдельный H2-раздел 'Особенности применения в {matched_city}' "
            f"(минимум 2-3 абзаца): местный климат, влажность, требования к материалу "
            f"под локальные условия. БЕЗ этого раздела статья - дубликат.\n"
            f"  5. Короткий раздел про доставку и работу в {matched_city} "
            f"(1-2 абзаца): откуда везут, сроки.\n"
            f"  6. НЕ ПИШИ общие фразы 'у нас в России', 'по всей стране' - "
            f"фокус только на {matched_city} и Крыму.\n"
            f"  7. ОСТАЛЬНОЙ контент (что такое материал, общие преимущества) - "
            f"короче обычного. Основной упор на гео-специфику.\n"
            f"=========================================="
        )

    @staticmethod
    def _augment_messages_after_validation(
        system: str,
        user: str,
        prior_issues: list[str],
    ) -> list[dict[str, str]]:
        """Inject previous validation feedback into the user turn."""
        if not prior_issues:
            return [{"role": "system", "content": system}, {"role": "user", "content": user}]

        feedback = (
            "\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ ПРОШЁЛ ВАЛИДАЦИЮ:\n"
            + "\n".join(f"- {issue}" for issue in prior_issues)
            + "\n\nИсправь и верни новую версию строго в формате JSON без обёрток."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user + feedback},
        ]

    async def _call_with_json_retry(self, messages: list[dict[str, str]]) -> str:
        """Call OpenRouter; on invalid JSON reply — retry once with strict prompt."""
        raw = await self._call_openrouter(messages, _MODEL_CHAIN_ARTICLE)
        try:
            _parse_draft(raw)  # purely validates the JSON; discard result
            return raw
        except BamboodomGenerationError:
            log.warning("bamboodom_ai_json_invalid_first_try", reply_preview=raw[:300])
            # Retry with a stricter nudge.
            strict_messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": raw[:4000],  # truncate very long replies for the history
                },
                {
                    "role": "user",
                    "content": (
                        "Твой ответ не был валидным JSON. Верни ТОЛЬКО JSON-объект "
                        '{"title":...,"excerpt":...,"blocks":[...]}, без markdown, '
                        "без обёрток ``` и без пояснений до или после."
                    ),
                },
            ]
            return await self._call_openrouter(strict_messages, _MODEL_CHAIN_ARTICLE)

    async def _call_openrouter(
        self,
        messages: list[dict[str, str]],
        model_chain: tuple[str, ...],
    ) -> str:
        """POST /chat/completions with fallback across the model chain."""
        last_exc: Exception | None = None
        primary = True
        for model in model_chain:
            if not primary:
                await self._emit("call_fallback", 1)
            primary = False
            try:
                resp = await self._http.post(
                    _OPENROUTER_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://bamboodom.ru",
                        "X-Title": "SEO Master Bamboodom",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 8000,
                        "temperature": 0.6,
                    },
                    timeout=_GENERATION_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                log.info(
                    "bamboodom_ai_call_ok",
                    model=model,
                    usage=data.get("usage"),
                )
                return content  # type: ignore[no-any-return]
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                log.warning("bamboodom_ai_call_failed", model=model, error=str(exc))
                last_exc = exc
                continue

        raise BamboodomGenerationError(f"All models failed: {[m for m in model_chain]}; last: {last_exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_prompt_template() -> dict[str, str]:
    """Load the YAML prompt. Raises on missing file or malformed YAML."""
    if not _PROMPT_PATH.exists():
        raise BamboodomGenerationError(f"prompt template not found: {_PROMPT_PATH}")
    with _PROMPT_PATH.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict) or "system" not in raw or "user" not in raw:
        raise BamboodomGenerationError(f"prompt template has unexpected shape: {_PROMPT_PATH}")
    return {"system": raw["system"], "user": raw["user"]}


def _load_knowledge_base() -> str:
    """Load the markdown knowledge base for inline injection into the prompt."""
    if not _KNOWLEDGE_BASE_PATH.exists():
        # Non-fatal — we can still generate, just without company context.
        log.warning("bamboodom_kb_missing", path=str(_KNOWLEDGE_BASE_PATH))
        return "(knowledge base not available)"
    return _KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")


def _collect_valid_codes(codes_obj: Any) -> frozenset[str]:
    """Flatten category-wise code lists into a single lookup set.

    Kept as a fallback for the case where the rich catalog endpoint is down —
    the bot still has a code allowlist to validate AI output against.
    """
    out: set[str] = set()
    for category_name, count in codes_obj.categories().items():
        # categories() returns only counts; access the raw list via getattr/extras
        raw_list = getattr(codes_obj, category_name, None)
        if isinstance(raw_list, list):
            out.update(str(c) for c in raw_list if isinstance(c, str))
        else:
            extras = getattr(codes_obj, "__pydantic_extra__", None) or {}
            extra_list = extras.get(category_name)
            if isinstance(extra_list, list):
                out.update(str(c) for c in extra_list if isinstance(c, str))
        _ = count  # unused, kept for clarity
    return frozenset(out)


_CODES_PER_LINE = 12  # codes per line for readability in the fallback prompt


def _format_codes_sample(codes_obj: Any, material: MaterialCategory) -> str:
    """Format the article-code catalog as a flat list (4B.1.7 fallback view).

    Used as a fallback when blog_article_index_full is unreachable. Shows ALL
    codes from ALL categories, sorted, in chunks of _CODES_PER_LINE per line.
    The chosen material category is shown first as "Основная", others go below
    as "Сопутствующая".

    The 4B.1.8 path prefers `format_catalog_for_prompt` from bamboodom_catalog
    module — that view has descriptions, series, texture types and is far
    richer. This function is the safety net.
    """

    def _list(name: str) -> list[str]:
        v = getattr(codes_obj, name, None) or []
        if not isinstance(v, list):
            extras = getattr(codes_obj, "__pydantic_extra__", None) or {}
            v = extras.get(name, []) if isinstance(extras, dict) else []
        return [str(c) for c in v if isinstance(c, str)]

    def _format_block(name: str, codes: list[str], primary: bool = False) -> str:
        if not codes:
            return ""
        sorted_codes = sorted(codes)
        chunks = [
            ", ".join(sorted_codes[i : i + _CODES_PER_LINE]) for i in range(0, len(sorted_codes), _CODES_PER_LINE)
        ]
        kind = "Основная" if primary else "Сопутствующая"
        prefix = f"{kind} категория ({name}, всего {len(codes)} артикулов):"
        return prefix + "\n  " + "\n  ".join(chunks)

    blocks: list[str] = []
    primary_block = _format_block(material, _list(material), primary=True)
    if primary_block:
        blocks.append(primary_block)

    # Auxiliary categories — full list of each so AI sees real XHS profile
    # codes, real reiki codes, real cross-material codes.
    for aux in ("wpc", "flex", "reiki", "profiles"):
        if aux == material:
            continue
        aux_block = _format_block(aux, _list(aux), primary=False)
        if aux_block:
            blocks.append(aux_block)

    return "\n\n".join(blocks) if blocks else "(список временно недоступен)"


def _parse_draft(raw_reply: str) -> BamboodomArticleDraft:  # noqa: C901 — strict JSON parse + field extraction
    """Strict JSON parse + shape validation. Raises BamboodomGenerationError."""
    # Models sometimes wrap JSON in ```json ... ``` or add a prose preamble.
    # Try to recover the first {...} block as a last resort.
    candidate = raw_reply.strip()
    if candidate.startswith("```"):
        # Strip fenced code block header + trailing fence
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
    # Find first top-level {...}
    first_brace = candidate.find("{")
    last_brace = candidate.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = candidate[first_brace : last_brace + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise BamboodomGenerationError(f"invalid JSON from model: {exc}") from exc

    if not isinstance(parsed, dict):
        raise BamboodomGenerationError(f"model returned {type(parsed).__name__}, expected object")
    title = parsed.get("title")
    excerpt = parsed.get("excerpt")
    blocks = parsed.get("blocks")
    if not isinstance(title, str) or not title.strip():
        raise BamboodomGenerationError("missing/empty title in model response")
    if not isinstance(excerpt, str) or not excerpt.strip():
        raise BamboodomGenerationError("missing/empty excerpt in model response")
    if not isinstance(blocks, list) or not blocks:
        raise BamboodomGenerationError("missing/empty blocks in model response")
    # Coerce blocks to list[dict]
    cleaned_blocks: list[dict[str, Any]] = []
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict) or "type" not in block:
            raise BamboodomGenerationError(f"block #{idx} is not a dict or missing 'type': {block!r}")
        cleaned_blocks.append(block)

    # Optional: seo block (meta_title / meta_description)
    seo_raw = parsed.get("seo")
    seo: dict[str, str] = {}
    if isinstance(seo_raw, dict):
        meta_title = seo_raw.get("meta_title")
        meta_description = seo_raw.get("meta_description")
        if isinstance(meta_title, str) and meta_title.strip():
            seo["meta_title"] = meta_title.strip()[:80]
        if isinstance(meta_description, str) and meta_description.strip():
            seo["meta_description"] = meta_description.strip()[:200]

    # v12 legacy: optional format (int 1-7). v14 doesn't use it but we keep
    # parsing for backward-compat in case an older reply rolls in.
    fmt_raw = parsed.get("format")
    fmt_val: int | None = None
    if isinstance(fmt_raw, int) and 1 <= fmt_raw <= 7:
        fmt_val = fmt_raw
    elif isinstance(fmt_raw, str) and fmt_raw.strip().isdigit():
        try:
            cand_fmt = int(fmt_raw.strip())
            if 1 <= cand_fmt <= 7:
                fmt_val = cand_fmt
        except ValueError:
            pass

    # v14: template_id (1-10) — required for new responses, but tolerate
    # absence (model could fall back to v12 shape during migration).
    tid_raw = parsed.get("template_id")
    tid_val: int | None = None
    if isinstance(tid_raw, int) and 1 <= tid_raw <= 10:
        tid_val = tid_raw
    elif isinstance(tid_raw, str) and tid_raw.strip().isdigit():
        try:
            cand_tid = int(tid_raw.strip())
            if 1 <= cand_tid <= 10:
                tid_val = cand_tid
        except ValueError:
            pass

    # v14: template_name — should match _TEMPLATE_NAMES[template_id], but if
    # missing we backfill from id; if mismatched we keep whatever model sent
    # (Side B doesn't validate it strictly).
    tname_raw = parsed.get("template_name")
    tname_val: str | None = None
    if isinstance(tname_raw, str) and tname_raw.strip():
        tname_val = tname_raw.strip().lower()
    elif tid_val is not None:
        tname_val = _TEMPLATE_NAMES.get(tid_val)

    # v14: category — required by Side B. We don't hard-fail here; let
    # blog_publish return HTTP 422 if value is bad. We just normalise.
    cat_raw = parsed.get("category")
    cat_val: str | None = None
    if isinstance(cat_raw, str) and cat_raw.strip():
        # Normalize but don't filter — let Side B's HTTP 422 surface bad values.
        cat_val = cat_raw.strip().lower()

    # v14: tags — optional list of short strings.
    tags_raw = parsed.get("tags")
    tags_val: list[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, str) and t.strip():
                tags_val.append(t.strip()[:40])

    # v14: cover — optional path. We don't validate the path here; Side B
    # may rewrite it after blog_upload_image lands (28.04 ETA).
    cover_raw = parsed.get("cover")
    cover_val = cover_raw.strip() if isinstance(cover_raw, str) else ""

    wc_raw = parsed.get("word_count")
    wc_val = 0
    if isinstance(wc_raw, int) and wc_raw > 0:
        wc_val = wc_raw

    draft = BamboodomArticleDraft(
        title=title.strip(),
        excerpt=excerpt.strip(),
        blocks=cleaned_blocks,
        seo=seo,
        format=fmt_val,
        template_id=tid_val,
        template_name=tname_val,
        category=cat_val,
        tags=tags_val,
        cover=cover_val,
    )
    # Always override with our own count — models underreport.
    draft.word_count = wc_val  # temporary (model's own) — overwritten later
    return draft


# ---------------------------------------------------------------------------
# Word-count helpers (v5)
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[\w\-]+", flags=re.UNICODE)


def _v14_quality_checklist(
    draft: BamboodomArticleDraft,
    valid_article_codes: frozenset[str] | None = None,
) -> list[str]:
    """Return human-readable failures for v14 quality requirements.

    Empty list = all checks passed (publish-ready). Each item is a bullet
    fed back to the model verbatim, so the model knows exactly what to fix.
    """
    failures: list[str] = []

    # 1. template_id ∈ {1..10}
    if draft.template_id is None or draft.template_id not in _TEMPLATE_NAMES:
        failures.append(
            "Поле `template_id` отсутствует или не из диапазона 1-10. "
            "Выбери шаблон по правилам блока 10_ШАБЛОНОВ_СТАТЕЙ и поставь "
            "число в `template_id`, плюс соответствующее имя в `template_name`."
        )

    # 2. category
    if not draft.category or draft.category not in _ALLOWED_CATEGORIES:
        failures.append(
            "Поле `category` отсутствует или не из набора "
            "{wpc, flex, reiki, prof, spc, magnez, lighting}. "
            "Поставь категорию материала статьи."
        )

    # 3. img-block with slot=hero present
    img_blocks = [b for b in draft.blocks if isinstance(b, dict) and b.get("type") == "img"]
    img_slots = [str(b.get("slot") or "") for b in img_blocks]
    has_hero = "hero" in img_slots
    if not has_hero:
        failures.append(
            'В массиве `blocks` нет img-блока со `slot:"hero"`. '
            "Добавь после первых 1-3 текстовых блоков "
            '`{"type":"img","slot":"hero","src":"","alt":"...BamBooDom","ratio":"21:9"}`.'
        )

    # 4. min img-blocks per template
    if draft.template_id in _TEMPLATE_MIN_IMG:
        min_imgs = _TEMPLATE_MIN_IMG[draft.template_id]
        if len(img_blocks) < min_imgs:
            failures.append(
                f"Для template_id={draft.template_id} "
                f"({_TEMPLATE_NAMES.get(draft.template_id, '?')}) "
                f"минимум {min_imgs} img-блоков, у тебя {len(img_blocks)}. "
                "Добавь недостающие img-блоки по схеме шаблона."
            )

    # 5. word count >= 1500
    if draft.word_count < _MIN_WORDS_HARD:
        failures.append(
            f"Слов в текстовых блоках всего {draft.word_count}, нужен минимум "
            f"{_MIN_WORDS_HARD}. Расширь короткие h2-разделы фактическим "
            "содержанием из KNOWLEDGE_BASE — без воды."
        )

    # 6. >= 2 product-blocks
    product_blocks = [b for b in draft.blocks if isinstance(b, dict) and b.get("type") == "product"]
    if len(product_blocks) < 2:
        failures.append(
            f"Product-блоков всего {len(product_blocks)}, нужно минимум 2 "
            "(каждый с подводкой по RULE 2). Возьми артикулы из ARTICLES_CATALOG."
        )

    # 7. valid article codes
    if valid_article_codes is not None:
        bad_codes: list[str] = []
        for blk in product_blocks:
            code = str(blk.get("article", "")).strip()
            if code and code not in valid_article_codes:
                bad_codes.append(code)
        if bad_codes:
            failures.append(
                "В product-блоках встречаются артикулы, которых нет в "
                f"ARTICLES_CATALOG: {', '.join(bad_codes[:5])}. Замени на "
                "реальные коды из каталога (RULE 5)."
            )

    # 8. trailing CTA
    has_trailing_cta = any(isinstance(b, dict) and b.get("type") == "cta" for b in draft.blocks[-3:])
    if not has_trailing_cta:
        failures.append(
            "В конце статьи нет cta-блока. Добавь "
            '`{"type":"cta","text":"...","link":"/wpc.html"}` '
            "(или /flex.html, /reiki.html, /profiles.html — по категории) "
            "последним блоком."
        )

    # 9. SEO
    if not draft.seo.get("meta_title"):
        failures.append("Поле `seo.meta_title` пустое. Поставь до 60 символов.")
    if not draft.seo.get("meta_description"):
        failures.append("Поле `seo.meta_description` пустое. Поставь до 160 символов.")

    return failures


def count_words(draft: BamboodomArticleDraft) -> int:
    """Sum words across text-bearing blocks.

    v14: includes h4, ul, ol, quote in addition to v12 set (p/h2/h3/list/callout).
    Excludes product/cta/img/table per ARTICLE_LENGTH_POLICY.md.
    """
    total = 0
    for block in draft.blocks:
        btype = block.get("type")
        if btype in ("p", "h2", "h3", "h4"):
            text = block.get("text") or ""
            total += len(_WORD_RE.findall(text))
        elif btype in ("list", "ul", "ol"):
            for item in block.get("items") or []:
                if isinstance(item, str):
                    total += len(_WORD_RE.findall(item))
        elif btype == "callout":
            total += len(_WORD_RE.findall(block.get("text") or ""))
            total += len(_WORD_RE.findall(block.get("title") or ""))
        elif btype == "quote":
            total += len(_WORD_RE.findall(block.get("text") or ""))
    return total
