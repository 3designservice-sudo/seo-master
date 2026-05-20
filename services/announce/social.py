"""Анонс новой статьи через существующие publishers (4L, упрощённая v2).

Использует services/publishers/{vk,pinterest,telegram}.py через PublishRequest
+ ConnectionsRepository — те самые подключения, что Александр настраивает в
основном UI бота через раздел «Подключения».

Нужна одна env-переменная BAMBOODOM_ANNOUNCE_PROJECT_ID — id project'а в БД,
куда привязаны три connection'а (vk, pinterest, telegram). Когда юзер
подключит соцсети через UI бота, проект-id ставится в Railway env.

Если переменная не задана или connection отсутствует — анонс на
платформу пропускается (graceful degrade).
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

import httpx
import structlog

from db.credential_manager import CredentialManager
from db.repositories.connections import ConnectionsRepository
from services.publishers import PublishRequest
from services.publishers.factory import create_publisher, make_token_refresh_cb

log = structlog.get_logger()

# Соответствие наша роль → платформа в БД
PLATFORMS = ["telegram", "vk", "pinterest"]


async def _fetch_image_bytes(http_client: httpx.AsyncClient, url: str) -> bytes | None:
    """Скачивает картинку для прикрепления к Pinterest pin."""
    if not url:
        return None
    try:
        resp = await http_client.get(url, timeout=20.0)
        if resp.status_code != 200:
            return None
        if len(resp.content) > 10 * 1024 * 1024:
            log.warning("announce_image_too_big", url=url[:80], size=len(resp.content))
            return None
        return resp.content
    except (httpx.HTTPError, OSError) as exc:
        log.warning("announce_image_fetch_failed", url=url[:80], error=str(exc)[:120])
        return None


def _to_png(raw: bytes | None) -> bytes | None:
    """Конвертирует любые байты картинки в PNG.

    Pinterest API v5 (media_source.image_base64) принимает только image/jpeg и
    image/png — НЕ webp. Обложки designservice генерятся в WebP, поэтому без
    конвертации Pinterest отклоняет пин. VK тоже надёжнее принимает PNG.
    """
    if not raw:
        return None
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — graceful degrade, любая ошибка PIL
        log.warning("announce_png_convert_failed", error=str(exc)[:120])
        return None


def _build_post_text(
    title: str,
    url: str,
    excerpt: str,
    content_type: str,
    extra_text: str = "",
    site_name: str = "bamboodom.ru",
) -> str:
    """Текст поста для соц. сетей. content_type: telegram_html / pin_text / plain_text.

    v14 (2026-04-26): excerpt 280/300 → 600/700 символов, плюс
    опциональный extra_text (первый параграф статьи) ещё до 700.
    Так пост получается информативнее — читатель видит начало статьи,
    а не только заголовок.
    """
    if content_type == "telegram_html":
        parts = [f"<b>{title.strip()}</b>"]
        if excerpt:
            parts.append("")
            parts.append(excerpt.strip()[:600])
        if extra_text:
            parts.append("")
            parts.append(extra_text.strip()[:700])
        parts.append("")
        parts.append(f'<a href="{url}">Читать на {site_name}</a>')
        return "\n".join(parts)
    # plain text для VK / pin_text для Pinterest
    parts = [title.strip()]
    if excerpt:
        parts.append("")
        parts.append(excerpt.strip()[:600])
    if extra_text:
        parts.append("")
        parts.append(extra_text.strip()[:700])
    parts.append("")
    parts.append(url)
    return "\n".join(parts)


def _build_pin_description(title: str, url: str, alt: str, excerpt: str = "") -> str:
    """Описание для одного Pinterest-пина.

    Каждое фото статьи даёт пин со СВОИМ описанием (alt картинки), плюс общий
    хвост с заголовком статьи и ссылкой. Так пины не дублируются.
    """
    parts = []
    a = (alt or "").strip()
    t = (title or "").strip()
    if a and a.lower() != t.lower():
        parts.append(a)
    parts.append(t)
    if excerpt:
        parts.append(excerpt.strip()[:400])
    parts.append(url)
    # dedupe, keep order
    seen = set()
    out = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "\n\n".join(out)[:800]


async def announce_to_social(
    *,
    db: Any,
    http_client: httpx.AsyncClient,
    settings: Any,
    title: str,
    url: str,
    excerpt: str = "",
    image_url: str = "",
    extra_text: str = "",
    project_id_override: int = 0,
    site_name: str = "bamboodom.ru",
    dedicated_tg_attr: str = "bamboodom_tg_channel",
    source_tag: str = "bamboodom_announce",
    pinterest_images: list[dict] | None = None,
) -> dict[str, str]:
    """Шлёт анонс во все привязанные платформы. Graceful degrade.

    Args:
        project_id_override: project_id для не-bamboodom вызовов (designservice).
            Если 0 — используется settings.bamboodom_announce_project_id (default).
        site_name: 'Читать на {site_name}' в подписи поста.
        dedicated_tg_attr: имя атрибута в settings для dedicated TG channel
            (используется чтобы пропустить duplicate-постинг в TG).
        source_tag: метка для logging/metadata.

    Возвращает map «платформа:identifier» → результат («ok» / причина skip).
    Если у платформы несколько подключений (напр. VK личная + группа) —
    постит во все и возвращает отдельный результат по каждому.
    """
    project_id = project_id_override or getattr(settings, "bamboodom_announce_project_id", 0) or 0
    if not project_id:
        return {p: "skip:no_project_id" for p in PLATFORMS}

    enc_key = settings.encryption_key.get_secret_value()
    cm = CredentialManager(enc_key)
    conn_repo = ConnectionsRepository(db, cm)

    image_bytes: bytes | None = None
    if image_url:
        image_bytes = await _fetch_image_bytes(http_client, image_url)

    # WebP-обложку конвертируем в PNG: Pinterest v5 webp не принимает,
    # VK надёжнее работает с png. Telegram идёт отдельным путём (см. ниже).
    png_image_bytes: bytes | None = _to_png(image_bytes)

    # 5C (2026-04-27): если задан BAMBOODOM_TG_CHANNEL — telegram-анонс уходит
    # через announce_article (отдельный путь с dedicated bot), connection-based
    # путь для telegram пропускаем чтобы не было двойных постов в одном канале.
    bbk_tg_channel_set = bool((getattr(settings, dedicated_tg_attr, "") or "").strip())

    results: dict[str, str] = {}
    for platform in PLATFORMS:
        if platform == "telegram" and bbk_tg_channel_set:
            results[platform] = "skip:duplicate_with_dedicated_channel"
            continue
        try:
            connections = await conn_repo.get_by_project_and_platform(project_id, platform)
        except Exception as exc:
            log.warning("announce_db_failed", platform=platform, error=str(exc)[:120])
            results[platform] = f"db_error:{exc}"
            continue

        active = [c for c in connections if (c.status or "active") == "active"]
        if not active:
            results[platform] = "skip:no_connection"
            continue

        # Готовим content_type под платформу
        if platform == "telegram":
            content_type = "telegram_html"
        elif platform == "vk":
            content_type = "plain_text"
        else:
            content_type = "pin_text"

        # Постим во ВСЕ активные подключения платформы (раньше брали только
        # active[0] — из-за этого VK-группа игнорировалась, постилось только
        # в личную страницу).
        for connection in active:
            label = f"{platform}:{connection.identifier or connection.id}"
            on_refresh = make_token_refresh_cb(db, connection.id, enc_key)
            try:
                publisher = create_publisher(platform, http_client, settings, on_token_refresh=on_refresh)
            except Exception as exc:
                log.warning("announce_publisher_init_failed", platform=platform, conn=label, exc_info=True)
                results[label] = f"error:{exc}"
                continue

            # ── Pinterest: ОТДЕЛЬНЫЙ пин на КАЖДОЕ фото статьи ──
            # 5-9 пинов на статью, у каждого своё описание (alt картинки) и
            # ссылка на статью. Список картинок приходит в pinterest_images
            # [{"url","alt"}]; если пуст — fallback на одну обложку.
            if platform == "pinterest":
                pin_imgs = pinterest_images or ([{"url": image_url, "alt": title}] if image_url else [])
                if not pin_imgs:
                    results[label] = "skip:no_image"
                    continue
                ok_n, fail_n, skip_n = 0, 0, 0
                first_err = ""
                for idx, im in enumerate(pin_imgs):
                    raw = await _fetch_image_bytes(http_client, im.get("url", ""))
                    png = _to_png(raw)
                    if not png:
                        skip_n += 1
                        continue
                    alt = (im.get("alt") or title).strip()
                    desc = _build_pin_description(title, url, alt, excerpt)
                    req = PublishRequest(
                        connection=connection,
                        content=desc,
                        content_type="pin_text",
                        images=[png],
                        images_meta=[{"alt": alt[:100]}],
                        title=title[:120],
                        metadata={
                            "source": source_tag,
                            "article_url": url,
                            "link": url,
                            "pin_title": (alt or title)[:100],
                        },
                    )
                    try:
                        r = await publisher.publish(req)
                    except Exception as exc:
                        fail_n += 1
                        first_err = first_err or str(exc)
                        log.warning("announce_pin_failed", conn=label, idx=idx, exc_info=True)
                        continue
                    if r.success:
                        ok_n += 1
                        log.info("announce_pin_sent", conn=label, idx=idx, post_url=r.post_url)
                    else:
                        fail_n += 1
                        first_err = first_err or (r.error or "unknown")
                        log.warning("announce_pin_fail", conn=label, idx=idx, error=r.error)
                    await asyncio.sleep(1.0)  # лёгкая пауза между пинами (anti-spam)
                results[label] = f"pins ok={ok_n} fail={fail_n} skip={skip_n}" + (f" err={first_err}" if first_err else "")
                continue

            # ── VK / Telegram: один пост с обложкой ──
            if platform == "vk":
                plat_image = png_image_bytes or image_bytes
            else:
                plat_image = image_bytes

            request = PublishRequest(
                connection=connection,
                content=_build_post_text(
                    title, url, excerpt, content_type, extra_text=extra_text, site_name=site_name
                ),
                content_type=content_type,
                images=[plat_image] if plat_image else [],
                images_meta=[{"alt": title[:100]}] if plat_image else [],
                title=title[:120],
                metadata={"source": source_tag, "article_url": url},
            )
            try:
                result = await publisher.publish(request)
            except Exception as exc:
                log.warning("announce_publish_failed", platform=platform, conn=label, exc_info=True)
                results[label] = f"error:{exc}"
                continue

            if result.success:
                results[label] = f"ok:{result.post_url or result.platform_post_id or ''}"
                log.info("announce_sent", platform=platform, conn=label, post_url=result.post_url)
            else:
                results[label] = f"fail:{result.error or 'unknown'}"
                log.warning("announce_publisher_fail", platform=platform, conn=label, error=result.error)

    return results
