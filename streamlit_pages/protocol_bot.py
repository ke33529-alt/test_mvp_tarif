# streamlit_pages/protocol_bot.py
import streamlit as st
import os
import json
from datetime import datetime, timedelta
import io
from docx import Document as DocxDocument
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

# =============================================================================
# Транскрибация аудио (ТОЛЬКО WAV — без ffmpeg)
# =============================================================================
def transcribe_audio(file_path: str, language: str = "ru") -> dict:
    """Преобразует аудио в текст через Whisper (только WAV формат)"""
    result = {
        "status": "error",
        "text": None,
        "duration": None,
        "error": None
    }
    
    if not os.path.exists(file_path):
        result["error"] = f"Файл не найден: {file_path}"
        return result
    
    ext = os.path.splitext(file_path)[1].lower()
    
    # ТОЛЬКО WAV — другие форматы требуют ffmpeg
    if ext != '.wav':
        result["error"] = f"⚠️ Поддерживается только WAV формат. Конвертируйте {ext} в WAV через онлайн-конвертер."
        return result
    
    # Проверка наличия библиотек
    try:
        import speech_recognition as sr
    except ImportError:
        result["error"] = "⚠️ Установите: pip install speechrecognition openai-whisper"
        return result
    
    try:
        recognizer = sr.Recognizer()
        
        with sr.AudioFile(file_path) as source:
            audio = recognizer.record(source)
            
            try:
                text = recognizer.recognize_whisper(audio, language=language)
                result["text"] = text
                result["status"] = "success"
                
                # Длительность
                import wave
                with wave.open(file_path, 'rb') as f:
                    frames = f.getnframes()
                    rate = f.getframerate()
                    duration = frames / float(rate)
                    result["duration"] = f"{int(duration // 60)}:{int(duration % 60):02d}"
                
                return result
            except Exception as e:
                result["error"] = f"Whisper ошибка: {str(e)}"
        
        return result
        
    except Exception as e:
        result["error"] = f"Ошибка транскрибации: {type(e).__name__}: {str(e)}"
        return result


# =============================================================================
# Генерация протокола через SUMMARIZER (НЕ advisor!)
# =============================================================================
def generate_protocol_with_summarizer(
    text: str,
    structure: str,
    meeting_type: str = "Совещание",
    model: str = "llama3",
    temperature: float = 0.3,
    max_length: int = 2048,
    detail_level: str = "средний"
) -> dict:
    """
    Генерирует протокол через core.summarizer
    detail_level: "краткий" | "средний" | "подробный"
    """
    result = {
        "status": "error",
        "protocol": None,
        "error": None
    }
    
    try:
        from core.summarizer import summarize_text
        
        truncated_text = text[:10000] if len(text) > 10000 else text
        
        if not structure.strip():
            structure = """1. Дата и время встречи
2. Присутствовали
3. Повестка дня
4. Обсуждаемые вопросы
5. Принятые решения
6. Поручения (кто, что, срок)
7. Следующая встреча"""
        
        # Настройка промта в зависимости от уровня детализации
        detail_instructions = {
            "краткий": "Пиши МАКСИМАЛЬНО кратко, только ключевые факты и решения. Без деталей.",
            "средний": "Пиши сбалансированно: ключевые факты + важные детали. Оптимальный объём.",
            "подробный": "Пиши ПОДРОБНО: все факты, детали, цитаты, контекст. Максимальный объём."
        }
        
        length_limits = {
            "краткий": 1000,
            "средний": 2000,
            "подробный": 4000
        }
        
        prompt = f"""Ты — профессиональный секретарь. Создай протокол встречи на русском языке.

Тип встречи: {meeting_type}

СТРУКТУРА ПРОТОКОЛА (следуй ей строго):
{structure}

ТЕКСТ ВСТРЕЧИ (расшифровка/заметки):
{truncated_text}

УРОВЕНЬ ДЕТАЛИЗАЦИИ: {detail_level}
{detail_instructions.get(detail_level, detail_instructions["средний"])}

ТРЕБОВАНИЯ:
1. Следуй указанной структуре
2. Выдели ключевые решения и поручения
3. Укажи ответственных и сроки
4. Используй деловой стиль
5. НЕ добавляй советы
6. Если чего-то нет — пиши "Не указано"
7. {detail_instructions.get(detail_level, "")}

ПРОТОКОЛ:"""
        
        # Используем лимит длины в зависимости от детализации
        effective_max_length = length_limits.get(detail_level, max_length)
        
        protocol = summarize_text(
            prompt,
            model=model,
            temperature=temperature,
            max_length=effective_max_length,
            language="ru"
        )
        
        if protocol.startswith("❌") or protocol.startswith("⏱️") or protocol.startswith("🔌"):
            result["error"] = protocol
        else:
            result["protocol"] = protocol
            result["status"] = "success"
        
        return result
    
    except ImportError as e:
        result["error"] = f"❌ Модуль summarizer не найден: {str(e)}"
        return result
    except Exception as e:
        result["error"] = f"❌ Ошибка: {type(e).__name__}: {str(e)}"
        return result


