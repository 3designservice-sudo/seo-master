"""Generate multiple inline images for blog article body.

Pipeline (called from api/designservice_publish.py before render_article):
    1. Parse body_html — find h2 section starts
    2. For each h2 section (max 6 inline + 1 cover = 7 total), build context-aware
       prompt: article topic + section title (h2 text) + section opening sentence
    3. Run Gemini-image generations CONCURRENTLY (Semaphore 3) → WebP 1024x576
    4. Upload all to /blog/{slug}/image-N.webp via ds_client.publish_image
    5. Inject <img> tags after first <p> of each h2 section

Image alts are written from h2 title + section context — good for accessibility
and Yandex image search.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

# Style mapping keyed by service slug (same as cover.py, but multi-perspective)
_STYLE_BY_SERVICE = {
    "design": [
        "elegant living room with sunlight, contemporary minimal style, neutral palette",
        "modern kitchen with marble countertops, brass fixtures, natural light",
        "stylish bedroom with linen textures, soft morning light, warm tones",
        "bathroom with terrazzo floor, brass fittings, plants, daylight",
        "home office with built-in shelves, ergonomic chair, wooden desk",
        "dining area with pendant lights, oak table, designer chairs",
    ],
    "renovation": [
        "freshly renovated empty apartment, clean white walls, hardwood floor, daylight",
        "kitchen renovation in progress, half-installed cabinets, clean tile work",
        "modern bathroom after renovation, white tile, brass fixtures, dry finishes",
        "newly painted living room walls, neutral colors, no furniture, sunlight",
        "renovation team installing baseboards, professional tools visible",
        "after-renovation hallway with new flooring and door frames",
    ],
    "architecture": [
        "modern residential building exterior, golden hour, architectural photography",
        "architect blueprints on a desk with rolled drawings, daylight",
        "exterior facade with clean geometric lines, blue sky",
        "interior structural beams in a modern minimalist home",
        "construction site of modern Crimean home, daytime",
        "courtyard with stone walls and Mediterranean plants",
    ],
    "construction": [
        "construction site of modern home with cranes, daytime, wide shot",
        "concrete foundation being poured by workers, professional gear",
        "wooden roof framing on a new build under blue sky",
        "exterior walls in progress, scaffolding, masonry work",
        "interior dry-wall installation, workers in safety gear",
        "finished exterior of new private house in Crimean landscape",
    ],
    "landscape": [
        "designed garden in Crimea with stone path, Mediterranean plants, warm afternoon",
        "swimming pool with stone surround, lavender bushes, evening light",
        "outdoor patio with pergola and dining table, vines overhead",
        "rock garden with cypresses and gravel paths",
        "front yard with new boxwood hedges and entrance lighting",
        "back terrace with fire pit and lounge chairs at dusk",
    ],
    "furniture": [
        "stylish living room with custom furniture, soft natural light, neutral tones",
        "custom kitchen with handmade cabinets, brass handles, marble top",
        "bedroom with built-in wardrobe, oak finish, soft lighting",
        "office with built-in desk and shelving, ergonomic chair",
        "dining room with custom oak table and upholstered chairs",
        "showroom with furniture samples and fabric swatches",
    ],
    "european-furniture": [
        "luxury Italian-style living room, evening, warm atmosphere",
        "Italian sofa in living room with view to Crimean coast",
        "Spanish dining set with hand-crafted leather chairs",
        "Portuguese ceramics on a wooden shelf in modern home",
        "European bedroom with linen headboard and brass details",
        "showroom of European furniture brands with fabric catalogs",
    ],
    "curtains": [
        "tall windows with flowing linen curtains, soft daylight",
        "bedroom with blackout curtains, soft morning light filtering through",
        "living room with sheer curtains and pleated heading",
        "kitchen window with cafe-style half curtains, plants on sill",
        "fabric samples laid out on a designer's table",
        "automated motorized curtains with hidden track, modern interior",
    ],
    "supervision": [
        "interior designer reviewing blueprints in modern apartment",
        "designer with construction team on a renovation site, daytime",
        "client and designer discussing finishes in showroom",
        "punch list inspection — designer pointing at finish details",
        "designer comparing tile samples against wall, daylight",
        "team meeting with architect and contractors, plans on table",
    ],
    "completion": [
        "fully furnished apartment ready for move-in, warm cozy atmosphere",
        "living room staged with throws, books, plants — turnkey delivery",
        "kitchen with full appliance package and decor accents",
        "bedroom dressed with linens, lamps, art on walls",
        "bathroom styled with towels, plants, candles — final touches",
        "interior decorator placing accent pillows in modern living room",
    ],
    "plaster": [
        "wall with decorative Venetian plaster texture, warm directional light",
        "close-up of marbled Venetian plaster finish in luxury bathroom",
        "feature wall with concrete-effect plaster in modern living room",
        "plastered ceiling with subtle texture, recessed lighting",
        "wall sample with three plaster finishes side by side",
        "artist applying decorative plaster with trowel, professional photo",
    ],
    "panels": [
        "wall with WPC wood panels, modern accent, contemporary lighting",
        "feature wall with bamboo panels in eco-style living room",
        "SPC panels installed on stairs and hallway, modern finish",
        "WPC ceiling panels in bedroom with hidden lighting",
        "panel installation in progress, craftsman with tools",
        "panel samples in showroom with different wood textures",
    ],
    "flexstone": [
        "exterior facade with flexible stone tiles, dramatic afternoon light",
        "curved wall finished with flexible stone in lobby",
        "outdoor archway covered in flex stone, evening light",
        "interior accent wall with stone tile, warm spotlight",
        "stone tile being installed on curved surface, craftsman hands",
        "bathroom with flex stone shower wall, modern fixtures",
    ],
}


async def _gen_one_image(
    openrouter_client: Any,
    http_client: httpx.AsyncClient,
    prompt: str,
    semaphore: asyncio.Semaphore,
) -> bytes | None:
    """Generate ONE image via Gemini → return WebP bytes 1024x576 or None."""
    async with semaphore:
        try:
            result = await openrouter_client.generate(
                prompt, aspect_ratio="16:9", size="1024x576"
            )
        except Exception as exc:
            log.warning("designservice.inline_image.gen_failed", err=str(exc))
            return None

        # Fetch raw bytes
        raw = None
        if result.data_b64:
            try:
                raw = base64.b64decode(result.data_b64)
            except Exception:
                return None
        elif result.url:
            try:
                r = await http_client.get(result.url, timeout=30.0)
                if r.status_code == 200:
                    raw = r.content
            except Exception:
                return None
        if raw is None:
            return None

        # Convert to WebP 1024x576
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            target_w, target_h = 1024, 576
            target_ratio = target_w / target_h
            src_w, src_h = img.size
            src_ratio = src_w / src_h
            if abs(src_ratio - target_ratio) > 0.05:
                if src_ratio > target_ratio:
                    new_w = int(src_h * target_ratio)
                    offset = (src_w - new_w) // 2
                    img = img.crop((offset, 0, offset + new_w, src_h))
                else:
                    new_h = int(src_w / target_ratio)
                    offset = (src_h - new_h) // 2
                    img = img.crop((0, offset, src_w, offset + new_h))
            img = img.resize((target_w, target_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="WEBP", quality=85, method=6)
            return buf.getvalue()
        except Exception as exc:
            log.warning("designservice.inline_image.process_failed", err=str(exc))
            return None


def _split_into_sections(body_html: str) -> list[dict]:
    """Split body into sections by <h2>. Returns list of {h2_title, opening, html_pos}."""
    # Find all h2 positions
    matches = list(re.finditer(r"<h2[^>]*>(.+?)</h2>", body_html, re.IGNORECASE | re.DOTALL))
    sections = []
    for i, m in enumerate(matches):
        title_raw = m.group(1)
        title = re.sub(r"<[^>]+>", "", title_raw).strip()
        # Opening sentence: first <p>...</p> after this h2 (up to next h2 or end)
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(body_html)
        section_html = body_html[m.end():end_pos]
        p_match = re.search(r"<p[^>]*>(.+?)</p>", section_html, re.IGNORECASE | re.DOTALL)
        opening = ""
        opening_end = m.end()
        if p_match:
            opening_raw = p_match.group(1)
            opening = re.sub(r"<[^>]+>", "", opening_raw).strip()[:300]
            opening_end = m.end() + p_match.end()
        sections.append({
            "h2_title": title,
            "opening": opening,
            "insert_at": opening_end,  # position after first <p>
        })
    return sections


def _build_inline_prompt(article: Any, section: dict, style_hint: str) -> str:
    """Compose photo prompt for one section, mixing article topic + section context."""
    section_topic = section["h2_title"][:120]
    article_topic = article.h1[:120]
    geo_hint = "Crimea, modern Russian context"
    prompt = (
        f"Professional editorial photograph for blog article body. "
        f"Article topic: {article_topic}. "
        f"This section: {section_topic}. "
        f"Scene description: {style_hint}. Context: {geo_hint}. "
        f"Aspect ratio 16:9, 1024x576. "
        f"Photography style: realistic, magazine quality, sharp focus, "
        f"soft natural light. No text overlays, no logos, no watermarks, "
        f"no people in foreground."
    )
    return prompt


async def enrich_with_inline_images(
    article: Any,
    body_html: str,
    *,
    openrouter_image_client: Any,
    designservice_client: Any,
    http_client: httpx.AsyncClient,
    max_images: int = 6,
    base_url: str = "https://designservice.group",
) -> str:
    """Insert 1..max_images inline <img> tags between h2 sections.

    Args:
        article: roadmap Article (with .h1, .service, .target_url).
        body_html: HTML body fragment from LLM.
        openrouter_image_client: integrations.openrouter_image.OpenRouterImageClient.
        designservice_client: integrations.designservice.DesignserviceClient.
        http_client: shared httpx.AsyncClient.
        max_images: cap for inline image count (default 6 — plus 1 cover = 7 total).
        base_url: site URL.

    Returns:
        body_html with <img> tags injected after first <p> of each h2 section.
        On any individual image failure, that section just stays text-only — the
        article still publishes.
    """
    sections = _split_into_sections(body_html)
    if not sections:
        return body_html

    # Limit and pick which sections get images (skip first 1 and last 1 — they
    # usually contain intro and CTA / FAQ, where inline image makes less sense)
    candidates = sections[1:-1] if len(sections) >= 4 else sections
    if len(candidates) > max_images:
        # Spread evenly through the article
        step = len(candidates) / max_images
        candidates = [candidates[int(i * step)] for i in range(max_images)]

    if not candidates:
        return body_html

    # Build style hints from service catalog (cycle through)
    service_slug = (article.service or "design").lower()
    style_pool = _STYLE_BY_SERVICE.get(service_slug, _STYLE_BY_SERVICE["design"])

    # Build slug for image filenames
    slug = (article.target_url or "").strip("/").removeprefix("blog/").rstrip("/")
    if not slug:
        log.warning("designservice.inline_images.no_slug")
        return body_html

    # PR 31: cache-check — collect which image-N.webp already exist on server.
    # Saves Gemini API calls on retry / blocked articles.
    existing_names: set[str] = set()
    for i in range(1, len(candidates) + 1):
        name = f"image-{i}.webp"
        url = f"{base_url.rstrip('/')}/blog/{slug}/{name}"
        try:
            r = await http_client.head(url, timeout=10.0)
            if r.status_code == 200:
                existing_names.add(name)
        except Exception:
            pass
    if existing_names:
        log.info(
            "designservice.inline_images.cache_hits",
            slug=slug,
            cached=len(existing_names),
            total=len(candidates),
        )

    # Generate all images in parallel (Semaphore 3) — skip cached
    semaphore = asyncio.Semaphore(3)
    prompts = [
        _build_inline_prompt(article, sec, style_pool[i % len(style_pool)])
        for i, sec in enumerate(candidates)
    ]
    log.info(
        "designservice.inline_images.start",
        article_id=getattr(article, "id", "?"),
        count=len(prompts),
    )
    async def _maybe_generate(idx, prompt):
        name = f"image-{idx + 1}.webp"
        if name in existing_names:
            return b"CACHED"  # sentinel — skip upload, just inject existing URL
        return await _gen_one_image(openrouter_image_client, http_client, prompt, semaphore)

    image_bytes_list = await asyncio.gather(
        *[_maybe_generate(i, p) for i, p in enumerate(prompts)],
        return_exceptions=False,
    )

    # Upload each (skip CACHED sentinel — already on server) + collect URLs
    upload_tasks = []
    cached_results = []
    for i, (sec, raw) in enumerate(zip(candidates, image_bytes_list)):
        if raw is None:
            continue
        name = f"image-{i + 1}.webp"
        if raw == b"CACHED":
            # Already on server — no upload, just collect URL
            url = f"{base_url.rstrip('/')}/blog/{slug}/{name}"
            alt = f"{article.h1}: {sec['h2_title']}"
            cached_results.append((i, sec, url, alt))
            continue
        upload_tasks.append((i, sec, name, raw))

    async def _upload(idx, sec, name, raw):
        try:
            await designservice_client.publish_image(slug, raw, name=name)
            alt = f"{article.h1}: {sec['h2_title']}"
            return idx, sec, f"{base_url.rstrip('/')}/blog/{slug}/{name}", alt
        except Exception as exc:
            log.warning("designservice.inline_images.upload_failed", name=name, err=str(exc))
            return None

    fresh_results = await asyncio.gather(
        *[_upload(idx, sec, name, raw) for idx, sec, name, raw in upload_tasks],
        return_exceptions=False,
    )
    upload_results = cached_results + list(fresh_results)

    # Inject <img> tags into body_html
    # We must process from highest insert_at down so positions stay valid
    injections = []  # list of (insert_at_in_body, html_to_insert)
    for res in upload_results:
        if res is None:
            continue
        idx, sec, url, alt = res
        # Find this section in `sections` to get insert_at
        # `sec` was sliced from candidates; insert_at is the body position
        injections.append((sec["insert_at"], url, alt))

    if not injections:
        log.warning("designservice.inline_images.zero_successful")
        return body_html

    # Sort by position desc
    injections.sort(key=lambda x: x[0], reverse=True)
    out = body_html
    for pos, url, alt in injections:
        img_tag = (
            f'\n<img class="art-inline" src="{url}" alt="{alt}" '
            f'loading="lazy" width="1024" height="576">\n'
        )
        out = out[:pos] + img_tag + out[pos:]

    log.info(
        "designservice.inline_images.done",
        article_id=getattr(article, "id", "?"),
        injected=len(injections),
        attempted=len(prompts),
    )
    return out
