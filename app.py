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

from config import TEST_FILES_DIR
from core.feedback import submit_feedback, get_feedback, get_answer_stats
from core import admin

# =============================================================================
# 📊 Статистика (прямое чтение файла)
# =============================================================================
def get_live_answer_stats(days: int = 7):
    feedback_file = os.path.join("data", "feedback", "feedback_log.jsonl")
    stats = {
        "total": 0, "rating_3": 0, "rating_2": 0, "rating_1": 0,
        "with_comment": 0, "by_category": {}, "top_bad_questions": [], "avg_rating": 0
    }
    if not os.path.exists(feedback_file):
        return stats
    with open(feedback_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fb = json.loads(line)
            except Exception:
                continue
            if fb.get("feedback_type") != "answer_rating":
                continue
            stats["total"] += 1
            rating = fb.get("rating")
            if rating == 3:   stats["rating_3"] += 1
            elif rating == 2: stats["rating_2"] += 1
            elif rating == 1: stats["rating_1"] += 1
            if fb.get("question"):
                stats["top_bad_questions"].append({
                    "question":  fb["question"][:100],
                    "answer":    fb.get("answer", "")[:200],
                    "comment":   fb.get("description", ""),
                    "timestamp": fb["timestamp"],
                })
            if stats["total"] > 0:
                stats["avg_rating"] = round(
                    (stats["rating_3"]*3 + stats["rating_2"]*2 + stats["rating_1"]*1) / stats["total"], 2
                )
    try:
        print(f"[APP STATS] total={stats['total']}, good={stats['rating_3']}, bad={stats['rating_1']}")
    except Exception:
        pass
    return stats


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
    padding: 0 0.15rem; line-height: 1.45; min-height: 3.6rem; display: block;
}

/* ── Метрики лендинга ──────────────────────────────────────────────────── */
.landing-metric {
    background: #ffffff; border: 1px solid var(--neutral-border);
    border-radius: var(--radius); padding: 0.8rem 1rem;
    text-align: center; margin-bottom: 0.5rem;
}
.landing-metric-value { font-size: 1.5rem; font-weight: 700; color: var(--brand-primary); }
.landing-metric-label { font-size: 0.75rem; color: var(--text-secondary); margin-top: 0.2rem; }
.main-title { display: none; }
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
    "Советчик": "Даёт ответы на вопросы по нормативной базе тарифного регулирования. Снижает нагрузку на специалистов на 30%. Ссылается на актуальные НПА.",
    "Сканер документов": "Распознаёт текст из PDF, DOCX и сканов. Формирует базу из ваших документов. Позволяет делать краткий пересказ и полнотекстовый поиск.",
    "Анализатор заявок": "Проверяет тарифную заявку на полноту комплекта. Подсвечивает риски по каждой статье затрат. Повышает проходимость заявок.",
    "Прогноз решения регулятора": "Оценивает вероятность одобрения заявки регулятором на основе исторических данных. Снижает риски неодобрения статей.",
    "Протокольщик": "Автоматически составляет протоколы заседаний из аудио или текста. Сокращает время подготовки протокола в несколько раз.",
    "Админка": "Управление системой: загрузка документов, настройка параметров поиска, промпты, аналитика использования.",
}

if "main_choice" not in st.session_state:
    st.session_state.main_choice = _ACTIVE_PRODUCTS[0]

def _on_active_select():
    st.session_state.main_choice = st.session_state._sidebar_active

def _on_dev_select():
    st.session_state.main_choice = st.session_state._sidebar_dev

