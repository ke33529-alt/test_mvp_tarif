# streamlit_pages/local_kb_admin.py
"""
UI-раздел для управления локальной базой знаний сегмента.

Две вкладки:
  1. Документы — загрузка + список проиндексированных с методом индексации
  2. Настройки чанкования — метод, размер, оверлап, защита от резки слов

Только сегментный админ / суперадмин может открыть страницу.
Локальная база сегмента изолирована коллекцией ChromaDB (local_kb_{org_id}).
Настройки чанкования per-сегмент (data/admin/local_kb_chunking.json).

Использование в app.py:
  from streamlit_pages.local_kb_admin import show_local_kb_admin
  show_local_kb_admin()
"""

import streamlit as st

from core.auth import get_current_user
try:
    from core.audit import log_event
except Exception:
    def log_event(*a, **kw):
        pass

try:
    from streamlit_pages.superadmin import _load_segments
except Exception:
    _load_segments = None

from core.local_kb import (
    index_uploaded_file,
    get_segment_docs,
    get_segment_doc_count,
    get_segment_chunk_count,
    remove_document,
    clear_segment_collection,
    ALLOWED_EXTENSIONS,
    get_chunking_settings,
    save_chunking_settings,
    reset_chunking_settings,
    DEFAULT_CHUNKING,
)


def show_local_kb_admin():
    user = get_current_user()
    if not user:
        st.error("Требуется авторизация.")
        return

    role = user.get("role", "")
    if role not in ("superadmin", "segment_admin"):
        st.error("Доступ только для админов сегмента и суперадмина.")
        return

    st.markdown("### Локальная база знаний сегмента")

    if role == "superadmin":
        segments = _load_segments() if _load_segments else {}
        active_segments = {k: v for k, v in segments.items() if v.get("status") == "active"}
        if not active_segments:
            st.info("Сегментов пока нет.")
            return
        org_id = st.selectbox(
            "Сегмент",
            options=list(active_segments.keys()),
            format_func=lambda x: active_segments[x]["name"],
            key="local_kb_seg_select",
        )
    else:
        org_id = user.get("org_id", "")
        if not org_id:
            st.error("У вашего аккаунта не назначен сегмент.")
            return

    tab_upload, tab_settings = st.tabs(["Документы", "Настройки чанкования"])

    with tab_upload:
        _tab_documents(org_id)

    with tab_settings:
        _tab_chunking_settings(org_id)


# ─────────────────────────────────────────────────────────────────────────────
# Вкладка 1: Документы
# ─────────────────────────────────────────────────────────────────────────────

