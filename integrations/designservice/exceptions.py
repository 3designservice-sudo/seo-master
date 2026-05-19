"""Exception hierarchy for Designservice integrations."""

from __future__ import annotations


class DesignserviceAPIError(Exception):
    """Base for all Designservice integration errors."""


class DesignserviceAuthError(DesignserviceAPIError):
    """403 from _bot_api.php or _receiver.php — wrong/missing key.

    Expected condition; do NOT capture to Sentry.
    """


class DesignserviceArticleNotFound(DesignserviceAPIError):
    """404 from _bot_api.php?action=get_article&id=N — article missing."""


class DesignserviceReceiverError(DesignserviceAPIError):
    """_receiver.php returned non-OK body or non-200 status.

    Receiver 404 typically means the script was overwritten (see CLAUDE.md
    in designservice project — known beget bug). Catch and re-deploy receiver.
    """


class DesignservicePublishError(DesignserviceAPIError):
    """Generic publishing error — file write failed, mkdir failed, etc."""
