"""Headless Playwright publisher for Я.Бизнес (yandex.ru/sprav).

Loads Я.ID cookies from designservice.group/_bot_api.php?action=ya_session_get,
opens organization posts page, fills new publication, uploads cover,
clicks Создать, verifies «Публикация на модерации» card appeared.

env:
    DESIGNSERVICE_BASE_URL   (default https://designservice.group)
    DESIGNSERVICE_BOT_API_KEY  (required)
    YANDEX_BUSINESS_ORG_ID   (default 220044162072)

DS_YABIZ_HEADLESS_v1
"""

from __future__ import annotations

import html
import io
import os
import re
from pathlib import Path

import httpx
import structlog
from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

log = structlog.get_logger()

DS_BASE = os.environ.get("DESIGNSERVICE_BASE_URL", "https://designservice.group")
BOT_KEY = os.environ.get("DESIGNSERVICE_BOT_API_KEY", "")
ORG_ID = os.environ.get("YANDEX_BUSINESS_ORG_ID", "220044162072")
POSTS_URL = f"https://yandex.ru/sprav/{ORG_ID}/p/edit/posts"
POST_LIMIT_CHARS = 2900  # запас 100 для хвостовой ссылки


async def _fetch_cookies() -> list[dict]:
    """Get cookies from designservice.group bot_api endpoint."""
    if not BOT_KEY:
        raise RuntimeError("DESIGNSERVICE_BOT_API_KEY not set in env")
    url = f"{DS_BASE.rstrip('/')}/_bot_api.php?action=ya_session_get&k={BOT_KEY}"
    async with httpx.AsyncClient(timeout=15) as cl:
        r = await cl.get(url)
        r.raise_for_status()
        j = r.json()
    cookies = j.get("cookies", [])
    if not cookies:
        raise RuntimeError("no cookies in ya_session_get response")
    log.info("yabiz.cookies.fetched", count=len(cookies), updated_at=j.get("updated_at"))
    return cookies


def _convert_cookies_for_playwright(cookies: list[dict]) -> list[dict]:
    """Convert Cookie-Editor JSON → Playwright add_cookies format."""
    out: list[dict] = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain")
        path = c.get("path", "/")
        if not name or value is None or not domain:
            continue
        pc: dict = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        # sameSite mapping
        ss = (c.get("sameSite") or "").lower()
        if ss in ("no_restriction", "none"):
            pc["sameSite"] = "None"
        elif ss == "strict":
            pc["sameSite"] = "Strict"
        else:
            pc["sameSite"] = "Lax"
        exp = c.get("expirationDate")
        if exp is not None:
            pc["expires"] = int(exp)
        out.append(pc)
    return out


async def _build_post_text(slug: str, h1: str) -> str:
    """Parse blog page → h1 + lead + N paragraphs + URL, limited to POST_LIMIT_CHARS."""
    url = f"{DS_BASE.rstrip('/')}/blog/{slug}/"
    async with httpx.AsyncClient(timeout=15) as cl:
        r = await cl.get(url)
        r.raise_for_status()
        page_html = r.text

    # lead из <p class="art-excerpt">
    m = re.search(r'<p class="art-excerpt"[^>]*>(.*?)</p>', page_html, re.S | re.I)
    lead = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip()) if m else ""

    # body параграфы из #root SSR
    m = re.search(r'<div id="root"[^>]*>(.*?)</div>\s*<script', page_html, re.S | re.I)
    root = m.group(1) if m else ""
    root = re.sub(r"<script[^>]*>.*?</script>", "", root, flags=re.S | re.I)

    paras_raw = re.findall(r"<p[^>]*>(.*?)</p>", root, re.S | re.I)
    paras: list[str] = []
    for p in paras_raw:
        t = re.sub(r"<[^>]+>", " ", p)
        t = html.unescape(t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 40 and "art-excerpt" not in t and t != lead:
            paras.append(t)
    if paras and paras[0].strip() == lead.strip():
        paras = paras[1:]

    tail = f"Подробнее на сайте: {url}"
    lines = [h1, "", lead, ""]
    used = sum(len(s) + 1 for s in lines)
    for p in paras:
        add = len(p) + 2
        if used + add + len(tail) + 4 > POST_LIMIT_CHARS:
            break
        lines.append(p)
        lines.append("")
        used = sum(len(s) + 1 for s in lines)
    lines.append(tail)
    return "\n".join(lines).strip()


async def _download_and_convert_cover(slug: str, dst: Path) -> int:
    """Download /blog/{slug}/cover.webp → convert to PNG at dst. Returns file size."""
    from PIL import Image

    url = f"{DS_BASE.rstrip('/')}/blog/{slug}/cover.webp"
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url)
        r.raise_for_status()
    img = Image.open(io.BytesIO(r.content))
    img.convert("RGB").save(str(dst), "PNG")
    return dst.stat().st_size


