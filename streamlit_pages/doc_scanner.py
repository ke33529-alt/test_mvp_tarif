# streamlit_pages/doc_scanner.py
import streamlit as st
import os
import json
import pandas as pd
from datetime import datetime, timedelta
import io
from docx import Document as DocxDocument
import re
import shutil

# =============================================================================
# Функции OCR (EasyOCR с GPU поддержкой)
# =============================================================================
def ocr_with_easyocr(image_path, use_gpu=True):
    """Распознавание текста через EasyOCR с GPU поддержкой"""
    try:
        import easyocr
        
        if not os.path.exists(image_path):
            return f"#ERROR: Файл не найден: {image_path}"
        
        file_size = os.path.getsize(image_path)
        if file_size == 0:
            return f"#ERROR: Пустой файл: {image_path}"
        if file_size > 50 * 1024 * 1024:
            return f"#ERROR: Файл слишком большой: {file_size / 1024 / 1024:.1f} MB"
        
        ext = os.path.splitext(image_path)[1].lower()
        valid_exts = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif']
        if ext not in valid_exts:
            return f"#ERROR: Не поддерживаемый формат: {ext}"
        
        try:
            from PIL import Image
            img = Image.open(image_path)
            img.verify()
            img = Image.open(image_path)
            img = img.convert('RGB')
            
            if img.size[0] == 0 or img.size[1] == 0:
                return f"#ERROR: Изображение пустое: {image_path}"
            
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                img.save(tmp.name, "PNG")
                tmp_path = tmp.name
        except Exception as e:
            return f"#ERROR: Не удалось загрузить изображение: {str(e)}"
        
        if "easyocr_reader" not in st.session_state:
            st.session_state.easyocr_reader = easyocr.Reader(
                ['ru', 'en'], 
                gpu=use_gpu,
                verbose=False,
                download_enabled=True,
                cudnn_benchmark=True
            )
        
        reader = st.session_state.easyocr_reader
        
        try:
            results = reader.readtext(tmp_path, detail=0)
            
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except:
                    pass
            
            if not results:
                return "#WARNING: Текст не распознан"
            
            text = ' '.join(results)
            return text
            
        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except:
                    pass
            return f"#ERROR EasyOCR: {str(e)}"

    except Exception as e:
        return f"#ERROR EasyOCR: {str(e)}"

def ocr_pdf(pdf_path, use_gpu=True):
    """Распознавание PDF (текст + изображения через EasyOCR)"""
    try:
        import fitz
        import tempfile
        
        if not os.path.exists(pdf_path):
            return f"#ERROR: Файл не найден: {pdf_path}", []
        
        doc = fitz.open(pdf_path)
        full_text = []
        pages_data = []
        
        for page_num, page in enumerate(doc, 1):
            try:
                text = page.get_text()
                
                if len(text.strip()) < 100:
                    try:
                        pix = page.get_pixmap()
                        img = pix.tobytes("png")
                        
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                            tmp.write(img)
                            tmp_path = tmp.name
                        
                        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                            ocr_text = ocr_with_easyocr(tmp_path, use_gpu=use_gpu)
                            text = ocr_text
                        else:
                            text = f"#ERROR: Не удалось создать временный файл для страницы {page_num}"
                        
                        if os.path.exists(tmp_path):
                            try:
                                os.remove(tmp_path)
                            except:
                                pass
                    except Exception as e:
                        text = f"#ERROR OCR страницы {page_num}: {str(e)}"
                
                full_text.append(text)
                pages_data.append({
                    "page": page_num,
                    "text": text,
                    "word_count": len(text.split())
                })
            except Exception as e:
                text = f"#ERROR Обработки страницы {page_num}: {str(e)}"
                full_text.append(text)
                pages_data.append({
                    "page": page_num,
                    "text": text,
                    "word_count": 0
                })
        
        doc.close()
        return "\n\n--- СТРАНИЦА {page} ---\n\n".join(full_text), pages_data

    except Exception as e:
        return f"#ERROR PDF: {str(e)}", []

