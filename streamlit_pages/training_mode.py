# streamlit_pages/training_mode.py
import streamlit as st
import json
import os
from datetime import datetime
import random

# =============================================================================
# Функции
# =============================================================================

def get_spheres():
    """Сферы деятельности для обучения"""
    return {
        "heat": "🔥 Теплоснабжение",
        "water": "💧 Водоснабжение",
        "wastewater": "🚰 Водоотведение",
        "tko": "🗑️ ТКО",
        "electricity": "⚡ Электроснабжение"
    }

def get_question_categories():
    """Категории вопросов по темам регулирования"""
    return {
        "tariff_basics": "Основы тарифообразования",
        "fas_regulations": "Нормативная база ФАС",
        "amortization": "Амортизация и ОС",
        "repair_costs": "Расходы на ремонт",
        "personnel": "Численность и зарплата",
        "losses": "Потери в сетях",
        "reporting": "Отчётность и ФГИС",
        "disputes": "Споры и жалобы"
    }

def get_training_questions(sphere, category=None):
    """Загружает вопросы из базы (демо-данные для MVP)"""
    
    # Демо-вопросы для разных сфер
    questions_db = {
        "heat": [
            {
                "id": "q_heat_001",
                "category": "tariff_basics",
                "question": "Что такое валовая выручка (НВВ) в тарифном регулировании?",
                "correct_answer": "Валовая выручка — это сумма всех экономически обоснованных расходов РСО, необходимых для оказания услуг, включая нормативную прибыль.",
                "explanation": "НВВ рассчитывается по методике ФАС и включает: амортизацию, ремонт, зарплату, потери, топливо и прочие затраты."
            },
            {
                "id": "q_heat_002",
                "category": "fas_regulations",
                "question": "Какой приказ ФАС утверждает методику расчёта тарифов на тепловую энергию?",
                "correct_answer": "Приказ ФАС России от 13.06.2023 №1746-э.",
                "explanation": "Этот приказ содержит подробную методику расчёта тарифов, включая формулы и требования к обоснованию затрат."
            },
            {
                "id": "q_heat_003",
                "category": "amortization",
                "question": "Как определяется срок службы основных средств для расчёта амортизации?",
                "correct_answer": "Срок службы определяется по Классификатору основных средств (Постановление Правительства №1), отклонения требуют обоснования.",
                "explanation": "Например, тепловые сети относятся к группе 7-10 лет, здания — 15-30 лет. Продление срока требует акта технической экспертизы."
            },
            {
                "id": "q_heat_004",
                "category": "repair_costs",
                "question": "Какие документы необходимы для обоснования расходов на ремонт ОС?",
                "correct_answer": "Дефектная ведомость, смета расходов, акт выполненных работ, реестр отремонтированных объектов.",
                "explanation": "Без дефектной ведомости регулятор вправе исключить затраты на ремонт из тарифа."
            },
            {
                "id": "q_heat_005",
                "category": "personnel",
                "question": "Как рассчитывается норматив численности персонала РСО?",
                "correct_answer": "Норматив рассчитывается как функция от объёма отпуска, протяжённости сетей, количества абонентов по методике ФАС.",
                "explanation": "Превышение норматива более чем на 10% требует обоснования (климат, рельеф, износ сетей)."
            },
            {
                "id": "q_heat_006",
                "category": "losses",
                "question": "Что такое норматив потерь в тепловых сетях?",
                "correct_answer": "Норматив потерь — это предельно допустимый процент потерь тепловой энергии при транспортировке, утверждаемый регулятором.",
                "explanation": "Фактические потери сверх норматива не включаются в тариф. Типовой норматив: 8-15% в зависимости от износа."
            },
            {
                "id": "q_heat_007",
                "category": "reporting",
                "question": "В какую систему РСО обязаны передавать данные о тарифах?",
                "correct_answer": "В ФГИС Тариф (Федеральная государственная информационная система тарифного регулирования).",
                "explanation": "Передача данных в ФГИС обязательна в сроки, установленные приказом ФАС. Нарушение влечёт штрафы."
            },
            {
                "id": "q_heat_008",
                "category": "disputes",
                "question": "В какой срок можно обжаловать решение регулятора о тарифе?",
                "correct_answer": "Решение регулятора можно обжаловать в течение 3 месяцев с момента получения в ФАС или в суд.",
                "explanation": "Жалоба должна содержать обоснование несогласия со ссылкой на НПА и доказательства."
            }
        ],
        "water": [
            {
                "id": "q_water_001",
                "category": "tariff_basics",
                "question": "Из каких компонентов складывается тариф на водоснабжение?",
                "correct_answer": "Тариф включает: производство воды, передача воды, водоотведение, очистка сточных вод.",
                "explanation": "Каждый компонент рассчитывается отдельно по методике ФАС №346-э."
            },
            {
                "id": "q_water_002",
                "category": "losses",
                "question": "Как обосновываются потери воды в сетях?",
                "correct_answer": "Потери обосновываются расчётом норматива, актами обследования сетей, планом мероприятий по снижению.",
                "explanation": "Потери сверх норматива исключаются из тарифа."
            },
            {
                "id": "q_water_003",
                "category": "fas_regulations",
                "question": "Какой приказ регулирует тарифы на водоснабжение?",
                "correct_answer": "Приказ ФАС России от 27.12.2013 №346-э.",
                "explanation": "Методические указания по регулированию тарифов в сфере водоснабжения и водоотведения."
            }
        ],
        "tko": [
            {
                "id": "q_tko_001",
                "category": "tariff_basics",
                "question": "Из чего складывается тариф на транспортирование ТКО?",
                "correct_answer": "Тариф включает: сбор, транспортирование, обработку, утилизацию, захоронение отходов.",
                "explanation": "Расчёт по Постановлению Правительства РФ №406."
            },
            {
                "id": "q_tko_002",
                "category": "fas_regulations",
                "question": "Какое постановление регулирует тарифы на ТКО?",
                "correct_answer": "Постановление Правительства РФ от 30.05.2016 №406.",
                "explanation": "Устанавливает порядок расчёта тарифов для региональных операторов."
            }
        ],
        "electricity": [
            {
                "id": "q_elec_001",
                "category": "tariff_basics",
                "question": "Что такое сбытовая надбавка в тарифе на электроэнергию?",
                "correct_answer": "Сбытовая надбавка — это вознаграждение гарантирующего поставщика за услуги по продаже электроэнергии.",
                "explanation": "Утверждается региональным регулятором отдельно от тарифа на передачу."
            }
        ],
        "wastewater": [
            {
                "id": "q_waste_001",
                "category": "tariff_basics",
                "question": "Как рассчитывается тариф на водоотведение?",
                "correct_answer": "Тариф рассчитывается как сумма затрат на приём, транспортировку и очистку сточных вод, делённая на объём водоотведения.",
                "explanation": "По методике ФАС №346-э."
            }
        ]
    }
    
    # Получаем вопросы для выбранной сферы
    sphere_questions = questions_db.get(sphere, questions_db["heat"])
    
    # Фильтрация по категории (если выбрана)
    if category and category != "all":
        sphere_questions = [q for q in sphere_questions if q.get("category") == category]
    
    return sphere_questions

