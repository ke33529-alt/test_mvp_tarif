# streamlit_pages/tasks_page.py
"""
Раздел «Задачи» — личные текстовые заметки пользователя.

Пользователь ведёт короткие заметки (до 500 символов) о том, что нужно
запросить в системе. У каждой задачи есть статус (Сделать / В работе /
Выполнено), приоритет (Низкий / Средний / Высокий, выделен цветом) и
необязательный срок исполнения с подсветкой по остатку рабочих дней:
    > 5 р.д. — зелёный, 2–5 — жёлтый, 0–1 — оранжевый, просрочка — красный.
Видимость и удаление зависят от роли (см. core/tasks.py).

Завершение задачи:
  Перевод статуса в «Выполнено» открывает модальное окно (core/tasks.complete_task):
  текст исполнения (до 2000 символов) + необязательная привязка к записи из
  истории Советчика / Протоколов / Прогнозиста / Заявок / Сканера документов
  (см. core/entity_picker.py). Статус фактически меняется только после
  подтверждения в модалке; отмена возвращает выбор статуса как было.

Правка задачи (кнопка «Правка»):
  В форме правятся ВСЕ атрибуты своей задачи — текст, приоритет, статус,
  срок исполнения, а для статуса «Выполнено» ещё дата выполнения, резолюция
  и привязанные записи. Сохранение — одним вызовом core/tasks.update_task.
  Перевод в «Выполнено» из формы правки модалку не открывает: поля итога
  выполнения показываются прямо в форме.
"""

import html
from datetime import date

import streamlit as st

from core.auth import get_current_user
from core import tasks as tasks_core
from core import entity_picker


def _audit(user: dict, event: str) -> None:
    """Аудит-лог события (не критично при сбое)."""
    try:
        from core.audit import log_event
        log_event(
            org_id=user.get("org_id", ""),
            user_id=user.get("user_id", ""),
            role=user.get("role", ""),
            event=event,
            module="tasks",
        )
    except Exception:
        pass


try:
    from core.usage_tracker import log_event as _log_usage
except Exception:
    def _log_usage(*a, **kw): pass  # noqa: E731


def _micro_label(text: str) -> str:
    """Компактная подпись категории перед чипом (мелкий серый капс)."""
    return (f'<span style="font-size:0.62rem;color:#9aa5b1;text-transform:uppercase;'
            f'letter-spacing:0.04em;margin-right:4px;vertical-align:middle">{text}</span>')


def _priority_chip(priority: str) -> str:
    color = tasks_core.PRIORITY_COLORS.get(priority, "#999")
    label = tasks_core.PRIORITY_LABELS.get(priority, priority)
    return (_micro_label("Приоритет")
            + f'<span style="background:{color};color:#fff;padding:2px 9px;'
              f'border-radius:10px;font-size:0.72rem;font-weight:600">{label}</span>')


def _status_chip(status: str) -> str:
    label = tasks_core.STATUS_LABELS.get(status, status)
    bg = "#eef2f6"
    if status == tasks_core.STATUS_DONE:
        bg = "#e3f3e8"
    elif status == tasks_core.STATUS_IN_PROGRESS:
        bg = "#e8f0f6"
    return (_micro_label("Статус")
            + f'<span style="background:{bg};color:#33475a;padding:2px 9px;'
              f'border-radius:10px;font-size:0.72rem">{label}</span>')


def _deadline_chip(due_date: str) -> str:
    tier = tasks_core.deadline_tier(due_date)
    if tier == tasks_core.DEADLINE_NONE:
        return ""
    bg = tasks_core.DEADLINE_COLORS.get(tier, "#999")
    fg = tasks_core.DEADLINE_TEXT_COLORS.get(tier, "#fff")
    label = tasks_core.deadline_label(due_date)
    return (_micro_label("Срок")
            + f'<span style="background:{bg};color:{fg};padding:2px 9px;'
              f'border-radius:10px;font-size:0.72rem;font-weight:600">{label}</span>')


def _completed_chip(completed_at: str) -> str:
    """
    Бейдж даты выполнения — заменяет собой срок исполнения на карточке
    выполненной задачи. Дата проставляется автоматически в момент перевода
    задачи в статус «Выполнено» (core/tasks.update_task), вручную не задаётся.
    """
    if not completed_at:
        return ""
    label = tasks_core.format_completed_ru(completed_at)
    return (_micro_label("Выполнено")
            + f'<span style="background:#e3f3e8;color:#1e7a45;padding:2px 9px;'
              f'border-radius:10px;font-size:0.72rem;font-weight:600">{label}</span>')


def _ref_chip(ref: dict) -> str:
    """Статичная подпись привязанной сущности — для источников без перехода."""
    if not ref or not ref.get("id"):
        return ""
    src_label = entity_picker.SOURCE_LABELS.get(ref.get("source", ""), ref.get("source", ""))
    label = ref.get("label", "")
    text = f"{src_label}: {label}" if label else src_label
    return (f'<div style="margin-top:6px;font-size:0.78rem;color:#5a6a7a">'
            f'🔗 Связано с записью — {html.escape(text)}</div>')


