# streamlit_pages/claim_analyzer.py
"""
Анализатор тарифных заявок
──────────────────────────────────────────────────────────────────────────────
Вкладки:
  1. Риски и комплектность — LLM-анализ рисков по статьям + оценка документов
  2. Резюме заявки        — структурированный текст Map-Reduce
  3. Реестр заявок        — сохранение, поиск, управление статусами

Расчётные Excel → core/calc_parser
Промпты         → config/prompts.json (Админка)
Реестр          → core/claim_registry  (data/claims/)
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
    {"id": "heat",    "label": "Теплоснабжение",          "icon": "🔥"},
    {"id": "water",   "label": "Водоснабжение и водоотведение", "icon": "💧"},
    {"id": "power",   "label": "Электроэнергетика",        "icon": "⚡"},
    {"id": "gas",     "label": "Газоснабжение",            "icon": "🔵"},
    {"id": "waste",   "label": "Обращение с ТКО",          "icon": "♻️"},
    {"id": "trans",   "label": "Транспорт (перевозки)",    "icon": "🚌"},
    {"id": "other",   "label": "Прочее",                   "icon": "📄"},
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
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user",   "content": user}],
            max_tokens=max_tokens,
            temperature=0.2,
        )
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
    Формирует короткий поисковый запрос по названию статьи затрат.
    Проверяет словарь _ARTICLE_QUERY_MAP, иначе использует само название.
    Короткие запросы работают лучше в семантическом поиске.
    """
    name_lower = article_name.lower()
    for key, query in _ARTICLE_QUERY_MAP.items():
        if key in name_lower:
            return query
    # Fallback: берём первые 5 слов названия статьи + "тариф"
    words = article_name.split()[:5]
    return " ".join(words) + " тариф обоснование"


def _rag_search(query: str, top_k: int = 10,
                spheres: Optional[List[str]] = None) -> List[Dict]:
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
        res    = debug_search_candidates(query, top_k=top_k, spheres=spheres or None)
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


_CHUNK_MAX_CHARS = 600  # лимит на один чанк: 10 × 600 = 6000 симв в промпте

def _format_chunks_for_prompt(chunks: List[Dict], max_chars: int = 6500) -> str:
    """
    Форматирует чанки НПА для промпта.
    Поле "doc" — чистый текст без соседей.
    Каждый чанк обрезается до _CHUNK_MAX_CHARS чтобы все 10 чанков влезли.
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


def _extract_articles_from_context(calc_context: str) -> List[Dict]:
    """
    Извлекает список статей затрат из контекста calc_parser.

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
                seen.add(name)
                articles.append({"name": name, "amounts": amounts})
            i = j
            continue

        # Компактный формат: "  Название: год/тип=значение, ..."
        if stripped and ":" in stripped and "=" in stripped and not stripped.startswith("#"):
            parts = stripped.split(":", 1)
            name  = parts[0].strip()
            amounts = parts[1].strip() if len(parts) > 1 else ""
            if name and name not in seen and len(name) > 3:
                seen.add(name)
                articles.append({"name": name, "amounts": amounts})

        i += 1

    return articles[:40]



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

# Строки-маркеры для фильтрации тарифных показателей (не статей затрат)
# Намеренно НЕ включаем слово "тариф" — оно встречается в названиях параметров ФОТ
_SKIP_ARTICLE_PATTERNS = re.compile(
    r"^(нвв|необходимая валовая выручка|средневзвешенный тариф|"
    r"всего по тарифу|итого нвв|итого по тарифу|"
    r"тариф на .+воду|тариф для населения|тариф \(в рамках)",
    re.IGNORECASE,
)


def _should_skip_article(name: str) -> bool:
    """True если статья является тарифным показателем а не статьёй затрат."""
    return bool(_SKIP_ARTICLE_PATTERNS.search(name.strip()))


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


# Параметры MAP-REDUCE для суммаризации файлов
_FILE_SUMMARY_CHUNK_SIZE   = 6_000
_FILE_SUMMARY_MAP_TOKENS   = 100
_FILE_SUMMARY_FINAL_TOKENS = 300
_FILE_SUMMARY_THRESHOLD    = 6_000


