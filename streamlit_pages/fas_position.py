# streamlit_pages/fas_position.py
import streamlit as st
import os
import json
from datetime import datetime

# =============================================================================
# 📊 Загрузка промпта из файла
# =============================================================================

def load_fas_prompt() -> str:
    """Загружает промпт для режима ФАС из файла"""
    prompt_file = os.path.join("prompts", "advisor_fas_mode.txt")
    
    if not os.path.exists(prompt_file):
        return """Ты — эксперт по тарифному регулированию в РФ с опытом работы в ФАС России.
Дай ответ в официальном стиле ФАС — строго, со ссылками на НПА.
Если информации нет — прямо скажи об этом.
⚠️ Ответ носит справочный характер и не является официальным заключением ФАС России.

КОНТЕКСТ: {context}
ВОПРОС: {question}
ОТВЕТ:"""
    
    with open(prompt_file, "r", encoding="utf-8") as f:
        return f.read()

# =============================================================================
# 🤖 Генерация ответа через Ollama
# =============================================================================

def generate_fas_answer(question: str, context: str) -> str:
    """Генерирует ответ в стиле ФАС через Ollama + Llama 3"""
    try:
        import ollama
        
        prompt_template = load_fas_prompt()
        prompt = prompt_template.format(context=context, question=question)
        
        response = ollama.chat(
            model='llama3',
            messages=[
                {'role': 'system', 'content': 'Ты эксперт ФАС России. Отвечай строго, со ссылками на НПА, без допущений.'},
                {'role': 'user', 'content': prompt}
            ],
            options={
                'temperature': 0.1,  # Минимальная креативность для детерминированности
                'top_p': 0.9,
                'num_predict': 800,
                'repeat_penalty': 1.2
            }
        )
        
        return response['message']['content'].strip()
        
    except Exception as e:
        return f"⚠️ Ошибка генерации: {e}"

# =============================================================================
# 🔍 Поиск по базе документов ФАС
# =============================================================================

def search_fas_documents(query: str, top_k: int = 5) -> list:
    """Ищет документы в папке data/raw/fas/"""
    fas_dir = os.path.join("data", "raw", "fas")
    
    if not os.path.exists(fas_dir):
        return []
    
    # Заглушка: возвращаем пустой список, если нет векторной базы для ФАС
    # В будущем здесь будет RAG-поиск по проиндексированным документам ФАС
    context_parts = []
    
    # Временная заглушка для демонстрации
    context_parts.append(f"📄 Поиск по документам ФАС для запроса: {query}")
    context_parts.append(f"📁 Папка документов: {fas_dir}")
    context_parts.append(f"⚠️ База документов ФАС находится в разработке. Ответ будет сформирован на основе общих знаний.")
    
    return [{"file": "База ФАС", "page": "", "snippet": "\n".join(context_parts)}]

# =============================================================================
# 🎨 Интерфейс Streamlit
# =============================================================================