def _ref_link_text(ref: dict) -> str:
    """Текст ссылки-перехода к записи (источники с реализованным переходом)."""
    src_label = entity_picker.SOURCE_LABELS.get(ref.get("source", ""), ref.get("source", ""))
    label = ref.get("label", "")
    return f"🔗 {src_label}: {label} →" if label else f"🔗 {src_label} →"


def _clear_edit_state(tid: str, final_status: str) -> None:
    """
    Выход из режима «Правка» (сохранение или отмена): чистим всё состояние
    формы и синхронизируем виджеты карточки с итоговым статусом.

    Селекторы приоритета/статуса на карточке (pr_/st_) не рисуются, пока
    открыта правка, но трекер _task_status_seen_ — обычный ключ, он хранит
    статус ДО правки. Если его не выставить в итоговый статус, при первой
    отрисовке карточки виджет (новый статус) отличался бы от трекера (старый),
    и это сочлось бы новым выбором пользователя — например, повторно открылась
    бы модалка завершения для уже выполненной через правку задачи.
    """
    st.session_state.pop("_task_edit_id", None)
    for k in (
        f"_task_edit_text_{tid}", f"_task_edit_note_{tid}", f"_task_edit_refs_{tid}",
        f"_task_edit_prio_{tid}", f"_task_edit_status_{tid}", f"_task_edit_done_{tid}",
        f"_task_nodue_{tid}", f"_task_due_{tid}", f"_te_src_{tid}",
        f"pr_{tid}", f"st_{tid}",
    ):
        st.session_state.pop(k, None)
    st.session_state[f"_task_status_seen_{tid}"] = final_status


@st.dialog("Удаление задачи")
def _show_delete_dialog(user: dict, pending: dict):
    """Модальное подтверждение удаления задачи."""
    st.markdown("Удалить эту задачу?")
    _txt = (pending.get("text", "") or "")[:200]
    if _txt:
        st.caption(_txt)
    st.caption("Действие необратимо.")

    c_yes, c_no = st.columns(2)
    if c_yes.button("Удалить", type="primary", use_container_width=True, key="_task_del_yes"):
        ok = tasks_core.delete_task(user, pending["id"], pending["owner_id"], pending["segment"])
        st.session_state.pop("_task_del", None)
        if ok:
            _audit(user, "task_delete")
            _log_usage("tasks", "task_deleted", meta={"task_id": pending["id"]})
        st.rerun()
    if c_no.button("Отмена", use_container_width=True, key="_task_del_no"):
        st.session_state.pop("_task_del", None)
        st.rerun()


