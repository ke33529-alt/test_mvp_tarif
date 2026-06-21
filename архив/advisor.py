# streamlit_pages/advisor.py
import streamlit as st
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.advisor import ask_question, get_available_models

def show_advisor():
    """Страница Советчика с выбором модели"""
    st.header("Советчик по нормативной базе")
    st.info("Задайте вопрос по тарифному регулированию — система ответит со ссылками на актуальные НПА")

    # Инициализация session_state
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "advisor_model" not in st.session_state:
        st.session_state.advisor_model = "phi3"  # Модель по умолчанию
    if "query_times" not in st.session_state:
        st.session_state.query_times = []

    # ─────────────────────────────────────────────────────────────────────
    # Настройки: Выбор модели и параметры
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("Настройки", expanded=False):
        # ✅ Выбор модели
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
        
        st.divider()
        
        # Параметры поиска
        col1, col2 = st.columns(2)
        with col1:
            top_k = st.slider("Количество источников", 1, 10, 5, key="advisor_top_k")
        with col2:
            temperature = st.slider("Креативность ответа", 0.0, 1.0, 0.3, 0.1, key="advisor_temp")
        
        # Кнопка очистки кэша
        if st.button("🗑 Очистить кэш LLM", use_container_width=True):
            from core.advisor import _llm_cache, save_llm_cache
            _llm_cache.clear()
            save_llm_cache()
            st.session_state.query_times = []
            st.success("✅ Кэш очищен")
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

    # ─────────────────────────────────────────────────────────────────────
    # История чата
    # ─────────────────────────────────────────────────────────────────────
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])
            
            # Если это ответ ассистента — показать модель и источники
            if msg["role"] == "assistant" and "model" in msg:
                st.caption(f"🤖 Модель: {msg['model']}")
            
            if msg["role"] == "assistant" and "sources" in msg and msg["sources"]:
                with st.expander("📚 Источники"):
                    for i, src in enumerate(msg["sources"], 1):
                        st.write(f"**{src.get('file', 'Неизвестно')}** (стр. {src.get('page', '')}): {src.get('snippet', '')[:200]}...")

    # ─────────────────────────────────────────────────────────────────────
    # Ввод вопроса
    # ─────────────────────────────────────────────────────────────────────
    if prompt := st.chat_input("Например: Что такое НВВ?"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.write(prompt)
        
        with st.chat_message("assistant"):
            with st.spinner("🤖 ИИ думает..."):
                start_time = __import__("datetime").datetime.now()
                
                # ✅ Передаём выбранную модель в ask_question
                result = ask_question(
                    prompt,
                    top_k=st.session_state.get("advisor_top_k", 5),
                    temperature=st.session_state.get("advisor_temp", 0.3),
                    model=st.session_state.advisor_model  # ✅ Выбор модели
                )
                
                # Замер времени
                end_time = __import__("datetime").datetime.now()
                query_time = (end_time - start_time).total_seconds()
                st.session_state.query_times.append(query_time)
                if len(st.session_state.query_times) > 10:
                    st.session_state.query_times = st.session_state.query_times[-10:]
                
                # Показ перенаправления
                if result.get("redirect"):
                    st.warning(result["redirect_reason"])
                
                # Показ ответа
                st.write(result["answer"])
                
                # Показ источников
                if result.get("sources"):
                    with st.expander("📚 Источники"):
                        for src in result["sources"]:
                            st.write(f"**{src.get('file', 'Неизвестно')}** (стр. {src.get('page', '')}): {src.get('snippet', '')[:200]}...")
                
                # Сохраняем ответ в историю с моделью
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": result["answer"],
                    "model": st.session_state.advisor_model,  # ✅ Сохраняем модель
                    "sources": result.get("sources", [])
                })
                
                # Статус кэша
                if result.get("from_cache"):
                    st.info("⚡ Ответ из кэша (мгновенно)")
                elif result.get("from_faq"):
                    st.success("✅ Ответ из базы частых вопросов")

    # ─────────────────────────────────────────────────────────────────────
    # Боковая панель: Управление историей
    # ─────────────────────────────────────────────────────────────────────
    if st.sidebar.button("🗑️ Очистить историю"):
        st.session_state.messages = []
        st.session_state.query_times = []
        st.rerun()
    
    # Информация о текущей модели
    st.sidebar.divider()
    st.sidebar.caption(f"🤖 Текущая модель: **{st.session_state.advisor_model}**")
    
    # Статус векторной базы
    vector_db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vector_db")
    db_file = os.path.join(vector_db_path, "chroma.sqlite3")
    if os.path.exists(db_file):
        st.sidebar.success("✅ Векторная база подключена")
    else:
        st.sidebar.warning("⚠️ Векторная база не найдена")

if __name__ == "__main__":
    show_advisor()
    show_advisor()