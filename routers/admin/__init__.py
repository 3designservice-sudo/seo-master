"""Admin routers — dashboard, broadcast, costs, users, bamboodom, designservice."""

from aiogram import Router

from routers.admin import (
    bamboodom,
    bamboodom_admin,
    bamboodom_analytics,
    bamboodom_keywords,
    costs,
    dashboard,
    debug_project_id,
    designservice_admin,
    users,
)

router = Router()
router.include_router(users.router)
router.include_router(costs.router)
router.include_router(dashboard.router)
# bamboodom_admin регистрируется ПЕРЕД bamboodom.router, чтобы новый
# bamboodom:entry handler (root, 3 кнопки) перехватывал callback вместо
# старого. Старый handler в bamboodom.router после правки слушает
# bamboodom:articles.
router.include_router(bamboodom_admin.router)
router.include_router(bamboodom_analytics.router)
router.include_router(bamboodom_keywords.router)
router.include_router(bamboodom.router)
# Designservice.group — параллельный admin-only раздел (PR 1, 2026-05-19).
# Сейчас в этом router только root меню + заглушки трёх пунктов;
# подключение _bot_api.php / _receiver.php / FSM генерации — в следующих PR.
router.include_router(designservice_admin.router)
# Debug: /myprojectid (admin-only) — показать project_id всех проектов
# админа. Нужен для настройки BAMBOODOM_ANNOUNCE_PROJECT_ID в Railway env.
router.include_router(debug_project_id.router)
