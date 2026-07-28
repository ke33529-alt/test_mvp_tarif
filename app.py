import streamlit as st
import os
import sys
from datetime import datetime
import json

# Подавляем баг телеметрии ChromaDB
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.feedback import submit_feedback, get_feedback, get_answer_stats
from core import admin

from streamlit_pages.advisor_page import show_advisor
from streamlit_pages.admin_panel import show_admin_panel, get_live_answer_stats
from streamlit_pages.tasks_page import show_tasks

# Аутентификация и модули
from core.auth import get_current_user, logout, _show_login_page
from core.audit import log_event
from core.modules import (
    get_visible_modules, get_module_status,
    is_module_accessible, ALL_MODULES, MODULE_STATUS_LABELS,
)
from core.help_requests import submit_request as submit_help, count_new as count_help_new, has_unread_replies, has_unseen_done, mark_read_by_user

# =============================================================================
# 🎨 Настройка страницы
# =============================================================================
st.set_page_config(page_title="РЕГУЛА.AI", layout="wide", page_icon="⚙")

# =============================================================================
# 🧪 ВРЕМЕННО: диагностика зависания загрузки файлов
# =============================================================================
if st.query_params.get("debug") == "upload_test":
    from streamlit_pages.debug_upload_test import show_debug_upload_test
    show_debug_upload_test()
    st.stop()

# =============================================================================
# 🔐 Session state
# =============================================================================
if "admin_logged_in" not in st.session_state:
    st.session_state.admin_logged_in = False
if "show_landing" not in st.session_state:
    st.session_state.show_landing = True

