# streamlit_pages/numeracy_check.py
import streamlit as st
import pandas as pd
import io
import random
import string
from datetime import datetime

# =============================================================================
# 📊 Нормативы численности по Приказу ФСТ №74 (MVP-заглушка)
# =============================================================================

NUMERACY_NORMS = {
    "электросети": {
        "Директор": 1,
        "Главный инженер": 1,
        "Главный бухгалтер": 1,
        "Бухгалтер": 1,
        "Инженер по охране труда": 1,
        "Инженер 1 категории": 0.005,
        "Инженер 2 категории": 0.008,
        "Электромонтер": 0.015,
        "Водитель": 0.5,
        "Уборщик служебных помещений": 0.5,
    },
    "теплосети": {
        "Директор": 1,
        "Главный инженер": 1,
        "Главный бухгалтер": 1,
        "Бухгалтер": 1,
        "Инженер по охране труда": 1,
        "Инженер 1 категории": 0.003,
        "Инженер 2 категории": 0.005,
        "Слесарь-сантехник": 0.02,
        "Оператор котельной": 0.01,
        "Водитель": 0.5,
        "Уборщик служебных помещений": 0.5,
    },
    "водоканал": {
        "Директор": 1,
        "Главный инженер": 1,
        "Главный бухгалтер": 1,
        "Бухгалтер": 1,
        "Инженер по охране труда": 1,
        "Инженер 1 категории": 0.004,
        "Инженер 2 категории": 0.006,
        "Слесарь-водопроводчик": 0.025,
        "Лаборант": 0.01,
        "Водитель": 0.5,
        "Уборщик служебных помещений": 0.5,
    },
}

# =============================================================================
# 🛠 Функции
# =============================================================================

def generate_file_id(length=6):
    """Генерирует случайный ID для названия файла"""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def parse_uploaded_file(uploaded_file):
    """Парсит загруженный файл штатного расписания"""
    try:
        df = pd.read_excel(uploaded_file)
        return df
    except Exception as e:
        st.error(f"❌ Ошибка чтения файла: {e}")
        return None

def calculate_numeracy(df, org_type, scale_param):
    """
    Сравнивает фактическую численность с нормативом
    """
    norms = NUMERACY_NORMS.get(org_type, {})
    
    results = []
    
    if "Должность" in df.columns:
        actual_counts = df.groupby("Должность").size().to_dict()
    elif "должность" in df.columns:
        actual_counts = df.groupby("должность").size().to_dict()
    else:
        col_match = [c for c in df.columns if "долж" in c.lower()]
        if col_match:
            actual_counts = df.groupby(col_match[0]).size().to_dict()
        else:
            st.warning("⚠️ Не найдена колонка с должностями. Используем пример данных.")
            actual_counts = {}
    
    all_positions = set(norms.keys()) | set(actual_counts.keys())
    
    for position in sorted(all_positions):
        norm_value = norms.get(position, 0)
        
        if isinstance(norm_value, float) and norm_value < 1:
            norm_count = round(norm_value * scale_param, 1)
        else:
            norm_count = norm_value
        
        actual_count = actual_counts.get(position, 0)
        deviation = actual_count - norm_count
        deviation_pct = round((deviation / norm_count * 100) if norm_count > 0 else 0, 1)
        
        if deviation == 0:
            status = "✅"
        elif deviation > 0 and deviation_pct <= 25:
            status = "⚠️"
        elif deviation > 0 and deviation_pct > 25:
            status = "🔴"
        else:
            status = "✅"
        
        results.append({
            "Должность": position,
            "Требуется по нормативу": norm_count,
            "Фактически в штате": actual_count,
            "Отклонение (чел.)": deviation,
            "Отклонение (%)": deviation_pct,
            "Статус": status,
        })
    
    return pd.DataFrame(results)

def create_excel_export(df_results, org_name, org_type):
    """Создаёт Excel-файл с формулами для отчёта"""
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df_results.to_excel(writer, sheet_name='Проверка численности', index=False)
        
        workbook = writer.book
        worksheet = writer.sheets['Проверка численности']
        
        worksheet['A1'] = 'Должность'
        worksheet['B1'] = 'Требуется по нормативу'
        worksheet['C1'] = 'Фактически в штате'
        worksheet['D1'] = 'Отклонение (чел.)'
        worksheet['E1'] = 'Отклонение (%)'
        worksheet['F1'] = 'Статус'
        
        for row in range(2, len(df_results) + 2):
            worksheet[f'D{row}'] = f'=C{row}-B{row}'
            worksheet[f'E{row}'] = f'=IF(B{row}>0, D{row}/B{row}*100, 0)'
        
        meta_df = pd.DataFrame([
            ['Организация', org_name],
            ['Тип организации', org_type],
            ['Дата проверки', datetime.now().strftime('%d.%m.%Y %H:%M')],
            ['Нормативный документ', 'Приказ ФСТ России от 17.02.2014 № 74'],
        ], columns=['Параметр', 'Значение'])
        meta_df.to_excel(writer, sheet_name='Метаинформация', index=False)
        
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            worksheet.column_dimensions[column_letter].width = adjusted_width
    
    output.seek(0)
    return output

