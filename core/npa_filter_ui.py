"""
Уточнение перечня НПА для Советчика — кнопка + модальное окно с датагридом.

НАЗНАЧЕНИЕ. Пользователь выбирает сферу (и, при желании, вид документа и
актуальность), нажимает «Уточнить перечень НПА» и в таблице снимает галки с
документов, которые не должны участвовать в ответе. Выбранные документы
уходят в core.advisor.search_vector_db(filenames=[...]) — поиск ведётся
ТОЛЬКО внутри них.

КУДА КЛАСТЬ ФАЙЛ: core/npa_filter_ui.py
Не в streamlit_pages/: Streamlit может принять модуль из каталога страниц за
самостоятельную страницу приложения.

ПОДКЛЮЧЕНИЕ В СТРАНИЦЕ СОВЕТЧИКА (3 строки):

    from core.npa_filter_ui import render_npa_filter, get_selected_filenames

    # после селектов сферы / вида / актуальности:
    render_npa_filter(spheres=selected_spheres,
                      doc_types=selected_doc_types,
                      doc_status=selected_status)

    # в месте вызова поиска:
    sources = search_vector_db(
        query, top_k=top_k, spheres=selected_spheres,
        doc_types=selected_doc_types, doc_status=selected_status,
        filenames=get_selected_filenames(spheres=selected_spheres,
                                         doc_types=selected_doc_types,
                                         doc_status=selected_status),
    )

ПОЧЕМУ ДАТАГРИД, А НЕ СПИСОК ЧЕКБОКСОВ. Streamlit рендерит каждый st.checkbox
отдельным виджетом со своим ключом и своим рероном; сотня чекбоксов рядом с
селектами — это сотня виджетов, заметные тормоза и разъезжающаяся вёрстка.
st.data_editor — один виджет с нативной колонкой-галкой, сортировкой и
прокруткой.

═════════════════════════════════════════════════════════════════════════════
ДВА БАГА, ИЗ-ЗА КОТОРЫХ ЭТОТ ФАЙЛ ПЕРЕПИСАН. Читать перед любой правкой
диалога — оба воспроизводятся моментально, если вернуть прежний подход.
═════════════════════════════════════════════════════════════════════════════

1. МОДАЛКА ЗАКРЫВАЛАСЬ ПРИ НАЖАТИИ «ВЫБРАТЬ ВСЕ» / «СНЯТЬ ВСЕ».

   @st.dialog реализован поверх механизма фрагментов. st.rerun() по умолчанию
   имеет scope="app": он перезапускает ВЕСЬ скрипт страницы, а диалог при
   этом не открывается заново — он показывается только в том прогоне, где
   декорированная функция была вызвана явно (у нас — по клику на кнопку,
   которая на следующем прогоне уже не нажата). Внешне это выглядело как
   «модалка сама свернулась».

   Правильно — st.rerun(scope="fragment"): перезапускается только тело
   диалога, окно остаётся открытым. Поддержка появилась в Streamlit 1.37;
   наличие проверяется по сигнатуре st.rerun (см. _rerun_dialog), потому что
   обернуть вызов в try/except НЕЛЬЗЯ — st.rerun работает через RerunException,
   и except перехватил бы поток управления вместо ошибки.

   Закрывать диалог перезапуском приложения нужно ровно в двух местах —
   «Сохранить уточнение» и «Сбросить», где закрытие и есть желаемый результат.

2. ПОСЛЕ ПОИСКА ГАЛКИ ИНОГДА ВОЗВРАЩАЛИСЬ К «ВЫБРАНО ВСЁ».

   Две независимые причины, обе устранены:

   а) Рабочее состояние галок жило ТОЛЬКО внутри st.data_editor, по ключу
      виджета. Любой перезапуск приложения (в том числе паразитный из п.1,
      а также двойной прогон скрипта, который Streamlit иногда делает сам)
      ронял незафиксированные правки, и таблица перестраивалась из data —
      то есть из состояния «отмечено всё». Теперь единственный источник
      истины — набор имён файлов в session_state (_work_key), а data_editor
      только отображает его и отдаёт правки, которые тут же в этот набор
      вливаются (см. _merge_editor_result).

   б) render_npa_filter при несовпадении подписи фильтров вызывал
      clear_npa_filter() — БЕЗВОЗВРАТНО удалял сохранённое уточнение.
      Достаточно было одного прогона с иным составом doc_types (например,
      когда страница вырезает служебный псевдотип hidden_only), чтобы работа
      пользователя исчезла без следа. Теперь несовпадение НИЧЕГО не удаляет:
      уточнение остаётся в session_state, просто не применяется, и
      пользователь видит об этом предупреждение с кнопкой возврата к прежней
      конфигурации. Решение «удалять» здесь в принципе неверно: это
      деструктивная реакция на неоднозначность.
"""

