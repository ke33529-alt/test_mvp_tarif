# streamlit_pages/advisor_page.py
"""
UI Советчика по нормативной базе
──────────────────────────────────────────────────────────────────────────────
Вкладки:
  1. Запрос        — поиск + стриминг ответа + уточнения
  2. История сессии — запросы текущей сессии
  3. Все запросы   — персистентная история с поиском и фильтрами
"""
from __future__ import annotations
import os
import json
import pandas as pd
from datetime import datetime

import streamlit as st
from core.feedback import submit_feedback


def show_advisor():
    st.header("Советчик по нормативной базе")
    st.info("Задайте вопрос по тарифному регулированию — система найдёт ответ в актуальной базе НПА")

    # Инициализация session_state
    # Загружаем сохранённые настройки советчика
    _adv_prefs_file = os.path.join("config", "advisor_prefs.json")
    _adv_defaults   = {"top_k": 20, "neighbor_radius": 0, "temperature": 0.3}
    if "_adv_prefs_loaded" not in st.session_state:
        try:
            if os.path.exists(_adv_prefs_file):
                with open(_adv_prefs_file, "r", encoding="utf-8") as _f:
                    _adv_prefs = {**_adv_defaults, **json.load(_f)}
            else:
                _adv_prefs = _adv_defaults
        except Exception:
            _adv_prefs = _adv_defaults
        st.session_state["_adv_top_k"]          = _adv_prefs["top_k"]
        st.session_state["_adv_neighbor_radius"] = _adv_prefs["neighbor_radius"]
        st.session_state["_adv_temperature"]     = _adv_prefs["temperature"]
        st.session_state["_adv_prefs_loaded"]    = True

    for key, val in [
        ("last_query", ""), ("last_result", None), ("search_triggered", False),
        ("sources_only_mode", False), ("query_times", []),
        ("advisor_model", "qwen/qwen3.5-9b"),
    ]:
        if key not in st.session_state:
            st.session_state[key] = val

    # Проверка векторной базы
    vector_db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vector_db")
    db_file = os.path.join(vector_db_path, "chroma.sqlite3")
    if not os.path.exists(db_file):
        st.warning("⚠️ Векторная база не найдена. Запустите индексацию в Админке.")
        st.info(f"📂 Ожидаемый путь: {db_file}")
        st.stop()

    with st.expander("Варианты использования", expanded=False):
        st.write("• Можно ли включать затраты на ДМС в тариф?")
        st.write("• Какие документы нужны для тарифной заявки по теплоснабжению?")
        st.write("• Как ФАС трактует расходы на программное обеспечение?")
        st.write("• Что такое валовая выручка и как она рассчитывается?")

    # Дефолты на случай если экспандер свёрнут и слайдеры не рендерились
    top_k           = st.session_state.get("_adv_top_k", 20)
    temperature     = float(st.session_state.get("_adv_temperature", 0.3))
    neighbor_radius = st.session_state.get("_adv_neighbor_radius", 0)

    # ── Настройки ────────────────────────────────────────────────────────────
    with st.expander("Настройки", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            top_k = st.slider(
                "Количество источников (топ-K)", 1, 50,
                st.session_state.get("_adv_top_k", 20),
                key="top_k_slider",
                help="Сколько чанков передаётся LLM после реранкинга",
            )
            temperature = st.slider(
                "Креативность ответа", 0.0, 1.0,
                float(st.session_state.get("_adv_temperature", 0.3)),
                0.1, key="temp_slider",
            )
            neighbor_radius = st.slider(
                "Соседних чанков с каждой стороны", 0, 5,
                st.session_state.get("_adv_neighbor_radius", 0),
                key="neighbor_radius_slider",
                help="Для каждого найденного чанка подтягивается N соседей. "
                     "0 — только сам чанк (рекомендуется при режиме ⚖️ По пунктам НПА). "
                     "Больше — шире контекст, но LLM может потеряться.",
            )
            st.session_state.neighbor_radius        = neighbor_radius
            st.session_state["_adv_top_k"]          = top_k
            st.session_state["_adv_neighbor_radius"] = neighbor_radius
            st.session_state["_adv_temperature"]     = temperature
            if neighbor_radius > 0:
                st.caption(f"Каждый результат даёт {1 + neighbor_radius * 2} чанков контекста")
            # Сохраняем в конфиг при каждом изменении
            try:
                os.makedirs("config", exist_ok=True)
                with open(os.path.join("config", "advisor_prefs.json"), "w", encoding="utf-8") as _f:
                    json.dump({"top_k": top_k, "neighbor_radius": neighbor_radius,
                               "temperature": float(temperature)}, _f, ensure_ascii=False)
            except Exception:
                pass
        with col2:
            try:
                from core.advisor import get_available_models
                model_names = [m["name"] for m in get_available_models()] or ["qwen/qwen3.5-9b"]
            except Exception:
                model_names = ["qwen/qwen3.5-9b"]
            selected_model = st.selectbox(
                "🤖 Модель", options=model_names,
                index=model_names.index(st.session_state.advisor_model)
                      if st.session_state.advisor_model in model_names else 0,
                key="advisor_model_select",
            )
            st.session_state.advisor_model = selected_model
            st.caption(f"Доступно моделей: {len(model_names)}")

            sources_only_mode = st.toggle(
                "🧪 Режим тестов чанков (без LLM)",
                value=st.session_state.sources_only_mode,
                key="sources_only_toggle",
            )
            st.session_state.sources_only_mode = sources_only_mode

            if st.button("🗑 Очистить кэш LLM", key="clear_cache_btn", use_container_width=True):
                from core.advisor import _llm_cache, save_llm_cache
                _llm_cache.clear()
                save_llm_cache()
                st.session_state.query_times = []
                st.success("✅ Кэш очищен")
                st.rerun()

        if st.session_state.query_times:
            st.divider()
            avg_time = sum(st.session_state.query_times) / len(st.session_state.query_times)
            c1, c2, c3 = st.columns(3)
            c1.metric("Запросов",     len(st.session_state.query_times))
            c2.metric("Среднее время", f"{avg_time:.1f} сек")
            c3.metric("Последний",    f"{st.session_state.query_times[-1]:.1f} сек")

    # ── Инициализация истории и состояния уточнений ──────────────────────────
    if "advisor_history" not in st.session_state:
        st.session_state.advisor_history = []
    if "clarifications" not in st.session_state:
        st.session_state.clarifications = []
    # ID последней записи в персистентной истории (для обновления уточнений)
    if "_adv_hist_id" not in st.session_state:
        st.session_state._adv_hist_id = None

    # ── Вкладки: Запрос / История сессии / Все запросы ───────────────────────
    tab_query, tab_history, tab_all_history = st.tabs(
        ["Запрос", "История сессии", "Все запросы"]
    )

    with tab_query:
        # ── Фильтр по сфере деятельности ─────────────────────────────────────
        _ADV_SPHERES = [
            "🔥 Теплоснабжение",
            "💧 Водоснабжение/водоотведение",
            "🗑️ Обращение с ТКО",
            "🔵 Газ",
            "⚡ Электрика",
            "📁 Иные сферы",
        ]
        adv_spheres = st.multiselect(
            "Сфера деятельности",
            options=_ADV_SPHERES,
            default=[],
            key="advisor_spheres_filter",
            placeholder="Все сферы — фильтр не применяется",
            help=(
                "Уточните сферу(ы) для целевого поиска. "
                "Документы без назначенной сферы всегда включаются в результаты."
            ),
        )
        if adv_spheres:
            _adv_sep = "  \xb7  "
            st.caption(f"Активен фильтр: **{_adv_sep.join(adv_spheres)}**")

        # ── Поле ввода ───────────────────────────────────────────────────────
        query = st.text_area(
            "Ваш вопрос",
            height=100,
            placeholder="Например: Какие расходы на ремонт можно включать в тариф?",
            key="question_input",
            value=st.session_state.last_query,
        )

        if st.session_state.sources_only_mode:
            st.warning("Режим тестов чанков активен: LLM отключён, показываются только источники")

        # ── Кнопка поиска — стриминг ─────────────────────────────────────────
        if st.button("Найти ответ", type="primary", key="search_btn"):
            if query.strip():
                try:
                    from core.advisor import (
                        search_faq, search_vector_db, stream_ai_answer,
                        strip_thinking_blocks, detect_section, set_sources_only_mode,
                    )
                    set_sources_only_mode(st.session_state.sources_only_mode)
                    start_time = datetime.now()

                    faq_results = search_faq(query)
                    if faq_results:
                        answer  = faq_results[0]["answer"]
                        sources = [{"snippet": faq_results[0]["question"],
                                    "file": "FAQ", "page": "", "category": "FAQ"}]
                        st.success("Ответ из базы частых вопросов")
                        st.markdown(f"### Ответ:\n{answer}")
                        from_faq = True
                    else:
                        with st.spinner("Ищем в базе знаний..."):
                            _effective_top_k = st.session_state.get("_adv_top_k", top_k)
                            sources = search_vector_db(
                                query,
                                top_k=_effective_top_k,
                                spheres=adv_spheres if adv_spheres else None,
                            )

                        if sources and not st.session_state.sources_only_mode:
                            st.success(f"Ответ сгенерирован ИИ · модель: {st.session_state.advisor_model}")
                            import itertools
                            gen = stream_ai_answer(
                                query, sources,
                                st.session_state.advisor_model,
                                temperature,
                            )
                            with st.spinner("Модель формирует ответ..."):
                                first_token = next(gen, None)
                            if first_token is not None:
                                raw_answer = st.write_stream(itertools.chain([first_token], gen))
                            else:
                                raw_answer = ""
                            answer = strip_thinking_blocks(raw_answer)

                        elif st.session_state.sources_only_mode:
                            answer = "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключён."
                            st.info(answer)
                        else:
                            answer = "❌ Не найдено релевантных документов в базе знаний."
                            st.warning(answer)
                        from_faq = False

                    query_time = (datetime.now() - start_time).total_seconds()
                    st.session_state.query_times.append(query_time)
                    if len(st.session_state.query_times) > 10:
                        st.session_state.query_times = st.session_state.query_times[-10:]

                    st.session_state.last_result = {
                        "answer":     answer,
                        "sources":    sources,
                        "from_faq":   from_faq,
                        "from_cache": False,
                        "model":      st.session_state.advisor_model,
                    }
                    st.session_state.last_query       = query
                    st.session_state.search_triggered = True
                    st.session_state._answer_streamed = True
                    # Сбрасываем цепочку уточнений для нового запроса
                    st.session_state.clarifications   = []

                    # Автосохранение в историю сессии
                    if answer and not answer.startswith("❌") and not st.session_state.sources_only_mode:
                        st.session_state.advisor_history.append({
                            "id":             id(datetime.now()),
                            "ts":             datetime.now().strftime("%H:%M:%S"),
                            "query":          query,
                            "answer":         answer,
                            "model":          st.session_state.advisor_model,
                            "spheres":        list(adv_spheres),
                            "sources":        sources,
                            "from_faq":       from_faq,
                            "clarifications": [],
                        })
                        # Сохраняем в персистентную историю
                        try:
                            from core.advisor_history import save_entry as _adv_save
                            _hist_id = _adv_save(
                                query=query,
                                answer=answer,
                                model=st.session_state.advisor_model,
                                spheres=list(adv_spheres),
                                sources=sources,
                                from_faq=from_faq,
                            )
                            st.session_state._adv_hist_id = _hist_id
                        except Exception as _he:
                            print(f"[HIST] Ошибка сохранения истории: {_he}")

                except Exception as e:
                    st.error(f"Ошибка: {type(e).__name__}: {str(e)}")
                    st.session_state.last_result = {"error": str(e)}
            else:
                st.warning("Введите вопрос")

        # ── Результат ────────────────────────────────────────────────────────
        result        = st.session_state.last_result
        just_streamed = st.session_state.pop("_answer_streamed", False) \
                        if "_answer_streamed" in st.session_state else False

        if result:
            if result.get("error"):
                st.error(f"Техническая ошибка: {result['error']}")
            else:
                answer  = result.get("answer", "")
                sources = result.get("sources", [])

                if not just_streamed:
                    if result.get("from_cache"):
                        st.info("Ответ из кэша")
                    elif result.get("from_faq"):
                        st.success("Ответ из базы частых вопросов")
                    elif answer and not answer.startswith("❌"):
                        if st.session_state.sources_only_mode:
                            st.info("Режим тестов: LLM отключён")
                        else:
                            st.success(f"Ответ сгенерирован ИИ (модель: {result.get('model', '')})")

                    if answer and not st.session_state.sources_only_mode:
                        import re as _re, io as _io
                        table_pattern = r'\|.*\|\n\|[-:\s|]+\|\n(?:\|.*\|\n)*'
                        tables = _re.findall(table_pattern, answer, _re.MULTILINE)
                        if tables:
                            for i, table_md in enumerate(tables):
                                try:
                                    df = pd.read_csv(_io.StringIO(table_md.replace('|', ',')),
                                                     header=0, index_col=0, skipinitialspace=True)
                                    df.columns = [str(c).strip() for c in df.columns]
                                    st.subheader(f"Таблица {i+1}")
                                    st.dataframe(df, use_container_width=True, hide_index=True)
                                    answer = answer.replace(table_md, "")
                                except Exception:
                                    st.code(table_md, language="markdown")
                        if answer.strip():
                            st.markdown(f"### Ответ:\n{answer.strip()}")
                    elif st.session_state.sources_only_mode:
                        st.info("В режиме тестов LLM отключён.")

                # ── Источники (один свёрнутый экспандер) ─────────────────────
                if sources:
                    with st.expander(f"Источники ({len(sources)})", expanded=False):
                        for i, src in enumerate(sources, 1):
                            st.markdown(f"**{i}. {src.get('file', '?')}**"
                                        + (f" (стр. {src['page']})" if src.get('page') else "")
                                        + (f" · {src['category']}" if src.get('category') else ""))
                            snippet = src.get('snippet', '')
                            st.caption(snippet[:600] + ("..." if len(snippet) > 600 else ""))
                            _src_sphere = src.get("sphere", "")
                            if _src_sphere:
                                _sp = [s.strip() for s in _src_sphere.split(",") if s.strip()]
                                st.caption("Сферы: " + "  \xb7  ".join(_sp))
                            if i < len(sources):
                                st.divider()

                # ── Перенаправление ───────────────────────────────────────────
                if result.get("redirect"):
                    st.divider()
                    st.info(f"💡 {result.get('redirect_reason', '')}")
                    st.markdown(f"""
                    <div class="redirect-box">
                        <b>👉 Перейдите в раздел «{result['redirect']}» в меню слева</b>
                    </div>""", unsafe_allow_html=True)

                # ── Оценка ───────────────────────────────────────────────────
                if not st.session_state.sources_only_mode and answer and not answer.startswith("❌"):
                    st.divider()
                    st.subheader("Оцените ответ")
                    col1, col2, col3 = st.columns(3)
                    query_for_fb = st.session_state.last_query
                    with col1:
                        if st.button("👍", key="btn_good", use_container_width=True):
                            submit_feedback("user", "answer_rating", "Полезно",
                                            question=query_for_fb[:500], answer=answer[:1000], rating=3)
                            st.success("Спасибо!")
                            st.session_state.last_result = None
                            st.session_state.search_triggered = False
                            st.session_state.clarifications = []
                            st.rerun()
                    with col2:
                        if st.button("😐", key="btn_neutral", use_container_width=True):
                            submit_feedback("user", "answer_rating", "Нормально",
                                            question=query_for_fb[:500], answer=answer[:1000], rating=2)
                            st.success("Спасибо!")
                            st.session_state.last_result = None
                            st.session_state.search_triggered = False
                            st.session_state.clarifications = []
                            st.rerun()
                    with col3:
                        if st.button("👎", key="btn_bad", use_container_width=True):
                            submit_feedback("user", "answer_rating", "Не помогло",
                                            question=query_for_fb[:500], answer=answer[:1000], rating=1)
                            st.success("Спасибо!")
                            st.session_state.last_result = None
                            st.session_state.search_triggered = False
                            st.session_state.clarifications = []
                            st.rerun()

                # ── Цепочка уточнений ─────────────────────────────────────────
                for ci, clar in enumerate(st.session_state.clarifications, 1):
                    st.divider()
                    st.markdown(f"#### Уточнение №{ci}")
                    st.caption(f"Вопрос: {clar['query']}")
                    st.markdown(clar["answer"])
                    if clar.get("sources"):
                        with st.expander(f"Источники ({len(clar['sources'])})", expanded=False):
                            for si, src in enumerate(clar["sources"], 1):
                                st.markdown(f"**{si}. {src.get('file', '?')}**"
                                            + (f" (стр. {src['page']})" if src.get('page') else ""))
                                st.caption(src.get('snippet', '')[:400] +
                                           ("..." if len(src.get('snippet', '')) > 400 else ""))
                                _sp2 = src.get("sphere", "")
                                if _sp2:
                                    st.caption("Сферы: " + "  \xb7  ".join(
                                        [s.strip() for s in _sp2.split(",") if s.strip()]))
                                if si < len(clar["sources"]):
                                    st.divider()

                # ── Форма уточнения ───────────────────────────────────────────
                if answer and not answer.startswith("❌") and not st.session_state.sources_only_mode:
                    st.divider()
                    clarify_q = st.text_area(
                        "Уточняющий вопрос",
                        height=80,
                        key="clarify_input",
                        placeholder="Задайте уточняющий вопрос по полученному ответу...",
                        label_visibility="collapsed",
                    )
                    if st.button("Уточнить", key="clarify_btn"):
                        if clarify_q.strip():
                            try:
                                from core.advisor import (
                                    search_vector_db as _svdb,
                                    stream_clarification_answer as _stream_clar,
                                    strip_thinking_blocks as _strip,
                                    set_sources_only_mode as _set_som,
                                )
                                _set_som(False)

                                # Предыдущий ответ для контекста:
                                # каждый раунд берёт ОДИН последний ответ (исходный или предыдущее уточнение)
                                _clars = st.session_state.clarifications
                                _prev_a = _clars[-1]["answer"] if _clars else result.get("answer", "")

                                # RAG-запрос — только текст уточнения (чистый эмбеддинг)
                                with st.spinner("Ищем в базе знаний..."):
                                    _new_sources = _svdb(
                                        clarify_q,
                                        top_k=st.session_state.get("_adv_top_k", 20),
                                        spheres=adv_spheres if adv_spheres else None,
                                    )

                                st.success(f"Уточнение · модель: {st.session_state.advisor_model}")
                                import itertools as _it
                                _gen = _stream_clar(
                                    clarify_q,
                                    _prev_a,
                                    _new_sources,
                                    st.session_state.advisor_model,
                                    st.session_state.get("_adv_temperature", 0.3),
                                )
                                with st.spinner("Модель формирует ответ..."):
                                    _first = next(_gen, None)
                                if _first is not None:
                                    _raw = st.write_stream(_it.chain([_first], _gen))
                                else:
                                    _raw = ""
                                _clar_answer = _strip(_raw) if _raw else "❌ Не найдено релевантных документов."

                                # Сохраняем уточнение в цепочку
                                st.session_state.clarifications.append({
                                    "query":   clarify_q,
                                    "answer":  _clar_answer,
                                    "sources": _new_sources,
                                })

                                # Обновляем последнюю запись в истории
                                if st.session_state.advisor_history:
                                    st.session_state.advisor_history[-1]["clarifications"] = \
                                        list(st.session_state.clarifications)
                                # Обновляем уточнения в персистентной истории
                                if st.session_state.get("_adv_hist_id"):
                                    try:
                                        from core.advisor_history import update_clarifications as _adv_upd
                                        _adv_upd(
                                            st.session_state._adv_hist_id,
                                            st.session_state.clarifications,
                                        )
                                    except Exception as _ue:
                                        print(f"[HIST] Ошибка обновления уточнений: {_ue}")

                                st.rerun()

                            except Exception as _e:
                                st.error(f"Ошибка уточнения: {type(_e).__name__}: {_e}")
                        else:
                            st.warning("Введите уточняющий вопрос")

                # ── Новый вопрос ──────────────────────────────────────────────
                st.divider()
                col1, col2 = st.columns([3, 1])
                with col2:
                    if st.button("Новый вопрос", key="btn_new", use_container_width=True):
                        st.session_state.last_query       = ""
                        st.session_state.last_result      = None
                        st.session_state.search_triggered = False
                        st.session_state.clarifications   = []
                        st.rerun()

        elif not st.session_state.search_triggered:
            st.info("Введите вопрос и нажмите «Найти ответ»")

    # ── Вкладка «История сессии» ─────────────────────────────────────────────
    with tab_history:
        # Шрифт истории чуть меньше стандартного
        st.markdown("""
        <style>
        [data-testid="stExpander"] .advisor-history-content p,
        [data-testid="stExpander"] .advisor-history-content li {
            font-size: 0.875rem !important;
        }
        </style>
        """, unsafe_allow_html=True)

        history = st.session_state.advisor_history
        if not history:
            st.info("История пуста — ответы сохраняются сюда автоматически после каждого запроса.")
        else:
            h_col1, h_col2 = st.columns([6, 1])
            with h_col1:
                st.caption(f"Сохранено в этой сессии: **{len(history)}**")
            with h_col2:
                if st.button("Очистить всё", key="hist_clear_all", use_container_width=True):
                    st.session_state.advisor_history = []
                    st.rerun()
            st.divider()

            _FS = "font-size: 0.875rem;"  # стиль меньшего шрифта

            for idx, entry in enumerate(reversed(history)):
                real_idx  = len(history) - 1 - idx
                _sp_label = ("  \xb7  ".join(entry["spheres"])
                             if entry.get("spheres") else "все сферы")
                card_label = (
                    f"{entry['ts']}  \xb7  "
                    f"{entry['query'][:80]}{'...' if len(entry['query']) > 80 else ''}"
                )
                _clars = entry.get("clarifications", [])
                if _clars:
                    card_label += f"  [{len(_clars)} уточн.]"

                with st.expander(card_label, expanded=(idx == 0)):
                    # Метаинфо
                    _meta = [f"Модель: {entry.get('model', '—')}"]
                    if entry.get("spheres"):
                        _meta.append(f"Сферы: {_sp_label}")
                    if entry.get("from_faq"):
                        _meta.append("из FAQ")
                    st.caption("  \xb7  ".join(_meta))
                    st.divider()

                    # Вопрос + ответ
                    st.markdown(
                        f'<div style="{_FS}">'
                        f'<p><strong>Вопрос:</strong> {entry["query"]}</p>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div style="{_FS}">{entry["answer"]}</div>',
                        unsafe_allow_html=True,
                    )

                    # Источники исходного ответа
                    if entry.get("sources"):
                        with st.expander(f"Источники ({len(entry['sources'])})", expanded=False):
                            for si, src in enumerate(entry["sources"], 1):
                                st.markdown(
                                    f'<div style="{_FS}"><b>{si}. {src.get("file","?")}</b>'
                                    + (f' (стр. {src["page"]})' if src.get('page') else '')
                                    + '</div>',
                                    unsafe_allow_html=True,
                                )
                                _sp = src.get("sphere", "")
                                if _sp:
                                    st.caption("Сферы: " + "  \xb7  ".join(
                                        [s.strip() for s in _sp.split(",") if s.strip()]))

                    # Уточнения
                    if _clars:
                        st.divider()
                        for ci, clar in enumerate(_clars, 1):
                            st.markdown(
                                f'<div style="{_FS} color: #555;"><strong>Уточнение №{ci}:</strong> '
                                f'{clar["query"]}</div>',
                                unsafe_allow_html=True,
                            )
                            st.markdown(
                                f'<div style="{_FS}">{clar["answer"]}</div>',
                                unsafe_allow_html=True,
                            )
                            if ci < len(_clars):
                                st.divider()

                    # Удалить запись
                    st.divider()
                    if st.button("Удалить", key=f"hist_del_{real_idx}_{entry['id']}",
                                 use_container_width=False):
                        st.session_state.advisor_history.pop(real_idx)
                        st.rerun()




    # ── Вкладка «Все запросы» (персистентная история) ────────────────────────
    with tab_all_history:
        try:
            from core.advisor_history import load_all as _ah_load, search_history as _ah_search, \
                delete_entry as _ah_delete, get_stats as _ah_stats
        except ImportError:
            st.error("Модуль core/advisor_history.py не найден.")
            st.stop()

        _ah_all = _ah_load()
        _ah_st  = _ah_stats(_ah_all)

        # ── Метрики ──────────────────────────────────────────────────────────
        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _mc1.metric("Всего запросов",  _ah_st["total"])
        _mc2.metric("Сегодня",         _ah_st["today"])
        _mc3.metric("С уточнениями",   _ah_st["with_clarifications"])
        _mc4.metric("С", _ah_st.get("oldest_date", "—"))

        st.divider()

        # ── Поиск и фильтры ──────────────────────────────────────────────────
        _ah_q = st.text_input(
            "Поиск по вопросам и ответам",
            placeholder="Введите слово или фразу...",
            key="ah_search_q",
        )

        _fc1, _fc2, _fc3, _fc4 = st.columns([2, 2, 2, 2])
        with _fc1:
            _ah_match = st.radio(
                "Тип совпадения",
                ["По словам", "Точное"],
                key="ah_match_type",
                horizontal=True,
            )
        with _fc2:
            _ah_scope = st.radio(
                "Где искать",
                ["Везде", "Вопрос", "Ответ"],
                key="ah_scope",
                horizontal=True,
            )
        with _fc3:
            _ah_date_from = st.date_input("Дата от", value=None, key="ah_date_from")
        with _fc4:
            _ah_date_to   = st.date_input("Дата до", value=None, key="ah_date_to")

        _AH_SPHERES = [
            "",
            "🔥 Теплоснабжение",
            "💧 Водоснабжение/водоотведение",
            "🗑️ Обращение с ТКО",
            "🔵 Газ",
            "⚡ Электрика",
            "📁 Иные сферы",
        ]
        _ah_sphere_filter = st.selectbox(
            "Фильтр по сфере",
            options=_AH_SPHERES,
            format_func=lambda x: "Все сферы" if x == "" else x,
            key="ah_sphere",
        )

        # ── Применяем поиск/фильтры ──────────────────────────────────────────
        _ah_filtered = _ah_search(
            _ah_all,
            query=_ah_q,
            match_type=_ah_match,
            scope=_ah_scope,
            date_from=str(_ah_date_from) if _ah_date_from else None,
            date_to=str(_ah_date_to)   if _ah_date_to   else None,
            sphere=_ah_sphere_filter,
        )

        _total_found = len(_ah_filtered)
        if _ah_q or _ah_date_from or _ah_date_to or _ah_sphere_filter:
            st.caption(f"Найдено: **{_total_found}** из {_ah_st['total']}")
        else:
            st.caption(f"Всего записей: **{_total_found}**")

        st.divider()

        if not _ah_filtered:
            st.info("Записей не найдено." if (_ah_q or _ah_date_from or _ah_date_to or _ah_sphere_filter)
                    else "История пуста — ответы сохраняются сюда автоматически.")
        else:
            # ── Пагинация ────────────────────────────────────────────────────
            _AH_PAGE_SIZE = 20
            _ah_total_pages = max(1, (_total_found + _AH_PAGE_SIZE - 1) // _AH_PAGE_SIZE)
            _ah_page = st.number_input(
                "Страница",
                min_value=1, max_value=_ah_total_pages,
                value=1, key="ah_page",
            )
            _ah_start = (_ah_page - 1) * _AH_PAGE_SIZE
            _ah_page_recs = _ah_filtered[_ah_start: _ah_start + _AH_PAGE_SIZE]
            st.caption(f"Страница {_ah_page} из {_ah_total_pages}  ·  записи {_ah_start+1}–{min(_ah_start+_AH_PAGE_SIZE, _total_found)}")
            st.divider()

            _FS2 = "font-size: 0.875rem;"

            for _ahi, _rec in enumerate(_ah_page_recs):
                _rec_clars = _rec.get("clarifications", [])
                _rec_spheres = "  ·  ".join(_rec.get("spheres", [])) or "все сферы"

                # Метка карточки: дата + время + начало вопроса
                _ts_display = _rec.get("ts", "")[:16].replace("T", " ")
                _card_lbl = (
                    f"{_ts_display}  ·  "
                    f"{_rec['query'][:70]}{'...' if len(_rec['query']) > 70 else ''}"
                )
                if _rec_clars:
                    _card_lbl += f"  [{len(_rec_clars)} уточн.]"

                # Если есть поисковый запрос — показываем сниппет под заголовком
                _snippet = _rec.get("_snippet", "")

                with st.expander(_card_lbl, expanded=False):
                    # Метаинфо
                    _ah_meta = [
                        f"Модель: {_rec.get('model', '—')}",
                        _rec_spheres,
                    ]
                    if _rec.get("from_faq"):
                        _ah_meta.append("из FAQ")
                    st.caption("  ·  ".join(_ah_meta))

                    if _ah_q and _snippet:
                        st.markdown(
                            f'<div style="{_FS2} color: #888; background: #f8f8f8; '
                            f'padding: 4px 8px; border-radius: 4px; margin-bottom: 6px;">'
                            f'…{_snippet}…</div>',
                            unsafe_allow_html=True,
                        )
                    st.divider()

                    # Вопрос + ответ
                    st.markdown(
                        f'<div style="{_FS2}"><p><strong>Вопрос:</strong> {_rec["query"]}</p></div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div style="{_FS2}">{_rec["answer"]}</div>',
                        unsafe_allow_html=True,
                    )

                    # Источники
                    if _rec.get("sources"):
                        with st.expander(f"Источники ({len(_rec['sources'])})", expanded=False):
                            for _si, _src in enumerate(_rec["sources"], 1):
                                st.markdown(
                                    f'<div style="{_FS2}"><b>{_si}. {_src.get("file","?")}</b>'
                                    + (f' (стр. {_src["page"]})' if _src.get("page") else "")
                                    + "</div>",
                                    unsafe_allow_html=True,
                                )
                                if _src.get("sphere"):
                                    st.caption("Сферы: " + _src["sphere"])

                    # Уточнения
                    if _rec_clars:
                        st.divider()
                        for _ci, _clar in enumerate(_rec_clars, 1):
                            st.markdown(
                                f'<div style="{_FS2} color:#555;">'
                                f'<strong>Уточнение №{_ci}:</strong> {_clar["query"]}</div>',
                                unsafe_allow_html=True,
                            )
                            st.markdown(
                                f'<div style="{_FS2}">{_clar["answer"]}</div>',
                                unsafe_allow_html=True,
                            )
                            if _ci < len(_rec_clars):
                                st.divider()

                    # Удалить из персистентной истории
                    st.divider()
                    if st.button(
                        "Удалить",
                        key=f"ah_del_{_rec['id']}_{_ahi}_{_ah_page}",
                        use_container_width=False,
                    ):
                        _ah_delete(_rec["id"])
                        st.rerun()