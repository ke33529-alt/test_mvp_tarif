# core/advisor.py
import os
import re
import sys
import json
import time
import hashlib
from datetime import datetime
import threading
from typing import Optional, List, Dict
from openai import OpenAI

# =============================================================================
# Отключение телеметрии ChromaDB
# =============================================================================
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["CHROMA_DB_TELEMETRY"]  = "false"

# =============================================================================
# Пути
# =============================================================================
CHROMA_DB_PATH  = os.path.join("data", "vector_db")
FAQ_PATH        = os.path.join("data", "faq", "faq.json")
CACHE_PATH      = os.path.join("data", "cache", "llm_cache.json")
CONFIG_FILE     = os.path.join("config", "advisor_config.json")
PROMPTS_FILE    = os.path.join("config", "prompts.json")
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# =============================================================================
# Промпты
# =============================================================================
DEFAULT_PROMPTS = {
    "advisor_system": (
        "Ты — эксперт-консультант по тарифному регулированию в Российской Федерации. "
        "Твоя задача — давать точные, структурированные ответы строго на основе "
        "предоставленных фрагментов нормативных документов.\n\n"
        "ПРАВИЛА ОТВЕТА:\n"
        "1. Отвечай ТОЛЬКО на русском языке.\n"
        "2. Опирайся исключительно на предоставленный контекст. "
        "Если в контексте нет ответа — честно сообщи об этом.\n"
        "3. ОБЯЗАТЕЛЬНО указывай источник: название документа, номер статьи / пункта.\n"
        "4. Структурируй ответ: используй нумерованные списки для перечислений.\n"
        "5. Для числовых данных — оформляй таблицей Markdown.\n"
        "6. Не выдумывай нормы и ссылки.\n"
        "7. Отвечай кратко и по существу."
    ),
    "advisor_user": (
        "Вопрос: {query}\n\n"
        "Фрагменты нормативных документов:\n{context}\n\n"
        "Дай ответ со ссылками на конкретные пункты документов из контекста выше."
    ),
    "advisor_system_description": "Системный промпт советчика.",
    "advisor_user_description":   "Шаблон запроса. Переменные: {query}, {context}.",
}


def load_prompts() -> Dict:
    if os.path.exists(PROMPTS_FILE):
        try:
            with open(PROMPTS_FILE, 'r', encoding='utf-8') as f:
                return {**DEFAULT_PROMPTS, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_PROMPTS)


def save_prompts(prompts: Dict):
    os.makedirs(os.path.dirname(PROMPTS_FILE), exist_ok=True)
    with open(PROMPTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)


# =============================================================================
# Конфиг
# =============================================================================
DEFAULT_CONFIG = {
    "lm_studio_url":   "http://127.0.0.1:1234/v1",
    "default_model":   "qwen/qwen3.5-9b",
    "max_tokens":      2048,
    "temperature":     0.3,
    "timeout_seconds": 300,
    "cache_ttl_days":  7,
}


def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(config: Dict):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


CONFIG = load_config()
client = OpenAI(
    base_url=CONFIG.get("lm_studio_url", "http://127.0.0.1:1234/v1"),
    api_key="lm-studio",
)


# =============================================================================
# Модели LM Studio
# =============================================================================
def get_available_models() -> List[Dict]:
    try:
        models = client.models.list()
        return [{"name": m.id, "size": "N/A", "family": "lm-studio"} for m in models.data]
    except Exception:
        return [{"name": CONFIG.get("default_model"), "size": "N/A", "family": "lm-studio"}]


def check_model_available(model_name: str) -> bool:
    return any(m["name"] == model_name for m in get_available_models())


# =============================================================================
# Embedding — истинный синглтон через sys.modules
#
# Streamlit перезагружает core.advisor на каждом рероне, сбрасывая
# обычные globals. sys.modules — процессный словарь, который Streamlit
# не трогает: модель загружается ровно ОДИН РАЗ за жизнь процесса.
# =============================================================================
_ST_MODEL_KEY = "__regula_ai_st_model__"
_st_lock      = threading.Lock()


