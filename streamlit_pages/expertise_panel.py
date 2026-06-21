"""
Вкладка «Протоколы / Экспертные заключения» — единый UI-каркас для двух типов
документов (doc_type = "protocol" | "expertise"), с общей таблицей, фильтрами
и загрузкой (одиночной и массовой).

ВАЖНО ПРО ИЗОЛЯЦИЮ БЭКЕНДА:
- doc_type == "expertise" -> чанкер core/expertise_chunker.py -> коллекция Chroma "expertise_docs"
- doc_type == "protocol"  -> ЗАГЛУШКА. Существующий протокольный пайплайн (core/indexer.py,
  collection_name="protocols", chunking_mode="protocol_articles") НЕ переиспользуется и
  НЕ вызывается отсюда. Кнопки индексации для protocol задизейблены до отдельного решения.

Реестр документов — единый файл data/documents_registry.json с полем doc_type,
используется ОБОИМИ типами документов (для protocol пока просто не наполняется).
Реестр — единственный источник данных для таблицы/фильтров; Chroma не читается
на каждый rerender (важно при 1000+ файлах).
"""

from __future__ import annotations

import os
import json
import glob
from datetime import datetime

import streamlit as st
import pandas as pd


# ──────────────────────────────────────────────────────────────────────────
# Константы и справочники
# ──────────────────────────────────────────────────────────────────────────

REGISTRY_PATH = os.path.join("data", "documents_registry.json")

DOC_TYPES = {
    "expertise": "📄 Экспертное заключение",
    "protocol":  "📋 Протокол",
}

SPHERES = [
    "Теплоснабжение",
    "Водоснабжение",
    "Водоотведение",
    "ТКО",
    "Электроэнергетика",
    "Газоснабжение",
    "Иное",
]

METHODS = [
    "Индексация",
    "ЭОЗ",
    "RAB",
    "Метод экономически обоснованных расходов",
    "метод_не_определён",
]

PAGE_SIZE = 50  # строк таблицы на страницу


# ──────────────────────────────────────────────────────────────────────────
# Реестр документов (единый JSON, общий для protocol/expertise)
# ──────────────────────────────────────────────────────────────────────────

def _load_registry() -> dict:
    """
    Возвращает {fname: {doc_type, region, sphere, year, method, organization,
    protocol_num, collection, chunks, indexed_at, status, error_msg}}
    """
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_registry(reg: dict) -> None:
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)


def _upsert_registry_entry(fname: str, entry: dict) -> None:
    reg = _load_registry()
    reg[fname] = {**reg.get(fname, {}), **entry}
    _save_registry(reg)


def _registry_to_df(reg: dict) -> pd.DataFrame:
    if not reg:
        return pd.DataFrame(columns=[
            "fname", "doc_type", "region", "sphere", "year", "method",
            "organization", "protocol_num", "collection", "chunks",
            "indexed_at", "status", "error_msg",
        ])
    rows = []
    for fname, meta in reg.items():
        rows.append({"fname": fname, **meta})
    df = pd.DataFrame(rows)
    # Гарантируем наличие всех колонок даже если в реестре их где-то не было
    for col in ["doc_type", "region", "sphere", "year", "method",
                "organization", "protocol_num", "collection", "chunks",
                "indexed_at", "status", "error_msg"]:
        if col not in df.columns:
            df[col] = None
    return df


# ──────────────────────────────────────────────────────────────────────────
# Парсинг атрибутов из имени файла
# Формат: Регион_Сфера_Год_Организация_Метод_Описание_pdf
# ──────────────────────────────────────────────────────────────────────────