# =============================================================================
# 🎨 CSS
# =============================================================================
st.markdown("""
<style>
:root {
    --brand-primary:  #1B5C74;
    --brand-mid:      #2E7A96;
    --brand-light1:   #4FA3C0;
    --brand-light2:   #c8e8f2;
    --brand-bg:       #e8f4f8;
    --brand-dark:     #063971;
    --neutral-bg:     #f4f6f9;
    --neutral-border: #dce3ec;
    --text-primary:   #1a2a3a;
    --text-secondary: #5a6a7a;
    --radius:         6px;
}
.stApp { background-color: var(--neutral-bg);
         font-family: "Inter", "Segoe UI", system-ui, sans-serif; }
h1, h2, h3, h4 { color: var(--text-primary); font-family: inherit; }
.stSidebar { background-color: #ffffff; border-right: 1px solid var(--neutral-border); }
.stSidebar * { text-align: left !important; }
.stButton > button {
    background-color: var(--brand-primary) !important;
    color: #ffffff !important;
    border-radius: var(--radius) !important;
    border: 1px solid var(--brand-primary) !important;
    outline: none !important;
    box-shadow: none !important;
    padding: 0.45rem 1rem !important;
    width: 100% !important;
    font-weight: 500 !important;
    transition: background-color 0.15s ease !important;
}
.stButton > button:hover {
    background-color: var(--brand-dark) !important;
    border-color: var(--brand-dark) !important;
    box-shadow: none !important;
}
.stButton > button:focus,
.stButton > button:focus-visible {
    box-shadow: 0 0 0 2px var(--brand-light2) !important;
    outline: none !important;
}
.stButton > button[kind="secondary"] {
    background-color: #ffffff !important;
    color: var(--brand-primary) !important;
    border: 1px solid var(--brand-primary) !important;
}
.stButton > button[kind="secondary"]:hover {
    background-color: var(--brand-bg) !important;
}
[data-testid="stExpander"] {
    border: 1px solid var(--neutral-border) !important;
    border-radius: var(--radius) !important;
    background: #ffffff !important;
    box-shadow: none !important;
}
[data-testid="stExpander"] > details,
[data-testid="stExpander"] > details > summary,
[data-testid="stExpander"] > details > div {
    border: none !important; box-shadow: none !important; outline: none !important;
}
[data-testid="stExpander"] > details > summary {
    border-bottom: 1px solid var(--neutral-border) !important;
    border-radius: 0 !important; padding: 0.6rem 0.9rem !important;
}
[data-testid="stExpander"] > details:not([open]) > summary { border-bottom: none !important; }
[data-testid="stSlider"] [role="slider"],
[data-testid="stSlider"] [class*="thumb"] {
    background-color: var(--brand-mid) !important;
    border-color: var(--brand-mid) !important;
    box-shadow: 0 0 0 3px var(--brand-light2) !important;
}
[data-testid="stSlider"] [class*="track"]:last-child,
[data-testid="stSlider"] [class*="Track"]:last-child { background-color: var(--brand-mid) !important; }
[data-testid="stProgress"] > div { background-color: var(--brand-light2) !important; border-radius: 4px !important; }
[data-testid="stProgress"] > div > div { background-color: var(--brand-mid) !important; border-radius: 4px !important; }
[data-testid="stProgress"] p, [data-testid="stProgress"] span,
[data-testid="stProgress"] div > p, [data-testid="stProgress"] + div p {
    color: var(--text-primary) !important; font-size: 0.82em !important;
}
[data-testid="stRadio"] label:hover { color: var(--brand-primary) !important; }
[data-testid="stRadio"] div[aria-checked="true"] ~ label {
    color: var(--brand-primary) !important; font-weight: 600 !important;
}
[data-baseweb="tab-list"] { border-bottom: 2px solid var(--neutral-border) !important; gap: 0 !important; }
[data-baseweb="tab"] {
    border-radius: var(--radius) var(--radius) 0 0 !important;
    border: none !important; color: var(--text-secondary) !important;
    font-weight: 500 !important; padding: 0.5rem 1.1rem !important;
    transition: color 0.15s ease, background-color 0.15s ease !important;
}
[data-baseweb="tab"]:hover { color: var(--brand-primary) !important; background-color: var(--brand-bg) !important; }
[aria-selected="true"][data-baseweb="tab"] {
    color: var(--brand-primary) !important; font-weight: 700 !important;
    border-bottom: 3px solid var(--brand-primary) !important; background-color: #ffffff !important;
}
[data-baseweb="tab-highlight"] { background-color: var(--brand-primary) !important; height: 3px !important; }
[data-testid="stAlert"][data-baseweb="notification"] { border-radius: var(--radius) !important; }
div[data-testid="stAlert"][kind="info"],
div.element-container div[data-baseweb="notification"][kind="info"] {
    background-color: var(--brand-bg) !important;
    border-left: 4px solid var(--brand-primary) !important;
    color: var(--text-primary) !important;
}
.stAlert > div[data-testid="stMarkdownContainer"] { color: var(--text-primary) !important; }
[data-testid="stNotification"], [class*="AlertContainer"] {
    background-color: var(--brand-bg) !important;
    border-left: 4px solid var(--brand-primary) !important;
    border-radius: var(--radius) !important; color: var(--text-primary) !important;
}
[class*="AlertContainer"] svg { color: var(--brand-primary) !important; }
.stDataFrame { min-height: 200px; }
.dataframe { border: 1px solid var(--neutral-border); border-radius: var(--radius); }
.stMetric { background: #ffffff; padding: 0.5rem;
            border-radius: var(--radius); border: 1px solid var(--neutral-border); }
.redirect-box {
    margin: 1rem 0; padding: 1rem; background: var(--brand-bg);
    border-left: 4px solid var(--brand-primary);
    border-radius: 0 var(--radius) var(--radius) 0;
}
.sidebar-logo button {
    background: none !important; border: none !important;
    box-shadow: none !important; padding: 0.3rem 0 !important;
    width: auto !important; font-size: 1.15rem !important;
    font-weight: 800 !important; letter-spacing: 0.03em !important;
    color: var(--brand-primary) !important;
    -webkit-text-fill-color: var(--brand-primary) !important;
    cursor: pointer !important; transition: opacity 0.15s !important;
}
.sidebar-logo button:hover { opacity: 0.7 !important; background-color: transparent !important; }
[data-testid="stSidebar"] .stButton > button {
    background-color: transparent !important;
    color: var(--text-secondary) !important;
    border: none !important; box-shadow: none !important;
    padding: 0.28rem 0.75rem !important; font-weight: 400 !important;
    font-size: 0.88rem !important; border-radius: var(--radius) !important;
    width: 100% !important; min-height: 0 !important; height: auto !important;
    display: flex !important; align-items: center !important;
    justify-content: flex-start !important;
    transition: background-color 0.12s ease, color 0.12s ease !important;
}
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stButton > button span,
[data-testid="stSidebar"] .stButton > button div {
    text-align: left !important; margin: 0 !important;
    padding: 0 !important; width: 100% !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background-color: var(--brand-bg) !important;
    color: var(--brand-primary) !important;
    border: none !important; box-shadow: none !important;
}
[data-testid="stSidebar"] .stButton > button:focus,
[data-testid="stSidebar"] .stButton > button:focus-visible {
    box-shadow: none !important; border: none !important; outline: none !important;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background-color: rgba(27, 92, 116, 0.1) !important;
    color: var(--brand-primary) !important; font-weight: 600 !important;
    border-left: 3px solid var(--brand-primary) !important;
    border-radius: 0 var(--radius) var(--radius) 0 !important;
    padding: 0.28rem 0.6rem 0.28rem 0.55rem !important;
    justify-content: space-between !important;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover {
    background-color: rgba(27, 92, 116, 0.16) !important;
    border-left: 3px solid var(--brand-primary) !important;
    box-shadow: none !important;
}
[data-testid="stSidebar"] .stButton > button[kind="primary"]::after {
    content: "›"; font-size: 1rem; font-weight: 300;
    opacity: 0.55; flex-shrink: 0; padding-left: 0.3rem;
}
[data-testid="stSidebar"] .element-container {
    margin-top: 0 !important; margin-bottom: 0 !important;
}
.nav-section-label {
    font-size: 0.68rem; font-weight: 600; letter-spacing: 0.09em;
    text-transform: uppercase; color: var(--text-secondary);
    padding: 0.6rem 0.75rem 0.2rem; margin-top: 0.2rem;
}
.landing-tile button {
    min-height: 52px !important; text-align: left !important;
    background: #ffffff !important; color: var(--text-primary) !important;
    border: 1.5px solid var(--neutral-border) !important;
    border-radius: var(--radius) !important; padding: 0.9rem 1.1rem !important;
    font-size: 0.95rem !important; font-weight: 600 !important;
    line-height: 1.4 !important; box-shadow: 0 1px 4px rgba(0,0,0,0.06) !important;
    transition: all 0.15s ease !important;
}
.landing-tile button:hover {
    border-color: var(--brand-primary) !important;
    background: var(--brand-bg) !important;
    box-shadow: 0 4px 14px rgba(27,92,116,0.12) !important;
    transform: translateY(-2px) !important; color: var(--brand-dark) !important;
}
.landing-tile-desc {
    font-size: 0.78rem; color: var(--text-secondary);
    margin-top: 0.15rem; margin-bottom: 1rem;
    padding: 0 0.15rem; line-height: 1.45; display: block;
}
.landing-tile-desc ul { margin: 0.2rem 0 0 0; padding-left: 1.1rem; list-style: disc; }
.landing-tile-desc ul li { margin-bottom: 0.2rem; }
/* Заблокированная плитка — недоступный модуль */
.landing-tile-locked {
    min-height: 52px; background: #f0f2f5;
    border: 1.5px dashed var(--neutral-border);
    border-radius: var(--radius); padding: 0.9rem 1.1rem;
    cursor: not-allowed; position: relative;
    transition: all 0.15s ease;
}
.landing-tile-locked:hover {
    background: #e8ebef;
    border-color: #b0b8c4;
}
.landing-tile-locked-inner {
    display: flex; align-items: center; gap: 8px;
    font-size: 0.95rem; font-weight: 600; color: #9aa5b1;
}
.landing-tile-locked .lock-icon { font-size: 0.9rem; }
.landing-metric {
    background: #ffffff; border: 1px solid var(--neutral-border);
    border-radius: var(--radius); padding: 0.8rem 1rem;
    text-align: center; margin-bottom: 0.5rem;
    min-height: 160px; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
}
.landing-metric-value { font-size: 1.5rem; font-weight: 700; color: var(--brand-primary); }
.landing-metric-label { font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.2rem; }
.main-title { display: none; }
[data-testid="stSidebar"] .stButton > button:active {
    opacity: 0.6 !important; transform: scale(0.97) !important;
    transition: all 0.05s ease !important;
}
</style>
""", unsafe_allow_html=True)