def get_st_model():
    if sys.modules.get(_ST_MODEL_KEY) is not None:
        return sys.modules[_ST_MODEL_KEY]

    with _st_lock:
        if sys.modules.get(_ST_MODEL_KEY) is not None:
            return sys.modules[_ST_MODEL_KEY]

        t0 = time.perf_counter()
        print(f"[EMBED] Загрузка модели {EMBEDDING_MODEL} (один раз)...")
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(EMBEDDING_MODEL)
            # Прогрев: transformers-сканирование ~150 модулей происходит
            # здесь один раз, а не при каждом запросе пользователя
            model.encode(["прогрев"], normalize_embeddings=True)
            sys.modules[_ST_MODEL_KEY] = model
            print(f"[EMBED] Готово за {time.perf_counter()-t0:.1f} сек. Далее ~0.1 сек/запрос.")
        except Exception as e:
            print(f"[EMBED ERROR] {e}")
            return None

    return sys.modules[_ST_MODEL_KEY]


def embed_query(query: str):
    """Возвращает [[float,...]] для ChromaDB или None при ошибке."""
    model = get_st_model()
    if model is None:
        return None
    return model.encode([query], normalize_embeddings=True).tolist()


# =============================================================================
# ChromaDB — синглтон коллекции
# =============================================================================
_chroma_client     = None
_chroma_collection = None
_chroma_lock       = threading.Lock()


def get_chroma_collection():
    global _chroma_client, _chroma_collection
    with _chroma_lock:
        if _chroma_collection is not None:
            return _chroma_collection

        import chromadb
        t0 = time.perf_counter()
        try:
            _chroma_client = chromadb.PersistentClient(
                path=CHROMA_DB_PATH,
                settings=chromadb.Settings(anonymized_telemetry=False, allow_reset=True),
            )
        except Exception:
            try:
                _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
            except Exception as e:
                print(f"[CHROMA ERROR] {e}")
                return None

        try:
            _chroma_collection = _chroma_client.get_collection("tariff_docs")
        except Exception:
            try:
                _chroma_collection = _chroma_client.create_collection("tariff_docs")
            except Exception as e:
                print(f"[CHROMA ERROR] {e}")
                return None

        print(f"[CHROMA] Готово за {time.perf_counter()-t0:.2f} сек ({_chroma_collection.count()} чанков)")
        return _chroma_collection


# =============================================================================
# Кэш LLM
# =============================================================================
_llm_cache:  Dict = {}
_cache_lock = threading.Lock()


def load_llm_cache():
    global _llm_cache
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                _llm_cache = json.load(f)
        except Exception:
            _llm_cache = {}


def save_llm_cache():
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(_llm_cache, f, ensure_ascii=False, indent=2)


def get_cache_key(query: str, sources: list, model: str) -> str:
    s = json.dumps(
        sorted([x.get('file', '') + x.get('snippet', '')[:100] for x in sources]),
        sort_keys=True,
    )
    return hashlib.md5(f"{query}|||{s}|||{model}".encode()).hexdigest()


# =============================================================================
# Режим тестирования чанков
# =============================================================================
_SOURCES_ONLY_MODE = False


def set_sources_only_mode(enabled: bool):
    global _SOURCES_ONLY_MODE
    _SOURCES_ONLY_MODE = enabled


# =============================================================================
# Маршрутизация
# =============================================================================
ROUTING_RULES = {
    "позиция фас": "⚖️ Позиция ФАС", "разъяснение фас": "⚖️ Позиция ФАС",
    "прецедент": "🔍 Поиск прецедентов", "судебная практика": "🔍 Поиск прецедентов",
    "численность": "👥 Сверка численности", "штат": "👥 Сверка численности",
    "амортизация": "🏭 Проверка амортизации", "основные средства": "🏭 Проверка амортизации",
    "фгис": "📤 Экспорт ФГИС", "пояснительная": "📝 Пояснительная записка",
    "риск": "📊 Калькулятор рисков", "жалоба": "📝 Робот-жалобщик",
    "оспорить": "📝 Робот-жалобщик", "изменения": "🔄 Трекер изменений законов",
    "расчет": "📊 Расчетный лист", "формула": "📊 Расчетный лист",
    "тариф": "🔮 Прогнозист тарифов", "прогноз": "🔮 Прогнозист тарифов",
}