def show_fas_position():
    """Страница режима «Позиция ФАС»"""
    
    st.header("⚖️ Позиция ФАС")
    st.info("📌 Получите ответ в официальном стиле ФАС России — строго, со ссылками на НПА, без допущений")
    
    # ─────────────────────────────────────────────────────────────────────
    # 📁 Инфо о базе документов ФАС
    # ─────────────────────────────────────────────────────────────────────
    fas_dir = os.path.join("data", "raw", "fas")
    
    with st.expander("📚 База документов ФАС", expanded=False):
        st.write(f"**Путь к папке:** `{fas_dir}`")
        
        if os.path.exists(fas_dir):
            files = os.listdir(fas_dir)
            st.write(f"**Файлов в папке:** {len(files)}")
            if files:
                st.write("**Список файлов:**")
                for f in files[:10]:
                    st.write(f"📄 {f}")
                if len(files) > 10:
                    st.write(f"... и ещё {len(files) - 10} файлов")
        else:
            st.warning("⚠️ Папка не найдена. Создайте `data/raw/fas/` и добавьте документы ФАС.")
        
        st.caption("💡 Поддерживаемые форматы: PDF, DOCX, TXT. Документы будут проиндексированы через админку.")
    
    # ─────────────────────────────────────────────────────────────────────
    # ❓ Вопрос пользователя
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1️⃣ Ваш вопрос")
    
    query = st.text_area(
        "Задайте вопрос по тарифному регулированию",
        height=100,
        placeholder="Например: Правомерно ли включать расходы на ДМС в тариф при превышении 6% от ФОТ?",
        key="fas_question_input"
    )
    
    # ─────────────────────────────────────────────────────────────────────
    # ⚙️ Настройки
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("⚙️ Настройки", expanded=False):
        top_k = st.slider("Количество источников", 1, 10, 5, key="fas_top_k")
        st.caption("🔒 Температура зафиксирована на 0.1 для максимальной детерминированности ответа")
    
    # ─────────────────────────────────────────────────────────────────────
    # 🔎 Генерация ответа
    # ─────────────────────────────────────────────────────────────────────
    if st.button("⚖️ Получить позицию ФАС", type="primary", use_container_width=True):
        if query.strip():
            with st.spinner("🔄 Анализируем документы ФАС и формируем ответ..."):
                # Поиск по базе ФАС
                sources = search_fas_documents(query, top_k)
                
                # Формируем контекст
                context = "\n---\n".join([src["snippet"] for src in sources]) if sources else ""
                
                # Генерация ответа
                answer = generate_fas_answer(query, context)
                
                # ──────────────────────────────────────────────────────────
                # 📊 Результаты
                # ──────────────────────────────────────────────────────────
                st.divider()
                st.subheader("2️⃣ Ответ в стиле ФАС")
                
                # Отображение ответа
                st.markdown(f"### 📝 Позиция:\n{answer}")
                
                # Источники
                if sources:
                    st.subheader("📚 Источники:")
                    for i, src in enumerate(sources, 1):
                        with st.expander(f"📄 {i}. {src['file']}"):
                            st.caption(src['snippet'][:500])
                
                # Дисклеймер
                st.warning("⚠️ Ответ носит справочный характер и не является официальным заключением ФАС России")
                
                # ──────────────────────────────────────────────────────────
                # 📤 Экспорт
                # ──────────────────────────────────────────────────────────
                st.subheader("3️⃣ Экспорт")
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("📋 Копировать ответ", use_container_width=True):
                        st.session_state.fas_answer_copy = answer
                        st.success("✅ Ответ скопирован в буфер обмена")
                
                with col2:
                    # Экспорт в TXT
                    file_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"FAS_Position_{file_id}.txt"
                    st.download_button(
                        label="📤 Скачать в TXT",
                        data=answer,
                        file_name=filename,
                        mime="text/plain",
                        use_container_width=True,
                    )
                
                # ──────────────────────────────────────────────────────────
                # 📝 Обратная связь
                # ──────────────────────────────────────────────────────────
                st.divider()
                st.subheader("📊 Оцените ответ")
                
                col1, col2, col3 = st.columns(3)
                with col1:
                    if st.button("👍 Полезно", key="fas_good"):
                        from core.feedback import submit_feedback
                        submit_feedback(
                            user_type="user",
                            feedback_type="fas_position_rating",
                            description="Полезно",
                            question=query[:500],
                            answer=answer[:1000],
                            rating=3
                        )
                        st.success("✅ Спасибо за оценку!")
                        st.rerun()
                
                with col2:
                    if st.button("😐 Нормально", key="fas_neutral"):
                        from core.feedback import submit_feedback
                        submit_feedback(
                            user_type="user",
                            feedback_type="fas_position_rating",
                            description="Нормально",
                            question=query[:500],
                            answer=answer[:1000],
                            rating=2
                        )
                        st.success("✅ Спасибо за оценку!")
                        st.rerun()
                
                with col3:
                    if st.button("👎 Не помогло", key="fas_bad"):
                        from core.feedback import submit_feedback
                        submit_feedback(
                            user_type="user",
                            feedback_type="fas_position_rating",
                            description="Не помогло",
                            question=query[:500],
                            answer=answer[:1000],
                            rating=1
                        )
                        st.success("✅ Спасибо за оценку!")
                        st.rerun()
        else:
            st.warning("⚠️ Введите вопрос")
    
    # ─────────────────────────────────────────────────────────────────────
    # 💡 Примеры вопросов
    # ─────────────────────────────────────────────────────────────────────
    st.divider()
    with st.expander("💡 Примеры вопросов", expanded=False):
        st.write("• Правомерно ли включать расходы на ДМС в тариф при превышении 6% от ФОТ?")
        st.write("• Какие документы требуются для обоснования расходов на ремонт ОС?")
        st.write("• Можно ли учитывать премиальные выплаты в составе ФОТ для тарифа?")
        st.write("• Как ФАС трактует расходы на программное обеспечение?")

# =============================================================================
# 🚀 Запуск
# =============================================================================

if __name__ == "__main__":
    show_fas_position()