import streamlit as st
import os
import sys
import pandas as pd
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

# =============================================================================

# =============================================================================
# 🎨 Настройка страницы
# =============================================================================
st.set_page_config(page_title="РЕГУЛА.AI", layout="wide", page_icon="⚙")

# =============================================================================
# 🔐 Session state
# =============================================================================
if "admin_logged_in" not in st.session_state:
    st.session_state.admin_logged_in = False
if "show_landing" not in st.session_state:
    st.session_state.show_landing = True

def is_admin_logged() -> bool:
    return st.session_state.get("admin_logged_in", False)

# =============================================================================
# 🎨 CSS
# =============================================================================
st.markdown("""
<style>
/* ── Переменные бренда ─────────────────────────────────────────────────── */
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

/* ── Фон и шрифт ───────────────────────────────────────────────────────── */
.stApp { background-color: var(--neutral-bg);
         font-family: "Inter", "Segoe UI", system-ui, sans-serif; }
h1, h2, h3, h4 { color: var(--text-primary); font-family: inherit; }

/* ── Боковая панель ────────────────────────────────────────────────────── */
.stSidebar { background-color: #ffffff; border-right: 1px solid var(--neutral-border); }
.stSidebar * { text-align: left !important; }

/* ── Кнопки — без двойной границы ──────────────────────────────────────── */
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

/* ── Экспандеры — одиночная граница, без дублирования ──────────────────── */
[data-testid="stExpander"] {
    border: 1px solid var(--neutral-border) !important;
    border-radius: var(--radius) !important;
    background: #ffffff !important;
    box-shadow: none !important;
}
[data-testid="stExpander"] > details,
[data-testid="stExpander"] > details > summary,
[data-testid="stExpander"] > details > div {
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
}
[data-testid="stExpander"] > details > summary {
    border-bottom: 1px solid var(--neutral-border) !important;
    border-radius: 0 !important;
    padding: 0.6rem 0.9rem !important;
}
[data-testid="stExpander"] > details:not([open]) > summary {
    border-bottom: none !important;
}

/* ── Ползунки (slider) — фирменный оттенок, только внутри stSlider ─────── */
[data-testid="stSlider"] [role="slider"],
[data-testid="stSlider"] [class*="thumb"] {
    background-color: var(--brand-mid) !important;
    border-color: var(--brand-mid) !important;
    box-shadow: 0 0 0 3px var(--brand-light2) !important;
}
[data-testid="stSlider"] [class*="track"]:last-child,
[data-testid="stSlider"] [class*="Track"]:last-child {
    background-color: var(--brand-mid) !important;
}

/* ── Прогресс-бар st.progress() ────────────────────────────────────────── */
[data-testid="stProgress"] > div {
    background-color: var(--brand-light2) !important;
    border-radius: 4px !important;
}
[data-testid="stProgress"] > div > div {
    background-color: var(--brand-mid) !important;
    border-radius: 4px !important;
}
/* Текст progress(text=...) — принудительно тёмный, перебиваем тему */
[data-testid="stProgress"] p,
[data-testid="stProgress"] span,
[data-testid="stProgress"] div > p,
[data-testid="stProgress"] + div p {
    color: var(--text-primary) !important;
    font-size: 0.82em !important;
}

/* ── Radio — цвет задаётся через .streamlit/config.toml primaryColor ─────── */
[data-testid="stRadio"] label:hover { color: var(--brand-primary) !important; }
[data-testid="stRadio"] div[aria-checked="true"] ~ label {
    color: var(--brand-primary) !important;
    font-weight: 600 !important;
}


/* ── Табы — активная вкладка фирменного цвета ───────────────────────────── */
[data-baseweb="tab-list"] {
    border-bottom: 2px solid var(--neutral-border) !important;
    gap: 0 !important;
}
[data-baseweb="tab"] {
    border-radius: var(--radius) var(--radius) 0 0 !important;
    border: none !important;
    color: var(--text-secondary) !important;
    font-weight: 500 !important;
    padding: 0.5rem 1.1rem !important;
    transition: color 0.15s ease, background-color 0.15s ease !important;
}
[data-baseweb="tab"]:hover {
    color: var(--brand-primary) !important;
    background-color: var(--brand-bg) !important;
}
[aria-selected="true"][data-baseweb="tab"] {
    color: var(--brand-primary) !important;
    font-weight: 700 !important;
    border-bottom: 3px solid var(--brand-primary) !important;
    background-color: #ffffff !important;
}
[data-baseweb="tab-highlight"] {
    background-color: var(--brand-primary) !important;
    height: 3px !important;
}

/* ── Алерты info/success/warning — фирменная гамма вместо синего ────────── */
[data-testid="stAlert"][data-baseweb="notification"] {
    border-radius: var(--radius) !important;
}
/* info (голубой) → фирменный */
div[data-testid="stAlert"][kind="info"],
div.element-container div[data-baseweb="notification"][kind="info"] {
    background-color: var(--brand-bg) !important;
    border-left: 4px solid var(--brand-primary) !important;
    color: var(--text-primary) !important;
}
/* Streamlit 1.3x+ selector */
.stAlert > div[data-testid="stMarkdownContainer"] { color: var(--text-primary) !important; }
[data-testid="stNotification"],
[class*="AlertContainer"] {
    background-color: var(--brand-bg) !important;
    border-left: 4px solid var(--brand-primary) !important;
    border-radius: var(--radius) !important;
    color: var(--text-primary) !important;
}
[class*="AlertContainer"] svg { color: var(--brand-primary) !important; }

/* ── Таблицы ───────────────────────────────────────────────────────────── */
.stDataFrame { min-height: 200px; }
.dataframe { border: 1px solid var(--neutral-border); border-radius: var(--radius); }

/* ── Метрики ───────────────────────────────────────────────────────────── */
.stMetric { background: #ffffff; padding: 0.5rem;
            border-radius: var(--radius); border: 1px solid var(--neutral-border); }

/* ── Кастомные блоки ───────────────────────────────────────────────────── */
.redirect-box {
    margin: 1rem 0; padding: 1rem; background: var(--brand-bg);
    border-left: 4px solid var(--brand-primary);
    border-radius: 0 var(--radius) var(--radius) 0;
}

/* ── Логотип в сайдбаре ────────────────────────────────────────────────── */
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

/* ── Навигация сайдбара ─────────────────────────────────────────────────── */

/* Сброс ВСЕХ кнопок в сайдбаре → nav-стиль */
[data-testid="stSidebar"] .stButton > button {
    background-color: transparent !important;
    color: var(--text-secondary) !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0.28rem 0.75rem !important;
    font-weight: 400 !important;
    font-size: 0.88rem !important;
    border-radius: var(--radius) !important;
    width: 100% !important;
    min-height: 0 !important;
    height: auto !important;
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    transition: background-color 0.12s ease, color 0.12s ease !important;
}
/* Текст внутри кнопки (Streamlit рендерит через <p>) */
[data-testid="stSidebar"] .stButton > button p,
[data-testid="stSidebar"] .stButton > button span,
[data-testid="stSidebar"] .stButton > button div {
    text-align: left !important;
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background-color: var(--brand-bg) !important;
    color: var(--brand-primary) !important;
    border: none !important;
    box-shadow: none !important;
}
[data-testid="stSidebar"] .stButton > button:focus,
[data-testid="stSidebar"] .stButton > button:focus-visible {
    box-shadow: none !important;
    border: none !important;
    outline: none !important;
}

/* Активная nav-кнопка (type="primary") */
[data-testid="stSidebar"] .stButton > button[kind="primary"] {
    background-color: rgba(27, 92, 116, 0.1) !important;
    color: var(--brand-primary) !important;
    font-weight: 600 !important;
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
    content: "›";
    font-size: 1rem;
    font-weight: 300;
    opacity: 0.55;
    flex-shrink: 0;
    padding-left: 0.3rem;
}

/* Убираем отступы между nav-элементами */
[data-testid="stSidebar"] .element-container {
    margin-top: 0 !important;
    margin-bottom: 0 !important;
}

.nav-section-label {
    font-size: 0.68rem; font-weight: 600; letter-spacing: 0.09em;
    text-transform: uppercase; color: var(--text-secondary);
    padding: 0.6rem 0.75rem 0.2rem; margin-top: 0.2rem;
}

/* ── Плитки лендинга ───────────────────────────────────────────────────── */
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
    transform: translateY(-2px) !important;
    color: var(--brand-dark) !important;
}
.landing-tile-desc {
    font-size: 0.78rem; color: var(--text-secondary);
    margin-top: 0.15rem; margin-bottom: 1rem;
    padding: 0 0.15rem; line-height: 1.45; display: block;
}
.landing-tile-desc ul {
    margin: 0.2rem 0 0 0; padding-left: 1.1rem; list-style: disc;
}
.landing-tile-desc ul li {
    margin-bottom: 0.2rem;
}

/* ── Метрики лендинга ──────────────────────────────────────────────────── */
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

/* ── Отзывчивость кнопок навигации ─────────────────────────────────────── */
[data-testid="stSidebar"] .stButton > button:active {
    opacity: 0.6 !important;
    transform: scale(0.97) !important;
    transition: all 0.05s ease !important;
}
[data-testid="stSidebar"] .stButton > button {
    cursor: pointer !important;
}

</style>
""", unsafe_allow_html=True)

