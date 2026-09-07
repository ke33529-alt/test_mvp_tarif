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
    is_module_accessible, is_admin_panel_enabled, ALL_MODULES, MODULE_STATUS_LABELS,
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
.st-key-landing_search_btn_wrap button {
    background-color: #1B5E20 !important;
    border: 1px solid #1B5E20 !important;
    color: #ffffff !important;
}
.st-key-landing_search_btn_wrap button:hover {
    background-color: #123D16 !important;
    border-color: #123D16 !important;
}
.st-key-landing_search_btn_wrap button:focus,
.st-key-landing_search_btn_wrap button:focus-visible {
    box-shadow: 0 0 0 2px #a7d6ab !important;
    outline: none !important;
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
# 🖼️ Фоновая картинка лендинга
# =============================================================================
# SVG зашит прямо здесь base64-строкой (а не читается из assets/), потому что
# volume-маунты Docker Compose покрывают только ./core, ./streamlit_pages и
# ./app.py — отдельная папка assets/ внутрь контейнера не попадает.
# Так фон гарантированно подхватывается через `docker compose restart regula`.
_LANDING_BG_SVG_B64 = (
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxOTIwIiBoZWlnaHQ9IjEwODAiIHZpZXdCb3g9IjAgMCAxOTIwIDEwODAiPgo8cmVjdCB3aWR0aD0iMTkyMCIgaGVpZ2h0PSIxMDgwIiBmaWxsPSIjRjRGNkY5Ii8+CjxnIHN0cm9rZT0iIzFCNUM3NCIgc3Ryb2tlLXdpZHRoPSIxLjMiIG9wYWNpdHk9IjAuMTEyIj4KPGxpbmUgeDE9IjEzLjQiIHkxPSItNDQuOSIgeDI9IjE1Ni4yIiB5Mj0iLTE1LjEiLz4KPGxpbmUgeDE9IjEzLjQiIHkxPSItNDQuOSIgeDI9Ii0yMS42IiB5Mj0iMTA4LjgiLz4KPGxpbmUgeDE9IjEzLjQiIHkxPSItNDQuOSIgeDI9IjI0OC4yIiB5Mj0iLTE5LjkiLz4KPGxpbmUgeDE9Ii0yMS42IiB5MT0iMTA4LjgiIHgyPSIxMDQuMSIgeTI9IjE3OC4yIi8+CjxsaW5lIHgxPSItMjEuNiIgeTE9IjEwOC44IiB4Mj0iMjIuNyIgeTI9IjI4Ni43Ii8+CjxsaW5lIHgxPSItMjEuNiIgeTE9IjEwOC44IiB4Mj0iMTIxLjUiIHkyPSIyMzEuNSIvPgo8bGluZSB4MT0iMjIuNyIgeTE9IjI4Ni43IiB4Mj0iMTIxLjUiIHkyPSIyMzEuNSIvPgo8bGluZSB4MT0iMjIuNyIgeTE9IjI4Ni43IiB4Mj0iMTA0LjEiIHkyPSIxNzguMiIvPgo8bGluZSB4MT0iMjIuNyIgeTE9IjI4Ni43IiB4Mj0iOTguNCIgeTI9IjQzNy44Ii8+CjxsaW5lIHgxPSItNDUuNSIgeTE9IjY0Ni41IiB4Mj0iNC4zIiB5Mj0iNzgzLjYiLz4KPGxpbmUgeDE9Ii00NS41IiB5MT0iNjQ2LjUiIHgyPSIxODIuNiIgeTI9IjY2My41Ii8+CjxsaW5lIHgxPSItNDUuNSIgeTE9IjY0Ni41IiB4Mj0iOTguNCIgeTI9IjQzNy44Ii8+CjxsaW5lIHgxPSI0LjMiIHkxPSI3ODMuNiIgeDI9IjE2OC44IiB5Mj0iODIxLjIiLz4KPGxpbmUgeDE9IjQuMyIgeTE9Ijc4My42IiB4Mj0iOC42IiB5Mj0iOTc0LjIiLz4KPGxpbmUgeDE9IjQuMyIgeTE9Ijc4My42IiB4Mj0iMTgyLjYiIHkyPSI2NjMuNSIvPgo8bGluZSB4MT0iOC42IiB5MT0iOTc0LjIiIHgyPSItNDcuNCIgeTI9IjExMDguOSIvPgo8bGluZSB4MT0iOC42IiB5MT0iOTc0LjIiIHgyPSIxNTYuOCIgeTI9IjEwMzcuMSIvPgo8bGluZSB4MT0iOC42IiB5MT0iOTc0LjIiIHgyPSIxNzEuOSIgeTI9Ijk1Mi4zIi8+CjxsaW5lIHgxPSItNDcuNCIgeTE9IjExMDguOSIgeDI9IjE1Ni44IiB5Mj0iMTAzNy4xIi8+CjxsaW5lIHgxPSIxNTYuMiIgeTE9Ii0xNS4xIiB4Mj0iMjQ4LjIiIHkyPSItMTkuOSIvPgo8bGluZSB4MT0iMTU2LjIiIHkxPSItMTUuMSIgeDI9IjIzMy45IiB5Mj0iMTA5LjciLz4KPGxpbmUgeDE9IjE1Ni4yIiB5MT0iLTE1LjEiIHgyPSIxMDQuMSIgeTI9IjE3OC4yIi8+CjxsaW5lIHgxPSIxMDQuMSIgeTE9IjE3OC4yIiB4Mj0iMTIxLjUiIHkyPSIyMzEuNSIvPgo8bGluZSB4MT0iMTA0LjEiIHkxPSIxNzguMiIgeDI9IjIzMy45IiB5Mj0iMTA5LjciLz4KPGxpbmUgeDE9IjEwNC4xIiB5MT0iMTc4LjIiIHgyPSIyMzYuMCIgeTI9IjI0OS4wIi8+CjxsaW5lIHgxPSIxMjEuNSIgeTE9IjIzMS41IiB4Mj0iMjM2LjAiIHkyPSIyNDkuMCIvPgo8bGluZSB4MT0iMTIxLjUiIHkxPSIyMzEuNSIgeDI9IjIzMy45IiB5Mj0iMTA5LjciLz4KPGxpbmUgeDE9IjEyMS41IiB5MT0iMjMxLjUiIHgyPSI5OC40IiB5Mj0iNDM3LjgiLz4KPGxpbmUgeDE9Ijk4LjQiIHkxPSI0MzcuOCIgeDI9IjI0Ni40IiB5Mj0iNTE4LjAiLz4KPGxpbmUgeDE9Ijk4LjQiIHkxPSI0MzcuOCIgeDI9IjIzNi4wIiB5Mj0iMjQ5LjAiLz4KPGxpbmUgeDE9Ijk4LjQiIHkxPSI0MzcuOCIgeDI9IjE4Mi42IiB5Mj0iNjYzLjUiLz4KPGxpbmUgeDE9IjE4Mi42IiB5MT0iNjYzLjUiIHgyPSIxNjguOCIgeTI9IjgyMS4yIi8+CjxsaW5lIHgxPSIxODIuNiIgeTE9IjY2My41IiB4Mj0iMjQ2LjQiIHkyPSI1MTguMCIvPgo8bGluZSB4MT0iMTgyLjYiIHkxPSI2NjMuNSIgeDI9IjI5Ni4zIiB5Mj0iNzc4LjIiLz4KPGxpbmUgeDE9IjE2OC44IiB5MT0iODIxLjIiIHgyPSIxNzEuOSIgeTI9Ijk1Mi4zIi8+CjxsaW5lIHgxPSIxNjguOCIgeTE9IjgyMS4yIiB4Mj0iMjk2LjMiIHkyPSI3NzguMiIvPgo8bGluZSB4MT0iMTY4LjgiIHkxPSI4MjEuMiIgeDI9IjI2Mi43IiB5Mj0iOTkxLjMiLz4KPGxpbmUgeDE9IjE3MS45IiB5MT0iOTUyLjMiIHgyPSIxNTYuOCIgeTI9IjEwMzcuMSIvPgo8bGluZSB4MT0iMTcxLjkiIHkxPSI5NTIuMyIgeDI9IjI2Mi43IiB5Mj0iOTkxLjMiLz4KPGxpbmUgeDE9IjE3MS45IiB5MT0iOTUyLjMiIHgyPSIyODcuNyIgeTI9IjEwODUuNCIvPgo8bGluZSB4MT0iMTU2LjgiIHkxPSIxMDM3LjEiIHgyPSIyNjIuNyIgeTI9Ijk5MS4zIi8+CjxsaW5lIHgxPSIxNTYuOCIgeTE9IjEwMzcuMSIgeDI9IjI4Ny43IiB5Mj0iMTA4NS40Ii8+CjxsaW5lIHgxPSIxNjguOCIgeTE9IjgyMS4yIiB4Mj0iMTU2LjgiIHkyPSIxMDM3LjEiLz4KPGxpbmUgeDE9IjI0OC4yIiB5MT0iLTE5LjkiIHgyPSIyMzMuOSIgeTI9IjEwOS43Ii8+CjxsaW5lIHgxPSIyNDguMiIgeTE9Ii0xOS45IiB4Mj0iNDI5LjIiIHkyPSIzMi40Ii8+CjxsaW5lIHgxPSIyNDguMiIgeTE9Ii0xOS45IiB4Mj0iNDM3LjkiIHkyPSIxMDkuNCIvPgo8bGluZSB4MT0iMjMzLjkiIHkxPSIxMDkuNyIgeDI9IjIzNi4wIiB5Mj0iMjQ5LjAiLz4KPGxpbmUgeDE9IjIzMy45IiB5MT0iMTA5LjciIHgyPSIzNjYuNSIgeTI9IjI1Mi42Ii8+CjxsaW5lIHgxPSIyMzMuOSIgeTE9IjEwOS43IiB4Mj0iNDM3LjkiIHkyPSIxMDkuNCIvPgo8bGluZSB4MT0iMjM2LjAiIHkxPSIyNDkuMCIgeDI9IjM2Ni41IiB5Mj0iMjUyLjYiLz4KPGxpbmUgeDE9IjIzNi4wIiB5MT0iMjQ5LjAiIHgyPSIzODkuMSIgeTI9IjM3Ny43Ii8+CjxsaW5lIHgxPSIyMi43IiB5MT0iMjg2LjciIHgyPSIyMzYuMCIgeTI9IjI0OS4wIi8+CjxsaW5lIHgxPSIyNDYuNCIgeTE9IjUxOC4wIiB4Mj0iMzg5LjEiIHkyPSIzNzcuNyIvPgo8bGluZSB4MT0iMjQ2LjQiIHkxPSI1MTguMCIgeDI9IjQ0Ny42IiB5Mj0iNTIyLjUiLz4KPGxpbmUgeDE9IjI5Ni4zIiB5MT0iNzc4LjIiIHgyPSIzODguOSIgeTI9Ijc4Ni4xIi8+CjxsaW5lIHgxPSIyOTYuMyIgeTE9Ijc3OC4yIiB4Mj0iNDE3LjMiIHkyPSI5MjIuNiIvPgo8bGluZSB4MT0iMTcxLjkiIHkxPSI5NTIuMyIgeDI9IjI5Ni4zIiB5Mj0iNzc4LjIiLz4KPGxpbmUgeDE9IjI2Mi43IiB5MT0iOTkxLjMiIHgyPSIyODcuNyIgeTI9IjEwODUuNCIvPgo8bGluZSB4MT0iMjYyLjciIHkxPSI5OTEuMyIgeDI9IjQxNy4zIiB5Mj0iOTIyLjYiLz4KPGxpbmUgeDE9IjI2Mi43IiB5MT0iOTkxLjMiIHgyPSI0MTkuNSIgeTI9IjExMTcuNiIvPgo8bGluZSB4MT0iMjg3LjciIHkxPSIxMDg1LjQiIHgyPSI0MTkuNSIgeTI9IjExMTcuNiIvPgo8bGluZSB4MT0iMjg3LjciIHkxPSIxMDg1LjQiIHgyPSI0MTcuMyIgeTI9IjkyMi42Ii8+CjxsaW5lIHgxPSI0MjkuMiIgeTE9IjMyLjQiIHgyPSI0MzcuOSIgeTI9IjEwOS40Ii8+CjxsaW5lIHgxPSI0MjkuMiIgeTE9IjMyLjQiIHgyPSI1MzguOSIgeTI9Ii0yNi41Ii8+CjxsaW5lIHgxPSI0MjkuMiIgeTE9IjMyLjQiIHgyPSI1OTYuMyIgeTI9IjEzNS45Ii8+CjxsaW5lIHgxPSI0MzcuOSIgeTE9IjEwOS40IiB4Mj0iNTA5LjMiIHkyPSIyMjcuMiIvPgo8bGluZSB4MT0iNDM3LjkiIHkxPSIxMDkuNCIgeDI9IjM2Ni41IiB5Mj0iMjUyLjYiLz4KPGxpbmUgeDE9IjQzNy45IiB5MT0iMTA5LjQiIHgyPSI1OTYuMyIgeTI9IjEzNS45Ii8+CjxsaW5lIHgxPSIzNjYuNSIgeTE9IjI1Mi42IiB4Mj0iMzg5LjEiIHkyPSIzNzcuNyIvPgo8bGluZSB4MT0iMzY2LjUiIHkxPSIyNTIuNiIgeDI9IjUwOS4zIiB5Mj0iMjI3LjIiLz4KPGxpbmUgeDE9IjM2Ni41IiB5MT0iMjUyLjYiIHgyPSI1MTEuMSIgeTI9IjQxNy4wIi8+CjxsaW5lIHgxPSIzODkuMSIgeTE9IjM3Ny43IiB4Mj0iNTExLjEiIHkyPSI0MTcuMCIvPgo8bGluZSB4MT0iMzg5LjEiIHkxPSIzNzcuNyIgeDI9IjQ0Ny42IiB5Mj0iNTIyLjUiLz4KPGxpbmUgeDE9IjM4OS4xIiB5MT0iMzc3LjciIHgyPSI1MDkuMyIgeTI9IjIyNy4yIi8+CjxsaW5lIHgxPSI0NDcuNiIgeTE9IjUyMi41IiB4Mj0iNTExLjEiIHkyPSI0MTcuMCIvPgo8bGluZSB4MT0iNDQ3LjYiIHkxPSI1MjIuNSIgeDI9IjU5Ni4yIiB5Mj0iNjc3LjgiLz4KPGxpbmUgeDE9IjM4OC45IiB5MT0iNzg2LjEiIHgyPSI0MTcuMyIgeTI9IjkyMi42Ii8+CjxsaW5lIHgxPSIzODguOSIgeTE9Ijc4Ni4xIiB4Mj0iNTgzLjIiIHkyPSI3NjMuOCIvPgo8bGluZSB4MT0iMTY4LjgiIHkxPSI4MjEuMiIgeDI9IjM4OC45IiB5Mj0iNzg2LjEiLz4KPGxpbmUgeDE9IjQxNy4zIiB5MT0iOTIyLjYiIHgyPSI1NjkuOCIgeTI9Ijk2Mi4yIi8+CjxsaW5lIHgxPSI0MTcuMyIgeTE9IjkyMi42IiB4Mj0iNTUyLjEiIHkyPSIxMDU4LjAiLz4KPGxpbmUgeDE9IjQxNy4zIiB5MT0iOTIyLjYiIHgyPSI0MTkuNSIgeTI9IjExMTcuNiIvPgo8bGluZSB4MT0iNDE5LjUiIHkxPSIxMTE3LjYiIHgyPSI1NTIuMSIgeTI9IjEwNTguMCIvPgo8bGluZSB4MT0iNDE5LjUiIHkxPSIxMTE3LjYiIHgyPSI1NjkuOCIgeTI9Ijk2Mi4yIi8+CjxsaW5lIHgxPSI0MTkuNSIgeTE9IjExMTcuNiIgeDI9IjYzNy44IiB5Mj0iMTA2My40Ii8+CjxsaW5lIHgxPSI1MzguOSIgeTE9Ii0yNi41IiB4Mj0iNjk5LjIiIHkyPSItMzYuNyIvPgo8bGluZSB4MT0iNDM3LjkiIHkxPSIxMDkuNCIgeDI9IjUzOC45IiB5Mj0iLTI2LjUiLz4KPGxpbmUgeDE9IjUzOC45IiB5MT0iLTI2LjUiIHgyPSI1OTYuMyIgeTI9IjEzNS45Ii8+CjxsaW5lIHgxPSI1OTYuMyIgeTE9IjEzNS45IiB4Mj0iNjc5LjUiIHkyPSIxMzAuNiIvPgo8bGluZSB4MT0iNTk2LjMiIHkxPSIxMzUuOSIgeDI9IjUwOS4zIiB5Mj0iMjI3LjIiLz4KPGxpbmUgeDE9IjU5Ni4zIiB5MT0iMTM1LjkiIHgyPSI2OTkuMiIgeTI9Ii0zNi43Ii8+CjxsaW5lIHgxPSI1MDkuMyIgeTE9IjIyNy4yIiB4Mj0iNTExLjEiIHkyPSI0MTcuMCIvPgo8bGluZSB4MT0iNTA5LjMiIHkxPSIyMjcuMiIgeDI9IjY3OS41IiB5Mj0iMTMwLjYiLz4KPGxpbmUgeDE9IjQyOS4yIiB5MT0iMzIuNCIgeDI9IjUwOS4zIiB5Mj0iMjI3LjIiLz4KPGxpbmUgeDE9IjUxMS4xIiB5MT0iNDE3LjAiIHgyPSI3MjkuMyIgeTI9IjMwNS41Ii8+CjxsaW5lIHgxPSI1OTYuMiIgeTE9IjY3Ny44IiB4Mj0iNTgzLjIiIHkyPSI3NjMuOCIvPgo8bGluZSB4MT0iNTk2LjIiIHkxPSI2NzcuOCIgeDI9IjcxMC45IiB5Mj0iODEzLjciLz4KPGxpbmUgeDE9IjM4OC45IiB5MT0iNzg2LjEiIHgyPSI1OTYuMiIgeTI9IjY3Ny44Ii8+CjxsaW5lIHgxPSI1ODMuMiIgeTE9Ijc2My44IiB4Mj0iNzEwLjkiIHkyPSI4MTMuNyIvPgo8bGluZSB4MT0iNTgzLjIiIHkxPSI3NjMuOCIgeDI9IjU2OS44IiB5Mj0iOTYyLjIiLz4KPGxpbmUgeDE9IjU4My4yIiB5MT0iNzYzLjgiIHgyPSI3MTIuNSIgeTI9Ijk0Ny45Ii8+CjxsaW5lIHgxPSI1NjkuOCIgeTE9Ijk2Mi4yIiB4Mj0iNTUyLjEiIHkyPSIxMDU4LjAiLz4KPGxpbmUgeDE9IjU2OS44IiB5MT0iOTYyLjIiIHgyPSI2MzcuOCIgeTI9IjEwNjMuNCIvPgo8bGluZSB4MT0iNTY5LjgiIHkxPSI5NjIuMiIgeDI9IjcxMi41IiB5Mj0iOTQ3LjkiLz4KPGxpbmUgeDE9IjU1Mi4xIiB5MT0iMTA1OC4wIiB4Mj0iNjM3LjgiIHkyPSIxMDYzLjQiLz4KPGxpbmUgeDE9IjU1Mi4xIiB5MT0iMTA1OC4wIiB4Mj0iNzEyLjUiIHkyPSI5NDcuOSIvPgo8bGluZSB4MT0iNTUyLjEiIHkxPSIxMDU4LjAiIHgyPSI3OTUuMiIgeTI9IjEwODMuNyIvPgo8bGluZSB4MT0iNjk5LjIiIHkxPSItMzYuNyIgeDI9Ijc3Ni43IiB5Mj0iNDAuNSIvPgo8bGluZSB4MT0iNjk5LjIiIHkxPSItMzYuNyIgeDI9IjY3OS41IiB5Mj0iMTMwLjYiLz4KPGxpbmUgeDE9IjY5OS4yIiB5MT0iLTM2LjciIHgyPSI4NTkuMiIgeTI9IjE2Ni4zIi8+CjxsaW5lIHgxPSI2NzkuNSIgeTE9IjEzMC42IiB4Mj0iNzc2LjciIHkyPSI0MC41Ii8+CjxsaW5lIHgxPSI2NzkuNSIgeTE9IjEzMC42IiB4Mj0iODA0LjQiIHkyPSIyMjguMiIvPgo8bGluZSB4MT0iNjc5LjUiIHkxPSIxMzAuNiIgeDI9IjcyOS4zIiB5Mj0iMzA1LjUiLz4KPGxpbmUgeDE9IjcyOS4zIiB5MT0iMzA1LjUiIHgyPSI4MDQuNCIgeTI9IjIyOC4yIi8+CjxsaW5lIHgxPSI3MjkuMyIgeTE9IjMwNS41IiB4Mj0iODU5LjIiIHkyPSIxNjYuMyIvPgo8bGluZSB4MT0iNzI5LjMiIHkxPSIzMDUuNSIgeDI9IjgyMS41IiB5Mj0iNDk5LjMiLz4KPGxpbmUgeDE9IjcxMC45IiB5MT0iODEzLjciIHgyPSI4MjcuNiIgeTI9Ijc4Ny44Ii8+CjxsaW5lIHgxPSI3MTAuOSIgeTE9IjgxMy43IiB4Mj0iNzEyLjUiIHkyPSI5NDcuOSIvPgo8bGluZSB4MT0iNzEwLjkiIHkxPSI4MTMuNyIgeDI9Ijg1OC42IiB5Mj0iOTM3LjciLz4KPGxpbmUgeDE9IjcxMi41IiB5MT0iOTQ3LjkiIHgyPSI2MzcuOCIgeTI9IjEwNjMuNCIvPgo8bGluZSB4MT0iNzEyLjUiIHkxPSI5NDcuOSIgeDI9Ijg1OC42IiB5Mj0iOTM3LjciLz4KPGxpbmUgeDE9IjcxMi41IiB5MT0iOTQ3LjkiIHgyPSI3OTUuMiIgeTI9IjEwODMuNyIvPgo8bGluZSB4MT0iNjM3LjgiIHkxPSIxMDYzLjQiIHgyPSI3OTUuMiIgeTI9IjEwODMuNyIvPgo8bGluZSB4MT0iNjM3LjgiIHkxPSIxMDYzLjQiIHgyPSI4NTguNiIgeTI9IjkzNy43Ii8+CjxsaW5lIHgxPSI3NzYuNyIgeTE9IjQwLjUiIHgyPSI4NTkuMiIgeTI9IjE2Ni4zIi8+CjxsaW5lIHgxPSI3NzYuNyIgeTE9IjQwLjUiIHgyPSI4MDQuNCIgeTI9IjIyOC4yIi8+CjxsaW5lIHgxPSI1OTYuMyIgeTE9IjEzNS45IiB4Mj0iNzc2LjciIHkyPSI0MC41Ii8+CjxsaW5lIHgxPSI4NTkuMiIgeTE9IjE2Ni4zIiB4Mj0iODA0LjQiIHkyPSIyMjguMiIvPgo8bGluZSB4MT0iODU5LjIiIHkxPSIxNjYuMyIgeDI9Ijk0MS45IiB5Mj0iMTgxLjgiLz4KPGxpbmUgeDE9Ijg1OS4yIiB5MT0iMTY2LjMiIHgyPSI5NzQuNCIgeTI9IjI2NC4yIi8+CjxsaW5lIHgxPSI4MDQuNCIgeTE9IjIyOC4yIiB4Mj0iOTQxLjkiIHkyPSIxODEuOCIvPgo8bGluZSB4MT0iODA0LjQiIHkxPSIyMjguMiIgeDI9Ijk3NC40IiB5Mj0iMjY0LjIiLz4KPGxpbmUgeDE9IjU5Ni4zIiB5MT0iMTM1LjkiIHgyPSI4MDQuNCIgeTI9IjIyOC4yIi8+CjxsaW5lIHgxPSI4MjEuNSIgeTE9IjQ5OS4zIiB4Mj0iOTMzLjEiIHkyPSI2MzQuNSIvPgo8bGluZSB4MT0iODI3LjYiIHkxPSI3ODcuOCIgeDI9IjkzNC4wIiB5Mj0iODQ4LjMiLz4KPGxpbmUgeDE9IjgyNy42IiB5MT0iNzg3LjgiIHgyPSI4NTguNiIgeTI9IjkzNy43Ii8+CjxsaW5lIHgxPSI4MjcuNiIgeTE9Ijc4Ny44IiB4Mj0iOTMzLjEiIHkyPSI2MzQuNSIvPgo8bGluZSB4MT0iODU4LjYiIHkxPSI5MzcuNyIgeDI9IjkzNC4wIiB5Mj0iODQ4LjMiLz4KPGxpbmUgeDE9Ijg1OC42IiB5MT0iOTM3LjciIHgyPSI5OTQuNSIgeTI9IjkwNC40Ii8+CjxsaW5lIHgxPSI4NTguNiIgeTE9IjkzNy43IiB4Mj0iNzk1LjIiIHkyPSIxMDgzLjciLz4KPGxpbmUgeDE9Ijc5NS4yIiB5MT0iMTA4My43IiB4Mj0iOTM0LjgiIHkyPSIxMDk2LjAiLz4KPGxpbmUgeDE9IjU2OS44IiB5MT0iOTYyLjIiIHgyPSI3OTUuMiIgeTI9IjEwODMuNyIvPgo8bGluZSB4MT0iOTgyLjEiIHkxPSItMjguMiIgeDI9IjEwNjkuNyIgeTI9Ii0zNC43Ii8+CjxsaW5lIHgxPSI5ODIuMSIgeTE9Ii0yOC4yIiB4Mj0iOTQxLjkiIHkyPSIxODEuOCIvPgo8bGluZSB4MT0iNzc2LjciIHkxPSI0MC41IiB4Mj0iOTgyLjEiIHkyPSItMjguMiIvPgo8bGluZSB4MT0iOTQxLjkiIHkxPSIxODEuOCIgeDI9Ijk3NC40IiB5Mj0iMjY0LjIiLz4KPGxpbmUgeDE9Ijk0MS45IiB5MT0iMTgxLjgiIHgyPSIxMDk0LjUiIHkyPSIyOTYuOSIvPgo8bGluZSB4MT0iOTQxLjkiIHkxPSIxODEuOCIgeDI9IjExMzkuMCIgeTI9IjE0MS43Ii8+CjxsaW5lIHgxPSI5NzQuNCIgeTE9IjI2NC4yIiB4Mj0iMTA5NC41IiB5Mj0iMjk2LjkiLz4KPGxpbmUgeDE9Ijk3NC40IiB5MT0iMjY0LjIiIHgyPSIxMTM5LjAiIHkyPSIxNDEuNyIvPgo8bGluZSB4MT0iNzI5LjMiIHkxPSIzMDUuNSIgeDI9Ijk3NC40IiB5Mj0iMjY0LjIiLz4KPGxpbmUgeDE9IjkzMy4xIiB5MT0iNjM0LjUiIHgyPSIxMTE5LjEiIHkyPSI2OTEuNCIvPgo8bGluZSB4MT0iOTMzLjEiIHkxPSI2MzQuNSIgeDI9IjEwNTguNiIgeTI9IjgwMC44Ii8+CjxsaW5lIHgxPSI5MzMuMSIgeTE9IjYzNC41IiB4Mj0iOTM0LjAiIHkyPSI4NDguMyIvPgo8bGluZSB4MT0iOTM0LjAiIHkxPSI4NDguMyIgeDI9Ijk5NC41IiB5Mj0iOTA0LjQiLz4KPGxpbmUgeDE9IjkzNC4wIiB5MT0iODQ4LjMiIHgyPSIxMDU4LjYiIHkyPSI4MDAuOCIvPgo8bGluZSB4MT0iOTM0LjAiIHkxPSI4NDguMyIgeDI9IjEwODEuNyIgeTI9Ijk3OS4yIi8+CjxsaW5lIHgxPSI5OTQuNSIgeTE9IjkwNC40IiB4Mj0iMTA4MS43IiB5Mj0iOTc5LjIiLz4KPGxpbmUgeDE9Ijk5NC41IiB5MT0iOTA0LjQiIHgyPSIxMDU4LjYiIHkyPSI4MDAuOCIvPgo8bGluZSB4MT0iOTk0LjUiIHkxPSI5MDQuNCIgeDI9IjEwNzMuMCIgeTI9IjEwNTAuNyIvPgo8bGluZSB4MT0iOTM0LjgiIHkxPSIxMDk2LjAiIHgyPSIxMDczLjAiIHkyPSIxMDUwLjciLz4KPGxpbmUgeDE9Ijg1OC42IiB5MT0iOTM3LjciIHgyPSI5MzQuOCIgeTI9IjEwOTYuMCIvPgo8bGluZSB4MT0iOTM0LjgiIHkxPSIxMDk2LjAiIHgyPSIxMDgxLjciIHkyPSI5NzkuMiIvPgo8bGluZSB4MT0iMTA2OS43IiB5MT0iLTM0LjciIHgyPSIxMjI5LjQiIHkyPSItNy40Ii8+CjxsaW5lIHgxPSIxMDY5LjciIHkxPSItMzQuNyIgeDI9IjExMzkuMCIgeTI9IjE0MS43Ii8+CjxsaW5lIHgxPSIxMDY5LjciIHkxPSItMzQuNyIgeDI9IjEyMTMuMCIgeTI9IjExMS40Ii8+CjxsaW5lIHgxPSIxMTM5LjAiIHkxPSIxNDEuNyIgeDI9IjEyMTMuMCIgeTI9IjExMS40Ii8+CjxsaW5lIHgxPSIxMTM5LjAiIHkxPSIxNDEuNyIgeDI9IjEwOTQuNSIgeTI9IjI5Ni45Ii8+CjxsaW5lIHgxPSIxMTM5LjAiIHkxPSIxNDEuNyIgeDI9IjEyMjkuNCIgeTI9Ii03LjQiLz4KPGxpbmUgeDE9IjEwOTQuNSIgeTE9IjI5Ni45IiB4Mj0iMTI3NC45IiB5Mj0iMjY0LjYiLz4KPGxpbmUgeDE9IjEwOTQuNSIgeTE9IjI5Ni45IiB4Mj0iMTIxMy4wIiB5Mj0iMTExLjQiLz4KPGxpbmUgeDE9IjExMTkuMSIgeTE9IjY5MS40IiB4Mj0iMTA1OC42IiB5Mj0iODAwLjgiLz4KPGxpbmUgeDE9IjExMTkuMSIgeTE9IjY5MS40IiB4Mj0iMTIzMi45IiB5Mj0iNzgyLjkiLz4KPGxpbmUgeDE9IjExMTkuMSIgeTE9IjY5MS40IiB4Mj0iMTI4Mi4yIiB5Mj0iNTcxLjgiLz4KPGxpbmUgeDE9IjEwNTguNiIgeTE9IjgwMC44IiB4Mj0iMTIzMi45IiB5Mj0iNzgyLjkiLz4KPGxpbmUgeDE9IjEwNTguNiIgeTE9IjgwMC44IiB4Mj0iMTA4MS43IiB5Mj0iOTc5LjIiLz4KPGxpbmUgeDE9IjEwNTguNiIgeTE9IjgwMC44IiB4Mj0iMTIyNC44IiB5Mj0iOTAzLjMiLz4KPGxpbmUgeDE9IjEwODEuNyIgeTE9Ijk3OS4yIiB4Mj0iMTA3My4wIiB5Mj0iMTA1MC43Ii8+CjxsaW5lIHgxPSIxMDgxLjciIHkxPSI5NzkuMiIgeDI9IjEyMjQuOCIgeTI9IjkwMy4zIi8+CjxsaW5lIHgxPSIxMDgxLjciIHkxPSI5NzkuMiIgeDI9IjEyMjIuNyIgeTI9IjExMjUuOSIvPgo8bGluZSB4MT0iMTA3My4wIiB5MT0iMTA1MC43IiB4Mj0iMTIyMi43IiB5Mj0iMTEyNS45Ii8+CjxsaW5lIHgxPSIxMDczLjAiIHkxPSIxMDUwLjciIHgyPSIxMjI0LjgiIHkyPSI5MDMuMyIvPgo8bGluZSB4MT0iODU4LjYiIHkxPSI5MzcuNyIgeDI9IjEwNzMuMCIgeTI9IjEwNTAuNyIvPgo8bGluZSB4MT0iMTIyOS40IiB5MT0iLTcuNCIgeDI9IjEyMTMuMCIgeTI9IjExMS40Ii8+CjxsaW5lIHgxPSIxMjI5LjQiIHkxPSItNy40IiB4Mj0iMTM0OC45IiB5Mj0iMjYuOCIvPgo8bGluZSB4MT0iMTIyOS40IiB5MT0iLTcuNCIgeDI9IjEzNjcuMSIgeTI9IjEyNy43Ii8+CjxsaW5lIHgxPSIxMjEzLjAiIHkxPSIxMTEuNCIgeDI9IjEzNjcuMSIgeTI9IjEyNy43Ii8+CjxsaW5lIHgxPSIxMjEzLjAiIHkxPSIxMTEuNCIgeDI9IjEzNDguOSIgeTI9IjI2LjgiLz4KPGxpbmUgeDE9IjEyMTMuMCIgeTE9IjExMS40IiB4Mj0iMTI3NC45IiB5Mj0iMjY0LjYiLz4KPGxpbmUgeDE9IjEyNzQuOSIgeTE9IjI2NC42IiB4Mj0iMTQxNS4zIiB5Mj0iMzE2LjgiLz4KPGxpbmUgeDE9IjEyNzQuOSIgeTE9IjI2NC42IiB4Mj0iMTM2Ny4xIiB5Mj0iMTI3LjciLz4KPGxpbmUgeDE9IjExMzkuMCIgeTE9IjE0MS43IiB4Mj0iMTI3NC45IiB5Mj0iMjY0LjYiLz4KPGxpbmUgeDE9IjEyODIuMiIgeTE9IjU3MS44IiB4Mj0iMTM1MS45IiB5Mj0iNTg0LjMiLz4KPGxpbmUgeDE9IjEyODIuMiIgeTE9IjU3MS44IiB4Mj0iMTIzMi45IiB5Mj0iNzgyLjkiLz4KPGxpbmUgeDE9IjEyODIuMiIgeTE9IjU3MS44IiB4Mj0iMTQ4NC4yIiB5Mj0iNDEzLjkiLz4KPGxpbmUgeDE9IjEyMzIuOSIgeTE9Ijc4Mi45IiB4Mj0iMTIyNC44IiB5Mj0iOTAzLjMiLz4KPGxpbmUgeDE9IjEyMzIuOSIgeTE9Ijc4Mi45IiB4Mj0iMTM3OS41IiB5Mj0iODEwLjMiLz4KPGxpbmUgeDE9IjEyMzIuOSIgeTE9Ijc4Mi45IiB4Mj0iMTQwNS4zIiB5Mj0iOTEyLjYiLz4KPGxpbmUgeDE9IjEyMjQuOCIgeTE9IjkwMy4zIiB4Mj0iMTM3OS41IiB5Mj0iODEwLjMiLz4KPGxpbmUgeDE9IjEyMjQuOCIgeTE9IjkwMy4zIiB4Mj0iMTQwNS4zIiB5Mj0iOTEyLjYiLz4KPGxpbmUgeDE9IjEyMjQuOCIgeTE9IjkwMy4zIiB4Mj0iMTIyMi43IiB5Mj0iMTEyNS45Ii8+CjxsaW5lIHgxPSIxMjIyLjciIHkxPSIxMTI1LjkiIHgyPSIxNDE1LjciIHkyPSIxMDQwLjMiLz4KPGxpbmUgeDE9IjEzNDguOSIgeTE9IjI2LjgiIHgyPSIxMzY3LjEiIHkyPSIxMjcuNyIvPgo8bGluZSB4MT0iMTM0OC45IiB5MT0iMjYuOCIgeDI9IjE0NzguNCIgeTI9IjkuMCIvPgo8bGluZSB4MT0iMTM0OC45IiB5MT0iMjYuOCIgeDI9IjE1MjUuNCIgeTI9IjExMC4wIi8+CjxsaW5lIHgxPSIxMzY3LjEiIHkxPSIxMjcuNyIgeDI9IjE1MjUuNCIgeTI9IjExMC4wIi8+CjxsaW5lIHgxPSIxMzY3LjEiIHkxPSIxMjcuNyIgeDI9IjE0NzguNCIgeTI9IjkuMCIvPgo8bGluZSB4MT0iMTM2Ny4xIiB5MT0iMTI3LjciIHgyPSIxNDE1LjMiIHkyPSIzMTYuOCIvPgo8bGluZSB4MT0iMTQxNS4zIiB5MT0iMzE2LjgiIHgyPSIxNDcyLjEiIHkyPSIzMDYuOSIvPgo8bGluZSB4MT0iMTQxNS4zIiB5MT0iMzE2LjgiIHgyPSIxNDg0LjIiIHkyPSI0MTMuOSIvPgo8bGluZSB4MT0iMTQxNS4zIiB5MT0iMzE2LjgiIHgyPSIxNjA0LjciIHkyPSIyNDIuOSIvPgo8bGluZSB4MT0iMTM1MS45IiB5MT0iNTg0LjMiIHgyPSIxNTUwLjMiIHkyPSI2NDcuMSIvPgo8bGluZSB4MT0iMTM1MS45IiB5MT0iNTg0LjMiIHgyPSIxNDg0LjIiIHkyPSI0MTMuOSIvPgo8bGluZSB4MT0iMTM1MS45IiB5MT0iNTg0LjMiIHgyPSIxMzc5LjUiIHkyPSI4MTAuMyIvPgo8bGluZSB4MT0iMTM3OS41IiB5MT0iODEwLjMiIHgyPSIxNDgzLjUiIHkyPSI4MDAuMiIvPgo8bGluZSB4MT0iMTM3OS41IiB5MT0iODEwLjMiIHgyPSIxNDA1LjMiIHkyPSI5MTIuNiIvPgo8bGluZSB4MT0iMTM3OS41IiB5MT0iODEwLjMiIHgyPSIxNTI1LjEiIHkyPSI5MjYuMSIvPgo8bGluZSB4MT0iMTQwNS4zIiB5MT0iOTEyLjYiIHgyPSIxNTI1LjEiIHkyPSI5MjYuMSIvPgo8bGluZSB4MT0iMTQwNS4zIiB5MT0iOTEyLjYiIHgyPSIxNDE1LjciIHkyPSIxMDQwLjMiLz4KPGxpbmUgeDE9IjE0MDUuMyIgeTE9IjkxMi42IiB4Mj0iMTQ4My41IiB5Mj0iODAwLjIiLz4KPGxpbmUgeDE9IjE0MTUuNyIgeTE9IjEwNDAuMyIgeDI9IjE0OTAuOSIgeTI9IjExMDMuOCIvPgo8bGluZSB4MT0iMTQxNS43IiB5MT0iMTA0MC4zIiB4Mj0iMTUyNS4xIiB5Mj0iOTI2LjEiLz4KPGxpbmUgeDE9IjE0MTUuNyIgeTE9IjEwNDAuMyIgeDI9IjE1OTguNSIgeTI9Ijk3NS4wIi8+CjxsaW5lIHgxPSIxNDc4LjQiIHkxPSI5LjAiIHgyPSIxNTI1LjQiIHkyPSIxMTAuMCIvPgo8bGluZSB4MT0iMTQ3OC40IiB5MT0iOS4wIiB4Mj0iMTYwNC43IiB5Mj0iLTMuOSIvPgo8bGluZSB4MT0iMTIyOS40IiB5MT0iLTcuNCIgeDI9IjE0NzguNCIgeTI9IjkuMCIvPgo8bGluZSB4MT0iMTUyNS40IiB5MT0iMTEwLjAiIHgyPSIxNjA0LjciIHkyPSItMy45Ii8+CjxsaW5lIHgxPSIxNTI1LjQiIHkxPSIxMTAuMCIgeDI9IjE2MDQuNyIgeTI9IjI0Mi45Ii8+CjxsaW5lIHgxPSIxNTI1LjQiIHkxPSIxMTAuMCIgeDI9IjE2OTMuNiIgeTI9IjE4MS45Ii8+CjxsaW5lIHgxPSIxNDcyLjEiIHkxPSIzMDYuOSIgeDI9IjE0ODQuMiIgeTI9IjQxMy45Ii8+CjxsaW5lIHgxPSIxNDcyLjEiIHkxPSIzMDYuOSIgeDI9IjE2MDQuNyIgeTI9IjI0Mi45Ii8+CjxsaW5lIHgxPSIxMjc0LjkiIHkxPSIyNjQuNiIgeDI9IjE0NzIuMSIgeTI9IjMwNi45Ii8+CjxsaW5lIHgxPSIxNDg0LjIiIHkxPSI0MTMuOSIgeDI9IjE2MjMuMiIgeTI9IjQ0NS45Ii8+CjxsaW5lIHgxPSIxNDg0LjIiIHkxPSI0MTMuOSIgeDI9IjE2MDQuNyIgeTI9IjI0Mi45Ii8+CjxsaW5lIHgxPSIxNDg0LjIiIHkxPSI0MTMuOSIgeDI9IjE1NTAuMyIgeTI9IjY0Ny4xIi8+CjxsaW5lIHgxPSIxNTUwLjMiIHkxPSI2NDcuMSIgeDI9IjE2NzcuOCIgeTI9IjY5NC4yIi8+CjxsaW5lIHgxPSIxNTUwLjMiIHkxPSI2NDcuMSIgeDI9IjE0ODMuNSIgeTI9IjgwMC4yIi8+CjxsaW5lIHgxPSIxNTUwLjMiIHkxPSI2NDcuMSIgeDI9IjE2MjMuMiIgeTI9IjQ0NS45Ii8+CjxsaW5lIHgxPSIxNDgzLjUiIHkxPSI4MDAuMiIgeDI9IjE1MjUuMSIgeTI9IjkyNi4xIi8+CjxsaW5lIHgxPSIxNDgzLjUiIHkxPSI4MDAuMiIgeDI9IjE1OTguNSIgeTI9Ijk3NS4wIi8+CjxsaW5lIHgxPSIxNDgzLjUiIHkxPSI4MDAuMiIgeDI9IjE2OTIuNSIgeTI9IjgyNC42Ii8+CjxsaW5lIHgxPSIxNTI1LjEiIHkxPSI5MjYuMSIgeDI9IjE1OTguNSIgeTI9Ijk3NS4wIi8+CjxsaW5lIHgxPSIxNTI1LjEiIHkxPSI5MjYuMSIgeDI9IjE0OTAuOSIgeTI9IjExMDMuOCIvPgo8bGluZSB4MT0iMTUyNS4xIiB5MT0iOTI2LjEiIHgyPSIxNjkyLjUiIHkyPSI4MjQuNiIvPgo8bGluZSB4MT0iMTQ5MC45IiB5MT0iMTEwMy44IiB4Mj0iMTYyNi41IiB5Mj0iMTA5NS40Ii8+CjxsaW5lIHgxPSIxNDkwLjkiIHkxPSIxMTAzLjgiIHgyPSIxNTk4LjUiIHkyPSI5NzUuMCIvPgo8bGluZSB4MT0iMTQwNS4zIiB5MT0iOTEyLjYiIHgyPSIxNDkwLjkiIHkyPSIxMTAzLjgiLz4KPGxpbmUgeDE9IjE2MDQuNyIgeTE9Ii0zLjkiIHgyPSIxNzQ1LjkiIHkyPSI5Ny45Ii8+CjxsaW5lIHgxPSIxNjA0LjciIHkxPSItMy45IiB4Mj0iMTY5My42IiB5Mj0iMTgxLjkiLz4KPGxpbmUgeDE9IjE2MDQuNyIgeTE9Ii0zLjkiIHgyPSIxODI1LjAiIHkyPSItMzQuNiIvPgo8bGluZSB4MT0iMTY5My42IiB5MT0iMTgxLjkiIHgyPSIxNzQ1LjkiIHkyPSI5Ny45Ii8+CjxsaW5lIHgxPSIxNjkzLjYiIHkxPSIxODEuOSIgeDI9IjE2MDQuNyIgeTI9IjI0Mi45Ii8+CjxsaW5lIHgxPSIxNjkzLjYiIHkxPSIxODEuOSIgeDI9IjE3ODguMCIgeTI9IjI0OC41Ii8+CjxsaW5lIHgxPSIxNjA0LjciIHkxPSIyNDIuOSIgeDI9IjE3ODguMCIgeTI9IjI0OC41Ii8+CjxsaW5lIHgxPSIxNjA0LjciIHkxPSIyNDIuOSIgeDI9IjE3NDUuOSIgeTI9Ijk3LjkiLz4KPGxpbmUgeDE9IjE2MDQuNyIgeTE9IjI0Mi45IiB4Mj0iMTYyMy4yIiB5Mj0iNDQ1LjkiLz4KPGxpbmUgeDE9IjE0NzIuMSIgeTE9IjMwNi45IiB4Mj0iMTYyMy4yIiB5Mj0iNDQ1LjkiLz4KPGxpbmUgeDE9IjE0MTUuMyIgeTE9IjMxNi44IiB4Mj0iMTYyMy4yIiB5Mj0iNDQ1LjkiLz4KPGxpbmUgeDE9IjE2MjMuMiIgeTE9IjQ0NS45IiB4Mj0iMTY3Ny44IiB5Mj0iNjk0LjIiLz4KPGxpbmUgeDE9IjE2NzcuOCIgeTE9IjY5NC4yIiB4Mj0iMTY5Mi41IiB5Mj0iODI0LjYiLz4KPGxpbmUgeDE9IjE2NzcuOCIgeTE9IjY5NC4yIiB4Mj0iMTc3NS41IiB5Mj0iNzg4LjkiLz4KPGxpbmUgeDE9IjE0ODMuNSIgeTE9IjgwMC4yIiB4Mj0iMTY3Ny44IiB5Mj0iNjk0LjIiLz4KPGxpbmUgeDE9IjE2OTIuNSIgeTE9IjgyNC42IiB4Mj0iMTc3NS41IiB5Mj0iNzg4LjkiLz4KPGxpbmUgeDE9IjE2OTIuNSIgeTE9IjgyNC42IiB4Mj0iMTczNS4yIiB5Mj0iOTcwLjYiLz4KPGxpbmUgeDE9IjE2OTIuNSIgeTE9IjgyNC42IiB4Mj0iMTU5OC41IiB5Mj0iOTc1LjAiLz4KPGxpbmUgeDE9IjE1OTguNSIgeTE9Ijk3NS4wIiB4Mj0iMTYyNi41IiB5Mj0iMTA5NS40Ii8+CjxsaW5lIHgxPSIxNTk4LjUiIHkxPSI5NzUuMCIgeDI9IjE3MzUuMiIgeTI9Ijk3MC42Ii8+CjxsaW5lIHgxPSIxNDA1LjMiIHkxPSI5MTIuNiIgeDI9IjE1OTguNSIgeTI9Ijk3NS4wIi8+CjxsaW5lIHgxPSIxNjI2LjUiIHkxPSIxMDk1LjQiIHgyPSIxNzM1LjIiIHkyPSI5NzAuNiIvPgo8bGluZSB4MT0iMTYyNi41IiB5MT0iMTA5NS40IiB4Mj0iMTc5Ni4wIiB5Mj0iMTA1Ny41Ii8+CjxsaW5lIHgxPSIxNTI1LjEiIHkxPSI5MjYuMSIgeDI9IjE2MjYuNSIgeTI9IjEwOTUuNCIvPgo8bGluZSB4MT0iMTgyNS4wIiB5MT0iLTM0LjYiIHgyPSIxOTQzLjIiIHkyPSI0LjkiLz4KPGxpbmUgeDE9IjE4MjUuMCIgeTE9Ii0zNC42IiB4Mj0iMTkxMy4xIiB5Mj0iODguNyIvPgo8bGluZSB4MT0iMTgyNS4wIiB5MT0iLTM0LjYiIHgyPSIxNzQ1LjkiIHkyPSI5Ny45Ii8+CjxsaW5lIHgxPSIxNzQ1LjkiIHkxPSI5Ny45IiB4Mj0iMTc4OC4wIiB5Mj0iMjQ4LjUiLz4KPGxpbmUgeDE9IjE3NDUuOSIgeTE9Ijk3LjkiIHgyPSIxOTEzLjEiIHkyPSI4OC43Ii8+CjxsaW5lIHgxPSIxNzQ1LjkiIHkxPSI5Ny45IiB4Mj0iMTk0My4yIiB5Mj0iNC45Ii8+CjxsaW5lIHgxPSIxNzg4LjAiIHkxPSIyNDguNSIgeDI9IjE4NzkuMiIgeTI9IjMwNi4yIi8+CjxsaW5lIHgxPSIxNzg4LjAiIHkxPSIyNDguNSIgeDI9IjE5MTMuMSIgeTI9Ijg4LjciLz4KPGxpbmUgeDE9IjE3ODguMCIgeTE9IjI0OC41IiB4Mj0iMTk1OC44IiB5Mj0iNDA5LjMiLz4KPGxpbmUgeDE9IjE3NzUuNSIgeTE9Ijc4OC45IiB4Mj0iMTkwMS42IiB5Mj0iNzEyLjciLz4KPGxpbmUgeDE9IjE3NzUuNSIgeTE9Ijc4OC45IiB4Mj0iMTg5Mi4yIiB5Mj0iOTIxLjMiLz4KPGxpbmUgeDE9IjE3NzUuNSIgeTE9Ijc4OC45IiB4Mj0iMTczNS4yIiB5Mj0iOTcwLjYiLz4KPGxpbmUgeDE9IjE3MzUuMiIgeTE9Ijk3MC42IiB4Mj0iMTc5Ni4wIiB5Mj0iMTA1Ny41Ii8+CjxsaW5lIHgxPSIxNzM1LjIiIHkxPSI5NzAuNiIgeDI9IjE4OTIuMiIgeTI9IjkyMS4zIi8+CjxsaW5lIHgxPSIxNzM1LjIiIHkxPSI5NzAuNiIgeDI9IjE4ODEuOSIgeTI9IjExMDYuNSIvPgo8bGluZSB4MT0iMTc5Ni4wIiB5MT0iMTA1Ny41IiB4Mj0iMTg4MS45IiB5Mj0iMTEwNi41Ii8+CjxsaW5lIHgxPSIxNzk2LjAiIHkxPSIxMDU3LjUiIHgyPSIxODkyLjIiIHkyPSI5MjEuMyIvPgo8bGluZSB4MT0iMTU5OC41IiB5MT0iOTc1LjAiIHgyPSIxNzk2LjAiIHkyPSIxMDU3LjUiLz4KPGxpbmUgeDE9IjE5NDMuMiIgeTE9IjQuOSIgeDI9IjE5MTMuMSIgeTI9Ijg4LjciLz4KPGxpbmUgeDE9IjE5MTMuMSIgeTE9Ijg4LjciIHgyPSIxODc5LjIiIHkyPSIzMDYuMiIvPgo8bGluZSB4MT0iMTY5My42IiB5MT0iMTgxLjkiIHgyPSIxOTEzLjEiIHkyPSI4OC43Ii8+CjxsaW5lIHgxPSIxODc5LjIiIHkxPSIzMDYuMiIgeDI9IjE5NTguOCIgeTI9IjQwOS4zIi8+CjxsaW5lIHgxPSIxNjkzLjYiIHkxPSIxODEuOSIgeDI9IjE4NzkuMiIgeTI9IjMwNi4yIi8+CjxsaW5lIHgxPSIxNzQ1LjkiIHkxPSI5Ny45IiB4Mj0iMTg3OS4yIiB5Mj0iMzA2LjIiLz4KPGxpbmUgeDE9IjE5MDEuNiIgeTE9IjcxMi43IiB4Mj0iMTk1NC42IiB5Mj0iODQ3LjciLz4KPGxpbmUgeDE9IjE5MDEuNiIgeTE9IjcxMi43IiB4Mj0iMTg5Mi4yIiB5Mj0iOTIxLjMiLz4KPGxpbmUgeDE9IjE2NzcuOCIgeTE9IjY5NC4yIiB4Mj0iMTkwMS42IiB5Mj0iNzEyLjciLz4KPGxpbmUgeDE9IjE5NTQuNiIgeTE9Ijg0Ny43IiB4Mj0iMTg5Mi4yIiB5Mj0iOTIxLjMiLz4KPGxpbmUgeDE9IjE3NzUuNSIgeTE9Ijc4OC45IiB4Mj0iMTk1NC42IiB5Mj0iODQ3LjciLz4KPGxpbmUgeDE9IjE3MzUuMiIgeTE9Ijk3MC42IiB4Mj0iMTk1NC42IiB5Mj0iODQ3LjciLz4KPGxpbmUgeDE9IjE4OTIuMiIgeTE9IjkyMS4zIiB4Mj0iMTg4MS45IiB5Mj0iMTEwNi41Ii8+CjxsaW5lIHgxPSIxNjkyLjUiIHkxPSI4MjQuNiIgeDI9IjE4OTIuMiIgeTI9IjkyMS4zIi8+CjxsaW5lIHgxPSIxNjI2LjUiIHkxPSIxMDk1LjQiIHgyPSIxODgxLjkiIHkyPSIxMTA2LjUiLz4KPC9nPgo8ZyBzdHJva2U9IiM1QkJDRDgiIHN0cm9rZS13aWR0aD0iMS40IiBmaWxsPSJub25lIiBvcGFjaXR5PSIwLjEyMiI+CjxwYXRoIGQ9Ik0gLTUwLDE5NC40IFEgNjcyLjAsMjEuNiAxMzQ0LjAsMjM3LjYgVCAxOTcwLDE2Mi4wIi8+CjxwYXRoIGQ9Ik0gLTUwLDkxOC4wIFEgNTc2LjAsMTEwMS42IDEyNDguMCw4NjQuMCBUIDE5NzAsOTcyLjAiLz4KPC9nPgo8Zz4KPGNpcmNsZSBjeD0iMTMuNCIgY3k9Ii00NC45IiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9Ii0yMS42IiBjeT0iMTA4LjgiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMjIuNyIgY3k9IjI4Ni43IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9Ii00NS41IiBjeT0iNjQ2LjUiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iNC4zIiBjeT0iNzgzLjYiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iOC42IiBjeT0iOTc0LjIiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iLTQ3LjQiIGN5PSIxMTA4LjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTU2LjIiIGN5PSItMTUuMSIgcj0iMi42IiBmaWxsPSIjNUJCQ0Q4IiBvcGFjaXR5PSIwLjI4NiIvPgo8Y2lyY2xlIGN4PSIxMDQuMSIgY3k9IjE3OC4yIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEyMS41IiBjeT0iMjMxLjUiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iOTguNCIgY3k9IjQzNy44IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE4Mi42IiBjeT0iNjYzLjUiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTY4LjgiIGN5PSI4MjEuMiIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSIxNzEuOSIgY3k9Ijk1Mi4zIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE1Ni44IiBjeT0iMTAzNy4xIiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9IjI0OC4yIiBjeT0iLTE5LjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMjMzLjkiIGN5PSIxMDkuNyIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSIyMzYuMCIgY3k9IjI0OS4wIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjI0Ni40IiBjeT0iNTE4LjAiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMjk2LjMiIGN5PSI3NzguMiIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSIyNjIuNyIgY3k9Ijk5MS4zIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjI4Ny43IiBjeT0iMTA4NS40IiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9IjQyOS4yIiBjeT0iMzIuNCIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI0MzcuOSIgY3k9IjEwOS40IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjM2Ni41IiBjeT0iMjUyLjYiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMzg5LjEiIGN5PSIzNzcuNyIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI0NDcuNiIgY3k9IjUyMi41IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjM4OC45IiBjeT0iNzg2LjEiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iNDE3LjMiIGN5PSI5MjIuNiIgcj0iMi42IiBmaWxsPSIjNUJCQ0Q4IiBvcGFjaXR5PSIwLjI4NiIvPgo8Y2lyY2xlIGN4PSI0MTkuNSIgY3k9IjExMTcuNiIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI1MzguOSIgY3k9Ii0yNi41IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjU5Ni4zIiBjeT0iMTM1LjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iNTA5LjMiIGN5PSIyMjcuMiIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI1MTEuMSIgY3k9IjQxNy4wIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjU5Ni4yIiBjeT0iNjc3LjgiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iNTgzLjIiIGN5PSI3NjMuOCIgcj0iMi42IiBmaWxsPSIjNUJCQ0Q4IiBvcGFjaXR5PSIwLjI4NiIvPgo8Y2lyY2xlIGN4PSI1NjkuOCIgY3k9Ijk2Mi4yIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjU1Mi4xIiBjeT0iMTA1OC4wIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjY5OS4yIiBjeT0iLTM2LjciIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iNjc5LjUiIGN5PSIxMzAuNiIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI3MjkuMyIgY3k9IjMwNS41IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjcxMC45IiBjeT0iODEzLjciIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iNzEyLjUiIGN5PSI5NDcuOSIgcj0iMi42IiBmaWxsPSIjNUJCQ0Q4IiBvcGFjaXR5PSIwLjI4NiIvPgo8Y2lyY2xlIGN4PSI2MzcuOCIgY3k9IjEwNjMuNCIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI3NzYuNyIgY3k9IjQwLjUiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iODU5LjIiIGN5PSIxNjYuMyIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI4MDQuNCIgY3k9IjIyOC4yIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjgyMS41IiBjeT0iNDk5LjMiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iODI3LjYiIGN5PSI3ODcuOCIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI4NTguNiIgY3k9IjkzNy43IiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9Ijc5NS4yIiBjeT0iMTA4My43IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9Ijk4Mi4xIiBjeT0iLTI4LjIiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iOTQxLjkiIGN5PSIxODEuOCIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI5NzQuNCIgY3k9IjI2NC4yIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjkzMy4xIiBjeT0iNjM0LjUiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iOTM0LjAiIGN5PSI4NDguMyIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSI5OTQuNSIgY3k9IjkwNC40IiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9IjkzNC44IiBjeT0iMTA5Ni4wIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEwNjkuNyIgY3k9Ii0zNC43IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjExMzkuMCIgY3k9IjE0MS43IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEwOTQuNSIgY3k9IjI5Ni45IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjExMTkuMSIgY3k9IjY5MS40IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEwNTguNiIgY3k9IjgwMC44IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEwODEuNyIgY3k9Ijk3OS4yIiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9IjEwNzMuMCIgY3k9IjEwNTAuNyIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSIxMjI5LjQiIGN5PSItNy40IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEyMTMuMCIgY3k9IjExMS40IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEyNzQuOSIgY3k9IjI2NC42IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEyODIuMiIgY3k9IjU3MS44IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEyMzIuOSIgY3k9Ijc4Mi45IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEyMjQuOCIgY3k9IjkwMy4zIiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9IjEyMjIuNyIgY3k9IjExMjUuOSIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSIxMzQ4LjkiIGN5PSIyNi44IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEzNjcuMSIgY3k9IjEyNy43IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE0MTUuMyIgY3k9IjMxNi44IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEzNTEuOSIgY3k9IjU4NC4zIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjEzNzkuNSIgY3k9IjgxMC4zIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE0MDUuMyIgY3k9IjkxMi42IiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9IjE0MTUuNyIgY3k9IjEwNDAuMyIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSIxNDc4LjQiIGN5PSI5LjAiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTUyNS40IiBjeT0iMTEwLjAiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTQ3Mi4xIiBjeT0iMzA2LjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTQ4NC4yIiBjeT0iNDEzLjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTU1MC4zIiBjeT0iNjQ3LjEiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTQ4My41IiBjeT0iODAwLjIiIHI9IjIuNiIgZmlsbD0iIzVCQkNEOCIgb3BhY2l0eT0iMC4yODYiLz4KPGNpcmNsZSBjeD0iMTUyNS4xIiBjeT0iOTI2LjEiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTQ5MC45IiBjeT0iMTEwMy44IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE2MDQuNyIgY3k9Ii0zLjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTY5My42IiBjeT0iMTgxLjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTYwNC43IiBjeT0iMjQyLjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTYyMy4yIiBjeT0iNDQ1LjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTY3Ny44IiBjeT0iNjk0LjIiIHI9IjIuNiIgZmlsbD0iIzVCQkNEOCIgb3BhY2l0eT0iMC4yODYiLz4KPGNpcmNsZSBjeD0iMTY5Mi41IiBjeT0iODI0LjYiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTU5OC41IiBjeT0iOTc1LjAiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTYyNi41IiBjeT0iMTA5NS40IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE4MjUuMCIgY3k9Ii0zNC42IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE3NDUuOSIgY3k9Ijk3LjkiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTc4OC4wIiBjeT0iMjQ4LjUiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTc3NS41IiBjeT0iNzg4LjkiIHI9IjIuNiIgZmlsbD0iIzVCQkNEOCIgb3BhY2l0eT0iMC4yODYiLz4KPGNpcmNsZSBjeD0iMTczNS4yIiBjeT0iOTcwLjYiIHI9IjEuOCIgZmlsbD0iIzFCNUM3NCIgb3BhY2l0eT0iMC4yMDQiLz4KPGNpcmNsZSBjeD0iMTc5Ni4wIiBjeT0iMTA1Ny41IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE5NDMuMiIgY3k9IjQuOSIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8Y2lyY2xlIGN4PSIxOTEzLjEiIGN5PSI4OC43IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE4NzkuMiIgY3k9IjMwNi4yIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE5NTguOCIgY3k9IjQwOS4zIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE5MDEuNiIgY3k9IjcxMi43IiByPSIyLjYiIGZpbGw9IiM1QkJDRDgiIG9wYWNpdHk9IjAuMjg2Ii8+CjxjaXJjbGUgY3g9IjE5NTQuNiIgY3k9Ijg0Ny43IiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE4OTIuMiIgY3k9IjkyMS4zIiByPSIxLjgiIGZpbGw9IiMxQjVDNzQiIG9wYWNpdHk9IjAuMjA0Ii8+CjxjaXJjbGUgY3g9IjE4ODEuOSIgY3k9IjExMDYuNSIgcj0iMS44IiBmaWxsPSIjMUI1Qzc0IiBvcGFjaXR5PSIwLjIwNCIvPgo8L2c+Cjwvc3ZnPg=="
)

def _set_landing_background():
    """Подключает фоновый SVG (сеть/сигналы, брендовые цвета) только на лендинге."""
    st.markdown(f"""
    <style>
    .stApp {{
        background-image: url("data:image/svg+xml;base64,{_LANDING_BG_SVG_B64}");
        background-size: cover;
        background-position: center top;
        background-repeat: no-repeat;
        background-attachment: fixed;
    }}
    </style>
    """, unsafe_allow_html=True)

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

# Добавляем Админку: superadmin — всегда, segment_admin — только если
# суперадмин не выключил доступ к Админке для его сегмента
_admin_panel_allowed = (
    _current_user.get("role") == "superadmin"
    or (
        _current_user.get("role") == "segment_admin"
        and is_admin_panel_enabled(_current_user.get("org_id", ""))
    )
)
if _admin_panel_allowed and "Админка" not in _visible_products:
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
    _set_landing_background()
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

    # Поле поиска — мини-RAG по маркетинговым карточкам модулей (core/landing_search.py).
    # Промпт и содержимое базы редактируются суперадмином на вкладке «Лендинг»
    # (streamlit_pages/superadmin.py) — здесь только вызов уже готового пайплайна.
    # Без узкой st.columns([1,4,1])-обёртки: строка поиска и блок ответа занимают
    # всю ширину контента, совпадая по краям с сеткой плашек модулей ниже.
    _sic1, _sic2 = st.columns([5, 1], vertical_alignment="bottom")
    with _sic1:
        _search_val = st.text_input(
            "Поиск",
            key="landing_search",
            placeholder="Задайте вопрос и я найду как помочь...",
            label_visibility="collapsed",
        )
    with _sic2:
        with st.container(key="landing_search_btn_wrap"):
            _search_go = st.button("Найти", key="landing_search_btn", use_container_width=True)

    if _search_go and _search_val.strip():
        try:
            from core.landing_search import search_landing_content, stream_landing_answer
            # advisor_model заполняется только при первом открытии Советчика
            # (streamlit_pages/advisor_page.py) — на лендинге его может ещё
            # не быть в session_state, тогда stream_landing_answer сама
            # возьмёт default_model из config/advisor_config.json.
            _landing_model = st.session_state.get("advisor_model")
            with st.spinner("Ищу подходящий раздел..."):
                _lsources = search_landing_content(_search_val.strip())
            if _lsources:
                _lgen = stream_landing_answer(_search_val.strip(), _lsources, model=_landing_model)
                with st.container(border=True):
                    st.write_stream(_lgen)
            else:
                st.info(
                    "Не нашлось точного совпадения по модулям. "
                    "Попробуйте переформулировать запрос или откройте Советчика в меню слева."
                )
        except Exception as _ld_e:
            st.warning(f"Умный поиск временно недоступен: {_ld_e}. Воспользуйтесь Советчиком.")

    st.markdown("<div style='height:1.5rem'></div>", unsafe_allow_html=True)
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
    # Защита на уровне страницы — даже если main_choice окажется выставлен
    # в обход кнопки сайдбара (не только скрытие кнопки выше).
    if not is_module_accessible(_current_user, "admin"):
        st.error("Доступ к этому разделу отключён для вашего сегмента.")
        st.stop()
    show_admin_panel()