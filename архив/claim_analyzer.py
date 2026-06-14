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

    # ── Группируем по label ───────────────────────────────────────────────────
    series: dict = OrderedDict()
    for yr, lbl, v in ts:
        series.setdefault(lbl, []).append((yr, v))
    for lbl in series:
        series[lbl].sort(key=lambda x: x[0])
    if not series:
        return ""

    # Серии с >= 2 точками — линии; с 1 точкой — только маркер
    line_series  = {k: v for k, v in series.items() if len(v) >= 2}
    point_series = {k: v for k, v in series.items() if len(v) == 1}
    # Если нет ни одной линейной серии — рисуем все как точки
    if not line_series:
        line_series  = series
        point_series = {}

    n_series = len(series)
    # Легенда справа от графика
    max_lbl_len = max(len(lbl) for lbl in series) if series else 6
    LEG_W    = 26 + max_lbl_len * 7   # ширина колонки легенды (пикселей)
    W        = 420
    H        = 155
    PAD_L    = 56
    PAD_R    = 12 + LEG_W             # правый отступ = место под легенду
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

    # Целевой рост — пунктир от первой точки первой серии
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

    # Ось X — прореживаем
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

    # Однопунктные серии — только маркеры (ромб) без линии
    for lbl, pts in point_series.items():
        color = lbl_colors[lbl]
        yr, v = pts[0]
        cx, cy = px(yr), py(v)
        is_reg = (yr == reg_year)
        r = 5 if is_reg else 4
        tip = f"{yr} ({lbl}): {v:,.0f} тыс.руб."
        # Ромб вместо круга — визуально отличается от линейных серий
        d = r
        circles_svg += (
            f'<polygon points="{cx:.1f},{cy-d:.1f} {cx+d:.1f},{cy:.1f} '
            f'{cx:.1f},{cy+d:.1f} {cx-d:.1f},{cy:.1f}" '
            f'fill="{color}" stroke="#fff" stroke-width="1.5" opacity="0.9">'
            f'<title>{tip}</title></polygon>'
        )
        # Подпись значения
        ty = cy - 9 if cy - 9 >= PAD_T + 6 else cy + 14
        circles_svg += (
            f'<text x="{cx:.1f}" y="{ty:.1f}" '
            f'text-anchor="middle" font-size="9" fill="{color}">{fmt_val(v)}</text>'
        )

    # Легенда справа от области графика
    leg_x = W - LEG_W + 4   # X-начало легенды
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

                if dynamics:
                    st.markdown(dynamics)

                # График под текстом
                if amounts:
                    ts = _parse_amounts_timeseries(amounts)
                    if ts and len(ts) >= 2:
                        svg = _render_timeseries_chart(
                            ts, _reg_year, risk, target_pct=_tgt_pct,
                        )
                        if svg:
                            st.markdown(svg, unsafe_allow_html=True)

                if basis:
                    st.markdown(f"**Нормативное основание:** {basis}")

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
            if a.get("article_summary"):
                lines.append(f"Динамика: {a['article_summary']}")
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
    MAX_TOKENS_PER_ARTICLE  = 300

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
        chunks = _rag_search(_make_rag_query(art["name"]), top_k=10,
                             spheres=spheres or None)
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

            npa_context = _format_chunks_for_prompt(
                chunks, max_chars=CHUNK_LIMIT_PER_ARTICLE
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

            # ── НПА инструкция ────────────────────────────────────────────────
            npa_instr = (
                "Используй ТОЛЬКО нормы НПА приведённые выше. Укажи документ и пункт."
                if has_npa else
                "НПА в базе не найдены. НЕ придумывай ссылки. "
                "В РЕКОМЕНДАЦИИ опиши какой документ нужен исходя из здравого смысла."
            )

            # ── LLM: динамика + рекомендация по обоснованию ───────────────────
            # Только названия файлов без содержимого — экономим токены
            # Формируем блок файлов обоснования
            if matched:
                files_lines = []
                for d in matched:
                    head = d.get("summary", "")[:1000].strip()
                    if head:
                        files_lines.append(f"[{d['file_name']}]\n{head}")
                    else:
                        files_lines.append(f"[{d['file_name']}]")
                files_block = "\n\n".join(files_lines)
            else:
                files_block = "не найдены"

            prompt = (
                f"Статья затрат тарифной заявки: {name}\n"
                f"Значения: {amounts[:300]}\n"
                f"Рост к предыдущему периоду: {growth_reason}\n\n"
                f"Файлы обоснования в заявке:\n{files_block}\n\n"
                f"НПА из базы знаний:\n{npa_context[:1500]}\n\n"
                f"{npa_instr}\n\n"
                "Ответ строго в формате (3 строки). БЕЗ эмодзи. БЕЗ лишнего текста.\n"
                "ДИНАМИКА: что происходит с этой статьёй в динамике (1-2 предложения)\n"
                "ОСНОВАНИЕ: закон/приказ/пункт НПА, который регулирует данную статью затрат\n"
                "РЕКОМЕНДАЦИЯ: конкретный тип документа который РСО должна приложить к заявке "
                "(договор/акт/расчёт/справка — без ссылок на НПА, только вид документа)"
            )

            art_result = _lm_call(
                prompts["claim_risks_system"],
                prompt,
                max_tokens=MAX_TOKENS_PER_ARTICLE,
            )

            # Парсим ответ модели
            parsed = _parse_article_result_v2(art_result)

            article_results.append({
                "name":             name,
                "amounts":          amounts,
                "risk":             final_risk,
                "risk_emoji":       final_risk_emoji,
                "growth_reason":    growth_reason,
                "base_val":         base_val,
                "reg_val":          reg_val,
                "article_summary":  parsed["dynamics"],
                "basis":            parsed["basis"],
                "recommendation":   parsed["recommendation"],
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



# =============================================================================
# Апрув статей затрат после парсинга
# =============================================================================

def _classify_article(name: str, amounts: str) -> str:
    """
    Автоматически определяет тип строки:
      'cost'  — статья затрат (подлежит анализу)
      'agg'   — агрегат / итоговая строка
      'ref'   — справочный показатель (тарифы, объёмы, индексы)
      'zero'  — все значения нулевые
    """
    n = name.lower().strip()

    # Итоговые агрегаты
    if any(k in n for k in (
        "итого", "всего", "нвв", "необходимая валовая выручка",
        "средневзвешенный", "итого по тарифу", "всего по тарифу",
    )):
        return "agg"

    # Справочные показатели
    if any(k in n for k in (
        "тариф", "руб./", "руб/", "индекс", "объём", "объем",
        "полезный отпуск", "доля товарной", "численность",
        "коэффициент", "норматив", "ставка 1 разряда",
    )):
        return "ref"

    # Нулевые
    if not _has_nonzero_value(amounts):
        return "zero"

    return "cost"


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
                    "checked": True,  # будет скорректировано ниже
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

    # Авто-отбор: снимаем галочки с агрегатов, справочных и нулевых
    n_cost = sum(1 for a in articles if a["type"] == "cost")
    if n_cost > 0:
        # Есть ненулевые статьи затрат — отмечаем только их
        for a in articles:
            a["checked"] = a["type"] == "cost"
    else:
        # Все нулевые — отмечаем все нулевые статьи чтобы пользователь видел список
        for a in articles:
            a["checked"] = a["type"] in ("cost", "zero")

    return articles


def _show_article_approval(readonly: bool = False):
    """
    Отображает таблицу статей затрат с чекбоксами для апрува.
    readonly=True: только просмотр, без изменений (после запуска анализа).
    Сохраняет выбор в ss.ca_parsed_articles и выставляет ss.ca_articles_approved.
    """
    ss = st.session_state
    articles = ss.ca_parsed_articles

    TYPE_LABEL = {
        "cost": "Статья затрат",
        "agg":  "Агрегат / итог",
        "ref":  "Справочно",
        "zero": "Нулевые значения",
    }
    TYPE_COLOR = {
        "cost": "success",
        "agg":  "warning",
        "ref":  "secondary",
        "zero": "error",
    }

    n_total   = len(articles)
    n_checked = sum(1 for a in articles if a["checked"])

    st.subheader("Проверка статей затрат")
    st.caption(
        f"Найдено строк: **{n_total}** · "
        f"К анализу: **{n_checked}** · "
        f"Исключено: **{n_total - n_checked}**"
    )

    # ── Панель управления ────────────────────────────────────────────────────
    if readonly:
        tc1, tc2 = st.columns([2, 1])
        search_q    = tc1.text_input("Поиск", placeholder="Фильтр по названию...",
                                     key="ca_ap_search", label_visibility="collapsed")
        type_filter = tc2.selectbox(
            "Тип", ["Все", "Статья затрат", "Агрегат / итог", "Справочно", "Нулевые значения"],
            key="ca_ap_type", label_visibility="collapsed",
        )
    else:
        tc1, tc2, tc3, tc4, tc5, tc6 = st.columns([2, 1, 1, 1, 1, 1])
        search_q = tc1.text_input("Поиск", placeholder="Фильтр по названию...",
                                   key="ca_ap_search", label_visibility="collapsed")
        type_filter = tc2.selectbox(
            "Тип", ["Все", "Статья затрат", "Агрегат / итог", "Справочно", "Нулевые значения"],
            key="ca_ap_type", label_visibility="collapsed",
        )
        def _reset_editor():
            if "ca_ap_editor" in ss:
                del ss["ca_ap_editor"]

        if tc3.button("Авто-отбор", key="ca_ap_auto", use_container_width=True,
                      help="Оставить только статьи затрат с ненулевыми значениями"):
            for a in articles:
                a["checked"] = a["type"] == "cost"
            ss.ca_parsed_articles = articles
            _reset_editor()
            st.rerun()
        if tc4.button("Выбрать все", key="ca_ap_all", use_container_width=True):
            for a in articles:
                a["checked"] = True
            ss.ca_parsed_articles = articles
            _reset_editor()
            st.rerun()
        if tc5.button("Убрать все", key="ca_ap_none", use_container_width=True):
            for a in articles:
                a["checked"] = False
            ss.ca_parsed_articles = articles
            _reset_editor()
            st.rerun()
        if tc6.button("Инверсия", key="ca_ap_inv", use_container_width=True,
                      help="Инвертировать выбор"):
            for a in articles:
                a["checked"] = not a["checked"]
            ss.ca_parsed_articles = articles
            _reset_editor()
            st.rerun()

    # ── Таблица ──────────────────────────────────────────────────────────────
    TYPE_FILTER_MAP = {
        "Все": None,
        "Статья затрат":    "cost",
        "Агрегат / итог":   "agg",
        "Справочно":        "ref",
        "Нулевые значения": "zero",
    }
    tf = TYPE_FILTER_MAP[type_filter]

    visible = [
        (i, a) for i, a in enumerate(articles)
        if (not search_q or search_q.lower() in a["name"].lower())
        and (tf is None or a["type"] == tf)
    ]

    st.caption(f"Показано: {len(visible)} из {n_total}")

    # ── Таблица через data_editor ────────────────────────────────────────────
    import pandas as pd

    TYPE_OPT_LBLS = {"cost": "Статья затрат", "agg": "Агрегат / итог",
                     "ref": "Справочно", "zero": "Нулевые"}

    # Берём реальные годы из временного ряда для заголовков колонок
    _sample_ts = None
    for _a in articles:
        _ts = _parse_amounts_timeseries(_a["amounts"])
        if len(_ts) >= 2:
            _sample_ts = _ts
            break
    _yr_prev = str(_sample_ts[-2][0]) if _sample_ts and len(_sample_ts) >= 2 else "Прошлый"
    _yr_reg  = str(_sample_ts[-1][0]) if _sample_ts else "Регул. год"

    def _make_df(arts, filt_q, filt_type):
        rows = []
        for i, a in enumerate(arts):
            if filt_q and filt_q.lower() not in a["name"].lower():
                continue
            if filt_type and filt_type != "Все":
                tm = {"Статья затрат": "cost", "Агрегат / итог": "agg",
                      "Справочно": "ref", "Нулевые значения": "zero"}
                if a["type"] != tm.get(filt_type):
                    continue
            ts = _parse_amounts_timeseries(a["amounts"])
            v_prev = ts[-2][2] if len(ts) >= 2 else None
            v_reg  = ts[-1][2] if ts else None
            rows.append({
                "_idx":       i,
                "Включить":   a["checked"],
                "Наименование": a["name"],
                _yr_prev:     f"{v_prev:,.0f}" if v_prev is not None else "—",
                _yr_reg:      f"{v_reg:,.0f}"  if v_reg  is not None else "—",
                "Тип":        TYPE_OPT_LBLS.get(a["type"], a["type"]),
            })
        return pd.DataFrame(rows)

    df_show = _make_df(articles, search_q, type_filter)
    st.caption(f"Показано: {len(df_show)} из {n_total}")

    if not readonly and not df_show.empty:
        edited = st.data_editor(
            df_show.drop(columns=["_idx"]),
            column_config={
                "Включить": st.column_config.CheckboxColumn("Включить", width="small"),
                "Наименование": st.column_config.TextColumn("Наименование", width="large", disabled=True),
                _yr_prev: st.column_config.TextColumn(_yr_prev, width="small", disabled=True),
                _yr_reg:  st.column_config.TextColumn(_yr_reg,  width="small", disabled=True),
                "Тип": st.column_config.SelectboxColumn(
                    "Тип", width="medium",
                    options=list(TYPE_OPT_LBLS.values()),
                ),
            },
            use_container_width=True,
            hide_index=True,
            key="ca_ap_editor",
        )
        # Считаем сколько отмечено прямо из edited — без rerun
        n_sel_live = int(edited["Включить"].sum()) if edited is not None else 0

    elif readonly and not df_show.empty:
        st.dataframe(
            df_show.drop(columns=["_idx"]).rename(columns={"Включить": "✓"}),
            use_container_width=True,
            hide_index=True,
        )
        n_sel_live = sum(1 for a in articles if a["checked"])
    else:
        n_sel_live = 0

    # ── Кнопка подтверждения ─────────────────────────────────────────────────
    st.divider()
    ap_c1, ap_c2 = st.columns([3, 1])
    ap_c1.caption(f"Отмечено к анализу: **{n_sel_live}** статей — нажмите «Подтвердить» чтобы продолжить")
    if not readonly and ap_c2.button(
        "Подтвердить и продолжить",
        type="primary",
        use_container_width=True,
        key="ca_ap_confirm",
        disabled=(n_sel_live == 0),
    ):
        # Читаем финальное состояние из data_editor и сохраняем
        _lbl_to_type = {v: k for k, v in TYPE_OPT_LBLS.items()}
        if edited is not None:
            for row_i, row in edited.iterrows():
                orig_i = int(df_show.iloc[row_i]["_idx"])
                articles[orig_i]["checked"] = bool(row["Включить"])
                articles[orig_i]["type"]    = _lbl_to_type.get(row["Тип"], "cost")

        approved = [a for a in articles if a["checked"]]
        ss.ca_parsed_articles   = approved   # только отмеченные идут в анализ
        ss.ca_articles_approved = True
        lines = []
        for a in approved:
            lines.append(f"★ {a['name']}")
            for part in a["amounts"].split(" | "):
                if part.strip():
                    lines.append(f"  {part.strip()}")
        ss.ca_calc_context = "\n".join(lines)
        if "ca_ap_editor" in ss:
            del ss["ca_ap_editor"]
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
            "ca_parsed_articles", "ca_articles_approved", "_pbar_max",
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
            # Извлекаем все статьи (без фильтров — пользователь сам решит)
            raw_articles = _extract_articles_from_context_unfiltered(calc_context)
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