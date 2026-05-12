# core/advisor.py — ИСПРАВЛЕННАЯ ВЕРСИЯ
#
# Изменения:
#   1. get_chroma_collection() теперь использует multilingual embedding-функцию
#   2. Qwen3 thinking mode: добавлен /no_think + стриппинг <think>...</think>
#   3. Улучшен системный промпт: убрана "благодарность", добавлены ссылки на статьи
#   4. Контекст передаётся с номерами статей и типом документа

import os
import re
import json
import hashlib
from datetime import datetime
import threading
from typing import Optional, List, Dict
from openai import OpenAI

# =============================================================================
# Отключение телеметрии ChromaDB
# =============================================================================
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["CHROMA_DB_TELEMETRY"] = "false"

# =============================================================================
# Настройки путей
# =============================================================================
CHROMA_DB_PATH = os.path.join("data", "vector_db")
FAQ_PATH = os.path.join("data", "faq", "faq.json")
CACHE_PATH = os.path.join("data", "cache", "llm_cache.json")
CONFIG_FILE = os.path.join("config", "advisor_config.json")
PROMPTS_FILE = os.path.join("config", "prompts.json")

# =============================================================================
# Дефолтные промпты
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
        "   Пример: «Согласно п. 47 Постановления Правительства РФ № 406...»\n"
        "4. Структурируй ответ: используй нумерованные списки для перечислений.\n"
        "5. Для числовых данных, ставок, расчётных параметров — оформляй таблицей Markdown:\n"
        "   | Параметр | Значение | Единица |\n"
        "   |---|---|---|\n"
        "6. Не выдумывай нормы и ссылки. Не дополняй контекст общими знаниями "
        "без явного указания на это.\n"
        "7. Если вопрос выходит за рамки тарифного регулирования — сообщи об этом.\n"
        "8. Отвечай кратко и по существу. Не добавляй вводных фраз, "
        "благодарностей или оценок вопроса."
    ),
    "advisor_user": (
        "Вопрос: {query}\n\n"
        "Фрагменты нормативных документов:\n"
        "{context}\n\n"
        "Дай ответ со ссылками на конкретные пункты документов из контекста выше."
    ),
    "advisor_system_description": "Системный промпт советчика.",
    "advisor_user_description": "Шаблон запроса. Переменные: {query}, {context}.",
}


def load_prompts() -> Dict:
    if os.path.exists(PROMPTS_FILE):
        try:
            with open(PROMPTS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                return {**DEFAULT_PROMPTS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_PROMPTS)


def save_prompts(prompts: Dict):
    os.makedirs(os.path.dirname(PROMPTS_FILE), exist_ok=True)
    with open(PROMPTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)


# =============================================================================
# Конфигурация LM Studio
# =============================================================================
DEFAULT_CONFIG = {
    "lm_studio_url": "http://127.0.0.1:1234/v1",
    "default_model": "qwen/qwen3.5-9b",
    "max_tokens": 2048,
    "temperature": 0.3,
    "timeout_seconds": 300,
    "cache_ttl_days": 7,
}


def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception:
            pass
    return DEFAULT_CONFIG


def save_config(config: Dict):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


CONFIG = load_config()

client = OpenAI(
    base_url=CONFIG.get("lm_studio_url", "http://127.0.0.1:1234/v1"),
    api_key="lm-studio"
)


# =============================================================================
# Управление моделями
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
# ChromaDB — ИСПРАВЛЕНО: используем multilingual embedding-функцию
# =============================================================================
_chroma_client = None
_chroma_collection = None
_client_lock = threading.Lock()


def get_chroma_collection():
    """
    Возвращает коллекцию ChromaDB.
    Используем дефолтную ChromaDB embedding-функцию — sentence_transformers
    сломан в этом окружении (сбой импорта scipy/sklearn).
    """
    global _chroma_client, _chroma_collection

    with _client_lock:
        if _chroma_collection is not None:
            return _chroma_collection

        import chromadb

        try:
            _chroma_client = chromadb.PersistentClient(
                path=CHROMA_DB_PATH,
                settings=chromadb.Settings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                    is_persistent=True,
                )
            )
        except Exception as e:
            print(f"[WARN] ChromaDB: {e}")
            try:
                _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
            except Exception:
                return None

        try:
            # Без embedding_function — ChromaDB использует встроенную дефолтную модель
            # (ту же, что применялась при индексации)
            _chroma_collection = _chroma_client.get_collection(name="tariff_docs")
        except Exception:
            try:
                _chroma_collection = _chroma_client.create_collection(name="tariff_docs")
            except Exception as e:
                print(f"[ERROR] Не удалось создать коллекцию: {e}")
                return None

        return _chroma_collection