# =============================================================================
# 📋 Реестр активных продуктов
# =============================================================================
# Порядок должен совпадать с ALL_MODULES в core/modules.py
_ACTIVE_PRODUCTS = [
    "Советчик", "Сканер документов", "Анализатор заявок",
    "Прогноз решения регулятора", "Протокольщик", "Задачи", "Админка",
]

# Соответствие названия продукта → module_id в core/modules.py
_CHOICE_TO_MODULE = {
    "Советчик":                   "advisor",
    "Сканер документов":          "scanner",
    "Анализатор заявок":          "analyzer",
    "Прогноз решения регулятора": "predictor",
    "Протокольщик":               "protocol",
    "Задачи":                     "tasks",
}

_DEV_PRODUCTS = [
    "Позиция ФАС", "Поиск прецедентов", "Сверка численности",
    "Проверка амортизации", "Экспорт ФГИС", "Пояснительная записка",
    "Калькулятор рисков", "Жалобщик", "Трекер изменений законов",
    "Расчетный лист", "Прогнозист тарифов", "Сравнение с аналогами",
    "Режим обучения", "Наведение порядка в документах",
    "Планировщик кампании", "Прогноз потребления",
]
_PRODUCT_DESCRIPTIONS = {
    "Советчик":
        "<ul><li>Отвечает на вопросы по нормативной базе тарифного регулирования</li>"
        "<li>Снижает нагрузку на специалистов на 30%</li>"
        "<li>Ссылается на актуальные НПА с точными цитатами</li></ul>",
    "Сканер документов":
        "<ul><li>Распознаёт текст из PDF, DOCX и сканов</li>"
        "<li>Формирует базу знаний из ваших документов</li>"
        "<li>Поддерживает пересказ и полнотекстовый поиск</li></ul>",
    "Анализатор заявок":
        "<ul><li>Проверяет комплектность тарифной заявки</li>"
        "<li>Подсвечивает риски по каждой статье затрат</li>"
        "<li>Повышает проходимость заявок у регулятора</li></ul>",
    "Прогноз решения регулятора":
        "<ul><li>Оценивает вероятность одобрения заявки</li>"
        "<li>Опирается на исторические данные решений</li>"
        "<li>Снижает риски отклонения статей затрат</li></ul>",
    "Протокольщик":
        "<ul><li>Составляет протоколы заседаний из аудио или текста</li>"
        "<li>Структурирует и форматирует содержание автоматически</li>"
        "<li>Сокращает время подготовки протокола в разы</li></ul>",
    "Админка":
        "<ul><li>Загрузка и индексация документов базы знаний</li>"
        "<li>Настройка параметров поиска и промптов</li>"
        "<li>Аналитика использования и качества ответов</li></ul>",
    "Задачи":
        "<ul><li>Короткие заметки на 500 символов — что нужно запросить в системе</li>"
        "<li>Статус, приоритет и срок исполнения с цветовой подсветкой</li>"
        "<li>Быстрая загрузка задачи прямо в Советчик</li></ul>",
}

