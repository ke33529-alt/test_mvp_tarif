# streamlit_pages/tariff_planner.py
import streamlit as st
import pandas as pd
import networkx as nx
from datetime import datetime, timedelta
import random

# =============================================================================
# Функции анализа зависимостей
# =============================================================================

def extract_organization_mentions(text, known_orgs, current_inn=None):
    """Извлекает упоминания других организаций из текста заявки"""
    mentions = []
    text_lower = text.lower()
    
    for org in known_orgs:
        # Пропускаем саму организацию
        if current_inn and org['inn'] == current_inn:
            continue
        
        # Поиск по названию или ИНН
        org_name = org.get('organization', '')
        org_inn = str(org.get('inn', ''))
        
        if org_name and org_name.lower() in text_lower or (org_inn and org_inn in text):
            mentions.append(org['inn'])
    
    return list(set(mentions))

def build_dependency_graph(applications):
    """Строит граф зависимостей на основе упоминаний в заявках"""
    
    G = nx.DiGraph()
    
    # Добавляем узлы
    for app in applications:
        G.add_node(app['inn'], name=app['organization'], sphere=app.get('sphere', 'unknown'))
    
    # Добавляем рёбра: А упоминает Б → А зависит от Б
    for app in applications:
        mentions = extract_organization_mentions(
            app.get('text', ''), 
            applications,
            current_inn=app['inn']
        )
        for mentioned_inn in mentions:
            if mentioned_inn in G.nodes:
                G.add_edge(app['inn'], mentioned_inn)
    
    return G

def detect_cycles(G):
    """Находит циклические зависимости"""
    try:
        cycles = list(nx.simple_cycles(G))
        return cycles
    except:
        return []

def topological_order_with_deadlines(G, applications, deadlines):
    """Топологическая сортировка с учётом дедлайнов"""
    
    G_copy = G.copy()
    deadline_map = {app['inn']: app.get('deadline') for app in applications}
    
    result = []
    in_degree = dict(G_copy.in_degree())
    available = [n for n, d in in_degree.items() if d == 0]
    
    while available:
        available.sort(key=lambda x: deadline_map.get(x) or datetime.max)
        current = available.pop(0)
        result.append(current)
        
        for neighbor in G_copy.successors(current):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0 and neighbor not in result and neighbor not in available:
                available.append(neighbor)
    
    remaining = [n for n in G.nodes if n not in result]
    result.extend(remaining)
    
    return result

def calculate_approval_dates(order, applications, start_date, deadlines):
    """Рассчитывает даты утверждения"""
    
    schedule = {}
    current_date = start_date
    deadline_map = {app['inn']: app.get('deadline') for app in applications}
    
    for inn in order:
        app = next((a for a in applications if a['inn'] == inn), None)
        if not app:
            continue
        
        deadline = deadline_map.get(inn)
        if deadline and deadline < current_date + timedelta(days=3):
            schedule[inn] = deadline
        else:
            schedule[inn] = current_date
        
        current_date = schedule[inn] + timedelta(days=1)
    
    return schedule

# =============================================================================
# Генерация демо-данных (40 организаций)
# =============================================================================

