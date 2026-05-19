"""QStash webhook handler — full designservice publishing pipeline.

Triggered 3x daily by QStash schedule:
    cron 'CRON_TZ=Europe/Moscow 0 9,13,18 * * *'
    POST /api/designservice/publish

Pipeline (each invocation publishes ONE article):
    1. HMAC + Redis-NX idempotency lock (TTL 5 min on Upstash-Message-Id)
    2. DesignserviceClient.get_planned(limit=1, sort='freq')
    3. mark_status(id, 'writing')
    4. DesignserviceArticleService.generate_and_validate (3 attempts max)
       - if not result.ok → mark_blocked(id, reason) + return JSON with status
    5. generate_and_publish_cover (Gemini → WebP → upload)
    6. render_article (3 ld+json + canonical + h1 SSR)
    7. publish_html → file at /blog/{slug}/index.html
    8. mark_published(id, url, date, word_count, humanizer_score)
    9. YandexWebmasterClient.add_to_recrawl(url) with DS site config
    10. announce_published_article → TG notification
    11. return JSON status to QStash for monitoring
"""

from __future__ import annotations

from datetime import datetime

import httpx
import structlog
from aiohttp import web

from api import require_qstash_signature
from integrations.designservice import (
    DesignserviceAPIError,
    DesignserviceArticleNotFound,
    DesignserviceClient,
)
from integrations.openrouter_image.client import OpenRouterImageClient
from integrations.yandex_webmaster import (
    YandexWebmasterClient,
    YandexWebmasterError,
)
from services.ai.designservice import DesignserviceArticleService
from services.announce.designservice_tg import announce_published_article
from services.designservice_images import generate_and_publish_cover
from services.seo import humanize_html, render_article

log = structlog.get_logger()

_LOCK_TTL_SECONDS = 300  # 5 minutes


