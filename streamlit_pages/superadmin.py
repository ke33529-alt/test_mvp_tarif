# streamlit_pages/superadmin.py
"""
Страница управления РЕГУЛА.AI — только для суперадмина.
════════════════════════════════════════════════════════════════════════════════

Четыре вкладки:
  1. Сегменты и пользователи — список сегментов, список пользователей,
     управление статусами, сброс пароля, заметки
  2. Создать пользователя — форма создания нового аккаунта
  3. Статистика — использование модулей по пользователям за период
  4. Лог действий — аудит лог с фильтрами и экспортом CSV

Вызов из app.py:
  from streamlit_pages.superadmin import show_superadmin
  show_superadmin()
"""

from __future__ import annotations

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

from core.auth import (
    ROLES,
    create_user,
    get_current_user,
    has_role,
    hash_password,
    reset_password,
    set_force_logout,
    set_user_status,
    require_auth,
    USERS_FILE,
    SEGMENTS_FILE,
)
from core.audit import export_to_csv, get_module_stats, log_event, read_log

# ── Пути ─────────────────────────────────────────────────────────────────────

_BASE_DIR = Path(__file__).parent.parent.resolve()

# ── Названия ролей для UI ─────────────────────────────────────────────────────

# ── Модули системы ───────────────────────────────────────────────────────────
# Порядок важен — именно в таком порядке отображаются в сайдбаре и UI управления

MODULES = {
    "advisor":   "Советчик",
    "scanner":   "Сканер документов",
    "analyzer":  "Анализатор заявок",
    "predictor": "Прогноз решения регулятора",
    "protocol":  "Протокольщик",
    "tasks":     "Задачи",
}

# Соответствие module_id → ключ main_choice в app.py
MODULE_TO_CHOICE = {
    "advisor":   "Советчик",
    "scanner":   "Сканер документов",
    "analyzer":  "Анализатор заявок",
    "predictor": "Прогноз решения регулятора",
    "protocol":  "Протокольщик",
    "tasks":     "Задачи",
}

# Дефолтная конфигурация модулей для нового сегмента — все включены
DEFAULT_MODULES = {
    mid: {"enabled": True, "status": "active"}
    for mid in MODULES
}

MODULE_STATUS_LABELS = {
    "active":      "Активен",
    "maintenance": "На обслуживании",
}

MODULE_STATUS_COLORS = {
    "active":      ("#EAF3DE", "#27500A"),
    "maintenance": ("#FAEEDA", "#633806"),
}

ROLE_LABELS = {
    "superadmin":    "Суперадмин",
    "segment_admin": "Админ сегмента",
    "superuser":     "Суперпользователь",
    "user":          "Пользователь",
}

ROLE_COLORS = {
    "superadmin":    ("#EEEDFE", "#3C3489"),
    "segment_admin": ("#E6F1FB", "#0C447C"),
    "superuser":     ("#EAF3DE", "#27500A"),
    "user":          ("#F1EFE8", "#444441"),
}

STATUS_LABELS = {
    "active":   "Активен",
    "blocked":  "Заблокирован",
    "archived": "Архив",
}

STATUS_COLORS = {
    "active":   ("#EAF3DE", "#27500A"),
    "blocked":  ("#FAEEDA", "#633806"),
    "archived": ("#F1EFE8", "#5F5E5A"),
}

MODULE_LABELS = {
    "advisor":    "Советчик",
    "scanner":    "Сканер документов",
    "analyzer":   "Анализатор заявок",
    "predictor":  "Прогнозист",
    "protocol":   "Протокольщик",
    "tasks":      "Задачи",
    "admin":      "Админка",
    "superadmin": "Управление",
}

# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции работы с данными
# ─────────────────────────────────────────────────────────────────────────────

