# streamlit_pages/document_organizer.py
import streamlit as st
import os
import shutil
import json
from datetime import datetime
from pathlib import Path
import re

# =============================================================================
# Функции
# =============================================================================

def get_spheres():
    """Сферы деятельности для чек-листов"""
    return {
        "heat": "🔥 Теплоснабжение",
        "water": "💧 Водоснабжение",
        "wastewater": "🚰 Водоотведение",
        "tko": "🗑️ ТКО",
        "electricity": "⚡ Электроснабжение"
    }

def get_document_structure():
    """Структура папок по законодательству"""
    return {
        "01_Заявки_и_расчёты": ["заявка", "расчёт", "тариф", "НВВ", "валовая выручка"],
        "02_Нормативная_база": ["приказ", "фас", "фз", "постановление", "нпа", "закон"],
        "03_Обоснования_затрат": ["амортизация", "ремонт", "ос", "оборудование", "затраты"],
        "04_Переписка_с_регулятором": ["письмо", "запрос", "ответ", "претензия", "уведомление"],
        "05_Отчётность_ФГИС": ["фгис", "отчёт", "раскрытие", "информация"],
        "06_Архив_решений": ["решение", "протокол", "заседание", "комиссия"],
        "07_Численность_и_зарплата": ["штат", "зарплата", "персонал", "численность", "оклад"],
        "08_Потери_в_сетях": ["потери", "норматив", "сети", "передача"],
        "_Нераспределённые": []
    }

def get_required_documents(sphere):
    """Чек-лист обязательных документов по сфере и НПА"""
    
    checklists = {
        "heat": {
            "name": "Приказ ФАС №1746-э (Теплоснабжение)",
            "required": [
                "Расчётная модель тарифа",
                "Реестр основных средств",
                "Дефектная ведомость",
                "Штатное расписание",
                "Расчёт потерь в сетях",
                "Договоры на поставку топлива",
                "Показания приборов учёта",
                "Пояснительная записка"
            ]
        },
        "water": {
            "name": "Приказ ФАС №346-э (Водоснабжение)",
            "required": [
                "Расчётная модель тарифа",
                "Реестр основных средств",
                "Расчёт норматива потерь воды",
                "Штатное расписание",
                "Договоры на услуги",
                "Отчёт о производстве воды",
                "Пояснительная записка"
            ]
        },
        "wastewater": {
            "name": "Приказ ФАС №346-э (Водоотведение)",
            "required": [
                "Расчётная модель тарифа",
                "Реестр очистных сооружений",
                "Лабораторные анализы",
                "Штатное расписание",
                "Договоры на услуги",
                "Отчёт о водоотведении",
                "Пояснительная записка"
            ]
        },
        "tko": {
            "name": "ПП РФ №406 (ТКО)",
            "required": [
                "Расчётная модель тарифа",
                "Договоры с перевозчиками",
                "Лицензия на утилизацию",
                "Штатное расписание",
                "Путевые листы",
                "Отчёт о транспортировании",
                "Пояснительная записка"
            ]
        },
        "electricity": {
            "name": "Приказ ФАС №1746-э (Электроснабжение)",
            "required": [
                "Расчётная модель тарифа",
                "Реестр основных средств",
                "Штатное расписание",
                "Расчёт потерь в сетях",
                "Договоры на поставку электроэнергии",
                "Показания приборов учёта",
                "Пояснительная записка"
            ]
        }
    }
    
    return checklists.get(sphere, checklists["heat"])

def get_supported_extensions():
    """Поддерживаемые форматы файлов"""
    return ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.jpeg', '.jpg', '.png']

