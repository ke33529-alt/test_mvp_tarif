# core/claim_analyzer_logic.py
"""
Бизнес-логика Анализатора тарифных заявок
──────────────────────────────────────────────────────────────────────────────
Этот модуль НЕ содержит Streamlit-кода.
UI находится в streamlit_pages/claim_analyzer.py

Экспортируемые функции:
  load_prompts()          — загрузка промптов из config/prompts.json
  summarize_claim()       — Map-Reduce суммаризация текста заявки
  analyze_risks()         — постатейный RAG-анализ рисков
  _render_timeseries_chart() — SVG-график временного ряда (без st.)
  _build_file_summaries() — чтение заголовков документов заявки
  _build_claim_summary_from_heads() — итоговое резюме заявки
  compute_mr_plan()       — план Map-Reduce по объёму текста
  load_mr_config()        — загрузка конфига Map-Reduce
  save_mr_config()        — сохранение конфига Map-Reduce
"""
from __future__ import annotations
import os, io, json, time, re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# Дефолтные промпты
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Справочник сфер регулирования
# ─────────────────────────────────────────────────────────────────────────────
REGULATION_SPHERES: List[Dict] = [
    {"id": "heat",    "label": "Теплоснабжение",               "icon": "🔥"},
    {"id": "water",   "label": "Водоснабжение и водоотведение", "icon": "💧"},
    {"id": "power",   "label": "Электроэнергетика",             "icon": "⚡"},
    {"id": "gas",     "label": "Газоснабжение",                 "icon": "🔵"},
    {"id": "waste",   "label": "Обращение с ТКО",               "icon": "♻️"},
    {"id": "trans",   "label": "Транспорт (перевозки)",         "icon": "🚌"},
    {"id": "other",   "label": "Прочее",                        "icon": "📄"},
]
SPHERE_IDS   = [s["id"]   for s in REGULATION_SPHERES]
SPHERE_LABELS = {s["id"]: f"{s['icon']} {s['label']}" for s in REGULATION_SPHERES}


DEFAULT_PROMPTS: Dict[str, str] = {
    "claim_map_system": (
        "Ты эксперт по тарифному регулированию РФ. "
        "Извлекаешь структурированные данные из фрагментов тарифных заявок. "
        "Отвечаешь строго на русском языке, только по делу."
    ),
    "claim_map_user": (
        "Это часть {i} из {total} тарифной заявки.\n"
        "Извлеки ТОЛЬКО (если есть во фрагменте):\n"
        "• статьи затрат: название, сумма (тыс. руб.), период\n"
        "• приложенные документы: наименование, реквизиты\n"
        "• ссылки на НПА: номер, статья/пункт\n"
        "• организация, период регулирования, вид деятельности\n"
        "Формат: маркированный список. Без вступлений.\n\n"
        "ФРАГМЕНТ:\n{chunk}"
    ),
    "claim_reduce_system": (
        "Ты эксперт по тарифному регулированию РФ. "
        "Составляешь структурированное резюме тарифной заявки. "
        "Все цифры — точно из источника. Отвечаешь на русском."
    ),
    "claim_reduce_user": (
        "Собери единое резюме тарифной заявки (~{target_words} слов).\n\n"
        "Разделы:\n"
        "## Организация и период\n"
        "## Статьи затрат\n"
        "(таблица: Статья | Сумма тыс. руб. | Период)\n"
        "## Приложенные документы\n"
        "## Ссылки на НПА\n"
        "## Пробелы в обосновании\n\n"
        "Устрани дублирование. Все цифры точно.\n\n"
        "ДАННЫЕ ИЗ ЧАСТЕЙ:\n{combined}"
    ),
    "claim_risks_system": (
        "Ты — эксперт-аудитор тарифных заявок в РФ. "
        "Для каждой статьи затрат проверяешь: "
        "1) есть ли обосновывающий документ в заявке; "
        "2) соответствует ли он требованиям НПА; "
        "3) нет ли противоречий. "
        "Отвечай СТРОГО в заданном формате. Только русский язык. "
        "Используй 🔴 нет документа или грубое нарушение НПА, "
        "🟡 документ есть но не полностью соответствует НПА, "
        "🟢 документ есть и соответствует НПА."
    ),
    "claim_risks_user": (
        "Проанализируй тарифную заявку и дай структурированное заключение.\n\n"
        "**Раздел 1. Комплектность документов** (3-5 предложений):\n"
        "Перечисли документы которые присутствуют в заявке. "
        "Укажи каких документов не хватает исходя из статей затрат.\n\n"
        "**Раздел 2. Топ-3 критичных замечания**:\n"
        "Пронумерованный список. Каждое замечание: статья затрат + сумма + "
        "в чём нарушение + что нужно исправить.\n\n"
        "**Раздел 3. Итоговый риск**:\n"
        "ВЫСОКИЙ / СРЕДНИЙ / НИЗКИЙ — и одно предложение обоснования.\n\n"
        "ДАННЫЕ РАСЧЁТНОГО ФАЙЛА:\n{calc_context}\n\n"
        "РЕЗЮМЕ ЗАЯВКИ:\n{summary}"
    ),
    "claim_verdict_system": (
        "Ты — эксперт-аудитор тарифных заявок в РФ. По названию статьи затрат, "
        "её показателям и приведённым фрагментам НПА дай краткий вердикт: каким НПА "
        "(закон/приказ + пункт) регулируется статья. Опирайся ТОЛЬКО на приведённые "
        "фрагменты НПА. Если фрагментов нет — прямо скажи, что норма в базе не найдена, "
        "и НЕ выдумывай ссылки. ЗАВЕРШАЮЩИМ предложением дай рекомендацию по "
        "обоснованию статьи: какое документальное обоснование требуется "
        "(договор/акт/расчёт/справка и т.п.). Формулируй нейтрально, чтобы "
        "рекомендация подходила и для РСО (что приложить к заявке), и для РЭК "
        "(что запросить и проверить). 3–6 предложений, деловой тон, только русский "
        "язык, без эмодзи и воды."
    ),
}

PROMPTS_FILE = os.path.join("config", "prompts.json")


def load_prompts() -> Dict[str, str]:
    if os.path.exists(PROMPTS_FILE):
        try:
            with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_PROMPTS, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_PROMPTS)


# ─────────────────────────────────────────────────────────────────────────────
# LM Studio с автопродолжением
# ─────────────────────────────────────────────────────────────────────────────
def _load_claim_analyzer_cfg():
    import os as _o, json as _j
    try:
        with open(_o.path.join('config', 'claim_analyzer_config.json'),
                  'r', encoding='utf-8') as _f:
            return _j.load(_f)
    except Exception:
        return {}


