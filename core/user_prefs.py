# core/user_prefs.py
"""
Персональные настройки Советчика.

ПРОБЛЕМА, КОТОРУЮ ЭТО ЧИНИТ
────────────────────────────
Раньше настройки лежали в config/advisor_prefs.json — ОДНОМ файле на всё
приложение. Последствия:

  1. user_context — это должность, организация, город и специфика работы
     конкретного человека, и он уходит в СИСТЕМНЫЙ ПРОМПТ его запросов.
     С общим файлом специалист РСО «Теплосеть» сохранял свой контекст, а
     сотрудник РЭК Тамбова его и видел, и перезаписывал, и получал чужую
     подпись в своих запросах к LLM.
  2. top_k / temperature / neighbor_radius / answer_length — пользователи
     молча затирали настройки друг друга.

Теперь: один файл на пользователя — data/user_prefs/{user_id}.json.

МИГРАЦИЯ. Старый config/advisor_prefs.json читается как источник ТЕХНИЧЕСКИХ
дефолтов (top_k, temperature, neighbor_radius, answer_length) для тех, у кого
своего файла ещё нет. user_context оттуда НЕ наследуется никогда: это личные
данные неизвестного автора, и раздать их всем — ровно то, что мы чиним.
Каждый заполняет свой контекст сам, один раз.
"""

from __future__ import annotations

import os
import re
import json
import threading
from typing import Dict, Optional

from core.advisor import get_current_org_id, get_current_user_id

# =============================================================================
# Пути
# =============================================================================
BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFS_DIR     = os.path.join(BASE_DIR, "data", "user_prefs")
LEGACY_PREFS  = os.path.join(BASE_DIR, "config", "advisor_prefs.json")

_lock = threading.Lock()

# Дефолты — совпадают с прежними значениями в advisor_page.py
DEFAULT_PREFS: Dict = {
    "top_k":           20,
    "neighbor_radius": 0,
    "temperature":     0.3,
    "user_context":    "",
    "answer_length":   "short",
}

# Поля, которые можно унаследовать из старого общего файла: технические,
# обезличенные. user_context в этот список не входит и входить не должен.
_INHERITABLE = ("top_k", "neighbor_radius", "temperature", "answer_length")

_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(part: str, fallback: str) -> str:
    """Санитизация id перед подстановкой в путь файла."""
    cleaned = _SAFE_RE.sub("_", str(part or "")).strip("._")
    return cleaned or fallback


def _prefs_file(user_id: str) -> str:
    return os.path.join(PREFS_DIR, _safe(user_id, "anonymous") + ".json")


def _load_legacy_defaults() -> Dict:
    """Технические дефолты из старого общего файла. Без user_context."""
    if not os.path.exists(LEGACY_PREFS):
        return {}
    try:
        with open(LEGACY_PREFS, "r", encoding="utf-8") as f:
            legacy = json.load(f)
        return {k: legacy[k] for k in _INHERITABLE if k in legacy}
    except Exception:
        return {}


def _coerce(prefs: Dict) -> Dict:
    """
    Приводит типы и загоняет значения в допустимые границы.
    Файл настроек редактируется руками — верить ему на слово нельзя,
    иначе строка вместо числа уронит слайдер при рендере.
    """
    out = dict(DEFAULT_PREFS)
    out.update(prefs or {})
    try:
        out["top_k"] = max(1, min(50, int(out.get("top_k", 20))))
    except Exception:
        out["top_k"] = DEFAULT_PREFS["top_k"]
    try:
        out["neighbor_radius"] = max(0, min(5, int(out.get("neighbor_radius", 0))))
    except Exception:
        out["neighbor_radius"] = DEFAULT_PREFS["neighbor_radius"]
    try:
        out["temperature"] = max(0.0, min(1.0, float(out.get("temperature", 0.3))))
    except Exception:
        out["temperature"] = DEFAULT_PREFS["temperature"]
    if out.get("answer_length") not in ("short", "detailed"):
        out["answer_length"] = DEFAULT_PREFS["answer_length"]
    out["user_context"] = str(out.get("user_context") or "")
    return out


def load_prefs(user_id: Optional[str] = None) -> Dict:
    """
    Настройки текущего (или указанного) пользователя.
    Если личного файла нет — дефолты, дополненные техническими значениями
    из старого общего конфига. user_context при этом всегда пустой.
    """
    if user_id is None:
        user_id = get_current_user_id()

    path = _prefs_file(user_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _coerce(json.load(f))
        except Exception as e:
            print(f"[PREFS] Не удалось прочитать {path}: {e}")

    return _coerce({**_load_legacy_defaults()})


def save_prefs(prefs: Dict, user_id: Optional[str] = None) -> bool:
    """Сохраняет настройки пользователя целиком."""
    if user_id is None:
        user_id = get_current_user_id()

    data = _coerce(prefs)
    # org_id пишем справочно — помогает при разборе инцидентов и чистке
    # настроек при архивации сегмента. На чтение не влияет.
    data["org_id"]     = get_current_org_id()
    data["user_id"]    = str(user_id)
    data["updated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")

    path = _prefs_file(user_id)
    try:
        os.makedirs(PREFS_DIR, exist_ok=True)
        with _lock:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[PREFS] Не удалось сохранить {path}: {e}")
        return False


def update_prefs(user_id: Optional[str] = None, **fields) -> Dict:
    """
    Точечно обновляет отдельные поля, не трогая остальные.
    Возвращает актуальный словарь настроек.

        update_prefs(top_k=25, temperature=0.4)
    """
    prefs = load_prefs(user_id)
    prefs.update(fields)
    save_prefs(prefs, user_id)
    return prefs


def delete_prefs(user_id: str) -> bool:
    """Удаляет настройки пользователя — при архивации аккаунта."""
    path = _prefs_file(user_id)
    try:
        if os.path.exists(path):
            os.remove(path)
        return True
    except Exception as e:
        print(f"[PREFS] Не удалось удалить {path}: {e}")
        return False