def _summarize_file(file_name: str, file_text: str) -> str:
    """
    Суммаризирует один файл через LLM.
    Короткие < 6000 симв: один вызов 300 токенов.
    Длинные: MAP-REDUCE — MAP 100 токенов/чанк, REDUCE 300 токенов.
    """
    if not file_text or len(file_text.strip()) < 50:
        return ""

    prompts = load_prompts()
    system  = prompts.get(
        "claim_file_summary_system",
        "Ты аудитор тарифных заявок. Кратко описываешь документы. Только русский язык."
    )

    if len(file_text) <= _FILE_SUMMARY_THRESHOLD:
        user = (
            f"Документ: {file_name}\n\n"
            "Напиши краткое резюме (2-4 предложения). "
            "Что за документ, стороны, предмет, суммы/даты, к каким расходам относится.\n\n"
            f"ТЕКСТ:\n{file_text}"
        )
        result = _lm_call(system, user, max_tokens=_FILE_SUMMARY_FINAL_TOKENS).strip()
        print(f"[ANALYSIS] Самари {file_name[:30]}: {len(result)} симв (прямой)")
        return result

    chunks = _split_chunks_dynamic(file_text, _FILE_SUMMARY_CHUNK_SIZE)
    total  = len(chunks)
    print(f"[ANALYSIS] MAP-REDUCE {file_name[:30]}: {total} чанков")

    mini_summaries = []
    for i, chunk in enumerate(chunks, 1):
        print(f"[ANALYSIS] MAP {i}/{total}: {file_name[:25]}...")
        map_user = (
            f"Часть {i}/{total} документа «{file_name}».\n"
            "Выдели ключевые факты (1-2 предложения): "
            "что за документ, стороны, суммы, даты, предмет.\n\n"
            f"ТЕКСТ:\n{chunk}"
        )
        mini = _lm_call(system, map_user, max_tokens=_FILE_SUMMARY_MAP_TOKENS).strip()
        if mini:
            mini_summaries.append(mini)

    if not mini_summaries:
        return ""

    combined = "\n".join(f"- {m}" for m in mini_summaries)
    reduce_user = (
        f"Документ: {file_name}\n\n"
        "На основе этих фрагментов составь единое резюме (2-4 предложения).\n\n"
        f"ФРАГМЕНТЫ:\n{combined}"
    )
    result = _lm_call(system, reduce_user, max_tokens=_FILE_SUMMARY_FINAL_TOKENS).strip()
    print(f"[ANALYSIS] Самари {file_name[:30]}: {len(result)} симв (MAP-REDUCE {total} чанков)")
    return result


def _build_file_summaries(
    uploaded_bytes: Dict[str, bytes],
    calc_file_names: List[str],
    progress_cb=None,
) -> Dict[str, str]:
    """
    Суммаризирует все документальные файлы заявки (не Excel).
    Возвращает {имя_файла: краткое_содержание}.
    """
    doc_files = [
        name for name in uploaded_bytes
        if name not in calc_file_names
        and os.path.splitext(name.lower())[1] in (".pdf", ".docx", ".doc", ".txt")
    ]
    print(f"[ANALYSIS] Файлов для суммаризации: {len(doc_files)}")
    if not doc_files:
        return {}

    _init_ocr_for_analysis()

    summaries: Dict[str, str] = {}
    for i, file_name in enumerate(doc_files, 1):
        if progress_cb:
            progress_cb(i / len(doc_files),
                        f"Суммаризация {i}/{len(doc_files)}: {file_name[:35]}...")
        file_text = _extract_file_text(uploaded_bytes[file_name], file_name)
        if not file_text:
            print(f"[ANALYSIS] Пустой текст: {file_name} — пропускаем")
            continue
        summary = _summarize_file(file_name, file_text)
        if summary:
            summaries[file_name] = summary

    print(f"[ANALYSIS] Суммаризировано файлов: {len(summaries)}")
    return summaries


