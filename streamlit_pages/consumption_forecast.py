# streamlit_pages/consumption_forecast.py
import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_percentage_error, mean_squared_error, r2_score
import plotly.graph_objects as go
import io
import random

# =============================================================================
# Конфигурация параметров по сферам
# =============================================================================

def get_sphere_config():
    """Конфигурация параметров для каждой сферы"""
    return {
        "Водоснабжение": {
            "target": "Объём подачи воды (тыс. м³)",
            "features": [
                "Количество потребителей (физ. лица)",
                "Количество потребителей (юр. лица)",
                "Длина сетей (км)",
                "Потери в сетях (%)",
                "Объём водоотведения (тыс. м³)"
            ],
            "weather": False
        },
        "Водоотведение": {
            "target": "Объём водоотведения (тыс. м³)",
            "features": [
                "Количество потребителей (физ. лица)",
                "Количество потребителей (юр. лица)",
                "Длина сетей (км)",
                "Потери в сетях (%)",
                "Объём водоподачи (тыс. м³)"
            ],
            "weather": False
        },
        "Теплоснабжение": {
            "target": "Отпуск тепла (тыс. Гкал)",
            "features": [
                "Длина тепловых сетей (км)",
                "Потери тепла (тыс. Гкал)",
                "Количество теплопотребляющих установок",
                "Площадь отапливаемых помещений (тыс. м²)"
            ],
            "weather": True,
            "weather_label": "Среднегодовая температура (°C)"
        },
        "Электроснабжение": {
            "target": "Отпуск электроэнергии (млн кВт⋅ч)",
            "features": [
                "Длина электрических сетей (км)",
                "Количество точек учёта",
                "Потери в сетях (млн кВт⋅ч)",
                "Максимальная нагрузка (МВт)"
            ],
            "weather": True,
            "weather_label": "Среднегодовая температура (°C)"
        },
        "Газоснабжение": {
            "target": "Отпуск газа (млн м³)",
            "features": [
                "Длина газовых сетей (км)",
                "Количество абонентов (физ. лица)",
                "Количество абонентов (юр. лица)",
                "Объём подачи (млн м³)"
            ],
            "weather": True,
            "weather_label": "Среднегодовая температура (°C)"
        },
        "ТКО": {
            "target": "Объём отходов (тыс. м³)",
            "features": [
                "Количество потребителей (физ. лица)",
                "Количество потребителей (юр. лица)",
                "Норматив накопления (м³/чел)",
                "Частота вывоза (раз/мес)"
            ],
            "weather": False
        }
    }

# =============================================================================
# Генерация тестовых данных (ИСПРАВЛЕНО)
# =============================================================================