def detect_section(query: str) -> Optional[str]:
    q = query.lower()
    for kw, section in ROUTING_RULES.items():
        if kw in q:
            return section
    return None


# =============================================================================
# FAQ
# =============================================================================
def search_faq(query: str, top_k: int = 3) -> list:
    if not os.path.exists(FAQ_PATH):
        return []
    try:
        with open(FAQ_PATH, 'r', encoding='utf-8') as f:
            faq_data = json.load(f)
        results, q = [], query.lower()
        for item in faq_data.get("questions", []):
            qw = set(item.get("question", "").lower().split())
            if len(qw & set(q.split())) >= 3:
                results.append(item)
                if len(results) >= top_k:
                    break
        return results
    except Exception as e:
        print(f"[FAQ ERROR] {e}")
        return []


# =============================================================================
# Векторный поиск
# =============================================================================
def search_vector_db(query: str, top_k: int = 5) -> list:
    t0 = time.perf_counter()

    collection = get_chroma_collection()
    if collection is None:
        return []

    t1 = time.perf_counter()
    embedding = embed_query(query)
    print(f"[TIMING] embed_query: {time.perf_counter()-t1:.3f} сек")

    try:
        if embedding is not None:
            results = collection.query(
                query_embeddings=embedding,
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
        else:
            results = collection.query(
                query_texts=[query],
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
    except Exception as e:
        print(f"[VECTOR DB ERROR] {e}")
        return []

    print(f"[TIMING] search_vector_db итого: {time.perf_counter()-t0:.3f} сек")

    if not results or not results.get("documents") or not results["documents"][0]:
        return []

    sources = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        if meta is None:
            meta = {}
        sources.append({
            "snippet":     doc[:800] + ("..." if len(doc) > 800 else ""),
            "file":        meta.get("filename", "Неизвестно"),
            "page":        meta.get("page", ""),
            "category":    meta.get("category", "Общее"),
            "doc_type":    meta.get("doc_type", ""),
            "article":     meta.get("article", ""),
            "chunk_index": meta.get("chunk_index", ""),
            "distance":    round(dist, 3),
        })
    return sources


# =============================================================================
# Удаление thinking-блоков Qwen3
# =============================================================================
def strip_thinking_blocks(text: str) -> str:
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()


# =============================================================================
# Стриминг ответа — генератор для st.write_stream()
# =============================================================================
def stream_ai_answer(
    query: str,
    sources: list,
    model: str = None,
    temperature: float = None,
):
    """
    Генератор токенов для Streamlit st.write_stream().
    Если ответ есть в кэше — возвращает его сразу одним куском.
    Иначе стримит токены по мере генерации LLM.
    Автоматически сохраняет ответ в кэш после завершения.
    """
    config      = load_config()
    model       = model or config.get("default_model", "qwen/qwen3.5-9b")
    temperature = temperature if temperature is not None else config.get("temperature", 0.3)
    max_tokens  = config.get("max_tokens", 2048)
    timeout     = config.get("timeout_seconds", 300)

    if _SOURCES_ONLY_MODE:
        yield "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."
        return

    # Кэш — возвращаем сразу без стриминга
    cache_key = get_cache_key(query, sources, model)
    with _cache_lock:
        if cache_key in _llm_cache:
            cached = _llm_cache[cache_key]
            if datetime.now().timestamp() - cached.get("timestamp", 0) < 604800:
                print(f"[CACHE HIT stream] {model}")
                yield cached["answer"]
                return

    # Строим промпт (та же логика что в generate_ai_answer)
    try:
        prompts = load_prompts()
        context_parts = []
        for i, src in enumerate(sources[:6], 1):
            art = f", пункт {src['article']}" if src.get('article') else ""
            context_parts.append(f"[{i}] {src['file']}{art}:\n{src['snippet']}")
        context = "\n\n---\n\n".join(context_parts)

        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])
        user_content  = prompts.get("advisor_user",   DEFAULT_PROMPTS["advisor_user"]).format(
            query=query, context=context,
        )

        is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        extra_body = {}
        if is_qwen3:
            user_content = "/no_think\n\n" + user_content
            # Пробуем все известные способы отключить thinking в LM Studio
            extra_body = {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            }

        kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            stream=True,
        )
        if extra_body:
            kwargs["extra_body"] = extra_body

        print(f"[LLM stream] {model} | max_tokens={max_tokens}")
        t0 = time.perf_counter()

        full_text    = ""
        buf          = ""     # буфер для обнаружения <think>-тегов
        in_think     = False  # флаг: сейчас внутри <think>...</think>

        response = client.chat.completions.create(**kwargs)

        for chunk in response:
            delta = chunk.choices[0].delta.content
            if not delta:
                continue

            full_text += delta
            buf       += delta

            # --- фильтр thinking-блоков в потоке ---
            while buf:
                if in_think:
                    end = buf.find("</think>")
                    if end >= 0:
                        in_think = False
                        buf = buf[end + len("</think>"):]
                    else:
                        buf = ""   # думаем — ничего не выводим
                        break
                else:
                    start = buf.find("<think>")
                    if start >= 0:
                        if start > 0:
                            yield buf[:start]   # текст до тега
                        in_think = True
                        buf = buf[start + len("<think>"):]
                    else:
                        yield buf
                        buf = ""
                        break

        print(f"[LLM stream] готово за {time.perf_counter()-t0:.2f} сек")

        # Сохраняем в кэш очищенный ответ
        answer = strip_thinking_blocks(full_text)
        with _cache_lock:
            _llm_cache[cache_key] = {
                "answer":    answer,
                "timestamp": datetime.now().timestamp(),
                "query":     query,
                "model":     model,
            }
            save_llm_cache()

    except Exception as e:
        err = str(e)
        if "Connection" in err or "refused" in err:
            yield "\n🔌 Ошибка подключения к LM Studio."
        elif "timeout" in err.lower():
            yield f"\n⏱️ Таймаут ({timeout} сек)."
        else:
            yield f"\n❌ Ошибка LLM: {err}"


