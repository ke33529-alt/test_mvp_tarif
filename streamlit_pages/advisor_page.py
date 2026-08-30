# streamlit_pages/advisor_page.py
"""
UI Советчика по нормативной базе
──────────────────────────────────────────────────────────────────────────────
Вкладки:
  1. Запрос         — поиск + стриминг ответа + уточнения
  2. История сессии — запросы текущей сессии (в памяти вкладки)
  3. Все запросы    — персистентная история с поиском и фильтрами

ИЗОЛЯЦИЯ ДАННЫХ (что изменилось)
─────────────────────────────────
1. enforce_identity_boundary() в начале рендера: при смене пользователя в
   одной вкладке session_state вычищается. Раньше история предыдущего
   оставалась на экране у следующего вошедшего.
2. Сессионная история — через core.session_scope (штамп личности на записи).
3. Персистентная история — core.advisor_history, файл на пользователя.
   Раньше load_all() читал общий файл: каждый видел запросы, ответы и
   сниппеты источников всех остальных, включая чужие локальные базы.
4. Настройки — core.user_prefs, файл на пользователя. Раньше общий
   config/advisor_prefs.json: user_context (должность, организация) утекал
   в системный промпт чужих запросов, а top_k/temperature затирались.
5. Кнопка очистки кэша — по роли: суперадмин чистит всё, остальные только
   свой сегмент. Раньше любой пользователь вайпил кэш всех сегментов.

Локальная база знаний сегмента:
  Пункт «📁 Локальная база сегмента» в мультиселекте «Вид документа» — это
  не фильтр по tariff_docs, а подключение отдельной ChromaDB-коллекции
  local_kb_{org_id} (см. core/local_kb.py). Обрабатывается внутри
  core.advisor.search_vector_db по значению "local" в doc_types.

Быстрый выбор из задач:
  Кнопка «Выбрать из задач» над полем запроса открывает модалку со списком
  личных задач (core/tasks.py). Выбранная задача подставляет свой текст в
  поле запроса (question_input / last_query) — быстрое заполнение без ручного
  копирования.
"""
from __future__ import annotations
import os
import pandas as pd
from datetime import datetime

import streamlit as st
from core.feedback import submit_feedback
from core.session_scope import (
    enforce_identity_boundary, history_add, history_get,
    history_clear, history_delete, history_update_last,
)
from core.user_prefs import load_prefs, save_prefs
from core import tasks as _tasks_core


# Модель Советчика по умолчанию — принудительно выставляется при первом
# открытии раздела в сессии (см. show_advisor), чтобы «на старте» всегда был
# Qwen 3.5 9B независимо от прошлого выбора.
DEFAULT_ADVISOR_MODEL = "qwen/qwen3.5-9b"


# =============================================================================
# Буферизация потока токенов
# =============================================================================
# ЗАЧЕМ. st.write_stream отправляет в браузер отдельное сообщение на КАЖДЫЙ
# элемент генератора, а фронтенд на каждое сообщение перерисовывает markdown
# целиком и заново прогоняет весь накопленный текст через KaTeX. На длинном
# ответе с таблицами и формулами это тысячи перерисовок: главный поток
# браузера уходит в непрерывный Scripting на десятки секунд (замерено:
# 29 488 мс при генерации 31.85 сек), клиентский код Streamlit перестаёт
# успевать обрабатывать WebSocket-события, соединение рвётся, и сессия
# остаётся мёртвой — сервер потом пишет "Discarding BackMsg for disconnected
# session" на каждый клик, а в браузере навсегда крутится индикатор.
#
# Диагностика, которая привела сюда: сервер в этот момент полностью свободен
# (py-spy — ни одного потока со скриптом, /_stcore/health отвечает ok),
# соединение при бездействии живёт минутами, короткие ответы проходят без
# единого сбоя (15+ запросов подряд), а длинные с таблицами вешают стабильно.
#
# ЧТО ДЕЛАЕТ. Копит токены и отдаёт их пачкой не чаще раза в FLUSH_INTERVAL.
# Стриминг для пользователя выглядит так же — текст идёт живым потоком, — но
# перерисовок становится в десятки раз меньше, и главный поток успевает
# обслуживать сокет.
_STREAM_FLUSH_INTERVAL = 0.25   # сек между обновлениями UI
_STREAM_FLUSH_CHARS    = 400    # либо когда накопилось столько символов


def _buffered_stream(token_iter, flush_interval: float = _STREAM_FLUSH_INTERVAL,
                     flush_chars: int = _STREAM_FLUSH_CHARS):
    """
    Оборачивает генератор токенов, отдавая их склеенными пачками.

    Пачка уходит в UI, когда с прошлой отдачи прошло flush_interval секунд
    или накопилось flush_chars символов — что наступит раньше. Остаток
    обязательно отдаётся в конце, иначе потерялся бы хвост ответа.

    Итоговый текст, который вернёт st.write_stream, не меняется: это та же
    последовательность символов, просто нарезанная крупнее.
    """
    import time as _time

    buf: list[str] = []
    buf_len = 0
    last_flush = _time.monotonic()

    for chunk in token_iter:
        if not chunk:
            continue
        buf.append(chunk)
        buf_len += len(chunk)

        now = _time.monotonic()
        if buf_len >= flush_chars or (now - last_flush) >= flush_interval:
            yield "".join(buf)
            buf.clear()
            buf_len = 0
            last_flush = now

    if buf:
        yield "".join(buf)