def read_docx_full(file_path):
    """Полное чтение DOCX файла (все параграфы + таблицы)"""
    try:
        doc = DocxDocument(file_path)
        all_text = []
        
        for para in doc.paragraphs:
            if para.text.strip():
                all_text.append(para.text)
        
        for table in doc.tables:
            for row in table.rows:
                row_text = []
                for cell in row.cells:
                    if cell.text.strip():
                        row_text.append(cell.text)
                if row_text:
                    all_text.append(" | ".join(row_text))
        
        for section in doc.sections:
            if section.header:
                for para in section.header.paragraphs:
                    if para.text.strip():
                        all_text.insert(0, f"[HEADER] {para.text}")
            if section.footer:
                for para in section.footer.paragraphs:
                    if para.text.strip():
                        all_text.append(f"[FOOTER] {para.text}")
        
        text = "\n".join(all_text)
        pages_data = [{
            "page": 1,
            "text": text,
            "word_count": len(text.split()),
            "paragraphs": len(doc.paragraphs),
            "tables": len(doc.tables)
        }]
        
        return text, pages_data
    
    except Exception as e:
        return f"#ERROR DOCX: {str(e)}", []

def process_document(file_path, use_gpu=True):
    """Обрабатывает документ с GPU поддержкой"""
    if not os.path.exists(file_path):
        return f"#ERROR: Файл не найден: {file_path}", []

    file_size = os.path.getsize(file_path)
    if file_size == 0:
        return f"#ERROR: Пустой файл: {file_path}", []

    ext = os.path.splitext(file_path)[1].lower()

    if ext in ['.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp']:
        text = ocr_with_easyocr(file_path, use_gpu=use_gpu)
        pages_data = [{"page": 1, "text": text, "word_count": len(text.split())}]

    elif ext == '.pdf':
        text, pages_data = ocr_pdf(file_path, use_gpu=use_gpu)

    elif ext in ['.docx', '.doc']:
        text, pages_data = read_docx_full(file_path)

    elif ext in ['.txt']:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
            pages_data = [{"page": 1, "text": text, "word_count": len(text.split())}]
        except Exception as e:
            text = f"#ERROR TXT: {str(e)}"
            pages_data = []

    else:
        text = f"#ERROR: Не поддерживаемый формат {ext}"
        pages_data = []

    return text, pages_data

# =============================================================================
# AI Суммаризация (с выбором модели)
# =============================================================================
def get_available_models():
    """Получает список доступных моделей от Ollama"""
    try:
        import requests
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        
        if response.status_code == 200:
            models = response.json().get("models", [])
            return [m.get("name", "unknown") for m in models]
        return ["phi3", "llama3.2"]  # Fallback
    except:
        return ["phi3", "llama3.2"]

def summarize_with_ai(text, max_length=500, temperature=0.3, model="phi3"):
    """Генерирует краткое содержание документа через core.summarizer"""
    try:
        from core.summarizer import summarize_text
        
        truncated_text = text[:8000] if len(text) > 8000 else text
        
        summary = summarize_text(
            truncated_text,
            model=model,
            max_length=max_length,
            temperature=temperature
        )
        
        return summary
    
    except ImportError as e:
        return f"#ERROR: Модуль summarizer не найден: {str(e)}"
    except Exception as e:
        return f"#ERROR AI Summarization: {str(e)}"

def get_document_summary_cached(doc_id, text, max_length=500, temperature=0.3, model="phi3"):
    """Получает суммаризацию из кэша или генерирует новую"""
    try:
        from core.summarizer import summarize_document
        
        result = summarize_document(
            doc_id=doc_id,
            text=text,
            model=model,
            max_length=max_length,
            temperature=temperature,
            use_cache=True
        )
        
        if result["status"] == "success":
            return result["summary"]
        else:
            return result.get("error", "#ERROR: Не удалось сгенерировать резюме")
    
    except ImportError:
        return summarize_with_ai(text, max_length, temperature, model)
    except Exception as e:
        return f"#ERROR: {str(e)}"

def summarize_folder_cached(folder_id, documents, max_length=1000, temperature=0.3, model="phi3"):
    """Генерирует сводное резюме по папке документов"""
    try:
        from core.summarizer import summarize_folder
        
        result = summarize_folder(
            folder_id=folder_id,
            documents=documents,
            model=model,
            max_length=max_length,
            temperature=temperature,
            use_cache=True
        )
        
        if result["status"] == "success":
            return result["summary"]
        else:
            return result.get("error", "#ERROR: Не удалось сгенерировать резюме по папке")
    
    except Exception as e:
        return f"#ERROR: {str(e)}"

