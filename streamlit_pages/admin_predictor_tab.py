# streamlit_pages/admin_predictor_tab.py
"""
Вкладка «Прогнозист» панели администратора.
Управление протоколами, настройки классификации, просмотр чанков, тест-запросы.
"""
from __future__ import annotations
import os
import json

import streamlit as st


def show_predictor_tab():
    st.header("Настройки прогнозиста решений")
    st.info("Параметры влияют на скорость и качество классификации. Изменения применяются к следующему запросу.")

    _PRED_CFG_FILE = os.path.join("config", "predictor_config.json")
    _PRED_DEFAULTS = {
        "chunk_chars_to_llm":    800,
        "justification_chars":   200,
        "classify_max_tokens":   100,
        "default_top_k":         30,
        "disable_thinking":      True,
    }
    if os.path.exists(_PRED_CFG_FILE):
        try:
            with open(_PRED_CFG_FILE, "r", encoding="utf-8") as _f:
                _pred_cfg = {**_PRED_DEFAULTS, **json.load(_f)}
        except Exception:
            _pred_cfg = dict(_PRED_DEFAULTS)
    else:
        _pred_cfg = dict(_PRED_DEFAULTS)

    # ── Параметры LLM ────────────────────────────────────────────────
    st.subheader("Параметры классификации (LLM)")
    st.caption(
        "Каждый найденный чанк протокола классифицируется отдельным вызовом LLM. "
        "Меньше токенов на вызов = быстрее, но меньше контекста у модели."
    )

    _pc1, _pc2 = st.columns(2)
    with _pc1:
        _pred_chunk_chars = st.slider(
            "Символов чанка подавать в LLM",
            min_value=200, max_value=1500, step=100,
            value=int(_pred_cfg["chunk_chars_to_llm"]),
            key="pred_cfg_chunk_chars",
            help="Сколько символов из найденного чанка протокола передаётся модели для классификации. "
                 "Меньше = быстрее, больше = точнее. Не влияет на индексирование.",
        )
        _tok_chunk = _pred_chunk_chars // 3
        st.caption(f"≈ {_tok_chunk} токенов из чанка")

        _pred_justify_chars = st.slider(
            "Символов обоснования подавать в LLM",
            min_value=0, max_value=600, step=50,
            value=int(_pred_cfg["justification_chars"]),
            key="pred_cfg_justify_chars",
            help="Сколько символов обоснования заявителя добавляется в промпт классификации. "
                 "0 = не передавать обоснование (быстрее).",
        )
        _tok_justify = _pred_justify_chars // 3
        st.caption(f"≈ {_tok_justify} токенов обоснования")

    with _pc2:
        _pred_max_tokens = st.slider(
            "max_tokens на ответ классификации",
            min_value=60, max_value=300, step=20,
            value=int(_pred_cfg["classify_max_tokens"]),
            key="pred_cfg_max_tokens",
            help="Максимум токенов в JSON-ответе модели. "
                 "80–100 достаточно для JSON с тремя полями.",
        )
        _pred_thinking_off = st.toggle(
            "Отключить thinking у Qwen3",
            value=bool(_pred_cfg["disable_thinking"]),
            key="pred_cfg_thinking",
            help="Qwen3 генерирует внутренние рассуждения <think>...</think> перед ответом. "
                 "Отключение экономит 300–800 токенов на вызов.",
        )

        _tok_total = _tok_chunk + _tok_justify + 80  # ~80 токенов промпт-обёртка
        st.metric("Токенов на вызов (оценка)", f"~{_tok_total}")
        st.caption(
            f"При top-K=30: ~{_tok_total * 30 // 1000}K токенов суммарно на один прогноз"
        )

    st.divider()

    # ── Параметры поиска ─────────────────────────────────────────────
    st.subheader("Параметры поиска по протоколам")
    _pred_top_k = st.slider(
        "top-K чанков по умолчанию",
        min_value=5, max_value=100, step=5,
        value=int(_pred_cfg["default_top_k"]),
        key="pred_cfg_top_k",
        help="Сколько чанков протоколов извлекается из ChromaDB перед классификацией. "
             "Больше = шире охват, но больше вызовов LLM.",
    )
    st.caption(
        f"При top-K={_pred_top_k}: до {_pred_top_k} вызовов LLM на один прогноз · "
        f"оценка времени при 7 t/s: ~{(_pred_top_k * _tok_total) // 7 // 60} мин "
        f"{(_pred_top_k * _tok_total) // 7 % 60} сек"
    )

    st.divider()
    _ps1, _ps2 = st.columns(2)
    with _ps1:
        if st.button("💾 Сохранить настройки прогнозиста", type="primary",
                     use_container_width=True, key="pred_cfg_save"):
            _new_pred_cfg = {
                "chunk_chars_to_llm":  _pred_chunk_chars,
                "justification_chars": _pred_justify_chars,
                "classify_max_tokens": _pred_max_tokens,
                "default_top_k":       _pred_top_k,
                "disable_thinking":    _pred_thinking_off,
            }
            os.makedirs(os.path.dirname(_PRED_CFG_FILE), exist_ok=True)
            with open(_PRED_CFG_FILE, "w", encoding="utf-8") as _f:
                json.dump(_new_pred_cfg, _f, ensure_ascii=False, indent=2)
            st.success("✅ Настройки прогнозиста сохранены.")
            st.rerun()
    with _ps2:
        if st.button("🔄 Сбросить к умолчаниям", use_container_width=True, key="pred_cfg_reset"):
            if os.path.exists(_PRED_CFG_FILE):
                os.remove(_PRED_CFG_FILE)
            st.toast("🔄 Настройки прогнозиста сброшены", icon="🔄")
            st.rerun()

    st.divider()

    # ── Просмотр чанков протоколов ───────────────────────────────────
    st.subheader("🔍 Просмотр чанков протоколов")
    try:
        import chromadb as _pvcdb
        _pvdb_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vector_db")
        _pvcli = _pvcdb.PersistentClient(path=_pvdb_path)
        try:
            _pvcol = _pvcli.get_collection(name="protocols")
            _pvall = _pvcol.get(include=["documents", "metadatas"])
            _pv_docs  = _pvall.get("documents", [])
            _pv_metas = _pvall.get("metadatas", [])
            _pv_ids   = _pvall.get("ids", [])
        except Exception:
            _pv_docs, _pv_metas, _pv_ids = [], [], []

        if not _pv_docs:
            st.warning("⚠️ Коллекция protocols пуста — проиндексируйте протоколы во вкладке «Документы»")
        else:
            # Группировка по файлу (поле "file" в метаданных)
            _pvfdict: dict = {}
            for _pvdid, _pvdoc, _pvmeta in zip(_pv_ids, _pv_docs, _pv_metas):
                if _pvmeta is None:
                    _pvmeta = {}
                _pvfn = _pvmeta.get("file") or _pvmeta.get("filename", "Неизвестно")
                if _pvfn not in _pvfdict:
                    _pvfdict[_pvfn] = {
                        "sphere":       _pvmeta.get("sphere", "—"),
                        "region":       _pvmeta.get("region", "—"),
                        "organization": _pvmeta.get("organization", "—"),
                        "date":         _pvmeta.get("date", "—"),
                        "chunks": [],
                    }
                _pvfdict[_pvfn]["chunks"].append({
                    "id":       _pvdid,
                    "content":  _pvdoc,
                    "metadata": _pvmeta,
                })

            # Сортируем чанки по chunk_index
            for _pvfn in _pvfdict:
                _pvfdict[_pvfn]["chunks"].sort(
                    key=lambda c: int(c["metadata"].get("chunk_index", 0))
                )

            # Статистика
            _pvc1, _pvc2, _pvc3, _pvc4 = st.columns(4)
            _pvc1.metric("Файлов", len(_pvfdict))
            _pvc2.metric("Чанков всего", len(_pv_docs))
            _pvc3.metric("Ср. чанков/файл", round(len(_pv_docs) / max(len(_pvfdict), 1), 1))
            _avg_len = round(sum(len(d) for d in _pv_docs) / max(len(_pv_docs), 1))
            _pvc4.metric("Ср. длина чанка", f"{_avg_len} симв.")
            st.divider()

            # Выбор файла
            _pvfnames = sorted(_pvfdict.keys())
            _pv_sel_file = st.selectbox(
                "📄 Протокол",
                _pvfnames,
                format_func=lambda x: f"{x}  ({len(_pvfdict[x]['chunks'])} чанков)",
                key="pv_file_sel",
            )
            _pvfi   = _pvfdict[_pv_sel_file]
            _pvchunks = _pvfi["chunks"]
            _pvtotal  = len(_pvchunks)

            # Метаданные файла
            _pvm1, _pvm2, _pvm3, _pvm4 = st.columns(4)
            _pvm1.caption(f"**Сфера:** {_pvfi['sphere']}")
            _pvm2.caption(f"**Регион:** {_pvfi['region']}")
            _pvm3.caption(f"**Орг-ция:** {_pvfi['organization']}")
            _pvm4.caption(f"**Дата:** {_pvfi['date']}")
            st.divider()

            # Навигация по чанкам
            if "pv_chunk_idx" not in st.session_state:
                st.session_state["pv_chunk_idx"] = 0
            if st.session_state.get("pv_last_file") != _pv_sel_file:
                st.session_state["pv_chunk_idx"] = 0
                st.session_state["pv_last_file"] = _pv_sel_file
            _pvcidx = min(st.session_state["pv_chunk_idx"], _pvtotal - 1)

            def _pvclabel(i):
                _c = _pvchunks[i]
                _ci = _c["metadata"].get("chunk_index", i)
                _prev = _c["content"][:80].replace("\n", " ")
                return f"#{_ci}  ·  {_prev}…"

            _pv_sel_i = st.selectbox(
                "🔢 Чанк",
                options=list(range(_pvtotal)),
                index=_pvcidx,
                format_func=_pvclabel,
                key="pv_chunk_sel",
            )
            if _pv_sel_i != _pvcidx:
                st.session_state["pv_chunk_idx"] = _pv_sel_i
                st.rerun()

            _pvnb1, _pvnb2, _pvnb3 = st.columns([1, 6, 1])
            with _pvnb1:
                if st.button("◀", disabled=(_pvcidx == 0), key="pv_prev", use_container_width=True):
                    st.session_state["pv_chunk_idx"] = _pvcidx - 1
                    st.rerun()
            with _pvnb2:
                st.caption(f"Чанк {_pvcidx + 1} из {_pvtotal}")
            with _pvnb3:
                if st.button("▶", disabled=(_pvcidx >= _pvtotal - 1), key="pv_next", use_container_width=True):
                    st.session_state["pv_chunk_idx"] = _pvcidx + 1
                    st.rerun()

            _pvchunk   = _pvchunks[_pvcidx]
            _pvcontent = _pvchunk["content"]
            _pvmeta    = _pvchunk["metadata"]

            st.text_area(
                f"Содержимое  ·  {len(_pvcontent)} символов",
                value=_pvcontent,
                height=max(150, min(520, len(_pvcontent) // 2)),
                disabled=True,
                key="pv_content_area",
            )

            with st.expander("🏷️ Метаданные чанка", expanded=False):
                _pvmf = st.columns(2)
                _pvmfields = [
                    ("chunk_index",  _pvmeta.get("chunk_index", "—")),
                    ("file",         _pvmeta.get("file", "—")),
                    ("sphere",       _pvmeta.get("sphere", "—")),
                    ("region",       _pvmeta.get("region", "—")),
                    ("organization", _pvmeta.get("organization", "—")),
                    ("date",         _pvmeta.get("date", "—")),
                    ("category",     _pvmeta.get("category", "—")),
                    ("id",           _pvchunk["id"]),
                ]
                for _pvj, (_pvk, _pvv) in enumerate(_pvmfields):
                    _pvmf[_pvj % 2].caption(f"**{_pvk}:** {_pvv}")

            # Тест-запрос по протоколам
            st.divider()
            st.subheader("🧪 Тест-запрос по протоколам")
            _pvtq_c1, _pvtq_c2 = st.columns([4, 1])
            with _pvtq_c1:
                _pvtest_query = st.text_input(
                    "Запрос",
                    placeholder="заработная плата, амортизация, ремонт ОС…",
                    key="pv_test_query",
                )
            with _pvtq_c2:
                _pvtest_k = st.number_input(
                    "Топ-K", min_value=1, max_value=100,
                    value=int(_pred_cfg["default_top_k"]),
                    key="pv_test_k",
                )
            if st.button("🔎 Найти в протоколах", key="pv_test_btn", type="primary"):
                if _pvtest_query.strip():
                    with st.spinner("Ищем…"):
                        try:
                            from core.indexer import get_embedding_function, _get_chroma_client
                            _pvsc = _get_chroma_client()
                            _pvef = get_embedding_function()
                            _pvtcol = _pvsc.get_collection("protocols", embedding_function=_pvef)
                            _pvtres = _pvtcol.query(
                                query_texts=[_pvtest_query],
                                n_results=int(_pvtest_k),
                                include=["documents", "metadatas", "distances"],
                            )
                            _pvtsrcs = []
                            for _pvtd, _pvtm, _pvtdi in zip(
                                _pvtres["documents"][0],
                                _pvtres["metadatas"][0],
                                _pvtres["distances"][0],
                            ):
                                _pvtsrcs.append({
                                    "text": _pvtd,
                                    "file": _pvtm.get("file", "?"),
                                    "distance": _pvtdi,
                                    "sphere": _pvtm.get("sphere", ""),
                                    "region": _pvtm.get("region", ""),
                                })
                            if _pvtsrcs:
                                st.success(f"✅ Найдено {len(_pvtsrcs)} чанков")
                                for _pvti, _pvts in enumerate(_pvtsrcs, 1):
                                    _pvscore = max(0, round((1 - _pvts["distance"]) * 100, 1))
                                    _pvsc_icon = "🟢" if _pvscore >= 70 else "🟡" if _pvscore >= 40 else "🔴"
                                    _pvlabel = f"#{_pvti} · {_pvts['file']} · {_pvsc_icon} {_pvscore}%"
                                    if _pvts.get("sphere"):
                                        _pvlabel += f" · {_pvts['sphere']}"
                                    with st.expander(_pvlabel, expanded=(_pvti == 1)):
                                        st.text_area("", _pvts["text"], height=200,
                                                     disabled=True, key=f"pvtr_{_pvti}")
                                        st.caption(
                                            f"Дистанция: {_pvts['distance']:.4f} · "
                                            f"Регион: {_pvts.get('region','—')}"
                                        )
                            else:
                                st.warning("🔍 Ничего не найдено.")
                        except Exception as _pvte:
                            st.error(f"❌ {type(_pvte).__name__}: {_pvte}")
                else:
                    st.warning("⚠️ Введите запрос")

    except Exception as _pve:
        st.error(f"❌ {type(_pve).__name__}: {_pve}")
