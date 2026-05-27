"""QStash webhook handler — Я.Бизнес publish from queue.

Triggered daily by QStash schedule:
    cron 'CRON_TZ=Europe/Moscow 0 11 * * *'
    POST /api/yandex_business/publish

Pipeline (each invocation publishes ONE pending article):
    1. HMAC + Redis-NX idempotency lock (TTL 5 min on Upstash-Message-Id)
    2. fetch ya_queue_pending (limit=1) from designservice.group
    3. if no pending → return JSON ok=true, skipped="empty queue"
    4. headless Playwright publish via services.publishers.yandex_business.publish
    5. if ok → mark ya_queue_mark status=submitted
       if auth fail → mark status=failed + send TG re-auth alert
       if other fail → mark status=failed + send TG error
    6. return JSON status to QStash for monitoring

DS_YABIZ_PUBLISH_v1
"""

from __future__ import annotations

import os

import httpx
import structlog
from aiohttp import web

from api import require_qstash_signature
from services.publishers.yandex_business import publish as ya_publish

log = structlog.get_logger()

DS_BASE = os.environ.get("DESIGNSERVICE_BASE_URL", "https://designservice.group")
BOT_KEY = os.environ.get("DESIGNSERVICE_BOT_API_KEY", "")
TG_BOT_TOKEN = os.environ.get("DESIGNSERVICE_TG_BOT_TOKEN", "")
TG_CHANNEL = os.environ.get("DESIGNSERVICE_TG_CHANNEL", "")


async def _fetch_pending(limit: int = 1) -> list[dict]:
    url = f"{DS_BASE.rstrip('/')}/_bot_api.php?action=ya_queue_pending&k={BOT_KEY}&limit={limit}"
    async with httpx.AsyncClient(timeout=15) as cl:
        r = await cl.get(url)
        r.raise_for_status()
        j = r.json()
    return j.get("items", [])


async def _mark_status(article_id: int, status: str, note: str) -> None:
    url = (
        f"{DS_BASE.rstrip('/')}/_bot_api.php?action=ya_queue_mark&article_id={article_id}"
        f"&k={BOT_KEY}"
    )
    async with httpx.AsyncClient(timeout=15) as cl:
        try:
            await cl.post(url, json={"status": status, "note": note})
        except Exception as exc:
            log.warning("yabiz.mark_status.failed", article_id=article_id, err=str(exc))


async def _tg_notify(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHANNEL:
        log.info("yabiz.tg.skipped", reason="no TG_BOT_TOKEN or TG_CHANNEL")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as cl:
        try:
            await cl.post(
                url,
                json={
                    "chat_id": TG_CHANNEL,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
        except Exception as exc:
            log.warning("yabiz.tg.failed", err=str(exc))


@require_qstash_signature
async def yandex_business_publish_handler(request: web.Request) -> web.Response:
    """Publish one pending article from Я.Бизнес queue."""
    # quick env check
    if not BOT_KEY:
        return web.json_response(
            {"ok": False, "error": "DESIGNSERVICE_BOT_API_KEY not set"}, status=500
        )

    # 1. fetch pending
    try:
        pending = await _fetch_pending(limit=1)
    except Exception as exc:
        log.exception("yabiz.pending.fetch_failed")
        return web.json_response(
            {"ok": False, "error": f"fetch_pending: {exc}"}, status=500
        )

    if not pending:
        log.info("yabiz.pending.empty")
        return web.json_response({"ok": True, "skipped": "empty queue"})

    entry = pending[0]
    article_id = int(entry["article_id"])
    slug = entry["slug"]
    h1 = entry.get("h1", "")
    log.info("yabiz.publish.start", article_id=article_id, slug=slug, h1=h1)

    # 2. publish via Playwright
    result = await ya_publish(article_id=article_id, slug=slug, h1=h1)

    if result.get("ok"):
        note = (
            f"Опубликовано auto-publisher (Playwright) — "
            f"text={result.get('text_len')}ch cover={result.get('cover_bytes')}b "
            f"took={result.get('took_ms')}ms"
        )
        await _mark_status(article_id, "submitted", note)
        await _tg_notify(
            f"✅ Я.Бизнес: <b>id={article_id}</b> {h1}\n"
            f"→ ушло на модерацию (до 7 дней)"
        )
        log.info("yabiz.publish.ok", article_id=article_id, **result)
        return web.json_response(
            {"ok": True, "article_id": article_id, "h1": h1, **result}
        )

    # FAILED
    err = result.get("error", "unknown")
    if result.get("action_required") == "refresh_yandex_session":
        # session expired — DO NOT mark as failed (will retry tomorrow with fresh cookies)
        await _tg_notify(
            f"⚠️ Я.Бизнес: <b>сессия истекла</b>\n"
            f"id={article_id} {h1}\n"
            f"Перелогиньтесь в yandex.ru → Cookie-Editor → Export → пришлите в чат, "
            f"я обновлю /data/yandex_session.json. После этого следующий QStash-запуск опубликует."
        )
        log.warning("yabiz.publish.session_expired", article_id=article_id)
        return web.json_response(
            {"ok": False, "article_id": article_id, "action_required": "refresh_session", **result},
            status=200,  # 200 to QStash so it doesn't retry; we'll wait for manual fix
        )

    # other error → mark failed + notify
    await _mark_status(article_id, "failed", f"auto-publisher: {err}")
    await _tg_notify(
        f"❌ Я.Бизнес: <b>id={article_id}</b> {h1}\n"
        f"Ошибка: {err}\n"
        f"Помечено failed в очереди. Можно вернуть в pending через ya_queue_mark."
    )
    log.warning("yabiz.publish.failed", article_id=article_id, err=err)
    return web.json_response(
        {"ok": False, "article_id": article_id, **result},
        status=200,  # 200 to QStash; we already handled the failure
    )