# =============================================================================
# Функции хранения и поиска
# =============================================================================
def get_scans_db_path():
    """Путь к базе распознанных документов"""
    db_dir = os.path.join("data", "doc_scanner")
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "scans_db.json")

def load_scans_db():
    """Загружает базу распознанных документов"""
    db_path = get_scans_db_path()
    if os.path.exists(db_path):
        with open(db_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"documents": [], "folders": {}, "stats": {"total": 0, "processed": 0, "last_scan": None}}

def save_scans_db(db):
    """Сохраняет базу распознанных документов"""
    db_path = get_scans_db_path()
    with open(db_path, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def add_document_to_db(db, file_path, text, pages_data, doc_type="unknown", folder_name=None):
    """Добавляет документ в базу"""
    doc_id = f"doc_{len(db['documents']) + 1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    doc_type_keywords = {
        "invoice": ["счет", "invoice", "счет-фактура"],
        "act": ["акт", "act", "авр"],
        "contract": ["договор", "contract", "соглашение", "контракт"],
        "nakladnaya": ["накладная", "торг-12", "упд"],
        "other": []
    }

    for dtype, keywords in doc_type_keywords.items():
        if any(kw in file_name.lower() for kw in keywords):
            doc_type = dtype
            break

    document = {
        "id": doc_id,
        "file_name": file_name,
        "file_path": file_path,
        "file_size": file_size,
        "doc_type": doc_type,
        "text": text,
        "pages": pages_data,
        "processed_at": datetime.now().isoformat(),
        "word_count": sum(p.get("word_count", 0) for p in pages_data),
        "folder_name": folder_name
    }

    db["documents"].append(document)
    db["stats"]["total"] += 1
    db["stats"]["processed"] += 1
    db["stats"]["last_scan"] = datetime.now().isoformat()
    
    if folder_name:
        if folder_name not in db["folders"]:
            db["folders"][folder_name] = []
        db["folders"][folder_name].append(doc_id)

    return doc_id

def search_in_documents(db, query, use_ai_synonyms=True):
    """Поиск по распознанным документам с AI-синонимами"""
    if not query.strip():
        return []

    synonyms = generate_synonyms(query) if use_ai_synonyms else [query]
    results = []

    for doc in db.get("documents", []):
        doc_matches = []
        
        for page in doc.get("pages", []):
            page_text = page.get("text", "").lower()
            
            match_found = False
            match_terms = []
            
            for term in synonyms:
                if term.lower() in page_text:
                    match_found = True
                    match_terms.append(term)
            
            if match_found:
                context = find_context(page_text, query)
                doc_matches.append({
                    "page": page.get("page", 1),
                    "context": context,
                    "match_terms": match_terms
                })
        
        if doc_matches:
            results.append({
                "document": doc,
                "matches": doc_matches,
                "total_matches": len(doc_matches)
            })

    results.sort(key=lambda x: x["total_matches"], reverse=True)
    return results

def generate_synonyms(query):
    """Генерирует синонимы для поиска (тарифная тематика)"""
    synonym_dict = {
        "счет": ["invoice", "счет-фактура", "счет на оплату"],
        "акт": ["act", "акт выполненных работ", "авр"],
        "договор": ["contract", "соглашение", "контракт"],
        "накладная": ["товарная накладная", "торг-12", "упд"],
        "тариф": ["цена", "ставка", "тарифный план"],
        "затраты": ["расходы", "издержки", "costs"],
        "выручка": ["доход", "revenue", "прибыль"],
        "амортизация": ["аморт", "износ", "depreciation"],
        "численность": ["штат", "сотрудники", "personnel", "кадры"],
        "ремонт": ["repair", "восстановление", "то"],
        "электроэнергия": ["электричество", "electric", "квт", "квтч"],
        "тепло": ["thermal", "гкал", "отопление"],
        "вода": ["water", "водоснабжение", "м3", "куб"],
        "тко": ["отходы", "мусор", "waste", "твердые коммунальные отходы"]
    }

    synonyms = [query]
    query_lower = query.lower()

    for key, syns in synonym_dict.items():
        if key in query_lower:
            synonyms.extend(syns)

    if query_lower.endswith("а"):
        synonyms.append(query_lower[:-1] + "у")
        synonyms.append(query_lower[:-1] + "е")
    elif query_lower.endswith("ы"):
        synonyms.append(query_lower[:-1] + "у")
        synonyms.append(query_lower[:-1])

    return list(set(synonyms))

