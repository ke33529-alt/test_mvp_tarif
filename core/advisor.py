# core/advisor.py
import os
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

# =============================================================================
# Конфигурация для LM Studio
# =============================================================================
DEFAULT_CONFIG = {
    "lm_studio_url": "http://127.0.0.1:1234/v1",
    "default_model": "qwen/qwen3.5-9b",
    "max_tokens": 2048,
    "temperature": 0.3,
    "timeout_seconds": 300,
    "cache_ttl_days": 7
}

def load_config() -> Dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return {**DEFAULT_CONFIG, **config}
        except Exception:
            pass
    return DEFAULT_CONFIG

def save_config(config: Dict):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

CONFIG = load_config()

# =============================================================================
# Клиент LM Studio
# =============================================================================
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
# Глобальный ChromaDB клиент (ИСПРАВЛЕНО)
# =============================================================================
_chroma_client = None
_chroma_collection = None
_client_lock = threading.Lock()

def get_chroma_collection():
    """
    Безопасное получение коллекции ChromaDB.
    Обрабатывает ошибку 'Instance already exists' при работе внутри Streamlit.
    """
    global _chroma_client, _chroma_collection
    
    with _client_lock:
        # Если коллекция уже создана, возвращаем её
        if _chroma_collection is not None:
            return _chroma_collection
            
        import chromadb
        
        try:
            # Попытка создать или получить клиент
            # Если клиент уже создан в другом месте (Streamlit), эта строка может выбросить ошибку
            # В новых версиях chromadb лучше использовать Settings для игнорирования конфликта
            settings = chromadb.Settings(
                anonymized_telemetry=False,
                allow_reset=True,
                is_persistent=True
            )
            _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH, settings=settings)
        except Exception as e:
            # Если ошибка "Instance already exists", пробуем получить клиент без явного создания
            # В некоторых случаях достаточно просто импортировать и работать, если путь тот же
            # Но надежнее поймать конкретную ошибку и продолжить
            print(f"[WARN] Предупреждение ChromaDB: {e}. Пробуем продолжить работу...")
            # Пытаемся пересоздать объект клиента, игнорируя предупреждение, если оно не критично
            try:
                 _chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
            except Exception:
                 # Если совсем ничего не выходит, возвращаем None (поиск не сработает)
                 return None

        try:
            # Пробуем получить существующую коллекцию
            _chroma_collection = _chroma_client.get_collection(name="tariff_docs")
        except Exception:
            # Если коллекции нет, создаём новую
            try:
                _chroma_collection = _chroma_client.create_collection(name="tariff_docs")
            except Exception as create_err:
                print(f"[ERROR] Не удалось создать коллекцию: {create_err}")
                return None
                
        return _chroma_collection

# =============================================================================
# Кэш LLM-ответов
# =============================================================================
_llm_cache = {}
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
    sources_str = json.dumps(sorted([s.get('file', '') + s.get('snippet', '')[:100] for s in sources]), sort_keys=True)
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
    "позиция фас": "⚖️ Позиция ФАС", "разъяснение фас": "⚖️ Позиция ФАС", "трактовка фас": "⚖️ Позиция ФАС",
    "прецедент": "🔍 Поиск прецедентов", "судебная практика": "🔍 Поиск прецедентов", "суд": "🔍 Поиск прецедентов", "арбитраж": "🔍 Поиск прецедентов",
    "численность": "👥 Сверка численности", "штат": "👥 Сверка численности", "сотрудники": "👥 Сверка численности",
    "амортизация": "🏭 Проверка амортизации", "ос": "🏭 Проверка амортизации", "основные средства": "🏭 Проверка амортизации",
    "фгис": "📤 Экспорт ФГИС", "экспорт": "📤 Экспорт ФГИС",
    "пояснительная": "📝 Пояснительная записка", "пояснение": "📝 Пояснительная записка",
    "риск": "📊 Калькулятор рисков", "вероятность": "📊 Калькулятор рисков",
    "жалоба": "📝 Робот-жалобщик", "оспорить": "📝 Робот-жалобщик", "отказ": "📝 Робот-жалобщик",
    "изменения": "🔄 Трекер изменений законов", "новое в законодательстве": "🔄 Трекер изменений законов",
    "скан": "📸 AI-Сканер документов", "распознать": "📸 AI-Сканер документов", "ocr": "📸 AI-Сканер документов",
    "расчет": "📊 Расчетный лист", "формула": "📊 Расчетный лист", "таблица": "📊 Расчетный лист",
    "протокол": "📋 Робот-протокольщик", "встреча": "📋 Робот-протокольщик",
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
            if query_lower in q or any(kw in query_lower for kw in q.split()[:5]):
                results.append({
                    "question": item["question"], "answer": item["answer"],
                    "category": item.get("category", "Общее"), "source": "FAQ"
                })
                if len(results) >= top_k:
                    break
        return results
    except Exception as e:
        print(f"[FAQ ERROR] {e}")
        return []