def _resolve_advisor_default(model_names: list) -> str:
    """
    Возвращает имя модели из доступного списка, наиболее близкое к Qwen 3.5 9B.
    Приоритет: точное совпадение → нормализованное совпадение (учитывает записи
    'qwen3.5-9b' / 'qwen-3.5-9b' / 'qwen3.5-9b-instruct' и т.п.) → сам
    DEFAULT_ADVISOR_MODEL, даже если его нет в списке (чтобы дефолт не «сползал»
    на первую попавшуюся модель).
    """
    if not model_names:
        return DEFAULT_ADVISOR_MODEL
    if DEFAULT_ADVISOR_MODEL in model_names:
        return DEFAULT_ADVISOR_MODEL

    def _norm(s: str) -> str:
        return "".join(ch for ch in str(s).lower() if ch.isalnum())

    for name in model_names:
        n = _norm(name)
        if "qwen" in n and "35" in n and "9b" in n:
            return name
    return DEFAULT_ADVISOR_MODEL


@st.dialog("Выбрать задачу")
def _adv_task_picker_dialog(user: dict):
    """
    Модалка быстрого выбора: текст выбранной задачи попадает в поле запроса.

    Флаг открытия гасится ДО вызова (см. show_advisor), поэтому закрытие
    крестиком не открывает модалку повторно. Клик по задаче проставляет
    question_input/last_query и вызывает rerun ДО инстанцирования text_area —
    значение подхватывается на следующем проходе без ошибки session_state.
    """
    items = _tasks_core.sort_for_display(_tasks_core.list_own_tasks(user))
    if not items:
        st.info("У вас пока нет задач. Создайте их в разделе «Задачи».")
        return

    st.caption("Нажмите на задачу — её текст попадёт в поле запроса.")
    _show_done = st.toggle("Показывать выполненные", value=False, key="_adv_pick_show_done")

    _shown = 0
    for t in items:
        if not _show_done and t.get("status") == _tasks_core.STATUS_DONE:
            continue
        _shown += 1
        _color  = _tasks_core.PRIORITY_COLORS.get(t.get("priority"), "#999")
        _st_lbl = _tasks_core.STATUS_LABELS.get(t.get("status"), "")
        _is_done = t.get("status") == _tasks_core.STATUS_DONE
        _dl_html = ""
        if _is_done:
            # Для выполненной задачи срок неактуален — вместо него дата выполнения.
            _completed = t.get("completed_at", "")
            if _completed:
                _dl_html = (f'&nbsp;&nbsp;<span style="color:#1e7a45;font-size:0.72rem;'
                            f'font-weight:600">Выполнено {_tasks_core.format_completed_ru(_completed)}</span>')
        else:
            _due = t.get("due_date", "")
            if _due:
                _tier = _tasks_core.deadline_tier(_due)
                if _tier != _tasks_core.DEADLINE_NONE:
                    _dc = _tasks_core.DEADLINE_COLORS.get(_tier, "#999")
                    _dl_html = (f'&nbsp;&nbsp;<span style="color:{_dc};font-size:0.72rem;'
                                f'font-weight:600">{_tasks_core.deadline_label(_due)}</span>')
        st.markdown(
            f'<span style="display:inline-block;width:10px;height:10px;'
            f'border-radius:50%;background:{_color};margin-right:6px;'
            f'vertical-align:middle"></span>'
            f'<span style="font-size:0.72rem;color:#5a6a7a">{_st_lbl}</span>'
            + _dl_html,
            unsafe_allow_html=True,
        )
        _txt   = t.get("text", "")
        _label = _txt[:90] + ("…" if len(_txt) > 90 else "")
        if st.button(_label, key=f"_adv_pick_{t['id']}", use_container_width=True):
            st.session_state["question_input"] = _txt
            st.session_state["last_query"]     = _txt
            st.rerun()

    if _shown == 0:
        st.info("Активных задач нет. Включите «Показывать выполненные».")


