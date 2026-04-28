# streamlit_pages/court_precedents.py
import streamlit as st
import os
import json
from datetime import datetime

# =============================================================================
# 📊 Загрузка промпта
# =============================================================================

def load_court_prompt() -> str:
    """Загружает промпт для поиска прецедентов"""
    prompt_file = os.path.join("prompts", "court_search.txt")
    
    if not os.path.exists(prompt_file):
        return """Ты — эксперт по судебной практике в сфере тарифного регулирования в РФ.
Найди релевантные судебные решения по запросу и кратко изложи суть прецедента.
Укажи: номер дела, дата, суд, уровень юрисдикции, суть спора, решение, применимость.
⚠️ Информация носит справочный характер.

КОНТЕКСТ: {context}
ЗАПРОС: {question}
ОТВЕТ:"""
    
    with open(prompt_file, "r", encoding="utf-8") as f:
        return f.read()

# =============================================================================
# 🤖 Генерация ответа через Ollama
# =============================================================================

def generate_court_answer(question: str, context: str) -> str:
    """Генерирует ответ по судебной практике через Ollama + Llama 3"""
    try:
        import ollama
        
        prompt_template = load_court_prompt()
        prompt = prompt_template.format(context=context, question=question)
        
        response = ollama.chat(
            model='llama3',
            messages=[
                {'role': 'system', 'content': 'Ты эксперт по судебной практике в тарифном регулировании. Отвечай строго по документам.'},
                {'role': 'user', 'content': prompt}
            ],
            options={
                'temperature': 0.1,
                'top_p': 0.9,
                'num_predict': 800,
                'repeat_penalty': 1.2
            }
        )
        
        return response['message']['content'].strip()
        
    except Exception as e:
        return f"⚠️ Ошибка генерации: {e}"

# =============================================================================
# 🔍 Поиск по базе судебных решений
# =============================================================================

def search_court_precedents(query: str, filters: dict, top_k: int = 5) -> list:
    """Ищет документы в папке data/raw/court/"""
    court_dir = os.path.join("data", "raw", "court")
    
    results = []
    
    # Заглушка: возвращаем демо-данные, если нет реальной базы
    if not os.path.exists(court_dir):
        return [
            {
                "case_number": "А40-12345/2023",
                "date": "15.03.2024",
                "court": "Арбитражный суд города Москвы",
                "jurisdiction": "Арбитражный суд субъекта",
                "sector": "электросети",
                "essence": "Спор о правомерности включения расходов на ДМС в тариф при превышении 6% от ФОТ",
                "decision": "Суд признал действия регулятора правомерными, расходы исключены из тарифа",
                "relevance": 0.92,
                "link": "https://kad.arbitr.ru/Card/12345"
            },
            {
                "case_number": "А41-67890/2022",
                "date": "22.11.2023",
                "court": "Арбитражный суд Московской области",
                "jurisdiction": "Арбитражный апелляционный суд",
                "sector": "теплосети",
                "essence": "Оспаривание снижения тарифа из-за завышения численности персонала",
                "decision": "Апелляция оставила решение первой инстанции без изменения",
                "relevance": 0.87,
                "link": "https://kad.arbitr.ru/Card/67890"
            }
        ]
    
    # В будущем: здесь будет RAG-поиск по проиндексированным судебным решениям
    # с учётом фильтров: sector, jurisdiction, date_range, etc.
    
    return results

# =============================================================================
# 🎨 Интерфейс Streamlit
# =============================================================================