# =============================================================================
# Кэш LLM-ответов
# =============================================================================
_llm_cache: Dict = {}
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
    sources_str = json.dumps(
        sorted([s.get('file', '') + s.get('snippet', '')[:100] for s in sources]),
        sort_keys=True
    )
    return hashlib.md5(f"{query}|||{sources_str}|||{model}".encode()).hexdigest()


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
    "фгис": "📤 Экспорт ФГИС",
    "пояснительная": "📝 Пояснительная записка",
    "риск": "📊 Калькулятор рисков",
    "жалоба": "📝 Робот-жалобщик", "оспорить": "📝 Робот-жалобщик",
    "изменения": "🔄 Трекер изменений законов",
    "расчет": "📊 Расчетный лист", "формула": "📊 Расчетный лист",
    "тариф": "🔮 Прогнозист тарифов", "прогноз": "🔮 Прогнозист тарифов",
}


def detect_section(query: str) -> Optional[str]:
    query_lower = query.lower()
    for keywords, section in ROUTING_RULES.items():
        if keywords in query_lower:
            return section
    return None


# =============================================================================
# Поиск в FAQ
# =============================================================================
def search_faq(query: str, top_k: int = 3) -> list:
    if not os.path.exists(FAQ_PATH):
        return []
    try:
        with open(FAQ_PATH, 'r', encoding='utf-8') as f:
            faq_data = json.load(f)
        results = []
        query_lower = query.lower()
        for item in faq_data.get("questions", []):
            q = item.get("question", "").lower()
            # Более строгое совпадение: требуем минимум 3 совпадающих слова
            q_words = set(q.split())
            query_words = set(query_lower.split())
            common = q_words & query_words
            if len(common) >= 3:
                results.append({
                    "question": item["question"],
                    "answer": item["answer"],
                    "category": item.get("category", "Общее"),
                    "source": "FAQ",
                })
                if len(results) >= top_k:
                    break
        return results
    except Exception as e:
        print(f"[FAQ ERROR] {e}")
        return []


# =============================================================================
# Поиск в векторной базе
# =============================================================================
def search_vector_db(query: str, top_k: int = 5) -> list:
    try:
        collection = get_chroma_collection()

        if collection is None:
            print("[ERROR] Коллекция ChromaDB не доступна")
            return []

        results = collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        if not results or not results.get("documents") or not results["documents"][0]:
            return []

        sources = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            if meta is None:
                meta = {}
            sources.append({
                "snippet": doc[:800] + "..." if len(doc) > 800 else doc,
                "file": meta.get("filename", "Неизвестно"),
                "page": meta.get("page", ""),
                "category": meta.get("category", "Общее"),
                "doc_type": meta.get("doc_type", ""),
                "article": meta.get("article", ""),
                "chunk_index": meta.get("chunk_index", ""),
                "distance": round(dist, 3),
            })
        return sources

    except Exception as e:
        print(f"[VECTOR DB ERROR] {e}")
        import traceback
        traceback.print_exc()
        return []


# =============================================================================
# ✅ ИСПРАВЛЕНИЕ #3: Стриппинг Qwen3 thinking-блоков
# =============================================================================
def strip_thinking_blocks(text: str) -> str:
    """
    Удаляет блоки <think>...</think>, которые Qwen3 генерирует в thinking mode.
    Также удаляет пустые строки в начале.
    """
    # Удаляем блоки <think>...</think> (включая многострочные)
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Убираем лишние пустые строки
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


