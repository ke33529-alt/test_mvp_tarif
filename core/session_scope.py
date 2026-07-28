# core/session_scope.py
"""
Изоляция session_state между пользователями в рамках одной браузерной сессии.

ПРОБЛЕМА
────────
st.session_state изолирован по браузерной сессии, но НЕ по личности.
core/auth.py хранит токен в URL (?s=<token>), а данные — в session_state.
При логауте чистится только "_auth_user" и токен; advisor_history,
clarifications, last_result, _adv_user_context и всё остальное остаётся
и показывается следующему, кто войдёт в этой же вкладке.

РЕШЕНИЕ
───────
enforce_identity_boundary() вызывается в начале рендера защищённой страницы,
сравнивает текущую личность (user_id + org_id) с привязанной к session_state
и при расхождении вычищает всё персональное. Логаут через logout() тоже
покрывается: следующий вход — уже другая личность → вайп.

ВТОРОЙ СЛОЙ
───────────
history_add() штампует запись user_id/org_id, history_get() фильтрует по
текущей личности. Даже если границу где-то забыли проверить, чужая запись
в выдачу не попадёт.

ЧТО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ
─────────────────────────
Не пишет на диск. Сессионная история живёт только в текущей вкладке.
Персистентная история — core/advisor_history.py (файл на пользователя).
Кэш LLM — core/advisor.py (неймспейс в ключе).
"""

from __future__ import annotations

from typing import Any, Dict, List

from core.advisor import get_identity

# =============================================================================
# Персональные ключи session_state
#
# Всё перечисленное вычищается при смене личности. Забытый ключ = утечка,
# поэтому список намеренно избыточен: лишний сброс стоит одного лишнего
# клика, пропущенный ключ — чужих данных на экране.
#
# КРИТИЧНО: "_auth_user" сюда добавлять НЕЛЬЗЯ — это сама личность,
# её удаление на каждом рендере разлогинит пользователя.
# =============================================================================
SCOPED_KEYS: List[str] = [
    # Советчик — результат и история
    "advisor_history",
    "clarifications",
    "last_query",
    "last_result",
    "search_triggered",
    "query_times",
    "_answer_streamed",
    "_adv_hist_id",
    "messages",
    # Советчик — настройки (персональные, см. core/user_prefs.py)
    "sources_only_mode",
    "advisor_model",
    "neighbor_radius",
    # Анализатор заявок
    "claim_files",
    "claim_result",
    "claim_project_id",
    # Прогнозист решений
    "pred_result",
    "pred_running",
    "pred_doc_text",
    # Сканер документов
    "scanner_db",
    "last_scanned_ids",
    "scan_sel_id",
    # Протокольщик
    "protocol_result",
    "protocol_transcript",
    # Навигация — сбрасываем на лендинг: у новой личности другой набор модулей
    "main_choice",
    "show_landing",
    "_dev_dialog_confirmed",
]

# Префиксы динамических ключей — точное имя заранее неизвестно.
# _adv_*      — кэш настроек советчика в session_state
# summary_*   — пересказы документов в сканере (summary_{doc_id})
# temp_pass_* — временные пароли в панели управления
SCOPED_PREFIXES = (
    "_adv_",
    "summary_",
    "temp_pass_",
    "reg_confirm_del_",
    "confirm_archive_",
)

_BOUND_KEY = "_bound_identity"


def _ss():
    """st.session_state или None вне Streamlit-процесса."""
    try:
        import streamlit as st
        return st.session_state
    except Exception:
        return None


def current_identity() -> Dict[str, str]:
    """{"user_id", "org_id"} текущего пользователя."""
    ident = get_identity()
    return {"user_id": ident["user_id"], "org_id": ident["org_id"]}


def register_scoped_keys(*keys: str) -> None:
    """
    Регистрирует дополнительные персональные ключи.
    Вызывать из модуля страницы, если у неё свои ключи с данными пользователя.
    """
    for k in keys:
        if k and k not in SCOPED_KEYS:
            SCOPED_KEYS.append(k)