def _load_users() -> Dict[str, Dict]:
    """Загружает всех пользователей сегментов из users.json."""
    if not USERS_FILE.exists():
        return {}
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_segments() -> Dict[str, Dict]:
    """Загружает все сегменты из segments.json."""
    if not SEGMENTS_FILE.exists():
        return {}
    try:
        data = json.loads(SEGMENTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_segments(segments: Dict[str, Dict]) -> None:
    """Сохраняет сегменты в segments.json."""
    import threading
    _lock = threading.Lock()
    with _lock:
        SEGMENTS_FILE.write_text(
            json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _create_segment(name: str, rag_collection: str = "") -> tuple[bool, str]:
    """
    Создаёт новый сегмент.
    org_id генерируется из временной метки — уникален, не зависит от названия.
    """
    segments = _load_segments()

    # Проверяем уникальность названия
    for seg in segments.values():
        if seg.get("name", "").strip().lower() == name.strip().lower():
            return False, f"Сегмент с названием «{name}» уже существует"

    org_id = datetime.now().strftime("org_%Y%m%d_%H%M%S")
    now    = datetime.now().isoformat(timespec="seconds")

    segments[org_id] = {
        "name":                  name.strip(),
        "created_at":            now,
        "status":                "active",
        "archived_at":           None,
        "rag_collection_name":   rag_collection.strip() or f"docs_{org_id}",
        "rag_doc_count":         0,
        "rag_last_indexed_at":   None,
        "license_until":         "9999-12-31",
        "max_users":             999,
        "admin_notes":           "",
        "admin_notes_updated_at": None,
        # Все модули включены по умолчанию — суперадмин отключает лишнее
        "modules":               {mid: {"enabled": True, "status": "active"} for mid in MODULES},
    }
    _save_segments(segments)

    # Логируем создание сегмента
    user = get_current_user()
    if user:
        log_event(
            org_id=user.get("org_id", ""),
            user_id=user["user_id"],
            role=user["role"],
            event="segment_created",
            module="superadmin",
            meta={"target_org_id": org_id, "name": name},
        )

    return True, org_id


def _archive_segment(org_id: str) -> bool:
    """Архивирует сегмент (мягкое удаление). Ставит force_logout всем пользователям."""
    segments = _load_segments()
    if org_id not in segments:
        return False

    segments[org_id]["status"]      = "archived"
    segments[org_id]["archived_at"] = datetime.now().isoformat(timespec="seconds")
    _save_segments(segments)

    # Выбиваем всех пользователей сегмента
    users = _load_users()
    for uid, rec in users.items():
        if rec.get("org_id") == org_id and rec.get("status") == "active":
            set_force_logout(uid)

    user = get_current_user()
    if user:
        log_event(
            org_id=user.get("org_id", ""),
            user_id=user["user_id"],
            role=user["role"],
            event="segment_archived",
            module="superadmin",
            meta={"target_org_id": org_id},
        )
    return True


def _restore_segment(org_id: str) -> bool:
    """Восстанавливает архивированный сегмент."""
    segments = _load_segments()
    if org_id not in segments:
        return False
    segments[org_id]["status"]      = "active"
    segments[org_id]["archived_at"] = None
    _save_segments(segments)
    return True


def _update_segment_rag(org_id: str, rag_collection: str) -> bool:
    """Обновляет имя RAG-коллекции сегмента."""
    segments = _load_segments()
    if org_id not in segments:
        return False
    segments[org_id]["rag_collection_name"] = rag_collection.strip()
    _save_segments(segments)
    return True


def _get_segment_modules(org_id: str) -> Dict:
    """
    Возвращает конфигурацию модулей сегмента.
    Если поле modules отсутствует (старые сегменты) — возвращает дефолт (все включены).
    """
    segments = _load_segments()
    seg = segments.get(org_id, {})
    modules = seg.get("modules")
    if not modules:
        # Миграция старых сегментов — все модули включены
        return {mid: {"enabled": True, "status": "active"} for mid in MODULES}
    # Дополняем новыми модулями если они появились после создания сегмента
    result = {}
    for mid in MODULES:
        result[mid] = modules.get(mid, {"enabled": True, "status": "active"})
    return result


def _save_segment_modules(org_id: str, modules: Dict) -> bool:
    """Сохраняет конфигурацию модулей сегмента."""
    segments = _load_segments()
    if org_id not in segments:
        return False
    segments[org_id]["modules"] = modules
    _save_segments(segments)
    return True


def _get_allowed_modules(user: Dict) -> list:
    """
    Возвращает список module_id доступных текущему пользователю.
    Суперадмин видит все модули всегда.
    Остальные — только enabled модули своего сегмента со статусом active или maintenance.
    """
    if user.get("role") == "superadmin":
        return list(MODULES.keys())

    org_id  = user.get("org_id", "")
    modules = _get_segment_modules(org_id)
    return [
        mid for mid, cfg in modules.items()
        if cfg.get("enabled", True)
    ]


def _get_module_usage_stats(org_id: str, days: int = 30) -> Dict[str, int]:
    """
    Возвращает счётчик открытий каждого модуля за последние N дней.
    Считается из audit_log.jsonl на лету — не хранится в segments.json.
    """
    from datetime import date as date_type
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to   = datetime.now().strftime("%Y-%m-%d")

    records = read_log(
        date_from=date_from,
        date_to=date_to,
        org_id=org_id,
        event="module_open",
        limit=50_000,
    )

    # Маппинг из имени модуля в audit_log на module_id
    choice_to_module = {v: k for k, v in MODULE_TO_CHOICE.items()}

    counts: Dict[str, int] = {mid: 0 for mid in MODULES}
    for rec in records:
        mod = rec.get("module", "")
        mid = choice_to_module.get(mod, mod)
        if mid in counts:
            counts[mid] += 1
    return counts


def _update_user(
    user_id: str,
    name: str,
    login_str: str,
    role: str,
    org_id: str,
) -> tuple[bool, str]:
    """
    Редактирует данные пользователя — имя, логин, роль, сегмент.
    Пароль не трогается — для смены пароля используется reset_password().
    """
    from core.auth import _load_users, _save_users, _get_user_by_login
    users     = _load_users()
    if user_id not in users:
        return False, "Пользователь не найден"

    login_str = login_str.strip().lower()

    # Проверяем что новый логин не занят другим пользователем
    existing = _get_user_by_login(login_str)
    if existing and existing["user_id"] != user_id:
        return False, f"Логин {login_str} уже занят другим пользователем"

    users[user_id]["name"]    = name.strip()
    users[user_id]["login"]   = login_str
    users[user_id]["role"]    = role
    users[user_id]["org_id"]  = org_id
    _save_users(users)
    return True, ""


def _restore_user(user_id: str) -> bool:
    """Восстанавливает пользователя из архива."""
    from core.auth import _load_users, _save_users
    users = _load_users()
    if user_id not in users:
        return False
    users[user_id]["status"]        = "active"
    users[user_id]["blocked_until"] = None
    _save_users(users)
    return True


def _save_segment_notes(org_id: str, notes: str) -> bool:
    """Сохраняет заметки суперадмина к сегменту."""
    segments = _load_segments()
    if org_id not in segments:
        return False
    segments[org_id]["admin_notes"]            = notes
    segments[org_id]["admin_notes_updated_at"] = datetime.now().isoformat(timespec="seconds")
    _save_segments(segments)
    return True


def _count_users_in_segment(org_id: str, users: Dict) -> int:
    """Считает активных пользователей в сегменте."""
    return sum(
        1 for rec in users.values()
        if rec.get("org_id") == org_id and rec.get("status") != "archived"
    )


def _is_online(user_id: str, minutes: int = 3) -> bool:
    """
    Проверяет онлайн-статус пользователя по heartbeat-файлу.
    Пользователь считается онлайн если его heartbeat свежее N минут.
    Heartbeat пишется в data/sessions/{user_id}.json при каждом рендере app.py.
    """
    from datetime import timedelta
    hb_file = _BASE_DIR / "data" / "sessions" / f"{user_id}.json"
    if not hb_file.exists():
        return False
    try:
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        ts   = datetime.fromisoformat(data.get("ts", ""))
        return datetime.now() - ts < timedelta(minutes=minutes)
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные UI-компоненты
# ─────────────────────────────────────────────────────────────────────────────

def _badge(label: str, bg: str, color: str) -> str:
    """Рендерит цветной бейдж через HTML."""
    return (
        f'<span style="font-size:11px;padding:2px 8px;border-radius:20px;'
        f'font-weight:500;background:{bg};color:{color}">{label}</span>'
    )


def _role_badge(role: str) -> str:
    bg, color = ROLE_COLORS.get(role, ("#F1EFE8", "#444441"))
    return _badge(ROLE_LABELS.get(role, role), bg, color)


def _status_badge(status: str) -> str:
    bg, color = STATUS_COLORS.get(status, ("#F1EFE8", "#5F5E5A"))
    return _badge(STATUS_LABELS.get(status, status), bg, color)


def _avatar(name: str) -> str:
    """Инициалы для аватара."""
    parts = name.split()
    initials = "".join(p[0].upper() for p in parts[:2]) if parts else "?"
    return initials


# ─────────────────────────────────────────────────────────────────────────────
# Вкладка 1: Сегменты и пользователи
# ─────────────────────────────────────────────────────────────────────────────

def _tab_segments():
    segments = _load_segments()
    users    = _load_users()


    active_segs   = {k: v for k, v in segments.items() if v.get("status") == "active"}
    archived_segs = {k: v for k, v in segments.items() if v.get("status") == "archived"}

    if not segments:
        st.caption("Сегментов пока нет. Создайте первый.")
    else:
        for org_id, seg in active_segs.items():
            user_count = _count_users_in_segment(org_id, users)
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.markdown(f"**{seg['name']}**")
                    st.caption(
                        f"{user_count} пользователей · "
                        f"создан {seg['created_at'][:10]}"
                    )
                with c2:
                    st.markdown(
                        _status_badge("active"),
                        unsafe_allow_html=True,
                    )

                # Раскрывающаяся панель управления сегментом
                with st.expander("Управление сегментом"):
                    # ── Модули ────────────────────────────────────────────
                    st.markdown("**Доступные модули**")

                    # Загружаем текущую конфигурацию и статистику использования
                    seg_modules = _get_segment_modules(org_id)
                    usage_stats = _get_module_usage_stats(org_id, days=30)

                    modules_changed = False
                    new_modules     = {}

                    for mid, label in MODULES.items():
                        cfg     = seg_modules.get(mid, {"enabled": True, "status": "active"})
                        enabled = cfg.get("enabled", True)
                        status  = cfg.get("status", "active")
                        count   = usage_stats.get(mid, 0)

                        mc1, mc2, mc3, mc4 = st.columns([3, 1, 1, 1])
                        with mc1:
                            new_enabled = st.checkbox(
                                label,
                                value=enabled,
                                key=f"mod_{org_id}_{mid}",
                            )
                        with mc2:
                            new_status = st.selectbox(
                                "Статус",
                                options=["active", "maintenance"],
                                index=0 if status == "active" else 1,
                                format_func=lambda x: MODULE_STATUS_LABELS.get(x, x),
                                key=f"modst_{org_id}_{mid}",
                                label_visibility="collapsed",
                            )
                        with mc3:
                            # Бейдж статуса
                            bg, color = MODULE_STATUS_COLORS.get(new_status, ("#F1EFE8", "#5F5E5A"))
                            if new_enabled:
                                st.markdown(
                                    f'<span style="font-size:11px;padding:2px 7px;'
                                    f'border-radius:20px;background:{bg};color:{color};'
                                    f'font-weight:500">{MODULE_STATUS_LABELS.get(new_status, new_status)}</span>',
                                    unsafe_allow_html=True,
                                )
                            else:
                                st.markdown(
                                    '<span style="font-size:11px;padding:2px 7px;'
                                    'border-radius:20px;background:#F1EFE8;color:#5F5E5A;'
                                    'font-weight:500">Выключен</span>',
                                    unsafe_allow_html=True,
                                )
                        with mc4:
                            # Счётчик использования за 30 дней
                            st.caption(f"↗ {count} за 30 дн.")

                        new_modules[mid] = {
                            "enabled": new_enabled,
                            "status":  new_status,
                        }
                        if new_enabled != enabled or new_status != status:
                            modules_changed = True

                    if st.button("Сохранить модули", key=f"save_mods_{org_id}",
                                 type="primary"):
                        if _save_segment_modules(org_id, new_modules):
                            st.success("Настройки модулей сохранены")
                            st.rerun()

                    st.divider()

                    # RAG-коллекция
                    rag_key = f"rag_{org_id}"
                    rag_val = seg.get("rag_collection_name", "")
                    new_rag = st.text_input(
                        "RAG-коллекция (код)",
                        value=rag_val,
                        key=rag_key,
                        placeholder="docs_tambov",
                    )
                    if st.button("Сохранить RAG", key=f"save_rag_{org_id}"):
                        if _update_segment_rag(org_id, new_rag):
                            st.success("RAG-коллекция обновлена")
                            st.rerun()

                    st.divider()

                    # Заметки суперадмина
                    notes_key = f"notes_{org_id}"
                    notes_val = seg.get("admin_notes", "")
                    new_notes = st.text_area(
                        "Заметки (видны только суперадмину)",
                        value=notes_val,
                        height=80,
                        key=notes_key,
                        placeholder="Контактное лицо, договор, дата оплаты...",
                    )
                    if st.button("Сохранить заметки", key=f"save_notes_{org_id}"):
                        if _save_segment_notes(org_id, new_notes):
                            st.success("Заметки сохранены")
                            st.rerun()

                    st.divider()

                    # Информация о лицензии
                    license_until = seg.get("license_until", "9999-12-31")
                    max_users     = seg.get("max_users", 999)
                    st.caption(
                        f"Лицензия до: **{license_until if license_until != '9999-12-31' else 'бессрочно'}** · "
                        f"Макс. пользователей: **{max_users if max_users != 999 else 'без ограничений'}**"
                    )

                    st.divider()

                    # Архивирование
                    if st.button(
                        "Архивировать сегмент",
                        key=f"archive_seg_{org_id}",
                        type="secondary",
                    ):
                        st.session_state[f"confirm_archive_{org_id}"] = True

                    if st.session_state.get(f"confirm_archive_{org_id}"):
                        st.warning(
                            f"Все пользователи сегмента «{seg['name']}» будут выбиты из системы. Подтвердить?"
                        )
                        ca1, ca2 = st.columns(2)
                        with ca1:
                            if st.button("Да, архивировать", key=f"confirm_yes_{org_id}"):
                                _archive_segment(org_id)
                                st.session_state.pop(f"confirm_archive_{org_id}", None)
                                st.success("Сегмент архивирован")
                                st.rerun()
                        with ca2:
                            if st.button("Отмена", key=f"confirm_no_{org_id}"):
                                st.session_state.pop(f"confirm_archive_{org_id}", None)
                                st.rerun()

        # Архивированные сегменты — с кнопкой восстановления
        if archived_segs:
            with st.expander(f"Архив ({len(archived_segs)})"):
                for org_id, seg in archived_segs.items():
                    c1, c2, c3 = st.columns([3, 1, 1])
                    with c1:
                        st.caption(
                            f"{seg['name']} · архив с {(seg.get('archived_at') or '')[:10]} · "
                            f"RAG: {seg.get('rag_collection_name', '—')}"
                        )
                    with c2:
                        st.markdown(_status_badge("archived"), unsafe_allow_html=True)
                    with c3:
                        if st.button("Восстановить", key=f"restore_seg_{org_id}"):
                            _restore_segment(org_id)
                            st.success(f"Сегмент «{seg['name']}» восстановлен")
                            st.rerun()

    st.divider()

    # Форма создания сегмента
    with st.expander("Создать сегмент"):
        new_seg_name = st.text_input("Название", key="new_seg_name", placeholder="РЭК Тамбовской обл.")
        new_seg_rag  = st.text_input(
            "Имя коллекции RAG (оставьте пустым для авто)",
            key="new_seg_rag",
            placeholder="docs_tambov",
        )
        if st.button("Создать", key="btn_create_seg", type="primary"):
            if not new_seg_name.strip():
                st.error("Введите название сегмента")
            else:
                ok, result = _create_segment(new_seg_name, new_seg_rag)
                if ok:
                    st.session_state["_seg_created_name"] = new_seg_name
                    st.rerun()
                else:
                    st.error(result)

        if st.session_state.get("_seg_created_name"):
            st.success(f"Сегмент «{st.session_state['_seg_created_name']}» создан")
            del st.session_state["_seg_created_name"]



def _tab_users():
    segments = _load_segments()
    users    = _load_users()

    # Форма создания нового пользователя в раскрывающемся блоке
    with st.expander("Создать пользователя"):
        active_segments = {k: v for k, v in segments.items() if v.get("status") == "active"}

        st.markdown("##### Новый пользователь")

        if not active_segments:
            st.warning("Сначала создайте хотя бы один активный сегмент.")
        else:
            col, _ = st.columns([1, 1])
            with col:
                with st.container(border=True):
                    name_in  = st.text_input("Фамилия Имя Отчество", key="cu_name",
                                             placeholder="Иванова Мария Алексеевна")
                    login_in = st.text_input("Email / логин", key="cu_login",
                                             placeholder="ivanova@rek-tambov.ru")

                    seg_in = st.selectbox(
                        "Сегмент",
                        options=list(active_segments.keys()),
                        format_func=lambda x: active_segments[x]["name"],
                        key="cu_seg",
                    )

                    role_in = st.selectbox(
                        "Роль",
                        options=["user", "superuser", "segment_admin"],
                        format_func=lambda x: ROLE_LABELS.get(x, x),
                        key="cu_role",
                    )

                    # Генерируем временный пароль автоматически
                    import secrets, string
                    if "cu_temp_pass" not in st.session_state:
                        alphabet = (
                            string.ascii_letters.replace("l", "").replace("O", "")
                            + string.digits.replace("0", "").replace("1", "")
                        )
                        st.session_state.cu_temp_pass = "".join(secrets.choice(alphabet) for _ in range(12))

                    st.markdown("**Временный пароль** (передайте пользователю):")
                    st.code(st.session_state.cu_temp_pass)
                    if st.button("Сгенерировать другой", key="cu_regen"):
                        alphabet = (
                            string.ascii_letters.replace("l", "").replace("O", "")
                            + string.digits.replace("0", "").replace("1", "")
                        )
                        st.session_state.cu_temp_pass = "".join(secrets.choice(alphabet) for _ in range(12))
                        st.rerun()

                    st.divider()

                    if st.button("Создать пользователя", key="cu_submit", type="primary",
                                 use_container_width=True):
                        if not name_in.strip():
                            st.error("Введите имя пользователя")
                        elif not login_in.strip():
                            st.error("Введите логин")
                        else:
                            ok, result = create_user(
                                name=name_in,
                                login_str=login_in,
                                password=st.session_state.cu_temp_pass,
                                org_id=seg_in,
                                role=role_in,
                            )
                            if ok:
                                user = get_current_user()
                                if user:
                                    log_event(
                                        org_id=user.get("org_id", ""),
                                        user_id=user["user_id"],
                                        role=user["role"],
                                        event="user_created",
                                        module="superadmin",
                                        meta={"target_user_id": result, "role": role_in},
                                    )
                                # Сохраняем имя для уведомления после rerun
                                st.session_state["_user_created_name"] = name_in
                                # Сбрасываем пароль чтобы следующий пользователь получил новый
                                del st.session_state["cu_temp_pass"]
                                st.rerun()
                            else:
                                st.error(result)

                    if st.session_state.get("_user_created_name"):
                        st.success(
                            f"Пользователь «{st.session_state['_user_created_name']}» создан. "
                            f"Передайте временный пароль вручную."
                        )
                        del st.session_state["_user_created_name"]

    st.divider()

    st.markdown("##### Пользователи")

    # Текстовый поиск по имени/логину пользователя
    search_query = st.text_input(
        "Поиск",
        key="users_search",
        placeholder="Поиск по фамилии, имени или логину...",
        label_visibility="collapsed",
    )

    # Фильтры — одинаковые колонки чтобы выпадашки были одной ширины
    fc1, fc2 = st.columns([1, 1])
    with fc1:
        seg_options = {"": "Все сегменты"} | {k: v["name"] for k, v in segments.items()}
        filter_seg  = st.selectbox(
            "Сегмент",
            options=list(seg_options.keys()),
            format_func=lambda x: seg_options[x],
            key="filter_seg",
            label_visibility="collapsed",
        )
    with fc2:
        filter_role = st.selectbox(
            "Роль",
            options=["", "segment_admin", "superuser", "user"],
            format_func=lambda x: "Все роли" if not x else ROLE_LABELS.get(x, x),
            key="filter_role",
            label_visibility="collapsed",
        )

    # Фильтрация — сегмент, роль и текстовый поиск по имени/логину
    _sq = search_query.strip().lower()
    filtered = {
        uid: rec for uid, rec in users.items()
        if (not filter_seg  or rec.get("org_id") == filter_seg)
        and (not filter_role or rec.get("role")   == filter_role)
        and (not _sq
             or _sq in rec.get("name", "").lower()
             or _sq in rec.get("login", "").lower())
    }

    if not filtered:
        st.caption("Пользователей не найдено")
    else:
        for uid, rec in filtered.items():
            seg_name   = segments.get(rec.get("org_id", ""), {}).get("name", "—")
            role       = rec.get("role", "user")
            status     = rec.get("status", "active")
            last_login = (rec.get("last_login") or "")[:10] or "никогда"

            with st.container(border=True):
                r1c1, r1c2 = st.columns([3, 2])
                with r1c1:
                    # Лампочка онлайн — зелёная если heartbeat свежее 3 минут
                    online      = _is_online(uid, minutes=3)
                    online_dot  = (
                        '<span style="display:inline-block;width:8px;height:8px;'
                        'border-radius:50%;background:#27AE60;margin-right:5px;'
                        'vertical-align:middle" title="Онлайн"></span>'
                        if online else
                        '<span style="display:inline-block;width:8px;height:8px;'
                        'border-radius:50%;background:#CCC;margin-right:5px;'
                        'vertical-align:middle" title="Офлайн"></span>'
                    )
                    st.markdown(
                        f"{online_dot}**{rec.get('name', '—')}** &nbsp;"
                        + _role_badge(role),
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"{seg_name} · логин: {rec.get('login', '—')} · вход: {last_login}"
                    )
                with r1c2:
                    st.markdown(_status_badge(status), unsafe_allow_html=True)

                # Управление пользователем — все кнопки в одну строку
                with st.expander("Действия"):

                    # Для архивного пользователя — только восстановление
                    if status == "archived":
                        if st.button("Восстановить из архива", key=f"restore_{uid}",
                                     type="primary", use_container_width=True):
                            _restore_user(uid)
                            st.success("Пользователь восстановлен")
                            st.rerun()

                    else:
                        # Для активных и заблокированных — полный набор кнопок
                        a1, a2, a3, a4 = st.columns(4)

                        with a1:
                            if st.button("Сбросить пароль", key=f"reset_{uid}",
                                         use_container_width=True):
                                ok, temp = reset_password(uid)
                                if ok:
                                    st.session_state[f"temp_pass_{uid}"] = temp
                                else:
                                    st.error("Ошибка сброса пароля")

                        with a2:
                            if status == "active":
                                if st.button("Выбить из системы", key=f"logout_{uid}",
                                             use_container_width=True):
                                    set_force_logout(uid)
                                    st.success("Пользователь будет выбит при следующем действии")

                        with a3:
                            if status == "active":
                                if st.button("Заблокировать", key=f"block_{uid}",
                                             use_container_width=True):
                                    set_user_status(uid, "blocked")
                                    st.rerun()
                            elif status == "blocked":
                                if st.button("Разблокировать", key=f"unblock_{uid}",
                                             use_container_width=True):
                                    set_user_status(uid, "active")
                                    st.rerun()

                        with a4:
                            if st.button("В архив", key=f"archive_{uid}",
                                         use_container_width=True):
                                set_user_status(uid, "archived")
                                user = get_current_user()
                                if user:
                                    log_event(
                                        org_id=user.get("org_id", ""),
                                        user_id=user["user_id"],
                                        role=user["role"],
                                        event="user_archived",
                                        module="superadmin",
                                        meta={"target_user_id": uid},
                                    )
                                st.rerun()

                    # Временный пароль
                    if st.session_state.get(f"temp_pass_{uid}"):
                        st.divider()
                        st.caption("Временный пароль — передайте пользователю. При входе он обязан его сменить.")
                        st.code(st.session_state[f"temp_pass_{uid}"])
                        if st.button("Скрыть пароль", key=f"hide_pass_{uid}"):
                            st.session_state.pop(f"temp_pass_{uid}", None)
                            st.rerun()

                    # Редактирование данных пользователя — только для не архивных
                    if status != "archived":
                        st.divider()
                        st.markdown("**Редактировать**")
                        active_segs = {k: v for k, v in segments.items() if v.get("status") == "active"}
                        e1, e2 = st.columns(2)
                        with e1:
                            edit_name = st.text_input(
                                "Имя", value=rec.get("name", ""), key=f"edit_name_{uid}"
                            )
                            edit_login = st.text_input(
                                "Логин", value=rec.get("login", ""), key=f"edit_login_{uid}"
                            )
                        with e2:
                            edit_role = st.selectbox(
                                "Роль",
                                options=["user", "superuser", "segment_admin"],
                                index=["user", "superuser", "segment_admin"].index(
                                    role if role in ["user", "superuser", "segment_admin"] else "user"
                                ),
                                format_func=lambda x: ROLE_LABELS.get(x, x),
                                key=f"edit_role_{uid}",
                            )
                            edit_seg = st.selectbox(
                                "Сегмент",
                                options=list(active_segs.keys()),
                                index=list(active_segs.keys()).index(rec.get("org_id", ""))
                                      if rec.get("org_id") in active_segs else 0,
                                format_func=lambda x: active_segs[x]["name"],
                                key=f"edit_seg_{uid}",
                            )
                        if st.button("Сохранить изменения", key=f"save_edit_{uid}",
                                     type="primary"):
                            ok, err = _update_user(uid, edit_name, edit_login, edit_role, edit_seg)
                            if ok:
                                st.success("Данные пользователя обновлены")
                                st.rerun()
                            else:
                                st.error(err)



# ─────────────────────────────────────────────────────────────────────────────
# Вкладка 3: Статистика использования модулей
# ─────────────────────────────────────────────────────────────────────────────

def _tab_stats():
    segments = _load_segments()
    users    = _load_users()

    st.markdown("##### Статистика использования модулей")

    # Фильтры периода
    sc1, sc2, sc3 = st.columns([1, 1, 1])
    with sc1:
        date_from = st.date_input(
            "С даты", value=date.today() - timedelta(days=30), key="stat_from"
        )
    with sc2:
        date_to = st.date_input("По дату", value=date.today(), key="stat_to")
    with sc3:
        seg_options = {"": "Все сегменты"} | {k: v["name"] for k, v in segments.items()}
        stat_seg    = st.selectbox(
            "Сегмент",
            options=list(seg_options.keys()),
            format_func=lambda x: seg_options[x],
            key="stat_seg",
            label_visibility="collapsed",
        )

    stats = get_module_stats(
        org_id=stat_seg or None,
        date_from=str(date_from),
        date_to=str(date_to),
    )

    if not stats:
        st.info("Нет данных за выбранный период. Убедитесь что аудит лог подключён к модулям.")
        return

    # Все уникальные модули
    all_modules = set()
    for row in stats:
        all_modules.update(row["modules"].keys())
    all_modules = sorted(all_modules)

    # Таблица: пользователь + модули + итого
    header_cols = st.columns([2, 1] + [1] * len(all_modules))
    header_cols[0].markdown("**Пользователь**")
    header_cols[1].markdown("**Итого**")
    for i, mod in enumerate(all_modules):
        header_cols[2 + i].markdown(f"**{MODULE_LABELS.get(mod, mod)}**")

    st.divider()

    for row in stats:
        uid      = row["user_id"]
        org_id   = row["org_id"]
        rec      = users.get(uid, {})
        name     = rec.get("name", uid)
        seg_name = segments.get(org_id, {}).get("name", "—")

        row_cols = st.columns([2, 1] + [1] * len(all_modules))
        row_cols[0].markdown(f"{name}  \n<small style='color:#5a6a7a'>{seg_name}</small>",
                             unsafe_allow_html=True)
        row_cols[1].markdown(f"**{row['total']}**")
        for i, mod in enumerate(all_modules):
            count = row["modules"].get(mod, 0)
            row_cols[2 + i].markdown(str(count) if count else "—")


# ─────────────────────────────────────────────────────────────────────────────
# Вкладка 4: Лог действий
# ─────────────────────────────────────────────────────────────────────────────

def _tab_audit_log():
    segments = _load_segments()
    users    = _load_users()

    st.markdown("##### Лог действий")

    # Первая строка фильтров: даты
    fr1c1, fr1c2 = st.columns([1, 1])
    with fr1c1:
        log_date_from = st.date_input("С даты", value=date.today(), key="log_from")
    with fr1c2:
        log_date_to = st.date_input("По дату", value=date.today(), key="log_to")

    # Вторая строка фильтров: сегмент, пользователь, событие
    fr2c1, fr2c2, fr2c3 = st.columns([1, 1, 1])
    with fr2c1:
        seg_opts = {"": "Все сегменты"} | {k: v["name"] for k, v in segments.items()}
        log_seg  = st.selectbox(
            "Сегмент", options=list(seg_opts.keys()),
            format_func=lambda x: seg_opts[x],
            key="log_seg", label_visibility="collapsed",
        )
    with fr2c2:
        user_opts = {"": "Все пользователи"} | {
            uid: rec.get("name", uid) for uid, rec in users.items()
        }
        log_user = st.selectbox(
            "Пользователь", options=list(user_opts.keys()),
            format_func=lambda x: user_opts[x],
            key="log_user", label_visibility="collapsed",
        )
    with fr2c3:
        from core.audit import EVENT_TYPES
        event_opts = {"": "Все события"} | {e: e for e in sorted(EVENT_TYPES)}
        log_event_filter = st.selectbox(
            "Событие", options=list(event_opts.keys()),
            format_func=lambda x: event_opts[x],
            key="log_event", label_visibility="collapsed",
        )

    records = read_log(
        date_from=str(log_date_from),
        date_to=str(log_date_to),
        org_id=log_seg   or None,
        user_id=log_user or None,
        event=log_event_filter or None,
        limit=200,
    )

    # Шапка + экспорт
    meta_col, export_col = st.columns([3, 1])
    with meta_col:
        st.caption(f"Показано записей: {len(records)}")
    with export_col:
        if records:
            csv = export_to_csv(records)
            st.download_button(
                "Экспорт CSV",
                data=csv,
                file_name=f"audit_{log_date_from}_{log_date_to}.csv",
                mime="text/csv",
                key="export_csv",
            )

    if not records:
        st.info("Событий за выбранный период не найдено.")
        return

    # Таблица лога
    EVENT_LABELS = {
        "login":            "Вход",
        "logout":           "Выход",
        "module_open":      "Раздел",
        "button_click":     "Кнопка",
        "llm_query":        "Запрос",
        "file_upload":      "Файл",
        "file_deleted":     "Удалён",
        "password_reset":   "Сброс пароля",
        "user_created":     "Создан польз.",
        "user_archived":    "Архив польз.",
        "segment_created":  "Создан сегм.",
        "segment_archived": "Архив сегм.",
    }

    EVENT_BADGE_COLORS = {
        "login":        ("#EAF3DE", "#27500A"),
        "logout":       ("#F1EFE8", "#5F5E5A"),
        "module_open":  ("#E6F1FB", "#0C447C"),
        "button_click": ("#FAECE7", "#712B13"),
        "llm_query":    ("#EEEDFE", "#3C3489"),
        "file_upload":  ("#FAEEDA", "#633806"),
        "file_deleted": ("#F1EFE8", "#5F5E5A"),
    }

    # Заголовок таблицы
    hc = st.columns([1, 1, 2, 1, 3])
    for col, label in zip(hc, ["Время", "Сегмент", "Пользователь", "Событие", "Детали"]):
        col.markdown(f"<small style='color:#5a6a7a;font-weight:600'>{label}</small>",
                     unsafe_allow_html=True)
    st.divider()

    for rec in records:
        ts       = rec.get("ts", "")[:16].replace("T", " ")
        org_id   = rec.get("org_id", "")
        uid      = rec.get("user_id", "")
        ev       = rec.get("event", "")
        mod      = rec.get("module") or ""
        meta     = rec.get("meta", {})

        seg_name  = segments.get(org_id, {}).get("name", org_id or "—")
        user_name = users.get(uid, {}).get("name", uid or "суперадмин")

        # Формируем детали из метаданных
        details_parts = []
        if mod:
            details_parts.append(MODULE_LABELS.get(mod, mod))
        if meta.get("label"):
            details_parts.append(meta["label"])
        if meta.get("file_type"):
            size = meta.get("size_kb", "")
            details_parts.append(f"{meta['file_type'].upper()} {size}КБ" if size else meta["file_type"].upper())
        if meta.get("target_user_id"):
            target = users.get(meta["target_user_id"], {}).get("name", meta["target_user_id"])
            details_parts.append(f"→ {target}")
        if meta.get("target_org_id"):
            target_seg = segments.get(meta["target_org_id"], {}).get("name", meta["target_org_id"])
            details_parts.append(f"→ {target_seg}")
        if meta.get("duration_min"):
            details_parts.append(f"длит. {meta['duration_min']} мин")

        details = " · ".join(details_parts) if details_parts else "—"

        # Цвет бейджа события
        bg, color = EVENT_BADGE_COLORS.get(ev, ("#F1EFE8", "#5F5E5A"))
        ev_badge  = _badge(EVENT_LABELS.get(ev, ev), bg, color)

        rc = st.columns([1, 1, 2, 1, 3])
        rc[0].markdown(f"<small style='color:#5a6a7a'>{ts}</small>", unsafe_allow_html=True)
        rc[1].markdown(f"<small>{seg_name}</small>", unsafe_allow_html=True)
        rc[2].markdown(f"<small>{user_name}</small>", unsafe_allow_html=True)
        rc[3].markdown(ev_badge, unsafe_allow_html=True)
        rc[4].markdown(f"<small style='color:#5a6a7a'>{details}</small>", unsafe_allow_html=True)


def _tab_help():
    """
    Вкладка входящих запросов помощи от пользователей.
    Суперадмин видит все сообщения, может писать ответы и отмечать как отработанные.
    Ответ и статус "отработано" — независимые действия.
    """
    from core.help_requests import get_requests, mark_done, save_reply, count_new, reopen_request
    import io, csv as _csv

    segments = _load_segments()
    users    = _load_users()

    st.markdown("##### Запросы на помощь")

    # Первая строка: статус + экспорт
    fc1, fc2 = st.columns([3, 1])
    with fc1:
        status_filter = st.selectbox(
            "Статус",
            options=["new", "done", ""],
            format_func=lambda x: {"new": "Неотработанные", "done": "Отработанные", "": "Все"}[x],
            key="help_status",
            index=0,
        )

    # Вторая строка: сегмент + пользователь
    hc1, hc2 = st.columns([1, 1])
    with hc1:
        seg_opts = {"": "Все сегменты"} | {k: v["name"] for k, v in segments.items()}
        help_seg = st.selectbox(
            "Сегмент", options=list(seg_opts.keys()),
            format_func=lambda x: seg_opts[x],
            key="help_seg", label_visibility="collapsed",
        )
    with hc2:
        user_opts = {"": "Все пользователи"} | {
            uid: rec.get("name", uid) for uid, rec in users.items()
        }
        help_user = st.selectbox(
            "Пользователь", options=list(user_opts.keys()),
            format_func=lambda x: user_opts[x],
            key="help_user", label_visibility="collapsed",
        )

    requests = get_requests(
        status=status_filter or None,
        org_id=help_seg  or None,
        user_id=help_user or None,
        limit=200,
    )

    # Счётчик + экспорт
    cnt_col, exp_col = st.columns([3, 1])
    with cnt_col:
        st.caption(f"Показано: {len(requests)} · Всего неотработанных: {count_new()}")
    with exp_col:
        if requests:
            out = io.StringIO()
            w   = _csv.writer(out)
            w.writerow(["Время", "Пользователь", "Сегмент", "Сообщение", "Ответ", "Статус", "Отработано"])
            for r in requests:
                w.writerow([
                    r.get("ts","")[:16], r.get("user_name",""), r.get("org_name",""),
                    r.get("text",""), r.get("reply","") or "",
                    r.get("status",""), (r.get("done_at") or "")[:10],
                ])
            st.download_button(
                "Экспорт CSV", data=out.getvalue(),
                file_name="help_requests.csv", mime="text/csv",
                key="help_csv", use_container_width=True,
            )

    if not requests:
        st.info("Запросов не найдено.")
        return

    for req in requests:
        ts         = req.get("ts", "")[:16].replace("T", " ")
        user_name  = req.get("user_name", req.get("user_id", "—"))
        org_name   = req.get("org_name", "—")
        text       = req.get("text", "")
        status     = req.get("status", "new")
        reply      = req.get("reply") or ""
        rid        = req.get("id", "")
        replied_at = (req.get("replied_at") or "")[:10]
        done_at    = (req.get("done_at") or "")[:10]

        with st.container(border=True):
            # Шапка: время · пользователь · сегмент · статус
            hc1, hc2 = st.columns([3, 1])
            with hc1:
                st.caption(f"{ts} · **{user_name}** · {org_name}")
            with hc2:
                if status == "done":
                    st.markdown(
                        "<small style='color:#27AE60;font-weight:500'>✓ Отработано</small>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        "<small style='color:#E24B4A;font-weight:500'>● Новый</small>",
                        unsafe_allow_html=True,
                    )

            # Текст запроса
            st.markdown(
                f"<div style='color:#1a2a3a;padding:0.2rem 0 0.4rem'>{text}</div>",
                unsafe_allow_html=True,
            )

            # Если есть ответ — показываем компактно, с кнопкой редактировать
            edit_key = f"edit_reply_{rid}"
            if reply and not st.session_state.get(edit_key):
                st.markdown(
                    f"<div style='background:#e8f4f8;border-left:3px solid #1B5C74;"
                    f"padding:0.4rem 0.75rem;border-radius:0 4px 4px 0;font-size:0.88rem;"
                    f"margin-bottom:0.4rem'>"
                    f"<span style='color:#5a6a7a;font-size:0.75rem'>Ответ · {replied_at}</span>"
                    f"<br>{reply}</div>",
                    unsafe_allow_html=True,
                )

            # Поле ответа — показывается если нет ответа или режим редактирования
            show_input = not reply or st.session_state.get(edit_key)
            if show_input:
                new_reply = st.text_area(
                    "Ответ", value=reply, height=68,
                    key=f"reply_text_{rid}",
                    placeholder="Введите ответ пользователю...",
                    label_visibility="collapsed",
                )
            else:
                new_reply = reply

            # Кнопки в одну строку
            if status == "new":
                bc1, bc2, bc3 = st.columns([2, 2, 1])
                with bc1:
                    btn_label = "Сохранить ответ" if not reply else ("Обновить ответ" if show_input else "Редактировать ответ")
                    if st.button(btn_label, key=f"save_reply_{rid}", use_container_width=True):
                        if not show_input:
                            # Открываем режим редактирования
                            st.session_state[edit_key] = True
                            st.rerun()
                        elif new_reply.strip():
                            save_reply(rid, new_reply)
                            st.session_state.pop(edit_key, None)
                            st.session_state[f"notif_reply_{rid}"] = True
                            st.rerun()
                        else:
                            st.error("Введите текст ответа")
                with bc2:
                    if st.button("Отработать", key=f"help_done_{rid}", use_container_width=True):
                        user = get_current_user()
                        mark_done(rid, done_by=user["user_id"] if user else "superadmin")
                        st.session_state.pop(edit_key, None)
                        st.session_state[f"notif_done_{rid}"] = True
                        st.rerun()
                with bc3:
                    if show_input and reply:
                        if st.button("Отмена", key=f"cancel_edit_{rid}", use_container_width=True):
                            st.session_state.pop(edit_key, None)
                            st.rerun()

            else:  # status == "done"
                bc1, bc2 = st.columns([2, 2])
                with bc1:
                    btn_label = "Редактировать ответ" if (reply and not show_input) else ("Обновить ответ" if show_input else "Добавить ответ")
                    if st.button(btn_label, key=f"save_reply_{rid}", use_container_width=True):
                        if not show_input:
                            st.session_state[edit_key] = True
                            st.rerun()
                        elif new_reply.strip():
                            save_reply(rid, new_reply)
                            st.session_state.pop(edit_key, None)
                            st.session_state[f"notif_reply_{rid}"] = True
                            st.rerun()
                        else:
                            st.error("Введите текст ответа")
                with bc2:
                    if st.button("Взять в работу", key=f"help_reopen_{rid}", use_container_width=True):
                        reopen_request(rid)
                        st.session_state.pop(edit_key, None)
                        st.session_state[f"notif_reopen_{rid}"] = True
                        st.rerun()

            # Нотификейшны
            if st.session_state.pop(f"notif_reply_{rid}", False):
                st.success("Ответ отправлен пользователю")
            if st.session_state.pop(f"notif_done_{rid}", False):
                st.success("Запрос отработан")
            if st.session_state.pop(f"notif_reopen_{rid}", False):
                st.info("Запрос возвращён в работу")


# ─────────────────────────────────────────────────────────────────────────────
# Главная функция страницы
# ─────────────────────────────────────────────────────────────────────────────

def show_superadmin():
    """
    Точка входа страницы управления.
    Вызывается из app.py при выборе пункта "Управление" в сайдбаре.
    Доступна только суперадмину — проверяется через require_auth.
    """
    # Проверяем права — только суперадмин
    # Если не залогинен или не суперадмин — показывает форму входа / ошибку
    user = require_auth(min_role="superadmin")

    st.markdown("### Управление")

    # Метрики верхнего уровня
    segments = _load_segments()
    users    = _load_users()
    today    = str(date.today())

    active_segs  = sum(1 for s in segments.values() if s.get("status") == "active")
    active_users = sum(1 for u in users.values()    if u.get("status") == "active")

    from core.audit import get_daily_stats
    from core.help_requests import count_new as _count_help_new
    daily      = get_daily_stats(today)
    help_count = _count_help_new()

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Сегментов",      active_segs)
    m2.metric("Пользователей",  active_users)
    m3.metric("Событий сегодня", daily["total_events"])
    m4.metric("Активны сегодня", daily["active_users"])
    m5.metric("Запросы на помощь", help_count)

    st.divider()

    # Вкладки
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Сегменты",
        "Пользователи",
        "Статистика",
        "Лог действий",
        "Помощь",
    ])

    with tab1:
        _tab_segments()

    with tab2:
        _tab_users()

    with tab3:
        _tab_stats()

    with tab4:
        _tab_audit_log()

    with tab5:
        _tab_help()