async def _set_textarea_value(page: Page, value: str) -> int:
    """Fill React-managed textarea via native setter + input event. Returns final length."""
    js = """
    (text) => {
        const ta = document.querySelector('textarea[placeholder*="Расскажите"]');
        if (!ta) throw new Error('textarea not found');
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(ta, text);
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));
        ta.focus();
        return ta.value.length;
    }
    """
    length = await page.evaluate(js, value)
    log.info("yabiz.textarea.filled", set_len=len(value), got_len=length)
    if length != len(value):
        raise RuntimeError(f"textarea fill mismatch: set {len(value)}, got {length}")
    return length


async def publish(article_id: int, slug: str, h1: str, *, headless: bool = True) -> dict:
    """Publish article to Я.Бизнес.

    Returns dict:
      {ok: True, text_len, cover_bytes, screenshot_bytes, took_ms}
      or
      {ok: False, error: str, url?, screenshot_bytes?}
    """
    import time

    start = time.time()
    work_dir = Path("/tmp") / f"yabiz_{article_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    cover_path = work_dir / "cover.png"

    try:
        text = await _build_post_text(slug, h1)
        if len(text) > 3000:
            return {"ok": False, "error": f"text too long: {len(text)}"}
        cover_bytes = await _download_and_convert_cover(slug, cover_path)
        cookies_raw = await _fetch_cookies()
        cookies = _convert_cookies_for_playwright(cookies_raw)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            )
            # дополнительный stealth: убираем navigator.webdriver
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            await context.add_cookies(cookies)

            page = await context.new_page()
            try:
                await page.goto(POSTS_URL, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                screenshot = await page.screenshot(full_page=False)
                await browser.close()
                return {
                    "ok": False,
                    "error": "navigation timeout",
                    "screenshot_bytes": len(screenshot),
                }

            # check auth — passport redirect = session expired
            cur_url = page.url
            if "passport.yandex" in cur_url or "auth" in cur_url:
                await browser.close()
                return {
                    "ok": False,
                    "error": "redirected to passport (session expired)",
                    "url": cur_url,
                    "action_required": "refresh_yandex_session",
                }

            # wait for textarea
            try:
                await page.locator('textarea[placeholder*="Расскажите"]').first.wait_for(
                    timeout=10000
                )
            except PlaywrightTimeoutError:
                screenshot = await page.screenshot(full_page=False)
                await browser.close()
                return {
                    "ok": False,
                    "error": "textarea did not appear in 10s",
                    "url": page.url,
                    "screenshot_bytes": len(screenshot),
                }

            # fill text via JS
            await _set_textarea_value(page, text)

            # upload cover via hidden file input
            file_input = page.locator('input[type="file"]').first
            await file_input.set_input_files(str(cover_path))

            # wait for cover preview + autosave
            await page.wait_for_timeout(3500)

            # click Создать
            create_btn = page.locator('button:has-text("Создать")').first
            try:
                await create_btn.click(timeout=5000)
            except PlaywrightTimeoutError:
                screenshot = await page.screenshot(full_page=False)
                await browser.close()
                return {
                    "ok": False,
                    "error": "Создать button not clickable",
                    "screenshot_bytes": len(screenshot),
                }

            # wait for «Публикация на модерации» card
            try:
                await page.locator('text="Публикация на модерации"').first.wait_for(timeout=10000)
                await page.wait_for_timeout(1500)  # animation settle
                screenshot = await page.screenshot(full_page=False)
                took_ms = int((time.time() - start) * 1000)
                await browser.close()
                return {
                    "ok": True,
                    "text_len": len(text),
                    "cover_bytes": cover_bytes,
                    "screenshot_bytes": len(screenshot),
                    "took_ms": took_ms,
                }
            except PlaywrightTimeoutError:
                screenshot = await page.screenshot(full_page=False)
                await browser.close()
                return {
                    "ok": False,
                    "error": "moderation card did not appear in 10s",
                    "screenshot_bytes": len(screenshot),
                }
    except Exception as exc:
        log.exception("yabiz.publish.failed", article_id=article_id)
        return {"ok": False, "error": f"exception: {type(exc).__name__}: {exc}"}
