# core/auth.py
"""
Аутентификация РЕГУЛА.AI
════════════════════════════════════════════════════════════════════════════════

Архитектура (серверные сессии через query_params):
  - Пользователи хранятся в data/admin/users.json
  - Суперадмин отдельно в config/superadmin.json
  - Пароли хэшируются bcrypt (work factor 12)
  - После логина создаётся файл сессии data/sessions/{token}.json
  - Токен пишется в st.query_params["s"] — сохраняется при F5
  - При каждом рендере: читаем токен из query_params → проверяем файл
    сессии → проверяем force_logout_flag → загружаем пользователя
  - Сессия живёт SESSION_EXPIRY_DAYS дней
  - force_logout_flag: суперадмин ставит флаг → сессионный файл удаляется
    при следующем запросе → пользователь видит форму входа
  - must_change_password: флаг в users.json → перехватываем после логина
    → показываем только форму смены пароля

Зависимости:
  pip install bcrypt PyJWT

Использование:
  from core.auth import get_current_user, login, logout, require_auth
  user = get_current_user()        # None если не залогинен
  user = require_auth()            # останавливает рендер если не залогинен
  user = require_auth("superadmin") # требует конкретную роль
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

import bcrypt
import streamlit as st

# ── Пути ─────────────────────────────────────────────────────────────────────

_BASE_DIR        = Path(__file__).parent.parent.resolve()
_CONFIG_DIR      = _BASE_DIR / "config"
_DATA_DIR        = _BASE_DIR / "data" / "admin"
_SESSIONS_DIR    = _BASE_DIR / "data" / "sessions"
USERS_FILE       = _DATA_DIR / "users.json"
SEGMENTS_FILE    = _DATA_DIR / "segments.json"
SUPERADMIN_FILE  = _CONFIG_DIR / "superadmin.json"

# ── Константы ─────────────────────────────────────────────────────────────────

SESSION_EXPIRY_DAYS = 30
QUERY_PARAM_KEY     = "s"   # имя параметра в URL: ?s=<token>

# Роли (порядок важен: выше индекс = меньше прав)
ROLES = ["superadmin", "segment_admin", "superuser", "user"]

# ── Блокировки ────────────────────────────────────────────────────────────────
_users_lock    = threading.Lock()
_segments_lock = threading.Lock()
_sessions_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Инициализация директорий
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Серверные сессии
# ─────────────────────────────────────────────────────────────────────────────

def _create_session(user_id: str, org_id: str, role: str) -> str:
    """
    Создаёт файл сессии на сервере.
    Возвращает токен (UUID) который кладётся в URL.
    Токен не содержит данных пользователя — только ссылка на файл.
    """
    _ensure_dirs()
    token   = uuid.uuid4().hex + uuid.uuid4().hex  # 64 символа
    expires = datetime.now() + timedelta(days=SESSION_EXPIRY_DAYS)

    session = {
        "token":      token,
        "user_id":    user_id,
        "org_id":     org_id,
        "role":       role,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
    }

    session_file = _SESSIONS_DIR / f"{token}.json"
    with _sessions_lock:
        session_file.write_text(
            json.dumps(session, ensure_ascii=False), encoding="utf-8"
        )
    return token


def _read_session(token: str) -> Optional[Dict]:
    """
    Читает файл сессии по токену.
    Возвращает данные сессии или None если сессия не найдена / истекла.
    """
    if not token:
        return None

    # Защита от path traversal — токен должен быть только hex-символами
    if not all(c in "0123456789abcdef" for c in token):
        return None

    session_file = _SESSIONS_DIR / f"{token}.json"
    if not session_file.exists():
        return None

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Проверяем срок действия
    try:
        expires = datetime.fromisoformat(data["expires_at"])
        if datetime.now() > expires:
            _delete_session_file(token)
            return None
    except Exception:
        return None

    return data


def _delete_session_file(token: str) -> None:
    """Удаляет файл сессии с диска."""
    if not token:
        return
    session_file = _SESSIONS_DIR / f"{token}.json"
    try:
        if session_file.exists():
            session_file.unlink()
    except Exception:
        pass


def _delete_all_user_sessions(user_id: str) -> None:
    """Удаляет все сессии пользователя (при force_logout)."""
    _ensure_dirs()
    for f in _SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("user_id") == user_id:
                f.unlink()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Работа с пользователями
# ─────────────────────────────────────────────────────────────────────────────

def _load_users() -> Dict[str, Dict]:
    if not USERS_FILE.exists():
        return {}
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_users(users: Dict[str, Dict]) -> None:
    _ensure_dirs()
    with _users_lock:
        USERS_FILE.write_text(
            json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _get_user_by_login(login: str) -> Optional[Dict]:
    users = _load_users()
    login = login.strip().lower()
    for uid, rec in users.items():
        if rec.get("login", "").strip().lower() == login:
            return {**rec, "user_id": uid}
    return None


def _get_user_by_id(user_id: str) -> Optional[Dict]:
    users = _load_users()
    rec   = users.get(user_id)
    if rec:
        return {**rec, "user_id": user_id}
    return None


def _get_superadmin() -> Optional[Dict]:
    if not SUPERADMIN_FILE.exists():
        return None
    try:
        data = json.loads(SUPERADMIN_FILE.read_text(encoding="utf-8"))
        if data.get("login"):
            return {**data, "role": "superadmin", "user_id": "superadmin"}
        return None
    except Exception:
        return None


def _save_superadmin_field(field: str, value) -> None:
    _ensure_dirs()
    data: Dict = {}
    if SUPERADMIN_FILE.exists():
        try:
            data = json.loads(SUPERADMIN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    data[field] = value
    SUPERADMIN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Хэширование паролей
# ─────────────────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Основная логика сессии
# ─────────────────────────────────────────────────────────────────────────────

def get_current_user() -> Optional[Dict]:
    """
    Возвращает данные текущего пользователя или None если не залогинен.

    Порядок проверки:
      1. Уже есть данные в session_state → быстрый путь
      2. Читаем токен из st.query_params
      3. Читаем файл сессии с диска
      4. Проверяем force_logout_flag
      5. Загружаем актуальные данные пользователя
    """
    # Быстрый путь
    if st.session_state.get("_auth_user"):
        user = st.session_state["_auth_user"]
        # Проверяем force_logout_flag при каждом рендере
        if user.get("role") != "superadmin":
            rec = _get_user_by_id(user["user_id"])
            if rec and rec.get("force_logout_flag"):
                _logout_current()
                return None
        return user

    # Читаем токен из URL
    token = st.query_params.get(QUERY_PARAM_KEY, "")
    if not token:
        return None

    # Читаем сессию с диска
    session = _read_session(token)
    if not session:
        # Сессия не найдена или истекла — чистим URL
        _clear_token_from_url()
        return None

    user_id = session["user_id"]
    role    = session["role"]

    # Суперадмин
    if role == "superadmin" and user_id == "superadmin":
        sa = _get_superadmin()
        if not sa:
            _logout_current()
            return None
        user = {
            "user_id":              "superadmin",
            "org_id":               None,
            "role":                 "superadmin",
            "name":                 sa.get("name", "Суперадмин"),
            "login":                sa.get("login", ""),
            "status":               "active",
            "must_change_password": False,
            "force_logout_flag":    False,
            "session_token":        token,
        }
        st.session_state["_auth_user"] = user
        return user

    # Обычный пользователь
    rec = _get_user_by_id(user_id)
    if not rec:
        _logout_current()
        return None

    # force_logout — удаляем все сессии пользователя
    if rec.get("force_logout_flag"):
        _delete_all_user_sessions(user_id)
        _clear_force_logout_flag(user_id)
        _clear_token_from_url()
        st.session_state.pop("_auth_user", None)
        return None

    # Проверяем статус
    status = rec.get("status", "active")
    if status in ("archived", "blocked"):
        _logout_current()
        return None

    # Временная блокировка
    blocked_until = rec.get("blocked_until")
    if blocked_until:
        try:
            if datetime.fromisoformat(blocked_until) > datetime.now():
                _logout_current()
                return None
        except Exception:
            pass

    user = {
        "user_id":              user_id,
        "org_id":               rec.get("org_id"),
        "role":                 rec.get("role", "user"),
        "name":                 rec.get("name", ""),
        "login":                rec.get("login", ""),
        "status":               status,
        "must_change_password": rec.get("must_change_password", False),
        "force_logout_flag":    False,
        "session_token":        token,
    }
    st.session_state["_auth_user"] = user
    return user


def _clear_token_from_url() -> None:
    """Убирает токен из URL."""
    try:
        if QUERY_PARAM_KEY in st.query_params:
            del st.query_params[QUERY_PARAM_KEY]
    except Exception:
        pass


def _clear_force_logout_flag(user_id: str) -> None:
    users = _load_users()
    if user_id in users:
        users[user_id]["force_logout_flag"] = False
        _save_users(users)


def _logout_current() -> None:
    """Внутренний выход — чистит session_state и URL."""
    token = st.session_state.get("_auth_user", {}).get("session_token", "")
    if token:
        _delete_session_file(token)
    st.session_state.pop("_auth_user", None)
    _clear_token_from_url()


# ─────────────────────────────────────────────────────────────────────────────
# Логин / Логаут
# ─────────────────────────────────────────────────────────────────────────────

def login(login_str: str, password: str) -> tuple[bool, str]:
    """
    Проверяет логин и пароль, создаёт серверную сессию, пишет токен в URL.
    Возвращает (success, error_message).
    """
    login_str = login_str.strip().lower()

    # Суперадмин
    sa = _get_superadmin()
    if sa and sa.get("login", "").strip().lower() == login_str:
        if verify_password(password, sa.get("password_hash", "")):
            token = _create_session("superadmin", org_id="", role="superadmin")
            st.query_params[QUERY_PARAM_KEY] = token
            _update_last_login("superadmin")
            return True, ""
        return False, "Неверный пароль"

    # Обычный пользователь
    rec = _get_user_by_login(login_str)
    if not rec:
        return False, "Неверный логин или пароль"

    status = rec.get("status", "active")
    if status == "archived":
        return False, "Аккаунт деактивирован. Обратитесь к администратору"
    if status == "blocked":
        until = rec.get("blocked_until", "")
        msg   = "Аккаунт временно заблокирован"
        if until:
            try:
                dt = datetime.fromisoformat(until)
                if dt > datetime.now():
                    msg += f" до {dt.strftime('%d.%m.%Y %H:%M')}"
                else:
                    # Блокировка истекла
                    status = "active"
            except Exception:
                pass
        if status == "blocked":
            return False, msg

    if not verify_password(password, rec.get("password_hash", "")):
        return False, "Неверный логин или пароль"

    uid    = rec["user_id"]
    org_id = rec.get("org_id", "")
    role   = rec.get("role", "user")

    token = _create_session(uid, org_id=org_id, role=role)
    st.query_params[QUERY_PARAM_KEY] = token
    _update_last_login(uid)

    return True, ""


def logout() -> None:
    """Завершает сессию: удаляет файл сессии и чистит state."""
    _logout_current()


def _update_last_login(user_id: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    if user_id == "superadmin":
        _save_superadmin_field("last_login", now)
        return
    users = _load_users()
    if user_id in users:
        users[user_id]["last_login"] = now
        _save_users(users)


# ─────────────────────────────────────────────────────────────────────────────
# Управление пользователями
# ─────────────────────────────────────────────────────────────────────────────

def create_user(
    name:      str,
    login_str: str,
    password:  str,
    org_id:    str,
    role:      str,
) -> tuple[bool, str]:
    if role == "superadmin":
        return False, "Суперадмин создаётся только через init_superadmin()"

    login_str = login_str.strip().lower()
    if _get_user_by_login(login_str):
        return False, f"Логин {login_str} уже занят"

    users = _load_users()
    uid   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    now   = datetime.now().isoformat(timespec="seconds")

    users[uid] = {
        "name":                 name.strip(),
        "login":                login_str,
        "password_hash":        hash_password(password),
        "org_id":               org_id,
        "role":                 role,
        "created_at":           now,
        "status":               "active",
        "blocked_until":        None,
        "last_login":           None,
        "force_logout_flag":    False,
        "must_change_password": False,
    }
    _save_users(users)
    return True, uid


def reset_password(user_id: str) -> tuple[bool, str]:
    """
    Сбрасывает пароль. Генерирует временный пароль, хэширует.
    Устанавливает must_change_password = True.
    Временный пароль показывается суперадмину ОДИН РАЗ — в файл не пишется.
    """
    import secrets, string
    alphabet  = string.ascii_letters.replace("l","").replace("O","") + string.digits.replace("0","").replace("1","")
    temp_pass = "".join(secrets.choice(alphabet) for _ in range(12))

    users = _load_users()
    if user_id not in users:
        return False, ""

    users[user_id]["password_hash"]        = hash_password(temp_pass)
    users[user_id]["must_change_password"] = True
    users[user_id]["force_logout_flag"]    = True
    _save_users(users)
    return True, temp_pass


def set_force_logout(user_id: str) -> bool:
    """
    Устанавливает force_logout_flag.
    При следующем рендере все сессии пользователя будут удалены.
    """
    if user_id == "superadmin":
        return False
    users = _load_users()
    if user_id not in users:
        return False
    users[user_id]["force_logout_flag"] = True
    _save_users(users)
    return True


def set_user_status(user_id: str, status: str, blocked_until: Optional[str] = None) -> bool:
    if status not in ("active", "blocked", "archived"):
        return False
    users = _load_users()
    if user_id not in users:
        return False
    users[user_id]["status"]        = status
    users[user_id]["blocked_until"] = blocked_until
    if status in ("archived", "blocked"):
        users[user_id]["force_logout_flag"] = True
    _save_users(users)
    return True


def change_password(user_id: str, old_pass: str, new_pass: str) -> tuple[bool, str]:
    users = _load_users()
    if user_id not in users:
        return False, "Пользователь не найден"
    rec = users[user_id]
    if not verify_password(old_pass, rec.get("password_hash", "")):
        return False, "Неверный текущий пароль"
    if len(new_pass) < 8:
        return False, "Пароль должен содержать минимум 8 символов"
    users[user_id]["password_hash"]        = hash_password(new_pass)
    users[user_id]["must_change_password"] = False
    _save_users(users)
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Проверка прав
# ─────────────────────────────────────────────────────────────────────────────

def require_auth(min_role: str = "user") -> Dict:
    """
    Проверяет авторизацию и права. Если не залогинен — показывает форму входа.
    Если недостаточно прав — показывает ошибку. Иначе возвращает данные пользователя.
    """
    user = get_current_user()

    if not user:
        _show_login_page()
        st.stop()

    user_role_idx = ROLES.index(user["role"]) if user["role"] in ROLES else len(ROLES)
    min_role_idx  = ROLES.index(min_role)     if min_role in ROLES     else len(ROLES)

    if user_role_idx > min_role_idx:
        st.error("Недостаточно прав для доступа к этому разделу")
        st.stop()

    if user.get("must_change_password") and not st.session_state.get("_changing_password"):
        _show_change_password_page(user)
        st.stop()

    return user


def has_role(user: Dict, min_role: str) -> bool:
    if not user:
        return False
    user_role_idx = ROLES.index(user.get("role","user")) if user.get("role") in ROLES else len(ROLES)
    min_role_idx  = ROLES.index(min_role)                if min_role in ROLES          else len(ROLES)
    return user_role_idx <= min_role_idx


def is_same_segment(user: Dict, org_id: str) -> bool:
    if user.get("role") == "superadmin":
        return True
    return user.get("org_id") == org_id


# ─────────────────────────────────────────────────────────────────────────────
# Страницы входа и смены пароля
# ─────────────────────────────────────────────────────────────────────────────

def _show_login_page() -> None:
    st.markdown("""
    <div style="max-width:380px;margin:4rem auto 0;text-align:center">
        <div style="font-size:2rem;font-weight:900;color:#1B5C74;margin-bottom:0.3rem">
            РЕГУЛА.AI
        </div>
        <div style="font-size:0.8rem;color:#5a6a7a;letter-spacing:.08em;
                    text-transform:uppercase;margin-bottom:2rem">
            вход в систему
        </div>
    </div>
    """, unsafe_allow_html=True)

    col = st.columns([1, 2, 1])[1]
    with col:
        login_input = st.text_input("Логин", key="_login_input", placeholder="email или логин")
        pass_input  = st.text_input("Пароль", type="password", key="_pass_input")

        if st.button("Войти", use_container_width=True, type="primary"):
            if not login_input or not pass_input:
                st.error("Введите логин и пароль")
            else:
                ok, err = login(login_input, pass_input)
                if ok:
                    try:
                        from core.audit import log_event
                        user = get_current_user()
                        if user:
                            log_event(
                                org_id=user.get("org_id", ""),
                                user_id=user["user_id"],
                                role=user["role"],
                                event="login",
                                module=None,
                            )
                    except Exception:
                        pass
                    st.rerun()
                else:
                    st.error(err)


def _show_change_password_page(user: Dict) -> None:
    st.info("Администратор сбросил ваш пароль. Для продолжения установите новый пароль.")
    st.markdown("### Смена пароля")

    old_p  = st.text_input("Текущий (временный) пароль", type="password", key="_cp_old")
    new_p  = st.text_input("Новый пароль (минимум 8 символов)", type="password", key="_cp_new")
    new_p2 = st.text_input("Повторите новый пароль", type="password", key="_cp_new2")

    if st.button("Сохранить пароль", type="primary"):
        if new_p != new_p2:
            st.error("Пароли не совпадают")
        else:
            ok, err = change_password(user["user_id"], old_p, new_p)
            if ok:
                st.session_state.pop("_auth_user", None)
                st.success("Пароль изменён. Добро пожаловать!")
                st.rerun()
            else:
                st.error(err)


# ─────────────────────────────────────────────────────────────────────────────
# Инициализация суперадмина
# ─────────────────────────────────────────────────────────────────────────────

def init_superadmin(name: str, login_str: str, password: str) -> bool:
    """
    Создаёт суперадмина в config/superadmin.json.
    Если уже существует — не перезаписывает.
    Запускается один раз через init_superadmin.py при первом деплое.
    """
    _ensure_dirs()
    if SUPERADMIN_FILE.exists():
        existing = json.loads(SUPERADMIN_FILE.read_text(encoding="utf-8"))
        if existing.get("login"):
            print(f"[auth] Суперадмин уже существует: {existing['login']}")
            return False

    data = {
        "name":          name.strip(),
        "login":         login_str.strip().lower(),
        "password_hash": hash_password(password),
        "created_at":    datetime.now().isoformat(timespec="seconds"),
        "last_login":    None,
    }
    SUPERADMIN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[auth] Суперадмин создан: {login_str}")
    return True