@st.dialog("Завершение задачи")
def _show_complete_dialog(user: dict, pending: dict):
    """
    Модалка, открываемая при переводе задачи в статус «Выполнено».

    Флаг _task_complete_pending, открывающий модалку, НЕ гасится сразу
    (см. show_tasks) — в отличие от одноразовых диалогов вроде помощи в
    app.py или выбора задачи в advisor_page.py. Причина: внутри этой модалки
    есть собственные кнопки («Добавить ссылку», удаление ссылки крестиком),
    которые тоже вызывают st.rerun() для перерисовки списка ссылок. Если бы
    флаг гасился при первом открытии, эти внутренние reruns не находили бы
    его и модалка закрывалась бы сама после первого же добавления ссылки.
    Флаг снимается только явно — кнопками «Завершить задачу» и «Отмена».
    «Отмена» также откатывает виджет статуса на карточке к прежнему значению.

    Ссылки на записи из истории копятся в session_state (refs_key) по одной —
    «Добавить ссылку» дописывает в список, ✕ убирает — до MAX_COMPLETION_REFS
    штук. Список коммитится в задачу одним вызовом complete_task() при
    подтверждении, независимо от того, из скольких разных модулей ссылки.
    """
    tid = pending["id"]

    st.caption("Задача:")
    st.markdown(f"<div style='color:#1a2a3a'>{html.escape(pending.get('text', ''))}</div>",
                unsafe_allow_html=True)
    st.divider()

    note_key = f"_task_complete_note_{tid}"
    note = st.text_area(
        "Текст исполнения",
        key=note_key,
        height=140,
        max_chars=tasks_core.COMPLETION_NOTE_MAX_CHARS,
        placeholder="Что сделано, какой результат получен...",
    )
    st.caption(f"{len(st.session_state.get(note_key, ''))}/{tasks_core.COMPLETION_NOTE_MAX_CHARS}")

    refs_key = f"_task_complete_refs_{tid}"
    _staged: list = st.session_state.setdefault(refs_key, [])

    st.markdown(f"**Привязанные записи** _(до {tasks_core.MAX_COMPLETION_REFS})_")
    if _staged:
        for i, r in enumerate(_staged):
            rc1, rc2 = st.columns([6, 1])
            with rc1:
                _lbl = entity_picker.SOURCE_LABELS.get(r.get("source", ""), r.get("source", ""))
                st.caption(f"🔗 {_lbl}: {r.get('label', '')}")
            with rc2:
                if st.button("✕", key=f"_tc_rmref_{tid}_{i}", use_container_width=True):
                    st.session_state[refs_key].pop(i)
                    st.rerun()
    else:
        st.caption("Записи не привязаны.")

    if len(_staged) < tasks_core.MAX_COMPLETION_REFS:
        st.markdown("**Добавить запись из истории** _(необязательно)_")
        src_options = ["—"] + entity_picker.SOURCE_ORDER
        src = st.selectbox(
            "Источник",
            options=src_options,
            format_func=lambda s: "Не выбирать" if s == "—" else entity_picker.SOURCE_LABELS.get(s, s),
            key=f"_tc_src_{tid}",
            label_visibility="collapsed",
        )
        if src != "—":
            entries = entity_picker.list_recent(src, user, limit=30)
            if not entries:
                st.caption("Записей не найдено.")
            else:
                # Ключ селектбокса записи включает источник — при переключении
                # источника Streamlit заводит НОВЫЙ виджет с чистым состоянием
                # (options всё равно разные для каждого источника), поэтому
                # смена источника сама по себе сбрасывает выбор на пустое.
                ent_key = f"_tc_ent_{tid}_{src}"
                ent_options = [""] + [e["id"] for e in entries]
                ent_labels = {e["id"]: e["label"] for e in entries}
                ac1, ac2 = st.columns([4, 2])
                with ac1:
                    chosen_id = st.selectbox(
                        "Запись",
                        options=ent_options,
                        format_func=lambda eid: "— не выбрано —" if eid == "" else ent_labels.get(eid, eid),
                        key=ent_key,
                        label_visibility="collapsed",
                    )
                with ac2:
                    if st.button("Добавить ссылку", key=f"_tc_addref_{tid}", use_container_width=True,
                                 disabled=not chosen_id):
                        _dup = any(r.get("source") == src and r.get("id") == chosen_id for r in _staged)
                        if _dup:
                            st.toast("Эта запись уже добавлена", icon="ℹ️")
                        else:
                            _new_ref = {"source": src, "id": chosen_id, "label": ent_labels.get(chosen_id, "")}
                            st.session_state[refs_key].append(_new_ref)
                            # Явно сбрасываем выбор «Запись», чтобы для следующего
                            # добавления сразу было видно, что поле снова пустое —
                            # без этого казалось, что кнопка «ничего не делает».
                            st.session_state.pop(ent_key, None)
                            st.toast(f"Добавлено: {_new_ref['label'][:60]}", icon="✅")
                        st.rerun()
    else:
        st.caption(f"Достигнут лимит в {tasks_core.MAX_COMPLETION_REFS} записей.")

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Завершить задачу", type="primary", use_container_width=True, key=f"_tc_ok_{tid}"):
            tasks_core.complete_task(user, tid, note=note, refs=list(st.session_state.get(refs_key, [])))
            _audit(user, "task_complete")
            _log_usage("tasks", "task_completed", meta={
                "task_id":  tid,
                "has_note": bool((note or "").strip()),
                "refs":     len(st.session_state.get(refs_key, [])),
            })
            st.session_state["_task_complete_pending"] = None
            st.session_state.pop(note_key, None)
            st.session_state.pop(refs_key, None)
            st.rerun()
    with c2:
        if st.button("Отмена", use_container_width=True, key=f"_tc_cancel_{tid}"):
            # Возвращаем виджет статуса на карточке к прежнему значению —
            # иначе selectbox продолжит показывать «Выполнено» без факта завершения.
            # "_task_status_seen_{tid}" синхронизируем тем же значением — это
            # трекер «последнего обработанного статуса» (см. show_tasks), без
            # него следующий выбор «Выполнено» не считался бы НОВЫМ изменением.
            st.session_state[f"st_{tid}"] = pending.get("prev_status", tasks_core.DEFAULT_STATUS)
            st.session_state[f"_task_status_seen_{tid}"] = pending.get("prev_status", tasks_core.DEFAULT_STATUS)
            st.session_state["_task_complete_pending"] = None
            st.session_state.pop(note_key, None)
            st.session_state.pop(refs_key, None)
            st.rerun()


