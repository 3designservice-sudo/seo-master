"""User-facing strings for the Designservice admin section (PR 1 skeleton)."""

# Section title
DESIGNSERVICE_TITLE = "DESIGNSERVICE.GROUP"

# Root entry screen
DESIGNSERVICE_ROOT_TITLE = "Designservice.group — управление контентом"
DESIGNSERVICE_ROOT_SUBTITLE = (
    "Студия дизайна интерьера в Крыму. 391 статья в roadmap, темп публикаций 3-4 в день."
)
DESIGNSERVICE_ROOT_HINT = (
    "Темы берутся из _bot_api.php (article_roadmap.json), публикация — через _receiver.php "
    "(POST base64 → файл /blog/{slug}/index.html на beget-хостинге)."
)

# Stub screens (PR 1 — заглушки)
DESIGNSERVICE_STUB_ARTICLES = (
    "Раздел «Статьи» появится в PR 2 — подключение _bot_api.php (источник тем) "
    "и _receiver.php (публикация HTML)."
)
DESIGNSERVICE_STUB_ADMIN = (
    "Раздел «Администрирование» появится в PR 2-3 — переобход страниц через Yandex "
    "Webmaster API, статистика roadmap, smoke-test API endpoint-ов."
)
DESIGNSERVICE_STUB_ANALYTICS = (
    "Раздел «Аналитика» появится в PR 4 — позиции из rank_tracker, статистика "
    "индексации, дайджест."
)