def generate_demo_data():
    """Генерирует 40 организаций с реалистичными зависимостями"""
    
    spheres_ru = {
        "heat": "Теплоснабжение",
        "water": "Водоснабжение",
        "wastewater": "Водоотведение",
        "tko": "ТКО",
        "electricity": "Электроснабжение"
    }
    
    org_types = ["ООО", "АО", "МУП", "ПАО"]
    cities = ["Москва", "Казань", "Самара", "Уфа", "Пермь", "Волгоград", "Ростов", "Омск"]
    
    applications = []
    base_inn = 7700000000
    
    # 1. Генерируем базовые организации (40 шт)
    for i in range(40):
        inn = str(base_inn + i)
        sphere_key = random.choice(list(spheres_ru.keys()))
        
        applications.append({
            "inn": inn,
            "organization": f"{random.choice(org_types)} «{cities[i % len(cities)]}-{spheres_ru[sphere_key]}»",
            "sphere": spheres_ru[sphere_key],
            "text": "",
            "deadline": datetime(2025, 5, 1) + timedelta(days=random.randint(0, 60))
        })
    
    # 2. Создаём зависимости в тексте заявок
    # Логика: Тепло зависит от Электричества и Воды. Вода зависит от Электричества.
    for app in applications:
        text_parts = [f"Заявка на тариф {app['sphere']} на 2025 год."]
        
        if "Тепло" in app['sphere']:
            # Тепло зависит от электричества (насосы) и воды (подпитка)
            deps = [a for a in applications if "Электроснабжение" in a['sphere']][:2]
            deps += [a for a in applications if "Водоснабжение" in a['sphere']][:1]
            for d in deps:
                text_parts.append(f"Учтены затраты на услуги {d['organization']} ({d['inn']}).")
        
        elif "Водоснабжение" in app['sphere'] or "Водоотведение" in app['sphere']:
            # Вода зависит от электричества
            deps = [a for a in applications if "Электроснабжение" in a['sphere']][:1]
            for d in deps:
                text_parts.append(f"Расходы на электроэнергию по договору с {d['organization']} ({d['inn']}).")
        
        elif "ТКО" in app['sphere']:
            # ТКО зависит от транспорта (условно электричество/топливо)
            deps = [a for a in applications if "Электроснабжение" in a['sphere']][:1]
            for d in deps:
                text_parts.append(f"Затраты на ГСМ и энергию {d['organization']} ({d['inn']}).")
        
        app['text'] = " ".join(text_parts)
    
    # 3. Добавим один явный цикл для демонстрации (Орг 1 ↔ Орг 2)
    if len(applications) > 1:
        applications[0]['text'] += f" Взаимные услуги с {applications[1]['organization']}."
        applications[1]['text'] += f" Взаимные услуги с {applications[0]['organization']}."
    
    return applications

# =============================================================================
# Визуализация графа
# =============================================================================

def visualize_dependency_graph(G, applications, schedule, cycles):
    """Создаёт визуализацию графа зависимостей"""
    
    try:
        import plotly.graph_objects as go
        
        pos = nx.spring_layout(G, k=2, iterations=50, seed=42)
        
        node_x, node_y, node_text, node_color = [], [], [], []
        
        for node in G.nodes():
            x, y = pos[node]
            app = next((a for a in applications if a['inn'] == node), None)
            
            node_x.append(x)
            node_y.append(y)
            
            status = "⚠️ ЦИКЛ" if any(node in cycle for cycle in cycles) else "✅"
            deadline = app.get('deadline') if app else None
            dl_text = f"Дедлайн: {deadline.strftime('%d.%m.%Y')}" if deadline else "Нет дедлайна"
            approved = schedule.get(node)
            appr_text = f"План: {approved.strftime('%d.%m.%Y')}" if approved else ""
            
            # Сокращаем имя для отображения
            short_name = (app['organization'] if app else node)[:20] + '...'
            node_text.append(f"{app['organization'] if app else node}<br>{status}<br>{dl_text}<br>{appr_text}")
            
            if any(node in cycle for cycle in cycles):
                node_color.append('#ef4444')
            elif deadline and deadline < (schedule.get(node) or datetime.now()) + timedelta(days=7):
                node_color.append('#f59e0b')
            else:
                node_color.append('#22c55e')
        
        edge_x, edge_y = [], []
        for edge in G.edges():
            x0, y0 = pos[edge[0]]
            x1, y1 = pos[edge[1]]
            edge_x.extend([x0, x1, None])
            edge_y.extend([y0, y1, None])
        
        fig = go.Figure()
        
        fig.add_trace(go.Scatter(
            x=edge_x, y=edge_y,
            line=dict(width=1, color='#94a3b8'),
            hoverinfo='skip',
            mode='lines'
        ))
        
        fig.add_trace(go.Scatter(
            x=node_x, y=node_y,
            mode='markers+text',
            marker=dict(size=20, color=node_color, line=dict(width=2, color='white')),
            text=[next((a['organization'] for a in applications if a['inn'] == n), n)[:15] + '...' for n in G.nodes()],
            textposition="bottom center",
            hovertext=node_text,
            hoverinfo='text',
            name='Организации'
        ))
        
        fig.update_layout(
            title="🔗 Граф зависимостей тарифных заявок",
            showlegend=False,
            hovermode='closest',
            margin=dict(b=20, l=5, r=5, t=40),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            plot_bgcolor='white',
            height=600
        )
        
        return fig
        
    except ImportError:
        st.warning("⚠️ Установите plotly: pip install plotly")
        return None

