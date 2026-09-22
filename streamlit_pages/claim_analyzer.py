# streamlit_pages/claim_analyzer.py
"""
UI Анализатора тарифных заявок
──────────────────────────────────────────────────────────────────────────────
Вкладки:
  1. Риски и комплектность — LLM-анализ рисков по статьям + оценка документов
  2. Реестр заявок         — сохранение, поиск, управление статусами

Бизнес-логика → core/claim_analyzer_logic.py
ZIP-архивы      → core/archive_extractor (распаковка, чтение файла из архива)
Карта документов «файл ↔ статья» — _render_doc_map (данные — _enrich_risks_json)
Расчётные Excel → core/calc_parser
Промпты         → config/prompts.json (Админка)
Реестр          → core/claim_registry  (data/claims/)
"""

from __future__ import annotations
import os, io, json, time, re, hashlib, uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st
from streamlit.errors import StreamlitAPIException

try:
    from core.usage_tracker import log_event as _log_usage
except Exception:
    def _log_usage(*a, **kw): pass  # noqa: E731

# Импорт всей бизнес-логики из core
from core.claim_analyzer_logic import (
    REGULATION_SPHERES,
    SPHERE_IDS,
    SPHERE_LABELS,
    DEFAULT_PROMPTS,
    load_prompts,
    load_mr_config,
    save_mr_config,
    compute_mr_plan,
    MR_DEFAULTS,
    summarize_claim,
    analyze_risks,
    _render_timeseries_chart,
    _parse_amounts_timeseries,
    _build_file_summaries,
    _build_claim_summary_from_heads,
    _extract_articles_from_context,
    _extract_articles_from_context_unfiltered,
    _extract_articles_from_df,
    _classify_article,
    _has_nonzero_value,
    _rag_diagnose,
    _save_log,
    _format_size,
)
from core.archive_extractor import expand_archive, is_archive, common_root, read_member

# Лимит документов, у которых ядро читает начало текста (_build_file_summaries)
try:
    from core.claim_analyzer_logic import _MAX_DOC_FILES as _DOC_LIMIT
except Exception:
    _DOC_LIMIT = 100
# Порог «высокого» смыслового сходства файла со статьёй
try:
    from core.claim_analyzer_logic import SIMILARITY_GREEN as _SIM_HIGH
except Exception:
    _SIM_HIGH = 0.85

_DOC_EXTS  = (".pdf", ".docx", ".doc", ".txt")
_CALC_EXTS = (".xlsx", ".xls")
_MAP_HEAD_CHARS = 600   # сколько символов начала документа хранить для карты

_RISK_ORDER = {"red": 0, "yellow": 1, "green": 2}
_RISK_EMOJI = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
_RISK_TEXT  = {"red": "Высокий", "yellow": "Средний", "green": "Низкий"}


# ─────────────────────────────────────────────────────────────────────────────
# ZIP-архивы заявок
# ─────────────────────────────────────────────────────────────────────────────
class _ArchivedFile:
    """
    Файл, извлечённый из ZIP-архива. Повторяет интерфейс UploadedFile
    (name / size / read / getvalue), поэтому дальше по коду обрабатывается
    так же, как файл, загруженный напрямую.

    name    — имя в анализе: путь внутри архива без общей корневой папки
              («Обоснования/Топливо/Договор.pdf»);
    archive — имя архива, под которым он сохраняется в реестр;
    inner   — полный путь внутри архива (как его видит пользователь).
    """
    __slots__ = ("name", "size", "archive", "inner", "_data")

    def __init__(self, name: str, data: bytes, archive: str, inner: str):
        self.name    = name
        self.size    = len(data)
        self.archive = archive
        self.inner   = inner
        self._data   = data

    def read(self) -> bytes:
        return self._data

    def getvalue(self) -> bytes:
        return self._data


def _upload_bytes(uf) -> bytes:
    """
    Байты загруженного файла без зависимости от позиции курсора:
    повторный uf.read() у UploadedFile возвращает b"".
    """
    try:
        return uf.getvalue()
    except Exception:
        try:
            uf.seek(0)
        except Exception:
            pass
        return uf.read()


def _unique_name(name: str, used: set) -> str:
    """«файл.pdf» → «файл (2).pdf», если имя уже занято."""
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while f"{stem} ({i}){ext}" in used:
        i += 1
    new = f"{stem} ({i}){ext}"
    used.add(new)
    return new


def _registry_names(uploaded) -> List[str]:
    """
    Имена исходных файлов в реестре. Файлы пишутся на диск по имени,
    поэтому два архива «Заявка.zip» получают «Заявка.zip» и «Заявка (2).zip».
    """
    used: set = set()
    return [_unique_name(uf.name, used) for uf in uploaded]


def _zip_cache_id(uf) -> str:
    return f"{getattr(uf, 'file_id', '')}|{uf.name}|{uf.size}"


def _upload_signature(uploaded) -> str:
    """Отпечаток набора загруженных файлов — чтобы заметить смену файлов."""
    return "|".join(sorted(f"{uf.name}:{uf.size}" for uf in uploaded))


def _expand_uploads(uploaded) -> Dict:
    """
    Разворачивает ZIP-архивы среди загруженных файлов.
    Возвращает {"files": [...], "skipped": [(архив, путь, причина)], "errors": [...]}.

    Распаковка кешируется в session_state по file_id загрузки и выполняется
    один раз, а не на каждом rerun (иначе повторяется баг с зависанием
    upload-хэндшейка из-за тяжёлой работы при каждом взаимодействии).
    Имена раздаются заново на каждом rerun, но детерминированно —
    от них зависят ключи галочек «расч.».
    """
    ss = st.session_state
    cache: Dict[str, dict] = ss.setdefault("ca_zip_cache", {})

    pending = [uf for uf in uploaded
               if is_archive(uf.name) and _zip_cache_id(uf) not in cache]
    if pending:
        with st.spinner("Распаковываю архив..."):
            for uf in pending:
                res = expand_archive(_upload_bytes(uf), uf.name)
                cache[_zip_cache_id(uf)] = {
                    "files":   res.files,
                    "skipped": res.skipped,
                    "errors":  res.errors,
                    "root":    common_root([p for p, _ in res.files]),
                }

    reg_names = _registry_names(uploaded)
    # Имена файлов, загруженных напрямую, резервируем первыми:
    # при совпадении переименовывается файл из архива, а не они
    used: set = {uf.name for uf in uploaded if not is_archive(uf.name)}

    files:   list = []
    skipped: list = []
    errors:  list = []
    live_ids = set()

    for uf, reg_name in zip(uploaded, reg_names):
        if not is_archive(uf.name):
            files.append(uf)
            continue
        cid = _zip_cache_id(uf)
        live_ids.add(cid)
        entry = cache[cid]
        root  = entry.get("root", "")
        for inner, blob in entry["files"]:
            short = inner[len(root):] if root and inner.startswith(root) else inner
            files.append(_ArchivedFile(_unique_name(short, used), blob, reg_name, inner))
        skipped.extend((reg_name, p, why) for p, why in entry["skipped"])
        errors.extend(entry["errors"])
        if not entry["files"] and not entry["errors"]:
            errors.append(f"{uf.name}: в архиве нет поддерживаемых файлов")

    # Сбрасываем из кеша архивы, которые пользователь убрал из загрузчика
    for stale in [k for k in cache if k not in live_ids]:
        cache.pop(stale, None)

    return {"files": files, "skipped": skipped, "errors": errors}


def _cache_upload_bytes(uploaded, bundle: Dict) -> None:
    """
    Кеширует байты в session_state:
      ca_uploaded_bytes / ca_uploaded_meta — файлы для анализа
                                             (содержимое архивов развёрнуто);
      ca_file_origins                      — откуда каждый файл: архив и путь;
      ca_registry_files                    — то, что уходит в реестр:
                                             исходные архивы целиком
                                             и файлы, загруженные напрямую.
    """
    ss = st.session_state
    files = bundle["files"]

    # Набор файлов сменился — прочитанные ранее документы и резюме
    # относятся к другой заявке и не должны попасть в новый анализ
    prev_names = set((ss.get("ca_uploaded_bytes") or {}).keys())
    if prev_names and prev_names != {f.name for f in files}:
        ss["ca_file_summaries"] = {}
        ss["ca_claim_summary"]  = ""

    ss.ca_uploaded_bytes = {}
    ss.ca_uploaded_meta  = []
    ss.ca_file_origins   = {}
    for f in files:
        b = _upload_bytes(f)
        ss.ca_uploaded_bytes[f.name] = b
        ss.ca_uploaded_meta.append({"name": f.name, "size": len(b)})
        if isinstance(f, _ArchivedFile):
            ss.ca_file_origins[f.name] = {"archive": f.archive, "path": f.inner}
        else:
            ss.ca_file_origins[f.name] = {"archive": "", "path": f.name}

    ss.ca_registry_files = [
        {"name": reg_name, "bytes": _upload_bytes(uf)}
        for uf, reg_name in zip(uploaded, _registry_names(uploaded))
    ]
    ss.ca_zip_skipped = list(bundle.get("skipped") or [])
    ss.ca_bundle_id   = uuid.uuid4().hex[:12]
    ss.ca_upload_sig  = _upload_signature(uploaded)


def _registry_files_data() -> List[Dict]:
    """Файлы для save_project: исходные архивы, а не их содержимое."""
    ss = st.session_state
    if ss.get("ca_registry_files"):
        return ss.ca_registry_files
    # Совместимость: данные, закешированные до появления поддержки архивов.
    # Файлы с «/» в имени — это содержимое архива, в реестр их не пишем.
    return [
        {"name": meta["name"],
         "bytes": ss.ca_uploaded_bytes.get(meta["name"], b"")}
        for meta in ss.ca_uploaded_meta
        if "/" not in meta["name"]
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Модальные окна и ленивые контейнеры
# ─────────────────────────────────────────────────────────────────────────────
def _try_open(dialog_fn, *args) -> None:
    """
    Открывает модальное окно, если в этом прогоне страницы другое ещё
    не открыто: Streamlit допускает одно окно за прогон, иначе падает.
    """
    ss = st.session_state
    tok = ss.get("_ca_run_token")
    if tok is not None and ss.get("_ca_dlg_token") == tok:
        return
    ss["_ca_dlg_token"] = tok
    dialog_fn(*args)


def _lazy_tabs(labels: List[str], key: str):
    """
    Вкладки с ленивым выполнением (Streamlit ≥1.57: on_change="rerun"
    + .open). На более старой версии — обычные вкладки, всё рендерится.
    """
    try:
        return st.tabs(labels, key=key, on_change="rerun")
    except TypeError:
        return st.tabs(labels)


def _lazy_expander(label: str, key: str, expanded: bool = False):
    """Экспандер, содержимое которого выполняется только в раскрытом виде."""
    try:
        return st.expander(label, expanded=expanded, key=key, on_change="rerun")
    except TypeError:
        return st.expander(label, expanded=expanded)


def _tab_open(tab) -> bool:
    """Открыта ли вкладка/экспандер (без ленивого режима — всегда да)."""
    return getattr(tab, "open", True) is not False


def _sel_rows(event) -> List[int]:
    """Выбранные строки st.dataframe(on_select=...)."""
    try:
        return [int(i) for i in event.selection.rows]
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Файлы заявки до анализа: строка на архив, список — в модальном окне
# ─────────────────────────────────────────────────────────────────────────────
_KIND_EXCEL, _KIND_DOC, _KIND_SKIP = "Excel", "Документ", "Не анализируется"


def _make_group(gid: str, label: str, icon: str, rows: List[Dict]) -> Dict:
    n_x = sum(r["kind"] == _KIND_EXCEL for r in rows)
    n_d = sum(r["kind"] == _KIND_DOC for r in rows)
    n_s = sum(r["kind"] == _KIND_SKIP for r in rows)
    parts = [f"{len(rows)} файл(ов)", f"Excel: {n_x}", f"документов: {n_d}"]
    if n_s:
        parts.append(f"не анализируются: {n_s}")
    parts.append(_format_size(sum(r["size"] or 0 for r in rows)))
    return {"gid": gid, "label": label, "icon": icon, "rows": rows,
            "summary": " · ".join(parts)}


def _upload_groups(uploaded, bundle: Dict) -> List[Dict]:
    """Группы файлов: «загружены отдельно» и по одной на каждый архив."""
    files = bundle["files"]

    # Порядковый номер документа — ядро читает первые _DOC_LIMIT по порядку
    doc_rank: Dict[str, int] = {}
    for f in files:
        if os.path.splitext(f.name.lower())[1] in _DOC_EXTS:
            doc_rank[f.name] = len(doc_rank)

    def _row(f) -> Dict:
        ext = os.path.splitext(f.name.lower())[1]
        org = (_make_origin(f.archive, f.inner) if isinstance(f, _ArchivedFile)
               else _make_origin("", f.name))
        return {
            "key": f.name, "obj": f, "src": None, "origin": org,
            "base": org["base"], "folder": org["folder"], "where": _loc_label(org),
            "kind": _KIND_EXCEL if ext in _CALC_EXTS else _KIND_DOC,
            "size": f.size, "reason": "", "rank": doc_rank.get(f.name),
        }

    groups: List[Dict] = []
    direct = [f for f in files if not isinstance(f, _ArchivedFile)]
    if direct:
        groups.append(_make_group("direct", "Загружены отдельными файлами", "📄",
                                  [_row(f) for f in direct]))
    for uf, reg_name in zip(uploaded, _registry_names(uploaded)):
        if not is_archive(uf.name):
            continue
        rows = [_row(f) for f in files
                if isinstance(f, _ArchivedFile) and f.archive == reg_name]
        for a, p, why in bundle.get("skipped") or []:
            if a == reg_name:
                org = _make_origin(reg_name, p)
                rows.append({
                    "key": "", "obj": None, "src": uf, "origin": org,
                    "base": org["base"], "folder": org["folder"], "where": _loc_label(org),
                    "kind": _KIND_SKIP, "size": None, "reason": why, "rank": None,
                })
        rows.sort(key=lambda r: (r["folder"].lower(), r["base"].lower()))
        gid = hashlib.md5(reg_name.encode("utf-8")).hexdigest()[:10]
        groups.append(_make_group(gid, reg_name, "📦", rows))
    return groups


def _pre_status(r: Dict, calc: set) -> str:
    """Что произойдёт с файлом при анализе."""
    if r["kind"] == _KIND_EXCEL:
        return ("расчётная модель — из неё возьмутся статьи затрат" if r["key"] in calc
                else "Excel не выбран как расчётная модель — не анализируется")
    if r["kind"] == _KIND_DOC:
        if r["rank"] is not None and r["rank"] >= _DOC_LIMIT:
            return f"не будет прочитан: сопоставляются первые {_DOC_LIMIT} документов"
        return "будет прочитано начало документа и сопоставлено со статьями"
    return f"не анализируется: {r['reason']}" if r["reason"] else "не анализируется"


def _pre_file_bytes(r: Dict) -> Optional[bytes]:
    if r["obj"] is not None:
        return _upload_bytes(r["obj"])
    if r["src"] is not None:
        return read_member(_upload_bytes(r["src"]), r["origin"]["path"])
    return None


@st.dialog("Файлы заявки", width="large")
def _files_dialog(group: Dict) -> None:
    """
    Список файлов архива: папки, типы, что с ними будет при анализе,
    скачивание. Всё внутри окна перезапускает только окно.
    """
    import pandas as pd
    ss  = st.session_state
    gid = group["gid"]

    st.markdown(f"{group['icon']} **{_md_escape(group['label'])}**")
    st.caption(group["summary"])

    q_col, k_col = st.columns([3, 2])
    q = q_col.text_input("Поиск", key=f"ca_fdlg_q_{gid}",
                         placeholder="Поиск по имени файла или папки…",
                         label_visibility="collapsed")
    kind = k_col.radio("Тип", ["Все", "Excel", "Документы", "Не анализируются"],
                       horizontal=True, key=f"ca_fdlg_k_{gid}",
                       label_visibility="collapsed")
    kmap = {"Excel": _KIND_EXCEL, "Документы": _KIND_DOC, "Не анализируются": _KIND_SKIP}
    ql = (q or "").strip().lower()
    view = [
        r for r in group["rows"]
        if (kind == "Все" or r["kind"] == kmap[kind])
        and (not ql or ql in f"{r['folder']}/{r['base']}".lower())
    ]
    if not view:
        st.caption("Нет файлов по выбранному фильтру.")
        return

    calc = set(ss.get("ca_calc_files_checked") or [])
    df = pd.DataFrame([{
        "Папка":  r["folder"].replace("/", " › ") or "—",
        "Файл":   r["base"],
        "Тип":    r["kind"],
        "Размер": _format_size(r["size"]) if r["size"] is not None else "—",
        "Статус": _pre_status(r, calc),
    } for r in view])
    st.caption("Нажмите на строку, чтобы посмотреть файл и скачать его.")
    event = st.dataframe(
        df, hide_index=True, width="stretch", height=_df_height(len(df)),
        key=f"ca_fdlg_tbl_{gid}_" + hashlib.md5(f"{ql}|{kind}".encode()).hexdigest()[:8],
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Папка":  st.column_config.TextColumn("Папка", width="medium"),
            "Файл":   st.column_config.TextColumn("Файл", width="large"),
            "Тип":    st.column_config.TextColumn("Тип", width="small"),
            "Размер": st.column_config.TextColumn("Размер", width="small"),
            "Статус": st.column_config.TextColumn("Статус", width="large"),
        },
    )
    sel = _sel_rows(event)
    if sel and sel[0] < len(view):
        r = view[sel[0]]
        st.divider()
        st.markdown(f"📄 **{_md_escape(r['base'])}**")
        size_txt = _format_size(r["size"]) if r["size"] is not None else "размер неизвестен"
        st.caption(f"{_md_escape(r['where'])} · {size_txt}  \n"
                   f"Статус: {_md_escape(_pre_status(r, calc))}")
        blob = _pre_file_bytes(r)
        if blob is not None:
            st.download_button(
                "Скачать файл", data=blob, file_name=r["base"],
                key=f"ca_fdlg_dl_{gid}_" + hashlib.md5(r["origin"]["path"].encode()).hexdigest()[:8],
                on_click="ignore", width="stretch",
            )
        else:
            st.caption("Не удалось прочитать файл из архива.")


