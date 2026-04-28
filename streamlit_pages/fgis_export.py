# streamlit_pages/fgis_export.py
import streamlit as st
import json
import pandas as pd
import plotly.express as px
from datetime import datetime

# =============================================================================
# 🛠 Функции
# =============================================================================

def load_uploaded_file(uploaded_file):
    """Загружает Excel/CSV файл в DataFrame"""
    try:
        file_name = uploaded_file.name.lower()
        
        if file_name.endswith('.csv'):
            # Пробуем разные кодировки и разделители для CSV
            uploaded_file.seek(0)
            for encoding in ['cp1251', 'utf-8', 'latin-1']:
                for sep in [';', ',']:
                    try:
                        uploaded_file.seek(0)
                        df = pd.read_csv(uploaded_file, encoding=encoding, sep=sep)
                        return df
                    except:
                        continue
            st.error("❌ Не удалось прочитать CSV файл. Проверьте формат.")
            return None
            
        elif file_name.endswith(('.xlsx', '.xls')):
            # Excel файл
            uploaded_file.seek(0)
            df = pd.read_excel(uploaded_file)
            return df
            
        else:
            st.error("❌ Неверный формат файла. Используйте CSV или Excel.")
            return None
            
    except Exception as e:
        st.error(f"❌ Ошибка чтения файла: {e}")
        return None

def generate_json(inn, kpp, org_name, period_start, period_end, activity_sphere, articles_df, doc_type="tariff_application"):
    """Генерирует JSON из данных"""
    articles_list = []
    for _, row in articles_df.iterrows():
        articles_list.append({
            "code": str(row.get("code", "")),
            "name": str(row.get("name", "")),
            "amount": float(row.get("amount", 0)) if pd.notna(row.get("amount")) else 0
        })
    
    revenue_total = sum(item["amount"] for item in articles_list)
    
    json_data = {
        "document_type": doc_type,
        "fgis_version": "2024.1",
        "generated_at": datetime.now().isoformat(),
        "header": {
            "inn": inn,
            "kpp": kpp,
            "organization_name": org_name
        },
        "application": {
            "activity_sphere": activity_sphere,
            "tariff_period": {
                "start": period_start,
                "end": period_end
            },
            "revenue": {
                "total": revenue_total,
                "articles": articles_list
            }
        },
        "metadata": {
            "source": "AI-Тарифщик",
            "version": "1.0"
        }
    }
    
    return json_data

def validate_json(inn, org_name, period_start, period_end, activity_sphere, articles_df):
    """Проверяет обязательные поля"""
    errors = []
    
    if not inn or not inn.isdigit() or len(inn) not in [10, 12]:
        errors.append("❌ ИНН должен содержать 10 или 12 цифр")
    
    if not org_name:
        errors.append("❌ Укажите наименование организации")
    
    if not period_start or not period_end:
        errors.append("❌ Укажите период")
    
    if not activity_sphere:
        errors.append("❌ Выберите сферу деятельности")
    
    if articles_df is None or len(articles_df) == 0:
        errors.append("❌ Загрузите файл со статьями затрат")
    
    required_cols = ["code", "name", "amount"]
    if articles_df is not None:
        missing_cols = [col for col in required_cols if col not in articles_df.columns]
        if missing_cols:
            errors.append(f"❌ В таблице отсутствуют колонки: {', '.join(missing_cols)}")
    
    return errors

# =============================================================================
# 🎨 Интерфейс Streamlit
# =============================================================================

