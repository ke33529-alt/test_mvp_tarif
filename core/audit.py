# core/audit.py
"""
Аудит системы РЕГУЛА.AI
════════════════════════════════════════════════════════════════════════════════

НАЗНАЧЕНИЕ
  Журнал безопасности и администрирования: кто вошёл, кто что изменил в
  пользователях, сегментах, правах и базах знаний. Отвечает на вопрос
  «кто это сделал и когда», нужен для разбора инцидентов.

  Работа пользователей в модулях (запросы, анализы, прогнозы, задачи,
  открытие разделов) сюда НЕ пишется — это продуктовая аналитика,
  она ведётся в core/usage_tracker.py (вкладка «Использование функций»).

  Правило разделения:
    аудит         — вход/выход, учётные записи, права, настройки сегментов,
                    состав баз знаний, выгрузка журналов;
    использование — всё, что пользователь делает в модулях для своей работы.

Принципы:
  - Логируем ФАКТЫ действий, не содержимое (тексты запросов и ответы не хранятся)
  - Хранение: data/audit/audit_YYYY_MM.jsonl — один файл на месяц
  - Время — московское (UTC+3), без зоны в строке: контейнер работает в UTC,
    а журнал читают люди в Москве
  - Запись защищена threading.Lock() — безопасно при параллельных Streamlit-сессиях

Формат одной записи:
  {
    "ts":      "2026-07-11T14:32:11",   # московское время
    "org_id":  "tambov",               # сегмент инициатора (пусто для суперадмина)
    "user_id": "20260101_120000_000",   # кто совершил действие
    "role":    "superuser",            # роль на момент события
    "event":   "user_created",         # тип события (см. EVENT_TYPES)
    "module":  "superadmin",           # раздел, где совершено действие, или None
    "meta":    {}                      # доп. данные (зависят от типа события)
  }

Типы событий — см. EVENT_TYPES / EVENT_LABELS ниже.

Использование:
  from core.audit import log_event
  log_event(org_id=user["org_id"], user_id=user["user_id"],
            role=user["role"], event="user_created", module="superadmin",
            meta={"target_user_id": uid, "role": "user"})

  # Короткая форма — инициатор берётся из текущей сессии
  from core.audit import audit
  audit("kb_doc_uploaded", module="local_kb", meta={"filename": name})
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ── Пути ─────────────────────────────────────────────────────────────────────

_BASE_DIR  = Path(__file__).parent.parent.resolve()
_AUDIT_DIR = _BASE_DIR / "data" / "audit"

# ── Время ────────────────────────────────────────────────────────────────────
# Москва без перехода на летнее время (с 2014 г.) — фиксированное смещение
# надёжнее zoneinfo: в slim-образе может не быть базы часовых поясов.
_MSK = timezone(timedelta(hours=3))


def now_local() -> datetime:
    """Текущее московское время без tzinfo (совместимо со старыми записями)."""
    return datetime.now(_MSK).replace(tzinfo=None)


# ── Допустимые типы событий ─────────────────────────────────────────────────

EVENT_LABELS: Dict[str, str] = {
    # Вход и сессии
    "login":                    "Вход",
    "login_failed":             "Неудачный вход",
    "logout":                   "Выход",
    "password_changed":         "Смена пароля",
    # Учётные записи (действия администратора)
    "password_reset":           "Сброс пароля",
    "force_logout":             "Принудительный выход",
    "user_created":             "Создан пользователь",
    "user_updated":             "Изменён пользователь",
    "user_blocked":             "Пользователь заблокирован",
    "user_unblocked":           "Пользователь разблокирован",
    "user_archived":            "Пользователь в архиве",
    "user_restored":            "Пользователь восстановлен",
    # Сегменты
    "segment_created":          "Создан сегмент",
    "segment_archived":         "Сегмент в архиве",
    "segment_restored":         "Сегмент восстановлен",
    "segment_modules_changed":  "Изменены модули сегмента",
    "segment_settings_changed": "Изменены настройки сегмента",
    # Локальная база знаний сегмента
    "kb_doc_uploaded":          "Документ добавлен в базу",
    "kb_doc_deleted":           "Документ удалён из базы",
    "kb_cleared":               "База знаний очищена",
    "kb_settings_changed":      "Изменены настройки базы",
    # Выгрузка журналов
    "log_exported":             "Выгрузка журнала",
}

EVENT_TYPES = set(EVENT_LABELS)

# Цвета бейджей по группам событий
EVENT_BADGE_COLORS: Dict[str, tuple] = {
    "login":                    ("#EAF3DE", "#27500A"),
    "logout":                   ("#F1EFE8", "#5F5E5A"),
    "login_failed":             ("#FCE8E6", "#8A1C12"),
    "password_changed":         ("#E6F1FB", "#0C447C"),
    "password_reset":           ("#FAEEDA", "#633806"),
    "force_logout":             ("#FAEEDA", "#633806"),
    "user_blocked":             ("#FCE8E6", "#8A1C12"),
    "user_archived":            ("#F1EFE8", "#5F5E5A"),
    "segment_archived":         ("#F1EFE8", "#5F5E5A"),
    "kb_cleared":               ("#FCE8E6", "#8A1C12"),
    "kb_doc_deleted":           ("#FAECE7", "#712B13"),
    "log_exported":             ("#EEEDFE", "#3C3489"),
}
EVENT_BADGE_DEFAULT = ("#E6F1FB", "#0C447C")

# События, которые раньше ошибочно писались в аудит, хотя относятся к
# использованию функций. Старые записи остаются в файлах, но из журнала
# аудита скрываются (read_log(include_legacy=False) — по умолчанию).
LEGACY_USAGE_EVENTS = {
    "llm_query", "module_open", "button_click", "file_upload", "file_deleted",
    "task_add", "task_complete", "task_edit", "task_delete",
}

# ── Блокировка записи ─────────────────────────────────────────────────────────
# Один Lock на весь процесс — достаточно т.к. Streamlit однопроцессный.
_write_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Запись события
# ─────────────────────────────────────────────────────────────────────────────

def log_event(
    org_id:  str,
    user_id: str,
    role:    str,
    event:   str,
    module:  Optional[str] = None,
    meta:    Optional[Dict] = None,
) -> None:
    """
    Записывает одно событие в аудит текущего месяца.

    Параметры:
      org_id  — ID сегмента инициатора (пусто "" для суперадмина)
      user_id — ID пользователя-инициатора
      role    — роль на момент события
      event   — тип события из EVENT_TYPES
      module  — раздел, где совершено действие (None для входа/выхода)
      meta    — словарь с доп. данными

    Функция не бросает исключений — сбой аудита не должен прерывать работу.
    События использования функций сюда не принимаются — они уходят
    в core/usage_tracker.py, в аудите не остаются.
    """
    if event in LEGACY_USAGE_EVENTS:
        print(f"[audit] Событие '{event}' относится к использованию функций — "
              f"пишите его через core.usage_tracker", file=sys.stderr)
        return
    if event not in EVENT_TYPES:
        print(f"[audit] Предупреждение: неизвестный тип события '{event}'", file=sys.stderr)

    now = now_local()
    entry = {
        "ts":      now.isoformat(timespec="seconds"),
        "org_id":  org_id  or "",
        "user_id": user_id or "",
        "role":    role    or "",
        "event":   event,
        "module":  module,
        "meta":    meta or {},
    }
    _write_entry(entry, now)


def audit(event: str, module: Optional[str] = None, meta: Optional[Dict] = None) -> None:
    """
    Короткая форма log_event: инициатор берётся из текущей сессии
    (st.session_state["_auth_user"]). Без сессии — не пишет ничего.
    """
    try:
        import streamlit as st
        user = st.session_state.get("_auth_user") or {}
    except Exception:
        user = {}
    if not user.get("user_id"):
        return
    log_event(
        org_id=user.get("org_id", ""),
        user_id=user.get("user_id", ""),
        role=user.get("role", ""),
        event=event,
        module=module,
        meta=meta,
    )


def _write_entry(entry: Dict, dt: datetime) -> None:
    """Дописывает запись в файл месяца под блокировкой."""
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _AUDIT_DIR / f"audit_{dt.strftime('%Y_%m')}.jsonl"
        line = json.dumps(entry, ensure_ascii=False)
        with _write_lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print(f"[audit] Ошибка записи: {e}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Чтение журнала
# ─────────────────────────────────────────────────────────────────────────────

def read_log(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    org_id:    Optional[str] = None,
    user_id:   Optional[str] = None,
    event:     Optional[str] = None,
    module:    Optional[str] = None,
    limit:     int = 500,
    include_legacy: bool = False,
) -> List[Dict]:
    """
    Читает события аудита с фильтрацией (новые первые).

    org_id фильтрует по сегменту инициатора ИЛИ по целевому сегменту
    (meta.target_org_id) — чтобы в журнале сегмента были видны и действия
    суперадмина над ним.

    include_legacy=False скрывает старые записи использования функций,
    попавшие в аудит до разделения журналов.
    """
    files = _get_files_for_range(date_from, date_to)
    results: List[Dict] = []

    for fpath in files:
        if not fpath.exists():
            continue
        try:
            with fpath.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ev = rec.get("event", "")
                    if not include_legacy and ev in LEGACY_USAGE_EVENTS:
                        continue

                    ts = rec.get("ts", "")
                    if date_from and ts[:10] < date_from:
                        continue
                    if date_to and ts[:10] > date_to:
                        continue

                    if org_id:
                        _meta = rec.get("meta") or {}
                        if rec.get("org_id") != org_id and _meta.get("target_org_id") != org_id:
                            continue
                    if user_id:
                        _meta = rec.get("meta") or {}
                        if rec.get("user_id") != user_id and _meta.get("target_user_id") != user_id:
                            continue
                    if event  and ev != event:
                        continue
                    if module and rec.get("module") != module:
                        continue

                    results.append(rec)
        except Exception as e:
            print(f"[audit] Ошибка чтения {fpath}: {e}", file=sys.stderr)

    results.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return results[:limit]


def _get_files_for_range(date_from: Optional[str], date_to: Optional[str]) -> List[Path]:
    """Файлы журнала, пересекающиеся с диапазоном дат (все — если диапазон пуст)."""
    if not _AUDIT_DIR.exists():
        return []

    all_files = sorted(_AUDIT_DIR.glob("audit_*.jsonl"))
    if not date_from and not date_to:
        return all_files

    result = []
    for fpath in all_files:
        parts = fpath.stem.split("_")  # ["audit", "2026", "07"]
        if len(parts) < 3:
            continue
        file_month = f"{parts[1]}-{parts[2]}"
        if date_from and file_month < date_from[:7]:
            continue
        if date_to and file_month > date_to[:7]:
            continue
        result.append(fpath)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Форматирование для UI
# ─────────────────────────────────────────────────────────────────────────────

_SECTION_LABELS = {
    "superadmin": "Управление",
    "admin":      "Админка",
    "local_kb":   "Локальная база знаний",
}

_FIELD_LABELS = {
    "name":           "название",
    "role":           "роль",
    "rag_collection": "RAG-коллекция",
    "admin_panel":    "Админка",
    "notes":          "заметки",
    "method":         "метод",
    "chunk_size":     "размер",
    "overlap":        "перекрытие",
    "word_safe":      "защита слов",
    "login":          "логин",
    "org_id":         "сегмент",
}


def format_details(rec: Dict, users: Dict, segments: Dict) -> str:
    """Человекочитаемая строка «Детали» для записи аудита."""
    meta = rec.get("meta") or {}
    parts: List[str] = []

    mod = rec.get("module") or ""
    if mod and mod in _SECTION_LABELS:
        parts.append(_SECTION_LABELS[mod])

    if meta.get("target_user_id"):
        tid = meta["target_user_id"]
        parts.append(f"→ {users.get(tid, {}).get('name', tid)}")
    if meta.get("target_org_id"):
        tid = meta["target_org_id"]
        parts.append(f"→ {segments.get(tid, {}).get('name', meta.get('name', tid))}")
    if meta.get("login"):
        parts.append(f"логин: {meta['login']}")
    if meta.get("reason"):
        parts.append(meta["reason"])
    if meta.get("filename"):
        parts.append(str(meta["filename"])[:60])
    if meta.get("count") is not None:
        parts.append(f"файлов: {meta['count']}")
    if meta.get("chunks") is not None:
        parts.append(f"фрагм.: {meta['chunks']}")
    if meta.get("errors"):
        parts.append(f"ошибок: {meta['errors']}")
    if meta.get("changed"):
        ch = meta["changed"]
        if isinstance(ch, dict):
            items = []
            for k, v in list(ch.items())[:4]:
                if isinstance(v, dict) and "from" in v:
                    items.append(f"{_FIELD_LABELS.get(k, k)}: {v.get('from')} → {v.get('to')}")
                else:
                    items.append(f"{_FIELD_LABELS.get(k, k)}: {v}")
            parts.append("; ".join(items))
        elif isinstance(ch, list):
            parts.append(", ".join(_FIELD_LABELS.get(k, k) for k in ch))
    if meta.get("log"):
        parts.append({"audit": "аудит", "usage": "использование"}.get(meta["log"], meta["log"]))
    if meta.get("records") is not None:
        parts.append(f"записей: {meta['records']}")
    if meta.get("duration_min"):
        parts.append(f"длит. {meta['duration_min']} мин")

    return " · ".join(str(p) for p in parts) if parts else "—"


# ─────────────────────────────────────────────────────────────────────────────
# Агрегация
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_stats(date: Optional[str] = None) -> Dict:
    """
    Сводка аудита за день (по умолчанию сегодня, московское время):
      total_events, by_event, logins, failed_logins, active_users.
    active_users — уникальные пользователи, у которых были входы.
    """
    if not date:
        date = now_local().strftime("%Y-%m-%d")

    records = read_log(date_from=date, date_to=date, limit=100_000)

    by_event: Dict[str, int] = {}
    users: set = set()
    for rec in records:
        ev = rec.get("event", "unknown")
        by_event[ev] = by_event.get(ev, 0) + 1
        if ev == "login" and rec.get("user_id"):
            users.add(rec["user_id"])

    return {
        "date":          date,
        "total_events":  len(records),
        "by_event":      by_event,
        "logins":        by_event.get("login", 0),
        "failed_logins": by_event.get("login_failed", 0),
        "active_users":  len(users),
    }


def get_module_stats(
    org_id:    Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
) -> List[Dict]:
    """
    Совместимость со старыми вызовами: статистика модулей теперь считается
    по журналу использования (core.usage_tracker.get_module_stats).
    """
    from core.usage_tracker import get_module_stats as _usage_module_stats
    return _usage_module_stats(org_id=org_id, date_from=date_from, date_to=date_to)


def export_to_csv(records: List[Dict]) -> str:
    """Конвертирует записи аудита в CSV-строку для st.download_button."""
    import csv
    import io

    if not records:
        return ""

    output = io.StringIO()
    fieldnames = ["ts", "org_id", "user_id", "role", "event", "module", "meta"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        row = {**rec, "meta": json.dumps(rec.get("meta", {}), ensure_ascii=False)}
        writer.writerow(row)
    # BOM — чтобы Excel на Windows корректно открыл кириллицу
    return "﻿" + output.getvalue()