def scan_directory(path, recursive=False):
    """Сканирует директорию и возвращает список файлов"""
    
    files = []
    path_obj = Path(path)
    
    if recursive:
        pattern = '**/*'
    else:
        pattern = '*'
    
    for file_path in path_obj.glob(pattern):
        if file_path.is_file() and file_path.suffix.lower() in get_supported_extensions():
            # Пропускаем файлы внутри структурированных папок
            if any(part.startswith('0') or part.startswith('_') for part in file_path.parts[:-1]):
                continue
            
            files.append({
                "path": str(file_path),
                "name": file_path.name,
                "suffix": file_path.suffix.lower(),
                "size": file_path.stat().st_size,
                "modified": datetime.fromtimestamp(file_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
            })
    
    return files

def classify_file(filename, content_preview=""):
    """Классифицирует файл по имени и содержимому"""
    
    filename_lower = filename.lower()
    content_lower = content_preview.lower()
    combined = filename_lower + " " + content_lower
    
    structure = get_document_structure()
    
    for folder, keywords in structure.items():
        if folder == "_Нераспределённые":
            continue
        
        for keyword in keywords:
            if keyword in combined:
                return folder
    
    return "_Нераспределённые"

def extract_tags(filename, content_preview=""):
    """Извлекает теги из файла"""
    
    combined = (filename + " " + content_preview).lower()
    
    tag_keywords = {
        "амортизация": ["амортизация", "ос", "основные средства"],
        "ремонт": ["ремонт", "восстановление", "модернизация"],
        "численность": ["численность", "штат", "персонал", "зарплата"],
        "потери": ["потери", "норматив потерь"],
        "НВВ": ["нвв", "валовая выручка", "тариф"],
        "фгис": ["фгис", "отчётность"],
        "фас": ["фас", "регулятор"]
    }
    
    tags = []
    for tag, keywords in tag_keywords.items():
        if any(kw in combined for kw in keywords):
            tags.append(tag)
    
    return tags

def generate_new_name(file_info, category, org_name=""):
    """Генерирует новое имя файла по шаблону [Тип][Дата][Организация][Комментарий]"""
    
    # Тип из категории
    type_map = {
        "01_Заявки_и_расчёты": "Заявка",
        "02_Нормативная_база": "НПА",
        "03_Обоснования_затрат": "Обоснование",
        "04_Переписка_с_регулятором": "Переписка",
        "05_Отчётность_ФГИС": "Отчёт",
        "06_Архив_решений": "Решение",
        "07_Численность_и_зарплата": "Кадры",
        "08_Потери_в_сетях": "Потери",
        "_Нераспределённые": "Файл"
    }
    
    file_type = type_map.get(category, "Файл")
    file_date = file_info.get('modified', datetime.now().strftime('%Y-%m-%d'))[:10].replace('-', '')
    organization = org_name if org_name else "Орг"
    comment = file_info['name'].split('.')[0][:30]
    
    return f"{file_type}_{file_date}_{organization}_{comment}{file_info['suffix']}"

def handle_name_conflict(dest_path):
    """Обрабатывает конфликт имён — добавляет номер"""
    
    if not dest_path.exists():
        return dest_path
    
    counter = 1
    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent
    
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1

def move_file(src_path, dest_path):
    """Перемещает файл с обработкой конфликтов"""
    
    dest_path = handle_name_conflict(dest_path)
    shutil.move(str(src_path), str(dest_path))
    return dest_path

def save_session_state(operations):
    """Сохраняет операции сессии для возможного отката"""
    
    session_file = os.path.join("data", "document_organizer", "session.json")
    os.makedirs(os.path.dirname(session_file), exist_ok=True)
    
    with open(session_file, 'w', encoding='utf-8') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "operations": operations
        }, f, ensure_ascii=False, indent=2)

def rollback_operations():
    """Откатывает операции последней сессии"""
    
    session_file = os.path.join("data", "document_organizer", "session.json")
    
    if not os.path.exists(session_file):
        return False, "Нет сохранённой сессии"
    
    with open(session_file, 'r', encoding='utf-8') as f:
        session = json.load(f)
    
    moved_count = 0
    for op in session.get("operations", []):
        if op["type"] == "move":
            try:
                src = Path(op["new_path"])
                dst = Path(op["old_path"])
                if src.exists():
                    shutil.move(str(src), str(dst))
                    moved_count += 1
            except Exception as e:
                pass
    
    # Удаляем файл сессии
    os.remove(session_file)
    
    return True, f"Восстановлено {moved_count} файлов"

