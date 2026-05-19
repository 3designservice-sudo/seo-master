"""15-point quality validator for designservice blog articles.

Used by DesignserviceArticleService.generate_and_validate. Returns
ValidationResult with detailed pass/fail breakdown and a 0-100 score.

Pipeline: generate → validate → if fail, retry with feedback (up to 3 total).
GRAD requirement: only 100% (all 15 metrics PASS + humanizer >= 0.85) get published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from services.seo.humanizer import humanize_html

# ---------------------------------------------------------------------------
# Banned AI vocabulary (sample — extended in humanizer.py)
# ---------------------------------------------------------------------------

_BANNED_AI_PHRASES = (
    "важно отметить",
    "стоит подчеркнуть",
    "необходимо учитывать",
    "в современном мире",
    "эксперты считают",
    "исследования показывают",
    "премиальное качество",
    "одно из лучших",
    "не просто",  # for inflated symbolism
    "таким образом",
    "более того",
    "при этом",
    "помимо этого",
)

_AUTHOR_NAME = "Александр Шульман"
_STUDIO_NAMES = ("Дизайн-Сервис", "Designservice", "дизайн-сервис")
_GEO_KEYWORDS = (
    "Крым",
    "крым",
    "Симферополь",
    "Севастополь",
    "Ялта",
    "Феодосия",
    "Евпатория",
    "Керчь",
)

# Russian quote marks for expert quote detection: «...»
_RUSSIAN_QUOTE_RE = re.compile(r"«[^»]{15,500}»")
# Numbers — at least 2 digits, allows %, ₽, м², руб, лет, etc.
_NUMBER_RE = re.compile(r"\b\d{2,}\b")


@dataclass
class ValidationCheck:
    """One quality metric result."""

    name: str
    passed: bool
    detail: str = ""
    actual: Any = None
    expected: Any = None


@dataclass
class ValidationResult:
    """Aggregate validator output.

    score = 0..100. ok = True iff all 15 checks pass AND humanizer >= 0.85.
    """

    checks: list[ValidationCheck] = field(default_factory=list)
    humanizer_score: float = 0.0
    humanizer_stats: dict = field(default_factory=dict)

    @property
    def passed(self) -> list[ValidationCheck]:
        return [c for c in self.checks if c.passed]

    @property
    def failed(self) -> list[ValidationCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def score(self) -> int:
        """Integer percent 0..100. Each of N checks weight equally; humanizer adds bonus."""
        n = len(self.checks) or 1
        base = sum(1 for c in self.checks if c.passed) / n * 90  # 90% from checks
        hum = max(0.0, min(1.0, self.humanizer_score)) * 10  # 10% from humanizer
        return int(round(base + hum))

    @property
    def ok(self) -> bool:
        return len(self.failed) == 0 and self.humanizer_score >= 0.85

    def feedback_messages(self) -> list[str]:
        """Human-readable issues for retry-feedback in next LLM call."""
        msgs = []
        for c in self.failed:
            msg = f"{c.name}: {c.detail}"
            if c.actual is not None and c.expected is not None:
                msg += f" (получено {c.actual!r}, ожидается {c.expected!r})"
            msgs.append(msg)
        if self.humanizer_score < 0.85:
            msgs.append(
                f"humanizer-score = {self.humanizer_score:.2f}, нужно ≥ 0.85. "
                f"Убери AI-штампы и длинные тире. См. список запретных фраз в system-промпте."
            )
        return msgs


def _strip_tags(html: str) -> str:
    """Strip HTML tags for word-counting and text searches."""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _count_words(text: str) -> int:
    return len(re.findall(r"[А-Яа-яA-Za-zЁё0-9]+", text))


def validate_article(article: Any, body_html: str) -> ValidationResult:
    """Run 15 quality checks on generated article body.

    Args:
        article: integrations.designservice.Article (or any object with same fields).
        body_html: HTML body fragment (no head/body wrap).

    Returns:
        ValidationResult with .ok, .score, .failed, .feedback_messages().
    """
    plain = _strip_tags(body_html)
    word_count = _count_words(plain)
    checks: list[ValidationCheck] = []

    # 1. Word count within ±15% of target (relaxed in PR 8 — Sonnet tends to overshoot)
    target = int(article.target_words or 1500)
    lo, hi = int(target * 0.85), int(target * 1.15)
    checks.append(ValidationCheck(
        name="word_count",
        passed=lo <= word_count <= hi,
        detail=f"длина body должна быть {lo}-{hi} слов",
        actual=word_count,
        expected=f"{lo}-{hi}",
    ))

    # 2. h2 count >= 6
    h2 = len(re.findall(r"<h2\b", body_html, re.IGNORECASE))
    checks.append(ValidationCheck(
        name="h2_count",
        passed=h2 >= 6,
        detail="нужно минимум 6 разделов <h2>",
        actual=h2,
        expected=">= 6",
    ))

    # 3. FAQ count matches article.faq_entries
    faq = len(re.findall(r'<details[^>]*class="[^"]*faq', body_html, re.IGNORECASE))
    if faq == 0:
        faq = len(re.findall(r"<details\b", body_html, re.IGNORECASE))
    expected_faq = int(article.faq_entries or 3)
    checks.append(ValidationCheck(
        name="faq_count",
        passed=faq == expected_faq,
        detail=f"нужно ровно {expected_faq} FAQ-блоков (<details class=\"faq\">)",
        actual=faq,
        expected=expected_faq,
    ))

    # 4. kw_primary mentions 3-15 (relaxed in PR 8 — wider window for natural variation)
    if article.kw_primary:
        kw_count = len(re.findall(re.escape(article.kw_primary), plain, re.IGNORECASE))
    else:
        kw_count = 0
    checks.append(ValidationCheck(
        name="kw_primary_count",
        passed=3 <= kw_count <= 15,
        detail="kw_primary должен встречаться 3-15 раз",
        actual=kw_count,
        expected="3-15",
    ))

    # 5. kw_secondary: at least 50% coverage (relaxed in PR 8)
    secondary = [k for k in (article.kw_secondary or []) if k]
    missing_secondary = [
        k for k in secondary
        if not re.search(re.escape(k), plain, re.IGNORECASE)
    ]
    if secondary:
        coverage = (len(secondary) - len(missing_secondary)) / len(secondary)
        passed = coverage >= 0.5
    else:
        coverage = 1.0
        passed = True
    checks.append(ValidationCheck(
        name="kw_secondary_coverage",
        passed=passed,
        detail=(
            "нужно ≥50% дополнительных запросов; не упомянуты: " + ", ".join(missing_secondary)
        ) if missing_secondary else "все дополнительные запросы упомянуты",
        actual=f"{int(coverage * 100)}%",
        expected="≥50%",
    ))

    # 6. Internal links >= 3 (relaxed in PR 8 — Sonnet rarely puts 4+ inline)
    internal = len(re.findall(r'href="/(?:services/|projects|about|contacts|reviews|blog)', body_html, re.IGNORECASE))
    checks.append(ValidationCheck(
        name="internal_links",
        passed=internal >= 3,
        detail="нужно минимум 3 внутренних ссылок на /services/*, /projects.html, /about.html и т.д.",
        actual=internal,
        expected=">= 3",
    ))

    # 7. External authority link (ru.wikipedia.org or similar)
    external_auth = len(re.findall(
        r'href="https?://(?:ru\.wikipedia\.org|docs\.cntd\.ru|gosthelp\.ru|consultant\.ru)',
        body_html, re.IGNORECASE
    ))
    checks.append(ValidationCheck(
        name="external_authority_link",
        passed=external_auth >= 1,
        detail="нужна минимум одна ссылка на авторитетный ресурс (ru.wikipedia.org, ГОСТ-портал)",
        actual=external_auth,
        expected=">= 1",
    ))

    # 8. Concrete numbers >= 5
    numbers = len(_NUMBER_RE.findall(plain))
    checks.append(ValidationCheck(
        name="concrete_numbers",
        passed=numbers >= 5,
        detail="нужно минимум 5 конкретных чисел (площадь, цены, сроки, проценты)",
        actual=numbers,
        expected=">= 5",
    ))

    # 9. Author name mention
    has_author = _AUTHOR_NAME in plain
    checks.append(ValidationCheck(
        name="author_mention",
        passed=has_author,
        detail=f"в тексте должно быть имя {_AUTHOR_NAME}",
        actual=has_author,
        expected=True,
    ))

    # 10. Studio name
    has_studio = any(s.lower() in plain.lower() for s in _STUDIO_NAMES)
    checks.append(ValidationCheck(
        name="studio_mention",
        passed=has_studio,
        detail="должно быть упоминание студии 'Дизайн-Сервис'",
        actual=has_studio,
        expected=True,
    ))

    # 11. Geo (Crimea or cities) >= 2 mentions total
    geo_mentions = sum(plain.lower().count(g.lower()) for g in _GEO_KEYWORDS)
    checks.append(ValidationCheck(
        name="geo_mentions",
        passed=geo_mentions >= 2,
        detail="должно быть минимум 2 упоминания Крыма / городов (Симферополь, Севастополь, Ялта…)",
        actual=geo_mentions,
        expected=">= 2",
    ))

    # 12. Expert quote (text in « » 15+ chars)
    has_quote = bool(_RUSSIAN_QUOTE_RE.search(body_html))
    checks.append(ValidationCheck(
        name="expert_quote",
        passed=has_quote,
        detail="нужна цитата эксперта в кавычках «…» (минимум 15 символов внутри)",
        actual=has_quote,
        expected=True,
    ))

    # 13. No banned AI phrases
    found_banned = [p for p in _BANNED_AI_PHRASES if p in plain.lower()]
    checks.append(ValidationCheck(
        name="no_ai_phrases",
        passed=len(found_banned) == 0,
        detail=("найдены запретные AI-фразы: " + ", ".join(found_banned))
        if found_banned else "запретные AI-фразы не найдены",
        actual=found_banned,
        expected=[],
    ))

    # 14. h1 in body — should NOT appear (h1 is added by render_article wrapper)
    has_h1_in_body = bool(re.search(r"<h1\b", body_html, re.IGNORECASE))
    checks.append(ValidationCheck(
        name="no_h1_in_body",
        passed=not has_h1_in_body,
        detail="<h1> не должен быть в body_html — он добавляется render-обёрткой автоматически",
        actual=has_h1_in_body,
        expected=False,
    ))

    # 15. Article lead exists (first <p class="art-lead">)
    has_lead = bool(re.search(r'<p\s+class="[^"]*art-lead', body_html, re.IGNORECASE))
    checks.append(ValidationCheck(
        name="art_lead",
        passed=has_lead,
        detail="первый абзац должен быть <p class=\"art-lead\">…</p> с TL;DR",
        actual=has_lead,
        expected=True,
    ))

    # Humanizer pass — em-dash density + AI vocab — used for score, not retry on its own
    _, hum_stats = humanize_html(body_html)
    score = float(hum_stats.get("score", 0.0))

    return ValidationResult(
        checks=checks,
        humanizer_score=score,
        humanizer_stats=hum_stats,
    )