def _tab_documents(org_id: str):
    st.info(
        "Загружайте сюда внутренние документы вашей организации — регламенты, "
        "переписку, методички, любые материалы произвольного характера. "
        "**После обработки файл удаляется с сервера** — хранится только его "
        "имя и метаданные индексации."
    )

    settings = get_chunking_settings(org_id)
    if settings["method"] == "fixed":
        st.caption(
            f"Метод чанкования: **фиксированный** · "
            f"{settings['chunk_size']} симв · overlap {settings['overlap']} · "
            f"{'не резать по слову' if settings['word_safe'] else 'резать где попало'}"
        )
    else:
        st.caption("Метод чанкования: **по структуре документа** (для НПА)")

    st.markdown("##### Загрузить документы")
    uploaded = st.file_uploader(
        "Перетащите файлы или выберите с компьютера",
        type=[e.lstrip(".") for e in ALLOWED_EXTENSIONS],
        accept_multiple_files=True,
        key="local_kb_uploader",
    )
    if uploaded:
        st.caption(
            f"Выбрано файлов: {len(uploaded)}. Допустимые форматы: "
            + ", ".join(e.lstrip(".").upper() for e in ALLOWED_EXTENSIONS)
            + ". После загрузки исходные файлы будут удалены с сервера."
        )
        if st.button(
            f"Загрузить и проиндексировать ({len(uploaded)} файл(ов))",
            type="primary",
            key="local_kb_upload_btn",
        ):
            progress = st.progress(0)
            ok_count, err_list = 0, []
            for i, uf in enumerate(uploaded):
                result = index_uploaded_file(uf, org_id)
                if result.get("status") == "success":
                    ok_count += 1
                else:
                    err_list.append(f"{uf.name}: {result.get('message', 'ошибка')}")
                progress.progress((i + 1) / len(uploaded))
            if ok_count:
                st.session_state["_local_kb_upload_msg"] = (
                    f"Загружено и проиндексировано: {ok_count} файл(ов)"
                )
            if err_list:
                st.session_state["_local_kb_upload_errs"] = err_list
            st.rerun()

    if st.session_state.get("_local_kb_upload_msg"):
        st.success(st.session_state.pop("_local_kb_upload_msg"))
    if st.session_state.get("_local_kb_upload_errs"):
        for e in st.session_state.pop("_local_kb_upload_errs"):
            st.error(e)

    st.divider()

    st.subheader("Документы в локальной базе")
    docs = get_segment_docs(org_id)
    total_chunks = get_segment_chunk_count(org_id)

    c1, c2 = st.columns([1, 1])
    with c1:
        st.metric("Документов", len(docs))
    with c2:
        st.metric("Фрагментов в индексе", total_chunks)

    if not docs:
        st.caption("Локальная база пуста. Загрузите первые документы.")
        return

    st.divider()

    filter_name = st.text_input(
        "Поиск по названию",
        value="",
        key="local_kb_filter_name",
        placeholder="Введите часть названия файла...",
    )

    def _matches(name: str) -> bool:
        if not filter_name.strip():
            return True
        return filter_name.strip().lower() in name.lower()

    filtered = {name: info for name, info in docs.items() if _matches(name)}
    st.caption(f"Найдено документов: {len(filtered)}")

    # Заголовки таблицы
    hdr = st.columns([1, 3, 2, 2, 1, 0.5])
    hdr[0].markdown("**Формат**")
    hdr[1].markdown("**Наименование**")
    hdr[2].markdown("**Дата индексации**")
    hdr[3].markdown("**Метод индексации**")
    hdr[4].markdown("**Фрагментов**")
    hdr[5].markdown("🗑️")

    for filename, info in sorted(
        filtered.items(),
        key=lambda kv: kv[1].get("indexed_at", ""),
        reverse=True,
    ):
        ext          = info.get("ext", "?")
        indexed_at   = info.get("indexed_at", "")[:16].replace("T", " ")
        chunks_count = info.get("chunks", 0)
        size_kb      = info.get("size_kb_original", 0)
        method       = info.get("chunk_method", "legal")

        if method == "fixed":
            cs   = info.get("chunk_size", "?")
            ovlp = info.get("overlap", "?")
            ws   = info.get("word_safe", True)
            method_str = (
                f"По длине: {cs} симв · overlap {ovlp} · "
                f"{'не резать по слову' if ws else 'резать где попало'}"
            )
        else:
            method_str = "По структуре документа"

        with st.container():
            row = st.columns([1, 3, 2, 2, 1, 0.5])
            with row[0]:
                st.markdown(
                    f"<span style='background:#e8f4f8;color:#1B5C74;"
                    f"padding:2px 8px;border-radius:4px;font-size:0.75rem;"
                    f"font-weight:600'>{ext}</span>",
                    unsafe_allow_html=True,
                )
            with row[1]:
                st.markdown(f"**{filename}**")
                st.caption(
                    f"исходный размер: {size_kb} КБ (файл удалён с сервера)"
                )
            with row[2]:
                st.markdown(indexed_at or "—")
            with row[3]:
                st.markdown(
                    f"<small style='color:#5a6a7a'>{method_str}</small>",
                    unsafe_allow_html=True,
                )
            with row[4]:
                st.markdown(f"{chunks_count} фрагм.")
            with row[5]:
                if st.button("🗑️", key=f"local_kb_del_{filename}", use_container_width=True):
                    result = remove_document(org_id, filename)
                    if result.get("status") == "success":
                        st.session_state["_local_kb_upload_msg"] = (
                            f"Удалено: {filename} ({result.get('deleted', 0)} фрагм.)"
                        )
                    else:
                        st.session_state["_local_kb_upload_errs"] = [
                            f"{filename}: {result.get('message', 'ошибка удаления')}"
                        ]
                    st.rerun()

    st.divider()

    st.markdown("##### Опасные действия")
    if st.button("Очистить всю локальную базу", type="secondary", key="local_kb_clear_all_btn"):
        st.session_state["_confirm_clear_local_kb"] = True

    if st.session_state.get("_confirm_clear_local_kb"):
        st.warning(
            "Будут удалены все документы из локальной базы этого сегмента "
            "и все чанки из индекса. Действие необратимо."
        )
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("Да, полностью очистить", type="primary",
                         key="conf_clear_local_kb"):
                result = clear_segment_collection(org_id)
                st.session_state["_confirm_clear_local_kb"] = False
                if result.get("status") == "success":
                    st.success("Локальная база очищена.")
                else:
                    st.error(f"Ошибка: {result.get('message', '?')}")
                st.rerun()
        with col_b:
            if st.button("Отмена", use_container_width=True, key="cancel_clear_local_kb"):
                st.session_state["_confirm_clear_local_kb"] = False
                st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Вкладка 2: Настройки чанкования
