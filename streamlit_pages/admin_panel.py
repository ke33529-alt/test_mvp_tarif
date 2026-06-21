# streamlit_pages/admin_panel.py
"""
Панель администратора
────────────────────────────────────                # ── RAG-фильтр по сферам ──────────────────────────────────────
                _CA_CFG_FILE = os.path.join("config", "claim_analyzer_config.json")
                def _load_ca_cfg():
                    try:
                        with open(_CA_CFG_FILE, "r", encoding="utf-8") as _f:
                            return json.load(_f)
                    except Exception:
                        return {}
                def _save_ca_cfg(cfg):
                    os.makedirs("config", exist_ok=True)
                    with open(_CA_CFG_FILE, "w", encoding="utf-8") as _f:
                        json.dump(cfg, _f, ensure_ascii=False, indent=2)
                _ca_cfg = _load_ca_cfg()
                _saved_spheres = _ca_cfg.get("rag_spheres", [])
                with st.expander("🔍 Фильтр базы знаний (RAG)", expanded=True):
                    st.markdown(
                        "Сферы для поиска НПА при анализе заявок. "
                        "Если ничего не выбрано — поиск по всей базе знаний."
                    )
                    _new_spheres = st.multiselect(
                        "Сферы", options=SPHERES,
                        default=[s for s in _saved_spheres if s in SPHERES],
                        placeholder="Не выбрано — вся база знаний",
                        key="ca_cfg_spheres", label_visibility="collapsed",
                    )
                    _cs1, _cs2 = st.columns([1, 1])
                    with _cs1:
                        if st.button("💾 Сохранить фильтр", key="ca_save_spheres",
                                     type="primary", use_container_width=True):
                            _ca_cfg["rag_spheres"] = _new_spheres
                            _save_ca_cfg(_ca_cfg)
                            st.success(f"✅ {', '.join(_new_spheres) if _new_spheres else 'Все сферы'}")
                            st.rerun()
                    with _cs2:
                        _cur = ', '.join(_saved_spheres) if _saved_spheres else 'все сферы'
                        st.info(f"Сейчас: {_cur}")
                st.divider()
──────────────────────────────────────────
Вкладки:
  📈 Аналитика ИИ   — статистика оценок советчика
  Документы         — загрузка и индексация НПА
  Настройки чанкования
  Поиск и реранкинг
  📝 Промпты
  Прогнозист        — управление протоколами и коллекцией
"""
from __future__ import annotations
import os
import json
import pandas as pd
from datetime import datetime

import streamlit as st
from core import admin
from core.feedback import get_feedback
from streamlit_pages.admin_predictor_tab import show_predictor_tab
from streamlit_pages.expertise_panel import show_documents_panel
from streamlit_pages.expertise_chunking_panel import show_expertise_chunking_panel


