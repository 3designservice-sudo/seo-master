"""Cover image generator for designservice blog articles.

Pipeline (called from api/designservice_publish.py — PR 7):
    1. Build prompt from article.h1 + service + geo
    2. OpenRouterImageClient.generate(prompt, aspect_ratio="16:9")
    3. Decode base64 or fetch URL → raw bytes
    4. Pillow: open → convert RGB → crop to 1024x576 → save WebP quality 85
    5. DesignserviceClient.publish_image(slug, webp_bytes, "cover.webp")
    6. Return absolute URL: https://designservice.group/blog/{slug}/cover.webp

Errors are logged but not raised — article publishes without cover if Gemini fails.
A missing cover only degrades the og:image / hero img fallback to /Logo_DS.png.
"""

from __future__ import annotations

import base64
import io
from typing import Any

import httpx
import structlog

log = structlog.get_logger()


def _build_cover_prompt(article: Any) -> str:
    """Compose Gemini-image prompt from article metadata.

    For interior design / renovation topics we want photo-realistic interior
    scenes shot from professional angle, soft natural light, modern Crimean
    apartment context. Avoid logos, text overlays, watermarks (Gemini occasionally
    adds these — explicitly negative prompt them).
    """
    service = (article.service or "").lower()
    geo_hint = "Crimea, modern Russian apartment" if "крым" in (article.geo or "").lower() else "modern apartment"

    style_map = {
        "design": "elegant living room with sunlight, contemporary minimal style, neutral palette",
        "renovation": "freshly renovated apartment, clean white walls, hardwood floor, professional photography",
        "architecture": "modern residential building exterior, golden hour, architectural photography",
        "construction": "construction site of modern home with cranes, daytime, wide shot",
        "landscape": "designed garden in Crimea with stone path, Mediterranean plants, warm afternoon light",
        "furniture": "stylish living room with custom furniture, soft natural light, neutral tones",
        "european-furniture": "luxury Italian-style living room with European furniture, evening light, warm atmosphere",
        "curtains": "tall windows with flowing linen curtains, soft daylight, modern interior",
        "supervision": "professional interior designer reviewing blueprints in modern apartment",
        "completion": "fully furnished apartment ready for move-in, warm cozy atmosphere",
        "plaster": "wall with decorative Venetian plaster texture, warm directional light",
        "panels": "wall with WPC wood panels, modern accent, contemporary lighting",
        "flexstone": "exterior facade with flexible stone tiles, dramatic afternoon light",
    }
    base_style = style_map.get(service, "modern interior design, professional photography")

    prompt = (
        f"Professional editorial photograph for blog cover. Topic: {article.h1}. "
        f"Scene: {base_style}. Context: {geo_hint}. Aspect ratio 16:9, 1024x576. "
        f"Photography style: realistic, magazine quality, sharp focus, soft natural light, "
        f"shallow depth of field. No text overlays, no logos, no watermarks, no people in foreground."
    )
    return prompt


async def _fetch_image_bytes(
    result: Any,
    http_client: httpx.AsyncClient,
) -> bytes | None:
    """Get raw bytes from ImageResult (either base64-decoded or downloaded from URL)."""
    if result.data_b64:
        try:
            return base64.b64decode(result.data_b64)
        except Exception as exc:
            log.warning("designservice.cover.b64_decode_failed", err=str(exc))
            return None
    if result.url:
        try:
            r = await http_client.get(result.url, timeout=30.0)
            if r.status_code == 200:
                return r.content
            log.warning("designservice.cover.url_fetch_status", status=r.status_code)
        except httpx.HTTPError as exc:
            log.warning("designservice.cover.url_fetch_failed", err=str(exc))
    return None


def _to_webp_1024x576(raw_bytes: bytes) -> bytes | None:
    """Open image → convert RGB → resize/crop to 1024x576 → WebP quality 85."""
    try:
        from PIL import Image
    except ImportError:
        log.warning("designservice.cover.no_pillow")
        return None
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.convert("RGB")
        # Crop to 16:9 if not already
        target_w, target_h = 1024, 576
        target_ratio = target_w / target_h
        src_w, src_h = img.size
        src_ratio = src_w / src_h
        if abs(src_ratio - target_ratio) > 0.05:
            if src_ratio > target_ratio:
                # Source too wide — crop horizontally
                new_w = int(src_h * target_ratio)
                offset = (src_w - new_w) // 2
                img = img.crop((offset, 0, offset + new_w, src_h))
            else:
                # Source too tall — crop vertically
                new_h = int(src_w / target_ratio)
                offset = (src_h - new_h) // 2
                img = img.crop((0, offset, src_w, offset + new_h))
        img = img.resize((target_w, target_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=85, method=6)
        return buf.getvalue()
    except Exception as exc:
        log.warning("designservice.cover.processing_failed", err=str(exc))
        return None


async def generate_and_publish_cover(
    article: Any,
    *,
    openrouter_image_client: Any,
    designservice_client: Any,
    http_client: httpx.AsyncClient,
    base_url: str = "https://designservice.group",
) -> str | None:
    """Full cover pipeline. Returns published URL or None on failure.

    Args:
        article: roadmap Article (with .h1, .service, .geo, .target_url).
        openrouter_image_client: integrations.openrouter_image.OpenRouterImageClient.
        designservice_client: integrations.designservice.DesignserviceClient.
        http_client: shared httpx.AsyncClient (passed to OpenRouter and image fetch).
        base_url: site URL (default designservice.group).

    Returns:
        Absolute URL of /blog/{slug}/cover.webp on success, else None.
    """
    prompt = _build_cover_prompt(article)
    slug = (article.target_url or "").strip("/").removeprefix("blog/")
    if not slug:
        log.warning("designservice.cover.no_slug", article_id=getattr(article, "id", "?"))
        return None

    # 1. Generate image via Gemini
    try:
        result = await openrouter_image_client.generate(
            prompt,
            aspect_ratio="16:9",
            size="1024x576",
        )
    except Exception as exc:
        log.warning("designservice.cover.gemini_failed", err=str(exc))
        return None

    # 2. Fetch raw bytes
    raw = await _fetch_image_bytes(result, http_client)
    if raw is None:
        return None

    # 3. Convert to WebP 1024x576
    webp_bytes = _to_webp_1024x576(raw)
    if webp_bytes is None:
        return None

    # 4. Upload via _receiver.php
    try:
        await designservice_client.publish_image(slug, webp_bytes, name="cover.webp")
    except Exception as exc:
        log.warning(
            "designservice.cover.upload_failed",
            err=str(exc),
            article_id=getattr(article, "id", "?"),
        )
        return None

    cover_url = f"{base_url.rstrip('/')}/blog/{slug}/cover.webp"
    log.info(
        "designservice.cover.published",
        url=cover_url,
        size_bytes=len(webp_bytes),
        article_id=getattr(article, "id", "?"),
    )
    return cover_url