# =============================================================================
# Создание DOCX
# =============================================================================
def create_protocol_docx(protocol_text: str, meeting_type: str, organization_name: str):
    """Создаёт DOCX файл протокола"""
    doc = DocxDocument()
    section = doc.sections[0]
    section.top_margin = Cm(2)
    section.bottom_margin = Cm(2)
    section.left_margin = Cm(3)
    section.right_margin = Cm(1.5)
    
    style = doc.styles['Normal']
    font = style.font
    font.name = 'Times New Roman'
    font.size = Pt(14)
    
    doc.add_paragraph("ПРОТОКОЛ")
    p = doc.add_paragraph(f"{meeting_type}")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.runs[0]
    run.bold = True
    run.font.size = Pt(16)
    
    doc.add_paragraph()
    doc.add_paragraph(f"Организация: {organization_name}")
    doc.add_paragraph(f"Дата протокола: {datetime.now().strftime('%d.%m.%Y')}")
    
    doc.add_paragraph()
    doc.add_paragraph("-" * 80)
    
    lines = protocol_text.split('\n')
    for line in lines:
        if line.strip() == '':
            doc.add_paragraph()
        elif line.strip().startswith('-') or line.strip().startswith('•'):
            p = doc.add_paragraph(line.strip())
            p.style = 'List Bullet'
        elif any(line.strip().startswith(f"{i}.") for i in range(1, 20)):
            p = doc.add_paragraph(line.strip())
            run = p.runs[0]
            run.bold = True
        else:
            doc.add_paragraph(line.strip())
    
    doc.add_paragraph()
    doc.add_paragraph("-" * 80)
    doc.add_paragraph()
    
    p = doc.add_paragraph("ПОДПИСИ СТОРОН:")
    p.runs[0].bold = True
    
    doc.add_paragraph()
    doc.add_paragraph("От РСО: _________________ / _________________ /")
    doc.add_paragraph()
    doc.add_paragraph("От регулятора: _________________ / _________________ /")
    
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output


# =============================================================================
# История протоколов
# =============================================================================
def get_protocol_history_path():
    """Путь к базе истории протоколов"""
    db_dir = os.path.join("data", "protocol_bot", "history")
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "protocols_db.json")

