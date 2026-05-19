"""Async HTTP client for Designservice.group _bot_api.php + _receiver.php.

Two endpoints:
    _bot_api.php  — read planned articles, update status (auth: ?k=<bot_api_key>)
    _receiver.php — POST base64 body → file on beget (auth: ?k=<receiver_key>)

Both endpoints belong to one site (https://designservice.group, controlled by
GRAD). See CLAUDE.md in designservice project for endpoint specs.
"""

from __future__ import annotations

import base64
import urllib.parse
from dataclasses import dataclass

import httpx
import structlog

from bot.config import get_settings
from integrations.designservice.exceptions import (
    DesignserviceAPIError,
    DesignserviceArticleNotFound,
    DesignserviceAuthError,
    DesignservicePublishError,
    DesignserviceReceiverError,
)
from integrations.designservice.models import (
    Article,
    MarkStatusResponse,
    PlannedResponse,
    PublishResult,
    StatsResponse,
)

log = structlog.get_logger()

_DEFAULT_TIMEOUT = 20.0
_PUBLISH_TIMEOUT = 60.0  # большой HTML с картинками base64 может идти долго


@dataclass
class DesignserviceClient:
    """Thin async wrapper for designservice.group blog automation.

    Args:
        base_url: site URL без trailing slash (default — from Settings).
        bot_api_key: key for _bot_api.php (default — from Settings, SecretStr).
        receiver_key: key for _receiver.php (default — from Settings, SecretStr).
        http_client: optional shared httpx.AsyncClient.
        timeout: per-request timeout (publish uses _PUBLISH_TIMEOUT).
    """

    base_url: str = ""
    bot_api_key: str = ""
    receiver_key: str = ""
    http_client: httpx.AsyncClient | None = None
    timeout: float = _DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        if not self.base_url or not self.bot_api_key or not self.receiver_key:
            s = get_settings()
            self.base_url = self.base_url or s.designservice_base_url.rstrip("/")
            self.bot_api_key = (
                self.bot_api_key or s.designservice_bot_api_key.get_secret_value()
            )
            self.receiver_key = (
                self.receiver_key or s.designservice_receiver_key.get_secret_value()
            )

    # ---- shared HTTP plumbing ----------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self.http_client is not None:
            return self.http_client
        return httpx.AsyncClient(timeout=self.timeout)

    async def _bot_api(self, params: dict[str, str], method: str = "GET", body: bytes | None = None) -> dict:
        if not self.bot_api_key:
            raise DesignserviceAuthError("DESIGNSERVICE_BOT_API_KEY not configured")
        url = f"{self.base_url}/_bot_api.php"
        merged = {"k": self.bot_api_key, **params}

        owned = self.http_client is None
        client = await self._get_client()
        try:
            if method == "GET":
                r = await client.get(url, params=merged, timeout=self.timeout)
            else:
                r = await client.request(
                    method,
                    url,
                    params=merged,
                    content=body,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout,
                )
        finally:
            if owned:
                await client.aclose()

        if r.status_code == 403:
            raise DesignserviceAuthError("forbidden by _bot_api.php")
        if r.status_code == 404:
            raise DesignserviceArticleNotFound(r.text[:200])
        if r.status_code >= 400:
            raise DesignserviceAPIError(
                f"_bot_api.php returned HTTP {r.status_code}: {r.text[:200]}"
            )
        return r.json()

    # ---- read API ----------------------------------------------------------

    async def get_planned(
        self,
        date: str = "today",
        limit: int = 4,
        sort: str = "freq",
    ) -> PlannedResponse:
        """Get planned articles for given date, sorted by keyword frequency desc.

        Args:
            date: 'today' | 'tomorrow' | 'YYYY-MM-DD'.
            limit: max articles to return (0 = no limit).
            sort: 'freq' (default) — by kw_total_freq desc.
        """
        params = {"action": "get_planned", "date": date, "sort": sort}
        if limit > 0:
            params["limit"] = str(limit)
        data = await self._bot_api(params)
        return PlannedResponse.model_validate(data)

    async def get_article(self, article_id: int) -> Article:
        """Fetch one article by id. Raises DesignserviceArticleNotFound on 404."""
        data = await self._bot_api({"action": "get_article", "id": str(article_id)})
        return Article.model_validate(data)

    async def stats(self) -> StatsResponse:
        """Pipeline stats — counts by status / service / kind / planned date."""
        data = await self._bot_api({"action": "stats"})
        return StatsResponse.model_validate(data)

    # ---- write API ---------------------------------------------------------

    async def mark_status(
        self,
        article_id: int,
        status: str,
        *,
        published_url: str | None = None,
        published_date: str | None = None,
        word_count: int | None = None,
        humanizer_score: float | None = None,
        block_reason: str | None = None,
        notes: str | None = None,
    ) -> MarkStatusResponse:
        """Atomic status update via _bot_api.php?action=mark_status (POST + flock)."""
        import json
        patch: dict[str, str | int | float] = {"status": status}
        if published_url is not None:
            patch["published_url"] = published_url
        if published_date is not None:
            patch["published_date"] = published_date
        if word_count is not None:
            patch["word_count"] = word_count
        if humanizer_score is not None:
            patch["humanizer_score"] = humanizer_score
        if block_reason is not None:
            patch["block_reason"] = block_reason
        if notes is not None:
            patch["notes"] = notes
        body = json.dumps(patch, ensure_ascii=False).encode("utf-8")
        data = await self._bot_api(
            {"action": "mark_status", "id": str(article_id)},
            method="POST",
            body=body,
        )
        return MarkStatusResponse.model_validate(data)

    async def mark_published(
        self,
        article_id: int,
        published_url: str,
        published_date: str,
        *,
        word_count: int | None = None,
        humanizer_score: float | None = None,
    ) -> MarkStatusResponse:
        """Shorthand for mark_status(status='published', ...)."""
        return await self.mark_status(
            article_id,
            "published",
            published_url=published_url,
            published_date=published_date,
            word_count=word_count,
            humanizer_score=humanizer_score,
        )

    async def mark_blocked(
        self,
        article_id: int,
        reason: str,
    ) -> MarkStatusResponse:
        """Shorthand for mark_status(status='blocked', block_reason=...)."""
        return await self.mark_status(article_id, "blocked", block_reason=reason)

    # ---- publish HTML through _receiver.php ---------------------------------

    async def publish_html(self, slug: str, html_content: str) -> PublishResult:
        """POST base64-encoded HTML body → file blog/{slug}/index.html on beget.

        _receiver.php signature: ?k=<receiver_key>&p=<relative_path> body=base64.
        Server response — plain text 'OK <bytes> -> <path>'.
        Receiver creates intermediate dirs automatically (@mkdir recursive).

        Args:
            slug: URL slug — relative path under /blog/ (e.g. 'chto-takoe-remont').
            html_content: full HTML document (head + body), UTF-8 string.

        Raises:
            DesignserviceAuthError on 403.
            DesignserviceReceiverError on receiver 404 (script overwritten — see
                HOWTO_large_files_via_receiver.md in designservice project).
            DesignservicePublishError on non-OK body or write failure.
        """
        if not self.receiver_key:
            raise DesignserviceAuthError("DESIGNSERVICE_RECEIVER_KEY not configured")
        if not slug or "/" in slug or ".." in slug:
            raise DesignservicePublishError(f"invalid slug: {slug!r}")
        dst = f"blog/{slug}/index.html"
        b64_body = base64.b64encode(html_content.encode("utf-8"))
        url = f"{self.base_url}/_receiver.php"
        params = {"k": self.receiver_key, "p": dst}

        owned = self.http_client is None
        client = await self._get_client()
        try:
            r = await client.post(
                url, params=params, content=b64_body, timeout=_PUBLISH_TIMEOUT
            )
        finally:
            if owned:
                await client.aclose()

        if r.status_code == 403:
            raise DesignserviceAuthError("forbidden by _receiver.php")
        if r.status_code == 404:
            raise DesignserviceReceiverError(
                "_receiver.php returned 404 — script was overwritten by another deploy. "
                "Restore from HOWTO_large_files_via_receiver.md."
            )
        if r.status_code >= 400:
            raise DesignservicePublishError(
                f"_receiver.php HTTP {r.status_code}: {r.text[:200]}"
            )

        text = r.text.strip()
        if not text.startswith("OK "):
            raise DesignservicePublishError(f"unexpected body: {text[:200]!r}")

        # body format: 'OK 30312 -> blog/chto-takoe-remont/index.html'
        bytes_written = 0
        try:
            parts = text.split()
            bytes_written = int(parts[1])
        except (IndexError, ValueError):
            pass

        published_url = f"{self.base_url}/blog/{slug}/"
        log.info(
            "designservice.publish_html",
            slug=slug,
            bytes=bytes_written,
            url=published_url,
        )
        return PublishResult(
            ok=True,
            dst_path=dst,
            bytes_written=bytes_written,
            raw_response=text,
        )

    # ---- ping / smoke-test -------------------------------------------------

    async def ping(self) -> bool:
        """Hit _bot_api.php?action=stats to verify auth + connectivity.

        Returns True if 200 OK. Raises DesignserviceAuthError on 403 or
        DesignserviceAPIError on any other failure.
        """
        try:
            await self.stats()
            return True
        except DesignserviceAuthError:
            raise
        except DesignserviceAPIError:
            raise
