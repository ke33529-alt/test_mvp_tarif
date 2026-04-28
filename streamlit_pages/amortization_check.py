# streamlit_pages/amortization_check.py
import streamlit as st
import pandas as pd
import io
import random
import string
from datetime import datetime

# =============================================================================
# 📊 Нормативы амортизационных групп (Постановление Правительства РФ № 1)
# =============================================================================

AMORTIZATION_GROUPS = {
    1: {"name": "Группа 1", "min_years": 1, "max_years": 2, "examples": "Машины и оборудование лёгкое"},
    2: {"name": "Группа 2", "min_years": 2, "max_years": 3, "examples": "Компьютеры, оргтехника"},
    3: {"name": "Группа 3", "min_years": 3, "max_years": 5, "examples": "Средства светофорные, измерительные приборы"},
    4: {"name": "Группа 4", "min_years": 5, "max_years": 7, "examples": "Машины и оборудование (насосы, компрессоры)"},
    5: {"name": "Группа 5", "min_years": 7, "max_years": 10, "examples": "Транспортные средства, котлы"},
    6: {"name": "Группа 6", "min_years": 10, "max_years": 15, "examples": "Скважина, сооружения"},
    7: {"name": "Группа 7", "min_years": 15, "max_years": 20, "examples": "Сети тепловые, водоснабжение"},
    8: {"name": "Группа 8", "min_years": 20, "max_years": 25, "examples": "Здания производственные"},
    9: {"name": "Группа 9", "min_years": 25, "max_years": 30, "examples": "Здания жилые"},
    10: {"name": "Группа 10", "min_years": 30, "max_years": 35, "examples": "Здания капитальные, мосты"},
}

# =============================================================================
# 🛠 Функции
# =============================================================================