# =============================================================================
# Поиск в векторной базе (ИСПРАВЛЕНО)
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
            include=["documents", "metadatas"]
        )
        
        sources = []
        # Проверяем, есть ли результаты
        if not results or not results.get("documents") or not results["documents"][0]:
            return []
            
        for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
            if meta is None:
                meta = {}
            sources.append({
                "snippet": doc[:500] + "..." if len(doc) > 500 else doc,
                "file": meta.get("filename", "Неизвестно"), 
                "page": meta.get("page", ""),
                "category": meta.get("category", "Общее"), 
                "doc_type": meta.get("doc_type", ""),
                "article": meta.get("article", ""), 
                "paragraph": meta.get("paragraph", "")
            })
        return sources
    except Exception as e:
        print(f"[VECTOR DB ERROR] {e}")
        import traceback
        traceback.print_exc()
        return []

# =============================================================================
# Генерация ответа через LM Studio
# =============================================================================
def generate_ai_answer(query: str, sources: list, model: str = None, temperature: float = None) -> str:
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
        context = "\n\n".join([
            f"[{i+1}] {src['file']} (стр. {src['page']}): {src['snippet']}"
            for i, src in enumerate(sources[:5])
        ])

        system_prompt = """Ты — эксперт по тарифному регулированию в РФ.
Отвечай ТОЛЬКО на русском языке, кратко, структурно и по существу.
ЗАПРЕЩЕНО писать 'Thinking Process', рассуждения или объяснения шагов.
Отвечай сразу итоговым ответом: списком, таблицей или чётким утверждением.
Основывайся на предоставленном контексте и законодательстве РФ, преимущественно на RAG.
Если информации в базе знаний недостаточно — честно скажи об этом. Не выдумывай факты. Всегда в конце благодари за интересный вопрос (или укажи, что вопрос был сложный)
Если в ответе есть сравнение данных, списки расходов, тарифные ставки или параметры, сметы или расчеты — ОБЯЗАТЕЛЬНО оформи их в виде Markdown-таблицы.
Пример:
| Параметр | Значение | Ед. изм. |
|---|---|---|
| Тариф | 100.50 | руб./Гкал |"""

        user_prompt = f"Вопрос пользователя: {query}\n\nКонтекст из документов:\n{context}\n\nОтвет:"

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout
        )

        raw_content = response.choices[0].message.content
        finish_reason = response.choices[0].finish_reason

        if finish_reason == "length":
            return "⚠️ Превышен лимит токенов. Увеличьте 'max_tokens' в конфиге или сократите запрос."

        answer = raw_content.strip() if raw_content else "⚠️ Модель вернула пустой ответ."

        with _cache_lock:
            _llm_cache[cache_key] = {"answer": answer, "timestamp": datetime.now().timestamp(), "query": query, "model": model}
            save_llm_cache()

        print(f"[CACHE MISS] Ответ сгенерирован (модель: {model})")
        return answer

    except Exception as e:
        err = str(e)
        if "Connection" in err or "refused" in err:
            return "🔌 Ошибка подключения к LM Studio. Убедитесь, что сервер запущен на 127.0.0.1:1234."
        elif "timeout" in err.lower():
            return f"⏱️ Таймаут: превышено {timeout} сек"
        return f"❌ Ошибка LLM: {err}"

# =============================================================================
# Основной метод
# =============================================================================
def ask_question(query: str, top_k: int = 5, temperature: float = None, use_faq: bool = True, model: str = None) -> dict:
    config = load_config()
    model = model or config.get("default_model", "qwen/qwen3.5-9b")

    if not _llm_cache:
        load_llm_cache()

    result = {"answer": "", "sources": [], "redirect": None, "redirect_reason": None, "from_faq": False, "model": model}

    if use_faq:
        faq_results = search_faq(query, top_k=3)
        if faq_results:
            result["answer"] = faq_results[0]["answer"]
            result["sources"] = [{"snippet": faq_results[0]["question"], "file": "FAQ", "page": "", "category": faq_results[0].get("category", "Общее")}]
            result["from_faq"] = True
            redirect_section = detect_section(query)
            if redirect_section:
                result["redirect"] = redirect_section
                result["redirect_reason"] = f"Для более детальной информации по теме «{query}» рекомендуем обратиться к специализированному разделу"
            return result

    vector_sources = search_vector_db(query, top_k=top_k)
    result["sources"] = vector_sources

    if vector_sources:
        result["answer"] = generate_ai_answer(query, vector_sources, model, temperature)
    else:
        result["answer"] = "❌ Не найдено релевантных документов в базе знаний."

    redirect_section = detect_section(query)
    if redirect_section:
        result["redirect"] = redirect_section
        result["redirect_reason"] = f"💡 Ваш вопрос относится к разделу «{redirect_section}». Ниже представлен предварительный ответ:"

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
        return {"total_entries": len(cache), "total_size_mb": round(size / 1024 / 1024, 2)}
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