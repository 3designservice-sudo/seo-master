"""Telegram notification for published designservice.group articles.

Sends a message to DESIGNSERVICE_TG_CHANNEL (user_id, @username or -100…)
via dedicated DESIGNSERVICE_TG_BOT_TOKEN if set, otherwise the main bot.

Mirrors services.announce.tg_channel.announce_article pattern but parameterised
for designservice config. Does NOT raise — logs warnings on any failure.
"""

from __future__ import annotations

import io
from typing import Any

import structlog
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile

log = structlog.get_logger()


def _escape_html(text: str) -> str:
    """Minimal HTML escape for Telegram parse_mode='HTML'."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _build_caption(
    *,
    title: str,
    url: str,
    excerpt: str = "",
    score: int = 0,
    word_count: int = 0,
    humanizer_score: float = 0.0,
) -> str:
    """Compose HTML caption — <= 1024 chars (Telegram caption limit)."""
    lines = [
        f"<b>{_escape_html(title)}</b>",
        "",
    ]
    if excerpt:
        excerpt = excerpt[:400].rstrip()
        if len(excerpt) >= 400:
            excerpt += "…"
        lines.append(_escape_html(excerpt))
        lines.append("")
    lines.append(f"📊 Качество: {score}%   ✍ Слов: {word_count}   🤖 Humanizer: {humanizer_score:.2f}")
    lines.append("")
    lines.append(f'<a href="{_escape_html(url)}">Читать на designservice.group →</a>')
    return "\n".join(lines)


async def announce_published_article(
    *,
    main_bot: Bot,
    settings: Any,
    title: str,
    url: str,
    excerpt: str = "",
    cover_url: str | None = None,
    score: int = 0,
    word_count: int = 0,
    humanizer_score: float = 0.0,
    http_client: Any = None,
) -> bool:
    """Send TG notification about published article. Never raises.

    Args:
        main_bot: aiogram Bot from app context (fallback if no dedicated token).
        settings: bot.config.Settings — reads designservice_tg_channel and
            designservice_tg_bot_token.
        title, url, excerpt: article metadata.
        cover_url: optional photo for send_photo; if absent → plain message.
        score, word_count, humanizer_score: quality metrics for the caption.
        http_client: optional httpx.AsyncClient for cover fetch.

    Returns:
        True on success, False on any failure (logged).
    """
    channel = settings.designservice_tg_channel
    if not channel:
        log.info("designservice.announce.skipped_no_channel")
        return False

    custom_token = ""
    try:
        custom_token = settings.designservice_tg_bot_token.get_secret_value()
    except Exception:
        pass

    # Use dedicated bot if token configured, else main bot
    bot_to_use: Bot
    own_bot = False
    if custom_token:
        bot_to_use = Bot(
            token=custom_token,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        own_bot = True
    else:
        bot_to_use = main_bot

    caption = _build_caption(
        title=title,
        url=url,
        excerpt=excerpt,
        score=score,
        word_count=word_count,
        humanizer_score=humanizer_score,
    )

    try:
        if cover_url and http_client is not None:
            try:
                r = await http_client.get(cover_url, timeout=20.0)
                if r.status_code == 200:
                    jpeg_bytes = _to_jpeg(r.content)
                    if jpeg_bytes:
                        photo = BufferedInputFile(jpeg_bytes, filename="cover.jpg")
                        await bot_to_use.send_photo(
                            chat_id=channel,
                            photo=photo,
                            caption=caption[:1024],
                            parse_mode="HTML",
                        )
                        return True
            except Exception as exc:
                log.warning("designservice.announce.cover_fetch_failed", err=str(exc))
        # Fallback: plain message
        await bot_to_use.send_message(
            chat_id=channel,
            text=caption,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        return True
    except Exception as exc:
        log.warning(
            "designservice.announce.failed",
            err=str(exc),
            channel=channel,
            using_custom_bot=own_bot,
        )
        return False
    finally:
        if own_bot:
            try:
                await bot_to_use.session.close()
            except Exception:
                pass


async def announce_blocked_article(
    *,
    main_bot: Any,
    settings: Any,
    article_id: int,
    title: str,
    score: int = 0,
    failed_checks: list[str] | None = None,
    humanizer_score: float = 0.0,
    attempts: int = 3,
) -> bool:
    """Notify GRAD that pipeline blocked an article after retry exhaustion.

    Sends short HTML message to DESIGNSERVICE_TG_CHANNEL summarizing what
    checks failed so GRAD knows whether to relax thresholds or refine prompt.
    Never raises.
    """
    channel = settings.designservice_tg_channel
    if not channel:
        log.info("designservice.announce_blocked.skipped_no_channel")
        return False

    custom_token = ""
    try:
        custom_token = settings.designservice_tg_bot_token.get_secret_value()
    except Exception:
        pass

    bot_to_use: Any
    own_bot = False
    if custom_token:
        bot_to_use = Bot(
            token=custom_token,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        own_bot = True
    else:
        bot_to_use = main_bot
        if bot_to_use is None:
            return False

    failed_str = ", ".join(failed_checks or []) or "—"
    text = (
        f"⚠ <b>Статья не опубликована</b>\n"
        f"id={article_id}: {_escape_html(title)}\n\n"
        f"📊 Score: {score}%   🤖 Humanizer: {humanizer_score:.2f}   ↻ Попыток: {attempts}\n"
        f"❌ Не прошли проверки: {_escape_html(failed_str)}\n\n"
        f"Статья помечена <code>blocked</code> в roadmap. "
        f"При следующем запуске pipeline возьмёт другую статью на сегодня."
    )

    try:
        await bot_to_use.send_message(
            chat_id=channel,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        log.warning("designservice.announce_blocked.failed", err=str(exc), channel=channel)
        return False
    finally:
        if own_bot:
            try:
                await bot_to_use.session.close()
            except Exception:
                pass


def _to_jpeg(raw_bytes: bytes) -> bytes | None:
    """Convert any image to JPEG for Telegram (WebP rejected via send_photo URL)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception as exc:
        log.warning("designservice.announce.jpeg_failed", err=str(exc))
        return None