def save_protocol_to_history(protocol_data: dict) -> str:
    """Сохраняет протокол в историю"""
    db_path = get_protocol_history_path()
    
    if os.path.exists(db_path):
        with open(db_path, 'r', encoding='utf-8') as f:
            db = json.load(f)
    else:
        db = {"protocols": []}
    
    protocol_id = f"proto_{len(db['protocols']) + 1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    protocol_data["id"] = protocol_id
    protocol_data["created_at"] = datetime.now().isoformat()
    
    db["protocols"].append(protocol_data)
    
    with open(db_path, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    
    return protocol_id

def load_protocol_history():
    """Загружает историю протоколов"""
    db_path = get_protocol_history_path()
    if not os.path.exists(db_path):
        return []
    
    try:
        with open(db_path, 'r', encoding='utf-8') as f:
            db = json.load(f)
        protocols = db.get("protocols", [])
        return sorted(protocols, key=lambda x: x.get("created_at", ""), reverse=True)
    except:
        return []


# =============================================================================
# Интерфейс Streamlit
# =============================================================================
def show_protocol_bot():
    """Страница Робота-протокольщика"""
    st.header("📋 Робот-протокольщик")
    st.info("📌 Аудио (WAV) / Текст → Протокол по вашей структуре")
    
    # Инициализация session_state
    if "current_protocol" not in st.session_state:
        st.session_state.current_protocol = None
    if "protocol_history" not in st.session_state:
        st.session_state.protocol_history = load_protocol_history()
    if "transcription_result" not in st.session_state:
        st.session_state.transcription_result = None
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 1: Параметры встречи
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1. Параметры встречи")
    
    col1, col2 = st.columns(2)
    
    with col1:
        meeting_type = st.text_input(
            "Тип встречи",
            value="Совещание по тарифам",
            key="meeting_type_input",
            placeholder="Например: Встреча с ФАС, Заседание РЭК, Переговоры"
        )
    
    with col2:
        organization_name = st.text_input(
            "Организация",
            value="ООО «РСО»",
            key="org_name_input"
        )
    
    col1, col2 = st.columns(2)
    with col1:
        meeting_date = st.date_input("Дата встречи", value=datetime.now())
    with col2:
        meeting_time = st.time_input("Время", value=datetime.now().time())
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 2: Источник данных (3 варианта)
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("2. Источник данных")
    
    input_method = st.radio(
        "Способ ввода",
        ["🎤 Аудио (WAV)", "📄 Загрузка текста протокола", "✏️ Ввод текста вручную"],
        horizontal=True,
        key="input_method_select"
    )
    
    notes_text = ""
    
    # ─── ВАРИАНТ 1: Аудио WAV ───
    if input_method == "🎤 Аудио (WAV)":
        st.caption("💡 Поддерживается только WAV формат (без конвертации)")
        st.info("📝 Если у вас MP3/M4A — конвертируйте онлайн: https://cloudconvert.com/mp3-to-wav")
        
        uploaded_audio = st.file_uploader(
            "Загрузите аудио (WAV)",
            type=['wav'],
            key="audio_upload"
        )
        
        if uploaded_audio:
            temp_dir = os.path.join("data", "protocol_bot", "temp")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, uploaded_audio.name)
            
            with open(temp_path, "wb") as f:
                f.write(uploaded_audio.getbuffer())
            
            st.success(f"✅ Файл загружен: {uploaded_audio.name}")
            
            if st.button("🎤 Транскрибировать аудио", type="secondary"):
                with st.spinner("🔄 Идёт расшифровка (1-5 минут)..."):
                    result = transcribe_audio(temp_path, language="ru")
                    
                    if result["status"] == "success":
                        st.session_state.transcription_result = result
                        notes_text = result["text"]
                        st.success(f"✅ Транскрибация завершена! ({result.get('duration', 'N/A')})")
                        st.rerun()
                    else:
                        st.error(f"❌ {result.get('error', 'Ошибка транскрибации')}")
        
        if st.session_state.transcription_result:
            st.markdown("**📄 Расшифровка:**")
            notes_text = st.text_area(
                "Отредактируйте текст при необходимости",
                value=st.session_state.transcription_result.get("text", ""),
                height=300,
                key="transcription_edit"
            )
    
    # ─── ВАРИАНТ 2: Загрузка текста протокола ───
    elif input_method == "📄 Загрузка текста протокола":
        st.caption("💡 Загрузите готовый текст протокола (TXT, DOCX, PDF)")
        
        uploaded_text = st.file_uploader(
            "Загрузите текст протокола",
            type=['txt', 'docx', 'pdf'],
            key="text_upload"
        )
        
        if uploaded_text:
            try:
                if uploaded_text.type == "text/plain":
                    notes_text = uploaded_text.read().decode('utf-8')
                elif uploaded_text.type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                    from docx import Document
                    doc = Document(uploaded_text)
                    notes_text = "\n".join([p.text for p in doc.paragraphs])
                elif uploaded_text.type == "application/pdf":
                    import fitz
                    doc = fitz.open(stream=uploaded_text.read(), filetype="pdf")
                    notes_text = "\n".join([page.get_text() for page in doc])
                    doc.close()
                
                st.success(f"✅ Файл загружен: {len(notes_text)} символов")
                
                # Редактирование загруженного текста
                notes_text = st.text_area(
                    "Отредактируйте текст при необходимости",
                    value=notes_text,
                    height=300,
                    key="uploaded_text_edit"
                )
            except Exception as e:
                st.error(f"❌ Ошибка чтения файла: {str(e)}")
    
    # ─── ВАРИАНТ 3: Ввод текста вручную ───
    else:
        notes_text = st.text_area(
            "Введите текст протокола или заметки",
            placeholder="""Пример:
Обсуждали тариф на 2025 год
ФАС запросила дополнительные документы
Срок: до 15 апреля 2025
Ответственный: главный бухгалтер
Следующая встреча: 20 апреля""",
            height=400,
            key="manual_text_input"
        )
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 3: Структура протокола + Настройки детализации
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("3. Структура и настройки")
    st.caption("💡 Введите нужные разделы и выберите уровень детализации")
    
    default_structure = """1. Дата и время встречи
2. Присутствовали
3. Повестка дня
4. Обсуждаемые вопросы
5. Принятые решения
6. Поручения (кто, что, срок)
7. Следующая встреча"""
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        structure_text = st.text_area(
            "Структура протокола (каждый раздел с новой строки)",
            value=default_structure,
            height=200,
            key="protocol_structure",
            placeholder="Введите структуру протокола..."
        )
    
    with col2:
        st.write("**📊 Уровень детализации:**")
        detail_level = st.radio(
            "Подробность протокола",
            ["краткий", "средний", "подробный"],
            index=1,
            key="detail_level_select",
            help="Краткий = только факты, Средний = баланс, Подробный = все детали"
        )
        
        st.write("**⚙️ Модель:**")
        model = st.selectbox(
            "Модель",
            ["llama3", "phi3", "gemma2"],
            key="proto_model"
        )
        
        st.write("**📏 Макс. длина:**")
        max_length = st.slider(
            "Длина",
            500, 4000, 2000, 100,
            key="proto_length"
        )
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 4: Генерация протокола
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("4. Генерация протокола")
    
    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.caption("💡 AI проанализирует текст и создаст структурированный протокол")
    
    with col2:
        generate_btn = st.button("🤖 Создать протокол", use_container_width=True, type="primary")
    
    if generate_btn and notes_text:
        with st.spinner("🔄 AI создаёт протокол..."):
            protocol_result = generate_protocol_with_summarizer(
                text=notes_text,
                structure=structure_text,
                meeting_type=meeting_type,
                model=model,
                max_length=max_length,
                detail_level=detail_level
            )
            
            if protocol_result["status"] == "success":
                st.session_state.current_protocol = {
                    "text": protocol_result["protocol"],
                    "meeting_type": meeting_type,
                    "organization": organization_name,
                    "date": meeting_date.isoformat(),
                    "time": meeting_time.isoformat(),
                    "notes": notes_text,
                    "structure": structure_text,
                    "detail_level": detail_level
                }
                st.success("✅ Протокол создан!")
                st.rerun()
            else:
                st.error(f"❌ {protocol_result.get('error', 'Ошибка генерации')}")
    
    elif generate_btn and not notes_text:
        st.warning("⚠️ Введите текст или загрузите аудио/файл")
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 5: Просмотр и редактирование
    # ─────────────────────────────────────────────────────────────────────
    if st.session_state.current_protocol:
        st.divider()
        st.subheader("5. Просмотр и редактирование")
        
        proto = st.session_state.current_protocol
        
        edited_text = st.text_area(
            "Текст протокола",
            value=proto["text"],
            height=400,
            key="edit_protocol"
        )
        
        proto["text"] = edited_text
        
        # ─────────────────────────────────────────────────────────────────────
        # Шаг 6: Экспорт и сохранение (БЕЗ раздела поручений)
        # ─────────────────────────────────────────────────────────────────────
        st.divider()
        st.subheader("6. Экспорт и сохранение")
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            if st.button("💾 Сохранить в историю", use_container_width=True):
                protocol_id = save_protocol_to_history(proto)
                st.success(f"✅ Протокол сохранён: {protocol_id}")
                st.session_state.protocol_history = load_protocol_history()
                st.rerun()
        
        with col2:
            docx_output = create_protocol_docx(
                edited_text,
                proto["meeting_type"],
                proto["organization"]
            )
            filename = f"Protocol_{proto['organization'][:20]}_{meeting_date.strftime('%Y%m%d')}.docx"
            
            st.download_button(
                label="📥 Скачать DOCX",
                data=docx_output,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True
            )
        
        with col3:
            if st.button("🔄 Перегенерировать", use_container_width=True):
                st.session_state.current_protocol = None
                st.rerun()
        
        with st.expander("👁️ Предпросмотр", expanded=False):
            st.text(edited_text[:2000] + "..." if len(edited_text) > 2000 else edited_text)
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 7: История протоколов
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("7. 📚 История протоколов")
    
    if st.session_state.protocol_history:
        st.caption(f"Всего протоколов: {len(st.session_state.protocol_history)}")
        
        for proto in st.session_state.protocol_history[:10]:
            with st.expander(f"📄 {proto.get('created_at', '')[:10]} — {proto.get('organization', 'Организация')}"):
                st.write(f"**Тип:** {proto.get('meeting_type', '')}")
                st.write(f"**Детализация:** {proto.get('detail_level', 'средний')}")
                st.write(f"**Дата встречи:** {proto.get('date', '')[:10]}")
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("📄 Открыть", key=f"open_proto_{proto.get('id', '')}"):
                        st.session_state.current_protocol = proto
                        st.rerun()
                with col2:
                    if st.button("🗑 Удалить", key=f"del_proto_{proto.get('id', '')}"):
                        st.success("✅ Протокол удалён")
                        st.session_state.protocol_history = load_protocol_history()
                        st.rerun()
    else:
        st.info("📭 История пуста")
    
    # ─────────────────────────────────────────────────────────────────────
    # Справка
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("💡 Как использовать", expanded=False):
        st.write("**Возможности:**")
        st.write("1. **🎤 Аудио (WAV)**: Загрузите WAV файл — расшифровка через Whisper")
        st.write("2. **📄 Загрузка текста**: TXT, DOCX, PDF — загрузите готовый текст протокола")
        st.write("3. **✏️ Ввод вручную**: Введите заметки или текст протокола напрямую")
        st.write("4. **Ваша структура**: Введите нужные разделы — AI следует ей строго")
        st.write("5. **📊 Детализация**: Краткий/Средний/Подробный — регулируйте объём протокола")
        st.write("6. **Без советчика**: Использует summarizer (нейтральный пересказ)")
        st.write("7. **Экспорт в DOCX**: Готовый документ с форматированием")
        st.write("8. **История**: Сохранение всех протоколов")
        st.write("")
        st.write("**Уровни детализации:**")
        st.write("- **Краткий**: Только ключевые факты и решения (~1000 слов)")
        st.write("- **Средний**: Баланс фактов и деталей (~2000 слов)")
        st.write("- **Подробный**: Все детали, цитаты, контекст (~4000 слов)")
        st.write("")
        st.write("**Зависимости:**")
        st.code("pip install speechrecognition openai-whisper")
        st.write("")
        st.write("**Хранение:**")
        st.write("- История: `data/protocol_bot/history/protocols_db.json`")
        st.write("- Временные файлы: `data/protocol_bot/temp/`")


# =============================================================================
# Запуск
# =============================================================================
if __name__ == "__main__":
    show_protocol_bot()