def enforce_identity_boundary() -> bool:
    """
    Вызывать ОДИН РАЗ в начале рендера защищённой страницы, до чтения
    истории и прочих персональных ключей.

    Возвращает True, если личность сменилась и данные были вычищены.

        from core.session_scope import enforce_identity_boundary
        enforce_identity_boundary()
    """
    ss = _ss()
    if ss is None:
        return False

    identity = current_identity()

    # До авторизации личности нет — вайпить нечего и незачем
    if identity["user_id"] == "anonymous":
        return False

    bound = ss.get(_BOUND_KEY)
    if bound is None:
        ss[_BOUND_KEY] = identity
        return False

    if bound == identity:
        return False

    wiped = clear_user_scope()
    ss[_BOUND_KEY] = identity
    print(f"[SCOPE] Смена личности {bound} → {identity}. "
          f"Очищено ключей session_state: {wiped}")
    return True


def clear_user_scope() -> int:
    """
    Вычищает все персональные ключи из session_state.
    Вызывать также при явном логауте — ДО core.auth.logout().
    Возвращает число удалённых ключей.
    """
    ss = _ss()
    if ss is None:
        return 0

    victims = set()
    for key in SCOPED_KEYS:
        if key in ss:
            victims.add(key)
    # Динамические ключи по префиксу
    try:
        for key in list(ss.keys()):
            if isinstance(key, str) and key.startswith(SCOPED_PREFIXES):
                victims.add(key)
    except Exception:
        pass

    removed = 0
    for key in victims:
        try:
            del ss[key]
            removed += 1
        except Exception:
            pass

    ss.pop(_BOUND_KEY, None)
    return removed


# =============================================================================
# Сессионная история советчика — со штампом личности
# =============================================================================
HISTORY_KEY        = "advisor_history"
CLARIFICATIONS_KEY = "clarifications"


def history_add(entry: Dict[str, Any]) -> None:
    """Добавляет запись в сессионную историю, проставляя user_id/org_id."""
    ss = _ss()
    if ss is None:
        return
    ident = current_identity()
    stamped = {**entry, "_user_id": ident["user_id"], "_org_id": ident["org_id"]}
    ss.setdefault(HISTORY_KEY, []).append(stamped)


def history_get() -> List[Dict[str, Any]]:
    """
    История ТОЛЬКО текущей личности.
    Записи без штампа считаются чужими и не показываются — безопасный дефолт.
    """
    ss = _ss()
    if ss is None:
        return []
    ident = current_identity()
    return [
        e for e in ss.get(HISTORY_KEY, [])
        if e.get("_user_id") == ident["user_id"]
        and e.get("_org_id") == ident["org_id"]
    ]


def history_clear() -> None:
    """Очищает сессионную историю и цепочку уточнений."""
    ss = _ss()
    if ss is None:
        return
    ss[HISTORY_KEY]        = []
    ss[CLARIFICATIONS_KEY] = []


def history_delete(entry_id: Any) -> bool:
    """
    Удаляет запись сессионной истории по её "id" — только у текущей личности.
    Индексы для удаления использовать нельзя: history_get() возвращает
    отфильтрованный список, и его индексы не совпадают с индексами в
    session_state, если там лежат записи другой личности.
    """
    ss = _ss()
    if ss is None:
        return False
    ident = current_identity()
    items = ss.get(HISTORY_KEY, [])
    for i, e in enumerate(items):
        if (e.get("id") == entry_id
                and e.get("_user_id") == ident["user_id"]
                and e.get("_org_id") == ident["org_id"]):
            items.pop(i)
            return True
    return False


def history_update_last(**fields: Any) -> None:
    """
    Обновляет поля последней записи текущей личности — для дописывания
    цепочки уточнений.
    """
    ss = _ss()
    if ss is None:
        return
    ident = current_identity()
    for entry in reversed(ss.get(HISTORY_KEY, [])):
        if (entry.get("_user_id") == ident["user_id"]
                and entry.get("_org_id") == ident["org_id"]):
            entry.update(fields)
            return