def show_court_precedents():
    """Страница поиска судебных прецедентов"""
    
    st.header("🔍 Поиск прецедентов за 3 клика")
    st.info("📌 Найдите судебные решения и практику ФАС по аналогичным тарифным спорам")
    
    # ─────────────────────────────────────────────────────────────────────
    # 📁 Инфо о базе судебных решений
    # ─────────────────────────────────────────────────────────────────────
    court_dir = os.path.join("data", "raw", "court")
    
    with st.expander("📚 База судебных решений", expanded=False):
        st.write(f"**Путь к папке:** `{court_dir}`")
        
        if os.path.exists(court_dir):
            subdirs = [d for d in os.listdir(court_dir) if os.path.isdir(os.path.join(court_dir, d))]
            st.write(f"**Категории:** {', '.join(subdirs) if subdirs else 'пусто'}")
        else:
            st.warning("⚠️ Папка не найдена. Создайте `data/raw/court/` для загрузки судебных решений.")
        
        st.caption("💡 Поддерживаемые форматы: PDF, DOCX, TXT. В v2.0 — авто-обновление через API Консультант+/Гарант")
    
    # ─────────────────────────────────────────────────────────────────────
    # 🔎 Шаг 1: Выбор сферы (мультивыбор)
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1️⃣ Выберите сферу")
    
    sectors = st.multiselect(
        "Отрасль",
        ["электросети", "теплосети", "водоканал", "газоснабжение", "утилизация ТКО", "иное"],
        default=["электросети", "теплосети", "водоканал"]
    )
    
    # ─────────────────────────────────────────────────────────────────────
    # 🔎 Шаг 2: Фильтры + вопрос
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("2️⃣ Уточните поиск")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        jurisdiction = st.multiselect(
            "Уровень юрисдикции",
            ["Арбитражный суд субъекта", "Арбитражный апелляционный", "Арбитражный суд округа", "ВС РФ", "ФАС"]
        )
    with col2:
        date_from = st.date_input("С даты", value=datetime(2020, 1, 1))
    with col3:
        date_to = st.date_input("По дату", value=datetime.now())
    
    query = st.text_area(
        "Ваш запрос",
        height=80,
        placeholder="Например: правомерно ли исключение расходов на ДМС из тарифа при превышении норматива?",
        key="court_query_input"
    )
    
    # ─────────────────────────────────────────────────────────────────────
    # 🔍 Шаг 3: Поиск и результаты
    # ─────────────────────────────────────────────────────────────────────
    if st.button("🔍 Найти прецеденты", type="primary", use_container_width=True):
        if query.strip():
            with st.spinner("🔄 Ищем в базе судебных решений..."):
                filters = {
                    "sectors": sectors,
                    "jurisdiction": jurisdiction,
                    "date_from": date_from,
                    "date_to": date_to
                }
                
                results = search_court_precedents(query, filters)
                
                st.divider()
                st.subheader(f"3️⃣ Найдено: {len(results)} прецедент(ов)")
                
                if results:
                    for i, res in enumerate(results, 1):
                        with st.expander(f"📄 {i}. {res['case_number']} — {res['court']}", expanded=True):
                            st.markdown(f"**Дата:** {res['date']}")
                            st.markdown(f"**Уровень:** {res['jurisdiction']}")
                            st.markdown(f"**Сфера:** {res['sector']}")
                            st.markdown(f"**Суть спора:** {res['essence']}")
                            st.markdown(f"**Решение:** {res['decision']}")
                            st.markdown(f"**🔗 [Открыть дело]({res['link']})**")
                    
                    # Генерация сводного ответа
                    st.divider()
                    st.subheader("📝 Сводный анализ")
                    
                    context = "\n---\n".join([f"{r['case_number']}: {r['essence']} — {r['decision']}" for r in results])
                    answer = generate_court_answer(query, context)
                    
                    st.markdown(answer)
                    st.warning("⚠️ Информация носит справочный характер. Для юридической консультации обратитесь к профильному юристу")
                    
                    # Экспорт
                    st.download_button(
                        "📤 Скачать анализ в TXT",
                        data=answer,
                        file_name=f"CourtAnalysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                        mime="text/plain",
                        use_container_width=True
                    )
                else:
                    st.info("📭 По вашему запросу не найдено релевантных прецедентов. Попробуйте изменить фильтры.")
        else:
            st.warning("⚠️ Введите запрос")
    
    # ─────────────────────────────────────────────────────────────────────
    # 💡 Примеры запросов
    # ─────────────────────────────────────────────────────────────────────
    st.divider()
    with st.expander("💡 Примеры запросов", expanded=False):
        st.write("• Правомерно ли исключение расходов на ДМС из тарифа при превышении 6% от ФОТ?")
        st.write("• Может ли регулятор снизить тариф из-за завышения численности персонала?")
        st.write("• Как суды трактуют расходы на программное обеспечение в тарифе?")
        st.write("• Можно ли оспорить отказ в включении инвестиционных расходов?")

# =============================================================================
# 🚀 Запуск
# =============================================================================

if __name__ == "__main__":
    show_court_precedents()