def find_context(text, query, window=100):
    """Находит контекст вокруг найденного запроса"""
    text_lower = text.lower()
    query_lower = query.lower()

    idx = text_lower.find(query_lower)
    if idx == -1:
        return text[:200] + "..." if len(text) > 200 else text

    start = max(0, idx - window)
    end = min(len(text), idx + len(query) + window)

    context = text[start:end]
    if start > 0:
        context = "..." + context
    if end < len(text):
        context = context + "..."

    context = context.replace(query, f"**{query}**")
    return context

def export_search_results(results, output_format="txt"):
    """Экспортирует результаты поиска"""
    if output_format == "txt":
        output = io.StringIO()
        output.write("РЕЗУЛЬТАТЫ ПОИСКА\n")
        output.write("=" * 80 + "\n\n")
        
        for res in results:
            doc = res["document"]
            output.write(f"Документ: {doc['file_name']}\n")
            output.write(f"Совпадений: {res['total_matches']}\n")
            output.write(f"Тип: {doc['doc_type']}\n")
            output.write(f"Обработан: {doc['processed_at'][:10]}\n")
            output.write("-" * 80 + "\n")
            
            for match in res["matches"]:
                output.write(f"  Страница {match['page']}: {match['context']}\n")
            output.write("\n")
        
        return output.getvalue().encode('utf-8')

    elif output_format == "docx":
        doc = DocxDocument()
        p = doc.add_paragraph("РЕЗУЛЬТАТЫ ПОИСКА")
        p.alignment = 1
        run = p.runs[0]
        run.bold = True
        run.font.size = 16
        
        for res in results:
            d = res["document"]
            p = doc.add_paragraph(f"Документ: {d['file_name']}")
            p.runs[0].bold = True
            doc.add_paragraph(f"Совпадений: {res['total_matches']}")
            doc.add_paragraph(f"Тип: {d['doc_type']}")
            
            for match in res["matches"]:
                doc.add_paragraph(f"  Страница {match['page']}: {match['context']}")
            doc.add_paragraph("-" * 80)
        
        output = io.BytesIO()
        doc.save(output)
        output.seek(0)
        return output

    return None

