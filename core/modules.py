# core/modules.py
"""
Управление видимостью модулей по сегментам.
════════════════════════════════════════════════════════════════════════════════

Логика доступа:
  - Суперадмин видит все модули всегда
  - Админ сегмента видит модули своего сегмента + Админку
  - Суперпользователь и Пользователь видят только модули своего сегмента

Структура modules в segments.json:
  {
    "modules": {
      "advisor":   {"enabled": true,  "status": "active"},
      "scanner":   {"enabled": false, "status": "active"},
      "predictor": {"enabled": true,  "status": "maintenance"}
    }
  }

  enabled  — включён ли модуль для сегмента
  status   — active | maintenance (на обслуживании показывает заглушку)

Использование в app.py:
  from core.modules import get_visible_modules, get_module_status
  visible = get_visible_modules(user)   # список модулей для сайдбара
  status  = get_module_status(user, "advisor")  # active | maintenance | disabled
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

# ── Пути ─────────────────────────────────────────────────────────────────────

_BASE_DIR     = Path(__file__).parent.parent.resolve()
_SEGMENTS_FILE = _BASE_DIR / "data" / "admin" / "segments.json"

# ── Реестр всех модулей системы ───────────────────────────────────────────────
# Порядок важен — именно в таком порядке отображаются в сайдбаре

ALL_MODULES = {
    "advisor":   "Советчик",
    "scanner":   "Сканер документов",
    "analyzer":  "Анализатор заявок",
    "predictor": "Прогноз решения регулятора",
    "protocol":  "Протокольщик",
    "tasks":     "Задачи",
}

# Модули которые не управляются через общий блок segments.json["modules"]
# Админка — для segment_admin, но её можно выключить суперадмином на уровне
# сегмента (см. is_admin_panel_enabled) через segments.json["admin_panel_enabled"]
# Управление — только для superadmin, всегда
SYSTEM_MODULES = {
    "admin":      "Админка",
    "superadmin": "Управление",
}

# Дефолтная конфигурация для нового сегмента — все включены
DEFAULT_MODULE_CONFIG = {
    mid: {"enabled": True, "status": "active"}
    for mid in ALL_MODULES
}

# Статусы модулей
MODULE_STATUS_LABELS = {
    "active":      "Активен",
    "maintenance": "На обслуживании",
}


# ─────────────────────────────────────────────────────────────────────────────
# Загрузка конфига модулей сегмента
# ─────────────────────────────────────────────────────────────────────────────

def _load_segment(org_id: str) -> Optional[Dict]:
    """Загружает данные сегмента из segments.json."""
    if not _SEGMENTS_FILE.exists():
        return None
    try:
        data = json.loads(_SEGMENTS_FILE.read_text(encoding="utf-8"))
        return data.get(org_id)
    except Exception:
        return None


def get_segment_modules(org_id: str) -> Dict[str, Dict]:
    """
    Возвращает конфигурацию модулей для сегмента.
    Если поле modules отсутствует (старые сегменты) — возвращает дефолт (все включены).
    """
    seg = _load_segment(org_id)
    if not seg:
        return DEFAULT_MODULE_CONFIG.copy()
    return seg.get("modules", DEFAULT_MODULE_CONFIG.copy())


def is_admin_panel_enabled(org_id: str) -> bool:
    """
    Проверяет, включена ли Админка для сегмента.

    Управляется суперадмином отдельным полем segments.json["admin_panel_enabled"]
    (не через общий блок "modules" — Админка системный, а не обычный раздел).
    Если поле отсутствует (все сегменты, созданные до этой правки) — считается
    включённой, чтобы не поменять поведение по умолчанию задним числом.
    """
    if not org_id:
        return False
    seg = _load_segment(org_id)
    if not seg:
        return False
    return seg.get("admin_panel_enabled", True)


# ─────────────────────────────────────────────────────────────────────────────
# Основная логика видимости
# ─────────────────────────────────────────────────────────────────────────────

def get_visible_modules(user: Dict) -> List[str]:
    """
    Возвращает список module_id которые видит пользователь в сайдбаре.
    Порядок соответствует ALL_MODULES.

    Логика:
      superadmin        → все модули из ALL_MODULES
      segment_admin     → включённые модули сегмента (status любой — показываем)
      superuser / user  → включённые модули сегмента (status любой — показываем)

    Модули со статусом maintenance показываются в сайдбаре но при открытии
    показывают заглушку вместо контента.
    """
    role = user.get("role", "user")

    # Суперадмин видит всё
    if role == "superadmin":
        return list(ALL_MODULES.keys())

    org_id = user.get("org_id", "")
    if not org_id:
        return []

    modules_config = get_segment_modules(org_id)

    # Фильтруем только включённые модули, сохраняем порядок из ALL_MODULES
    return [
        mid for mid in ALL_MODULES
        if modules_config.get(mid, {}).get("enabled", True)
    ]


def get_module_status(user: Dict, module_id: str) -> str:
    """
    Возвращает статус модуля для текущего пользователя:
      active      — модуль работает нормально
      maintenance — показываем заглушку "на обслуживании"
      disabled    — модуль отключён для сегмента (не должен показываться в сайдбаре)

    Суперадмин всегда получает active — он видит всё.
    """
    role = user.get("role", "user")

    if role == "superadmin":
        return "active"

    org_id = user.get("org_id", "")
    if not org_id:
        return "disabled"

    modules_config = get_segment_modules(org_id)
    module = modules_config.get(module_id, {})

    if not module.get("enabled", True):
        return "disabled"

    return module.get("status", "active")


def is_module_accessible(user: Dict, module_id: str) -> bool:
    """
    Проверяет что пользователь имеет доступ к модулю.
    Используется как защита на уровне страницы — даже если кто-то
    откроет URL напрямую.
    """
    role = user.get("role", "user")

    # Суперадмин — доступ ко всему
    if role == "superadmin":
        return True

    # Системные модули
    if module_id == "admin":
        return role == "segment_admin" and is_admin_panel_enabled(user.get("org_id", ""))
    if module_id == "superadmin":
        return False

    # Обычные модули — проверяем конфиг сегмента
    status = get_module_status(user, module_id)
    return status in ("active", "maintenance")


# ─────────────────────────────────────────────────────────────────────────────
# Статистика использования модулей (для карточки сегмента)
# ─────────────────────────────────────────────────────────────────────────────

def get_module_usage_30d(org_id: str) -> Dict[str, int]:
    """
    Возвращает количество рабочих действий в каждом модуле за последние
    30 дней для указанного сегмента.

    Данные берутся из журнала использования функций (core/usage_tracker.py,
    data/usage_log/) — не из аудита: аудит хранит только события
    безопасности и администрирования.
    Возвращает dict: {module_id: count}
    """
    from core.usage_tracker import get_segment_module_counts

    counts = get_segment_module_counts(org_id, days=30)
    return {mid: counts.get(mid, 0) for mid in ALL_MODULES}