def generate_test_data(sphere, history_years, config):
    """Генерирует реалистичные тестовые данные для выбранной сферы"""
    
    random.seed(42)
    
    periods = pd.date_range(
        end=datetime.now().replace(day=1),
        periods=history_years * 12,
        freq='M'
    )
    
    target = config['target']
    features = config['features']
    has_weather = config.get('weather', False)
    weather_label = config.get('weather_label', 'Температура')
    
    # Базовые значения по сферам (ИСПРАВЛЕНО: нет обращения к config внутри словаря)
    base_values = {
        "Водоснабжение": {
            target: (50, 80),
            "Количество потребителей (физ. лица)": (10000, 15000),
            "Количество потребителей (юр. лица)": (200, 400),
            "Длина сетей (км)": (150, 200),
            "Потери в сетях (%)": (10, 18),
            "Объём водоотведения (тыс. м³)": (40, 60)
        },
        "Водоотведение": {
            target: (40, 60),
            "Количество потребителей (физ. лица)": (10000, 15000),
            "Количество потребителей (юр. лица)": (200, 400),
            "Длина сетей (км)": (120, 180),
            "Потери в сетях (%)": (8, 15),
            "Объём водоподачи (тыс. м³)": (50, 80)
        },
        "Теплоснабжение": {
            target: (100, 200),
            "Длина тепловых сетей (км)": (200, 400),
            "Потери тепла (тыс. Гкал)": (10, 25),
            "Количество теплопотребляющих установок": (500, 1000),
            "Площадь отапливаемых помещений (тыс. м²)": (500, 1000),
            "weather": (-5, 15)
        },
        "Электроснабжение": {
            target: (20, 40),
            "Длина электрических сетей (км)": (300, 600),
            "Количество точек учёта": (5000, 10000),
            "Потери в сетях (млн кВт⋅ч)": (2, 5),
            "Максимальная нагрузка (МВт)": (50, 100),
            "weather": (5, 25)
        },
        "Газоснабжение": {
            target: (10, 25),
            "Длина газовых сетей (км)": (400, 800),
            "Количество абонентов (физ. лица)": (15000, 25000),
            "Количество абонентов (юр. лица)": (100, 300),
            "Объём подачи (млн м³)": (12, 30),
            "weather": (-5, 20)
        },
        "ТКО": {
            target: (5, 15),
            "Количество потребителей (физ. лица)": (20000, 40000),
            "Количество потребителей (юр. лица)": (300, 600),
            "Норматив накопления (м³/чел)": (0.08, 0.12),
            "Частота вывоза (раз/мес)": (15, 25)
        }
    }
    
    data = {col: [] for col in [target] + features}
    if has_weather:
        data[weather_label] = []
    
    sphere_base = base_values.get(sphere, base_values["Водоснабжение"])
    
    for i, period in enumerate(periods):
        month = period.month
        
        # Сезонность
        if sphere in ["Теплоснабжение", "Газоснабжение"]:
            seasonal_factor = 1.5 if month in [12, 1, 2] else (0.6 if month in [6, 7, 8] else 1.0)
        elif sphere == "Электроснабжение":
            seasonal_factor = 1.3 if month in [12, 1, 2, 6, 7, 8] else 1.0
        else:
            seasonal_factor = 1.0
        
        for col in [target] + features:
            if col in sphere_base:
                min_val, max_val = sphere_base[col]
                
                # Для погоды — сезонная вариация
                if col == "weather" or 'температура' in col.lower():
                    if sphere == "Теплоснабжение":
                        temp_base = -10 if month in [12, 1, 2] else (20 if month in [6, 7, 8] else 5)
                        value = temp_base + random.uniform(-3, 3)
                    else:
                        value = random.uniform(min_val, max_val)
                else:
                    base = (min_val + max_val) / 2
                    trend = i * (max_val - min_val) * 0.001
                    noise = random.uniform(-0.1, 0.1) * base
                    value = base * seasonal_factor + trend + noise
                    value = max(min_val, min(max_val, value))
            else:
                value = random.uniform(0, 100)
            
            data[col].append(round(value, 2))
        
        # Добавляем погоду если нужно
        if has_weather:
            if sphere == "Теплоснабжение":
                temp_base = -10 if month in [12, 1, 2] else (20 if month in [6, 7, 8] else 5)
                temp_val = temp_base + random.uniform(-3, 3)
            elif sphere == "Электроснабжение":
                temp_val = random.uniform(5, 25)
            else:  # Газ
                temp_val = random.uniform(-5, 20)
            data[weather_label].append(round(temp_val, 1))
    
    df = pd.DataFrame(data, index=periods)
    return df

# =============================================================================
# Функции моделирования
# =============================================================================

def prepare_regression_data(df, target, features, weather_feature=None):
    """Подготавливает данные для регрессии"""
    
    X_cols = features.copy()
    if weather_feature and weather_feature in df.columns:
        X_cols.append(weather_feature)
    
    df_clean = df[[target] + X_cols].dropna()
    
    if len(df_clean) < 3:
        return None, None, None
    
    X = df_clean[X_cols].values
    y = df_clean[target].values
    
    return X, y, X_cols

def fit_linear_regression(X, y):
    """Обучает линейную регрессию"""
    
    model = LinearRegression()
    model.fit(X, y)
    
    y_pred = model.predict(X)
    
    metrics = {
        "R²": r2_score(y, y_pred),
        "RMSE": np.sqrt(mean_squared_error(y, y_pred)),
        "MAPE": mean_absolute_percentage_error(y, y_pred) * 100 if np.all(y != 0) else None
    }
    
    return model, metrics, y_pred