@require_qstash_signature
async def designservice_publish_handler(request: web.Request) -> web.Response:
    """Full pipeline for one article publication. Idempotent per Upstash-Message-Id."""
    redis = request.app["redis"]
    settings = request.app["settings"]
    main_bot = request.app.get("bot")
    msg_id = request.get("qstash_msg_id", "") or "no-msg-id"

    # 1. Redis idempotency lock
    lock_key = f"designservice:publish_lock:{msg_id}"
    try:
        acquired = await redis.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SECONDS)
    except Exception as exc:
        log.error("designservice.publish.lock_redis_failed", err=str(exc))
        return web.json_response({"status": "error", "stage": "lock", "error": str(exc)}, status=500)
    if not acquired:
        log.info("designservice.publish.duplicate", msg_id=msg_id)
        return web.json_response({"status": "duplicate", "msg_id": msg_id})

    # 2. Build clients
    today_iso = datetime.now().strftime("%Y-%m-%d")
    async with httpx.AsyncClient() as http_client:
        ds_client = DesignserviceClient(http_client=http_client)

        try:
            planned = await ds_client.get_planned(date="today", limit=1, sort="freq")
        except DesignserviceAPIError as exc:
            log.error("designservice.publish.get_planned_failed", err=str(exc))
            return web.json_response(
                {"status": "error", "stage": "get_planned", "error": str(exc)},
                status=500,
            )

        if planned.count == 0:
            log.info("designservice.publish.nothing_planned", date=today_iso)
            return web.json_response({"status": "skipped", "reason": "no planned articles today"})

        article = planned.articles[0]
        log.info(
            "designservice.publish.start",
            article_id=article.id,
            h1=article.h1,
            kw_primary=article.kw_primary,
        )

        # 3. Mark writing
        try:
            await ds_client.mark_status(article.id, "writing")
        except DesignserviceAPIError as exc:
            log.warning("designservice.publish.mark_writing_failed", err=str(exc))

        # 4. Generate + validate (with retry loop)
        try:
            article_service = DesignserviceArticleService(
                http_client=http_client,
                openrouter_api_key=settings.openrouter_api_key.get_secret_value(),
            )
            draft, result = await article_service.generate_and_validate(
                article, current_date_iso=today_iso
            )
        except Exception as exc:
            log.exception("designservice.publish.generate_failed", err=str(exc))
            try:
                await ds_client.mark_blocked(article.id, f"generation_error: {exc}")
            except DesignserviceAPIError:
                pass
            return web.json_response(
                {"status": "error", "stage": "generate", "article_id": article.id, "error": str(exc)},
                status=500,
            )

        if not result.ok:
            failed_names = [c.name for c in result.failed]
            reason = (
                f"validation_failed_after_{draft.attempts}_attempts: "
                + ", ".join(failed_names)
            )
            log.warning(
                "designservice.publish.validation_blocked",
                article_id=article.id,
                score=result.score,
                failed=failed_names,
                humanizer=result.humanizer_score,
            )
            try:
                await ds_client.mark_blocked(article.id, reason[:500])
            except DesignserviceAPIError:
                pass
            # TG notification — let GRAD know which checks failed
            if main_bot is not None:
                try:
                    from services.announce.designservice_tg import announce_blocked_article
                    await announce_blocked_article(
                        main_bot=main_bot,
                        settings=settings,
                        article_id=article.id,
                        title=article.h1,
                        score=result.score,
                        failed_checks=failed_names,
                        humanizer_score=result.humanizer_score,
                        attempts=draft.attempts,
                    )
                except Exception as exc:
                    log.warning("designservice.publish.announce_blocked_failed", err=str(exc))
            return web.json_response(
                {
                    "status": "blocked",
                    "article_id": article.id,
                    "score": result.score,
                    "failed_checks": failed_names,
                    "humanizer_score": result.humanizer_score,
                }
            )

        # 5. Generate cover image (also init openrouter_img for inline use)
        cover_url: str | None = None
        openrouter_img: OpenRouterImageClient | None = None
        if settings.designservice_images_enabled:
            try:
                openrouter_img = OpenRouterImageClient(
                    api_key=settings.openrouter_api_key.get_secret_value()
                )
                cover_url = await generate_and_publish_cover(
                    article,
                    openrouter_image_client=openrouter_img,
                    designservice_client=ds_client,
                    http_client=http_client,
                    base_url=settings.designservice_base_url,
                )
            except Exception as exc:
                log.warning("designservice.publish.cover_failed", err=str(exc))

        # 6a. Generate 6 inline images and inject between h2 sections
        body_html_with_images = draft.body_html
        if settings.designservice_images_enabled and openrouter_img is not None:
            try:
                from services.designservice_images import enrich_with_inline_images
                body_html_with_images = await enrich_with_inline_images(
                    article,
                    draft.body_html,
                    openrouter_image_client=openrouter_img,
                    designservice_client=ds_client,
                    http_client=http_client,
                    max_images=6,
                    base_url=settings.designservice_base_url,
                )
            except Exception as exc:
                log.warning("designservice.publish.inline_images_failed", err=str(exc))

        # 6b. Fetch 3 most recent published articles for «Читать дальше» block
        recent_articles: list[dict] = []
        try:
            # Fetch all to find published; bot_api has no dedicated 'list published'
            # endpoint, so we use stats + iterating get_article on published candidates.
            # In practice we use a small probe — newest article IDs near current.
            stats_resp = await ds_client.stats()
            # The roadmap doesn't return list of published from stats —
            # fallback: scan IDs from current-10 to current+30 looking for published
            current_id = article.id
            seen_pub: list[dict] = []
            candidate_ids = list(range(max(1, current_id - 30), current_id + 30))
            # Exclude current
            candidate_ids = [i for i in candidate_ids if i != current_id]
            for cid in candidate_ids:
                if len(seen_pub) >= 3:
                    break
                try:
                    a = await ds_client.get_article(cid)
                    if a.status == "published" and a.published_url:
                        cover = f"{settings.designservice_base_url.rstrip('/')}/blog/{a.target_url.strip('/').removeprefix('blog/').rstrip('/')}/cover.webp"
                        seen_pub.append({
                            "h1": a.h1,
                            "published_url": a.published_url,
                            "cover_url": cover,
                            "published_date": a.published_date,
                        })
                except Exception:
                    continue
            recent_articles = seen_pub
        except Exception as exc:
            log.warning("designservice.publish.recent_fetch_failed", err=str(exc))

        # 6c. Humanizer pass + render
        body_html, hum_stats = humanize_html(body_html_with_images, max_em_dash_per_1k=8)
        full_html = render_article(
            article,
            body_html,
            cover_url=cover_url,
            date_iso=f"{today_iso}T10:00:00+03:00",
            base_url=settings.designservice_base_url,
            recent_articles=recent_articles,
        )

        # 7. Publish HTML
        slug = article.target_url.strip("/").removeprefix("blog/").rstrip("/")
        try:
            pub_result = await ds_client.publish_html(slug, full_html)
        except DesignserviceAPIError as exc:
            log.exception("designservice.publish.publish_html_failed", err=str(exc))
            return web.json_response(
                {"status": "error", "stage": "publish", "article_id": article.id, "error": str(exc)},
                status=500,
            )

        published_url = f"{settings.designservice_base_url.rstrip('/')}/blog/{slug}/"
        log.info(
            "designservice.publish.html_done",
            article_id=article.id,
            url=published_url,
            bytes=pub_result.bytes_written,
        )

        # 8. Mark published in roadmap
        try:
            await ds_client.mark_published(
                article.id,
                published_url=published_url,
                published_date=today_iso,
                word_count=draft.word_count,
                humanizer_score=result.humanizer_score,
            )
        except DesignserviceAPIError as exc:
            log.warning("designservice.publish.mark_published_failed", err=str(exc))

        # 9. Yandex Webmaster recrawl
        recrawl_ok = False
        try:
            wm_token = (
                settings.yandex_webmaster_token_ds.get_secret_value()
                or settings.yandex_webmaster_token.get_secret_value()
            )
            if wm_token:
                wm_client = YandexWebmasterClient(
                    token=wm_token,
                    site_url=settings.yandex_webmaster_site_ds or settings.designservice_base_url,
                    host_id=settings.yandex_webmaster_host_id_ds,
                    http_client=http_client,
                )
                await wm_client.add_to_recrawl(published_url)
                recrawl_ok = True
                log.info("designservice.publish.recrawl_ok", url=published_url)
        except YandexWebmasterError as exc:
            log.warning("designservice.publish.recrawl_failed", err=str(exc))
        except Exception as exc:
            log.warning("designservice.publish.recrawl_unexpected", err=str(exc))

        # 10a. Update /blog.html article grid (insert new card first)
        try:
            from services.designservice_blog_index import update_blog_index
            # Reading time estimate based on body word count
            reading_minutes = max(1, round(draft.word_count / 180))
            # Russian date for the card meta
            months_ru = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
                         "июля", "августа", "сентября", "октября", "ноября", "декабря"]
            try:
                y, m, d = today_iso.split("-")
                date_ru = f"{int(d)} {months_ru[int(m)]} {y}"
            except (ValueError, IndexError):
                date_ru = today_iso
            await update_blog_index(
                article=article,
                cover_url=cover_url or f"{settings.designservice_base_url.rstrip('/')}/Logo_DS.png",
                reading_time=reading_minutes,
                date_ru=date_ru,
                http_client=http_client,
                designservice_client=ds_client,
                base_url=settings.designservice_base_url,
            )
        except Exception as exc:
            log.warning("designservice.publish.blog_index_failed", err=str(exc))

        # 10b. TG announcement
        announced = False
        if main_bot is not None:
            announced = await announce_published_article(
                main_bot=main_bot,
                settings=settings,
                title=article.h1,
                url=published_url,
                excerpt=article.meta_description,
                cover_url=cover_url,
                score=result.score,
                word_count=draft.word_count,
                humanizer_score=result.humanizer_score,
                http_client=http_client,
            )

    return web.json_response(
        {
            "status": "published",
            "article_id": article.id,
            "url": published_url,
            "score": result.score,
            "word_count": draft.word_count,
            "humanizer_score": round(result.humanizer_score, 2),
            "attempts": draft.attempts,
            "cover_published": cover_url is not None,
            "recrawl_sent": recrawl_ok,
            "tg_announced": announced,
            "model": draft.model_used,
        }
    )