# ─────────────────────────────────────────────────────────────────────────────
# Документ заявки после анализа — модальное окно из карты документов
# ─────────────────────────────────────────────────────────────────────────────
@st.dialog("Документ заявки", width="large", on_dismiss="rerun")
def _doc_dialog(row: Dict, data: Dict, project_id: Optional[str],
                key_prefix: str, sig: str) -> None:
    """
    Где лежит файл, к каким статьям привязан и почему, начало текста,
    скачивание. on_dismiss="rerun": после закрытия таблица карты
    перерисовывается без выделения — ту же строку можно открыть снова.
    """
    rid = row.get("key") or f"{row['archive']}|{row['origin']['path']}"
    rh  = hashlib.md5(rid.encode("utf-8")).hexdigest()[:8]

    st.markdown(f"📄 **{_md_escape(row['base'])}**")
    st.caption(f"{_md_escape(row['where'])}  \nСтатус: {_md_escape(row['status_text'])}")

    if row["links"]:
        st.markdown("**Подтверждает статьи затрат:**")
        for emoji, art_name, lvl, why in row["links"]:
            st.markdown(f"- {emoji} {_md_escape(art_name)} — **{lvl}**, {_md_escape(why)}")
    elif row["status"] == "read":
        st.caption(
            "Файл не подошёл ни к одной статье затрат. Возможно, он не нужен "
            "в заявке или по его названию и началу текста нельзя понять, "
            "к какой статье он относится."
        )

    if row["head"]:
        # Обычный текст в рамке с прокруткой: отключённое поле ввода
        # показывает текст бледно-серым — его трудно читать
        st.caption("Начало документа (как его прочитала система)")
        with st.container(border=True, height=220):
            st.text(row["head"])

    # Файл читается один раз при открытии окна (явное действие пользователя)
    cache = _map_cache(key_prefix, sig)
    prepared = cache.get("doc")
    if not (prepared and prepared[0] == rid):
        with st.spinner("Готовлю файл…"):
            prepared = (rid, _get_file_bytes(row, data, project_id))
        cache["doc"] = prepared   # в памяти держим только один файл
    if prepared[1] is not None:
        st.download_button(
            "Скачать файл", data=prepared[1], file_name=row["base"],
            key=f"{key_prefix}_dlg_dl_{sig}_{rh}", on_click="ignore", width="stretch",
        )
    else:
        st.warning("Файл недоступен: исходные файлы этой заявки не найдены "
                   "ни в текущей сессии, ни в реестре.")


# ─────────────────────────────────────────────────────────────────────────────
# Чтение документов: порядок и сообщения прогресса
# ─────────────────────────────────────────────────────────────────────────────
def _doc_order(calc_names: List[str]) -> List[str]:
    """
    Документы в том порядке, в котором их читает _build_file_summaries
    (ядро берёт первые _DOC_LIMIT из этого списка).
    """
    ss = st.session_state
    return [
        n for n in (ss.get("ca_uploaded_bytes") or {})
        if n not in calc_names and os.path.splitext(n.lower())[1] in _DOC_EXTS
    ]


def _doc_progress_msg(frac: float, fallback: str, order: List[str]) -> str:
    """«Читаю документ 5 из 40: Договор.pdf (Обоснования › Топливо)»."""
    n = len(order)
    if not n:
        return fallback
    i = max(1, min(n, int(round(frac * n))))
    org = _file_origin(order[i - 1], st.session_state.get("ca_file_origins") or {})
    where = " › ".join(s for s in org["folder"].split("/") if s)
    return f"Читаю документ {i} из {n}: {org['base']}" + (f"  ({where})" if where else "")


def _report_doc_reading(file_summaries: Dict[str, str], n_doc_files: int) -> None:
    """Итог чтения документов — сколько прочитано и что не вошло."""
    n_read = len(file_summaries or {})
    n_try  = min(n_doc_files, _DOC_LIMIT)
    if n_read:
        msg = f"Прочитано документов: **{n_read}** из {n_doc_files}."
        if n_try > n_read:
            msg += (f" Из {n_try - n_read} текст не извлечён — "
                    f"они отмечены в карте документов.")
        st.info(msg)
    else:
        st.caption("Документальные файлы не обработаны — анализ только по НПА.")
    if n_doc_files > _DOC_LIMIT:
        st.warning(
            f"В заявке {n_doc_files} документов, прочитаны первые {_DOC_LIMIT}. "
            f"Остальные не участвуют в сопоставлении со статьями затрат."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Откуда файл и почему он привязан к статье
# ─────────────────────────────────────────────────────────────────────────────
# Те же правила, что у лексического сопоставления в ядре
# (_match_files_to_article): ключевые слова статьи длиннее 3 символов
# без стоп-слов. Нужны только для объяснения пользователю.
_MATCH_STOP_WORDS = {
    "расходы", "затраты", "оплата", "труда", "итого",
    "прочие", "всего", "общие", "иные", "прочих",
}

# Общие слова из названий статей: совпадение только по ним ничего не говорит
# о содержании документа («Амортизация основных средств» ↔ «Дефектная
# ведомость основных средств»), поэтому такую привязку просим проверить
_WEAK_KEYWORDS = {
    "основных", "средств", "услуги", "услуг", "работы", "работ", "нужды", "нужд",
    "собственные", "собственных", "производственные", "производственных",
    "общехозяйственные", "общепроизводственные", "связанные", "другие", "других",
    "целей", "объектов", "имущества", "организации", "предприятия",
    "регулируемой", "деятельности", "товаров", "приобретение", "содержание",
}

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|~$])")