# =============================================================================
# Экспорт в Excel
# =============================================================================

def export_schedule_to_excel(applications, schedule, order, cycles, G):
    """Экспортирует план в Excel"""
    
    rows = []
    for i, inn in enumerate(order, 1):
        app = next((a for a in applications if a['inn'] == inn), {})
        
        deps = list(G.predecessors(inn)) if G else []
        dep_names = []
        for dep_inn in deps:
            dep_app = next((a for a in applications if a['inn'] == dep_inn), {})
            dep_names.append(dep_app.get('organization', dep_inn))
        
        rows.append({
            "Порядок": i,
            "Организация": app.get('organization', inn),
            "ИНН": inn,
            "Сфера": app.get('sphere', ''),
            "Плановая дата утверждения": schedule.get(inn),
            "Дедлайн по НПА": app.get('deadline'),
            "Циклическая зависимость": "⚠️ ДА" if any(inn in c for c in cycles) else "Нет",
            "Зависит от": ", ".join(dep_names) if dep_names else "-"
        })
    
    df = pd.DataFrame(rows)
    output = f"tariff_schedule_{datetime.now().strftime('%Y%m%d')}.xlsx"
    df.to_excel(output, index=False)
    
    return output

# =============================================================================
# Интерфейс Streamlit
# =============================================================================

