"""Application configuration via Pydantic Settings v2."""

from functools import lru_cache
from typing import Annotated

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """All env vars from docs/API_CONTRACTS.md section 4.2."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === Required ===
    telegram_bot_token: SecretStr
    admin_ids: Annotated[list[int], NoDecode]
    supabase_url: str
    supabase_key: SecretStr
    upstash_redis_url: str
    upstash_redis_token: SecretStr
    qstash_token: SecretStr
    qstash_current_signing_key: SecretStr
    qstash_next_signing_key: SecretStr
    openrouter_api_key: SecretStr
    firecrawl_api_key: SecretStr = SecretStr("")
    encryption_key: SecretStr
    telegram_webhook_secret: SecretStr

    # === Optional ===
    dataforseo_login: str = ""
    dataforseo_password: SecretStr = SecretStr("")
    serper_api_key: SecretStr = SecretStr("")
    yookassa_shop_id: str = ""
    yookassa_secret_key: SecretStr = SecretStr("")
    yookassa_return_url: str = ""
    sentry_dsn: str = ""
    railway_public_url: str = ""
    pinterest_app_id: str = ""
    pinterest_app_secret: SecretStr = SecretStr("")
    vk_app_id: int = 0
    vk_secure_key: SecretStr = SecretStr("")
    vk_service_key: SecretStr = SecretStr("")
    health_check_token: SecretStr = SecretStr("")
    telegram_api_id: int = 0  # from https://my.telegram.org — needed for forum topics
    telegram_api_hash: SecretStr = SecretStr("")  # from https://my.telegram.org
    usd_rub_rate: float = 92.5

    # --- Bamboodom.ru blog API (Session 1 skeleton) ---
    bamboodom_blog_key: SecretStr = SecretStr("")
    bamboodom_api_base: str = "https://bamboodom.ru/api.php"
    bamboodom_enabled: bool = False

    # --- Yandex Webmaster (Bamboodom recrawl, 4D) ---
    # OAuth-токен с правом webmaster:hosts.
    # Если user_id / host_id оставить пустыми — клиент попросит API сам определить
    # их по списку хостов пользователя при первом вызове.
    yandex_webmaster_token: SecretStr = SecretStr("")
    yandex_webmaster_user_id: str = ""
    yandex_webmaster_host_id: str = ""
    yandex_webmaster_site: str = "https://bamboodom.ru"

    # --- Yandex.Metrika (Bamboodom analytics, 4F) ---
    yandex_metrika_token: SecretStr = SecretStr("")
    yandex_metrika_counter_id: str = ""

    # --- Bamboodom TG-канал анонсов (4G.tg) ---
    # @username или числовой -100xxxxxxxxxx. Бот должен быть админом с правом постинга.
    bamboodom_tg_channel: str = ""
    # 5B (2026-04-27): optional dedicated bot token for the bamboodom channel.
    # When set, services/announce/tg_channel.py uses Bot(token) instead of
    # piggybacking on the main @best_seo_master_bot. Useful when the bamboodom
    # channel admin is a separate bot (e.g. @BamBooDom_bot for @bamboodom).
    bamboodom_tg_bot_token: SecretStr = SecretStr("")

    # --- Google Search Console (4G) ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")

    # --- Bamboodom social-анонс (4L v2) ---
    # Используем существующие publishers — нужен только project_id из БД,
    # куда юзер привязал connections (vk, pinterest, telegram) через UI бота.
    bamboodom_announce_project_id: int = 0
    # --- Bamboodom AI image generation (4T, 2026-04-27) ---
    # Когда True: для каждого img-блока в AI-статье сгенерируем фото
    # через Gemini → WebP 1024 → multipart-upload в blog_upload_image.
    # При False img-блоки идут с пустым src и B рендерит placeholder.
    bamboodom_images_enabled: bool = False

    # --- Designservice.group (admin-only section, 2026-05-19) ---
    # Второй сайт студии (designservice.group). Публикуется не через
    # серверный blog_publish (как bamboodom), а через _receiver.php — POST
    # base64-тело создаёт файл /blog/{slug}/index.html на beget-хостинге.
    # Темы берутся из _bot_api.php (article_roadmap.json, 391 статья).
    designservice_enabled: bool = False
    # Базовый URL сайта (используется в canonical, og:url, ld+json).
    designservice_base_url: str = "https://designservice.group"
    # Ключ для _receiver.php (заливка готового HTML файлом). См. tokens.json.
    designservice_receiver_key: SecretStr = SecretStr("")
    # Ключ для _bot_api.php (get_planned / get_article / mark_status).
    designservice_bot_api_key: SecretStr = SecretStr("")
    # Включает генерацию картинок через openrouter_image для статей DS.
    designservice_images_enabled: bool = False
    # Telegram-канал для анонсов опубликованных статей (опционально).
    designservice_tg_channel: str = ""
    designservice_tg_bot_token: SecretStr = SecretStr("")
    # project_id из таблицы projects для общего publishers/announce flow.
    designservice_announce_project_id: int = 0

    # --- Yandex Webmaster (Designservice recrawl) ---
    # Параллельно с bamboodom — отдельный OAuth-токен и host_id для
    # designservice.group. Если не задано, используется общий yandex_webmaster_token.
    yandex_webmaster_token_ds: SecretStr = SecretStr("")
    yandex_webmaster_user_id_ds: str = ""
    yandex_webmaster_host_id_ds: str = ""
    yandex_webmaster_site_ds: str = "https://designservice.group"

    # === Server ===
    port: int = 8080

    # === Defaults ===
    default_timezone: str = "Europe/Moscow"
    fsm_ttl_seconds: int = 86400
    fsm_inactivity_timeout: int = 1800
    preview_ttl_seconds: int = 86400
    max_regenerations_free: int = 2
    railway_graceful_shutdown_timeout: int = 120

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, v: str | list[int]) -> list[int]:
        ids = v if isinstance(v, list) else [int(x.strip()) for x in str(v).split(",") if x.strip()]
        if not ids:
            msg = "ADMIN_IDS must contain at least one admin ID"
            raise ValueError(msg)
        return ids

    @field_validator("encryption_key")
    @classmethod
    def _validate_encryption_key(cls, v: SecretStr) -> SecretStr:
        """Validate that ENCRYPTION_KEY is a valid Fernet key at startup."""
        raw = v.get_secret_value()
        if not raw:
            msg = "ENCRYPTION_KEY must not be empty"
            raise ValueError(msg)
        try:
            Fernet(raw.encode() if isinstance(raw, str) else raw)
        except Exception as exc:
            msg = f"ENCRYPTION_KEY is not a valid Fernet key: {exc}"
            raise ValueError(msg) from exc
        return v

    @field_validator("supabase_url")
    @classmethod
    def _supabase_url_must_be_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            msg = "SUPABASE_URL must start with https://"
            raise ValueError(msg)
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton Settings instance (cached after first call)."""
    return Settings()  # type: ignore[call-arg]