# =============================================================================
# 🧭 Боковое меню
# =============================================================================
_ACTIVE_PRODUCTS = [
    "Советчик", "Сканер документов", "Анализатор заявок",
    "Прогноз решения регулятора", "Протокольщик", "Админка",
]
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
}

if "main_choice" not in st.session_state:
    st.session_state.main_choice = _ACTIVE_PRODUCTS[0]

with st.sidebar:
    st.markdown('<div class="sidebar-logo">', unsafe_allow_html=True)
    if st.button("РЕГУЛА.AI — Главная", key="sidebar_home_btn"):
        st.session_state.show_landing = True
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    st.divider()
    _on_product_page = not st.session_state.get("show_landing", True)
    for _product in _ACTIVE_PRODUCTS:
        _is_active = _on_product_page and st.session_state.main_choice == _product
        if st.button(
            _product,
            key=f"nav_active_{_product}",
            use_container_width=True,
            type="primary" if _is_active else "secondary",
        ):
            st.session_state.main_choice = _product
            st.session_state.show_landing = False
            st.rerun()

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

    st.divider()
    if is_admin_logged():
        st.success("Админка: вход выполнен")
        if st.button("Выйти", key="sidebar_logout_btn"):
            st.session_state.admin_logged_in = False
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
    st.markdown("""
    <div style="text-align:center; padding: 2rem 0 1.5rem;">
        <div style="font-size:2.6rem; font-weight:900; letter-spacing:0.02em;
                    color:#1B5C74; margin-bottom:0.5rem;">РЕГУЛА.AI</div>
        <div style="font-size:0.9rem; color:#1B5C74; font-weight:500; letter-spacing:0.08em;
                    text-transform:uppercase; margin-bottom:1rem;">
            ИИ-система в сфере тарифного регулирования РФ
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Метрики — ценностное предложение
    m1, m2, m3 = st.columns(3)
    with m1:
        st.markdown("""
        <div class="landing-metric">
            <div class="landing-metric-value">–30%</div>
            <div class="landing-metric-label">нагрузки на специалистов по регулированию</div>
        </div>""", unsafe_allow_html=True)
    with m2:
        st.markdown("""
        <div class="landing-metric">
            <div class="landing-metric-value">+90%</div>
            <div class="landing-metric-label">проходимость заявок с учётом подсвеченных рисков</div>
        </div>""", unsafe_allow_html=True)
    with m3:
        st.markdown("""
        <div class="landing-metric">
            <div class="landing-metric-value">НПА</div>
            <div class="landing-metric-label">ответы опираются на актуальную нормативную базу</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<hr style='border:none;border-top:1px solid #dce3ec;margin:1.5rem 0 1rem;'>", unsafe_allow_html=True)
    st.markdown("#### Функции")
    st.markdown("<div style='height:0.3rem'></div>", unsafe_allow_html=True)
    _cols_per_row = 3
    for _row_start in range(0, len(_ACTIVE_PRODUCTS), _cols_per_row):
        _row_items = _ACTIVE_PRODUCTS[_row_start:_row_start + _cols_per_row]
        _cols = st.columns(_cols_per_row, gap="medium")
        for _ci, _product in enumerate(_row_items):
            with _cols[_ci]:
                _desc = _PRODUCT_DESCRIPTIONS.get(_product, "")
                st.markdown('<div class="landing-tile">', unsafe_allow_html=True)
                if st.button(_product, key=f"landing_tile_{_product}", use_container_width=True):
                    st.session_state.main_choice = _product
                    st.session_state.show_landing = False
                    st.rerun()
                st.markdown(f'<div class="landing-tile-desc">{_desc}</div>', unsafe_allow_html=True)
                st.markdown('</div>', unsafe_allow_html=True)
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
# 🏷️ Бренд-бар — отображается на каждом под-продукте
# =============================================================================
st.markdown("""
<div style="
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    margin-bottom: 1.2rem;
    margin-top: -1.5rem;
    line-height: 1;
    width: 100%;
    gap: 0.55rem;
">
    <span style="
        font-size: 2.6rem;
        font-weight: 900;
        color: #1B5C74;
        letter-spacing: 0.02em;
        line-height: 1;
    ">РЕГУЛА.AI</span>
    <span style="
        font-size: 0.9rem;
        font-weight: 500;
        color: #1B5C74;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        opacity: 0.7;
    ">ИИ-система в сфере тарифного регулирования РФ</span>
</div>
""", unsafe_allow_html=True)

# (тестовые данные перенесены в streamlit_pages/claim_analyzer.py)

# =============================================================================
# Вкладка 1: Анализатор заявок
# =============================================================================
if main_choice == "Анализатор заявок":
    try:
        from streamlit_pages.claim_analyzer import show_claim_analyzer
        show_claim_analyzer()
    except ImportError as e:
        st.error(f"Ошибка загрузки анализатора: {e}")

# =============================================================================
# Вкладка 2: Советчик — со стримингом ответа
# =============================================================================

# =============================================================================
# Вкладка 2: Советчик
# =============================================================================
elif main_choice == "Советчик":
    show_advisor()

# =============================================================================
# Остальные продукты
# =============================================================================
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

elif main_choice == "Сканер документов":
    try:
        from streamlit_pages.doc_scanner import show_doc_scanner
        show_doc_scanner()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Протокольщик":
    try:
        from streamlit_pages.protocol_bot import show_protocol_bot
        show_protocol_bot()
    except ImportError as e:
        st.error(f"Ошибка: {e}")

elif main_choice == "Прогноз решения регулятора":
    try:
        from streamlit_pages.predictor import show_predictor
        show_predictor()
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

# =============================================================================
# Вкладка: Админка
# =============================================================================

# =============================================================================
# Вкладка: Админка
# =============================================================================
elif main_choice == "Админка":
    show_admin_panel()