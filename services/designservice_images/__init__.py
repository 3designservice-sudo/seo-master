"""Image generation pipeline for designservice.group blog articles.

Public surface:
    generate_and_publish_cover(article, client, openrouter_image, base_url) -> str | None
        Generates 1024x576 cover, crops to 16:9 WebP, uploads via _receiver.php.
        Returns absolute URL of published cover (or None on failure).
"""

from services.designservice_images.cover import generate_and_publish_cover
from services.designservice_images.inline_images import enrich_with_inline_images

__all__ = ["enrich_with_inline_images", "generate_and_publish_cover"]