def forecast_with_confidence(model, X_future, alpha=0.05):
    """Прогноз с 95% доверительным интервалом"""
    
    y_pred = model.predict(X_future)
    
    residuals = model.predict(model._X_train) - model._y_train if hasattr(model, '_X_train') else np.zeros_like(y_pred)
    std_err = np.std(residuals) if len(residuals) > 0 else np.mean(y_pred) * 0.1
    
    z = 1.96
    
    lower = y_pred - z * std_err
    upper = y_pred + z * std_err
    
    return y_pred, lower, upper

# =============================================================================
# Визуализация
# =============================================================================

def plot_forecast_chart(historical_df, target, forecast_index, forecast_pred, forecast_lower, forecast_upper, metrics):
    """Создаёт красивый график прогноза"""
    
    fig = go.Figure()
    
    fig.add_trace(go.Scatter(
        x=historical_df.index,
        y=historical_df[target],
        mode='lines+markers',
        name='Факт',
        line=dict(color='#2563eb', width=2),
        marker=dict(size=6)
    ))
    
    fig.add_trace(go.Scatter(
        x=forecast_index,
        y=forecast_pred,
        mode='lines+markers',
        name='Прогноз',
        line=dict(color='#dc2626', width=2, dash='dot'),
        marker=dict(size=6)
    ))
    
    fig.add_trace(go.Scatter(
        x=pd.concat([
            pd.Series(forecast_index), 
            pd.Series(forecast_index)[::-1]
        ]),
        y=pd.concat([
            pd.Series(forecast_upper), 
            pd.Series(forecast_lower)[::-1]
        ]),
        fill='toself',
        fillcolor='rgba(220, 38, 38, 0.15)',
        line=dict(color='rgba(255,255,255,0)'),
        name='95% доверительный интервал',
        hoverinfo='skip'
    ))
    
    fig.update_layout(
        title="📈 Прогноз потребления",
        xaxis_title="Период",
        yaxis_title=target,
        hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
        plot_bgcolor='white',
        height=500
    )
    
    metrics_text = f"R²: {metrics['R²']:.3f} | RMSE: {metrics['RMSE']:.2f}"
    if metrics['MAPE'] is not None:
        metrics_text += f" | MAPE: {metrics['MAPE']:.1f}%"
    
    fig.add_annotation(
        text=metrics_text,
        xref="paper", yref="paper",
        x=0.02, y=0.98,
        showarrow=False,
        bgcolor="rgba(255,255,255,0.8)",
        bordercolor="#2563eb",
        borderwidth=1,
        font=dict(size=10)
    )
    
    return fig

# =============================================================================
# Экспорт в Excel
# =============================================================================

def export_forecast_to_excel(historical_df, forecast_index, forecast_pred, forecast_lower, forecast_upper, target, metrics, sphere):
    """Экспортирует прогноз в Excel"""
    
    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        historical_df.to_excel(writer, sheet_name='История', index=True)
        
        forecast_result = pd.DataFrame({
            'Период': forecast_index,
            f'Прогноз {target}': forecast_pred,
            'Нижняя граница (95%)': forecast_lower,
            'Верхняя граница (95%)': forecast_upper
        })
        forecast_result.to_excel(writer, sheet_name='Прогноз', index=False)
        
        metrics_df = pd.DataFrame([
            {'Метрика': 'R²', 'Значение': f"{metrics['R²']:.4f}"},
            {'Метрика': 'RMSE', 'Значение': f"{metrics['RMSE']:.2f}"},
            {'Метрика': 'MAPE', 'Значение': f"{metrics['MAPE']:.1f}%" if metrics['MAPE'] else 'N/A'},
            {'Метрика': 'Сфера', 'Значение': sphere},
            {'Метрика': 'Целевой показатель', 'Значение': target},
            {'Метрика': 'Дата расчёта', 'Значение': datetime.now().strftime('%Y-%m-%d %H:%M')}
        ])
        metrics_df.to_excel(writer, sheet_name='Метрики', index=False)
    
    output.seek(0)
    return output