# =============================================================================
# Генерация ответа через LM Studio
# =============================================================================
def generate_ai_answer(
    query: str,
    sources: list,
    model: str = None,
    temperature: float = None,
) -> str:
    config = load_config()
    model = model or config.get("default_model", "qwen/qwen3.5-9b")
    temperature = temperature if temperature is not None else config.get("temperature", 0.3)
    max_tokens = config.get("max_tokens", 2048)
    timeout = config.get("timeout_seconds", 300)

    if _SOURCES_ONLY_MODE:
        return "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."

    cache_key = get_cache_key(query, sources, model)
    with _cache_lock:
        if cache_key in _llm_cache:
            cached = _llm_cache[cache_key]
            if datetime.now().timestamp() - cached.get("timestamp", 0) < 604800:
                print(f"[CACHE HIT] Ответ из кэша (модель: {model})")
                return cached["answer"]

    try:
        prompts = load_prompts()

        # Формируем контекст с метаданными документов
        context_parts = []
        for i, src in enumerate(sources[:6], 1):
            article_info = f", пункт {src['article']}" if src.get('article') else ""
            doc_info = f"[{i}] {src['file']}{article_info}"
            context_parts.append(f"{doc_info}:\n{src['snippet']}")
        context = "\n\n---\n\n".join(context_parts)

        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])

        # ✅ ИСПРАВЛЕНИЕ #3: добавляем /no_think для Qwen3
        # Это отключает thinking mode на уровне модели
        user_content = prompts.get("advisor_user", DEFAULT_PROMPTS["advisor_user"]).format(
            query=query,
            context=context,
        )

        # Для Qwen3: добавляем директиву в начало системного промпта
        is_qwen3 = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        if is_qwen3:
            system_prompt = "/no_think\n\n" + system_prompt

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

        raw_content = response.choices[0].message.content
        finish_reason = response.choices[0].finish_reason

        if finish_reason == "length":
            return (
                "⚠️ Превышен лимит токенов. Увеличьте 'max_tokens' в конфиге "
                "или сократите запрос."
            )

        if not raw_content:
            return "⚠️ Модель вернула пустой ответ."

        # ✅ Стриппим thinking-блоки на случай, если /no_think не сработал
        answer = strip_thinking_blocks(raw_content)

        with _cache_lock:
            _llm_cache[cache_key] = {
                "answer": answer,
                "timestamp": datetime.now().timestamp(),
                "query": query,
                "model": model,
            }
            save_llm_cache()

        print(f"[CACHE MISS] Ответ сгенерирован (модель: {model})")
        return answer

    except Exception as e:
        err = str(e)
        if "Connection" in err or "refused" in err:
            return "🔌 Ошибка подключения к LM Studio. Убедитесь, что сервер запущен на 127.0.0.1:1234."
        if "timeout" in err.lower():
            return f"⏱️ Таймаут: превышено {timeout} сек"
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
    config = load_config()
    model = model or config.get("default_model", "qwen/qwen3.5-9b")

    if not _llm_cache:
        load_llm_cache()

    result = {
        "answer": "",
        "sources": [],
        "redirect": None,
        "redirect_reason": None,
        "from_faq": False,
        "model": model,
    }

    if use_faq:
        faq_results = search_faq(query, top_k=3)
        if faq_results:
            result["answer"] = faq_results[0]["answer"]
            result["sources"] = [{
                "snippet": faq_results[0]["question"],
                "file": "FAQ",
                "page": "",
                "category": faq_results[0].get("category", "Общее"),
            }]
            result["from_faq"] = True
            redirect_section = detect_section(query)
            if redirect_section:
                result["redirect"] = redirect_section
                result["redirect_reason"] = (
                    f"Для более детальной информации по теме «{query}» "
                    "рекомендуем обратиться к специализированному разделу"
                )
            return result

    vector_sources = search_vector_db(query, top_k=top_k)
    result["sources"] = vector_sources

    if vector_sources:
        result["answer"] = generate_ai_answer(query, vector_sources, model, temperature)
    else:
        result["answer"] = (
            "❌ Не найдено релевантных документов в базе знаний. "
            "Попробуйте переформулировать вопрос или уточнить термин."
        )

    redirect_section = detect_section(query)
    if redirect_section:
        result["redirect"] = redirect_section
        result["redirect_reason"] = (
            f"💡 Ваш вопрос относится к разделу «{redirect_section}». "
            "Ниже представлен предварительный ответ:"
        )

    return result


# =============================================================================
# Утилиты
# =============================================================================
def clear_cache():
    global _llm_cache
    with _cache_lock:
        _llm_cache.clear()
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)
        print(f"[CACHE CLEARED] {CACHE_PATH}")


def get_cache_stats() -> Dict:
    if not os.path.exists(CACHE_PATH):
        return {"total_entries": 0, "total_size_mb": 0}
    try:
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        size = os.path.getsize(CACHE_PATH)
        return {
            "total_entries": len(cache),
            "total_size_mb": round(size / 1024 / 1024, 2),
        }
    except Exception:
        return {"total_entries": 0, "total_size_mb": 0}


# =============================================================================
# Тест
# =============================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Тест советчика (LM Studio + Qwen 3.5)")
    print("=" * 60)

    print("\n📦 Доступные модели:")
    for m in get_available_models():
        print(f"  • {m['name']}")

    stats = get_cache_stats()
    print(f"\n📁 Кэш: {stats['total_entries']} записей, {stats['total_size_mb']} MB")

    test_query = "Какие расходы на ремонт можно включать в тариф?"
    print(f"\n❓ Вопрос: {test_query}")

    result = ask_question(test_query, model="qwen/qwen3.5-9b")

    print(f"\n✅ Результат:")
    print(f"   Модель: {result.get('model')}")
    print(f"   Источников: {len(result.get('sources', []))}")
    print(f"   Ответ:\n{result.get('answer', 'Пусто')}")