# =============================================================================
# Интерфейс Streamlit
# =============================================================================
def show_doc_scanner():
    """Страница AI-Сканера документов"""
    st.header("📸 AI-Сканер документов")
    st.info("📌 Распознавание через EasyOCR + Поиск по содержимому + GPU ускорение")

    # Инициализация session_state
    if "scan_db" not in st.session_state:
        st.session_state.scan_db = load_scans_db()
    if "search_results" not in st.session_state:
        st.session_state.search_results = []
    if "selected_doc" not in st.session_state:
        st.session_state.selected_doc = None
    if "current_summary" not in st.session_state:
        st.session_state.current_summary = None
    if "selected_folder_summary" not in st.session_state:
        st.session_state.selected_folder_summary = None

    # ─────────────────────────────────────────────────────────────────────
    # Шаг 1: Выбор режима (Файл ИЛИ Папка)
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1. Выбор источника документов")
    
    scan_mode = st.radio(
        "Режим работы",
        ["📄 Один файл", "📁 Папка с документами"],
        horizontal=True,
        key="scan_mode_select"
    )
    
    # Настройки GPU и МОДЕЛИ
    with st.expander("⚙️ Настройки OCR и AI", expanded=False):
        use_gpu = st.checkbox("Использовать GPU для OCR", value=True, help="Требуется NVIDIA CUDA")
        st.caption("Если GPU не доступен — автоматически переключится на CPU")
        
        st.divider()
        
        # ✅ ВЫБОР МОДЕЛИ ДЛЯ СУММАРИЗАЦИИ
        st.write("**🤖 Модель для суммаризации:**")
        available_models = get_available_models()
        selected_model = st.selectbox(
            "Выберите модель",
            options=available_models,
            index=0 if "phi3" in available_models else 0,
            key="scanner_model_select",
            help="phi3 быстрее для 4GB VRAM, llama3.2 качественнее но требует больше памяти"
        )
        st.caption(f"✅ Доступные модели: {', '.join(available_models)}")
    
    files_to_process = []
    folder_name = None

    if scan_mode == "📄 Один файл":
        uploaded_file = st.file_uploader(
            "Загрузить файл",
            type=['pdf', 'jpg', 'jpeg', 'png', 'tiff', 'tif', 'docx', 'doc', 'txt'],
            key="scanner_single_upload"
        )
        
        if uploaded_file:
            temp_dir = os.path.join("data", "doc_scanner", "temp")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, uploaded_file.name)
            with open(temp_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            files_to_process = [temp_path]
    
    else:  # Папка
        folder_path = st.text_input(
            "📁 Путь к папке с документами",
            placeholder="C:\\documents\\scans",
            key="folder_path_input"
        )
        
        col1, col2 = st.columns(2)
        with col1:
            batch_size = st.slider("Пакетная обработка", 1, 100, 20)
        with col2:
            folder_name = st.text_input("Название папки (для группировки)", value="")
        
        if folder_path and os.path.exists(folder_path):
            if not folder_name:
                folder_name = os.path.basename(folder_path)
            
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    if file.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.tif', '.docx', '.doc', '.txt')):
                        files_to_process.append(os.path.join(root, file))
            
            st.info(f"📊 Найдено документов: {len(files_to_process)}")

    # ─────────────────────────────────────────────────────────────────────
    # Кнопка запуска распознавания
    # ─────────────────────────────────────────────────────────────────────
    col1, col2 = st.columns([3, 1])
    
    with col1:
        if st.button("🚀 Запустить распознавание", use_container_width=True, type="primary", disabled=not files_to_process):
            if files_to_process:
                progress_bar = st.progress(0)
                status_text = st.empty()
                
                processed = 0
                errors = []
                
                for i, file_path in enumerate(files_to_process[:batch_size] if scan_mode == "📁 Папка" else files_to_process):
                    try:
                        status_text.text(f"🔄 Обработка: {os.path.basename(file_path)} ({i+1}/{len(files_to_process)})")
                        text, pages_data = process_document(file_path, use_gpu=use_gpu)
                        
                        if text.startswith("#ERROR") or text.startswith("#WARNING"):
                            errors.append(f"{os.path.basename(file_path)}: {text}")
                        
                        add_document_to_db(
                            st.session_state.scan_db, 
                            file_path, 
                            text, 
                            pages_data, 
                            folder_name=folder_name
                        )
                        processed += 1
                        progress_bar.progress((i + 1) / len(files_to_process))
                    except Exception as e:
                        errors.append(f"{os.path.basename(file_path)}: {str(e)}")
                
                save_scans_db(st.session_state.scan_db)
                status_text.text(f"✅ Обработано: {processed} документов")
                
                if processed > 0:
                    st.success(f"✅ Готово! Обработано {processed} документов.")
                
                if errors:
                    st.warning(f"⚠️ Ошибок: {len(errors)}")
                    with st.expander("📋 Показать ошибки"):
                        for err in errors[:10]:
                            st.write(f"• {err}")
                        if len(errors) > 10:
                            st.write(f"... и ещё {len(errors) - 10} ошибок")
                
                st.rerun()
            else:
                st.warning("⚠️ Нет файлов для обработки")
    
    with col2:
        if st.button("🗑 Очистить базу", use_container_width=True):
            st.session_state.scan_db = {"documents": [], "folders": {}, "stats": {"total": 0, "processed": 0, "last_scan": None}}
            save_scans_db(st.session_state.scan_db)
            st.success("✅ База очищена")
            st.rerun()

    st.divider()

    # ─────────────────────────────────────────────────────────────────────
    # Шаг 2: Статистика и список документов
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("2. База распознанных документов")

    stats = st.session_state.scan_db.get("stats", {})

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("📊 Всего", stats.get("total", 0))
    col2.metric("✅ Обработано", stats.get("processed", 0))
    col3.metric("📄 Страниц", sum(len(doc.get("pages", [])) for doc in st.session_state.scan_db.get("documents", [])))
    col4.metric("🕐 Последний", stats.get("last_scan", "—")[:16] if stats.get("last_scan") else "—")

    documents = st.session_state.scan_db.get("documents", [])
    folders = st.session_state.scan_db.get("folders", {})

    if documents:
        st.write("**📋 Последние документы:**")
        df_docs = pd.DataFrame([
            {
                "ID": doc.get("id", "")[:20],
                "Файл": doc.get("file_name", ""),
                "Тип": doc.get("doc_type", ""),
                "Страниц": len(doc.get("pages", [])),
                "Слов": doc.get("word_count", 0),
                "Папка": doc.get("folder_name", "—"),
                "Дата": doc.get("processed_at", "")[:10]
            }
            for doc in documents[-20:]
        ])
        st.dataframe(df_docs, use_container_width=True, hide_index=True)
        
        # Выбор режима просмотра
        view_mode = st.radio("Просмотр", ["По документу", "По папке"], horizontal=True, key="view_mode_select")
        
        if view_mode == "По документу":
            selected_doc_id = st.selectbox(
                "📄 Просмотр документа",
                options=[""] + [doc.get("id", "") for doc in documents],
                format_func=lambda x: next((doc.get("file_name", "") for doc in documents if doc.get("id") == x), "") if x else "— Выберите —"
            )
            
            if selected_doc_id:
                selected_doc = next((doc for doc in documents if doc.get("id") == selected_doc_id), None)
                if selected_doc:
                    st.session_state.selected_doc = selected_doc
                    st.session_state.selected_folder_summary = None
                    
                    st.write(f"**📄 {selected_doc['file_name']}**")
                    st.write(f"**Тип:** {selected_doc.get('doc_type', '')}")
                    st.write(f"**Страниц:** {len(selected_doc.get('pages', []))}")
                    st.write(f"**Слов:** {selected_doc.get('word_count', 0)}")
                    
                    first_page_text = selected_doc.get("pages", [{}])[0].get("text", "")
                    if first_page_text.startswith("#ERROR") or first_page_text.startswith("#WARNING"):
                        st.warning(f"⚠️ {first_page_text}")
                    
                    st.divider()
                    
                    # КНОПКА AI-СУММАРИЗАЦИИ (ОДИН ДОКУМЕНТ) С ВЫБОРОМ МОДЕЛИ
                    col1, col2 = st.columns([3, 1])
                    
                    with col1:
                        st.write("**📋 Содержимое:**")
                    
                    with col2:
                        summary_length = st.slider("Длина резюме", 200, 2000, 800, 100, key=f"sum_len_{selected_doc['id']}")
                        
                        # ✅ Модель выбирается из настроек выше
                        if st.button("🤖 AI-Резюме", use_container_width=True, type="secondary", key=f"summarize_{selected_doc['id']}"):
                            with st.spinner(f"🔄 AI создаёт краткое содержание ({selected_model})..."):
                                full_text = "\n".join([page.get("text", "") for page in selected_doc.get("pages", [])])
                                summary = get_document_summary_cached(
                                    selected_doc["id"], 
                                    full_text, 
                                    max_length=summary_length,
                                    model=selected_model  # ✅ Передаём выбранную модель
                                )
                                st.session_state.current_summary = summary
                                st.session_state.selected_folder_summary = None
                                st.success("✅ Резюме готово!")
                    
                    # Показываем содержимое по страницам
                    for page in selected_doc.get("pages", [])[:5]:
                        st.markdown(f"#### 📃 Страница {page.get('page', 1)}")
                        page_text = page.get("text", "")
                        if len(page_text) > 2000:
                            with st.expander("Показать полностью"):
                                st.text(page_text)
                        else:
                            st.text(page_text)
                        st.divider()
                    
                    if len(selected_doc.get("pages", [])) > 5:
                        if st.button(f"📖 Показать все {len(selected_doc['pages'])} страниц"):
                            for page in selected_doc.get("pages", []):
                                st.markdown(f"#### 📃 Страница {page.get('page', 1)}")
                                st.text(page.get("text", ""))
                                st.divider()
                    
                    # ОКНО С AI-РЕЗЮМЕ
                    if st.session_state.current_summary and not st.session_state.selected_folder_summary:
                        st.divider()
                        st.subheader(f"🤖 AI-Резюме документа ({selected_model})")
                        
                        with st.expander("📄 Показать резюме", expanded=True):
                            if st.session_state.current_summary.startswith("#ERROR"):
                                st.error(st.session_state.current_summary)
                            else:
                                st.markdown(st.session_state.current_summary)
                                
                                col1, col2 = st.columns(2)
                                with col1:
                                    if st.button("🔄 Перегенерировать", key=f"regen_sum_{selected_doc['id']}"):
                                        st.session_state.current_summary = None
                                        st.rerun()
                                with col2:
                                    summary_text = st.session_state.current_summary
                                    st.download_button(
                                        label="📥 Скачать резюме (TXT)",
                                        data=summary_text.encode('utf-8'),
                                        file_name=f"Summary_{selected_doc['file_name'][:30]}.txt",
                                        mime="text/plain",
                                        use_container_width=True
                                    )
        
        else:  # По папке
            if folders:
                selected_folder = st.selectbox(
                    "📁 Выберите папку",
                    options=[""] + list(folders.keys()),
                    key="folder_summary_select"
                )
                
                if selected_folder:
                    folder_doc_ids = folders[selected_folder]
                    folder_docs = [doc for doc in documents if doc.get("id") in folder_doc_ids]
                    
                    st.write(f"**📁 Папка:** {selected_folder}")
                    st.write(f"**Документов:** {len(folder_docs)}")
                    
                    st.divider()
                    
                    # КНОПКА AI-СУММАРИЗАЦИИ (ПАПКА) С ВЫБОРОМ МОДЕЛИ
                    if st.button("🤖 AI-Резюме по папке", type="primary", key=f"folder_summarize_{selected_folder}"):
                        with st.spinner(f"🔄 AI создаёт сводное резюме по папке ({selected_model})..."):
                            summary = summarize_folder_cached(
                                selected_folder,
                                folder_docs,
                                max_length=1500,
                                model=selected_model  # ✅ Передаём выбранную модель
                            )
                            st.session_state.selected_folder_summary = {
                                "folder": selected_folder,
                                "summary": summary,
                                "docs": folder_docs
                            }
                            st.success("✅ Сводное резюме готово!")
                    
                    # Список документов в папке
                    st.write("**📄 Документы в папке:**")
                    for doc in folder_docs:
                        with st.expander(f"📄 {doc['file_name']} ({doc.get('word_count', 0)} слов)"):
                            st.write(f"**Тип:** {doc.get('doc_type', '')}")
                            st.write(f"**Страниц:** {len(doc.get('pages', []))}")
                            if st.button("Открыть", key=f"open_from_folder_{doc['id']}"):
                                st.session_state.selected_doc = doc
                                st.rerun()
                    
                    # ОКНО С AI-РЕЗЮМЕ ПО ПАПКЕ
                    if st.session_state.selected_folder_summary:
                        st.divider()
                        st.subheader(f"🤖 AI-Резюме по папке: {st.session_state.selected_folder_summary['folder']} ({selected_model})")
                        
                        with st.expander("📄 Показать сводное резюме", expanded=True):
                            if st.session_state.selected_folder_summary["summary"].startswith("#ERROR"):
                                st.error(st.session_state.selected_folder_summary["summary"])
                            else:
                                st.markdown(st.session_state.selected_folder_summary["summary"])
                                
                                col1, col2 = st.columns(2)
                                with col1:
                                    if st.button("🔄 Перегенерировать", key="regen_folder_sum"):
                                        st.session_state.selected_folder_summary = None
                                        st.rerun()
                                with col2:
                                    summary_text = st.session_state.selected_folder_summary["summary"]
                                    st.download_button(
                                        label="📥 Скачать резюме (TXT)",
                                        data=summary_text.encode('utf-8'),
                                        file_name=f"Folder_Summary_{selected_folder[:30]}.txt",
                                        mime="text/plain",
                                        use_container_width=True
                                    )
            else:
                st.info("📭 Нет сгруппированных папок. Обработайте документы в режиме 'Папка с документами'")

    st.divider()

    # ─────────────────────────────────────────────────────────────────────
    # Шаг 3: Поиск по документам
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("3. 🔍 Поиск по документам")
    st.caption("💡 Поиск с синонимами: 'счет' найдёт 'invoice', 'счет-фактура'")

    col1, col2 = st.columns([4, 1])
    with col1:
        search_query = st.text_input("Поисковый запрос", placeholder="Например: тариф, затраты, амортизация...", key="search_query_input")
    with col2:
        use_ai = st.checkbox("AI-синонимы", value=True)

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        doc_type_filter = st.selectbox("Фильтр по типу", ["all", "invoice", "act", "contract", "nakladnaya", "other"], format_func=lambda x: {"all": "Все типы", "invoice": "Счета", "act": "Акты", "contract": "Договоры", "nakladnaya": "Накладные", "other": "Прочее"}.get(x, x))
    with col2:
        if st.button("🔎 Найти", use_container_width=True, type="primary"):
            if search_query:
                results = search_in_documents(st.session_state.scan_db, search_query, use_ai)
                if doc_type_filter != "all":
                    results = [r for r in results if r["document"].get("doc_type") == doc_type_filter]
                st.session_state.search_results = results
            else:
                st.warning("Введите поисковый запрос")
    with col3:
        if st.session_state.search_results:
            if st.button("📥 Экспорт", use_container_width=True):
                output = export_search_results(st.session_state.search_results, "txt")
                filename = f"SearchResults_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                st.download_button(label="📄 Скачать TXT", data=output, file_name=filename, mime="text/plain", use_container_width=True)

    if st.session_state.search_results:
        st.success(f"✅ Найдено документов: {len(st.session_state.search_results)}")
        for i, res in enumerate(st.session_state.search_results, 1):
            doc = res["document"]
            with st.expander(f"🔍 {i}. {doc['file_name']} ({res['total_matches']} совпадений)", expanded=(i==1)):
                col1, col2 = st.columns([2, 1])
                with col1:
                    st.write(f"**Тип:** {doc.get('doc_type', '')}")
                    st.write(f"**Страниц:** {len(doc.get('pages', []))}")
                    st.write(f"**Обработан:** {doc.get('processed_at', '')[:10]}")
                with col2:
                    if st.button(f"📄 Открыть", key=f"open_doc_{doc['id']}"):
                        st.session_state.selected_doc = doc
                        st.rerun()
                st.write("**📍 Совпадения:**")
                for match in res["matches"][:5]:
                    st.info(f"📃 Страница {match['page']}: {match['context']}")
                if len(res["matches"]) > 5:
                    st.caption(f"... и ещё {len(res['matches']) - 5} совпадений")

    st.divider()

    # ─────────────────────────────────────────────────────────────────────
    # Шаг 4: Управление базой
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("4. ⚙️ Управление базой")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("💾 Экспорт базы (JSON)", use_container_width=True):
            json_str = json.dumps(st.session_state.scan_db, ensure_ascii=False, indent=2)
            filename = f"ScansDB_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            st.download_button(label="📄 Скачать JSON", data=json_str, file_name=filename, mime="application/json", use_container_width=True)
    with col2:
        uploaded_db = st.file_uploader("📥 Импорт базы", type=['json'], key="import_db")
        if uploaded_db:
            try:
                db_data = json.load(uploaded_db)
                st.session_state.scan_db = db_data
                save_scans_db(st.session_state.scan_db)
                st.success("✅ База импортирована")
                st.rerun()
            except Exception as e:
                st.error(f"❌ Ошибка импорта: {str(e)}")

    # ─────────────────────────────────────────────────────────────────────
    # Справка
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("💡 Как использовать", expanded=False):
        st.write("""
**Возможности:**
- Распознавание через EasyOCR (PDF, JPG, PNG, TIFF, DOCX, TXT)
- **GPU ускорение:** Включите в настройках OCR (требуется NVIDIA CUDA)
- **Режимы:** Один файл ИЛИ Папка с документами
- **Резюме:** По одному документу ИЛИ по всей папке (с указанием что в каком файле)
- **🆕 Выбор модели:** phi3 (быстрее), llama3.2 (качественнее)
- Поиск с синонимами: 'тариф' найдёт 'цена', 'ставка'
- Экспорт: TXT, DOCX, JSON

**Модели для суммаризации:**
- **phi3** (2.3 GB) — ✅ Рекомендуется для 4GB VRAM, быстрее
- **llama3.2** (3.2 GB) — ✅ Лучше качество, но требует больше памяти
- **gemma2:2b** (1.6 GB) — ✅✅ Самый быстрый, но меньше качество

**Хранение:**
- База: `data/doc_scanner/scans_db.json`
- Резюме: `data/doc_scanner/summaries/`
- Временные файлы: `data/doc_scanner/temp/`
        """)

# =============================================================================
# Запуск
# =============================================================================
if __name__ == "__main__":
    show_doc_scanner()