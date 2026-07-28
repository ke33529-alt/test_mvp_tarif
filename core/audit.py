# core/audit.py
"""
Аудит лог РЕГУЛА.AI
════════════════════════════════════════════════════════════════════════════════

Принципы:
  - Логируем ФАКТЫ действий, не содержимое (тексты запросов, ответы LLM не хранятся)
  - Хранение: data/audit/audit_YYYY_MM.jsonl — один файл на месяц
  - Ротация по месяцам: при чтении за период открываем только нужные файлы
  - Запись защищена threading.Lock() — безопасно при параллельных Streamlit-сессиях
    (Streamlit запускает каждую сессию в отдельном потоке одного процесса)

Формат одной записи:
  {
    "ts":      "2026-07-11T14:32:11",   # ISO datetime
    "org_id":  "tambov",               # сегмент (пусто для суперадмина)
    "user_id": "20260101_120000_000",   # ID пользователя
    "role":    "superuser",            # роль на момент события
    "event":   "llm_query",            # тип события (см. EVENT_TYPES)
    "module":  "advisor",              # модуль или None
    "meta":    {}                      # доп. данные (зависят от типа события)
  }

Типы событий (event):
  login             — вход в систему
  logout            — выход / завершение сессии (meta: duration_min)
  module_open       — переход в раздел (module: название)
  button_click      — нажатие значимой кнопки (meta: button_id, label)
  llm_query         — факт отправки запроса к LLM (meta: model, num_ctx)
  file_upload       — загрузка файла (meta: file_type, size_kb)
  file_deleted      — подтверждение удаления файла после обработки (meta: file_type)
  password_reset    — суперадмин сбросил пароль (meta: target_user_id)
  user_created      — создан пользователь (meta: target_user_id, role)
  user_archived     — пользователь заархивирован (meta: target_user_id)
  segment_created   — создан сегмент (meta: target_org_id)
  segment_archived  — сегмент заархивирован (meta: target_org_id)

Использование в модулях:
  from core.audit import log_event

  # Минимальный вызов
  log_event(org_id=user["org_id"], user_id=user["user_id"],
            role=user["role"], event="module_open", module="advisor")

  # С метаданными
  log_event(org_id=user["org_id"], user_id=user["user_id"],
            role=user["role"], event="llm_query", module="advisor",
            meta={"model": "qwen3.5:9b", "num_ctx": 20000})

  # Кнопка
  log_event(org_id=user["org_id"], user_id=user["user_id"],
            role=user["role"], event="button_click", module="predictor",
            meta={"button_id": "analyze", "label": "Анализировать"})
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ── Пути ─────────────────────────────────────────────────────────────────────

_BASE_DIR  = Path(__file__).parent.parent.resolve()
_AUDIT_DIR = _BASE_DIR / "data" / "audit"

# ── Допустимые типы событий (защита от опечаток при вызове) ─────────────────

EVENT_TYPES = {
    "login", "logout", "module_open", "button_click",
    "llm_query", "file_upload", "file_deleted",
    "password_reset", "user_created", "user_archived",
    "segment_created", "segment_archived",
}

# ── Блокировка записи ─────────────────────────────────────────────────────────
# Один Lock на весь процесс — достаточно т.к. Streamlit однопроцессный.
# Время ожидания блокировки = микросекунды (запись одной строки JSON),
# пользователь не заметит задержку.
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
    Записывает одно событие в аудит лог текущего месяца.

    Параметры:
      org_id  — ID сегмента (пусто "" для суперадмина)
      user_id — ID пользователя
      role    — роль на момент события
      event   — тип события из EVENT_TYPES
      module  — название модуля (None для системных событий)
      meta    — словарь с доп. данными (зависит от типа события)

    Функция не бросает исключений — ошибки логируются в stderr.
    Это намеренно: сбой аудита не должен прерывать работу пользователя.
    """
    # Мягкая валидация типа события (предупреждение, не исключение)
    if event not in EVENT_TYPES:
        import sys
        print(f"[audit] Предупреждение: неизвестный тип события '{event}'", file=sys.stderr)

    now = datetime.now()
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


