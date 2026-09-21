# streamlit_pages/hidden_layer_panel.py
"""
Вкладка Админки «Служебный слой»
──────────────────────────────────────────────────────────────────────────────
ЧТО ЭТО ТАКОЕ (кратко, для того кто будет читать код через полгода)

Служебный слой — документы с пояснениями и внутренней терминологией, которые
ВЛИЯЮТ на ответы Советчика, но НЕ показываются пользователю как источник и не
цитируются моделью. Архитектура и причины принятых решений описаны большим
комментарием в начале core/advisor.py.

Что делает эта вкладка:
  1. Загрузка файлов слоя в data/raw/hidden и их индексация с
     doc_type="hidden" (через config/doc_types_override.json).
  2. Настройки поведения слоя (пишутся в config/search_settings.json,
     читаются core.advisor._load_search_settings).
  3. Проверка: вводим вопрос — видим, какие фрагменты слоя подхватились
     и с каким скором. Это ЕДИНСТВЕННОЕ место, где содержимое слоя видно
     в интерфейсе; в самом Советчике оно не отображается нигде.

ФОРМУЛИРОВКИ В UI. Тексты подсказок намеренно написаны бытовым языком и
вынесены в help= (иконка «?» рядом с настройкой), а не в подписи — иначе
вкладка превращается в стену текста.
"""
from __future__ import annotations
import os
import json

import streamlit as st


# ── Пути ──────────────────────────────────────────────────────────────────
SEARCH_SETTINGS_FILE = os.path.join("config", "search_settings.json")
ALLOWED_EXT = ["pdf", "txt", "docx"]


# ── Тексты подсказок (иконка «?») ─────────────────────────────────────────
HELP = {
    "enabled": (
        "Главный выключатель. Когда выключен — Советчик работает так, "
        "будто служебного слоя нет вовсе. Сами файлы и их индекс никуда "
        "не пропадают, их можно включить обратно в любой момент."
    ),
    "top_k": (
        "Сколько кусочков пояснений максимум подмешивается в один ответ. "
        "Больше — модель получит больше контекста, но и больше шанс, что "
        "в ответ попадёт что-то не по теме. Разумно 2–4."
    ),
    "min_score": (
        "Насколько уверенно кусочек должен подходить к вопросу, чтобы его "
        "вообще использовать. Это оценка от программы-оценщика: примерно "
        "от -10 (совсем не по теме) до +10 (точно по теме).\n\n"
        "Порог 0 — «скорее подходит, чем нет». Поднимите до 2–3, если слой "
        "лезет в ответы не по теме. Опустите до -2, если слой почти не "
        "срабатывает там, где должен."
    ),
    "neighbor_radius": (
        "Пояснение часто не помещается в один кусочек текста и обрывается "
        "на полуслове. Эта настройка добавляет соседние кусочки из того же "
        "файла — по одному до и после. 0 — только сам найденный кусочек."
    ),
    "max_chars": (
        "Сколько всего символов пояснений максимум уйдёт в один запрос к "
        "модели. Ограничение нужно, чтобы слой не вытеснил из запроса "
        "нормативные документы. 2500 символов — примерно одна страница."
    ),
    "query_expansion": (
        "Если в найденном пояснении упомянуты конкретные документы или формы "
        "(«считаем по 760-э, форма 4.2»), система дополнительно поищет эти "
        "документы в базе НПА — даже если специалист спросил своими словами "
        "и никаких номеров не называл.\n\n"
        "Помогает связать внутренний язык организации с нормативкой. "
        "Замедляет поиск примерно на секунду. Если заметите, что в источники "
        "стало попадать лишнее — выключите."
    ),
    "upload": (
        "Файлы этого раздела не попадают в обычный список НПА и никогда не "
        "показываются пользователям как источник. Сюда стоит класть: "
        "расшифровки внутренних сокращений, памятки «как мы это делаем», "
        "пояснения к спорным статьям затрат, общие сведения об организации."
    ),
    "test": (
        "Введите вопрос так, как его задал бы специалист. Ниже увидите, какие "
        "пояснения подхватились и с какой оценкой. Если нужного пояснения нет "
        "— снизьте порог уверенности в настройках выше или переформулируйте "
        "текст в самом файле пояснения."
    ),
}