def show_fgis_export():
    """Страница экспорта в ФГИС Тариф"""
    
    st.header("📤 Экспорт в ФГИС Тариф")
    st.info("📌 Загрузите файл (CSV/Excel) → Переименуйте колонки → Сформируйте JSON")
    
    # ─────────────────────────────────────────────────────────────────────
    # 📁 Шаг 1: Загрузка файла
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1️⃣ Загрузка файла (CSV/Excel)")
    
    uploaded_file = st.file_uploader(
        "📄 Загрузите файл со статьями затрат",
        type=['xlsx', 'xls', 'csv'],
        help="Поддерживаемые форматы: Excel (.xlsx, .xls) или CSV"
    )
    
    articles_df = None
    
    if uploaded_file:
        st.info(f"📂 Загружен файл: **{uploaded_file.name}** ({uploaded_file.size / 1024:.1f} КБ)")
        articles_df = load_uploaded_file(uploaded_file)
        
        if articles_df is not None:
            st.success(f"✅ Файл успешно прочитан: {len(articles_df)} строк, {len(articles_df.columns)} колонок")
            st.write("**Исходные колонки файла:**")
            st.write(list(articles_df.columns))
            st.dataframe(articles_df.head(10), use_container_width=True)
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # 📝 Шаг 2: Переименование колонок (маппинг)
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("2️⃣ Переименование колонок")
    st.info("💡 Сопоставьте колонки из файла с обязательными полями JSON")
    
    if articles_df is not None:
        original_columns = list(articles_df.columns)
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            code_col = st.selectbox(
                "🔢 Код статьи (code)",
                options=[""] + original_columns,
                help="Колонка с кодом статьи затрат"
            )
        
        with col2:
            name_col = st.selectbox(
                "📝 Наименование (name)",
                options=[""] + original_columns,
                help="Колонка с наименованием статьи"
            )
        
        with col3:
            amount_col = st.selectbox(
                "💰 Сумма (amount)",
                options=[""] + original_columns,
                help="Колонка с суммой (руб.)"
            )
        
        if code_col and name_col and amount_col:
            renamed_df = articles_df[[code_col, name_col, amount_col]].copy()
            renamed_df.columns = ["code", "name", "amount"]
            
            # ✅ Конвертируем amount в число (обязательно!)
            renamed_df["amount"] = pd.to_numeric(renamed_df["amount"], errors='coerce').fillna(0)
            
            articles_df = renamed_df
            
            st.success("✅ Колонки переименованы и данные преобразованы")
            
            # ──────────────────────────────────────────────────────
            # 📊 АНАЛИТИКА: Таблица + Круговая диаграмма
            # ──────────────────────────────────────────────────────
            st.subheader("📊 Аналитика статей затрат")
            
            # Сортируем по сумме (по убыванию)
            sorted_df = articles_df.sort_values("amount", ascending=False).reset_index(drop=True)
            
            # Показываем ТОП-10 статей
            st.write("**🔝 ТОП-10 статей затрат:**")
            top_10 = sorted_df.head(10)
            st.dataframe(top_10, use_container_width=True)
            
            # Показываем общую сумму
            total_revenue = sorted_df["amount"].sum()
            st.metric("💰 Валовая выручка (итого)", f"{total_revenue:,.0f} ₽", delta_color="normal")
            
            # Круговая диаграмма (Pie Chart)
            st.write("**🥧 Структура затрат (круговая диаграмма):**")
            
            # Для наглядности: ТОП-7 + "Прочие"
            if len(sorted_df) > 7:
                top_7 = sorted_df.head(7).copy()
                other_sum = sorted_df.iloc[7:]["amount"].sum()
                if other_sum > 0:
                    other_row = pd.DataFrame([{"code": "999", "name": "📦 Прочие", "amount": other_sum}])
                    chart_df = pd.concat([top_7, other_row], ignore_index=True)
                else:
                    chart_df = top_7
            else:
                chart_df = sorted_df.copy()
            
            # Создаём подпись с процентами
            chart_df["label"] = chart_df.apply(
                lambda row: f"{row['name']}\n({row['amount']/total_revenue*100:.1f}%)" if total_revenue > 0 else row['name'], 
                axis=1
            )
            
            # Строим график
            fig = px.pie(
                chart_df,
                values="amount",
                names="label",
                title="Распределение статей затрат",
                color_discrete_sequence=px.colors.qualitative.Pastel,
                hole=0.3  # Донат-чарт
            )
            fig.update_traces(textposition="inside", textinfo="percent+label")
            fig.update_layout(showlegend=False, height=400)
            
            st.plotly_chart(fig, use_container_width=True)
            
            # Экспорт аналитики
            with st.expander("📥 Скачать аналитику"):
                csv_data = sorted_df.to_csv(index=False, encoding='utf-8-sig', sep=';')
                st.download_button(
                    label="📊 Скачать таблицу (CSV)",
                    data=csv_data,
                    file_name="articles_analytics.csv",
                    mime="text/csv"
                )
                
                json_data = json.dumps(chart_df.to_dict('records'), ensure_ascii=False, indent=2)
                st.download_button(
                    label="📄 Скачать данные (JSON)",
                    data=json_data,
                    file_name="articles_data.json",
                    mime="application/json"
                )
            
        else:
            st.warning("⚠️ Выберите все 3 колонки для продолжения")
    else:
        st.warning("⚠️ Сначала загрузите файл (Шаг 1)")
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # 🏢 Шаг 3: Общие сведения
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("3️⃣ Общие сведения об организации")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        inn = st.text_input("ИНН *", placeholder="10 или 12 цифр")
    with col2:
        kpp = st.text_input("КПП", placeholder="9 символов")
    with col3:
        org_name = st.text_input("Наименование организации *", placeholder="ООО «Теплосеть»")
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # 📅 Шаг 4: Период и сфера
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("4️⃣ Период и сфера деятельности")
    
    col1, col2 = st.columns(2)
    with col1:
        period_start = st.date_input("Начало периода *", value=datetime(2025, 1, 1))
    with col2:
        period_end = st.date_input("Конец периода *", value=datetime(2025, 12, 31))
    
    activity_sphere = st.radio(
        "Сфера деятельности *",
        ["water_supply", "water_drainage", "waste_management", "heat_supply"],
        format_func=lambda x: {
            "water_supply": "💧 Водоснабжение",
            "water_drainage": "🚰 Водоотведение",
            "waste_management": "🗑️ ТКО",
            "heat_supply": "🔥 Теплоснабжение"
        }.get(x, x),
        horizontal=True
    )
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # 🔍 Шаг 5: Генерация JSON
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("5️⃣ Генерация JSON")
    
    if st.button("🔍 Сформировать JSON", type="primary", use_container_width=True):
        errors = validate_json(inn, org_name, period_start.isoformat(), period_end.isoformat(), activity_sphere, articles_df)
        
        if errors:
            st.error("❌ Обнаружены ошибки:")
            for e in errors:
                st.write(e)
            st.info("💡 Исправьте ошибки перед формированием JSON")
        else:
            fgis_json = generate_json(
                inn=inn,
                kpp=kpp,
                org_name=org_name,
                period_start=period_start.isoformat(),
                period_end=period_end.isoformat(),
                activity_sphere=activity_sphere,
                articles_df=articles_df
            )
            
            st.success("✅ JSON сформирован!")
            
            st.subheader("📄 Предпросмотр JSON")
            st.json(fgis_json)
            
            st.subheader("📤 Скачать файл")
            
            sphere_suffix = activity_sphere
            json_str = json.dumps(fgis_json, ensure_ascii=False, indent=2)
            file_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"FGIS_TariffApplication_{sphere_suffix}_{inn}_{file_id}.json"
            
            st.download_button(
                label="📥 Скачать JSON для ФГИС Тариф",
                data=json_str,
                file_name=filename,
                mime="application/json",
                use_container_width=True,
            )
            
            st.caption(f"📁 Файл: {filename}")
            st.info("💡 Загрузите этот файл в ФГИС Тариф через кнопку «Импорт JSON»")
    
    # ─────────────────────────────────────────────────────────────────────
    # 💡 Пример файла
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("💡 Пример файла для загрузки", expanded=False):
        st.write("**CSV формат (запятая, UTF-8):**")
        st.code("""Код статьи,Наименование,Сумма
301,Транспортирование ТКО,40000000
302,Размещение на полигоне,20000000
303,Обработка ТКО,15000000""")
        
        st.write("**Excel формат:**")
        st.write("Те же 3 колонки: Код статьи | Наименование | Сумма")

# =============================================================================
# 🚀 Запуск
# =============================================================================

if __name__ == "__main__":
    show_fgis_export()