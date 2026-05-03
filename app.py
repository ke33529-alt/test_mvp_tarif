import streamlit as st
import os
import sys
import pandas as pd
from datetime import datetime
import json

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
</style>
""", unsafe_allow_html=True)

# =============================================================================
#  Боковое меню
# =============================================================================
with st.sidebar:
    st.title("🧭 Меню")
    st.subheader("📌 Доступно (21/21)")
    main_choice = st.radio(
        "Раздел",
        [
            "🔍 Анализатор заявок",
            "🤝 Советчик",
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
            "📸 AI-Сканер документов",
            "📋 Робот-протокольщик",
            "🔮 Предсказание решения регулятора",
            "🔮 Прогнозист тарифов",
            "🌐 Сравнение с аналогами в регионе",
            "🎓 Режим обучения для новичков",
            "🗂️ Наведение порядка в документах",
            "🗓️ Планировщик тарифной кампании",
            "📊 Прогноз потребления",
            "🛠 Админка"
        ],
        index=0
    )
    st.divider()
    st.subheader("⚙️ В разработке")
    st.caption("🚧 Ведутся работы по внедрению")
    st.divider()
    if is_admin_logged():
        st.success("🔓 Админка: вход выполнен")
        if st.button("🚪 Выйти"):
            st.session_state.admin_logged_in = False
            st.rerun()

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
        st.session_state.advisor_model = "phi3"  # Модель по умолчанию

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
                    model_names = [m["name"] for m in available_models] if available_models else ["phi3", "llama3.2"]
                    selected_model = st.selectbox(
                        "🤖 Модель для ответов",
                        options=model_names,
                        index=model_names.index(st.session_state.advisor_model) if st.session_state.advisor_model in model_names else 0,
                        key="advisor_model_select",
                        help="phi3 быстрее для 4GB VRAM, llama3.2 качественнее но требует больше памяти"
                    )
                    st.session_state.advisor_model = selected_model
                    st.caption(f"✅ Доступные модели: {', '.join(model_names)}")
                except:
                    st.session_state.advisor_model = st.selectbox(
                        "🤖 Модель для ответов",
                        options=["phi3", "llama3.2"],
                        index=0,
                        key="advisor_model_select"
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
            ["📈 Аналитика ИИ", "📚 Документы", "📊 Просмотр чанков", "⚙️ Настройки чанкования", "📝 Отзывы", "⚙️ Настройки"],
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
            st.header("📚 Управление документами базы знаний")
            doc_category = st.selectbox(
                "Категория документов",
                ["📜 Общие НПА", "⚖️ Документы ФАС", "🏛️ Судебная практика", "📋 Методички и разъяснения"],
                key="doc_cat_select"
            )
            category_folders = {
                "📜 Общие НПА": "npa",
                "⚖️ Документы ФАС": "fas",
                "🏛️ Судебная практика": "court",
                "📋 Методички и разъяснения": "methodics"
            }
            selected_folder = category_folders.get(doc_category, "npa")
            base_path = os.path.join("data", "raw", selected_folder)
            st.info(f"📁 Папка: `{base_path}`")

            if not os.path.exists(base_path):
                os.makedirs(base_path, exist_ok=True)
                st.success(f"✅ Папка создана: {base_path}")

            st.subheader("📄 Файлы в категории")
            if os.path.exists(base_path):
                files = os.listdir(base_path)
                if files:
                    for f in files:
                        file_path = os.path.join(base_path, f)
                        file_size = os.path.getsize(file_path)
                        col1, col2, col3 = st.columns([4, 1, 1])
                        with col1:
                            st.write(f"📄 {f} ({file_size / 1024:.1f} КБ)")
                        with col2:
                            if st.button("🗑 Удалить", key=f"del_{f}_{selected_folder}"):
                                os.remove(file_path)
                                st.success(f"✅ {f} удалён")
                                st.rerun()
                        with col3:
                            if st.button("🔄 Индексировать", key=f"reindex_{f}_{selected_folder}"):
                                try:
                                    from core.indexer import index_file
                                    result = index_file(file_path, selected_folder)
                                    if result["status"] == "success":
                                        st.success(f"✅ {f}: {result.get('chunks', 0)} чанков")
                                    else:
                                        st.error(f"❌ {result.get('message', '')}")
                                except Exception as e:
                                    st.error(f"❌ Ошибка: {str(e)}")
                else:
                    st.info("📭 В этой категории пока нет файлов")

            st.divider()
            col1, col2 = st.columns(2)
            with col1:
                if st.button("🚀 Переиндексировать категорию", type="primary", use_container_width=True):
                    with st.spinner("⏳ Индексация..."):
                        try:
                            from core.indexer import index_category
                            result = index_category(selected_folder)
                            if result["status"] == "success":
                                st.success(f"✅ Проиндексировано файлов: {len(result['files'])}")
                            else:
                                st.error(f"❌ Ошибка: {result.get('message', '')}")
                        except Exception as e:
                            st.error(f"❌ Ошибка: {str(e)}")
            with col2:
                if st.button("🗑️ Очистить весь индекс", type="secondary", use_container_width=True):
                    try:
                        from core.indexer import clear_index
                        result = clear_index()
                        if result["status"] == "success":
                            st.success("✅ Индекс очищен")
                        else:
                            st.error(f"❌ Ошибка: {result.get('message', '')}")
                    except Exception as e:
                        st.error(f"❌ Ошибка: {str(e)}")

        elif admin_subtab == "📊 Просмотр чанков":
            st.header("📊 Просмотр чанков векторной базы")
            # Общая статистика
            try:
                from core.indexer import get_chunk_stats, get_chunks_by_file
                stats = get_chunk_stats()
                if stats["status"] == "success":
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Всего чанков", stats["total_chunks"])
                    col2.metric("Файлов в индексе", len(stats.get("doc_types", {})))
                    col3.metric("Категорий", len(stats.get("categories", {})))

                    # Распределение по типам документов
                    if stats.get("doc_types"):
                        st.subheader("📁 По типам документов")
                        doc_df = pd.DataFrame({
                            "Тип документа": list(stats["doc_types"].keys()),
                            "Чанков": list(stats["doc_types"].values())
                        })
                        st.bar_chart(doc_df.set_index("Тип документа"))

                    # Распределение по категориям
                    if stats.get("categories"):
                        st.subheader("📂 По категориям")
                        cat_df = pd.DataFrame({
                            "Категория": list(stats["categories"].keys()),
                            "Чанков": list(stats["categories"].values())
                        })
                        st.bar_chart(cat_df.set_index("Категория"))

                    # Список файлов с чанками
                    st.divider()
                    st.subheader("📄 Файлы и их чанки")
                    chunks_data = get_chunks_by_file(limit_per_file=5)
                    if chunks_data["status"] == "success":
                        for file_idx, file_info in enumerate(chunks_data["files"]):
                            file_key = f"file_{file_idx}"
                            st.markdown(f"### 📁 {file_info['filename']}")
                            # Метаданные файла
                            col1, col2, col3, col4 = st.columns(4)
                            col1.metric("Чанков", file_info['total_chunks'])
                            col2.write(f"**Тип:** {file_info.get('doc_type', '—')}")
                            col3.write(f"**Номер:** {file_info.get('doc_number', '—')}")
                            col4.write(f"**Дата:** {file_info.get('doc_date', '—')}")
                            st.divider()

                            # Список чанков (без вложенных expanders!)
                            st.caption(f"Показано первые {len(file_info['chunks'])} из {file_info['total_chunks']} чанков:")
                            for chunk_idx, chunk in enumerate(file_info["chunks"], 1):
                                chunk_key = f"{file_key}_chunk_{chunk_idx}"
                                with st.container():
                                    show_chunk = st.checkbox(f"Чанк #{chunk_idx}", key=f"{chunk_key}_toggle", value=False)
                                    if show_chunk:
                                        st.markdown("**Содержимое:**")
                                        st.code(chunk["content"][:1000] + ("..." if len(chunk["content"]) > 1000 else ""), language="text")
                                        st.markdown("**Метаданные:**")
                                        meta = chunk["metadata"]
                                        meta_cols = st.columns(2)
                                        if meta.get("struct_type"):
                                            meta_cols[0].write(f"- Структура: {meta['struct_type']} → {meta.get('struct_text', '')}")
                                        if meta.get("article"):
                                            meta_cols[0].write(f"- Статья: {meta['article']}")
                                        if meta.get("paragraph"):
                                            meta_cols[1].write(f"- Пункт: {meta['paragraph']}")
                                        if meta.get("category"):
                                            meta_cols[1].write(f"- Категория: {meta['category']}")
                                        st.divider()
                    else:
                        st.error(f"❌ Ошибка: {chunks_data.get('message', '')}")
            except Exception as e:
                st.error(f"❌ Ошибка загрузки: {type(e).__name__}: {str(e)}")
                st.info("💡 Убедитесь, что векторная база проиндексирована")

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
            tab1, tab2, tab3, tab4 = st.tabs([
                "📐 Структурные паттерны",
                "🏷️ Типы документов",
                "📋 Метаданные",
                "⚙️ Параметры чанкования"
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
                st.caption("Настройки разделителя и размеров чанков")
                settings = config.get("chunking_settings", {})

                # ← ГЛАВНОЕ: Разделитель чанков
                separator = st.text_input(
                    "📍 Разделитель чанков",
                    value=settings.get("separator", "###"),
                    key="chunk_separator",
                    help="Символ или текст, который разделяет чанки в документе. Например: ### или & или -"
                )
                st.info(f"💡 Пример использования: Разместите '{separator}' между смысловыми блоками в документе")

                st.divider()
                min_chunk = st.slider(
                    "Минимальная длина чанка (символов)",
                    10, 500,
                    settings.get("min_chunk_length", 50),
                    key="min_chunk",
                    help="Чанки короче этого значения будут пропущены"
                )
                max_chunk = st.slider(
                    "Максимальная длина чанка (символов)",
                    500, 5000,
                    settings.get("max_chunk_length", 2000),
                    key="max_chunk",
                    help="Если чанк длиннее — система найдёт безопасную границу (точка, запятая, перенос)"
                )
                st.divider()

                if st.button("💾 Сохранить параметры", key="save_settings", use_container_width=True, type="primary"):
                    config["chunking_settings"] = {
                        "separator": separator,
                        "min_chunk_length": min_chunk,
                        "max_chunk_length": max_chunk,
                        "chunk_overlap": 0
                    }
                    os.makedirs(os.path.dirname(config_file), exist_ok=True)
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(config, f, ensure_ascii=False, indent=2)
                    st.success("✅ Параметры сохранены!")
                    st.info("🔄 Перезапустите индексацию для применения изменений")
                    st.rerun()

                # Кнопка сброса
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