with st.sidebar:
    st.markdown('<div class="sidebar-logo">', unsafe_allow_html=True)
    if st.button("РЕГУЛА.AI — Главная", key="sidebar_home_btn"):
        st.session_state.show_landing = True
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)
    st.markdown("**Разделы**")
    # Синхронизируем radio-ключи с main_choice: при переходе с лендинга
    # session_state[key] имеет приоритет над index, поэтому обновляем явно.
    if st.session_state.main_choice in _ACTIVE_PRODUCTS:
        st.session_state["_sidebar_active"] = st.session_state.main_choice
    _active_idx = (
        _ACTIVE_PRODUCTS.index(st.session_state.main_choice)
        if st.session_state.main_choice in _ACTIVE_PRODUCTS else None
    )
    st.radio("active", _ACTIVE_PRODUCTS, index=_active_idx, key="_sidebar_active",
             label_visibility="collapsed", on_change=_on_active_select)
    st.divider()
    _dev_expanded = st.session_state.main_choice in _DEV_PRODUCTS
    with st.expander("Наши планы", expanded=_dev_expanded):
        st.caption("Продукты в активной разработке, доступны для ознакомления.")
        if st.session_state.main_choice in _DEV_PRODUCTS:
            st.session_state["_sidebar_dev"] = st.session_state.main_choice
        _dev_idx = (
            _DEV_PRODUCTS.index(st.session_state.main_choice)
            if st.session_state.main_choice in _DEV_PRODUCTS else None
        )
        st.radio("dev", _DEV_PRODUCTS, index=_dev_idx, key="_sidebar_dev",
                 label_visibility="collapsed", on_change=_on_dev_select)
    st.divider()
    if is_admin_logged():
        st.success("Админка: вход выполнен")
        if st.button("Выйти"):
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
            Советчик в сфере тарифного регулирования РФ
        </div>
        <div style="font-size:1rem; color:#5a6a7a; max-width:580px; margin:0 auto; line-height:1.65;">
            ИИ-система поддержки принятия решений в области тарифного регулирования
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
    ">регулирование сегодня</span>
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
elif main_choice == "Советчик":
    st.header("Советчик по нормативной базе")
    st.info("Задайте вопрос по тарифному регулированию — система найдёт ответ в актуальной базе НПА")

    # Инициализация session_state
    # Загружаем сохранённые настройки советчика
    _adv_prefs_file = os.path.join("config", "advisor_prefs.json")
    _adv_defaults   = {"top_k": 20, "neighbor_radius": 0, "temperature": 0.3}
    if "_adv_prefs_loaded" not in st.session_state:
        try:
            if os.path.exists(_adv_prefs_file):
                with open(_adv_prefs_file, "r", encoding="utf-8") as _f:
                    _adv_prefs = {**_adv_defaults, **json.load(_f)}
            else:
                _adv_prefs = _adv_defaults
        except Exception:
            _adv_prefs = _adv_defaults
        st.session_state["_adv_top_k"]          = _adv_prefs["top_k"]
        st.session_state["_adv_neighbor_radius"] = _adv_prefs["neighbor_radius"]
        st.session_state["_adv_temperature"]     = _adv_prefs["temperature"]
        st.session_state["_adv_prefs_loaded"]    = True

    for key, val in [
        ("last_query", ""), ("last_result", None), ("search_triggered", False),
        ("sources_only_mode", False), ("query_times", []),
        ("advisor_model", "qwen/qwen3.5-9b"),
    ]:
        if key not in st.session_state:
            st.session_state[key] = val

    # Проверка векторной базы
    vector_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vector_db")
    db_file = os.path.join(vector_db_path, "chroma.sqlite3")
    if not os.path.exists(db_file):
        st.warning("⚠️ Векторная база не найдена. Запустите индексацию в Админке.")
        st.info(f"📂 Ожидаемый путь: {db_file}")
        st.stop()

    with st.expander("Варианты использования", expanded=False):
        st.write("• Можно ли включать затраты на ДМС в тариф?")
        st.write("• Какие документы нужны для тарифной заявки по теплоснабжению?")
        st.write("• Как ФАС трактует расходы на программное обеспечение?")
        st.write("• Что такое валовая выручка и как она рассчитывается?")

    # ── Настройки ────────────────────────────────────────────────────────────
    with st.expander("Настройки", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            top_k = st.slider(
                "Количество источников (топ-K)", 1, 50,
                st.session_state.get("_adv_top_k", 20),
                key="top_k_slider",
                help="Сколько чанков передаётся LLM после реранкинга",
            )
            temperature = st.slider(
                "Креативность ответа", 0.0, 1.0,
                float(st.session_state.get("_adv_temperature", 0.3)),
                0.1, key="temp_slider",
            )
            neighbor_radius = st.slider(
                "Соседних чанков с каждой стороны", 0, 5,
                st.session_state.get("_adv_neighbor_radius", 0),
                key="neighbor_radius_slider",
                help="Для каждого найденного чанка подтягивается N соседей. "
                     "0 — только сам чанк (рекомендуется при режиме ⚖️ По пунктам НПА). "
                     "Больше — шире контекст, но LLM может потеряться.",
            )
            st.session_state.neighbor_radius        = neighbor_radius
            st.session_state["_adv_top_k"]          = top_k
            st.session_state["_adv_neighbor_radius"] = neighbor_radius
            st.session_state["_adv_temperature"]     = temperature
            if neighbor_radius > 0:
                st.caption(f"Каждый результат даёт {1 + neighbor_radius * 2} чанков контекста")
            # Сохраняем в конфиг при каждом изменении
            try:
                os.makedirs("config", exist_ok=True)
                with open(os.path.join("config", "advisor_prefs.json"), "w", encoding="utf-8") as _f:
                    json.dump({"top_k": top_k, "neighbor_radius": neighbor_radius,
                               "temperature": float(temperature)}, _f, ensure_ascii=False)
            except Exception:
                pass
        with col2:
            try:
                from core.advisor import get_available_models
                model_names = [m["name"] for m in get_available_models()] or ["qwen/qwen3.5-9b"]
            except Exception:
                model_names = ["qwen/qwen3.5-9b"]
            selected_model = st.selectbox(
                "🤖 Модель", options=model_names,
                index=model_names.index(st.session_state.advisor_model)
                      if st.session_state.advisor_model in model_names else 0,
                key="advisor_model_select",
            )
            st.session_state.advisor_model = selected_model
            st.caption(f"Доступно моделей: {len(model_names)}")

            sources_only_mode = st.toggle(
                "🧪 Режим тестов чанков (без LLM)",
                value=st.session_state.sources_only_mode,
                key="sources_only_toggle",
            )
            st.session_state.sources_only_mode = sources_only_mode

            if st.button("🗑 Очистить кэш LLM", key="clear_cache_btn", use_container_width=True):
                from core.advisor import _llm_cache, save_llm_cache
                _llm_cache.clear()
                save_llm_cache()
                st.session_state.query_times = []
                st.success("✅ Кэш очищен")
                st.rerun()

        if st.session_state.query_times:
            st.divider()
            avg_time = sum(st.session_state.query_times) / len(st.session_state.query_times)
            c1, c2, c3 = st.columns(3)
            c1.metric("Запросов",     len(st.session_state.query_times))
            c2.metric("Среднее время", f"{avg_time:.1f} сек")
            c3.metric("Последний",    f"{st.session_state.query_times[-1]:.1f} сек")

    # ── Поле ввода ───────────────────────────────────────────────────────────
    query = st.text_area(
        "Ваш вопрос",
        height=100,
        placeholder="Например: Какие расходы на ремонт можно включать в тариф?",
        key="question_input",
        value=st.session_state.last_query,
    )

    if st.session_state.sources_only_mode:
        st.warning("🧪 **Режим тестов чанков активен:** LLM отключён, показываются только источники")

    # ── Кнопка поиска — стриминг ────────────────────────────────────────────
    if st.button("Найти ответ", type="primary", key="search_btn"):
        if query.strip():
            try:
                from core.advisor import (
                    search_faq, search_vector_db, stream_ai_answer,
                    strip_thinking_blocks, detect_section, set_sources_only_mode,
                )
                set_sources_only_mode(st.session_state.sources_only_mode)
                start_time = datetime.now()

                # 1. Проверяем FAQ
                faq_results = search_faq(query)
                if faq_results:
                    answer  = faq_results[0]["answer"]
                    sources = [{"snippet": faq_results[0]["question"],
                                "file": "FAQ", "page": "", "category": "FAQ"}]
                    st.success("✅ Ответ из базы частых вопросов")
                    st.markdown(f"### 📝 Ответ:\n{answer}")
                    from_faq = True
                else:
                    # 2. Векторный поиск
                    with st.spinner("🔍 Ищем в базе знаний..."):
                        _effective_top_k = st.session_state.get("_adv_top_k", top_k)
                        sources = search_vector_db(query, top_k=_effective_top_k)

                    if sources and not st.session_state.sources_only_mode:
                        st.success(f"✅ Ответ сгенерирован ИИ · модель: {st.session_state.advisor_model}")

                        import itertools
                        gen = stream_ai_answer(
                            query, sources,
                            st.session_state.advisor_model,
                            temperature,
                        )

                        # Показываем спиннер пока ждём первый токен.
                        # Если thinking mode активен — спиннер "держит" 35 сек пока
                        # модель думает; как только появляется первый токен ответа —
                        # спиннер гасится и начинается стриминг.
                        with st.spinner("🤔 Модель формирует ответ..."):
                            first_token = next(gen, None)

                        if first_token is not None:
                            raw_answer = st.write_stream(
                                itertools.chain([first_token], gen)
                            )
                        else:
                            raw_answer = ""

                        answer = strip_thinking_blocks(raw_answer)

                    elif st.session_state.sources_only_mode:
                        answer = "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключён."
                        st.info(answer)
                    else:
                        answer = "❌ Не найдено релевантных документов в базе знаний."
                        st.warning(answer)
                    from_faq = False

                # 3. Замер времени
                query_time = (datetime.now() - start_time).total_seconds()
                st.session_state.query_times.append(query_time)
                if len(st.session_state.query_times) > 10:
                    st.session_state.query_times = st.session_state.query_times[-10:]

                # 4. Сохраняем результат для источников и оценки
                st.session_state.last_result = {
                    "answer":     answer,
                    "sources":    sources,
                    "from_faq":   from_faq,
                    "from_cache": False,
                    "model":      st.session_state.advisor_model,
                }
                st.session_state.last_query       = query
                st.session_state.search_triggered = True
                # Флаг: ответ уже отрисован стримингом — не дублировать
                st.session_state._answer_streamed = True

            except Exception as e:
                st.error(f"❌ Ошибка: {type(e).__name__}: {str(e)}")
                st.session_state.last_result = {"error": str(e)}
        else:
            st.warning("⚠️ Введите вопрос")

    # ── Источники, оценка, перенаправление ───────────────────────────────────
    result         = st.session_state.last_result
    just_streamed  = st.session_state.pop("_answer_streamed", False) \
                     if "_answer_streamed" in st.session_state else False

    if result:
        if result.get("error"):
            st.error(f"🔧 Техническая ошибка: {result['error']}")
        else:
            answer  = result.get("answer", "")
            sources = result.get("sources", [])

            # Ответ показываем только если НЕ только что стримили
            if not just_streamed:
                if result.get("from_cache"):
                    st.info("⚡ Ответ из кэша")
                elif result.get("from_faq"):
                    st.success("✅ Ответ из базы частых вопросов")
                elif answer and not answer.startswith("❌"):
                    if st.session_state.sources_only_mode:
                        st.info("🧪 Режим тестов: LLM отключён")
                    else:
                        st.success(f"✅ Ответ сгенерирован ИИ (модель: {result.get('model', '')})")

                if answer and not st.session_state.sources_only_mode:
                    import re as _re, io as _io
                    table_pattern = r'\|.*\|\n\|[-:\s|]+\|\n(?:\|.*\|\n)*'
                    tables = _re.findall(table_pattern, answer, _re.MULTILINE)
                    if tables:
                        for i, table_md in enumerate(tables):
                            try:
                                df = pd.read_csv(_io.StringIO(table_md.replace('|', ',')),
                                                 header=0, index_col=0, skipinitialspace=True)
                                df.columns = [str(c).strip() for c in df.columns]
                                st.subheader(f"📊 Таблица {i+1}")
                                st.dataframe(df, use_container_width=True, hide_index=True)
                                answer = answer.replace(table_md, "")
                            except Exception:
                                st.code(table_md, language="markdown")
                    if answer.strip():
                        st.markdown(f"### 📝 Ответ:\n{answer.strip()}")
                elif st.session_state.sources_only_mode:
                    st.info("ℹ️ В режиме тестов LLM отключён.")

            # Источники — показываем всегда
            if sources:
                st.subheader(f"📚 Источники ({len(sources)}):")
                for i, src in enumerate(sources, 1):
                    label = f"📄 {i}. {src.get('file', '?')}"
                    if src.get('page'):     label += f" (стр. {src['page']})"
                    if src.get('category'): label += f" · {src['category']}"
                    with st.expander(label):
                        snippet = src.get('snippet', '')
                        st.caption(snippet[:600] + ("..." if len(snippet) > 600 else ""))

            # Перенаправление
            if result.get("redirect"):
                st.divider()
                st.info(f"💡 {result.get('redirect_reason', '')}")
                st.markdown(f"""
                <div class="redirect-box">
                    <b>👉 Перейдите в раздел «{result['redirect']}» в меню слева</b>
                </div>""", unsafe_allow_html=True)

            # Оценка
            if not st.session_state.sources_only_mode and answer and not answer.startswith("❌"):
                st.divider()
                st.subheader("📊 Оцените ответ")
                col1, col2, col3 = st.columns(3)
                query_for_fb = st.session_state.last_query
                with col1:
                    if st.button("👍", key="btn_good", use_container_width=True):
                        submit_feedback("user", "answer_rating", "Полезно",
                                        question=query_for_fb[:500], answer=answer[:1000], rating=3)
                        st.success("✅ Спасибо!")
                        st.session_state.last_result = None
                        st.session_state.search_triggered = False
                        st.rerun()
                with col2:
                    if st.button("😐", key="btn_neutral", use_container_width=True):
                        submit_feedback("user", "answer_rating", "Нормально",
                                        question=query_for_fb[:500], answer=answer[:1000], rating=2)
                        st.success("✅ Спасибо!")
                        st.session_state.last_result = None
                        st.session_state.search_triggered = False
                        st.rerun()
                with col3:
                    if st.button("👎", key="btn_bad", use_container_width=True):
                        submit_feedback("user", "answer_rating", "Не помогло",
                                        question=query_for_fb[:500], answer=answer[:1000], rating=1)
                        st.success("✅ Спасибо!")
                        st.session_state.last_result = None
                        st.session_state.search_triggered = False
                        st.rerun()

            # Новый вопрос
            st.divider()
            col1, col2 = st.columns([3, 1])
            with col2:
                if st.button("🔄 Новый", key="btn_new", use_container_width=True):
                    st.session_state.last_query       = ""
                    st.session_state.last_result      = None
                    st.session_state.search_triggered = False
                    st.rerun()

    elif not st.session_state.search_triggered:
        st.info("Введите вопрос и нажмите «Найти ответ»")

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
elif main_choice == "Админка":
    st.header("Панель администратора")

    if not is_admin_logged():
        st.warning("🔒 Требуется вход администратора")
        password = st.text_input("Пароль", type="password")
        if st.button("🔓 Войти"):
            if admin.check_admin(password):
                st.session_state.admin_logged_in = True
                st.success("✅ Вход выполнен!")
                st.rerun()
            else:
                st.error("❌ Неверный пароль")
    else:
        admin_subtab = st.radio(
            "Раздел админки",
            ["📈 Аналитика ИИ", "📚 Документы", "⚙️ Настройки чанкования", "🎯 Поиск и реранкинг", "📝 Промпты", "📝 Отзывы", "⚙️ Настройки"],
            horizontal=True,
        )

        if admin_subtab == "📈 Аналитика ИИ":
            col1, col2 = st.columns([4, 1])
            with col1:
                st.header("📊 Качество работы ИИ-советчика")
            with col2:
                if st.button("🔄 Обновить", key="refresh_stats"):
                    st.rerun()
            st.caption(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
            period = st.selectbox("Период", ["7 дней","30 дней","90 дней","Всё время"], key="period_select")
            days   = {"7 дней":7,"30 дней":30,"90 дней":90,"Всё время":365}[period]
            try:
                stats = get_live_answer_stats(days=days)
                if stats["total"] > 0:
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Всего оценок",   stats["total"])
                    col2.metric("Средний рейтинг", f"{stats['avg_rating']}/3.0")
                    col3.metric("👍 Полезно",      stats["rating_3"])
                    col4.metric("👎 Не помогло",   stats["rating_1"])
                    quality_pct = round((stats["rating_3"] / stats["total"]) * 100)
                    st.subheader("📈 Процент полезных ответов")
                    st.progress(quality_pct / 100)
                    st.caption(f"{quality_pct}% ответов оценены как 👍 Полезно (цель: 85%)")
                    st.subheader("📊 Распределение оценок")
                    rating_df = pd.DataFrame({
                        "Оценка": ["👍 Полезно","😐 Нормально","👎 Не помогло"],
                        "Количество": [stats["rating_3"],stats["rating_2"],stats["rating_1"]],
                    })
                    st.bar_chart(rating_df.set_index("Оценка"))
                    if stats["top_bad_questions"]:
                        st.subheader("❓ Топ вопросов для улучшения")
                        for i, item in enumerate(stats["top_bad_questions"], 1):
                            with st.expander(f"{i}. «{item['question']}...»"):
                                st.write(f"**Ответ ИИ:** {item['answer']}")
                                st.write(f"**Комментарий:** {item['comment']}")
                                st.write(f"**Дата:** {item['timestamp'][:10]}")
                else:
                    st.info("📭 Пока нет оценок.")
            except Exception as e:
                st.error(f"Ошибка загрузки статистики: {e}")

        elif admin_subtab == "📚 Документы":
            st.header("📚 База знаний — документы")
            SPHERES = ["🔥 Теплоснабжение","💧 Водоснабжение/водоотведение","🗑️ Обращение с ТКО","🔵 Газ","⚡ Электрика","📁 Иные сферы"]
            CATEGORY_FOLDERS = {"📜 Общие НПА":"npa","⚖️ Документы ФАС":"fas","🏛️ Судебная практика":"court","📋 Методички и разъяснения":"methodics"}
            SPHERES_FILE = os.path.join("config","doc_spheres.json")

            def load_spheres_map():
                if os.path.exists(SPHERES_FILE):
                    try:
                        with open(SPHERES_FILE,"r",encoding="utf-8") as f: return json.load(f)
                    except Exception: pass
                return {}
            def save_spheres_map(m):
                os.makedirs(os.path.dirname(SPHERES_FILE),exist_ok=True)
                with open(SPHERES_FILE,"w",encoding="utf-8") as f: json.dump(m,f,ensure_ascii=False,indent=2)

            spheres_map = load_spheres_map()

            st.subheader("📤 Загрузить документы")
            col_up1, col_up2 = st.columns([3,1])
            with col_up1:
                upload_category = st.selectbox("Категория для загрузки", list(CATEGORY_FOLDERS.keys()), key="upload_cat_select")
            with col_up2:
                upload_spheres = st.multiselect("Сферы", SPHERES, key="upload_spheres_select", placeholder="Выберите...")
            uploaded = st.file_uploader("Перетащите файлы или выберите с компьютера",
                                        type=["pdf","txt","docx","xlsx"], accept_multiple_files=True,
                                        key="doc_uploader", label_visibility="collapsed")
            if uploaded:
                dest_folder = CATEGORY_FOLDERS[upload_category]
                dest_path   = os.path.join("data","raw",dest_folder)
                os.makedirs(dest_path, exist_ok=True)
                if st.button(f"💾 Сохранить и индексировать ({len(uploaded)} файл(ов))", type="primary", key="save_upload_btn"):
                    progress = st.progress(0)
                    for i, uf in enumerate(uploaded):
                        file_path = os.path.join(dest_path, uf.name)
                        with open(file_path,"wb") as f: f.write(uf.getbuffer())
                        if upload_spheres:
                            spheres_map[uf.name] = upload_spheres
                            save_spheres_map(spheres_map)
                        try:
                            from core.indexer import index_file
                            index_file(file_path, dest_folder)
                        except Exception: pass
                        progress.progress((i+1)/len(uploaded))
                    st.success(f"✅ Загружено и проиндексировано: {len(uploaded)} файл(ов)")
                    try:
                        from core.advisor import invalidate_hybrid_retriever
                        invalidate_hybrid_retriever()
                    except Exception: pass
                    st.rerun()

            st.divider()
            st.subheader("📋 Список документов")
            fc1, fc2, fc3 = st.columns([2,2,3])
            with fc1: filter_cat    = st.selectbox("Категория", ["— Все —"]+list(CATEGORY_FOLDERS.keys()), key="filter_cat")
            with fc2: filter_sphere = st.selectbox("Сфера",     ["— Все —"]+SPHERES, key="filter_sphere")
            with fc3: filter_name   = st.text_input("🔍 Поиск по имени файла", placeholder="Введите часть названия...", key="filter_name")

            _chroma_index = {}
            try:
                import chromadb as _chromadb
                _vector_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),"data","vector_db")
                _chroma_client  = _chromadb.PersistentClient(path=_vector_db_path)
                _collection     = _chroma_client.get_collection(name="tariff_docs")
                _results        = _collection.get(include=["metadatas"])
                for meta in _results["metadatas"]:
                    fn = meta.get("filename","")
                    if not fn: continue
                    if fn not in _chroma_index:
                        _chroma_index[fn] = {"chunks":0,"indexed_at":meta.get("indexed_at","")[:10] if meta.get("indexed_at") else "—"}
                    _chroma_index[fn]["chunks"] += 1
            except Exception: pass

            all_files = []
            cats_to_show = {filter_cat: CATEGORY_FOLDERS[filter_cat]} if filter_cat != "— Все —" else CATEGORY_FOLDERS
            for cat_label, folder in cats_to_show.items():
                folder_path = os.path.join("data","raw",folder)
                if not os.path.exists(folder_path): continue
                for fname in sorted(os.listdir(folder_path)):
                    fpath = os.path.join(folder_path, fname)
                    if not os.path.isfile(fpath) or fname.endswith(".indexed") or fname.startswith("."): continue
                    ext = os.path.splitext(fname)[1].upper().lstrip(".") or "—"
                    chroma_info = _chroma_index.get(fname,{})
                    all_files.append({
                        "fname":fname,"fpath":fpath,"folder":folder,"cat_label":cat_label,
                        "ext":ext,"size_kb":os.path.getsize(fpath)/1024,
                        "indexed_at":chroma_info.get("indexed_at","—") if chroma_info.get("chunks",0)>0 else "—",
                        "chunks_count":chroma_info.get("chunks",0),"spheres":spheres_map.get(fname,[]),
                    })
            if filter_sphere != "— Все —": all_files = [f for f in all_files if filter_sphere in f["spheres"]]
            if filter_name.strip():         all_files = [f for f in all_files if filter_name.lower() in f["fname"].lower()]

            if not all_files:
                st.info("📭 Документов не найдено. Загрузите файлы выше.")
            else:
                st.caption(f"Найдено документов: **{len(all_files)}**")
                hc = st.columns([1,4,2,3,2,1,1,1,1])
                for col, label in zip(hc, ["Формат","Наименование","Категория","Сферы","Дата индексации","📥","🔄","📤","🗑️"]):
                    col.markdown(f"**{label}**")
                st.divider()
                EXT_ICONS = {"PDF":"📕","TXT":"📄","DOCX":"📘","XLSX":"📗"}
                for fi in all_files:
                    row = st.columns([1,4,2,3,2,1,1,1,1])
                    icon = EXT_ICONS.get(fi["ext"],"📄")
                    with row[0]: st.markdown(f"{icon} `{fi['ext']}`")
                    with row[1]:
                        st.markdown(f"**{fi['fname']}**")
                        st.caption(f"{fi['size_kb']:.1f} КБ")
                    with row[2]: st.caption(fi["cat_label"])
                    with row[3]:
                        new_spheres = st.multiselect("сферы", SPHERES, default=fi["spheres"],
                                                     key=f"spheres_{fi['fname']}_{fi['folder']}", label_visibility="collapsed")
                        if new_spheres != fi["spheres"]:
                            spheres_map[fi["fname"]] = new_spheres
                            save_spheres_map(spheres_map)
                    with row[4]:
                        live_chunks = st.session_state.get(f"chunks_{fi['fname']}", fi["chunks_count"])
                        if live_chunks > 0:
                            st.markdown(f"✅ {fi['indexed_at']}")
                            st.caption(f"{live_chunks} чанков")
                        else:
                            st.caption("⬜ не индексирован")
                    with row[5]:
                        with open(fi["fpath"],"rb") as f:
                            st.download_button("📥", data=f.read(), file_name=fi["fname"],
                                               key=f"dl_{fi['fname']}_{fi['folder']}", use_container_width=True)
                    with row[6]:
                        if st.button("🔄", key=f"idx_{fi['fname']}_{fi['folder']}", use_container_width=True, help="Переиндексировать"):
                            with st.spinner(f"Индексация {fi['fname']}..."):
                                try:
                                    from core.indexer import remove_file_from_index, index_file
                                    # Сначала удаляем старые чанки
                                    old_chunks = fi["chunks_count"]
                                    try: remove_file_from_index(fi["fname"])
                                    except Exception: pass
                                    res = index_file(fi["fpath"], fi["folder"])
                                    if res["status"] == "success":
                                        new_chunks = res.get("chunks", 0)
                                        # Обновляем счётчик в session_state без rerun
                                        st.session_state[f"chunks_{fi['fname']}"] = new_chunks
                                        try:
                                            from core.advisor import invalidate_hybrid_retriever
                                            invalidate_hybrid_retriever()
                                        except Exception: pass
                                        delta = new_chunks - old_chunks
                                        delta_str = f"+{delta}" if delta >= 0 else str(delta)
                                        st.toast(f"✅ {fi['fname']}: {new_chunks} чанков ({delta_str})", icon="📥")
                                    else:
                                        st.toast(f"❌ {res.get('message','Ошибка индексации')}", icon="🚨")
                                except Exception as e:
                                    st.toast(f"❌ {e}", icon="🚨")
                            st.rerun()
                    with row[7]:
                        if st.button("📤", key=f"rmidx_{fi['fname']}_{fi['folder']}", use_container_width=True):
                            st.session_state[f"_confirm_rmidx_{fi['fname']}"] = True
                    with row[8]:
                        if st.button("🗑️", key=f"del_{fi['fname']}_{fi['folder']}", use_container_width=True):
                            st.session_state[f"_confirm_del_{fi['fname']}"] = True

                    if st.session_state.get(f"_confirm_rmidx_{fi['fname']}"):
                        @st.dialog(f"📤 Удалить «{fi['fname']}» из индекса?")
                        def _confirm_rmidx(fname=fi["fname"]):
                            st.info("Файл останется в папке, чанки будут удалены.")
                            ca, cb = st.columns(2)
                            with ca:
                                if st.button("📤 Да", type="primary", use_container_width=True, key=f"conf_rmidx_{fname}"):
                                    removed = 0
                                    try:
                                        from core.indexer import remove_file_from_index
                                        removed = _chroma_index.get(fname, {}).get("chunks", 0)
                                        remove_file_from_index(fname)
                                    except Exception: pass
                                    try:
                                        from core.advisor import invalidate_hybrid_retriever
                                        invalidate_hybrid_retriever()
                                    except Exception: pass
                                    st.session_state.pop(f"_confirm_rmidx_{fname}", None)
                                    st.session_state[f"chunks_{fname}"] = 0
                                    st.toast(f"📤 {fname}: удалено {removed} чанков из индекса", icon="📤")
                                    st.rerun()
                            with cb:
                                if st.button("← Отмена", use_container_width=True, key=f"cancel_rmidx_{fname}"):
                                    st.session_state.pop(f"_confirm_rmidx_{fname}", None)
                                    st.rerun()
                        _confirm_rmidx()

                    if st.session_state.get(f"_confirm_del_{fi['fname']}"):
                        @st.dialog(f"🗑️ Удалить файл «{fi['fname']}»?")
                        def _confirm_delete(fpath=fi["fpath"], fname=fi["fname"]):
                            st.warning("Файл будет удалён с диска и из индекса.")
                            ca, cb = st.columns(2)
                            with ca:
                                if st.button("🗑️ Да", type="primary", use_container_width=True, key=f"conf_del_{fname}"):
                                    removed = 0
                                    try:
                                        from core.indexer import remove_file_from_index
                                        removed = _chroma_index.get(fname, {}).get("chunks", 0)
                                        remove_file_from_index(fname)
                                    except Exception: pass
                                    os.remove(fpath)
                                    spheres_map.pop(fname, None)
                                    save_spheres_map(spheres_map)
                                    try:
                                        from core.advisor import invalidate_hybrid_retriever
                                        invalidate_hybrid_retriever()
                                    except Exception: pass
                                    st.session_state.pop(f"_confirm_del_{fname}", None)
                                    st.toast(f"🗑️ {fname} удалён ({removed} чанков)", icon="🗑️")
                                    st.rerun()
                            with cb:
                                if st.button("← Отмена", use_container_width=True, key=f"cancel_del_{fname}"):
                                    st.session_state.pop(f"_confirm_del_{fname}", None)
                                    st.rerun()
                        _confirm_delete()
                    st.divider()

                st.divider()
                st.subheader("⚙️ Массовые операции")
                reindex_cat = st.selectbox("Категория для переиндексации", list(CATEGORY_FOLDERS.keys()), key="reindex_cat_select")
                if st.button("🚀 Переиндексировать категорию", type="primary", use_container_width=True, key="reindex_cat_btn"):
                    with st.spinner("⏳ Индексация..."):
                        try:
                            from core.indexer import index_category
                            res = index_category(CATEGORY_FOLDERS[reindex_cat])
                            if res["status"] == "success":
                                _fi_count = len(res.get("files", []))
                                _chunk_count = sum(
                                    r.get("result", {}).get("chunks", 0)
                                    for r in res.get("files", [])
                                    if isinstance(r.get("result"), dict)
                                )
                                st.session_state["_mass_reindex_msg"] = (
                                    f"✅ Раздел **{reindex_cat}** переиндексирован: "
                                    f"{_fi_count} файл(ов), {_chunk_count} чанков"
                                )
                                try:
                                    from core.advisor import invalidate_hybrid_retriever
                                    invalidate_hybrid_retriever()
                                except Exception: pass
                                st.rerun()
                            else: st.error(f"❌ {res.get('message','')}")
                        except Exception as e: st.error(f"❌ {e}")
                if st.session_state.get("_mass_reindex_msg"):
                    st.success(st.session_state["_mass_reindex_msg"])
                    del st.session_state["_mass_reindex_msg"]
                st.divider()
                if st.button("🗑️ Очистить весь индекс", type="secondary", use_container_width=True, key="clear_index_btn"):
                    st.session_state._confirm_clear_index = True
                if st.session_state.get("_confirm_clear_index"):
                    @st.dialog("🗑️ Очистить весь индекс?")
                    def _confirm_clear():
                        st.warning("Все чанки будут удалены. Файлы останутся на диске.")
                        ca, cb = st.columns(2)
                        with ca:
                            if st.button("🗑️ Да, очистить", type="primary", use_container_width=True, key="conf_clear_idx"):
                                try:
                                    from core.indexer import clear_index
                                    clear_index()
                                except Exception: pass
                                try:
                                    from core.advisor import invalidate_chroma_collection
                                    invalidate_chroma_collection()
                                except Exception: pass
                                st.session_state._confirm_clear_index = False
                                st.session_state["_mass_clear_msg"] = "🗑️ Весь индекс очищен. Файлы на диске сохранены."
                                st.rerun()
                        with cb:
                            if st.button("← Отмена", use_container_width=True, key="cancel_clear_idx"):
                                st.session_state._confirm_clear_index = False
                                st.rerun()
                    _confirm_clear()
                if st.session_state.get("_mass_clear_msg"):
                    st.success(st.session_state["_mass_clear_msg"])
                    del st.session_state["_mass_clear_msg"]

        elif admin_subtab == "⚙️ Настройки чанкования":
            st.header("⚙️ Настройки чанкования документов")
            config_dir  = os.path.join("config")
            os.makedirs(config_dir, exist_ok=True)
            config_file = os.path.join(config_dir,"chunking_patterns.json")
            if os.path.exists(config_file):
                with open(config_file,'r',encoding='utf-8') as f: config = json.load(f)
            else:
                config = {
                    "patterns":{"section":r"^(РАЗДЕЛ|ГЛАВА)\s+[IVX0-9]+","article":r"^(Статья|ст\.)\s+[0-9]+",
                                "paragraph":r"^(п\.|пункт)\s*[0-9.]+","subparagraph":r"^[0-9]+\.[0-9]+"},
                    "doc_types":{"фас":"fas_document","фз":"federal_law","приказ":"order","письмо":"letter","методич":"methodology"},
                    "metadata_patterns":{"doc_number":r"(\d+[А-Я]?-\d+[А-Я]?)","doc_date":r"(\d{2}\.\d{2}\.\d{4})","doc_year":r"(\d{4})"},
                    "chunking_settings":{"chunk_size":500,"chunk_overlap":50,"min_chunk_length":100},
                }
            tab4, tab5 = st.tabs(["⚙️ Параметры чанкования","🔍 Просмотр и тест чанков"])
            with tab4:
                st.subheader("Параметры чанкования")
                settings = config.get("chunking_settings",{})
                chunking_mode = st.radio("Режим чанкования",
                    options=["legal","structural","separator","fixed"],
                    format_func=lambda x:{
                        "legal":      "⚖️ По пунктам НПА (рекомендуется)",
                        "structural": "🧠 Умный (по структуре)",
                        "separator":  "✂️ По разделителю",
                        "fixed":      "📏 Фиксированная длина",
                    }.get(x, x),
                    index=["legal","structural","separator","fixed"].index(
                        settings.get("chunking_mode","legal")
                        if settings.get("chunking_mode","legal") in ["legal","structural","separator","fixed"]
                        else "legal"
                    ),
                    key="chunking_mode_radio")
                st.divider()
                # Инициализируем все переменные из settings ДО if/elif —
                # иначе NameError если режим не выбирает свой виджет
                separator     = settings.get("separator", "&&")
                fixed_length  = settings.get("fixed_chunk_length", 1000)
                min_chunk     = settings.get("min_chunk_length", 80)
                max_chunk     = settings.get("max_chunk_length", 1500)
                chunk_overlap = settings.get("chunk_overlap", 0)
                if chunking_mode == "legal":
                    st.caption("⚖️ Один чанк = один пункт/статья/подпункт НПА. Максимальная точность цитирования.")
                    col1, col2 = st.columns(2)
                    with col1:
                        max_chunk = st.slider("Макс. длина чанка (симв.)", 500, 8000, max_chunk,
                            key="max_chunk_legal",
                            help="Если пункт длиннее — режется по предложениям с сохранением заголовка пункта")
                    with col2:
                        min_chunk = st.slider("Мин. длина блока (симв.)", 10, 300, min_chunk,
                            key="min_chunk_legal",
                            help="Блоки короче этого значения объединяются со следующим")
                elif chunking_mode == "structural":
                    col1,col2 = st.columns(2)
                    with col1:
                        min_chunk = st.slider("Мин. длина чанка (симв.)", 10, 500, min_chunk, key="min_chunk_s",
                            help="Чанки короче этого значения отфильтровываются как мусор (заголовки, пустые строки)")
                    with col2:
                        max_chunk = st.slider("Макс. длина чанка (симв.)", 200, 5000, max_chunk, key="max_chunk_s",
                            help="Рекомендуется 800–1000 для нормативных документов. Один пункт НПА — ~600–900 символов")
                elif chunking_mode == "separator":
                    separator = st.text_input("Маркер конца чанка", value=separator, key="chunk_separator_input")
                    col1,col2 = st.columns(2)
                    with col1: min_chunk = st.slider("Мин. длина чанка",10,500,min_chunk,key="min_chunk_sep")
                    with col2: max_chunk = st.slider("Макс. длина чанка",200,5000,max_chunk,key="max_chunk_sep")
                elif chunking_mode == "fixed":
                    fixed_length = st.slider("Длина чанка (символов)",100,5000,fixed_length,step=50,key="fixed_chunk_length_slider")
                st.divider()
                chunk_overlap = st.slider("Перекрытие (символов)", 0, 500, chunk_overlap, step=10, key="chunk_overlap_slider",
                    help="Сколько символов из конца предыдущего чанка добавляется в начало следующего. Рекомендуется 100–200")
                st.divider()
                st.subheader("🔒 Границы разрезания")
                no_cut_word = st.toggle(
                    "Не резать в середине слова",
                    value=settings.get("no_cut_word", True), key="no_cut_word",
                    help="Чанк всегда заканчивается на границе слова. Если лимит достигнут внутри слова — откатываемся до предыдущего пробела."
                )
                no_cut_sentence = st.toggle(
                    "Не резать в середине предложения",
                    value=settings.get("no_cut_sentence", True), key="no_cut_sentence",
                    help="Чанк заканчивается на знаке препинания (. ! ?). Рекомендуется для нормативных текстов — сохраняет юридически значимые формулировки целиком."
                )
                no_cut_paragraph = st.toggle(
                    "Не резать в середине абзаца",
                    value=settings.get("no_cut_paragraph", False), key="no_cut_paragraph",
                    help="Чанк заканчивается только на пустой строке (границе абзаца). Может давать чанки разного размера, зато каждый абзац НПА остаётся нетронутым."
                )
                if no_cut_paragraph:
                    st.info("ℹ️ При включённом режиме 'не резать абзац' параметр макс. длины становится мягким ограничением — абзац целиком важнее размера.")
                st.divider()
                if st.button("💾 Сохранить параметры", key="save_settings", use_container_width=True, type="primary"):
                    config["chunking_settings"] = {
                        "chunking_mode":      chunking_mode,
                        "separator":          separator,
                        "fixed_chunk_length": fixed_length,
                        "min_chunk_length":   min_chunk,
                        "max_chunk_length":   max_chunk,
                        "chunk_overlap":      chunk_overlap,
                        "no_cut_word":        no_cut_word,
                        "no_cut_sentence":    no_cut_sentence,
                        "no_cut_paragraph":   no_cut_paragraph,
                    }
                    with open(config_file,'w',encoding='utf-8') as f: json.dump(config,f,ensure_ascii=False,indent=2)
                    st.toast("✅ Параметры сохранены — не забудьте переиндексировать документы", icon="💾")
                    st.session_state["_settings_saved"] = True
                    st.rerun()
                if st.session_state.get("_settings_saved"):
                    st.success("✅ Параметры чанкования сохранены. Для применения — переиндексируйте документы в разделе **Документы → Массовые операции**.")
                    st.session_state["_settings_saved"] = False
                if st.button("🔄 Сбросить к умолчаниям", key="reset_config", use_container_width=True):
                    if os.path.exists(config_file): os.remove(config_file)
                    st.toast("✅ Конфигурация сброшена", icon="🔄")
                    st.session_state["_settings_reset"] = True
                    st.rerun()
                if st.session_state.get("_settings_reset"):
                    st.info("🔄 Настройки сброшены к умолчаниям.")
                    st.session_state["_settings_reset"] = False
            with tab5:
                st.subheader("🔍 Просмотр чанков")
                try:
                    import chromadb as _cdb
                    _vdb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vector_db")
                    _cli = _cdb.PersistentClient(path=_vdb_path)
                    try:
                        _col = _cli.get_collection(name="tariff_docs")
                        _all = _col.get(include=["documents", "metadatas"])
                        _raw_docs  = _all.get("documents", [])
                        _raw_metas = _all.get("metadatas", [])
                        _raw_ids   = _all.get("ids", [])
                    except Exception:
                        _raw_docs = []
                        _raw_metas = []
                        _raw_ids = []

                    if not _raw_docs:
                        st.warning("⚠️ Векторная база пуста — проиндексируйте документы")
                    else:
                        # ── Группировка по файлам ────────────────────────────
                        _fdict: dict = {}
                        for _did, _doc, _meta in zip(_raw_ids, _raw_docs, _raw_metas):
                            if _meta is None:
                                _meta = {}
                            _fn = _meta.get("filename", "Неизвестно")
                            if _fn not in _fdict:
                                _fdict[_fn] = {
                                    "doc_type":   _meta.get("doc_type",   "—"),
                                    "doc_number": _meta.get("doc_number", "—"),
                                    "doc_date":   _meta.get("doc_date",   "—"),
                                    "chunks": [],
                                }
                            _fdict[_fn]["chunks"].append({
                                "id":       _did,
                                "content":  _doc,          # полный текст, без обрезки
                                "metadata": _meta,
                            })
                        # Сортируем чанки внутри файла по chunk_index
                        for _fn in _fdict:
                            _fdict[_fn]["chunks"].sort(
                                key=lambda c: int(c["metadata"].get("chunk_index", 0))
                            )

                        # ── Статистика ───────────────────────────────────────
                        _sc1, _sc2 = st.columns(2)
                        _sc1.metric("Всего чанков", len(_raw_docs))
                        _sc2.metric("Файлов", len(_fdict))
                        st.divider()

                        # ── Выбор файла ──────────────────────────────────────
                        _fnames = sorted(_fdict.keys())
                        _sel_file = st.selectbox(
                            "📄 Документ",
                            _fnames,
                            format_func=lambda x: f"{x}  ({len(_fdict[x]['chunks'])} чанков)",
                            key="cv_file_sel",
                        )
                        _fi = _fdict[_sel_file]
                        _chunks = _fi["chunks"]
                        _total  = len(_chunks)

                        _mc = st.columns(4)
                        _mc[0].metric("Чанков", _total)
                        _mc[1].caption(f"**Тип:** {_fi['doc_type']}")
                        _mc[2].caption(f"**Номер:** {_fi['doc_number']}")
                        _mc[3].caption(f"**Дата:** {_fi['doc_date']}")
                        st.divider()

                        # ── Выбор чанка ─────────────────────────────────────
                        # cv_chunk_idx — единственный источник истины.
                        # selectbox рендерится БЕЗ key, чтобы не было конфликта
                        # между его внутренним состоянием и нашей переменной.
                        if "cv_chunk_idx" not in st.session_state:
                            st.session_state["cv_chunk_idx"] = 0
                        if st.session_state.get("cv_last_file") != _sel_file:
                            st.session_state["cv_chunk_idx"] = 0
                            st.session_state["cv_last_file"] = _sel_file
                        _cidx = min(st.session_state["cv_chunk_idx"], _total - 1)

                        def _clabel(i):
                            _c = _chunks[i]
                            _ci = _c["metadata"].get("chunk_index", i)
                            _prev = _c["content"][:80].replace("\n", " ")
                            return f"#{_ci}  ·  {_prev}…"

                        # selectbox — без key, index задаётся из cv_chunk_idx
                        _sel_i = st.selectbox(
                            "🔢 Чанк",
                            options=list(range(_total)),
                            index=_cidx,
                            format_func=_clabel,
                        )
                        # Если пользователь выбрал вручную — синхронизируем и перезапускаем
                        if _sel_i != _cidx:
                            st.session_state["cv_chunk_idx"] = _sel_i
                            st.rerun()

                        # Кнопки навигации
                        _nb1, _nb2, _nb3 = st.columns([1, 6, 1])
                        with _nb1:
                            if st.button("◀", disabled=(_cidx == 0), key="cv_prev", use_container_width=True):
                                st.session_state["cv_chunk_idx"] = _cidx - 1
                                st.rerun()
                        with _nb2:
                            st.caption(f"Чанк {_cidx + 1} из {_total}")
                        with _nb3:
                            if st.button("▶", disabled=(_cidx >= _total - 1), key="cv_next", use_container_width=True):
                                st.session_state["cv_chunk_idx"] = _cidx + 1
                                st.rerun()

                        # ── Полное содержимое ────────────────────────────────
                        _chunk   = _chunks[_cidx]
                        _content = _chunk["content"]
                        _meta    = _chunk["metadata"]

                        st.text_area(
                            f"Содержимое  ·  {len(_content)} символов",
                            value=_content,
                            height=max(150, min(520, len(_content) // 2)),
                            disabled=True,
                        )

                        # ── Метаданные чанка ─────────────────────────────────
                        with st.expander("🏷️ Метаданные чанка", expanded=False):
                            _mf = st.columns(2)
                            _mfields = [
                                ("chunk_index", _meta.get("chunk_index", "—")),
                                ("struct_type",  _meta.get("struct_type",  "—")),
                                ("struct_text",  _meta.get("struct_text",  "—")),
                                ("article",      _meta.get("article",      "—")),
                                ("paragraph",    _meta.get("paragraph",    "—")),
                                ("category",     _meta.get("category",     "—")),
                                ("doc_type",     _meta.get("doc_type",     "—")),
                                ("id",           _chunk["id"]),
                            ]
                            for _j, (_k, _v) in enumerate(_mfields):
                                _mf[_j % 2].caption(f"**{_k}:** {_v}")

                        # ── Тест-запрос (вспомогательный) ───────────────────
                        st.divider()
                        st.subheader("🧪 Тест-запрос к базе")
                        _tq_c1, _tq_c2 = st.columns([4, 1])
                        with _tq_c1:
                            test_query = st.text_input("Запрос", placeholder="расходы на ремонт основных средств", key="test_query_input")
                        with _tq_c2:
                            try:
                                _sr_file_path = os.path.join('config', 'search_settings.json')
                                _sr_saved = json.load(open(_sr_file_path, encoding='utf-8')) if os.path.exists(_sr_file_path) else {}
                                _test_default_k = int(_sr_saved.get('candidates_per_var', 25))
                            except Exception:
                                _test_default_k = 25
                            test_top_k = st.number_input('Топ-K', min_value=1, max_value=200,
                                value=_test_default_k, key='test_top_k',
                                help='По умолчанию = кандидатов на вариант из настроек поиска')
                        if st.button("🔎 Найти чанки", key="test_search_btn", type="primary"):
                            if test_query.strip():
                                with st.spinner("Ищем..."):
                                    try:
                                        from core.advisor import search_vector_db as _svdb
                                        test_sources = _svdb(test_query, top_k=int(test_top_k))
                                        if test_sources:
                                            st.success(f"✅ Найдено {len(test_sources)} чанков")
                                            for _ti, _src in enumerate(test_sources, 1):
                                                _score = max(0, round((1 - _src.get("distance", 1)) * 100, 1))
                                                _sc = "🟢" if _score >= 70 else "🟡" if _score >= 40 else "🔴"
                                                with st.expander(f"#{_ti} · {_src['file']} · {_sc} {_score}%", expanded=(_ti == 1)):
                                                    st.text_area("", _src["snippet"], height=200, disabled=True, key=f"tsr_{_ti}")
                                                    st.caption(f"Дистанция: {_src['distance']} · Чанк: {_src.get('chunk_index', '')}")
                                        else:
                                            st.warning("🔍 Ничего не найдено.")
                                    except Exception as _te:
                                        st.error(f"❌ {type(_te).__name__}: {_te}")
                            else:
                                st.warning("⚠️ Введите запрос")

                except Exception as e:
                    st.error(f"❌ {type(e).__name__}: {e}")

        elif admin_subtab == "🎯 Поиск и реранкинг":
            st.header("🎯 Настройки поиска и реранкинга")
            st.info("Изменения применяются сразу к следующему запросу. Перезапуск не нужен.")

            _sr_file = os.path.join("config", "search_settings.json")
            _sr_defaults = {
                "bm25_weight":        1.5,
                "candidates_per_var": 15,
                "context_max_chars":  8000,
                "reranker_enabled":   True,
            }
            if os.path.exists(_sr_file):
                try:
                    with open(_sr_file, "r", encoding="utf-8") as f:
                        _sr_cur = {**_sr_defaults, **json.load(f)}
                except Exception:
                    _sr_cur = dict(_sr_defaults)
            else:
                _sr_cur = dict(_sr_defaults)

            st.subheader("⚖️ Гибридный поиск (BM25 + вектор)")
            _bm25_w = st.slider(
                "Вес BM25 относительно векторного поиска",
                min_value=0.5, max_value=3.0, step=0.1,
                value=float(_sr_cur["bm25_weight"]),
                help="1.0 = равный вес. >1 = BM25 важнее (точное вхождение слов). <1 = вектор важнее (семантика).",
                key="sr_bm25_weight",
            )
            st.caption(f"{'🔤 Точные слова важнее' if _bm25_w > 1.0 else '🧠 Семантика важнее' if _bm25_w < 1.0 else '⚖️ Равный вес'}")

            _cands = st.slider(
                "Кандидатов на вариант запроса",
                min_value=5, max_value=40, step=5,
                value=int(_sr_cur["candidates_per_var"]),
                help="Сколько чанков отбирается от каждого варианта запроса перед реранкингом. Больше = точнее, но медленнее.",
                key="sr_candidates",
            )

            st.divider()
            st.subheader("🔁 Реранкинг (CrossEncoder)")
            _reranker_on = st.toggle(
                "Включить реранкинг",
                value=bool(_sr_cur["reranker_enabled"]),
                help="CrossEncoder переставляет кандидатов по реальной релевантности запросу. Отключите если медленно.",
                key="sr_reranker_on",
            )
            if _reranker_on:
                try:
                    from core.advisor import get_reranker_status as _grs, get_reranker as _gr, invalidate_reranker as _ir
                    _status = _grs()
                    if _status["loaded"]:
                        st.success(f"✅ Загружена модель: `{_status['model_name']}`")
                    else:
                        with st.spinner("⏳ Загружаем реранкер..."):
                            _rm = _gr()
                        if _rm:
                            st.success(f"✅ Загружена модель: `{_rm.model_name}`")
                        else:
                            _last_err = _status.get("last_error", "")
                            st.warning("⚠️ Реранкер не загружен")
                            if _last_err:
                                st.code(_last_err, language="text")
                            if st.button("🔄 Попробовать снова", key="sr_reload_reranker"):
                                _ir()
                                st.rerun()
                except Exception as _e:
                    st.warning(f"⚠️ Ошибка импорта: {_e}")
            else:
                st.caption("Результаты ранжируются только по RRF-score (BM25 + вектор).")

            st.divider()
            st.subheader("🤖 Модель реранкера")
            try:
                from core.advisor import AVAILABLE_RERANKER_MODELS as _ARM, get_reranker_status as _grs2, invalidate_reranker as _ir3, get_reranker as _gr2
                _model_ids    = [m["id"]    for m in _ARM]
                _model_labels = [m["label"] for m in _ARM]
                _model_descs  = {m["id"]: m["desc"] for m in _ARM}
                _cur_model    = _sr_cur.get("reranker_model", _model_ids[0])
                _cur_idx      = _model_ids.index(_cur_model) if _cur_model in _model_ids else 0
                _sel_label    = st.radio(
                    "Выберите модель реранкера",
                    _model_labels,
                    index=_cur_idx,
                    key="sr_reranker_model",
                )
                _sel_model_id = _model_ids[_model_labels.index(_sel_label)]
                st.caption(f"ℹ️ {_model_descs.get(_sel_model_id, '')}")
                _loaded_status = _grs2()
                _currently_loaded = _loaded_status.get("model_name", "")
                if _loaded_status["loaded"] and _currently_loaded != _sel_model_id:
                    if st.button("⚡ Переключить на выбранную модель", key="sr_switch_model", type="primary", use_container_width=True):
                        _ir3()
                        _new_sr_model = {**_sr_cur, "reranker_model": _sel_model_id}
                        os.makedirs("config", exist_ok=True)
                        with open(_sr_file, "w", encoding="utf-8") as f:
                            json.dump(_new_sr_model, f, ensure_ascii=False, indent=2)
                        st.session_state["_search_settings"] = _new_sr_model
                        with st.spinner(f"⏳ Загружаем {_sel_model_id}..."):
                            _new_rm = _gr2()
                        if _new_rm:
                            st.session_state["_model_switched"] = _new_rm.model_name
                        else:
                            st.session_state["_model_switch_failed"] = _sel_model_id
                        st.rerun()
                if st.session_state.get("_model_switched"):
                    st.success(f"✅ Модель переключена: `{st.session_state['_model_switched']}`")
                    del st.session_state["_model_switched"]
                if st.session_state.get("_model_switch_failed"):
                    st.error(f"❌ Не удалось загрузить `{st.session_state['_model_switch_failed']}`")
                    del st.session_state["_model_switch_failed"]
            except Exception as _me:
                st.warning(f"Не удалось загрузить список моделей: {_me}")
                _sel_model_id = _sr_cur.get("reranker_model", "DiTy/cross-encoder-russian-msmarco")

            st.divider()
            st.subheader("🔎 Тест вариантов запроса")
            st.caption(
                "Показывает варианты запроса, **все кандидаты до реранкинга** "
                "(пул = «Кандидатов на вариант» × кол-во вариантов – дубли) "
                "и финальные результаты после реранкинга."
            )
            _dbg_c1, _dbg_c2 = st.columns([4, 1])
            with _dbg_c1:
                _test_q = st.text_input(
                    "Введите запрос для проверки",
                    placeholder="например: ДМС, расходы на ремонт",
                    key="sr_query_expand_test",
                )
            with _dbg_c2:
                _dbg_topk = st.number_input(
                    "Финальный топ-K",
                    min_value=1, max_value=20, value=5,
                    help="Сколько результатов вернуть ПОСЛЕ реранкинга",
                    key="sr_debug_topk",
                )

            if st.button("🔬 Запустить тест кандидатов", key="sr_debug_btn", type="primary"):
                if _test_q.strip():
                    with st.spinner("⏳ Выполняем поиск..."):
                        try:
                            from core.advisor import debug_search_candidates as _dsc
                            _dbg = _dsc(_test_q.strip(), top_k=int(_dbg_topk))
                        except Exception as _dbe:
                            st.error(f"❌ {type(_dbe).__name__}: {_dbe}")
                            _dbg = None

                    if _dbg:
                        if _dbg.get("error"):
                            st.error(f"❌ {_dbg['error']}")
                        else:
                            # ── варианты запроса
                            st.markdown("##### 🔀 Варианты запроса")
                            for _vi, _vq in enumerate(_dbg["query_variants"], 1):
                                _vlabel = "🎯 оригинал" if _vi == 1 else f"🔁 синоним {_vi-1}"
                                st.code(f"{_vlabel}: {_vq}", language=None)

                            # ── пул ДО реранкинга
                            _pre = _dbg["pre_rerank"]
                            _cpv = _cands
                            _nv  = len(_dbg["query_variants"])
                            st.markdown(
                                f"##### 📥 Пул до реранкинга: **{len(_pre)}** уникальных кандидатов"
                                f"  <span style='color:grey;font-size:0.85em'>"
                                f"(настройка {_cpv} × {_nv} вар. → дедупликация)</span>",
                                unsafe_allow_html=True,
                            )
                            for _pi, _pc in enumerate(_pre, 1):
                                _pm  = _pc.get("meta") or {}
                                _inv = "🔵 vec" if _pc.get("in_vector") else ""
                                _inb = "🟤 bm25" if _pc.get("in_bm25") else ""
                                _rrf = f"RRF={_pc.get('score', 0):.5f}"
                                with st.expander(
                                    f"#{_pi} · {_pm.get('filename','?')} · "
                                    f"чанк {_pm.get('chunk_index','')} · {_rrf} {_inv} {_inb}",
                                    expanded=False,
                                ):
                                    st.text_area("", _pc.get("doc", "")[:600],
                                                 height=120, disabled=True,
                                                 key=f"dbg_pre_{_pi}")

                            # ── результаты ПОСЛЕ реранкинга
                            _post  = _dbg["post_rerank"]
                            _rused = "✅ CrossEncoder" if _dbg["reranker_used"] else "⚠️ реранкинг отключён"
                            st.markdown(
                                f"##### 🏆 После реранкинга: **{len(_post)}** результатов "
                                f"<span style='color:grey;font-size:0.85em'>({_rused})</span>",
                                unsafe_allow_html=True,
                            )
                            for _qi, _qc in enumerate(_post, 1):
                                _qm     = _qc.get("meta") or {}
                                _rrf2   = f"RRF={_qc.get('score', 0):.5f}"
                                _rscore = (
                                    f" | rerank={_qc.get('rerank_score', 0):.3f}"
                                    if _dbg["reranker_used"] else ""
                                )
                                with st.expander(
                                    f"#{_qi} · {_qm.get('filename','?')} · "
                                    f"чанк {_qm.get('chunk_index','')} · {_rrf2}{_rscore}",
                                    expanded=(_qi == 1),
                                ):
                                    st.text_area("", _qc.get("doc", "")[:600],
                                                 height=150, disabled=True,
                                                 key=f"dbg_post_{_qi}")

                            st.caption(f"⏱ Время: {_dbg['elapsed']} сек")
                else:
                    st.warning("⚠️ Введите запрос")

            st.divider()
            st.subheader("📄 Контекст для LLM")
            _ctx = st.slider(
                "Максимум символов контекста",
                min_value=2000, max_value=20000, step=1000,
                value=int(_sr_cur["context_max_chars"]),
                help="Сколько символов из найденных чанков передаётся LLM. Больше = полнее ответ, но медленнее генерация.",
                key="sr_context",
            )
            _tok_est = _ctx // 3
            st.caption(f"≈ {_tok_est} токенов контекста · при radius=1 и чанке 1750 симв. один источник = ~5250 символов")

            st.divider()
            _sc1, _sc2 = st.columns(2)
            with _sc1:
                if st.button("💾 Сохранить настройки поиска", type="primary", use_container_width=True, key="sr_save"):
                    _new_sr = {
                        "bm25_weight":        _bm25_w,
                        "candidates_per_var": _cands,
                        "context_max_chars":  _ctx,
                        "reranker_enabled":   _reranker_on,
                        "reranker_model":     _sel_model_id,
                    }
                    os.makedirs("config", exist_ok=True)
                    with open(_sr_file, "w", encoding="utf-8") as f:
                        json.dump(_new_sr, f, ensure_ascii=False, indent=2)
                    st.session_state["_search_settings"] = _new_sr
                    st.session_state["_sr_saved"] = True
                    st.rerun()
            with _sc2:
                if st.button("🔄 Сбросить к умолчаниям", use_container_width=True, key="sr_reset"):
                    if os.path.exists(_sr_file):
                        os.remove(_sr_file)
                    st.session_state.pop("_search_settings", None)
                    st.session_state["_sr_reset"] = True
                    st.rerun()
            if st.session_state.get("_sr_saved"):
                st.success("✅ Настройки поиска сохранены — применятся к следующему запросу.")
                del st.session_state["_sr_saved"]
            if st.session_state.get("_sr_reset"):
                st.info("🔄 Настройки сброшены к умолчаниям.")
                del st.session_state["_sr_reset"]

        elif admin_subtab == "📝 Промпты":
            st.header("📝 Управление промптами")
            st.info("💡 Изменения применяются сразу. Кэш LLM сбрасывается при сохранении.")
            PROMPTS_FILE_ADMIN = os.path.join("config","prompts.json")
            DEFAULT_PROMPTS_ADMIN = {
                "advisor_system": (
                    "Ты — эксперт по тарифному регулированию в РФ.\n"
                    "Отвечай ТОЛЬКО на русском языке, кратко, структурно и по существу.\n"
                    "ЗАПРЕЩЕНО писать 'Thinking Process', рассуждения или объяснения шагов.\n"
                    "Отвечай сразу итоговым ответом: списком, таблицей или чётким утверждением.\n"
                    "Основывайся на предоставленном контексте и законодательстве РФ.\n"
                    "Если информации в базе знаний недостаточно — честно скажи об этом.\n"
                    "Если в ответе есть сравнение данных, ставки или параметры — "
                    "оформи в виде Markdown-таблицы.\n"
                    "Пример:\n| Параметр | Значение | Ед. изм. |\n|---|---|---|\n| Тариф | 100.50 | руб./Гкал |"
                ),
                "advisor_user": "Вопрос пользователя: {query}\n\nКонтекст из документов:\n{context}\n\nОтвет:",
                # ── Анализатор заявок: суммаризатор ─────────────────────────
                "claim_map_system": (
                    "Ты эксперт по тарифному регулированию РФ. "
                    "Извлекаешь структурированные данные из фрагментов тарифных заявок. "
                    "Отвечаешь строго на русском языке, только по делу."
                ),
                "claim_map_user": (
                    "Это часть {i} из {total} тарифной заявки.\n"
                    "Извлеки ТОЛЬКО (если есть): статьи затрат (название, сумма тыс. руб., период), "
                    "приложенные документы, ссылки на НПА, организацию и период.\n"
                    "Формат: маркированный список. Без вступлений.\n\nФРАГМЕНТ:\n{chunk}"
                ),
                "claim_reduce_system": (
                    "Ты эксперт по тарифному регулированию РФ. "
                    "Составляешь структурированное резюме тарифной заявки. "
                    "Все цифры точно из источника. Отвечаешь на русском."
                ),
                "claim_reduce_user": (
                    "Собери единое резюме тарифной заявки (~{target_words} слов).\n\n"
                    "Разделы: ## Организация и период ## Статьи затрат "
                    "## Приложенные документы ## НПА ## Пробелы в обосновании\n\n"
                    "Устрани дублирование. Все цифры точно.\n\nДАННЫЕ:\n{combined}"
                ),
                # ── Анализатор заявок: риски ─────────────────────────────────
                "claim_risks_system": (
                    "Ты эксперт-аудитор по тарифному регулированию РФ. "
                    "Анализируешь тарифные заявки на риск отклонения регулятором. "
                    "Отвечаешь структурированно на русском языке. "
                    "Используй эмодзи 🔴 (высокий риск), 🟡 (средний), 🟢 (низкий)."
                ),
                "claim_risks_user": (
                    "Проанализируй тарифную заявку и составь отчёт о рисках.\n\n"
                    "## 1. Оценка комплектности документов\n"
                    "Перечисли какие документы упоминаются. "
                    "Укажи какие отсутствуют исходя из статей затрат.\n\n"
                    "## 2. Риски по статьям затрат\n"
                    "Для каждой значимой статьи: 🔴/🟡/🟢 Статья: сумма.\n"
                    "Основание риска и рекомендация.\n\n"
                    "## 3. Итоговая оценка\n"
                    "Общий уровень и топ-3 рекомендации.\n\n"
                    "ДАННЫЕ РАСЧЁТНОГО ФАЙЛА:\n{calc_context}\n\n"
                    "РЕЗЮМЕ ЗАЯВКИ:\n{summary}"
                ),
            }
            if os.path.exists(PROMPTS_FILE_ADMIN):
                try:
                    with open(PROMPTS_FILE_ADMIN,'r',encoding='utf-8') as f:
                        current_prompts = {**DEFAULT_PROMPTS_ADMIN, **json.load(f)}
                except Exception:
                    current_prompts = dict(DEFAULT_PROMPTS_ADMIN)
            else:
                current_prompts = dict(DEFAULT_PROMPTS_ADMIN)

            st.subheader("🤝 Советчик")
            with st.expander("ℹ️ Переменные"):
                st.markdown("**Пользовательский промпт:** `{query}` — вопрос, `{context}` — чанки из RAG")
            col1,col2 = st.columns(2)
            with col1: st.caption("Загружен из: " + ("📁 prompts.json" if os.path.exists(PROMPTS_FILE_ADMIN) else "⚙️ дефолт"))
            with col2:
                is_mod = (current_prompts.get("advisor_system") != DEFAULT_PROMPTS_ADMIN["advisor_system"] or
                          current_prompts.get("advisor_user")   != DEFAULT_PROMPTS_ADMIN["advisor_user"])
                if is_mod: st.warning("✏️ Промпты изменены")
                else:      st.success("✅ Дефолтные промпты")
            st.divider()
            new_system = st.text_area("🧠 Системный промпт", value=current_prompts.get("advisor_system",DEFAULT_PROMPTS_ADMIN["advisor_system"]), height=280, key="prompt_advisor_system")
            st.divider()
            new_user   = st.text_area("💬 Пользовательский промпт", value=current_prompts.get("advisor_user",DEFAULT_PROMPTS_ADMIN["advisor_user"]), height=120, key="prompt_advisor_user")
            if "{query}" not in new_user or "{context}" not in new_user:
                st.error("⚠️ Промпт должен содержать {query} и {context}")
            else:
                st.caption("✅ Переменные присутствуют")
            st.divider()
            col1,col2,col3 = st.columns([2,2,1])
            with col1:
                if st.button("💾 Сохранить промпты", type="primary", use_container_width=True, key="save_prompts_btn"):
                    if "{query}" in new_user and "{context}" in new_user:
                        os.makedirs(os.path.dirname(PROMPTS_FILE_ADMIN),exist_ok=True)
                        with open(PROMPTS_FILE_ADMIN,'w',encoding='utf-8') as f:
                            json.dump({**current_prompts,"advisor_system":new_system,"advisor_user":new_user,
                                       "updated_at":datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
                        try:
                            from core.advisor import _llm_cache, save_llm_cache
                            _llm_cache.clear(); save_llm_cache()
                            st.success("✅ Промпты сохранены. Кэш сброшен.")
                        except Exception:
                            st.success("✅ Промпты сохранены.")
                        st.rerun()
                    else:
                        st.error("❌ Исправьте ошибки")
            with col2:
                if st.button("🔄 Сбросить к дефолтным", use_container_width=True, key="reset_prompts_btn"):
                    st.session_state._confirm_reset_prompts = True
                @st.dialog("⚠️ Сброс промптов")
                def confirm_reset_prompts_dialog():
                    st.warning("Промпты вернутся к дефолтным значениям.")
                    ca,cb = st.columns(2)
                    with ca:
                        if st.button("🗑️ Да, сбросить", type="primary", use_container_width=True, key="dialog_confirm_reset"):
                            if os.path.exists(PROMPTS_FILE_ADMIN):
                                try:
                                    with open(PROMPTS_FILE_ADMIN,'r',encoding='utf-8') as f: saved = json.load(f)
                                    saved.pop("advisor_system",None); saved.pop("advisor_user",None)
                                    with open(PROMPTS_FILE_ADMIN,'w',encoding='utf-8') as f: json.dump(saved,f,ensure_ascii=False,indent=2)
                                except Exception: pass
                            st.session_state._confirm_reset_prompts = False; st.rerun()
                    with cb:
                        if st.button("← Отмена", use_container_width=True, key="dialog_cancel_reset"):
                            st.session_state._confirm_reset_prompts = False; st.rerun()
                if st.session_state.get("_confirm_reset_prompts"):
                    confirm_reset_prompts_dialog()
            with col3:
                prompts_json = json.dumps({"advisor_system":new_system,"advisor_user":new_user},ensure_ascii=False,indent=2)
                st.download_button("📥 Скачать", data=prompts_json.encode("utf-8"),
                                   file_name="prompts_backup.json", mime="application/json",
                                   use_container_width=True, key="download_prompts_btn")

            # ── Анализатор заявок ─────────────────────────────────────────
            st.divider()
            st.subheader("🔍 Анализатор заявок")
            st.caption("Промпты суммаризатора (Map-Reduce) и анализа рисков")

            with st.expander("ℹ️ Переменные анализатора"):
                st.markdown(
                    "**MAP:** `{i}` — номер части, `{total}` — всего частей, `{chunk}` — текст фрагмента\n\n"
                    "**REDUCE:** `{target_words}` — целевой объём, `{combined}` — результаты MAP\n\n"
                    "**РИСКИ:** `{calc_context}` — данные расчётного файла, `{summary}` — резюме заявки"
                )

            st.markdown("**🗺️ Суммаризатор MAP — системный промпт**")
            new_claim_map_sys = st.text_area(
                "", value=current_prompts.get("claim_map_system", DEFAULT_PROMPTS_ADMIN["claim_map_system"]),
                height=100, key="prompt_claim_map_sys", label_visibility="collapsed"
            )
            st.markdown("**🗺️ Суммаризатор MAP — пользовательский промпт**")
            new_claim_map_usr = st.text_area(
                "", value=current_prompts.get("claim_map_user", DEFAULT_PROMPTS_ADMIN["claim_map_user"]),
                height=120, key="prompt_claim_map_usr", label_visibility="collapsed"
            )
            for v, name in [("{i}", "MAP user"), ("{total}", "MAP user"), ("{chunk}", "MAP user")]:
                if v not in new_claim_map_usr:
                    st.error(f"⚠️ {name} промпт должен содержать {v}")

            st.markdown("**📦 Суммаризатор REDUCE — системный промпт**")
            new_claim_red_sys = st.text_area(
                "", value=current_prompts.get("claim_reduce_system", DEFAULT_PROMPTS_ADMIN["claim_reduce_system"]),
                height=80, key="prompt_claim_red_sys", label_visibility="collapsed"
            )
            st.markdown("**📦 Суммаризатор REDUCE — пользовательский промпт**")
            new_claim_red_usr = st.text_area(
                "", value=current_prompts.get("claim_reduce_user", DEFAULT_PROMPTS_ADMIN["claim_reduce_user"]),
                height=120, key="prompt_claim_red_usr", label_visibility="collapsed"
            )

            st.markdown("**⚠️ Анализ рисков — системный промпт**")
            new_claim_risk_sys = st.text_area(
                "", value=current_prompts.get("claim_risks_system", DEFAULT_PROMPTS_ADMIN["claim_risks_system"]),
                height=100, key="prompt_claim_risk_sys", label_visibility="collapsed"
            )
            st.markdown("**⚠️ Анализ рисков — пользовательский промпт**")
            new_claim_risk_usr = st.text_area(
                "", value=current_prompts.get("claim_risks_user", DEFAULT_PROMPTS_ADMIN["claim_risks_user"]),
                height=180, key="prompt_claim_risk_usr", label_visibility="collapsed"
            )
            for v, name in [("{calc_context}", "Риски user"), ("{summary}", "Риски user")]:
                if v not in new_claim_risk_usr:
                    st.error(f"⚠️ {name} промпт должен содержать {v}")

            col1c, col2c = st.columns([2, 1])
            with col1c:
                if st.button("💾 Сохранить промпты анализатора", type="primary",
                             use_container_width=True, key="save_claim_prompts_btn"):
                    os.makedirs(os.path.dirname(PROMPTS_FILE_ADMIN), exist_ok=True)
                    updated = {
                        **current_prompts,
                        "claim_map_system":    new_claim_map_sys,
                        "claim_map_user":      new_claim_map_usr,
                        "claim_reduce_system": new_claim_red_sys,
                        "claim_reduce_user":   new_claim_red_usr,
                        "claim_risks_system":  new_claim_risk_sys,
                        "claim_risks_user":    new_claim_risk_usr,
                        "updated_at":          datetime.now().isoformat(),
                    }
                    with open(PROMPTS_FILE_ADMIN, "w", encoding="utf-8") as f:
                        json.dump(updated, f, ensure_ascii=False, indent=2)
                    st.success("✅ Промпты анализатора сохранены.")
                    st.rerun()
            with col2c:
                claim_prompts_json = json.dumps({
                    "claim_map_system":    new_claim_map_sys,
                    "claim_map_user":      new_claim_map_usr,
                    "claim_reduce_system": new_claim_red_sys,
                    "claim_reduce_user":   new_claim_red_usr,
                    "claim_risks_system":  new_claim_risk_sys,
                    "claim_risks_user":    new_claim_risk_usr,
                }, ensure_ascii=False, indent=2)
                st.download_button("📥 Скачать", data=claim_prompts_json.encode("utf-8"),
                                   file_name="claim_prompts_backup.json",
                                   mime="application/json",
                                   use_container_width=True, key="dl_claim_prompts_btn")

        elif admin_subtab == "📝 Отзывы":
            st.header("📝 Отзывы пользователей")
            feedbacks = get_feedback(limit=200)
            if feedbacks:
                for fb in feedbacks:
                    icon = {3:"👍",2:"😐",1:"👎"}.get(fb.get("rating"),"📝")
                    with st.expander(f"{icon} {fb['id']} — {fb['timestamp'][:10]} — {fb['feedback_type']}"):
                        if fb.get("question"): st.write(f"**Вопрос:** {fb['question']}")
                        st.write(f"**Комментарий:** {fb.get('description','—')}")
            else:
                st.info("Нет отзывов")

        elif admin_subtab == "⚙️ Настройки":
            st.header("⚙️ Настройки системы")
            st.info("Здесь будут настройки порога FAQ, модели, параметров поиска...")

if __name__ == "__main__":
    pass