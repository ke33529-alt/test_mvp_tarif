# streamlit_pages/predictor.py
"""
Прогноз решения регулятора
──────────────────────────────────────────────────────────────────────────────
Логика:
  1. Пользователь вводит статью затрат + описание/документы-обоснования
  2. Запрос расширяется через QueryExpander (синонимы статей затрат)
  3. Если приложен файл — сжимается через Map-Reduce (как в doc_scanner)
  4. Векторный поиск по коллекции протоколов в ChromaDB (top-K чанков)
  5. LLM классифицирует каждый чанк: положительное / отрицательное / нейтральное
  6. Агрегация по файлам (1 файл = 1 голос, по большинству чанков)
  7. Результат: счётчик за/против/нейтр + цитаты со свёрнутыми источниками
  8. Сохранение в реестр прогнозов (data/predictor/registry_NNNN.jsonl)
──────────────────────────────────────────────────────────────────────────────
"""
import io
import json
import os
import re
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st

# =============================================================================
# Константы
# =============================================================================
_BASE_DIR        = os.path.join("data", "predictor")
_REGISTRY_DIR    = _BASE_DIR
_MAX_PER_FILE    = 1000
_CHROMA_DIR      = os.path.join("data", "vector_db")
_PROTOCOLS_COLLECTION = "protocols"
_EXPERTISE_COLLECTION = "expertise_docs"

_LARGE_DOC_THRESHOLD = 12_000
_CHUNK_SIZE          = 6_000
_CHUNK_OVERLAP       = 300

_DEFAULT_TOP_K = 30
_RAG_CONTEXT_CHAR_BUDGET = 35_000  # суммарный лимит символов по всем найденным чанкам перед классификацией

_REGISTRY_LOCK = threading.Lock()


# =============================================================================
# Загрузка настроек прогнозиста из Админки
# =============================================================================
_PRED_CFG_FILE = os.path.join("config", "predictor_config.json")
_PRED_CFG_DEFAULTS = {
    "chunk_chars_to_llm":  1500,
    "justification_chars": 200,
    "classify_max_tokens": 350,
    "default_top_k":       30,
    "disable_thinking":    True,
}


def load_predictor_config() -> dict:
    """Читает config/predictor_config.json, возвращает merged с defaults."""
    if os.path.exists(_PRED_CFG_FILE):
        try:
            with open(_PRED_CFG_FILE, "r", encoding="utf-8") as f:
                return {**_PRED_CFG_DEFAULTS, **json.load(f)}
        except Exception:
            pass
    return dict(_PRED_CFG_DEFAULTS)


_PROMPTS_FILE = os.path.join("config", "prompts.json")
_PRED_PROMPT_DEFAULTS = {
    "predictor_classify_system": (
        "Ты — тарифный эксперт РФ. Тебе нужно определить, ПОДТВЕРЖДАЕТ или "
        "ОПРОВЕРГАЕТ найденный прецедент МЕТОДОЛОГИЮ/ПРИНЦИП пользователя — "
        "а не тему статьи затрат и не числовой результат (снижено/повышено). "
        "Сравнивай ИМЕННО ПОДХОД: каким способом регулятор определяет "
        "значение. Совпадение ОБЩЕЙ ТЕМЫ подхода (например, оба случая "
        "касаются 'срока полезного использования' или 'выбора варианта из "
        "диапазона') ещё не значит совпадение позиции — если регулятор и "
        "пользователь выбирают ПРОТИВОПОЛОЖНЫЕ варианты внутри этой темы "
        "(например, один настаивает на максимальном значении, другой — на "
        "минимальном; один — на фактических данных, другой — на нормативе), "
        "это ПРОТИВОРЕЧИЕ (negative), а не совпадение. Числовой результат "
        "(сумма выросла или снизилась) сам по себе не определяет "
        "positive/negative — важно, выбрал ли регулятор ТОТ ЖЕ вариант "
        "решения, что и пользователь, или ПРОТИВОПОЛОЖНЫЙ.\n\n"
        "КРИТИЧЕСКИ ВАЖНО: в материалах ДВА РАЗНЫХ ИСТОЧНИКА текста — "
        "позиция ТЕКУЩЕГО пользователя (помечена '=== ПОЗИЦИЯ ТЕКУЩЕГО "
        "ПОЛЬЗОВАТЕЛЯ ===') и решение регулятора по ДРУГОЙ организации из "
        "прецедента (помечено '=== РЕШЕНИЕ ИЗ ПРЕЦЕДЕНТА ==='). Внутри "
        "блока прецедента может быть фраза 'заявлено предприятием X тыс. "
        "руб.' — это позиция ДРУГОЙ организации из прецедента, а НЕ "
        "текущего пользователя. Не путай их.\n\n"
        "ФОРМАТ ОТВЕТА — СТРОГО ВАЖНО: ты должен выдать ТОЛЬКО готовый "
        "финальный результат сравнения, БЕЗ цепочки рассуждений, БЕЗ "
        "цитирования инструкции, БЕЗ слов 'перечитаем', 'однако', 'но "
        "инструкция гласит', БЕЗ промежуточных вопросов самому себе "
        "(например 'значит decision должен быть X?'). Сравнение "
        "методологии должно происходить у тебя ДО генерации ответа, "
        "а не внутри текста поля reason. Поле reason — это ИТОГ "
        "сравнения в одном утвердительном предложении (до 120 символов), "
        "а не процесс рассуждения. decision должен точно соответствовать "
        "этому итоговому reason. Отвечай только JSON, без какого-либо "
        "текста до или после него."
    ),
    "predictor_classify_user": (
        "СТАТЬЯ ЗАТРАТ: {article_name}\n"
        "{justification_line}"
        "\n"
        "НАЙДЕННЫЙ ПРЕЦЕДЕНТ:\n{chunk}\n\n"
        "ЗАДАЧА: определи, КАКОЙ ИМЕННО ВАРИАНТ/ПОДХОД выбрал регулятор в "
        "блоке '=== РЕШЕНИЕ ИЗ ПРЕЦЕДЕНТА ===' (не саму цифру и не итог "
        "'больше/меньше', а суть решения — например 'применил максимальный "
        "срок', 'применил минимальный срок', 'учёл фактические расходы', "
        "'применил норматив вместо факта') и сравни этот ВЫБОР с тем, что "
        "заявляет пользователь в блоке '=== ПОЗИЦИЯ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ "
        "==='.\n\n"
        "Определи decision:\n"
        "- positive — регулятор выбрал ТОТ ЖЕ вариант/подход, что заявляет "
        "пользователь (например, оба настаивают на максимальном сроке, оба "
        "— на минимальном, оба — на учёте фактических расходов, оба — на "
        "применении норматива). Засчитывай как positive, ДАЖЕ ЕСЛИ в "
        "прецеденте этот выбор привёл к снижению суммы у той организации — "
        "важно совпадение выбранного варианта, а не итоговое движение "
        "цифры\n"
        "- negative — регулятор выбрал ДРУГОЙ или ПРОТИВОПОЛОЖНЫЙ вариант "
        "внутри той же темы (например, пользователь настаивает на "
        "минимальном сроке, а регулятор в прецеденте применяет максимальный "
        "— это противоположные значения одного и того же параметра, не "
        "совпадение; или пользователь просит фактические расходы, а "
        "регулятор применяет норматив)\n"
        "- neutral — в прецеденте нет решения по этой статье, ИЛИ "
        "невозможно определить выбранный вариант регулятора из фрагмента, "
        "ИЛИ тема прецедента не связана по существу с тем, что заявляет "
        "пользователь\n\n"
        "ВАЖНО: 'максимальный' и 'минимальный' (как и 'факт' и 'норматив', "
        "'включить' и 'исключить') — это ПРОТИВОПОЛОЖНЫЕ варианты одной "
        "темы. Если ОБА (и регулятор, и пользователь) выбрали ОДИНАКОВЫЙ "
        "вариант (например, оба — 'норматив', оба — 'максимальный срок') — "
        "это ВСЕГДА positive, без исключений и без дополнительных "
        "рассуждений. Не путай тематическое совпадение (оба текста "
        "говорят о 'сроке полезного использования') с совпадением позиции "
        "(выбран ли тот же конкретный вариант) — но если конкретный "
        "вариант СОВПАЛ, сомнений быть не должно. Не путай 'заявлено "
        "предприятием' внутри блока ПРЕЦЕДЕНТА (это другая организация) с "
        "позицией ТЕКУЩЕГО пользователя. Конкретные числа (года, проценты, "
        "суммы) сравнивать не нужно — сравнивай только то, какой "
        "вариант/подход выбран.\n\n"
        "Ответь СРАЗУ готовым JSON без рассуждений, без вопросов самому "
        "себе, без цитирования этой инструкции в ответе:\n"
        'JSON: {{"decision":"positive|negative|neutral","quote":"цитата, какой вариант выбрал регулятор, до 120 симв.","reason":"краткий итоговый вывод одним предложением: совпадают варианты или нет, до 120 симв."}}'
    ),
}




def load_predictor_prompts() -> dict:
    """Читает промпты прогнозиста из config/prompts.json."""
    if os.path.exists(_PROMPTS_FILE):
        try:
            with open(_PROMPTS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**_PRED_PROMPT_DEFAULTS,
                    **{k: saved[k] for k in _PRED_PROMPT_DEFAULTS if k in saved}}
        except Exception:
            pass
    return dict(_PRED_PROMPT_DEFAULTS)


# =============================================================================
# Утилиты реестра (jsonl, ротация по 1000 записей)
# =============================================================================
def _ensure_dirs():
    os.makedirs(_REGISTRY_DIR, exist_ok=True)