# =============================================================================
# Генерация ответа (не-стриминг, используется как fallback)
# =============================================================================
def generate_ai_answer(
    query: str,
    sources: list,
    model: str = None,
    temperature: float = None,
) -> str:
    config      = load_config()
    model       = model or config.get("default_model", "qwen/qwen3.5-9b")
    temperature = temperature if temperature is not None else config.get("temperature", 0.3)
    max_tokens  = config.get("max_tokens", 2048)   # НЕ урезаем — берём из конфига
    timeout     = config.get("timeout_seconds", 300)

    if _SOURCES_ONLY_MODE:
        return "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."

    cache_key = get_cache_key(query, sources, model)
    with _cache_lock:
        if cache_key in _llm_cache:
            cached = _llm_cache[cache_key]
            if datetime.now().timestamp() - cached.get("timestamp", 0) < 604800:
                print(f"[CACHE HIT] {model}")
                return cached["answer"]

    try:
        prompts = load_prompts()
        context_parts = []
        for i, src in enumerate(sources[:6], 1):
            art = f", пункт {src['article']}" if src.get('article') else ""
            context_parts.append(f"[{i}] {src['file']}{art}:\n{src['snippet']}")
        context = "\n\n---\n\n".join(context_parts)

        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])
        user_content  = prompts.get("advisor_user",   DEFAULT_PROMPTS["advisor_user"]).format(
            query=query, context=context,
        )

        # Qwen3: отключаем thinking mode
        # /no_think должен быть в USER-сообщении (не в system!)
        is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        extra_body = {}
        if is_qwen3:
            user_content = "/no_think\n\n" + user_content
            extra_body   = {"enable_thinking": False}

        t0 = time.perf_counter()
        print(f"[LLM] {model} | max_tokens={max_tokens} | "
              f"thinking={'OFF' if is_qwen3 else 'n/a'} | "
              f"промпт ~{len(system_prompt)+len(user_content)} симв.")

        kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        if extra_body:
            kwargs["extra_body"] = extra_body

        response      = client.chat.completions.create(**kwargs)
        raw_content   = response.choices[0].message.content
        finish_reason = response.choices[0].finish_reason

        print(f"[LLM] ответ за {time.perf_counter()-t0:.2f} сек | "
              f"finish={finish_reason} | len={len(raw_content or '')}")

        if finish_reason == "length":
            return ("⚠️ Превышен лимит токенов. "
                    "Увеличьте 'max_tokens' в конфиге или сократите запрос.")
        if not raw_content:
            return "⚠️ Модель вернула пустой ответ."

        answer = strip_thinking_blocks(raw_content)

        with _cache_lock:
            _llm_cache[cache_key] = {
                "answer": answer, "timestamp": datetime.now().timestamp(),
                "query": query, "model": model,
            }
            save_llm_cache()

        return answer

    except Exception as e:
        err = str(e)
        if "Connection" in err or "refused" in err:
            return "🔌 Ошибка подключения к LM Studio. Проверьте, что сервер запущен на 127.0.0.1:1234."
        if "timeout" in err.lower():
            return f"⏱️ Таймаут ({timeout} сек)."
        return f"❌ Ошибка LLM: {err}"