import inspect
import time
from typing import List, Optional, Set

import pandas as pd
import streamlit as st

from core import advisor


# =============================================================================
# Человекочитаемые подписи метаданных
# =============================================================================
DOC_TYPE_LABELS = {
    "npa":       "НПА",
    "fas":       "Позиция ФАС",
    "court":     "Судебная практика",
    "methodics": "Методика",
    "unknown":   "Не определён",
    "":          "Не определён",
}

DOC_STATUS_LABELS = {
    "active":  "Действует",
    "pending": "Не вступил в силу",
    "expired": "Утратил силу",
    "":        "Не указан",
}

# Заголовок колонки с галкой в DataFrame. Короткий — колонка не должна
# тянуть на себя ширину; подпись для пользователя задаётся в column_config.
COL_PICK = "✓"


# =============================================================================
# Перезапуск диалога без его закрытия
#
# См. пункт 1 в шапке файла. Проверяем поддержку scope по сигнатуре функции,
# а не через try/except: st.rerun реализован через RerunException, и except
# поймал бы штатный поток управления.
# =============================================================================
try:
    _RERUN_HAS_SCOPE = "scope" in inspect.signature(st.rerun).parameters
except (TypeError, ValueError):       # сигнатура недоступна (обёртка/сборка)
    _RERUN_HAS_SCOPE = False


def _rerun_dialog():
    """
    Перезапускает ТОЛЬКО тело диалога, оставляя окно открытым.

    На Streamlit старше 1.37 (без scope) деградирует до обычного перезапуска:
    окно закроется, но данные уже лежат в session_state и не потеряются —
    пользователь просто откроет диалог заново и увидит свои галки.
    """
    if _RERUN_HAS_SCOPE:
        st.rerun(scope="fragment")
    else:
        st.rerun()


# =============================================================================
# Ключи состояния
#
# ПОЧЕМУ КЛЮЧ ЗАВИСИТ ОТ НЕЙМСПЕЙСА. Один Streamlit-процесс обслуживает всех
# пользователей. session_state у каждого свой, но ключ с неймспейсом стоит
# дёшево и страхует от путаницы при перелогине внутри одной вкладки.
#
# ПОЧЕМУ ХРАНИТСЯ ПОДПИСЬ ФИЛЬТРОВ. Перечень документов осмыслен только в
# паре со сферой/видом/актуальностью, при которых он собран. Сменил
# пользователь сферу — прежний набор имён относится к другой выборке и
# применяться не должен. Но и удаляться не должен (см. пункт 2б в шапке):
# он просто не применяется, пока конфигурация не совпадёт снова.
# =============================================================================
def _base_key() -> str:
    try:
        ns = advisor.get_cache_namespace()
    except Exception:
        ns = "anon"
    return f"npa_filter::{ns}"


def _saved_key() -> str:
    """Сохранённое (подтверждённое кнопкой) уточнение."""
    return f"{_base_key()}::saved"


def _work_key() -> str:
    """
    Рабочий набор галок внутри открытого диалога.

    ЕДИНСТВЕННЫЙ источник истины для таблицы. st.data_editor его только
    отображает — см. пункт 2а в шапке файла.
    """
    return f"{_base_key()}::work"


def _ver_key() -> str:
    """
    Счётчик версии датагрида.

    st.data_editor запоминает правки пользователя по ключу виджета и НЕ
    перечитывает переданный data при перезапуске. Поэтому когда набор галок
    меняем МЫ (кнопки «Выбрать все» / «Снять все»), виджету нужен новый
    ключ — иначе он покажет прежние галки, проигнорировав новые данные.
    """
    return f"{_base_key()}::ver"


def _signature(spheres, doc_types, doc_status) -> str:
    """
    Подпись конфигурации фильтров.

    None и [] намеренно дают одинаковую строку: на странице Советчика
    «фильтр не выбран» приходит и тем, и другим способом, и различать их
    здесь означало бы ложные несовпадения подписи.
    """
    return "|".join([
        ",".join(sorted(spheres or [])),
        ",".join(sorted(doc_types or [])),
        str(doc_status or ""),
    ])


