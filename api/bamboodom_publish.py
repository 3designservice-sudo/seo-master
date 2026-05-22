"""QStash webhook handler — full bamboodom.ru publishing pipeline.

Аналог api/designservice_publish.py, но под bamboodom:
- контент-план живёт на САЙТЕ (data/seo/article_roadmap.json), управляется
  через blog_roadmap.php (get_next / mark_status) — Вариант A;
- генерация: services.ai.bamboodom.BamboodomArticleService.generate_and_validate
  (вход = material + keyword); публикация: draft.to_publish_payload() ->
  BamboodomClient.publish(payload);
- картинки + кросс-пост: services.bamboodom_images.article_images
  .run_background_image_pipeline — публикуем статью с ПУСТЫМИ img-src,
  затем helper генерит/заливает картинки, републишит статью (inline
  сохраняются) и сам анонсит (TG-канал + VK/Pinterest через connections).
  Это проверенный путь; вариант images-before-publish терял inline
  (Side B удалял картинки, залитые до создания статьи).

Триггерится QStash 2x/день: cron 'CRON_TZ=Europe/Moscow 0 8,17 * * *'
POST /api/bamboodom/publish   (body {} — обычный; {"announce_only_id": "<id>"} — догон соцсетей)

Регистрация роута — в bot/main.py:
    from api.bamboodom_publish import bamboodom_publish_handler
    app.router.add_post("/api/bamboodom/publish", bamboodom_publish_handler)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import structlog
from aiohttp import web

from api import require_qstash_signature
from integrations.bamboodom import BamboodomAPIError, BamboodomClient
from integrations.yandex_webmaster import YandexWebmasterClient, YandexWebmasterError
from services.ai.bamboodom import BamboodomArticleService, BamboodomGenerationError
from services.announce.social import announce_to_social
from services.bamboodom_images.article_images import run_background_image_pipeline

log = structlog.get_logger()

_LOCK_TTL_SECONDS = 300  # 5 минут
_SITE = "https://bamboodom.ru"
_ROADMAP_API = f"{_SITE}/blog_roadmap.php"

# roadmap material -> MaterialCategory генератора. Активны: wpc/flex/reiki/prof/spc.
# Убраны по решению владельца: magnez, lighting, texture, plumbing, furniture (эндпоинт их пропустит).
_MATERIAL_TO_CATEGORY: dict[str, str] = {
    "wpc": "wpc",
    "flex": "flex",
    "reiki": "reiki",
    "profiles": "prof",
    "spc_floor": "spc",
    "spc_wall": "spc",
    "bamboo": "wpc",  # бамбуковые панели публикуем как WPC-категорию
}
_MAX_SKIP_SCAN = 6  # сколько неподдерживаемых статей пропустить за прогон


# ---------- roadmap helpers (blog_roadmap.php) ----------
async def _roadmap_get(http: httpx.AsyncClient, key: str, params: dict[str, Any]) -> dict[str, Any]:
    params = {"key": key, **params}
    r = await http.get(_ROADMAP_API, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


async def _roadmap_next(http: httpx.AsyncClient, key: str, lock: bool = True) -> dict | None:
    p: dict[str, Any] = {"action": "get_next"}
    if lock:
        p["lock"] = "1"
    data = await _roadmap_get(http, key, p)
    return data.get("article") if data.get("ok") else None


async def _roadmap_get_by_id(http: httpx.AsyncClient, key: str, aid: str) -> dict | None:
    data = await _roadmap_get(http, key, {"action": "get", "id": aid})
    return data.get("article") if data.get("ok") else None


async def _roadmap_mark(http: httpx.AsyncClient, key: str, aid: str, status: str,
                        slug: str | None = None) -> None:
    p: dict[str, Any] = {"action": "mark_status", "id": aid, "status": status}
    if slug:
        p["slug"] = slug
    try:
        await _roadmap_get(http, key, p)
    except Exception as exc:  # mark — best-effort
        log.warning("bamboodom.publish.mark_failed", id=aid, status=status, err=str(exc))


def _hero_src_from_blocks(blocks: list[dict], site: str = _SITE) -> str:
    """hero (или первый img со src) -> абсолютный URL для cover/announce."""
    src = ""
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == "img" and b.get("slot") == "hero" and b.get("src"):
            src = str(b["src"]); break
    if not src:
        for b in blocks or []:
            if isinstance(b, dict) and b.get("type") == "img" and b.get("src"):
                src = str(b["src"]); break
    if src.startswith("/"):
        src = site + src
    elif src and not src.startswith(("http://", "https://")):
        src = f"{site}/" + src.lstrip("/")
    return src


# ---------- handler ----------
@require_qstash_signature
async def bamboodom_publish_handler(request: web.Request) -> web.Response:
    """Полный пайплайн публикации одной статьи. Идемпотентно per Upstash-Message-Id."""
    redis = request.app["redis"]
    settings = request.app["settings"]
    db = request.app.get("db")
    msg_id = request.get("qstash_msg_id", "") or "no-msg-id"
    blog_key = settings.bamboodom_blog_key.get_secret_value()

    # 1. Redis idempotency lock
    lock_key = f"bamboodom:publish_lock:{msg_id}"
    try:
        acquired = await redis.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SECONDS)
    except Exception as exc:
        log.error("bamboodom.publish.lock_redis_failed", err=str(exc))
        return web.json_response({"status": "error", "stage": "lock", "error": str(exc)}, status=500)
    if not acquired:
        log.info("bamboodom.publish.duplicate", msg_id=msg_id)
        return web.json_response({"status": "duplicate", "msg_id": msg_id})

    today_iso = datetime.now().strftime("%Y-%m-%d")
    ann_pid = getattr(settings, "bamboodom_announce_project_id", 0) or 0

    async with httpx.AsyncClient() as http_client:
        bd_client = BamboodomClient(http_client=http_client)

        # ── ANNOUNCE-ONLY режим ──
        _vb = request.get("verified_body") or {}
        _announce_id = _vb.get("announce_only_id")
        if _announce_id:
            art = await _roadmap_get_by_id(http_client, blog_key, str(_announce_id))
            if not art:
                return web.json_response({"status": "error", "stage": "announce_only_get",
                                          "error": "article_not_found"}, status=404)
            slug = art.get("published_slug") or art["slug"]
            pub_url = f"{_SITE}/article.html?slug={slug}"
            cover = art.get("cover") or ""
            pins = [{"url": cover, "alt": art["h1"]}] if cover else []
            if db is None or not ann_pid:
                return web.json_response({"status": "error", "stage": "announce_only",
                                          "error": "no project_id or db"}, status=500)
            social = await announce_to_social(
                db=db, http_client=http_client, settings=settings,
                title=art["h1"], url=pub_url, excerpt=art.get("meta_description", ""),
                image_url=cover, project_id_override=ann_pid, pinterest_images=pins,
            )
            log.info("bamboodom.publish.announce_only.results", article_id=_announce_id, results=social)
            return web.json_response({"status": "announced", "article_id": _announce_id,
                                      "url": pub_url, "social_results": social})

        # 2. Взять следующую статью; пропустить неподдерживаемые категории
        article = None
        category = None
        for _ in range(_MAX_SKIP_SCAN):
            article = await _roadmap_next(http_client, blog_key, lock=True)
            if not article:
                return web.json_response({"status": "skipped", "reason": "roadmap empty/exhausted"})
            category = _MATERIAL_TO_CATEGORY.get(article.get("material", ""))
            if category:
                break
            log.info("bamboodom.publish.skip_unsupported_material",
                     id=article["id"], material=article.get("material"))
            await _roadmap_mark(http_client, blog_key, article["id"], "skipped")
            article = None
        if not article or not category:
            return web.json_response({"status": "skipped", "reason": "no supported material in scan window"})

        aid = article["id"]
        keyword = article.get("kw_primary") or article["h1"]
        slug = article["slug"]
        log.info("bamboodom.publish.start", id=aid, material=article["material"],
                 category=category, keyword=keyword)

        # 3. Генерация + валидация (контекст генератор тянет сам)
        try:
            service = BamboodomArticleService(
                http_client=http_client,
                openrouter_api_key=settings.openrouter_api_key.get_secret_value(),
                bamboodom_client=bd_client,
            )
            draft, result = await service.generate_and_validate(
                material=category, keyword=keyword, current_date_iso=today_iso,
            )
        except BamboodomGenerationError as exc:
            log.exception("bamboodom.publish.generate_failed", id=aid, err=str(exc))
            await _roadmap_mark(http_client, blog_key, aid, "planned")
            return web.json_response({"status": "error", "stage": "generate", "id": aid,
                                      "error": str(exc)}, status=500)
        except Exception as exc:
            log.exception("bamboodom.publish.generate_unexpected", id=aid, err=str(exc))
            await _roadmap_mark(http_client, blog_key, aid, "planned")
            return web.json_response({"status": "error", "stage": "generate", "id": aid,
                                      "error": str(exc)}, status=500)

        if not getattr(result, "ok", False):
            failed = [getattr(c, "name", str(c)) for c in getattr(result, "failed", [])]
            log.warning("bamboodom.publish.validation_blocked", id=aid, failed=failed)
            await _roadmap_mark(http_client, blog_key, aid, "blocked")
            return web.json_response({"status": "blocked", "id": aid, "failed_checks": failed})

        # 4. Публикация (production) — img-блоки идут с ПУСТЫМ src.
        #    draft -> payload; фиксируем наш slug из роадмапа.
        payload = draft.to_publish_payload()
        payload["slug"] = slug
        try:
            pub = await bd_client.publish(payload, sandbox=False)
        except BamboodomAPIError as exc:
            log.exception("bamboodom.publish.publish_failed", id=aid, err=str(exc))
            await _roadmap_mark(http_client, blog_key, aid, "planned")
            return web.json_response({"status": "error", "stage": "publish", "id": aid,
                                      "error": str(exc)}, status=500)
        published_slug = getattr(pub, "slug", slug) or slug
        published_url = f"{_SITE}/article.html?slug={published_slug}"

        # 5. Картинки + кросс-пост через проверенный helper.
        #    helper сам: генерит картинки -> заливает -> републишит статью
        #    (inline СОХРАНЯЮТСЯ) -> анонс (TG-канал через announce_article +
        #    VK/Pinterest через announce_to_social). announce_to_social внутри
        #    пропускает connection-based TG, т.к. задан dedicated bamboodom-канал
        #    (без дубля). Ждём завершения (republish + cover нужны до анонса).
        try:
            await run_background_image_pipeline(
                slug=published_slug,
                blocks=payload["blocks"],
                payload=payload,
                http_client=http_client,
                settings=settings,
                sandbox=False,
                announce_bot=request.app.get("bot"),
                announce_db=db,
                announce_title=draft.title,
                announce_url=published_url,
                announce_excerpt=draft.excerpt,
            )
        except Exception as exc:
            log.warning("bamboodom.publish.img_pipeline_failed", id=aid, err=str(exc))
        # helper мутирует payload["blocks"] in-place — теперь там реальные src.
        cover_url = _hero_src_from_blocks(payload["blocks"])

        # 6. sitemap + переобход Яндекс
        try:
            await bd_client.regenerate_sitemap()
        except Exception as exc:
            log.warning("bamboodom.publish.sitemap_failed", err=str(exc))
        recrawl_ok = False
        try:
            wm_token_obj = getattr(settings, "yandex_webmaster_token", None)
            wm_token = wm_token_obj.get_secret_value() if wm_token_obj else ""
            wm_site = getattr(settings, "yandex_webmaster_site", "") or _SITE
            if wm_token:
                wm = YandexWebmasterClient(
                    token=wm_token, site_url=wm_site,
                    host_id=getattr(settings, "yandex_webmaster_host_id", None),
                    http_client=http_client,
                )
                await wm.add_to_recrawl(published_url)
                recrawl_ok = True
        except YandexWebmasterError as exc:
            log.warning("bamboodom.publish.recrawl_failed", err=str(exc))
        except Exception as exc:
            log.warning("bamboodom.publish.recrawl_unexpected", err=str(exc))

        # 7. Отметить published в роадмапе
        await _roadmap_mark(http_client, blog_key, aid, "published", slug=published_slug)

        return web.json_response({
            "status": "published", "id": aid, "url": published_url,
            "category": category, "cover_published": bool(cover_url),
            "recrawl_sent": recrawl_ok,
        })