def _match_files_to_article(
    article_name: str,
    file_summaries: Dict[str, str],
    top_k: int = 3,
) -> List[Dict]:
    """
    Находит топ-k файлов релевантных статье затрат.
    Возвращает список {file_name, summary, _similarity}.
    Уровень 1 — лексика, уровень 2 — эмбеддинги.
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
                texts = [f"passage: {d['file_name']} {d['summary']}" for d in unscored]
                vecs  = model.encode(texts, normalize_embeddings=True,
                                     batch_size=32, show_progress_bar=False)
                for doc, vec in zip(unscored, vecs):
                    sim = _cosine_similarity(article_vec, vec.tolist())
                    doc["_similarity"] = round(sim, 3)
                    if sim >= SIMILARITY_YELLOW:
                        print(f"[SEMANTIC] {article_name[:25]} ↔ "
                              f"{doc['file_name'][:25]} sim={sim:.3f}")
        except Exception as e:
            print(f"[SEMANTIC] Ошибка: {e}")

    above = [d for d in scored if d["_similarity"] >= SIMILARITY_YELLOW]
    above.sort(key=lambda x: x["_similarity"], reverse=True)
    return above[:top_k]


def _parse_article_result(text: str) -> Dict:
    """Парсит структурированный ответ модели по одной статье."""
    result = {
        "risk": "unknown", "risk_emoji": "⚪",
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


def _render_risks_tab(risks_json: str):
    """
    Кастомный рендеринг постатейного анализа рисков.
    risks_json — строка JSON от analyze_risks() или старый markdown.
    """
    # Пробуем распарсить JSON
    data = None
    try:
        data = json.loads(risks_json)
    except Exception:
        pass

    # Старый формат (markdown) — просто рендерим
    if data is None or "articles" not in data:
        st.markdown(risks_json)
        return

    articles = data.get("articles", [])
    stats    = data.get("stats", {})
    summary  = data.get("summary", "")
    rag_note = data.get("rag_note", "")

    # ── Сводная шапка ────────────────────────────────────────────────────────
    n_red    = stats.get("red", 0)
    n_yellow = stats.get("yellow", 0)
    n_green  = stats.get("green", 0)
    n_total  = stats.get("total", len(articles))

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Всего статей",   n_total)
    col2.metric("🔴 Высокий риск", n_red,   delta=None,
                delta_color="inverse" if n_red > 0 else "normal")
    col3.metric("🟡 Средний риск", n_yellow)
    col4.metric("🟢 Без замечаний", n_green)

    # Общий индикатор
    if n_red > 0:
        st.error(f"🔴 **ВЫСОКИЙ РИСК** — {n_red} статей требуют немедленного внимания")
    elif n_yellow > 0:
        st.warning(f"🟡 **СРЕДНИЙ РИСК** — {n_yellow} статей с замечаниями")
    else:
        st.success("🟢 **НИЗКИЙ РИСК** — существенных замечаний не выявлено")

    if rag_note:
        st.caption(rag_note)

    # ── Заключение LLM ───────────────────────────────────────────────────────
    if summary:
        with st.expander("📋 Итоговое заключение", expanded=True):
            st.markdown(summary)

    st.divider()

    # ── Фильтр ───────────────────────────────────────────────────────────────
    st.markdown("**Постатейный анализ**")
    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
    show_red    = f_col1.checkbox("🔴 Высокий", value=True,  key="ca_f_red")
    show_yellow = f_col2.checkbox("🟡 Средний",  value=True,  key="ca_f_yellow")
    show_green  = f_col3.checkbox("🟢 Норма",    value=False, key="ca_f_green")
    # Неизвестно больше не используется — fallback всегда даёт 🔴
    filter_map = {
        "red":     show_red,
        "yellow":  show_yellow,
        "green":   show_green,
        "unknown": show_red,   # unknown = красный для целей фильтрации
    }
    visible = [a for a in articles if filter_map.get(a.get("risk", "unknown"), True)]
    st.caption(f"Показано: {len(visible)} из {n_total}")

    # ── Expander на каждую статью ────────────────────────────────────────────
    RISK_COLOR = {"red": "🔴", "yellow": "🟡", "green": "🟢", "unknown": "⚪"}
    RISK_LABEL = {"red": "Высокий риск", "yellow": "Средний риск",
                  "green": "Без замечаний", "unknown": "Не определён"}

    for art in visible:
        risk     = art.get("risk", "unknown")
        emoji    = RISK_COLOR[risk]
        label    = RISK_LABEL[risk]
        name     = art.get("name", "—")
        amounts  = art.get("amounts", "")
        doc      = art.get("document", "не указан")
        basis    = art.get("basis", "")
        rec      = art.get("recommendation", "")
        has_npa  = art.get("has_npa", False)

        # Заголовок expander
        exp_title = f"{emoji} {name[:70]}"
        if amounts:
            # Берём первое значение из amounts для отображения в заголовке
            first_val = amounts.split("|")[0].strip() if amounts else ""
            if first_val:
                exp_title += f"  ·  {first_val[:40]}"

        expanded_default = risk in ("red", "yellow")

        with st.expander(exp_title, expanded=expanded_default):
            # Строка статуса
            doc_ok = doc.lower() not in ("отсутствует", "не указан", "нет", "")
            doc_icon = "✅" if doc_ok else "❌"

            c_left, c_right = st.columns([3, 2])
            with c_left:
                st.markdown(f"**{emoji} {label}**")

                # Топ-3 файла со score и самари
                matched_files = art.get("matched_files", [])
                best_sim      = art.get("best_sim", 0.0)
                if matched_files:
                    st.markdown("**Файлы из заявки:**")
                    for d in matched_files:
                        sim_pct   = int(d.get("_similarity", 0) * 100)
                        sim_bar   = "🟩" * (sim_pct // 20) + "⬜" * (5 - sim_pct // 20)
                        fname     = d.get("file_name", "—")
                        file_sum  = d.get("summary", "")[:120]
                        st.caption(f"{sim_bar} **{sim_pct}%** — {fname}")
                        if file_sum:
                            st.caption(f"   *{file_sum}*")
                else:
                    st.markdown("❌ **Файл-обоснование:** не найден")

                if not has_npa:
                    st.caption("⚠️ НПА по этой статье в базе знаний не найдены")

            with c_right:
                if amounts:
                    st.markdown("**Значения:**")
                    for v in amounts.split("|")[:4]:
                        st.caption(v.strip())

            if basis:
                st.markdown(f"**Основание:** {basis}")
            if rec:
                st.info(f"💡 {rec}")

    # ── Кнопка скачать только проблемные ─────────────────────────────────────
    st.divider()
    problem_articles = [a for a in articles if a.get("risk") in ("red", "yellow")]
    if problem_articles:
        lines = [f"АНАЛИЗ РИСКОВ ТАРИФНОЙ ЗАЯВКИ\n{'='*50}\n"]
        if summary:
            lines.append(summary + "\n\n" + "="*50 + "\n")
        for a in problem_articles:
            lines.append(f"\n{a['risk_emoji']} {a['name']}")
            lines.append(f"Значения: {a['amounts']}")
            lines.append(f"Документ: {a['document']}")
            lines.append(f"Основание: {a['basis']}")
            lines.append(f"Рекомендация: {a['recommendation']}")
            lines.append("-"*40)
        report_text = "\n".join(lines)
        st.download_button(
            f"⬇️ Скачать замечания ({len(problem_articles)} статей)",
            data=report_text.encode("utf-8"),
            file_name="замечания_регулятора.txt",
            mime="text/plain",
            key="ca_dl_problems",
        )


def analyze_risks(calc_context: str, summary: str, progress_cb=None,
                  spheres: Optional[List[str]] = None,
                  file_summaries: Optional[Dict[str, str]] = None) -> str:
    """
    Постатейный RAG-анализ рисков.
    spheres:        фильтр RAG по сфере (None = все).
    file_summaries: {имя_файла: самари} из _build_file_summaries().
    """
    BATCH_SIZE = 10
    CHUNK_LIMIT_PER_ARTICLE = 8000
    MAX_TOKENS_PER_ARTICLE  = 300

    prompts  = load_prompts()
    articles = _extract_articles_from_context(calc_context) if calc_context else []

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
    article_results: List[str] = []
    rag_available = True

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

    # Прогрев RAG / инициализация реранкера
    if progress_cb:
        sphere_label = f" · сферы: {', '.join(spheres)}" if spheres else " · все сферы"
        progress_cb(P_INIT_START, f"Инициализация RAG (русский реранкер DiTy){sphere_label}...")
    _rag_search("тарифное регулирование НВВ", top_k=1, spheres=spheres or None)
    if progress_cb:
        progress_cb(P_INIT_END, f"Реранкер готов. Статей к анализу: {total}")

    # Счётчики для независимого расчёта прогресса каждой фазы
    rag_done = 0  # сколько статей прошло RAG
    llm_done = 0  # сколько статей прошло LLM

    # ── Батчевая обработка ────────────────────────────────────────────────────
    for batch_start in range(0, total, BATCH_SIZE):
        batch = articles[batch_start: batch_start + BATCH_SIZE]

        # Шаг 1: RAG для всего батча подряд
        batch_chunks: List[List[Dict]] = []
        for j, art in enumerate(batch):
            # Прогресс: позиция внутри RAG-диапазона по числу уже обработанных
            rag_frac = rag_done / total  # 0..1 внутри фазы
            if progress_cb:
                progress_cb(
                    P_RAG_START + rag_frac * (P_RAG_END - P_RAG_START),
                    f"RAG {rag_done + 1}/{total}: {art['name'][:40]}..."
                )
            chunks = _rag_search(_make_rag_query(art["name"]), top_k=10,
                                  spheres=spheres or None)
            batch_chunks.append(chunks)
            rag_done += 1
            if not chunks:
                rag_available = False

        # Шаг 1.5: освобождаем реранкер перед LLM — он заново загрузился в RAG-фазе
        try:
            from core.advisor import invalidate_reranker, invalidate_hybrid_retriever
            invalidate_reranker()
            invalidate_hybrid_retriever()
        except Exception:
            pass
        # Принудительная сборка мусора Python между батчами
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

        # Шаг 2: LLM для всего батча
        for j, (art, chunks) in enumerate(zip(batch, batch_chunks)):
            name    = art["name"]
            amounts = art["amounts"]
            # Прогресс: позиция внутри LLM-диапазона по числу уже обработанных
            llm_frac = llm_done / total  # 0..1 внутри фазы
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

            npa_context = _format_chunks_for_prompt(
                chunks, max_chars=CHUNK_LIMIT_PER_ARTICLE
            )
            has_npa = bool(chunks)

            # ── Сопоставление с инвентарём документов (топ-3 по score) ───
            summaries = file_summaries or {}
            matched   = _match_files_to_article(name, summaries, top_k=3)

            # Детерминированный цвет по best similarity
            best_sim  = matched[0]["_similarity"] if matched else 0.0
            if best_sim >= SIMILARITY_GREEN:
                forced_risk       = "green"
                forced_risk_emoji = "🟢"
            elif best_sim >= SIMILARITY_YELLOW:
                forced_risk       = "yellow"
                forced_risk_emoji = "🟡"
            else:
                # Документов нет — красный принудительно
                forced_risk       = "red"
                forced_risk_emoji = "🔴"

            # НПА инструкция
            npa_instr = (
                "Используй ТОЛЬКО нормы НПА приведённые выше. Укажи документ и пункт."
                if has_npa else
                "НПА в базе не найдены. НЕ придумывай ссылки. "
                "В РЕКОМЕНДАЦИИ опиши какой документ нужен исходя из здравого смысла."
            )

            if matched:
                # Файлы найдены: оцениваем соответствие НПА
                files_block = "\n".join(
                    f"  - {d['file_name']} [{int(d['_similarity']*100)}%]:\n"
                    f"    {d['summary'][:200]}"
                    for d in matched
                )
                prompt = (
                    f"Оцени статью затрат тарифной заявки.\n\n"
                    f"СТАТЬЯ: {name}\n"
                    f"ЗНАЧЕНИЯ: {amounts}\n\n"
                    f"НАЙДЕННЫЕ ФАЙЛЫ В ЗАЯВКЕ:\n{files_block}\n\n"
                    f"НПА ИЗ БАЗЫ ЗНАНИЙ:\n{npa_context}\n\n"
                    f"{npa_instr}\n\n"
                    f"Цвет риска уже определён автоматически. "
                    f"Оцени содержание найденных файлов.\n\n"
                    f"Ответ строго в формате (3 строки):\n"
                    f"ДОКУМЕНТ: название наиболее подходящего файла\n"
                    f"ОСНОВАНИЕ: пункт НПА — соблюдён или есть замечание\n"
                    f"РЕКОМЕНДАЦИЯ: что проверить или исправить заявителю"
                )
            else:
                # Файлов нет: рекомендуем что приложить
                prompt = (
                    f"Статья затрат тарифной заявки НЕ подтверждена документами.\n\n"
                    f"СТАТЬЯ: {name}\n"
                    f"ЗНАЧЕНИЯ: {amounts}\n\n"
                    f"НПА ИЗ БАЗЫ ЗНАНИЙ:\n{npa_context}\n\n"
                    f"{npa_instr}\n\n"
                    f"Цвет: 🔴 (документ отсутствует — определено автоматически).\n"
                    f"Укажи какой именно документ требуется.\n\n"
                    f"Ответ строго в формате (3 строки):\n"
                    f"ДОКУМЕНТ: отсутствует\n"
                    f"ОСНОВАНИЕ: какой пункт НПА требует обоснования\n"
                    f"РЕКОМЕНДАЦИЯ: какой именно документ нужно приложить"
                )

            art_result = _lm_call(
                prompts["claim_risks_system"],
                prompt,
                max_tokens=MAX_TOKENS_PER_ARTICLE,
            )

            # Парсим ответ модели
            parsed = _parse_article_result(art_result)

            # Применяем принудительный цвет если он был назначен
            final_risk       = forced_risk       or parsed["risk"]
            final_risk_emoji = forced_risk_emoji or parsed["risk_emoji"]

            article_results.append({
                "name":           name,
                "amounts":        amounts,
                "risk":           final_risk,
                "risk_emoji":     final_risk_emoji,
                "document":       parsed["document"],
                "basis":          parsed["basis"],
                "recommendation": parsed["recommendation"],
                "raw":            art_result,
                "has_npa":        has_npa,
                "matched_files":  matched,          # топ-3 файла со score
                "best_sim":       round(best_sim, 3),
            })
            llm_done += 1

    if progress_cb:
        progress_cb(0.88, f"Агрегирую {total} статей...")

    # ── Итоговый отчёт (агрегация через LLM) ─────────────────────────────────
    rag_note = (
        "⚠️ Часть статей без данных НПА — добавьте НПА в базу знаний."
        if not rag_available else
        "✅ Анализ выполнен с привлечением нормативной базы знаний."
    )

    # Краткая текстовая выжимка для LLM-агрегации
    items_for_agg = []
    for a in article_results:
        items_for_agg.append(
            f"{a['risk_emoji']} {a['name']}: {a['amounts'][:80]}\n"
            f"  Документ: {a['document']}\n"
            f"  Основание: {a['basis'][:120]}"
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

    # Возвращаем JSON-совместимую структуру как строку через json.dumps
    output = json.dumps({
        "summary":     summary_result,
        "rag_note":    rag_note,
        "articles":    article_results,
        "stats": {
            "total":   total,
            "red":     sum(1 for a in article_results if a["risk"] == "red"),
            "yellow":  sum(1 for a in article_results if a["risk"] == "yellow"),
            "green":   sum(1 for a in article_results if a["risk"] == "green"),
        },
    }, ensure_ascii=False)

    if progress_cb:
        progress_cb(1.0, f"Готово — {total} статей")
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Хелперы
# ─────────────────────────────────────────────────────────────────────────────
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
# ─────────────────────────────────────────────────────────────────────────────
def _show_mr_settings():
    """Панель настроек Map-Reduce с калькулятором контекста."""
    cfg = load_mr_config()

    with st.expander("⚙️ Настройки Map-Reduce", expanded=False):
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
        if bc1.button("💾 Сохранить настройки", key="mr_save",
                      use_container_width=True, type="primary"):
            save_mr_config(new_cfg)
            st.success("Настройки сохранены.")
            st.rerun()

        if bc2.button("↺ Сбросить к умолчаниям", key="mr_reset",
                      use_container_width=True):
            save_mr_config(MR_DEFAULTS)
            st.success("Настройки сброшены к умолчаниям.")
            st.rerun()


def show_claim_analyzer():
    st.header("Анализатор тарифных заявок")
    st.caption("Риски · Резюме · Реестр заявок")

    ss = st.session_state
    for k, v in [
        ("ca_summary",        ""),
        ("ca_risks",          ""),
        ("ca_calc_context",   ""),
        ("ca_org",            ""),
        ("ca_period",         ""),
        ("ca_done",           False),
        ("ca_summary_words",  2000),
        ("ca_project_id",     None),
        ("ca_uploaded_meta",  []),
        ("ca_uploaded_bytes", {}),
        ("ca_spheres",        []),   # выбранные сферы для RAG
        ("ca_file_summaries", {}),   # словарь {файл: самари} из суммаризации
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

    # ── Настройки Map-Reduce ──────────────────────────────────────────────────
    _show_mr_settings()

    # ── Выбор сферы регулирования ─────────────────────────────────────────────
    st.subheader("🏭 Сфера регулирования")
    st.caption("Выберите сферу — RAG будет искать НПА только по ней. "
               "Не выбрано = поиск по всей базе.")

    sphere_cols = st.columns(len(REGULATION_SPHERES))
    for col, sph in zip(sphere_cols, REGULATION_SPHERES):
        sid   = sph["id"]
        label = f"{sph['icon']}\n{sph['label']}"
        checked = sid in ss.ca_spheres
        if col.checkbox(label, value=checked, key=f"ca_sphere_{sid}"):
            if sid not in ss.ca_spheres:
                ss.ca_spheres.append(sid)
        else:
            if sid in ss.ca_spheres:
                ss.ca_spheres.remove(sid)

    if ss.ca_spheres:
        selected_names = [SPHERE_LABELS.get(s, s) for s in ss.ca_spheres]
        st.info(f"Фильтр RAG: {' · '.join(selected_names)}")
    else:
        st.caption("Фильтр не задан — поиск по всей нормативной базе.")

    # ── Реквизиты ─────────────────────────────────────────────────────────────
    with st.expander("📋 Реквизиты заявки", expanded=not ss.ca_done):
        c1, c2 = st.columns(2)
        ss.ca_org    = c1.text_input("Организация", value=ss.ca_org,
                                     placeholder="ООО «Теплоснабжение»",
                                     key="ca_org_input")
        ss.ca_period = c2.text_input("Период регулирования", value=ss.ca_period,
                                     placeholder="2025 год",
                                     key="ca_period_input")

    # ── Загрузка файлов ───────────────────────────────────────────────────────
    st.subheader("📁 Файлы заявки")

    st.caption(
        "Чтобы загрузить папку целиком: откройте папку в проводнике, "
        "нажмите **Ctrl+A** для выделения всех файлов, затем перетащите их сюда."
    )
    uploaded = st.file_uploader(
        "Перетащите файлы или нажмите «Browse files»",
        type=["xlsx", "xls", "pdf", "docx", "doc"],
        accept_multiple_files=True,
        key="ca_uploader",
    ) or []

    if uploaded:
        # Разделяем Excel и документы
        xlsx_files = [f for f in uploaded
                      if os.path.splitext(f.name.lower())[1] in (".xlsx", ".xls")]
        doc_files  = [f for f in uploaded
                      if os.path.splitext(f.name.lower())[1] in (".pdf", ".docx", ".doc")]

        st.success(
            f"Загружено: **{len(uploaded)}** файл(ов) — "
            f"📊 {len(xlsx_files)} расчётных · 📄 {len(doc_files)} документов"
        )

        # ── Список файлов с выбором расчётной модели ─────────────────────────
        st.markdown("**Отметьте расчётные модели** (Excel-файлы со статьями затрат):")

        calc_checked: List[str] = []
        for uf in uploaded:
            ext = os.path.splitext(uf.name.lower())[1]
            is_xlsx = ext in (".xlsx", ".xls")
            c1, c2 = st.columns([5, 1])
            icon = "📊" if is_xlsx else "📄"
            c1.write(f"{icon} {uf.name} · {_format_size(uf.size)}")
            if is_xlsx:
                default_checked = (
                    uf.name in ss.get("ca_calc_files_checked", [])
                    or (not ss.get("ca_calc_files_checked") and len(xlsx_files) == 1)
                )
                if c2.checkbox(
                    "🧮", key=f"ca_calc_{uf.name}",
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
                "⚠️ Не выбрана ни одна расчётная модель. "
                "Отметьте галочкой 🧮 хотя бы один Excel-файл со статьями затрат — "
                "без него анализ рисков будет неполным."
            )
        elif not xlsx_files:
            st.info(
                "ℹ️ В загруженных файлах нет Excel-таблиц. "
                "Анализ рисков будет выполнен только на основе текста документов."
            )

        st.divider()
        c1, c2, c3 = st.columns([2, 1, 1])
        ss.ca_summary_words = c1.select_slider(
            "Объём резюме",
            options=[500, 1000, 2000, 3000, 5000],
            value=ss.ca_summary_words,
            format_func=lambda x: f"{x} слов",
            key="ca_words_slider",
        )

        # Блокируем кнопки если есть Excel но ни одна не помечена
        _block_run = bool(xlsx_files) and not has_calc
        run_full  = c2.button(
            "🔍 Полный анализ", type="primary",
            use_container_width=True, key="ca_run_full",
            disabled=_block_run,
        )
        run_risks = c3.button(
            "⚡ Только риски",
            use_container_width=True, key="ca_run_risks",
            disabled=_block_run,
        )
        if _block_run:
            st.error(
                "🚫 Выберите хотя бы одну расчётную модель (галочка 🧮 напротив Excel-файла), "
                "чтобы запустить анализ."
            )

        # ── Полный анализ ─────────────────────────────────────────────────────
        if run_full:
            pbar   = st.progress(0.0)
            status = st.empty()

            calc_context = ""
            calc_names   = ss.get("ca_calc_files_checked", [])

            # Кешируем байты файлов пока они доступны
            ss.ca_uploaded_bytes = {}
            ss.ca_uploaded_meta  = []
            for uf in uploaded:
                b = uf.read()
                ss.ca_uploaded_bytes[uf.name] = b
                ss.ca_uploaded_meta.append({"name": uf.name, "size": len(b)})

            # Парсим все отмеченные расчётные модели и объединяем контекст
            for uf_name, uf_bytes in ss.ca_uploaded_bytes.items():
                ext = os.path.splitext(uf_name.lower())[1]
                if ext not in (".xlsx", ".xls"):
                    continue
                if calc_names and uf_name not in calc_names:
                    continue
                status.text(f"📊 Парсю расчётный файл: {uf_name}...")
                pbar.progress(0.05)
                try:
                    from core.calc_parser import parse_workbook, to_llm_context
                    df_calc, meta_calc = parse_workbook(uf_bytes)
                    if not df_calc.empty:
                        calc_context += f"\n\n# {uf_name}\n" + to_llm_context(df_calc)
                        st.info(
                            f"✅ {uf_name}: "
                            f"{df_calc['article'].nunique()} статей · "
                            f"формат: {meta_calc.get('format','?')} · "
                            f"периоды: {sorted(df_calc['period'].unique().tolist())}"
                        )
                    else:
                        st.warning(f"⚠️ {uf_name}: статьи затрат не найдены (пустой файл или незаполненный шаблон)")
                except Exception as e:
                    st.warning(f"calc_parser [{uf_name}]: {e}")

            ss.ca_calc_context = calc_context
            pbar.progress(0.15)

            full_text = ""
            for uf_name, uf_bytes in ss.ca_uploaded_bytes.items():
                if uf_name in calc_names:
                    continue
                status.text(f"📄 Читаю {uf_name}...")
                try:
                    try:
                        from streamlit_pages.doc_scanner import extract_text
                        pages = extract_text(uf_bytes, uf_name)
                        full_text += f"\n\n=== {uf_name} ===\n" + \
                                     "\n".join(p["text"] for p in pages)
                    except ImportError:
                        full_text += f"\n\n=== {uf_name} ===\n" + \
                                     uf_bytes.decode("utf-8", errors="ignore")
                except Exception as e:
                    st.warning(f"Ошибка чтения {uf_name}: {e}")
            pbar.progress(0.25)

            combined = ""
            if calc_context:
                combined += "=== РАСЧЁТНЫЙ ФАЙЛ ===\n" + calc_context + "\n\n"
            if full_text.strip():
                combined += "=== ДОКУМЕНТЫ ===\n" + full_text

            if not combined.strip():
                st.error("Не удалось извлечь данные.")
                st.stop()

            st.info(f"📊 Подготовлено: **{len(combined.split()):,} слов**")

            ss["_pbar_max"] = 0.25

            def _pcb_sum(pct, msg):
                val = min(0.25 + pct * 0.40, 0.65)
                if val > ss.get("_pbar_max", 0):
                    ss["_pbar_max"] = val
                    pbar.progress(val)
                status.text(msg)

            summary = summarize_claim(combined, ss.ca_summary_words, _pcb_sum)
            ss.ca_summary = summary
            ss["_pbar_max"] = 0.65
            pbar.progress(0.65)

            def _pcb_risk(pct, msg):
                val = min(0.65 + pct * 0.34, 0.99)
                if val > ss.get("_pbar_max", 0):
                    ss["_pbar_max"] = val
                    pbar.progress(val)
                status.text(msg)

            # ── Шаг 2: суммаризация документальных файлов заявки ────────
            n_doc_files = sum(
                1 for name in ss.ca_uploaded_bytes
                if name not in calc_names
                and os.path.splitext(name.lower())[1]
                in ('.pdf', '.docx', '.doc', '.txt')
            )
            if n_doc_files > 0:
                status.text(f'Суммаризация документов ({n_doc_files} файлов)...')
                pbar.progress(0.60)

                def _pcb_sum(frac, msg):
                    val = 0.60 + frac * 0.06
                    pbar.progress(min(val, 0.66))
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
                    st.info(
                        f'Суммаризировано файлов: **{len(file_summaries)}** — '
                        f'{names_preview}{suffix}'
                    )
                else:
                    st.caption('Документальные файлы не обработаны — анализ только по НПА.')
            else:
                file_summaries = {}
                ss["ca_file_summaries"] = {}

            risks = analyze_risks(calc_context, summary, _pcb_risk,
                                  spheres=ss.ca_spheres or None,
                                  file_summaries=ss.get("ca_file_summaries", {}))
            ss.ca_risks    = risks
            ss.ca_done     = True
            ss.ca_project_id = None  # сброс — анализ обновлён, нужно пересохранить

            _save_log(ss.ca_org, ss.ca_period, summary, risks)
            pbar.progress(1.0)
            status.success("✅ Анализ завершён!")
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
                    status.text(f"📊 Парсю расчётный файл: {uf_name}...")
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
                    status.text(f'Суммаризация документов ({n_doc_files} файлов)...')
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
                        st.info(f'Суммаризировано: **{len(file_summaries)}** — {names_preview}{suffix}')
                    else:
                        st.caption('Документы не обработаны — анализ только по НПА.')

            ss["_pbar_max"] = 0.15

            def _pcb_r(pct, msg):
                val = min(0.15 + pct * 0.84, 0.99)
                if val > ss.get("_pbar_max", 0):
                    ss["_pbar_max"] = val
                    pbar.progress(val)
                status.text(msg)

            ss.ca_risks      = analyze_risks(
                ss.ca_calc_context, ss.ca_summary, _pcb_r,
                spheres=ss.ca_spheres or None,
                file_summaries=ss.get("ca_file_summaries", {}),
            )
            ss.ca_done       = True
            ss.ca_project_id = None
            pbar.progress(1.0)
            status.success("✅ Риски обновлены!")
            st.rerun()

    # ── Баннер + кнопка «Сохранить в реестр» ─────────────────────────────────
    if ss.ca_done:
        col_info, col_save = st.columns([4, 1])
        if ss.ca_project_id:
            col_info.success(
                f"✅ Сохранено в реестр · ID: `{ss.ca_project_id}`"
                + ("" if uploaded else f" · **{ss.ca_org or '—'}** · {ss.ca_period or '—'}")
            )
        elif not uploaded:
            col_info.info(
                f"💾 Данные в памяти: **{ss.ca_org or '—'}** · {ss.ca_period or '—'}"
            )

        if ss.ca_summary or ss.ca_risks:
            if col_save.button(
                "💾 В реестр" if not ss.ca_project_id else "🔄 Обновить",
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
                    st.success(f"✅ Сохранено: `{pid}`")
                    st.rerun()
                except Exception as e:
                    st.error(f"Ошибка сохранения: {e}")

    st.divider()
    st.markdown("#### Результаты анализа")
    tab_risks, tab_summary, tab_registry = st.tabs([
        "Риски и комплектность",
        "Резюме заявки",
        "Реестр заявок",
    ])

    # =========================================================================
    # Вкладка 1: Риски
    # =========================================================================
    with tab_risks:
        # Диагностика RAG (компактно)
        diag = _rag_diagnose()
        if diag:
            if "недоступен" in diag or "Не удалось" in diag:
                st.error(f"⚠️ {diag}")
            else:
                st.caption(f"✅ {diag}")
        # Реестр суммаризированных файлов
        file_sums = ss.get("ca_file_summaries", {})
        if file_sums:
            with st.expander(
                f'Файлы заявки ({len(file_sums)} суммаризировано)',
                expanded=False,
            ):
                for fname, fsum in file_sums.items():
                    st.markdown(f"**{fname}**")
                    st.caption(fsum[:200] if fsum else "—")
                    st.divider()

        if ss.ca_risks:
            _render_risks_tab(ss.ca_risks)
        else:
            st.info(
                "Загрузите файлы и нажмите «Полный анализ» — "
                "здесь появится постатейная оценка рисков с фильтрацией."
            )

    # =========================================================================
    # Вкладка 2: Резюме
    # =========================================================================
    with tab_summary:
        st.subheader("Структурированное резюме заявки")
        if ss.ca_calc_context:
            with st.expander("📊 Данные расчётного файла", expanded=False):
                st.code(ss.ca_calc_context[:5000], language=None)
                if len(ss.ca_calc_context) > 5000:
                    st.caption(f"… ещё {len(ss.ca_calc_context)-5000} символов")
        if ss.ca_summary:
            st.caption(f"Объём: {len(ss.ca_summary.split()):,} слов")
            st.markdown(ss.ca_summary)
            st.download_button(
                "⬇️ Скачать резюме (.txt)",
                data=ss.ca_summary.encode("utf-8"),
                file_name=f"резюме_{ss.ca_org or 'заявка'}.txt",
                mime="text/plain",
                key="ca_dl_summary",
            )
        else:
            st.info("Загрузите файлы и нажмите «Полный анализ».")

    # =========================================================================
    # Вкладка 3: Реестр
    # =========================================================================
    with tab_registry:
        _show_registry()

    # ── Обратная связь ────────────────────────────────────────────────────────
    st.divider()
    with st.expander("📝 Сообщить об ошибке", expanded=False):
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
                    st.success("✅ Спасибо!")
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
    search        = fc1.text_input("🔍 Поиск", placeholder="организация, период, тег...",
                                   key="reg_search", label_visibility="collapsed")
    status_filter = fc2.selectbox("Статус", ["все"] + STATUSES,
                                  key="reg_status_filter", label_visibility="collapsed")

    projects = list_projects(search=search, status_filter=status_filter)

    if not projects:
        st.info(
            "Реестр пуст. Выполните анализ заявки и нажмите «💾 В реестр»."
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

            if hc3.button("🗑️", key=f"reg_del_{pid}",
                          help="Удалить из реестра"):
                ss = st.session_state
                ss[f"reg_confirm_del_{pid}"] = True

            if st.session_state.get(f"reg_confirm_del_{pid}"):
                st.warning(f"Удалить **{org} · {period}**? Это действие необратимо.")
                da, db = st.columns(2)
                if da.button("✅ Да, удалить", key=f"reg_del_yes_{pid}",
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
                        f"{'📄' if not fpath else '📎'} {fname} · "
                        f"{_format_size(fsize)}"
                    )
                    if fpath:
                        with open(fpath, "rb") as f_bin:
                            fc2_f.download_button(
                                "⬇️",
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
                        "⬇️ Скачать резюме (.txt)",
                        data=summary.encode("utf-8"),
                        file_name=f"резюме_{org}_{period}.txt",
                        mime="text/plain",
                        key=f"reg_dl_sum_{pid}",
                    )
                else:
                    st.caption("Резюме не сохранено.")

            with sub2:
                if risks:
                    st.markdown(risks)
                    st.download_button(
                        "⬇️ Скачать риски (.txt)",
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
                f"↩️ Открыть в анализаторе",
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