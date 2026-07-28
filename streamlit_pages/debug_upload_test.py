# streamlit_pages/debug_upload_test.py
"""
ВРЕМЕННАЯ диагностическая страница — бисекция бага зависания загрузки.

v4: добавлен блок ПОСЛЕ file_uploader — разделение xlsx/doc файлов,
список с чекбоксами "расч.", предупреждения, кнопка "Разобрать"
(без реальной обработки по клику — только сама кнопка).

Если ЗДЕСЬ зависнет — виновник в этом блоке (скорее всего, в цикле
с чекбоксами, использующем uf.name как часть ключа).
Если работает — значит проблема глубже, внутри самой обработки по
клику btn_parse (core.claim_analyzer_logic._build_file_summaries и т.д.)
или в чём-то, что отличает вызов через app.py от прямого вызова.
"""
import os
import streamlit as st

from core.claim_analyzer_logic import REGULATION_SPHERES, SPHERE_LABELS


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 ** 2:
        return f"{n/1024:.1f} КБ"
    return f"{n/1024**2:.2f} МБ"


def show_debug_upload_test():
    st.header("🧪 Диагностика v4: + список файлов с чекбоксами")
    st.info(
        "Тест с полным блоком до И после file_uploader (без реальной "
        "обработки по кнопке). Если зависнет здесь — виновник в списке файлов."
    )

    if not st.session_state.get("_ca_reranker_preloaded"):
        try:
            from core.advisor import get_reranker
            get_reranker()
            st.session_state["_ca_reranker_preloaded"] = True
        except Exception:
            pass

    ss = st.session_state
    for k, v in [
        ("ca_org", ""), ("ca_period", ""), ("ca_done", False),
        ("ca_spheres", []), ("ca_target_pct", 5.0), ("ca_risk_pct", 10.0),
        ("ca_calc_files_checked", []),
    ]:
        if k not in ss:
            ss[k] = v

    st.subheader("Сфера регулирования")
    selected_sphere_labels = st.multiselect(
        "Сферы регулирования",
        options=[f"{s['icon']} {s['label']}" for s in REGULATION_SPHERES],
        default=[SPHERE_LABELS[sid] for sid in ss.ca_spheres if sid in SPHERE_LABELS],
        label_visibility="collapsed",
        key="ca_spheres_select",
        placeholder="Все сферы (без фильтра)",
    )
    label_to_id = {f"{s['icon']} {s['label']}": s["id"] for s in REGULATION_SPHERES}
    ss.ca_spheres = [label_to_id[lbl] for lbl in selected_sphere_labels if lbl in label_to_id]

    with st.expander("Реквизиты заявки", expanded=not ss.ca_done):
        c1, c2 = st.columns(2)
        ss.ca_org    = c1.text_input("Организация", value=ss.ca_org, key="ca_org_input")
        ss.ca_period = c2.text_input("Период регулирования", value=ss.ca_period, key="ca_period_input")

    st.divider()
    st.subheader("Файлы заявки (тест)")

    uploaded = st.file_uploader(
        "Перетащите файлы или нажмите «Browse files»",
        type=["xlsx", "xls", "pdf", "docx", "doc"],
        accept_multiple_files=True,
        key="ca_uploader_static",
    ) or []

    if uploaded:
        # ── Точная копия реального блока из claim_analyzer.py ────────────
        xlsx_files = [f for f in uploaded
                      if os.path.splitext(f.name.lower())[1] in (".xlsx", ".xls")]
        doc_files  = [f for f in uploaded
                      if os.path.splitext(f.name.lower())[1] in (".pdf", ".docx", ".doc")]

        st.success(
            f"Загружено: **{len(uploaded)}** файл(ов) — "
            f"{len(xlsx_files)} расчётных · {len(doc_files)} документов"
        )

        st.markdown("Отметьте расчётные модели (Excel-файлы со статьями затрат):")

        calc_checked = []
        for uf in uploaded:
            ext = os.path.splitext(uf.name.lower())[1]
            is_xlsx = ext in (".xlsx", ".xls")
            cc1, cc2 = st.columns([5, 1])
            cc1.write(f"{uf.name} · {_format_size(uf.size)}")
            if is_xlsx:
                default_checked = (
                    uf.name in ss.get("ca_calc_files_checked", [])
                    or (not ss.get("ca_calc_files_checked") and len(xlsx_files) == 1)
                )
                if cc2.checkbox("расч.", key=f"ca_calc_{uf.name}", value=default_checked):
                    calc_checked.append(uf.name)
            else:
                cc2.write("")

        ss["ca_calc_files_checked"] = calc_checked

        has_calc = bool(calc_checked)
        if xlsx_files and not has_calc:
            st.warning("Не выбрана ни одна расчётная модель.")
        elif not xlsx_files:
            st.info("В загруженных файлах нет Excel-таблиц.")

        st.divider()

        _block_run = bool(xlsx_files) and not has_calc
        if _block_run:
            st.error("Выберите хотя бы одну расчётную модель.")

        btn_parse = st.button(
            "Разобрать расчётный файл (тест — без реальной обработки)",
            type="primary",
            use_container_width=True,
            key="ca_btn_parse",
            disabled=_block_run,
        )
        if btn_parse:
            st.success("✅ Кнопка нажата (реальная обработка НЕ выполняется в тесте)")
    else:
        st.warning("Файлы ещё не выбраны")


if __name__ == "__main__":
    show_debug_upload_test()