def _render_jumped_entry(entry: dict):
    """
    Отдельный экран записи, открытой по ссылке из задачи («Перейти к записи»
    в разделе «Задачи»). Показывается ВМЕСТО обычного интерфейса Советчика
    (show_advisor делает return сразу после вызова) — иначе под карточкой
    оставался бы весь интерфейс нового запроса, что и вызывало путаницу.

    Кнопка «Назад» ведёт не в пустой Советчик, а прямо в «Задачи» — туда,
    откуда пользователь пришёл.
    """
    st.markdown(
        '<span style="background:#1B5C74;color:#fff;padding:3px 10px;'
        'border-radius:10px;font-size:0.75rem;font-weight:600">'
        '🔗 Запись, связанная с задачей</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)

    with st.container(border=True):
        _ts = (entry.get("ts", "") or "")[:16].replace("T", " ")
        _meta = [f"Модель: {entry.get('model', '—')}"]
        if _ts:
            _meta.append(_ts)
        if entry.get("spheres"):
            _meta.append("Сферы: " + "  \xb7  ".join(entry["spheres"]))
        st.caption("  \xb7  ".join(_meta))
        st.divider()
        st.markdown(f"**Вопрос:** {entry.get('query', '')}")
        st.markdown(entry.get("answer", ""))
        if entry.get("sources"):
            with st.expander(f"Источники ({len(entry['sources'])})", expanded=False):
                for si, src in enumerate(entry["sources"], 1):
                    st.markdown(f"**{si}. {src.get('file', '?')}**"
                                + (f" (стр. {src['page']})" if src.get('page') else ""))
                    st.caption(src.get('snippet', '')[:400]
                               + ("..." if len(src.get('snippet', '')) > 400 else ""))

    st.divider()
    if st.button("← Назад к задачам", key="_adv_jumped_back", type="primary"):
        st.session_state.pop("_adv_jump_entry_id", None)
        st.session_state["main_choice"]  = "Задачи"
        st.session_state["show_landing"] = False
        st.rerun()


def show_advisor():
    # ── Переход по ссылке «Связано с записью» из раздела «Задачи» ────────────
    # ВАЖНО: флаг читаем через get(), НЕ через pop(). Streamlit иногда
    # выполняет скрипт дважды подряд без участия пользователя (двойной
    # прогон при рендере) — при одноразовом pop() второй, никем не
    # инициированный прогон видел уже пустой флаг и откатывался на обычный
    # интерфейс Советчика, хотя пользователь ничего не нажимал (баг:
    # jump-экран сам "сбрасывался"). Флаг теперь ЖИВЁТ в session_state,
    # пока пользователь явно не нажмёт «Назад» — тогда он снимается вместе
    # с переключением main_choice на «Задачи» (см. кнопку выше и ниже).
    _jump_entry_id = st.session_state.get("_adv_jump_entry_id")
    if _jump_entry_id:
        try:
            from core.auth import get_current_user as _jget_user
            _jump_user = _jget_user() or {}
        except Exception:
            _jump_user = {}
        try:
            from core.advisor_history import load_all as _jump_load_all
            _jump_entries = _jump_load_all(
                scope="user",
                org_id=_jump_user.get("org_id", ""),
                user_id=_jump_user.get("user_id", ""),
            )
            _jump_entry = next((e for e in _jump_entries if e.get("id") == _jump_entry_id), None)
        except Exception:
            _jump_entry = None

        if _jump_entry:
            _render_jumped_entry(_jump_entry)
        else:
            st.warning("Запись, на которую ссылалась задача, не найдена — возможно, была удалена.")
            if st.button("← Назад к задачам", key="_adv_jumped_back_missing", type="primary"):
                st.session_state.pop("_adv_jump_entry_id", None)
                st.session_state["main_choice"]  = "Задачи"
                st.session_state["show_landing"] = False
                st.rerun()
        return

    # ── Граница личности ─────────────────────────────────────────────────────
    # ДО чтения любых персональных ключей: если в этой вкладке сменился
    # пользователь, здесь вычищается история предыдущего.
    if enforce_identity_boundary():
        st.info("Сессия начата заново: сменился пользователь.")

    st.header("Советчик по нормативной базе")
    st.info("Задайте вопрос по тарифному регулированию — система найдёт ответ в актуальной базе НПА")

    # Текущий пользователь: org_id нужен для локальной базы, role — для
    # области очистки кэша, user_id — для персональных настроек и истории.
    try:
        from core.auth import get_current_user
        _current_user = get_current_user()
    except Exception:
        _current_user = None
    _current_user = _current_user or {}
    _user_org_id  = _current_user.get("org_id") or ""
    _user_id      = _current_user.get("user_id") or ""
    _user_role    = _current_user.get("role") or "user"

    # ── Персональные настройки ───────────────────────────────────────────────
    # Читаем файл один раз за сессию, дальше живём в session_state.
    if "_adv_prefs_loaded" not in st.session_state:
        _p = load_prefs(_user_id)
        st.session_state["_adv_top_k"]           = _p["top_k"]
        st.session_state["_adv_neighbor_radius"] = _p["neighbor_radius"]
        st.session_state["_adv_temperature"]     = _p["temperature"]
        st.session_state["_adv_user_context"]    = _p["user_context"]
        st.session_state["_adv_answer_length"]   = _p["answer_length"]
        st.session_state["_adv_prefs_loaded"]    = True

    def _persist_prefs():
        """Сохраняет текущий снимок настроек в личный файл пользователя."""
        save_prefs({
            "top_k":           st.session_state.get("_adv_top_k", 20),
            "neighbor_radius": st.session_state.get("_adv_neighbor_radius", 0),
            "temperature":     st.session_state.get("_adv_temperature", 0.3),
            "user_context":    st.session_state.get("_adv_user_context", ""),
            "answer_length":   st.session_state.get("_adv_answer_length", "short"),
        }, _user_id)

    for key, val in [
        ("last_query", ""), ("last_result", None), ("search_triggered", False),
        ("sources_only_mode", False), ("query_times", []),
    ]:
        if key not in st.session_state:
            st.session_state[key] = val

    # Модель по умолчанию: при первом открытии Советчика в сессии принудительно
    # ставим Qwen 3.5 9B и сбрасываем виджет выбора модели, чтобы selectbox взял
    # это значение как стартовое. Далее выбор пользователя сохраняется в рамках
    # сессии (можно переключиться на другую модель).
    if not st.session_state.get("_adv_model_boot"):
        st.session_state["advisor_model"] = DEFAULT_ADVISOR_MODEL
        st.session_state.pop("advisor_model_select", None)
        st.session_state["_adv_model_boot"] = True
    st.session_state.setdefault("advisor_model", DEFAULT_ADVISOR_MODEL)

    # Проверка векторной базы
    vector_db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vector_db"
    )
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
        # Сброс к значениям по умолчанию. Флаг ставится кнопкой ниже, а
        # применяется ЗДЕСЬ — до инстанцирования слайдеров: Streamlit не даёт
        # менять session_state виджета после его создания в этом же прогоне.
        # Дефолты: 40 источников, 0 соседей, креативность 0.1, пустой контекст.
        if st.session_state.pop("_adv_reset_defaults", False):
            st.session_state["_adv_top_k"]             = 40
            st.session_state["_adv_neighbor_radius"]   = 0
            st.session_state["_adv_temperature"]       = 0.1
            st.session_state["_adv_user_context"]      = ""
            st.session_state["neighbor_radius"]        = 0
            # ключи виджетов — чтобы слайдеры и поле показали дефолт
            st.session_state["top_k_slider"]           = 40
            st.session_state["temp_slider"]            = 0.1
            st.session_state["neighbor_radius_slider"] = 0
            st.session_state["user_context_input"]     = ""
            _persist_prefs()
            st.toast("Настройки сброшены к значениям по умолчанию")

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
                     "0 — только сам чанк. Больше — шире контекст, но LLM может потеряться.",
            )
            _changed = (
                top_k           != st.session_state.get("_adv_top_k")
                or neighbor_radius != st.session_state.get("_adv_neighbor_radius")
                or float(temperature) != float(st.session_state.get("_adv_temperature", 0.3))
            )
            st.session_state.neighbor_radius         = neighbor_radius
            st.session_state["_adv_top_k"]           = top_k
            st.session_state["_adv_neighbor_radius"] = neighbor_radius
            st.session_state["_adv_temperature"]     = float(temperature)
            if neighbor_radius > 0:
                st.caption(f"Каждый результат даёт {1 + neighbor_radius * 2} чанков контекста")
            # Пишем файл только при реальном изменении, а не на каждом рероне
            if _changed:
                _persist_prefs()

        with col2:
            try:
                from core.advisor import get_available_models
                model_names = [m["name"] for m in get_available_models()] or [DEFAULT_ADVISOR_MODEL]
            except Exception:
                model_names = [DEFAULT_ADVISOR_MODEL]

            # По умолчанию — Qwen 3.5 9B (или ближайшее совпадение в списке).
            _default_model = _resolve_advisor_default(model_names)
            _cur_model = st.session_state.get("advisor_model") or _default_model
            if _cur_model not in model_names:
                _cur_model = _default_model
            _model_idx = model_names.index(_cur_model) if _cur_model in model_names else 0
            selected_model = st.selectbox(
                "🤖 Модель", options=model_names,
                index=_model_idx,
                key="advisor_model_select",
            )
            st.session_state.advisor_model = selected_model
            _def_note = (" · по умолчанию Qwen 3.5 9B"
                         if _default_model in model_names
                         else " · Qwen 3.5 9B недоступна на сервере")
            st.caption(f"Доступно моделей: {len(model_names)}{_def_note}")

            # Длина ответа
            _AL_OPTIONS = {"Краткий": "short", "Развёрнутый": "detailed"}
            _al_saved   = st.session_state.get("_adv_answer_length", "short")
            _al_label   = next((k for k, v in _AL_OPTIONS.items() if v == _al_saved), "Краткий")
            _al_selected = st.radio(
                "Длина ответа",
                options=list(_AL_OPTIONS.keys()),
                index=list(_AL_OPTIONS.keys()).index(_al_label),
                horizontal=True,
                key="advisor_answer_length_radio",
                help="Краткий — 3–5 предложений по существу. "
                     "Развёрнутый — все нормы, условия, исключения.",
            )
            _al_value = _AL_OPTIONS[_al_selected]
            if _al_value != _al_saved:
                st.session_state["_adv_answer_length"] = _al_value
                _persist_prefs()

            sources_only_mode = st.toggle(
                "🧪 Режим тестов чанков (без LLM)",
                value=st.session_state.sources_only_mode,
                key="sources_only_toggle",
            )
            st.session_state.sources_only_mode = sources_only_mode

            # Очистка кэша по роли: суперадмин — весь, остальные — свой сегмент
            _cache_btn_label = ("🗑 Очистить кэш LLM (все сегменты)"
                                if _user_role == "superadmin"
                                else "🗑 Очистить кэш LLM (мой сегмент)")
            if st.button(_cache_btn_label, key="clear_cache_btn", use_container_width=True):
                from core.advisor import clear_llm_cache_for_current_user
                _removed, _scope_label = clear_llm_cache_for_current_user()
                st.session_state.query_times = []
                st.success(f"✅ Кэш очищен: {_removed} записей ({_scope_label})")
                st.rerun()

            if st.button(
                "Сбросить к дефолтным",
                key="reset_defaults_btn",
                use_container_width=True,
                help="40 источников · 0 соседей · креативность 0.1 · пустой контекст пользователя",
            ):
                st.session_state["_adv_reset_defaults"] = True
                st.rerun()

        if st.session_state.query_times:
            st.divider()
            avg_time = sum(st.session_state.query_times) / len(st.session_state.query_times)
            c1, c2, c3 = st.columns(3)
            c1.metric("Запросов",      len(st.session_state.query_times))
            c2.metric("Среднее время", f"{avg_time:.1f} сек")
            c3.metric("Последний",     f"{st.session_state.query_times[-1]:.1f} сек")

        # ── Контекст пользователя ────────────────────────────────────────────
        st.divider()
        _uc_saved = st.session_state.get("_adv_user_context", "")
        _uc_label = "Контекст пользователя  ·  ✅ задан" if _uc_saved.strip() else "Контекст пользователя"
        st.markdown(f"**{_uc_label}**")
        st.caption(
            "Роль, должность, место работы — ИИ учитывает это при ответах. "
            "Виден только вам и сохраняется между входами."
        )
        _uc_new = st.text_area(
            "Контекст пользователя",
            value=_uc_saved,
            height=90,
            placeholder=(
                "Пример: Специалист тарифного отдела РСО «Теплосеть», г. Екатеринбург. "
                "Подготавливаю тарифные заявки по теплоснабжению. "
                "Интересует практика РЭК и позиция ФАС по спорным статьям затрат."
            ),
            key="user_context_input",
            label_visibility="collapsed",
        )
        _uc_btn1, _uc_btn2 = st.columns([2, 1])
        with _uc_btn1:
            if st.button("💾 Сохранить контекст", key="save_user_context_btn",
                         type="primary", use_container_width=True):
                st.session_state["_adv_user_context"] = _uc_new
                if _persist_prefs() is not False:
                    st.success("✅ Контекст сохранён" if _uc_new.strip() else "✅ Контекст очищен")
                st.rerun()
        with _uc_btn2:
            if _uc_saved.strip() and st.button("Очистить", key="clear_user_context_btn",
                                               use_container_width=True):
                st.session_state["_adv_user_context"] = ""
                _persist_prefs()
                st.rerun()

    # ── Инициализация истории и состояния уточнений ──────────────────────────
    if "advisor_history" not in st.session_state:
        st.session_state.advisor_history = []
    if "clarifications" not in st.session_state:
        st.session_state.clarifications = []
    if "_adv_hist_id" not in st.session_state:
        st.session_state._adv_hist_id = None

    # ── Вкладки ──────────────────────────────────────────────────────────────
    tab_query, tab_history, tab_all_history = st.tabs(
        ["Запрос", "История сессии", "Все запросы"]
    )

    with tab_query:
        # ── Фильтры: сфера + вид документа ───────────────────────────────────
        _ADV_SPHERES = [
            "🔥 Теплоснабжение",
            "💧 Водоснабжение/водоотведение",
            "🗑️ Обращение с ТКО",
            "🔵 Газ",
            "⚡ Электрика",
            "📁 Иные сферы",
        ]
        _ADV_DOC_TYPES = {
            "📜 Общие НПА":               "npa",
            "⚖️ Документы ФАС":          "fas",
            "🏛️ Судебная практика":      "court",
            "📋 Методички и разъяснения": "methodics",
            "📁 Локальная база сегмента": "local",
        }

        _flt_col1, _flt_col2 = st.columns(2)
        with _flt_col1:
            adv_spheres = st.multiselect(
                "Сфера деятельности",
                options=_ADV_SPHERES,
                default=[],
                key="advisor_spheres_filter",
                placeholder="Все сферы",
                help="Уточните сферу(ы) для целевого поиска. "
                     "Документы без назначенной сферы всегда включаются в результаты.",
            )
        with _flt_col2:
            adv_doc_type_labels = st.multiselect(
                "Вид документа",
                options=list(_ADV_DOC_TYPES.keys()),
                default=["📜 Общие НПА"],
                key="advisor_doc_types_filter",
                placeholder="Все виды",
                help="Ограничьте поиск конкретными видами документов. "
                     "«Локальная база сегмента» — ваши внутренние документы, "
                     "видимые только вашей организации.",
            )
            adv_doc_types = [_ADV_DOC_TYPES[lbl] for lbl in adv_doc_type_labels]

        if "local" in adv_doc_types and not _user_org_id:
            st.warning(
                "«Локальная база сегмента» недоступна: у вашего аккаунта не назначен сегмент."
            )

        # ── Статус документа ─────────────────────────────────────────────────
        _STATUS_OPTIONS = {
            "Только действующие":   "active",
            "Не вступившие в силу": "pending",
            "Утратившие силу":      "expired",
        }
        _status_label = st.radio(
            "Статус документов",
            options=list(_STATUS_OPTIONS.keys()),
            index=0,
            horizontal=True,
            key="advisor_doc_status_radio",
            help="Документы без проставленных дат всегда включаются в результаты. "
                 "На документы локальной базы сегмента статус действия не распространяется.",
        )
        adv_doc_status = _STATUS_OPTIONS[_status_label]

        _active_filters = []
        if adv_spheres:
            _active_filters.append("сферы: " + "  \xb7  ".join(adv_spheres))
        if adv_doc_type_labels:
            _active_filters.append("виды: " + "  \xb7  ".join(adv_doc_type_labels))
        if _active_filters:
            st.caption("Активен фильтр: **" + "   |   ".join(_active_filters) + "**")

        # ── Быстрый выбор из задач ───────────────────────────────────────────
        # Кнопка открывает модалку со списком личных задач; выбранная задача
        # подставляется в поле запроса ниже. Флаг гасим сразу (как в app.py),
        # чтобы закрытие модалки крестиком не открывало её повторно.
        if st.button("Выбрать из задач", key="adv_pick_task_btn"):
            st.session_state["_adv_show_task_picker"] = True
            st.rerun()
        if st.session_state.get("_adv_show_task_picker"):
            st.session_state["_adv_show_task_picker"] = False
            _adv_task_picker_dialog(_current_user)

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
                        strip_thinking_blocks, set_sources_only_mode,
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
                                doc_types=adv_doc_types if adv_doc_types else None,
                                doc_status=adv_doc_status,
                                org_id=_user_org_id,
                            )

                        if sources and not st.session_state.sources_only_mode:
                            st.success(f"Ответ сгенерирован ИИ · модель: {st.session_state.advisor_model}")
                            import itertools
                            gen = stream_ai_answer(
                                query, sources,
                                st.session_state.advisor_model,
                                temperature,
                                user_context=st.session_state.get("_adv_user_context", ""),
                                answer_length=st.session_state.get("_adv_answer_length", "short"),
                                org_id=_user_org_id,
                            )
                            with st.spinner("Модель формирует ответ..."):
                                first_token = next(gen, None)
                            if first_token is not None:
                                raw_answer = st.write_stream(
                                    _buffered_stream(
                                        itertools.chain([first_token], gen)
                                    )
                                )
                            else:
                                raw_answer = ""
                            answer = strip_thinking_blocks(raw_answer)

                            # Аудит: фиксируем ФАКТ запроса, без текста вопроса
                            try:
                                from core.audit import log_event
                                log_event(
                                    org_id=_user_org_id,
                                    user_id=_user_id,
                                    role=_user_role,
                                    event="llm_query",
                                    module="advisor",
                                    meta={
                                        "model":       st.session_state.advisor_model,
                                        "num_sources": len(sources),
                                        "local_kb":    "local" in adv_doc_types,
                                    },
                                )
                            except Exception:
                                pass

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
                    st.session_state.clarifications   = []

                    # Автосохранение в историю
                    if answer and not answer.startswith("❌") and not st.session_state.sources_only_mode:
                        _entry_id = id(datetime.now())
                        history_add({
                            "id":             _entry_id,
                            "ts":             datetime.now().strftime("%H:%M:%S"),
                            "query":          query,
                            "answer":         answer,
                            "model":          st.session_state.advisor_model,
                            "spheres":        list(adv_spheres),
                            "sources":        sources,
                            "from_faq":       from_faq,
                            "clarifications": [],
                        })
                        # Персистентная история — в личный файл пользователя
                        try:
                            from core.advisor_history import save_entry as _adv_save
                            st.session_state._adv_hist_id = _adv_save(
                                query=query,
                                answer=answer,
                                model=st.session_state.advisor_model,
                                spheres=list(adv_spheres),
                                sources=sources,
                                from_faq=from_faq,
                                org_id=_user_org_id,
                                user_id=_user_id,
                            )
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

                # ── Источники ────────────────────────────────────────────────
                if sources:
                    with st.expander(f"Источники ({len(sources)})", expanded=False):
                        for i, src in enumerate(sources, 1):
                            _kind_mark = " · 📁 локальная база" \
                                         if src.get("source_kind") == "local" else ""
                            st.markdown(f"**{i}. {src.get('file', '?')}**"
                                        + (f" (стр. {src['page']})" if src.get('page') else "")
                                        + (f" · {src['category']}" if src.get('category') else "")
                                        + _kind_mark)
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

                    def _rate(label: str, rating: int):
                        submit_feedback("user", "answer_rating", label,
                                        question=query_for_fb[:500],
                                        answer=answer[:1000], rating=rating)
                        st.session_state.last_result      = None
                        st.session_state.search_triggered = False
                        st.session_state.clarifications   = []

                    with col1:
                        if st.button("👍", key="btn_good", use_container_width=True):
                            _rate("Полезно", 3)
                            st.success("Спасибо!")
                            st.rerun()
                    with col2:
                        if st.button("😐", key="btn_neutral", use_container_width=True):
                            _rate("Нормально", 2)
                            st.success("Спасибо!")
                            st.rerun()
                    with col3:
                        if st.button("👎", key="btn_bad", use_container_width=True):
                            _rate("Не помогло", 1)
                            st.success("Спасибо!")
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

                                _clars  = st.session_state.clarifications
                                _prev_a = _clars[-1]["answer"] if _clars else result.get("answer", "")

                                with st.spinner("Ищем в базе знаний..."):
                                    _new_sources = _svdb(
                                        clarify_q,
                                        top_k=st.session_state.get("_adv_top_k", 20),
                                        spheres=adv_spheres if adv_spheres else None,
                                        doc_types=adv_doc_types if adv_doc_types else None,
                                        doc_status=adv_doc_status,
                                        org_id=_user_org_id,
                                    )

                                st.success(f"Уточнение · модель: {st.session_state.advisor_model}")
                                import itertools as _it
                                _gen = _stream_clar(
                                    clarify_q,
                                    _prev_a,
                                    _new_sources,
                                    st.session_state.advisor_model,
                                    st.session_state.get("_adv_temperature", 0.3),
                                    user_context=st.session_state.get("_adv_user_context", ""),
                                    answer_length=st.session_state.get("_adv_answer_length", "short"),
                                )
                                with st.spinner("Модель формирует ответ..."):
                                    _first = next(_gen, None)
                                _raw = st.write_stream(
                                    _buffered_stream(_it.chain([_first], _gen))
                                ) if _first is not None else ""
                                _clar_answer = _strip(_raw) if _raw else "❌ Не найдено релевантных документов."

                                st.session_state.clarifications.append({
                                    "query":   clarify_q,
                                    "answer":  _clar_answer,
                                    "sources": _new_sources,
                                })

                                # Сессионная история — последняя запись текущей личности
                                history_update_last(
                                    clarifications=list(st.session_state.clarifications)
                                )
                                # Персистентная история
                                if st.session_state.get("_adv_hist_id"):
                                    try:
                                        from core.advisor_history import update_clarifications as _adv_upd
                                        _adv_upd(
                                            st.session_state._adv_hist_id,
                                            st.session_state.clarifications,
                                            org_id=_user_org_id,
                                            user_id=_user_id,
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
        st.markdown("""
        <style>
        [data-testid="stExpander"] .advisor-history-content p,
        [data-testid="stExpander"] .advisor-history-content li {
            font-size: 0.875rem !important;
        }
        </style>
        """, unsafe_allow_html=True)

        # history_get() отдаёт только записи текущей личности
        history = history_get()
        if not history:
            st.info("История пуста — ответы сохраняются сюда автоматически после каждого запроса.")
        else:
            h_col1, h_col2 = st.columns([6, 1])
            with h_col1:
                st.caption(f"Сохранено в этой сессии: **{len(history)}**")
            with h_col2:
                if st.button("Очистить всё", key="hist_clear_all", use_container_width=True):
                    history_clear()
                    st.rerun()
            st.divider()

            _FS = "font-size: 0.875rem;"

            for idx, entry in enumerate(reversed(history)):
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
                    _meta = [f"Модель: {entry.get('model', '—')}"]
                    if entry.get("spheres"):
                        _meta.append(f"Сферы: {_sp_label}")
                    if entry.get("from_faq"):
                        _meta.append("из FAQ")
                    st.caption("  \xb7  ".join(_meta))
                    st.divider()

                    st.markdown(
                        f'<div style="{_FS}"><p><strong>Вопрос:</strong> {entry["query"]}</p></div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div style="{_FS}">{entry["answer"]}</div>',
                        unsafe_allow_html=True,
                    )

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

                    # Удаление по id, а не по индексу: history_get() — это
                    # отфильтрованный список, его индексы не совпадают с
                    # индексами в session_state.
                    st.divider()
                    if st.button("Удалить", key=f"hist_del_{entry['id']}",
                                 use_container_width=False):
                        history_delete(entry["id"])
                        st.rerun()

    # ── Вкладка «Все запросы» (персистентная история) ────────────────────────
    with tab_all_history:
        try:
            from core.advisor_history import (
                load_all as _ah_load, search_history as _ah_search,
                delete_entry as _ah_delete, get_stats as _ah_stats,
                legacy_count as _ah_legacy,
            )
        except ImportError:
            st.error("Модуль core/advisor_history.py не найден.")
            st.stop()

        # ── Область видимости ────────────────────────────────────────────────
        # user — своя история (всем), org — весь сегмент (админ сегмента),
        # all — всё (суперадмин). Проверка прав здесь, а не в модуле истории.
        _ah_scope = "user"
        if _user_role == "superadmin":
            _scope_label = st.radio(
                "Показывать",
                options=["Только мои", "Все сегменты"],
                index=0, horizontal=True, key="ah_scope_sa",
            )
            _ah_scope = "user" if _scope_label == "Только мои" else "all"
        elif _user_role == "segment_admin":
            _scope_label = st.radio(
                "Показывать",
                options=["Только мои", "Весь мой сегмент"],
                index=0, horizontal=True, key="ah_scope_sadm",
            )
            _ah_scope = "user" if _scope_label == "Только мои" else "org"

        _ah_all = _ah_load(scope=_ah_scope, org_id=_user_org_id, user_id=_user_id)
        _ah_st  = _ah_stats(_ah_all)

        # Legacy-файл: у его записей нет владельца, поэтому он не показывается
        # никому. Сообщаем об этом только суперадмину — решение принимать ему.
        if _user_role == "superadmin":
            _legacy_n = _ah_legacy()
            if _legacy_n:
                st.warning(
                    f"Найден старый общий файл истории: {_legacy_n} записей без "
                    f"владельца. Он не показывается никому. Присвоить его "
                    f"конкретному пользователю: `adopt_legacy(user_id, org_id)`; "
                    f"удалить безвозвратно: `purge_legacy()` из core.advisor_history."
                )

        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _mc1.metric("Всего запросов", _ah_st["total"])
        _mc2.metric("Сегодня",        _ah_st["today"])
        _mc3.metric("С уточнениями",  _ah_st["with_clarifications"])
        _mc4.metric("С",              _ah_st.get("oldest_date", "—"))

        st.divider()

        _ah_q = st.text_input(
            "Поиск по вопросам и ответам",
            placeholder="Введите слово или фразу...",
            key="ah_search_q",
        )

        _fc1, _fc2, _fc3, _fc4 = st.columns([2, 2, 2, 2])
        with _fc1:
            _ah_match = st.radio("Тип совпадения", ["По словам", "Точное"],
                                 key="ah_match_type", horizontal=True)
        with _fc2:
            _ah_text_scope = st.radio("Где искать", ["Везде", "Вопрос", "Ответ"],
                                      key="ah_scope", horizontal=True)
        with _fc3:
            _ah_date_from = st.date_input("Дата от", value=None, key="ah_date_from")
        with _fc4:
            _ah_date_to = st.date_input("Дата до", value=None, key="ah_date_to")

        _AH_SPHERES = [
            "", "🔥 Теплоснабжение", "💧 Водоснабжение/водоотведение",
            "🗑️ Обращение с ТКО", "🔵 Газ", "⚡ Электрика", "📁 Иные сферы",
        ]
        _ah_sphere_filter = st.selectbox(
            "Фильтр по сфере", options=_AH_SPHERES,
            format_func=lambda x: "Все сферы" if x == "" else x,
            key="ah_sphere",
        )

        _ah_filtered = _ah_search(
            _ah_all,
            query=_ah_q,
            match_type=_ah_match,
            scope=_ah_text_scope,
            date_from=str(_ah_date_from) if _ah_date_from else None,
            date_to=str(_ah_date_to)     if _ah_date_to   else None,
            sphere=_ah_sphere_filter,
        )

        _total_found = len(_ah_filtered)
        if _ah_q or _ah_date_from or _ah_date_to or _ah_sphere_filter:
            st.caption(f"Найдено: **{_total_found}** из {_ah_st['total']}")
        else:
            st.caption(f"Всего записей: **{_total_found}**")

        st.divider()

        if not _ah_filtered:
            st.info("Записей не найдено."
                    if (_ah_q or _ah_date_from or _ah_date_to or _ah_sphere_filter)
                    else "История пуста — ответы сохраняются сюда автоматически.")
        else:
            # ВАЖНО: 5, а не 20.
            # st.expander(expanded=False) в Streamlit НЕ ленивый — содержимое
            # рендерится и уходит в DOM всегда, свёрнутость это только CSS.
            # Каждая запись здесь тяжёлая: полный текст ответа с markdown-
            # таблицами + до нескольких десятков источников + уточнения с их
            # полными ответами. При 20 записях браузер получал разом сотни
            # килобайт разметки, KaTeX проходил по всем формулам (сотни
            # предупреждений в консоли), главный поток надолго блокировался,
            # переставал обрабатывать сообщения WebSocket — включая финальное
            # "скрипт завершён". Из-за этого индикатор навсегда оставался в
            # состоянии выполнения, а страница не реагировала на клики, хотя
            # сервер был уже полностью свободен (проверено py-spy: ни одного
            # активного потока; /_stcore/health отвечал ok).
            _AH_PAGE_SIZE   = 5
            _ah_total_pages = max(1, (_total_found + _AH_PAGE_SIZE - 1) // _AH_PAGE_SIZE)
            _ah_page = st.number_input("Страница", min_value=1,
                                       max_value=_ah_total_pages, value=1, key="ah_page")
            _ah_start     = (_ah_page - 1) * _AH_PAGE_SIZE
            _ah_page_recs = _ah_filtered[_ah_start: _ah_start + _AH_PAGE_SIZE]
            st.caption(f"Страница {_ah_page} из {_ah_total_pages}  ·  записи "
                       f"{_ah_start+1}–{min(_ah_start+_AH_PAGE_SIZE, _total_found)}")
            st.divider()

            _FS2 = "font-size: 0.875rem;"

            for _ahi, _rec in enumerate(_ah_page_recs):
                _rec_clars   = _rec.get("clarifications", [])
                _rec_spheres = "  ·  ".join(_rec.get("spheres", [])) or "все сферы"
                _ts_display  = _rec.get("ts", "")[:16].replace("T", " ")
                _card_lbl = (f"{_ts_display}  ·  "
                             f"{_rec['query'][:70]}{'...' if len(_rec['query']) > 70 else ''}")
                if _rec_clars:
                    _card_lbl += f"  [{len(_rec_clars)} уточн.]"

                _snippet  = _rec.get("_snippet", "")
                _is_mine  = _rec.get("user_id") == _user_id

                with st.expander(_card_lbl, expanded=False):
                    _ah_meta = [f"Модель: {_rec.get('model', '—')}", _rec_spheres]
                    if _rec.get("from_faq"):
                        _ah_meta.append("из FAQ")
                    # При scope org/all показываем автора — иначе непонятно, чьё
                    if _ah_scope != "user":
                        _ah_meta.append(f"автор: {_rec.get('user_id', '—')}")
                    st.caption("  ·  ".join(_ah_meta))

                    if _ah_q and _snippet:
                        st.markdown(
                            f'<div style="{_FS2} color: #888; background: #f8f8f8; '
                            f'padding: 4px 8px; border-radius: 4px; margin-bottom: 6px;">'
                            f'…{_snippet}…</div>',
                            unsafe_allow_html=True,
                        )
                    st.divider()

                    st.markdown(
                        f'<div style="{_FS2}"><p><strong>Вопрос:</strong> {_rec["query"]}</p></div>',
                        unsafe_allow_html=True,
                    )

                    # ── Ответ: превью по умолчанию, полный текст по клику ──────
                    # Раньше здесь безусловно рендерился ПОЛНЫЙ текст ответа для
                    # каждой записи страницы. Поскольку expander не ленивый, это
                    # означало сотни килобайт разметки в DOM при одном открытии
                    # вкладки — браузер вставал колом (см. комментарий у
                    # _AH_PAGE_SIZE). Теперь по умолчанию показываем короткое
                    # превью как обычный текст (без unsafe_allow_html — незачем
                    # разбирать HTML/формулы в куске, который всё равно обрезан),
                    # а полный ответ разворачивается только для той записи, по
                    # которой пользователь явно нажал кнопку.
                    _ah_full_key = f"ah_full_{_rec['id']}"
                    _ah_answer   = _rec.get("answer", "") or ""
                    _AH_PREVIEW_LEN = 600

                    if st.session_state.get(_ah_full_key):
                        st.markdown(f'<div style="{_FS2}">{_ah_answer}</div>',
                                    unsafe_allow_html=True)
                        if st.button("Свернуть ответ",
                                     key=f"ah_hide_{_rec['id']}_{_ahi}_{_ah_page}"):
                            st.session_state[_ah_full_key] = False
                            st.rerun()
                    else:
                        _ah_preview = _ah_answer[:_AH_PREVIEW_LEN]
                        st.text(_ah_preview + ("…" if len(_ah_answer) > _AH_PREVIEW_LEN else ""))
                        if len(_ah_answer) > _AH_PREVIEW_LEN:
                            if st.button("Показать ответ полностью",
                                         key=f"ah_show_{_rec['id']}_{_ahi}_{_ah_page}"):
                                st.session_state[_ah_full_key] = True
                                st.rerun()

                    if _rec.get("sources"):
                        # Источников бывает несколько десятков на запись, и каждый
                        # это отдельный markdown-элемент. Вложенный expander их не
                        # экономит — он тоже не ленивый. Поэтому список источников
                        # выводим только когда запись развёрнута кнопкой выше.
                        if st.session_state.get(_ah_full_key):
                            with st.expander(f"Источники ({len(_rec['sources'])})",
                                             expanded=False):
                                for _si, _src in enumerate(_rec["sources"], 1):
                                    _km = " · 📁 локальная база" \
                                          if _src.get("source_kind") == "local" else ""
                                    st.markdown(
                                        f'<div style="{_FS2}"><b>{_si}. {_src.get("file","?")}</b>'
                                        + (f' (стр. {_src["page"]})' if _src.get("page") else "")
                                        + _km + "</div>",
                                        unsafe_allow_html=True,
                                    )
                                    if _src.get("sphere"):
                                        st.caption("Сферы: " + _src["sphere"])
                        else:
                            st.caption(f"Источников: {len(_rec['sources'])} "
                                       f"(показать — раскройте ответ полностью)")

                    if _rec_clars:
                        st.divider()
                        # Ответы уточнений — такие же тяжёлые, как основной, и их
                        # может быть несколько на запись. Показываем их полностью
                        # только когда запись развёрнута кнопкой выше; иначе —
                        # лишь текст самого уточняющего вопроса.
                        _ah_show_full = bool(st.session_state.get(_ah_full_key))
                        for _ci, _clar in enumerate(_rec_clars, 1):
                            st.markdown(
                                f'<div style="{_FS2} color:#555;">'
                                f'<strong>Уточнение №{_ci}:</strong> {_clar["query"]}</div>',
                                unsafe_allow_html=True,
                            )
                            if _ah_show_full:
                                st.markdown(f'<div style="{_FS2}">{_clar["answer"]}</div>',
                                            unsafe_allow_html=True)
                            else:
                                _clar_ans = _clar.get("answer", "") or ""
                                st.text(_clar_ans[:300] + ("…" if len(_clar_ans) > 300 else ""))
                            if _ci < len(_rec_clars):
                                st.divider()

                    # Удалять можно только свои записи: delete_entry работает
                    # с файлом текущего пользователя, для чужих он бесполезен.
                    st.divider()
                    if _is_mine:
                        if st.button("Удалить", key=f"ah_del_{_rec['id']}_{_ahi}_{_ah_page}",
                                     use_container_width=False):
                            _ah_delete(_rec["id"], org_id=_user_org_id, user_id=_user_id)
                            st.rerun()
                    else:
                        st.caption("Чужая запись — удаление недоступно.")