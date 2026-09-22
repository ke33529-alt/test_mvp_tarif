"""
core/usage_tracker.py
─────────────────────────────────────────────────────────────────────────────
Журнал использования функций — продуктовая аналитика РЕГУЛА.AI.

НАЗНАЧЕНИЕ
  Что пользователи реально делают в модулях: открывают разделы, запускают
  анализы и прогнозы, получают ответы, выгружают результаты. Отвечает на
  вопрос «чем пользуются и насколько успешно».

  Безопасность и администрирование (вход/выход, пользователи, сегменты,
  права, базы знаний) сюда НЕ пишутся — это core/audit.py
  (вкладка «Аудит системы»).

ХРАНЕНИЕ
  data/usage_log/YYYY-MM.jsonl — одна строка = одно событие.
  Время — московское (UTC+3), без зоны в строке.

ФОРМАТ ЗАПИСИ
  {"ts": "...", "module": "predictor", "action": "prediction_completed",
   "user_id": "...", "org": "...", "meta": {...}}

ПОДКЛЮЧЕНИЕ В МОДУЛЕ
  try:
      from core.usage_tracker import log_event as _log_usage
  except Exception:
      def _log_usage(*a, **kw): pass

  _log_usage("advisor", "query_submitted", meta={"query_len": len(q)})

  user_id и org берутся из st.session_state["_auth_user"] автоматически.
  Тексты запросов и ответов в журнал не пишутся — только факты и размеры.

СОБЫТИЯ ПО МОДУЛЯМ — см. MODULE_ACTIONS ниже.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# =============================================================================
# Константы
# =============================================================================
_BASE_DIR = Path(__file__).parent.parent.resolve()
_LOG_DIR  = str(_BASE_DIR / "data" / "usage_log")
_LOG_LOCK = threading.Lock()

# Москва без перехода на летнее время — фиксированное смещение
_MSK = timezone(timedelta(hours=3))


def _now() -> datetime:
    """Московское время без tzinfo (совместимо со старыми записями)."""
    return datetime.now(_MSK).replace(tzinfo=None)


# Человекочитаемые названия модулей (для UI)
MODULE_LABELS: Dict[str, str] = {
    "advisor":        "Советчик",
    "claim_analyzer": "Анализатор заявок",
    "predictor":      "Прогнозист",
    "doc_scanner":    "Сканер документов",
    "protocol":       "Протокольщик",
    "tasks":          "Задачник",
}

# Идентификаторы модулей в настройках сегмента (segments.json, app.py)
# отличаются от идентификаторов журнала — сопоставление в обе стороны.
SEGMENT_TO_USAGE_MODULE: Dict[str, str] = {
    "advisor":   "advisor",
    "scanner":   "doc_scanner",
    "analyzer":  "claim_analyzer",
    "predictor": "predictor",
    "protocol":  "protocol",
    "tasks":     "tasks",
}
USAGE_TO_SEGMENT_MODULE: Dict[str, str] = {v: k for k, v in SEGMENT_TO_USAGE_MODULE.items()}

# Человекочитаемые названия действий
ACTION_LABELS: Dict[str, str] = {
    # общее
    "module_open":             "Открыт модуль",
    # advisor
    "query_submitted":         "Запрос отправлен",
    "answer_generated":        "Ответ получен",
    "answer_streamed":         "Ответ получен",          # старое имя события
    "faq_matched":             "Ответ из FAQ",
    "cache_hit":               "Ответ из кэша",
    "answer_not_found":        "Ответ не найден",
    "clarification_submitted": "Уточняющий вопрос",
    "answer_rated":            "Ответ оценён",
    "letter_generated":        "Письмо сгенерировано",
    "source_opened":           "Источник НПА открыт",
    # claim_analyzer
    "analysis_started":        "Анализ запущен",
    "analysis_completed":      "Анализ завершён",
    "risk_detail_opened":      "Риск открыт",
    "report_downloaded":       "Отчёт скачан",
    "project_deleted":         "Заявка удалена",
    # predictor
    "prediction_started":      "Прогноз запущен",
    "prediction_completed":    "Прогноз завершён",
    "expert_override":         "Ручная правка вердикта",
    "source_viewed":           "Источник просмотрен",
    "doc_attached":            "Документ приложен",
    # doc_scanner
    "scan_started":            "Сканирование запущено",
    "scan_completed":          "Сканирование завершено",
    "summary_generated":       "Пересказ сгенерирован",
    "document_exported":       "Документ экспортирован",
    "document_deleted":        "Документ удалён",
    # protocol
    "transcription_started":   "Транскрипция запущена",
    "transcription_completed": "Транскрипция завершена",
    "protocol_generated":      "Протокол сгенерирован",
    "protocol_exported":       "Протокол скачан",
    "protocol_deleted":        "Протокол удалён",
    # tasks
    "task_created":            "Задача создана",
    "task_edited":             "Задача изменена",
    "task_completed":          "Задача выполнена",
    "task_status_changed":     "Статус изменён",
    "task_deleted":            "Задача удалена",
    "task_viewed":             "Задача просмотрена",
}

# Какие действия возможны в каком модуле — для зависимого фильтра в UI
MODULE_ACTIONS: Dict[str, List[str]] = {
    "advisor": [
        "module_open", "query_submitted", "answer_generated", "faq_matched",
        "answer_not_found", "clarification_submitted", "answer_rated",
    ],
    "claim_analyzer": [
        "module_open", "analysis_started", "analysis_completed", "project_deleted",
    ],
    "predictor": [
        "module_open", "prediction_started", "prediction_completed",
        "doc_attached", "source_viewed", "expert_override",
    ],
    "doc_scanner": [
        "module_open", "scan_started", "scan_completed", "summary_generated",
        "document_exported", "document_deleted",
    ],
    "protocol": [
        "module_open", "transcription_started", "transcription_completed",
        "protocol_generated", "protocol_exported", "protocol_deleted",
    ],
    "tasks": [
        "module_open", "task_created", "task_edited", "task_status_changed",
        "task_completed", "task_deleted",
    ],
}

# Воронка: (события старта, события успешного результата)
FUNNEL: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "advisor":        (("query_submitted",),       ("answer_generated", "answer_streamed", "faq_matched")),
    "claim_analyzer": (("analysis_started",),      ("analysis_completed",)),
    "predictor":      (("prediction_started",),    ("prediction_completed",)),
    "doc_scanner":    (("scan_started",),          ("scan_completed",)),
    "protocol":       (("transcription_started",), ("transcription_completed",)),
    "tasks":          (("task_created",),          ("task_completed",)),
}

# Служебные действия, которые не считаются «работой» в модуле
_NON_WORK_ACTIONS = {"module_open"}

# Значения, которые пишутся английскими ключами — перевод для колонки «Детали»
_VALUE_LABELS: Dict[str, str] = {
    "low": "низкий", "medium": "средний", "high": "высокий",
    "todo": "сделать", "in_progress": "в работе", "done": "выполнено",
    "positive": "за", "negative": "против", "neutral": "нейтр.",
    "full": "полный", "quick": "быстрый",
    "expertise": "экспертизы", "protocols": "протоколы",
}


def _v(val: Any) -> str:
    return _VALUE_LABELS.get(str(val), str(val))


# =============================================================================
# Вспомогательные функции
# =============================================================================
def _ensure_log_dir() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)


def _current_log_path() -> str:
    return os.path.join(_LOG_DIR, _now().strftime("%Y-%m") + ".jsonl")


def _session_context() -> Tuple[str, str]:
    """user_id и org_id из st.session_state["_auth_user"] (ключ core/auth.py)."""
    try:
        import streamlit as st
        user = st.session_state.get("_auth_user") or {}
        return str(user.get("user_id") or ""), str(user.get("org_id") or "")
    except Exception:
        return "", ""


# =============================================================================
# Запись
# =============================================================================
def log_event(
    module: str,
    action: str,
    user_id: str = "",
    org: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Регистрирует одно событие использования. Ошибки не бросаются —
    трекер не должен ломать работу модуля.
    """
    try:
        _ensure_log_dir()
        if not user_id and not org:
            user_id, org = _session_context()
        record = {
            "ts":      _now().isoformat(),
            "module":  module,
            "action":  action,
            "user_id": user_id,
            "org":     org,
            "meta":    meta or {},
        }
        with _LOG_LOCK:
            with open(_current_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[USAGE_TRACKER] Ошибка записи события: {e}", flush=True)


def log_module_open(segment_module_id: str) -> None:
    """
    Фиксирует переход в модуль. Принимает id модуля из настроек сегмента
    (advisor, scanner, analyzer, ...). Повторный rerun того же модуля
    в одной сессии не пишется — только смена раздела.
    """
    try:
        import streamlit as st
        if st.session_state.get("_usage_last_module") == segment_module_id:
            return
        st.session_state["_usage_last_module"] = segment_module_id
    except Exception:
        pass
    mod = SEGMENT_TO_USAGE_MODULE.get(segment_module_id, segment_module_id)
    log_event(mod, "module_open")


# =============================================================================
# Чтение
# =============================================================================
def _load_log_by_dates(date_from_str: str, date_to_str: str) -> List[Dict]:
    """События за диапазон дат (включительно) из JSONL-файлов."""
    _ensure_log_dir()
    try:
        dt_from = datetime.strptime(date_from_str, "%Y-%m-%d")
        dt_to   = datetime.strptime(date_to_str, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
    except ValueError:
        return []

    records: List[Dict] = []
    try:
        files = sorted(f for f in os.listdir(_LOG_DIR) if f.endswith(".jsonl"))
    except Exception:
        return []

    for fname in files:
        try:
            file_ym = datetime.strptime(fname[:7], "%Y-%m")
            next_month = (file_ym.replace(day=28) + timedelta(days=4)).replace(day=1)
            if next_month - timedelta(seconds=1) < dt_from or file_ym > dt_to:
                continue
        except ValueError:
            continue

        path = os.path.join(_LOG_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts_str = rec.get("ts", "")
                        if not ts_str:
                            continue
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is not None:
                            ts = ts.astimezone(_MSK).replace(tzinfo=None)
                        if dt_from <= ts <= dt_to:
                            records.append(rec)
                    except Exception:
                        pass
        except Exception:
            pass

    return records


def read_usage(
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    org_id:    Optional[str] = None,
    days:      int = 30,
) -> List[Dict]:
    """События за период (по умолчанию — последние `days` дней), опц. по сегменту."""
    if not date_to:
        date_to = _now().strftime("%Y-%m-%d")
    if not date_from:
        date_from = (_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    records = _load_log_by_dates(date_from, date_to)
    if org_id:
        records = [r for r in records if r.get("org") == org_id]
    return records


# =============================================================================
# Агрегация
# =============================================================================
def get_module_stats(
    org_id:    Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
) -> List[Dict]:
    """
    Рабочие действия по пользователям и модулям (без открытий разделов):
    [{"user_id", "org_id", "modules": {usage_module: n}, "opens": n,
      "total": n, "last_ts": "..."}], по убыванию total.
    """
    records = read_usage(date_from=date_from, date_to=date_to, org_id=org_id)

    stats: Dict[str, Dict] = {}
    for rec in records:
        uid = rec.get("user_id") or ""
        if not uid:
            continue
        row = stats.setdefault(uid, {
            "user_id": uid, "org_id": rec.get("org", ""),
            "modules": {}, "opens": 0, "total": 0, "last_ts": "",
        })
        ts = rec.get("ts", "")
        if ts > row["last_ts"]:
            row["last_ts"] = ts
        if rec.get("action") in _NON_WORK_ACTIONS:
            row["opens"] += 1
            continue
        mod = rec.get("module", "unknown")
        row["modules"][mod] = row["modules"].get(mod, 0) + 1
        row["total"] += 1

    return sorted(stats.values(), key=lambda x: x["total"], reverse=True)


def get_funnel_stats(
    org_id:    Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
) -> List[Dict]:
    """
    Воронка по модулям: [{"module", "users", "started", "completed",
    "conversion", "actions"}] в порядке MODULE_LABELS.
    """
    records = read_usage(date_from=date_from, date_to=date_to, org_id=org_id)

    users: Dict[str, set] = defaultdict(set)
    actions: Counter = Counter()
    started: Counter = Counter()
    completed: Counter = Counter()

    for rec in records:
        mod = rec.get("module", "")
        act = rec.get("action", "")
        if act in _NON_WORK_ACTIONS:
            continue
        actions[mod] += 1
        if rec.get("user_id"):
            users[mod].add(rec["user_id"])
        if mod in FUNNEL:
            st_events, done_events = FUNNEL[mod]
            if act in st_events:
                started[mod] += 1
            if act in done_events:
                completed[mod] += 1

    result = []
    for mod in MODULE_LABELS:
        s, c = started[mod], completed[mod]
        result.append({
            "module":     mod,
            "users":      len(users[mod]),
            "actions":    actions[mod],
            "started":    s,
            "completed":  c,
            "conversion": (min(c, s) / s * 100) if s else None,
        })
    return result


def get_segment_module_counts(org_id: str, days: int = 30) -> Dict[str, int]:
    """
    Рабочие действия за N дней по модулям сегмента — ключи в формате
    segments.json (advisor, scanner, analyzer, ...). Для карточки сегмента.
    """
    counts: Dict[str, int] = {mid: 0 for mid in SEGMENT_TO_USAGE_MODULE}
    for rec in read_usage(org_id=org_id, days=days):
        if rec.get("action") in _NON_WORK_ACTIONS:
            continue
        mid = USAGE_TO_SEGMENT_MODULE.get(rec.get("module", ""))
        if mid in counts:
            counts[mid] += 1
    return counts


def get_daily_usage(date: Optional[str] = None) -> Dict:
    """Сводка за день: число рабочих действий и активных пользователей."""
    if not date:
        date = _now().strftime("%Y-%m-%d")
    records = _load_log_by_dates(date, date)
    work = [r for r in records if r.get("action") not in _NON_WORK_ACTIONS]
    return {
        "date":         date,
        "actions":      len(work),
        "active_users": len({r.get("user_id") for r in records if r.get("user_id")}),
    }


# =============================================================================
# Форматирование
# =============================================================================
def _badge(label: str, bg: str, color: str) -> str:
    return (
        f'<span style="font-size:11px;padding:2px 8px;border-radius:20px;'
        f'font-weight:500;background:{bg};color:{color}">{label}</span>'
    )


_MODULE_BADGE_COLORS: Dict[str, Tuple[str, str]] = {
    "advisor":        ("#E6F1FB", "#0C447C"),
    "claim_analyzer": ("#FAEEDA", "#633806"),
    "predictor":      ("#EEEDFE", "#3C3489"),
    "doc_scanner":    ("#EAF3DE", "#27500A"),
    "protocol":       ("#FAECE7", "#712B13"),
    "tasks":          ("#F1EFE8", "#5F5E5A"),
}

_ACTION_BADGE_COLORS: Dict[str, Tuple[str, str]] = {
    "answer_generated":        ("#E6F1FB", "#0C447C"),
    "answer_streamed":         ("#E6F1FB", "#0C447C"),
    "faq_matched":             ("#E6F1FB", "#0C447C"),
    "prediction_completed":    ("#EEEDFE", "#3C3489"),
    "analysis_completed":      ("#FAEEDA", "#633806"),
    "scan_completed":          ("#EAF3DE", "#27500A"),
    "transcription_completed": ("#FAECE7", "#712B13"),
    "protocol_generated":      ("#FAECE7", "#712B13"),
    "task_completed":          ("#EAF3DE", "#27500A"),
    "expert_override":         ("#FFF3CD", "#856404"),
    "answer_not_found":        ("#FCE8E6", "#8A1C12"),
    "module_open":             ("#FFFFFF", "#9AA5B1"),
}
_ACTION_BADGE_DEFAULT = ("#F1EFE8", "#5F5E5A")

_RATING_LABELS = {3: "полезно", 2: "нормально", 1: "не помогло"}


def _format_meta(action: str, meta: Dict) -> str:
    """Форматирует meta в читаемую строку для колонки «Детали»."""
    if not meta:
        return "—"

    if meta.get("error"):
        err = f"ошибка: {str(meta['error'])[:60]}"
    else:
        err = ""

    def _join(parts: List[str]) -> str:
        parts = [p for p in parts if p]
        if err:
            parts.append(err)
        return " · ".join(parts) or "—"

    if action == "prediction_started":
        return _join([
            str(meta.get("article", ""))[:40],
            f"top-{meta['top_k']}" if meta.get("top_k") else "",
            ", ".join(_v(s) for s in meta.get("sources", []) or []),
            f"документов: {meta['n_docs']}" if meta.get("n_docs") else "",
        ])

    if action == "prediction_completed":
        score = meta.get("score_pct")
        return _join([
            str(meta.get("article", ""))[:30],
            f"score {score:.0f}%" if isinstance(score, (int, float)) else "",
            f"за:{meta.get('n_positive', 0)} против:{meta.get('n_negative', 0)} "
            f"нейтр:{meta.get('n_neutral', 0)}",
        ])

    if action == "expert_override":
        fr = _v(meta.get("from_decision", ""))
        to = _v(meta.get("to_decision", ""))
        article = str(meta.get("article", ""))[:30]
        return f"{article} · {fr} → {to}" if article else f"{fr} → {to}"

    if action == "source_viewed":
        dec = _v(meta.get("decision", "")) if meta.get("decision") else ""
        return str(meta.get("file", ""))[:50] + (f" ({dec})" if dec else "")

    if action == "doc_attached":
        ok = meta.get("ok")
        return _join([
            str(meta.get("filename", ""))[:50],
            f"{meta['chars']} симв." if ok and meta.get("chars") else "",
            "не прочитан" if ok is False and not meta.get("error") else "",
        ])

    if action == "query_submitted":
        return _join([
            f"{meta['query_len']} симв." if meta.get("query_len") else "",
            "служебный режим" if meta.get("internal_mode") else "",
            f"фильтр НПА: {meta['npa_filter']}" if meta.get("npa_filter") else "",
        ])

    if action in ("answer_generated", "answer_streamed"):
        return _join([
            f"источников: {meta.get('sources', meta.get('num_sources'))}"
            if meta.get("sources", meta.get("num_sources")) is not None else "",
            f"{meta['duration_sec']} сек" if meta.get("duration_sec") else "",
            "локальная база" if meta.get("local_kb") else "",
            "служебный режим" if meta.get("internal_mode") else "",
        ])

    if action == "faq_matched":
        return _join([f"{meta['duration_sec']} сек" if meta.get("duration_sec") else ""])

    if action == "answer_not_found":
        return _join([f"фильтр НПА: {meta['npa_filter']}" if meta.get("npa_filter") else ""])

    if action == "clarification_submitted":
        return _join([
            f"уточнение №{meta['n']}" if meta.get("n") else "",
            f"источников: {meta['sources']}" if meta.get("sources") is not None else "",
        ])

    if action == "answer_rated":
        return _RATING_LABELS.get(meta.get("rating"), str(meta.get("rating", "—")))

    if action == "analysis_started":
        return _join([
            _v(meta.get("mode", "")) if meta.get("mode") else "",
            f"файлов: {meta['file_count']}" if meta.get("file_count") is not None else "",
            f"статей: {meta['article_count']}" if meta.get("article_count") else "",
        ])

    if action == "analysis_completed":
        return _join([
            f"высоких: {meta.get('n_high', meta.get('high', 0))}",
            f"средних: {meta.get('n_medium', meta.get('med', 0))}",
            f"статей: {meta['n_articles']}" if meta.get("n_articles") is not None else "",
        ])

    if action == "project_deleted":
        return _join([str(meta.get("org", ""))[:40], str(meta.get("period", ""))])

    if action == "scan_started":
        return _join([f"файлов: {meta.get('file_count', '—')}"])

    if action == "scan_completed":
        pages = meta.get("total_pages", meta.get("pages"))
        words = meta.get("total_words", meta.get("words"))
        return _join([f"{pages} стр." if pages else "", f"{words} слов" if words else ""])

    if action == "summary_generated":
        return _join([
            str(meta.get("filename", ""))[:30],
            f"{meta['pages']} стр." if meta.get("pages") else "",
        ])

    if action in ("document_exported", "document_deleted"):
        fmt = meta.get("format", "")
        return str(meta.get("filename", ""))[:40] + (f" ({fmt})" if fmt else "") or "—"

    if action == "transcription_started":
        return _join([
            str(meta.get("filename", ""))[:30],
            f"{meta['size_mb']} МБ" if meta.get("size_mb") else "",
        ])

    if action == "transcription_completed":
        return _join([
            f"{meta['elapsed_sec']} сек" if meta.get("elapsed_sec") else "",
            f"{meta['chars']} симв." if meta.get("chars") else "",
        ])

    if action == "protocol_generated":
        return _join([
            str(meta.get("source_type", "")),
            f"{meta['attendees']} участн." if meta.get("attendees") else "",
        ])

    if action in ("protocol_exported", "protocol_deleted"):
        return str(meta.get("meeting_name", ""))[:50] or "—"

    if action == "task_created":
        return _join([
            f"приоритет: {_v(meta.get('priority', '—'))}",
            "со сроком" if meta.get("has_due") else "",
        ])

    if action == "task_status_changed":
        fr, to = meta.get("from", ""), meta.get("to", "")
        return f"{_v(fr)} → {_v(to)}" if fr and to else "—"

    if action == "task_edited":
        parts = []
        if meta.get("status_from") != meta.get("status_to"):
            parts.append(f"статус: {_v(meta.get('status_from'))} → {_v(meta.get('status_to'))}")
        if meta.get("prio_from") != meta.get("prio_to"):
            parts.append(f"приоритет: {_v(meta.get('prio_from'))} → {_v(meta.get('prio_to'))}")
        return _join(parts)

    if action == "task_completed":
        return _join([
            "с комментарием" if meta.get("has_note") else "",
            f"ссылок: {meta['refs']}" if meta.get("refs") else "",
        ])

    if action in ("task_deleted", "module_open"):
        return "—"

    # Общий случай: первые 3 непустых поля
    parts = [f"{k}: {_v(v)}" for k, v in list(meta.items())[:3] if v not in (None, "", False, [])]
    return " · ".join(parts) or "—"


# =============================================================================
# Streamlit UI — вкладка «Использование функций» в Управлении
# =============================================================================
def show_usage_stats() -> None:
    """Журнал использования функций с фильтрами и экспортом."""
    import streamlit as st

    # Справочники пользователей и сегментов — для отображения имён
    _users: Dict[str, Dict] = {}
    _segments: Dict[str, Dict] = {}
    try:
        from core.auth import USERS_FILE, SEGMENTS_FILE
        _uf, _sf = Path(USERS_FILE), Path(SEGMENTS_FILE)
        if _uf.exists():
            _users = json.loads(_uf.read_text(encoding="utf-8"))
        if _sf.exists():
            _segments = json.loads(_sf.read_text(encoding="utf-8"))
    except Exception:
        pass

    def _resolve_user(uid: str) -> str:
        if uid == "superadmin":
            return "Суперадмин"
        if uid and uid in _users:
            return _users[uid].get("name", uid)
        return uid or "—"

    today = _now().date()

    # ── Фильтры: даты ─────────────────────────────────────────────────────────
    fc1, fc2 = st.columns(2)
    with fc1:
        date_from = st.date_input("С даты", value=today, key="usage_date_from")
    with fc2:
        date_to = st.date_input("По дату", value=today, key="usage_date_to")

    # ── Фильтры: сегмент, модуль, действие ───────────────────────────────────
    fc3, fc4, fc5 = st.columns(3)
    with fc3:
        seg_opts = {"": "Все сегменты"} | {k: v.get("name", k) for k, v in _segments.items()}
        filter_seg = st.selectbox(
            "Сегмент", options=list(seg_opts.keys()),
            format_func=lambda x: seg_opts[x],
            key="usage_filter_seg", label_visibility="collapsed",
        )
    with fc4:
        module_opts = {"": "Все модули"} | dict(MODULE_LABELS)
        filter_module = st.selectbox(
            "Модуль", options=list(module_opts.keys()),
            format_func=lambda x: module_opts[x],
            key="usage_filter_module", label_visibility="collapsed",
        )
    with fc5:
        # Список действий зависит от выбранного модуля
        if filter_module:
            _acts = MODULE_ACTIONS.get(filter_module, [])
        else:
            _acts = sorted({a for lst in MODULE_ACTIONS.values() for a in lst},
                           key=lambda a: ACTION_LABELS.get(a, a))
        action_opts = {"": "Все действия"} | {a: ACTION_LABELS.get(a, a) for a in _acts}
        if st.session_state.get("usage_filter_action") not in action_opts:
            st.session_state["usage_filter_action"] = ""
        filter_action = st.selectbox(
            "Действие", options=list(action_opts.keys()),
            format_func=lambda x: action_opts[x],
            key="usage_filter_action", label_visibility="collapsed",
        )

    show_opens = st.checkbox("Показывать открытия модулей", value=False,
                             key="usage_show_opens")

    # ── Загрузка и фильтрация ─────────────────────────────────────────────────
    try:
        records = _load_log_by_dates(str(date_from), str(date_to))
    except Exception:
        records = []

    if filter_seg:
        records = [r for r in records if r.get("org") == filter_seg]
    if filter_module:
        records = [r for r in records if r.get("module") == filter_module]
    if filter_action:
        records = [r for r in records if r.get("action") == filter_action]
    elif not show_opens:
        records = [r for r in records if r.get("action") not in _NON_WORK_ACTIONS]

    records = sorted(records, key=lambda r: r.get("ts", ""), reverse=True)
    display_records = records[:200]

    # ── Счётчик + экспорт ─────────────────────────────────────────────────────
    meta_col, export_col = st.columns([3, 1])
    with meta_col:
        suffix = f" из {len(records)} (показаны первые 200)" if len(records) > 200 else ""
        st.caption(f"Показано записей: {len(display_records)}{suffix}")
    with export_col:
        if records:
            raw_jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)

            def _on_export(n=len(records)):
                try:
                    from core.audit import audit
                    audit("log_exported", module="superadmin",
                          meta={"log": "usage", "records": n,
                                "date_from": str(date_from), "date_to": str(date_to)})
                except Exception:
                    pass

            st.download_button(
                "Экспорт JSONL",
                data=raw_jsonl.encode("utf-8"),
                file_name=f"usage_{date_from}_{date_to}.jsonl",
                mime="application/jsonl",
                key="usage_export_btn",
                on_click=_on_export,
            )

    if not display_records:
        st.info("Событий за выбранный период не найдено.")
        return

    # ── Таблица ───────────────────────────────────────────────────────────────
    widths = [1, 1, 2, 1.2, 3]
    hc = st.columns(widths)
    for col, label in zip(hc, ["Время", "Модуль", "Пользователь", "Действие", "Детали"]):
        col.markdown(
            f"<small style='color:#5a6a7a;font-weight:600'>{label}</small>",
            unsafe_allow_html=True,
        )
    st.divider()

    import html as _html
    for rec in display_records:
        ts     = rec.get("ts", "")[:16].replace("T", " ")
        module = rec.get("module", "")
        action = rec.get("action", "")
        meta   = rec.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {"значение": meta}

        org = rec.get("org", "")
        user_name = _resolve_user(rec.get("user_id", ""))
        seg_name  = _segments.get(org, {}).get("name", "") if org else ""

        mod_bg, mod_color = _MODULE_BADGE_COLORS.get(module, ("#F1EFE8", "#5F5E5A"))
        act_bg, act_color = _ACTION_BADGE_COLORS.get(action, _ACTION_BADGE_DEFAULT)

        rc = st.columns(widths)
        rc[0].markdown(f"<small style='color:#5a6a7a'>{ts}</small>", unsafe_allow_html=True)
        rc[1].markdown(_badge(MODULE_LABELS.get(module, module), mod_bg, mod_color),
                       unsafe_allow_html=True)
        rc[2].markdown(
            f"<small style='color:#333'>{_html.escape(user_name)}</small>"
            + (f"<br><small style='color:#9aa5b1'>{_html.escape(seg_name)}</small>" if seg_name else ""),
            unsafe_allow_html=True,
        )
        rc[3].markdown(_badge(ACTION_LABELS.get(action, action), act_bg, act_color),
                       unsafe_allow_html=True)
        rc[4].markdown(
            f"<small style='color:#5a6a7a'>{_html.escape(_format_meta(action, meta))}</small>",
            unsafe_allow_html=True,
        )