def _get_saved() -> dict:
    key = _saved_key()
    if key not in st.session_state:
        st.session_state[key] = {"signature": None, "filenames": None}
    return st.session_state[key]


def get_selected_filenames(spheres: Optional[List[str]] = None,
                           doc_types: Optional[List[str]] = None,
                           doc_status: Optional[str] = "active") -> Optional[List[str]]:
    """
    Активное уточнение перечня НПА или None.

    None означает «фильтр не задан» — поиск идёт по всем документам выборки.
    None же возвращается, если конфигурация фильтров изменилась с момента
    сохранения: прежний перечень относится к другой выборке. Сам перечень при
    этом НЕ удаляется — вернётся прежняя конфигурация, вернётся и он.
    """
    saved = _get_saved()
    if not saved.get("filenames"):
        return None
    if saved.get("signature") != _signature(spheres, doc_types, doc_status):
        return None
    return list(saved["filenames"])


def clear_npa_filter():
    """
    Полный сброс уточнения — и сохранённого, и рабочего.

    Вызывается ТОЛЬКО по явному действию пользователя («Сбросить»). Не
    использовать как реакцию на несовпадение подписи фильтров: это удаляет
    работу пользователя без его ведома (см. пункт 2б в шапке файла).
    """
    st.session_state[_saved_key()] = {"signature": None, "filenames": None}
    _discard_work()


def _discard_work():
    """
    Убирает рабочее состояние диалога и все ключи датагрида.

    Ключи датагрида (их может накопиться по одному на каждое нажатие
    «Выбрать все») удаляем поимённо: иначе при следующем открытии диалога
    st.data_editor с тем же ключом воспроизвёл бы прежние правки поверх
    актуальных данных.
    """
    st.session_state.pop(_work_key(), None)
    st.session_state.pop(_ver_key(), None)
    prefix = f"{_base_key()}::grid::"
    for k in [k for k in st.session_state if str(k).startswith(prefix)]:
        st.session_state.pop(k, None)


# =============================================================================
# Рабочий набор галок
# =============================================================================
def _get_work(sig: str, all_names: List[str]) -> Set[str]:
    """
    Рабочий набор выбранных документов для текущей конфигурации фильтров.

    Инициализация при первом открытии диалога:
      • есть сохранённое уточнение той же конфигурации → берём его;
      • иначе → отмечено всё (по схеме процесса пользователь снимает лишние
        галки, а не набирает нужные с нуля).

    Набор пересекается с all_names: между сохранением и повторным открытием
    база могла быть переиндексирована, и часть документов исчезнуть. Без
    пересечения в фильтр ушли бы имена, которых в базе больше нет — поиск
    вернул бы пустоту без объяснимой причины.
    """
    key   = _work_key()
    state = st.session_state.get(key)
    names = set(all_names)

    if isinstance(state, dict) and state.get("sig") == sig:
        return state["sel"] & names

    saved = _get_saved()
    if saved.get("filenames") and saved.get("signature") == sig:
        sel = set(saved["filenames"]) & names
    else:
        sel = set(names)

    st.session_state[key] = {"sig": sig, "sel": sel}
    return sel


def _set_work(sig: str, sel: Set[str]):
    st.session_state[_work_key()] = {"sig": sig, "sel": set(sel)}


def _bump_grid_version():
    """Новый ключ датагрида — чтобы он перечитал данные (см. _ver_key)."""
    ver_key = _ver_key()
    old_ver = st.session_state.get(ver_key, 0)
    st.session_state.pop(f"{_base_key()}::grid::{old_ver}", None)
    st.session_state[ver_key] = old_ver + 1