def _md_escape(text) -> str:
    """Экранирует markdown: имена файлов и статей выводятся как есть."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def _excerpt(text: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _make_origin(archive: str, path: str) -> Dict[str, str]:
    path  = (path or "").replace("\\", "/").strip("/")
    parts = [p for p in path.split("/") if p]
    return {
        "archive": archive or "",
        "path":    path,
        "folder":  "/".join(parts[:-1]),
        "base":    parts[-1] if parts else path,
    }


def _file_origin(name: str, origins: Dict) -> Dict[str, str]:
    """
    Откуда файл: архив, папка внутри архива, имя.
    Для результатов без карты происхождения разбираем само имя
    («Архив.zip/Папка/Файл.pdf» — формат предыдущей версии).
    """
    o = (origins or {}).get(name)
    if o:
        return _make_origin(o.get("archive", ""), o.get("path") or name)
    m = re.match(r"^(.+?\.zip)/(.+)$", name or "", re.IGNORECASE)
    if m:
        return _make_origin(m.group(1), m.group(2))
    return _make_origin("", name or "")


def _loc_label(org: Dict[str, str], icons: bool = True) -> str:
    """«📦 Заявка.zip › Обоснования › Топливо» / «загружен отдельным файлом»."""
    segs = [s for s in org["folder"].split("/") if s]
    if org["archive"]:
        return " › ".join([f"📦 {org['archive']}" if icons else org["archive"]] + segs)
    if segs:
        return ("📁 " if icons else "") + " › ".join(segs)
    return "загружен отдельным файлом"


def _match_reason(article_name: str, file_name: str, head: str,
                  similarity) -> Tuple[str, str]:
    """
    Насколько надёжна привязка файла к статье и почему.
    Возвращает (уровень, пояснение):
      «Надёжно»   — слово из статьи есть в названии файла или папки;
      «Вероятно»  — слово есть в тексте документа или смысл очень близок;
      «Проверьте» — совпало только общее слово или привязка
                    лишь по смысловому сходству.
    """
    try:
        sim = float(similarity or 0)
    except (TypeError, ValueError):
        sim = 0.0
    if sim >= 0.999:
        kws = [w for w in re.split(r"\W+", (article_name or "").lower())
               if len(w) > 3 and w not in _MATCH_STOP_WORDS]
        strong = [k for k in kws if k not in _WEAK_KEYWORDS]
        fl = (file_name or "").lower()
        hl = (head or "").lower()
        kw = next((k for k in strong if k in fl), None)
        if kw:
            return "Надёжно", f"в названии файла или папки есть «{kw}»"
        kw = next((k for k in strong if k in hl), None)
        if kw:
            return "Вероятно", f"в тексте документа встречается «{kw}»"
        kw = next((k for k in kws if k in fl or k in hl), None)
        if kw:
            return "Проверьте", f"совпало только общее слово «{kw}»"
        return "Вероятно", "совпадение по ключевым словам"
    pct = int(round(sim * 100))
    if sim >= _SIM_HIGH:
        return "Вероятно", f"содержание близко по смыслу ({pct}%)"
    return "Проверьте", f"сходство по смыслу {pct}% — откройте файл и убедитесь"


# ─────────────────────────────────────────────────────────────────────────────
# Карта документов: данные для результатов анализа
# ─────────────────────────────────────────────────────────────────────────────
def _enrich_risks_json(risks_json: str, calc_names: List[str]) -> str:
    """
    Дописывает в JSON результатов то, что нужно карте документов,
    чтобы она работала и после сохранения в реестр:
      documents     — все файлы анализа со статусом и началом текста;
      file_origins  — архив и путь внутри архива для каждого файла;
      skipped_files — файлы архивов, не участвовавшие в анализе;
      bundle_id     — метка набора файлов текущей сессии.
    """
    ss = st.session_state
    try:
        data = json.loads(risks_json)
    except Exception:
        return risks_json
    if not isinstance(data, dict) or "articles" not in data:
        return risks_json

    heads     = ss.get("ca_file_summaries") or {}
    origins   = ss.get("ca_file_origins") or {}
    names     = list((ss.get("ca_uploaded_bytes") or {}).keys())
    attempted = set(_doc_order(calc_names)[:_DOC_LIMIT])

    documents = []
    for n in names:
        ext = os.path.splitext(n.lower())[1]
        if n in calc_names:
            code = "calc"
        elif ext in _CALC_EXTS:
            code = "excel"
        elif ext in _DOC_EXTS:
            code = "read" if n in heads else ("empty" if n in attempted else "limit")
        else:
            continue
        documents.append({
            "name":   n,
            "status": code,
            "head":   (heads.get(n) or "")[:_MAP_HEAD_CHARS],
        })

    data["documents"]     = documents
    data["file_origins"]  = {n: origins[n] for n in names if n in origins}
    data["skipped_files"] = [
        {"archive": a, "path": p, "reason": why}
        for a, p, why in (ss.get("ca_zip_skipped") or [])
    ]
    data["bundle_id"] = ss.get("ca_bundle_id", "")
    return json.dumps(data, ensure_ascii=False)


def _article_reg_val(a: Dict) -> Optional[float]:
    rv = a.get("reg_val")
    if rv is None:
        ts = _parse_amounts_timeseries(a.get("amounts", ""))
        rv = ts[-1][2] if ts else None
    try:
        return float(rv) if rv is not None else None
    except (TypeError, ValueError):
        return None


def _file_status_text(code: str, n_links: int, reason: str = "") -> str:
    if code == "read":
        return (f"подтверждает статей: {n_links}" if n_links
                else "не подошёл ни к одной статье")
    if code == "empty":
        return "текст не извлечён (пустой файл или нераспознанный скан)"
    if code == "limit":
        return f"не прочитан: в сопоставлении участвуют первые {_DOC_LIMIT} документов"
    if code == "calc":
        return "расчётная модель — из неё взяты статьи затрат"
    if code == "excel":
        return "Excel без отметки «расч.» — не анализировался"
    if code == "skipped":
        return f"не анализировался: {reason}" if reason else "не анализировался"
    return "—"


def _build_mapping(data: Dict) -> Dict:
    """Связи «статья ↔ файл» в обе стороны + сводные цифры."""
    articles  = data.get("articles") or []
    origins   = data.get("file_origins") or {}
    documents = data.get("documents")
    legacy    = documents is None

    links: Dict[str, list] = {}
    for a in articles:
        for m in a.get("matched_files") or []:
            fn = m.get("file_name")
            if fn:
                links.setdefault(fn, []).append((a, m))

    if legacy:
        # Результаты предыдущих версий: известны только привязанные файлы
        documents = [
            {"name": fn, "status": "read", "head": (lst[0][1].get("summary") or "")}
            for fn, lst in links.items()
        ]

    # ── По статьям ───────────────────────────────────────────────────────────
    art_rows: List[Dict] = []
    nodoc:    List[Dict] = []
    for a in sorted(articles, key=lambda x: (_RISK_ORDER.get(x.get("risk"), 3),
                                             -(_article_reg_val(x) or 0))):
        risk = a.get("risk", "")
        base = {
            "risk":      risk,
            "emoji":     _RISK_EMOJI.get(risk, "⚪"),
            "risk_text": _RISK_TEXT.get(risk, "—"),
            "article":   a.get("name", "—"),
            "reg_val":   _article_reg_val(a),
        }
        mf = a.get("matched_files") or []
        if not mf:
            nodoc.append(base)
            art_rows.append({**base, "key": "", "base": "", "archive": "",
                             "folder": "", "where": "", "level": "", "why": ""})
            continue
        for m in mf:
            fn  = m.get("file_name", "")
            org = _file_origin(fn, origins)
            lvl, why = _match_reason(base["article"], fn, m.get("summary", ""),
                                     m.get("_similarity", 0))
            art_rows.append({**base, "key": fn, "base": org["base"],
                             "archive": org["archive"], "folder": org["folder"],
                             "where": _loc_label(org), "level": lvl, "why": why})

    # ── По файлам ────────────────────────────────────────────────────────────
    file_rows: List[Dict] = []
    n_read = n_linked = 0
    for d in documents:
        fn   = d.get("name", "")
        org  = _file_origin(fn, origins)
        code = d.get("status", "read")
        lk   = links.get(fn, [])
        if code == "read":
            n_read += 1
            if lk:
                n_linked += 1
        file_rows.append({
            "key":     fn,
            "origin":  org,
            "base":    org["base"],
            "archive": org["archive"],
            "folder":  org["folder"],
            "where":   _loc_label(org),
            "status":  code,
            "status_text": _file_status_text(code, len(lk)),
            "links": [
                (_RISK_EMOJI.get(a.get("risk"), "⚪"), a.get("name", "—"),
                 *_match_reason(a.get("name", ""), fn, m.get("summary", ""),
                                m.get("_similarity", 0)))
                for a, m in lk
            ],
            "articles_text": "; ".join(
                f"{_RISK_EMOJI.get(a.get('risk'), '⚪')} {a.get('name', '—')}" for a, _ in lk
            ),
            "head": d.get("head") or "",
        })
    for s in data.get("skipped_files") or []:
        org = _make_origin(s.get("archive", ""), s.get("path", ""))
        file_rows.append({
            "key": "", "origin": org, "base": org["base"], "archive": org["archive"],
            "folder": org["folder"], "where": _loc_label(org), "status": "skipped",
            "status_text": _file_status_text("skipped", 0, s.get("reason", "")),
            "links": [], "articles_text": "", "head": "",
        })
    # Порядок как в архиве: архив → папка → файл; отдельные файлы в конце
    file_rows.sort(key=lambda r: (r["archive"] == "", r["archive"].lower(),
                                  r["folder"].lower(), r["base"].lower()))

    has_docs = any(r["status"] in ("read", "empty", "limit") for r in file_rows)
    return {
        "art_rows":   art_rows,
        "file_rows":  file_rows,
        "nodoc":      nodoc,
        "n_art":      len(articles),
        "n_art_with": len(articles) - len(nodoc),
        "n_docs_read": n_read,
        "n_linked":   n_linked,
        "n_unlinked": n_read - n_linked,
        "legacy":     legacy,
        "has_docs":   has_docs,
    }


def _mapping_xlsx(mp: Dict) -> bytes:
    """Карта соответствия в Excel: по статьям, по файлам, статьи без документов."""
    import pandas as pd

    art = [{
        "Риск":                r["risk_text"],
        "Статья затрат":       r["article"],
        "Рег. год, тыс.руб.":  r["reg_val"],
        "Документ":            r["base"] or "нет документов",
        "Архив":               r["archive"],
        "Папка в архиве":      r["folder"].replace("/", " › "),
        "Соответствие":        r["level"],
        "Почему":              r["why"],
    } for r in mp["art_rows"]]
    files = [{
        "Архив":           r["archive"] or "загружен отдельным файлом",
        "Папка в архиве":  r["folder"].replace("/", " › "),
        "Файл":            r["base"],
        "Статус":          r["status_text"],
        "Статьи затрат":   "; ".join(lk[1] for lk in r["links"]),
        "Начало документа": _excerpt(r["head"], 300),
    } for r in mp["file_rows"]]
    nodoc = [{
        "Риск":               r["risk_text"],
        "Статья затрат":      r["article"],
        "Рег. год, тыс.руб.": r["reg_val"],
    } for r in mp["nodoc"]]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(art, columns=list(art[0].keys()) if art else
                     ["Риск", "Статья затрат", "Рег. год, тыс.руб.", "Документ",
                      "Архив", "Папка в архиве", "Соответствие", "Почему"]
                     ).to_excel(writer, sheet_name="По статьям", index=False)
        pd.DataFrame(files, columns=["Архив", "Папка в архиве", "Файл", "Статус",
                                     "Статьи затрат", "Начало документа"]
                     ).to_excel(writer, sheet_name="По файлам", index=False)
        pd.DataFrame(nodoc, columns=["Риск", "Статья затрат", "Рег. год, тыс.руб."]
                     ).to_excel(writer, sheet_name="Статьи без документов", index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col[:300] if c.value is not None),
                            default=8)
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, width + 2))
    return buf.getvalue()


def _get_file_bytes(row: Dict, data: Dict, project_id: Optional[str]) -> Optional[bytes]:
    """
    Байты файла для скачивания — только по явному клику.
    Сначала из текущей сессии (если результаты относятся к её файлам),
    затем из реестра: исходный архив читается с диска и из него
    достаётся один нужный файл.
    """
    ss  = st.session_state
    org = row["origin"]
    key = row.get("key") or ""

    same_bundle = bool(data.get("bundle_id")) and data.get("bundle_id") == ss.get("ca_bundle_id")
    if same_bundle:
        b = (ss.get("ca_uploaded_bytes") or {}).get(key) if key else None
        if b:
            return b
        if org["archive"]:
            for fd in ss.get("ca_registry_files") or []:
                if fd.get("name") == org["archive"]:
                    b = read_member(fd.get("bytes", b""), org["path"])
                    if b is not None:
                        return b

    if project_id:
        try:
            from core.claim_registry import get_file_path
            if org["archive"]:
                zpath = get_file_path(project_id, org["archive"])
                if zpath:
                    with open(zpath, "rb") as fh:
                        return read_member(fh.read(), org["path"])
            else:
                fpath = get_file_path(project_id, org["path"] or key)
                if fpath:
                    with open(fpath, "rb") as fh:
                        return fh.read()
        except Exception as e:
            print(f"[DOC_MAP] Ошибка чтения файла из реестра: {e}")
    return None


def _map_cache(key_prefix: str, sig: str) -> Dict:
    """Кеш карты (Excel, подготовленный файл) — сбрасывается с новыми результатами."""
    ck = f"{key_prefix}_mapcache"
    c = st.session_state.get(ck)
    if not isinstance(c, dict) or c.get("sig") != sig:
        c = {"sig": sig, "xlsx": None, "doc": None}
        st.session_state[ck] = c
    return c


def _df_height(n_rows: int) -> int:
    return min(420, 35 * (n_rows + 1) + 3)


def _render_doc_map(data: Dict, key_prefix: str,
                    project_id: Optional[str], sig: str) -> None:
    """Раздел «Документы заявки ↔ статьи затрат»."""
    import pandas as pd

    mp = _build_mapping(data)
    st.markdown("#### Документы заявки ↔ статьи затрат")

    if not mp["has_docs"] and not mp["n_linked"]:
        skipped_rows = [r for r in mp["file_rows"] if r["status"] == "skipped"]
        if skipped_rows:
            # Например, архив из одних jpg-сканов: объясняем, почему карта пуста
            st.caption(
                "В архиве нет документов в поддерживаемых форматах (PDF, DOCX, DOC, TXT) — "
                "сопоставлять со статьями нечего. Эти файлы в анализе не участвовали:"
            )
            st.dataframe(
                pd.DataFrame([{"Где лежит": r["where"], "Файл": r["base"],
                               "Статус": r["status_text"]} for r in skipped_rows]),
                hide_index=True, width="stretch", height=_df_height(len(skipped_rows)),
            )
        else:
            st.caption(
                "Документы-обоснования (PDF, DOCX, DOC, TXT) в заявке не найдены — "
                "сопоставлять со статьями нечего. Загрузите архив заявки целиком, "
                "чтобы увидеть, какой файл подтверждает какую статью."
            )
        return

    st.caption(
        "Какой файл заявки подтверждает какую статью затрат, какие статьи "
        "остались без документов и какие файлы не подошли ни к одной статье."
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Статей с документами", f"{mp['n_art_with']} из {mp['n_art']}")
    m2.metric("Статей без документов", len(mp["nodoc"]))
    m3.metric("Файлов привязано", f"{mp['n_linked']} из {mp['n_docs_read']}")
    m4.metric("Файлов не привязано", mp["n_unlinked"],
              help="Прочитанные документы, которые не подошли ни к одной статье затрат")

    red_nodoc = [a for a in mp["nodoc"] if a["risk"] == "red"]
    if red_nodoc:
        names = "; ".join(_md_escape(a["article"]) for a in red_nodoc[:5])
        more  = f" и ещё {len(red_nodoc) - 5}" if len(red_nodoc) > 5 else ""
        st.warning(
            f"Высокий риск и нет документов-обоснований — "
            f"{len(red_nodoc)} стат.: {names}{more}"
        )

    if mp["legacy"]:
        st.caption(
            "Заявка проанализирована до появления карты документов: показаны только "
            "файлы, привязанные к статьям. Перезапустите анализ, чтобы увидеть все файлы."
        )

    # Ленивые вкладки: выполняется только открытая. Клик по строке любой
    # таблицы открывает документ в модальном окне (_doc_dialog).
    st.caption("Нажмите на строку таблицы, чтобы открыть документ в отдельном окне.")
    t_art, t_file = _lazy_tabs(
        ["По статьям", "По файлам архива"],
        key=f"{key_prefix}_map_tabs",
    )
    ss = st.session_state
    # Версия ключа таблиц: после открытия окна следующий полный прогон рисует
    # таблицы без выделения — иначе окно открывалось бы на каждом прогоне,
    # а повторный клик по той же строке снимал бы выделение вместо открытия
    selv = ss.get(f"{key_prefix}_map_selv", 0)
    open_row = None

    # ── По статьям ───────────────────────────────────────────────────────────
    with t_art:
        if _tab_open(t_art):
            flt = st.radio(
                "Показать", ["Все статьи", "Без документов", "С документами"],
                horizontal=True, key=f"{key_prefix}_map_af", label_visibility="collapsed",
            )
            rows = mp["art_rows"]
            if flt == "Без документов":
                rows = [r for r in rows if not r["key"]]
            elif flt == "С документами":
                rows = [r for r in rows if r["key"]]
            if rows:
                df = pd.DataFrame([{
                    "Риск":               r["emoji"],
                    "Статья затрат":      r["article"],
                    "Рег. год, тыс.руб.": r["reg_val"],
                    "Документ":           r["base"] or "— нет документов —",
                    "Где лежит":          r["where"],
                    "Соответствие":       r["level"],
                    "Почему":             r["why"],
                } for r in rows])
                event = st.dataframe(
                    df, hide_index=True, width="stretch",
                    height=_df_height(len(df)), placeholder="—",
                    key=f"{key_prefix}_map_atab_{sig}_{selv}",
                    on_select="rerun", selection_mode="single-row",
                    column_config={
                        "Риск":               st.column_config.TextColumn("Риск", width="small"),
                        "Статья затрат":      st.column_config.TextColumn("Статья затрат", width="large"),
                        "Рег. год, тыс.руб.": st.column_config.NumberColumn(
                            "Рег. год, тыс.руб.", format="localized", width="small"),
                        "Документ":           st.column_config.TextColumn("Документ", width="large"),
                        "Где лежит":          st.column_config.TextColumn("Где лежит", width="medium"),
                        "Соответствие":       st.column_config.TextColumn("Соответствие", width="small"),
                        "Почему":             st.column_config.TextColumn("Почему", width="medium"),
                    },
                )
                st.caption(
                    "«Надёжно» — слово из статьи есть в названии файла или папки; "
                    "«Вероятно» — в тексте документа или содержание очень близко по смыслу; "
                    "«Проверьте» — совпало только общее слово или есть лишь смысловое сходство."
                )
                sel = _sel_rows(event)
                if sel and sel[0] < len(rows):
                    picked = rows[sel[0]]
                    if picked["key"]:
                        open_row = next((fr for fr in mp["file_rows"]
                                         if fr["key"] == picked["key"]), None)
                    else:
                        st.toast("Для этой статьи документы в заявке не найдены.")
                        ss[f"{key_prefix}_map_selv"] = selv + 1
            else:
                st.caption("Нет статей по выбранному фильтру.")

    # ── По файлам архива ─────────────────────────────────────────────────────
    with t_file:
        if _tab_open(t_file):
            flt_f = st.radio(
                "Показать", ["Все файлы", "Не привязанные", "Привязанные"],
                horizontal=True, key=f"{key_prefix}_map_ff", label_visibility="collapsed",
            )
            rows_f = mp["file_rows"]
            if flt_f == "Не привязанные":
                rows_f = [r for r in rows_f if not r["links"] and r["status"] != "calc"]
            elif flt_f == "Привязанные":
                rows_f = [r for r in rows_f if r["links"]]
            if rows_f:
                df_f = pd.DataFrame([{
                    "Где лежит":     r["where"],
                    "Файл":          r["base"],
                    "Статьи затрат": r["articles_text"] or "—",
                    "Статус":        r["status_text"],
                } for r in rows_f])
                event_f = st.dataframe(
                    df_f, hide_index=True, width="stretch",
                    height=_df_height(len(df_f)),
                    key=f"{key_prefix}_map_ftab_{sig}_{selv}",
                    on_select="rerun", selection_mode="single-row",
                    column_config={
                        "Где лежит":     st.column_config.TextColumn("Где лежит", width="medium"),
                        "Файл":          st.column_config.TextColumn("Файл", width="large"),
                        "Статьи затрат": st.column_config.TextColumn("Статьи затрат", width="large"),
                        "Статус":        st.column_config.TextColumn("Статус", width="medium"),
                    },
                )
                st.caption("Файлы перечислены в порядке папок архива.")
                sel_f = _sel_rows(event_f)
                if sel_f and sel_f[0] < len(rows_f):
                    open_row = rows_f[sel_f[0]]
            else:
                st.caption("Нет файлов по выбранному фильтру.")

    if open_row is not None:
        ss[f"{key_prefix}_map_selv"] = selv + 1
        _try_open(_doc_dialog, open_row, data, project_id, key_prefix, sig)

    # ── Выгрузка карты в Excel ───────────────────────────────────────────────
    cache = _map_cache(key_prefix, sig)
    if cache.get("xlsx"):
        st.download_button(
            "Скачать карту соответствия (Excel)",
            data=cache["xlsx"],
            file_name="карта_документов_заявки.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key_prefix}_map_xlsx_dl_{sig}",
        )
    elif st.button("Сформировать карту соответствия в Excel",
                   key=f"{key_prefix}_map_xlsx_{sig}"):
        _xlsx_err = ""
        try:
            cache["xlsx"] = _mapping_xlsx(mp)
        except Exception as e:
            _xlsx_err = str(e)
        if _xlsx_err:
            st.error(f"Не удалось сформировать Excel: {_xlsx_err}")
        else:
            st.rerun()


def _render_risks_tab(risks_json: str, claim_summary: str = "", show_summary: bool = True,
                      key_prefix: str = "ca", project_id: Optional[str] = None):
    """
    Кастомный рендеринг постатейного анализа рисков.
    risks_json    — строка JSON от analyze_risks() или старый markdown.
    claim_summary — итоговое резюме заявки (отображается над списком статей).
    project_id    — ID заявки в реестре: из её сохранённого архива карта
                    документов достаёт файл для скачивания.
    """
    data = None
    try:
        data = json.loads(risks_json)
    except Exception:
        pass

    if data is None or "articles" not in data:
        st.markdown(risks_json)
        return

    articles   = data.get("articles", [])
    stats      = data.get("stats", {})
    rag_note   = data.get("rag_note", "")
    _reg_year  = data.get("reg_year", 0)
    _tgt_pct   = data.get("target_pct", 5.0)
    origins    = data.get("file_origins") or {}
    _sig       = hashlib.md5(risks_json.encode("utf-8", "ignore")).hexdigest()[:12]
    # Были ли в заявке документы вообще: без них сообщение «не найдено
    # документов» у каждой статьи — шум
    _docs_meta = data.get("documents")
    _has_docs  = (
        any(d.get("status") in ("read", "empty", "limit") for d in _docs_meta)
        if _docs_meta is not None
        else any(a.get("matched_files") for a in articles)
    )

    # ── Резюме заявки (над списком статей) ───────────────────────────────────
    if show_summary:
        if claim_summary:
            st.subheader("Резюме заявки")
            st.markdown(claim_summary)
            st.divider()
        elif data.get("summary"):
            st.subheader("Резюме заявки")
            st.markdown(data["summary"])
            st.divider()

    # ── Визуализация состава статей (matplotlib) ──────────────────────────────
    _viz_rows = []
    for _a in articles:
        _rv = _a.get("reg_val")
        if _rv is None:
            _ts = _parse_amounts_timeseries(_a.get("amounts", ""))
            _rv = _ts[-1][2] if _ts else 0
        # ВАЖНО: фильтруем по округлённому значению, а не по сырому float.
        # Иначе статья с исходным значением вроде 0.3 тыс.руб. проходит фильтр
        # ">0", но после round() превращается в 0 — и тогда в _sq() на
        # последнем шаге рекурсии total может стать равным rS при непустом
        # остатке items, что даёт ZeroDivisionError при rS / total.
        if _rv and float(_rv) > 0:
            _rounded_val = round(float(_rv))
            if _rounded_val > 0:
                _viz_rows.append({
                    "name":  _a.get("name", "")[:45],
                    "value": _rounded_val,
                    "risk":  _a.get("risk", "gray"),
                    "sheet": _a.get("sheet", ""),
                })
    if _viz_rows:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.ticker as _mticker

        # Фирменная палитра
        _RISK_FACE = {
            "red":    "#F5DDD6",
            "yellow": "#F7ECCF",
            "green":  "#DCEAE2",
            "gray":   "#D6E0E4",
        }
        _RISK_EDGE = {
            "red":    "#CF6B5A",
            "yellow": "#E0B354",
            "green":  "#5FA37E",
            "gray":   "#2E6276",
        }
        _RISK_LBL  = {
            "red":    "Высокий риск",
            "yellow": "Средний риск",
            "green":  "Без замечаний",
            "gray":   "Не оценено",
        }

        def _wst(row, s, w, h, total):
            a = s / total * w * h if total else 1
            sc = a / s if s else 1
            mx = max(d["value"] for d in row)
            mn = min(d["value"] for d in row)
            side = min(w, h)
            try:
                return max(side**2 * mx * sc / a**2, a**2 / (side**2 * mn * sc))
            except ZeroDivisionError:
                return float("inf")

        def _sq(items, x, y, w, h, total, out):
            if not items:
                return
            # ── Защита от деления на ноль ────────────────────────────────
            # total может стать <= 0 на глубоких уровнях рекурсии, даже
            # если items непустой: это происходит когда сумма значений
            # оставшихся элементов (total) рассинхронизировалась с их
            # фактическим весом (из-за округления value ещё на этапе
            # построения _viz_rows) или когда все оставшиеся элементы
            # имеют value=0. В обоих случаях пропорциональное разбиение
            # невозможно — просто раскладываем элементы равными долями,
            # чтобы не упасть с ZeroDivisionError.
            if len(items) == 1 or total <= 0:
                if len(items) == 1:
                    out.append((items[0], x, y, w, h))
                else:
                    n = len(items)
                    if w >= h:
                        step = w / n
                        cx = x
                        for d in items:
                            out.append((d, cx, y, step, h))
                            cx += step
                    else:
                        step = h / n
                        cy = y
                        for d in items:
                            out.append((d, x, cy, w, step))
                            cy += step
                return
            row, rS = [items[0]], items[0]["value"]
            for i in range(1, len(items)):
                nr, ns = row + [items[i]], rS + items[i]["value"]
                if _wst(row, rS, w, h, total) >= _wst(nr, ns, w, h, total):
                    row, rS = nr, ns
                else:
                    break
            rf, rest = rS / total, items[len(row):]
            if w >= h:
                rw, cy = w * rf, y
                for d in row:
                    ch = h * (d["value"] / rS) if rS else h / len(row)
                    out.append((d, x, cy, rw, ch))
                    cy += ch
                _sq(rest, x + rw, y, w - rw, h, total - rS, out)
            else:
                rh, cx = h * rf, x
                for d in row:
                    cw = w * (d["value"] / rS) if rS else w / len(row)
                    out.append((d, cx, y, cw, rh))
                    cx += cw
                _sq(rest, x, y + rh, w, h - rh, total - rS, out)

        with st.expander("📊 Визуализация статей затрат", expanded=True):
          _vtab1, _vtab2 = st.tabs(["Карта затрат", "Топ-20"])

          with _vtab1:
            _sorted = sorted(_viz_rows, key=lambda x: -x["value"])
            _total  = sum(d["value"] for d in _sorted)
            _rects  = []
            if _total > 0:
                _sq(_sorted, 0, 0, 1, 1, _total, _rects)

            fig1, ax1 = plt.subplots(figsize=(14, 6))
            ax1.set_xlim(0, 1)
            ax1.set_ylim(0, 1)
            ax1.axis("off")
            fig1.patch.set_facecolor("#F8F9FA")
            ax1.set_facecolor("#F8F9FA")

            for (d, x, y, w, h) in _rects:
                ax1.add_patch(mpatches.FancyBboxPatch(
                    (x + 0.003, y + 0.003), w - 0.006, h - 0.006,
                    boxstyle="round,pad=0.002",
                    facecolor=_RISK_FACE[d["risk"]],
                    edgecolor=_RISK_EDGE[d["risk"]],
                    linewidth=1.2,
                ))
                if w > 0.06 and h > 0.04:
                    fs = max(5, min(9, w * 52))
                    lbl = d["name"] if w > 0.18 else d["name"][:16] + "…"
                    ax1.text(x + w / 2, y + h / 2 + 0.013, lbl,
                             ha="center", va="center", fontsize=fs,
                             fontweight="bold",
                             color=_RISK_EDGE[d["risk"]], clip_on=True)
                    ax1.text(x + w / 2, y + h / 2 - 0.013,
                             f"{d['value']:,.0f} тыс.",
                             ha="center", va="center",
                             fontsize=max(4, fs - 1.5),
                             color="#555555")

            _handles1 = [
                mpatches.Patch(facecolor=_RISK_FACE[k],
                               edgecolor=_RISK_EDGE[k], label=_RISK_LBL[k])
                for k in ["red", "yellow", "green", "gray"]
                if any(d["risk"] == k for d in _viz_rows)
            ]
            ax1.legend(handles=_handles1, loc="lower center",
                       bbox_to_anchor=(0.5, -0.06), ncol=4,
                       fontsize=8, framealpha=0.0)
            plt.tight_layout(pad=0.3)
            st.pyplot(fig1, use_container_width=True)
            plt.close(fig1)

          with _vtab2:
            _top20 = sorted(_viz_rows, key=lambda x: -x["value"])[:20]
            _rev   = list(reversed(_top20))
            fig2, ax2 = plt.subplots(figsize=(14, max(5, len(_top20) * 0.44)))
            fig2.patch.set_facecolor("#F8F9FA")
            ax2.set_facecolor("#F8F9FA")
            ax2.barh(
                [d["name"] for d in _rev],
                [d["value"] for d in _rev],
                color=[_RISK_FACE[d["risk"]] for d in _rev],
                edgecolor=[_RISK_EDGE[d["risk"]] for d in _rev],
                linewidth=1.0,
                height=0.65,
            )
            ax2.set_xlabel("тыс.руб.", color="#555555", fontsize=9)
            ax2.xaxis.set_major_formatter(
                _mticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
            )
            ax2.spines[["top", "right", "left"]].set_visible(False)
            ax2.spines["bottom"].set_color("#CCCCCC")
            ax2.tick_params(axis="y", labelsize=8.5, colors="#333333")
            ax2.tick_params(axis="x", colors="#888888", labelsize=8)
            ax2.xaxis.set_tick_params(length=0)
            for i, d in enumerate(_rev):
                ax2.text(d["value"] * 1.008, i,
                         f"{d['value']:,.0f}",
                         va="center", fontsize=7.5,
                         color=_RISK_EDGE[d["risk"]])
            _handles2 = [
                mpatches.Patch(facecolor=_RISK_FACE[k],
                               edgecolor=_RISK_EDGE[k], label=_RISK_LBL[k])
                for k in ["red", "yellow", "green", "gray"]
                if any(d["risk"] == k for d in _top20)
            ]
            ax2.legend(handles=_handles2, fontsize=8,
                       loc="lower right", framealpha=0.0)
            plt.tight_layout(pad=0.5)
            st.pyplot(fig2, use_container_width=True)
            plt.close(fig2)

        st.divider()


    # ── Сводная шапка ────────────────────────────────────────────────────────
    n_red    = stats.get("red", 0)
    n_yellow = stats.get("yellow", 0)
    n_green  = stats.get("green", 0)
    n_total  = stats.get("total", len(articles))

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Всего статей",  n_total)
    col2.metric("🔴 Высокий риск", n_red)
    col3.metric("🟡 Средний риск", n_yellow)
    col4.metric("🟢 Без замечаний", n_green)

    if n_red > 0:
        st.error(f"ВЫСОКИЙ РИСК — {n_red} статей с превышением критического порога")
    elif n_yellow > 0:
        st.warning(f"СРЕДНИЙ РИСК — {n_yellow} статей с превышением целевого индекса")
    else:
        st.success("НИЗКИЙ РИСК — рост статей в пределах целевого индекса")

    if rag_note:
        st.caption(rag_note)

    st.divider()

    # ── Карта документов: какой файл к какой статье ─────────────────────────
    _render_doc_map(data, key_prefix, project_id, _sig)

    st.divider()

    # ── Фильтр ───────────────────────────────────────────────────────────────
    st.markdown("**Постатейный анализ**")
    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
    show_red    = f_col1.checkbox("🔴 Высокий риск", value=True,  key=f"{key_prefix}_f_red")
    show_yellow = f_col2.checkbox("🟡 Средний риск",  value=True,  key=f"{key_prefix}_f_yellow")
    show_green  = f_col3.checkbox("🟢 Без замечаний", value=False, key=f"{key_prefix}_f_green")
    only_nodoc  = f_col4.checkbox(
        "📎 Только без документов", value=False, key=f"{key_prefix}_f_nodoc",
        help="Статьи, для которых в заявке не найдено ни одного документа-обоснования",
    )
    filter_map  = {"red": show_red, "yellow": show_yellow,
                   "green": show_green}
    visible = [
        a for a in articles
        if filter_map.get(a.get("risk", "red"), True)
        and (not only_nodoc or not a.get("matched_files"))
    ]
    st.caption(f"Показано: {len(visible)} из {n_total}")

    RISK_COLOR = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
    RISK_LABEL = {"red": "Высокий риск", "yellow": "Средний риск",
                  "green": "Без замечаний"}

    for art in visible:
        risk          = art.get("risk", "unknown")
        emoji         = RISK_COLOR[risk]
        label         = RISK_LABEL[risk]
        name          = art.get("name", "—")
        amounts       = art.get("amounts", "")
        basis         = art.get("basis", "")
        rec           = art.get("recommendation", "")
        dynamics      = art.get("article_summary", "")
        growth_reason = art.get("growth_reason", "")
        base_val      = art.get("base_val")
        reg_val       = art.get("reg_val")
        has_npa       = art.get("has_npa", False)
        matched_files = art.get("matched_files") or []

        # Заголовок: emoji + название + значение регулируемого года
        # + сколько документов-обоснований нашлось в заявке
        exp_title = f"{emoji} {name[:70]}"
        if reg_val is not None:
            exp_title += f"  ·  {reg_val:,.0f} тыс.руб."
        elif amounts:
            first_val = amounts.split("|")[0].strip()
            if first_val:
                exp_title += f"  ·  {first_val[:40]}"
        if matched_files:
            exp_title += f"  ·  📎 {len(matched_files)}"
        elif _has_docs:
            exp_title += "  ·  без документов"

        with st.expander(exp_title, expanded=(risk in ("red", "yellow"))):

            # Две колонки: 2/3 — аналитика, 1/3 — временной ряд цифрами
            c_left, c_right = st.columns([2, 1])

            # ── Правая колонка: временной ряд ────────────────────────────────
            with c_right:
                if amounts:
                    ts = _parse_amounts_timeseries(amounts)
                    if ts:
                        for yr, lbl, val in ts:
                            marker = "→" if (reg_val is not None and
                                             abs(val - reg_val) < 0.01) else " "
                            st.caption(f"{marker} {yr} ({lbl}): {val:,.0f} тыс.руб.")
                    else:
                        for v in amounts.split("|")[:5]:
                            st.caption(v.strip())

            # ── Левая колонка: статус, текст, график, НПА ────────────────────
            with c_left:
                st.markdown(f"**{emoji} {label}**")
                if growth_reason:
                    st.caption(f"Индекс роста: {growth_reason}")

                # График под текстом
                if amounts:
                    ts = _parse_amounts_timeseries(amounts)
                    if ts and len(ts) >= 2:
                        svg = _render_timeseries_chart(
                            ts, _reg_year, risk, target_pct=_tgt_pct,
                        )
                        if svg:
                            st.markdown(svg, unsafe_allow_html=True)

                verdict = art.get("verdict") or basis
                if verdict:
                    st.markdown(f"**Вердикт:** {verdict}")

                if rec:
                    st.info(f"**Что необходимо обосновать:** {rec}")

                # ── Документы-обоснования: какой файл, где лежит, почему ─────
                if matched_files:
                    st.markdown(f"**Документы-обоснования в заявке ({len(matched_files)}):**")
                    for d in matched_files:
                        _fn  = d.get("file_name", "—")
                        _org = _file_origin(_fn, origins)
                        _lvl, _why = _match_reason(
                            name, _fn, d.get("summary", ""), d.get("_similarity", 0)
                        )
                        _ex = _excerpt(d.get("summary", ""), 160)
                        st.markdown(f"📄 **{_md_escape(_org['base'])}** · {_lvl}")
                        st.caption(
                            f"{_md_escape(_loc_label(_org))}  \n"
                            f"Почему: {_md_escape(_why)}"
                            + (f"  \nНачало документа: «{_md_escape(_ex)}»" if _ex else "")
                        )
                elif not _has_docs:
                    st.caption("📎 Документы-обоснования в заявку не загружены.")
                elif risk in ("red", "yellow"):
                    st.warning(
                        "📎 В заявке не найдено документов, подтверждающих эту статью. "
                        "Без обоснования затраты могут быть не приняты регулятором."
                    )
                else:
                    st.caption("📎 Документы-обоснования в заявке не найдены.")

                if not has_npa:
                    st.caption("НПА по этой статье в базе знаний не найдены.")

    # ── Скачать замечания ─────────────────────────────────────────────────────
    st.divider()
    problem_articles = [a for a in articles if a.get("risk") in ("red", "yellow")]
    if problem_articles:
        lines = [f"АНАЛИЗ РИСКОВ ТАРИФНОЙ ЗАЯВКИ\n{'='*50}\n"]
        for a in problem_articles:
            gr = a.get("growth_reason", "")
            lines.append(f"\n{a.get('risk_emoji', '🔴')} {a['name']}")
            if gr:
                lines.append(f"Рост: {gr}")
            bv = a.get("base_val")
            rv = a.get("reg_val")
            if bv is not None and rv is not None:
                lines.append(f"База: {bv:,.0f} → Регул.год: {rv:,.0f} тыс.руб.")
            if a.get("verdict") or a.get("article_summary"):
                lines.append(f"Вердикт: {a.get('verdict') or a.get('article_summary')}")
            if a.get("basis"):
                lines.append(f"Основание: {a['basis']}")
            if a.get("recommendation"):
                lines.append(f"Рекомендация: {a['recommendation']}")
            _mf = a.get("matched_files") or []
            if _mf:
                lines.append("Документы в заявке:")
                for _d in _mf:
                    _o = _file_origin(_d.get("file_name", ""), origins)
                    _l, _ = _match_reason(a.get("name", ""), _d.get("file_name", ""),
                                          _d.get("summary", ""), _d.get("_similarity", 0))
                    lines.append(f"  • {_o['base']} ({_loc_label(_o, icons=False)}) — {_l.lower()}")
            elif _has_docs:
                lines.append("Документы в заявке: не найдены")
            lines.append("-"*40)
        report_text = "\n".join(lines)
        st.download_button(
            f"Скачать замечания ({len(problem_articles)} статей)",
            data=report_text.encode("utf-8"),
            file_name="замечания_регулятора.txt",
            mime="text/plain",
            key=f"{key_prefix}_dl_problems",
        )



# ─────────────────────────────────────────────────────────────────────────────
# Главный UI
# ─────────────────────────────────────────────────────────────────────────────
def _show_mr_settings():
    """Панель настроек Map-Reduce с калькулятором контекста."""
    cfg = load_mr_config()

    with st.expander("Настройки Map-Reduce", expanded=False):
        st.caption("Параметры разбивки текста и расчёт контекста для LM Studio")

        c1, c2, c3 = st.columns(3)
        ctx = c1.number_input(
            "Контекст модели (токенов)",
            min_value=4_000, max_value=128_000,
            value=cfg["context_tokens"], step=1_000,
            key="mr_context_tokens",
            help="Значение из настроек LM Studio → Context Length"
        )
        map_out = c2.number_input(
            "MAP: токенов на ответ",
            min_value=200, max_value=2_000,
            value=cfg["map_output_tokens"], step=100,
            key="mr_map_output_tokens",
            help="Сколько токенов модель тратит на одно мини-резюме"
        )
        max_chunk = c3.number_input(
            "Потолок чанка (токенов)",
            min_value=500, max_value=8_000,
            value=cfg["max_chunk_tokens"], step=500,
            key="mr_max_chunk_tokens",
            help="Максимальный размер одного MAP-чанка. Больше = медленнее, но связнее"
        )

        c4, c5, c6 = st.columns(3)
        ovhd = c4.number_input(
            "Накладные расходы REDUCE (токенов)",
            min_value=200, max_value=3_000,
            value=cfg["reduce_overhead_tokens"], step=100,
            key="mr_reduce_overhead",
            help="Системный промпт + инструкция REDUCE"
        )
        ra = c5.number_input(
            "REDUCE: токенов на ответ",
            min_value=1_000, max_value=16_000,
            value=cfg["reduce_answer_tokens"], step=500,
            key="mr_reduce_answer",
        )
        grp = c6.number_input(
            "Группа для MID-REDUCE",
            min_value=2, max_value=10,
            value=cfg["mid_reduce_group_size"], step=1,
            key="mr_group_size",
            help="Сколько MAP-резюме объединять в промежуточный блок при 3-уровневом режиме"
        )

        cpt = st.number_input(
            "Символов на токен (русский текст)",
            min_value=2.0, max_value=6.0,
            value=float(cfg["chars_per_token"]), step=0.5,
            key="mr_chars_per_token",
            format="%.1f",
        )

        # ── Калькулятор ───────────────────────────────────────────────────────
        st.divider()
        st.markdown("**Калькулятор: оцени план по объёму документа**")
        col_sl, col_res = st.columns([2, 3])

        text_size_kb = col_sl.select_slider(
            "Объём текста",
            options=[10, 25, 50, 100, 200, 500, 1_000, 2_000, 5_000],
            value=100,
            format_func=lambda x: f"{x} КБ" if x < 1_000 else f"{x//1000} МБ",
            key="mr_calc_size",
        )
        text_len_est = text_size_kb * 1024

        new_cfg = {
            "context_tokens":        int(ctx),
            "map_output_tokens":     int(map_out),
            "max_chunk_tokens":      int(max_chunk),
            "reduce_overhead_tokens": int(ovhd),
            "reduce_answer_tokens":  int(ra),
            "mid_reduce_group_size": int(grp),
            "chars_per_token":       float(cpt),
        }
        plan = compute_mr_plan(text_len_est, new_cfg)

        mode_icon = "2️⃣" if plan["mode"] == "2-level" else "3️⃣"
        with col_res:
            st.markdown(
                f"| Параметр | Значение |\n"
                f"|---|---|\n"
                f"| Режим | {mode_icon} {plan['mode']} |\n"
                f"| Чанков | {plan['actual_chunks']} |\n"
                f"| Размер чанка | ~{plan['chunk_chars']//1000}К симв "
                f"/ ~{plan['chunk_tokens']:,} токенов |\n"
                + (f"| MID-REDUCE блоков | {plan['mid_blocks']} |\n"
                   if plan['mode'] == '3-level' else "")
                + f"| Примерное время | ~{plan['est_minutes']} мин |"
            )

        # ── Рекомендация для LM Studio ────────────────────────────────────────
        rec = plan["recommended_ctx"]
        st.info(
            f"**LM Studio → Context Length:** установи **{rec:,}** токенов  \n"
            f"Это минимум для обработки документа ~{text_size_kb} КБ в режиме {plan['mode']}."
        )

        # ── Кнопки сохранить / сбросить ───────────────────────────────────────
        bc1, bc2 = st.columns(2)
        if bc1.button("Сохранить настройки", key="mr_save",
                      use_container_width=True, type="primary"):
            save_mr_config(new_cfg)
            st.success("Настройки сохранены.")
            st.rerun()

        if bc2.button("Сбросить к умолчаниям", key="mr_reset",
                      use_container_width=True):
            save_mr_config(MR_DEFAULTS)
            st.success("Настройки сброшены к умолчаниям.")
            st.rerun()



# ─────────────────────────────────────────────────────────────────────────────
# Проверка статей затрат — в модальном окне
# ─────────────────────────────────────────────────────────────────────────────
# Почему окно: раньше каждое действие в таблице на странице перезапускало весь
# скрипт — загрузку с архивами, результаты, реестр. На большой заявке это
# секунды на каждый клик. Окно (st.dialog) — фрагмент: клик внутри
# перезапускает только само окно.
#
# Как окно переживает перезапуски:
#   • правки ячеек, чекбоксы, фильтры — перезапуск фрагмента, окно остаётся;
#   • кнопки массового выбора и «Добавить статью» — st.rerun(scope="fragment");
#   • закрывают окно только «Подтвердить и продолжить» и крестик.
#
# Почему редактору передаётся неизменный снимок: идентичность st.data_editor
# зависит от переданных данных. Если на каждом перезапуске передавать таблицу,
# собранную заново из уже изменённых статей, редактор пересоздаётся — правки
# в нём сбрасываются, прокрутка прыгает наверх. Поэтому редактор получает
# снимок (_ap_base), а правки сразу записываются в статьи колбэком
# _ap_on_edit. Снимок пересобирается только при смене фильтров, после
# массовых действий и при новом открытии окна.

_AP_TYPE_LBLS = {
    "cost": "Статья затрат",
    "agg":  "Агрегат / итог",
    "ref":  "Справочно",
    "zero": "Нулевые",
}
_AP_LBL_TO_TYPE = {v: k for k, v in _AP_TYPE_LBLS.items()}
_AP_TYPE_FILTER = {
    "Все": None, "Статья затрат": "cost", "Агрегат / итог": "agg",
    "Справочно": "ref", "Нулевые": "zero",
}
_AP_STATE_KEYS = ("_ap_base", "_ap_view_sig", "_ap_year_cols")


def _rerun_dialog() -> None:
    """
    Перезапуск только окна. Вне фрагмента (например, если содержимое окна
    вызвано напрямую) Streamlit запрещает scope="fragment" — тогда полный.
    """
    try:
        st.rerun(scope="fragment")
    except StreamlitAPIException:
        st.rerun()


def _ap_year_cols(articles: List[Dict]) -> List[str]:
    """Годы для столбцов: из настроек, если заданы, иначе — по данным."""
    ss = st.session_state
    reg_yr = int(ss.get("ca_reg_year", 0)) if ss.get("ca_reg_year") else 0
    yr_range = ss.get("ca_year_range")
    if reg_yr and yr_range:
        return [str(y) for y in range(int(yr_range[0]), int(yr_range[1]) + 1)]
    for a in articles:
        ts = _parse_amounts_timeseries(a["amounts"])
        if len(ts) >= 2:
            return [str(t[0]) for t in ts[-4:]]
    return ["Рег.год"]


def _ap_in_filter(a: Dict, search_q: str, tf: Optional[str],
                  sheet_filter: Optional[str]) -> bool:
    if search_q and search_q.lower() not in a["name"].lower():
        return False
    if tf and a["type"] != tf:
        return False
    if sheet_filter and a.get("sheet") != sheet_filter:
        return False
    return True


def _ap_make_df(articles: List[Dict], search_q: str, tf: Optional[str],
                sheet_filter: Optional[str], only_checked: bool,
                year_cols: List[str]):
    import pandas as pd
    rows = []
    for i, a in enumerate(articles):
        if not _ap_in_filter(a, search_q, tf, sheet_filter):
            continue
        if only_checked and not a["checked"]:
            continue
        ts = _parse_amounts_timeseries(a["amounts"])
        ts_by_yr = {str(t[0]): t[2] for t in ts}
        sheet_lbl = a.get("sheet", "")
        if a.get("tech_sheet"):
            sheet_lbl = f"⚠️ {sheet_lbl}"
        elif a.get("manual"):
            sheet_lbl = "✏️ вручную"
        row = {
            "_idx":         i,
            "Включить":     a["checked"],
            "Наименование": a["name"],
            "Лист":         sheet_lbl,
            "Ед.изм.":      a.get("unit", ""),
        }
        for yr in year_cols:
            v = ts_by_yr.get(yr)
            # Числовые значения — None для пустых ячеек (NumberColumn)
            row[yr] = float(v) if v is not None else None
        row["Тип"] = _AP_TYPE_LBLS.get(a["type"], a["type"])
        rows.append(row)
    columns = ["_idx", "Включить", "Наименование", "Лист", "Ед.изм."] + year_cols + ["Тип"]
    df = pd.DataFrame(rows, columns=columns)
    # Годы — всегда float: столбец из одних пустых ячеек иначе получает тип
    # object, а целочисленный столбец Streamlit редактирует с шагом 1
    for yr in year_cols:
        df[yr] = pd.to_numeric(df[yr], errors="coerce").astype("float64")
    return df


def _fmt_amount(v: float) -> str:
    """
    Число для строки amounts в формате, который читает ядро.
    _parse_amounts_timeseries ищет «цифры с пробелами + один десятичный
    разделитель» перед «тыс» — «12 345.67». Раньше здесь было {:,.2f}
    («1,500.00»): запятая-разделитель тысяч ломала разбор, и после любой
    правки у статьи пропадали все значения от 1000 — рост не считался.
    Точность — до 6 знаков, лишние нули отбрасываются.
    """
    s = f"{v:,.6f}".rstrip("0").rstrip(".")
    return s.replace(",", " ")


def _ap_year_config(year_cols: List[str], editable: bool) -> Dict:
    """
    Столбцы лет: дробные значения. Без step — у дробного столбца Streamlit
    не ограничивает знаки после запятой (step=1 давал 0 знаков: редактор
    выбрасывал набранную запятую или точку, вводились только целые).
    «localized» — формат браузера: в русской локали «1 500,25».
    """
    return {
        yr: st.column_config.NumberColumn(
            yr, width="small", format="localized",
            help=("Введите значение (или оставьте пустым). "
                  "Дробная часть — через запятую или точку") if editable else None,
        )
        for yr in year_cols
    }


def _ap_apply_changes(a: Dict, changes: Dict, year_cols: List[str]) -> None:
    """Применяет правки одной строки таблицы к статье."""
    import math as _math
    if "Включить" in changes:
        a["checked"] = bool(changes["Включить"])
    if "Тип" in changes:
        a["type"] = _AP_LBL_TO_TYPE.get(changes["Тип"], "cost")
    if "Ед.изм." in changes:
        a["unit"] = str(changes["Ед.изм."] or "").strip()
    if "Наименование" in changes:
        # Сохраняем и пустое наименование — чтобы проверка перед
        # подтверждением поймала незаполненные новые статьи
        a["name"] = str(changes["Наименование"] or "").strip()

    # ── Пересобираем amounts из отредактированных годовых ячеек ──────────────
    # NumberColumn возвращает float или NaN/None для пустых ячеек.
    yrs = [yr for yr in year_cols if yr in changes]
    if not yrs:
        return
    orig_ts   = _parse_amounts_timeseries(a["amounts"])
    period_by = {str(t[0]): (t[1] if len(t) > 1 else "") for t in orig_ts}
    val_by    = {str(t[0]): t[2] for t in orig_ts}
    unit_out  = a.get("unit", "") or "тыс.руб."
    changed = False
    for yr in yrs:
        cell = changes[yr]
        # Пустая ячейка (NaN/None) — убираем год, если он был
        if cell is None or (isinstance(cell, float) and _math.isnan(cell)):
            if yr in val_by:
                val_by.pop(yr)
                changed = True
            continue
        try:
            num = float(cell)
        except (ValueError, TypeError):
            continue
        prev = val_by.get(yr)
        if prev is None or abs(num - prev) > 1e-6:
            val_by[yr] = num
            changed = True
    if changed:
        a["amounts"] = " | ".join(
            f"{yr} ({period_by.get(yr, '') or 'Принято'}): {_fmt_amount(val_by[yr])} {unit_out}"
            for yr in sorted(val_by.keys())
        )


def _ap_on_edit(editor_key: str) -> None:
    """
    Колбэк редактора: сразу записывает правки в статьи.
    edited_rows накапливает все правки этого экземпляра редактора —
    применение повторяемо (значения абсолютные), поэтому его можно вызывать
    сколько угодно раз.
    """
    ss = st.session_state
    base = ss.get("_ap_base")
    state = ss.get(editor_key) or {}
    if base is None or not isinstance(state, dict):
        return
    idx_list  = [int(i) for i in base["_idx"].tolist()]
    year_cols = ss.get("_ap_year_cols") or []
    arts      = ss.get("ca_parsed_articles") or []
    for pos, changes in (state.get("edited_rows") or {}).items():
        try:
            pos = int(pos)
        except (TypeError, ValueError):
            continue
        if 0 <= pos < len(idx_list) and 0 <= idx_list[pos] < len(arts):
            _ap_apply_changes(arts[idx_list[pos]], changes or {}, year_cols)


def _ap_reset() -> None:
    """После массовых действий: пересобрать снимок, создать новый редактор."""
    ss = st.session_state
    ss["_ap_ver"] = ss.get("_ap_ver", 0) + 1
    ss.pop("_ap_base", None)


def _ap_on_dismiss() -> None:
    """Закрыли крестиком: правки уже в статьях, снимок при открытии — заново."""
    for k in _AP_STATE_KEYS:
        st.session_state.pop(k, None)


def _approval_body(readonly: bool = False) -> None:
    """
    Таблица апрува статей затрат с фильтрацией по листам,
    ручным добавлением, редактированием значений.
    """
    ss = st.session_state
    articles = ss.ca_parsed_articles

    n_total   = len(articles)
    n_checked = sum(1 for a in articles if a["checked"])
    st.caption(
        f"Найдено строк: **{n_total}** · "
        f"К анализу: **{n_checked}** · "
        f"Исключено: **{n_total - n_checked}**"
    )

    # ── Фильтры ──────────────────────────────────────────────────────────
    all_sheets  = list(dict.fromkeys(a["sheet"] for a in articles if a.get("sheet")))
    tech_sheets = {a["sheet"] for a in articles if a.get("tech_sheet")}
    # Легенда листов: технические помечаем ⚠️
    sheet_options = ["Все листы"] + [
        (f"⚠️ {s}" if s in tech_sheets else s) for s in all_sheets
    ]
    sheet_display_to_real = {(f"⚠️ {s}" if s in tech_sheets else s): s for s in all_sheets}

    f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
    search_q = f1.text_input("Поиск", placeholder="Фильтр по названию...",
                             key="ca_ap_search", label_visibility="collapsed")
    type_filter = f2.selectbox("Тип", list(_AP_TYPE_FILTER.keys()),
                               key="ca_ap_type", label_visibility="collapsed")
    sheet_sel_lbl = f3.selectbox("Лист", sheet_options,
                                 key="ca_ap_sheet", label_visibility="collapsed")
    sheet_filter = None if sheet_sel_lbl == "Все листы" else sheet_display_to_real.get(sheet_sel_lbl)
    f4.markdown("<div style='padding-top:4px'></div>", unsafe_allow_html=True)
    only_checked = f4.checkbox("☑ Только выбранные", key="ca_ap_only_checked")
    tf = _AP_TYPE_FILTER.get(type_filter)
    year_cols = _ap_year_cols(articles)

    # ── Кнопки действий ──────────────────────────────────────────────────
    if not readonly:
        def _in_filter(a):
            return _ap_in_filter(a, search_q, tf, sheet_filter)

        _filter_hint = (
            f" (лист: {sheet_filter})" if sheet_filter else
            f" (тип: {type_filter})" if type_filter != "Все" else
            f" (поиск: {search_q})" if search_q else ""
        )
        bc = st.columns(7)
        action = None
        if bc[0].button("Авто-отбор", key="ca_ap_auto", width="stretch",
                        help="Только статьи затрат без технических листов"):
            action = "auto"
        if bc[1].button("Снять нулевые", key="ca_ap_unzero", width="stretch",
                        help="Снять флаги со всех нулевых/справочных"):
            action = "unzero"
        # Снять без значений — статьи с прочерком в регул. году
        if bc[2].button("Снять без значений", key="ca_ap_unblank", width="stretch",
                        help="Снять флаги со всех статей у которых нет ни одного ненулевого значения"):
            action = "unblank"
        if bc[3].button("Снять лист", key="ca_ap_unsheet", width="stretch",
                        help="Снять флаги со всех статей выбранного листа",
                        disabled=(sheet_filter is None)):
            action = "unsheet"
        if bc[4].button(f"✅ Выбрать{_filter_hint or ' все'}", key="ca_ap_all", width="stretch",
                        help="Выбрать все статьи в текущей фильтрации"):
            action = "all"
        if bc[5].button(f"☐ Убрать{_filter_hint or ' все'}", key="ca_ap_none", width="stretch",
                        help="Убрать флаги со всех статей в текущей фильтрации"):
            action = "none"
        if bc[6].button("Инверсия", key="ca_ap_inv", width="stretch",
                        help="Инвертировать выбор в текущей фильтрации"):
            action = "inv"

        if action:
            for a in articles:
                if action == "auto":
                    a["checked"] = (a["type"] == "cost" and not a.get("tech_sheet"))
                elif action == "unzero":
                    if a["type"] in ("zero", "ref", "agg"):
                        a["checked"] = False
                elif action == "unblank":
                    ts = _parse_amounts_timeseries(a["amounts"])
                    if not any(v != 0 for _, _, v in ts):
                        a["checked"] = False
                elif action == "unsheet":
                    if a.get("sheet") == sheet_filter:
                        a["checked"] = False
                elif _in_filter(a):
                    if action == "all":
                        a["checked"] = True
                    elif action == "none":
                        a["checked"] = False
                    elif action == "inv":
                        a["checked"] = not a["checked"]
            ss.ca_parsed_articles = articles
            _ap_reset()
            _rerun_dialog()   # окно остаётся открытым

    # ── Снимок для редактора ─────────────────────────────────────────────
    view_sig = "|".join([
        str(ss.get("_ap_ver", 0)), search_q or "", str(tf), str(sheet_filter),
        str(only_checked), ",".join(year_cols), str(len(articles)), str(readonly),
    ])
    if ss.get("_ap_view_sig") != view_sig or ss.get("_ap_base") is None:
        ss["_ap_base"]      = _ap_make_df(articles, search_q, tf, sheet_filter,
                                          only_checked, year_cols)
        ss["_ap_view_sig"]  = view_sig
        ss["_ap_year_cols"] = year_cols
        ss["_ap_snap"]      = ss.get("_ap_snap", 0) + 1   # ключи редактора не повторяются
    base = ss["_ap_base"]
    editor_key = f"ca_ap_editor_{ss['_ap_snap']}"

    # Сообщение об успешном добавлении (после перезапуска окна)
    if ss.get("_ap_added_msg"):
        st.success(f"✅ Добавлено: {ss['_ap_added_msg']}")
        ss.pop("_ap_added_msg", None)

    st.caption(f"Показано: {len(base)} из {n_total}")
    if base.empty:
        st.caption("Нет строк по выбранному фильтру.")
    elif not readonly:
        st.data_editor(
            base.drop(columns=["_idx"]),
            column_config={
                "Включить":     st.column_config.CheckboxColumn("Включить", width="small"),
                "Наименование": st.column_config.TextColumn("Наименование", width="large"),
                "Лист":         st.column_config.TextColumn("Лист", width="medium", disabled=True),
                "Ед.изм.":      st.column_config.TextColumn("Ед.изм.", width="small"),
                **_ap_year_config(year_cols, editable=True),
                "Тип":          st.column_config.SelectboxColumn(
                    "Тип", width="medium",
                    options=list(_AP_TYPE_LBLS.values()),
                ),
            },
            width="stretch",
            height=min(480, 35 * (len(base) + 1) + 3),
            hide_index=True,
            placeholder="—",
            key=editor_key,
            on_change=_ap_on_edit,
            args=(editor_key,),
        )
    else:
        st.dataframe(
            base.drop(columns=["_idx"]).rename(columns={"Включить": "✓"}),
            width="stretch", hide_index=True, placeholder="—",
            height=min(480, 35 * (len(base) + 1) + 3),
            column_config=_ap_year_config(year_cols, editable=False),
        )

    # Счётчики — по всем статьям, а не только по видимым в фильтре
    n_sel_live        = sum(1 for a in articles if a["checked"])
    _empty_named_live = sum(1 for a in articles
                            if a["checked"] and not (a.get("name") or "").strip())

    # ── Добавление статьи затрат ──────────────────────────────────────────
    if not readonly:
        st.divider()
        _fa, _fb = st.columns([5, 1])
        _add_name = _fa.text_input(
            "Наименование",
            key="ca_ap_add_name",
            placeholder="Введите наименование статьи затрат или показателя...",
            label_visibility="collapsed",
        )
        if _fb.button("➕ Добавить статью", key="ca_ap_add_blank",
                      type="primary", width="stretch"):
            if _add_name.strip():
                _blank_year = (
                    year_cols[-1]
                    if year_cols and year_cols[0] != "Рег.год"
                    else "2027"
                )
                ss.ca_parsed_articles.append({
                    "name":       _add_name.strip(),
                    "amounts":    f"{_blank_year} (Принято): 0.00 тыс.руб.",
                    "type":       "cost",
                    "checked":    True,
                    "sheet":      "вручную",
                    "unit":       "тыс.руб.",
                    "tech_sheet": False,
                    "manual":     True,
                })
                # Сбрасываем фильтры, чтобы новая статья была видна
                for k in ["ca_ap_add_name", "ca_ap_sheet", "ca_ap_search",
                          "ca_ap_type", "ca_ap_only_checked"]:
                    ss.pop(k, None)
                ss["_ap_added_msg"] = _add_name.strip()
                _ap_reset()
                _rerun_dialog()
            else:
                st.warning("Введите наименование статьи затрат")

    # ── Подтверждение ────────────────────────────────────────────────────
    st.divider()
    ap_c1, ap_c2 = st.columns([3, 1])
    ap_c1.caption(f"Отмечено к анализу: **{n_sel_live}** статей")
    if _empty_named_live:
        ap_c1.warning(
            f"⚠️ {_empty_named_live} отмеченных "
            f"{'статья' if _empty_named_live == 1 else 'статей'} без наименования — "
            f"укажите наименование в таблице, чтобы продолжить."
        )
    if readonly:
        return
    if ap_c2.button(
        "Подтвердить и продолжить", type="primary",
        width="stretch", key="ca_ap_confirm",
        disabled=(n_sel_live == 0 or _empty_named_live > 0),
    ):
        # Страховка: правки из таблицы уже записаны колбэком, применяем ещё раз
        _ap_on_edit(editor_key)

        # ── Проверка: у всех отмеченных статей должно быть наименование ──
        _empty_named = [
            a for a in articles
            if a["checked"] and not (a.get("name") or "").strip()
        ]
        if _empty_named:
            st.error(
                f"Нельзя продолжить: {len(_empty_named)} отмеченных "
                f"{'статья' if len(_empty_named) == 1 else 'статей'} без наименования. "
                f"Укажите наименование в таблице или снимите отметку «Включить»."
            )
            return

        approved = [a for a in articles if a["checked"]]
        ss.ca_parsed_articles   = approved
        ss.ca_articles_approved = True
        lines = []
        for a in approved:
            lines.append(f"★ {a['name']}")
            for part in a["amounts"].split(" | "):
                if part.strip():
                    lines.append(f"  {part.strip()}")
        ss.ca_calc_context = "\n".join(lines)
        for k in _AP_STATE_KEYS + ("ca_ap_add_name",):
            ss.pop(k, None)
        st.rerun()   # полный перезапуск закрывает окно и обновляет страницу


@st.dialog("Проверка статей затрат", width="large", on_dismiss=_ap_on_dismiss)
def _approval_dialog(readonly: bool = False) -> None:
    if readonly:
        st.caption("Анализ уже выполнен — список доступен только для просмотра. "
                   "Чтобы изменить состав статей, нажмите «Перезапустить анализ».")
    _approval_body(readonly)


def show_claim_analyzer():
    # Метка полного прогона страницы — _try_open не даёт открыть два окна
    # за один прогон (Streamlit это запрещает)
    st.session_state["_ca_run_token"] = uuid.uuid4().hex

    # Прогрев реранкера при первом открытии анализатора
    if not st.session_state.get("_ca_reranker_preloaded"):
        try:
            from core.advisor import get_reranker
            get_reranker()
            st.session_state["_ca_reranker_preloaded"] = True
        except Exception:
            pass

    hdr_col, clear_col = st.columns([5, 1])
    hdr_col.header("Анализатор тарифных заявок")
    hdr_col.caption("Риски · Реестр заявок")
    if clear_col.button("Очистить", key="ca_clear_all", use_container_width=True,
                        help="Сбросить весь анализ и загруженные файлы"):
        _CA_KEYS = [
            "ca_summary", "ca_risks", "ca_calc_context", "ca_done",
            "ca_project_id", "ca_uploaded_meta", "ca_uploaded_bytes",
            "ca_file_summaries", "ca_claim_summary", "ca_calc_files_checked",
            "ca_parsed_articles", "ca_articles_approved", "_pbar_max", "ca_df_calc",
            "ca_registry_files", "ca_zip_cache", "ca_file_origins", "ca_zip_skipped",
            "ca_bundle_id", "ca_upload_sig", "ca_mapcache",
            "ca_calc_select", "_ca_calc_opts_sig", "_ca_open_dialog",
            "_ap_base", "_ap_view_sig", "_ap_year_cols", "_ap_ver", "_ap_added_msg",
            "ca_ap_search", "ca_ap_type", "ca_ap_sheet", "ca_ap_only_checked", "ca_ap_add_name",
        ]
        for _k in _CA_KEYS:
            st.session_state.pop(_k, None)
        # Явно очищаем список файлов в uploader (ключ теперь статичный —
        # это часть диагностического теста на гипотезу "динамический key
        # ломает upload"; permanent-версию решим по результату теста)
        st.session_state.pop("ca_uploader_static", None)
        st.rerun()

    ss = st.session_state
    for k, v in [
        ("ca_summary",        ""),
        ("ca_risks",          ""),
        ("ca_calc_context",   ""),
        ("ca_org",            ""),
        ("ca_period",         ""),
        ("ca_done",           False),
        ("ca_project_id",     None),
        ("ca_uploaded_meta",  []),
        ("ca_uploaded_bytes", {}),
        ("ca_registry_files", []),      # файлы для реестра (архивы целиком)
        ("ca_file_origins",   {}),      # {файл: {archive, path}} — откуда файл
        ("ca_zip_skipped",    []),      # [(архив, путь, причина)] — не анализируются
        ("ca_spheres",        []),      # выбранные сферы для RAG
        ("ca_file_summaries", {}),      # словарь {файл: самари}
        ("ca_target_pct",      5.0),     # целевой индекс роста, %
        ("ca_risk_pct",        10.0),    # дополнительный рисковый порог, %
        ("ca_claim_summary",   ""),      # итоговое резюме заявки
        ("ca_parsed_articles", []),      # статьи после парсинга до апрува
        ("ca_df_calc", None),             # DataFrame кальк файла
        ("ca_articles_approved", False), # флаг: пользователь апрувил список
    ]:
        if k not in ss:
            ss[k] = v

    # ── Миграция и дедупликация сфер ─────────────────────────────────────────────
    _sphere_id_migration = {
        'water': 'Водоснабжение', 'heat': 'Теплоснабжение',
        'power': 'Электроэнергетика', 'gas': 'Газоснабжение',
        'waste': 'ТКО', 'trans': 'Транспорт', 'other': 'Прочее',
    }
    if ss.get('ca_spheres'):
        # Мигрируем старые id и дедуплицируем
        migrated = [_sphere_id_migration.get(s, s) for s in ss.ca_spheres]
        seen_sph = set()
        ss.ca_spheres = [x for x in migrated if not (x in seen_sph or seen_sph.add(x))]

    # ── Выбор сферы регулирования ─────────────────────────────────────────────
    st.subheader("Сфера регулирования")
    st.caption("Выберите сферу — RAG будет искать НПА только по ней. "
               "Не выбрано = поиск по всей базе.")

    selected_sphere_labels = st.multiselect(
        "Сферы регулирования",
        options=[f"{s['icon']} {s['label']}" for s in REGULATION_SPHERES],
        default=[SPHERE_LABELS[sid] for sid in ss.ca_spheres if sid in SPHERE_LABELS],
        label_visibility="collapsed",
        key="ca_spheres_select",
        placeholder="Все сферы (без фильтра)",
    )
    # Конвертируем "иконка label" → id
    label_to_id = {f"{s['icon']} {s['label']}": s["id"] for s in REGULATION_SPHERES}
    ss.ca_spheres = [label_to_id[lbl] for lbl in selected_sphere_labels if lbl in label_to_id]

    if ss.ca_spheres:
        selected_names = [SPHERE_LABELS.get(s, s) for s in ss.ca_spheres]
        st.caption(f"Фильтр RAG: {' · '.join(selected_names)}")
    else:
        st.caption("Фильтр не задан — поиск по всей нормативной базе.")

    # ── Реквизиты ─────────────────────────────────────────────────────────────
    with st.expander("Реквизиты заявки", expanded=not ss.ca_done):
        c1, c2 = st.columns(2)
        ss.ca_org    = c1.text_input("Организация", value=ss.ca_org,
                                     placeholder="ООО «Теплоснабжение»",
                                     key="ca_org_input")
        ss.ca_period = c2.text_input("Период регулирования", value=ss.ca_period,
                                     placeholder="2025 год",
                                     key="ca_period_input")
        st.divider()
        c3, c4 = st.columns(2)
        ss.ca_target_pct = c3.number_input(
            "Целевой индекс роста, %",
            min_value=0.0, max_value=100.0,
            value=float(ss.ca_target_pct), step=0.5, format="%.1f",
            key="ca_target_pct_input",
            help="Допустимый рост статьи затрат к предыдущему периоду. "
                 "Превышение → жёлтый цвет. Например: 5% означает рост не более чем в 1,05 раза.",
        )
        ss.ca_risk_pct = c4.number_input(
            "Рисковый порог (дополнительно), %",
            min_value=0.0, max_value=100.0,
            value=float(ss.ca_risk_pct), step=0.5, format="%.1f",
            key="ca_risk_pct_input",
            help="Превышение целевого индекса + этого порога → красный цвет. "
                 "Например: целевой 5% + рисковый 10% = красный при росте >15%.",
        )
        st.caption(
            f"Жёлтый: рост > {ss.ca_target_pct:.1f}%  ·  "
            f"Красный: рост > {ss.ca_target_pct + ss.ca_risk_pct:.1f}%  ·  "
            f"Зелёный: рост ≤ {ss.ca_target_pct:.1f}%"
        )
        st.divider()
        import re as _re2
        _period_str = str(ss.get('ca_period', '') or '')
        _yr_m = _re2.search(r'(\d{4})', _period_str)
        _ry = int(_yr_m.group(1)) if _yr_m else 2027
        ss['ca_reg_year'] = _ry
        _yr_def = ss.get('ca_year_range')
        if not _yr_def or abs(_yr_def[1] - _ry) > 10:
            _yr_def = (_ry - 5, _ry)
        ss.ca_year_range = st.slider(
            'Диапазон лет анализа',
            min_value=_ry - 10,
            max_value=_ry + 10,
            value=_yr_def,
            key='ca_year_range_input',
            help='Годы которые выводятся в таблице апрува и на графиках',
        )
        _yf, _yt = ss.ca_year_range
        st.caption(f'Анализ за {_yf}–{_yt}  ·  рег. год: {_ry}')

    # ── Загрузка файлов ───────────────────────────────────────────────────────
    st.subheader("Файлы заявки")

    st.caption(
        "Загрузите ZIP-архив заявки целиком или отдельные файлы. "
        "Чтобы загрузить папку: откройте её в проводнике, нажмите Ctrl+A "
        "и перетащите файлы сюда."
    )
    uploaded = st.file_uploader(
        "Перетащите файлы или нажмите «Browse files»",
        type=["xlsx", "xls", "pdf", "docx", "doc", "zip"],
        accept_multiple_files=True,
        key="ca_uploader_static",
    ) or []

    if uploaded:
        # ── Разворачиваем ZIP-архивы (результат кешируется) ──────────────────
        n_archives = sum(1 for uf in uploaded if is_archive(uf.name))
        bundle = _expand_uploads(uploaded)
        analysis_files = bundle["files"]

        for _err in bundle["errors"]:
            st.error(_err)

        # Файлы заменили после анализа, не нажав «Очистить»: результаты ниже
        # относятся к прежнему набору — предупреждаем, чтобы не перепутать
        if (ss.ca_done and ss.get("ca_upload_sig")
                and _upload_signature(uploaded) != ss.ca_upload_sig):
            st.warning(
                "Набор файлов изменился после анализа — результаты ниже относятся "
                "к прежним файлам. Чтобы проанализировать новые, нажмите «Очистить» "
                "вверху страницы и загрузите файлы заново."
            )

        # Разделяем Excel и документы
        xlsx_files = [f for f in analysis_files
                      if os.path.splitext(f.name.lower())[1] in _CALC_EXTS]
        doc_files  = [f for f in analysis_files
                      if os.path.splitext(f.name.lower())[1] in _DOC_EXTS]

        if n_archives:
            st.success(
                f"Загружено: **{len(uploaded)}** файл(ов), из них архивов: {n_archives} → "
                f"к анализу **{len(analysis_files)}** файл(ов) — "
                f"{len(xlsx_files)} расчётных · {len(doc_files)} документов"
            )
            st.caption(
                "После анализа в разделе «Документы заявки ↔ статьи затрат» будет "
                "видно, какой файл архива подтверждает какую статью. Сопоставление "
                "точнее, если файлы разложены по папкам с понятными названиями "
                "(«Топливо», «Ремонт», «Оплата труда»)."
            )
        else:
            st.success(
                f"Загружено: **{len(uploaded)}** файл(ов) — "
                f"{len(xlsx_files)} расчётных · {len(doc_files)} документов"
            )

        # ── Состав загрузки: одна строка на архив, файлы — в модальном окне ─
        # Раньше каждый файл архива выводился на странице отдельным элементом:
        # при нескольких архивах — сотни элементов, которые перерисовывались
        # на каждом действии. Теперь список открывается по кнопке «Файлы».
        for g in _upload_groups(uploaded, bundle):
            gc1, gc2 = st.columns([5, 1])
            gc1.markdown(f"{g['icon']} **{_md_escape(g['label'])}** — {g['summary']}")
            if gc2.button("Файлы", key=f"ca_grp_{g['gid']}", width="stretch",
                          help="Список файлов в отдельном окне: папки, типы, "
                               "что будет с файлом при анализе, скачивание"):
                _try_open(_files_dialog, g)

        # ── Расчётные модели: одно поле вместо галочки у каждого Excel ──────
        calc_opts = [f.name for f in xlsx_files]
        _xlsx_by_name = {f.name: f for f in xlsx_files}

        def _calc_label(name: str) -> str:
            f = _xlsx_by_name.get(name)
            if isinstance(f, _ArchivedFile):
                org = _make_origin(f.archive, f.inner)
                return f"{org['base']}  —  {_loc_label(org, icons=False)}"
            return name

        if calc_opts:
            # Набор Excel изменился — выбор по умолчанию: прежний выбор среди
            # новых файлов, а если Excel один — он сам
            _opts_sig = "|".join(calc_opts)
            if ss.get("_ca_calc_opts_sig") != _opts_sig:
                _prev = [n for n in (ss.get("ca_calc_files_checked") or []) if n in calc_opts]
                ss["ca_calc_select"] = _prev or (calc_opts[:1] if len(calc_opts) == 1 else [])
                ss["_ca_calc_opts_sig"] = _opts_sig
            calc_checked: List[str] = st.multiselect(
                "Расчётные модели (Excel-файлы со статьями затрат)",
                options=calc_opts,
                format_func=_calc_label,
                key="ca_calc_select",
                placeholder="Выберите Excel-файлы со статьями затрат",
                help="Из этих файлов будут взяты статьи затрат для анализа",
            )
        else:
            calc_checked = []

        ss["ca_calc_files_checked"] = calc_checked

        # ── Предупреждение если нет ни одной расчётной модели ────────────────
        has_calc = bool(calc_checked)
        if xlsx_files and not has_calc:
            st.warning(
                "Не выбрана ни одна расчётная модель. "
                "Выберите хотя бы один Excel-файл со статьями затрат в поле "
                "«Расчётные модели» — без него анализ рисков будет неполным."
            )
        elif not xlsx_files:
            st.info(
                "В загруженных файлах нет Excel-таблиц. "
                "Анализ рисков будет выполнен только на основе текста документов."
            )

        st.divider()

        # Блокируем если есть Excel но ни одна не помечена
        _block_run = bool(xlsx_files) and not has_calc
        if _block_run:
            st.error(
                "Выберите хотя бы одну расчётную модель в поле «Расчётные модели»."
            )

        # ── Кнопка: разобрать расчётный файл ─────────────────────────────────
        btn_parse = st.button(
            "Разобрать расчётный файл",
            type="primary",
            use_container_width=True,
            key="ca_btn_parse",
            disabled=_block_run,
        )

        # ── Шаг 1: парсинг расчётного файла ─────────────────────────────────
        if btn_parse:
            calc_names = ss.get("ca_calc_files_checked", [])
            # Кешируем байты (содержимое архивов — для анализа,
            # исходные архивы — для реестра)
            _cache_upload_bytes(uploaded, bundle)
            # Повторный разбор: прежняя таблица не должна склеиваться с новой
            # (раньше статьи в таблице удваивались)
            ss["ca_df_calc"] = None

            calc_context = ""
            with st.spinner("Парсю расчётный файл..."):
                for uf_name, uf_bytes in ss.ca_uploaded_bytes.items():
                    ext = os.path.splitext(uf_name.lower())[1]
                    if ext not in (".xlsx", ".xls"):
                        continue
                    if calc_names and uf_name not in calc_names:
                        continue
                    try:
                        from core.calc_parser import parse_workbook, to_llm_context
                        df_calc, meta_calc = parse_workbook(uf_bytes)
                        if not df_calc.empty:
                            calc_context += f"\n\n# {uf_name}\n" + to_llm_context(df_calc)
                            # Сохраняем df для блока апрува
                            if ss.get("ca_df_calc") is None:
                                ss["ca_df_calc"] = df_calc
                            else:
                                import pandas as _pd
                                ss["ca_df_calc"] = _pd.concat([ss["ca_df_calc"], df_calc], ignore_index=True)
                            st.info(
                                f"{uf_name}: "
                                f"{df_calc['article'].nunique()} статей · "
                                f"формат: {meta_calc.get('format','?')} · "
                                f"периоды: {sorted(df_calc['period'].unique().tolist())}"
                            )
                        else:
                            st.warning(f"{uf_name}: статьи затрат не найдены")
                    except Exception as e:
                        st.warning(f"calc_parser [{uf_name}]: {e}")

            if not calc_context.strip():
                st.error("Не удалось извлечь данные из расчётного файла.")
                st.stop()

            ss.ca_calc_context = calc_context
            # Строим список статей напрямую из df (с листами, ед. изм., тех. признаком)
            raw_articles = _extract_articles_from_df(
                ss.get("ca_df_calc")  # df уже сохранён в session state выше
            ) if ss.get("ca_df_calc") is not None else \
                _extract_articles_from_context_unfiltered(calc_context)
            ss.ca_parsed_articles  = raw_articles
            ss.ca_articles_approved = False

            # Информируем пользователя о составе
            n_all   = len(raw_articles)
            n_cost  = sum(1 for a in raw_articles if a["type"] == "cost")
            n_zero  = sum(1 for a in raw_articles if a["type"] == "zero")
            n_other = n_all - n_cost - n_zero

            if n_all == 0:
                st.error("Статьи затрат не найдены. Возможно, файл является незаполненным шаблоном.")
            elif n_cost == 0 and n_zero > 0:
                st.warning(
                    f"Найдено {n_all} строк, но все значения нулевые — файл может быть незаполненным шаблоном. "
                    f"Вы можете вручную отметить нужные строки в таблице ниже (тип «Нулевые»)."
                )
            else:
                st.success(
                    f"Найдено строк: **{n_all}** — "
                    f"статей затрат: **{n_cost}**, "
                    f"нулевых: **{n_zero}**, "
                    f"прочих: **{n_other}**. "
                    f"Проверьте список и нажмите «Подтвердить»."
                )
            if n_all:
                ss["_ca_open_dialog"] = "approval"   # открыть окно проверки статей
            st.rerun()

        # ── Шаг 2: экспандер с таблицей апрува ──────────────────────────────
        # Если парсинг выполнен но ничего не нашли
        if ss.ca_calc_context and not ss.ca_parsed_articles and not ss.ca_done:
            st.error(
                "Статьи затрат не найдены в расчётном файле. "
                "Возможные причины: незаполненный шаблон, нераспознанный формат, "
                "или все строки имеют нулевые значения."
            )
        _auto_open = ss.pop("_ca_open_dialog", None) == "approval"
        if ss.ca_parsed_articles:
            n_arts = len(ss.ca_parsed_articles)
            n_sel  = sum(1 for a in ss.ca_parsed_articles if a["checked"])
            _frozen = ss.ca_done  # после запуска анализа — только просмотр
            _approved = ss.ca_articles_approved
            _state = ("анализ запущен" if _frozen
                      else "подтверждено" if _approved else "требует подтверждения")

            # Таблица статей — в модальном окне: правки в ней перезапускают
            # только окно, а не всю страницу с архивами и реестром
            ac1, ac2 = st.columns([3, 2])
            ac1.markdown(f"**Статьи затрат:** {n_sel} к анализу из {n_arts} · {_state}")
            _need = not (_frozen or _approved)
            if ac2.button(
                "Проверить и подтвердить статьи" if _need else "Открыть список статей",
                key="ca_ap_open", type="primary" if _need else "secondary",
                width="stretch",
            ):
                _try_open(_approval_dialog, bool(_frozen))
            elif _auto_open and not _frozen:
                _try_open(_approval_dialog, False)
            if _need:
                ac1.caption("Отметьте статьи для анализа и нажмите «Подтвердить и продолжить» "
                            "в окне проверки.")

        # ── Шаг 3: кнопки запуска ────────────────────────────────────────────
        run_full  = False
        run_risks = False
        if ss.ca_articles_approved and ss.ca_parsed_articles and not ss.ca_done:
            c1, c2 = st.columns(2)
            run_full  = c1.button("Полный анализ",  type="primary",
                                  use_container_width=True, key="ca_run_full")
            run_risks = c2.button("Только риски",
                                  use_container_width=True, key="ca_run_risks")
        elif ss.ca_done and ss.ca_parsed_articles:
            # Показываем кнопку повторного анализа если нужно
            if st.button("Перезапустить анализ", key="ca_rerun",
                         use_container_width=True):
                ss.ca_done              = False
                ss.ca_risks             = ""
                ss.ca_claim_summary     = ""
                ss.ca_articles_approved = True  # список уже подтверждён
                st.rerun()

        if run_full:
            pbar   = st.progress(0.0)
            status = st.empty()
            calc_context = ss.ca_calc_context
            calc_names   = ss.get("ca_calc_files_checked", [])

            if not calc_context.strip():
                st.error("Не удалось извлечь данные из расчётного файла.")
                st.stop()

            _log_usage("claim_analyzer", "analysis_started", meta={
                "mode":       "full",
                "file_count": len(ss.ca_uploaded_meta or []),
                "sphere":     str(ss.get("ca_spheres") or ""),
            })

            # ── Чтение заголовков документов (первые 2 страницы каждого файла) ──
            n_doc_files = sum(
                1 for name in ss.ca_uploaded_bytes
                if name not in calc_names
                and os.path.splitext(name.lower())[1]
                in ('.pdf', '.docx', '.doc', '.txt')
            )
            if n_doc_files > 0:
                pbar.progress(0.20)
                _order = _doc_order(calc_names)[:_DOC_LIMIT]

                def _pcb_sum(frac, msg):
                    val = 0.20 + frac * 0.20
                    pbar.progress(min(val, 0.40))
                    status.text(_doc_progress_msg(frac, msg, _order))

                file_summaries = _build_file_summaries(
                    uploaded_bytes=ss.ca_uploaded_bytes,
                    calc_file_names=calc_names,
                    progress_cb=_pcb_sum,
                )
                ss["ca_file_summaries"] = file_summaries
                _report_doc_reading(file_summaries, n_doc_files)
            else:
                file_summaries = {}
                ss["ca_file_summaries"] = {}

            pbar.progress(0.40)
            ss["_pbar_max"] = 0.40

            def _pcb_risk(pct, msg):
                val = min(0.40 + pct * 0.57, 0.97)
                if val > ss.get("_pbar_max", 0):
                    ss["_pbar_max"] = val
                    pbar.progress(val)
                status.text(msg)

            risks = analyze_risks(
                calc_context, "", _pcb_risk,
                spheres=ss.ca_spheres or None,
                file_summaries=ss.get("ca_file_summaries", {}),
                target_pct=float(ss.get("ca_target_pct", 5.0)),
                risk_pct=float(ss.get("ca_risk_pct", 10.0)),
                approved_articles=ss.ca_parsed_articles or None,
            )
            # Карта документов: статусы файлов, происхождение, пропущенные
            risks = _enrich_risks_json(risks, calc_names)
            ss.ca_risks = risks
            ss.ca_done  = True
            ss.ca_project_id = None

            # ── Резюме заявки (первые 1000 симв каждого файла) ───────────────
            status.text("Формирую резюме заявки...")
            pbar.progress(0.97)
            try:
                risk_data = json.loads(risks)
                art_list  = risk_data.get("articles", [])
            except Exception:
                art_list = []
            ss.ca_claim_summary = _build_claim_summary_from_heads(
                uploaded_bytes=ss.ca_uploaded_bytes,
                calc_file_names=calc_names,
                calc_context=calc_context,
                article_results=art_list,
                org=ss.ca_org,
                period=ss.ca_period,
                file_summaries=ss.get("ca_file_summaries", {}),
            )

            _save_log(ss.ca_org, ss.ca_period, ss.ca_claim_summary, risks)
            pbar.progress(1.0)

            # Считаем риски для метрики
            try:
                _rd = json.loads(risks)
                _arts = _rd.get("articles", [])
                _n_red    = sum(1 for a in _arts if a.get("risk") == "red")
                _n_yellow = sum(1 for a in _arts if a.get("risk") == "yellow")
            except Exception:
                _n_red = _n_yellow = 0
            _log_usage("claim_analyzer", "analysis_completed", meta={
                "mode":       "full",
                "n_articles": len(_arts) if "_arts" in dir() else 0,
                "n_high":     _n_red,
                "n_medium":   _n_yellow,
            })

            # ── Автосохранение в реестр ───────────────────────────────────────
            status.text("Сохраняю в реестр...")
            try:
                from core.claim_registry import save_project
                _files_data = _registry_files_data()
                _pid = save_project(
                    org          = ss.ca_org,
                    period       = ss.ca_period,
                    files_data   = _files_data,
                    calc_context = ss.ca_calc_context,
                    summary      = ss.ca_claim_summary,
                    risks        = risks,
                    project_id   = None,
                )
                ss.ca_project_id = _pid
            except Exception as _e:
                print(f"[AUTOSAVE] Ошибка: {_e}")

            status.success("Анализ завершён!")
            st.rerun()

        if run_risks:
            pbar   = st.progress(0.0)
            status = st.empty()

            _log_usage("claim_analyzer", "analysis_started", meta={
                "mode":       "risks_only",
                "file_count": len(ss.ca_uploaded_meta or []),
            })

            # Всегда перечитываем байты — они доступны только при нажатии кнопки
            calc_names = ss.get("ca_calc_files_checked", [])
            _cache_upload_bytes(uploaded, bundle)

            # Парсим расчётные файлы если calc_context ещё пустой
            if not ss.ca_calc_context:
                combined_calc = ""
                for uf_name, uf_bytes in ss.ca_uploaded_bytes.items():
                    ext = os.path.splitext(uf_name.lower())[1]
                    if ext not in (".xlsx", ".xls"):
                        continue
                    if calc_names and uf_name not in calc_names:
                        continue
                    status.text(f"Парсю расчётный файл: {uf_name}...")
                    pbar.progress(0.1)
                    try:
                        from core.calc_parser import parse_workbook, to_llm_context
                        df_calc, _ = parse_workbook(uf_bytes)
                        if not df_calc.empty:
                            combined_calc += f"\n\n# {uf_name}\n" + to_llm_context(df_calc)
                    except Exception as e:
                        st.warning(f"calc_parser [{uf_name}]: {e}")
                ss.ca_calc_context = combined_calc

            # Инвентаризация документов — запускаем всегда при нажатии кнопки
            # Суммаризация файлов если ещё не сделана
            if not ss.get("ca_file_summaries"):
                n_doc_files = sum(
                    1 for name in ss.ca_uploaded_bytes
                    if name not in calc_names
                    and os.path.splitext(name.lower())[1]
                    in ('.pdf', '.docx', '.doc', '.txt')
                )
                if n_doc_files > 0:
                    pbar.progress(0.12)
                    _order_r = _doc_order(calc_names)[:_DOC_LIMIT]

                    def _pcb_sum_r(frac, msg):
                        val = 0.12 + frac * 0.03
                        pbar.progress(min(val, 0.15))
                        status.text(_doc_progress_msg(frac, msg, _order_r))

                    file_summaries = _build_file_summaries(
                        uploaded_bytes=ss.ca_uploaded_bytes,
                        calc_file_names=calc_names,
                        progress_cb=_pcb_sum_r,
                    )
                    ss["ca_file_summaries"] = file_summaries
                    _report_doc_reading(file_summaries, n_doc_files)

            ss["_pbar_max"] = 0.15

            def _pcb_r(pct, msg):
                val = min(0.15 + pct * 0.84, 0.99)
                if val > ss.get("_pbar_max", 0):
                    ss["_pbar_max"] = val
                    pbar.progress(val)
                status.text(msg)

            ss.ca_risks = _enrich_risks_json(
                analyze_risks(
                    ss.ca_calc_context, ss.ca_summary, _pcb_r,
                    spheres=ss.ca_spheres or None,
                    file_summaries=ss.get("ca_file_summaries", {}),
                    target_pct=float(ss.get("ca_target_pct", 5.0)),
                    risk_pct=float(ss.get("ca_risk_pct", 10.0)),
                    approved_articles=ss.ca_parsed_articles or None,
                ),
                calc_names,
            )
            ss.ca_done       = True
            ss.ca_project_id = None
            pbar.progress(0.97)

            # Резюме если ещё нет
            if not ss.get("ca_claim_summary"):
                status.text("Формирую резюме заявки...")
                try:
                    risk_data = json.loads(ss.ca_risks)
                    art_list  = risk_data.get("articles", [])
                except Exception:
                    art_list = []
                ss.ca_claim_summary = _build_claim_summary_from_heads(
                    uploaded_bytes=ss.ca_uploaded_bytes,
                    calc_file_names=calc_names,
                    calc_context=ss.ca_calc_context,
                    article_results=art_list,
                    org=ss.ca_org,
                    period=ss.ca_period,
                    file_summaries=ss.get("ca_file_summaries", {}),
                )

            # Автосохранение в реестр
            status.text("Сохраняю в реестр...")
            try:
                from core.claim_registry import save_project
                _files_data = _registry_files_data()
                _pid = save_project(
                    org          = ss.ca_org,
                    period       = ss.ca_period,
                    files_data   = _files_data,
                    calc_context = ss.ca_calc_context,
                    summary      = ss.ca_claim_summary,
                    risks        = ss.ca_risks,
                    project_id   = None,
                )
                ss.ca_project_id = _pid
            except Exception as _e:
                print(f"[AUTOSAVE] Ошибка: {_e}")

            pbar.progress(1.0)
            status.success("Риски обновлены!")
            try:
                _rd2 = json.loads(ss.ca_risks)
                _arts2 = _rd2.get("articles", [])
                _nr2 = sum(1 for a in _arts2 if a.get("risk") == "red")
                _ny2 = sum(1 for a in _arts2 if a.get("risk") == "yellow")
            except Exception:
                _arts2, _nr2, _ny2 = [], 0, 0
            _log_usage("claim_analyzer", "analysis_completed", meta={
                "mode":       "risks_only",
                "n_articles": len(_arts2),
                "n_high":     _nr2,
                "n_medium":   _ny2,
            })
            st.rerun()

    # ── Баннер + кнопка «Сохранить в реестр» ─────────────────────────────────
    if ss.ca_done:
        col_info, col_save = st.columns([4, 1])
        if ss.ca_project_id:
            col_info.success(
                f"Сохранено в реестр · ID: `{ss.ca_project_id}`"
                + ("" if uploaded else f" · **{ss.ca_org or '—'}** · {ss.ca_period or '—'}")
            )
        elif not uploaded:
            col_info.info(
                f"Данные в памяти: **{ss.ca_org or '—'}** · {ss.ca_period or '—'}"
            )

        if ss.ca_summary or ss.ca_risks:
            if col_save.button(
                "Сохранить в реестр" if not ss.ca_project_id else "Обновить",
                type="primary" if not ss.ca_project_id else "secondary",
                use_container_width=True,
                key="ca_save_registry",
            ):
                try:
                    from core.claim_registry import save_project
                    files_data = _registry_files_data()
                    pid = save_project(
                        org          = ss.ca_org,
                        period       = ss.ca_period,
                        files_data   = files_data,
                        calc_context = ss.ca_calc_context,
                        # Резюме свежего анализа — в ca_claim_summary; ca_summary
                        # заполняется только при открытии заявки из реестра.
                        # Раньше здесь сохранялся пустой ca_summary и «Обновить»
                        # стирал резюме в реестре.
                        summary      = ss.ca_claim_summary or ss.ca_summary,
                        risks        = ss.ca_risks,
                        project_id   = ss.ca_project_id,
                    )
                    ss.ca_project_id = pid
                    st.success(f"Сохранено: `{pid}`")
                    st.rerun()
                except Exception as e:
                    st.error(f"Ошибка сохранения: {e}")

    st.divider()
    st.markdown("#### Результаты анализа")
    # Ленивые вкладки: выполняется только открытая. Раньше реестр (все
    # карточки с графиками) перестраивался на каждом действии на странице,
    # даже когда пользователь смотрел риски.
    if ss.pop("_ca_goto_risks", False):
        ss["ca_main_tabs"] = "Риски и комплектность"   # после «Открыть в анализаторе»
    tab_risks, tab_registry = _lazy_tabs(
        ["Риски и комплектность", "Реестр заявок"], key="ca_main_tabs",
    )

    # =========================================================================
    # Вкладка 1: Риски + Резюме
    # =========================================================================
    with tab_risks:
        if _tab_open(tab_risks):
            diag = _rag_diagnose()
            if diag:
                if "недоступен" in diag or "Не удалось" in diag:
                    st.error(diag)
                else:
                    st.caption(diag)

            if ss.ca_risks:
                _render_risks_tab(ss.ca_risks, claim_summary=ss.get("ca_claim_summary", ""),
                                  project_id=ss.get("ca_project_id"))
            else:
                st.info(
                    "Загрузите файлы и нажмите «Полный анализ» — "
                    "здесь появится резюме заявки и постатейная оценка рисков."
                )

    # =========================================================================
    # Вкладка 2: Реестр
    # =========================================================================
    with tab_registry:
        if _tab_open(tab_registry):
            _show_registry()

    # ── Обратная связь ────────────────────────────────────────────────────────
    st.divider()
    with st.expander("Сообщить об ошибке", expanded=False):
        with st.form("ca_fb"):
            issue = st.selectbox("Тип проблемы", [
                "Файл не распознан", "Ошибка расчётного файла",
                "Резюме некорректное", "Риски определены неверно", "Другое",
            ])
            desc = st.text_area("Описание", placeholder="Что пошло не так?")
            if st.form_submit_button("Отправить"):
                if desc.strip():
                    try:
                        from core.feedback import submit_feedback
                        submit_feedback("user", issue, desc)
                    except Exception:
                        pass
                    st.success("Отправлено. Спасибо!")
                else:
                    st.warning("Опишите проблему")


# ─────────────────────────────────────────────────────────────────────────────
# Модалка подтверждения удаления проекта из реестра
# ─────────────────────────────────────────────────────────────────────────────
@st.dialog("Удаление заявки")
def _confirm_delete_project_dialog(pid: str, org: str, period: str):
    """Модальное подтверждение удаления проекта (заявки) из реестра."""
    st.markdown(f"Удалить заявку **{org or '—'} · {period or '—'}** из реестра?")
    st.caption("Будут удалены все файлы, расчёты и результаты анализа. Действие необратимо.")

    c_yes, c_no = st.columns(2)
    if c_yes.button("Удалить", type="primary", use_container_width=True,
                    key=f"_dlg_reg_yes_{pid}"):
        try:
            from core.claim_registry import delete_project
            delete_project(pid)
        except Exception as _e:
            st.error(f"Ошибка удаления: {_e}")
            return
        _log_usage("claim_analyzer", "project_deleted", meta={
            "org":    (org or "")[:60],
            "period": period or "",
        })
        st.session_state["_ca_delete_done"] = f"Заявка «{org or '—'} · {period or '—'}» удалена."
        st.rerun()
    if c_no.button("Отмена", use_container_width=True, key=f"_dlg_reg_no_{pid}"):
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# UI Реестра
# ─────────────────────────────────────────────────────────────────────────────
def _show_registry():
    try:
        from core.claim_registry import (
            list_projects, get_project, update_status,
            update_notes, delete_project, get_file_path,
            STATUSES, STATUS_COLORS,
        )
    except ImportError as e:
        st.error(f"Ошибка импорта claim_registry: {e}")
        return

    st.subheader("Реестр тарифных заявок")

    _ca_del_msg = st.session_state.pop("_ca_delete_done", None)
    if _ca_del_msg:
        st.success(_ca_del_msg)

    # ── Фильтры ───────────────────────────────────────────────────────────────
    fc1, fc2 = st.columns([3, 1])
    search        = fc1.text_input("Поиск", placeholder="организация, период, тег...",
                                   key="reg_search", label_visibility="collapsed")
    status_filter = fc2.selectbox("Статус", ["все"] + STATUSES,
                                  key="reg_status_filter", label_visibility="collapsed")

    projects = list_projects(search=search, status_filter=status_filter)

    if not projects:
        st.info(
            "Реестр пуст. Выполните анализ заявки и нажмите «Сохранить в реестр»."
            if not search and status_filter == "все"
            else "Нет заявок по выбранным фильтрам."
        )
        return

    st.caption(f"Найдено: {len(projects)} заявок")
    st.divider()

    for proj in projects:
        pid      = proj["id"]
        org      = proj.get("org") or "—"
        period   = proj.get("period") or "—"
        status   = proj.get("status", "анализ")
        updated  = proj.get("updated_at", "")[:10]
        files    = proj.get("files", [])
        summary  = proj.get("summary", "")
        risks    = proj.get("risks", "")
        notes    = proj.get("notes", "")
        bg, fg   = STATUS_COLORS.get(status, ("var(--color-background-secondary)",
                                              "var(--color-text-secondary)"))

        # Содержимое карточки (файлы, резюме, риски с графиками) строится,
        # только когда карточка раскрыта
        _card = _lazy_expander(
            f"**{org}** · {period} · "
            f":{status}: · {updated}",
            key=f"reg_exp_{pid}",
        )
        with _card:
            if not _tab_open(_card):
                continue
            # ── Заголовок карточки ────────────────────────────────────────
            hc1, hc2, hc3 = st.columns([3, 2, 1])
            hc1.markdown(f"**{org}** — {period}")
            new_status = hc2.selectbox(
                "Статус",
                STATUSES,
                index=STATUSES.index(status) if status in STATUSES else 0,
                key=f"reg_status_{pid}",
                label_visibility="collapsed",
            )
            if new_status != status:
                update_status(pid, new_status)
                st.rerun()

            if hc3.button("✕", key=f"reg_del_{pid}",
                          help="Удалить из реестра"):
                _try_open(_confirm_delete_project_dialog, pid, org, period)

            # ── Файлы ─────────────────────────────────────────────────────
            if files:
                st.markdown("**Файлы:**")
                for fmeta in files:
                    fname = fmeta.get("name", "")
                    fsize = fmeta.get("size", 0)
                    saved = fmeta.get("saved", False)
                    fpath = get_file_path(pid, fname) if saved else None

                    fc1_f, fc2_f = st.columns([4, 1])
                    fc1_f.caption(
                        f"{fname} · "
                        f"{_format_size(fsize)}"
                    )
                    if fpath:
                        # ── Ленивое чтение файла — ТОЛЬКО по явному клику ────
                        #
                        # ПРИЧИНА БАГА "загрузка файлов не работает в Анализаторе":
                        # раньше файл читался с диска БЕЗУСЛОВНО на каждом
                        # rerun страницы (даже если экспандер свёрнут — код
                        # внутри st.expander выполняется всегда, сворачивание
                        # влияет только на отображение). При накопившемся
                        # реестре заявок это означало: каждое взаимодействие
                        # со страницей (включая простой выбор нового файла
                        # для загрузки в другом месте страницы) синхронно
                        # перечитывало С ДИСКА все файлы всех сохранённых
                        # заявок — отсюда зависание upload-хэндшейка.
                        _dl_bytes_key = f"reg_dl_bytes_{pid}_{fname}"
                        if st.session_state.get(_dl_bytes_key) is not None:
                            fc2_f.download_button(
                                "Скачать",
                                data=st.session_state[_dl_bytes_key],
                                file_name=fname,
                                key=f"reg_dl_{pid}_{fname}",
                                use_container_width=True,
                                help="Скачать файл",
                            )
                        else:
                            if fc2_f.button(
                                "Подготовить",
                                key=f"reg_prep_{pid}_{fname}",
                                use_container_width=True,
                                help="Прочитать файл с диска перед скачиванием",
                            ):
                                try:
                                    with open(fpath, "rb") as f_bin:
                                        st.session_state[_dl_bytes_key] = f_bin.read()
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Ошибка чтения файла: {e}")

            # ── Заметки ───────────────────────────────────────────────────
            new_notes = st.text_area(
                "Заметки",
                value=notes,
                height=68,
                key=f"reg_notes_{pid}",
                placeholder="Заметки по заявке...",
            )
            if new_notes != notes:
                update_notes(pid, new_notes)

            # ── Резюме и риски ────────────────────────────────────────────
            sub1, sub2 = _lazy_tabs(["Резюме", "Риски"], key=f"reg_tabs_{pid}")

            with sub1:
                if _tab_open(sub1):
                    if summary:
                        st.markdown(summary)
                        st.download_button(
                            "Скачать резюме (.txt)",
                            data=summary.encode("utf-8"),
                            file_name=f"резюме_{org}_{period}.txt",
                            mime="text/plain",
                            key=f"reg_dl_sum_{pid}",
                        )
                    else:
                        st.caption("Резюме не сохранено.")

            with sub2:
                if _tab_open(sub2):
                    if risks:
                        # Пробуем отрендерить через _render_risks_tab (JSON-формат)
                        try:
                            import json as _json
                            _json.loads(risks)  # проверяем что это JSON
                            _render_risks_tab(risks, show_summary=False, key_prefix=f"reg_{pid}",
                                              project_id=pid)
                        except Exception:
                            # Старый формат — просто markdown
                            st.markdown(risks)
                        st.download_button(
                            "Скачать риски (.txt)",
                            data=risks.encode("utf-8"),
                            file_name=f"риски_{org}_{period}.txt",
                            mime="text/plain",
                            key=f"reg_dl_risk_{pid}",
                        )
                    else:
                        st.caption("Анализ рисков не сохранён.")

            # ── Загрузить в рабочую область ───────────────────────────────
            st.divider()
            if st.button(
                f"Открыть в анализаторе",
                key=f"reg_load_{pid}",
                use_container_width=True,
                help="Загрузить резюме и риски в текущую рабочую область",
            ):
                ss = st.session_state
                ss.ca_org          = proj.get("org", "")
                ss.ca_period       = proj.get("period", "")
                ss.ca_summary      = proj.get("summary", "")
                ss.ca_risks        = proj.get("risks", "")
                ss.ca_calc_context = proj.get("calc_context", "")
                # Резюме именно этой заявки: вкладка рисков показывает
                # ca_claim_summary, иначе на экране оставалось резюме
                # предыдущего анализа
                ss.ca_claim_summary = proj.get("summary", "")
                ss.ca_done         = True
                ss.ca_project_id   = pid
                # Файлы предыдущей сессии к этой заявке не относятся: без сброса
                # «Обновить» перезаписал бы ими файлы заявки в реестре, а карта
                # документов отдавала бы чужие файлы. Файлы заявки карта
                # достанет из её сохранённого архива.
                ss.ca_uploaded_bytes = {}
                ss.ca_uploaded_meta  = []
                ss.ca_registry_files = []
                ss.ca_file_summaries = {}
                ss.ca_file_origins   = {}
                ss.ca_zip_skipped    = []
                ss.pop("ca_bundle_id", None)
                ss.pop("ca_upload_sig", None)
                ss["_ca_goto_risks"] = True
                st.success(f"Загружено: {org} · {period}")
                st.rerun()