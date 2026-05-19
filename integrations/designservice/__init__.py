"""Designservice.group blog automation client.

Public surface:
    DesignserviceClient            — async HTTP client (read + publish)
    Article / PlannedResponse / StatsResponse / MarkStatusResponse
                                   — Pydantic models for _bot_api.php responses
    PublishResult                  — result of publish_html()
    DesignserviceAPIError          — base exception
    DesignserviceAuthError         — 403 (wrong/missing key)
    DesignserviceArticleNotFound   — 404 on get_article
    DesignserviceReceiverError     — _receiver.php 404 (script overwritten)
    DesignservicePublishError      — non-OK response / write failure
"""

from integrations.designservice.client import DesignserviceClient
from integrations.designservice.exceptions import (
    DesignserviceAPIError,
    DesignserviceArticleNotFound,
    DesignserviceAuthError,
    DesignservicePublishError,
    DesignserviceReceiverError,
)
from integrations.designservice.models import (
    Article,
    ArticleStatus,
    MarkStatusResponse,
    PlannedResponse,
    PublishResult,
    StatsResponse,
)

__all__ = [
    "Article",
    "ArticleStatus",
    "DesignserviceAPIError",
    "DesignserviceArticleNotFound",
    "DesignserviceAuthError",
    "DesignserviceClient",
    "DesignservicePublishError",
    "DesignserviceReceiverError",
    "MarkStatusResponse",
    "PlannedResponse",
    "PublishResult",
    "StatsResponse",
]