def _load_lm_config() -> Tuple[str, str]:
    cfg = {}
    path = os.path.join("config", "advisor_config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
    return (
        cfg.get("lm_studio_url", "http://127.0.0.1:1234/v1"),
        cfg.get("default_model",  "qwen/qwen3.5-9b"),
    )


def _is_complete(text: str) -> bool:
    t = text.rstrip()
    return not t or t[-1] in ".!?:»\n" or t.endswith("---") or t.endswith("```")


def _lm_call(system: str, user: str, max_tokens: int = 2000) -> str:
    try:
        from openai import OpenAI
        lm_url, model = _load_lm_config()
        client = OpenAI(base_url=lm_url, api_key="lm-studio", timeout=300.0)

        is_qwen3 = "qwen3" in model.lower()
        extra_body: dict = {}
        if is_qwen3:
            extra_body = {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "cache_prompt": False,
            }

        kwargs = dict(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user",   "content": user}],
            max_tokens=max_tokens,
            temperature=0.2,
        )
        if extra_body:
            kwargs["extra_body"] = extra_body

        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"[Ошибка LM: {e}]"

def _lm_call_full(system: str, user: str,
                  max_tokens: int = 2000,
                  max_continuations: int = 4,
                  status_cb=None) -> str:
    result = _lm_call(system, user, max_tokens)
    if result.startswith("[Ошибка"):
        return result
    for i in range(max_continuations):
        if _is_complete(result):
            break
        if status_cb:
            status_cb(f"↪️ Продолжение {i+1}/{max_continuations}...")
        cont = _lm_call(
            system,
            f"Твой предыдущий ответ оборвался. Продолжи ТОЧНО с места обрыва, "
            f"не повторяя уже написанное.\n\nКонец предыдущего ответа:\n...{result[-800:]}",
            max_tokens,
        )
        if not cont or cont.startswith("[Ошибка"):
            break
        result = result.rstrip() + "\n" + cont.strip()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Конфигурация Map-Reduce (динамическая)
# ─────────────────────────────────────────────────────────────────────────────
MR_CONFIG_FILE = os.path.join("config", "mapreduce_config.json")

# Дефолты — рассчитаны под 25 000 токенов контекста, Qwen 9B, 16 GB VRAM
MR_DEFAULTS: Dict = {
    "context_tokens":       25_000,   # контекст LM Studio (токенов)
    "map_output_tokens":      600,    # токенов на один MAP-ответ
    "max_chunk_tokens":      3_000,   # потолок качества: MAP не видит > этого
    "reduce_overhead_tokens": 1_000,  # системный промпт + инструкция REDUCE
    "reduce_answer_tokens":  4_000,   # токенов под ответ REDUCE
    "chars_per_token":           4,   # симв/токен (русский текст ~4)
    "mid_reduce_group_size":     3,   # сколько MAP-резюме в одном промежуточном REDUCE
}


def load_mr_config() -> Dict:
    if os.path.exists(MR_CONFIG_FILE):
        try:
            with open(MR_CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**MR_DEFAULTS, **json.load(f)}
        except Exception:
            pass
    return dict(MR_DEFAULTS)


def save_mr_config(cfg: Dict) -> None:
    os.makedirs(os.path.dirname(MR_CONFIG_FILE), exist_ok=True)
    with open(MR_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def compute_mr_plan(text_len: int, cfg: Dict) -> Dict:
    """
    По длине текста и конфигу вычисляет параметры разбивки.
    Возвращает словарь с планом + рекомендуемый контекст для LM Studio.
    """
    cpt   = cfg["chars_per_token"]
    ctx   = cfg["context_tokens"]
    mo    = cfg["map_output_tokens"]
    mct   = cfg["max_chunk_tokens"]
    ovhd  = cfg["reduce_overhead_tokens"]
    ra    = cfg["reduce_answer_tokens"]
    grp   = cfg["mid_reduce_group_size"]

    # Бюджет под MAP-резюме в финальном REDUCE
    reduce_budget_tokens = ctx - ovhd - ra
    max_chunks_in_reduce = max(1, reduce_budget_tokens // mo)

    # Идеальный размер чанка (равномерно)
    text_tokens    = text_len // cpt
    ideal_chunk_t  = max(1, text_tokens // max_chunks_in_reduce)

    # Ограничиваем потолком качества MAP
    chunk_tokens   = min(ideal_chunk_t, mct)
    chunk_chars    = chunk_tokens * cpt

    # Сколько чанков реально получится
    actual_chunks  = max(1, -(-text_len // chunk_chars))  # ceil division

    # Режим: сколько уровней REDUCE нужно
    if actual_chunks <= max_chunks_in_reduce:
        mode   = "2-level"   # MAP → REDUCE
        levels = 2
    else:
        mode   = "3-level"   # MAP → MID-REDUCE → REDUCE
        levels = 3

    # Промежуточных REDUCE-блоков (для 3-level)
    mid_blocks = -(-actual_chunks // grp)  # ceil

    # Рекомендуемый контекст для LM Studio:
    # MAP-вызов: chunk_tokens вход + map_output_tokens выход
    map_ctx_needed = chunk_tokens + mo + 200   # +200 на системный промпт MAP
    # REDUCE-вызов: финальный (самый тяжёлый)
    if mode == "2-level":
        reduce_input_t = actual_chunks * mo + ovhd
    else:
        # промежуточный REDUCE читает grp*mo токенов
        mid_input_t    = grp * mo + ovhd
        reduce_input_t = mid_blocks * mo + ovhd
    reduce_ctx_needed  = max(reduce_input_t, map_ctx_needed) + ra

    recommended_ctx = max(map_ctx_needed, reduce_ctx_needed)
    # Округляем вверх до ближайшей тысячи
    recommended_ctx = (recommended_ctx + 999) // 1000 * 1000

    # Примерное время (35 сек на MAP-вызов при Qwen 9B)
    secs_per_map   = 35
    secs_map       = actual_chunks * secs_per_map
    secs_reduce    = (mid_blocks * 60 + 300) if mode == "3-level" else 300
    total_secs     = secs_map + secs_reduce

    return {
        "text_len":          text_len,
        "text_tokens":       text_tokens,
        "chunk_chars":       chunk_chars,
        "chunk_tokens":      chunk_tokens,
        "actual_chunks":     actual_chunks,
        "max_chunks_reduce": max_chunks_in_reduce,
        "mode":              mode,
        "levels":            levels,
        "mid_blocks":        mid_blocks if mode == "3-level" else 0,
        "recommended_ctx":   recommended_ctx,
        "est_minutes":       round(total_secs / 60, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Суммаризатор Map-Reduce (динамический)
# ─────────────────────────────────────────────────────────────────────────────
def _split_chunks_dynamic(text: str, chunk_chars: int, overlap_chars: int = 0) -> List[str]:
    """Нарезка текста на чанки заданного размера с мягким выравниванием по границам."""
    if len(text) <= chunk_chars:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_chars
        if end >= len(text):
            tail = text[start:].strip()
            if tail:
                chunks.append(tail)
            break
        min_pos   = start + chunk_chars * 3 // 4
        split_end = end
        for sep in ('\n\n', '. ', ' '):
            pos = text.rfind(sep, min_pos, end)
            if pos != -1:
                split_end = pos + len(sep)
                break
        chunk = text[start:split_end].strip()
        if chunk:
            chunks.append(chunk)
        overlap = max(overlap_chars, chunk_chars // 20)  # минимум 5% перекрытия
        start = max(start + chunk_chars // 2, split_end - overlap)
    return chunks


def summarize_claim(text: str, target_words: int = 2000,
                    progress_cb=None) -> str:
    prompts = load_prompts()
    cfg     = load_mr_config()
    plan    = compute_mr_plan(len(text), cfg)

    cpt            = cfg["chars_per_token"]
    map_out_tokens = cfg["map_output_tokens"]
    overlap_chars  = cfg["max_chunk_tokens"] * cpt // 10  # 10% перекрытие

    # ── Режим: текст маленький — прямой REDUCE ────────────────────────────────
    direct_thresh = cfg["max_chunk_tokens"] * cpt * 2
    if len(text) <= direct_thresh:
        if progress_cb:
            progress_cb(0.1, "Текст небольшой — прямое резюме...")
        result = _lm_call_full(
            prompts["claim_reduce_system"],
            prompts["claim_reduce_user"].format(target_words=target_words, combined=text),
            max_tokens=int(target_words * 2),
            status_cb=lambda m: progress_cb(0.8, m) if progress_cb else None,
        )
        if progress_cb:
            progress_cb(1.0, "Готово")
        return result

    chunk_chars = plan["chunk_chars"]
    chunks      = _split_chunks_dynamic(text, chunk_chars, overlap_chars)
    total       = len(chunks)
    t0          = time.perf_counter()
    times: List[float] = []
    minis: List[str]   = []

    mode_label = "3-уровневый" if plan["mode"] == "3-level" else "2-уровневый"
    if progress_cb:
        progress_cb(0.01, f"Режим: {mode_label} · чанков: {total} · "
                          f"~{chunk_chars//1000}К симв/чанк")

    # ── MAP-фаза ──────────────────────────────────────────────────────────────
    for i, chunk in enumerate(chunks, 1):
        elapsed = int(time.perf_counter() - t0)
        eta     = f"~{int(sum(times)/len(times)*(total-i+1))}с" if times else "..."
        if progress_cb:
            progress_cb(
                0.02 + (i - 1) / total * 0.55,
                f"MAP {i}/{total} · чанк {chunk_chars//1000}К симв · {elapsed}с · {eta}"
            )
        mini = _lm_call(
            prompts["claim_map_system"],
            prompts["claim_map_user"].format(i=i, total=total, chunk=chunk),
            max_tokens=map_out_tokens,
        )
        times.append(time.perf_counter() - t0 - sum(times))
        minis.append(f"=== Часть {i}/{total} ===\n{mini}")

    # ── REDUCE-фаза ───────────────────────────────────────────────────────────
    if plan["mode"] == "2-level":
        # Прямой REDUCE
        if progress_cb:
            progress_cb(0.58, f"REDUCE — синтез {total} частей...")
        result = _lm_call_full(
            prompts["claim_reduce_system"],
            prompts["claim_reduce_user"].format(
                target_words=target_words, combined="\n\n".join(minis)
            ),
            max_tokens=int(target_words * 2),
            status_cb=lambda m: progress_cb(0.85, m) if progress_cb else None,
        )

    else:
        # 3-level: промежуточный REDUCE по группам, потом финальный
        grp      = cfg["mid_reduce_group_size"]
        mid_minis: List[str] = []
        groups   = [minis[i:i+grp] for i in range(0, len(minis), grp)]
        n_groups = len(groups)

        for gi, group in enumerate(groups, 1):
            if progress_cb:
                progress_cb(
                    0.58 + (gi - 1) / n_groups * 0.25,
                    f"MID-REDUCE {gi}/{n_groups} · группа {grp} частей..."
                )
            mid = _lm_call(
                prompts["claim_reduce_system"],
                prompts["claim_reduce_user"].format(
                    target_words=600,
                    combined="\n\n".join(group)
                ),
                max_tokens=700,
            )
            mid_minis.append(f"=== Блок {gi}/{n_groups} ===\n{mid}")

        if progress_cb:
            progress_cb(0.84, f"REDUCE — финальный синтез {n_groups} блоков...")
        result = _lm_call_full(
            prompts["claim_reduce_system"],
            prompts["claim_reduce_user"].format(
                target_words=target_words, combined="\n\n".join(mid_minis)
            ),
            max_tokens=int(target_words * 2),
            status_cb=lambda m: progress_cb(0.93, m) if progress_cb else None,
        )

    if progress_cb:
        t_total = time.perf_counter() - t0
        progress_cb(1.0, f"Готово за {int(t_total//60)}м {int(t_total%60)}с · "
                         f"режим: {mode_label} · чанков: {total}")
    return result


_rag_status: Dict = {"ok": None, "error": "", "last_query": "", "chunks": 0}

# Словарь расширений: короткое ключевое слово → поисковая фраза
# Покрывает типичные статьи затрат в тарифных заявках РФ
_ARTICLE_QUERY_MAP = {
    "аренд":          "аренда имущества тариф обоснование",
    "лизинг":         "лизинг имущество тариф включение",
    "амортизац":      "амортизация основных средств тариф",
    "износ":          "амортизация износ основных средств тариф",
    "ремонт":         "ремонт техническое обслуживание основных средств тариф",
    "оплата труда":   "оплата труда персонал тариф нормативы",
    "фот":            "оплата труда фонд тариф нормативы",
    "зарплат":        "заработная плата тариф нормативы численность",
    "страховы":       "страховые взносы отчисления тариф",
    "топлив":         "топливо энергетические ресурсы тариф",
    "газ":            "природный газ тариф расходы",
    "электроэнерг":   "электроэнергия технологические нужды тариф",
    "тепловая энерг": "тепловая энергия тариф расходы покупка",
    "водоснабж":      "водоснабжение тариф расходы",
    "материал":       "материалы химреагенты тариф расходы",
    "химреагент":     "химические реагенты тариф обоснование",
    "программн":      "программное обеспечение информационные технологии тариф",
    "связь":          "услуги связи тариф административные расходы",
    "почтов":         "почтовые расходы административные тариф",
    "командиров":     "командировочные расходы тариф обоснование",
    "обучени":        "обучение повышение квалификации тариф",
    "охран":          "охрана безопасность тариф расходы",
    "страховани":     "страхование имущества тариф включение",
    "налог":          "налоги сборы тариф неподконтрольные расходы",
    "расходы на управл": "управленческие расходы АУП тариф нормативы",
    "общехозяйств":   "общехозяйственные расходы тариф нормативы",
    "хозяйствен":     "хозяйственные расходы тариф административные",
    "прочие расход":  "прочие расходы тариф обоснование состав",
    "иные расход":    "иные расходы тариф состав обоснование",
    "нвв":            "необходимая валовая выручка НВВ расчёт",
    "передач":        "передача тепловой энергии тариф неподконтрольные",
    "транспорт":      "транспортные расходы тариф обоснование",
    "гсм":            "горюче-смазочные материалы тариф обоснование",
    "горюч":          "горюче-смазочные материалы тариф обоснование",
    "смазочн":        "горюче-смазочные материалы тариф обоснование",
    "дизельн":        "горюче-смазочные материалы дизельное топливо тариф",
    "бензин":         "горюче-смазочные материалы тариф обоснование",
    "нефтепрод":      "горюче-смазочные материалы тариф обоснование",
    "масло моторн":   "горюче-смазочные материалы тариф обоснование",
    "топлив":         "топливо энергетические ресурсы тариф",
    "спецодежд":      "спецодежда средства защиты тариф расходы",
    "сиз":            "средства индивидуальной защиты спецодежда тариф",
    "вода питьев":    "водоснабжение питьевая вода тариф расходы",
    "водоотвед":      "водоотведение канализация тариф расходы",
    "утилизац":       "утилизация отходов тариф расходы",
    "вывоз":          "вывоз мусора отходы тариф расходы",
    "хвс":            "холодное водоснабжение тариф расходы",
    "гвс":            "горячее водоснабжение тариф расходы",
    "теплоноситель":  "теплоноситель тариф расходы покупка",
    "инвестиц":       "инвестиционная программа тариф капвложения",
    "капитальн":      "капитальные вложения инвестиции тариф",
    "концессион":     "концессионное соглашение тариф расходы",
    "субаренд":       "субаренда аренда имущества тариф",
    "лицензи":        "лицензирование разрешения тариф расходы",
    "сертификац":     "сертификация лицензирование тариф",
    "метрологи":      "метрология поверка приборов тариф",
    "поверк":         "поверка приборов учёта тариф",
}


def _make_rag_query(article_name: str) -> str:
    """
    Поисковый запрос для RAG = само название статьи затрат.
    Раньше название подменялось обобщённой фразой из _ARTICLE_QUERY_MAP,
    что уводило поиск от конкретики. Теперь ищем ровно по названию —
    как Советчик ищет по сырому пользовательскому запросу.
    """
    return article_name.strip()


def _rag_search(query: str, top_k: int = 10,
                spheres: Optional[List[str]] = None,
                doc_filter: Optional[List[str]] = None) -> List[Dict]:
    """
    Поиск по нормативной базе БЕЗ расширения соседями.
    debug_search_candidates явно не подтягивает соседей — возвращает
    чистый текст чанка в поле "doc" (~1750 симв вместо 35 000).

    spheres: список сфер для фильтрации (None = все сферы).
    10 чанков × 600 симв = 6000 симв чистого релевантного текста в промпте.
    """
    global _rag_status
    _rag_status["last_query"] = query

    try:
        from core.advisor import debug_search_candidates
    except ImportError as e:
        _rag_status["ok"]    = False
        _rag_status["error"] = f"Импорт core.advisor: {e}"
        return []

    try:
        res    = debug_search_candidates(query, top_k=top_k,
                                         spheres=spheres or None,
                                         filenames=doc_filter or None)
        err    = res.get("error")
        if err:
            _rag_status["ok"]    = False
            _rag_status["error"] = err
            print(f"[RAG_CLAIM] ERROR: {err}")
            return []

        # post_rerank = топ-K без соседей
        # если реранкер упал — возвращает pre_rerank[:K]
        candidates = res.get("post_rerank") or res.get("pre_rerank", [])
        chunks = []
        for c in candidates:
            doc  = (c.get("doc") or "").strip()
            meta = c.get("meta") or {}
            if doc:
                chunks.append({
                    "doc":  doc,
                    "file": meta.get("filename", "НПА"),
                    "page": meta.get("page", ""),
                    "meta": meta,
                })

        _rag_status["ok"]     = True
        _rag_status["chunks"] = len(chunks)
        _rag_status["error"]  = ""
        preview = [c["doc"][:50].replace("\n"," ") for c in chunks[:2]]
        print(f"[RAG_CLAIM] {query!r} → {len(chunks)} чанков (без соседей) | {preview}")
        return chunks

    except Exception as e:
        _rag_status["ok"]    = False
        _rag_status["error"] = str(e)
        print(f"[RAG_CLAIM] EXCEPTION: {e}")
        return []


def _rag_diagnose() -> str:
    """Строка диагностики для UI."""
    s = _rag_status
    if s["ok"] is None:
        return ""
    if s["ok"]:
        return f"RAG работает — последний запрос вернул {s['chunks']} чанков (без соседей)"
    return f"RAG недоступен: {s['error']}"


_CHUNK_MAX_CHARS = 1200  # лимит на один чанк (raw чанк ~1750 симв)

def _format_chunks_for_prompt(chunks: List[Dict], max_chars: int = 6500) -> str:
    """
    Форматирует чанки НПА для промпта.
    Поле "doc" — чистый текст без соседей.
    Каждый чанк обрезается до _CHUNK_MAX_CHARS, общий объём ограничен max_chars.
    """
    if not chunks:
        return "Релевантные нормы НПА не найдены в базе знаний."
    lines = []
    total = 0
    for i, ch in enumerate(chunks, 1):
        text   = str(ch.get("doc", ch.get("snippet", ""))).strip()
        source = ch.get("file", "НПА")
        page   = ch.get("page", "")
        ref    = source + (f", стр. {page}" if page else "")
        text_cut = text[:_CHUNK_MAX_CHARS] + ("…" if len(text) > _CHUNK_MAX_CHARS else "")
        line     = f"[{i}] {ref}:\n{text_cut}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n\n".join(lines)


def _has_nonzero_value(amounts: str) -> bool:
    """True если есть хоть одно ненулевое значение (после = или :, не годы)."""
    nums = re.findall(r"[=:]\s*([\d]+(?:[.,]\d+)?)", amounts)
    return any(float(n.replace(",", ".")) != 0.0 for n in nums if n)


def _extract_articles_from_context(calc_context: str) -> List[Dict]:
    """
    Извлекает список статей затрат из контекста calc_parser.
    Статьи, у которых все значения нулевые, пропускаются.

    Поддерживает два формата:

    Полный (to_llm_context):
        # Лист
        ★ Название статьи
          2025 (Принято): 12 345 тыс.руб.

    Компактный (to_llm_context_compact):
        Название статьи: 2025/При=12345, 2026/Пре=13000
    """
    articles = []
    seen = set()
    lines = calc_context.splitlines()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Полный формат: строка начинается с ★
        if stripped.startswith("★"):
            name = stripped.lstrip("★").strip()
            amounts_parts = []
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if not next_line or next_line.startswith("★") or next_line.startswith("#"):
                    break
                amounts_parts.append(next_line)
                j += 1
            amounts = " | ".join(amounts_parts)
            if name and name not in seen and len(name) > 3:
                if _has_nonzero_value(amounts):
                    seen.add(name)
                    articles.append({"name": name, "amounts": amounts})
                else:
                    print(f"[EXTRACT] Пропускаю нулевую статью: {name[:50]}")
            i = j
            continue

        # Компактный формат: "  Название: год/тип=значение, ..."
        if stripped and ":" in stripped and "=" in stripped and not stripped.startswith("#"):
            parts = stripped.split(":", 1)
            name  = parts[0].strip()
            amounts = parts[1].strip() if len(parts) > 1 else ""
            if name and name not in seen and len(name) > 3:
                if _has_nonzero_value(amounts):
                    seen.add(name)
                    articles.append({"name": name, "amounts": amounts})
                else:
                    print(f"[EXTRACT] Пропускаю нулевую статью: {name[:50]}")

        i += 1

    return articles



# =============================================================================
# Инвентаризация обосновывающих документов (Шаг 2.5)
# =============================================================================

def _extract_text_from_file(uf_bytes: bytes, uf_name: str) -> str:
    """
    Извлекает текст из одного файла (PDF, DOCX, DOC).
    Возвращает пустую строку при ошибке.
    """
    print(f"[INVENTORY] Читаю: {uf_name} ({len(uf_bytes):,} байт)")
    try:
        # Пробуем оба пути импорта — зависит от контекста запуска
        try:
            from streamlit_pages.doc_scanner import extract_text as _et
        except ImportError:
            from doc_scanner import extract_text as _et
        pages = _et(uf_bytes, uf_name)
        text = "\n".join(p["text"] for p in pages).strip()
        print(f"[INVENTORY] Текст: {len(text):,} симв, {len(pages)} стр")

        # Если 0 символов и PDF — скан, пробуем _extract_pdf напрямую (с OCR)
        if not text and uf_name.lower().endswith(".pdf"):
            print(f"[INVENTORY] Скан, пробую OCR через _extract_pdf: {uf_name}")
            try:
                try:
                    from streamlit_pages.doc_scanner import _extract_pdf as _epdf
                except ImportError:
                    from doc_scanner import _extract_pdf as _epdf
                ocr_pages = _epdf(uf_bytes, uf_name)
                ocr_text = "\n".join(p["text"] for p in ocr_pages).strip()
                print(f"[INVENTORY] OCR результат: {len(ocr_text):,} симв")
                if ocr_text:
                    return ocr_text
            except Exception as e_ocr:
                print(f"[INVENTORY] OCR ошибка: {type(e_ocr).__name__}: {e_ocr}")
        return text
    except Exception as e:
        print(f"[INVENTORY] Ошибка: {type(e).__name__}: {e}")
    try:
        return uf_bytes.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _inventory_single_file(file_name: str, file_text: str) -> List[Dict]:
    """
    Один LLM-вызов: извлекает список документов из текста одного файла.

    Возвращает список словарей:
        {
            "name":     str,   # название документа
            "details":  str,   # реквизиты (номер, дата)
            "articles": str,   # к каким статьям затрат относится
            "source":   str,   # имя файла-источника
        }

    Возвращает пустой список если документов не найдено или LLM вернул мусор.
    """
    if not file_text or len(file_text.strip()) < 50:
        return []

    # Системный промпт можно переопределить через prompts.json
    prompts = load_prompts()
    system = prompts.get(
        "claim_doc_inventory_system",
        (
            "Ты аудитор тарифных заявок РФ. "
            "Из текста документа извлекаешь список обосновывающих документов. "
            "Отвечай ТОЛЬКО валидным JSON-массивом без пояснений, "
            "без markdown-блоков и без лишнего текста."
        ),
    )

    # Берём первые 5000 символов файла — этого достаточно для перечня
    text_fragment = file_text[:5000]

    user = (
        "Из приведённого текста выпиши ВСЕ упомянутые обосновывающие документы: "
        "договоры, акты, сметы, расчёты, ведомости, справки, "
        "штатные расписания, технические задания, протоколы и т.п.\n\n"
        "Для каждого документа укажи:\n"
        "  name     — название документа (как написано в тексте)\n"
        "  details  — реквизиты: номер, дата, стороны (если есть)\n"
        "  articles — к каким статьям затрат или расходам относится (если понятно)\n\n"
        "Если документов нет — верни пустой массив: []\n\n"
        "Формат ответа — ТОЛЬКО JSON-массив:\n"
        '[{"name":"...", "details":"...", "articles":"..."}, ...]\n\n'
        f"ТЕКСТ ДОКУМЕНТА ({file_name}):\n{text_fragment}"
    )

    try:
        raw = _lm_call(system, user, max_tokens=800).strip()
        print(f"[INVENTORY] LLM ({file_name[:30]}): {raw[:200] if raw else '(пусто)'}")

        if not raw:
            print(f"[INVENTORY] Пустой ответ — пропускаем {file_name}")
            return []

        if "```" in raw:
            raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()

        if "[" in raw and "]" in raw:
            raw = raw[raw.index("[") : raw.rindex("]") + 1]

        docs = json.loads(raw)

        if not isinstance(docs, list):
            print(f"[INVENTORY] Не список: {type(docs)}")
            return []

        # Добавляем источник и чистим пустые записи
        result = []
        for d in docs:
            if not isinstance(d, dict):
                continue
            name = str(d.get("name", "")).strip()
            if not name or len(name) < 3:
                continue
            result.append({
                "name":     name,
                "details":  str(d.get("details", "")).strip(),
                "articles": str(d.get("articles", "")).strip(),
                "source":   file_name,
            })
        return result

    except (json.JSONDecodeError, ValueError):
        return []
    except Exception:
        return []


def _build_doc_inventory(
    uploaded_bytes: Dict[str, bytes],
    calc_file_names: List[str],
    progress_cb=None,
) -> List[Dict]:
    """
    Шаг 2.5: обходит все НЕ-расчётные файлы из загруженных документов,
    для каждого вызывает _inventory_single_file() и собирает единый список.

    uploaded_bytes:  {имя_файла: байты}
    calc_file_names: имена файлов отмеченных как расчётная модель (пропускаем)

    Возвращает дедуплицированный список документов.
    """
    inventory: List[Dict] = []
    doc_files = [
        name for name in uploaded_bytes
        if name not in calc_file_names
        and os.path.splitext(name.lower())[1] in (".pdf", ".docx", ".doc", ".txt")
    ]

    if not doc_files:
        return []

    # Инициализируем OCR как в doc_scanner (один раз за сессию)
    try:
        try:
            from streamlit_pages.doc_scanner import _init_ocr
        except ImportError:
            from doc_scanner import _init_ocr
        _init_ocr()
        print("[INVENTORY] OCR инициализирован")
    except Exception as e:
        print(f"[INVENTORY] OCR инициализация пропущена: {e}")

    for i, file_name in enumerate(doc_files, 1):
        if progress_cb:
            progress_cb(i / len(doc_files), f"Инвентаризация: {file_name[:40]}...")

        file_bytes = uploaded_bytes[file_name]
        file_text  = _extract_text_from_file(file_bytes, file_name)

        if not file_text:
            continue

        found = _inventory_single_file(file_name, file_text)
        inventory.extend(found)

    # Дедупликация по имени документа (оставляем первое вхождение)
    seen: set = set()
    unique: List[Dict] = []
    for doc in inventory:
        key = doc["name"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(doc)

    return unique


# Порог косинусного сходства для семантического сопоставления.
# 0.60 — достаточно мягко чтобы поймать «ГСМ» ↔ «дизельное топливо»,
# достаточно строго чтобы не смешивать несвязанные статьи.
_SEMANTIC_MATCH_THRESHOLD = 0.60


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Косинусное сходство двух нормализованных векторов."""
    dot = sum(x * y for x, y in zip(a, b))
    # Векторы уже нормализованы (normalize_embeddings=True) — деление не нужно,
    # но добавляем защиту от нулевых векторов.
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# Пороги уверенности для детерминированного назначения цвета
SIMILARITY_GREEN  = 0.85   # >= 85% → 🟢 принудительно
SIMILARITY_YELLOW = 0.60   # >= 60% → 🟡 принудительно, < 60% → не учитываем

# Минимальный фильтр — только чистые строки-итоги без содержательных статей затрат.
# calc_parser сам отбирает статьи; здесь отсекаем лишь технические агрегаты.
_SKIP_ARTICLE_PATTERNS = re.compile(
    r"^(средневзвешенный тариф$|всего по тарифу$|итого нвв$|итого по тарифу$)",
    re.IGNORECASE,
)


def _should_skip_article(name: str) -> bool:
    """True только для агрегирующих строк-итогов без содержательных данных."""
    return bool(_SKIP_ARTICLE_PATTERNS.match(name.strip()))


def _match_docs_to_article(article_name: str,
                            inventory: List[Dict],
                            top_k: int = 3) -> List[Dict]:
    """
    Сопоставляет статью затрат с документами из инвентаря.

    Стратегия — двухуровневая:
    1. Лексическое совпадение — даёт score=1.0 (точное вхождение ключевых слов)
    2. Семантическое сходство через эмбеддинги — батчевое, один вызов GPU

    Возвращает топ-k документов выше SIMILARITY_YELLOW, отсортированных
    по убыванию score. Каждый документ содержит поле _similarity (0.0-1.0).
    """
    if not inventory:
        return []

    stop_words = {
        "расходы", "затраты", "оплата", "труда", "итого",
        "прочие", "всего", "общие", "иные", "прочих",
    }
    name_lower = article_name.lower()
    keywords = [
        w for w in re.split(r"\W+", name_lower)
        if len(w) > 3 and w not in stop_words
    ]

    # Назначаем всем документам базовый score через лексику
    scored: List[Dict] = []
    for doc in inventory:
        doc_text = (doc.get("name", "") + " " + doc.get("articles", "")).lower()
        d = dict(doc)
        if keywords and any(kw in doc_text for kw in keywords):
            d["_similarity"] = 1.0   # лексическое совпадение = максимум
        else:
            d["_similarity"] = 0.0   # будет уточнено через эмбеддинги
        scored.append(d)

    # Документы без лексического совпадения — проверяем через эмбеддинги
    unscored = [d for d in scored if d["_similarity"] == 0.0]

    if unscored:
        try:
            from core.advisor import get_st_model
            model = get_st_model()
            if model is not None:
                article_vec = model.encode(
                    [f"query: {article_name}"],
                    normalize_embeddings=True,
                )[0].tolist()

                doc_texts = [
                    f"passage: {d.get('name', '')} {d.get('details', '')}".strip()
                    for d in unscored
                ]
                doc_vecs = model.encode(
                    doc_texts,
                    normalize_embeddings=True,
                    batch_size=32,
                    show_progress_bar=False,
                )
                for doc, doc_vec in zip(unscored, doc_vecs):
                    sim = _cosine_similarity(article_vec, doc_vec.tolist())
                    doc["_similarity"] = round(sim, 3)
                    if sim >= SIMILARITY_YELLOW:
                        print(f"[SEMANTIC MATCH] {article_name[:30]} ↔ "
                              f"{doc.get('name','')[:30]} sim={sim:.3f}")
        except Exception as e:
            print(f"[SEMANTIC MATCH] Ошибка: {e}")

    # Возвращаем топ-k выше порога SIMILARITY_YELLOW, по убыванию score
    above_threshold = [d for d in scored if d["_similarity"] >= SIMILARITY_YELLOW]
    above_threshold.sort(key=lambda x: x["_similarity"], reverse=True)
    return above_threshold[:top_k]

def _init_ocr_for_analysis():
    """Инициализирует OCR как в doc_scanner — автоматически, один раз за сессию."""
    try:
        try:
            from streamlit_pages.doc_scanner import _init_ocr
        except ImportError:
            from doc_scanner import _init_ocr
        _init_ocr()
        print("[ANALYSIS] OCR инициализирован")
    except Exception as e:
        print(f"[ANALYSIS] OCR инициализация: {e}")


def _extract_file_text(file_bytes: bytes, file_name: str) -> str:
    """
    Извлекает текст из файла через doc_scanner.extract_text.
    OCR запускается автоматически для сканов (< 50 симв/страница).
    """
    try:
        try:
            from streamlit_pages.doc_scanner import extract_text
        except ImportError:
            from doc_scanner import extract_text
        pages = extract_text(file_bytes, file_name)
        text  = "\n".join(p["text"] for p in pages).strip()
        ocr_count = sum(1 for p in pages if p.get("method") == "ocr")
        print(f"[ANALYSIS] {file_name}: {len(text):,} симв, {len(pages)} стр"
              + (f", {ocr_count} OCR" if ocr_count else ""))
        return text
    except Exception as e:
        print(f"[ANALYSIS] Ошибка извлечения {file_name}: {e}")
        return ""


# Параметры суммаризации файлов
_FILE_SUMMARY_FINAL_TOKENS = 300   # токенов на ответ одиночного вызова (fallback)


# Параметры батчевой суммаризации файлов
_FILE_HEAD_CHARS     = 1_500  # символов с начала каждого файла (OCR)
_FILE_BATCH_SIZE     = 10     # не используется, оставлен для совместимости

# ── Предохранители контекста (лимит LLM: 20 000 токенов) ──────────────────
# Бюджет REDUCE-вызова резюме (~18 800 токенов на вход):
#   calc_block    : 1 500 симв  (~  375 токенов)
#   arts_combined : 6 групп × 1 000 симв = 6 000 симв  (~1 500 токенов)
#   docs_block    : 100 файлов × 500 симв = 50 000 симв (~12 500 токенов)
#   системный+инструкция+ответ: ~4 500 токенов
#   Итого: ~18 875 токенов ✓
#
# MAP-вызов: 50 статей × 120 симв = 6 000 симв (~1 750 токенов) ✓
_MAX_DOC_FILES       = 100   # максимум файлов для OCR-чтения заголовков
_MAX_DOC_FOR_SUMMARY  = 100  # максимум файлов в блоке документов резюме
_MAX_DOC_HEAD_CHARS   = 1_000 # символов из каждого файла (заголовок)
_DOC_MAP_GROUP        = 10   # файлов в одном MAP-вызове документов
                              # 10 × 1000 симв ≈ 2 500 токенов на вызов ✓
                              # 100 файлов → 10 MAP → 10 блоков × ~800 симв ≈ 2 000 токенов в REDUCE ✓
_MAX_ARTICLES        = 300   # максимум статей затрат в анализе
_SUMMARY_MAP_GROUP   = 50    # статей в одном MAP-вызове
_MAX_MAP_GROUPS      = 6     # ceil(300/50) — не более 6 групп


def _extract_file_head(file_bytes: bytes, file_name: str,
                       max_chars: int = _FILE_HEAD_CHARS) -> str:
    """
    Извлекает первые max_chars символов. OCR ограничен 2 страницами.
    Текст обеих страниц склеивается перед обрезкой.
    """
    try:
        try:
            from streamlit_pages.doc_scanner import extract_text as _et_h
        except ImportError:
            from doc_scanner import extract_text as _et_h
        pages = _et_h(file_bytes, file_name, max_pages=2)
        text = "\n".join(p.get("text", "") for p in pages).strip()
        ocr_pages = sum(1 for p in pages if p.get("method") == "ocr")
        if ocr_pages:
            print(f"[ANALYSIS] {file_name[:30]}: OCR {ocr_pages} стр. из {len(pages)}")
    except Exception:
        text = file_bytes.decode("utf-8", errors="ignore").strip()
    head = text[:max_chars]
    print(f"[ANALYSIS] Голова {file_name[:30]}: {len(head)} симв")
    return head


def _summarize_file_batch(file_heads: Dict[str, str]) -> Dict[str, str]:
    """
    Суммаризирует батч файлов за ОДИН LLM-вызов.
    file_heads: {имя_файла: первые 1500 симв}
    Возвращает {имя_файла: краткое_резюме}.

    Бюджет токенов при батче 10 файлов × 1500 симв:
      10 × 1500 / 4 ≈ 3750 токенов входа — комфортно для 20k контекста.
    Ответ: 10 файлов × 2 предложения ≈ 500 токенов.
    """
    if not file_heads:
        return {}

    prompts = load_prompts()
    system = prompts.get(
        "claim_file_summary_system",
        "Ты аудитор тарифных заявок РФ. Кратко описываешь документы. Только русский язык."
    )

    # Формируем блок с нумерованными файлами
    blocks = []
    file_list = list(file_heads.items())
    for idx, (fname, head) in enumerate(file_list, 1):
        blocks.append(f"=== Документ {idx}: {fname} ===\n{head}")

    user = (
        f"Для каждого из {len(file_list)} документов напиши краткое резюме "
        "(1-2 предложения): что за документ, стороны, предмет, суммы/даты, "
        "к каким статьям затрат относится.\n\n"
        "Формат ответа — строго для каждого документа:\n"
        "Документ 1: <резюме>\n"
        "Документ 2: <резюме>\n"
        "...и так далее.\n\n"
        + "\n\n".join(blocks)
    )

    max_tokens = len(file_list) * 80  # ~80 токенов на файл
    raw = _lm_call(system, user, max_tokens=max(max_tokens, 400)).strip()
    print(f"[ANALYSIS] Батч-самари {len(file_list)} файлов: {len(raw)} симв ответа")

    # Парсим ответ: "Документ N: текст"
    results: Dict[str, str] = {}
    pat = re.compile(r"Документ\s+(\d+)\s*:\s*(.+?)(?=Документ\s+\d+\s*:|$)",
                     re.DOTALL | re.IGNORECASE)
    for m in pat.finditer(raw):
        idx = int(m.group(1)) - 1
        text = m.group(2).strip().replace("\n", " ")
        if 0 <= idx < len(file_list):
            fname = file_list[idx][0]
            results[fname] = text
            print(f"[ANALYSIS]   [{idx+1}] {fname[:30]}: {text[:60]}")

    # Fallback: если парсинг не сработал — весь ответ на первый файл
    if not results and file_list:
        results[file_list[0][0]] = raw[:200]

    return results


def _summarize_file(file_name: str, file_text: str) -> str:
    """Одиночная суммаризация (fallback, используется из _build_claim_summary_from_heads)."""
    if not file_text or len(file_text.strip()) < 50:
        return ""
    prompts = load_prompts()
    system  = prompts.get(
        "claim_file_summary_system",
        "Ты аудитор тарифных заявок. Кратко описываешь документы. Только русский язык."
    )
    head = file_text[:_FILE_HEAD_CHARS].strip()
    user = (
        f"Документ: {file_name}\n\n"
        "Напиши краткое резюме (1-2 предложения): "
        "что за документ, стороны, предмет, суммы/даты, к каким расходам относится.\n\n"
        f"НАЧАЛО ТЕКСТА:\n{head}"
    )
    return _lm_call(system, user, max_tokens=_FILE_SUMMARY_FINAL_TOKENS).strip()


def _build_file_summaries(
    uploaded_bytes: Dict[str, bytes],
    calc_file_names: List[str],
    progress_cb=None,
) -> Dict[str, str]:
    """
    Читает заголовки документальных файлов заявки (первые _FILE_HEAD_CHARS символов).
    Без LLM — возвращает {имя_файла: заголовок_текста}.
    """
    doc_files = [
        name for name in uploaded_bytes
        if name not in calc_file_names
        and os.path.splitext(name.lower())[1] in (".pdf", ".docx", ".doc", ".txt")
    ]
    if len(doc_files) > _MAX_DOC_FILES:
        doc_files = doc_files[:_MAX_DOC_FILES]
    print(f"[ANALYSIS] Файлов для чтения заголовков: {len(doc_files)}")
    if not doc_files:
        return {}

    _init_ocr_for_analysis()

    heads: Dict[str, str] = {}
    n = len(doc_files)
    for i, file_name in enumerate(doc_files, 1):
        short = os.path.splitext(file_name)[0][:40]
        if progress_cb:
            progress_cb(i / n, f"Читаю {short}...")
        head = _extract_file_head(uploaded_bytes[file_name], file_name)
        if head:
            heads[file_name] = head
        else:
            print(f"[ANALYSIS] Пустой текст: {file_name}")

    print(f"[ANALYSIS] Прочитано: {len(heads)}")
    return heads


def _precompute_file_vecs(file_summaries: Dict[str, str]) -> Dict[str, list]:
    """
    Вычисляет эмбеддинги всех файлов заявки ОДИН РАЗ перед LLM-фазой.
    Без кэша при 100 статьях × 100 файлов = 10 000 encode-вызовов.
    С кэшем = 1 батч.
    """
    if not file_summaries:
        return {}
    try:
        from core.advisor import get_st_model
        model = get_st_model()
        if model is None:
            return {}
        fnames = list(file_summaries.keys())
        texts  = [f"passage: {fn} {file_summaries[fn]}" for fn in fnames]
        t0 = __import__("time").perf_counter()
        vecs = model.encode(texts, normalize_embeddings=True,
                            batch_size=32, show_progress_bar=False)
        elapsed = __import__("time").perf_counter() - t0
        cache = {fn: vec.tolist() for fn, vec in zip(fnames, vecs)}
        print(f"[FILE_VECS] Предвычислено {len(cache)} векторов файлов за {elapsed:.2f}с")
        return cache
    except Exception as e:
        print(f"[FILE_VECS] Ошибка: {e}")
        return {}


def _match_files_to_article(
    article_name: str,
    file_summaries: Dict[str, str],
    top_k: int = 3,
    file_vecs_cache: Optional[Dict[str, list]] = None,
) -> List[Dict]:
    """
    Находит топ-k файлов релевантных статье затрат.
    Трёхуровневая: лексика → эмбеддинги (кэш) → реранкер DiTy.
    """
    if not file_summaries:
        return []

    stop_words = {
        "расходы", "затраты", "оплата", "труда", "итого",
        "прочие", "всего", "общие", "иные", "прочих",
    }
    name_lower = article_name.lower()
    keywords = [
        w for w in re.split(r"\W+", name_lower)
        if len(w) > 3 and w not in stop_words
    ]

    scored: List[Dict] = []
    for fname, summary in file_summaries.items():
        text = (fname + " " + summary).lower()
        sim  = 1.0 if (keywords and any(kw in text for kw in keywords)) else 0.0
        scored.append({"file_name": fname, "summary": summary, "_similarity": sim})

    unscored = [d for d in scored if d["_similarity"] == 0.0]
    if unscored:
        try:
            from core.advisor import get_st_model
            model = get_st_model()
            if model is not None:
                article_vec = model.encode(
                    [f"query: {article_name}"],
                    normalize_embeddings=True,
                )[0].tolist()
                for doc in unscored:
                    fn = doc["file_name"]
                    if file_vecs_cache and fn in file_vecs_cache:
                        doc_vec = file_vecs_cache[fn]
                    else:
                        doc_vec = model.encode(
                            [f"passage: {fn} {doc['summary']}"],
                            normalize_embeddings=True,
                            show_progress_bar=False,
                        )[0].tolist()
                    sim = _cosine_similarity(article_vec, doc_vec)
                    doc["_similarity"] = round(sim, 3)
                    if sim >= SIMILARITY_YELLOW:
                        print(f"[SEMANTIC] {article_name[:25]} ↔ "
                              f"{fn[:25]} sim={sim:.3f}")
        except Exception as e:
            print(f"[SEMANTIC] Ошибка: {e}")

    above = [d for d in scored if d["_similarity"] >= SIMILARITY_YELLOW]
    above.sort(key=lambda x: x["_similarity"], reverse=True)

    if len(above) > 1:
        try:
            from core.advisor import get_reranker
            reranker = get_reranker()
            if reranker is not None:
                candidates = [
                    {"doc": f"{d['file_name']} {d['summary'][:300]}", **d}
                    for d in above
                ]
                reranked = reranker.rerank(article_name, candidates, top_n=top_k)
                result = []
                for r in reranked:
                    orig = next(
                        (d for d in above if d["file_name"] == r.get("file_name")),
                        None,
                    )
                    if orig:
                        result.append(orig)
                if result:
                    print(f"[RERANK_FILES] {article_name[:30]}: "
                          f"{[r['file_name'][:20] for r in result]}")
                    return result[:top_k]
        except Exception as e:
            print(f"[RERANK_FILES] Ошибка реранкера: {e}")

    return above[:top_k]


def _parse_article_result(text: str) -> Dict:
    """Парсит структурированный ответ модели по одной статье."""
    result = {
        "risk": "red", "risk_emoji": "🔴",
        "document": "не указан",
        "basis": "", "recommendation": "",
    }
    for line in text.splitlines():
        s = line.strip()
        up = s.upper()
        if up.startswith("РИСК:"):
            val = s[5:].strip()
            if "🔴" in val:
                result["risk"] = "red";    result["risk_emoji"] = "🔴"
            elif "🟡" in val:
                result["risk"] = "yellow"; result["risk_emoji"] = "🟡"
            elif "🟢" in val:
                result["risk"] = "green";  result["risk_emoji"] = "🟢"
        elif up.startswith("ДОКУМЕНТ:"):
            result["document"] = s[9:].strip() or "не указан"
        elif up.startswith("ОСНОВАНИЕ:"):
            result["basis"] = s[10:].strip()
        elif up.startswith("РЕКОМЕНДАЦИЯ:"):
            result["recommendation"] = s[13:].strip()
    # Fallback 1: ищем эмодзи в любом месте текста
    if result["risk"] == "unknown":
        if "🔴" in text:
            result["risk"] = "red";    result["risk_emoji"] = "🔴"
        elif "🟡" in text:
            result["risk"] = "yellow"; result["risk_emoji"] = "🟡"
        elif "🟢" in text:
            result["risk"] = "green";  result["risk_emoji"] = "🟢"
    # Fallback 2: если вообще ничего — ставим красный (нет документа = риск)
    if result["risk"] == "unknown":
        result["risk"]       = "red"
        result["risk_emoji"] = "🔴"
    # basis fallback — весь текст если не распарсился
    if not result["basis"]:
        result["basis"] = text.strip()[:300]
    return result





# =============================================================================
# Расчёт роста статей затрат (детерминированный цвет)
# =============================================================================

def _parse_amounts_timeseries(amounts_str: str) -> List[Tuple[int, str, float]]:
    """
    Разбирает строку amounts в список (year, label, value).
    Поддерживает форматы:
      - "2024 (Факт): 12 345 тыс.руб. | 2025 (Принято): 13 000 тыс.руб."
      - "2024/Фак=12345, 2025/При=13000"
    Возвращает отсортированный по году список.
    """
    results: List[Tuple[int, str, float]] = []

    # Формат полный: "2025 (Принято): 12 345 тыс.руб."
    pat_full = re.compile(
        r"(\d{4})\s*\(([^)]+)\)\s*:\s*([\d\s]+(?:[.,]\d+)?)\s*тыс",
        re.IGNORECASE,
    )
    for m in pat_full.finditer(amounts_str):
        yr  = int(m.group(1))
        lbl = m.group(2).strip()
        val_str = m.group(3).replace(" ", "").replace(",", ".")
        try:
            results.append((yr, lbl, float(val_str)))
        except ValueError:
            pass

    # Формат компактный: "2025/При=13000"
    if not results:
        pat_compact = re.compile(r"(\d{4})/([^=,\s]+)=([\d.]+)")
        for m in pat_compact.finditer(amounts_str):
            yr  = int(m.group(1))
            lbl = m.group(2).strip()
            try:
                results.append((yr, lbl, float(m.group(3))))
            except ValueError:
                pass

    results.sort(key=lambda x: x[0])
    return results


def _classify_label_priority(label: str) -> int:
    """Приоритет типа значения: факт > предвар. > план > принято > прочее."""
    lbl = label.lower()
    if any(k in lbl for k in ("факт", "отч")):
        return 0
    if any(k in lbl for k in ("предв", "пред")):
        return 1
    if any(k in lbl for k in ("план", "план")):
        return 2
    if any(k in lbl for k in ("прин", "при")):
        return 3
    return 4


def _get_growth_color(amounts_str: str, reg_year: int,
                      target_pct: float, risk_pct: float
                      ) -> Tuple[str, str, Optional[float], Optional[float]]:
    """
    Определяет цвет риска по росту статьи затрат.

    reg_year    — регулируемый год (год, по которому ищем значение)
    target_pct  — целевой индекс роста, % (например 5.0 → 1.05)
    risk_pct    — порог риска, % поверх целевого (например 10.0 → 1.15)

    Возвращает (color, reason, base_val, reg_val).
    """
    ts = _parse_amounts_timeseries(amounts_str)
    if not ts:
        return "yellow", "нет данных для расчёта роста", None, None

    # Значение регулируемого года
    reg_vals = [(yr, lbl, v) for yr, lbl, v in ts if yr == reg_year]
    if not reg_vals:
        return "yellow", f"год {reg_year} не найден в данных", None, None
    reg_vals.sort(key=lambda x: _classify_label_priority(x[1]))
    _, reg_lbl, reg_val = reg_vals[0]

    # Базовое значение: лучший вариант предыдущего года
    prev_vals = [(yr, lbl, v) for yr, lbl, v in ts if yr < reg_year]
    if not prev_vals:
        return "yellow", "нет предыдущего периода для сравнения", None, reg_val
    prev_vals.sort(key=lambda x: (-x[0], _classify_label_priority(x[1])))
    _, base_lbl, base_val = prev_vals[0]

    if base_val <= 0:
        return "yellow", "базовое значение равно нулю", base_val, reg_val

    growth = (reg_val - base_val) / base_val * 100  # в процентах
    target = target_pct
    risk   = target_pct + risk_pct

    if growth > risk:
        reason = (f"рост {growth:+.1f}% превышает критический порог {risk:.1f}% "
                  f"(целевой {target:.1f}% + рисковый {risk_pct:.1f}%)")
        return "red", reason, base_val, reg_val
    elif growth > target:
        reason = (f"рост {growth:+.1f}% превышает целевой индекс {target:.1f}%")
        return "yellow", reason, base_val, reg_val
    else:
        reason = (f"рост {growth:+.1f}% в пределах целевого индекса {target:.1f}%")
        return "green", reason, base_val, reg_val


# =============================================================================
# Резюме заявки на основе первых 1000 симв каждого файла
# =============================================================================



def _build_claim_summary_from_heads(
    uploaded_bytes: Dict[str, bytes],
    calc_file_names: List[str],
    calc_context: str,
    article_results: List[Dict],
    org: str,
    period: str,
    progress_cb=None,
    file_summaries: Optional[Dict[str, str]] = None,
) -> str:
    """
    Строит итоговое резюме заявки в стиле регулятора РФ.

    Использует уже готовые file_summaries из _build_file_summaries
    вместо повторного OCR-чтения файлов.

    MAP-REDUCE по статьям затрат:
      - Группы по _SUMMARY_MAP_GROUP статей → промежуточное резюме (MAP)
      - Все промежуточные резюме + документы + calc → финальное резюме (REDUCE)
    """
    prompts = load_prompts()

    system = prompts.get(
        "claim_summary_system",
        (
            "Ты — старший эксперт-аудитор тарифного регулятора РФ (ФАС/РЭК). "
            "Составляешь официальное заключение о тарифной заявке. "
            "Стиль — деловой, лаконичный. Только русский язык."
        )
    )

    # ── Блок документов: MAP по группам файлов ──────────────────────────────
    _all_heads: Dict[str, str] = {}
    if file_summaries:
        _all_heads = {
            fname: head[:_MAX_DOC_HEAD_CHARS]
            for fname, head in list(file_summaries.items())[:_MAX_DOC_FOR_SUMMARY]
            if head
        }
    else:
        # Fallback: читаем первые 2 страницы если заголовков нет
        for fname, fbytes in uploaded_bytes.items():
            if fname in calc_file_names:
                continue
            if os.path.splitext(fname.lower())[1] not in (".pdf", ".docx", ".doc", ".txt"):
                continue
            try:
                try:
                    from streamlit_pages.doc_scanner import extract_text as _et2
                except ImportError:
                    from doc_scanner import extract_text as _et2
                pages = _et2(fbytes, fname, max_pages=2)
                raw = "\n".join(p["text"] for p in pages).strip()
            except Exception:
                raw = fbytes.decode("utf-8", errors="ignore").strip()
            head = raw[:_MAX_DOC_HEAD_CHARS].strip()
            if head:
                _all_heads[fname] = head
            if len(_all_heads) >= _MAX_DOC_FOR_SUMMARY:
                break

    # MAP по документам — группы по _DOC_MAP_GROUP файлов
    _doc_items  = list(_all_heads.items())
    _doc_blocks: List[str] = []

    if not _doc_items:
        _doc_blocks = ["(документы не приложены)"]
    elif len(_doc_items) <= _DOC_MAP_GROUP:
        # Мало файлов — MAP не нужен
        _doc_blocks = ["\n\n".join(f"[{fn}]:\n{h}" for fn, h in _doc_items)]
    else:
        # MAP: группируем файлы
        _doc_groups = [
            _doc_items[i: i + _DOC_MAP_GROUP]
            for i in range(0, len(_doc_items), _DOC_MAP_GROUP)
        ]
        _n_dg = len(_doc_groups)
        print(f"[SUMMARY] Документы MAP: {len(_doc_items)} файлов → {_n_dg} групп")
        for _gi, _grp in enumerate(_doc_groups):
            if progress_cb:
                progress_cb(
                    0.05 + _gi / _n_dg * 0.20,
                    f"Резюме документов: группа {_gi+1}/{_n_dg}..."
                )
            _grp_text = "\n\n".join(f"[{fn}]:\n{h}" for fn, h in _grp)
            _map_user = (
                f"Группа {_gi+1}/{_n_dg} документов тарифной заявки "
                f"({org or '—'}, {period or '—'}).\n\n"
                "Кратко (2-4 предложения) опиши состав этих документов: "
                "типы документов, стороны, предметы, суммы/даты если есть.\n\n"
                f"ДОКУМЕНТЫ:\n{_grp_text}"
            )
            _blk = _lm_call(system, _map_user, max_tokens=200).strip()
            if _blk:
                _doc_blocks.append(f"=== Документы группа {_gi+1} ===\n{_blk}")
            print(f"[SUMMARY] Документы MAP {_gi+1}/{_n_dg}: {len(_blk)} симв")

    docs_block = "\n\n".join(_doc_blocks)[:4000]
    calc_block = calc_context[:1500] if calc_context else "(расчётный файл не загружен)"

    # ── MAP: группы по _SUMMARY_MAP_GROUP статей → промежуточные блоки ─────────
    art_items = [
        f"• {a['name']} [{a.get('risk','?')}]: {a.get('article_summary','')[:120]}"
        for a in article_results
        if a.get("article_summary") or a.get("name")
    ]

    map_blocks: List[str] = []

    if not art_items:
        # Нет статей — пропускаем MAP-фазу
        map_blocks = ["(статьи затрат не распознаны)"]

    elif len(art_items) <= _SUMMARY_MAP_GROUP:
        # Мало статей — MAP не нужен, используем напрямую
        map_blocks = ["\n".join(art_items)]

    else:
        # Много статей — MAP по группам
        groups = [
            art_items[i: i + _SUMMARY_MAP_GROUP]
            for i in range(0, len(art_items), _SUMMARY_MAP_GROUP)
        ]
        if len(groups) > _MAX_MAP_GROUPS:
            print(f"[SUMMARY] Групп {len(groups)} > лимита {_MAX_MAP_GROUPS}, берём топ по размеру")
            # Берём первые N групп (начало расчётного файла обычно важнее)
            groups = groups[:_MAX_MAP_GROUPS]
        n_groups = len(groups)
        print(f"[SUMMARY] MAP: {len(art_items)} статей → {n_groups} групп")

        for gi, group in enumerate(groups):
            if progress_cb:
                progress_cb(
                    0.1 + gi / n_groups * 0.6,
                    f"Резюме: группа статей {gi + 1}/{n_groups}..."
                )
            group_text = "\n".join(group)
            map_user = (
                f"Группа {gi + 1}/{n_groups} статей затрат тарифной заявки "
                f"({org or '—'}, {period or '—'}).\n\n"
                "Кратко (3-5 предложений) опиши структуру и динамику этих статей: "
                "общий объём, наиболее значимые статьи, характер роста.\n\n"
                f"СТАТЬИ:\n{group_text}"
            )
            block = _lm_call(system, map_user, max_tokens=250).strip()
            if block:
                map_blocks.append(f"=== Группа {gi + 1} ===\n{block}")
            print(f"[SUMMARY] MAP {gi + 1}/{n_groups}: {len(block)} симв")

    # ── REDUCE: финальное резюме ─────────────────────────────────────────────
    if progress_cb:
        progress_cb(0.30, "Формирую итоговое резюме заявки...")

    arts_combined = "\n\n".join(map_blocks)[:4000]

    reduce_user = (
        f"Организация: {org or 'не указана'}\n"
        f"Период регулирования: {period or 'не указан'}\n\n"
        "Составь РЕЗЮМЕ ТАРИФНОЙ ЗАЯВКИ (200–300 слов) в стиле заключения регулятора.\n\n"
        "Структура:\n"
        "**1. Суть заявки** — что организация просит утвердить, вид деятельности, период.\n"
        "**2. Состав заявки** — какие документы приложены, общая укомплектованность.\n"
        "**3. Структура затрат** — ключевые статьи и их динамика (цифры если есть).\n"
        "**4. Предварительная оценка** — уровень обоснованности, уязвимые позиции.\n\n"
        f"РАСЧЁТНЫЙ ФАЙЛ:\n{calc_block}\n\n"
        f"АНАЛИЗ СТАТЕЙ ЗАТРАТ:\n{arts_combined}\n\n"
        f"ДОКУМЕНТЫ ЗАЯВКИ:\n{docs_block}"
    )

    result = _lm_call(system, reduce_user, max_tokens=700).strip()
    if progress_cb:
        progress_cb(1.0, "Резюме готово")
    print(f"[SUMMARY] Итог: {len(result)} симв, {len(art_items)} статей, "
          f"{len(map_blocks)} MAP-блоков")
    return result





def _strip_leading_emoji(s: str) -> str:
    """Убирает эмодзи и мусорные символы с начала строки."""
    return re.sub(r"^[🌀-🿿☀-⟿\s🟴🔴🟡🟢⚪]+", "", s).strip()


def _parse_article_result_v2(text: str) -> Dict:
    """Парсит ответ LLM по одной статье (новый формат: ДИНАМИКА/ОСНОВАНИЕ/РЕКОМЕНДАЦИЯ)."""
    result = {"dynamics": "", "basis": "", "recommendation": ""}
    for line in text.splitlines():
        s = line.strip()
        up = s.upper()
        if up.startswith("ДИНАМИКА:"):
            result["dynamics"] = _strip_leading_emoji(s[9:])
        elif up.startswith("ОСНОВАНИЕ:"):
            result["basis"] = _strip_leading_emoji(s[10:])
        elif up.startswith("РЕКОМЕНДАЦИЯ:"):
            result["recommendation"] = _strip_leading_emoji(s[13:])
    # Fallback: если модель не использовала метки — берём весь текст как динамику
    if not result["dynamics"] and not result["basis"]:
        result["dynamics"] = _strip_leading_emoji(text.strip()[:250])
    return result





# Фирменный цвет платформы
_BRAND_COLOR       = "#1a6b7c"
_BRAND_COLOR_LIGHT = "#2a8fa5"
_COLOR_RED         = "#e05252"
_COLOR_YELLOW      = "#d4a017"
_COLOR_GREEN       = "#3a9e6e"


def _render_timeseries_chart(ts, reg_year, risk, target_pct=5.0):
    """
    SVG-график временного ряда статьи затрат.
    ts: список (year, label, value) отсортированный по году.
    Строит отдельную линию для каждого уникального label.
    """
    if not ts or len(ts) < 2:
        return ""

    from collections import OrderedDict

    series: dict = OrderedDict()
    for yr, lbl, v in ts:
        series.setdefault(lbl, []).append((yr, v))
    for lbl in series:
        series[lbl].sort(key=lambda x: x[0])
    if not series:
        return ""

    line_series  = {k: v for k, v in series.items() if len(v) >= 2}
    point_series = {k: v for k, v in series.items() if len(v) == 1}
    if not line_series:
        line_series  = series
        point_series = {}

    max_lbl_len = max(len(lbl) for lbl in series) if series else 6
    LEG_W    = 26 + max_lbl_len * 7
    W        = 420
    H        = 155
    PAD_L    = 56
    PAD_R    = 12 + LEG_W
    PAD_T    = 18
    PAD_B    = 34
    CW       = W - PAD_L - PAD_R
    CH       = H - PAD_T - PAD_B

    all_vals  = [v for pts in series.values() for _, v in pts]
    vmin, vmax = min(all_vals), max(all_vals)
    vrange    = vmax - vmin or 1

    all_years = sorted({yr for pts in series.values() for yr, _ in pts})
    n_years   = len(all_years)
    year_idx  = {yr: i for i, yr in enumerate(all_years)}

    def px(yr):
        i = year_idx.get(yr, 0)
        return PAD_L + (i / (n_years - 1)) * CW if n_years > 1 else PAD_L + CW / 2

    def py(v):
        return PAD_T + CH - ((v - vmin) / vrange) * CH

    def fmt_val(v):
        if abs(v) >= 1_000_000:
            return f"{v/1_000_000:.1f}М"
        if abs(v) >= 10_000:
            return f"{v/1000:.0f}К"
        return f"{v:,.0f}"

    base_color = {"red": _COLOR_RED, "yellow": _COLOR_YELLOW,
                  "green": _COLOR_GREEN}.get(risk, _BRAND_COLOR)
    _PAL = [base_color, "#2a8fa5", "#6cb4c2", "#9ecdd6", "#aaaaaa"]
    lbl_colors = {lbl: _PAL[i % len(_PAL)] for i, lbl in enumerate(series)}

    uid = abs(hash(str(ts) + str(reg_year))) % 999999

    target_svg = ""
    first_pts  = next(iter(series.values()))
    if len(first_pts) >= 2:
        base_v = first_pts[0][1]
        coords = []
        for yr in all_years:
            i     = year_idx[yr]
            t_val = base_v * ((1 + target_pct / 100) ** i)
            y_c   = max(PAD_T, min(PAD_T + CH, py(
                min(max(t_val, vmin - vrange * 0.05), vmax + vrange * 0.05)
            )))
            coords.append(f"{px(yr):.1f},{y_c:.1f}")
        target_svg = (
            '<polyline points="' + " ".join(coords) + '" '
            'fill="none" stroke="' + _COLOR_YELLOW + '" '
            'stroke-width="1.2" stroke-dasharray="5,3" opacity="0.5"/>'
        )

    step = max(1, n_years // 14)
    x_labels = ""
    for i, yr in enumerate(all_years):
        if i % step == 0 or yr == reg_year:
            x_labels += (
                f'<text x="{px(yr):.1f}" y="{H - PAD_B + 14}" '
                f'text-anchor="middle" font-size="9" fill="#999">{yr}</text>'
            )

    y_labels = (
        f'<text x="{PAD_L - 4}" y="{PAD_T + CH}" '
        f'text-anchor="end" font-size="10" fill="#999">{fmt_val(vmin)}</text>'
        f'<text x="{PAD_L - 4}" y="{PAD_T + 8}" '
        f'text-anchor="end" font-size="10" fill="#999">{fmt_val(vmax)}</text>'
    )

    lines_svg = grad_svg = circles_svg = legend_svg = ""

    for s_idx, (lbl, pts) in enumerate(line_series.items()):
        color      = lbl_colors[lbl]
        is_primary = s_idx == 0
        poly_pts   = " ".join(f"{px(yr):.1f},{py(v):.1f}" for yr, v in pts)
        sw         = "2.5" if is_primary else "1.8"
        dash       = "" if is_primary else ' stroke-dasharray="6,3"'

        lines_svg += (
            f'<polyline points="{poly_pts}" fill="none" stroke="{color}" '
            f'stroke-width="{sw}" stroke-linejoin="round" '
            f'stroke-linecap="round"{dash} opacity="0.9"/>'
        )

        if is_primary and len(pts) >= 2:
            area_p = (
                f"{px(pts[0][0]):.1f},{PAD_T + CH} "
                + poly_pts
                + f" {px(pts[-1][0]):.1f},{PAD_T + CH}"
            )
            grad_svg += (
                f'<defs><linearGradient id="ag{uid}" x1="0" y1="0" x2="0" y2="1">'
                f'<stop offset="0%" stop-color="{color}" stop-opacity="0.18"/>'
                f'<stop offset="100%" stop-color="{color}" stop-opacity="0.01"/>'
                f'</linearGradient></defs>'
                f'<polygon points="{area_p}" fill="url(#ag{uid})"/>'
            )

        for yr, v in pts:
            cx, cy = px(yr), py(v)
            is_reg = (yr == reg_year)
            r      = 5 if is_reg else 3
            cf     = color if is_reg else "#fff"
            cs     = "#fff" if is_reg else color
            tip    = f"{yr} ({lbl}): {v:,.0f} тыс.руб."
            circles_svg += (
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" '
                f'fill="{cf}" stroke="{cs}" stroke-width={"2" if is_reg else "1.5"}>'
                f'<title>{tip}</title></circle>'
            )
            if is_reg and is_primary:
                ty = cy - 9 if cy - 9 >= PAD_T + 6 else cy + 14
                circles_svg += (
                    f'<text x="{cx:.1f}" y="{ty:.1f}" '
                    f'text-anchor="middle" font-size="9" font-weight="bold" '
                    f'fill="{color}">{fmt_val(v)}</text>'
                )

    for lbl, pts in point_series.items():
        color = lbl_colors[lbl]
        yr, v = pts[0]
        cx, cy = px(yr), py(v)
        is_reg = (yr == reg_year)
        r = 5 if is_reg else 4
        tip = f"{yr} ({lbl}): {v:,.0f} тыс.руб."
        d = r
        circles_svg += (
            f'<polygon points="{cx:.1f},{cy-d:.1f} {cx+d:.1f},{cy:.1f} '
            f'{cx:.1f},{cy+d:.1f} {cx-d:.1f},{cy:.1f}" '
            f'fill="{color}" stroke="#fff" stroke-width="1.5" opacity="0.9">'
            f'<title>{tip}</title></polygon>'
        )
        ty = cy - 9 if cy - 9 >= PAD_T + 6 else cy + 14
        circles_svg += (
            f'<text x="{cx:.1f}" y="{ty:.1f}" '
            f'text-anchor="middle" font-size="9" fill="{color}">{fmt_val(v)}</text>'
        )

    leg_x = W - LEG_W + 4
    for i, (lbl, _) in enumerate(series.items()):
        color  = lbl_colors[lbl]
        ly     = PAD_T + i * 18
        is_pt  = lbl in point_series
        if is_pt:
            mx, my = leg_x + 9, ly + 4
            legend_svg += (
                f'<polygon points="{mx},{my-4} {mx+4},{my} {mx},{my+4} {mx-4},{my}" '
                f'fill="{color}" opacity="0.9"/>'
            )
        else:
            dash_l = "" if i == 0 else ' stroke-dasharray="5,3"'
            legend_svg += (
                f'<line x1="{leg_x}" y1="{ly+4}" x2="{leg_x+18}" y2="{ly+4}" '
                f'stroke="{color}" stroke-width="2"{dash_l}/>'
            )
        legend_svg += (
            f'<text x="{leg_x + 22}" y="{ly + 8}" '
            f'font-size="10" fill="#888">{lbl}</text>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {W} {H}" '
        f'style="font-family:sans-serif;display:block;width:60%;min-height:180px;">'
        f'{grad_svg}'
        f'<line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{PAD_T+CH}" stroke="#e0e0e0" stroke-width="1"/>'
        f'<line x1="{PAD_L}" y1="{PAD_T+CH}" x2="{PAD_L+CW}" y2="{PAD_T+CH}" stroke="#e0e0e0" stroke-width="1"/>'
        f'{target_svg}{lines_svg}{circles_svg}{x_labels}{y_labels}{legend_svg}'
        f'</svg>'
    )


def analyze_risks(calc_context: str, summary: str, progress_cb=None,
                  spheres: Optional[List[str]] = None,
                  file_summaries: Optional[Dict[str, str]] = None,
                  target_pct: float = 5.0,
                  risk_pct: float = 10.0,
                  reg_year: int = 0,
                  approved_articles: Optional[List[Dict]] = None) -> str:
    """
    Постатейный RAG-анализ рисков.
    spheres:           фильтр RAG по сфере (None = все).
    file_summaries:    {имя_файла: самари} из _build_file_summaries().
    approved_articles: готовый список статей после апрува (если передан —
                       парсинг calc_context не выполняется).
    """
    CHUNK_LIMIT_PER_ARTICLE = 8000
    MAX_TOKENS_PER_ARTICLE  = 500  # краткий вердикт ~500 ток., как в Советчике
    NPA_CHUNKS_TO_LLM       = 20     # сколько топ-чанков подаём в промпт LLM
    NPA_PROMPT_CHARS        = 24000  # 20 × 1200 = 24000 симв ≈ ~12000 ток. в худшем случае

    prompts  = load_prompts()
    # Используем уже одобренный список если передан, иначе парсим контекст
    if approved_articles is not None:
        articles = approved_articles
    else:
        articles = _extract_articles_from_context(calc_context) if calc_context else []
    # Предохранитель: не более _MAX_ARTICLES статей
    if len(articles) > _MAX_ARTICLES:
        print(f"[ANALYSIS] Статей {len(articles)} > лимита {_MAX_ARTICLES}, берём первые {_MAX_ARTICLES}")
        articles = articles[:_MAX_ARTICLES]

    # ── Fallback: нет статей из расчётного файла ─────────────────────────────
    if not articles:
        if progress_cb:
            progress_cb(0.1, "Расчётный файл не распознан, анализирую по резюме...")
        result = _lm_call_full(
            prompts["claim_risks_system"],
            prompts["claim_risks_user"].format(
                calc_context=calc_context[:4000] if calc_context else "Нет данных",
                summary=summary[:3000] if summary else "Нет резюме",
            ),
            max_tokens=1200,
            status_cb=lambda m: progress_cb(0.7, m) if progress_cb else None,
        )
        if progress_cb:
            progress_cb(1.0, "Готово")
        return result

    total         = len(articles)
    article_results: List[Dict] = []
    rag_available = True

    # Определяем регулируемый год автоматически если не задан явно
    _reg_year = reg_year
    if _reg_year == 0 and articles:
        # берём максимальный год из amounts всех статей
        all_years = []
        for _a in articles:
            ts = _parse_amounts_timeseries(_a.get("amounts", ""))
            all_years.extend(yr for yr, _, _ in ts)
        _reg_year = max(all_years) if all_years else datetime.now().year

    # ── Диапазоны прогресс-бара (монотонно возрастают) ───────────────────────
    # [0.00..0.05] — инициализация реранкера
    # [0.05..0.50] — RAG-фаза (все статьи)
    # [0.50..0.88] — LLM-фаза (все статьи)
    # [0.88..1.00] — агрегация итогового отчёта
    P_INIT_START = 0.00
    P_INIT_END   = 0.05
    P_RAG_START  = 0.05
    P_RAG_END    = 0.50
    P_LLM_START  = 0.50
    P_LLM_END    = 0.88

    # Принудительно включаем русский реранкер DiTy для анализатора
    try:
        import json as _json
        _ss_path = os.path.join("config", "search_settings.json")
        _ss = {}
        if os.path.exists(_ss_path):
            with open(_ss_path, "r", encoding="utf-8") as _f:
                _ss = _json.load(_f)
        _ss_changed = (
            _ss.get("reranker_model")   != "DiTy/cross-encoder-russian-msmarco" or
            _ss.get("reranker_enabled") != True
        )
        if _ss_changed:
            _ss["reranker_model"]   = "DiTy/cross-encoder-russian-msmarco"
            _ss["reranker_enabled"] = True
            with open(_ss_path, "w", encoding="utf-8") as _f:
                _json.dump(_ss, _f, ensure_ascii=False, indent=2)
            from core.advisor import invalidate_reranker
            invalidate_reranker()
    except Exception:
        pass

    # Маппинг sphere_id → ключевое слово для _sphere_match
    _SPHERE_ID_KW = {
        'heat':  'Тепло',
        'water': 'Водо',
        'power': 'Электр',
        'gas':   'Газ',
        'waste': 'ТКО',
        'trans': 'Транспорт',
        'other': 'Иные',
    }
    def _resolve_sph(raw):
        return [_SPHERE_ID_KW.get(s, s) for s in (raw or [])]

    _adm_cfg  = _load_claim_analyzer_cfg()
    _adm_docs = _adm_cfg.get('rag_docs', []) or None
    if not spheres:
        _sp = _adm_cfg.get('rag_spheres', [])
        if _sp:
            spheres = _resolve_sph(_sp)
            print(f'[RAG_CLAIM] Сферы из кнф: {spheres}')
    elif spheres:
        spheres = _resolve_sph(spheres)
        print(f'[RAG_CLAIM] Сферы резолв.: {spheres}')
    if _adm_docs:
        print(f'[RAG_CLAIM] doc_filter: {len(_adm_docs)} ф.')

    # Прогрев RAG / инициализация реранкера
    if progress_cb:
        sphere_label = f" · сферы: {', '.join(spheres)}" if spheres else " · все сферы"
        progress_cb(P_INIT_START, f"Инициализация RAG (русский реранкер DiTy){sphere_label}...")
    _rag_search("тарифное регулирование НВВ", top_k=1, spheres=spheres or None)
    if progress_cb:
        progress_cb(P_INIT_END, f"Реранкер готов. Статей к анализу: {total}")

    # ── Фаза 1: RAG для всех статей (реранкер в памяти) ─────────────────────
    # Каждые RAG_FLUSH_EVERY статей выгружаем реранкер чтобы не накапливать VRAM.
    # После выгрузки следующий _rag_search загрузит его заново автоматически.
    RAG_FLUSH_EVERY = 50

    def _flush_reranker():
        try:
            from core.advisor import invalidate_reranker
            invalidate_reranker()
            # BM25 не сбрасываем — он в RAM, пересборка занимает ~40 сек
        except Exception:
            pass
        import gc; gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # Прогресс только в начале RAG-фазы — не вызываем внутри цикла
    # чтобы избежать Streamlit rerun который перезагружает реранкер
    if progress_cb:
        progress_cb(P_RAG_START, f"RAG-поиск для {total} статей...")

    all_chunks: List[List[Dict]] = []
    for rag_done, art in enumerate(articles):
        chunks = _rag_search(_make_rag_query(art["name"]), top_k=20,
                             spheres=spheres or None,
                             doc_filter=_adm_docs)
        all_chunks.append(chunks)
        if not chunks:
            rag_available = False

        # Периодически освобождаем реранкер чтобы не держать VRAM весь цикл
        if (rag_done + 1) % RAG_FLUSH_EVERY == 0:
            print(f"[RAG] Flush реранкера после {rag_done + 1} статей")
            _flush_reranker()

    # ── Финальная выгрузка реранкера перед LLM-фазой ─────────────────────────
    _flush_reranker()

    if progress_cb:
        progress_cb(P_RAG_END, f"RAG завершён. Запускаю LLM для {total} статей...")

    # ── Фаза 2: LLM для всех статей ──────────────────────────────────────────
    # Предвычисляем эмбеддинги файлов один раз для всей LLM-фазы
    # Без кэша: N_статей × N_файлов encode-вызовов. С кэшем: 1 батч.
    _file_vecs_cache = _precompute_file_vecs(file_summaries or {})

    llm_done = 0
    for (art, chunks) in zip(articles, all_chunks):
        name    = art["name"]
        amounts = art["amounts"]
        llm_frac = llm_done / total
        if progress_cb:
            progress_cb(
                P_LLM_START + llm_frac * (P_LLM_END - P_LLM_START),
                f"LLM {llm_done + 1}/{total}: {name[:40]}..."
            )

        # ── Фильтр: пропускаем тарифные показатели ───────────────────
        if _should_skip_article(name):
            print(f"[SKIP] Пропускаем тарифный показатель: {name[:50]}")
            llm_done += 1
            continue

        # Топ-N реранкованных чанков → в промпт. Остальное отбрасываем как шум.
        llm_chunks = chunks[:NPA_CHUNKS_TO_LLM]
        npa_context = _format_chunks_for_prompt(
            llm_chunks, max_chars=NPA_PROMPT_CHARS
        )
        has_npa = bool(chunks)

        # ── Сопоставление с файлами заявки (только имена + первые 1000 симв) ──
        summaries = file_summaries or {}
        matched   = _match_files_to_article(name, summaries, top_k=3, file_vecs_cache=_file_vecs_cache)

        # ── Детерминированный цвет по росту затрат ────────────────────────
        growth_color, growth_reason, base_val, reg_val = _get_growth_color(
            amounts, _reg_year, target_pct, risk_pct
        )
        EMOJI_MAP = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
        final_risk       = growth_color
        final_risk_emoji = EMOJI_MAP[growth_color]

        # ── LLM: краткий вердикт по статье на основе НПА (как в Советчике) ──
        verdict_user = (
            f"Статья затрат: {name}\n"
            f"Значения: {amounts}\n"
            f"Динамика к предыдущему периоду: {growth_reason}\n\n"
            f"Фрагменты НПА (опорные пункты):\n"
            f"{npa_context if has_npa else 'не найдены'}\n\n"
            "Дай краткий вердикт по статье на основе фрагментов НПА выше."
        )

        art_result = _lm_call(
            prompts.get("claim_verdict_system", DEFAULT_PROMPTS["claim_verdict_system"]),
            verdict_user,
            max_tokens=MAX_TOKENS_PER_ARTICLE,
        )

        verdict = _strip_leading_emoji(art_result.strip())

        article_results.append({
            "name":             name,
            "amounts":          amounts,
            "risk":             final_risk,
            "risk_emoji":       final_risk_emoji,
            "growth_reason":    growth_reason,
            "base_val":         base_val,
            "reg_val":          reg_val,
            "verdict":          verdict,
            # Совместимость со старыми потребителями (итоговый отчёт/экспорт)
            "article_summary":  verdict,
            "basis":            "",
            "recommendation":   "",
            "raw":              art_result,
            "has_npa":          has_npa,
            "matched_files":    matched,
        })
        llm_done += 1

    if progress_cb:
        progress_cb(0.88, f"Агрегирую {total} статей...")

    # ── Итоговый отчёт (агрегация через LLM) ─────────────────────────────────
    rag_note = (
        "Часть статей без данных НПА — добавьте НПА в базу знаний."
        if not rag_available else
        "Анализ выполнен с привлечением нормативной базы знаний."
    )

    # Краткая текстовая выжимка для LLM-агрегации
    EMOJI_AGG = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
    items_for_agg = []
    for a in article_results:
        emoji = EMOJI_AGG.get(a.get("risk", "red"), "🔴")
        items_for_agg.append(
            f"{emoji} {a['name']}: {a.get('amounts', '')[:80]}\n"
            f"  Рост: {a.get('growth_reason', '—')}\n"
            f"  Основание: {a.get('basis', '')[:120]}"
        )

    aggregate_prompt = (
        f"По постатейным оценкам составь краткое заключение (без таблиц и HTML).\n\n"
        f"**Раздел 1. Общий риск** (2-3 предложения): уровень риска, "
        f"сколько статей под угрозой, общая сумма под риском.\n\n"
        f"**Раздел 2. Топ-3 замечания** (пронумерованный список):\n"
        f"Каждое: статья + сумма + в чём проблема + что требовать от заявителя.\n\n"
        f"**Раздел 3. Отсутствующие документы** (список).\n\n"
        f"ОЦЕНКИ:\n{''.join(items_for_agg[:5000])}\n\n"
        f"РЕЗЮМЕ:\n{summary[:1500]}"
    )
    summary_result = _lm_call(
        prompts["claim_risks_system"],
        aggregate_prompt,
        max_tokens=500,
    )
    if progress_cb:
        progress_cb(0.94, "Итоговое заключение готово")

    # total = только реально обработанные статьи (без отсеянных _should_skip_article)
    processed = len(article_results)
    output = json.dumps({
        "summary":    summary_result,
        "rag_note":   rag_note,
        "articles":   article_results,
        "reg_year":   _reg_year,
        "target_pct": target_pct,
        "stats": {
            "total":  processed,
            "red":    sum(1 for a in article_results if a["risk"] == "red"),
            "yellow": sum(1 for a in article_results if a["risk"] == "yellow"),
            "green":  sum(1 for a in article_results if a["risk"] == "green"),
        },
    }, ensure_ascii=False)

    if progress_cb:
        progress_cb(1.0, f"Готово — {processed} статей")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Хелперы
# ─────────────────────────────────────────────────────────────────────────────
def _classify_article(name: str, amounts: str) -> str:
    """
    Автоматически определяет тип строки:
      'cost'  — статья затрат (подлежит анализу)
      'agg'   — агрегат / итоговая строка
      'ref'   — справочный показатель (тарифы, объёмы, индексы)
      'zero'  — все значения нулевые
    """
    n = name.lower().strip()
    if any(k in n for k in (
        "итого", "всего", "нвв", "необходимая валовая выручка",
        "средневзвешенный", "итого по тарифу", "всего по тарифу",
    )):
        return "agg"
    if any(k in n for k in (
        "тариф", "руб./", "руб/", "индекс", "объём", "объем",
        "полезный отпуск", "доля товарной", "численность",
        "коэффициент", "норматив", "ставка 1 разряда",
    )):
        return "ref"
    if not _has_nonzero_value(amounts):
        return "zero"
    return "cost"



# Технические паттерны листов
_TECH_SHEET_PATTERNS = re.compile(
    r'^(К_ФАС|Р_ФАС|БТр_|БПр_|ТМ\d|Столбцы|Экон\.|Индексы|Справочники|'
    r'Перечень|Амортизация_ФАС|Реестр потребит)',
    re.I,
)


def _is_tech_sheet(sheet_name: str, df_sheet) -> bool:
    """True если лист технический: паттерн имени ИЛИ >80% статей без единицы."""
    if _TECH_SHEET_PATTERNS.match(sheet_name.strip()):
        return True
    if df_sheet is not None and len(df_sheet) > 0:
        no_unit = (df_sheet["unit"].astype(str).str.strip() == "").sum()
        if no_unit / len(df_sheet) > 0.8:
            return True
    return False


def _extract_articles_from_df(df) -> List[Dict]:
    """
    Строит список статей для апрува напрямую из DataFrame calc_parser.
    Поля: name, amounts, type, checked, sheet, unit, tech_sheet, manual.
    """
    if df is None or df.empty:
        return []

    articles: List[Dict] = []
    seen_key: dict = {}

    for sheet_name, sdf in df.groupby("sheet", sort=False):
        is_tech = _is_tech_sheet(sheet_name, sdf)

        for article_name, adf in sdf.groupby("article", sort=False):
            adf = adf.sort_values("period")
            parts = []
            for _, row in adf.iterrows():
                period = str(row["period"]) if row["period"] else "—"
                pf     = f" ({row['pf']})" if row["pf"] else ""
                val    = row["value"]
                unit   = str(row["unit"]) if row["unit"] else "тыс.руб."
                parts.append(f"{period}{pf}: {val:,.2f} {unit}")
            amounts = " | ".join(parts)

            units = adf["unit"].astype(str).str.strip()
            units = units[units != ""]
            unit_str = units.mode().iloc[0] if len(units) > 0 else ""

            art_type = _classify_article(article_name, amounts)

            key = (sheet_name, article_name)
            if key not in seen_key:
                seen_key[key] = len(articles)
                articles.append({
                    "name":       article_name,
                    "amounts":    amounts,
                    "type":       art_type,
                    "checked":    art_type == "cost",
                    "sheet":      sheet_name,
                    "unit":       unit_str,
                    "tech_sheet": is_tech,
                    "manual":     False,
                })

    # Авто-отбор: cost-статьи с нетехнических листов
    n_cost = sum(1 for a in articles if a["type"] == "cost" and not a["tech_sheet"])
    if n_cost > 0:
        for a in articles:
            a["checked"] = (a["type"] == "cost" and not a["tech_sheet"])
    else:
        for a in articles:
            a["checked"] = a["type"] in ("cost", "zero")

    return articles


def _extract_articles_from_context_unfiltered(calc_context: str) -> List[Dict]:
    """
    Извлекает ВСЕ строки из calc_context без фильтрации.
    Каждой строке назначается автоматический тип через _classify_article.
    Используется для показа таблицы апрува.
    """
    articles = []
    seen = set()
    lines = calc_context.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if stripped.startswith("★"):
            name = stripped.lstrip("★").strip()
            parts = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt or nxt.startswith("★") or nxt.startswith("#"):
                    break
                parts.append(nxt)
                j += 1
            amounts = " | ".join(parts)
            if name and name not in seen and len(name) > 3:
                seen.add(name)
                articles.append({
                    "name":    name,
                    "amounts": amounts,
                    "type":    _classify_article(name, amounts),
                    "checked": True,
                })
            i = j
            continue

        if stripped and ":" in stripped and "=" in stripped and not stripped.startswith("#"):
            parts2 = stripped.split(":", 1)
            name = parts2[0].strip()
            amounts = parts2[1].strip() if len(parts2) > 1 else ""
            if name and name not in seen and len(name) > 3:
                seen.add(name)
                articles.append({
                    "name":    name,
                    "amounts": amounts,
                    "type":    _classify_article(name, amounts),
                    "checked": True,
                })
        i += 1

    n_cost = sum(1 for a in articles if a["type"] == "cost")
    if n_cost > 0:
        for a in articles:
            a["checked"] = a["type"] == "cost"
    else:
        for a in articles:
            a["checked"] = a["type"] in ("cost", "zero")

    return articles


def _save_log(org: str, period: str, summary: str, risks: str):
    try:
        path = os.path.join("data", "feedback", "feedback_log.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        entry = {
            "feedback_type": "claim_analysis",
            "timestamp":     datetime.now().isoformat(),
            "org":           org, "period": period,
            "summary_words": len(summary.split()),
            "risks_words":   len(risks.split()),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _format_size(b: int) -> str:
    if b < 1024:
        return f"{b} Б"
    if b < 1024 * 1024:
        return f"{b/1024:.0f} КБ"
    return f"{b/1024/1024:.1f} МБ"


# ─────────────────────────────────────────────────────────────────────────────
# Главный UI