def generate_file_id(length=6):
    """Генерирует случайный ID для названия файла"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def parse_excel_file(uploaded_file):
    """Парсит загруженный Excel файл с обработкой ошибок"""
    try:
        df = pd.read_excel(uploaded_file)
        return df
    except Exception as e:
        st.error(f"❌ Ошибка чтения файла: {e}")
        return None

def validate_amortization_group(group, useful_life):
    """Проверяет соответствие группы ОС сроку полезного использования"""
    try:
        group = int(group)
        useful_life = float(useful_life)
    except (ValueError, TypeError):
        return False, "Неверный формат данных"
    
    if group not in AMORTIZATION_GROUPS:
        return False, "Неверная группа ОС"
    
    norm = AMORTIZATION_GROUPS[group]
    if useful_life < norm["min_years"] or useful_life > norm["max_years"]:
        return False, f"Срок не соответствует группе (норма: {norm['min_years']}-{norm['max_years']} лет)"
    
    return True, "✅"

def calculate_amortization(initial_cost, useful_life, method="linear"):
    """Расчёт годовой суммы амортизации с безопасной конвертацией"""
    # Конвертируем стоимость в число
    try:
        if isinstance(initial_cost, str):
            # Удаляем ₽, пробелы, запятые как разделители
            initial_cost = float(initial_cost.replace('₽', '').replace(' ', '').replace(',', '.').strip())
        else:
            initial_cost = float(initial_cost)
    except (ValueError, AttributeError, TypeError):
        return 0.0
    
    # Конвертируем срок
    try:
        useful_life = float(useful_life)
    except (ValueError, TypeError):
        return 0.0
    
    if useful_life <= 0:
        return 0.0
    
    if method == "linear":
        annual = initial_cost / useful_life
    else:
        # Упрощённый нелинейный метод
        annual = initial_cost * (2 / useful_life)
    
    return round(annual, 2)

def calculate_average_annual_amortization(df):
    """Расчёт среднегодовой суммы амортизации по формуле ФСТ"""
    if 'Амортизация_начало' in df.columns and 'Амортизация_конец' in df.columns:
        return ((df['Амортизация_начало'] + df['Амортизация_конец']) / 2).mean()
    elif 'Амортизация_год' in df.columns:
        return df['Амортизация_год'].mean()
    else:
        return 0.0

def compare_with_registry(amortization_df, registry_df):
    """Сравнивает объекты амортизации с реестром инфраструктуры"""
    results = []
    
    if registry_df is None or len(registry_df) == 0:
        # Если реестр не загружен — помечаем все как "не проверено"
        for _ in range(len(amortization_df)):
            results.append({
                "registry_status": "⚪",
                "registry_comment": "Реестр не загружен"
            })
        return results
    
    for idx, row in amortization_df.iterrows():
        inv_number = str(row.get('Инвентарный номер', '')).strip()
        object_name = str(row.get('Объект ОС', '')).strip()
        
        # Поиск по инвентарному номеру или наименованию
        match = None
        
        if 'Инвентарный номер' in registry_df.columns and inv_number:
            match = registry_df[registry_df['Инвентарный номер'].astype(str).str.strip() == inv_number]
        
        if match is None or len(match) == 0:
            if 'Наименование' in registry_df.columns and object_name:
                match = registry_df[
                    registry_df['Наименование'].astype(str).str.strip().str.contains(object_name[:20], case=False, na=False)
                ]
        
        if match is not None and len(match) > 0:
            registry_row = match.iloc[0]
            
            status = "✅"
            comment = "Совпадает"
            
            # Проверка расхождений по дате
            if 'Дата ввода' in row and 'Дата ввода' in registry_row:
                try:
                    date_diff = abs((pd.to_datetime(row['Дата ввода']) - pd.to_datetime(registry_row['Дата ввода'])).days)
                    if date_diff > 30:
                        status = "⚠️"
                        comment = f"Расхождение даты: {date_diff} дн."
                except:
                    pass
            
            # Проверка расхождений по стоимости
            if 'Первоначальная стоимость' in row and 'Первоначальная стоимость' in registry_row:
                try:
                    row_cost = float(str(row['Первоначальная стоимость']).replace('₽','').replace(' ','').replace(',','.'))
                    reg_cost = float(str(registry_row['Первоначальная стоимость']).replace('₽','').replace(' ','').replace(',','.'))
                    if row_cost > 0:
                        cost_diff_pct = abs(row_cost - reg_cost) / row_cost
                        if cost_diff_pct > 0.1:
                            status = "⚠️"
                            comment = f"Расхождение стоимости: {cost_diff_pct*100:.1f}%"
                except:
                    pass
        else:
            status = "❌"
            comment = "Не обнаружено в реестре инфраструктуры"
        
        results.append({
            "registry_status": status,
            "registry_comment": comment
        })
    
    return results

def create_grouped_summary(df):
    """Создаёт сводную таблицу по амортизационным группам"""
    if 'Группа ОС' not in df.columns or len(df) == 0:
        return pd.DataFrame()
    
    summary = df.groupby('Группа ОС').agg(
        Объектов=('Объект ОС', 'count'),
        Ошибок=('Статус_группы', lambda x: (x == '🔴').sum()),
        Сумма_отклонений=('Отклонение_сумма', 'sum')
    ).reset_index()
    
    summary['Название группы'] = summary['Группа ОС'].apply(
        lambda x: f"Группа {x} ({AMORTIZATION_GROUPS.get(int(x), {}).get('examples', '')})" if pd.notna(x) and int(x) in AMORTIZATION_GROUPS else f"Группа {x}"
    )
    
    return summary

def create_excel_export(df, registry_results):
    """Создаёт Excel-файл с отчётом"""
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        # Лист 1: Детальная таблица
        df.to_excel(writer, sheet_name='Проверка амортизации', index=False)
        
        # Лист 2: Свод по группам
        summary = create_grouped_summary(df)
        if len(summary) > 0:
            summary.to_excel(writer, sheet_name='Свод по группам', index=False)
        
        # Лист 3: Реестр
        if registry_results:
            registry_df = pd.DataFrame(registry_results)
            registry_df.to_excel(writer, sheet_name='Сверка с реестром', index=False)
        
        # Лист 4: Метаинформация
        meta_df = pd.DataFrame([
            ['Дата проверки', datetime.now().strftime('%d.%m.%Y %H:%M')],
            ['Нормативный документ', 'Постановление Правительства РФ № 1'],
            ['Объектов проверено', len(df)],
            ['Ошибок найдено', len(df[df['Статус_группы'] == '🔴'])],
            ['Не в реестре', len([r for r in registry_results if r.get('registry_status') == '❌'])],
        ], columns=['Параметр', 'Значение'])
        meta_df.to_excel(writer, sheet_name='Метаинформация', index=False)
    
    output.seek(0)
    return output

# =============================================================================
# 🎨 Интерфейс Streamlit
# =============================================================================

def show_amortization_check():
    """Основная страница проверки амортизации"""
    
    st.header("🏭 Проверка ведомости амортизации")
    st.info("📌 Загрузите ведомость амортизации и реестр инфраструктуры для проверки на соответствие нормативам")
    
    # ─────────────────────────────────────────────────────────────────────
    # 📁 Загрузка файлов
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1️⃣ Загрузка данных")
    
    col1, col2 = st.columns(2)
    with col1:
        uploaded_amortization = st.file_uploader(
            "📄 Ведомость амортизации (XLSX/XLS)",
            type=['xlsx', 'xls'],
            help="Файл должен содержать: Объект ОС, Инвентарный номер, Группа ОС, Дата ввода, Первоначальная стоимость, Срок полезного использования"
        )
    
    with col2:
        uploaded_registry = st.file_uploader(
            "📥 Реестр инфраструктуры (ЕИАС/ФГИС Тариф) — опционально",
            type=['xlsx', 'xls', 'xml'],
            help="Выгрузка из реестра инфраструктуры для сверки объектов"
        )
    
    # Кнопка для авто-загрузки из API (заглушка)
    if st.button("🌐 Загрузить из ФГИС Тариф (API)", disabled=True):
        st.info("🚧 Функция в разработке. Требуется интеграция с API ФГИС Тариф.")
    
    # ─────────────────────────────────────────────────────────────────────
    # 🔍 Анализ
    # ─────────────────────────────────────────────────────────────────────
    if uploaded_amortization:
        st.success(f"✅ Файл загружен: {uploaded_amortization.name}")
        
        if st.button("🔍 Проверить амортизацию", type="primary", use_container_width=True):
            with st.spinner("🔄 Анализируем ведомость амортизации..."):
                # Парсинг файла
                df = parse_excel_file(uploaded_amortization)
                
                # Парсинг реестра (если загружен)
                registry_df = None
                if uploaded_registry:
                    registry_df = parse_excel_file(uploaded_registry)
                    if registry_df is not None:
                        st.info(f"📊 Реестр загружен: {len(registry_df)} объектов")
                
                if df is not None:
                    # Проверка обязательных колонок
                    required_cols = ['Объект ОС', 'Группа ОС', 'Срок полезного использования', 'Первоначальная стоимость']
                    missing_cols = [c for c in required_cols if c not in df.columns]
                    
                    if missing_cols:
                        st.error(f"❌ В файле отсутствуют колонки: {', '.join(missing_cols)}")
                        st.info("💡 Добавьте недостающие колонки в файл и загрузите повторно")
                        st.stop()
                    
                    # Расчёт амортизации и проверка
                    results = []
                    for idx, row in df.iterrows():
                        # Безопасное извлечение и конвертация данных
                        try:
                            group = int(row.get('Группа ОС', 0))
                        except (ValueError, TypeError):
                            group = 0
                        
                        try:
                            useful_life = float(row.get('Срок полезного использования', 0))
                        except (ValueError, TypeError):
                            useful_life = 0
                        
                        try:
                            initial_cost = float(str(row.get('Первоначальная стоимость', 0)).replace('₽','').replace(' ','').replace(',','.'))
                        except (ValueError, TypeError, AttributeError):
                            initial_cost = 0
                        
                        # Проверка группы
                        group_valid, group_comment = validate_amortization_group(group, useful_life)
                        
                        # Расчёт амортизации
                        annual_amortization = calculate_amortization(initial_cost, useful_life)
                        
                        # Сравнение с реестром (заглушка — реальное сравнение в compare_with_registry)
                        registry_status = "⚪"
                        registry_comment = "—"
                        
                        results.append({
                            "Объект ОС": row.get('Объект ОС', ''),
                            "Инв. номер": row.get('Инвентарный номер', ''),
                            "Группа ОС": group,
                            "Срок (факт)": useful_life,
                            "Срок (норм.)": f"{AMORTIZATION_GROUPS.get(group, {}).get('min_years', '?')}-{AMORTIZATION_GROUPS.get(group, {}).get('max_years', '?')} лет",
                            "Первоначальная стоимость": f"{initial_cost:,.0f} ₽",
                            "Амортизация (год)": f"{annual_amortization:,.0f} ₽",
                            "Статус_группы": "✅" if group_valid else "🔴",
                            "Комментарий": group_comment,
                            "Реестр": registry_status,
                            "Комментарий реестра": registry_comment,
                            "Отклонение_сумма": 1 if not group_valid else 0,
                        })
                    
                    results_df = pd.DataFrame(results)
                    
                    # Если загружен реестр — делаем реальное сравнение
                    if registry_df is not None:
                        registry_results = compare_with_registry(results_df, registry_df)
                        for i, reg in enumerate(registry_results):
                            results_df.at[i, 'Реестр'] = reg['registry_status']
                            results_df.at[i, 'Комментарий реестра'] = reg['registry_comment']
                            if reg['registry_status'] == '❌':
                                results_df.at[i, 'Отклонение_сумма'] += 1
                    
                    # ──────────────────────────────────────────────────────
                    # 📊 Сводная таблица по группам (Вариант Б)
                    # ──────────────────────────────────────────────────────
                    st.subheader("2️⃣ Свод по амортизационным группам")
                    
                    summary = create_grouped_summary(results_df)
                    
                    if len(summary) > 0:
                        # Сортировка по номеру группы
                        summary = summary.sort_values('Группа ОС')
                        
                        # Отображение сводной таблицы с разворачиванием
                        for idx, row in summary.iterrows():
                            group_num = int(row['Группа ОС'])
                            group_info = AMORTIZATION_GROUPS.get(group_num, {})
                            group_name = group_info.get('name', f"Группа {group_num}")
                            examples = group_info.get('examples', '')
                            
                            status_icon = "🔴" if row['Ошибок'] > 0 else "✅"
                            
                            # Заголовок группы
                            with st.expander(f"{status_icon} {group_name} ({examples}) — {int(row['Объектов'])} объектов, {int(row['Ошибок'])} ошибок, {row['Сумма_отклонений']:,.0f} ₽", expanded=False):
                                # Фильтруем объекты этой группы
                                group_objects = results_df[results_df['Группа ОС'] == group_num]
                                
                                # Отображение детальной таблицы
                                st.dataframe(
                                    group_objects[['Объект ОС', 'Инв. номер', 'Срок (факт)', 'Срок (норм.)', 'Амортизация (год)', 'Статус_группы', 'Реестр', 'Комментарий реестра']],
                                    use_container_width=True,
                                    hide_index=True,
                                    column_config={
                                        "Объект ОС": st.column_config.TextColumn("Объект ОС", width="medium"),
                                        "Инв. номер": st.column_config.TextColumn("Инв. номер", width="small"),
                                        "Срок (факт)": st.column_config.NumberColumn("Срок (факт)", format="%.1f"),
                                        "Срок (норм.)": st.column_config.TextColumn("Срок (норм.)"),
                                        "Амортизация (год)": st.column_config.TextColumn("Амортизация (год)"),
                                        "Статус_группы": st.column_config.TextColumn("Статус", width="small"),
                                        "Реестр": st.column_config.TextColumn("Реестр", width="small"),
                                        "Комментарий реестра": st.column_config.TextColumn("Комментарий", width="medium"),
                                    }
                                )
                                
                                # Кнопка экспорта для группы
                                col1, col2 = st.columns([3, 1])
                                with col2:
                                    if st.button(f"📤 Экспорт группы {group_num}", key=f"export_group_{group_num}"):
                                        group_output = io.BytesIO()
                                        with pd.ExcelWriter(group_output, engine='openpyxl') as writer:
                                            group_objects.to_excel(writer, sheet_name=f'Группа {group_num}', index=False)
                                        group_output.seek(0)
                                        st.download_button(
                                            label="⬇️ Скачать",
                                            data=group_output,
                                            file_name=f"Amortization_Group{group_num}_{datetime.now().strftime('%Y%m%d')}.xlsx",
                                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                        )
                    else:
                        st.info("📭 Нет данных для сводной таблицы")
                    
                    # ──────────────────────────────────────────────────────
                    # 📊 Итоговые метрики
                    # ──────────────────────────────────────────────────────
                    st.subheader("3️⃣ Итоговые показатели")
                    
                    total_objects = len(results_df)
                    total_errors = len(results_df[results_df['Статус_группы'] == '🔴'])
                    registry_not_found = len(results_df[results_df['Реестр'] == '❌'])
                    total_deviation = results_df['Отклонение_сумма'].sum()
                    
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Всего объектов", total_objects)
                    col2.metric("Ошибок в группах", total_errors, delta_color="inverse")
                    col3.metric("Не в реестре", registry_not_found, delta_color="inverse")
                    col4.metric("Сумма отклонений", f"{total_deviation:,.0f} ₽", delta_color="inverse")
                    
                    # Среднегодовая амортизация
                    avg_annual = calculate_average_annual_amortization(df)
                    if avg_annual > 0:
                        st.info(f"📊 **Среднегодовая сумма амортизации:** {avg_annual:,.0f} ₽")
                    
                    # ──────────────────────────────────────────────────────
                    # 📤 Экспорт отчёта
                    # ──────────────────────────────────────────────────────
                    st.subheader("4️⃣ Экспорт отчёта")
                    
                    file_id = generate_file_id()
                    date_str = datetime.now().strftime("%Y%m%d")
                    filename = f"AmortizationCheck_{date_str}_{file_id}.xlsx"
                    
                    registry_results = compare_with_registry(results_df, registry_df) if registry_df is not None else []
                    excel_data = create_excel_export(results_df, registry_results)
                    
                    st.download_button(
                        label="📤 Скачать полный отчёт в Excel",
                        data=excel_data,
                        file_name=filename,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
                    
                    st.caption(f"📁 Файл: {filename}")
                    st.info("💡 Отчёт содержит 4 листа: Проверка, Свод по группам, Сверка с реестром, Метаинформация")
    
    else:
        # Заглушка для демонстрации
        st.info("📭 Файл не загружен. Загрузите ведомость амортизации для начала проверки.")
        
        if st.button("🧪 Показать пример результатов", use_container_width=True):
            st.session_state.show_amortization_demo = True
        
        if st.session_state.get("show_amortization_demo"):
            st.subheader("📊 Пример сводной таблицы")
            
            demo_summary = pd.DataFrame([
                {"Группа ОС": 4, "Название группы": "Группа 4 (машины)", "Объектов": 45, "Ошибок": 3, "Сумма_отклонений": 120000},
                {"Группа ОС": 7, "Название группы": "Группа 7 (сети)", "Объектов": 67, "Ошибок": 8, "Сумма_отклонений": 980000},
                {"Группа ОС": 10, "Название группы": "Группа 10 (здания)", "Объектов": 33, "Ошибок": 1, "Сумма_отклонений": 150000},
            ])
            
            for idx, row in demo_summary.iterrows():
                group_num = int(row['Группа ОС'])
                with st.expander(f"{'🔴' if row['Ошибок'] > 0 else '✅'} {row['Название группы']} — {int(row['Объектов'])} объектов, {int(row['Ошибок'])} ошибок, {row['Сумма_отклонений']:,.0f} ₽", expanded=False):
                    st.write(f"**Примеры объектов:**")
                    st.write(f"• Насос циркуляционный (инв. 001234)")
                    st.write(f"• Компрессор винтовой (инв. 005678)")
                    st.write(f"• ... и ещё {int(row['Объектов']) - 2} объектов")

# =============================================================================
# 🚀 Запуск
# =============================================================================

if __name__ == "__main__":
    show_amortization_check()