def parse_attrs_from_filename(fname: str) -> dict:
    """
    Примитивный, надёжный парсер: разбивает имя файла по "_" и пытается
    угадать сферу/год/метод по известным токенам. Не бросает исключений —
    при невозможности распознать поле возвращает "не_определён"/"не определена".
    Реальную точную грамматику имени файла уточним на реальных данных
    (см. uploaded примеры batch-импорта протоколов).
    """
    base = os.path.splitext(fname)[0]
    parts = base.split("_")

    sphere_map = {
        "teplo": "Теплоснабжение",
        "voda": "Водоснабжение",
        "vodootved": "Водоотведение",
        "tko": "ТКО",
        "elektro": "Электроэнергетика",
        "gaz": "Газоснабжение",
    }
    method_map = {
        "indx": "Индексация",
        "eoz": "ЭОЗ",
        "rab": "RAB",
    }

    region = parts[0] if len(parts) > 0 else "регион_не_определён"
    sphere = "сфера_не_определена"
    year = "год_не_определён"
    method = "метод_не_определён"
    organization = "организация_не_определена"

    for p in parts[1:]:
        low = p.lower()
        if low in sphere_map:
            sphere = sphere_map[low]
        elif low in method_map:
            method = method_map[low]
        elif _looks_like_year(p):
            year = p

    # Организация — между сферой/годом и методом (эвристика; уточняем позже)
    try:
        # ищем индекс сферы и индекс метода, берём всё что между годом и методом
        idx_year = next((i for i, p in enumerate(parts) if _looks_like_year(p)), None)
        idx_method = next((i for i, p in enumerate(parts) if p.lower() in method_map), None)
        if idx_year is not None and idx_method is not None and idx_method > idx_year + 1:
            organization = "_".join(parts[idx_year + 1: idx_method]).strip("_") or organization
    except Exception:
        pass

    return {
        "region": region,
        "sphere": sphere,
        "year": year,
        "method": method,
        "organization": organization,
        "protocol_num": "номер_не_определён",
    }


def _looks_like_year(token: str) -> bool:
    t = token.strip()
    if len(t) == 4 and t.isdigit():
        return True
    if len(t) == 9 and t[:4].isdigit() and t[4] == "-" and t[5:].isdigit():
        return True  # "2025-2029"
    return False


# ──────────────────────────────────────────────────────────────────────────
# Индексация
# ──────────────────────────────────────────────────────────────────────────

def index_expertise_file(fpath: str, fname: str, attrs: dict) -> dict:
    """
    Реальная индексация через core/expertise_chunker.py ->
    коллекция Chroma "expertise_docs". Возвращает результат для записи
    в реестр.
    """
    from core.expertise_chunker import index_expertise_file as _chunker_index, ExpertiseDocAttrs

    doc_attrs = ExpertiseDocAttrs(
        region=attrs.get("region", "регион_не_определён"),
        sphere=attrs.get("sphere", "сфера_не_определена"),
        year=attrs.get("year", "год_не_определён"),
        method=attrs.get("method", "метод_не_определён"),
        organization=attrs.get("organization", "организация_не_определена"),
        protocol_num=attrs.get("protocol_num", "номер_не_определён"),
        protocol_date=attrs.get("protocol_date", ""),
        doc_type="expertise",
    )
    result = _chunker_index(fpath, doc_attrs)

    # Сбрасываем BM25-индекс гибридного поиска предиктора — иначе он
    # продолжит работать со старым числом чанков, не зная о новом файле,
    # до следующего изменения количества документов.
    try:
        from streamlit_pages.predictor import invalidate_expertise_hybrid_retriever
        invalidate_expertise_hybrid_retriever()
    except Exception:
        pass

    return result


