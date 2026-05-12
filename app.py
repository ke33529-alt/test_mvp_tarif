import streamlit as st
import os
import sys
import pandas as pd
from datetime import datetime
import json

# Подавляем баг телеметрии ChromaDB (capture() takes 1 positional argument but 3 were given)
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY", "False")

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import TEST_FILES_DIR
from core.feedback import submit_feedback, get_feedback, get_answer_stats
from core import admin

# =============================================================================
# 📊 Функция статистики (ПРЯМОЕ ЧТЕНИЕ ФАЙЛА — без кэша)
# =============================================================================
def get_live_answer_stats(days: int = 7):
    """Читает файл напрямую — гарантированно свежие данные"""
    feedback_file = os.path.join("data", "feedback", "feedback_log.jsonl")
    stats = {
        "total": 0,
        "rating_3": 0,
        "rating_2": 0,
        "rating_1": 0,
        "with_comment": 0,
        "by_category": {},
        "top_bad_questions": [],
        "avg_rating": 0
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
            except:
                continue

            if fb.get("feedback_type") != "answer_rating":
                continue

            stats["total"] += 1
            rating = fb.get("rating")
            if rating == 3:
                stats["rating_3"] += 1
            elif rating == 2:
                stats["rating_2"] += 1
            elif rating == 1:
                stats["rating_1"] += 1

            if fb.get("question"):
                stats["top_bad_questions"].append({
                    "question": fb["question"][:100],
                    "answer": fb.get("answer", "")[:200],
                    "comment": fb.get("description", ""),
                    "timestamp": fb["timestamp"]
                })

            if stats["total"] > 0:
                stats["avg_rating"] = round(
                    (stats["rating_3"] * 3 + stats["rating_2"] * 2 + stats["rating_1"] * 1) / stats["total"],
                    2
                )
    

    # Логирование без эмодзи для совместимости с Windows
    try:
        print(f"[APP STATS] total={stats['total']}, good={stats['rating_3']}, bad={stats['rating_1']}")
    except:
        pass
    return stats

# =============================================================================
# 🎨 Настройка страницы
# =============================================================================
st.set_page_config(page_title="РЕГУЛА.AI", layout="wide", page_icon="⚡")

# =============================================================================
# 🔐 Инициализация session_state (ОБЯЗАТЕЛЬНО перед любым использованием)
# =============================================================================
if "admin_logged_in" not in st.session_state:
    st.session_state.admin_logged_in = False

if "show_landing" not in st.session_state:
    st.session_state.show_landing = True

# Безопасное получение значения (защита от race conditions)
def is_admin_logged() -> bool:
    return st.session_state.get("admin_logged_in", False)

# =============================================================================
# 🎨 CSS стили
# =============================================================================
st.markdown("""
<style>
.stApp { background-color: #f8f9fa; }
.main-title {
    font-size: 3rem;
    font-weight: 700;
    color: #063971;
    text-align: center;
    padding: 1.5rem 0;
    margin-bottom: 1rem;
    background: linear-gradient(90deg, #3498db, #2c3e50);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
}
.stSidebar { background-color: #ffffff; border-right: 1px solid #e0e0e0; }
.stSidebar * { text-align: left !important; }
.stDataFrame { min-height: 200px; }
.dataframe { border: 1px solid #e0e0e0; border-radius: 6px; }
.stButton>button { background-color: #6c757d; color: white; border-radius: 6px; border: none; padding: 0.5rem 1rem; width: 100%; }
.stButton>button:hover { background-color: #5a6268; }
h1, h2, h3 { color: #343a40; }
.stMetric { background: #ffffff; padding: 0.5rem; border-radius: 6px; }
.stExpander { background: #ffffff; border: 1px solid #e0e0e0; border-radius: 6px; }
.redirect-box {
    margin: 1rem 0;
    padding: 1rem;
    background: #e3f2fd;
    border-left: 4px solid #1976d2;
    border-radius: 0 6px 6px 0;
}

/* ── Логотип-кнопка в сайдбаре ── */
.sidebar-logo button {
    background: none !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0.3rem 0 !important;
    width: auto !important;
    font-size: 1.25rem !important;
    font-weight: 800 !important;
    letter-spacing: 0.02em !important;
    background: linear-gradient(90deg, #3498db, #063971) !important;
    -webkit-background-clip: text !important;
    -webkit-text-fill-color: transparent !important;
    cursor: pointer !important;
    transition: opacity 0.15s !important;
}
.sidebar-logo button:hover {
    opacity: 0.75 !important;
    background-color: transparent !important;
}

/* ── Лендинг: плитки продуктов ── */
.landing-tile button {
    min-height: 120px !important;
    text-align: left !important;
    white-space: pre-line !important;
    background: #ffffff !important;
    color: #1a3a5c !important;
    border: 1.5px solid #dde6f0 !important;
    border-radius: 14px !important;
    padding: 1.2rem 1.4rem !important;
    font-size: 1rem !important;
    font-weight: 600 !important;
    line-height: 1.5 !important;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05) !important;
    transition: all 0.18s ease !important;
}
.landing-tile button:hover {
    border-color: #3498db !important;
    background: #f3f8ff !important;
    box-shadow: 0 6px 20px rgba(52,152,219,0.15) !important;
    transform: translateY(-3px) !important;
    color: #063971 !important;
}
.landing-tile-desc {
    font-size: 0.78rem !important;
    color: #8a97a8 !important;
    margin-top: 0.1rem !important;
    margin-bottom: 1rem !important;
    padding: 0 0.25rem !important;
    line-height: 1.4 !important;
}
</style>
""", unsafe_allow_html=True)

# =============================================================================
#  Боковое меню
# =============================================================================

# Списки продуктов
_ACTIVE_PRODUCTS = [
    "🤝 Советчик",
    "📸 AI-Сканер документов",
    "🔍 Анализатор заявок",
    "🔮 Предсказание решения регулятора",
    "📋 Робот-протокольщик",
    "🛠 Админка",
]

_DEV_PRODUCTS = [
    "⚖️ Позиция ФАС",
    "🔍 Поиск прецедентов",
    "👥 Сверка численности",
    "🏭 Проверка амортизации",
    "📤 Экспорт ФГИС",
    "📝 Пояснительная записка",
    "📊 Калькулятор рисков",
    "📝 Робот-жалобщик",
    "🔄 Трекер изменений законов",
    "📊 Расчетный лист",
    "🔮 Прогнозист тарифов",
    "🌐 Сравнение с аналогами в регионе",
    "🎓 Режим обучения для новичков",
    "🗂️ Наведение порядка в документах",
    "🗓️ Планировщик тарифной кампании",
    "📊 Прогноз потребления",
]

_PRODUCT_DESCRIPTIONS = {
    "🤝 Советчик": "Ответы на вопросы по нормативной базе тарифного регулирования с опорой на актуальные НПА",
    "📸 AI-Сканер документов": "Автоматическое распознавание и структурирование загружаемых документов",
    "🔍 Анализатор заявок": "Проверка тарифных заявок на полноту комплекта и соответствие требованиям регулятора",
    "🔮 Предсказание решения регулятора": "Оценка вероятности одобрения заявки на основе исторических данных",
    "📋 Робот-протокольщик": "Автоматическое составление и форматирование протоколов заседаний",
    "🛠 Админка": "Управление системой: промпты, статистика, обратная связь",
}

# Инициализация выбора
if "main_choice" not in st.session_state:
    st.session_state.main_choice = _ACTIVE_PRODUCTS[0]

def _on_active_select():
    st.session_state.main_choice = st.session_state._sidebar_active

def _on_dev_select():
    st.session_state.main_choice = st.session_state._sidebar_dev

with st.sidebar:
    # Логотип — клик возвращает на лендинг
    st.markdown('<div class="sidebar-logo">', unsafe_allow_html=True)
    if st.button("⚡ REGULA.AI", key="sidebar_home_btn"):
        st.session_state.show_landing = True
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

    st.title("🧭 Меню")

    # --- Запущенные продукты ---
    st.markdown("**✅ Запущено**")
    _active_idx = (
        _ACTIVE_PRODUCTS.index(st.session_state.main_choice)
        if st.session_state.main_choice in _ACTIVE_PRODUCTS else None
    )
    st.radio(
        "active", _ACTIVE_PRODUCTS,
        index=_active_idx,
        key="_sidebar_active",
        label_visibility="collapsed",
        on_change=_on_active_select
    )

    # --- В разработке ---
    st.divider()
    _dev_expanded = st.session_state.main_choice in _DEV_PRODUCTS
    with st.expander("🚧 В разработке", expanded=_dev_expanded):
        st.caption("Эти продукты находятся в стадии разработки и будут доступны позже.")
        _dev_idx = (
            _DEV_PRODUCTS.index(st.session_state.main_choice)
            if st.session_state.main_choice in _DEV_PRODUCTS else None
        )
        st.radio(
            "dev", _DEV_PRODUCTS,
            index=_dev_idx,
            key="_sidebar_dev",
            label_visibility="collapsed",
            on_change=_on_dev_select
        )

    st.divider()
    if is_admin_logged():
        st.success("🔓 Админка: вход выполнен")
        if st.button("🚪 Выйти"):
            st.session_state.admin_logged_in = False
            st.rerun()

main_choice = st.session_state.main_choice

# =============================================================================
# 🏠 Посадочная страница
# =============================================================================
if st.session_state.show_landing:
    # Скрываем сайдбар на лендинге
    st.markdown("""
    <style>
    [data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none !important; }
    .block-container { padding-top: 2rem !important; max-width: 1100px !important; }
    </style>
    """, unsafe_allow_html=True)

    # Заголовок
    st.markdown("""
    <div style="text-align:center; padding: 2.5rem 0 1rem;">
        <div style="font-size:3rem; font-weight:900; letter-spacing:-0.02em;
                    background: linear-gradient(100deg, #3498db 0%, #063971 100%);
                    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
                    margin-bottom:0.5rem;">
            ⚡ REGULA.AI
        </div>
        <div style="font-size:1.05rem; color:#6b7a90; font-weight:400; max-width:560px; margin:0 auto; line-height:1.6;">
            ИИ-система поддержки принятия решений<br>в области тарифного регулирования
        </div>
        <div style="margin-top:0.8rem; font-size:0.82rem; color:#aab4c0; letter-spacing:0.04em;">
            🔹 21 продукт &nbsp;·&nbsp; 🔹 2025–2026 &nbsp;·&nbsp; 🔹 Россия
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("<hr style='border:none;border-top:1px solid #e8ecf2;margin:1rem 0 2rem;'>", unsafe_allow_html=True)

    st.markdown("#### ✅ Запущенные продукты")
    st.markdown("<div style='height:0.4rem'></div>", unsafe_allow_html=True)

    _cols_per_row = 3
    _products = _ACTIVE_PRODUCTS
    for _row_start in range(0, len(_products), _cols_per_row):
        _row_items = _products[_row_start : _row_start + _cols_per_row]
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
@st.dialog("🚧 Продукт в разработке")
def show_dev_dialog(product_name: str):
    st.markdown(f"### {product_name}")
    st.markdown("""
Этот продукт **находится в активной разработке** и пока не готов к полноценному использованию.

В интерфейсе представлен **прототип решения** — демонстрация концепции и будущего функционала.
Данные и результаты носят ознакомительный характер и не могут использоваться для принятия решений.

> 💡 Если у вас есть пожелания к функциональности этого модуля — свяжитесь с командой разработки.
    """)
    st.divider()
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("✅ Понятно, продолжить", type="primary", use_container_width=True):
            st.session_state._dev_dialog_confirmed = product_name
            st.rerun()
    with col2:
        if st.button("← Вернуться", use_container_width=True):
            st.session_state.main_choice = _ACTIVE_PRODUCTS[0]
            st.session_state._dev_dialog_confirmed = None
            st.rerun()

# Показываем диалог при первом входе в продукт из раздела "В разработке"
if main_choice in _DEV_PRODUCTS:
    if st.session_state.get("_dev_dialog_confirmed") != main_choice:
        show_dev_dialog(main_choice)
else:
    st.session_state._dev_dialog_confirmed = None

# =============================================================================
# 🎯 Заголовок системы
# =============================================================================
st.markdown("""
<div class="main-title">
RegulaAI<br>
<span style="font-size: 1.5rem; font-weight: 400;">ИИ-Система поддержки принятия решений в области тарифного регулирования</span><br>
<span style="font-size: 1rem; font-weight: 400; opacity: 0.8;">🔹 21 продукт | 🔹 2025-2026 | 🔹 Россия</span>
</div>
""", unsafe_allow_html=True)
st.divider()

# =============================================================================
# 📊 Данные для примеров (кэширование)
# =============================================================================
@st.cache_data
def get_example_data():
    completeness = pd.DataFrame([
        {"Документ": "Устав организации", "Требуется": "✅", "Приложено": "✅", "Статус": "🟢"},
        {"Документ": "Договор теплоснабжения", "Требуется": "✅", "Приложено": "✅", "Статус": "🟢"},
        {"Документ": "Бухгалтерский баланс (Ф-1)", "Требуется": "✅", "Приложено": "❌", "Статус": "🔴"},
        {"Документ": "Акт сверки с контрагентами", "Требуется": "✅", "Приложено": "✅", "Статус": "🟡"},
        {"Документ": "Лицензия на деятельность", "Требуется": "❌", "Приложено": "—", "Статус": "⚪"},
    ])
    articles = pd.DataFrame([
        {"Статья": "Расходы на тепловую энергию", "Сумма (тыс. ₽)": 113705, "Документы": 3, "Риск": "🟢 5%", "Рекомендация": "—"},
        {"Статья": "Расходы на оплату труда АУП", "Сумма (тыс. ₽)": 9038, "Документы": 1, "Риск": "🟡 35%", "Рекомендация": "Приложите приказ о зарплате"},
        {"Статья": "Расходы на ремонт ОС", "Сумма (тыс. ₽)": 25, "Документы": 0, "Риск": "🔴 90%", "Рекомендация": "Приложите дефектную ведомость"},
        {"Статья": "Программное обеспечение", "Сумма (тыс. ₽)": 590, "Документы": 1, "Риск": "🟡 40%", "Рекомендация": "Приложите лицензию"},
        {"Статья": "Хозяйственные расходы", "Сумма (тыс. ₽)": 150, "Документы": 2, "Риск": "🟢 10%", "Рекомендация": "—"},
    ])
    return {"completeness": completeness, "articles": articles}

example_data = get_example_data()
df_comp = example_data["completeness"]
df_art = example_data["articles"]

# =============================================================================
# Вкладка 1: Анализатор заявок 🔍
# =============================================================================
if main_choice == "🔍 Анализатор заявок":
    st.header("🔍 Анализатор тарифных заявок")
    st.info("📌 Загрузите расчётную модель и документы для проверки")

    uploaded_files = st.file_uploader(
        "Загрузите файлы",
        type=['xlsx', 'xls', 'pdf', 'docx'],
        accept_multiple_files=True
    )

    if uploaded_files:
        st.success(f"✅ Загружено: {len(uploaded_files)} файл(ов)")
        st.subheader("📁 Приложенные файлы")

        if "calc_file" not in st.session_state:
            st.session_state.calc_file = None

        for uploaded_file in uploaded_files:
            col1, col2 = st.columns([4, 1])
            with col1:
                st.write(f"📄 {uploaded_file.name}")
            with col2:
                is_calc = st.checkbox("🧮 Расчёт", key=f"calc_{uploaded_file.name}",
                                      value=(st.session_state.calc_file == uploaded_file.name))
                if is_calc:
                    st.session_state.calc_file = uploaded_file.name

        if st.session_state.calc_file:
            st.info(f"🧮 **Расчётный файл:** {st.session_state.calc_file}")

        st.subheader("📋 Шаг 1: Проверка комплектности документов")
        st.dataframe(df_comp, use_container_width=True, hide_index=True)

        st.subheader("📊 Шаг 2: Статьи затрат и риски")
        st.dataframe(df_art, use_container_width=True, hide_index=True)

        st.subheader("💰 Валовая выручка")
        total = int(df_art["Сумма (тыс. ₽)"].sum())
        col1, col2, col3 = st.columns(3)
        col1.metric("2024", f"{total:,.0f} тыс. ₽")
        col2.metric("2025", f"{int(total * 1.04):,.0f} тыс. ₽")
        col3.metric("2026", f"{int(total * 1.08):,.0f} тыс. ₽")

        with st.expander("📝 Сообщить об ошибке", expanded=False):
            with st.form("feedback_analyzer"):
                issue = st.selectbox("Тип проблемы", [
                    "Файл не распознан", "Неверная классификация",
                    "Статья не извлечена", "Неверный риск", "Другое"
                ])
                file_list = ["— не относится —"] + [f.name for f in uploaded_files] if uploaded_files else ["— нет файлов —"]
                file = st.selectbox("Файл", file_list)
                desc = st.text_area("Описание", placeholder="Что пошло не так?")
                submitted = st.form_submit_button("📤 Отправить")
                if submitted:
                    if desc:
                        submit_feedback("user", issue, desc, file_name=file if file != "— не относится —" else None)
                        st.success("✅ Спасибо за отзыв!")
                    else:
                        st.warning("Опишите проблему")

# =============================================================================
# Вкладка 2: Советчик 🤝 (ОБНОВЛЁНО С ВЫБОРОМ МОДЕЛИ И ТАБЛИЦАМИ)
# =============================================================================
elif main_choice == "🤝 Советчик":
    st.header("🤝 Советчик по нормативной базе")
    st.info("📌 Задайте вопрос по тарифному регулированию — ИИ определит нужный раздел и даст ответ")

    # Инициализация session_state
    if "last_query" not in st.session_state:
        st.session_state.last_query = ""
    if "last_result" not in st.session_state:
        st.session_state.last_result = None
    if "search_triggered" not in st.session_state:
        st.session_state.search_triggered = False
    if "sources_only_mode" not in st.session_state:
        st.session_state.sources_only_mode = False
    if "query_times" not in st.session_state:
        st.session_state.query_times = []
    if "advisor_model" not in st.session_state:
        st.session_state.advisor_model = "qwen/qwen3.5-9b"  # Модель по умолчанию (LM Studio)

    # Проверка векторной базы
    vector_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vector_db")
    db_file = os.path.join(vector_db_path, "chroma.sqlite3")

    if not os.path.exists(db_file):
        st.warning("⚠️ Векторная база не найдена. Запустите индексацию в админке.")
        st.info(f"📂 Ожидаемый путь: {db_file}")
    else:
        with st.expander("💡 Примеры вопросов", expanded=False):
            st.write("• Можно ли включать затраты на ДМС в тариф?")
            st.write("• Какие документы нужны для тарифной заявки по теплоснабжению?")
            st.write("• Как ФАС трактует расходы на программное обеспечение?")
            st.write("• Что такое валовая выручка и как она рассчитывается?")

        # 🔧 Панель настроек и тестирования
        with st.expander("⚙️ Настройки и тестирование", expanded=False):
            col1, col2 = st.columns(2)
            with col1:
                top_k = st.slider("Количество источников", 1, 10, 5, key="top_k_slider")
                temperature = st.slider("Креативность ответа", 0.0, 1.0, 0.3, 0.1, key="temp_slider")
            with col2:
                # ✅ ВЫБОР МОДЕЛИ
                try:
                    from core.advisor import get_available_models
                    available_models = get_available_models()
                    model_names = [m["name"] for m in available_models] if available_models else ["qwen/qwen3.5-9b"]
                    selected_model = st.selectbox(
                        "🤖 Модель для ответов",
                        options=model_names,
                        index=model_names.index(st.session_state.advisor_model) if st.session_state.advisor_model in model_names else 0,
                        key="advisor_model_select",
                        help="Список моделей загружается из LM Studio (127.0.0.1:1234). Убедитесь, что сервер запущен."
                    )
                    st.session_state.advisor_model = selected_model
                    st.caption(f"✅ Доступные модели: {', '.join(model_names)}")
                except:
                    st.session_state.advisor_model = st.selectbox(
                        "🤖 Модель для ответов",
                        options=["qwen/qwen3.5-9b"],
                        index=0,
                        key="advisor_model_select",
                        help="Не удалось подключиться к LM Studio. Проверьте, что сервер запущен на 127.0.0.1:1234."
                    )

                # Переключатель режима тестирования чанков
                sources_only_mode = st.toggle(
                    "🧪 Режим тестов чанков (без LLM)",
                    value=st.session_state.sources_only_mode,
                    key="sources_only_toggle",
                    help="Отключает LLM для быстрого тестирования качества источников"
                )
                st.session_state.sources_only_mode = sources_only_mode

                # Кнопка очистки кэша
                if st.button("🗑 Очистить кэш LLM", key="clear_cache_btn", use_container_width=True):
                    from core.advisor import _llm_cache, save_llm_cache
                    _llm_cache.clear()
                    save_llm_cache()
                    st.success("✅ Кэш очищен")
                    st.session_state.query_times = []
                    st.rerun()

            # Статистика производительности
            if st.session_state.query_times:
                st.divider()
                st.caption("📊 Статистика запросов:")
                avg_time = sum(st.session_state.query_times) / len(st.session_state.query_times)
                col1, col2, col3 = st.columns(3)
                col1.metric("Запросов", len(st.session_state.query_times))
                col2.metric("Среднее время", f"{avg_time:.2f} сек")
                col3.metric("Последний", f"{st.session_state.query_times[-1]:.2f} сек" if st.session_state.query_times else "—")

        # Поле ввода вопроса
        query = st.text_area(
            "Ваш вопрос",
            height=100,
            placeholder="Например: Какие расходы на ремонт можно включать в тариф?",
            key="question_input",
            value=st.session_state.last_query
        )

        # Индикатор режима тестирования
        if st.session_state.sources_only_mode:
            st.warning("🧪 **Режим тестов чанков активен:** LLM отключён, показываются только источники")

        # Кнопка поиска
        if st.button("🔎 Найти ответ", type="primary", key="search_btn"):
            if query.strip():
                st.session_state.search_triggered = True
                start_time = datetime.now()
                with st.spinner("🔄 Анализируем вопрос и ищем ответ..."):
                    try:
                        from core.advisor import ask_question, set_sources_only_mode

                        # Устанавливаем режим тестирования
                        set_sources_only_mode(st.session_state.sources_only_mode)

                        # ✅ Передаём выбранную модель в ask_question
                        result = ask_question(
                            query,
                            top_k=top_k,
                            temperature=temperature,
                            model=st.session_state.advisor_model # ✅ Выбор модели
                        )

                        # Замер времени
                        end_time = datetime.now()
                        query_time = (end_time - start_time).total_seconds()
                        st.session_state.query_times.append(query_time)

                        # Храним только последние 10 запросов
                        if len(st.session_state.query_times) > 10:
                            st.session_state.query_times = st.session_state.query_times[-10:]

                        # Debug-лог в терминал
                        cache_status = "[CACHE HIT] " if result.get("from_cache") else "[CACHE MISS] "
                        print(f"[DEBUG] {cache_status} answer_len={len(result.get('answer', ''))}, sources={len(result.get('sources', []))}, time={query_time:.2f}s, model={st.session_state.advisor_model}")

                        st.session_state.last_query = query
                        st.session_state.last_result = result
                        st.rerun()

                    except ImportError as e:
                        st.error(f"❌ Не удалось загрузить модуль: {e}")
                        st.session_state.last_result = {"error": str(e)}
                    except Exception as e:
                        st.error(f"❌ Ошибка: {type(e).__name__}: {str(e)}")
                        st.session_state.last_result = {"error": str(e)}
            else:
                st.warning("⚠️ Введите вопрос")

        # Отображение результата (ОТДЕЛЬНО от кнопки)
        result = st.session_state.last_result

        # Debug-инфо в интерфейсе
        if result:
            st.caption(f"🔍 DEBUG: answer_len={len(result.get('answer', ''))}, sources={len(result.get('sources', []))}, model={result.get('model', st.session_state.advisor_model)}")

        # Показываем результат, если он есть
        if result:
            # Обработка ошибки
            if result.get("error"):
                st.error(f"🔧 Техническая ошибка: {result['error']}")
                st.info("💡 Попробуйте перезапустить приложение")

            # Показываем ответ
            else:
                answer = result.get("answer", "")

                # Статус
                if result.get("from_cache"):
                    st.info("⚡ Ответ из кэша (мгновенно)")
                elif result.get("from_faq"):
                    st.success("✅ Ответ из базы частых вопросов")
                elif answer and not answer.startswith("❌"):
                    if st.session_state.sources_only_mode:
                        st.info("🧪 Режим тестов: LLM отключён")
                    else:
                        st.success(f"✅ Ответ сгенерирован ИИ (модель: {result.get('model', st.session_state.advisor_model)})")
                elif answer.startswith("❌"):
                    st.warning(answer)

                # Сам ответ
                if answer and not st.session_state.sources_only_mode:
                    # --- НАЧАЛО ИЗМЕНЕНИЙ ДЛЯ ПОДДЕРЖКИ ТАБЛИЦ ---
                    import re
                    import io

                    # Проверяем наличие Markdown таблиц
                    table_pattern = r'\|.*\|\n\|[-:\s|]+\|\n(?:\|.*\|\n)*'
                    tables = re.findall(table_pattern, answer, re.MULTILINE)

                    if tables:
                        for i, table_md in enumerate(tables):
                            try:
                                # Преобразуем Markdown таблицу в Pandas DataFrame
                                df = pd.read_csv(
                                    io.StringIO(table_md.replace('|', ',')),
                                    header=0,
                                    index_col=0,
                                    skipinitialspace=True
                                )

                                # Очищаем заголовки от лишних пробелов
                                df.columns = [str(col).strip() for col in df.columns]
                                df.index.name = None

                                # Отображаем красивую таблицу
                                st.subheader(f"📊 Таблица {i+1}")
                                st.dataframe(df, use_container_width=True, hide_index=True)

                                # Удаляем сырую Markdown-таблицу из текста ответа, чтобы не дублировать
                                answer = answer.replace(table_md, "")

                            except Exception as e:
                                # Если парсинг упал, просто показываем исходный текст таблицы
                                st.code(table_md, language="markdown")
                    else:
                        # Если таблиц нет, просто выводим текст
                        pass

                    # Выводим оставшийся текст (с поддержкой жирного/курсива)
                    if answer.strip():
                        st.markdown(f"### 📝 Ответ:\n{answer.strip()}")
                    # --- КОНЕЦ ИЗМЕНЕНИЙ ДЛЯ ПОДДЕРЖКИ ТАБЛИЦ ---

                elif st.session_state.sources_only_mode:
                    st.info("ℹ️ В режиме тестов показываем только источники. Включите LLM для полного ответа.")

                # Источники
                sources = result.get("sources", [])
                if sources:
                    st.subheader(f"📚 Источники ({len(sources)}):")
                    for i, src in enumerate(sources, 1):
                        file_name = src.get("file", "Неизвестно")
                        page = src.get("page", "")
                        snippet = src.get("snippet", "")
                        category = src.get("category", "")
                        label = f"📄 {i}. {file_name}"
                        if page:
                            label += f" (стр. {page})"
                        if category:
                            label += f" • {category}"
                        with st.expander(label):
                            st.caption(snippet[:600] + ("..." if len(snippet) > 600 else ""))

                # Перенаправление
                if result.get("redirect"):
                    redirect_name = result["redirect"]
                    redirect_reason = result.get("redirect_reason", f"Ваш вопрос относится к: {redirect_name}")
                    st.divider()
                    st.info(f"💡 {redirect_reason}")
                    st.markdown(f"""
                    <div class="redirect-box">
                        <b>👉 Перейдите в раздел «{redirect_name}» в меню слева</b>
                    </div>
                    """, unsafe_allow_html=True)

                # Оценка (только если не режим тестов)
                if not st.session_state.sources_only_mode:
                    st.divider()
                    st.subheader("📊 Оцените ответ")
                    col1, col2, col3 = st.columns(3)
                    with col1:
                        if st.button("👍", key="btn_good", use_container_width=True):
                            from core.feedback import submit_feedback
                            submit_feedback("user", "answer_rating", "Полезно", question=query[:500], answer=answer[:1000], rating=3)
                            st.success("✅ Спасибо!")
                            st.session_state.last_result = None
                            st.session_state.search_triggered = False
                            st.rerun()
                    with col2:
                        if st.button("😐", key="btn_neutral", use_container_width=True):
                            from core.feedback import submit_feedback
                            submit_feedback("user", "answer_rating", "Нормально", question=query[:500], answer=answer[:1000], rating=2)
                            st.success("✅ Спасибо!")
                            st.session_state.last_result = None
                            st.session_state.search_triggered = False
                            st.rerun()
                    with col3:
                        if st.button("👎", key="btn_bad", use_container_width=True):
                            from core.feedback import submit_feedback
                            submit_feedback("user", "answer_rating", "Не помогло", question=query[:500], answer=answer[:1000], rating=1)
                            st.success("✅ Спасибо!")
                            st.session_state.last_result = None
                            st.session_state.search_triggered = False
                            st.rerun()

                # Новый вопрос
                st.divider()
                col1, col2 = st.columns([3, 1])
                with col2:
                    if st.button("🔄 Новый", key="btn_new", use_container_width=True):
                        st.session_state.last_query = ""
                        st.session_state.last_result = None
                        st.session_state.search_triggered = False
                        st.rerun()

        # Подсказка если нет результата
        elif not st.session_state.search_triggered:
            st.info("💡 Задайте вопрос и нажмите «🔎 Найти ответ»")

# =============================================================================
# Вкладка 3-21: Остальные продукты (заглушки)
# =============================================================================
elif main_choice == "⚖️ Позиция ФАС":
    try:
        from streamlit_pages.fas_position import show_fas_position
        show_fas_position()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🔍 Поиск прецедентов":
    try:
        from streamlit_pages.court_precedents import show_court_precedents
        show_court_precedents()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "👥 Сверка численности":
    try:
        from streamlit_pages.numeracy_check import show_numeracy_check
        show_numeracy_check()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🏭 Проверка амортизации":
    try:
        from streamlit_pages.amortization_check import show_amortization_check
        show_amortization_check()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📤 Экспорт ФГИС":
    try:
        from streamlit_pages.fgis_export import show_fgis_export
        show_fgis_export()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📝 Пояснительная записка":
    try:
        from streamlit_pages.explanatory_note import show_explanatory_note
        show_explanatory_note()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📊 Калькулятор рисков":
    try:
        from streamlit_pages.risk_calculator import show_risk_calculator
        show_risk_calculator()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📝 Робот-жалобщик":
    try:
        from streamlit_pages.complaint_bot import show_complaint_bot
        show_complaint_bot()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🔄 Трекер изменений законов":
    try:
        from streamlit_pages.law_tracker import show_law_tracker
        show_law_tracker()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📊 Расчетный лист":
    try:
        from streamlit_pages.calc_sheet import show_calc_sheet
        show_calc_sheet()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📸 AI-Сканер документов":
    try:
        from streamlit_pages.doc_scanner import show_doc_scanner
        show_doc_scanner()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📋 Робот-протокольщик":
    try:
        from streamlit_pages.protocol_bot import show_protocol_bot
        show_protocol_bot()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🔮 Предсказание решения регулятора":
    try:
        from streamlit_pages.predictor import show_predictor
        show_predictor()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🔮 Прогнозист тарифов":
    try:
        from streamlit_pages.tariff_forecaster import show_tariff_forecaster
        show_tariff_forecaster()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🌐 Сравнение с аналогами в регионе":
    try:
        from streamlit_pages.peer_comparison import show_peer_comparison
        show_peer_comparison()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🎓 Режим обучения для новичков":
    try:
        from streamlit_pages.training_mode import show_training_mode
        show_training_mode()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🗂️ Наведение порядка в документах":
    try:
        from streamlit_pages.document_organizer import show_document_organizer
        show_document_organizer()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "🗓️ Планировщик тарифной кампании":
    try:
        from streamlit_pages.tariff_planner import show_tariff_planner
        show_tariff_planner()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

elif main_choice == "📊 Прогноз потребления":
    try:
        from streamlit_pages.consumption_forecast import show_consumption_forecast
        show_consumption_forecast()
    except ImportError as e:
        st.error(f"❌ Не удалось загрузить модуль: {e}")

# =============================================================================
# Вкладка 22: Админка 🛠
# =============================================================================
elif main_choice == "🛠 Админка":
    st.header("🛠 Панель администратора")

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
        # ✅ Определяем admin_subtab ПЕРЕД использованием
        admin_subtab = st.radio(
            "Раздел админки",
            ["📈 Аналитика ИИ", "📚 Документы", "⚙️ Настройки чанкования", "📝 Промпты", "📝 Отзывы", "⚙️ Настройки"],
            horizontal=True
        )

        if admin_subtab == "📈 Аналитика ИИ":
            col1, col2 = st.columns([4, 1])
            with col1:
                st.header("📊 Качество работы ИИ-советчика")
            with col2:
                if st.button("🔄 Обновить", key="refresh_stats"):
                    st.rerun()
            st.caption(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")

            period = st.selectbox("Период", ["7 дней", "30 дней", "90 дней", "Всё время"], key="period_select")
            days = {"7 дней": 7, "30 дней": 30, "90 дней": 90, "Всё время": 365}[period]

            try:
                stats = get_live_answer_stats(days=days)
                if stats["total"] > 0:
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Всего оценок", stats["total"])
                    col2.metric("Средний рейтинг", f"{stats['avg_rating']}/3.0")
                    col3.metric("👍 Полезно", stats["rating_3"])
                    col4.metric("👎 Не помогло", stats["rating_1"])

                    quality_pct = round((stats["rating_3"] / stats["total"]) * 100) if stats["total"] > 0 else 0
                    st.subheader("📈 Процент полезных ответов")
                    st.progress(quality_pct / 100)
                    st.caption(f"{quality_pct}% ответов оценены как 👍 Полезно (цель: 85%)")

                    st.subheader("📊 Распределение оценок")
                    rating_df = pd.DataFrame({
                        "Оценка": ["👍 Полезно", "😐 Нормально", "👎 Не помогло"],
                        "Количество": [stats["rating_3"], stats["rating_2"], stats["rating_1"]]
                    })
                    st.bar_chart(rating_df.set_index("Оценка"))

                    if stats["top_bad_questions"]:
                        st.subheader("❓ Топ вопросов для улучшения")
                        st.info(f"Найдено {len(stats['top_bad_questions'])} вопросов с оценкой 👎")
                        for i, item in enumerate(stats["top_bad_questions"], 1):
                            with st.expander(f"{i}. «{item['question']}...»"):
                                st.write(f"**Ответ ИИ:** {item['answer']}")
                                st.write(f"**Комментарий:** {item['comment']}")
                                st.write(f"**Дата:** {item['timestamp'][:10]}")

                        st.divider()
                        st.subheader("💡 Рекомендации по улучшению")
                        if quality_pct < 50:
                            st.warning("⚠️ Низкое качество ответов (<50%). Рекомендуется:")
                            st.write("• Добавить 50–100 вопросов в FAQ")
                            st.write("• Увеличить размер чанков при индексации")
                        elif quality_pct < 70:
                            st.info("ℹ️ Среднее качество (50–70%). Рекомендуется:")
                            st.write("• Добавить query-трансформацию (синонимы)")
                            st.write("• Внедрить гибридный поиск")
                        else:
                            st.success("✅ Хорошее качество (>70%). Продолжайте в том же духе!")
                else:
                    st.info("📭 Пока нет оценок. Попросите пользователей оценивать ответы в Советчике.")
            except Exception as e:
                st.error(f"Ошибка загрузки статистики: {e}")

        elif admin_subtab == "📚 Документы":
            st.header("📚 База знаний — документы")

            SPHERES = [
                "🔥 Теплоснабжение",
                "💧 Водоснабжение/водоотведение",
                "🗑️ Обращение с ТКО",
                "🔵 Газ",
                "⚡ Электрика",
                "📁 Иные сферы",
            ]
            CATEGORY_FOLDERS = {
                "📜 Общие НПА": "npa",
                "⚖️ Документы ФАС": "fas",
                "🏛️ Судебная практика": "court",
                "📋 Методички и разъяснения": "methodics",
            }
            SPHERES_FILE = os.path.join("config", "doc_spheres.json")

            def load_spheres_map() -> dict:
                if os.path.exists(SPHERES_FILE):
                    try:
                        with open(SPHERES_FILE, "r", encoding="utf-8") as f:
                            return json.load(f)
                    except Exception:
                        pass
                return {}

            def save_spheres_map(m: dict):
                os.makedirs(os.path.dirname(SPHERES_FILE), exist_ok=True)
                with open(SPHERES_FILE, "w", encoding="utf-8") as f:
                    json.dump(m, f, ensure_ascii=False, indent=2)

            spheres_map = load_spheres_map()

            # ── Загрузка файлов ──────────────────────────────────────────────
            st.subheader("📤 Загрузить документы")
            col_up1, col_up2 = st.columns([3, 1])
            with col_up1:
                upload_category = st.selectbox(
                    "Категория для загрузки",
                    list(CATEGORY_FOLDERS.keys()),
                    key="upload_cat_select"
                )
            with col_up2:
                upload_spheres = st.multiselect(
                    "Сферы",
                    SPHERES,
                    key="upload_spheres_select",
                    placeholder="Выберите..."
                )

            uploaded = st.file_uploader(
                "Перетащите файлы или выберите с компьютера",
                type=["pdf", "txt", "docx", "xlsx"],
                accept_multiple_files=True,
                key="doc_uploader",
                label_visibility="collapsed"
            )

            if uploaded:
                dest_folder = CATEGORY_FOLDERS[upload_category]
                dest_path = os.path.join("data", "raw", dest_folder)
                os.makedirs(dest_path, exist_ok=True)
                if st.button(f"💾 Сохранить и индексировать ({len(uploaded)} файл(ов))", type="primary", key="save_upload_btn"):
                    progress = st.progress(0)
                    for i, uf in enumerate(uploaded):
                        file_path = os.path.join(dest_path, uf.name)
                        with open(file_path, "wb") as f:
                            f.write(uf.getbuffer())
                        if upload_spheres:
                            spheres_map[uf.name] = upload_spheres
                            save_spheres_map(spheres_map)
                        try:
                            from core.indexer import index_file
                            result = index_file(file_path, dest_folder)
                            chunks = result.get("chunks", 0)
                        except Exception:
                            chunks = 0
                        progress.progress((i + 1) / len(uploaded))
                    st.success(f"✅ Загружено и проиндексировано: {len(uploaded)} файл(ов)")
                    st.rerun()

            st.divider()

            # ── Фильтры ──────────────────────────────────────────────────────
            st.subheader("📋 Список документов")
            fc1, fc2, fc3 = st.columns([2, 2, 3])
            with fc1:
                filter_cat = st.selectbox(
                    "Категория",
                    ["— Все —"] + list(CATEGORY_FOLDERS.keys()),
                    key="filter_cat"
                )
            with fc2:
                filter_sphere = st.selectbox(
                    "Сфера",
                    ["— Все —"] + SPHERES,
                    key="filter_sphere"
                )
            with fc3:
                filter_name = st.text_input("🔍 Поиск по имени файла", placeholder="Введите часть названия...", key="filter_name")

            # ── Запрашиваем статус индексации из ChromaDB один раз ──────────
            _chroma_index = {}  # fname -> {"chunks": N, "indexed_at": "2025-..."}
            try:
                import chromadb as _chromadb
                _vector_db_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "data", "vector_db"
                )
                _chroma_client = _chromadb.PersistentClient(path=_vector_db_path)
                _collection = _chroma_client.get_collection(name="tariff_docs")
                _results = _collection.get(include=["metadatas"])
                for meta in _results["metadatas"]:
                    fn = meta.get("filename", "")
                    if not fn:
                        continue
                    if fn not in _chroma_index:
                        _chroma_index[fn] = {
                            "chunks": 0,
                            "indexed_at": meta.get("indexed_at", "")[:10] if meta.get("indexed_at") else "—"
                        }
                    _chroma_index[fn]["chunks"] += 1
            except Exception:
                pass  # база пуста или не найдена — покажем "не индексирован"

            # ── Собираем все файлы ───────────────────────────────────────────
            all_files = []
            cats_to_show = (
                {filter_cat: CATEGORY_FOLDERS[filter_cat]}
                if filter_cat != "— Все —"
                else CATEGORY_FOLDERS
            )
            for cat_label, folder in cats_to_show.items():
                folder_path = os.path.join("data", "raw", folder)
                if not os.path.exists(folder_path):
                    continue
                for fname in sorted(os.listdir(folder_path)):
                    fpath = os.path.join(folder_path, fname)
                    if not os.path.isfile(fpath) or fname.endswith(".indexed") or fname.startswith("."):
                        continue
                    ext = os.path.splitext(fname)[1].upper().lstrip(".") or "—"
                    size_kb = os.path.getsize(fpath) / 1024
                    file_spheres = spheres_map.get(fname, [])
                    chroma_info = _chroma_index.get(fname, {})
                    chunks_count = chroma_info.get("chunks", 0)
                    indexed_at = chroma_info.get("indexed_at", "—") if chunks_count > 0 else "—"
                    all_files.append({
                        "fname": fname,
                        "fpath": fpath,
                        "folder": folder,
                        "cat_label": cat_label,
                        "ext": ext,
                        "size_kb": size_kb,
                        "indexed_at": indexed_at,
                        "chunks_count": chunks_count,
                        "spheres": file_spheres,
                    })

            # Фильтр по сфере и имени
            if filter_sphere != "— Все —":
                all_files = [f for f in all_files if filter_sphere in f["spheres"]]
            if filter_name.strip():
                all_files = [f for f in all_files if filter_name.lower() in f["fname"].lower()]

            if not all_files:
                st.info("📭 Документов не найдено. Загрузите файлы выше.")
            else:
                st.caption(f"Найдено документов: **{len(all_files)}**")

                # ── Заголовок таблицы ────────────────────────────────────────
                hc = st.columns([1, 4, 2, 3, 2, 1, 1, 1, 1])
                for col, label in zip(hc, ["Формат", "Наименование", "Категория", "Сферы", "Дата индексации", "📥", "🔄", "📤", "🗑️"]):
                    col.markdown(f"**{label}**")
                with st.container():
                    _, _, _, _, _, c1, c2, c3, c4 = st.columns([1, 4, 2, 3, 2, 1, 1, 1, 1])
                    c1.caption("скачать")
                    c2.caption("индекс")
                    c3.caption("из индекса")
                    c4.caption("удалить файл")
                st.divider()

                # ── Строки ───────────────────────────────────────────────────
                EXT_ICONS = {"PDF": "📕", "TXT": "📄", "DOCX": "📘", "XLSX": "📗"}

                for fi in all_files:
                    row = st.columns([1, 4, 2, 3, 2, 1, 1, 1, 1])
                    icon = EXT_ICONS.get(fi["ext"], "📄")

                    with row[0]:
                        st.markdown(f"{icon} `{fi['ext']}`")
                    with row[1]:
                        st.markdown(f"**{fi['fname']}**")
                        st.caption(f"{fi['size_kb']:.1f} КБ")
                    with row[2]:
                        st.caption(fi["cat_label"])
                    with row[3]:
                        new_spheres = st.multiselect(
                            "сферы",
                            SPHERES,
                            default=fi["spheres"],
                            key=f"spheres_{fi['fname']}_{fi['folder']}",
                            label_visibility="collapsed"
                        )
                        if new_spheres != fi["spheres"]:
                            spheres_map[fi["fname"]] = new_spheres
                            save_spheres_map(spheres_map)
                    with row[4]:
                        if fi["chunks_count"] > 0:
                            st.markdown(f"✅ {fi['indexed_at']}")
                            st.caption(f"{fi['chunks_count']} чанков")
                        else:
                            st.caption("⬜ не индексирован")

                    # 📥 Скачать
                    with row[5]:
                        with open(fi["fpath"], "rb") as f:
                            st.download_button(
                                "📥",
                                data=f.read(),
                                file_name=fi["fname"],
                                key=f"dl_{fi['fname']}_{fi['folder']}",
                                use_container_width=True,
                                help="Скачать файл"
                            )

                    # 🔄 Индексировать файл (переиндексация — сначала удаляем старые чанки)
                    with row[6]:
                        if st.button("🔄", key=f"idx_{fi['fname']}_{fi['folder']}", use_container_width=True, help="Индексировать файл (если уже был — старые чанки заменяются)"):
                            with st.spinner(f"Индексация {fi['fname']}..."):
                                try:
                                    from core.indexer import remove_file_from_index, index_file
                                    # Безопасное удаление — не падаем если файла не было в индексе
                                    try:
                                        remove_file_from_index(fi["fname"])
                                    except Exception:
                                        pass
                                    result = index_file(fi["fpath"], fi["folder"])
                                    if result["status"] == "success":
                                        st.success(f"✅ {result.get('chunks', 0)} чанков")
                                    else:
                                        st.error(f"❌ {result.get('message', '')}")
                                except Exception as e:
                                    st.error(f"❌ {str(e)}")
                            st.rerun()

                    # 📤 Удалить только из индекса (файл остаётся)
                    with row[7]:
                        if st.button("📤", key=f"rmidx_{fi['fname']}_{fi['folder']}", use_container_width=True, help="Удалить из индекса (файл останется в папке)"):
                            st.session_state[f"_confirm_rmidx_{fi['fname']}"] = True

                    # 🗑️ Удалить файл из папки
                    with row[8]:
                        if st.button("🗑️", key=f"del_{fi['fname']}_{fi['folder']}", use_container_width=True, help="Удалить файл из папки (и из индекса)"):
                            st.session_state[f"_confirm_del_{fi['fname']}"] = True

                    # Диалог: удалить только из индекса
                    if st.session_state.get(f"_confirm_rmidx_{fi['fname']}"):
                        @st.dialog(f"📤 Удалить «{fi['fname']}» из индекса?")
                        def _confirm_rmidx(fname=fi["fname"], fpath=fi["fpath"]):
                            st.info("Файл **останется в папке**, но его чанки будут удалены из векторной базы. Вы сможете переиндексировать его позже.")
                            ca, cb = st.columns(2)
                            with ca:
                                if st.button("📤 Да, удалить из индекса", type="primary", use_container_width=True, key=f"conf_rmidx_{fname}"):
                                    try:
                                        from core.indexer import remove_file_from_index
                                        remove_file_from_index(fname)
                                    except Exception:
                                        pass
                                    st.session_state.pop(f"_confirm_rmidx_{fname}", None)
                                    st.rerun()
                            with cb:
                                if st.button("← Отмена", use_container_width=True, key=f"cancel_rmidx_{fname}"):
                                    st.session_state.pop(f"_confirm_rmidx_{fname}", None)
                                    st.rerun()
                        _confirm_rmidx()

                    # Диалог: удалить файл полностью
                    if st.session_state.get(f"_confirm_del_{fi['fname']}"):
                        @st.dialog(f"🗑️ Удалить файл «{fi['fname']}»?")
                        def _confirm_delete(fpath=fi["fpath"], fname=fi["fname"]):
                            st.warning("Файл будет **удалён из папки** и из индекса чанков. Восстановить будет невозможно.")
                            ca, cb = st.columns(2)
                            with ca:
                                if st.button("🗑️ Да, удалить", type="primary", use_container_width=True, key=f"conf_del_{fname}"):
                                    try:
                                        from core.indexer import remove_file_from_index
                                        remove_file_from_index(fname)
                                    except Exception:
                                        pass
                                    os.remove(fpath)
                                    spheres_map.pop(fname, None)
                                    save_spheres_map(spheres_map)
                                    st.session_state.pop(f"_confirm_del_{fname}", None)
                                    st.rerun()
                            with cb:
                                if st.button("← Отмена", use_container_width=True, key=f"cancel_del_{fname}"):
                                    st.session_state.pop(f"_confirm_del_{fname}", None)
                                    st.rerun()
                        _confirm_delete()

                    st.divider()

            # ── Массовые операции (вертикальный стек) ───────────────────────
            st.divider()
            st.subheader("⚙️ Массовые операции")

            # 1. Переиндексировать категорию
            reindex_cat = st.selectbox(
                "Категория для переиндексации",
                list(CATEGORY_FOLDERS.keys()),
                key="reindex_cat_select"
            )
            if st.button("🚀 Переиндексировать категорию", type="primary", use_container_width=True, key="reindex_cat_btn"):
                with st.spinner("⏳ Индексация..."):
                    try:
                        from core.indexer import index_category
                        result = index_category(CATEGORY_FOLDERS[reindex_cat])
                        if result["status"] == "success":
                            st.success(f"✅ Проиндексировано файлов: {len(result['files'])}")
                        else:
                            st.error(f"❌ {result.get('message', '')}")
                    except Exception as e:
                        st.error(f"❌ {str(e)}")

            st.divider()

            # 2. Очистить весь индекс
            if st.button("🗑️ Очистить весь индекс", type="secondary", use_container_width=True, key="clear_index_btn"):
                st.session_state._confirm_clear_index = True

            if st.session_state.get("_confirm_clear_index"):
                @st.dialog("🗑️ Очистить весь индекс?")
                def _confirm_clear():
                    st.warning("Все чанки будут удалены из векторной базы. Файлы останутся на диске.")
                    ca, cb = st.columns(2)
                    with ca:
                        if st.button("🗑️ Да, очистить", type="primary", use_container_width=True, key="conf_clear_idx"):
                            try:
                                from core.indexer import clear_index
                                clear_index()
                            except Exception:
                                pass
                            st.session_state._confirm_clear_index = False
                            st.rerun()
                    with cb:
                        if st.button("← Отмена", use_container_width=True, key="cancel_clear_idx"):
                            st.session_state._confirm_clear_index = False
                            st.rerun()
                _confirm_clear()


        elif admin_subtab == "⚙️ Настройки чанкования":
            st.header("⚙️ Настройки чанкования документов")
            st.info("💡 Здесь можно настроить регулярные выражения для распознавания структуры документов")

            # ← КРИТИЧНО: Создаём папку и определяем путь ДО использования
            config_dir = os.path.join("config")
            os.makedirs(config_dir, exist_ok=True)
            config_file = os.path.join(config_dir, "chunking_patterns.json")

            # Загрузка текущей конфигурации
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            else:
                config = {
                    "patterns": {
                        "section": r"^(РАЗДЕЛ|ГЛАВА)\s+[IVX0-9]+",
                        "article": r"^(Статья|ст\.)\s+[0-9]+",
                        "paragraph": r"^(п\.|пункт)\s*[0-9.]+",
                        "subparagraph": r"^[0-9]+\.[0-9]+"
                    },
                    "doc_types": {
                        "фас": "fas_document",
                        "фз": "federal_law",
                        "приказ": "order",
                        "письмо": "letter",
                        "методич": "methodology"
                    },
                    "metadata_patterns": {
                        "doc_number": r"(\d+[А-Я]?-\d+[А-Я]?)",
                        "doc_date": r"(\d{2}\.\d{2}\.\d{4})",
                        "doc_year": r"(\d{4})"
                    },
                    "chunking_settings": {
                        "chunk_size": 500,
                        "chunk_overlap": 50,
                        "min_chunk_length": 100
                    }
                }

            # Вкладки для разных типов настроек
            tab1, tab2, tab3, tab4, tab5 = st.tabs([
                "📐 Структурные паттерны",
                "🏷️ Типы документов",
                "📋 Метаданные",
                "⚙️ Параметры чанкования",
                "🔍 Просмотр и тест чанков"
            ])

            with tab1:
                st.subheader("Структурные паттерны")
                st.caption("Регулярные выражения для поиска разделов, статей, пунктов")
                patterns = config.get("patterns", {})
                new_patterns = {}
                labels = {
                    "section": "Раздел/Глава",
                    "article": "Статья",
                    "paragraph": "Пункт",
                    "subparagraph": "Подпункт"
                }
                for key, label in labels.items():
                    new_patterns[key] = st.text_input(
                        f"{label} ({key})",
                        value=patterns.get(key, ""),
                        key=f"pattern_{key}",
                        help=f"Пример для {label}: ^(РАЗДЕЛ|ГЛАВА)\\s+[IVX0-9]+"
                    )
                if st.button("💾 Сохранить паттерны", key="save_patterns", use_container_width=True):
                    config["patterns"] = new_patterns
                    os.makedirs(os.path.dirname(config_file), exist_ok=True)
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(config, f, ensure_ascii=False, indent=2)
                    st.success("✅ Паттерны сохранены!")
                    st.info("🔄 Перезапустите индексацию для применения изменений")
                    st.rerun()

            with tab2:
                st.subheader("Типы документов")
                st.caption("Ключевые слова → тип документа")
                doc_types = config.get("doc_types", {})

                # Отображение существующих
                if doc_types:
                    st.write("Текущие соответствия:")
                    cols = st.columns(2)
                    for i, (keyword, doc_type) in enumerate(doc_types.items()):
                        with cols[i % 2]:
                            st.text(f"{keyword} → {doc_type}")
                    st.divider()

                # Добавление нового
                st.write("➕ Добавить новое соответствие:")
                col1, col2, col3 = st.columns(3)
                with col1:
                    new_keyword = st.text_input("Ключевое слово", key="new_keyword")
                with col2:
                    new_doc_type = st.text_input("Тип документа", key="new_doc_type")
                with col3:
                    if st.button("➕ Добавить", key="add_doc_type", use_container_width=True):
                        if new_keyword and new_doc_type:
                            doc_types[new_keyword] = new_doc_type
                            config["doc_types"] = doc_types
                            os.makedirs(os.path.dirname(config_file), exist_ok=True)
                            with open(config_file, 'w', encoding='utf-8') as f:
                                json.dump(config, f, ensure_ascii=False, indent=2)
                            st.success(f"✅ Добавлено: {new_keyword} → {new_doc_type}")
                            st.rerun()

                # Удаление
                st.divider()
                st.write("🗑️ Удалить соответствие:")
                if doc_types:
                    delete_keyword = st.selectbox("Выберите для удаления", list(doc_types.keys()), key="delete_doc_type")
                    if st.button("🗑️ Удалить", key="confirm_delete_doc_type", use_container_width=True):
                        del doc_types[delete_keyword]
                        config["doc_types"] = doc_types
                        os.makedirs(os.path.dirname(config_file), exist_ok=True)
                        with open(config_file, 'w', encoding='utf-8') as f:
                            json.dump(config, f, ensure_ascii=False, indent=2)
                        st.success(f"✅ Удалено: {delete_keyword}")
                        st.rerun()

            with tab3:
                st.subheader("Паттерны метаданных")
                st.caption("Извлечение номера, даты из имени файла")
                metadata_patterns = config.get("metadata_patterns", {})
                new_metadata = {}
                meta_labels = {
                    "doc_number": "Номер документа",
                    "doc_date": "Дата документа",
                    "doc_year": "Год"
                }
                for key, label in meta_labels.items():
                    new_metadata[key] = st.text_input(
                        f"{label} ({key})",
                        value=metadata_patterns.get(key, ""),
                        key=f"meta_{key}",
                        help="Пример: (\\d+[А-Я]?-\\d+[А-Я]?)"
                    )
                if st.button("💾 Сохранить метаданные", key="save_metadata", use_container_width=True):
                    config["metadata_patterns"] = new_metadata
                    os.makedirs(os.path.dirname(config_file), exist_ok=True)
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(config, f, ensure_ascii=False, indent=2)
                    st.success("✅ Метаданные сохранены!")
                    st.rerun()

            with tab4:
                st.subheader("Параметры чанкования")
                settings = config.get("chunking_settings", {})

                # ── Режим чанкования ─────────────────────────────────────────
                st.markdown("#### 🔀 Режим чанкования")
                chunking_mode = st.radio(
                    "Выберите способ разбивки документов на чанки",
                    options=["structural", "separator", "fixed"],
                    format_func=lambda x: {
                        "structural": "🧠 Умный (по структуре документа — разделы, статьи, пункты)",
                        "separator":  "✂️ По разделителю (указываете маркер конца чанка)",
                        "fixed":      "📏 Фиксированная длина (разбивка строго по количеству символов)",
                    }[x],
                    index=["structural", "separator", "fixed"].index(
                        settings.get("chunking_mode", "structural")
                    ),
                    key="chunking_mode_radio"
                )

                st.divider()

                # ── Параметры в зависимости от режима ───────────────────────
                separator = settings.get("separator", "&&")
                fixed_length = settings.get("fixed_chunk_length", 1000)
                min_chunk = settings.get("min_chunk_length", 50)
                max_chunk = settings.get("max_chunk_length", 2000)
                chunk_overlap = settings.get("chunk_overlap", 0)

                if chunking_mode == "structural":
                    st.info("🧠 Чанкер автоматически определяет границы по структуре документа: разделы, статьи, пункты, подпункты. Оптимально для НПА и методических документов.")
                    col1, col2 = st.columns(2)
                    with col1:
                        min_chunk = st.slider("Минимальная длина чанка (симв.)", 10, 500, settings.get("min_chunk_length", 50), key="min_chunk_s", help="Чанки короче будут пропущены")
                    with col2:
                        max_chunk = st.slider("Максимальная длина чанка (симв.)", 200, 5000, settings.get("max_chunk_length", 2000), key="max_chunk_s", help="При превышении чанк будет разбит по безопасной границе")

                elif chunking_mode == "separator":
                    st.info("✂️ Документ будет разбит по указанному маркеру. Разместите маркер в исходном документе там, где должен заканчиваться чанк.")
                    separator = st.text_input(
                        "Маркер конца чанка",
                        value=settings.get("separator", "&&"),
                        key="chunk_separator_input",
                        help="Например: && или ### или --- . Регистр важен."
                    )
                    st.caption(f"Пример: вставьте `{separator}` в конец нужного абзаца в документе — система разрежет там.")
                    col1, col2 = st.columns(2)
                    with col1:
                        min_chunk = st.slider("Минимальная длина чанка (симв.)", 10, 500, settings.get("min_chunk_length", 50), key="min_chunk_sep")
                    with col2:
                        max_chunk = st.slider("Максимальная длина чанка (симв.)", 200, 5000, settings.get("max_chunk_length", 2000), key="max_chunk_sep", help="Если чанк длиннее — будет разбит дополнительно")

                elif chunking_mode == "fixed":
                    st.info("📏 Документ разбивается строго по количеству символов. Простой и предсказуемый режим, но может разрезать предложения.")
                    fixed_length = st.slider(
                        "Длина чанка (символов)",
                        100, 5000,
                        settings.get("fixed_chunk_length", 1000),
                        step=50,
                        key="fixed_chunk_length_slider",
                        help="Каждый чанк будет содержать ровно столько символов (кроме последнего)"
                    )
                    st.caption(f"При длине документа 10 000 симв. получится ~{10000 // fixed_length} чанков")

                # ── Перекрытие — общее для всех режимов ─────────────────────
                st.divider()
                st.markdown("#### 🔁 Перекрытие между чанками")
                chunk_overlap = st.slider(
                    "Перекрытие (символов)",
                    0, 500,
                    settings.get("chunk_overlap", 0),
                    step=10,
                    key="chunk_overlap_slider",
                    help="Сколько символов из конца предыдущего чанка будет повторено в начале следующего. 0 = без перекрытия."
                )
                if chunk_overlap > 0:
                    st.caption(f"💡 Последние {chunk_overlap} симв. каждого чанка будут дублированы в начале следующего — улучшает поиск на границах смысловых блоков.")
                else:
                    st.caption("Перекрытие отключено.")

                if chunking_mode == "fixed" and chunk_overlap >= fixed_length:
                    st.error(f"⚠️ Перекрытие ({chunk_overlap}) не может быть больше длины чанка ({fixed_length})")

                # ── Сохранение ───────────────────────────────────────────────
                st.divider()
                if st.button("💾 Сохранить параметры", key="save_settings", use_container_width=True, type="primary"):
                    if chunking_mode == "fixed" and chunk_overlap >= fixed_length:
                        st.error("❌ Исправьте ошибки перед сохранением")
                    else:
                        config["chunking_settings"] = {
                            "chunking_mode": chunking_mode,
                            "separator": separator,
                            "fixed_chunk_length": fixed_length,
                            "min_chunk_length": min_chunk,
                            "max_chunk_length": max_chunk,
                            "chunk_overlap": chunk_overlap,
                        }
                        os.makedirs(os.path.dirname(config_file), exist_ok=True)
                        with open(config_file, 'w', encoding='utf-8') as f:
                            json.dump(config, f, ensure_ascii=False, indent=2)
                        st.success("✅ Параметры сохранены!")
                        st.warning("🔄 Примените изменения: переиндексируйте документы в разделе «Документы»")
                        st.rerun()

                st.divider()
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.caption(f"📁 Файл конфигурации: `{config_file}`")
                with col2:
                    if st.button("🔄 Сбросить к настройкам по умолчанию", key="reset_config", use_container_width=True):
                        if os.path.exists(config_file):
                            os.remove(config_file)
                        st.success("✅ Конфигурация удалена")
                        st.info("🔄 При следующем запуске будут использованы настройки по умолчанию")
                        st.rerun()

            with tab5:
                st.subheader("🔍 Просмотр и тест чанков")

                try:
                    from core.indexer import get_chunk_stats, get_chunks_by_file
                    import chromadb

                    # --- Статистика базы ---
                    stats = get_chunk_stats()
                    if stats["status"] == "success" and stats.get("total_chunks", 0) > 0:
                        col1, col2, col3 = st.columns(3)
                        col1.metric("Всего чанков", stats["total_chunks"])
                        col2.metric("Файлов в индексе", len(stats.get("doc_types", {})))
                        col3.metric("Категорий", len(stats.get("categories", {})))

                        doc_types = {k: v for k, v in stats.get("doc_types", {}).items() if v > 0}
                        if doc_types:
                            with st.expander("📊 Распределение по типам документов", expanded=False):
                                doc_df = pd.DataFrame({
                                    "Тип документа": list(doc_types.keys()),
                                    "Чанков": list(doc_types.values())
                                })
                                st.bar_chart(doc_df.set_index("Тип документа"))

                        categories = {k: v for k, v in stats.get("categories", {}).items() if v > 0}
                        if categories:
                            with st.expander("📂 Распределение по категориям", expanded=False):
                                cat_df = pd.DataFrame({
                                    "Категория": list(categories.keys()),
                                    "Чанков": list(categories.values())
                                })
                                st.bar_chart(cat_df.set_index("Категория"))
                    else:
                        st.warning("⚠️ Векторная база пуста — проиндексируйте документы")

                    st.divider()

                    # --- Тест-запрос (Вариант 5) ---
                    st.subheader("🧪 Тест-запрос к векторной базе")
                    st.caption("Введите запрос и посмотрите какие чанки вернёт система — без LLM, только поиск")

                    col1, col2 = st.columns([4, 1])
                    with col1:
                        test_query = st.text_input(
                            "Тестовый запрос",
                            placeholder="Например: расходы на ремонт основных средств",
                            key="test_query_input"
                        )
                    with col2:
                        test_top_k = st.number_input("Топ-K", min_value=1, max_value=20, value=5, key="test_top_k")

                    if st.button("🔎 Найти чанки", key="test_search_btn", type="primary", use_container_width=False):
                        if test_query.strip():
                            with st.spinner("Ищем релевантные чанки..."):
                                try:
                                    vector_db_path = os.path.join(
                                        os.path.dirname(os.path.abspath(__file__)), "data", "vector_db"
                                    )
                                    chroma_client = chromadb.PersistentClient(path=vector_db_path)
                                    collection = chroma_client.get_collection(name="tariff_docs")

                                    results = collection.query(
                                        query_texts=[test_query],
                                        n_results=int(test_top_k),
                                        include=["documents", "metadatas", "distances"]
                                    )

                                    docs = results["documents"][0]
                                    metas = results["metadatas"][0]
                                    distances = results["distances"][0]

                                    if docs:
                                        st.success(f"✅ Найдено {len(docs)} чанков")
                                        st.divider()

                                        for i, (doc, meta, dist) in enumerate(zip(docs, metas, distances), 1):
                                            # Переводим distance в score релевантности (0–100%)
                                            score = max(0, round((1 - dist) * 100, 1))
                                            score_color = "🟢" if score >= 70 else "🟡" if score >= 40 else "🔴"

                                            col_a, col_b = st.columns([5, 1])
                                            with col_a:
                                                st.markdown(f"**#{i} · {meta.get('filename', '—')}** · {meta.get('category', '—')}")
                                            with col_b:
                                                st.markdown(f"{score_color} **{score}%**")

                                            # Подсветка слов запроса в тексте чанка
                                            highlighted = doc
                                            for word in test_query.split():
                                                if len(word) > 3:
                                                    highlighted = highlighted.replace(
                                                        word, f"**{word}**"
                                                    )

                                            with st.expander(f"📄 Текст чанка #{i} ({len(doc)} симв.)", expanded=(i == 1)):
                                                st.markdown(highlighted[:1500] + ("..." if len(doc) > 1500 else ""))
                                                st.divider()
                                                meta_col1, meta_col2 = st.columns(2)
                                                meta_col1.caption(f"🗂 Тип: {meta.get('doc_type', '—')}")
                                                meta_col1.caption(f"📑 Структура: {meta.get('struct_type', '—')}")
                                                meta_col2.caption(f"📏 Дистанция: {round(dist, 4)}")
                                                meta_col2.caption(f"📄 Страница: {meta.get('page', '—')}")

                                            st.write("")
                                    else:
                                        st.warning("🔍 Ничего не найдено. Попробуйте другой запрос.")

                                except Exception as e:
                                    st.error(f"❌ Ошибка поиска: {type(e).__name__}: {str(e)}")
                        else:
                            st.warning("⚠️ Введите запрос")

                    st.divider()

                    # --- Просмотр чанков по файлам ---
                    st.subheader("📄 Чанки по файлам")
                    chunks_data = get_chunks_by_file(limit_per_file=5)
                    if chunks_data["status"] == "success" and chunks_data["files"]:
                        # Выбор файла через selectbox вместо рендера всего сразу
                        file_names = [f["filename"] for f in chunks_data["files"]]
                        selected_file = st.selectbox("Выберите файл", file_names, key="chunk_file_select")
                        file_info = next((f for f in chunks_data["files"] if f["filename"] == selected_file), None)

                        if file_info:
                            col1, col2, col3, col4 = st.columns(4)
                            col1.metric("Чанков", file_info["total_chunks"])
                            col2.write(f"**Тип:** {file_info.get('doc_type', '—')}")
                            col3.write(f"**Номер:** {file_info.get('doc_number', '—')}")
                            col4.write(f"**Дата:** {file_info.get('doc_date', '—')}")

                            st.caption(f"Показаны первые {len(file_info['chunks'])} из {file_info['total_chunks']} чанков:")
                            for chunk_idx, chunk in enumerate(file_info["chunks"], 1):
                                with st.expander(f"Чанк #{chunk_idx} · {len(chunk['content'])} симв.", expanded=False):
                                    st.code(chunk["content"][:1000] + ("..." if len(chunk["content"]) > 1000 else ""), language="text")
                                    meta = chunk["metadata"]
                                    meta_cols = st.columns(2)
                                    if meta.get("struct_type"):
                                        meta_cols[0].caption(f"Структура: {meta['struct_type']} → {meta.get('struct_text', '')}")
                                    if meta.get("article"):
                                        meta_cols[0].caption(f"Статья: {meta['article']}")
                                    if meta.get("paragraph"):
                                        meta_cols[1].caption(f"Пункт: {meta['paragraph']}")
                                    if meta.get("category"):
                                        meta_cols[1].caption(f"Категория: {meta['category']}")
                    else:
                        st.info("📭 Нет проиндексированных файлов")

                except Exception as e:
                    st.error(f"❌ Ошибка загрузки: {type(e).__name__}: {str(e)}")
                    st.info("💡 Убедитесь, что векторная база проиндексирована")

        elif admin_subtab == "📝 Промпты":
            st.header("📝 Управление промптами")
            st.info("💡 Изменения применяются сразу — без перезапуска приложения. Кэш LLM автоматически сбрасывается при сохранении.")

            PROMPTS_FILE = os.path.join("config", "prompts.json")

            DEFAULT_PROMPTS = {
                "advisor_system": (
                    "Ты — эксперт по тарифному регулированию в РФ.\n"
                    "Отвечай ТОЛЬКО на русском языке, кратко, структурно и по существу.\n"
                    "ЗАПРЕЩЕНО писать 'Thinking Process', рассуждения или объяснения шагов.\n"
                    "Отвечай сразу итоговым ответом: списком, таблицей или чётким утверждением.\n"
                    "Основывайся на предоставленном контексте и законодательстве РФ, преимущественно на RAG.\n"
                    "Если информации в базе знаний недостаточно — честно скажи об этом. Не выдумывай факты. "
                    "Всегда в конце благодари за интересный вопрос (или укажи, что вопрос был сложный)\n"
                    "Если в ответе есть сравнение данных, списки расходов, тарифные ставки или параметры, "
                    "сметы или расчеты — ОБЯЗАТЕЛЬНО оформи их в виде Markdown-таблицы.\n"
                    "Пример:\n| Параметр | Значение | Ед. изм. |\n|---|---|---|\n| Тариф | 100.50 | руб./Гкал |"
                ),
                "advisor_user": (
                    "Вопрос пользователя: {query}\n\n"
                    "Контекст из документов:\n{context}\n\n"
                    "Ответ:"
                ),
            }

            # Загрузка текущих промптов
            if os.path.exists(PROMPTS_FILE):
                try:
                    with open(PROMPTS_FILE, 'r', encoding='utf-8') as f:
                        current_prompts = {**DEFAULT_PROMPTS, **json.load(f)}
                except Exception:
                    current_prompts = dict(DEFAULT_PROMPTS)
            else:
                current_prompts = dict(DEFAULT_PROMPTS)

            # --- Советчик ---
            st.subheader("🤝 Советчик")

            with st.expander("ℹ️ Доступные переменные", expanded=False):
                st.markdown("""
**Системный промпт** — переменных нет, это инструкция для роли ИИ.

**Пользовательский промпт** — обязательные переменные:
- `{query}` — вопрос пользователя
- `{context}` — найденные чанки из векторной базы
                """)

            col1, col2 = st.columns([1, 1])
            with col1:
                st.caption("📊 Текущий промпт загружен из: " + ("📁 prompts.json" if os.path.exists(PROMPTS_FILE) else "⚙️ дефолтных настроек"))
            with col2:
                is_modified = current_prompts.get("advisor_system") != DEFAULT_PROMPTS["advisor_system"] or \
                              current_prompts.get("advisor_user") != DEFAULT_PROMPTS["advisor_user"]
                if is_modified:
                    st.warning("✏️ Промпты изменены относительно дефолтных")
                else:
                    st.success("✅ Используются дефолтные промпты")

            st.divider()

            new_system = st.text_area(
                "🧠 Системный промпт (роль и правила ИИ)",
                value=current_prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"]),
                height=280,
                key="prompt_advisor_system",
                help="Определяет поведение, тон и формат ответов ИИ"
            )

            st.divider()

            new_user = st.text_area(
                "💬 Шаблон пользовательского промпта",
                value=current_prompts.get("advisor_user", DEFAULT_PROMPTS["advisor_user"]),
                height=120,
                key="prompt_advisor_user",
                help="Шаблон запроса к LLM. Обязательно используйте {query} и {context}"
            )

            # Валидация переменных
            if "{query}" not in new_user or "{context}" not in new_user:
                st.error("⚠️ Пользовательский промпт должен содержать {query} и {context}")
            else:
                st.caption("✅ Переменные {query} и {context} присутствуют")

            st.divider()
            col1, col2, col3 = st.columns([2, 2, 1])

            with col1:
                if st.button("💾 Сохранить промпты", type="primary", use_container_width=True, key="save_prompts_btn"):
                    if "{query}" in new_user and "{context}" in new_user:
                        os.makedirs(os.path.dirname(PROMPTS_FILE), exist_ok=True)
                        prompts_to_save = {
                            **current_prompts,
                            "advisor_system": new_system,
                            "advisor_user": new_user,
                            "updated_at": datetime.now().isoformat()
                        }
                        with open(PROMPTS_FILE, 'w', encoding='utf-8') as f:
                            json.dump(prompts_to_save, f, ensure_ascii=False, indent=2)
                        # Сбрасываем кэш LLM, чтобы старые ответы не применялись с новым промптом
                        try:
                            from core.advisor import _llm_cache, save_llm_cache
                            _llm_cache.clear()
                            save_llm_cache()
                            st.success("✅ Промпты сохранены. Кэш LLM сброшен — изменения активны.")
                        except Exception:
                            st.success("✅ Промпты сохранены.")
                        st.rerun()
                    else:
                        st.error("❌ Исправьте ошибки перед сохранением")

            with col2:
                if st.button("🔄 Сбросить к дефолтным", use_container_width=True, key="reset_prompts_btn"):
                    st.session_state._confirm_reset_prompts = True

                # Диалог подтверждения сброса
                @st.dialog("⚠️ Сброс промптов")
                def confirm_reset_prompts_dialog():
                    st.warning("**Все изменения будут сброшены** — системный и пользовательский промпты вернутся к дефолтным значениям.")
                    st.caption("Это действие нельзя отменить. Рекомендуем сначала скачать текущий вариант.")

                    # Кнопка скачать прямо в диалоге
                    backup_json = json.dumps({
                        "advisor_system": new_system,
                        "advisor_user": new_user,
                        "saved_at": datetime.now().isoformat()
                    }, ensure_ascii=False, indent=2)
                    st.download_button(
                        "📥 Скачать текущий промпт перед сбросом",
                        data=backup_json.encode("utf-8"),
                        file_name=f"prompts_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                        mime="application/json",
                        use_container_width=True,
                        key="dialog_download_prompts_btn"
                    )

                    st.divider()
                    col_a, col_b = st.columns(2)
                    with col_a:
                        if st.button("🗑️ Да, сбросить", type="primary", use_container_width=True, key="dialog_confirm_reset"):
                            if os.path.exists(PROMPTS_FILE):
                                try:
                                    with open(PROMPTS_FILE, 'r', encoding='utf-8') as f:
                                        saved = json.load(f)
                                    saved.pop("advisor_system", None)
                                    saved.pop("advisor_user", None)
                                    with open(PROMPTS_FILE, 'w', encoding='utf-8') as f:
                                        json.dump(saved, f, ensure_ascii=False, indent=2)
                                except Exception:
                                    pass
                            st.session_state._confirm_reset_prompts = False
                            st.rerun()
                    with col_b:
                        if st.button("← Отмена", use_container_width=True, key="dialog_cancel_reset"):
                            st.session_state._confirm_reset_prompts = False
                            st.rerun()

                if st.session_state.get("_confirm_reset_prompts"):
                    confirm_reset_prompts_dialog()

            with col3:
                # Кнопка скачать текущие промпты как JSON
                prompts_json = json.dumps({
                    "advisor_system": new_system,
                    "advisor_user": new_user
                }, ensure_ascii=False, indent=2)
                st.download_button(
                    "📥 Скачать",
                    data=prompts_json.encode("utf-8"),
                    file_name="prompts_backup.json",
                    mime="application/json",
                    use_container_width=True,
                    key="download_prompts_btn"
                )

            # --- Предпросмотр ---
            st.divider()
            with st.expander("🔍 Предпросмотр промпта с тестовыми данными", expanded=False):
                test_q = st.text_input("Тестовый вопрос", value="Какие расходы на ремонт можно включать в тариф?", key="prompt_preview_q")
                test_ctx = st.text_area("Тестовый контекст (имитация чанка)", value="[1] нпа_123.txt (стр. 5): Расходы на ремонт основных средств включаются в тариф при наличии дефектной ведомости...", height=80, key="prompt_preview_ctx")
                if st.button("👁️ Показать итоговый промпт", key="preview_prompt_btn"):
                    try:
                        rendered = new_user.format(query=test_q, context=test_ctx)
                        st.markdown("**Системный промпт:**")
                        st.code(new_system, language="text")
                        st.markdown("**Пользовательский промпт (после подстановки):**")
                        st.code(rendered, language="text")
                    except KeyError as e:
                        st.error(f"❌ Неизвестная переменная в промпте: {e}")

        elif admin_subtab == "📝 Отзывы":
            st.header("📝 Отзывы пользователей")
            feedbacks = get_feedback(limit=200)
            if feedbacks:
                for fb in feedbacks:
                    rating_icon = {3: "👍", 2: "😐", 1: "👎"}.get(fb.get("rating"), "📝")
                    with st.expander(f"{rating_icon} {fb['id']} — {fb['timestamp'][:10]} — {fb['feedback_type']}"):
                        if fb.get("question"):
                            st.write(f"**Вопрос:** {fb['question']}")
                        st.write(f"**Комментарий:** {fb.get('description', '—')}")
            else:
                st.info("Нет отзывов")

        elif admin_subtab == "⚙️ Настройки":
            st.header("⚙️ Настройки системы")
            st.info("Здесь будут настройки порога FAQ, модели, параметров поиска...")

# =============================================================================
# 🚀 Запуск
# =============================================================================
if __name__ == "__main__":
    pass