def check_completeness(files_by_category, sphere):
    """Проверяет комплектность документов по чек-листу"""
    
    checklist = get_required_documents(sphere)
    found_docs = []
    missing_docs = []
    
    # Анализируем названия файлов
    all_filenames = " ".join([f["name"].lower() for cat_files in files_by_category.values() for f in cat_files])
    
    for required in checklist["required"]:
        if required.lower() in all_filenames:
            found_docs.append(required)
        else:
            missing_docs.append(required)
    
    return {
        "checklist_name": checklist["name"],
        "found": found_docs,
        "missing": missing_docs,
        "completeness_percent": round(len(found_docs) / len(checklist["required"]) * 100, 1) if checklist["required"] else 0
    }

# =============================================================================
# Интерфейс Streamlit
# =============================================================================

def show_document_organizer():
    """Страница Наведения порядка в документах"""
    
    st.header("🗂️ Наведение порядка в документах")
    st.info("📌 Организация файлов тарифной кампании по структуре законодательства")
    
    # Инициализация session_state
    if "scan_result" not in st.session_state:
        st.session_state.scan_result = None
    if "classification_result" not in st.session_state:
        st.session_state.classification_result = None
    if "operations_log" not in st.session_state:
        st.session_state.operations_log = []
    if "processing_complete" not in st.session_state:
        st.session_state.processing_complete = False
    if "selected_sphere" not in st.session_state:
        st.session_state.selected_sphere = "heat"
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 1: Выбор папки
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1. Выбор папки с документами")
    
    # Инструкция для разных ОС
    with st.expander("📖 Как получить путь к папке", expanded=False):
        st.write("""
        **Windows:**
        1. Откройте Проводник
        2. Перейдите в нужную папку
        3. Скопируйте путь из адресной строки (например: `C:\\Users\\Name\\Documents\\Tariffs`)
        
        **macOS:**
        1. Откройте Finder
        2. Перейдите в нужную папку
        3. Нажмите Cmd+L или правой кнопкой → «Свести к одному файлу»
        4. Скопируйте путь (например: `/Users/Name/Documents/Tariffs`)
        
        **Linux:**
        1. Откройте терминал
        2. Перейдите в папку: `cd /path/to/folder`
        3. Введите: `pwd` и скопируйте вывод
        """)
    
    folder_path = st.text_input(
        "📁 Путь к папке с документами",
        placeholder="C:\\Users\\Name\\Documents\\Tariffs (Windows) или /Users/Name/Documents/Tariffs (Mac/Linux)",
        key="folder_path_input"
    )
    
    # Опции сканирования
    col1, col2, col3 = st.columns(3)
    
    with col1:
        recursive = st.checkbox("📂 Сканировать подпапки", value=False, key="recursive_check")
    
    with col2:
        rename_files = st.checkbox("✏️ Переименовывать файлы", value=False, key="rename_check")
    
    with col3:
        org_name = st.text_input("🏢 Организация", value="ООО «РСО»", key="org_name_input")
    
    # Выбор сферы для чек-листа
    sphere = st.selectbox(
        "🌍 Сфера деятельности (для проверки комплектности)",
        list(get_spheres().keys()),
        format_func=lambda x: get_spheres().get(x, x),
        key="sphere_select"
    )
    
    st.session_state.selected_sphere = sphere
    
    # Кнопка сканирования
    if st.button("🔍 Сканировать папку", use_container_width=True, key="scan_btn"):
        if folder_path and os.path.exists(folder_path):
            with st.spinner("🔄 Сканирование файлов..."):
                files = scan_directory(folder_path, recursive)
                
                if files:
                    st.session_state.scan_result = {
                        "path": folder_path,
                        "files": files,
                        "recursive": recursive,
                        "rename_files": rename_files,
                        "org_name": org_name
                    }
                    st.success(f"✅ Найдено {len(files)} файлов")
                    st.rerun()
                else:
                    st.warning("⚠️ Файлы не найдены. Проверьте путь и форматы (pdf, doc, xlsx, jpeg, png)")
        else:
            st.error("❌ Укажите корректный путь к существующей папке")
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 2: Предпросмотр классификации
    # ─────────────────────────────────────────────────────────────────────
    if st.session_state.scan_result:
        scan_data = st.session_state.scan_result
        
        st.divider()
        st.subheader("2. Предпросмотр классификации")
        
        if st.session_state.classification_result is None:
            # Классификация файлов
            with st.spinner("🔄 Анализ файлов..."):
                classification = {}
                
                for file_info in scan_data["files"]:
                    # Простая эмуляция контента (в реальной версии — через AI-Сканер)
                    content_preview = file_info["name"]
                    
                    category = classify_file(file_info["name"], content_preview)
                    tags = extract_tags(file_info["name"], content_preview)
                    
                    new_name = generate_new_name(file_info, category, scan_data["org_name"]) if scan_data["rename_files"] else file_info["name"]
                    
                    if category not in classification:
                        classification[category] = []
                    
                    classification[category].append({
                        **file_info,
                        "category": category,
                        "tags": tags,
                        "new_name": new_name
                    })
                
                st.session_state.classification_result = classification
                st.rerun()
        
        # Отображение классификации
        classification = st.session_state.classification_result
        
        st.write(f"**📊 Распределение файлов по папкам:**")
        
        # Структура папок
        structure = get_document_structure()
        
        for folder, keywords in structure.items():
            files_in_folder = classification.get(folder, [])
            
            if files_in_folder:
                with st.expander(f"📁 {folder} ({len(files_in_folder)} файлов)", expanded=(folder != "_Нераспределённые")):
                    for file_info in files_in_folder:
                        col1, col2 = st.columns([3, 2])
                        
                        with col1:
                            original_name = file_info["name"]
                            new_name = file_info["new_name"]
                            
                            if scan_data["rename_files"] and original_name != new_name:
                                st.write(f"📄 `{original_name}`")
                                st.write(f"➡️ `{new_name}`")
                            else:
                                st.write(f"📄 `{original_name}`")
                        
                        with col2:
                            tags_str = ", ".join([f"`#{t}`" for t in file_info["tags"]]) if file_info["tags"] else "без тегов"
                            st.caption(f"Теги: {tags_str}")
        
        # Кнопки действий
        st.divider()
        st.subheader("3. Подтверждение")
        
        col1, col2, col3 = st.columns([2, 2, 1])
        
        with col1:
            move_btn = st.button("🚀 Переместить файлы", use_container_width=True, type="primary", key="move_btn")
        
        with col2:
            reset_btn = st.button("🔄 Начать заново", use_container_width=True, key="reset_btn")
        
        with col3:
            rollback_btn = st.button("↩️ Откат", use_container_width=True, key="rollback_btn", disabled=(len(st.session_state.operations_log) == 0))
        
        if move_btn:
            # Перемещение файлов
            operations = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            total_files = sum(len(files) for files in classification.values())
            processed = 0
            
            for category, files in classification.items():
                for file_info in files:
                    try:
                        src_path = Path(file_info["path"])
                        dest_folder = Path(scan_data["path"]) / category
                        
                        # Создаём папку если нет
                        dest_folder.mkdir(parents=True, exist_ok=True)
                        
                        # Новое имя
                        dest_name = file_info["new_name"] if scan_data["rename_files"] else file_info["name"]
                        dest_path = dest_folder / dest_name
                        
                        # Перемещение
                        old_path = str(src_path)
                        move_file(src_path, dest_path)
                        new_path = str(dest_path)
                        
                        operations.append({
                            "type": "move",
                            "old_path": old_path,
                            "new_path": new_path,
                            "category": category
                        })
                        
                        processed += 1
                        progress_bar.progress(processed / total_files)
                        status_text.text(f"Обработано {processed} из {total_files} файлов")
                        
                    except Exception as e:
                        st.error(f"❌ Ошибка с файлом {file_info['name']}: {str(e)}")
            
            # Сохранение сессии для отката
            save_session_state(operations)
            st.session_state.operations_log = operations
            st.session_state.processing_complete = True
            
            status_text.text("✅ Готово!")
            st.success(f"🎉 Обработано {processed} файлов")
            st.rerun()
        
        if reset_btn:
            st.session_state.scan_result = None
            st.session_state.classification_result = None
            st.rerun()
        
        if rollback_btn:
            success, message = rollback_operations()
            if success:
                st.success(f"✅ {message}")
                st.session_state.operations_log = []
                st.session_state.scan_result = None
                st.session_state.classification_result = None
                st.rerun()
            else:
                st.error(f"❌ {message}")
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 4: Результаты и комплектность
    # ─────────────────────────────────────────────────────────────────────
    if st.session_state.processing_complete:
        st.divider()
        st.subheader("4. Результаты")
        
        # Проверка комплектности
        completeness = check_completeness(st.session_state.classification_result, sphere)
        
        st.markdown(f"""
        <div style="background: linear-gradient(90deg, #3498db, #2c3e50); 
                    padding: 2rem; border-radius: 10px; text-align: center; margin: 1rem 0;">
            <h2 style="color: white; margin: 0;">📋 Комплектность документов</h2>
            <h1 style="color: white; margin: 0.5rem 0; font-size: 3rem;">
                {completeness['completeness_percent']}%
            </h1>
            <p style="color: #ecf0f1; margin: 0;">
                {completeness['checklist_name']}
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.success(f"✅ Найдено: {len(completeness['found'])}")
            for doc in completeness['found'][:5]:
                st.write(f"  • {doc}")
        
        with col2:
            if completeness['missing']:
                st.error(f"⚠️ Отсутствует: {len(completeness['missing'])}")
                for doc in completeness['missing'][:5]:
                    st.write(f"  • {doc}")
        
        # Поиск по файлам
        st.divider()
        st.subheader("🔍 Поиск по файлам")
        
        search_query = st.text_input("Введите запрос для поиска", key="file_search")
        
        if search_query:
            results = []
            for category, files in st.session_state.classification_result.items():
                for file_info in files:
                    if search_query.lower() in file_info["name"].lower() or \
                       any(search_query.lower() in tag for tag in file_info["tags"]):
                        results.append(f"📁 {category} → 📄 {file_info['name']}")
            
            if results:
                st.write(f"Найдено {len(results)} файлов:")
                for r in results[:10]:
                    st.write(r)
            else:
                st.info("Ничего не найдено")
        
        # Кнопка завершения
        st.divider()
        if st.button("✅ Завершить сессию", use_container_width=True):
            st.session_state.scan_result = None
            st.session_state.classification_result = None
            st.session_state.processing_complete = False
            st.session_state.operations_log = []
            st.success("Сессия завершена")
            st.rerun()
    
    # ─────────────────────────────────────────────────────────────────────
    # Справка
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("💡 Как использовать", expanded=False):
        st.write("""
**Назначение:**

Организация файлов тарифной кампании по структуре, соответствующей законодательству РФ.

**Как работает:**

1. **Укажите путь к папке** с документами (см. инструкцию выше)
2. **Выберите опции**: сканирование подпапок, переименование, сфера
3. **Просмотрите классификацию**: какие файлы куда попадут
4. **Подтвердите перемещение**: файлы будут физически перемещены в папки структуры
5. **Проверьте комплектность**: какие документы отсутствуют по чек-листу НПА

**Структура папок:**

- `01_Заявки_и_расчёты` — заявки, расчётные модели, тарифы
- `02_Нормативная_база` — приказы ФАС, ФЗ, постановления
- `03_Обоснования_затрат` — амортизация, ремонт, ОС
- `04_Переписка_с_регулятором` — письма, запросы, ответы
- `05_Отчётность_ФГИС` — отчёты, раскрытие информации
- `06_Архив_решений` — решения, протоколы
- `07_Численность_и_зарплата` — штат, зарплата, персонал
- `08_Потери_в_сетях` — нормативы, расчёты потерь
- `_Нераспределённые` — файлы без распознанной категории

**Возможности:**

- ✅ Физическое перемещение файлов (без удаления)
- ✅ Переименование по шаблону [Тип][Дата][Организация][Комментарий]
- ✅ Теги в метаданных и интерфейсе
- ✅ Проверка комплектности по чек-листу НПА
- ✅ Поиск по файлам после обработки
- ✅ Откат операций в рамках сессии

**Важно:**

- Файлы не удаляются — только перемещаются
- Нераспознанные файлы остаются в корне в папке `_Нераспределённые`
- Конфликты имён разрешаются авто-добавлением номера
        """)

# =============================================================================
# Запуск
# =============================================================================

if __name__ == "__main__":
    show_document_organizer()