def index_protocol_file(fpath: str, fname: str, attrs: dict) -> dict:
    """
    ЗАГЛУШКА. Протокольный чанкер ещё не подключён к этому единому UI.
    Намеренно не вызывает core/indexer.py — изоляция от существующего пайплайна.
    """
    return {
        "chunks": 0,
        "collection": "protocols",
        "status": "not_implemented",
        "error_msg": "Индексация протоколов через этот раздел пока не подключена",
        "indexed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


# ──────────────────────────────────────────────────────────────────────────
# UI: основная функция вкладки
# ──────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────
# Удаление и переиндексация
# ──────────────────────────────────────────────────────────────────────────

def _remove_document(fname: str, doc_type: str) -> tuple[bool, str]:
    """
    Полное удаление документа: чанки из Chroma + запись из реестра + файл
    с диска (data/raw/{doc_type}_docs/{fname}).
    Возвращает (success, message).
    """
    errors = []

    # 1) Чанки из Chroma
    if doc_type == "expertise":
        try:
            from core.expertise_chunker import remove_file_from_expertise
            remove_file_from_expertise(fname)
            from streamlit_pages.predictor import invalidate_expertise_hybrid_retriever
            invalidate_expertise_hybrid_retriever()
        except Exception as e:
            errors.append(f"Chroma: {e}")
    # doc_type == "protocol" — индексация ещё не подключена, чанков в Chroma нет

    # 2) Файл с диска
    fpath = os.path.join("data", "raw", f"{doc_type}_docs", fname)
    try:
        if os.path.exists(fpath):
            os.remove(fpath)
    except Exception as e:
        errors.append(f"Файл на диске: {e}")

    # 3) Запись из реестра
    try:
        reg = _load_registry()
        reg.pop(fname, None)
        _save_registry(reg)
    except Exception as e:
        errors.append(f"Реестр: {e}")

    if errors:
        return False, "; ".join(errors)
    return True, "OK"


def _reindex_document(fname: str) -> dict:
    """
    Переиндексация уже загруженного файла (без повторной загрузки) —
    перечитывает атрибуты и файл из data/raw/{doc_type}_docs/{fname},
    прогоняет через чанкер заново. Используется для файлов со
    status == "error" (например после исправления бага в чанкере).
    """
    reg = _load_registry()
    entry = reg.get(fname)
    if not entry:
        return {"status": "error", "error_msg": "Документ не найден в реестре", "chunks": 0}

    doc_type = entry.get("doc_type", "expertise")
    fpath = os.path.join("data", "raw", f"{doc_type}_docs", fname)
    if not os.path.exists(fpath):
        return {"status": "error", "error_msg": f"Файл не найден на диске: {fpath}", "chunks": 0}

    attrs = {
        "region": entry.get("region") or "регион_не_определён",
        "sphere": entry.get("sphere") or "сфера_не_определена",
        "year": entry.get("year") or "год_не_определён",
        "method": entry.get("method") or "метод_не_определён",
        "organization": entry.get("organization") or "организация_не_определена",
        "protocol_num": entry.get("protocol_num") or "номер_не_определён",
        "protocol_date": entry.get("protocol_date") or "",
    }

    if doc_type == "expertise":
        result = index_expertise_file(fpath, fname, attrs)
    else:
        result = index_protocol_file(fpath, fname, attrs)

    _upsert_registry_entry(fname, {"doc_type": doc_type, **attrs, **result})
    return result


def show_documents_panel():
    st.header("📑 Протоколы и экспертные заключения")
    st.caption(
        "Единая база документов по обеим категориям. Атрибуты определяются "
        "из имени файла при загрузке и могут быть отредактированы вручную."
    )

    reg = _load_registry()

    # ── Блок загрузки ───────────────────────────────────────────────────
    with st.expander("📤 Загрузка документов", expanded=True):
        _doc_type_label = st.radio(
            "Тип документа",
            list(DOC_TYPES.values()),
            horizontal=True,
            key="doc_type_radio",
        )
        doc_type = "expertise" if _doc_type_label.startswith("📄") else "protocol"

        if doc_type == "protocol":
            st.warning(
                "⚠️ Индексация протоколов через этот раздел пока не реализована. "
                "Раздел появится отдельно, когда будет готов протокольный чанкер. "
                "Существующий пайплайн протоколов (вкладка «Документ» → "
                "«Протоколы регуляторов») работает как раньше и не затронут."
            )

        upload_mode = st.radio(
            "Способ загрузки",
            ["Одиночный файл", "Массово — папка"],
            horizontal=True,
            key="upload_mode_radio",
        )

        st.divider()

        # ── Одиночная загрузка ────────────────────────────────────────
        if upload_mode == "Одиночный файл":
            uploaded = st.file_uploader(
                "Выберите файл",
                type=["pdf", "txt", "docx"],
                key="doc_single_uploader",
            )
            if uploaded:
                attrs = parse_attrs_from_filename(uploaded.name)
                st.write("**Распознанные атрибуты (можно поправить перед сохранением):**")
                c1, c2, c3 = st.columns(3)
                with c1:
                    attrs["region"] = st.text_input("Регион", value=attrs["region"], key="single_region")
                    attrs["sphere"] = st.selectbox(
                        "Сфера", SPHERES,
                        index=SPHERES.index(attrs["sphere"]) if attrs["sphere"] in SPHERES else 0,
                        key="single_sphere",
                    )
                with c2:
                    attrs["year"] = st.text_input("Год", value=attrs["year"], key="single_year")
                    attrs["method"] = st.selectbox(
                        "Метод", METHODS,
                        index=METHODS.index(attrs["method"]) if attrs["method"] in METHODS else len(METHODS) - 1,
                        key="single_method",
                    )
                with c3:
                    attrs["organization"] = st.text_input("Организация", value=attrs["organization"], key="single_org")
                    attrs["protocol_num"] = st.text_input("Номер документа", value=attrs["protocol_num"], key="single_num")

                disabled = doc_type == "protocol"
                if st.button(
                    f"💾 Индексировать ({DOC_TYPES[doc_type]})",
                    type="primary", key="single_index_btn", disabled=disabled,
                ):
                    dest_folder = os.path.join("data", "raw", f"{doc_type}_docs")
                    os.makedirs(dest_folder, exist_ok=True)
                    fpath = os.path.join(dest_folder, uploaded.name)
                    with open(fpath, "wb") as f:
                        f.write(uploaded.getbuffer())

                    if doc_type == "expertise":
                        result = index_expertise_file(fpath, uploaded.name, attrs)
                    else:
                        result = index_protocol_file(fpath, uploaded.name, attrs)

                    _upsert_registry_entry(uploaded.name, {
                        "doc_type": doc_type,
                        **attrs,
                        **result,
                    })
                    st.success(f"✅ {uploaded.name} добавлен в реестр (статус: {result['status']})")
                    st.rerun()

        # ── Массовая загрузка ─────────────────────────────────────────
        else:
            folder_path = st.text_input(
                "Путь к папке с файлами",
                placeholder=r"C:\tariff_ai_mvp\data\import\expertise_batch",
                key="batch_folder_path",
            )
            scan_col, _ = st.columns([1, 3])
            with scan_col:
                scan_clicked = st.button("🔍 Сканировать папку", key="batch_scan_btn")

            if scan_clicked:
                if not folder_path or not os.path.isdir(folder_path):
                    st.error("⚠️ Папка не найдена. Проверьте путь.")
                else:
                    found = sorted(glob.glob(os.path.join(folder_path, "*")))
                    found = [f for f in found if os.path.isfile(f)]
                    st.session_state["batch_found_files"] = found
                    st.session_state["batch_folder_confirmed"] = folder_path

            found_files = st.session_state.get("batch_found_files", [])
            if found_files and st.session_state.get("batch_folder_confirmed") == folder_path:
                already = set(reg.keys())
                new_files = [f for f in found_files if os.path.basename(f) not in already]
                st.info(
                    f"Найдено файлов: **{len(found_files)}**. "
                    f"Уже в реестре: **{len(found_files) - len(new_files)}**. "
                    f"К загрузке: **{len(new_files)}**."
                )

                disabled = doc_type == "protocol"
                start_clicked = st.button(
                    f"▶️ Начать импорт ({DOC_TYPES[doc_type]})",
                    type="primary", key="batch_start_btn", disabled=disabled,
                )

                if start_clicked:
                    st.session_state["batch_queue"] = new_files
                    st.session_state["batch_done"] = []
                    st.session_state["batch_errors"] = []
                    st.session_state["batch_running"] = True
                    st.rerun()

            # ── Примитивный последовательный loader: 1 файл за rerun ──
            if st.session_state.get("batch_running"):
                queue = st.session_state.get("batch_queue", [])
                done = st.session_state.get("batch_done", [])
                errors = st.session_state.get("batch_errors", [])
                total = len(queue) + len(done) + len(errors)

                progress_val = (len(done) + len(errors)) / total if total else 1.0
                st.progress(progress_val)
                st.caption(f"Обработано {len(done) + len(errors)} из {total} "
                           f"(ошибок: {len(errors)})")

                if queue:
                    fpath = queue.pop(0)
                    fname = os.path.basename(fpath)
                    attrs = parse_attrs_from_filename(fname)
                    try:
                        if doc_type == "expertise":
                            result = index_expertise_file(fpath, fname, attrs)
                        else:
                            result = index_protocol_file(fpath, fname, attrs)
                        _upsert_registry_entry(fname, {
                            "doc_type": doc_type,
                            **attrs,
                            **result,
                        })
                        done.append(fname)
                    except Exception as e:
                        errors.append({"fname": fname, "error": str(e)})

                    st.session_state["batch_queue"] = queue
                    st.session_state["batch_done"] = done
                    st.session_state["batch_errors"] = errors
                    st.rerun()
                else:
                    st.session_state["batch_running"] = False
                    st.success(f"✅ Импорт завершён. Успешно: {len(done)}, ошибок: {len(errors)}")
                    if errors:
                        with st.expander(f"⚠️ Ошибки ({len(errors)})"):
                            for err in errors:
                                st.write(f"**{err['fname']}**: {err['error']}")
                    if st.button("Закрыть отчёт", key="batch_close_report"):
                        for k in ["batch_queue", "batch_done", "batch_errors",
                                  "batch_running", "batch_found_files",
                                  "batch_folder_confirmed"]:
                            st.session_state.pop(k, None)
                        st.rerun()

    st.divider()

    # ── Таблица с фильтрами ─────────────────────────────────────────────
    st.subheader("📋 База документов")

    reg = _load_registry()  # перечитываем — могла обновиться после загрузки
    df = _registry_to_df(reg)

    if df.empty:
        st.info("Документов пока нет. Загрузите файлы выше.")
        return

    f1, f2, f3, f4, f5 = st.columns([2, 1.5, 1.5, 1.5, 1.5])
    with f1:
        search = st.text_input("🔍 Поиск (имя файла / организация / номер)", key="reg_search")
    with f2:
        f_type = st.selectbox("Тип документа", ["— Все —"] + list(DOC_TYPES.values()), key="reg_filter_type")
    with f3:
        f_sphere = st.selectbox("Сфера", ["— Все —"] + SPHERES, key="reg_filter_sphere")
    with f4:
        years_present = sorted(df["year"].dropna().unique().tolist())
        f_year = st.selectbox("Год", ["— Все —"] + years_present, key="reg_filter_year")
    with f5:
        f_method = st.selectbox("Метод", ["— Все —"] + METHODS, key="reg_filter_method")

    f_region = st.text_input("Регион (фильтр по подстроке)", key="reg_filter_region")

    view = df.copy()
    if search.strip():
        s = search.strip().lower()
        mask = (
            view["fname"].astype(str).str.lower().str.contains(s, na=False)
            | view["organization"].astype(str).str.lower().str.contains(s, na=False)
            | view["protocol_num"].astype(str).str.lower().str.contains(s, na=False)
        )
        view = view[mask]
    if f_type != "— Все —":
        view = view[view["doc_type"] == ("expertise" if f_type.startswith("📄") else "protocol")]
    if f_sphere != "— Все —":
        view = view[view["sphere"] == f_sphere]
    if f_year != "— Все —":
        view = view[view["year"] == f_year]
    if f_method != "— Все —":
        view = view[view["method"] == f_method]
    if f_region.strip():
        view = view[view["region"].astype(str).str.lower().str.contains(f_region.strip().lower(), na=False)]

    st.caption(f"Найдено: {len(view)} из {len(df)}")

    # ── Ручная пагинация (без data_editor на тысячи строк) ─────────────
    total_pages = max(1, (len(view) - 1) // PAGE_SIZE + 1)
    page = st.session_state.get("reg_page", 1)
    page = min(page, total_pages)

    p1, p2, p3 = st.columns([1, 2, 1])
    with p1:
        if st.button("← Назад", disabled=page <= 1, key="reg_prev"):
            page -= 1
    with p2:
        st.markdown(f"<div style='text-align:center'>Страница {page} из {total_pages}</div>", unsafe_allow_html=True)
    with p3:
        if st.button("Вперёд →", disabled=page >= total_pages, key="reg_next"):
            page += 1
    st.session_state["reg_page"] = page

    start = (page - 1) * PAGE_SIZE
    page_df = view.iloc[start:start + PAGE_SIZE]

    display_df = page_df[[
        "fname", "doc_type", "region", "sphere", "year", "method",
        "organization", "protocol_num", "chunks", "indexed_at", "status",
    ]].rename(columns={
        "fname": "Файл", "doc_type": "Тип", "region": "Регион", "sphere": "Сфера",
        "year": "Год", "method": "Метод", "organization": "Организация",
        "protocol_num": "Номер", "chunks": "Чанков", "indexed_at": "Индексирован",
        "status": "Статус",
    })
    display_df["Тип"] = display_df["Тип"].map(lambda t: DOC_TYPES.get(t, t))
    display_df.insert(0, "Выбрать", False)

    edited_df = st.data_editor(
        display_df,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in display_df.columns if c != "Выбрать"],
        key=f"reg_table_editor_page_{page}",
    )

    selected_fnames = page_df.loc[edited_df["Выбрать"].values, "fname"].tolist()

    bc1, bc2 = st.columns([1, 4])
    with bc1:
        bulk_delete_clicked = st.button(
            f"🗑️ Удалить выбранные ({len(selected_fnames)})",
            type="secondary", disabled=not selected_fnames, key="reg_bulk_delete_btn",
        )
    with bc2:
        if selected_fnames:
            st.caption("Файлы будут удалены из Chroma, реестра и с диска.")

    if bulk_delete_clicked:
        st.session_state["_confirm_bulk_delete"] = selected_fnames

    if st.session_state.get("_confirm_bulk_delete"):
        to_delete = st.session_state["_confirm_bulk_delete"]

        @st.dialog("Удалить выбранные документы?")
        def _confirm_bulk_delete():
            st.warning(
                f"Будет безвозвратно удалено {len(to_delete)} документ(ов) — "
                f"чанки из Chroma, записи реестра и файлы с диска."
            )
            with st.expander("Список файлов"):
                for fn in to_delete:
                    st.caption(fn)
            d1, d2 = st.columns(2)
            with d1:
                if st.button("🗑️ Да, удалить", type="primary", use_container_width=True, key="confirm_bulk_del_yes"):
                    ok_count, err_list = 0, []
                    for fn in to_delete:
                        doc_type_for_fn = reg.get(fn, {}).get("doc_type", "expertise")
                        success, msg = _remove_document(fn, doc_type_for_fn)
                        if success:
                            ok_count += 1
                        else:
                            err_list.append(f"{fn}: {msg}")
                    st.session_state.pop("_confirm_bulk_delete", None)
                    if err_list:
                        st.session_state["_bulk_delete_report"] = (
                            f"Удалено: {ok_count} из {len(to_delete)}. Ошибки: " + "; ".join(err_list)
                        )
                    else:
                        st.session_state["_bulk_delete_report"] = f"✅ Удалено: {ok_count} документ(ов)."
                    st.rerun()
            with d2:
                if st.button("← Отмена", use_container_width=True, key="confirm_bulk_del_no"):
                    st.session_state.pop("_confirm_bulk_delete", None)
                    st.rerun()

        _confirm_bulk_delete()

    if st.session_state.get("_bulk_delete_report"):
        st.success(st.session_state.pop("_bulk_delete_report"))

    # ── Редактирование атрибутов одной записи ──────────────────────────
    with st.expander("✏️ Редактировать / удалить / переиндексировать документ"):
        fname_to_edit = st.selectbox(
            "Выберите файл", page_df["fname"].tolist(), key="reg_edit_select"
        )
        if fname_to_edit:
            current = reg.get(fname_to_edit, {})
            e1, e2, e3 = st.columns(3)
            with e1:
                new_region = st.text_input("Регион", value=current.get("region", ""), key="edit_region")
                new_sphere = st.selectbox(
                    "Сфера", SPHERES,
                    index=SPHERES.index(current.get("sphere")) if current.get("sphere") in SPHERES else 0,
                    key="edit_sphere",
                )
            with e2:
                new_year = st.text_input("Год", value=current.get("year", ""), key="edit_year")
                new_method = st.selectbox(
                    "Метод", METHODS,
                    index=METHODS.index(current.get("method")) if current.get("method") in METHODS else len(METHODS) - 1,
                    key="edit_method",
                )
            with e3:
                new_org = st.text_input("Организация", value=current.get("organization", ""), key="edit_org")
                new_num = st.text_input("Номер документа", value=current.get("protocol_num", ""), key="edit_num")

            ab1, ab2, ab3 = st.columns(3)
            with ab1:
                if st.button("💾 Сохранить изменения", key="edit_save_btn", use_container_width=True):
                    _upsert_registry_entry(fname_to_edit, {
                        "region": new_region.strip(),
                        "sphere": new_sphere,
                        "year": new_year.strip(),
                        "method": new_method,
                        "organization": new_org.strip(),
                        "protocol_num": new_num.strip(),
                    })
                    # TODO: при подключении реального чанкера — также обновить
                    # metadata соответствующих чанков в Chroma (expertise_docs / protocols)
                    st.success("✅ Атрибуты обновлены в реестре.")
                    st.rerun()

            with ab2:
                reindex_disabled = current.get("doc_type") == "protocol"
                if st.button("🔄 Переиндексировать", key="edit_reindex_btn",
                             use_container_width=True, disabled=reindex_disabled):
                    with st.spinner(f"Переиндексация {fname_to_edit}..."):
                        result = _reindex_document(fname_to_edit)
                    if result.get("status") == "ok":
                        st.success(f"✅ Переиндексировано: {result.get('chunks', 0)} чанк(ов).")
                    else:
                        st.error(f"❌ Ошибка переиндексации: {result.get('error_msg', '—')}")
                    st.rerun()

            with ab3:
                if st.button("🗑️ Удалить документ", key="edit_delete_btn",
                             type="secondary", use_container_width=True):
                    st.session_state["_confirm_single_delete"] = fname_to_edit

            if st.session_state.get("_confirm_single_delete") == fname_to_edit:
                @st.dialog("Удалить документ?")
                def _confirm_single_delete():
                    st.warning(
                        f"Документ «{fname_to_edit}» будет безвозвратно удалён "
                        f"(чанки из Chroma, запись реестра, файл с диска)."
                    )
                    sd1, sd2 = st.columns(2)
                    with sd1:
                        if st.button("🗑️ Да, удалить", type="primary", use_container_width=True, key="confirm_single_del_yes"):
                            doc_type_for_fn = current.get("doc_type", "expertise")
                            success, msg = _remove_document(fname_to_edit, doc_type_for_fn)
                            st.session_state.pop("_confirm_single_delete", None)
                            if success:
                                st.session_state["_bulk_delete_report"] = f"✅ Удалён: {fname_to_edit}"
                            else:
                                st.session_state["_bulk_delete_report"] = f"⚠️ Ошибка при удалении {fname_to_edit}: {msg}"
                            st.rerun()
                    with sd2:
                        if st.button("← Отмена", use_container_width=True, key="confirm_single_del_no"):
                            st.session_state.pop("_confirm_single_delete", None)
                            st.rerun()
                _confirm_single_delete()