# =============================================================================
# Основной метод
# =============================================================================
def ask_question(
    query: str,
    top_k: int = 5,
    temperature: float = None,
    use_faq: bool = True,
    model: str = None,
) -> dict:
    t_start = time.perf_counter()
    config  = load_config()
    model   = model or config.get("default_model", "qwen/qwen3.5-9b")

    if not _llm_cache:
        load_llm_cache()

    print(f"\n{'='*55}\n[ASK] «{query[:70]}» | {model}\n{'='*55}")

    result = {
        "answer": "", "sources": [], "redirect": None,
        "redirect_reason": None, "from_faq": False,
        "from_cache": False, "model": model,
    }

    # FAQ
    if use_faq:
        faq = search_faq(query, top_k=3)
        if faq:
            result.update({
                "answer":  faq[0]["answer"],
                "sources": [{"snippet": faq[0]["question"], "file": "FAQ",
                              "page": "", "category": "FAQ"}],
                "from_faq": True,
            })
            sec = detect_section(query)
            if sec:
                result["redirect"]        = sec
                result["redirect_reason"] = f"Для деталей рекомендуем раздел «{sec}»"
            print(f"[ASK] FAQ за {time.perf_counter()-t_start:.2f} сек")
            return result

    # Векторный поиск
    sources = search_vector_db(query, top_k=top_k)
    result["sources"] = sources

    if sources:
        cache_key  = get_cache_key(query, sources, model)
        was_cached = (
            cache_key in _llm_cache and
            datetime.now().timestamp() - _llm_cache[cache_key].get("timestamp", 0) < 604800
        )
        result["answer"]     = generate_ai_answer(query, sources, model, temperature)
        result["from_cache"] = was_cached
    else:
        result["answer"] = ("❌ Не найдено релевантных документов в базе знаний. "
                            "Попробуйте переформулировать вопрос.")

    sec = detect_section(query)
    if sec:
        result["redirect"]        = sec
        result["redirect_reason"] = f"💡 Ваш вопрос относится к разделу «{sec}»."

    print(f"[ASK] Итого: {time.perf_counter()-t_start:.2f} сек\n")
    return result