def get_live_answer_stats(days: int = 7):
    """Статистика оценок советчика из feedback_log.jsonl."""
    feedback_file = os.path.join("data", "feedback", "feedback_log.jsonl")
    stats = {
        "total": 0, "rating_3": 0, "rating_2": 0, "rating_1": 0,
        "with_comment": 0, "by_category": {}, "top_bad_questions": [], "avg_rating": 0
    }
    if not os.path.exists(feedback_file):
        return stats
    with open(feedback_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                fb = json.loads(line)
            except Exception:
                continue
            if fb.get("feedback_type") != "answer_rating":
                continue
            stats["total"] += 1
            rating = fb.get("rating")
            if rating == 3:   stats["rating_3"] += 1
            elif rating == 2: stats["rating_2"] += 1
            elif rating == 1: stats["rating_1"] += 1
            if fb.get("question"):
                stats["top_bad_questions"].append({
                    "question":  fb["question"][:100],
                    "answer":    fb.get("answer", "")[:200],
                    "comment":   fb.get("description", ""),
                    "timestamp": fb["timestamp"],
                })
            if stats["total"] > 0:
                stats["avg_rating"] = round(
                    (stats["rating_3"]*3 + stats["rating_2"]*2 + stats["rating_1"]*1) / stats["total"], 2
                )
    return stats



def is_admin_logged() -> bool:
    return st.session_state.get("admin_logged_in", False)


def show_admin_panel():
    st.header("Панель администратора")

    if not is_admin_logged():
        st.warning("🔒 Требуется вход администратора")
        password = st.text_input("Пароль", type="password")
        if st.button("🔓 Войти"):
            if admin.check_admin(password):
                st.session_state.admin_logged_in = True
                st.success("✅ Вход выполнен!")
                st.rerun()
            else:
                st.error("❌ Неверный пароль")
    else:
        tab_analytics, tab_docs, tab_chunking, tab_search, tab_prompts, tab_predictor, tab_claim_rag, tab_expertise, tab_expertise_chunking = st.tabs(
            ["📈 Аналитика ИИ", "📚 НПА", "⚙️ Чанкование НПА", "Поиск и реранкинг", "📝 Промпты", "Прогнозист", "📋 Анализатор заявок", "📑 Протоколы/Экспертные", "⚙️ Чанкование экспертных"]
        )

        with tab_analytics:
            col1, col2 = st.columns([4, 1])
            with col1:
                st.header("Качество работы ИИ-советчика")
            with col2:
                if st.button("🔄 Обновить", key="refresh_stats"):
                    st.rerun()
            st.caption(f"🕐 Обновлено: {datetime.now().strftime('%H:%M:%S')}")
            period = st.selectbox("Период", ["7 дней","30 дней","90 дней","Всё время"], key="period_select")
            days   = {"7 дней":7,"30 дней":30,"90 дней":90,"Всё время":365}[period]
            try:
                stats = get_live_answer_stats(days=days)
                if stats["total"] > 0:
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("Всего оценок",   stats["total"])
                    col2.metric("Средний рейтинг", f"{stats['avg_rating']}/3.0")
                    col3.metric("👍 Полезно",      stats["rating_3"])
                    col4.metric("👎 Не помогло",   stats["rating_1"])
                    quality_pct = round((stats["rating_3"] / stats["total"]) * 100)
                    st.subheader("Процент полезных ответов")
                    st.progress(quality_pct / 100)
                    st.caption(f"{quality_pct}% ответов оценены как полезно (цель: 85%)")
                    st.subheader("Распределение оценок")
                    rating_df = pd.DataFrame({
                        "Оценка": ["👍 Полезно","😐 Нормально","👎 Не помогло"],
                        "Количество": [stats["rating_3"],stats["rating_2"],stats["rating_1"]],
                    })
                    st.bar_chart(rating_df.set_index("Оценка"))
                    if stats["top_bad_questions"]:
                        st.subheader("Топ вопросов для улучшения")
                        for i, item in enumerate(stats["top_bad_questions"], 1):
                            with st.expander(f"{i}. «{item['question']}...»"):
                                st.write(f"**Ответ ИИ:** {item['answer']}")
                                st.write(f"**Комментарий:** {item['comment']}")
                                st.write(f"**Дата:** {item['timestamp'][:10]}")
                else:
                    st.info("📭 Пока нет оценок.")
            except Exception as e:
                st.error(f"Ошибка загрузки статистики: {e}")

        with tab_docs:
            st.header("База знаний — документы")
            SPHERES = ["🔥 Теплоснабжение","💧 Водоснабжение/водоотведение","🗑️ Обращение с ТКО","🔵 Газ","⚡ Электрика","📁 Иные сферы"]
            CATEGORY_FOLDERS = {"📜 Общие НПА":"npa","⚖️ Документы ФАС":"fas","🏛️ Судебная практика":"court","📋 Методички и разъяснения":"methodics"}
            SPHERES_FILE = os.path.join("config","doc_spheres.json")

            def load_spheres_map():
                if os.path.exists(SPHERES_FILE):
                    try:
                        with open(SPHERES_FILE,"r",encoding="utf-8") as f: return json.load(f)
                    except Exception: pass
                return {}
            def save_spheres_map(m):
                os.makedirs(os.path.dirname(SPHERES_FILE),exist_ok=True)
                with open(SPHERES_FILE,"w",encoding="utf-8") as f: json.dump(m,f,ensure_ascii=False,indent=2)

            spheres_map = load_spheres_map()

            st.divider()

            st.subheader("📤 Загрузить документы")

            # ── Загрузка НПА (оригинальный блок) ────────────────────────
            col_up1, col_up2 = st.columns([3,1])
            with col_up1:
                upload_category = st.selectbox("Категория для загрузки", list(CATEGORY_FOLDERS.keys()), key="upload_cat_select")
            with col_up2:
                upload_spheres = st.multiselect("Сферы", SPHERES, key="upload_spheres_select", placeholder="Выберите...")
            uploaded = st.file_uploader("Перетащите файлы или выберите с компьютера",
                                        type=["pdf","txt","docx","xlsx"], accept_multiple_files=True,
                                        key="doc_uploader", label_visibility="collapsed")
            if uploaded:
                dest_folder = CATEGORY_FOLDERS[upload_category]
                dest_path   = os.path.join("data","raw",dest_folder)
                os.makedirs(dest_path, exist_ok=True)
                if st.button(f"💾 Сохранить и индексировать ({len(uploaded)} файл(ов))", type="primary", key="save_upload_btn"):
                    if not upload_spheres:
                        st.error(
                            "⚠️ **Необходимо выбрать хотя бы одну сферу** перед индексацией! "
                            "Выберите сферу в поле «Сферы» выше и повторите."
                        )
                        st.stop()
                    progress = st.progress(0)
                    for i, uf in enumerate(uploaded):
                        file_path = os.path.join(dest_path, uf.name)
                        with open(file_path,"wb") as f: f.write(uf.getbuffer())
                        if upload_spheres:
                            spheres_map[uf.name] = upload_spheres
                            save_spheres_map(spheres_map)
                        try:
                            from core.indexer import index_file
                            index_file(file_path, dest_folder)
                        except Exception: pass
                        progress.progress((i+1)/len(uploaded))
                    st.success(f"✅ Загружено и проиндексировано: {len(uploaded)} файл(ов)")
                    try:
                        from core.advisor import invalidate_hybrid_retriever
                        invalidate_hybrid_retriever()
                    except Exception: pass
                    st.rerun()

            st.divider()
            st.subheader("📋 Список документов")

            # ── НПА: оригинальный список ─────────────────────────────────
            fc1, fc2, fc3 = st.columns([2,2,3])
            with fc1: filter_cat    = st.selectbox("Категория", ["— Все —"]+list(CATEGORY_FOLDERS.keys()), key="filter_cat")
            with fc2: filter_sphere = st.selectbox("Сфера",     ["— Все —"]+SPHERES, key="filter_sphere")
            with fc3: filter_name   = st.text_input("🔍 Поиск по имени файла", placeholder="Введите часть названия...", key="filter_name")

            _chroma_index = {}
            try:
                import chromadb as _chromadb
                _vector_db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"data","vector_db")
                _chroma_client  = _chromadb.PersistentClient(path=_vector_db_path)
                _collection     = _chroma_client.get_collection(name="tariff_docs")
                _results        = _collection.get(include=["metadatas"])
                for meta in _results["metadatas"]:
                    fn = meta.get("filename","")
                    if not fn: continue
                    if fn not in _chroma_index:
                        _chroma_index[fn] = {"chunks":0,"indexed_at":meta.get("indexed_at","")[:10] if meta.get("indexed_at") else "—"}
                    _chroma_index[fn]["chunks"] += 1
            except Exception: pass

            all_files = []
            cats_to_show = {filter_cat: CATEGORY_FOLDERS[filter_cat]} if filter_cat != "— Все —" else CATEGORY_FOLDERS
            for cat_label, folder in cats_to_show.items():
                folder_path = os.path.join("data","raw",folder)
                if not os.path.exists(folder_path): continue
                for fname in sorted(os.listdir(folder_path)):
                    fpath = os.path.join(folder_path, fname)
                    if not os.path.isfile(fpath) or fname.endswith(".indexed") or fname.startswith("."): continue
                    ext = os.path.splitext(fname)[1].upper().lstrip(".") or "—"
                    chroma_info = _chroma_index.get(fname,{})
                    all_files.append({
                        "fname":fname,"fpath":fpath,"folder":folder,"cat_label":cat_label,
                        "ext":ext,"size_kb":os.path.getsize(fpath)/1024,
                        "indexed_at":chroma_info.get("indexed_at","—") if chroma_info.get("chunks",0)>0 else "—",
                        "chunks_count":chroma_info.get("chunks",0),"spheres":spheres_map.get(fname,[]),
                    })
            if filter_sphere != "— Все —": all_files = [f for f in all_files if filter_sphere in f["spheres"]]
            if filter_name.strip():         all_files = [f for f in all_files if filter_name.lower() in f["fname"].lower()]

            if not all_files:
                st.info("📭 Документов не найдено. Загрузите файлы выше.")
            else:
                st.caption(f"Найдено документов: **{len(all_files)}**")
                hc = st.columns([1,4,2,3,2,1,1,1,1])
                for col, label in zip(hc, ["Формат","Наименование","Категория","Сферы","Дата индексации","📥","🔄","📤","🗑️"]):
                    col.markdown(f"**{label}**")
                st.divider()
                EXT_ICONS = {"PDF":"📕","TXT":"📄","DOCX":"📘","XLSX":"📗"}
                for fi in all_files:
                    row = st.columns([1,4,2,3,2,1,1,1,1])
                    icon = EXT_ICONS.get(fi["ext"],"📄")
                    with row[0]: st.markdown(f"{icon} `{fi['ext']}`")
                    with row[1]:
                        st.markdown(f"**{fi['fname']}**")
                        st.caption(f"{fi['size_kb']:.1f} КБ")
                    with row[2]: st.caption(fi["cat_label"])
                    with row[3]:
                        new_spheres = st.multiselect("сферы", SPHERES, default=fi["spheres"],
                                                     key=f"spheres_{fi['fname']}_{fi['folder']}", label_visibility="collapsed")
                        if new_spheres != fi["spheres"]:
                            spheres_map[fi["fname"]] = new_spheres
                            save_spheres_map(spheres_map)
                    with row[4]:
                        live_chunks = st.session_state.get(f"chunks_{fi['fname']}", fi["chunks_count"])
                        if live_chunks > 0:
                            st.markdown(f"✅ {fi['indexed_at']}")
                            st.caption(f"{live_chunks} чанков")
                        else:
                            st.caption("⬜ не индексирован")
                    with row[5]:
                        with open(fi["fpath"],"rb") as f:
                            st.download_button("📥", data=f.read(), file_name=fi["fname"],
                                               key=f"dl_{fi['fname']}_{fi['folder']}", use_container_width=True)
                    with row[6]:
                        if st.button("🔄", key=f"idx_{fi['fname']}_{fi['folder']}", use_container_width=True, help="Переиндексировать"):
                            if not spheres_map.get(fi["fname"]):
                                st.toast(
                                    f"⚠️ «Файл {fi['fname']}» не имеет сферы — "
                                    "выберите сферу в колонке «Сферы» и сохраните.",
                                    icon="⚠️"
                                )
                            else:
                                with st.spinner(f"Индексация {fi['fname']}..."):
                                    try:
                                        from core.indexer import remove_file_from_index, index_file
                                        # Сначала удаляем старые чанки
                                        old_chunks = fi["chunks_count"]
                                        try: remove_file_from_index(fi["fname"])
                                        except Exception: pass
                                        res = index_file(fi["fpath"], fi["folder"])
                                        if res["status"] == "success":
                                            new_chunks = res.get("chunks", 0)
                                            # Обновляем счётчик в session_state без rerun
                                            st.session_state[f"chunks_{fi['fname']}"] = new_chunks
                                            try:
                                                from core.advisor import invalidate_hybrid_retriever
                                                invalidate_hybrid_retriever()
                                            except Exception: pass
                                            delta = new_chunks - old_chunks
                                            delta_str = f"+{delta}" if delta >= 0 else str(delta)
                                            st.toast(f"✅ {fi['fname']}: {new_chunks} чанков ({delta_str})", icon="📥")
                                        else:
                                            st.toast(f"❌ {res.get('message','Ошибка индексации')}", icon="🚨")
                                    except Exception as e:
                                        st.toast(f"❌ {e}", icon="🚨")
                                st.rerun()
                    with row[7]:
                        if st.button("📤", key=f"rmidx_{fi['fname']}_{fi['folder']}", use_container_width=True):
                            st.session_state[f"_confirm_rmidx_{fi['fname']}"] = True
                    with row[8]:
                        if st.button("🗑️", key=f"del_{fi['fname']}_{fi['folder']}", use_container_width=True):
                            st.session_state[f"_confirm_del_{fi['fname']}"] = True

                    if st.session_state.get(f"_confirm_rmidx_{fi['fname']}"):
                        @st.dialog(f"📤 Удалить «{fi['fname']}» из индекса?")
                        def _confirm_rmidx(fname=fi["fname"]):
                            st.info("Файл останется в папке, чанки будут удалены.")
                            ca, cb = st.columns(2)
                            with ca:
                                if st.button("📤 Да", type="primary", use_container_width=True, key=f"conf_rmidx_{fname}"):
                                    removed = 0
                                    try:
                                        from core.indexer import remove_file_from_index
                                        removed = _chroma_index.get(fname, {}).get("chunks", 0)
                                        remove_file_from_index(fname)
                                    except Exception: pass
                                    try:
                                        from core.advisor import invalidate_hybrid_retriever
                                        invalidate_hybrid_retriever()
                                    except Exception: pass
                                    st.session_state.pop(f"_confirm_rmidx_{fname}", None)
                                    st.session_state[f"chunks_{fname}"] = 0
                                    st.toast(f"📤 {fname}: удалено {removed} чанков из индекса", icon="📤")
                                    st.rerun()
                            with cb:
                                if st.button("← Отмена", use_container_width=True, key=f"cancel_rmidx_{fname}"):
                                    st.session_state.pop(f"_confirm_rmidx_{fname}", None)
                                    st.rerun()
                        _confirm_rmidx()

                    if st.session_state.get(f"_confirm_del_{fi['fname']}"):
                        @st.dialog(f"🗑️ Удалить файл «{fi['fname']}»?")
                        def _confirm_delete(fpath=fi["fpath"], fname=fi["fname"]):
                            st.warning("Файл будет удалён с диска и из индекса.")
                            ca, cb = st.columns(2)
                            with ca:
                                if st.button("🗑️ Да", type="primary", use_container_width=True, key=f"conf_del_{fname}"):
                                    removed = 0
                                    try:
                                        from core.indexer import remove_file_from_index
                                        removed = _chroma_index.get(fname, {}).get("chunks", 0)
                                        remove_file_from_index(fname)
                                    except Exception: pass
                                    os.remove(fpath)
                                    spheres_map.pop(fname, None)
                                    save_spheres_map(spheres_map)
                                    try:
                                        from core.advisor import invalidate_hybrid_retriever
                                        invalidate_hybrid_retriever()
                                    except Exception: pass
                                    st.session_state.pop(f"_confirm_del_{fname}", None)
                                    st.toast(f"🗑️ {fname} удалён ({removed} чанков)", icon="🗑️")
                                    st.rerun()
                            with cb:
                                if st.button("← Отмена", use_container_width=True, key=f"cancel_del_{fname}"):
                                    st.session_state.pop(f"_confirm_del_{fname}", None)
                                    st.rerun()
                        _confirm_delete()
                    st.divider()

                st.divider()
                st.subheader("⚙️ Массовые операции")
                reindex_cat = st.selectbox("Категория для переиндексации", list(CATEGORY_FOLDERS.keys()), key="reindex_cat_select")
                if st.button("🚀 Переиндексировать категорию", type="primary", use_container_width=True, key="reindex_cat_btn"):
                    # Проверяем файлы без сферы в выбранной категории
                    _reindex_folder_path = os.path.join("data", "raw", CATEGORY_FOLDERS[reindex_cat])
                    _no_sphere_files = []
                    if os.path.exists(_reindex_folder_path):
                        for _fn in sorted(os.listdir(_reindex_folder_path)):
                            if _fn.startswith(".") or _fn.endswith(".indexed"): continue
                            if not os.path.isfile(os.path.join(_reindex_folder_path, _fn)): continue
                            if not spheres_map.get(_fn):
                                _no_sphere_files.append(_fn)
                    if _no_sphere_files:
                        st.warning(
                            f"⚠️ **{len(_no_sphere_files)} файл(ов) без назначенной сферы.** "
                            "Назначьте сферы перед индексацией:\n\n" +
                            "\n".join(f"• {fn}" for fn in _no_sphere_files)
                        )
                        st.stop()
                    with st.spinner("⏳ Индексация..."):
                        try:
                            from core.indexer import index_category
                            res = index_category(CATEGORY_FOLDERS[reindex_cat])
                            if res["status"] == "success":
                                _fi_count = len(res.get("files", []))
                                _chunk_count = sum(
                                    r.get("result", {}).get("chunks", 0)
                                    for r in res.get("files", [])
                                    if isinstance(r.get("result"), dict)
                                )
                                st.session_state["_mass_reindex_msg"] = (
                                    f"✅ Раздел **{reindex_cat}** переиндексирован: "
                                    f"{_fi_count} файл(ов), {_chunk_count} чанков"
                                )
                                try:
                                    from core.advisor import invalidate_hybrid_retriever
                                    invalidate_hybrid_retriever()
                                except Exception: pass
                                st.rerun()
                            else: st.error(f"❌ {res.get('message','')}")
                        except Exception as e: st.error(f"❌ {e}")
                if st.session_state.get("_mass_reindex_msg"):
                    st.success(st.session_state["_mass_reindex_msg"])
                    del st.session_state["_mass_reindex_msg"]
                st.divider()
                if st.button("🗑️ Очистить весь индекс", type="secondary", use_container_width=True, key="clear_index_btn"):
                    st.session_state._confirm_clear_index = True
                if st.session_state.get("_confirm_clear_index"):
                    @st.dialog("🗑️ Очистить весь индекс?")
                    def _confirm_clear():
                        st.warning("Все чанки будут удалены. Файлы останутся на диске.")
                        ca, cb = st.columns(2)
                        with ca:
                            if st.button("🗑️ Да, очистить", type="primary", use_container_width=True, key="conf_clear_idx"):
                                try:
                                    from core.indexer import clear_index
                                    clear_index()
                                except Exception: pass
                                try:
                                    from core.advisor import invalidate_chroma_collection
                                    invalidate_chroma_collection()
                                except Exception: pass
                                st.session_state._confirm_clear_index = False
                                st.session_state["_mass_clear_msg"] = "🗑️ Весь индекс очищен. Файлы на диске сохранены."
                                st.rerun()
                        with cb:
                            if st.button("← Отмена", use_container_width=True, key="cancel_clear_idx"):
                                st.session_state._confirm_clear_index = False
                                st.rerun()
                    _confirm_clear()
                if st.session_state.get("_mass_clear_msg"):
                    st.success(st.session_state["_mass_clear_msg"])
                    del st.session_state["_mass_clear_msg"]

        with tab_chunking:
            st.header("Настройки чанкования документов")
            config_dir  = os.path.join("config")
            os.makedirs(config_dir, exist_ok=True)
            config_file = os.path.join(config_dir,"chunking_patterns.json")
            if os.path.exists(config_file):
                with open(config_file,'r',encoding='utf-8') as f: config = json.load(f)
            else:
                config = {
                    "patterns":{"section":r"^(РАЗДЕЛ|ГЛАВА)\s+[IVX0-9]+","article":r"^(Статья|ст\.)\s+[0-9]+",
                                "paragraph":r"^(п\.|пункт)\s*[0-9.]+","subparagraph":r"^[0-9]+\.[0-9]+"},
                    "doc_types":{"фас":"fas_document","фз":"federal_law","приказ":"order","письмо":"letter","методич":"methodology"},
                    "metadata_patterns":{"doc_number":r"(\d+[А-Я]?-\d+[А-Я]?)","doc_date":r"(\d{2}\.\d{2}\.\d{4})","doc_year":r"(\d{4})"},
                    "chunking_settings":{"chunk_size":500,"chunk_overlap":50,"min_chunk_length":100},
                }
            tab4, tab5 = st.tabs(["Параметры чанкования", "Просмотр и тест чанков"])
            with tab4:
                st.subheader("Параметры чанкования")
                settings = config.get("chunking_settings",{})
                chunking_mode = st.radio("Режим чанкования",
                    options=["legal","structural","separator","fixed"],
                    format_func=lambda x:{
                        "legal":      "⚖️ По пунктам НПА (рекомендуется)",
                        "structural": "🧠 Умный (по структуре)",
                        "separator":  "✂️ По разделителю",
                        "fixed":      "📏 Фиксированная длина",
                    }.get(x, x),
                    index=["legal","structural","separator","fixed"].index(
                        settings.get("chunking_mode","legal")
                        if settings.get("chunking_mode","legal") in ["legal","structural","separator","fixed"]
                        else "legal"
                    ),
                    key="chunking_mode_radio")
                st.divider()
                # Инициализируем все переменные из settings ДО if/elif —
                # иначе NameError если режим не выбирает свой виджет
                separator     = settings.get("separator", "&&")
                fixed_length  = settings.get("fixed_chunk_length", 1000)
                min_chunk     = settings.get("min_chunk_length", 80)
                max_chunk     = settings.get("max_chunk_length", 1500)
                chunk_overlap = settings.get("chunk_overlap", 0)
                if chunking_mode == "legal":
                    st.caption("⚖️ Один чанк = один пункт/статья/подпункт НПА. Максимальная точность цитирования.")
                    col1, col2 = st.columns(2)
                    with col1:
                        max_chunk = st.slider("Макс. длина чанка (симв.)", 500, 8000, max_chunk,
                            key="max_chunk_legal",
                            help="Если пункт длиннее — режется по предложениям с сохранением заголовка пункта")
                    with col2:
                        min_chunk = st.slider("Мин. длина блока (симв.)", 10, 300, min_chunk,
                            key="min_chunk_legal",
                            help="Блоки короче этого значения объединяются со следующим")
                elif chunking_mode == "structural":
                    col1,col2 = st.columns(2)
                    with col1:
                        min_chunk = st.slider("Мин. длина чанка (симв.)", 10, 500, min_chunk, key="min_chunk_s",
                            help="Чанки короче этого значения отфильтровываются как мусор (заголовки, пустые строки)")
                    with col2:
                        max_chunk = st.slider("Макс. длина чанка (симв.)", 200, 5000, max_chunk, key="max_chunk_s",
                            help="Рекомендуется 800–1000 для нормативных документов. Один пункт НПА — ~600–900 символов")
                elif chunking_mode == "separator":
                    separator = st.text_input("Маркер конца чанка", value=separator, key="chunk_separator_input")
                    col1,col2 = st.columns(2)
                    with col1: min_chunk = st.slider("Мин. длина чанка",10,500,min_chunk,key="min_chunk_sep")
                    with col2: max_chunk = st.slider("Макс. длина чанка",200,5000,max_chunk,key="max_chunk_sep")
                elif chunking_mode == "fixed":
                    fixed_length = st.slider("Длина чанка (символов)",100,5000,fixed_length,step=50,key="fixed_chunk_length_slider")
                st.divider()
                chunk_overlap = st.slider("Перекрытие (символов)", 0, 500, chunk_overlap, step=10, key="chunk_overlap_slider",
                    help="Сколько символов из конца предыдущего чанка добавляется в начало следующего. Рекомендуется 100–200")
                st.divider()
                st.subheader("🔒 Границы разрезания")
                no_cut_word = st.toggle(
                    "Не резать в середине слова",
                    value=settings.get("no_cut_word", True), key="no_cut_word",
                    help="Чанк всегда заканчивается на границе слова. Если лимит достигнут внутри слова — откатываемся до предыдущего пробела."
                )
                no_cut_sentence = st.toggle(
                    "Не резать в середине предложения",
                    value=settings.get("no_cut_sentence", True), key="no_cut_sentence",
                    help="Чанк заканчивается на знаке препинания (. ! ?). Рекомендуется для нормативных текстов — сохраняет юридически значимые формулировки целиком."
                )
                no_cut_paragraph = st.toggle(
                    "Не резать в середине абзаца",
                    value=settings.get("no_cut_paragraph", False), key="no_cut_paragraph",
                    help="Чанк заканчивается только на пустой строке (границе абзаца). Может давать чанки разного размера, зато каждый абзац НПА остаётся нетронутым."
                )
                if no_cut_paragraph:
                    st.info("ℹ️ При включённом режиме 'не резать абзац' параметр макс. длины становится мягким ограничением — абзац целиком важнее размера.")
                st.divider()
                if st.button("💾 Сохранить параметры", key="save_settings", use_container_width=True, type="primary"):
                    config["chunking_settings"] = {
                        "chunking_mode":      chunking_mode,
                        "separator":          separator,
                        "fixed_chunk_length": fixed_length,
                        "min_chunk_length":   min_chunk,
                        "max_chunk_length":   max_chunk,
                        "chunk_overlap":      chunk_overlap,
                        "no_cut_word":        no_cut_word,
                        "no_cut_sentence":    no_cut_sentence,
                        "no_cut_paragraph":   no_cut_paragraph,
                    }
                    with open(config_file,'w',encoding='utf-8') as f: json.dump(config,f,ensure_ascii=False,indent=2)
                    st.toast("✅ Параметры сохранены — не забудьте переиндексировать документы", icon="💾")
                    st.session_state["_settings_saved"] = True
                    st.rerun()
                if st.session_state.get("_settings_saved"):
                    st.success("✅ Параметры чанкования сохранены. Для применения — переиндексируйте документы в разделе **Документы → Массовые операции**.")
                    st.session_state["_settings_saved"] = False
                if st.button("🔄 Сбросить к умолчаниям", key="reset_config", use_container_width=True):
                    if os.path.exists(config_file): os.remove(config_file)
                    st.toast("✅ Конфигурация сброшена", icon="🔄")
                    st.session_state["_settings_reset"] = True
                    st.rerun()
                if st.session_state.get("_settings_reset"):
                    st.info("🔄 Настройки сброшены к умолчаниям.")
                    st.session_state["_settings_reset"] = False
            with tab5:
                st.subheader("🔍 Просмотр чанков")
                try:
                    import chromadb as _cdb
                    _vdb_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vector_db")
                    _cli = _cdb.PersistentClient(path=_vdb_path)
                    try:
                        _col = _cli.get_collection(name="tariff_docs")
                        _all = _col.get(include=["documents", "metadatas"])
                        _raw_docs  = _all.get("documents", [])
                        _raw_metas = _all.get("metadatas", [])
                        _raw_ids   = _all.get("ids", [])
                    except Exception:
                        _raw_docs = []
                        _raw_metas = []
                        _raw_ids = []

                    if not _raw_docs:
                        st.warning("⚠️ Векторная база пуста — проиндексируйте документы")
                    else:
                        # ── Группировка по файлам ────────────────────────────
                        _fdict: dict = {}
                        for _did, _doc, _meta in zip(_raw_ids, _raw_docs, _raw_metas):
                            if _meta is None:
                                _meta = {}
                            _fn = _meta.get("filename", "Неизвестно")
                            if _fn not in _fdict:
                                _fdict[_fn] = {
                                    "doc_type":   _meta.get("doc_type",   "—"),
                                    "doc_number": _meta.get("doc_number", "—"),
                                    "doc_date":   _meta.get("doc_date",   "—"),
                                    "chunks": [],
                                }
                            _fdict[_fn]["chunks"].append({
                                "id":       _did,
                                "content":  _doc,          # полный текст, без обрезки
                                "metadata": _meta,
                            })
                        # Сортируем чанки внутри файла по chunk_index
                        for _fn in _fdict:
                            _fdict[_fn]["chunks"].sort(
                                key=lambda c: int(c["metadata"].get("chunk_index", 0))
                            )

                        # ── Статистика ───────────────────────────────────────
                        _sc1, _sc2 = st.columns(2)
                        _sc1.metric("Всего чанков", len(_raw_docs))
                        _sc2.metric("Файлов", len(_fdict))
                        st.divider()

                        # ── Выбор файла ──────────────────────────────────────
                        _fnames = sorted(_fdict.keys())
                        _sel_file = st.selectbox(
                            "📄 Документ",
                            _fnames,
                            format_func=lambda x: f"{x}  ({len(_fdict[x]['chunks'])} чанков)",
                            key="cv_file_sel",
                        )
                        _fi = _fdict[_sel_file]
                        _chunks = _fi["chunks"]
                        _total  = len(_chunks)

                        _mc = st.columns(4)
                        _mc[0].metric("Чанков", _total)
                        _mc[1].caption(f"**Тип:** {_fi['doc_type']}")
                        _mc[2].caption(f"**Номер:** {_fi['doc_number']}")
                        _mc[3].caption(f"**Дата:** {_fi['doc_date']}")
                        st.divider()

                        # ── Выбор чанка ─────────────────────────────────────
                        # cv_chunk_idx — единственный источник истины.
                        # selectbox рендерится БЕЗ key, чтобы не было конфликта
                        # между его внутренним состоянием и нашей переменной.
                        if "cv_chunk_idx" not in st.session_state:
                            st.session_state["cv_chunk_idx"] = 0
                        if st.session_state.get("cv_last_file") != _sel_file:
                            st.session_state["cv_chunk_idx"] = 0
                            st.session_state["cv_last_file"] = _sel_file
                        _cidx = min(st.session_state["cv_chunk_idx"], _total - 1)

                        def _clabel(i):
                            _c = _chunks[i]
                            _ci = _c["metadata"].get("chunk_index", i)
                            _prev = _c["content"][:80].replace("\n", " ")
                            return f"#{_ci}  ·  {_prev}…"

                        # selectbox — без key, index задаётся из cv_chunk_idx
                        _sel_i = st.selectbox(
                            "🔢 Чанк",
                            options=list(range(_total)),
                            index=_cidx,
                            format_func=_clabel,
                        )
                        # Если пользователь выбрал вручную — синхронизируем и перезапускаем
                        if _sel_i != _cidx:
                            st.session_state["cv_chunk_idx"] = _sel_i
                            st.rerun()

                        # Кнопки навигации
                        _nb1, _nb2, _nb3 = st.columns([1, 6, 1])
                        with _nb1:
                            if st.button("◀", disabled=(_cidx == 0), key="cv_prev", use_container_width=True):
                                st.session_state["cv_chunk_idx"] = _cidx - 1
                                st.rerun()
                        with _nb2:
                            st.caption(f"Чанк {_cidx + 1} из {_total}")
                        with _nb3:
                            if st.button("▶", disabled=(_cidx >= _total - 1), key="cv_next", use_container_width=True):
                                st.session_state["cv_chunk_idx"] = _cidx + 1
                                st.rerun()

                        # ── Полное содержимое ────────────────────────────────
                        _chunk   = _chunks[_cidx]
                        _content = _chunk["content"]
                        _meta    = _chunk["metadata"]

                        st.text_area(
                            f"Содержимое  ·  {len(_content)} символов",
                            value=_content,
                            height=max(150, min(520, len(_content) // 2)),
                            disabled=True,
                        )

                        # ── Метаданные чанка ─────────────────────────────────
                        with st.expander("🏷️ Метаданные чанка", expanded=False):
                            _mf = st.columns(2)
                            _mfields = [
                                ("chunk_index", _meta.get("chunk_index", "—")),
                                ("struct_type",  _meta.get("struct_type",  "—")),
                                ("struct_text",  _meta.get("struct_text",  "—")),
                                ("article",      _meta.get("article",      "—")),
                                ("paragraph",    _meta.get("paragraph",    "—")),
                                ("category",     _meta.get("category",     "—")),
                                ("doc_type",     _meta.get("doc_type",     "—")),
                                ("id",           _chunk["id"]),
                            ]
                            for _j, (_k, _v) in enumerate(_mfields):
                                _mf[_j % 2].caption(f"**{_k}:** {_v}")

                        # ── Тест-запрос (вспомогательный) ───────────────────
                        st.divider()
                        st.subheader("🧪 Тест-запрос к базе")
                        _tq_c0, _tq_c1, _tq_c2 = st.columns([2, 4, 1])
                        with _tq_c0:
                            _test_collection = st.selectbox(
                                "Коллекция",
                                ["tariff_docs", "protocols"],
                                key="test_collection_select",
                                help="tariff_docs — НПА для советчика, protocols — протоколы для прогнозиста",
                            )
                        with _tq_c1:
                            test_query = st.text_input("Запрос", placeholder="расходы на ремонт основных средств", key="test_query_input")
                        with _tq_c2:
                            try:
                                _sr_file_path = os.path.join('config', 'search_settings.json')
                                _sr_saved = json.load(open(_sr_file_path, encoding='utf-8')) if os.path.exists(_sr_file_path) else {}
                                _test_default_k = int(_sr_saved.get('candidates_per_var', 25))
                            except Exception:
                                _test_default_k = 25
                            test_top_k = st.number_input('Топ-K', min_value=1, max_value=200,
                                value=_test_default_k, key='test_top_k',
                                help='По умолчанию = кандидатов на вариант из настроек поиска')
                        if st.button("🔎 Найти чанки", key="test_search_btn", type="primary"):
                            if test_query.strip():
                                with st.spinner("Ищем..."):
                                    try:
                                        if _test_collection == "protocols":
                                            # Поиск по коллекции протоколов
                                            import chromadb as _tcdb
                                            from chromadb.config import Settings as _TCS
                                            _tc_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vector_db")
                                            _tc_client = _tcdb.PersistentClient(path=_tc_path, settings=_TCS(anonymized_telemetry=False))
                                            _tc_col = _tc_client.get_collection("protocols")
                                            _tc_res = _tc_col.query(query_texts=[test_query], n_results=int(test_top_k), include=["documents","metadatas","distances"])
                                            test_sources = []
                                            for _td, _tm, _tdi in zip(_tc_res["documents"][0], _tc_res["metadatas"][0], _tc_res["distances"][0]):
                                                test_sources.append({"snippet": _td, "file": _tm.get("file","?"), "distance": _tdi, "chunk_index": "", "sphere": _tm.get("sphere",""), "region": _tm.get("region",""), "organization": _tm.get("organization","")})
                                        else:
                                            from core.advisor import search_vector_db as _svdb
                                            test_sources = _svdb(test_query, top_k=int(test_top_k))
                                        if test_sources:
                                            st.success(f"✅ Найдено {len(test_sources)} чанков")
                                            for _ti, _src in enumerate(test_sources, 1):
                                                _score = max(0, round((1 - _src.get("distance", 1)) * 100, 1))
                                                _sc = "🟢" if _score >= 70 else "🟡" if _score >= 40 else "🔴"
                                                with st.expander(f"#{_ti} · {_src['file']} · {_sc} {_score}%", expanded=(_ti == 1)):
                                                    st.text_area("", _src["snippet"], height=200, disabled=True, key=f"tsr_{_ti}")
                                                    st.caption(f"Дистанция: {_src['distance']} · Чанк: {_src.get('chunk_index', '')}")
                                        else:
                                            st.warning("🔍 Ничего не найдено.")
                                    except Exception as _te:
                                        st.error(f"❌ {type(_te).__name__}: {_te}")
                            else:
                                st.warning("⚠️ Введите запрос")

                except Exception as e:
                    st.error(f"❌ {type(e).__name__}: {e}")

        with tab_search:
            st.header("Настройки поиска и реранкинга")
            st.info("Изменения применяются сразу к следующему запросу. Перезапуск не нужен.")

            _sr_file = os.path.join("config", "search_settings.json")
            _sr_defaults = {
                "bm25_weight":        1.5,
                "candidates_per_var": 15,
                "context_max_chars":  8000,
                "reranker_enabled":   True,
            }
            if os.path.exists(_sr_file):
                try:
                    with open(_sr_file, "r", encoding="utf-8") as f:
                        _sr_cur = {**_sr_defaults, **json.load(f)}
                except Exception:
                    _sr_cur = dict(_sr_defaults)
            else:
                _sr_cur = dict(_sr_defaults)

            st.subheader("⚖️ Гибридный поиск (BM25 + вектор)")
            _bm25_w = st.slider(
                "Вес BM25 относительно векторного поиска",
                min_value=0.5, max_value=3.0, step=0.1,
                value=float(_sr_cur["bm25_weight"]),
                help="1.0 = равный вес. >1 = BM25 важнее (точное вхождение слов). <1 = вектор важнее (семантика).",
                key="sr_bm25_weight",
            )
            st.caption(f"{'🔤 Точные слова важнее' if _bm25_w > 1.0 else '🧠 Семантика важнее' if _bm25_w < 1.0 else '⚖️ Равный вес'}")

            _cands = st.slider(
                "Кандидатов на вариант запроса",
                min_value=5, max_value=40, step=5,
                value=int(_sr_cur["candidates_per_var"]),
                help="Сколько чанков отбирается от каждого варианта запроса перед реранкингом. Больше = точнее, но медленнее.",
                key="sr_candidates",
            )

            st.divider()
            st.subheader("🔁 Реранкинг (CrossEncoder)")
            _reranker_on = st.toggle(
                "Включить реранкинг",
                value=bool(_sr_cur["reranker_enabled"]),
                help="CrossEncoder переставляет кандидатов по реальной релевантности запросу. Отключите если медленно.",
                key="sr_reranker_on",
            )
            if _reranker_on:
                try:
                    from core.advisor import get_reranker_status as _grs, get_reranker as _gr, invalidate_reranker as _ir
                    _status = _grs()
                    if _status["loaded"]:
                        st.success(f"✅ Загружена модель: `{_status['model_name']}`")
                    else:
                        with st.spinner("⏳ Загружаем реранкер..."):
                            _rm = _gr()
                        if _rm:
                            st.success(f"✅ Загружена модель: `{_rm.model_name}`")
                        else:
                            _last_err = _status.get("last_error", "")
                            st.warning("⚠️ Реранкер не загружен")
                            if _last_err:
                                st.code(_last_err, language="text")
                            if st.button("🔄 Попробовать снова", key="sr_reload_reranker"):
                                _ir()
                                st.rerun()
                except Exception as _e:
                    st.warning(f"⚠️ Ошибка импорта: {_e}")
            else:
                st.caption("Результаты ранжируются только по RRF-score (BM25 + вектор).")

            st.divider()
            st.subheader("🤖 Модель реранкера")
            try:
                from core.advisor import AVAILABLE_RERANKER_MODELS as _ARM, get_reranker_status as _grs2, invalidate_reranker as _ir3, get_reranker as _gr2
                _model_ids    = [m["id"]    for m in _ARM]
                _model_labels = [m["label"] for m in _ARM]
                _model_descs  = {m["id"]: m["desc"] for m in _ARM}
                _cur_model    = _sr_cur.get("reranker_model", _model_ids[0])
                _cur_idx      = _model_ids.index(_cur_model) if _cur_model in _model_ids else 0
                _sel_label    = st.radio(
                    "Выберите модель реранкера",
                    _model_labels,
                    index=_cur_idx,
                    key="sr_reranker_model",
                )
                _sel_model_id = _model_ids[_model_labels.index(_sel_label)]
                st.caption(f"ℹ️ {_model_descs.get(_sel_model_id, '')}")
                _loaded_status = _grs2()
                _currently_loaded = _loaded_status.get("model_name", "")
                if _loaded_status["loaded"] and _currently_loaded != _sel_model_id:
                    if st.button("⚡ Переключить на выбранную модель", key="sr_switch_model", type="primary", use_container_width=True):
                        _ir3()
                        _new_sr_model = {**_sr_cur, "reranker_model": _sel_model_id}
                        os.makedirs("config", exist_ok=True)
                        with open(_sr_file, "w", encoding="utf-8") as f:
                            json.dump(_new_sr_model, f, ensure_ascii=False, indent=2)
                        st.session_state["_search_settings"] = _new_sr_model
                        with st.spinner(f"⏳ Загружаем {_sel_model_id}..."):
                            _new_rm = _gr2()
                        if _new_rm:
                            st.session_state["_model_switched"] = _new_rm.model_name
                        else:
                            st.session_state["_model_switch_failed"] = _sel_model_id
                        st.rerun()
                if st.session_state.get("_model_switched"):
                    st.success(f"✅ Модель переключена: `{st.session_state['_model_switched']}`")
                    del st.session_state["_model_switched"]
                if st.session_state.get("_model_switch_failed"):
                    st.error(f"❌ Не удалось загрузить `{st.session_state['_model_switch_failed']}`")
                    del st.session_state["_model_switch_failed"]
            except Exception as _me:
                st.warning(f"Не удалось загрузить список моделей: {_me}")
                _sel_model_id = _sr_cur.get("reranker_model", "DiTy/cross-encoder-russian-msmarco")

            st.divider()
            st.subheader("🔎 Тест вариантов запроса")
            st.caption(
                "Показывает варианты запроса, **все кандидаты до реранкинга** "
                "(пул = «Кандидатов на вариант» × кол-во вариантов – дубли) "
                "и финальные результаты после реранкинга."
            )
            _dbg_c1, _dbg_c2 = st.columns([4, 1])
            with _dbg_c1:
                _test_q = st.text_input(
                    "Введите запрос для проверки",
                    placeholder="например: ДМС, расходы на ремонт",
                    key="sr_query_expand_test",
                )
            with _dbg_c2:
                _dbg_topk = st.number_input(
                    "Финальный топ-K",
                    min_value=1, max_value=20, value=5,
                    help="Сколько результатов вернуть ПОСЛЕ реранкинга",
                    key="sr_debug_topk",
                )

            if st.button("🔬 Запустить тест кандидатов", key="sr_debug_btn", type="primary"):
                if _test_q.strip():
                    with st.spinner("⏳ Выполняем поиск..."):
                        try:
                            from core.advisor import debug_search_candidates as _dsc
                            _dbg = _dsc(_test_q.strip(), top_k=int(_dbg_topk))
                        except Exception as _dbe:
                            st.error(f"❌ {type(_dbe).__name__}: {_dbe}")
                            _dbg = None

                    if _dbg:
                        if _dbg.get("error"):
                            st.error(f"❌ {_dbg['error']}")
                        else:
                            # ── варианты запроса
                            st.markdown("##### 🔀 Варианты запроса")
                            for _vi, _vq in enumerate(_dbg["query_variants"], 1):
                                _vlabel = "🎯 оригинал" if _vi == 1 else f"🔁 синоним {_vi-1}"
                                st.code(f"{_vlabel}: {_vq}", language=None)

                            # ── пул ДО реранкинга
                            _pre = _dbg["pre_rerank"]
                            _cpv = _cands
                            _nv  = len(_dbg["query_variants"])
                            st.markdown(
                                f"##### 📥 Пул до реранкинга: **{len(_pre)}** уникальных кандидатов"
                                f"  <span style='color:grey;font-size:0.85em'>"
                                f"(настройка {_cpv} × {_nv} вар. → дедупликация)</span>",
                                unsafe_allow_html=True,
                            )
                            for _pi, _pc in enumerate(_pre, 1):
                                _pm  = _pc.get("meta") or {}
                                _inv = "🔵 vec" if _pc.get("in_vector") else ""
                                _inb = "🟤 bm25" if _pc.get("in_bm25") else ""
                                _rrf = f"RRF={_pc.get('score', 0):.5f}"
                                with st.expander(
                                    f"#{_pi} · {_pm.get('filename','?')} · "
                                    f"чанк {_pm.get('chunk_index','')} · {_rrf} {_inv} {_inb}",
                                    expanded=False,
                                ):
                                    st.text_area("", _pc.get("doc", "")[:600],
                                                 height=120, disabled=True,
                                                 key=f"dbg_pre_{_pi}")

                            # ── результаты ПОСЛЕ реранкинга
                            _post  = _dbg["post_rerank"]
                            _rused = "✅ CrossEncoder" if _dbg["reranker_used"] else "⚠️ реранкинг отключён"
                            st.markdown(
                                f"##### 🏆 После реранкинга: **{len(_post)}** результатов "
                                f"<span style='color:grey;font-size:0.85em'>({_rused})</span>",
                                unsafe_allow_html=True,
                            )
                            for _qi, _qc in enumerate(_post, 1):
                                _qm     = _qc.get("meta") or {}
                                _rrf2   = f"RRF={_qc.get('score', 0):.5f}"
                                _rscore = (
                                    f" | rerank={_qc.get('rerank_score', 0):.3f}"
                                    if _dbg["reranker_used"] else ""
                                )
                                with st.expander(
                                    f"#{_qi} · {_qm.get('filename','?')} · "
                                    f"чанк {_qm.get('chunk_index','')} · {_rrf2}{_rscore}",
                                    expanded=(_qi == 1),
                                ):
                                    st.text_area("", _qc.get("doc", "")[:600],
                                                 height=150, disabled=True,
                                                 key=f"dbg_post_{_qi}")

                            st.caption(f"⏱ Время: {_dbg['elapsed']} сек")
                else:
                    st.warning("⚠️ Введите запрос")

            st.divider()
            st.subheader("📄 Контекст для LLM")
            _ctx = st.slider(
                "Максимум символов контекста",
                min_value=2000, max_value=20000, step=1000,
                value=int(_sr_cur["context_max_chars"]),
                help="Сколько символов из найденных чанков передаётся LLM. Больше = полнее ответ, но медленнее генерация.",
                key="sr_context",
            )
            _tok_est = _ctx // 3
            st.caption(f"≈ {_tok_est} токенов контекста · при radius=1 и чанке 1750 симв. один источник = ~5250 символов")

            st.divider()
            _sc1, _sc2 = st.columns(2)
            with _sc1:
                if st.button("💾 Сохранить настройки поиска", type="primary", use_container_width=True, key="sr_save"):
                    _new_sr = {
                        "bm25_weight":        _bm25_w,
                        "candidates_per_var": _cands,
                        "context_max_chars":  _ctx,
                        "reranker_enabled":   _reranker_on,
                        "reranker_model":     _sel_model_id,
                    }
                    os.makedirs("config", exist_ok=True)
                    with open(_sr_file, "w", encoding="utf-8") as f:
                        json.dump(_new_sr, f, ensure_ascii=False, indent=2)
                    st.session_state["_search_settings"] = _new_sr
                    st.session_state["_sr_saved"] = True
                    st.rerun()
            with _sc2:
                if st.button("🔄 Сбросить к умолчаниям", use_container_width=True, key="sr_reset"):
                    if os.path.exists(_sr_file):
                        os.remove(_sr_file)
                    st.session_state.pop("_search_settings", None)
                    st.session_state["_sr_reset"] = True
                    st.rerun()
            if st.session_state.get("_sr_saved"):
                st.success("✅ Настройки поиска сохранены — применятся к следующему запросу.")
                del st.session_state["_sr_saved"]
            if st.session_state.get("_sr_reset"):
                st.info("🔄 Настройки сброшены к умолчаниям.")
                del st.session_state["_sr_reset"]

        with tab_prompts:
            st.header("Управление промптами")
            st.info("Изменения применяются сразу. Кэш LLM сбрасывается при сохранении.")
            PROMPTS_FILE_ADMIN = os.path.join("config","prompts.json")
            DEFAULT_PROMPTS_ADMIN = {
                "advisor_system": (
                    "Ты — эксперт по тарифному регулированию в РФ.\n"
                    "Отвечай ТОЛЬКО на русском языке, кратко, структурно и по существу.\n"
                    "ЗАПРЕЩЕНО писать 'Thinking Process', рассуждения или объяснения шагов.\n"
                    "Отвечай сразу итоговым ответом: списком, таблицей или чётким утверждением.\n"
                    "Основывайся на предоставленном контексте и законодательстве РФ.\n"
                    "Если информации в базе знаний недостаточно — честно скажи об этом.\n"
                    "Если в ответе есть сравнение данных, ставки или параметры — "
                    "оформи в виде Markdown-таблицы.\n"
                    "Пример:\n| Параметр | Значение | Ед. изм. |\n|---|---|---|\n| Тариф | 100.50 | руб./Гкал |"
                ),
                "advisor_user": "Вопрос пользователя: {query}\n\nКонтекст из документов:\n{context}\n\nОтвет:",
                # ── Анализатор заявок: суммаризатор ─────────────────────────
                "claim_map_system": (
                    "Ты эксперт по тарифному регулированию РФ. "
                    "Извлекаешь структурированные данные из фрагментов тарифных заявок. "
                    "Отвечаешь строго на русском языке, только по делу."
                ),
                "claim_map_user": (
                    "Это часть {i} из {total} тарифной заявки.\n"
                    "Извлеки ТОЛЬКО (если есть): статьи затрат (название, сумма тыс. руб., период), "
                    "приложенные документы, ссылки на НПА, организацию и период.\n"
                    "Формат: маркированный список. Без вступлений.\n\nФРАГМЕНТ:\n{chunk}"
                ),
                "claim_reduce_system": (
                    "Ты эксперт по тарифному регулированию РФ. "
                    "Составляешь структурированное резюме тарифной заявки. "
                    "Все цифры точно из источника. Отвечаешь на русском."
                ),
                "claim_reduce_user": (
                    "Собери единое резюме тарифной заявки (~{target_words} слов).\n\n"
                    "Разделы: ## Организация и период ## Статьи затрат "
                    "## Приложенные документы ## НПА ## Пробелы в обосновании\n\n"
                    "Устрани дублирование. Все цифры точно.\n\nДАННЫЕ:\n{combined}"
                ),
                # ── Анализатор заявок: риски ─────────────────────────────────
                "claim_risks_system": (
                    "Ты эксперт-аудитор по тарифному регулированию РФ. "
                    "Анализируешь тарифные заявки на риск отклонения регулятором. "
                    "Отвечаешь структурированно на русском языке. "
                    "Используй эмодзи 🔴 (высокий риск), 🟡 (средний), 🟢 (низкий)."
                ),
                "claim_risks_user": (
                    "Проанализируй тарифную заявку и составь отчёт о рисках.\n\n"
                    "## 1. Оценка комплектности документов\n"
                    "Перечисли какие документы упоминаются. "
                    "Укажи какие отсутствуют исходя из статей затрат.\n\n"
                    "## 2. Риски по статьям затрат\n"
                    "Для каждой значимой статьи: 🔴/🟡/🟢 Статья: сумма.\n"
                    "Основание риска и рекомендация.\n\n"
                    "## 3. Итоговая оценка\n"
                    "Общий уровень и топ-3 рекомендации.\n\n"
                    "ДАННЫЕ РАСЧЁТНОГО ФАЙЛА:\n{calc_context}\n\n"
                    "РЕЗЮМЕ ЗАЯВКИ:\n{summary}"
                ),
                # ── Прогнозист решений: классификация ────────────────────────
                "predictor_classify_system": "Тарифный эксперт РФ. JSON только.",
                "predictor_classify_user": (
                    "Статья: {article_name}\n"
                    "{justification_line}"
                    "\nФрагмент:\n{chunk}\n\n"
                    "Решение регулятора по статье: positive/negative/neutral?\n"
                    "positive=включена, negative=снижена/отклонена, neutral=без решения/не по теме\n"
                    'JSON: {{"decision":"?","quote":"до 100 симв.","reason":"до 80 симв."}}'
                ),
            }
            if os.path.exists(PROMPTS_FILE_ADMIN):
                try:
                    with open(PROMPTS_FILE_ADMIN,'r',encoding='utf-8') as f:
                        current_prompts = {**DEFAULT_PROMPTS_ADMIN, **json.load(f)}
                except Exception:
                    current_prompts = dict(DEFAULT_PROMPTS_ADMIN)
            else:
                current_prompts = dict(DEFAULT_PROMPTS_ADMIN)

            # ── Выбор раздела промптов ────────────────────────────────────
            prompt_section = st.radio(
                "Раздел",
                ["Советчик", "Анализатор заявок", "Прогнозист решений", "Протокольщик"],
                horizontal=True,
                key="prompt_section_radio",
            )
            st.divider()

            if prompt_section == "Советчик":
                with st.expander("ℹ️ Переменные"):
                    st.markdown("**Пользовательский промпт:** `{query}` — вопрос, `{context}` — чанки из RAG")
                col1, col2 = st.columns(2)
                with col1:
                    st.caption("Загружен из: " + ("📁 prompts.json" if os.path.exists(PROMPTS_FILE_ADMIN) else "⚙️ дефолт"))
                with col2:
                    is_mod = (current_prompts.get("advisor_system") != DEFAULT_PROMPTS_ADMIN["advisor_system"] or
                              current_prompts.get("advisor_user")   != DEFAULT_PROMPTS_ADMIN["advisor_user"])
                    if is_mod: st.warning("✏️ Промпты изменены")
                    else:      st.success("✅ Дефолтные промпты")
                st.divider()
                new_system = st.text_area("Системный промпт", value=current_prompts.get("advisor_system", DEFAULT_PROMPTS_ADMIN["advisor_system"]), height=280, key="prompt_advisor_system")
                st.divider()
                new_user   = st.text_area("Пользовательский промпт", value=current_prompts.get("advisor_user", DEFAULT_PROMPTS_ADMIN["advisor_user"]), height=120, key="prompt_advisor_user")
                if "{query}" not in new_user or "{context}" not in new_user:
                    st.error("⚠️ Промпт должен содержать {query} и {context}")
                else:
                    st.caption("✅ Переменные присутствуют")
                st.divider()
                col1, col2, col3 = st.columns([2, 2, 1])
                with col1:
                    if st.button("💾 Сохранить промпты", type="primary", use_container_width=True, key="save_prompts_btn"):
                        if "{query}" in new_user and "{context}" in new_user:
                            os.makedirs(os.path.dirname(PROMPTS_FILE_ADMIN), exist_ok=True)
                            with open(PROMPTS_FILE_ADMIN, 'w', encoding='utf-8') as f:
                                json.dump({**current_prompts, "advisor_system": new_system, "advisor_user": new_user,
                                           "updated_at": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
                            try:
                                from core.advisor import _llm_cache, save_llm_cache
                                _llm_cache.clear(); save_llm_cache()
                                st.success("✅ Промпты сохранены. Кэш сброшен.")
                            except Exception:
                                st.success("✅ Промпты сохранены.")
                            st.rerun()
                        else:
                            st.error("❌ Исправьте ошибки")
                with col2:
                    if st.button("🔄 Сбросить к дефолтным", use_container_width=True, key="reset_prompts_btn"):
                        st.session_state._confirm_reset_prompts = True
                    @st.dialog("⚠️ Сброс промптов")
                    def confirm_reset_prompts_dialog():
                        st.warning("Промпты вернутся к дефолтным значениям.")
                        ca, cb = st.columns(2)
                        with ca:
                            if st.button("🗑️ Да, сбросить", type="primary", use_container_width=True, key="dialog_confirm_reset"):
                                if os.path.exists(PROMPTS_FILE_ADMIN):
                                    try:
                                        with open(PROMPTS_FILE_ADMIN, 'r', encoding='utf-8') as f: saved = json.load(f)
                                        saved.pop("advisor_system", None); saved.pop("advisor_user", None)
                                        with open(PROMPTS_FILE_ADMIN, 'w', encoding='utf-8') as f: json.dump(saved, f, ensure_ascii=False, indent=2)
                                    except Exception: pass
                                st.session_state._confirm_reset_prompts = False; st.rerun()
                        with cb:
                            if st.button("← Отмена", use_container_width=True, key="dialog_cancel_reset"):
                                st.session_state._confirm_reset_prompts = False; st.rerun()
                    if st.session_state.get("_confirm_reset_prompts"):
                        confirm_reset_prompts_dialog()
                with col3:
                    prompts_json = json.dumps({"advisor_system": new_system, "advisor_user": new_user}, ensure_ascii=False, indent=2)
                    st.download_button("📥 Скачать", data=prompts_json.encode("utf-8"),
                                       file_name="prompts_backup.json", mime="application/json",
                                       use_container_width=True, key="download_prompts_btn")

            elif prompt_section == "Анализатор заявок":
                st.caption("Промпты суммаризатора (Map-Reduce) и анализа рисков")
                with st.expander("ℹ️ Переменные анализатора"):
                    st.markdown(
                        "**MAP:** `{i}` — номер части, `{total}` — всего частей, `{chunk}` — текст фрагмента\n\n"
                        "**REDUCE:** `{target_words}` — целевой объём, `{combined}` — результаты MAP\n\n"
                        "**РИСКИ:** `{calc_context}` — данные расчётного файла, `{summary}` — резюме заявки"
                    )

                st.markdown("**Суммаризатор MAP — системный промпт**")
                new_claim_map_sys = st.text_area(
                    "", value=current_prompts.get("claim_map_system", DEFAULT_PROMPTS_ADMIN["claim_map_system"]),
                    height=100, key="prompt_claim_map_sys", label_visibility="collapsed"
                )
                st.markdown("**Суммаризатор MAP — пользовательский промпт**")
                new_claim_map_usr = st.text_area(
                    "", value=current_prompts.get("claim_map_user", DEFAULT_PROMPTS_ADMIN["claim_map_user"]),
                    height=120, key="prompt_claim_map_usr", label_visibility="collapsed"
                )
                for v, name in [("{i}", "MAP user"), ("{total}", "MAP user"), ("{chunk}", "MAP user")]:
                    if v not in new_claim_map_usr:
                        st.error(f"⚠️ {name} промпт должен содержать {v}")

                st.divider()
                st.markdown("**Суммаризатор REDUCE — системный промпт**")
                new_claim_red_sys = st.text_area(
                    "", value=current_prompts.get("claim_reduce_system", DEFAULT_PROMPTS_ADMIN["claim_reduce_system"]),
                    height=80, key="prompt_claim_red_sys", label_visibility="collapsed"
                )
                st.markdown("**Суммаризатор REDUCE — пользовательский промпт**")
                new_claim_red_usr = st.text_area(
                    "", value=current_prompts.get("claim_reduce_user", DEFAULT_PROMPTS_ADMIN["claim_reduce_user"]),
                    height=120, key="prompt_claim_red_usr", label_visibility="collapsed"
                )

                st.divider()
                st.markdown("**Анализ рисков — системный промпт**")
                new_claim_risk_sys = st.text_area(
                    "", value=current_prompts.get("claim_risks_system", DEFAULT_PROMPTS_ADMIN["claim_risks_system"]),
                    height=100, key="prompt_claim_risk_sys", label_visibility="collapsed"
                )
                st.markdown("**Анализ рисков — пользовательский промпт**")
                new_claim_risk_usr = st.text_area(
                    "", value=current_prompts.get("claim_risks_user", DEFAULT_PROMPTS_ADMIN["claim_risks_user"]),
                    height=180, key="prompt_claim_risk_usr", label_visibility="collapsed"
                )
                for v, name in [("{calc_context}", "Риски user"), ("{summary}", "Риски user")]:
                    if v not in new_claim_risk_usr:
                        st.error(f"⚠️ {name} промпт должен содержать {v}")

                st.divider()
                col1c, col2c = st.columns([2, 1])
                with col1c:
                    if st.button("💾 Сохранить промпты анализатора", type="primary",
                                 use_container_width=True, key="save_claim_prompts_btn"):
                        os.makedirs(os.path.dirname(PROMPTS_FILE_ADMIN), exist_ok=True)
                        updated = {
                            **current_prompts,
                            "claim_map_system":    new_claim_map_sys,
                            "claim_map_user":      new_claim_map_usr,
                            "claim_reduce_system": new_claim_red_sys,
                            "claim_reduce_user":   new_claim_red_usr,
                            "claim_risks_system":  new_claim_risk_sys,
                            "claim_risks_user":    new_claim_risk_usr,
                            "updated_at":          datetime.now().isoformat(),
                        }
                        with open(PROMPTS_FILE_ADMIN, "w", encoding="utf-8") as f:
                            json.dump(updated, f, ensure_ascii=False, indent=2)
                        st.success("✅ Промпты анализатора сохранены.")
                        st.rerun()
                with col2c:
                    claim_prompts_json = json.dumps({
                        "claim_map_system":    new_claim_map_sys,
                        "claim_map_user":      new_claim_map_usr,
                        "claim_reduce_system": new_claim_red_sys,
                        "claim_reduce_user":   new_claim_red_usr,
                        "claim_risks_system":  new_claim_risk_sys,
                        "claim_risks_user":    new_claim_risk_usr,
                    }, ensure_ascii=False, indent=2)
                    st.download_button("📥 Скачать", data=claim_prompts_json.encode("utf-8"),
                                       file_name="claim_prompts_backup.json",
                                       mime="application/json",
                                       use_container_width=True, key="dl_claim_prompts_btn")

            elif prompt_section == "Прогнозист решений":
                st.caption("Промпты классификации чанков протоколов регулятора")
                with st.expander("ℹ️ Переменные прогнозиста"):
                    st.markdown(
                        "**Системный:** без переменных — короткий системный контекст для модели\n\n"
                        "**Пользовательский:** `{article_name}` — статья затрат, "
                        "`{justification_line}` — строка обоснования (пустая если не указано), "
                        "`{chunk}` — фрагмент протокола"
                    )

                st.markdown("**Системный промпт классификации**")
                new_pred_cls_sys = st.text_area(
                    "", value=current_prompts.get("predictor_classify_system",
                        DEFAULT_PROMPTS_ADMIN["predictor_classify_system"]),
                    height=80, key="prompt_pred_cls_sys", label_visibility="collapsed",
                )

                st.markdown("**Пользовательский промпт классификации**")
                new_pred_cls_usr = st.text_area(
                    "", value=current_prompts.get("predictor_classify_user",
                        DEFAULT_PROMPTS_ADMIN["predictor_classify_user"]),
                    height=200, key="prompt_pred_cls_usr", label_visibility="collapsed",
                )
                for v, name in [("{article_name}", "User"), ("{chunk}", "User")]:
                    if v not in new_pred_cls_usr:
                        st.error(f"⚠️ {name} промпт должен содержать {v}")

                st.divider()
                _pp1, _pp2 = st.columns([2, 1])
                with _pp1:
                    if st.button("💾 Сохранить промпты прогнозиста", type="primary",
                                 use_container_width=True, key="save_pred_prompts_btn"):
                        if "{article_name}" in new_pred_cls_usr and "{chunk}" in new_pred_cls_usr:
                            os.makedirs(os.path.dirname(PROMPTS_FILE_ADMIN), exist_ok=True)
                            updated = {
                                **current_prompts,
                                "predictor_classify_system": new_pred_cls_sys,
                                "predictor_classify_user":   new_pred_cls_usr,
                                "updated_at": datetime.now().isoformat(),
                            }
                            with open(PROMPTS_FILE_ADMIN, "w", encoding="utf-8") as f:
                                json.dump(updated, f, ensure_ascii=False, indent=2)
                            st.success("✅ Промпты прогнозиста сохранены.")
                            st.rerun()
                        else:
                            st.error("❌ Исправьте ошибки в промпте")
                with _pp2:
                    pred_prompts_json = json.dumps({
                        "predictor_classify_system": new_pred_cls_sys,
                        "predictor_classify_user":   new_pred_cls_usr,
                    }, ensure_ascii=False, indent=2)
                    st.download_button("📥 Скачать", data=pred_prompts_json.encode("utf-8"),
                                       file_name="predictor_prompts_backup.json",
                                       mime="application/json",
                                       use_container_width=True, key="dl_pred_prompts_btn")

            elif prompt_section == "Протокольщик":
                # ── Конфиг промптов протокольщика ────────────────────────────
                _PROTO_PROMPTS_FILE_ADMIN = os.path.join("config", "protocol_prompts.json")

                _PROTO_DEFAULT_STRUCTURE = (
                    "1. Дата и время встречи\n"
                    "2. Присутствовали\n"
                    "3. Повестка дня\n"
                    "4. Обсуждаемые вопросы\n"
                    "5. Принятые решения\n"
                    "6. Поручения (кто, что, срок)\n"
                    "7. Следующая встреча"
                )
                _PROTO_DEFAULT_SYSTEM = (
                    "Ты профессиональный секретарь. Составляй официальные протоколы встреч. "
                    "Отвечай только на русском языке. "
                    "Строго следуй указанной структуре — не добавляй разделов, не пропускай указанные."
                )
                _PROTO_DEFAULT_USER_TMPL = (
                    "Составь официальный протокол встречи на русском языке.\n\n"
                    "РЕКВИЗИТЫ:\n"
                    "Название:     {meeting_name}\n"
                    "Организация:  {organization}\n"
                    "Дата:         {meeting_date}\n"
                    "Время:        {meeting_time}\n"
                    "{attendees_block}\n\n"
                    "ОБЯЗАТЕЛЬНАЯ СТРУКТУРА ПРОТОКОЛА:\n"
                    "Ниже перечислены все разделы. Ты ОБЯЗАН включить каждый из них в строго указанном порядке.\n"
                    "Не добавляй разделов сверх списка. Не объединяй разделы. Не меняй порядок.\n\n"
                    "{structure}\n\n"
                    "УРОВЕНЬ ДЕТАЛИЗАЦИИ: {detail_level} — {detail_caption}\n\n"
                    "ТЕКСТ / РАСШИФРОВКА ВСТРЕЧИ:\n"
                    "{source_text}\n\n"
                    "ТРЕБОВАНИЯ К ОФОРМЛЕНИЮ:\n"
                    "- Оформи каждый раздел как заголовок, выделенный на отдельной строке\n"
                    "- Выдели ключевые решения и поручения отдельно\n"
                    "- Укажи ответственных и сроки исполнения\n"
                    "- Официально-деловой стиль, без лишних слов\n"
                    "- Если информации по разделу нет — пиши «Не указано»\n"
                    "- Не добавляй советов, рекомендаций и комментариев от себя\n\n"
                    "ПРОТОКОЛ:"
                )
                _proto_prompt_defaults = {
                    "system_prompt":        _PROTO_DEFAULT_SYSTEM,
                    "user_prompt_template": _PROTO_DEFAULT_USER_TMPL,
                    "default_structure":    _PROTO_DEFAULT_STRUCTURE,
                }

                # Загружаем текущие значения
                if os.path.exists(_PROTO_PROMPTS_FILE_ADMIN):
                    try:
                        with open(_PROTO_PROMPTS_FILE_ADMIN, "r", encoding="utf-8") as _f:
                            _proto_cur = {**_proto_prompt_defaults, **json.load(_f)}
                    except Exception:
                        _proto_cur = dict(_proto_prompt_defaults)
                else:
                    _proto_cur = dict(_proto_prompt_defaults)

                _proto_is_mod = _proto_cur != _proto_prompt_defaults
                st.caption(
                    "Загружен из: " + (
                        "📁 protocol_prompts.json" if os.path.exists(_PROTO_PROMPTS_FILE_ADMIN)
                        else "⚙️ заводские значения"
                    )
                )
                if _proto_is_mod:
                    st.warning("✏️ Промпты изменены относительно заводских")
                else:
                    st.success("✅ Заводские промпты")

                st.divider()

                # ── Справка по переменным ─────────────────────────────────────
                with st.expander("ℹ️ Переменные User Prompt шаблона", expanded=False):
                    st.markdown(
                        "| Переменная | Описание |\n|---|---|\n"
                        "| `{meeting_name}` | Название встречи |\n"
                        "| `{organization}` | Организация |\n"
                        "| `{meeting_date}` | Дата встречи |\n"
                        "| `{meeting_time}` | Время встречи |\n"
                        "| `{attendees_block}` | Список участников (готовая строка) |\n"
                        "| `{structure}` | **Структура протокола** — подставляется из шага 3 |\n"
                        "| `{detail_level}` | Уровень детализации (краткий/средний/подробный) |\n"
                        "| `{detail_caption}` | Описание уровня детализации |\n"
                        "| `{source_text}` | **Расшифровка / текст встречи** |"
                    )

                # ── System Prompt ─────────────────────────────────────────────
                st.markdown("**Системный промпт**")
                st.caption("Задаёт роль и базовое поведение модели.")
                _proto_new_sys = st.text_area(
                    "", value=_proto_cur["system_prompt"],
                    height=120, key="prompt_proto_system", label_visibility="collapsed",
                )

                st.divider()

                # ── User Prompt ───────────────────────────────────────────────
                st.markdown("**User Prompt (шаблон)**")
                st.caption(
                    "Основной промпт. `{structure}` — обязательная переменная: "
                    "именно через неё структура из шага 3 попадает в запрос к модели."
                )
                _proto_new_usr = st.text_area(
                    "", value=_proto_cur["user_prompt_template"],
                    height=480, key="prompt_proto_user", label_visibility="collapsed",
                )
                _proto_required = ["{structure}", "{source_text}", "{meeting_name}"]
                _proto_missing = [v for v in _proto_required if v not in _proto_new_usr]
                if _proto_missing:
                    st.error(f"❌ Отсутствуют обязательные переменные: {', '.join(_proto_missing)}")
                else:
                    st.caption("✅ Все обязательные переменные присутствуют")

                st.divider()

                # ── Структура по умолчанию ────────────────────────────────────
                st.markdown("**Структура протокола по умолчанию**")
                st.caption(
                    "Заполняет поле «Структура» на шаге 3 при каждом открытии протокольщика. "
                    "Пользователь может изменить её под конкретное совещание."
                )
                _proto_new_struct = st.text_area(
                    "", value=_proto_cur["default_structure"],
                    height=200, key="prompt_proto_structure", label_visibility="collapsed",
                )

                st.divider()

                # ── Превью финального промпта ─────────────────────────────────
                if st.button("👁 Предпросмотр финального промпта", key="proto_prompt_preview_btn"):
                    st.session_state["_proto_prompt_preview"] = True
                if st.session_state.get("_proto_prompt_preview"):
                    try:
                        _proto_preview = _proto_new_usr.format(
                            meeting_name    = "Совещание по тарифам",
                            organization    = "АО Ромашка",
                            meeting_date    = "2026-05-31",
                            meeting_time    = "10:00",
                            attendees_block = "Присутствовали:\n  • Иванов И.И. — Директор — АО Ромашка",
                            structure       = _proto_new_struct,
                            detail_level    = "средний",
                            detail_caption  = "Факты + важные детали, оптимальный объём",
                            source_text     = "[← здесь будет расшифровка встречи до 14 000 символов]",
                        )
                        st.code(_proto_preview, language=None)
                    except KeyError as _pke:
                        st.error(f"Неизвестная переменная: {{{_pke}}}. Проверьте имена переменных.")
                    if st.button("Скрыть превью", key="proto_prompt_preview_hide"):
                        st.session_state["_proto_prompt_preview"] = False
                        st.rerun()

                st.divider()

                # ── Кнопки сохранения / сброса / скачивания ──────────────────
                _pb1, _pb2, _pb3 = st.columns([2, 2, 1])
                with _pb1:
                    if st.button("💾 Сохранить промпты", type="primary",
                                 use_container_width=True, key="save_proto_prompts_btn"):
                        if _proto_missing:
                            st.error(f"❌ Исправьте ошибки перед сохранением")
                        else:
                            _proto_to_save = {
                                "system_prompt":        _proto_new_sys,
                                "user_prompt_template": _proto_new_usr,
                                "default_structure":    _proto_new_struct,
                                "updated_at":           datetime.now().isoformat(),
                            }
                            os.makedirs("config", exist_ok=True)
                            with open(_PROTO_PROMPTS_FILE_ADMIN, "w", encoding="utf-8") as _f:
                                json.dump(_proto_to_save, _f, ensure_ascii=False, indent=2)
                            st.success("✅ Промпты протокольщика сохранены. Применятся при следующей генерации.")
                            st.rerun()

                with _pb2:
                    if st.button("🔄 Сбросить к заводским", use_container_width=True,
                                 key="reset_proto_prompts_btn"):
                        st.session_state["_confirm_reset_proto_prompts"] = True
                    if st.session_state.get("_confirm_reset_proto_prompts"):
                        @st.dialog("⚠️ Сброс промптов протокольщика")
                        def _confirm_reset_proto_prompts_dialog():
                            st.warning("Промпты вернутся к заводским значениям. Файл protocol_prompts.json будет удалён.")
                            _rpa, _rpb = st.columns(2)
                            with _rpa:
                                if st.button("🗑️ Да, сбросить", type="primary", use_container_width=True,
                                             key="dialog_confirm_reset_proto"):
                                    if os.path.exists(_PROTO_PROMPTS_FILE_ADMIN):
                                        os.remove(_PROTO_PROMPTS_FILE_ADMIN)
                                    for _k in ("prompt_proto_system", "prompt_proto_user", "prompt_proto_structure"):
                                        st.session_state.pop(_k, None)
                                    st.session_state["_confirm_reset_proto_prompts"] = False
                                    st.rerun()
                            with _rpb:
                                if st.button("← Отмена", use_container_width=True,
                                             key="dialog_cancel_reset_proto"):
                                    st.session_state["_confirm_reset_proto_prompts"] = False
                                    st.rerun()
                        _confirm_reset_proto_prompts_dialog()

                with _pb3:
                    _proto_dl_json = json.dumps({
                        "system_prompt":        _proto_new_sys,
                        "user_prompt_template": _proto_new_usr,
                        "default_structure":    _proto_new_struct,
                    }, ensure_ascii=False, indent=2)
                    st.download_button(
                        "📥 Скачать",
                        data=_proto_dl_json.encode("utf-8"),
                        file_name="protocol_prompts_backup.json",
                        mime="application/json",
                        use_container_width=True,
                        key="dl_proto_prompts_btn",
                    )



        with tab_predictor:
            show_predictor_tab()



        with tab_claim_rag:
            st.header("База знаний Анализатора заявок")
            st.info(
                "Выберите документы из базы знаний, по которым RAG будет искать НПА при анализе заявок. "
                "Если ничего не выбрано — поиск по всей базе знаний."
            )

            _CA_CFG = os.path.join("config", "claim_analyzer_config.json")
            def _lcfg():
                try:
                    with open(_CA_CFG, "r", encoding="utf-8") as _f: return json.load(_f)
                except Exception: return {}
            def _scfg(c):
                os.makedirs("config", exist_ok=True)
                with open(_CA_CFG, "w", encoding="utf-8") as _f: json.dump(c, _f, ensure_ascii=False, indent=2)

            _cfg = _lcfg()
            _saved_fnames = set(_cfg.get("rag_docs", []))

            # Индекс чанков по файлам (своя независимая копия для этой вкладки)
            _chroma_index = {}
            try:
                import chromadb as _chromadb
                _vector_db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "vector_db")
                _chroma_client = _chromadb.PersistentClient(path=_vector_db_path)
                _collection = _chroma_client.get_collection(name="tariff_docs")
                _results = _collection.get(include=["metadatas"])
                for meta in _results["metadatas"]:
                    fn = meta.get("filename", "")
                    if not fn: continue
                    if fn not in _chroma_index:
                        _chroma_index[fn] = {"chunks": 0, "indexed_at": meta.get("indexed_at", "")[:10] if meta.get("indexed_at") else "—"}
                    _chroma_index[fn]["chunks"] += 1
            except Exception:
                pass

            # Фильтры
            _rf1, _rf2, _rf3 = st.columns([2, 2, 2])
            _rf_name   = _rf1.text_input("Фильтр по названию", key="crag_fn",
                                          placeholder="Название файла...", label_visibility="collapsed")
            _rf_sphere = _rf2.selectbox("Сфера", ["— Все —"] + SPHERES,
                                         key="crag_sp", label_visibility="collapsed")
            _rf_only   = _rf3.checkbox("Только выбранные", key="crag_only")

            # Собираем все индексированные файлы
            _all_docs = []
            for _cat_lbl, _folder in CATEGORY_FOLDERS.items():
                _fpath = os.path.join("data", "raw", _folder)
                if not os.path.exists(_fpath): continue
                for _fn in sorted(os.listdir(_fpath)):
                    if _fn.startswith(".") or _fn.endswith(".indexed"): continue
                    _ci = _chroma_index.get(_fn, {})
                    if _ci.get("chunks", 0) == 0: continue
                    _sph = spheres_map.get(_fn, [])
                    _all_docs.append({
                        "fname":   _fn,
                        "cat":     _cat_lbl,
                        "spheres": _sph,
                        "chunks":  _ci.get("chunks", 0),
                        "checked": _fn in _saved_fnames,
                    })

            # Применяем фильтры
            _docs_view = _all_docs[:]
            if _rf_name.strip():
                _docs_view = [d for d in _docs_view if _rf_name.lower() in d["fname"].lower()]
            if _rf_sphere != "— Все —":
                _docs_view = [d for d in _docs_view if _rf_sphere in d["spheres"]]
            if _rf_only:
                _docs_view = [d for d in _docs_view if d["checked"]]

            # Кнопки быстрого выбора
            _btn1, _btn2, _btn3, _info_col = st.columns([1, 1, 1, 3])
            if _btn1.button("✅ Выбрать видимые", key="crag_sel_all", use_container_width=True):
                _vis_fnames = {d["fname"] for d in _docs_view}
                _new_sel = _saved_fnames | _vis_fnames
                _scfg({**_cfg, "rag_docs": list(_new_sel)}); st.rerun()
            if _btn2.button("☐ Снять видимые", key="crag_sel_none", use_container_width=True):
                _vis_fnames = {d["fname"] for d in _docs_view}
                _new_sel = _saved_fnames - _vis_fnames
                _scfg({**_cfg, "rag_docs": list(_new_sel)}); st.rerun()
            if _btn3.button("💾 Сохранить", key="crag_save",
                            type="primary", use_container_width=True):
                _new_sel = {d["fname"] for d in _all_docs if d["checked"]}
                _scfg({**_cfg, "rag_docs": list(_new_sel)})
                st.success(f"✅ Сохранено: {len(_new_sel)} документов"); st.rerun()
            _n_sel = len(_saved_fnames & {d["fname"] for d in _all_docs})
            _info_col.info(f"Выбрано: **{_n_sel}** из {len(_all_docs)} · показано: {len(_docs_view)}")

            if not _docs_view:
                st.info("Документов не найдено. Индексируйте документы во вкладке «Документы».")
            else:
                st.divider()
                _hc = st.columns([0.5, 4, 2.5, 2, 1])
                for _col, _lbl in zip(_hc, ["✓", "Документ", "Сферы", "Категория", "Чанки"]):
                    _col.markdown(f"**{_lbl}**")
                st.divider()
                for _d in _docs_view:
                    _row = st.columns([0.5, 4, 2.5, 2, 1])
                    _chk = _row[0].checkbox("", value=_d["fname"] in _saved_fnames,
                                             key=f"crag_{_d['fname']}", label_visibility="collapsed")
                    if _chk and _d["fname"] not in _saved_fnames:
                        _saved_fnames.add(_d["fname"])
                        _scfg({**_lcfg(), "rag_docs": list(_saved_fnames)})
                    elif not _chk and _d["fname"] in _saved_fnames:
                        _saved_fnames.discard(_d["fname"])
                        _scfg({**_lcfg(), "rag_docs": list(_saved_fnames)})
                    _row[1].markdown(f"**{_d['fname']}**")
                    _row[2].caption(", ".join(_d["spheres"]) if _d["spheres"] else "—")
                    _row[3].caption(_d["cat"])
                    _row[4].caption(str(_d["chunks"]))

        with tab_expertise:
            show_documents_panel()

        with tab_expertise_chunking:
            show_expertise_chunking_panel()

if __name__ == "__main__":
    pass