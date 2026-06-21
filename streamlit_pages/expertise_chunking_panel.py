"""
Вкладка «⚙️ Чанкование экспертных» — настройки и просмотр чанков для
экспертных заключений (doc_type="expertise"), коллекция Chroma "expertise_docs".

Логика чанкования полностью находится в core/expertise_chunker.py — эта
вкладка только показывает текущие правила (пока не редактируемые из UI —
правила завязаны на устойчивые эвристики, а не на простые числовые
параметры вроде chunk_size, см. пояснение ниже) и даёт просмотр уже
проиндексированных чанков.

Эта вкладка НЕ имеет отношения к «⚙️ Чанкование НПА» (tab_chunking,
режимы legal/structural/separator/fixed, коллекция tariff_docs).
"""

from __future__ import annotations

import os

import streamlit as st


def show_expertise_chunking_panel():
    st.header("⚙️ Чанкование экспертных заключений")
    st.caption(
        "Разбор структурированных разделов «### N» экспертных заключений "
        "и протоколов. Коллекция Chroma: expertise_docs. "
        "Логика — в core/expertise_chunker.py."
    )

    tab_params, tab_preview = st.tabs(["Параметры чанкования", "Просмотр и тест чанков"])

    with tab_params:
        _show_params_tab()

    with tab_preview:
        _show_preview_tab()


def _show_params_tab():
    st.info(
        "Правила чанкования экспертных заключений построены на гибких "
        "эвристиках (структура исходных файлов сгенерирована LLM и не "
        "стабильна между документами — нет фиксированного числа или "
        "порядка разделов), поэтому здесь нет числовых ползунков "
        "chunk_size/overlap, как во вкладке «Чанкование НПА»."
    )

    st.subheader("Правила (зашиты в core/expertise_chunker.py)")

    st.markdown("**1. Источник текста**")
    st.caption(
        "Чанкуется блок «ПОДРОБНЫЙ ПЕРЕСКАЗ», разбитый на секции по "
        "заголовкам `### N[.M] \"Название\" [tag: ...]`. Под-разделы "
        "(например 5.1, 5.2 внутри раздела 5) — каждый отдельный чанк, "
        "ничего не объединяется."
    )

    st.markdown("**2. Тег `[tag: ...]`**")
    st.caption(
        "Если заголовок секции содержит `[tag: xxx]` — тег выносится в "
        "поле metadata `tag` и убирается из текста чанка (не засоряет "
        "embedding)."
    )

    st.markdown("**3. Фильтр пустых разделов**")
    st.caption(
        "Раздел пропускается (не индексируется), если он короткий "
        "(≤ 600 символов) и начинается с шаблонной фразы об отсутствии "
        "данных — например «В документе/постановлении/источнике/"
        "предоставленных данных/выдержках отсутствует(-ют)...» или "
        "начинается прямо с «Не указано/не определено/не приведено...». "
        "Раздел НЕ считается пустым, если такая фраза встречается не в "
        "самом начале (например внутри структурированной строки "
        "«Заявлено: Не указано отдельно. Принято: 2,59 тыс. руб.» — там "
        "есть содержательные цифры дальше)."
    )

    st.markdown("**4. Раздел с тарифами**")
    st.caption(
        "Раздел, название которого содержит «тариф» (независимо от "
        "номера — он не всегда «9»), индексируется отдельным цельным "
        "чанком без дробления и без применения фильтра пустых разделов."
    )

    st.markdown("**5. OCR fallback**")
    st.caption(
        "Блок «РАСПОЗНАННЫЙ ТЕКСТ (OCR)» в конце файла индексируется "
        "отдельным нерасчленённым чанком с `block_kind=\"ocr_raw\"` — "
        "подстраховка на случай, если структурированный пересказ не "
        "покрывает нужную информацию."
    )

    st.divider()
    st.caption(
        "Если нужно скорректировать пороги (например EMPTY_SECTION_MAX_LEN) "
        "или добавить новые формулировки пустых разделов — правки вносятся "
        "в core/expertise_chunker.py."
    )


