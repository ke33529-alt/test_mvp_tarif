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

_LARGE_DOC_THRESHOLD = 12_000
_CHUNK_SIZE          = 6_000
_CHUNK_OVERLAP       = 300

_DEFAULT_TOP_K = 30

_REGISTRY_LOCK = threading.Lock()


# =============================================================================
# Загрузка настроек прогнозиста из Админки
# =============================================================================
_PRED_CFG_FILE = os.path.join("config", "predictor_config.json")
_PRED_CFG_DEFAULTS = {
    "chunk_chars_to_llm":  800,
    "justification_chars": 200,
    "classify_max_tokens": 100,
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
    "predictor_classify_system": "Тарифный эксперт РФ. JSON только.",
    "predictor_classify_user": (
        "Статья: {article_name}\n"
        "{justification_line}"
        "\nФрагмент:\n{chunk}\n\n"
        "Решение регулятора по статье: positive/negative/neutral?\n"
        "positive=включена, negative=снижена/отклонена, neutral=без решения/не по теме\n"
        'JSON: {{"decision":"?","quote":"до 100 симв.","reason":"до 80 симв."}}'
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
# ChromaDB — поиск по коллекции протоколов
# =============================================================================
def _get_chroma_collection():
    """
    Возвращает коллекцию протоколов из ChromaDB.
    Использует ту же E5EmbeddingFunction что и indexer.py —
    без этого query-векторы несовместимы с индексированными passage-векторами.
    """
    try:
        from core.indexer import get_embedding_function, _get_chroma_client
        client = _get_chroma_client()
        ef = get_embedding_function()
        try:
            return client.get_collection(
                name=_PROTOCOLS_COLLECTION,
                embedding_function=ef,
            )
        except Exception:
            return None
    except Exception as e:
        print(f"[PREDICTOR] Ошибка подключения к ChromaDB: {e}")
        return None


def search_protocols(query: str, top_k: int = _DEFAULT_TOP_K,
                     filters: Optional[Dict] = None) -> List[Dict]:
    """
    Векторный поиск по коллекции протоколов.
    Возвращает список чанков с текстом и метаданными.
    """
    collection = _get_chroma_collection()
    if collection is None:
        return []

    try:
        where = {}
        if filters:
            clauses = []

            # spheres — список строк без эмодзи, поддерживает $in для нескольких сфер
            _spheres = filters.get("spheres", [])
            if isinstance(_spheres, str) and _spheres:   # legacy: одна строка
                _spheres = [_spheres]
            if _spheres:
                if len(_spheres) == 1:
                    clauses.append({"sphere": {"$eq": _spheres[0]}})
                else:
                    clauses.append({"sphere": {"$in": _spheres}})

            # regions — список регионов, $in для нескольких, $eq для одного
            _regions = filters.get("regions", filters.get("region", []))
            if isinstance(_regions, str) and _regions:
                _regions = [_regions]
            if _regions:
                if len(_regions) == 1:
                    clauses.append({"region": {"$eq": _regions[0]}})
                else:
                    clauses.append({"region": {"$in": _regions}})

            if len(clauses) == 1:
                where = clauses[0]
            elif len(clauses) > 1:
                where = {"$and": clauses}

        kwargs = dict(query_texts=[query], n_results=top_k, include=["documents", "metadatas", "distances"])
        if where:
            kwargs["where"] = where

        results = collection.query(**kwargs)

        chunks = []
        docs      = results.get("documents", [[]])[0]
        metas     = results.get("metadatas",  [[]])[0]
        distances = results.get("distances",  [[]])[0]

        for doc, meta, dist in zip(docs, metas, distances):
            chunks.append({
                "text":         doc,
                "file":         meta.get("file", ""),
                "date":         meta.get("date", ""),
                "sphere":       meta.get("sphere", ""),
                "region":       meta.get("region", ""),
                "organization": meta.get("organization", ""),
                "distance":     dist,
            })
        return chunks

    except Exception as e:
        print(f"[PREDICTOR] Ошибка поиска: {e}")
        return []


# =============================================================================
# Классификация чанка через LLM
# =============================================================================

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
    _justify = justification_summary[:_justify_chars] if justification_summary and _justify_chars > 0 else ""
    _justify_line = f"Обоснование: {_justify}\n" if _justify else ""

    system_prompt = prompts["predictor_classify_system"]
    user_template = prompts["predictor_classify_user"]

    prompt = user_template.format(
        article_name     = article_name,
        justification_line = _justify_line,
        chunk            = _chunk,
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
        return {
            "decision": decision,
            "quote":    data.get("quote", chunk_text[:150]),
            "reason":   data.get("reason", ""),
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
        return {"decision": decision, "quote": chunk_text[:150], "reason": raw[:100]}


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
            "organization": best_chunk.get("organization", ""),
            "quote":        best_chunk.get("quote", ""),
            "reason":       best_chunk.get("reason", ""),
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
    _progress_cb=None,
) -> Optional[Dict]:
    """
    Запускает полный цикл прогноза. Возвращает dict с результатами или None при ошибке.
    """
    if not article_name.strip():
        return None

    # Читаем top_k из конфига если не передан явно
    if top_k is None:
        top_k = int(load_predictor_config().get("default_top_k", _DEFAULT_TOP_K))

    # 1. Расширяем запрос
    if _progress_cb:
        _progress_cb(0.05, "Расширение поискового запроса…")
    try:
        from core.query_expander import expand_query
        query_variants = expand_query(article_name)
        # Добавляем обоснование в запрос если оно короткое
        if justification_text and len(justification_text) <= 500:
            search_query = f"{article_name} {justification_text}"
        else:
            search_query = " ".join(query_variants[:3])
    except Exception:
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

    # 3. Поиск по ChromaDB
    if _progress_cb:
        _progress_cb(0.32, f"Поиск по протоколам (top-{top_k})…")
    chunks = search_protocols(search_query, top_k=top_k, filters=filters)

    if not chunks:
        return {
            "article":    article_name,
            "query":      search_query,
            "chunks":     [],
            "aggregated": {"positive": [], "negative": [], "neutral": [], "total_files": 0},
            "error":      "Протоколы не найдены. Проверьте, загружена ли коллекция в Админке.",
        }

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
        "aggregated":           aggregated,
        "timestamp":            datetime.now().isoformat(),
        "top_k":                top_k,
        "filters":              filters or {},
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


def _source_card(record: Dict, idx: int, decision: str) -> None:
    """Отображает одну карточку-источник в свёрнутом виде (как в советчике)."""
    color_map = {"positive": "#2e7a50", "negative": "#b33a3a", "neutral": "#888"}
    border_color = color_map.get(decision, "#888")

    header = record.get("file", "неизвестный файл")
    if record.get("date"):
        header += f"  ·  {record['date']}"
    if record.get("organization"):
        header += f"  ·  {record['organization']}"

    with st.expander(header, expanded=False):
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
        # Причина
        if record.get("reason"):
            st.caption(f"Обоснование: {record['reason']}")
        # Метаданные
        meta_parts = []
        if record.get("sphere"):
            meta_parts.append(f"Сфера: {record['sphere']}")
        if record.get("region"):
            meta_parts.append(f"Регион: {record['region']}")
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
def _show_predict_tab():
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

    # ── Шаг 3: Фильтры ───────────────────────────────────────────────────────
    st.subheader("3. Фильтры (опционально)")

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
        help="Ограничивает поиск протоколами выбранных регионов.",
    )
    if filter_regions:
        st.caption(f"Фильтр: **{'  ·  '.join(filter_regions)}**")

    # ── Шаг 4: Настройки поиска ───────────────────────────────────────────────
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
            st.session_state._pred_params = {
                "article":       article_name,
                "justification": justification_text,
                "top_k":         top_k,
                "filters": {
                    k: v for k, v in {
                        "spheres":  filter_spheres,   # список без эмодзи
                        "regions":  filter_regions,   # список регионов
                    }.items() if v
                },
            }
            st.rerun()

    # ── Запуск прогноза ───────────────────────────────────────────────────────
    if st.session_state.get("pred_running") and st.session_state.get("_pred_params"):
        params = st.session_state._pred_params
        progress_bar  = st.progress(0.0, text="Запуск…")
        status_text   = st.empty()

        def _progress(pct: float, msg: str):
            progress_bar.progress(min(pct, 0.99), text=msg)
            status_text.caption(msg)

        with st.spinner("Анализирую протоколы регулятора…"):
            result = run_prediction(
                article_name      = params["article"],
                justification_text= params["justification"],
                top_k             = params["top_k"],
                filters           = params["filters"],
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
    pos  = agg.get("positive", [])
    neg  = agg.get("negative", [])
    neu  = agg.get("neutral",  [])
    total = agg.get("total_files", 0)

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
    st.markdown("")

    # Процентный вывод
    if total > 0:
        pct_pos = round(len(pos) / total * 100)
        pct_neg = round(len(neg) / total * 100)
        pct_neu = round(len(neu) / total * 100)
        st.progress(len(pos) / total)
        st.caption(
            f"По аналогичным случаям в протоколах: "
            f"одобрено — {pct_pos}%, отклонено — {pct_neg}%, нейтрально — {pct_neu}%"
        )

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