if "main_choice" not in st.session_state:
    st.session_state.main_choice = _ACTIVE_PRODUCTS[0]

# =============================================================================
# 🔐 Проверка авторизации
# =============================================================================
_current_user = get_current_user()
if not _current_user:
    _show_login_page()
    st.stop()

_is_superadmin = _current_user.get("role") == "superadmin"

# Heartbeat — метка активности для индикатора онлайн в управлении
try:
    import threading as _threading
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _datetime
    _sessions_dir = _Path(__file__).parent / "data" / "sessions"
    _sessions_dir.mkdir(parents=True, exist_ok=True)
    _hb_file = _sessions_dir / f"{_current_user['user_id']}.json"
    _hb_lock = _threading.Lock()
    with _hb_lock:
        _hb_file.write_text(
            _json.dumps({
                "user_id": _current_user["user_id"],
                "org_id":  _current_user.get("org_id", ""),
                "ts":      _datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
except Exception:
    pass

# =============================================================================
# 💬 Диалог помощи
# =============================================================================
@st.dialog("Запрос помощи")
def _show_help_dialog():
    st.markdown("Опишите проблему или задайте вопрос — суперадмин получит ваше сообщение.")
    text = st.text_area(
        "Сообщение",
        max_chars=500,
        height=150,
        placeholder="Опишите что случилось или что хотите уточнить...",
        key="_help_text",
    )
    st.caption(f"{len(text)}/500 символов")
    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("Отмена", use_container_width=True):
            st.rerun()
    with c2:
        if st.button("Отправить", type="primary", use_container_width=True):
            if not text.strip():
                st.error("Напишите сообщение")
            else:
                # Определяем название сегмента для записи
                _seg_name = ""
                if _current_user.get("org_id"):
                    try:
                        import json as _j
                        from pathlib import Path as _P
                        _sf = _P(__file__).parent / "data" / "admin" / "segments.json"
                        _segs = _j.loads(_sf.read_text(encoding="utf-8"))
                        _seg_name = _segs.get(_current_user["org_id"], {}).get("name", "")
                    except Exception:
                        pass

                submit_help(
                    user_id=_current_user["user_id"],
                    user_name=_current_user.get("name", ""),
                    org_id=_current_user.get("org_id", ""),
                    org_name=_seg_name,
                    text=text,
                )
                st.success("Сообщение отправлено. Мы свяжемся с вами.")
                st.rerun()

# Показываем диалог если флаг установлен
if st.session_state.get("_show_help_dialog"):
    st.session_state._show_help_dialog = False
    _show_help_dialog()
# Определяем доступные модули для текущего пользователя через core/modules.py.
# get_visible_modules() возвращает список module_id в правильном порядке.
# Суперадмин всегда видит все модули.
_visible_module_ids = get_visible_modules(_current_user)

# Строим список отображаемых продуктов из видимых module_id
_MODULE_TO_CHOICE = {v: k for k, v in _CHOICE_TO_MODULE.items()}
_visible_products = [
    _MODULE_TO_CHOICE[mid]
    for mid in _visible_module_ids
    if mid in _MODULE_TO_CHOICE
]

# Добавляем Админку для segment_admin и superadmin
if _current_user.get("role") in ("segment_admin", "superadmin") and "Админка" not in _visible_products:
    _visible_products.append("Админка")

with st.sidebar:
    st.markdown('<div class="sidebar-logo">', unsafe_allow_html=True)
    if st.button("РЕГУЛА.AI — Главная", key="sidebar_home_btn"):
        st.session_state.show_landing = True
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    st.divider()

    _on_product_page = not st.session_state.get("show_landing", True)

    for _product in _ACTIVE_PRODUCTS:
        # Админка выносится в отдельный блок внизу — пропускаем здесь
        if _product == "Админка":
            continue
        # Скрываем модуль если он не в списке видимых для этого пользователя
        if _product not in _visible_products:
            continue

        _is_active = _on_product_page and st.session_state.main_choice == _product

        # Проверяем статус модуля — если на обслуживании, показываем иначе
        _mid    = _CHOICE_TO_MODULE.get(_product)
        _status = get_module_status(_current_user, _mid) if _mid else "active"

        if _status == "maintenance":
            # Показываем модуль серым с иконкой обслуживания — не кнопка
            st.markdown(
                f'<div style="padding:0.28rem 0.75rem;font-size:0.88rem;'
                f'color:#B0B8C4;display:flex;align-items:center;gap:6px">'
                f'<span>🔧</span><span>{_product}</span>'
                f'<span style="font-size:10px">(обслуживание)</span></div>',
                unsafe_allow_html=True,
            )
        else:
            if st.button(
                _product,
                key=f"nav_active_{_product}",
                use_container_width=True,
                type="primary" if _is_active else "secondary",
            ):
                st.session_state.main_choice = _product
                st.session_state.show_landing = False
                st.rerun()

    # ── Задачи попадают сюда автоматически как часть _ACTIVE_PRODUCTS —
    # отдельного блока больше нет, гейтинг (видимость/обслуживание) для
    # них общий с остальными модулями, через _visible_products/get_module_status.

    st.divider()
    _dev_expanded = st.session_state.main_choice in _DEV_PRODUCTS
    with st.expander("Наши планы", expanded=_dev_expanded):
        st.caption("Продукты в активной разработке, доступны для ознакомления.")
        for _product in _DEV_PRODUCTS:
            _is_active = _on_product_page and st.session_state.main_choice == _product
            if st.button(
                _product,
                key=f"nav_dev_{_product}",
                use_container_width=True,
                type="primary" if _is_active else "secondary",
            ):
                st.session_state.main_choice = _product
                st.session_state.show_landing = False
                st.rerun()

    # ── Служебные разделы после "Наши планы" ──────────────────────────────────
    # Админка — для segment_admin и superadmin
    # Локальная база знаний — для segment_admin и superadmin
    # Управление — только для superadmin
    _has_admin = "Админка" in _visible_products
    _can_manage_local_kb = _current_user.get("role") in ("segment_admin", "superadmin")
    _show_service_block = _has_admin or _can_manage_local_kb or _is_superadmin

    if _show_service_block:
        st.divider()

        # Админка
        if _has_admin:
            _admin_active = _on_product_page and st.session_state.main_choice == "Админка"
            if st.button(
                "Админка",
                key="nav_admin",
                use_container_width=True,
                type="primary" if _admin_active else "secondary",
            ):
                st.session_state.main_choice = "Админка"
                st.session_state.show_landing = False
                st.rerun()

        # Локальная база знаний — доступна segment_admin и суперадмину
        if _can_manage_local_kb:
            _local_kb_active = _on_product_page and st.session_state.main_choice == "Локальная база знаний"
            if st.button(
                "Локальная база знаний",
                key="nav_local_kb",
                use_container_width=True,
                type="primary" if _local_kb_active else "secondary",
            ):
                st.session_state.main_choice = "Локальная база знаний"
                st.session_state.show_landing = False
                st.rerun()

        # Управление — только суперадмин
        if _is_superadmin:
            _mgmt_active = _on_product_page and st.session_state.main_choice == "Управление"
            if st.button(
                "Управление",
                key="nav_superadmin",
                use_container_width=True,
                type="primary" if _mgmt_active else "secondary",
            ):
                st.session_state.main_choice = "Управление"
                st.session_state.show_landing = False
                st.rerun()

    st.divider()
    # Кнопка помощи — видна всем пользователям
    if st.button("Помощь", key="sidebar_help_btn", use_container_width=True):
        st.session_state._show_help_dialog = True
        st.rerun()

    # Кнопка "Мои обращения" с индикатором:
    # 🔴 новые ответы     — есть непрочитанные ответы (приоритет выше)
    # 🟢 обращение отработано — есть отработанные которые пользователь не видел
    # без индикатора      — всё просмотрено
    _has_unread  = has_unread_replies(_current_user["user_id"])
    _has_done    = has_unseen_done(_current_user["user_id"])

    if _has_unread:
        _my_label = "Мои обращения 🔴 новые ответы"
    elif _has_done:
        _my_label = "Мои обращения 🟢 обращение отработано"
    else:
        _my_label = "Мои обращения"

    if st.button(_my_label, key="sidebar_myhelp_btn", use_container_width=True):
        st.session_state.main_choice   = "Мои обращения"
        st.session_state.show_landing  = False
        st.rerun()

    st.caption(f"{_current_user.get('name', '')} · {_current_user.get('role', '')}")
    if st.button("Выйти", key="sidebar_logout_btn"):
        log_event(
            org_id=_current_user.get("org_id", ""),
            user_id=_current_user["user_id"],
            role=_current_user["role"],
            event="logout",
            module=None,
        )
        logout()
        st.rerun()

main_choice = st.session_state.main_choice

# =============================================================================
# 🏠 Лендинг
# =============================================================================
if st.session_state.show_landing:
    st.markdown("""
    <style>
    [data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none !important; }
    .block-container { padding-top: 2rem !important; max-width: 1100px !important; }
    </style>
    """, unsafe_allow_html=True)

    # Шапка лендинга: имя+сегмент слева, выход справа
    _landing_seg_name = ""
    if _current_user.get("org_id"):
        try:
            import json as _lj
            from pathlib import Path as _lp
            _lsf  = _lp(__file__).parent / "data" / "admin" / "segments.json"
            _lsegs = _lj.loads(_lsf.read_text(encoding="utf-8"))
            _landing_seg_name = _lsegs.get(_current_user["org_id"], {}).get("name", "")
        except Exception:
            pass

    _lhc1, _lhc2 = st.columns([3, 1])
    with _lhc1:
        _user_label = _current_user.get("name", "")
        if _landing_seg_name:
            _user_label += f" · {_landing_seg_name}"
        st.markdown(
            f'<div style="padding:0.3rem 0;font-size:0.85rem;color:#5a6a7a">{_user_label}</div>',
            unsafe_allow_html=True,
        )
    with _lhc2:
        _lhc2a, _lhc2b = st.columns([1, 1])
        with _lhc2a:
            if st.button("Помощь", key="landing_help_btn", use_container_width=True):
                st.session_state._show_help_dialog = True
                st.rerun()
        with _lhc2b:
            if st.button("Выйти", key="landing_logout_btn", use_container_width=True):
                log_event(
                    org_id=_current_user.get("org_id", ""),
                    user_id=_current_user["user_id"],
                    role=_current_user["role"],
                    event="logout",
                    module=None,
                )
                logout()
                st.rerun()

    st.markdown("""
    <div style="text-align:center; padding: 2rem 0 1rem;">
        <div style="font-size:2.6rem; font-weight:900; letter-spacing:0.02em;
                    color:#1B5C74; margin-bottom:0.5rem;">РЕГУЛА.AI</div>
        <div style="font-size:0.9rem; color:#1B5C74; font-weight:500; letter-spacing:0.08em;
                    text-transform:uppercase; margin-bottom:1rem;">
            ИИ-система в сфере тарифного регулирования РФ
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Поле поиска — заглушка, LLM будет подключён позднее
    _sc1, _sc2, _sc3 = st.columns([1, 4, 1])
    with _sc2:
        _search_val = st.text_input(
            "Поиск",
            key="landing_search",
            placeholder="Задайте вопрос и я найду как помочь...",
            label_visibility="collapsed",
        )
        if _search_val.strip():
            st.caption("Умный поиск по модулям — в разработке. Воспользуйтесь Советчиком.")

    st.markdown("<div style='height:1rem'></div>", unsafe_allow_html=True)
    st.markdown("<hr style='border:none;border-top:1px solid #dce3ec;margin:1.5rem 0 1rem;'>", unsafe_allow_html=True)
    st.markdown("#### Функции")
    st.markdown("<div style='height:0.3rem'></div>", unsafe_allow_html=True)

    # На лендинге показываем продуктовые модули (без Админки), включая «Задачи» —
    # теперь это обычный гейтируемый модуль, как и остальные.
    _landing_products = [p for p in _ACTIVE_PRODUCTS if p != "Админка"]

    _cols_per_row = 3
    for _row_start in range(0, len(_landing_products), _cols_per_row):
        _row_items = _landing_products[_row_start:_row_start + _cols_per_row]
        _cols = st.columns(_cols_per_row, gap="medium")
        for _ci, _product in enumerate(_row_items):
            with _cols[_ci]:
                _desc = _PRODUCT_DESCRIPTIONS.get(_product, "")
                _available = _product in _visible_products

                if _available:
                    st.markdown('<div class="landing-tile">', unsafe_allow_html=True)
                    if st.button(_product, key=f"landing_tile_{_product}", use_container_width=True):
                        st.session_state.main_choice = _product
                        st.session_state.show_landing = False
                        st.rerun()
                    st.markdown(f'<div class="landing-tile-desc">{_desc}</div>', unsafe_allow_html=True)
                    st.markdown('</div>', unsafe_allow_html=True)
                else:
                    # Недоступный модуль — серая плитка с замочком и tooltip
                    st.markdown(
                        f'<div class="landing-tile-locked" '
                        f'title="Недоступно в вашем тарифном плане">'
                        f'<div class="landing-tile-locked-inner">'
                        f'<span class="lock-icon">🔒</span>'
                        f'<span>{_product}</span></div></div>'
                        f'<div class="landing-tile-desc" style="opacity:0.5">{_desc}</div>',
                        unsafe_allow_html=True,
                    )
    st.stop()

# =============================================================================
# 🚧 Диалог "В разработке"
# =============================================================================
@st.dialog("Продукт в разработке")
def show_dev_dialog(product_name: str):
    st.markdown(f"### {product_name}")
    st.markdown("""
Этот продукт **находится в активной разработке** и пока не готов к полноценному использованию.
В интерфейсе представлен **прототип решения** — демонстрация концепции и будущего функционала.
> Если у вас есть пожелания — свяжитесь с командой разработки.
    """)
    st.divider()
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("Понятно, продолжить", type="primary", use_container_width=True):
            st.session_state._dev_dialog_confirmed = product_name
            st.rerun()
    with col2:
        if st.button("Вернуться", use_container_width=True):
            st.session_state.main_choice = _ACTIVE_PRODUCTS[0]
            st.session_state._dev_dialog_confirmed = None
            st.rerun()

if main_choice in _DEV_PRODUCTS:
    if st.session_state.get("_dev_dialog_confirmed") != main_choice:
        show_dev_dialog(main_choice)
else:
    st.session_state._dev_dialog_confirmed = None

# =============================================================================
# 🏷️ Бренд-бар
# =============================================================================
st.markdown("""
<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;
            margin-bottom:1.2rem;margin-top:-1.5rem;line-height:1;width:100%;gap:0.55rem;">
    <span style="font-size:2.6rem;font-weight:900;color:#1B5C74;
                 letter-spacing:0.02em;line-height:1;">РЕГУЛА.AI</span>
    <span style="font-size:0.9rem;font-weight:500;color:#1B5C74;
                 letter-spacing:0.08em;text-transform:uppercase;opacity:0.7;">
        ИИ-система в сфере тарифного регулирования РФ
    </span>
</div>
""", unsafe_allow_html=True)

# =============================================================================
# 🛡️ Защита модулей от прямого доступа
# =============================================================================
def _check_module(choice: str) -> bool:
    """
    Финальная проверка доступа к модулю.
    Защищает от обхода сайдбара через прямую навигацию.
    Суперадмин проходит всегда.
    """
    if _is_superadmin:
        return True
    mid = _CHOICE_TO_MODULE.get(choice)
    if not mid:
        return True  # не управляемый модуль — пропускаем
    status = get_module_status(_current_user, mid)
    if status == "disabled":
        st.warning("Этот модуль недоступен для вашей организации.")
        return False
    if status == "maintenance":
        st.markdown("""
        <div style="text-align:center;padding:3rem 1rem;color:#5a6a7a">
            <div style="font-size:2rem;margin-bottom:1rem">🔧</div>
            <div style="font-size:1.1rem;font-weight:600;margin-bottom:0.5rem">
                Модуль на техническом обслуживании
            </div>
            <div style="font-size:0.9rem">
                Временно недоступен. Попробуйте позже.
            </div>
        </div>
        """, unsafe_allow_html=True)
        return False
    return True

# =============================================================================
# 🗂️ Роутинг
# =============================================================================

if main_choice == "Управление":
    from streamlit_pages.superadmin import show_superadmin
    show_superadmin()

elif main_choice == "Локальная база знаний":
    from streamlit_pages.local_kb_admin import show_local_kb_admin
    show_local_kb_admin()

elif main_choice == "Анализатор заявок":
    if _check_module("Анализатор заявок"):
        try:
            from streamlit_pages.claim_analyzer import show_claim_analyzer
            show_claim_analyzer()
        except ImportError as e:
            st.error(f"Ошибка загрузки анализатора: {e}")

elif main_choice == "Советчик":
    if _check_module("Советчик"):
        show_advisor()

elif main_choice == "Задачи":
    if _check_module("Задачи"):
        show_tasks()

elif main_choice == "Сканер документов":
    if _check_module("Сканер документов"):
        try:
            from streamlit_pages.doc_scanner import show_doc_scanner
            show_doc_scanner()
        except ImportError as e:
            st.error(f"Ошибка: {e}")

elif main_choice == "Прогноз решения регулятора":
    if _check_module("Прогноз решения регулятора"):
        try:
            from streamlit_pages.predictor import show_predictor
            show_predictor()
        except ImportError as e:
            st.error(f"Ошибка: {e}")

elif main_choice == "Протокольщик":
    if _check_module("Протокольщик"):
        try:
            from streamlit_pages.protocol_bot import show_protocol_bot
            show_protocol_bot()
        except ImportError as e:
            st.error(f"Ошибка: {e}")

elif main_choice == "Позиция ФАС":
    try:
        from streamlit_pages.fas_position import show_fas_position
        show_fas_position()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Поиск прецедентов":
    try:
        from streamlit_pages.court_precedents import show_court_precedents
        show_court_precedents()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Сверка численности":
    try:
        from streamlit_pages.numeracy_check import show_numeracy_check
        show_numeracy_check()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Проверка амортизации":
    try:
        from streamlit_pages.amortization_check import show_amortization_check
        show_amortization_check()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Экспорт ФГИС":
    try:
        from streamlit_pages.fgis_export import show_fgis_export
        show_fgis_export()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Пояснительная записка":
    try:
        from streamlit_pages.explanatory_note import show_explanatory_note
        show_explanatory_note()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Калькулятор рисков":
    try:
        from streamlit_pages.risk_calculator import show_risk_calculator
        show_risk_calculator()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Жалобщик":
    try:
        from streamlit_pages.complaint_bot import show_complaint_bot
        show_complaint_bot()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Трекер изменений законов":
    try:
        from streamlit_pages.law_tracker import show_law_tracker
        show_law_tracker()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Расчетный лист":
    try:
        from streamlit_pages.calc_sheet import show_calc_sheet
        show_calc_sheet()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Прогнозист тарифов":
    try:
        from streamlit_pages.tariff_forecaster import show_tariff_forecaster
        show_tariff_forecaster()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Сравнение с аналогами":
    try:
        from streamlit_pages.peer_comparison import show_peer_comparison
        show_peer_comparison()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Режим обучения":
    try:
        from streamlit_pages.training_mode import show_training_mode
        show_training_mode()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Наведение порядка в документах":
    try:
        from streamlit_pages.document_organizer import show_document_organizer
        show_document_organizer()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Планировщик кампании":
    try:
        from streamlit_pages.tariff_planner import show_tariff_planner
        show_tariff_planner()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Прогноз потребления":
    try:
        from streamlit_pages.consumption_forecast import show_consumption_forecast
        show_consumption_forecast()
    except ImportError as e:
        st.error(f"❌ {e}")

elif main_choice == "Мои обращения":
    # При открытии помечаем все ответы как прочитанные — кнопка в сайдбаре гаснет.
    # Это намеренное решение: факт открытия раздела = пользователь увидел ответы.
    mark_read_by_user(_current_user["user_id"])

    st.markdown("### Мои обращения")
    st.caption("История ваших запросов в поддержку и ответы на них.")

    try:
        from core.help_requests import get_requests as _get_help
        _my_requests = _get_help(user_id=_current_user["user_id"], limit=100)

        if not _my_requests:
            st.info("У вас пока нет обращений. Используйте кнопку «Помощь» чтобы задать вопрос.")
        else:
            for _req in _my_requests:
                _ts     = _req.get("ts", "")[:16].replace("T", " ")
                _text   = _req.get("text", "")
                _reply  = _req.get("reply")
                _status = _req.get("status", "new")
                _rat    = (_req.get("replied_at") or "")[:10]

                with st.container(border=True):
                    _rc1, _rc2 = st.columns([4, 1])
                    with _rc1:
                        st.caption(_ts)
                    with _rc2:
                        if _status == "done":
                            st.markdown(
                                "<small style='color:#27AE60'>✓ Отработано</small>",
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(
                                "<small style='color:#E24B4A'>● Открыто</small>",
                                unsafe_allow_html=True,
                            )

                    st.markdown(
                        f"<div style='color:#1a2a3a;padding:0.3rem 0'>{_text}</div>",
                        unsafe_allow_html=True,
                    )

                    if _reply:
                        st.markdown(
                            f"<div style='background:#e8f4f8;border-left:3px solid #1B5C74;"
                            f"padding:0.5rem 0.75rem;border-radius:0 6px 6px 0;"
                            f"margin-top:0.5rem;font-size:0.9rem'>"
                            f"<span style='color:#5a6a7a;font-size:0.78rem'>"
                            f"Ответ поддержки · {_rat}</span><br>{_reply}</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.caption("Ответ ожидается...")
    except Exception as _e:
        st.error(f"Ошибка загрузки обращений: {_e}")

elif main_choice == "Админка":
    show_admin_panel()