# ─────────────────────────────────────────────────────────────────────────────

def _tab_chunking_settings(org_id: str):
    st.markdown("##### Настройки чанкования для сегмента")
    st.caption(
        "Настройки применяются при следующей загрузке документов. "
        "Уже проиндексированные файлы не меняются — их метод хранится "
        "в записи документа."
    )

    settings = get_chunking_settings(org_id)

    method = st.radio(
        "Метод чанкования",
        options=["legal", "fixed"],
        format_func=lambda x: {
            "legal": "По структуре документа (для НПА)",
            "fixed": "Фиксированный размер с оверлапом",
        }[x],
        index=0 if settings["method"] == "legal" else 1,
        key=f"chunk_method_{org_id}",
        horizontal=True,
    )

    if method == "legal":
        st.info(
            "Разбивка по структуре документа: статьи, пункты, разделы. "
            "Оптимально для нормативно-правовых актов. Настроек не требует."
        )
        chunk_size = settings["chunk_size"]
        overlap    = settings["overlap"]
        word_safe  = settings["word_safe"]

    else:
        st.info(
            "Фиксированные чанки заданного размера с оверлапом. "
            "Подходит для документов без чёткой структуры."
        )
        c1, c2 = st.columns(2)
        with c1:
            chunk_size = st.number_input(
                "Размер чанка (символов)",
                min_value=200,
                max_value=5000,
                value=settings["chunk_size"],
                step=100,
                key=f"chunk_size_{org_id}",
                help="Оптимально: 800–1500. Меньше — точнее поиск, больше — больше контекста.",
            )
        with c2:
            overlap = st.number_input(
                "Оверлап (символов)",
                min_value=0,
                max_value=chunk_size // 2,
                value=min(settings["overlap"], chunk_size // 2),
                step=10,
                key=f"chunk_overlap_{org_id}",
                help="Пересечение между соседними чанками. Оптимально: 10–15% от размера чанка.",
            )

        word_safe = st.checkbox(
            "Не резать по середине слова",
            value=settings["word_safe"],
            key=f"chunk_wordsafe_{org_id}",
            help="При обрезке ищет ближайшую границу слова слева от лимита.",
        )

    st.divider()

    col_save, col_reset = st.columns([2, 1])
    with col_save:
        if st.button("Сохранить настройки", type="primary",
                     key=f"save_chunk_{org_id}", use_container_width=True):
            save_chunking_settings(org_id, {
                "method":     method,
                "chunk_size": int(chunk_size),
                "overlap":    int(overlap),
                "word_safe":  bool(word_safe),
            })
            st.session_state["_chunk_saved"] = True
            st.rerun()

    with col_reset:
        if st.button("Сбросить к дефолтным", type="secondary",
                     key=f"reset_chunk_{org_id}", use_container_width=True):
            reset_chunking_settings(org_id)
            st.session_state["_chunk_reset"] = True
            st.rerun()

    if st.session_state.pop("_chunk_saved", False):
        st.success("Настройки сохранены. Будут применены к следующим загрузкам.")
    if st.session_state.pop("_chunk_reset", False):
        st.info(
            f"Настройки сброшены. Текущие дефолтные значения: "
            f"метод={DEFAULT_CHUNKING['method']}, "
            f"размер={DEFAULT_CHUNKING['chunk_size']}, "
            f"overlap={DEFAULT_CHUNKING['overlap']}, "
            f"защита слов={'да' if DEFAULT_CHUNKING['word_safe'] else 'нет'}."
        )