def _current_registry_path() -> str:
    """Возвращает путь к активному файлу реестра, создаёт новый при переполнении."""
    _ensure_dirs()
    idx = 1
    while True:
        path = os.path.join(_REGISTRY_DIR, f"registry_{idx:04d}.jsonl")
        if not os.path.exists(path):
            return path
        # Считаем строки
        with open(path, "r", encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
        if count < _MAX_PER_FILE:
            return path
        idx += 1


def save_to_registry(record: Dict):
    """Дозаписывает запись в активный файл реестра."""
    _ensure_dirs()
    path = _current_registry_path()
    with _REGISTRY_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_registry(max_records: int = 200) -> List[Dict]:
    """Загружает последние max_records записей из всех файлов реестра."""
    _ensure_dirs()
    all_records = []
    files = sorted(
        [f for f in os.listdir(_REGISTRY_DIR) if f.startswith("registry_") and f.endswith(".jsonl")],
        reverse=True,
    )
    for fname in files:
        path = os.path.join(_REGISTRY_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            for line in reversed(lines):
                try:
                    all_records.append(json.loads(line))
                except Exception:
                    pass
            if len(all_records) >= max_records:
                break
        except Exception:
            pass
    return all_records[:max_records]


# =============================================================================
# LM Studio — переиспользуем логику doc_scanner
# =============================================================================
def _load_lm_config() -> Tuple[str, str]:
    config_path = os.path.join("config", "advisor_config.json")
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    lm_url = config.get("lm_studio_url", "http://127.0.0.1:1234/v1")
    model  = config.get("default_model", "qwen/qwen3.5-9b")
    return lm_url, model


def _strip_thinking(text: str) -> str:
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()


def _lm_call(client, model: str, system: str, user: str, max_tokens: int = 600) -> str:
    cfg = load_predictor_config()
    _disable_thinking = bool(cfg.get("disable_thinking", True))
    _kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
        temperature=0.1,
    )
    # cache_prompt: false — запрещаем llama.cpp кэшировать KV между запросами.
    # Без этого после 5-10 последовательных вызовов слот переполняется,
    # llama.cpp перестраивает кэш и часть вычислений падает на CPU.
    extra = {"cache_prompt": False}
    if _disable_thinking:
        extra["thinking"] = {"type": "disabled"}
    _kwargs["extra_body"] = extra
    try:
        resp = client.chat.completions.create(**_kwargs)
        raw = (resp.choices[0].message.content or "").strip()
        return _strip_thinking(raw)
    except Exception:
        # Fallback без extra_body если модель не поддерживает параметр
        _kwargs.pop("extra_body", None)
        try:
            resp = client.chat.completions.create(**_kwargs)
            raw = (resp.choices[0].message.content or "").strip()
            return _strip_thinking(raw)
        except Exception as e2:
            return f"[Ошибка LM: {e2}]"


def _split_text_chunks(text: str) -> List[str]:
    """Разбивает текст на чанки по границам абзацев (из doc_scanner)."""
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
        min_pos = start + _CHUNK_SIZE * 3 // 4
        split_end = end
        for sep in ('\n\n', '. ', ' '):
            pos = text.rfind(sep, min_pos, end)
            if pos != -1:
                split_end = pos + len(sep)
                break
        chunk = text[start:split_end].strip()
        if chunk:
            chunks.append(chunk)
        next_start = split_end - _CHUNK_OVERLAP
        start = max(start + _CHUNK_SIZE // 2, next_start)
    return chunks


def compress_document(text: str, article_name: str, _progress_cb=None) -> str:
    """
    Сжимает текст документа-обоснования до краткого резюме через Map-Reduce.
    Акцентирует на связи с конкретной статьёй затрат.
    """
    if not text.strip():
        return ""
    try:
        from openai import OpenAI
        lm_url, model = _load_lm_config()
        client = OpenAI(base_url=lm_url, api_key="lm-studio", timeout=180.0)
        system_msg = (
            "Ты эксперт по тарифному регулированию. "
            "Кратко извлекай только факты, суммы и показатели, "
            "относящиеся к статье затрат."
        )

        if len(text) <= _LARGE_DOC_THRESHOLD:
            prompt = (
                f"Извлеки ключевые факты из документа, относящиеся к статье затрат "
                f'«{article_name}»: суммы, обоснования, нормативы, методику расчёта. '
                f"Ответь кратко, 3-7 пунктов. Без вступлений.\n\nДОКУМЕНТ:\n{text}"
            )
            return _lm_call(client, model, system_msg, prompt, max_tokens=500)

        # Map-Reduce для большого документа
        chunks = _split_text_chunks(text)
        total  = len(chunks)
        mini_summaries = []
        for i, chunk in enumerate(chunks, 1):
            if _progress_cb:
                _progress_cb((i - 1) / (total + 1), f"Сжатие документа: часть {i}/{total}…")
            map_prompt = (
                f"Часть {i} из {total} документа-обоснования.\n"
                f'Извлеки только факты, относящиеся к статье затрат «{article_name}»: '
                f"суммы, нормативы, методику. Если нет — напиши «нет данных».\n\n"
                f"ЧАСТЬ:\n{chunk}"
            )
            mini = _lm_call(client, model, system_msg, map_prompt, max_tokens=300)
            mini_summaries.append(f"=== Часть {i}/{total} ===\n{mini}")

        if _progress_cb:
            _progress_cb(total / (total + 1), "Финальный синтез…")

        combined = "\n\n".join(mini_summaries)
        reduce_prompt = (
            f"Ниже — резюме частей документа. Создай единое краткое резюме (5-10 пунктов) "
            f'по статье затрат «{article_name}». Сохрани все суммы и нормативы.\n\n'
            f"РЕЗЮМЕ ЧАСТЕЙ:\n{combined}"
        )
        return _lm_call(client, model, system_msg, reduce_prompt, max_tokens=600)

    except Exception as e:
        return f"[Ошибка сжатия: {e}]"


# =============================================================================
# Извлечение текста из загруженного файла (переиспользуем doc_scanner)
# =============================================================================
def extract_file_text(file_bytes: bytes, filename: str) -> str:
    """Извлекает текст из файла через логику doc_scanner."""
    try:
        from streamlit_pages.doc_scanner import extract_text
        pages = extract_text(file_bytes, filename)
        return "\n".join(p.get("text", "") for p in pages if p.get("text"))
    except Exception as e:
        return f"[Ошибка извлечения текста: {e}]"


# =============================================================================
# ChromaDB — поиск по коллекциям протоколов / экспертных заключений
# =============================================================================
def _get_chroma_collection(collection_name: str):
    """
    Возвращает коллекцию ChromaDB по имени (protocols | expertise_docs).
    Использует ту же E5EmbeddingFunction что и indexer.py —
    без этого query-векторы несовместимы с индексированными passage-векторами.
    """
    try:
        from core.indexer import _get_chroma_client
        client = _get_chroma_client()

        # expertise_docs создаётся через core/expertise_chunker.py, который
        # оборачивает embedding function в совместимую с разными версиями
        # ChromaDB обёртку (см. get_chroma_embedding_function). Для protocols
        # используем embedding function как раньше, напрямую из indexer.py.
        if collection_name == _EXPERTISE_COLLECTION:
            from core.expertise_chunker import get_chroma_embedding_function
            ef = get_chroma_embedding_function()
        else:
            from core.indexer import get_embedding_function
            ef = get_embedding_function()

        try:
            return client.get_collection(name=collection_name, embedding_function=ef)
        except Exception:
            return None
    except Exception as e:
        print(f"[PREDICTOR] Ошибка подключения к ChromaDB ({collection_name}): {e}")
        return None


def _build_where_clause(filters: Optional[Dict]) -> dict:
    """
    Собирает ChromaDB `where`-выражение из словаря фильтров.
    Поддерживаемые ключи: spheres, regions, years, methods — каждый список
    строк (без эмодзи), $in для нескольких значений, $eq для одного.
    """
    if not filters:
        return {}

    clauses = []

    def _add_clause(field: str, values_key: str, legacy_key: str = None):
        values = filters.get(values_key, filters.get(legacy_key, []) if legacy_key else [])
        if isinstance(values, str) and values:
            values = [values]
        if values:
            if len(values) == 1:
                clauses.append({field: {"$eq": values[0]}})
            else:
                clauses.append({field: {"$in": values}})

    _add_clause("sphere", "spheres")
    _add_clause("region", "regions", "region")
    _add_clause("year", "years")
    _add_clause("method", "methods")

    if len(clauses) == 1:
        return clauses[0]
    elif len(clauses) > 1:
        return {"$and": clauses}
    return {}


def search_documents(query: str, top_k: int = _DEFAULT_TOP_K,
                      filters: Optional[Dict] = None,
                      sources: Optional[List[str]] = None) -> List[Dict]:
    """
    Векторный поиск по одной или нескольким коллекциям (protocols,
    expertise_docs). `sources` — список из {"protocols", "expertise"};
    по умолчанию — только expertise.

    ВАЖНО: при поиске и в protocols, и в expertise одновременно, фильтры
    year/method применяются ТОЛЬКО к expertise_docs — коллекция protocols
    исторически индексировалась без этих полей в metadata (старые чанки их
    не содержат), поэтому year/method-фильтр на ней просто не будет давать
    результатов. Если в фильтрах заданы years/methods, а источник включает
    protocols, year/method для protocols-части запроса не передаются.
    """
    if sources is None:
        sources = ["expertise"]

    where = _build_where_clause(filters)
    has_year_or_method = bool(filters and (filters.get("years") or filters.get("methods")))

    all_chunks: List[Dict] = []

    if "expertise" in sources:
        collection = _get_chroma_collection(_EXPERTISE_COLLECTION)
        if collection is not None:
            try:
                kwargs = dict(query_texts=[query], n_results=top_k,
                              include=["documents", "metadatas", "distances"])
                if where:
                    kwargs["where"] = where
                results = collection.query(**kwargs)
                docs      = results.get("documents", [[]])[0]
                metas     = results.get("metadatas",  [[]])[0]
                distances = results.get("distances",  [[]])[0]
                for doc, meta, dist in zip(docs, metas, distances):
                    all_chunks.append({
                        "text":          doc,
                        "file":          meta.get("filename", ""),
                        "date":          meta.get("protocol_date", ""),
                        "sphere":        meta.get("sphere", ""),
                        "region":        meta.get("region", ""),
                        "year":          meta.get("year", ""),
                        "method":        meta.get("method", ""),
                        "organization":  meta.get("organization", ""),
                        "section":       meta.get("section", ""),
                        "source":        "expertise",
                        "distance":      dist,
                    })
            except Exception as e:
                print(f"[PREDICTOR] Ошибка поиска в expertise_docs: {e}")

    if "protocols" in sources:
        # При комбинированном поиске year/method не применяются к protocols
        # (исторические данные без этих полей в metadata) — собираем
        # отдельный where без year/method для этой коллекции.
        protocols_filters = dict(filters or {})
        protocols_filters.pop("years", None)
        protocols_filters.pop("methods", None)
        protocols_where = _build_where_clause(protocols_filters)

        collection = _get_chroma_collection(_PROTOCOLS_COLLECTION)
        if collection is not None:
            try:
                kwargs = dict(query_texts=[query], n_results=top_k,
                              include=["documents", "metadatas", "distances"])
                if protocols_where:
                    kwargs["where"] = protocols_where
                results = collection.query(**kwargs)
                docs      = results.get("documents", [[]])[0]
                metas     = results.get("metadatas",  [[]])[0]
                distances = results.get("distances",  [[]])[0]
                for doc, meta, dist in zip(docs, metas, distances):
                    all_chunks.append({
                        "text":         doc,
                        "file":         meta.get("file", ""),
                        "date":         meta.get("date", ""),
                        "sphere":       meta.get("sphere", ""),
                        "region":       meta.get("region", ""),
                        "year":         "",   # отсутствует в исторических metadata
                        "method":       "",   # отсутствует в исторических metadata
                        "organization": meta.get("organization", ""),
                        "section":      "",
                        "source":       "protocols",
                        "distance":     dist,
                    })
            except Exception as e:
                print(f"[PREDICTOR] Ошибка поиска в protocols: {e}")

    # Сортируем общий пул по близости (меньше distance = релевантнее) и
    # обрезаем до top_k — иначе при двух источниках результатов будет до 2×top_k.
    all_chunks.sort(key=lambda c: c.get("distance", 1.0))
    return all_chunks[:top_k]


def search_protocols(query: str, top_k: int = _DEFAULT_TOP_K,
                     filters: Optional[Dict] = None) -> List[Dict]:
    """
    DEPRECATED: оставлена для обратной совместимости с любым внешним кодом,
    который мог импортировать именно эту функцию. Эквивалентна
    search_documents(..., sources=["expertise"]).
    """
    return search_documents(query, top_k=top_k, filters=filters, sources=["expertise"])


# =============================================================================
# Гибридный поиск (BM25 + векторный + CrossEncoder reranking) для expertise_docs
#
# Переиспользует инфраструктуру core/advisor.py (HybridRetriever, Reranker) —
# тот же подход, что и в Советчике для НПА — вместо чистого векторного
# cosine-similarity поиска, который на коротких запросах (название статьи
# затрат) даёт заметно более слабые результаты.
#
# ВАЖНО: создаётся ОТДЕЛЬНЫЙ синглтон HybridRetriever для expertise_docs —
# не путать с тем, что использует Советчик для tariff_docs (НПА). Coллекция
# передаётся в конструктор HybridRetriever явно.
# =============================================================================
_EXPERTISE_HYBRID_RETRIEVER_KEY = "__regula_ai_expertise_hybrid_retriever__"


def _get_expertise_hybrid_retriever():
    """
    Синглтон HybridRetriever для коллекции expertise_docs. Использует
    класс HybridRetriever из core/advisor.py напрямую (тот же BM25 + RRF
    fusion алгоритм), но со своим кэшем и своей коллекцией — изолирован
    от ретривера НПА.
    """
    import sys
    try:
        from core.advisor import HybridRetriever, BM25_AVAILABLE
    except Exception as e:
        print(f"[PREDICTOR] core.advisor недоступен для гибридного поиска: {e}")
        return None

    if not BM25_AVAILABLE:
        return None

    collection = _get_chroma_collection(_EXPERTISE_COLLECTION)
    if collection is None:
        return None

    try:
        current_count = collection.count()
    except Exception:
        current_count = -1

    existing = sys.modules.get(_EXPERTISE_HYBRID_RETRIEVER_KEY)
    if existing is not None and getattr(existing, "_collection_count", -1) == current_count:
        return existing

    try:
        retriever = HybridRetriever(collection)
        retriever._collection_count = current_count
        sys.modules[_EXPERTISE_HYBRID_RETRIEVER_KEY] = retriever
        return retriever
    except Exception as e:
        print(f"[PREDICTOR] Не удалось построить HybridRetriever для expertise_docs: {e}")
        return None


def invalidate_expertise_hybrid_retriever():
    """Сбрасывает BM25-индекс expertise_docs — вызывать после переиндексации."""
    import sys
    sys.modules.pop(_EXPERTISE_HYBRID_RETRIEVER_KEY, None)


def _filter_candidates_by_where(candidates: List[Dict], filters: Optional[Dict]) -> List[Dict]:
    """
    HybridRetriever.search() не поддерживает ChromaDB `where` напрямую
    (BM25-часть ищет по всем чанкам в памяти), поэтому фильтрацию по
    sphere/region/year/method применяем после получения кандидатов —
    на полном пуле документов это недорого (тысячи, не миллионы записей).
    """
    if not filters:
        return candidates

    def _matches(meta: dict) -> bool:
        for key, field in [("spheres", "sphere"), ("regions", "region"),
                            ("years", "year"), ("methods", "method")]:
            values = filters.get(key)
            if values and meta.get(field) not in values:
                return False
        return True

    return [c for c in candidates if _matches(c.get("meta", {}))]


def search_documents_hybrid(
    query: str,
    article_name: str = "",
    top_k: int = _DEFAULT_TOP_K,
    filters: Optional[Dict] = None,
    retrieval_pool: int = 60,
) -> List[Dict]:
    """
    Гибридный поиск по expertise_docs: BM25 + векторный (RRF fusion) →
    CrossEncoder reranking относительно `article_name` (или `query`, если
    article_name не передан) → top_k финальных результатов.

    `retrieval_pool` — сколько кандидатов берёт HybridRetriever ДО
    реранкинга (с запасом, т.к. фильтры sphere/region/year/method и
    последующий реранкинг могут уменьшить пул).

    При недоступности BM25/reranker — мягкий fallback на обычный
    векторный поиск (search_documents).
    """
    retriever = _get_expertise_hybrid_retriever()
    if retriever is None:
        print("[PREDICTOR] Гибридный поиск недоступен, fallback на векторный поиск.")
        return search_documents(query, top_k=top_k, filters=filters, sources=["expertise"])

    candidates = retriever.search(query, top_k=retrieval_pool)
    candidates = _filter_candidates_by_where(candidates, filters)

    if not candidates:
        return []

    try:
        from core.advisor import get_reranker
        reranker = get_reranker()
    except Exception:
        reranker = None

    rerank_query = article_name.strip() if article_name and article_name.strip() else query

    if reranker is not None:
        reranked = reranker.rerank(rerank_query, candidates, top_n=top_k)
    else:
        # Без реранкера — берём по RRF-score (уже отсортированы HybridRetriever.search)
        reranked = candidates[:top_k]

    results: List[Dict] = []
    for c in reranked:
        meta = c.get("meta", {})
        results.append({
            "text":          c.get("doc", ""),
            "file":          meta.get("filename", ""),
            "date":          meta.get("protocol_date", ""),
            "sphere":        meta.get("sphere", ""),
            "region":        meta.get("region", ""),
            "year":          meta.get("year", ""),
            "method":        meta.get("method", ""),
            "organization":  meta.get("organization", ""),
            "section":       meta.get("section", ""),
            "tag":           meta.get("tag", ""),
            "source":        "expertise",
            "rerank_score":  c.get("rerank_score"),
            "distance":      1.0 - (c.get("rerank_score") or 0) if c.get("rerank_score") is not None else c.get("score", 1.0),
        })
    return results


def _truncate_chunks_by_char_budget(
    chunks: List[Dict], budget: int = _RAG_CONTEXT_CHAR_BUDGET,
) -> Tuple[List[Dict], int]:
    """
    Обрезает список чанков (уже отсортированных по релевантности) так,
    чтобы суммарная длина их текста не превышала `budget` символов.
    Берём чанки по порядку, пока сумма не превысит лимит — остальные
    отбрасываем целиком (не дробим текст внутри чанка).

    Защищает от случаев, когда большое top_k (например 30) с длинными
    чанками создаёт огромный совокупный контекст для последовательной
    LLM-классификации — это и замедляет генерацию (особенно при
    включённом Unified KV Cache в LM Studio, который накапливает
    контекст между последовательными вызовами).

    Возвращает (обрезанный_список, отброшено_чанков).
    """
    kept: List[Dict] = []
    total_chars = 0
    for chunk in chunks:
        chunk_len = len(chunk.get("text", "") or "")
        if kept and total_chars + chunk_len > budget:
            break
        kept.append(chunk)
        total_chars += chunk_len
    dropped = len(chunks) - len(kept)
    return kept, dropped


# =============================================================================
# Классификация чанка через LLM
# =============================================================================

_DECISION_FIELDS_RE = re.compile(
    r'(?:[*•]\s*)?\*{0,2}Заявлено(?:\s+предприятием)?\*{0,2}\s*:?\s*\*{0,2}\s*(?P<claimed>[^\n\r]+)|'
    r'(?:[*•]\s*)?\*{0,2}Принято(?:\s+экспертами)?\*{0,2}\s*:?\s*\*{0,2}\s*(?P<accepted>[^\n\r]+)|'
    r'(?:[*•]\s*)?\*{0,2}Корректировка\*{0,2}\s*(?:\+/-)?\s*:?\s*\*{0,2}\s*(?P<adjustment>[^\n\r]+)|'
    r'(?:[*•]\s*)?\*{0,2}ОБОСНОВАНИЕ\*{0,2}\s*:?\s*\*{0,2}\s*(?P<rationale>[^\n\r]+)',
    re.IGNORECASE,
)


def extract_decision_fields(chunk_text: str) -> Dict[str, str]:
    """
    Извлекает структурированные поля "Заявлено / Принято / Корректировка /
    ОБОСНОВАНИЕ" из текста чанка (формат экспертных заключений). Эти поля
    содержат ГОТОВОЕ решение регулятора по статье — не нужно угадывать
    тон текста, решение уже есть в цифрах.

    Формат варьируется между документами (с markdown-bold, со
    звёздочками/буллетами, с разбивкой по годам), поэтому используется
    гибкая регулярка без строгой привязки к одной форме записи. Если
    поля не найдены — возвращает пустой словарь (не пытаемся "придумать"
    структуру там, где её нет в тексте).
    """
    found: Dict[str, str] = {}
    for m in _DECISION_FIELDS_RE.finditer(chunk_text):
        for key in ("claimed", "accepted", "adjustment", "rationale"):
            val = m.group(key)
            if val and key not in found:
                val = val.strip().rstrip(".,;").strip("*").strip()
                found[key] = val
    return found


# Маркеры явного согласия/несогласия в тексте reason — используются для
# программной проверки согласованности с decision (модель иногда пишет
# текстом верный вывод "совпадение позиции", но ставит decision=negative
# по инерции — это противоречие, и оно перебивает decision модели,
# поскольку текстовый вывод reason надёжнее одного отдельного поля).
_REASON_AGREE_RE = re.compile(
    r'совпадени[ея]\s+(позици|подход|вариант|метод)|'
    r'(тот\s+же|такой\s+же|один\s+и\s+тот\s+же)\s+(вариант|подход|принцип|срок|метод)|'
    r'совпадает\s+с\s+позицией|регулятор\s+(также|тоже)\s+(выбрал|применил|использовал)',
    re.IGNORECASE,
)
_REASON_DISAGREE_RE = re.compile(
    r'противоречи[ея]|не\s+совпадает|противоположн|расхожд|'
    r'(другой|иной)\s+(вариант|подход|принцип)|'
    r'регулятор\s+(не\s+согласен|отклонил|отказал)',
    re.IGNORECASE,
)


def _reconcile_decision_with_reason(decision: str, reason: str) -> tuple[str, bool]:
    """
    Программная проверка согласованности decision с текстом reason.
    Модель иногда формулирует в reason явный и корректный вывод
    ("это совпадение позиции"), но всё равно выставляет противоречащий
    decision (например negative). Полагаться на то, что модель сама себя
    проверит в один проход, ненадёжно — поэтому здесь явный пост-фактум
    разбор reason по маркерам согласия/несогласия.

    При обнаруженном противоречии текстовый вывод reason считается более
    надёжным сигналом (модель его явно сформулировала словами) и
    перебивает decision. Возвращает (итоговый_decision, был_ли_исправлен).
    """
    agree   = bool(_REASON_AGREE_RE.search(reason))
    disagree = bool(_REASON_DISAGREE_RE.search(reason))

    # Однозначный сигнал согласия в reason, а decision говорит об обратном
    if agree and not disagree and decision == "negative":
        return "positive", True
    # Однозначный сигнал несогласия в reason, а decision говорит о позитиве
    if disagree and not agree and decision == "positive":
        return "negative", True

    return decision, False


def _verify_regulator_choice_vs_user_position(
    quote: str, justification: str, client, model: str,
) -> tuple[bool, str]:
    """
    ВТОРОЙ ЭТАП проверки (отдельный, узкий LLM-вызов) — ПЕРЕРАБОТАННАЯ ВЕРСИЯ.

    Прежняя версия сравнивала quote с reason первого этапа — но reason сам
    может быть сформулирован размыто/двусмысленно ("совпадает с позицией
    пользователя о фактических потерях в контексте применения
    нормативного подхода" — формально не повторяет противоречащее слово
    напрямую, поэтому узкая текстовая сверка quote↔reason такое
    пропускает).

    Новая версия убирает reason как посредника: верификатор САМ читает
    quote (что реально выбрал регулятор) и justification (позицию
    текущего пользователя), и САМ определяет совпадение или
    противоречие — независимо от того, как это сформулировал первый
    этап. Это устраняет риск унаследовать путаную формулировку.

    Возвращает (is_consistent, verification_note).
    Если проверка сама не удалась технически — возвращает (True, "") —
    не блокирует результат первого этапа при сбое самой проверки.
    """
    if not quote or not justification:
        return True, ""

    system_prompt = (
        "Ты определяешь, какой конкретный вариант/подход выбран в двух "
        "текстах, и совпадают ли эти варианты. Отвечай только JSON, без "
        "рассуждений."
    )
    user_prompt = (
        f"ТЕКСТ 1 (решение регулятора, цитата из документа): {quote}\n"
        f"ТЕКСТ 2 (позиция пользователя): {justification}\n\n"
        f"Шаг 1: определи, какой конкретный вариант/подход выбран в "
        f"ТЕКСТЕ 1 (например 'норматив', 'фактические показатели', "
        f"'максимальный срок', 'минимальный срок' — конкретное значение, "
        f"а не общая тема).\n"
        f"Шаг 2: определи, какой конкретный вариант/подход заявлен в "
        f"ТЕКСТЕ 2.\n"
        f"Шаг 3: сравни — это ОДИН И ТОТ ЖЕ вариант, или ПРОТИВОПОЛОЖНЫЕ "
        f"варианты внутри одной темы (например 'норматив' и "
        f"'фактические показатели' — противоположны; 'максимальный' и "
        f"'минимальный' — противоположны)?\n\n"
        'JSON: {{"same_choice": true|false, "regulator_choice": "вариант из ТЕКСТА 1, до 40 симв.", "user_choice": "вариант из ТЕКСТА 2, до 40 симв."}}'
    )

    print(f"[VERIFY] Независимая проверка: quote='{quote[:60]}...' justification='{justification[:60]}...'", flush=True)
    try:
        raw = _lm_call(client, model, system_prompt, user_prompt, max_tokens=150)
        if raw.startswith("[Ошибка LM:"):
            print(f"[VERIFY] LM-вызов завершился ошибкой: {raw}", flush=True)
            return True, ""
        clean = re.sub(r'```json|```', '', raw).strip()
        data = json.loads(clean)
        same_choice = data.get("same_choice")
        if same_choice is None:
            # Модель не дала однозначный ответ — не блокируем
            return True, ""
        reg_choice = data.get("regulator_choice", "")
        user_choice = data.get("user_choice", "")
        note = f"регулятор: «{reg_choice}», пользователь: «{user_choice}»"
        print(f"[VERIFY] Независимый результат: same_choice={same_choice} ({note})", flush=True)
        return bool(same_choice), note
    except Exception as e:
        print(f"[VERIFY] Сбой независимой проверки (raw='{raw if 'raw' in dir() else '?'}'): {e}", flush=True)
        return True, ""


def classify_chunk(chunk_text: str, article_name: str, justification_summary: str,
                   client, model: str) -> Dict:
    """
    Классифицирует один чанк протокола.
    Возвращает: {"decision": "positive"|"negative"|"neutral", "quote": str, "reason": str}
    Параметры читаются из config/predictor_config.json,
    промпты — из config/prompts.json (настраиваются в Админке).
    """
    cfg     = load_predictor_config()
    prompts = load_predictor_prompts()

    _chunk_chars   = int(cfg["chunk_chars_to_llm"])
    _justify_chars = int(cfg["justification_chars"])
    _max_tokens    = int(cfg["classify_max_tokens"])

    _chunk   = chunk_text[:_chunk_chars]

    # Извлекаем структурированное решение (Заявлено/Принято/Корректировка/
    # ОБОСНОВАНИЕ), если оно присутствует, и явно подсвечиваем его перед
    # текстом чанка — чтобы модель опиралась на цифры решения, а не
    # угадывала тональность по формулировкам.
    #
    # ВАЖНО: и "Заявлено предприятием" из decision_fields (то, что просила
    # ДРУГАЯ организация в прецеденте), и "Обоснование решения" из
    # decision_fields (причина регулятора по ТОЙ организации) — это текст
    # из найденного прецедента, не имеющий отношения к текущему
    # пользователю. Чтобы модель не путала это с позицией ТЕКУЩЕГО
    # пользователя (justification_line), оборачиваем оба источника в явные
    # блочные метки.
    decision_fields = extract_decision_fields(_chunk)
    if decision_fields:
        _hint_lines = ["[Структурированное решение по статье из ПРЕЦЕДЕНТА (другая организация, не текущий пользователь):]"]
        if "claimed" in decision_fields:
            _hint_lines.append(f"В прецеденте заявлено той организацией: {decision_fields['claimed']}")
        if "accepted" in decision_fields:
            _hint_lines.append(f"В прецеденте принято экспертами/регулятором: {decision_fields['accepted']}")
        if "adjustment" in decision_fields:
            _hint_lines.append(f"Корректировка в прецеденте: {decision_fields['adjustment']}")
        if "rationale" in decision_fields:
            _hint_lines.append(f"Причина решения регулятора в прецеденте: {decision_fields['rationale']}")
        _chunk = "\n".join(_hint_lines) + "\n\n" + _chunk

    _justify = justification_summary[:_justify_chars] if justification_summary and _justify_chars > 0 else ""
    _justify_line = (
        f"=== ПОЗИЦИЯ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ (то, что он обосновывает сейчас) ===\n"
        f"{_justify}\n"
        f"=== КОНЕЦ ПОЗИЦИИ ПОЛЬЗОВАТЕЛЯ ===\n"
    ) if _justify else ""

    _chunk = (
        f"=== РЕШЕНИЕ ИЗ ПРЕЦЕДЕНТА (другая организация, другой случай) ===\n"
        f"{_chunk}\n"
        f"=== КОНЕЦ РЕШЕНИЯ ИЗ ПРЕЦЕДЕНТА ==="
    )

    system_prompt = prompts["predictor_classify_system"]
    user_template = prompts["predictor_classify_user"]

    prompt = (
        user_template
        .replace("{article_name}",      article_name)
        .replace("{justification_line}", _justify_line)
        .replace("{chunk}",             _chunk)
    )
    raw = _lm_call(client, model, system_prompt, prompt, max_tokens=_max_tokens)
    # Парсим JSON
    try:
        # Убираем возможные обёртки ```json
        clean = re.sub(r'```json|```', '', raw).strip()
        data = json.loads(clean)
        decision = data.get("decision", "neutral")
        if decision not in ("positive", "negative", "neutral"):
            decision = "neutral"
        reason = data.get("reason", "")
        quote  = data.get("quote", chunk_text[:150])
        decision, _was_fixed = _reconcile_decision_with_reason(decision, reason)

        # ВТОРОЙ ЭТАП: НЕЗАВИСИМАЯ проверка quote vs justification_summary
        # (позиция пользователя), без посредника reason. Если регулятор в
        # цитате выбрал вариант, противоположный позиции пользователя —
        # понижаем результат до neutral с пометкой на проверку эксперта,
        # не пытаясь угадать "правильный" decision программно.
        needs_expert_review = False
        if decision != "neutral":
            print(f"[VERIFY] Запуск второго этапа для decision={decision}", flush=True)
            is_consistent, verify_note = _verify_regulator_choice_vs_user_position(
                quote, justification_summary, client, model,
            )
            if not is_consistent:
                print(f"[VERIFY] ⚠️ Противоречие найдено, понижаем decision {decision} → neutral", flush=True)
                decision = "neutral"
                needs_expert_review = True
                reason = f"Требует проверки эксперта: позиции не совпадают ({verify_note})"

        return {
            "decision": decision,
            "quote":    quote,
            "reason":   reason,
            "decision_fields": decision_fields,
            "needs_expert_review": needs_expert_review,
        }
    except Exception:
        # Fallback: пробуем угадать по ключевым словам
        text_lower = raw.lower()
        if "positive" in text_lower:
            decision = "positive"
        elif "negative" in text_lower:
            decision = "negative"
        else:
            decision = "neutral"
        decision, _was_fixed = _reconcile_decision_with_reason(decision, raw)
        return {"decision": decision, "quote": chunk_text[:150], "reason": raw[:100],
                "decision_fields": decision_fields}


# =============================================================================
# Агрегация: 1 файл = 1 голос (по большинству чанков внутри файла)
# =============================================================================
def aggregate_by_file(classified_chunks: List[Dict]) -> Dict:
    """
    Группирует чанки по файлу и определяет решение каждого файла
    по большинству голосов среди его чанков.
    Возвращает:
      {
        "positive": [{"file": ..., "quote": ..., "reason": ..., "date": ..., ...}, ...],
        "negative": [...],
        "neutral":  [...],
        "total_files": int,
      }
    """
    from collections import defaultdict, Counter

    # Группировка по файлу
    by_file: Dict[str, List[Dict]] = defaultdict(list)
    for chunk in classified_chunks:
        fname = chunk.get("file") or "неизвестный файл"
        by_file[fname].append(chunk)

    result = {"positive": [], "negative": [], "neutral": [], "total_files": 0}

    for fname, chunks in by_file.items():
        # Считаем голоса
        counter = Counter(c["decision"] for c in chunks)
        # Определяем победившее решение
        decision = counter.most_common(1)[0][0]

        # Берём лучшую цитату — от чанка с победившим решением
        best_chunk = next(
            (c for c in chunks if c["decision"] == decision), chunks[0]
        )

        file_record = {
            "file":         fname,
            "date":         best_chunk.get("date", ""),
            "sphere":       best_chunk.get("sphere", ""),
            "region":       best_chunk.get("region", ""),
            "year":         best_chunk.get("year", ""),
            "method":       best_chunk.get("method", ""),
            "organization": best_chunk.get("organization", ""),
            "section":      best_chunk.get("section", ""),
            "source":       best_chunk.get("source", ""),
            "quote":        best_chunk.get("quote", ""),
            "reason":       best_chunk.get("reason", ""),
            "decision_fields": best_chunk.get("decision_fields", {}),
            "needs_expert_review": best_chunk.get("needs_expert_review", False),
            "chunks_total": len(chunks),
            "chunks_decision": dict(counter),
        }
        result[decision].append(file_record)
        result["total_files"] += 1

    return result


# =============================================================================
# Основная функция прогноза
# =============================================================================
def run_prediction(
    article_name: str,
    justification_text: str,
    top_k: int = None,
    filters: Optional[Dict] = None,
    sources: Optional[List[str]] = None,
    _progress_cb=None,
) -> Optional[Dict]:
    """
    Запускает полный цикл прогноза. Возвращает dict с результатами или None при ошибке.
    `sources` — список из {"protocols", "expertise"}; по умолчанию ["expertise"].
    """
    if not article_name.strip():
        return None

    if sources is None:
        sources = ["expertise"]

    # Читаем top_k из конфига если не передан явно
    if top_k is None:
        top_k = int(load_predictor_config().get("default_top_k", _DEFAULT_TOP_K))

    # 1. Формируем поисковый запрос.
    # ИСПРАВЛЕНО: раньше при длинном обосновании (>500 симв.) article_name
    # полностью выбрасывался из запроса, заменяясь только синонимами от
    # expand_query — поиск уходил в сторону от реальной статьи затрат.
    # Теперь article_name участвует в запросе всегда; для длинного
    # обоснования берём только начальный фрагмент (для контекста), не весь
    # текст целиком — он и так дальше используется отдельно при
    # классификации каждого чанка (justification_summary).
    if _progress_cb:
        _progress_cb(0.05, "Формирование поискового запроса…")
    _JUSTIFICATION_QUERY_CHARS = 400
    if justification_text:
        search_query = f"{article_name} {justification_text[:_JUSTIFICATION_QUERY_CHARS]}"
    else:
        search_query = article_name

    # 2. Сжимаем обоснование если длинное
    justification_summary = justification_text
    if justification_text and len(justification_text) > _LARGE_DOC_THRESHOLD:
        if _progress_cb:
            _progress_cb(0.1, "Сжатие документа-обоснования…")
        justification_summary = compress_document(
            justification_text, article_name,
            _progress_cb=lambda p, m: _progress_cb(0.1 + p * 0.2, m) if _progress_cb else None,
        )

    # 3. Поиск
    # Для expertise — гибридный поиск (BM25 + векторный + CrossEncoder
    # reranking относительно article_name), та же инфраструктура, что и в
    # Советчике (core/advisor.py). Заметно точнее на коротких запросах
    # (название статьи затрат), чем чистый векторный cosine similarity.
    # Для protocols (или их комбинации с expertise) — оставляем обычный
    # векторный поиск через search_documents, т.к. protocols пока не имеет
    # отдельного BM25-индекса.
    if _progress_cb:
        _src_label = " + ".join(sources)
        _progress_cb(0.32, f"Поиск по базе ({_src_label}, top-{top_k})…")

    if sources == ["expertise"]:
        chunks = search_documents_hybrid(
            search_query, article_name=article_name, top_k=top_k, filters=filters,
        )
    else:
        chunks = search_documents(search_query, top_k=top_k, filters=filters, sources=sources)

    if not chunks:
        return {
            "article":    article_name,
            "query":      search_query,
            "chunks":     [],
            "aggregated": {"positive": [], "negative": [], "neutral": [], "total_files": 0},
            "error":      "Документы не найдены. Проверьте, загружена ли коллекция в Админке, и не слишком ли узкие фильтры.",
        }

    # 3.5. Обрезаем по общему бюджету символов — защита от слишком
    # длинной последовательной классификации (особенно при включённом
    # Unified KV Cache в LM Studio, который накапливает контекст между
    # вызовами и резко замедляет генерацию на больших top_k).
    chunks, dropped_count = _truncate_chunks_by_char_budget(chunks, _RAG_CONTEXT_CHAR_BUDGET)
    if dropped_count and _progress_cb:
        _progress_cb(
            0.35,
            f"Контекст обрезан до {_RAG_CONTEXT_CHAR_BUDGET:,} симв. "
            f"(отброшено {dropped_count} наименее релевантных фрагментов)…".replace(",", " "),
        )

    # 4. Классификация чанков через LLM
    if _progress_cb:
        _progress_cb(0.40, f"Классификация {len(chunks)} фрагментов…")
    try:
        from openai import OpenAI
        lm_url, model = _load_lm_config()
        client = OpenAI(base_url=lm_url, api_key="lm-studio", timeout=180.0)
    except Exception as e:
        return {"error": f"LM Studio недоступен: {e}", "article": article_name}

    classified = []
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if _progress_cb:
            pct = 0.40 + (i / total) * 0.50
            _progress_cb(pct, f"Классифицирую фрагмент {i + 1} / {total}…")
        classification = classify_chunk(
            chunk["text"], article_name, justification_summary, client, model
        )
        classified.append({**chunk, **classification})

    # 5. Агрегация по файлам
    if _progress_cb:
        _progress_cb(0.92, "Агрегация результатов…")
    aggregated = aggregate_by_file(classified)

    return {
        "article":              article_name,
        "query":                search_query,
        "justification_summary": justification_summary,
        "chunks_raw":           len(chunks),
        "chunks_dropped_budget": dropped_count,
        "aggregated":           aggregated,
        "timestamp":            datetime.now().isoformat(),
        "top_k":                top_k,
        "filters":              filters or {},
        "sources":              sources,
    }


# =============================================================================
# UI — счётчик-бейдж (цветной)
# =============================================================================
def _badge(label: str, count: int, color: str) -> str:
    return (
        f"<span style='display:inline-block;padding:3px 12px;border-radius:12px;"
        f"background:{color};color:#fff;font-weight:600;font-size:0.9rem;margin-right:6px'>"
        f"{label}: {count}</span>"
    )


_NEGATIVE_WEIGHT = 1.5  # negative весит сильнее positive — принцип осторожности
_HIGH_CONFIDENCE_THRESHOLD = 5  # содержательных источников (positive+negative) для "высокой" уверенности


def compute_approval_score(n_positive: int, n_negative: int, n_neutral: int) -> Dict:
    """
    Взвешенная агрегирующая оценка вероятности одобрения статьи затрат.

    Методология:
    - Учитываются только содержательные источники (positive + negative);
      neutral в сам процент не входит — они ничего не говорят по существу
      о позиции регулятора в отношении конкретной заявленной логики.
    - negative весит в _NEGATIVE_WEIGHT раз сильнее positive (принцип
      осторожности: ошибочно успокоить заявителя дороже, чем ошибочно
      насторожить — отказ в тарифной заявке создаёт больше риска для
      бизнеса, чем избыточная осторожность).
    - Если содержательных источников нет вообще (все найденные —
      neutral) — возвращается 50% с явной пометкой "низкая уверенность":
      это означает, что регуляторы, по всей видимости, ещё не
      сталкивались именно с такой комбинацией статьи и обоснования,
      а не что у организации есть основания на одобрение или отказ.
    - Уверенность (confidence) считается по числу содержательных
      источников: >= _HIGH_CONFIDENCE_THRESHOLD — высокая, 1..4 —
      средняя, 0 — низкая.
    """
    weighted_pos = n_positive
    weighted_neg = n_negative * _NEGATIVE_WEIGHT
    total_weighted = weighted_pos + weighted_neg
    n_substantive = n_positive + n_negative

    if total_weighted <= 0:
        approval_pct = 50.0
    else:
        approval_pct = (weighted_pos / total_weighted) * 100

    if n_substantive >= _HIGH_CONFIDENCE_THRESHOLD:
        confidence = "high"
    elif n_substantive >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "approval_pct": round(approval_pct, 1),
        "confidence": confidence,
        "n_substantive": n_substantive,
        "n_positive": n_positive,
        "n_negative": n_negative,
        "n_neutral": n_neutral,
        "all_neutral": n_substantive == 0 and n_neutral > 0,
    }


def _source_card(record: Dict, idx: int, decision: str) -> None:
    """Отображает одну карточку-источник в свёрнутом виде (как в советчике)."""
    color_map = {"positive": "#2e7a50", "negative": "#b33a3a", "neutral": "#888"}
    border_color = color_map.get(decision, "#888")

    header = record.get("file", "неизвестный файл")
    if record.get("date"):
        header += f"  ·  {record['date']}"
    if record.get("organization"):
        header += f"  ·  {record['organization']}"
    if record.get("source"):
        _src_badge = "экспертное" if record["source"] == "expertise" else "протокол"
        header += f"  ·  {_src_badge}"

    with st.expander(header, expanded=False):
        # Просмотр исходного txt-файла источника (скачивание — внутри диалога)
        _fname = record.get("file", "")
        _doc_type = "expertise" if record.get("source") == "expertise" else "protocol"
        _src_fpath = os.path.join("data", "raw", f"{_doc_type}_docs", _fname)
        if _fname and os.path.exists(_src_fpath):
            try:
                with open(_src_fpath, "rb") as _f:
                    _file_bytes = _f.read()
                _file_text = _file_bytes.decode("utf-8", errors="replace")

                if st.button(
                    "Просмотреть",
                    key=f"preview_{decision}_{idx}_{_fname}",
                ):
                    st.session_state["_preview_file"] = {
                        "name": _fname, "text": _file_text, "bytes": _file_bytes,
                    }
                    st.rerun()
            except Exception:
                pass
            st.markdown("")

        # Цитата
        quote = record.get("quote", "")
        if quote:
            st.markdown(
                f"<div style='border-left:3px solid {border_color};"
                f"padding:8px 12px;background:#f8f9fa;"
                f"border-radius:0 6px 6px 0;font-style:italic;"
                f"font-size:0.88rem;margin-bottom:8px;'>"
                f"{quote}</div>",
                unsafe_allow_html=True,
            )
        # Причина (от LLM-классификатора — почему отнесён к за/против/нейтрально)
        if record.get("reason"):
            st.caption(f"Оценка системы: {record['reason']}")

        # ── Ручной выбор эксперта для спорных neutral-источников ───────────
        # Только для тех, что понижены автоматической проверкой
        # согласованности (needs_expert_review=True) — не для всех neutral,
        # большинство которых нейтральны по делу (нет решения в прецеденте).
        if decision == "neutral" and record.get("needs_expert_review"):
            _fkey = record.get("file", "")
            _current_override = st.session_state.get("pred_expert_overrides", {}).get(_fkey)

            st.caption("Модель не смогла однозначно определить позицию — выберите вручную:")
            ec1, ec2, ec3 = st.columns(3)
            with ec1:
                if st.button(
                    "Это «за»", key=f"override_pos_{idx}_{_fkey}",
                    type="primary" if _current_override == "positive" else "secondary",
                    use_container_width=True,
                ):
                    st.session_state.setdefault("pred_expert_overrides", {})[_fkey] = "positive"
                    st.rerun()
            with ec2:
                if st.button(
                    "Это «против»", key=f"override_neg_{idx}_{_fkey}",
                    type="primary" if _current_override == "negative" else "secondary",
                    use_container_width=True,
                ):
                    st.session_state.setdefault("pred_expert_overrides", {})[_fkey] = "negative"
                    st.rerun()
            with ec3:
                if _current_override and st.button(
                    "Сбросить", key=f"override_reset_{idx}_{_fkey}",
                    use_container_width=True,
                ):
                    st.session_state.get("pred_expert_overrides", {}).pop(_fkey, None)
                    st.rerun()

            if _current_override:
                _override_label = "за" if _current_override == "positive" else "против"
                st.caption(f"✓ Учтено вручную как «{_override_label}»")

        # Метаданные
        meta_parts = []
        if record.get("sphere"):
            meta_parts.append(f"Сфера: {record['sphere']}")
        if record.get("region"):
            meta_parts.append(f"Регион: {record['region']}")
        if record.get("year"):
            meta_parts.append(f"Год: {record['year']}")
        if record.get("method"):
            meta_parts.append(f"Метод: {record['method']}")
        if record.get("section"):
            meta_parts.append(f"Раздел: {record['section']}")
        chunks_info = record.get("chunks_decision", {})
        if chunks_info:
            parts_str = " / ".join(
                f"{k}: {v}" for k, v in chunks_info.items()
            )
            meta_parts.append(f"Фрагментов ({parts_str})")
        if meta_parts:
            st.caption("  ·  ".join(meta_parts))


# =============================================================================
# Страница реестра
# =============================================================================
def _show_registry():
    st.subheader("Реестр прогнозов")

    records = load_registry(max_records=500)
    if not records:
        st.info("Реестр пуст — запустите первый прогноз.")
        return

    # Фильтры
    col1, col2, col3 = st.columns(3)
    with col1:
        filter_article = st.text_input("Фильтр по статье", key="reg_filter_article")
    with col2:
        filter_org = st.text_input("Фильтр по организации", key="reg_filter_org")
    with col3:
        filter_date = st.text_input("Фильтр по дате (ГГГГ-ММ)", key="reg_filter_date")

    # Применяем фильтры
    filtered = records
    if filter_article.strip():
        filtered = [r for r in filtered if filter_article.lower() in r.get("article", "").lower()]
    if filter_org.strip():
        filtered = [
            r for r in filtered
            if filter_org.lower() in json.dumps(r.get("sources", r.get("aggregated", {})), ensure_ascii=False).lower()
        ]
    if filter_date.strip():
        filtered = [r for r in filtered if r.get("timestamp", "").startswith(filter_date)]

    st.caption(f"Показано: {len(filtered)} из {len(records)}")
    st.divider()

    # Пагинация (по 20 записей)
    page_size = 20
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page_num = st.number_input("Страница", min_value=1, max_value=total_pages,
                                value=1, key="reg_page")
    start = (page_num - 1) * page_size
    page_records = filtered[start: start + page_size]

    for rec in page_records:
        agg = rec.get("aggregated_summary", rec.get("aggregated", {}))
        # aggregated_summary хранит целые числа; aggregated — legacy списки
        pos = agg.get("positive", 0) if isinstance(agg.get("positive", 0), int) else len(agg.get("positive", []))
        neg = agg.get("negative", 0) if isinstance(agg.get("negative", 0), int) else len(agg.get("negative", []))
        neu = agg.get("neutral",  0) if isinstance(agg.get("neutral",  0), int) else len(agg.get("neutral",  []))

        ts  = rec.get("timestamp", "")[:16].replace("T", " ")
        lbl = f"{ts}  ·  {rec.get('article', '—')}"

        with st.expander(lbl, expanded=False):
            st.markdown(
                _badge("За", pos, "#2e7a50")
                + _badge("Против", neg, "#b33a3a")
                + _badge("Нейтр.", neu, "#888"),
                unsafe_allow_html=True,
            )
            st.caption(f"Запрос: {rec.get('query', '—')[:120]}")
            if rec.get("filters"):
                st.caption(f"Фильтры: {rec['filters']}")


# =============================================================================
# Главная страница прогнозиста
# =============================================================================
def show_predictor():
    st.header("Прогноз решения регулятора")
    st.info(
        "Введите статью затрат и обоснование — система найдёт аналогичные случаи "
        "в протоколах и экспертных заключениях регуляторов и оценит вероятность одобрения."
    )

    # ── session_state ────────────────────────────────────────────────────────
    for key, val in [
        ("pred_result",       None),
        ("pred_running",      False),
        ("pred_doc_text",     ""),
    ]:
        if key not in st.session_state:
            st.session_state[key] = val

    # ── Вкладки ──────────────────────────────────────────────────────────────
    tab_predict, tab_registry = st.tabs(["Прогноз", "Реестр прогнозов"])

    with tab_predict:
        _show_predict_tab()

    with tab_registry:
        _show_registry()


# =============================================================================
# Вкладка «Прогноз»
# =============================================================================
def _show_file_preview_dialog():
    """
    Показывает содержимое исходного txt-файла во всплывающем окне, если
    пользователь нажал «Просмотреть» на одной из карточек источников.

    Поиск реализован как самодостаточный HTML/JS-компонент (через
    st.components.v1.html): JS сам подсвечивает совпадения и прокручивает
    к активному при нажатии «Далее»/«Назад» — это единственный способ
    физически проскроллить к найденному фрагменту, чистый Streamlit
    скроллом управлять не может. Сам текст экранируется от HTML-инъекций
    перед вставкой (документы пользовательские, могут случайно содержать
    символы вроде "<").
    """
    preview = st.session_state.get("_preview_file")
    if not preview:
        return

    @st.dialog(preview["name"], width="large")
    def _dialog():
        import html as _html
        import streamlit.components.v1 as components

        full_text = preview["text"]
        escaped_text = _html.escape(full_text)
        text_for_js = json.dumps(escaped_text.replace("\n", "<br>"))

        html_block = f"""
        <div style="font-family: -apple-system, sans-serif;">
          <div style="display:flex; gap:8px; margin-bottom:8px; align-items:center;">
            <input id="pv-search" type="text" placeholder="Поиск по тексту…"
                   style="flex:1; padding:8px 10px; border:1px solid #ccc;
                          border-radius:6px; font-size:0.9rem;" />
            <button id="pv-prev" style="padding:8px 12px; border:1px solid #ccc;
                    border-radius:6px; background:#f5f5f5; cursor:pointer;">‹ Назад</button>
            <button id="pv-next" style="padding:8px 12px; border:1px solid #ccc;
                    border-radius:6px; background:#f5f5f5; cursor:pointer;">Далее ›</button>
          </div>
          <div id="pv-count" style="color:#666; font-size:0.82rem; margin-bottom:6px;"></div>
          <div id="pv-content" style="height:460px; overflow-y:auto; padding:12px;
               border:1px solid #ddd; border-radius:6px; font-family:monospace;
               font-size:0.85rem; white-space:pre-wrap; line-height:1.5;"></div>
        </div>
        <script>
          const rawHtml = {text_for_js};
          const contentEl = document.getElementById('pv-content');
          const searchEl = document.getElementById('pv-search');
          const countEl = document.getElementById('pv-count');
          const prevBtn = document.getElementById('pv-prev');
          const nextBtn = document.getElementById('pv-next');

          contentEl.innerHTML = rawHtml;
          let matches = [];
          let activeIndex = -1;

          function escapeRegExp(s) {{
            return s.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
          }}

          function runSearch() {{
            const query = searchEl.value.trim();
            contentEl.innerHTML = rawHtml;
            matches = [];
            activeIndex = -1;

            if (!query) {{
              countEl.textContent = '';
              return;
            }}

            const re = new RegExp(escapeRegExp(query), 'gi');
            const walker = document.createTreeWalker(contentEl, NodeFilter.SHOW_TEXT, null);
            const textNodes = [];
            let node;
            while (node = walker.nextNode()) {{ textNodes.push(node); }}

            textNodes.forEach(function(textNode) {{
              const text = textNode.nodeValue;
              let lastIndex = 0;
              let m;
              re.lastIndex = 0;
              const frag = document.createDocumentFragment();
              let found = false;
              while ((m = re.exec(text)) !== null) {{
                found = true;
                frag.appendChild(document.createTextNode(text.slice(lastIndex, m.index)));
                const mark = document.createElement('mark');
                mark.style.background = '#fff3a0';
                mark.textContent = m[0];
                frag.appendChild(mark);
                matches.push(mark);
                lastIndex = m.index + m[0].length;
                if (m.index === re.lastIndex) re.lastIndex++;
              }}
              if (found) {{
                frag.appendChild(document.createTextNode(text.slice(lastIndex)));
                textNode.parentNode.replaceChild(frag, textNode);
              }}
            }});

            countEl.textContent = matches.length
              ? ('Найдено совпадений: ' + matches.length)
              : 'Совпадений не найдено';

            if (matches.length) {{
              activeIndex = 0;
              highlightActive();
            }}
          }}

          function highlightActive() {{
            matches.forEach(function(m, i) {{
              m.style.background = (i === activeIndex) ? '#ffa500' : '#fff3a0';
            }});
            if (matches[activeIndex]) {{
              matches[activeIndex].scrollIntoView({{ block: 'center', behavior: 'smooth' }});
              countEl.textContent = 'Совпадение ' + (activeIndex + 1) + ' из ' + matches.length;
            }}
          }}

          searchEl.addEventListener('input', runSearch);
          nextBtn.addEventListener('click', function() {{
            if (!matches.length) return;
            activeIndex = (activeIndex + 1) % matches.length;
            highlightActive();
          }});
          prevBtn.addEventListener('click', function() {{
            if (!matches.length) return;
            activeIndex = (activeIndex - 1 + matches.length) % matches.length;
            highlightActive();
          }});
        </script>
        """
        components.html(html_block, height=560, scrolling=False)

        dc1, dc2 = st.columns(2)
        with dc1:
            st.download_button(
                "Скачать",
                data=preview.get("bytes", full_text.encode("utf-8")),
                file_name=preview["name"],
                mime="text/plain",
                use_container_width=True,
            )
        with dc2:
            if st.button("Закрыть", use_container_width=True):
                st.session_state.pop("_preview_file", None)
                st.rerun()

    _dialog()


def _show_predict_tab():
    _show_file_preview_dialog()

    # ── Шаг 1: Статья затрат ─────────────────────────────────────────────────
    st.subheader("1. Статья затрат")
    article_name = st.text_input(
        "Наименование статьи",
        placeholder="Например: Заработная плата, Амортизация, Расходы на ремонт ОС",
        key="pred_article",
    )

    # ── Шаг 2: Документы-обоснования ─────────────────────────────────────────
    st.subheader("2. Документы-обоснования")
    st.caption(
        "Опишите обоснование текстом и/или приложите файл. "
        "Большие документы будут автоматически сжаты."
    )

    input_method = st.radio(
        "Способ ввода",
        ["Текстом", "Загрузить файл", "Из Сканера документов"],
        horizontal=True,
        key="pred_input_method",
    )

    justification_text = ""

    if input_method == "Текстом":
        justification_text = st.text_area(
            "Описание обоснования",
            height=160,
            placeholder=(
                "Опишите документы и суть обоснования: "
                "какие нормативы применялись, какие расчёты выполнены, "
                "какие документы прилагаются..."
            ),
            key="pred_justification_text",
        )

    elif input_method == "Загрузить файл":
        uploaded = st.file_uploader(
            "Загрузите документ-обоснование",
            type=["pdf", "docx", "doc", "txt", "xlsx"],
            key="pred_upload",
        )
        if uploaded:
            with st.spinner("Извлекаю текст из файла…"):
                raw_text = extract_file_text(uploaded.read(), uploaded.name)
            st.session_state.pred_doc_text = raw_text
            st.success(f"Файл прочитан: {len(raw_text):,} символов")
            preview = raw_text[:500]
            with st.expander("Предпросмотр текста"):
                st.text(preview + ("…" if len(raw_text) > 500 else ""))
        justification_text = st.session_state.get("pred_doc_text", "")

        # Дополнительный текст
        extra = st.text_area(
            "Дополнительное описание (опционально)",
            height=80,
            key="pred_extra_text",
        )
        if extra.strip():
            justification_text = (justification_text + "\n\n" + extra).strip()

    elif input_method == "Из Сканера документов":
        try:
            from streamlit_pages.doc_scanner import load_db as load_scan_db, _fname
            scan_db = load_scan_db()
            docs = scan_db.get("documents", [])
        except Exception:
            docs = []

        if not docs:
            st.warning("База Сканера пуста. Загрузите документы в разделе «Сканер документов».")
        else:
            doc_options = {_fname(d): d for d in docs}
            selected_name = st.selectbox(
                "Выберите документ",
                list(doc_options.keys()),
                key="pred_scanner_select",
            )
            if selected_name:
                selected_doc = doc_options[selected_name]
                scan_text = selected_doc.get("full_text", "")
                if not scan_text:
                    scan_text = "\n".join(
                        p.get("text", "") for p in selected_doc.get("pages", [])
                    )
                st.session_state.pred_doc_text = scan_text
                st.caption(f"Объём: {len(scan_text):,} символов · {selected_doc.get('word_count', 0):,} слов")
                justification_text = scan_text

    # ── Шаг 3: Источник поиска ───────────────────────────────────────────────
    st.subheader("3. Источник поиска")
    _SOURCE_OPTIONS = {
        "Только экспертные заключения": ["expertise"],
        "Только протоколы":             ["protocols"],
        "Оба источника":                ["expertise", "protocols"],
    }
    source_label = st.radio(
        "Где искать аналогичные случаи",
        list(_SOURCE_OPTIONS.keys()),
        index=0,  # по умолчанию — только экспертные
        horizontal=True,
        key="pred_source_radio",
        help=(
            "Экспертные заключения — новая база с полным набором атрибутов "
            "(регион, сфера, год, метод). Протоколы — старая коллекция; "
            "фильтры по году и методу регулирования к ней не применяются, "
            "так как эти поля отсутствуют в её метаданных."
        ),
    )
    selected_sources = _SOURCE_OPTIONS[source_label]
    if selected_sources == ["protocols"]:
        st.caption(
            "В коллекции протоколов нет полей «год» и «метод регулирования» — "
            "соответствующие фильтры ниже будут проигнорированы для этого источника."
        )
    elif "protocols" in selected_sources:
        st.caption(
            "Фильтры «год» и «метод» при комбинированном поиске применяются "
            "только к экспертным заключениям — у протоколов этих полей нет."
        )

    # ── Шаг 4: Фильтры ───────────────────────────────────────────────────────
    st.subheader("4. Фильтры (опционально)")

    def _collect_available_years() -> List[str]:
        """
        Год регулирования — не фиксированный справочник (бывают значения
        вида "2025-2029"), поэтому собираем реально встречающиеся значения
        из реестра документов (data/documents_registry.json), а не из
        захардкоженного списка как со сферами/регионами.
        """
        try:
            registry_path = os.path.join("data", "documents_registry.json")
            if not os.path.exists(registry_path):
                return []
            with open(registry_path, "r", encoding="utf-8") as f:
                reg = json.load(f)
            years = {
                entry.get("year") for entry in reg.values()
                if entry.get("year") and "не_определён" not in entry.get("year", "")
            }
            return sorted(years)
        except Exception:
            return []

    _PRED_YEARS = _collect_available_years()

    _PRED_SPHERES = [
        "🔥 Теплоснабжение",
        "💧 Водоснабжение/водоотведение",
        "🗑️ Обращение с ТКО",
        "🔵 Газ",
        "⚡ Электрика",
        "📁 Иные сферы",
    ]

    filter_spheres_raw = st.multiselect(
        "Сфера регулирования",
        options=_PRED_SPHERES,
        default=[],
        key="pred_filter_spheres",
        placeholder="Все сферы — фильтр не применяется",
        help=(
            "Ограничивает поиск протоколами выбранных сфер. "
            "Если не выбрано — поиск по всем протоколам."
        ),
    )
    # Убираем эмодзи для сравнения с метаданными ChromaDB
    # (в базе хранится "Теплоснабжение", а не "🔥 Теплоснабжение")
    import re as _re
    def _strip_emoji(s: str) -> str:
        return _re.sub(r"^[𐀀-􏿿☀-➿︀-️‍]+\s*", "", s).strip()

    filter_spheres = [_strip_emoji(s) for s in filter_spheres_raw]
    if filter_spheres:
        st.caption(f"Фильтр: **{'  ·  '.join(filter_spheres_raw)}**")

    _PRED_REGIONS = [
        # Центральный федеральный округ
        "Белгородская область", "Брянская область", "Владимирская область",
        "Воронежская область", "Ивановская область", "Калужская область",
        "Костромская область", "Курская область", "Липецкая область",
        "Московская область", "Орловская область", "Рязанская область",
        "Смоленская область", "Тамбовская область", "Тверская область",
        "Тульская область", "Ярославская область", "Москва",
        # Северо-Западный федеральный округ
        "Республика Карелия", "Республика Коми", "Архангельская область",
        "Ненецкий автономный округ", "Вологодская область",
        "Калининградская область", "Ленинградская область",
        "Мурманская область", "Новгородская область", "Псковская область",
        "Санкт-Петербург",
        # Южный федеральный округ
        "Республика Адыгея", "Республика Калмыкия", "Республика Крым",
        "Краснодарский край", "Астраханская область", "Волгоградская область",
        "Ростовская область", "Севастополь",
        # Северо-Кавказский федеральный округ
        "Республика Дагестан", "Республика Ингушетия",
        "Кабардино-Балкарская Республика", "Республика Северная Осетия — Алания",
        "Карачаево-Черкесская Республика", "Чеченская Республика",
        "Ставропольский край",
        # Приволжский федеральный округ
        "Республика Башкортостан", "Республика Марий Эл", "Республика Мордовия",
        "Республика Татарстан", "Удмуртская Республика", "Чувашская Республика",
        "Пермский край", "Кировская область", "Нижегородская область",
        "Оренбургская область", "Пензенская область", "Самарская область",
        "Саратовская область", "Ульяновская область",
        # Уральский федеральный округ
        "Курганская область", "Свердловская область", "Тюменская область",
        "Челябинская область", "Ханты-Мансийский автономный округ — Югра",
        "Ямало-Ненецкий автономный округ",
        # Сибирский федеральный округ
        "Республика Алтай", "Республика Бурятия", "Республика Тыва",
        "Республика Хакасия", "Алтайский край", "Красноярский край",
        "Иркутская область", "Кемеровская область", "Новосибирская область",
        "Омская область", "Томская область", "Забайкальский край",
        # Дальневосточный федеральный округ
        "Республика Саха (Якутия)", "Камчатский край", "Приморский край",
        "Хабаровский край", "Амурская область", "Магаданская область",
        "Сахалинская область", "Еврейская автономная область",
        "Чукотский автономный округ",
        # Новые регионы
        "Донецкая Народная Республика", "Луганская Народная Республика",
        "Запорожская область", "Херсонская область",
    ]

    filter_regions = st.multiselect(
        "Регион",
        options=_PRED_REGIONS,
        default=[],
        key="pred_filter_regions",
        placeholder="Все регионы — фильтр не применяется",
        help="Ограничивает поиск документами выбранных регионов.",
    )
    if filter_regions:
        st.caption(f"Фильтр: **{'  ·  '.join(filter_regions)}**")

    _PRED_METHODS = [
        "Индексация",
        "ЭОЗ",
        "RAB",
        "Метод экономически обоснованных расходов",
    ]

    fcol1, fcol2 = st.columns(2)
    with fcol1:
        filter_years = st.multiselect(
            "Год регулирования",
            options=_PRED_YEARS,
            default=[],
            key="pred_filter_years",
            placeholder="Все годы — фильтр не применяется",
            help="Ограничивает поиск документами с указанным годом регулирования.",
        )
        if filter_years:
            st.caption(f"Фильтр: **{'  ·  '.join(filter_years)}**")
    with fcol2:
        filter_methods = st.multiselect(
            "Метод регулирования",
            options=_PRED_METHODS,
            default=[],
            key="pred_filter_methods",
            placeholder="Все методы — фильтр не применяется",
            help="Ограничивает поиск документами с указанным методом регулирования.",
        )
        if filter_methods:
            st.caption(f"Фильтр: **{'  ·  '.join(filter_methods)}**")

    # ── Шаг 5: Настройки поиска ───────────────────────────────────────────────
    with st.expander("Настройки поиска", expanded=False):
        top_k = st.slider(
            "Количество источников (top-K)",
            min_value=5,
            max_value=100,
            value=_DEFAULT_TOP_K,
            step=5,
            key="pred_top_k",
            help="Сколько фрагментов протоколов извлекается перед классификацией",
        )

    # ── Кнопка запуска ────────────────────────────────────────────────────────
    st.divider()
    run_disabled = not article_name.strip()
    if st.button(
        "Рассчитать прогноз",
        type="primary",
        disabled=run_disabled,
        use_container_width=True,
        key="pred_run_btn",
    ):
        if not article_name.strip():
            st.warning("Введите наименование статьи затрат.")
        else:
            # Сохраняем параметры для запуска
            st.session_state.pred_result  = None
            st.session_state.pred_running = True
            st.session_state.pred_expert_overrides = {}  # сброс ручных правок предыдущего прогноза
            st.session_state._pred_params = {
                "article":       article_name,
                "justification": justification_text,
                "top_k":         top_k,
                "sources":       selected_sources,
                "filters": {
                    k: v for k, v in {
                        "spheres":  filter_spheres,   # список без эмодзи
                        "regions":  filter_regions,   # список регионов
                        "years":    filter_years,     # список годов
                        "methods":  filter_methods,   # список методов
                    }.items() if v
                },
            }
            st.rerun()

    # ── Запуск прогноза ───────────────────────────────────────────────────────
    if st.session_state.get("pred_running") and st.session_state.get("_pred_params"):
        params = st.session_state._pred_params
        st.warning("Идёт анализ протоколов — не переключайте раздел и не закрывайте вкладку")
        progress_bar  = st.progress(0.0, text="Запуск…")
        status_text   = st.empty()

        def _progress(pct: float, msg: str):
            progress_bar.progress(min(pct, 0.99), text=msg)
            status_text.caption(msg)

        with st.spinner("Анализирую протоколы и экспертные заключения…"):
            result = run_prediction(
                article_name      = params["article"],
                justification_text= params["justification"],
                top_k             = params["top_k"],
                filters           = params["filters"],
                sources           = params.get("sources", ["expertise"]),
                _progress_cb      = _progress,
            )

        progress_bar.progress(1.0, text="Готово")
        st.session_state.pred_result  = result
        st.session_state.pred_running = False

        # Сохраняем в реестр
        if result and not result.get("error"):
            registry_record = {
                "timestamp": result.get("timestamp", datetime.now().isoformat()),
                "article":   result.get("article", ""),
                "query":     result.get("query", ""),
                "filters":   result.get("filters", {}),
                "aggregated_summary": {
                    "positive": len(result["aggregated"]["positive"]),
                    "negative": len(result["aggregated"]["negative"]),
                    "neutral":  len(result["aggregated"]["neutral"]),
                    "total_files": result["aggregated"]["total_files"],
                },
                "sources": [
                    {"file": r["file"], "decision": d}
                    for d in ("positive", "negative", "neutral")
                    for r in result["aggregated"].get(d, [])
                ],
            }
            save_to_registry(registry_record)

        st.rerun()

    # ── Отображение результатов ───────────────────────────────────────────────
    result = st.session_state.get("pred_result")
    if result is None:
        st.info("Введите данные и нажмите «Рассчитать прогноз».")
        return

    if result.get("error"):
        st.error(result["error"])
        return

    agg  = result["aggregated"]
    pos  = list(agg.get("positive", []))
    neg  = list(agg.get("negative", []))
    neu  = list(agg.get("neutral",  []))
    total = agg.get("total_files", 0)

    # ── Применяем ручные правки эксперта по спорным neutral-источникам ──────
    # Хранится отдельно от result, чтобы не модифицировать исходные данные
    # прогноза — override применяется только к отображению/подсчёту.
    if "pred_expert_overrides" not in st.session_state:
        st.session_state["pred_expert_overrides"] = {}
    _overrides = st.session_state["pred_expert_overrides"]

    if _overrides:
        _still_neutral = []
        for rec in neu:
            _key = rec.get("file", "")
            _override = _overrides.get(_key)
            if _override == "positive":
                pos.append(rec)
            elif _override == "negative":
                neg.append(rec)
            else:
                _still_neutral.append(rec)
        neu = _still_neutral

    st.divider()
    st.subheader("Результаты")

    # ── Счётчик ───────────────────────────────────────────────────────────────
    st.markdown(
        _badge("За", len(pos), "#2e7a50")
        + _badge("Против", len(neg), "#b33a3a")
        + _badge("Нейтрально", len(neu), "#888")
        + f"<span style='color:#666;font-size:0.85rem;margin-left:8px'>"
        f"Уникальных источников: {total} · Фрагментов в поиске: {result.get('chunks_raw', 0)}</span>",
        unsafe_allow_html=True,
    )
    if result.get("chunks_dropped_budget"):
        st.caption(
            f"Контекст ограничен бюджетом {_RAG_CONTEXT_CHAR_BUDGET:,} символов — "
            f"{result['chunks_dropped_budget']} наименее релевантных фрагментов "
            f"не учитывались в анализе.".replace(",", " ")
        )
    st.markdown("")

    # ── Итоговая взвешенная оценка ──────────────────────────────────────────
    score = compute_approval_score(len(pos), len(neg), len(neu))

    _conf_label = {"high": "Высокая", "medium": "Средняя", "low": "Низкая"}[score["confidence"]]
    _conf_color = {"high": "#2e7a50", "medium": "#b8860b", "low": "#888"}[score["confidence"]]

    if score["all_neutral"]:
        st.info(
            "Все найденные источники нейтральны — ни один не содержит явного "
            "решения регулятора по схожей логике обоснования. Это говорит не "
            "о шансах на одобрение или отказ, а о том, что РЭКи, вероятно, "
            "ещё не сталкивались именно с такой комбинацией статьи затрат и "
            "обоснования. Показан нейтральный результат 50% с низкой "
            "уверенностью."
        )

    sc1, sc2 = st.columns([2, 1])
    with sc1:
        st.markdown(
            f"<div style='font-size:2.2rem;font-weight:700;color:#1a1a1a'>"
            f"{score['approval_pct']:.0f}% <span style='font-size:1rem;font-weight:400;color:#666'>"
            f"вероятность одобрения</span></div>",
            unsafe_allow_html=True,
        )
    with sc2:
        st.markdown(
            f"<div style='text-align:right'>"
            f"<span style='display:inline-block;padding:4px 14px;border-radius:14px;"
            f"background:{_conf_color};color:#fff;font-weight:600;font-size:0.85rem'>"
            f"Уверенность: {_conf_label}</span></div>",
            unsafe_allow_html=True,
        )

    st.progress(score["approval_pct"] / 100)

    with st.expander("Как считается эта оценка"):
        st.markdown(
            f"""
**Методология расчёта:**

1. Учитываются только содержательные источники — **{score['n_positive']} «за»** и
   **{score['n_negative']} «против»**. Нейтральные источники ({score['n_neutral']})
   в сам процент не входят: они не содержат решения регулятора по той же
   логике, что заявляет пользователь, и не должны размывать оценку.
2. «Против» весит сильнее «за» — в **{_NEGATIVE_WEIGHT}×**. Это сознательный
   перекос в сторону осторожности: ошибочно успокоить заявителя в случае
   риска отказа дороже, чем ошибочно насторожить при реальных шансах на
   одобрение.
3. Формула: `за / (за + против × {_NEGATIVE_WEIGHT}) × 100%`.
4. Если содержательных источников нет вообще (все найденные — нейтральны),
   показывается 50% с пометкой «низкая уверенность» — это не означает
   нейтральный шанс, а означает отсутствие данных по такой комбинации
   статьи и обоснования.
5. **Уверенность** оценки зависит от числа содержательных источников:
   {_HIGH_CONFIDENCE_THRESHOLD}+ — высокая, 1–{_HIGH_CONFIDENCE_THRESHOLD - 1} — средняя,
   0 — низкая.
            """
        )

    st.markdown("")

    st.divider()

    # ── Источники — положительные ─────────────────────────────────────────────
    if pos:
        st.markdown(
            "<div style='color:#2e7a50;font-weight:600;font-size:1rem;margin-bottom:6px'>"
            "Одобрено / включено</div>",
            unsafe_allow_html=True,
        )
        for i, rec in enumerate(pos):
            _source_card(rec, i, "positive")
        st.markdown("")

    # ── Источники — отрицательные ─────────────────────────────────────────────
    if neg:
        st.markdown(
            "<div style='color:#b33a3a;font-weight:600;font-size:1rem;margin-bottom:6px'>"
            "Отклонено / снижено</div>",
            unsafe_allow_html=True,
        )
        for i, rec in enumerate(neg):
            _source_card(rec, i, "negative")
        st.markdown("")

    # ── Источники — нейтральные ───────────────────────────────────────────────
    if neu:
        st.markdown(
            "<div style='color:#888;font-weight:600;font-size:1rem;margin-bottom:6px'>"
            "Нейтральные упоминания</div>",
            unsafe_allow_html=True,
        )
        for i, rec in enumerate(neu):
            _source_card(rec, i, "neutral")

    if not pos and not neg and not neu:
        st.warning("По данной статье затрат не найдено релевантных фрагментов в протоколах.")

    # ── Запрос и сжатое обоснование ───────────────────────────────────────────
    with st.expander("Детали поиска", expanded=False):
        st.caption(f"Поисковый запрос: {result.get('query', '—')}")
        if result.get("justification_summary") and result["justification_summary"] != result.get("justification_text"):
            st.markdown("**Сжатое обоснование:**")
            st.text(result["justification_summary"][:800])

    # ── Сброс ─────────────────────────────────────────────────────────────────
    st.divider()
    if st.button("Новый прогноз", key="pred_reset_btn"):
        for k in ["pred_result", "pred_running", "_pred_params", "pred_doc_text"]:
            st.session_state.pop(k, None)
        st.rerun()


# =============================================================================
# Точка входа
# =============================================================================
if __name__ == "__main__":
    show_predictor()