def _show_preview_tab():
    st.subheader("🔍 Просмотр чанков (expertise_docs)")

    vdb_path = os.path.join("data", "vector_db")
    try:
        # Переиспользуем тот же синглтон-клиент и embedding function, что
        # и при индексации (core/indexer.py) — PersistentClient нельзя
        # открывать дважды на одну папку.
        from core.indexer import _get_chroma_client
        from core.expertise_chunker import get_chroma_embedding_function
        client = _get_chroma_client()
        ef = get_chroma_embedding_function()
        collection = client.get_collection(name="expertise_docs", embedding_function=ef)
        all_data = collection.get(include=["documents", "metadatas"])
        raw_docs = all_data.get("documents", [])
        raw_metas = all_data.get("metadatas", [])
        raw_ids = all_data.get("ids", [])
    except Exception:
        st.warning(
            "⚠️ Коллекция expertise_docs пуста или ещё не создана — "
            "проиндексируйте документы во вкладке «Протоколы/Экспертные»."
        )
        return

    if not raw_docs:
        st.warning("⚠️ Коллекция expertise_docs пуста — проиндексируйте документы.")
        return

    # ── Группировка по файлам ───────────────────────────────────────
    fdict: dict = {}
    for did, doc, meta in zip(raw_ids, raw_docs, raw_metas):
        meta = meta or {}
        fn = meta.get("filename", "Неизвестно")
        if fn not in fdict:
            fdict[fn] = {
                "doc_type": meta.get("doc_type", "—"),
                "region": meta.get("region", "—"),
                "sphere": meta.get("sphere", "—"),
                "year": meta.get("year", "—"),
                "method": meta.get("method", "—"),
                "chunks": [],
            }
        fdict[fn]["chunks"].append({"id": did, "content": doc, "metadata": meta})

    for fn in fdict:
        fdict[fn]["chunks"].sort(key=lambda c: int(c["metadata"].get("chunk_index", 0)))

    sc1, sc2 = st.columns(2)
    sc1.metric("Всего чанков", len(raw_docs))
    sc2.metric("Файлов", len(fdict))
    st.divider()

    fnames = sorted(fdict.keys())
    sel_file = st.selectbox(
        "📄 Документ", fnames,
        format_func=lambda x: f"{x}  ({len(fdict[x]['chunks'])} чанков)",
        key="ecv_file_sel",
    )
    fi = fdict[sel_file]
    chunks = fi["chunks"]
    total = len(chunks)

    mc = st.columns(4)
    mc[0].metric("Чанков", total)
    mc[1].caption(f"**Сфера:** {fi['sphere']}")
    mc[2].caption(f"**Год:** {fi['year']}")
    mc[3].caption(f"**Метод:** {fi['method']}")
    st.caption(f"**Регион:** {fi['region']}")
    st.divider()

    if "ecv_chunk_idx" not in st.session_state:
        st.session_state["ecv_chunk_idx"] = 0
    if st.session_state.get("ecv_last_file") != sel_file:
        st.session_state["ecv_chunk_idx"] = 0
        st.session_state["ecv_last_file"] = sel_file
    cidx = min(st.session_state["ecv_chunk_idx"], total - 1)

    def _label(i):
        c = chunks[i]
        section = c["metadata"].get("section", "—")
        block_kind = c["metadata"].get("block_kind", "")
        art = c["metadata"].get("article_num", "")
        prefix = "🔊 OCR" if block_kind == "ocr_raw" else f"§{art}"
        return f"{prefix}  ·  {section[:50]}"

    sel_i = st.selectbox(
        "🔢 Чанк", options=list(range(total)), index=cidx, format_func=_label,
        key="ecv_chunk_select",
    )
    if sel_i != cidx:
        st.session_state["ecv_chunk_idx"] = sel_i
        st.rerun()

    nb1, nb2, nb3 = st.columns([1, 6, 1])
    with nb1:
        if st.button("◀", disabled=(cidx == 0), key="ecv_prev", use_container_width=True):
            st.session_state["ecv_chunk_idx"] = cidx - 1
            st.rerun()
    with nb2:
        st.caption(f"Чанк {cidx + 1} из {total}")
    with nb3:
        if st.button("▶", disabled=(cidx >= total - 1), key="ecv_next", use_container_width=True):
            st.session_state["ecv_chunk_idx"] = cidx + 1
            st.rerun()

    chunk = chunks[cidx]
    meta = chunk["metadata"]

    badge_cols = st.columns(4)
    badge_cols[0].caption(f"**Раздел:** {meta.get('section', '—')}")
    badge_cols[1].caption(f"**Номер:** {meta.get('article_num', '—')}")
    badge_cols[2].caption(f"**Тег:** {meta.get('tag') or '—'}")
    badge_cols[3].caption(f"**Тип блока:** {meta.get('block_kind', '—')}")

    st.text_area(
        "Текст чанка", value=chunk["content"], height=300,
        key=f"ecv_text_{chunk['id']}", disabled=True,
    )

    with st.expander("Полная metadata"):
        st.json(meta)