# =============================================================================
# 🎨 Интерфейс Streamlit
# =============================================================================

def show_numeracy_check():
    """Основная страница проверки численности"""
    
    st.header("👥 Сверка численности с Приказом №74")
    st.info("📌 Загрузите штатное расписание для проверки на соответствие нормативам численности")
    
    st.subheader("1️⃣ Загрузка данных")
    
    col1, col2 = st.columns([3, 1])
    with col1:
        uploaded_file = st.file_uploader(
            "Загрузите штатное расписание (XLSX/XLS)",
            type=['xlsx', 'xls'],
            help="Файл должен содержать колонку 'Должность' с наименованиями должностей"
        )
    
    with col2:
        st.markdown("**Пример формата:**")
        st.code("Должность\nДиректор\nГлавный инженер\nБухгалтер\n...", language="text")
    
    st.subheader("2️⃣ Параметры организации")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        org_name = st.text_input("Название организации", placeholder="ООО «Теплосеть»")
    with col2:
        org_type = st.selectbox(
            "Тип организации",
            ["электросети", "теплосети", "водоканал"],
            help="Выберите тип для применения соответствующих нормативов"
        )
    with col3:
        scale_param = st.number_input(
            "Масштабный параметр (км сетей)",
            min_value=0.0,
            value=100.0,
            step=10.0,
            help="Протяжённость сетей для расчёта нормативной численности"
        )
    
    if uploaded_file:
        st.success(f"✅ Файл загружен: {uploaded_file.name}")
        
        if st.button("🔍 Проверить численность", type="primary", use_container_width=True):
            with st.spinner("🔄 Анализируем штатное расписание..."):
                df = parse_uploaded_file(uploaded_file)
                
                if df is not None:
                    results_df = calculate_numeracy(df, org_type, scale_param)
                    
                    st.subheader("3️⃣ Результаты проверки")
                    
                    total_positions = len(results_df)
                    overstaffed = len(results_df[results_df["Отклонение (чел.)"] > 0])
                    critical = len(results_df[results_df["Статус"] == "🔴"])
                    
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Всего должностей", total_positions)
                    col2.metric("Превышение норматива", overstaffed, delta_color="inverse")
                    col3.metric("Критические отклонения", critical, delta_color="inverse")
                    
                    st.dataframe(
                        results_df,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Должность": st.column_config.TextColumn("Должность", width="medium"),
                            "Требуется по нормативу": st.column_config.NumberColumn("Норматив", format="%.1f"),
                            "Фактически в штате": st.column_config.NumberColumn("Факт", format="%.0f"),
                            "Отклонение (чел.)": st.column_config.NumberColumn("Отклонение", format="%+.1f"),
                            "Отклонение (%)": st.column_config.NumberColumn("%", format="%+.1f%%"),
                            "Статус": st.column_config.TextColumn("Статус", width="small"),
                        }
                    )
                    
                    st.subheader("4️⃣ Экспорт отчёта")
                    
                    file_id = generate_file_id()
                    date_str = datetime.now().strftime("%Y%m%d")
                    filename = f"NumeracyCheck_{date_str}_{file_id}.xlsx"
                    
                    excel_data = create_excel_export(results_df, org_name, org_type)
                    
                    st.download_button(
                        label="📤 Скачать отчёт в Excel",
                        data=excel_data,
                        file_name=filename,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
                    
                    st.caption(f"📁 Файл: {filename}")
                    st.info("💡 Отчёт содержит формулы для самостоятельного пересчёта в Excel")
    
    else:
        st.info("📭 Файл не загружен. Загрузите штатное расписание для начала проверки.")
        
        if st.button("🧪 Показать пример результатов", use_container_width=True):
            st.session_state.show_demo = True
        
        if st.session_state.get("show_demo"):
            st.subheader("📊 Пример результатов")
            
            demo_df = pd.DataFrame([
                {"Должность": "Директор", "Требуется по нормативу": 1, "Фактически в штате": 1, "Отклонение (чел.)": 0, "Отклонение (%)": 0.0, "Статус": "✅"},
                {"Должность": "Главный инженер", "Требуется по нормативу": 1, "Фактически в штате": 1, "Отклонение (чел.)": 0, "Отклонение (%)": 0.0, "Статус": "✅"},
                {"Должность": "Главный бухгалтер", "Требуется по нормативу": 1, "Фактически в штате": 1, "Отклонение (чел.)": 0, "Отклонение (%)": 0.0, "Статус": "✅"},
                {"Должность": "Бухгалтер", "Требуется по нормативу": 1, "Фактически в штате": 2, "Отклонение (чел.)": 1, "Отклонение (%)": 100.0, "Статус": "🔴"},
                {"Должность": "Инженер 1 категории", "Требуется по нормативу": 5, "Фактически в штате": 6, "Отклонение (чел.)": 1, "Отклонение (%)": 20.0, "Статус": "⚠️"},
                {"Должность": "Электромонтер", "Требуется по нормативу": 15, "Фактически в штате": 15, "Отклонение (чел.)": 0, "Отклонение (%)": 0.0, "Статус": "✅"},
            ])
            
            st.dataframe(demo_df, use_container_width=True, hide_index=True)

if __name__ == "__main__":
    show_numeracy_check()