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
import os, io, json, time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# Дефолтные промпты
# ─────────────────────────────────────────────────────────────────────────────
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
        "Ты эксперт-аудитор по тарифному регулированию РФ. "
        "Анализируешь тарифные заявки на риск отклонения регулятором. "
        "Отвечаешь структурированно на русском языке. "
        "Используй эмодзи 🔴 (высокий риск), 🟡 (средний), 🟢 (низкий)."
    ),
    "claim_risks_user": (
        "Проанализируй тарифную заявку и составь отчёт о рисках.\n\n"
        "## 1. Оценка комплектности документов\n"
        "Перечисли какие документы упоминаются в заявке. "
        "Укажи какие документы отсутствуют исходя из заявленных статей затрат "
        "(например: статья «Ремонт ОС» без дефектной ведомости — 🔴).\n\n"
        "## 2. Риски по статьям затрат\n"
        "Для каждой значимой статьи (сумма > 0):\n"
        "- 🔴/🟡/🟢 Статья: сумма тыс. руб.\n"
        "  Основание риска: ...\n"
        "  Рекомендация: ...\n\n"
        "## 3. Итоговая оценка\n"
        "Общий уровень риска и топ-3 рекомендации.\n\n"
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
# Суммаризатор Map-Reduce
# ─────────────────────────────────────────────────────────────────────────────
_CHUNK_SIZE    = 6_000
_CHUNK_OVERLAP = 300
_LARGE_THRESH  = 12_000


def _split_chunks(text: str) -> List[str]:
    if len(text) <= _CHUNK_SIZE:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = start + _CHUNK_SIZE
        if end >= len(text):
            tail = text[start:].strip()
            if tail:
                chunks.append(tail)
            break
        min_pos   = start + _CHUNK_SIZE * 3 // 4
        split_end = end
        for sep in ('\n\n', '. ', ' '):
            pos = text.rfind(sep, min_pos, end)
            if pos != -1:
                split_end = pos + len(sep)
                break
        chunk = text[start:split_end].strip()
        if chunk:
            chunks.append(chunk)
        start = max(start + _CHUNK_SIZE // 2, split_end - _CHUNK_OVERLAP)
    return chunks


def summarize_claim(text: str, target_words: int = 2000,
                    progress_cb=None) -> str:
    prompts = load_prompts()
    if len(text) <= _LARGE_THRESH:
        if progress_cb:
            progress_cb(0.1, "Формирую резюме...")
        result = _lm_call_full(
            prompts["claim_reduce_system"],
            prompts["claim_reduce_user"].format(target_words=target_words, combined=text),
            max_tokens=int(target_words * 2),
            status_cb=lambda m: progress_cb(0.8, m) if progress_cb else None,
        )
        if progress_cb:
            progress_cb(1.0, "Готово")
        return result

    chunks = _split_chunks(text)
    total  = len(chunks)
    t0     = time.perf_counter()
    times: List[float] = []
    minis: List[str]   = []

    for i, chunk in enumerate(chunks, 1):
        elapsed = int(time.perf_counter() - t0)
        eta = f"~{int(sum(times)/len(times)*(total-i+1))}с" if times else "..."
        if progress_cb:
            progress_cb((i-1)/(total+1), f"MAP {i}/{total} · {elapsed}с · {eta}")
        mini = _lm_call(
            prompts["claim_map_system"],
            prompts["claim_map_user"].format(i=i, total=total, chunk=chunk),
            max_tokens=600,
        )
        times.append(time.perf_counter() - t0 - sum(times))
        minis.append(f"=== Часть {i}/{total} ===\n{mini}")

    if progress_cb:
        progress_cb(total/(total+1), f"REDUCE — синтез {total} частей...")

    result = _lm_call_full(
        prompts["claim_reduce_system"],
        prompts["claim_reduce_user"].format(
            target_words=target_words, combined="\n\n".join(minis)
        ),
        max_tokens=int(target_words * 2),
        status_cb=lambda m: progress_cb(0.95, m) if progress_cb else None,
    )
    if progress_cb:
        t_total = time.perf_counter() - t0
        progress_cb(1.0, f"Готово за {int(t_total//60)}м {int(t_total%60)}с")
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


def _rag_search(query: str, top_k: int = 10) -> List[Dict]:
    """
    Поиск по нормативной базе БЕЗ расширения соседями.
    debug_search_candidates явно не подтягивает соседей — возвращает
    чистый текст чанка в поле "doc" (~1750 симв вместо 35 000).

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
        res    = debug_search_candidates(query, top_k=top_k)
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
    Извлекает список статей затрат из плоского контекста calc_parser.
    Формат строк: "    Название статьи: период=сумма, ..."
    Возвращает [{"name": str, "amounts": str}]
    """
    articles = []
    seen = set()
    for line in calc_context.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[") or line.startswith("★"):
            continue
        if ":" in line and "=" in line:
            parts  = line.split(":", 1)
            name   = parts[0].strip().lstrip("★").strip()
            amounts = parts[1].strip() if len(parts) > 1 else ""
            if name and name not in seen and len(name) > 3:
                seen.add(name)
                articles.append({"name": name, "amounts": amounts})
    return articles[:30]  # не больше 30 статей за раз


def analyze_risks(calc_context: str, summary: str, progress_cb=None) -> str:
    """
    Постатейный RAG-анализ рисков — батчевый режим для Qwen 9B.

    BATCH_SIZE=5: для каждого батча сначала RAG, потом LLM.
    Контекст на статью: 5 чанков × 400 симв = 2000 симв.
    max_tokens=120: три строки ответа — достаточно для 9B модели.
    """
    BATCH_SIZE = 3
    # Сокращаем контекст для маленькой модели
    CHUNK_LIMIT_PER_ARTICLE = 4200
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

    # Прогрев RAG
    if progress_cb:
        progress_cb(0.02, "Инициализация RAG (русский реранкер DiTy)...")
    _rag_search("тарифное регулирование НВВ", top_k=1)

    # ── Батчевая обработка ────────────────────────────────────────────────────
    for batch_start in range(0, total, BATCH_SIZE):
        batch = articles[batch_start: batch_start + BATCH_SIZE]

        # Шаг 1: RAG для всего батча подряд
        batch_chunks: List[List[Dict]] = []
        for j, art in enumerate(batch):
            idx = batch_start + j + 1
            if progress_cb:
                progress_cb(
                    0.03 + (idx - 1) / total * 0.40,
                    f"RAG {idx}/{total}: {art['name'][:40]}..."
                )
            chunks = _rag_search(_make_rag_query(art["name"]), top_k=10)
            batch_chunks.append(chunks)
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
            idx     = batch_start + j + 1
            name    = art["name"]
            amounts = art["amounts"]
            if progress_cb:
                progress_cb(
                    0.43 + (idx - 1) / total * 0.44,
                    f"LLM {idx}/{total}: {name[:40]}..."
                )

            npa_context = _format_chunks_for_prompt(
                chunks, max_chars=CHUNK_LIMIT_PER_ARTICLE
            )
            has_npa = bool(chunks)
            npa_instr = (
                "Используй ТОЛЬКО нормы из НПА выше. Укажи документ и пункт."
                if has_npa else
                "НЕ придумывай ссылки на НПА. Напиши: нет данных в базе знаний."
            )
            prompt = (
                f"Оцени риск статьи затрат. Ответ — ровно три строки.\n\n"
                f"СТАТЬЯ: {name}\n"
                f"ЗНАЧЕНИЯ: {amounts}\n\n"
                f"НПА:\n{npa_context}\n\n"
                f"{npa_instr}\n\n"
                f"РИСК: 🔴/🟡/🟢\n"
                f"ОСНОВАНИЕ: ...\n"
                f"РЕКОМЕНДАЦИЯ: ..."
            )
            art_result = _lm_call(
                prompts["claim_risks_system"],
                prompt,
                max_tokens=MAX_TOKENS_PER_ARTICLE,
            )
            article_results.append(f"### {name}\n{amounts}\n\n{art_result}")

    if progress_cb:
        progress_cb(0.88, f"Агрегирую {total} статей...")

    # ── Итоговый отчёт ────────────────────────────────────────────────────────
    rag_note = (
        "\n\n> ⚠️ *Часть статей без данных НПА — добавьте НПА в базу знаний.*"
        if not rag_available else
        "\n\n> ✅ *Анализ выполнен с привлечением нормативной базы знаний.*"
    )
    articles_block = "\n\n---\n\n".join(article_results)

    aggregate_prompt = (
        f"По постатейным оценкам составь краткий итоговый отчёт.\n\n"
        f"1. Сводка: общий риск, суммы под угрозой (2-3 предложения)\n"
        f"2. Топ-3 замечания с рекомендациями\n"
        f"3. Какие документы явно отсутствуют\n\n"
        f"ОЦЕНКИ:\n{articles_block[:5000]}\n\n"
        f"РЕЗЮМЕ:\n{summary[:1500]}"
    )
    summary_result = _lm_call_full(
        prompts["claim_risks_system"],
        aggregate_prompt,
        max_tokens=600,
        status_cb=lambda m: progress_cb(0.94, m) if progress_cb else None,
    )

    full_report = (
        f"{summary_result}"
        f"{rag_note}\n\n"
        f"---\n\n"
        f"## Постатейный анализ\n\n"
        f"{articles_block}"
    )
    if progress_cb:
        progress_cb(1.0, f"Готово — {total} статей за {BATCH_SIZE}-статейные батчи")
    return full_report


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
    ]:
        if k not in ss:
            ss[k] = v

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
    uploaded = st.file_uploader(
        "Загрузите файлы",
        type=["xlsx", "xls", "pdf", "docx", "doc"],
        accept_multiple_files=True,
        key="ca_uploader",
    )

    if uploaded:
        st.success(f"Загружено: **{len(uploaded)}** файл(ов)")
        for uf in uploaded:
            c1, c2 = st.columns([5, 1])
            c1.write(f"📄 {uf.name}")
            if c2.checkbox("🧮", key=f"ca_calc_{uf.name}",
                           value=(ss.get("ca_calc_file") == uf.name),
                           help="Расчётный файл"):
                ss["ca_calc_file"] = uf.name

        st.divider()
        c1, c2, c3 = st.columns([2, 1, 1])
        ss.ca_summary_words = c1.select_slider(
            "Объём резюме",
            options=[500, 1000, 2000, 3000, 5000],
            value=ss.ca_summary_words,
            format_func=lambda x: f"{x} слов",
            key="ca_words_slider",
        )
        run_full  = c2.button("🔍 Полный анализ", type="primary",
                              use_container_width=True, key="ca_run_full")
        run_risks = c3.button("⚡ Только риски",
                              use_container_width=True, key="ca_run_risks",
                              disabled=not (ss.ca_summary or ss.ca_calc_context))

        # ── Полный анализ ─────────────────────────────────────────────────────
        if run_full:
            pbar   = st.progress(0.0)
            status = st.empty()

            calc_context = ""
            calc_name    = ss.get("ca_calc_file", "")

            # Кешируем байты файлов пока они доступны
            ss.ca_uploaded_bytes = {}
            ss.ca_uploaded_meta  = []
            for uf in uploaded:
                b = uf.read()
                ss.ca_uploaded_bytes[uf.name] = b
                ss.ca_uploaded_meta.append({"name": uf.name, "size": len(b)})

            for uf_name, uf_bytes in ss.ca_uploaded_bytes.items():
                ext = os.path.splitext(uf_name.lower())[1]
                if ext in (".xlsx", ".xls") and (not calc_name or uf_name == calc_name):
                    status.text(f"📊 Парсю расчётный файл: {uf_name}...")
                    pbar.progress(0.05)
                    try:
                        from core.calc_parser import parse_workbook, to_llm_context
                        df_calc, _ = parse_workbook(uf_bytes)
                        if not df_calc.empty:
                            calc_context = to_llm_context(df_calc)
                            st.info(
                                f"✅ Расчётный файл: "
                                f"{df_calc['article'].nunique()} статей · "
                                f"периоды: {sorted(df_calc['period'].unique().tolist())}"
                            )
                    except Exception as e:
                        st.warning(f"calc_parser: {e}")
                    break

            ss.ca_calc_context = calc_context
            pbar.progress(0.15)

            full_text = ""
            for uf_name, uf_bytes in ss.ca_uploaded_bytes.items():
                if uf_name == calc_name:
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

            def _pcb_sum(pct, msg):
                pbar.progress(0.25 + pct * 0.40)
                status.text(msg)

            summary = summarize_claim(combined, ss.ca_summary_words, _pcb_sum)
            ss.ca_summary = summary
            pbar.progress(0.65)

            def _pcb_risk(pct, msg):
                pbar.progress(0.65 + pct * 0.30)
                status.text(msg)

            risks = analyze_risks(calc_context, summary, _pcb_risk)
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

            def _pcb_r(pct, msg):
                pbar.progress(pct)
                status.text(msg)

            ss.ca_risks      = analyze_risks(ss.ca_calc_context, ss.ca_summary, _pcb_r)
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

    # ── Вкладки ───────────────────────────────────────────────────────────────
    tab_risks, tab_summary, tab_registry = st.tabs([
        "🔴 Риски и комплектность",
        "📄 Резюме заявки",
        "📂 Реестр заявок",
    ])

    # =========================================================================
    # Вкладка 1: Риски
    # =========================================================================
    with tab_risks:
        st.subheader("Риски и оценка комплектности")
        # Диагностика RAG
        diag = _rag_diagnose()
        if diag:
            if "недоступен" in diag or "Не удалось" in diag:
                st.error(f"⚠️ {diag}")
            else:
                st.success(f"✅ {diag}")
        if ss.ca_risks:
            st.caption(f"Объём: {len(ss.ca_risks.split()):,} слов")
            st.markdown(ss.ca_risks)
            st.download_button(
                "⬇️ Скачать анализ рисков (.txt)",
                data=ss.ca_risks.encode("utf-8"),
                file_name=f"риски_{ss.ca_org or 'заявка'}.txt",
                mime="text/plain",
                key="ca_dl_risks",
            )
        else:
            st.info(
                "Загрузите файлы и нажмите «Полный анализ» — "
                "здесь появится оценка рисков по каждой статье затрат "
                "и анализ комплектности документов."
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
            sub1, sub2 = st.tabs(["📄 Резюме", "🔴 Риски"])

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