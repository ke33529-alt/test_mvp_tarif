# streamlit_pages/calc_sheet.py
import streamlit as st
import pandas as pd
import json
import os
import re
from datetime import datetime
import io

# =============================================================================
# Функции
# =============================================================================

def create_empty_sheet(rows=50, cols=26):
    """Создаёт пустую таблицу 50x26 (A-Z)"""
    data = [["" for _ in range(cols)] for _ in range(rows)]
    columns = [chr(65 + i) for i in range(cols)]
    return {"columns": columns, "data": data}

def calculate_formula(formula, df, sheets_data=None, current_sheet_name=None):
    """Вычисляет формулы Excel с поддержкой ссылок на другие листы"""
    
    if not formula or not isinstance(formula, str) or not formula.startswith("="):
        return formula
    
    expr = formula[1:].upper()
    
    try:
        # 1. Обработка SUM
        sum_pattern = r'SUM\(([A-Z])(\d+):([A-Z])(\d+)\)'
        for match in re.finditer(sum_pattern, expr):
            start_col, start_row, end_col, end_row = match.groups()
            start_col_idx = ord(start_col.upper()) - ord('A')
            end_col_idx = ord(end_col.upper()) - ord('A')
            start_row_idx = int(start_row) - 1
            end_row_idx = int(end_row) - 1
            
            total = 0
            for row_idx in range(start_row_idx, end_row_idx + 1):
                for col_idx in range(start_col_idx, end_col_idx + 1):
                    if row_idx < len(df) and col_idx < len(df.columns):
                        val = df.iloc[row_idx, col_idx]
                        if isinstance(val, (int, float)):
                            total += val
                        elif isinstance(val, str) and val.replace('.', '').replace('-', '').isdigit():
                            total += float(val)
            
            expr = expr.replace(match.group(0), str(total))
        
        # 2. Обработка AVERAGE
        avg_pattern = r'AVERAGE\(([A-Z])(\d+):([A-Z])(\d+)\)'
        for match in re.finditer(avg_pattern, expr):
            start_col, start_row, end_col, end_row = match.groups()
            start_col_idx = ord(start_col.upper()) - ord('A')
            end_col_idx = ord(end_col.upper()) - ord('A')
            start_row_idx = int(start_row) - 1
            end_row_idx = int(end_row) - 1
            
            total = 0
            count = 0
            for row_idx in range(start_row_idx, end_row_idx + 1):
                for col_idx in range(start_col_idx, end_col_idx + 1):
                    if row_idx < len(df) and col_idx < len(df.columns):
                        val = df.iloc[row_idx, col_idx]
                        if isinstance(val, (int, float)):
                            total += val
                            count += 1
                        elif isinstance(val, str) and val.replace('.', '').replace('-', '').isdigit():
                            total += float(val)
                            count += 1
            
            avg = total / count if count > 0 else 0
            expr = expr.replace(match.group(0), str(avg))
        
        # 3. Замена ссылок на другие листы
        cross_sheet_pattern = r'([\'"]?)([^\'"!]+)\1 !([A-Z])(\d+)'
        def replace_cross_sheet(match):
            sheet_name = match.group(2).strip()
            col = match.group(3)
            row = int(match.group(4)) - 1
            if sheets_data and sheet_name in sheets_data:
                target_sheet = sheets_data[sheet_name]
                target_df = pd.DataFrame(target_sheet["data"])
                target_df.columns = target_sheet["columns"][:len(target_df.columns)]
                col_idx = ord(col.upper()) - ord('A')
                if row < len(target_df) and col_idx < len(target_df.columns):
                    val = target_df.iloc[row, col_idx]
                    if isinstance(val, (int, float)):
                        return str(val)
                    elif isinstance(val, str) and val.replace('.', '').replace('-', '').isdigit():
                        return val
            return "0"
        
        expr = re.sub(cross_sheet_pattern, replace_cross_sheet, expr, flags=re.IGNORECASE)
        
        # 4. Замена ссылок на ячейки текущего листа
        cell_pattern = r'([A-Z])(\d+)'
        def replace_cell(match):
            col = match.group(1)
            row = int(match.group(2)) - 1
            col_idx = ord(col.upper()) - ord('A')
            if row < len(df) and col_idx < len(df.columns):
                val = df.iloc[row, col_idx]
                if isinstance(val, (int, float)):
                    return str(val)
                elif isinstance(val, str) and val.replace('.', '').replace('-', '').isdigit():
                    return val
            return "0"
        
        expr = re.sub(cell_pattern, replace_cell, expr)
        
        # 5. Безопасное вычисление
        allowed_names = {"sum": sum, "len": len, "round": round, "abs": abs, "max": max, "min": min}
        result = eval(expr, {"__builtins__": {}}, allowed_names)
        
        return round(result, 2) if isinstance(result, float) else result
    
    except Exception as e:
        return f"#ERROR"