def show_tariff_planner():
    """Страница Планировщика тарифной кампании"""
    
    st.header("🗓️ Планировщик тарифной кампании")
    st.info("📌 Определяет порядок утверждения тарифов на основе взаимных зависимостей в НВВ")
    
    if "applications" not in st.session_state:
        st.session_state.applications = []
    if "graph" not in st.session_state:
        st.session_state.graph = None
    if "schedule" not in st.session_state:
        st.session_state.schedule = {}
    if "order" not in st.session_state:
        st.session_state.order = []
    if "cycles" not in st.session_state:
        st.session_state.cycles = []
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 1: Загрузка данных
    # ─────────────────────────────────────────────────────────────────────
    st.subheader("1. Загрузка заявок")
    
    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("📥 40 организаций (Демо)", use_container_width=True):
            st.session_state.applications = generate_demo_data()
            st.success(f"✅ Загружено {len(st.session_state.applications)} заявок")
            st.session_state.graph = None  # Сброс предыдущего анализа
            st.rerun()
    
    with col2:
        if st.button("📥 Загрузить из «Анализатора заявок»", use_container_width=True):
            # Заглушка для интеграции
            st.info("ℹ️ Интеграция с Анализатором заявок будет доступна после сохранения данных в общем хранилище.")
    
    # Ручной ввод
    with st.expander("➕ Добавить заявку вручную", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            new_inn = st.text_input("ИНН", key="new_inn")
        with col2:
            new_org = st.text_input("Организация", key="new_org")
        with col3:
            new_sphere = st.selectbox("Сфера", ["Теплоснабжение", "Водоснабжение", "Водоотведение", "ТКО", "Электроснабжение"], key="new_sphere")
        new_deadline = st.date_input("Дедлайн", key="new_deadline")
        new_text = st.text_area("Текст заявки", key="new_text")
        
        if st.button("Добавить", key="add_app_btn"):
            if new_inn and new_org:
                st.session_state.applications.append({
                    "inn": new_inn,
                    "organization": new_org,
                    "sphere": new_sphere,
                    "text": new_text,
                    "deadline": datetime.combine(new_deadline, datetime.min.time())
                })
                st.success("✅ Заявка добавлена")
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 2: Анализ зависимостей
    # ─────────────────────────────────────────────────────────────────────
    if st.session_state.applications:
        st.divider()
        st.subheader("2. Анализ зависимостей")
        st.write(f"📊 Всего заявок: **{len(st.session_state.applications)}**")
        
        if st.button("🔍 Проанализировать зависимости", use_container_width=True, key="analyze_btn"):
            with st.spinner("🔄 Строим граф зависимостей..."):
                G = build_dependency_graph(st.session_state.applications)
                cycles = detect_cycles(G)
                
                start_date = datetime.now()
                order = topological_order_with_deadlines(
                    G, 
                    st.session_state.applications, 
                    {app['inn']: app.get('deadline') for app in st.session_state.applications}
                )
                schedule = calculate_approval_dates(
                    order,
                    st.session_state.applications,
                    start_date,
                    {app['inn']: app.get('deadline') for app in st.session_state.applications}
                )
                
                st.session_state.graph = G
                st.session_state.cycles = cycles
                st.session_state.order = order
                st.session_state.schedule = schedule
                
                st.success("✅ Анализ завершён")
                st.rerun()
        
        if st.session_state.graph:
            st.write("**📋 Рекомендуемый порядок утверждения (первые 10):**")
            
            order_data = []
            for i, inn in enumerate(st.session_state.order[:10], 1):
                app = next((a for a in st.session_state.applications if a['inn'] == inn), {})
                cycle_mark = "⚠️" if any(inn in c for c in st.session_state.cycles) else ""
                order_data.append({
                    "№": i,
                    "Организация": f"{cycle_mark} {app.get('organization', inn)}",
                    "Сфера": app.get('sphere', ''),
                    "Плановая дата": st.session_state.schedule.get(inn),
                    "Дедлайн": app.get('deadline')
                })
            
            st.dataframe(pd.DataFrame(order_data), use_container_width=True, hide_index=True)
            
            if len(st.session_state.order) > 10:
                st.caption(f"... и ещё {len(st.session_state.order) - 10} организаций в полном отчёте Excel")
            
            # ─────────────────────────────────────────────────────────────
            # Шаг 3: Календарная сетка
            # ─────────────────────────────────────────────────────────────
            st.divider()
            st.subheader("3. Календарь утверждения")
            
            calendar_data = []
            for inn, date in st.session_state.schedule.items():
                app = next((a for a in st.session_state.applications if a['inn'] == inn), {})
                calendar_data.append({
                    "Дата": date.date(),
                    "Организация": app.get('organization', inn),
                    "Сфера": app.get('sphere', ''),
                    "Дедлайн": app.get('deadline').date() if app.get('deadline') else None
                })
            
            calendar_df = pd.DataFrame(calendar_data).sort_values("Дата")
            st.dataframe(calendar_df, use_container_width=True, hide_index=True)
            
            # ─────────────────────────────────────────────────────────────
            # Шаг 4: Визуализация графа
            # ─────────────────────────────────────────────────────────────
            st.divider()
            st.subheader("4. 🕸️ Граф зависимостей")
            
            st.info("""
            **Как читать граф:**
            - 🟢 Зелёный узел: нет проблем
            - 🟡 Жёлтый узел: дедлайн близко (< 7 дней)
            - 🔴 Красный узел: ⚠️ циклическая зависимость
            - Стрелка А → Б: А зависит от Б
            """)
            
            fig = visualize_dependency_graph(
                st.session_state.graph,
                st.session_state.applications,
                st.session_state.schedule,
                st.session_state.cycles
            )
            
            if fig:
                st.plotly_chart(fig, use_container_width=True)
            
            # ─────────────────────────────────────────────────────────────
            # Шаг 5: Экспорт
            # ─────────────────────────────────────────────────────────────
            st.divider()
            st.subheader("5. Экспорт")
            
            if st.button("📊 Экспортировать в Excel", use_container_width=True, key="export_btn"):
                output_path = export_schedule_to_excel(
                    st.session_state.applications,
                    st.session_state.schedule,
                    st.session_state.order,
                    st.session_state.cycles,
                    st.session_state.graph
                )
                
                with open(output_path, "rb") as f:
                    st.download_button(
                        label="⬇️ Скачать файл",
                        data=f,
                        file_name=output_path,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )
    
    with st.expander("💡 Как использовать", expanded=False):
        st.write("""
**Назначение:** Планировщик определяет порядок утверждения тарифов на основе зависимостей в НВВ.
**Логика:** Если заявка А упоминает организацию Б → Б должно быть утверждено раньше.
        """)

if __name__ == "__main__":
    show_tariff_planner()