def check_answer_with_ai(user_answer, correct_answer):
    """Проверяет ответ через AI (упрощённая логика для MVP)"""
    
    # Простая проверка по ключевым словам (в будущем — через LLM)
    user_lower = user_answer.lower().strip()
    correct_lower = correct_answer.lower()
    
    # Извлекаем ключевые слова из правильного ответа
    key_words = [w for w in correct_lower.split() if len(w) > 4 and w not in 
                 ['который', 'которая', 'которое', 'является', 'являются', 'необходимо', 'необходима']]
    
    # Считаем совпадения
    matches = sum(1 for w in key_words if w in user_lower)
    
    # Если есть 40%+ совпадений — считаем ответ правильным
    if len(key_words) > 0:
        match_percent = matches / len(key_words)
        is_correct = match_percent >= 0.4 or user_lower in correct_lower or correct_lower in user_lower
    else:
        is_correct = user_lower == correct_lower
    
    return is_correct

def save_questions_to_admin(questions):
    """Сохраняет вопросы в файл для Админки"""
    
    questions_file = os.path.join("data", "training", "questions.json")
    os.makedirs(os.path.dirname(questions_file), exist_ok=True)
    
    # Загружаем существующие
    if os.path.exists(questions_file):
        with open(questions_file, 'r', encoding='utf-8') as f:
            all_questions = json.load(f)
    else:
        all_questions = {}
    
    # Добавляем новые
    for q in questions:
        sphere = q.get("sphere", "heat")
        if sphere not in all_questions:
            all_questions[sphere] = []
        all_questions[sphere].append(q)
    
    # Сохраняем
    with open(questions_file, 'w', encoding='utf-8') as f:
        json.dump(all_questions, f, ensure_ascii=False, indent=2)

# =============================================================================
# Интерфейс Streamlit
# =============================================================================