def show_tasks():
    user = get_current_user()
    if not user:
        st.warning("Требуется авторизация.")
        return

    # Если пользователь попал в «Задачи» НЕ через кнопку «Назад к задачам»
    # в Советчике/Сканере (например, кликнул «Задачи» в сайдбаре, минуя её),
    # флаг перехода к записи мог остаться выставленным. Без этой чистки
    # следующий заход в раздел снова показал бы старую запись вместо
    # обычного интерфейса — приводим состояние к ожидаемому при явном
    # визите в «Задачи».
    st.session_state.pop("_adv_jump_entry_id", None)
    st.session_state.pop("_scan_jump_doc_id", None)

    role = user.get("role", "")
    is_super     = role == tasks_core.ROLE_SUPERADMIN
    is_seg_admin = role == tasks_core.ROLE_SEGMENT_ADMIN
    my_uid       = user.get("user_id", "")

    # Кнопка-переход к связанной записи. Streamlit не умеет запускать
    # Python-код по клику на <a>, только через виджет (st.button) — здесь
    # переопределяем размер шрифта и межблочный отступ через стабильный
    # класс-обёртку Streamlit (.element-container), а не только через
    # свой .task-ref-link — так вернее долетает до реального DOM-узла.
    st.markdown("""
    <style>
    div.element-container:has(.task-ref-link) {
        margin-top: -8px !important;
        margin-bottom: -8px !important;
    }
    .task-ref-link .stButton > button,
    .task-ref-link button[kind="secondary"],
    .task-ref-link button[kind="primary"] {
        background: none !important;
        border: none !important;
        box-shadow: none !important;
        padding: 2px 4px !important;
        margin-top: 2px !important;
        color: #1B5C74 !important;
        font-size: 0.68rem !important;
        font-weight: 500 !important;
        text-decoration: underline !important;
        width: auto !important;
        min-height: 0 !important;
        height: auto !important;
    }
    .task-ref-link .stButton > button:hover,
    .task-ref-link button[kind="secondary"]:hover,
    .task-ref-link button[kind="primary"]:hover {
        color: #063971 !important;
        background: none !important;
    }
    </style>
    """, unsafe_allow_html=True)

    st.markdown("### Задачи")
    st.caption(
        "Ведите короткие заметки — что нужно запросить в системе, чтобы не забыть. "
        f"До {tasks_core.MAX_CHARS} символов. Срок подсвечивается по остатку рабочих дней. "
        "Быстрый выбор задачи доступен в Советчике."
    )

    # ── Добавление новой задачи ──────────────────────────────────────────────
    _NEW = "_task_new_text"
    if st.session_state.pop("_task_clear_new", False):
        st.session_state[_NEW] = ""
        st.session_state.pop("_task_new_due", None)
        st.session_state.pop("_task_new_prio", None)

    new_text = st.text_area(
        "Новая задача",
        key=_NEW,
        height=90,
        max_chars=tasks_core.MAX_CHARS,
        placeholder="Например: уточнить в Советчике порядок учёта арендной платы в НВВ",
    )
    st.caption(f"{len(st.session_state.get(_NEW, ''))}/{tasks_core.MAX_CHARS}")

    ac1, ac2 = st.columns(2)
    with ac1:
        _new_prio = st.selectbox(
            "Приоритет",
            options=tasks_core.PRIORITY_CHOICES,
            index=tasks_core.PRIORITY_CHOICES.index(tasks_core.DEFAULT_PRIORITY),
            format_func=lambda p: tasks_core.PRIORITY_LABELS.get(p, p),
            key="_task_new_prio",
        )
    with ac2:
        _new_due = st.date_input(
            "Срок исполнения (необязательно)",
            value=None,
            format="DD.MM.YYYY",
            key="_task_new_due",
        )

    if st.button("Добавить задачу", type="primary", use_container_width=True):
        if (new_text or "").strip():
            tasks_core.add_task(
                user, new_text,
                priority=_new_prio,
                due_date=(_new_due.isoformat() if _new_due else ""),
            )
            _audit(user, "task_add")
            _log_usage("tasks", "task_created", meta={
                "priority": _new_prio,
                "has_due":  bool(_new_due),
            })
            st.session_state["_task_clear_new"] = True
            st.rerun()
        else:
            st.warning("Введите текст задачи.")

    st.divider()

    # ── Фильтры ───────────────────────────────────────────────────────────────
    _search_q = st.text_input(
        "Поиск по задачам",
        placeholder="Поиск по тексту задачи и резолюции...",
        key="_task_search_q",
    )

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        _status_opts = ["all"] + tasks_core.STATUS_ORDER
        _status_f = st.selectbox(
            "Статус",
            options=_status_opts,
            format_func=lambda s: "Все статусы" if s == "all"
                                  else tasks_core.STATUS_LABELS.get(s, s),
            key="_task_status_filter",
        )
    with fc2:
        _prio_opts = ["all"] + tasks_core.PRIORITY_CHOICES
        _prio_f = st.selectbox(
            "Приоритет",
            options=_prio_opts,
            format_func=lambda p: "Все приоритеты" if p == "all"
                                  else tasks_core.PRIORITY_LABELS.get(p, p),
            key="_task_prio_filter",
        )
    with fc3:
        segment_filter = None
        if is_super:
            segs = tasks_core.list_segments()
            _seg_opts = [""] + sorted(segs.keys(), key=lambda k: (segs.get(k, k) or k).lower())
            _seg_choice = st.selectbox(
                "Сегмент",
                options=_seg_opts,
                format_func=lambda v: "Все сегменты" if v == "" else segs.get(v, v),
                key="_task_seg_filter",
            )
            segment_filter = _seg_choice or None

    # ── Загрузка + фильтрация + сортировка ────────────────────────────────────
    items = tasks_core.list_tasks(user, segment_filter=segment_filter)
    if _status_f != "all":
        items = [t for t in items if t.get("status") == _status_f]
    if _prio_f != "all":
        items = [t for t in items if t.get("priority") == _prio_f]
    if _search_q.strip():
        _q = _search_q.strip().lower()
        items = [
            t for t in items
            if _q in (t.get("text", "") or "").lower()
            or _q in (t.get("completion_note", "") or "").lower()
        ]
    items = tasks_core.sort_for_display(items)

    seg_names = tasks_core.list_segments() if (is_super or is_seg_admin) else {}

    # ── Модалка завершения задачи (переход в статус «Выполнено») ─────────────
    # ВАЖНО: флаг НЕ сбрасывается здесь. Внутри модалки кнопка «Добавить
    # ссылку» тоже вызывает st.rerun() (нужно перерисовать список ссылок);
    # если бы флаг гасился сразу при первом открытии, этот повторный прогон
    # находил бы его уже пустым — код, открывающий модалку, не срабатывал бы
    # заново, и модалка закрывалась бы сама, теряя всё кроме первой ссылки.
    # Флаг снимается только явными действиями внутри модалки — «Завершить
    # задачу» и «Отмена» (см. _show_complete_dialog).
    _complete_pending = st.session_state.get("_task_complete_pending")
    if _complete_pending:
        _show_complete_dialog(user, _complete_pending)

    # ── Подтверждение удаления (модалка) ──────────────────────────────────────
    pend = st.session_state.get("_task_del")
    if pend:
        _show_delete_dialog(user, pend)

    if not items:
        st.info("Задач по текущему фильтру нет.")
        return

    _TASKS_PAGE_SIZE = 20
    _total_found      = len(items)
    _total_pages      = max(1, (_total_found + _TASKS_PAGE_SIZE - 1) // _TASKS_PAGE_SIZE)

    # Если фильтр/поиск сократили список, сохранённый номер страницы может
    # оказаться больше нового максимума — st.number_input упадёт с ошибкой,
    # если значение в session_state вне [min_value, max_value].
    if st.session_state.get("_task_page", 1) > _total_pages:
        st.session_state["_task_page"] = _total_pages

    _pcap1, _pcap2 = st.columns([3, 1])
    with _pcap1:
        st.caption(f"Показано задач: {_total_found}")
    with _pcap2:
        _page = st.number_input(
            "Страница", min_value=1, max_value=_total_pages, value=1,
            key="_task_page", label_visibility="collapsed",
        )
    if _total_pages > 1:
        st.caption(f"Страница {_page} из {_total_pages}")

    _page_start = (_page - 1) * _TASKS_PAGE_SIZE
    _page_items = items[_page_start: _page_start + _TASKS_PAGE_SIZE]

    # ── Список задач ──────────────────────────────────────────────────────────
    for t in _page_items:
        tid      = t.get("id")
        owner_id = t.get("owner_id", "")
        seg      = t.get("segment", "")
        status       = t.get("status", tasks_core.DEFAULT_STATUS)
        priority     = t.get("priority", tasks_core.DEFAULT_PRIORITY)
        due_date     = t.get("due_date", "")
        completed_at = t.get("completed_at", "")

        with st.container(border=True):
            # Левая группа: приоритет + статус + мета (включая автора задачи —
            # ФИО подписано явным префиксом «Автор:», а не голым именем).
            # Правая: срок исполнения — либо, если задача выполнена, дата
            # выполнения вместо него (для «Выполнено» срок неактуален).
            left_chips = _priority_chip(priority) + "&nbsp;&nbsp;&nbsp;" + _status_chip(status)

            meta_bits = []
            if is_super or is_seg_admin:
                author = t.get("owner_name") or owner_id or "—"
                if owner_id == my_uid:
                    author += " (вы)"
                meta_bits.append(f"Автор: {author}")
            if is_super:
                meta_bits.append(seg_names.get(seg, seg) or "без сегмента")
            upd = (t.get("updated_at") or "")[:16].replace("T", " ")
            if upd:
                meta_bits.append(upd)
            if meta_bits:
                left_chips += ('&nbsp;&nbsp;<span style="font-size:0.72rem;color:#8a96a3">'
                               + " · ".join(html.escape(m) for m in meta_bits) + "</span>")

            if status == tasks_core.STATUS_DONE:
                right_chip = _completed_chip(completed_at)
            else:
                right_chip = _deadline_chip(due_date)

            if right_chip:
                st.markdown(
                    '<div style="display:flex;justify-content:space-between;'
                    'align-items:flex-start;gap:8px;flex-wrap:wrap">'
                    f'<div>{left_chips}</div>'
                    f'<div style="flex-shrink:0">{right_chip}</div>'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                # Нет бейджа справа (нет срока / задача не выполнена) — не
                # резервируем под него пустое место, просто левая группа.
                st.markdown(left_chips, unsafe_allow_html=True)

            editing = st.session_state.get("_task_edit_id") == tid

            if editing:
                # ── Режим «Правка»: ВСЕ атрибуты задачи ──────────────────────
                # Текст, приоритет, статус, срок, а для «Выполнено» — дата
                # выполнения, резолюция и ссылки. Всё сохраняется ОДНИМ вызовом
                # update_task (атомарная запись). Перевод в «Выполнено» отсюда
                # НЕ открывает модалку завершения: поля резолюции и ссылок
                # появляются прямо в форме, как только выбран статус «Выполнено».
                ekey = f"_task_edit_text_{tid}"
                if ekey not in st.session_state:
                    st.session_state[ekey] = t.get("text", "")
                st.text_area(
                    "Редактирование задачи",
                    key=ekey,
                    height=90,
                    max_chars=tasks_core.MAX_CHARS,
                    label_visibility="collapsed",
                )
                st.caption(f"{len(st.session_state.get(ekey, ''))}/{tasks_core.MAX_CHARS}")

                # Приоритет + статус
                _eprio_key = f"_task_edit_prio_{tid}"
                _estat_key = f"_task_edit_status_{tid}"
                if _eprio_key not in st.session_state:
                    st.session_state[_eprio_key] = (priority if priority in tasks_core.PRIORITY_CHOICES
                                                    else tasks_core.DEFAULT_PRIORITY)
                if _estat_key not in st.session_state:
                    st.session_state[_estat_key] = (status if status in tasks_core.STATUS_ORDER
                                                    else tasks_core.DEFAULT_STATUS)
                epc1, epc2 = st.columns(2)
                with epc1:
                    _edit_prio = st.selectbox(
                        "Приоритет",
                        options=tasks_core.PRIORITY_CHOICES,
                        format_func=lambda p: tasks_core.PRIORITY_LABELS.get(p, p),
                        key=_eprio_key,
                    )
                with epc2:
                    _edit_status = st.selectbox(
                        "Статус",
                        options=tasks_core.STATUS_ORDER,
                        format_func=lambda s: tasks_core.STATUS_LABELS.get(s, s),
                        key=_estat_key,
                    )

                _edit_is_done = _edit_status == tasks_core.STATUS_DONE

                # Предупреждение: уход из «Выполнено» стирает итог выполнения
                if (status == tasks_core.STATUS_DONE and not _edit_is_done
                        and (t.get("completion_note") or t.get("completion_refs"))):
                    st.warning("При смене статуса дата выполнения, резолюция и привязанные "
                               "записи будут удалены.")

                _nkey = f"_task_edit_note_{tid}"
                _erefs_key = f"_task_edit_refs_{tid}"
                _edone_key = f"_task_edit_done_{tid}"
                if _edit_is_done:
                    # Дата выполнения: по умолчанию — текущая (для уже выполненной
                    # задачи) или сегодня (если задачу переводят в «Выполнено»
                    # прямо здесь). Будущую дату выбрать нельзя.
                    _cur_done = None
                    if completed_at:
                        try:
                            _cur_done = date.fromisoformat(completed_at[:10])
                        except Exception:
                            _cur_done = None
                    _cur_done = min(_cur_done or date.today(), date.today())
                    st.date_input(
                        "Дата выполнения",
                        value=_cur_done,
                        format="DD.MM.YYYY",
                        max_value=date.today(),
                        key=_edone_key,
                    )

                    if _nkey not in st.session_state:
                        st.session_state[_nkey] = t.get("completion_note", "")
                    st.markdown("**Резолюция**")
                    st.text_area(
                        "Резолюция",
                        key=_nkey,
                        height=110,
                        max_chars=tasks_core.COMPLETION_NOTE_MAX_CHARS,
                        label_visibility="collapsed",
                        placeholder="Что сделано, какой результат получен...",
                    )
                    st.caption(f"{len(st.session_state.get(_nkey, ''))}/{tasks_core.COMPLETION_NOTE_MAX_CHARS}")

                    # Ссылки на сущности системы — та же логика накопления,
                    # что и в модалке завершения (_show_complete_dialog):
                    # заготовка стартует с текущих ссылок задачи, «Добавить
                    # ссылку» дописывает, ✕ убирает. На «Сохранить» список
                    # ЦЕЛИКОМ заменяет старый.
                    st.markdown("**Ссылки на записи системы**")
                    if _erefs_key not in st.session_state:
                        st.session_state[_erefs_key] = list(t.get("completion_refs", []))
                    _estaged = st.session_state[_erefs_key]

                    if _estaged:
                        for _ei, _eref in enumerate(_estaged):
                            erc1, erc2 = st.columns([6, 1])
                            with erc1:
                                _elbl = entity_picker.SOURCE_LABELS.get(
                                    _eref.get("source", ""), _eref.get("source", "")
                                )
                                st.caption(f"🔗 {_elbl}: {_eref.get('label', '')}")
                            with erc2:
                                if st.button("✕", key=f"_te_rmref_{tid}_{_ei}", use_container_width=True):
                                    st.session_state[_erefs_key].pop(_ei)
                                    st.rerun()
                    else:
                        st.caption("Ссылок нет.")

                    if len(_estaged) < tasks_core.MAX_COMPLETION_REFS:
                        _esrc_options = ["—"] + entity_picker.SOURCE_ORDER
                        _esrc = st.selectbox(
                            "Источник",
                            options=_esrc_options,
                            format_func=lambda s: "Не выбирать" if s == "—"
                                                  else entity_picker.SOURCE_LABELS.get(s, s),
                            key=f"_te_src_{tid}",
                            label_visibility="collapsed",
                        )
                        if _esrc != "—":
                            _eentries = entity_picker.list_recent(_esrc, user, limit=30)
                            if not _eentries:
                                st.caption("Записей не найдено.")
                            else:
                                # Ключ поля «Запись» включает источник — смена
                                # источника сама даёт чистый выбор (см. тот же
                                # приём в _show_complete_dialog).
                                _eent_key = f"_te_ent_{tid}_{_esrc}"
                                _eent_options = [""] + [e["id"] for e in _eentries]
                                _eent_labels = {e["id"]: e["label"] for e in _eentries}
                                eac1, eac2 = st.columns([4, 2])
                                with eac1:
                                    _echosen = st.selectbox(
                                        "Запись",
                                        options=_eent_options,
                                        format_func=lambda eid: "— не выбрано —" if eid == ""
                                                                else _eent_labels.get(eid, eid),
                                        key=_eent_key,
                                        label_visibility="collapsed",
                                    )
                                with eac2:
                                    if st.button("Добавить ссылку", key=f"_te_addref_{tid}",
                                                 use_container_width=True, disabled=not _echosen):
                                        _edup = any(
                                            r.get("source") == _esrc and r.get("id") == _echosen
                                            for r in _estaged
                                        )
                                        if _edup:
                                            st.toast("Эта запись уже добавлена", icon="ℹ️")
                                        else:
                                            st.session_state[_erefs_key].append({
                                                "source": _esrc, "id": _echosen,
                                                "label": _eent_labels.get(_echosen, ""),
                                            })
                                            st.session_state.pop(_eent_key, None)
                                            st.toast("Ссылка добавлена", icon="✅")
                                        st.rerun()
                    else:
                        st.caption(f"Достигнут лимит в {tasks_core.MAX_COMPLETION_REFS} записей.")

                # Срок исполнения: чекбокс «Без срока» + дата
                _cur_due_obj = None
                if due_date:
                    try:
                        _cur_due_obj = date.fromisoformat(due_date)
                    except Exception:
                        _cur_due_obj = None
                dcol1, dcol2 = st.columns([1, 2])
                with dcol1:
                    _no_due = st.checkbox("Без срока", value=(due_date == ""),
                                          key=f"_task_nodue_{tid}")
                with dcol2:
                    _edit_due = st.date_input(
                        "Срок исполнения",
                        value=_cur_due_obj,
                        format="DD.MM.YYYY",
                        key=f"_task_due_{tid}",
                        disabled=_no_due,
                    )

                ec1, ec2 = st.columns(2)
                with ec1:
                    if st.button("Сохранить", type="primary", key=f"save_{tid}", use_container_width=True):
                        if st.session_state.get(ekey, "").strip():
                            _due_str = "" if _no_due else (_edit_due.isoformat() if _edit_due else "")
                            _kw = dict(
                                text=st.session_state[ekey],
                                status=_edit_status,
                                priority=_edit_prio,
                                due_date=_due_str,
                            )
                            if _edit_is_done:
                                _done_val = st.session_state.get(_edone_key)
                                _kw["completed_at"]    = _done_val.isoformat() if _done_val else ""
                                _kw["completion_note"] = st.session_state.get(_nkey, "")
                                _kw["completion_refs"] = list(st.session_state.get(_erefs_key, []))
                            _changed = tasks_core.update_task(user, tid, **_kw)
                            if _changed:
                                _audit(user, "task_edit")
                                _log_usage("tasks", "task_edited", meta={
                                    "task_id":     tid,
                                    "status_from": status,
                                    "status_to":   _edit_status,
                                    "prio_from":   priority,
                                    "prio_to":     _edit_prio,
                                })
                            _clear_edit_state(tid, _edit_status)
                            st.rerun()
                        else:
                            st.warning("Текст не может быть пустым.")
                with ec2:
                    if st.button("Отмена", key=f"cancel_{tid}", use_container_width=True):
                        _clear_edit_state(tid, status)
                        st.rerun()
            else:
                st.markdown(
                    f"<div style='color:#1a2a3a;white-space:pre-wrap'>"
                    f"{html.escape(t.get('text', ''))}</div>",
                    unsafe_allow_html=True,
                )

                # Итог выполнения — только для завершённых задач с заметкой
                # и/или ссылками. Раскладка в две колонки: резолюция слева,
                # ссылки справа. Пустая резолюция не рисует рамку — только
                # если в ней реально есть текст.
                if status == tasks_core.STATUS_DONE:
                    _note = t.get("completion_note", "")
                    _refs = t.get("completion_refs", [])
                    if _note or _refs:
                        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
                        _col_note, _col_links = st.columns([5, 4])

                        with _col_note:
                            if _note:
                                st.markdown(
                                    f"<div style='padding:8px 10px;"
                                    f"background:#f4f8f5;border-left:3px solid #27AE60;"
                                    f"border-radius:0 6px 6px 0;font-size:0.85rem;"
                                    f"color:#1a2a3a;white-space:pre-wrap'>"
                                    f"{html.escape(_note)}</div>",
                                    unsafe_allow_html=True,
                                )

                        with _col_links:
                            if _refs:
                                # Переход реализован для источников «Советчик»
                                # и «Сканер документов»; для остальных —
                                # просто подпись. Задача может быть привязана
                                # сразу к нескольким записям из разных модулей.
                                for _ri, _ref in enumerate(_refs):
                                    _rsrc = _ref.get("source", "")
                                    if _rsrc == "advisor" and _ref.get("id"):
                                        st.markdown('<div class="task-ref-link">', unsafe_allow_html=True)
                                        if st.button(_ref_link_text(_ref), key=f"_goto_adv_{tid}_{_ri}"):
                                            st.session_state["main_choice"]        = "Советчик"
                                            st.session_state["show_landing"]       = False
                                            st.session_state["_adv_jump_entry_id"] = _ref["id"]
                                            st.rerun()
                                        st.markdown('</div>', unsafe_allow_html=True)
                                    elif _rsrc == "scanner" and _ref.get("id"):
                                        st.markdown('<div class="task-ref-link">', unsafe_allow_html=True)
                                        if st.button(_ref_link_text(_ref), key=f"_goto_scan_{tid}_{_ri}"):
                                            st.session_state["main_choice"]        = "Сканер документов"
                                            st.session_state["show_landing"]       = False
                                            st.session_state["_scan_jump_doc_id"]  = _ref["id"]
                                            st.rerun()
                                        st.markdown('</div>', unsafe_allow_html=True)
                                    else:
                                        st.markdown(_ref_chip(_ref), unsafe_allow_html=True)

                can_e = tasks_core.can_edit(user, owner_id)
                can_d = tasks_core.can_delete(user, owner_id, seg)

                if can_e:
                    # Свои задачи: быстрая смена приоритета/статуса + правка + удаление.
                    # Срок, дата выполнения, резолюция и ссылки (а также те же
                    # приоритет/статус) правятся в режиме «Правка».
                    #
                    # Внешние колонки [5, 4] — та же пропорция, что у блока
                    # резолюция/ссылки выше: ряд управления встаёт точно под
                    # ним по ширине, без лишнего пустого пространства сбоку.
                    # Подписи — сбоку от селектора, не сверху, выровнены по
                    # центру высоты виджета (~38px — стандартная высота
                    # Streamlit-селектбокса) через flex-контейнер.
                    _LBL = (
                        "<div style='display:flex;align-items:center;height:38px;"
                        "font-size:0.78rem;color:#5a6a7a;font-weight:500;'>{}</div>"
                    )
                    oc_left, oc_right = st.columns([5, 4])
                    with oc_left:
                        oc_pl, oc_pr_, oc_sl, oc_st_ = st.columns([1, 2, 1, 2])
                        with oc_pl:
                            st.markdown(_LBL.format("Приоритет"), unsafe_allow_html=True)
                        with oc_pr_:
                            _np = st.selectbox(
                                "Приоритет",
                                options=tasks_core.PRIORITY_CHOICES,
                                index=tasks_core.PRIORITY_CHOICES.index(priority)
                                      if priority in tasks_core.PRIORITY_CHOICES else 1,
                                format_func=lambda p: tasks_core.PRIORITY_LABELS.get(p, p),
                                key=f"pr_{tid}",
                                label_visibility="collapsed",
                            )
                            if _np != priority:
                                tasks_core.update_task(user, tid, priority=_np)
                                st.rerun()
                        with oc_sl:
                            st.markdown(_LBL.format("Статус"), unsafe_allow_html=True)
                        with oc_st_:
                            _seen_key = f"_task_status_seen_{tid}"
                            if _seen_key not in st.session_state:
                                st.session_state[_seen_key] = status
                            _ns = st.selectbox(
                                "Статус",
                                options=tasks_core.STATUS_ORDER,
                                index=tasks_core.STATUS_ORDER.index(status)
                                      if status in tasks_core.STATUS_ORDER else 0,
                                format_func=lambda s: tasks_core.STATUS_LABELS.get(s, s),
                                key=f"st_{tid}",
                                label_visibility="collapsed",
                            )
                            # Сравниваем НЕ с status (диск), а с "последним обработанным
                            # нами значением" (_seen_key). Раньше сравнение шло с
                            # диском напрямую + отдельное inflight-множество гасило
                            # повторные срабатывания, пока диск не догонит виджет —
                            # два независимых флага могли рассинхронизироваться (сброс
                            # одного без другого), из-за чего повторное Выполнено после
                            # возврата в работу не всегда переоткрывало модалку. Теперь
                            # один-единственный трекер: как только смена ОБРАБОТАНА
                            # (модалка открыта или update_task вызван), _seen_key сразу
                            # выставляется в новое значение — следующее реальное
                            # изменение виджета снова будет отличаться от _seen_key.
                            if _ns != st.session_state[_seen_key]:
                                st.session_state[_seen_key] = _ns
                                if _ns == tasks_core.STATUS_DONE:
                                    st.session_state["_task_complete_pending"] = {
                                        "id": tid, "owner_id": owner_id, "segment": seg,
                                        "text": t.get("text", ""), "prev_status": status,
                                    }
                                    st.rerun()
                                else:
                                    tasks_core.update_task(user, tid, status=_ns)
                                    _log_usage("tasks", "task_status_changed", meta={
                                        "task_id":  tid,
                                        "from":     status,
                                        "to":       _ns,
                                    })
                                    st.rerun()
                    with oc_right:
                        oc3, oc4 = st.columns(2)
                        with oc3:
                            if st.button("Правка", type="primary", key=f"edit_{tid}", use_container_width=True):
                                st.session_state["_task_edit_id"] = tid
                                st.rerun()
                        with oc4:
                            if st.button("✕", key=f"del_{tid}", use_container_width=True,
                                         help="Удалить задачу"):
                                st.session_state["_task_del"] = {
                                    "id": tid, "owner_id": owner_id,
                                    "segment": seg, "text": t.get("text", ""),
                                }
                                st.rerun()
                elif can_d:
                    # Чужая задача, но роль позволяет удалить (админ/суперадмин)
                    if st.button("✕", key=f"del_{tid}", use_container_width=False,
                                 help="Удалить задачу"):
                        st.session_state["_task_del"] = {
                            "id": tid, "owner_id": owner_id,
                            "segment": seg, "text": t.get("text", ""),
                        }
                        st.rerun()