def load_saved_projects():
    """Загружает список сохраненных проектов"""
    projects_dir = os.path.join("data", "calc_projects")
    if not os.path.exists(projects_dir):
        os.makedirs(projects_dir, exist_ok=True)
        return []
    
    projects = []
    for filename in os.listdir(projects_dir):
        if filename.endswith(".json"):
            filepath = os.path.join(projects_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    project = json.load(f)
                    project["filename"] = filename
                    projects.append(project)
            except:
                continue
    
    return sorted(projects, key=lambda x: x.get("updated", ""), reverse=True)

def save_project(sheets_data, project_name):
    """Сохраняет проект в файл"""
    projects_dir = os.path.join("data", "calc_projects")
    if not os.path.exists(projects_dir):
        os.makedirs(projects_dir, exist_ok=True)
    
    project_data = {
        "name": project_name,
        "sheets": sheets_data,
        "updated": datetime.now().isoformat()
    }
    
    filename = f"{project_name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    filepath = os.path.join(projects_dir, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(project_data, f, ensure_ascii=False, indent=2)
    
    return filepath

def export_to_excel(sheets_data, project_name):
    """Экспортирует в Excel"""
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_name, sheet_data in sheets_data.items():
            df = pd.DataFrame(sheet_data["data"])
            df.columns = sheet_data["columns"][:len(df.columns)]
            df.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    
    output.seek(0)
    return output

# =============================================================================
# Интерфейс Streamlit
# =============================================================================

def show_calc_sheet():
    """Страница Расчетного листа"""
    
    st.header("📊 Расчетный лист")
    st.info("📌 Таблица с формулами Excel и ссылками на другие листы")
    
    # Инициализация session_state
    if "sheets_data" not in st.session_state or not st.session_state.sheets_data:
        st.session_state.sheets_data = {
            "Лист 1": create_empty_sheet(),
            "Лист 2": create_empty_sheet(),
            "Лист 3": create_empty_sheet(),
            "Лист 4": create_empty_sheet(),
            "Лист 5": create_empty_sheet()
        }
    if "current_sheet" not in st.session_state:
        st.session_state.current_sheet = "Лист 1"
    
    # ─────────────────────────────────────────────────────────────────────
    # Управление проектами
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1. Проект")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        saved_projects = load_saved_projects()
        project_options = ["— Новый проект —"] + [p.get("name", p.get("filename", "")) for p in saved_projects]
        selected_project = st.selectbox("Загрузить", project_options, key="load_project")
    
    with col2:
        if st.button("📁 Открыть", use_container_width=True, disabled=(selected_project == "— Новый проект —")):
            if selected_project != "— Новый проект —":
                project_data = next((p for p in saved_projects if p.get("name") == selected_project), None)
                if project_data:
                    st.session_state.sheets_data = project_data.get("sheets", {})
                    st.session_state.current_sheet = list(st.session_state.sheets_data.keys())[0]
                    st.rerun()
    
    with col3:
        project_name = st.text_input("Название", value=f"Проект_{datetime.now().strftime('%Y%m%d')}", key="proj_name", label_visibility="collapsed")
    
    with col4:
        if st.button("💾 Сохранить", use_container_width=True):
            filepath = save_project(st.session_state.sheets_data, project_name)
            st.success(f"✅ Сохранено: {filepath}")
    
    # Вторая строка кнопок
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        if st.button("🆕 Новый проект", use_container_width=True, type="primary"):
            st.session_state.sheets_data = {
                "Лист 1": create_empty_sheet(),
                "Лист 2": create_empty_sheet(),
                "Лист 3": create_empty_sheet(),
                "Лист 4": create_empty_sheet(),
                "Лист 5": create_empty_sheet()
            }
            st.session_state.current_sheet = "Лист 1"
            st.rerun()
    
    with col2:
        st.write("")
    
    with col3:
        st.write("")
    
    with col4:
        if st.button("🗑 Очистить", use_container_width=True):
            st.session_state.sheets_data = {
                "Лист 1": create_empty_sheet(),
                "Лист 2": create_empty_sheet(),
                "Лист 3": create_empty_sheet(),
                "Лист 4": create_empty_sheet(),
                "Лист 5": create_empty_sheet()
            }
            st.session_state.current_sheet = "Лист 1"
            st.rerun()
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Выбор листа
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("2. Листы")
    
    sheet_names = list(st.session_state.sheets_data.keys())
    
    cols = st.columns(min(len(sheet_names), 5))
    
    for i, sheet_name in enumerate(sheet_names):
        with cols[i]:
            is_selected = (sheet_name == st.session_state.current_sheet)
            if st.button(
                f"{'📍' if is_selected else '📄'} {sheet_name}",
                key=f"sheet_btn_{sheet_name}",
                use_container_width=True,
                type="primary" if is_selected else "secondary"
            ):
                st.session_state.current_sheet = sheet_name
                st.rerun()
    
    # Кнопка добавления листа
    col1, col2 = st.columns([4, 1])
    with col1:
        st.write("")
    with col2:
        new_sheet_name = st.text_input("Новый лист", placeholder="Название", key="new_sheet_input")
        if st.button("➕ Добавить", use_container_width=True):
            if new_sheet_name and new_sheet_name not in st.session_state.sheets_data:
                st.session_state.sheets_data[new_sheet_name] = create_empty_sheet()
                st.session_state.current_sheet = new_sheet_name
                st.rerun()
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Таблица с формулами
    # ─────────────────────────────────────────────────────────────────────
    st.subheader(f"3. Таблица: {st.session_state.current_sheet}")
    st.caption("💡 Формулы: =A1+B2 | Ссылки на другие листы: ='Лист 2'!A1 | =SUM('Лист 2'!A1:A10)")
    
    current_sheet_data = st.session_state.sheets_data[st.session_state.current_sheet]
    
    # Создаём DataFrame для редактирования
    df = pd.DataFrame(current_sheet_data["data"])
    df.columns = current_sheet_data["columns"][:len(df.columns)]
    
    # Редактируемая таблица (БЕЗ on_change callback - он вызывает ошибку)
    edited_df = st.data_editor(
        df,
        num_rows="dynamic",
        use_container_width=True,
        height=600,
        key=f"editor_{st.session_state.current_sheet}"
    )
    
    # Сохраняем изменения СРАЗУ после редактирования
    current_sheet_data["data"] = edited_df.values.tolist()
    current_sheet_data["columns"] = list(edited_df.columns)
    st.session_state.sheets_data[st.session_state.current_sheet] = current_sheet_data
    
    # Пересчёт формул
    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔢 Пересчитать формулы", use_container_width=True, type="primary"):
            for row_idx in range(len(edited_df)):
                for col_idx in range(len(edited_df.columns)):
                    cell_value = edited_df.iloc[row_idx, col_idx]
                    if isinstance(cell_value, str) and cell_value.startswith("="):
                        result = calculate_formula(
                            cell_value, 
                            edited_df, 
                            st.session_state.sheets_data,
                            st.session_state.current_sheet
                        )
                        edited_df.iloc[row_idx, col_idx] = result
            
            current_sheet_data["data"] = edited_df.values.tolist()
            st.session_state.sheets_data[st.session_state.current_sheet] = current_sheet_data
            st.success("✅ Формулы пересчитаны (включая ссылки на другие листы)")
            st.rerun()
    
    with col2:
        st.metric("Ячеек", f"{len(edited_df)} x {len(edited_df.columns)}")
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Примеры формул
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("📚 Примеры формул")
    
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.markdown("**Текущий лист:**")
        st.code("=A1+B2\n=A1*B2\n=SUM(A1:A10)\n=AVERAGE(B1:B5)")
    
    with col2:
        st.markdown("**Другие листы:**")
        st.code("='Лист 2'!A1\n='Лист 1'!B5\n='Лист 3'!A1+'Лист 2'!B2")
    
    with col3:
        st.markdown("**SUM с других листов:**")
        st.code("=SUM('Лист 2'!A1:A10)\n=SUM('Лист 1'!B1:'Лист 1'!B10)")
    
    st.divider()
    
    # ─────────────────────────────────────────────────────────────────────
    # Экспорт
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("4. Экспорт")
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("📥 Экспорт в Excel", use_container_width=True, type="primary"):
            output = export_to_excel(st.session_state.sheets_data, st.session_state.current_sheet)
            filename = f"CalcSheet_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            
            st.download_button(
                label="📄 Скачать Excel",
                data=output,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
    
    with col2:
        if st.button("📥 Экспорт в JSON", use_container_width=True):
            json_str = json.dumps(st.session_state.sheets_data, ensure_ascii=False, indent=2)
            filename = f"CalcSheet_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            
            st.download_button(
                label="📄 Скачать JSON",
                data=json_str,
                file_name=filename,
                mime="application/json",
                use_container_width=True,
            )
    
    # ─────────────────────────────────────────────────────────────────────
    # Справка
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("💡 Как использовать", expanded=False):
        st.write("""
        **Возможности:**
        
        1. **50 строк x 26 колонок** (A-Z) на каждом листе
        2. **5 листов** по умолчанию + добавление новых
        3. **Формулы Excel:** =A1+B2, =SUM(A1:A10), =AVERAGE()
        4. **Ссылки на другие листы:** ='Лист 2'!A1
        5. **SUM с других листов:** =SUM('Лист 2'!A1:A10)
        6. **Сохранение проектов** в data/calc_projects
        7. **Экспорт** в Excel (.xlsx) и JSON
        8. **Мгновенное сохранение** при редактировании
        
        **Примеры ссылок на другие листы:**
        
        - `='Лист 2'!A1` — значение ячейки A1 с Лист 2
        - `='Лист 1'!B5*2` — значение B5 с Лист 1, умноженное на 2
        - `='Лист 2'!A1+'Лист 3'!B2` — сумма ячеек с разных листов
        - `=SUM('Лист 2'!A1:A10)` — сумма диапазона с Лист 2
        - `=AVERAGE('Лист 1'!C1:C20)` — среднее значение с Лист 1
        
        **Важно:**
        
        - Название листа в кавычках: `'Лист 2'` или без: `Лист2`
        - После названия листа обязательно `!`
        - Нажмите "🔢 Пересчитать формулы" для обновления
        - Данные сохраняются автоматически при редактировании
        """)

# =============================================================================
# Запуск
# =============================================================================

if __name__ == "__main__":
    show_calc_sheet()