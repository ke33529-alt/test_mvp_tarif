# streamlit_pages/claim_analyzer.py
"""
UI Анализатора тарифных заявок
──────────────────────────────────────────────────────────────────────────────
Вкладки:
  1. Риски и комплектность — LLM-анализ рисков по статьям + оценка документов
  2. Реестр заявок         — сохранение, поиск, управление статусами

Бизнес-логика → core/claim_analyzer_logic.py
Расчётные Excel → core/calc_parser
Промпты         → config/prompts.json (Админка)
Реестр          → core/claim_registry  (data/claims/)
"""

from __future__ import annotations
import os, io, json, time, re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st

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

def _render_risks_tab(risks_json: str, claim_summary: str = "", show_summary: bool = True, key_prefix: str = "ca"):
    """
    Кастомный рендеринг постатейного анализа рисков.
    risks_json    — строка JSON от analyze_risks() или старый markdown.
    claim_summary — итоговое резюме заявки (отображается над списком статей).
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
        if _rv and float(_rv) > 0:
            _viz_rows.append({
                "name":  _a.get("name", "")[:45],
                "value": round(float(_rv)),
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
            if len(items) == 1:
                out.append((items[0], x, y, w, h))
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
                    ch = h * (d["value"] / rS)
                    out.append((d, x, cy, rw, ch))
                    cy += ch
                _sq(rest, x + rw, y, w - rw, h, total - rS, out)
            else:
                rh, cx = h * rf, x
                for d in row:
                    cw = w * (d["value"] / rS)
                    out.append((d, cx, y, cw, rh))
                    cx += cw
                _sq(rest, x, y + rh, w, h - rh, total - rS, out)

        with st.expander("📊 Визуализация статей затрат", expanded=True):
          _vtab1, _vtab2 = st.tabs(["Карта затрат", "Топ-20"])

          with _vtab1:
            _sorted = sorted(_viz_rows, key=lambda x: -x["value"])
            _total  = sum(d["value"] for d in _sorted)
            _rects  = []
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

    # ── Фильтр ───────────────────────────────────────────────────────────────
    st.markdown("**Постатейный анализ**")
    f_col1, f_col2, f_col3 = st.columns(3)
    show_red    = f_col1.checkbox("🔴 Высокий риск", value=True,  key=f"{key_prefix}_f_red")
    show_yellow = f_col2.checkbox("🟡 Средний риск",  value=True,  key=f"{key_prefix}_f_yellow")
    show_green  = f_col3.checkbox("🟢 Без замечаний", value=False, key=f"{key_prefix}_f_green")
    filter_map  = {"red": show_red, "yellow": show_yellow,
                   "green": show_green}
    visible = [a for a in articles if filter_map.get(a.get("risk", "red"), True)]
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
        matched_files = art.get("matched_files", [])

        # Заголовок: emoji + название + значение регулируемого года
        exp_title = f"{emoji} {name[:70]}"
        if reg_val is not None:
            exp_title += f"  ·  {reg_val:,.0f} тыс.руб."
        elif amounts:
            first_val = amounts.split("|")[0].strip()
            if first_val:
                exp_title += f"  ·  {first_val[:40]}"

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

                if matched_files:
                    st.markdown("**Наиболее вероятные документы-обоснования:**")
                    for d in matched_files:
                        sim_pct = int(d.get("_similarity", 0) * 100)
                        fname   = d.get("file_name", "—")
                        st.caption(f"{'▓' * (sim_pct // 20)}{'░' * (5 - sim_pct // 20)} "
                                   f"{sim_pct}%  —  {fname}")
                else:
                    st.caption("Файлы-обоснования в загруженных документах не найдены.")

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



def _show_article_approval(readonly: bool = False):
    """
    Таблица апрува статей затрат с фильтрацией по листам,
    ручным добавлением, редактированием значений.
    """
    import pandas as pd
    ss = st.session_state
    articles = ss.ca_parsed_articles

    TYPE_OPT_LBLS = {
        "cost": "Статья затрат",
        "agg":  "Агрегат / итог",
        "ref":  "Справочно",
        "zero": "Нулевые",
    }

    n_total   = len(articles)
    n_checked = sum(1 for a in articles if a["checked"])

    st.subheader("Проверка статей затрат")
    st.caption(
        f"Найдено строк: **{n_total}** · "
        f"К анализу: **{n_checked}** · "
        f"Исключено: **{n_total - n_checked}**"
    )

    # ── Фильтры ──────────────────────────────────────────────────────────
    all_sheets = list(dict.fromkeys(a["sheet"] for a in articles if a.get("sheet")))
    tech_sheets = {a["sheet"] for a in articles if a.get("tech_sheet")}

    # Легенда листов: технические помечаем ⚠️
    sheet_options = ["Все листы"] + [
        (f"⚠️ {s}" if s in tech_sheets else s) for s in all_sheets
    ]
    sheet_display_to_real = {}
    for s in all_sheets:
        label = f"⚠️ {s}" if s in tech_sheets else s
        sheet_display_to_real[label] = s

    f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
    search_q = f1.text_input("Поиск", placeholder="Фильтр по названию...",
                              key="ca_ap_search", label_visibility="collapsed")
    type_filter = f2.selectbox("Тип",
        ["Все", "Статья затрат", "Агрегат / итог", "Справочно", "Нулевые"],
        key="ca_ap_type", label_visibility="collapsed")
    sheet_sel_lbl = f3.selectbox("Лист", sheet_options,
        key="ca_ap_sheet", label_visibility="collapsed")
    sheet_filter = None if sheet_sel_lbl == "Все листы" else sheet_display_to_real.get(sheet_sel_lbl)
    # Фильтр «только выбранные» — toggle через session state
    if "ca_ap_only_checked" not in ss:
        ss["ca_ap_only_checked"] = False
    f4.markdown("<div style='padding-top:4px'></div>", unsafe_allow_html=True)
    only_checked = f4.checkbox("☑ Только выбранные", key="ca_ap_only_checked")

    # ── Кнопки действий ──────────────────────────────────────────────────
    if not readonly:
        def _reset_editor():
            for k in ["ca_ap_editor", "ca_ap_add_name", "ca_ap_add_value",
                      "ca_ap_add_unit", "ca_ap_add_year", "ca_ap_add_pf"]:
                ss.pop(k, None)

        # Вычисляем tf здесь чтобы передать в _in_filter
        _TYPE_FILTER_MAP = {
            "Все": None, "Статья затрат": "cost", "Агрегат / итог": "agg",
            "Справочно": "ref", "Нулевые": "zero",
        }
        tf = _TYPE_FILTER_MAP.get(type_filter)
        bc = st.columns(7)
        if bc[0].button("Авто-отбор", key="ca_ap_auto", use_container_width=True,
                        help="Только статьи затрат без технических листов"):
            for a in articles:
                a["checked"] = (a["type"] == "cost" and not a.get("tech_sheet"))
            ss.ca_parsed_articles = articles; _reset_editor(); st.rerun()

        if bc[1].button("Снять нулевые", key="ca_ap_unzero", use_container_width=True,
                        help="Снять флаги со всех нулевых/справочных"):
            for a in articles:
                if a["type"] in ("zero", "ref", "agg"):
                    a["checked"] = False
            ss.ca_parsed_articles = articles; _reset_editor(); st.rerun()

        # Снь без значений — статьи с прочерком в регул. году
        if bc[2].button("Снять без значений", key="ca_ap_unblank", use_container_width=True,
                        help="Снять флаги со всех статей у которых нет ни одного ненулевого значения"):
            for a in articles:
                ts = _parse_amounts_timeseries(a["amounts"])
                has_value = any(v != 0 for _, _, v in ts)
                if not has_value:
                    a["checked"] = False
            ss.ca_parsed_articles = articles; _reset_editor(); st.rerun()

        if bc[3].button("Снять лист", key="ca_ap_unsheet", use_container_width=True,
                        help="Снять флаги со всех статей выбранного листа",
                        disabled=(sheet_filter is None)):
            for a in articles:
                if a.get("sheet") == sheet_filter:
                    a["checked"] = False
            ss.ca_parsed_articles = articles; _reset_editor(); st.rerun()

        # Вспомогательная функция: попадает ли статья в текущий фильтр
        def _in_filter(a):
            if search_q and search_q.lower() not in a["name"].lower():
                return False
            if tf and a["type"] != tf:
                return False
            if sheet_filter and a.get("sheet") != sheet_filter:
                return False
            return True

        _filter_hint = (
            f" (лист: {sheet_filter})" if sheet_filter else
            f" (тип: {type_filter})" if type_filter != "Все" else
            f" (поиск: {search_q})" if search_q else ""
        )

        if bc[4].button(f"✅ Выбрать{_filter_hint or ' все'}",
                        key="ca_ap_all", use_container_width=True,
                        help="Выбрать все статьи в текущей фильтрации"):
            for a in articles:
                if _in_filter(a):
                    a["checked"] = True
            ss.ca_parsed_articles = articles; _reset_editor(); st.rerun()

        if bc[5].button(f"☐ Убрать{_filter_hint or ' все'}",
                        key="ca_ap_none", use_container_width=True,
                        help="Убрать флаги со всех статей в текущей фильтрации"):
            for a in articles:
                if _in_filter(a):
                    a["checked"] = False
            ss.ca_parsed_articles = articles; _reset_editor(); st.rerun()

        if bc[6].button("Инверсия", key="ca_ap_inv", use_container_width=True,
                        help="Инвертировать выбор в текущей фильтрации"):
            for a in articles:
                if _in_filter(a):
                    a["checked"] = not a["checked"]
            ss.ca_parsed_articles = articles; _reset_editor(); st.rerun()

    # ── Таблица ──────────────────────────────────────────────────────────
    TYPE_FILTER_MAP = {
        "Все": None, "Статья затрат": "cost", "Агрегат / итог": "agg",
        "Справочно": "ref", "Нулевые": "zero",
    }
    tf = TYPE_FILTER_MAP.get(type_filter)

    # Годы для столбцов: из настроек если заданы, иначе — авто
    _reg_yr_set = int(ss.get('ca_reg_year', 0)) if ss.get('ca_reg_year') else 0
    _yr_range   = ss.get('ca_year_range')
    if _reg_yr_set and _yr_range:
        _yr_from, _yr_to = int(_yr_range[0]), int(_yr_range[1])
        _year_cols = [str(y) for y in range(_yr_from, _yr_to + 1)]
    else:
        _sample_ts = None
        for _a in articles:
            _ts = _parse_amounts_timeseries(_a['amounts'])
            if len(_ts) >= 2:
                _sample_ts = _ts; break
        if _sample_ts:
            _year_cols = [str(t[0]) for t in _sample_ts[-4:]]
        else:
            _year_cols = ['Рег.год']

    def _make_df(arts):
        rows = []
        for i, a in enumerate(arts):
            if search_q and search_q.lower() not in a['name'].lower():
                continue
            if only_checked and not a['checked']:
                continue
            if tf and a['type'] != tf:
                continue
            if sheet_filter and a.get('sheet') != sheet_filter:
                continue
            ts = _parse_amounts_timeseries(a['amounts'])
            ts_by_yr = {str(t[0]): t[2] for t in ts}
            sheet_lbl = a.get('sheet', '')
            if a.get('tech_sheet'):
                sheet_lbl = f'⚠️ {sheet_lbl}'
            elif a.get('manual'):
                sheet_lbl = '✏️ вручную'
            row = {
                '_idx':          i,
                'Включить':    a['checked'],
                'Наименование': a['name'],
                'Лист':        sheet_lbl,
                'Ед.изм.':     a.get('unit', ''),
            }
            for _yr in _year_cols:
                _v = ts_by_yr.get(_yr)
                row[_yr] = f'{_v:,.0f}' if _v is not None else '—'
            row['Тип'] = TYPE_OPT_LBLS.get(a['type'], a['type'])
            rows.append(row)
        return pd.DataFrame(rows)

    # Сообщение об успешном добавлении (после rerun)
    if ss.get("_ap_added_msg"):
        st.success(f"✅ Добавлено: {ss['_ap_added_msg']}")
        ss.pop("_ap_added_msg", None)

    df_show = _make_df(articles)
    st.caption(f"Показано: {len(df_show)} из {n_total}")

    edited = None
    if not readonly and not df_show.empty:
        edited = st.data_editor(
            df_show.drop(columns=["_idx"]),
            column_config={
                "Включить":    st.column_config.CheckboxColumn("Включить", width="small"),
                "Наименование": st.column_config.TextColumn("Наименование", width="large", disabled=True),
                "Лист":        st.column_config.TextColumn("Лист", width="medium", disabled=True),
                "Ед.изм.":     st.column_config.TextColumn("Ед.изм.", width="small"),
                **{yr: st.column_config.TextColumn(yr, width='small', disabled=True) for yr in _year_cols},
                "Тип":         st.column_config.SelectboxColumn(
                    "Тип", width="medium",
                    options=list(TYPE_OPT_LBLS.values()),
                ),
            },
            use_container_width=True,
            hide_index=True,
            key="ca_ap_editor",
        )
        n_sel_live = int(edited["Включить"].sum()) if edited is not None else 0

    elif readonly and not df_show.empty:
        st.dataframe(
            df_show.drop(columns=["_idx"]).rename(columns={"Включить": "✓"}),
            use_container_width=True, hide_index=True,
        )
        n_sel_live = sum(1 for a in articles if a["checked"])
    else:
        n_sel_live = 0

    # ── Ручное добавление статьи (вне expander — иначе кнопка не работает) ──
    if not readonly:
        st.markdown("**➕ Добавить статью вручную**")
        ac1, ac2, ac3, ac4, ac5, ac6 = st.columns([3, 1, 1, 1, 1, 1])
        add_name  = ac1.text_input("Наименование", key="ca_ap_add_name",
                                   placeholder="Расходы на ремонт...")
        add_year  = ac2.text_input("Год", key="ca_ap_add_year",
                                   placeholder="2027")
        add_pf    = ac3.selectbox("Тип", ["Принято", "Предложение", "Факт"],
                                  key="ca_ap_add_pf")
        add_value = ac4.text_input("Значение", key="ca_ap_add_value",
                                   placeholder="12345.00")
        if "ca_ap_add_unit" not in ss:
            ss["ca_ap_add_unit"] = "тыс.руб."
        add_unit = ac5.text_input("Ед.изм.", key="ca_ap_add_unit",
                                  placeholder="тыс.руб.")
        if ac6.button("Добавить", key="ca_ap_add_btn", type="primary",
                      use_container_width=True):
            if add_name.strip() and add_value.strip():
                try:
                    val  = float(add_value.replace(",", ".").replace(" ", ""))
                    year = add_year.strip() or "2027"
                    amounts = f"{year} ({add_pf}): {val:,.2f} {add_unit}"
                    ss.ca_parsed_articles.append({
                        "name":       add_name.strip(),
                        "amounts":    amounts,
                        "type":       "cost",
                        "checked":    True,
                        "sheet":      "вручную",
                        "unit":       add_unit.strip(),
                        "tech_sheet": False,
                        "manual":     True,
                    })
                    for k in ["ca_ap_add_name", "ca_ap_add_value",
                               "ca_ap_add_year", "ca_ap_editor"]:
                        ss.pop(k, None)
                    ss.pop("ca_ap_sheet", None)
                    ss["_ap_added_msg"] = add_name.strip()
                    st.rerun()
                except ValueError:
                    st.error("Некорректное значение — введите число")
            else:
                st.warning("Заполните наименование и значение")

    # ── Подтверждение ────────────────────────────────────────────────────
    st.divider()
    ap_c1, ap_c2 = st.columns([3, 1])
    ap_c1.caption(f"Отмечено к анализу: **{n_sel_live}** статей")
    if not readonly and ap_c2.button(
        "Подтвердить и продолжить", type="primary",
        use_container_width=True, key="ca_ap_confirm",
        disabled=(n_sel_live == 0),
    ):
        _lbl_to_type = {v: k for k, v in TYPE_OPT_LBLS.items()}
        if edited is not None:
            for row_i, row in edited.iterrows():
                orig_i = int(df_show.iloc[row_i]["_idx"])
                articles[orig_i]["checked"] = bool(row["Включить"])
                articles[orig_i]["type"]    = _lbl_to_type.get(row["Тип"], "cost")
                # Сохраняем отредактированную единицу
                articles[orig_i]["unit"] = str(row["Ед.изм."]).strip()

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
        for k in ["ca_ap_editor", "ca_ap_add_name", "ca_ap_add_value",
                  "ca_ap_add_unit", "ca_ap_add_year"]:
            ss.pop(k, None)
        st.rerun()



def show_claim_analyzer():
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
        ]
        for _k in _CA_KEYS:
            st.session_state.pop(_k, None)
        # Сбрасываем uploader через смену ключа
        st.session_state["ca_uploader_key"] = st.session_state.get("ca_uploader_key", 0) + 1
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
        "Чтобы загрузить папку целиком: откройте папку в проводнике, "
        "нажмите Ctrl+A для выделения всех файлов, затем перетащите их сюда."
    )
    _uploader_key = f"ca_uploader_{ss.get('ca_uploader_key', 0)}"
    uploaded = st.file_uploader(
        "Перетащите файлы или нажмите «Browse files»",
        type=["xlsx", "xls", "pdf", "docx", "doc"],
        accept_multiple_files=True,
        key=_uploader_key,
    ) or []

    if uploaded:
        # Разделяем Excel и документы
        xlsx_files = [f for f in uploaded
                      if os.path.splitext(f.name.lower())[1] in (".xlsx", ".xls")]
        doc_files  = [f for f in uploaded
                      if os.path.splitext(f.name.lower())[1] in (".pdf", ".docx", ".doc")]

        st.success(
            f"Загружено: **{len(uploaded)}** файл(ов) — "
            f"{len(xlsx_files)} расчётных · {len(doc_files)} документов"
        )

        # ── Список файлов с выбором расчётной модели ─────────────────────────
        st.markdown("Отметьте расчётные модели (Excel-файлы со статьями затрат):")

        calc_checked: List[str] = []
        for uf in uploaded:
            ext = os.path.splitext(uf.name.lower())[1]
            is_xlsx = ext in (".xlsx", ".xls")
            c1, c2 = st.columns([5, 1])
            c1.write(f"{uf.name} · {_format_size(uf.size)}")
            if is_xlsx:
                default_checked = (
                    uf.name in ss.get("ca_calc_files_checked", [])
                    or (not ss.get("ca_calc_files_checked") and len(xlsx_files) == 1)
                )
                if c2.checkbox(
                    "расч.", key=f"ca_calc_{uf.name}",
                    value=default_checked,
                    help="Отметить как расчётную модель"
                ):
                    calc_checked.append(uf.name)
            else:
                c2.write("")  # выравнивание

        ss["ca_calc_files_checked"] = calc_checked

        # ── Предупреждение если нет ни одной расчётной модели ────────────────
        has_calc = bool(calc_checked)
        if xlsx_files and not has_calc:
            st.warning(
                "Не выбрана ни одна расчётная модель. "
                "Отметьте галочкой 🧮 хотя бы один Excel-файл со статьями затрат — "
                "без него анализ рисков будет неполным."
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
                "Выберите хотя бы одну расчётную модель (галочка напротив Excel-файла)."
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
            # Кешируем байты
            ss.ca_uploaded_bytes = {}
            ss.ca_uploaded_meta  = []
            for uf in uploaded:
                b = uf.read()
                ss.ca_uploaded_bytes[uf.name] = b
                ss.ca_uploaded_meta.append({"name": uf.name, "size": len(b)})

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
            st.rerun()

        # ── Шаг 2: экспандер с таблицей апрува ──────────────────────────────
        # Если парсинг выполнен но ничего не нашли
        if ss.ca_calc_context and not ss.ca_parsed_articles and not ss.ca_done:
            st.error(
                "Статьи затрат не найдены в расчётном файле. "
                "Возможные причины: незаполненный шаблон, нераспознанный формат, "
                "или все строки имеют нулевые значения."
            )
        if ss.ca_parsed_articles:
            n_arts = len(ss.ca_parsed_articles)
            n_sel  = sum(1 for a in ss.ca_parsed_articles if a["checked"])
            _frozen = ss.ca_done  # после запуска анализа — только просмотр

            exp_label = (
                f"Статьи затрат: {n_sel} к анализу из {n_arts}"
                + (" · анализ запущен" if _frozen else " · требует подтверждения" if not ss.ca_articles_approved else " · подтверждено")
            )
            with st.expander(exp_label, expanded=not ss.ca_articles_approved and not _frozen):
                if _frozen:
                    # Режим просмотра — только чтение
                    _show_article_approval(readonly=True)
                else:
                    _show_article_approval(readonly=False)

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

            # ── Чтение заголовков документов (первые 2 страницы каждого файла) ──
            n_doc_files = sum(
                1 for name in ss.ca_uploaded_bytes
                if name not in calc_names
                and os.path.splitext(name.lower())[1]
                in ('.pdf', '.docx', '.doc', '.txt')
            )
            if n_doc_files > 0:
                pbar.progress(0.20)

                def _pcb_sum(frac, msg):
                    val = 0.20 + frac * 0.20
                    pbar.progress(min(val, 0.40))
                    status.text(msg)

                file_summaries = _build_file_summaries(
                    uploaded_bytes=ss.ca_uploaded_bytes,
                    calc_file_names=calc_names,
                    progress_cb=_pcb_sum,
                )
                ss["ca_file_summaries"] = file_summaries
                if file_summaries:
                    names_preview = ', '.join(list(file_summaries.keys())[:3])
                    suffix = '...' if len(file_summaries) > 3 else ''
                    st.info(f'Документов суммаризировано: **{len(file_summaries)}** — '
                            f'{names_preview}{suffix}')
                else:
                    st.caption('Документальные файлы не обработаны — анализ только по НПА.')
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

            # ── Автосохранение в реестр ───────────────────────────────────────
            status.text("Сохраняю в реестр...")
            try:
                from core.claim_registry import save_project
                _files_data = [
                    {"name": meta["name"],
                     "bytes": ss.ca_uploaded_bytes.get(meta["name"], b"")}
                    for meta in ss.ca_uploaded_meta
                ]
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

            # Всегда перечитываем байты — они доступны только при нажатии кнопки
            calc_names = ss.get("ca_calc_files_checked", [])
            ss.ca_uploaded_bytes = {}
            ss.ca_uploaded_meta  = []
            for uf in uploaded:
                b = uf.read()
                ss.ca_uploaded_bytes[uf.name] = b
                ss.ca_uploaded_meta.append({"name": uf.name, "size": len(b)})

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

                    def _pcb_sum_r(frac, msg):
                        val = 0.12 + frac * 0.03
                        pbar.progress(min(val, 0.15))
                        status.text(msg)

                    file_summaries = _build_file_summaries(
                        uploaded_bytes=ss.ca_uploaded_bytes,
                        calc_file_names=calc_names,
                        progress_cb=_pcb_sum_r,
                    )
                    ss["ca_file_summaries"] = file_summaries
                    if file_summaries:
                        names_preview = ', '.join(list(file_summaries.keys())[:3])
                        suffix = '...' if len(file_summaries) > 3 else ''
                        st.info(f'Прочитано заголовков: **{len(file_summaries)}** — {names_preview}{suffix}')
                    else:
                        st.caption('Документы не обработаны — анализ только по НПА.')

            ss["_pbar_max"] = 0.15

            def _pcb_r(pct, msg):
                val = min(0.15 + pct * 0.84, 0.99)
                if val > ss.get("_pbar_max", 0):
                    ss["_pbar_max"] = val
                    pbar.progress(val)
                status.text(msg)

            ss.ca_risks = analyze_risks(
                ss.ca_calc_context, ss.ca_summary, _pcb_r,
                spheres=ss.ca_spheres or None,
                file_summaries=ss.get("ca_file_summaries", {}),
                target_pct=float(ss.get("ca_target_pct", 5.0)),
                risk_pct=float(ss.get("ca_risk_pct", 10.0)),
                approved_articles=ss.ca_parsed_articles or None,
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
                _files_data = [
                    {"name": meta["name"],
                     "bytes": ss.ca_uploaded_bytes.get(meta["name"], b"")}
                    for meta in ss.ca_uploaded_meta
                ]
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
                    files_data = [
                        {"name": meta["name"],
                         "bytes": ss.ca_uploaded_bytes.get(meta["name"], b"")}
                        for meta in ss.ca_uploaded_meta
                    ]
                    pid = save_project(
                        org          = ss.ca_org,
                        period       = ss.ca_period,
                        files_data   = files_data,
                        calc_context = ss.ca_calc_context,
                        summary      = ss.ca_summary,
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
    tab_risks, tab_registry = st.tabs([
        "Риски и комплектность",
        "Реестр заявок",
    ])

    # =========================================================================
    # Вкладка 1: Риски + Резюме
    # =========================================================================
    with tab_risks:
        diag = _rag_diagnose()
        if diag:
            if "недоступен" in diag or "Не удалось" in diag:
                st.error(diag)
            else:
                st.caption(diag)

        if ss.ca_risks:
            _render_risks_tab(ss.ca_risks, claim_summary=ss.get("ca_claim_summary", ""))
        else:
            st.info(
                "Загрузите файлы и нажмите «Полный анализ» — "
                "здесь появится резюме заявки и постатейная оценка рисков."
            )

    # =========================================================================
    # Вкладка 2: Реестр
    # =========================================================================
    with tab_registry:
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

        with st.expander(
            f"**{org}** · {period} · "
            f":{status}: · {updated}",
            expanded=False,
        ):
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

            if hc3.button("Удалить", key=f"reg_del_{pid}",
                          help="Удалить из реестра"):
                ss = st.session_state
                ss[f"reg_confirm_del_{pid}"] = True

            if st.session_state.get(f"reg_confirm_del_{pid}"):
                st.warning(f"Удалить **{org} · {period}**? Это действие необратимо.")
                da, db = st.columns(2)
                if da.button("Да, удалить", key=f"reg_del_yes_{pid}",
                             type="primary", use_container_width=True):
                    delete_project(pid)
                    st.session_state.pop(f"reg_confirm_del_{pid}", None)
                    st.success("Удалено.")
                    st.rerun()
                if db.button("← Отмена", key=f"reg_del_no_{pid}",
                             use_container_width=True):
                    st.session_state.pop(f"reg_confirm_del_{pid}", None)
                    st.rerun()

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
                        with open(fpath, "rb") as f_bin:
                            fc2_f.download_button(
                                "Скачать",
                                data=f_bin.read(),
                                file_name=fname,
                                key=f"reg_dl_{pid}_{fname}",
                                use_container_width=True,
                                help="Скачать файл",
                            )

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
            sub1, sub2 = st.tabs(["Резюме", "Риски"])

            with sub1:
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
                if risks:
                    # Пробуем отрендерить через _render_risks_tab (JSON-формат)
                    try:
                        import json as _json
                        _json.loads(risks)  # проверяем что это JSON
                        _render_risks_tab(risks, show_summary=False, key_prefix=f"reg_{pid}")
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
                ss.ca_done         = True
                ss.ca_project_id   = pid
                st.success(f"Загружено: {org} · {period}")
                st.rerun()