# =============================================================================
# Интерфейс Streamlit
# =============================================================================

def show_consumption_forecast():
    """Страница Прогноз потребления"""
    
    st.header("📊 Прогноз потребления")
    st.info("📌 Линейная регрессия для прогнозирования ключевых показателей по сферам ЖКХ")
    
    if "sphere" not in st.session_state:
        st.session_state.sphere = None
    if "historical_data" not in st.session_state:
        st.session_state.historical_data = None
    if "forecast_result" not in st.session_state:
        st.session_state.forecast_result = None
    
    sphere_config = get_sphere_config()
    
    st.subheader("1. Параметры прогноза")
    
    col1, col2 = st.columns(2)
    
    with col1:
        sphere = st.selectbox(
            "🌍 Сфера деятельности",
            list(sphere_config.keys()),
            key="sphere_select"
        )
    
    with col2:
        history_years = st.selectbox(
            "📅 Период исторических данных",
            [1, 2, 3, 4, 5],
            format_func=lambda x: f"{x} год(а)",
            key="history_years_select"
        )
    
    forecast_years = st.selectbox(
        "🔮 Горизонт прогноза",
        [1, 2, 3, 4, 5],
        format_func=lambda x: f"{x} год(а)",
        key="forecast_years_select"
    )
    
    config = sphere_config[sphere]
    target = config['target']
    features = config['features']
    
    st.session_state.sphere = sphere
    
    st.divider()
    st.subheader("2. Исторические данные")
    
    st.write(f"**Целевой показатель:** {target}")
    st.write(f"**Параметры для анализа:** {', '.join(features)}")
    if config.get('weather'):
        st.write(f"**Погодный фактор:** {config['weather_label']}")
    
    col1, col2, col3 = st.columns([1, 1, 2])
    
    with col1:
        if st.button("📋 Создать шаблон", key="create_template_btn", use_container_width=True):
            periods = pd.date_range(
                end=datetime.now().replace(day=1),
                periods=history_years * 12,
                freq='M'
            )
            
            columns = [target] + features
            if config.get('weather'):
                columns.append(config['weather_label'])
            
            df_template = pd.DataFrame(index=periods, columns=columns)
            st.session_state.historical_data = df_template
            st.session_state.forecast_result = None
            st.success("✅ Шаблон создан")
            st.rerun()
    
    with col2:
        if st.button("🎲 Тестовые данные", key="fill_test_data_btn", use_container_width=True):
            with st.spinner("🔄 Генерация данных..."):
                test_df = generate_test_data(sphere, history_years, config)
                st.session_state.historical_data = test_df
                st.session_state.forecast_result = None
                st.success(f"✅ Заполнено {len(test_df)} строк тестовыми данными")
                st.rerun()
    
    if st.session_state.historical_data is not None:
        st.write("📝 Заполните данные (пустые ячейки будут пропущены):")
        
        edited_df = st.data_editor(
            st.session_state.historical_data,
            use_container_width=True,
            num_rows="dynamic",
            key="data_editor"
        )
        
        st.session_state.historical_data = edited_df
        
        if st.button("🔮 Рассчитать прогноз", use_container_width=True, type="primary", key="calculate_btn"):
            with st.spinner("🔄 Выполняется регрессионный анализ..."):
                
                X_cols = features.copy()
                if config.get('weather'):
                    X_cols.append(config['weather_label'])
                
                df_numeric = edited_df.copy()
                for col in [target] + X_cols:
                    df_numeric[col] = pd.to_numeric(df_numeric[col], errors='coerce')
                
                df_clean = df_numeric[[target] + X_cols].dropna()
                
                if len(df_clean) < 3:
                    st.error(f"❌ Недостаточно данных для расчёта. Заполните минимум 3 строки без пропусков.")
                else:
                    X = df_clean[X_cols].values.astype(float)
                    y = df_clean[target].values.astype(float)
                    
                    model = LinearRegression()
                    model.fit(X, y)
                    model._X_train = X
                    model._y_train = y
                    
                    y_pred_train = model.predict(X)
                    metrics = {
                        "R²": r2_score(y, y_pred_train),
                        "RMSE": np.sqrt(mean_squared_error(y, y_pred_train)),
                        "MAPE": mean_absolute_percentage_error(y, y_pred_train) * 100 if np.all(y != 0) else None
                    }
                    
                    future_periods = pd.date_range(
                        start=edited_df.index[-1] + pd.offsets.MonthBegin(1),
                        periods=forecast_years * 12,
                        freq='M'
                    )
                    
                    X_mean = np.nanmean(X, axis=0).astype(float)
                    X_future = np.tile(X_mean, (len(future_periods), 1))
                    
                    y_pred, lower, upper = forecast_with_confidence(model, X_future, alpha=0.05)
                    
                    st.session_state.forecast_result = {
                        "historical": df_numeric,
                        "forecast_index": future_periods,
                        "forecast_pred": y_pred,
                        "forecast_lower": lower,
                        "forecast_upper": upper,
                        "target": target,
                        "metrics": metrics,
                        "model": model
                    }
                    
                    st.success("✅ Прогноз рассчитан")
                    st.rerun()
    
    if st.session_state.forecast_result:
        res = st.session_state.forecast_result
        
        st.divider()
        st.subheader("3. Результаты прогноза")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("R²", f"{res['metrics']['R²']:.3f}", help="Доля объяснённой дисперсии")
        with col2:
            st.metric("RMSE", f"{res['metrics']['RMSE']:.2f}", help="Среднеквадратичная ошибка")
        with col3:
            mape_val = f"{res['metrics']['MAPE']:.1f}%" if res['metrics']['MAPE'] else "N/A"
            st.metric("MAPE", mape_val, help="Средняя абсолютная процентная ошибка")
        
        st.plotly_chart(
            plot_forecast_chart(
                res['historical'],
                res['target'],
                res['forecast_index'],
                res['forecast_pred'],
                res['forecast_lower'],
                res['forecast_upper'],
                res['metrics']
            ),
            use_container_width=True
        )
        
        with st.expander("📋 Таблица прогноза", expanded=False):
            forecast_table = pd.DataFrame({
                'Период': res['forecast_index'],
                'Прогноз': res['forecast_pred'],
                'Нижняя граница (95%)': res['forecast_lower'],
                'Верхняя граница (95%)': res['forecast_upper']
            })
            st.dataframe(forecast_table, use_container_width=True)
        
        st.divider()
        st.subheader("4. Экспорт")
        
        if st.button("📊 Экспортировать в Excel", use_container_width=True, key="export_forecast_btn"):
            excel_buffer = export_forecast_to_excel(
                res['historical'],
                res['forecast_index'],
                res['forecast_pred'],
                res['forecast_lower'],
                res['forecast_upper'],
                res['target'],
                res['metrics'],
                sphere
            )
            
            st.download_button(
                label="⬇️ Скачать файл",
                data=excel_buffer,
                file_name=f"forecast_{sphere}_{datetime.now().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        
        if st.button("🔄 Начать новый прогноз", key="reset_forecast_btn"):
            st.session_state.historical_data = None
            st.session_state.forecast_result = None
            st.rerun()
    
    with st.expander("💡 Как использовать", expanded=False):
        st.write("""
**Назначение:**
Прогнозирование ключевых показателей потребления на основе исторических данных и линейной регрессии.

**Как работает:**
1. Выберите сферу — параметры ввода адаптируются
2. Укажите период истории — от 1 до 5 лет
3. **Быстрый старт:** нажмите «🎲 Тестовые данные» для автозаполнения
4. Или создайте шаблон и заполните вручную
5. Рассчитайте — линейная регрессия построит прогноз
6. Получите результат — график + метрики + Excel

**Методология:**
- Модель: линейная регрессия (sklearn)
- Доверительный интервал: 95%
- Метрики: R², RMSE, MAPE

**Важно:**
- Прогноз при условии неизменности входных параметров
- Все данные хранятся только в сессии
- Экспорт: 3 листа (История, Прогноз, Метрики)
        """)

if __name__ == "__main__":
    show_consumption_forecast()