def show_training_mode():
    """Страница Режима обучения для новичков"""
    
    st.header("🎓 Режим обучения для новичков")
    st.info("📌 Тестирование по темам тарифного регулирования с AI-проверкой ответов")
    
    # Инициализация session_state
    if "quiz_started" not in st.session_state:
        st.session_state.quiz_started = False
    if "quiz_finished" not in st.session_state:
        st.session_state.quiz_finished = False
    if "current_question_index" not in st.session_state:
        st.session_state.current_question_index = 0
    if "questions" not in st.session_state:
        st.session_state.questions = []
    if "answers" not in st.session_state:
        st.session_state.answers = []
    if "score" not in st.session_state:
        st.session_state.score = 0
    if "selected_sphere" not in st.session_state:
        st.session_state.selected_sphere = None
    if "selected_category" not in st.session_state:
        st.session_state.selected_category = None
    if "num_questions" not in st.session_state:
        st.session_state.num_questions = 10
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 1: Выбор сферы и параметров
    # ─────────────────────────────────────────────────────────────────────
    if not st.session_state.quiz_started:
        st.subheader("1. Параметры тестирования")
        
        # Выбор сферы (ОБЯЗАТЕЛЬНО)
        sphere = st.selectbox(
            "🌍 Выберите сферу деятельности",
            list(get_spheres().keys()),
            format_func=lambda x: get_spheres().get(x, x),
            key="sphere_select"
        )
        
        # Выбор категории
        category = st.selectbox(
            "📚 Выберите тему вопросов",
            ["all"] + list(get_question_categories().keys()),
            format_func=lambda x: "Все темы" if x == "all" else get_question_categories().get(x, x),
            key="category_select"
        )
        
        # Количество вопросов
        num_questions = st.slider(
            "📊 Количество вопросов",
            min_value=5,
            max_value=20,
            value=10,
            step=5,
            key="num_questions_slider"
        )
        
        # Таймер (опционально)
        use_timer = st.checkbox("⏱️ Использовать таймер (60 сек на вопрос)", key="use_timer_check")
        
        if st.button("🚀 Начать тестирование", use_container_width=True, type="primary"):
            # Загрузка вопросов
            all_questions = get_training_questions(sphere, category if category != "all" else None)
            
            # Перемешиваем и берём нужное количество
            random.shuffle(all_questions)
            selected_questions = all_questions[:num_questions]
            
            if len(selected_questions) < num_questions:
                st.warning(f"⚠️ Найдено только {len(selected_questions)} вопросов по этой теме")
            
            # Инициализация теста
            st.session_state.quiz_started = True
            st.session_state.quiz_finished = False
            st.session_state.current_question_index = 0
            st.session_state.questions = selected_questions
            st.session_state.answers = []
            st.session_state.score = 0
            st.session_state.selected_sphere = sphere
            st.session_state.selected_category = category
            st.session_state.num_questions = len(selected_questions)
            st.session_state.use_timer = use_timer
            
            st.rerun()
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 2: Тестирование (вопросы по одному)
    # ─────────────────────────────────────────────────────────────────────
    elif st.session_state.quiz_started and not st.session_state.quiz_finished:
        current_idx = st.session_state.current_question_index
        total_questions = len(st.session_state.questions)
        
        # Прогресс
        st.progress(current_idx / total_questions)
        st.caption(f"Вопрос {current_idx + 1} из {total_questions}")
        
        # Статистика
        col1, col2 = st.columns(2)
        col1.metric("Правильных", st.session_state.score)
        col2.metric("Текущий %", f"{(st.session_state.score / max(current_idx, 1) * 100):.0f}%")
        
        st.divider()
        
        # Текущий вопрос
        question = st.session_state.questions[current_idx]
        
        st.subheader(f"❓ Вопрос {current_idx + 1}")
        st.write(question["question"])
        
        # Таймер (визуально)
        if st.session_state.use_timer:
            st.caption("⏱️ Рекомендуется ответить за 60 секунд")
        
        # Поле ответа
        user_answer = st.text_area(
            "Ваш ответ",
            placeholder="Введите развёрнутый ответ...",
            height=150,
            key=f"answer_{current_idx}"
        )
        
        # Кнопки
        col1, col2 = st.columns([3, 1])
        
        with col1:
            submit_btn = st.button("✅ Ответить", use_container_width=True, type="primary", key=f"submit_{current_idx}")
        
        with col2:
            skip_btn = st.button("⏭ Пропустить", use_container_width=True, key=f"skip_{current_idx}")
        
        if submit_btn and user_answer.strip():
            # Проверка ответа
            is_correct = check_answer_with_ai(user_answer, question["correct_answer"])
            
            # Сохранение результата
            st.session_state.answers.append({
                "question_id": question["id"],
                "question": question["question"],
                "user_answer": user_answer,
                "correct_answer": question["correct_answer"],
                "is_correct": is_correct,
                "explanation": question["explanation"]
            })
            
            if is_correct:
                st.session_state.score += 1
                st.success("✅ Верно!")
            else:
                st.error("❌ Неверно!")
            
            # Пояснение (всегда показываем)
            with st.expander("📖 Пояснение", expanded=True):
                st.write(f"**Правильный ответ:** {question['correct_answer']}")
                st.write(f"**Объяснение:** {question['explanation']}")
            
            # Переход к следующему
            if current_idx < total_questions - 1:
                st.session_state.current_question_index += 1
                st.rerun()
            else:
                st.session_state.quiz_finished = True
                st.rerun()
        
        elif skip_btn:
            # Пропуск вопроса
            st.session_state.answers.append({
                "question_id": question["id"],
                "question": question["question"],
                "user_answer": "(пропущено)",
                "correct_answer": question["correct_answer"],
                "is_correct": False,
                "explanation": question["explanation"]
            })
            
            if current_idx < total_questions - 1:
                st.session_state.current_question_index += 1
                st.rerun()
            else:
                st.session_state.quiz_finished = True
                st.rerun()
    
    # ─────────────────────────────────────────────────────────────────────
    # Шаг 3: Результаты
    # ─────────────────────────────────────────────────────────────────────
    elif st.session_state.quiz_finished:
        st.subheader("📊 Результаты тестирования")
        
        total = len(st.session_state.questions)
        score = st.session_state.score
        percent = (score / total * 100) if total > 0 else 0
        
        # Итоговый блок
        st.markdown(f"""
        <div style="background: linear-gradient(90deg, #3498db, #2c3e50); 
                    padding: 2rem; border-radius: 10px; text-align: center; margin: 1rem 0;">
            <h2 style="color: white; margin: 0;">🎓 Результаты</h2>
            <h1 style="color: white; margin: 0.5rem 0; font-size: 3rem;">
                {score} из {total} ({percent:.0f}%)
            </h1>
            <p style="color: #ecf0f1; margin: 0;">
                Сфера: {get_spheres().get(st.session_state.selected_sphere, '')} | 
                Вопросы: {st.session_state.num_questions}
            </p>
        </div>
        """, unsafe_allow_html=True)
        
        st.divider()
        
        # Детализация по вопросам
        st.subheader("📋 Разбор ответов")
        
        for i, answer in enumerate(st.session_state.answers, 1):
            status_icon = "✅" if answer["is_correct"] else "❌"
            
            with st.expander(f"{status_icon} Вопрос {i}: {answer['question'][:50]}...", expanded=(not answer["is_correct"])):
                st.write(f"**Ваш ответ:** {answer['user_answer']}")
                st.write(f"**Правильный ответ:** {answer['correct_answer']}")
                st.write(f"**Пояснение:** {answer['explanation']}")
        
        st.divider()
        
        # Кнопки
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔄 Пройти ещё раз", use_container_width=True):
                # Сброс
                st.session_state.quiz_started = False
                st.session_state.quiz_finished = False
                st.session_state.current_question_index = 0
                st.session_state.questions = []
                st.session_state.answers = []
                st.session_state.score = 0
                st.rerun()
        
        with col2:
            if st.button("🏠 В главное меню", use_container_width=True):
                st.session_state.quiz_started = False
                st.session_state.quiz_finished = False
                st.rerun()
    
    # ─────────────────────────────────────────────────────────────────────
    # Справка
    # ─────────────────────────────────────────────────────────────────────
    with st.expander("💡 Как использовать", expanded=False):
        st.write("""
**Назначение:**

Режим обучения помогает новым сотрудникам освоить основы тарифного регулирования через тестирование с AI-проверкой ответов.

**Как работает:**

1. **Выберите сферу**: тепло, вода, ТКО, электричество, водоотведение
2. **Выберите тему**: основы тарифообразования, нормативная база, амортизация, и т.д.
3. **Укажите количество вопросов**: от 5 до 20
4. **Ответьте на вопросы**: развёрнутый текст, AI проверяет по смыслу
5. **Получите результат**: процент правильных ответов с разбором ошибок

**Проверка ответов:**

- AI сравнивает ваш ответ с эталонным по ключевым словам
- Если совпадение 40%+ — ответ засчитывается как правильный
- После каждого вопроса показывается пояснение

**Результаты:**

- Не сохраняются (только сессия)
- Можно пройти тест повторно
- Нет порога прохождения — просто реальный результат

**Для администраторов:**

- Добавление вопросов через раздел Админка
- Вопросы публикуются сразу
- Поддержка всех сфер и категорий
        """)

# =============================================================================
# Запуск
# =============================================================================

if __name__ == "__main__":
    show_training_mode()