# =============================================================================
# Таблица документов
# =============================================================================
def _build_dataframe(docs: List[dict], selected: Set[str]) -> pd.DataFrame:
    """
    Собирает DataFrame для датагрида.

    filename уходит в ИНДЕКС, а не в видимую колонку: индекс переживает
    сортировку и фильтрацию, и по нему однозначно восстанавливается выбор.
    Сопоставлять выбор по колонке «Документ» нельзя — названия НПА
    дублируются (несколько редакций одного приказа).
    """
    rows = []
    for d in docs:
        fname = d["filename"]
        rows.append({
            COL_PICK:      fname in selected,
            "Документ":    fname,
            "Вид":         DOC_TYPE_LABELS.get(d.get("doc_type", ""),
                                               d.get("doc_type", "")),
            "Статус":      DOC_STATUS_LABELS.get(d.get("doc_status", ""),
                                                 d.get("doc_status", "")),
            "Сфера":       d.get("sphere", "") or "—",
            "Действует с": d.get("valid_from", "") or "—",
            "Фрагментов":  d.get("chunks", 0),
            "_fname":      fname,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.set_index("_fname")
    return df


def _merge_editor_result(edited: pd.DataFrame, sel: Set[str]) -> Set[str]:
    """
    Вливает правки датагрида в рабочий набор.

    Обрабатываются только СТРОКИ, показанные в этом прогоне: при активном
    поиске по названию остальные документы не видны, и их состояние менять
    нельзя — иначе пользователь, набравший подстроку, чтобы найти один
    документ, потерял бы весь остальной выбор.
    """
    out = set(sel)
    for fname, checked in edited[COL_PICK].items():
        if bool(checked):
            out.add(fname)
        else:
            out.discard(fname)
    return out


@st.dialog("Уточнение перечня НПА", width="large")
def _npa_dialog(spheres, doc_types, doc_status):
    sig = _signature(spheres, doc_types, doc_status)
    t0  = time.perf_counter()

    docs = advisor.list_documents(spheres=spheres, doc_types=doc_types,
                                  doc_status=doc_status)
    if not docs:
        st.warning(
            "По выбранной сфере и фильтрам в базе нет документов. "
            "Измените сферу, вид документа или актуальность."
        )
        return

    all_names = [d["filename"] for d in docs]
    sel       = _get_work(sig, all_names)

    st.caption(
        f"Документов в выборке: {len(docs)}. "
        "Снимите галки с тех, которые не должны участвовать в ответе — "
        "поиск будет вестись только по отмеченным."
    )

    # ── Массовые действия ───────────────────────────────────────────────────
    # Меняем рабочий набор, обновляем ключ датагрида и перезапускаем ТОЛЬКО
    # диалог. Перезапуск прерывает текущий прогон, поэтому таблица ниже в
    # этом проходе уже не рендерится — и отрисуется с новыми галками.
    _b1, _b2, _b3 = st.columns([1, 1, 3])
    with _b1:
        if st.button("Выбрать все", use_container_width=True,
                     key=f"{_base_key()}::bulk_all"):
            _set_work(sig, set(all_names))
            _bump_grid_version()
            _rerun_dialog()
    with _b2:
        if st.button("Снять все", use_container_width=True,
                     key=f"{_base_key()}::bulk_none"):
            _set_work(sig, set())
            _bump_grid_version()
            _rerun_dialog()

    # ── Поиск по названию ───────────────────────────────────────────────────
    # Обычный text_input: его правка перезапускает фрагмент диалога, а не
    # приложение, поэтому окно остаётся открытым само, без нашего участия.
    search = st.text_input(
        "Поиск по названию",
        key=f"{_base_key()}::search",
        placeholder="часть названия документа",
    )

    df   = _build_dataframe(docs, sel)
    view = df
    if search.strip():
        mask = df["Документ"].str.contains(search.strip(), case=False,
                                          regex=False, na=False)
        view = df[mask]
        st.caption(f"Показано по фильтру: {len(view)} из {len(df)}")
        if view.empty:
            st.info("Ничего не найдено по этой подстроке. "
                    "Галки остальных документов не затронуты.")

    grid_key = f"{_base_key()}::grid::{st.session_state.get(_ver_key(), 0)}"

    edited = st.data_editor(
        view,
        key=grid_key,
        hide_index=True,
        use_container_width=True,
        height=420,
        num_rows="fixed",
        column_config={
            COL_PICK: st.column_config.CheckboxColumn(
                "Вкл.", help="Участвует в поиске", default=True, width="small",
            ),
            "Документ":    st.column_config.TextColumn("Документ", width="large"),
            "Вид":         st.column_config.TextColumn("Вид", width="small"),
            "Статус":      st.column_config.TextColumn("Статус", width="small"),
            "Сфера":       st.column_config.TextColumn("Сфера", width="medium"),
            "Действует с": st.column_config.TextColumn("Действует с", width="small"),
            "Фрагментов":  st.column_config.NumberColumn(
                "Фрагментов", help="Число проиндексированных чанков документа",
                width="small",
            ),
        },
        disabled=["Документ", "Вид", "Статус", "Сфера", "Действует с", "Фрагментов"],
    )

    # Правки — сразу в рабочий набор, а не при нажатии «Сохранить»: иначе
    # любой промежуточный перезапуск диалога (ввод в поиск, клик по массовой
    # кнопке) терял бы уже снятые галки.
    sel = _merge_editor_result(edited, sel)
    _set_work(sig, sel)

    st.divider()
    _all_selected = len(sel) == len(all_names)
    if _all_selected:
        st.markdown(f"**Выбрано: все {len(all_names)} документов**")
        st.caption("Уточнение с полным перечнем равносильно его отсутствию — "
                   "поиск пойдёт по всей выборке.")
    else:
        st.markdown(f"**Выбрано документов: {len(sel)} из {len(all_names)}**")

    _s1, _s2 = st.columns([2, 1])
    with _s1:
        save = st.button("Сохранить уточнение", type="primary",
                         use_container_width=True, disabled=not sel,
                         key=f"{_base_key()}::save")
    with _s2:
        reset = st.button("Сбросить", use_container_width=True,
                          key=f"{_base_key()}::reset")

    if not sel:
        st.warning("Не выбрано ни одного документа — сохранять нечего.")

    # Здесь перезапуск ПРИЛОЖЕНИЯ (scope по умолчанию) — и это правильно:
    # закрытие диалога является желаемым результатом обеих кнопок.
    if save:
        if _all_selected:
            # Полный перечень как фильтр бессмысленен и только мешает: он
            # отключил бы FAQ и локальную базу сегмента (см. search_vector_db),
            # ничего при этом не сузив.
            clear_npa_filter()
            st.toast("Выбраны все документы — уточнение снято")
        else:
            st.session_state[_saved_key()] = {
                "signature": sig,
                "filenames": sorted(sel),
            }
            _discard_work()
            print(f"[NPA FILTER] Сохранено {len(sel)} из {len(all_names)} "
                  f"документов за {time.perf_counter()-t0:.2f} сек "
                  f"(подпись: {sig})")
        st.rerun()

    if reset:
        clear_npa_filter()
        st.rerun()


# =============================================================================
# Кнопка на странице Советчика
# =============================================================================
def render_npa_filter(spheres: Optional[List[str]] = None,
                      doc_types: Optional[List[str]] = None,
                      doc_status: Optional[str] = "active",
                      label: str = "Уточнить перечень НПА"):
    """
    Рисует кнопку уточнения и строку его текущего состояния.

    Кнопка НЕАКТИВНА, пока не выбрана сфера: без сферы список документов —
    это вся база целиком, выбирать в нём вручную бессмысленно.

    При несовпадении подписи фильтров уточнение НЕ удаляется — показывается
    предупреждение (см. пункт 2б в шапке файла).
    """
    saved       = _get_saved()
    sig         = _signature(spheres, doc_types, doc_status)
    has_sphere  = bool(spheres)
    has_saved   = bool(saved.get("filenames"))
    is_active   = has_saved and saved.get("signature") == sig

    c1, c2 = st.columns([2, 3])
    with c1:
        clicked = st.button(
            label,
            disabled=not has_sphere,
            use_container_width=True,
            key="npa_filter_open_btn",
            help=("Выберите сферу, чтобы уточнить перечень документов"
                  if not has_sphere
                  else "Выбрать конкретные документы для ответа"),
        )
    with c2:
        if is_active:
            st.success(f"Уточнение активно: {len(saved['filenames'])} док.",
                       icon="✅")
        elif has_saved:
            st.warning("Уточнение не применяется: фильтры изменились.",
                       icon="⚠️")
        elif has_sphere:
            st.caption("Уточнение не задано — поиск по всем документам выборки.")

    if clicked:
        _npa_dialog(spheres, doc_types, doc_status)

    if has_saved and not is_active:
        # Перечень цел и ждёт прежней конфигурации. Показываем, какой именно
        # она была: пользователь обычно не помнит, при какой сфере он собирал
        # список, и без этой подсказки уточнение выглядит просто сломанным.
        _prev = (saved.get("signature") or "").split("|")
        _prev_spheres = _prev[0] if _prev and _prev[0] else "(все сферы)"
        st.caption(
            f"Сохранённый перечень ({len(saved['filenames'])} док.) собран для: "
            f"{_prev_spheres.replace(',', '  ·  ')}. Верните эту сферу, чтобы он "
            f"снова применялся, либо соберите перечень заново."
        )
        if st.button("Удалить сохранённый перечень", key="npa_filter_drop_stale"):
            clear_npa_filter()
            st.rerun()

    if is_active:
        with st.expander("Какие документы участвуют в ответе"):
            for fname in saved["filenames"]:
                st.write("• " + fname)
            if st.button("Сбросить уточнение", key="npa_filter_reset_btn"):
                clear_npa_filter()
                st.rerun()