def _write_entry(entry: Dict, dt: datetime) -> None:
    """
    Записывает подготовленную запись в файл текущего месяца.
    Защищена threading.Lock() от одновременной записи из разных потоков.
    """
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _AUDIT_DIR / f"audit_{dt.strftime('%Y_%m')}.jsonl"

        line = json.dumps(entry, ensure_ascii=False)

        with _write_lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    except Exception as e:
        import sys
        print(f"[audit] Ошибка записи: {e}", file=sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Чтение лога
# ─────────────────────────────────────────────────────────────────────────────

def read_log(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    org_id:    Optional[str] = None,
    user_id:   Optional[str] = None,
    event:     Optional[str] = None,
    module:    Optional[str] = None,
    limit:     int = 500,
) -> List[Dict]:
    """
    Читает события из аудит лога с фильтрацией.

    Параметры:
      date_from — фильтр с даты "YYYY-MM-DD" (включительно)
      date_to   — фильтр по дату "YYYY-MM-DD" (включительно)
      org_id    — фильтр по сегменту
      user_id   — фильтр по пользователю
      event     — фильтр по типу события
      module    — фильтр по модулю
      limit     — максимум записей (новые первые)

    Возвращает список записей, отсортированных от новых к старым.
    Читает только файлы нужных месяцев — не грузит лишнее.
    """
    files = _get_files_for_range(date_from, date_to)

    # Собираем все подходящие записи из нужных файлов
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
                        # Пропускаем повреждённые строки (не должны возникать
                        # благодаря Lock, но на всякий случай)
                        continue

                    # Фильтрация по дате (используем префикс ts)
                    ts = rec.get("ts", "")
                    if date_from and ts[:10] < date_from:
                        continue
                    if date_to and ts[:10] > date_to:
                        continue

                    # Фильтрация по полям
                    if org_id  and rec.get("org_id")  != org_id:
                        continue
                    if user_id and rec.get("user_id") != user_id:
                        continue
                    if event   and rec.get("event")   != event:
                        continue
                    if module  and rec.get("module")  != module:
                        continue

                    results.append(rec)

        except Exception as e:
            import sys
            print(f"[audit] Ошибка чтения {fpath}: {e}", file=sys.stderr)

    # Сортировка: новые первые (по ts, строковое сравнение работает для ISO)
    results.sort(key=lambda r: r.get("ts", ""), reverse=True)

    return results[:limit]


def _get_files_for_range(
    date_from: Optional[str],
    date_to:   Optional[str],
) -> List[Path]:
    """
    Возвращает список файлов аудит лога которые нужно прочитать
    для заданного диапазона дат.

    Логика: если оба параметра None — возвращаем все файлы.
    Иначе — только файлы нужных месяцев (экономия I/O).
    """
    if not _AUDIT_DIR.exists():
        return []

    all_files = sorted(_AUDIT_DIR.glob("audit_*.jsonl"))

    if not date_from and not date_to:
        return all_files

    result = []
    for fpath in all_files:
        # Имя файла: audit_YYYY_MM.jsonl → извлекаем YYYY-MM
        stem = fpath.stem  # "audit_2026_07"
        parts = stem.split("_")  # ["audit", "2026", "07"]
        if len(parts) < 3:
            continue
        file_month = f"{parts[1]}-{parts[2]}"  # "2026-07"

        # Проверяем пересечение с запрошенным диапазоном
        if date_from and file_month < date_from[:7]:
            continue
        if date_to and file_month > date_to[:7]:
            continue

        result.append(fpath)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Агрегация для страницы управления
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_stats(date: Optional[str] = None) -> Dict:
    """
    Возвращает агрегированную статистику за указанный день (по умолчанию сегодня).

    Используется для дашборда суперадмина:
      total_events   — всего событий
      by_event       — dict {event_type: count}
      by_module      — dict {module: count}
      active_users   — количество уникальных пользователей
      by_org         — dict {org_id: count}
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    records = read_log(date_from=date, date_to=date, limit=10_000)

    by_event:  Dict[str, int] = {}
    by_module: Dict[str, int] = {}
    by_org:    Dict[str, int] = {}
    users:     set            = set()

    for rec in records:
        ev = rec.get("event", "unknown")
        by_event[ev] = by_event.get(ev, 0) + 1

        mod = rec.get("module")
        if mod:
            by_module[mod] = by_module.get(mod, 0) + 1

        oid = rec.get("org_id", "")
        if oid:
            by_org[oid] = by_org.get(oid, 0) + 1

        uid = rec.get("user_id", "")
        if uid:
            users.add(uid)

    return {
        "date":         date,
        "total_events": len(records),
        "by_event":     by_event,
        "by_module":    by_module,
        "active_users": len(users),
        "by_org":       by_org,
    }


def get_module_stats(
    org_id:    Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
) -> List[Dict]:
    """
    Возвращает статистику использования модулей по пользователям.
    Используется на вкладке "Статистика" страницы Управление.

    Возвращает список:
    [
      {
        "user_id": "...",
        "org_id":  "...",
        "modules": {"advisor": 12, "predictor": 5, ...},
        "total":   17,
      },
      ...
    ]
    """
    records = read_log(
        date_from=date_from,
        date_to=date_to,
        org_id=org_id,
        event="module_open",
        limit=50_000,
    )

    # Агрегируем по пользователю
    user_stats: Dict[str, Dict] = {}
    for rec in records:
        uid = rec.get("user_id", "")
        oid = rec.get("org_id", "")
        mod = rec.get("module", "unknown")
        if not uid:
            continue
        if uid not in user_stats:
            user_stats[uid] = {"user_id": uid, "org_id": oid, "modules": {}, "total": 0}
        user_stats[uid]["modules"][mod] = user_stats[uid]["modules"].get(mod, 0) + 1
        user_stats[uid]["total"] += 1

    return sorted(user_stats.values(), key=lambda x: x["total"], reverse=True)


def export_to_csv(records: List[Dict]) -> str:
    """
    Конвертирует список записей лога в CSV-строку для st.download_button.

    Использование в Streamlit:
      csv = export_to_csv(records)
      st.download_button("Экспорт CSV", csv, "audit.csv", "text/csv")
    """
    import io
    import csv

    if not records:
        return ""

    output = io.StringIO()
    # Фиксированный порядок колонок для читаемого CSV
    fieldnames = ["ts", "org_id", "user_id", "role", "event", "module", "meta"]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        # meta — сериализуем в строку чтобы влезло в одну ячейку
        row = {**rec, "meta": json.dumps(rec.get("meta", {}), ensure_ascii=False)}
        writer.writerow(row)

    return output.getvalue()