def _load_settings() -> dict:
    """Настройки поиска целиком (слой живёт в том же файле)."""
    try:
        from core.advisor import DEFAULT_SEARCH_SETTINGS
        defaults = dict(DEFAULT_SEARCH_SETTINGS)
    except Exception:
        defaults = {}
    if os.path.exists(SEARCH_SETTINGS_FILE):
        try:
            with open(SEARCH_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return {**defaults, **json.load(f)}
        except Exception:
            pass
    return defaults


def _save_settings(new_values: dict) -> None:
    """
    Дописывает переданные ключи в config/search_settings.json, сохраняя
    остальные настройки поиска нетронутыми (в этом же файле живут вес BM25,
    реранкер и прочее — вкладка «Поиск и реранкинг»).
    """
    cur = _load_settings()
    cur.update(new_values)
    os.makedirs(os.path.dirname(SEARCH_SETTINGS_FILE), exist_ok=True)
    with open(SEARCH_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    # session_state — приоритетный источник настроек в core.advisor,
    # без обновления изменения применились бы только после перезапуска.
    st.session_state["_search_settings"] = cur


def show_hidden_layer_panel():
    st.header("Служебный слой знаний")
    st.info(
        "Сюда загружаются пояснения, расшифровки внутренних терминов и общая "
        "информация. Советчик учитывает их при подготовке ответа, но **никогда "
        "не показывает пользователю** и не ссылается на них как на документ."
    )

    try:
        from core.indexer import (
            hidden_layer_dir, mark_as_hidden, unmark_hidden,
            get_hidden_chunk_index, index_file, remove_file_from_index,
            HIDDEN_DOC_TYPE,
        )
    except Exception as e:
        st.error(f"Не удалось подключить индексатор: {e}")
        return

    hidden_dir = hidden_layer_dir()
    settings   = _load_settings()

    tab_files, tab_settings, tab_test = st.tabs(
        ["Файлы слоя", "Настройки", "Проверка"]
    )

    # =====================================================================
    # ВКЛАДКА 1: файлы
    # =====================================================================
    with tab_files:
        st.subheader("Загрузить пояснения")
        st.caption(HELP["upload"])

        uploaded = st.file_uploader(
            "Перетащите файлы или выберите с компьютера",
            type=ALLOWED_EXT,
            accept_multiple_files=True,
            key="hidden_uploader",
            label_visibility="collapsed",
        )

        if uploaded:
            if st.button(
                f"Сохранить и проиндексировать ({len(uploaded)} файл(ов))",
                type="primary", key="hidden_save_btn", use_container_width=True,
            ):
                progress = st.progress(0)
                ok, errors = 0, []
                for i, uf in enumerate(uploaded):
                    fpath = os.path.join(hidden_dir, uf.name)
                    try:
                        with open(fpath, "wb") as f:
                            f.write(uf.getbuffer())
                        # ВАЖЕН ПОРЯДОК: пометку ставим ДО индексации, иначе
                        # resolve_doc_type() в индексаторе не увидит её и файл
                        # уйдёт в обычные источники как npa/unknown.
                        mark_as_hidden(uf.name)
                        res = index_file(fpath, category=HIDDEN_DOC_TYPE)
                        if res.get("status") == "success":
                            ok += 1
                        else:
                            errors.append(f"{uf.name}: {res.get('message', 'ошибка')}")
                    except Exception as _e:
                        errors.append(f"{uf.name}: {_e}")
                    progress.progress((i + 1) / len(uploaded))

                try:
                    from core.advisor import invalidate_hybrid_retriever
                    invalidate_hybrid_retriever()
                except Exception:
                    pass

                if ok:
                    st.success(f"Загружено и проиндексировано: {ok} файл(ов)")
                for err in errors:
                    st.error(err)
                st.rerun()

        st.divider()
        st.subheader("Файлы слоя")

        chunk_index = get_hidden_chunk_index()

        files = []
        for fn in sorted(os.listdir(hidden_dir)):
            fp = os.path.join(hidden_dir, fn)
            if fn.startswith(".") or fn.endswith(".indexed") or not os.path.isfile(fp):
                continue
            info = chunk_index.get(fn, {})
            files.append({
                "fname": fn,
                "fpath": fp,
                "size_kb": os.path.getsize(fp) / 1024,
                "chunks": info.get("chunks", 0),
                "indexed_at": info.get("indexed_at", "—"),
            })

        # Файлы, помеченные как слой, но лежащие не в этой папке (например,
        # помеченные вручную через конфиг) — показываем отдельно, чтобы
        # админ понимал полную картину того, что сейчас в слое.
        _orphans = [fn for fn in chunk_index if fn not in {f["fname"] for f in files}]

        if not files and not _orphans:
            st.info("Файлов пока нет. Загрузите первый файл пояснений выше.")
        else:
            _total_chunks = sum(v.get("chunks", 0) for v in chunk_index.values())
            st.caption(f"Файлов: **{len(files)}**  ·  всего фрагментов в слое: "
                       f"**{_total_chunks}**")
            st.divider()

            hc = st.columns([4, 2, 2, 1, 1])
            for col, label in zip(hc, ["Файл", "Проиндексирован",
                                       "Фрагментов", "Индекс", "Удалить"]):
                col.markdown(f"**{label}**")
            st.divider()

            for fi in files:
                row = st.columns([4, 2, 2, 1, 1])
                with row[0]:
                    st.markdown(f"**{fi['fname']}**")
                    st.caption(f"{fi['size_kb']:.1f} КБ")
                with row[1]:
                    if fi["chunks"] > 0:
                        st.caption(f"да · {fi['indexed_at']}")
                    else:
                        st.caption("нет")
                with row[2]:
                    st.caption(str(fi["chunks"]))
                with row[3]:
                    if st.button("Обновить", key=f"hid_idx_{fi['fname']}",
                                 use_container_width=True,
                                 help="Проиндексировать файл заново"):
                        with st.spinner(f"Индексация {fi['fname']}..."):
                            try:
                                remove_file_from_index(fi["fname"])
                            except Exception:
                                pass
                            mark_as_hidden(fi["fname"])
                            res = index_file(fi["fpath"], category=HIDDEN_DOC_TYPE)
                            try:
                                from core.advisor import invalidate_hybrid_retriever
                                invalidate_hybrid_retriever()
                            except Exception:
                                pass
                        if res.get("status") == "success":
                            st.toast(f"{fi['fname']}: {res.get('chunks', 0)} фрагментов")
                        else:
                            st.toast(f"Ошибка: {res.get('message', '')}", icon="🚨")
                        st.rerun()
                with row[4]:
                    if st.button("Удалить", key=f"hid_del_{fi['fname']}",
                                 use_container_width=True):
                        st.session_state[f"_hid_confirm_del_{fi['fname']}"] = True

                if st.session_state.get(f"_hid_confirm_del_{fi['fname']}"):
                    @st.dialog(f"Удалить «{fi['fname']}»?")
                    def _confirm(fname=fi["fname"], fpath=fi["fpath"]):
                        st.warning(
                            "Файл будет удалён с диска и из индекса. "
                            "Пояснения из него перестанут учитываться в ответах."
                        )
                        ca, cb = st.columns(2)
                        with ca:
                            if st.button("Да, удалить", type="primary",
                                         use_container_width=True,
                                         key=f"hid_conf_del_{fname}"):
                                try:
                                    remove_file_from_index(fname)
                                except Exception:
                                    pass
                                try:
                                    os.remove(fpath)
                                except Exception:
                                    pass
                                unmark_hidden(fname)
                                try:
                                    from core.advisor import invalidate_hybrid_retriever
                                    invalidate_hybrid_retriever()
                                except Exception:
                                    pass
                                st.session_state.pop(f"_hid_confirm_del_{fname}", None)
                                st.rerun()
                        with cb:
                            if st.button("Отмена", use_container_width=True,
                                         key=f"hid_cancel_del_{fname}"):
                                st.session_state.pop(f"_hid_confirm_del_{fname}", None)
                                st.rerun()
                    _confirm()
                st.divider()

            if _orphans:
                st.caption(
                    "В слое также есть фрагменты файлов, которых нет в этой папке "
                    "(помечены вручную или загружены раньше): "
                    + ", ".join(_orphans)
                )

    # =====================================================================
    # ВКЛАДКА 2: настройки
    # =====================================================================
    with tab_settings:
        st.subheader("Как слой участвует в ответах")

        _enabled = st.toggle(
            "Использовать служебный слой",
            value=bool(settings.get("hidden_layer_enabled", True)),
            key="hid_enabled",
            help=HELP["enabled"],
        )
        if not _enabled:
            st.caption("Слой выключен — ответы формируются без пояснений.")

        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            _top_k = st.slider(
                "Сколько пояснений использовать за раз",
                min_value=1, max_value=8,
                value=int(settings.get("hidden_top_k", 3)),
                key="hid_top_k",
                help=HELP["top_k"],
            )
            _radius = st.slider(
                "Добавлять соседние кусочки текста",
                min_value=0, max_value=3,
                value=int(settings.get("hidden_neighbor_radius", 1)),
                key="hid_radius",
                help=HELP["neighbor_radius"],
            )
        with c2:
            _min_score = st.slider(
                "Порог уверенности",
                min_value=-5.0, max_value=5.0, step=0.5,
                value=float(settings.get("hidden_min_score", 0.0)),
                key="hid_min_score",
                help=HELP["min_score"],
            )
            if _min_score <= -2:
                st.caption("Слой будет срабатывать очень часто, в том числе не по теме.")
            elif _min_score >= 2:
                st.caption("Слой сработает только при явном совпадении с вопросом.")
            else:
                st.caption("Обычный режим: слой срабатывает, когда тема совпадает.")

            _max_chars = st.slider(
                "Максимум символов пояснений в одном запросе",
                min_value=500, max_value=8000, step=500,
                value=int(settings.get("hidden_max_chars", 2500)),
                key="hid_max_chars",
                help=HELP["max_chars"],
            )

        st.divider()
        _qexp = st.toggle(
            "Искать нормативку по подсказкам из слоя",
            value=bool(settings.get("hidden_query_expansion", False)),
            key="hid_qexp",
            help=HELP["query_expansion"],
        )

        st.divider()
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Сохранить настройки", type="primary",
                         use_container_width=True, key="hid_save_settings"):
                _save_settings({
                    "hidden_layer_enabled":   bool(_enabled),
                    "hidden_top_k":           int(_top_k),
                    "hidden_min_score":       float(_min_score),
                    "hidden_neighbor_radius": int(_radius),
                    "hidden_max_chars":       int(_max_chars),
                    "hidden_query_expansion": bool(_qexp),
                })
                st.session_state["_hid_saved"] = True
                st.rerun()
        with b2:
            if st.button("Вернуть значения по умолчанию",
                         use_container_width=True, key="hid_reset_settings"):
                _save_settings({
                    "hidden_layer_enabled":   True,
                    "hidden_top_k":           3,
                    "hidden_min_score":       0.0,
                    "hidden_neighbor_radius": 1,
                    "hidden_max_chars":       2500,
                    "hidden_query_expansion": False,
                })
                st.session_state["_hid_reset"] = True
                st.rerun()

        if st.session_state.pop("_hid_saved", False):
            st.success("Настройки сохранены — применятся к следующему запросу.")
        if st.session_state.pop("_hid_reset", False):
            st.info("Настройки возвращены к значениям по умолчанию.")

        st.divider()
        with st.expander("Как это работает — подробнее", expanded=False):
            st.markdown(
                "1. Специалист задаёт вопрос в Советчике.\n"
                "2. Система отдельно ищет подходящие пояснения в этом слое.\n"
                "3. Пояснения, прошедшие порог уверенности, передаются модели "
                "вместе с найденными нормативными документами — но с прямым "
                "запретом ссылаться на них и упоминать их существование.\n"
                "4. В списке источников под ответом пояснения не появляются: "
                "там только нормативные документы.\n\n"
                "Отдельно есть **внутренний режим**: в Советчике в поле «Вид "
                "документа» выбирается пункт «Только служебный слой». Тогда "
                "поиск по нормативной базе не выполняется вообще, и модель "
                "отвечает на своих знаниях плюс эти пояснения. Источники в "
                "таком ответе не показываются."
            )

    # =====================================================================
    # ВКЛАДКА 3: проверка
    # =====================================================================
    with tab_test:
        st.subheader("Проверить, что подхватывается")
        st.caption(HELP["test"])

        _q = st.text_input(
            "Вопрос специалиста",
            placeholder="Например: сколько людей закладывать в тариф",
            key="hid_test_q",
        )
        if st.button("Проверить", type="primary", key="hid_test_btn"):
            if not _q.strip():
                st.warning("Введите вопрос")
            else:
                with st.spinner("Ищем в служебном слое..."):
                    try:
                        from core.advisor import debug_hidden_layer
                        dbg = debug_hidden_layer(_q.strip())
                    except Exception as e:
                        st.error(f"{type(e).__name__}: {e}")
                        dbg = None

                if dbg:
                    if dbg.get("error"):
                        st.error(dbg["error"])
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Фрагментов в слое", dbg.get("total_chunks", 0))
                    m2.metric("Подошло к вопросу", len(dbg.get("found", [])))
                    m3.metric("Время, сек", dbg.get("elapsed", 0))

                    if dbg.get("total_chunks", 0) == 0:
                        st.warning(
                            "В слое нет ни одного фрагмента. Загрузите файлы "
                            "на вкладке «Файлы слоя»."
                        )
                    elif not dbg.get("found"):
                        st.info(
                            "Ни одно пояснение не подошло к этому вопросу. "
                            "Это нормально, если вопрос не по теме пояснений. "
                            "Если пояснение должно было сработать — снизьте "
                            "порог уверенности на вкладке «Настройки»."
                        )
                    else:
                        st.success("Эти пояснения попадут в ответ (пользователю их не видно):")
                        for i, src in enumerate(dbg["found"], 1):
                            _sc = src.get("rerank_score", "")
                            with st.expander(
                                f"{i}. {src.get('file', '?')}  ·  оценка {_sc}",
                                expanded=(i == 1),
                            ):
                                st.text_area(
                                    "Текст фрагмента",
                                    value=src.get("snippet", ""),
                                    height=200, disabled=True,
                                    key=f"hid_test_txt_{i}",
                                    label_visibility="collapsed",
                                )