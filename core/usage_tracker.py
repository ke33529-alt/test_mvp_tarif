"""
core/usage_tracker.py
─────────────────────────────────────────────────────────────────────────────
Аудит-логгер событий пользователей для анализа воронки использования.

ХРАНЕНИЕ:
  data/usage_log/YYYY-MM.jsonl  — одна строка = одно JSON-событие.
  Ротация по месяцам, файлы накапливаются, старые не удаляются.

ПОДКЛЮЧЕНИЕ (в любом модуле):
  from core.usage_tracker import log_event

  log_event("advisor", "query_submitted", meta={"query_len": 120})
  log_event("predictor", "prediction_completed",
            meta={"score": 72, "n_positive": 5, "n_negative": 2})

  user_id и org считываются автоматически из Streamlit session_state
  (ключи "user_id" и "org_dir"). Можно передать явно при необходимости.

СОБЫТИЯ ПО МОДУЛЯМ:
  advisor         : query_submitted, answer_generated, letter_generated, source_opened
  claim_analyzer  : analysis_started, analysis_completed, risk_detail_opened, report_downloaded
  predictor       : prediction_started, prediction_completed, expert_override, source_viewed
  doc_scanner     : scan_started, scan_completed, summary_generated, document_exported
  protocol        : transcription_started, transcription_completed, protocol_generated, protocol_exported
  tasks           : task_created, task_completed, task_viewed

ADMIN UI:
  from core.usage_tracker import show_usage_stats
  show_usage_stats()    # вызвать внутри нужной вкладки Админки

─────────────────────────────────────────────────────────────────────────────
ИНТЕГРАЦИЯ В МОДУЛИ (минимальная — 2 строки в начало + вызовы по месту):

  # В начало файла (с защитой от ImportError):
  try:
      from core.usage_tracker import log_event as _log_usage
  except Exception:
      def _log_usage(*a, **kw): pass

  # Примеры вызовов:
  _log_usage("advisor", "query_submitted", meta={"query_len": len(q)})
  _log_usage("advisor", "answer_generated", meta={"sources": n_sources})
  _log_usage("advisor", "letter_generated")
  _log_usage("advisor", "source_opened", meta={"file": fname})

  _log_usage("claim_analyzer", "analysis_started", meta={"article_count": n})
  _log_usage("claim_analyzer", "analysis_completed", meta={"high": n_high, "med": n_med})
  _log_usage("claim_analyzer", "risk_detail_opened", meta={"risk_id": rid})
  _log_usage("claim_analyzer", "report_downloaded")

  _log_usage("doc_scanner", "scan_started", meta={"file_count": n})
  _log_usage("doc_scanner", "scan_completed", meta={"pages": p, "words": w})
  _log_usage("doc_scanner", "summary_generated")
  _log_usage("doc_scanner", "document_exported", meta={"file": fname})

  _log_usage("protocol", "transcription_started")
  _log_usage("protocol", "transcription_completed", meta={"words": w})
  _log_usage("protocol", "protocol_generated")
  _log_usage("protocol", "protocol_exported")

  _log_usage("tasks", "task_created", meta={"source_module": mod})
  _log_usage("tasks", "task_completed", meta={"task_id": tid})
  _log_usage("tasks", "task_viewed",   meta={"task_id": tid})
─────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# =============================================================================
# Константы
# =============================================================================
_LOG_DIR  = os.path.join("data", "usage_log")
_LOG_LOCK = threading.Lock()

# Человекочитаемые названия модулей (для UI)
MODULE_LABELS: Dict[str, str] = {
    "advisor":        "Советчик",
    "claim_analyzer": "Анализатор заявок",
    "predictor":      "Прогнозист",
    "doc_scanner":    "Сканер документов",
    "protocol":       "Протокольщик",
    "tasks":          "Задачник",
}

# Человекочитаемые названия действий
ACTION_LABELS: Dict[str, str] = {
    # advisor
    "query_submitted":         "Запрос отправлен",
    "answer_generated":        "Ответ получен",
    "answer_streamed":         "Ответ сгенерирован",
    "cache_hit":               "Ответ из кэша",
    "faq_matched":             "Ответ из FAQ",
    "clarification_submitted": "Уточняющий вопрос",
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
    "task_completed":          "Задача выполнена",
    "task_status_changed":     "Статус изменён",
    "task_deleted":            "Задача удалена",
}

# Воронка: (событие_старта, событие_финиша) — для расчёта конверсии
FUNNEL_PAIRS: Dict[str, Tuple[str, str]] = {
    "advisor":        ("query_submitted",       "answer_streamed"),
    "claim_analyzer": ("analysis_started",      "analysis_completed"),
    "predictor":      ("prediction_started",    "prediction_completed"),
    "doc_scanner":    ("scan_started",          "scan_completed"),
    "protocol":       ("transcription_started", "protocol_generated"),
    "tasks":          ("task_created",          "task_completed"),
}


# =============================================================================
# Вспомогательные функции
# =============================================================================
def _ensure_log_dir() -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)


def _current_log_path() -> str:
    return os.path.join(_LOG_DIR, datetime.now().strftime("%Y-%m") + ".jsonl")


def _session_context() -> Tuple[str, str]:
    """
    Читает user_id и org_id из st.session_state["_auth_user"] —
    тот же ключ что устанавливает core/auth.py после логина.
    Для суперадмина user_id = "superadmin", org_id = None/""
    """
    try:
        import streamlit as st
        user = st.session_state.get("_auth_user") or {}
        uid  = str(user.get("user_id") or "")
        org  = str(user.get("org_id")  or "")
        return uid, org
    except Exception:
        return "", ""


# =============================================================================
# Основная функция логирования
# =============================================================================
def log_event(
    module: str,
    action: str,
    user_id: str = "",
    org: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Регистрирует одно событие в лог-файл текущего месяца.

    user_id и org автоматически читаются из Streamlit session_state
    если не переданы явно. Ошибки записи не бросают исключений —
    трекер не должен ломать работу модуля.

    Пример:
        log_event("predictor", "prediction_started",
                  meta={"article": "Амортизация", "top_k": 30})
    """
    try:
        _ensure_log_dir()
        if not user_id and not org:
            user_id, org = _session_context()
        record = {
            "ts":      datetime.now().isoformat(),
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


# =============================================================================
# Чтение и агрегация
# =============================================================================
def _load_log_files(days: int = 30) -> List[Dict]:
    """Загружает события за последние `days` дней из всех файлов лога."""
    _ensure_log_dir()
    cutoff = datetime.now() - timedelta(days=days)
    records: List[Dict] = []

    try:
        files = sorted(
            [f for f in os.listdir(_LOG_DIR) if f.endswith(".jsonl")],
            reverse=True,
        )
    except Exception:
        return []

    for fname in files:
        # Быстрая отсечка по имени файла.
        # ВАЖНО: сравниваем только год-месяц без времени суток —
        # иначе cutoff.replace(day=1) оставляет ненулевое время, и файл
        # текущего/недавнего месяца ложно пропускается.
        try:
            file_month = datetime.strptime(fname[:7], "%Y-%m")
            cutoff_month = cutoff.replace(day=1, hour=0, minute=0,
                                          second=0, microsecond=0)
            if file_month < cutoff_month:
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
                        ts = datetime.fromisoformat(rec.get("ts", ""))
                        if ts >= cutoff:
                            records.append(rec)
                    except Exception:
                        pass
        except Exception:
            pass

    return records


def _aggregate(records: List[Dict]) -> Dict:
    """Агрегирует список событий в структуру для показа в UI."""
    from collections import Counter, defaultdict

    by_module: Counter = Counter()
    by_day:    Counter = Counter()
    by_user:   Counter = Counter()
    module_action: Dict = defaultdict(Counter)

    for rec in records:
        mod    = rec.get("module", "unknown")
        action = rec.get("action", "unknown")
        ts     = rec.get("ts", "")[:10]  # YYYY-MM-DD
        uid    = rec.get("user_id", "") or rec.get("org", "") or "аноним"

        by_module[mod] += 1
        by_day[ts] += 1
        by_user[uid] += 1
        module_action[mod][action] += 1

    return {
        "total":        len(records),
        "by_module":    dict(by_module),
        "by_day":       dict(sorted(by_day.items())),
        "by_user":      dict(by_user.most_common(20)),
        "module_action": {k: dict(v) for k, v in module_action.items()},
    }


# =============================================================================
# Streamlit UI — показывается в Админке
# =============================================================================
def _badge(label: str, bg: str, color: str) -> str:
    """Цветной бейдж — тот же стиль что в superadmin.py."""
    return (
        f'<span style="font-size:11px;padding:2px 8px;border-radius:20px;'
        f'font-weight:500;background:{bg};color:{color}">{label}</span>'
    )


# Цвета бейджей модулей
_MODULE_BADGE_COLORS: Dict[str, Tuple[str, str]] = {
    "advisor":        ("#E6F1FB", "#0C447C"),   # синий
    "claim_analyzer": ("#FAEEDA", "#633806"),   # оранжевый
    "predictor":      ("#EEEDFE", "#3C3489"),   # фиолетовый
    "doc_scanner":    ("#EAF3DE", "#27500A"),   # зелёный
    "protocol":       ("#FAECE7", "#712B13"),   # красный
    "tasks":          ("#F1EFE8", "#5F5E5A"),   # серый
}

# Цвета бейджей действий (нейтральные — акцент на модуле)
_ACTION_BADGE_COLORS: Dict[str, Tuple[str, str]] = {
    # завершающие события — насыщеннее
    "answer_generated":        ("#E6F1FB", "#0C447C"),
    "prediction_completed":    ("#EEEDFE", "#3C3489"),
    "analysis_completed":      ("#FAEEDA", "#633806"),
    "scan_completed":          ("#EAF3DE", "#27500A"),
    "protocol_generated":      ("#FAECE7", "#712B13"),
    "task_completed":          ("#EAF3DE", "#27500A"),
    # ручные правки — выделяем
    "expert_override":         ("#FFF3CD", "#856404"),
}
_ACTION_BADGE_DEFAULT = ("#F1EFE8", "#5F5E5A")

_DECISION_RU_SHORT = {"positive": "за", "negative": "против", "neutral": "нейтр."}


def _format_meta(action: str, meta: Dict) -> str:
    """Форматирует meta в читаемую строку для колонки Детали."""
    if not meta:
        return "—"

    if action == "prediction_started":
        parts = []
        if meta.get("article"):
            parts.append(meta["article"][:40])
        if meta.get("top_k"):
            parts.append(f"top-{meta['top_k']}")
        if meta.get("sources"):
            parts.append(", ".join(meta["sources"]))
        return " · ".join(parts) or "—"

    if action == "prediction_completed":
        score = meta.get("score_pct")
        n_pos = meta.get("n_positive", 0)
        n_neg = meta.get("n_negative", 0)
        n_neu = meta.get("n_neutral", 0)
        parts = []
        if meta.get("article"):
            parts.append(meta["article"][:30])
        if score is not None:
            parts.append(f"score {score:.0f}%")
        parts.append(f"за:{n_pos} против:{n_neg} нейтр:{n_neu}")
        if meta.get("error"):
            parts.append(f"ошибка: {meta['error'][:40]}")
        return " · ".join(parts)

    if action == "expert_override":
        fr = _DECISION_RU_SHORT.get(meta.get("from_decision", ""), meta.get("from_decision", ""))
        to = _DECISION_RU_SHORT.get(meta.get("to_decision", ""),   meta.get("to_decision", ""))
        article = meta.get("article", "")[:30]
        return f"{article} · {fr} → {to}" if article else f"{fr} → {to}"

    if action == "source_viewed":
        fname = meta.get("file", "")
        dec   = _DECISION_RU_SHORT.get(meta.get("decision", ""), "")
        return f"{fname[:50]}" + (f" ({dec})" if dec else "")

    if action == "query_submitted":
        parts = []
        if meta.get("query_len"):
            parts.append(f"{meta['query_len']} симв.")
        return " · ".join(parts) or "—"

    if action == "answer_generated":
        parts = []
        if meta.get("sources") is not None:
            parts.append(f"источников: {meta['sources']}")
        return " · ".join(parts) or "—"

    if action == "analysis_started":
        return f"статей: {meta.get('article_count', '—')}"

    if action == "analysis_completed":
        return (f"высоких: {meta.get('n_high', meta.get('high', 0))} · "
                f"средних: {meta.get('n_medium', meta.get('med', 0))} · "
                f"статей: {meta.get('n_articles', '—')}")

    if action == "scan_started":
        return f"файлов: {meta.get('file_count', '—')}"

    if action == "scan_completed":
        parts = []
        if meta.get("total_pages") or meta.get("pages"):
            parts.append(f"{meta.get('total_pages', meta.get('pages', '?'))} стр.")
        if meta.get("total_words") or meta.get("words"):
            parts.append(f"{meta.get('total_words', meta.get('words', '?'))} слов")
        return " · ".join(parts) or "—"

    if action == "summary_generated":
        parts = []
        if meta.get("filename"):
            parts.append(meta["filename"][:30])
        if meta.get("pages"):
            parts.append(f"{meta['pages']} стр.")
        return " · ".join(parts) or "—"

    if action == "document_exported":
        fname = meta.get("filename", "")
        fmt   = meta.get("format", "")
        return f"{fname[:40]}" + (f" ({fmt})" if fmt else "")

    if action == "transcription_started":
        parts = []
        if meta.get("filename"):
            parts.append(meta["filename"][:30])
        if meta.get("size_mb"):
            parts.append(f"{meta['size_mb']} МБ")
        return " · ".join(parts) or "—"

    if action == "transcription_completed":
        parts = []
        if meta.get("elapsed_sec"):
            parts.append(f"{meta['elapsed_sec']} сек")
        if meta.get("chars"):
            parts.append(f"{meta['chars']} симв.")
        return " · ".join(parts) or "—"

    if action == "protocol_generated":
        parts = []
        if meta.get("source_type"):
            parts.append(meta["source_type"])
        if meta.get("attendees"):
            parts.append(f"{meta['attendees']} участн.")
        return " · ".join(parts) or "—"

    if action == "protocol_exported":
        return meta.get("meeting_name", "")[:50] or "—"

    if action == "task_created":
        return f"приоритет: {meta.get('priority', '—')}"

    if action == "task_status_changed":
        fr = meta.get("from", "")
        to = meta.get("to", "")
        return f"{fr} → {to}" if fr and to else "—"

    # Общий случай: первые 3 поля meta
    parts = [f"{k}: {v}" for k, v in list(meta.items())[:3] if v not in (None, "", False)]
    return " · ".join(str(p) for p in parts) or "—"


def _load_log_by_dates(date_from_str: str, date_to_str: str) -> List[Dict]:
    """
    Загружает события за диапазон дат (включительно) напрямую из JSONL-файлов.
    Не зависит от _load_log_files — читает сам, с try/except на каждой строке.
    """
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
        # Быстрая отсечка: если месяц файла точно вне диапазона — пропускаем
        try:
            file_ym = datetime.strptime(fname[:7], "%Y-%m")
            # конец месяца файла (последняя секунда)
            next_month = (file_ym.replace(day=28) + timedelta(days=4)).replace(day=1)
            file_month_end = next_month - timedelta(seconds=1)
            if file_month_end < dt_from or file_ym > dt_to:
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
                        if dt_from <= ts <= dt_to:
                            records.append(rec)
                    except Exception:
                        pass
        except Exception:
            pass

    return records


def show_usage_stats() -> None:
    """
    Отображает лог использования функций в стиле аудит-лога.
    Вызов из Админки:

        from core.usage_tracker import show_usage_stats
        show_usage_stats()
    """
    import streamlit as st
    from datetime import date as date_type

    # ── Загрузка справочников пользователей и сегментов ──────────────────────
    # Используем те же данные что и аудит-лог — для отображения имён
    _users: Dict[str, Dict] = {}
    _segments: Dict[str, Dict] = {}
    try:
        from core.auth import USERS_FILE, SEGMENTS_FILE
        import json as _json
        from pathlib import Path as _Path
        _uf = _Path(USERS_FILE)
        _sf = _Path(SEGMENTS_FILE)
        if _uf.exists():
            _users = _json.loads(_uf.read_text(encoding="utf-8"))
        if _sf.exists():
            _segments = _json.loads(_sf.read_text(encoding="utf-8"))
    except Exception:
        pass

    def _resolve_user(uid: str, org: str) -> str:
        if uid and uid in _users:
            name = _users[uid].get("name", uid)
            seg  = _segments.get(org, {}).get("name", "") if org else ""
            return f"{name}" + (f" · {seg}" if seg else "")
        if org and org in _segments:
            return _segments[org].get("name", org)
        return uid or org or "суперадмин"

    # ── Строка 1: фильтры по дате ─────────────────────────────────────────────
    fc1, fc2 = st.columns(2)
    with fc1:
        date_from = st.date_input(
            "С даты",
            value=date_type.today(),
            key="usage_date_from",
        )
    with fc2:
        date_to = st.date_input(
            "По дату",
            value=date_type.today(),
            key="usage_date_to",
        )

    # ── Строка 2: фильтры по модулю и действию ───────────────────────────────
    fc3, fc4 = st.columns(2)
    with fc3:
        module_opts = {"": "Все модули"} | {
            k: v for k, v in MODULE_LABELS.items()
        }
        filter_module = st.selectbox(
            "Модуль",
            options=list(module_opts.keys()),
            format_func=lambda x: module_opts[x],
            key="usage_filter_module",
            label_visibility="collapsed",
        )
    with fc4:
        action_opts = {"": "Все действия"} | {
            k: v for k, v in sorted(ACTION_LABELS.items(), key=lambda x: x[1])
        }
        filter_action = st.selectbox(
            "Действие",
            options=list(action_opts.keys()),
            format_func=lambda x: action_opts[x],
            key="usage_filter_action",
            label_visibility="collapsed",
        )

    # ── Загрузка и фильтрация ─────────────────────────────────────────────────
    try:
        records = _load_log_by_dates(str(date_from), str(date_to))
    except Exception:
        records = []

    if filter_module:
        records = [r for r in records if r.get("module") == filter_module]
    if filter_action:
        records = [r for r in records if r.get("action") == filter_action]

    # Сортировка: новые сверху
    records = sorted(records, key=lambda r: r.get("ts", ""), reverse=True)
    display_records = records[:200]

    # ── Счётчик + экспорт ─────────────────────────────────────────────────────
    meta_col, export_col = st.columns([3, 1])
    with meta_col:
        suffix = f" (показаны первые 200)" if len(records) > 200 else ""
        st.caption(f"Показано записей: {len(display_records)}{suffix}")
    with export_col:
        if records:
            raw_jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
            st.download_button(
                "Экспорт JSONL",
                data=raw_jsonl.encode("utf-8"),
                file_name=f"usage_{date_from}_{date_to}.jsonl",
                mime="application/jsonl",
                key="usage_export_btn",
            )

    if not display_records:
        st.info(
            "Событий за выбранный период не найдено. "
            "Убедитесь что usage_tracker подключён в модулях и "
            "пользователи выполняли действия в системе."
        )
        return

    # ── Заголовок таблицы ─────────────────────────────────────────────────────
    hc = st.columns([1, 1, 2, 1, 3])
    for col, label in zip(hc, ["Время", "Модуль", "Пользователь", "Действие", "Детали"]):
        col.markdown(
            f"<small style='color:#5a6a7a;font-weight:600'>{label}</small>",
            unsafe_allow_html=True,
        )
    st.divider()

    # ── Строки таблицы ────────────────────────────────────────────────────────
    for rec in display_records:
        ts     = rec.get("ts", "")[:16].replace("T", " ")
        module = rec.get("module", "")
        action = rec.get("action", "")
        uid    = rec.get("user_id", "") or rec.get("org", "") or "—"
        meta   = rec.get("meta", {})

        mod_label = MODULE_LABELS.get(module, module)
        act_label = ACTION_LABELS.get(action, action)
        details   = _format_meta(action, meta)
        user_name = _resolve_user(uid, rec.get("org", ""))

        mod_bg, mod_color = _MODULE_BADGE_COLORS.get(module, ("#F1EFE8", "#5F5E5A"))
        act_bg, act_color = _ACTION_BADGE_COLORS.get(action, _ACTION_BADGE_DEFAULT)

        mod_badge = _badge(mod_label, mod_bg, mod_color)
        act_badge = _badge(act_label, act_bg, act_color)

        rc = st.columns([1, 1, 2, 1, 3])
        rc[0].markdown(
            f"<small style='color:#5a6a7a'>{ts}</small>",
            unsafe_allow_html=True,
        )
        rc[1].markdown(mod_badge, unsafe_allow_html=True)
        rc[2].markdown(
            f"<small style='color:#333'>{user_name}</small>",
            unsafe_allow_html=True,
        )
        rc[3].markdown(act_badge, unsafe_allow_html=True)
        rc[4].markdown(
            f"<small style='color:#5a6a7a'>{details}</small>",
            unsafe_allow_html=True,
        )