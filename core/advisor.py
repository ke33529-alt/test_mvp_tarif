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
# Исправление кодировки консоли на Windows (cp1252 → utf-8)
# Без этого print() падает с UnicodeEncodeError на символах →, ✅, ⚡ и т.п.
# =============================================================================
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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
# [NEW] HybridRetriever — BM25 + векторный поиск + RRF fusion
#
# Индекс BM25 строится из ChromaDB при первом обращении и хранится
# как синглтон в sys.modules — так же, как модель эмбеддингов.
# При переиндексации достаточно вызвать invalidate_hybrid_retriever().
# =============================================================================
_HYBRID_RETRIEVER_KEY = "__regula_ai_hybrid_retriever__"
_hybrid_lock          = threading.Lock()

# rank_bm25 подключается с graceful degradation: если не установлен,
# search_vector_db автоматически падает на чистый векторный поиск.
try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False
    print("[HYBRID] rank_bm25 не установлен. Запустите: pip install rank_bm25")
    print("[HYBRID] Будет использоваться только векторный поиск.")


class HybridRetriever:
    """BM25 + векторный поиск с Reciprocal Rank Fusion."""

    def __init__(self, collection):
        self.collection = collection
        self.all_docs:  list = []
        self.all_ids:   list = []
        self.all_meta:  list = []
        self.bm25 = None
        self._build_index()

    def _tokenize(self, text: str) -> list:
        """Простая токенизация: буквы/цифры, нижний регистр."""
        return re.findall(r'\w+', text.lower())

    def _build_index(self):
        t0 = time.perf_counter()
        try:
            result = self.collection.get(include=["documents", "metadatas"])
            self.all_docs  = result["documents"]
            self.all_ids   = result["ids"]
            self.all_meta  = result["metadatas"]

            if BM25_AVAILABLE and self.all_docs:
                tokenized   = [self._tokenize(doc) for doc in self.all_docs]
                self.bm25   = BM25Okapi(tokenized)
                print(f"[HYBRID] BM25-индекс построен: {len(self.all_docs)} чанков "
                      f"за {time.perf_counter()-t0:.2f} сек")
            else:
                print(f"[HYBRID] BM25 недоступен, работаем без него.")
        except Exception as e:
            print(f"[HYBRID ERROR] Ошибка построения индекса: {e}")

    # ------------------------------------------------------------------
    # Основной метод поиска
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 20) -> list:
        """
        Возвращает список кандидатов, отсортированных по RRF-score.
        Каждый кандидат: {"id", "doc", "meta", "score", "in_vector", "in_bm25"}
        """
        vector_hits = self._vector_search(query, top_k)
        bm25_hits   = self._bm25_search(query, top_k) if self.bm25 else {}
        return self._rrf_merge(vector_hits, bm25_hits)

    def _vector_search(self, query: str, top_k: int) -> dict:
        """Возвращает {id: {"doc", "meta", "vector_rank"}}"""
        try:
            embedding = embed_query(query)
            if embedding is not None:
                results = self.collection.query(
                    query_embeddings=embedding,
                    n_results=top_k,
                    include=["documents", "metadatas", "distances"],
                )
            else:
                results = self.collection.query(
                    query_texts=[query],
                    n_results=top_k,
                    include=["documents", "metadatas", "distances"],
                )

            hits = {}
            for rank, (id_, doc, meta) in enumerate(zip(
                results["ids"][0],
                results["documents"][0],
                results["metadatas"][0],
            )):
                hits[id_] = {"doc": doc, "meta": meta or {}, "vector_rank": rank}
            return hits
        except Exception as e:
            print(f"[HYBRID] Ошибка векторного поиска: {e}")
            return {}

    def _bm25_search(self, query: str, top_k: int) -> dict:
        """Возвращает {id: {"doc", "meta", "bm25_rank"}}"""
        try:
            tokens     = self._tokenize(query)
            scores     = self.bm25.get_scores(tokens)
            top_idx    = sorted(range(len(scores)),
                                key=lambda i: scores[i], reverse=True)[:top_k]
            return {
                self.all_ids[i]: {
                    "doc":      self.all_docs[i],
                    "meta":     self.all_meta[i] or {},
                    "bm25_rank": rank,
                }
                for rank, i in enumerate(top_idx)
                if scores[i] > 0   # отфильтровываем нулевые совпадения
            }
        except Exception as e:
            print(f"[HYBRID] Ошибка BM25: {e}")
            return {}

    @staticmethod
    def _rrf_merge(vector_hits: dict, bm25_hits: dict, k: int = 60) -> list:
        """
        Reciprocal Rank Fusion:
        score = 1/(k + rank_vector + 1) + 1/(k + rank_bm25 + 1)
        """
        all_ids = set(vector_hits) | set(bm25_hits)
        scored  = []
        for id_ in all_ids:
            score = 0.0
            if id_ in vector_hits:
                score += 1.0 / (k + vector_hits[id_]["vector_rank"] + 1)
            if id_ in bm25_hits:
                score += 1.0 / (k + bm25_hits[id_]["bm25_rank"] + 1)
            source = vector_hits.get(id_) or bm25_hits.get(id_)
            scored.append({
                "id":        id_,
                "doc":       source["doc"],
                "meta":      source["meta"],
                "score":     score,
                "in_vector": id_ in vector_hits,
                "in_bm25":   id_ in bm25_hits,
            })
        return sorted(scored, key=lambda x: x["score"], reverse=True)


def get_hybrid_retriever() -> Optional[HybridRetriever]:
    """Синглтон HybridRetriever. Перестраивает индекс, если коллекция изменилась."""
    if not BM25_AVAILABLE:
        return None

    existing = sys.modules.get(_HYBRID_RETRIEVER_KEY)
    collection = get_chroma_collection()
    if collection is None:
        return None

    current_count = collection.count()

    # Если синглтон есть и размер базы не изменился — возвращаем
    if existing is not None:
        if getattr(existing, "_collection_count", -1) == current_count:
            return existing

    with _hybrid_lock:
        # Двойная проверка после блокировки
        existing = sys.modules.get(_HYBRID_RETRIEVER_KEY)
        if existing is not None and getattr(existing, "_collection_count", -1) == current_count:
            return existing

        retriever = HybridRetriever(collection)
        retriever._collection_count = current_count
        sys.modules[_HYBRID_RETRIEVER_KEY] = retriever
        return retriever


def invalidate_hybrid_retriever():
    """Принудительно сбрасывает BM25-индекс (вызывать после переиндексации)."""
    sys.modules.pop(_HYBRID_RETRIEVER_KEY, None)
    print("[HYBRID] Индекс сброшен. Будет перестроен при следующем запросе.")


# =============================================================================
# [NEW] Reranker — CrossEncoder для финального ранжирования топ-K
#
# Модель mmarco-mMiniLMv2 обучена на русскоязычных данных (MS MARCO RU),
# ms-marco-MiniLM-L-6-v2 быстрее, но хуже на кириллице.
# Модель выбирается автоматически; при ошибке загрузки reranking отключается.
# =============================================================================
_RERANKER_KEY  = "__regula_ai_reranker__"
_reranker_lock = threading.Lock()

# Приоритет: скорость → качество → отключение
# L-6 (6 слоёв) ~в 2 раза быстрее L-12; для нормативных запросов разница
# в качестве несущественна, а выигрыш по времени — 3-8 сек на CPU.
_RERANKER_MODELS = [
    "cross-encoder/ms-marco-MiniLM-L-6-v2",          # быстро, ~3-5 сек на CPU
    "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",   # качественнее, но медленнее
]


class Reranker:
    """CrossEncoder для ранжирования финального списка кандидатов."""

    def __init__(self, model_name: str):
        from sentence_transformers import CrossEncoder
        t0 = time.perf_counter()
        self.model      = CrossEncoder(model_name)
        self.model_name = model_name
        # Прогрев
        self.model.predict([("тест", "тест прогрев")])
        print(f"[RERANKER] Загружен {model_name} за {time.perf_counter()-t0:.1f} сек")

    def rerank(self, query: str, candidates: list, top_n: int = 5) -> list:
        if not candidates:
            return candidates
        t0 = time.perf_counter()
        # Обрезаем текст до 350 символов: CrossEncoder обрабатывает каждый
        # символ через BERT — 800 симв. vs 350 симв. даёт ~2x ускорение
        # без потери качества ранжирования (структура нормативного текста
        # раскрывается в первых предложениях).
        pairs  = [(query, c["doc"][:350]) for c in candidates]
        scores = self.model.predict(pairs)
        for candidate, score in zip(candidates, scores):
            candidate["rerank_score"] = float(score)
        result = sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)[:top_n]
        print(f"[RERANKER] {len(candidates)} -> {top_n} кандидатов за {time.perf_counter()-t0:.2f} сек")
        return result


def get_reranker() -> Optional[Reranker]:
    """Синглтон Reranker с автовыбором модели."""
    existing = sys.modules.get(_RERANKER_KEY)
    if existing is not None:
        return existing

    with _reranker_lock:
        existing = sys.modules.get(_RERANKER_KEY)
        if existing is not None:
            return existing

        for model_name in _RERANKER_MODELS:
            try:
                reranker = Reranker(model_name)
                sys.modules[_RERANKER_KEY] = reranker
                return reranker
            except Exception as e:
                print(f"[RERANKER] Не удалось загрузить {model_name}: {e}")

        print("[RERANKER] Все модели недоступны. Reranking отключён.")
        # Ставим заглушку-None, чтобы не пытаться снова на каждом запросе
        sys.modules[_RERANKER_KEY] = False
        return None


# =============================================================================
# Предзагрузка моделей в фоне при старте
#
# Embedding-модель и CrossEncoder грузятся ~12-30 сек каждая.
# Запускаем их в daemon-потоке сразу при импорте модуля, чтобы к моменту
# первого запроса пользователя они уже были в памяти.
# Если пользователь задал вопрос раньше — get_st_model() и get_reranker()
# дождутся завершения через _st_lock / _reranker_lock (thread-safe).
# =============================================================================
def _preload_models_background():
    try:
        get_st_model()
    except Exception as e:
        print(f"[PRELOAD] Ошибка embedding: {e}")
    try:
        get_reranker()
    except Exception as e:
        print(f"[PRELOAD] Ошибка reranker: {e}")
    print("[PRELOAD] Модели готовы.")


threading.Thread(
    target=_preload_models_background,
    daemon=True,
    name="model-preload",
).start()


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
# Вспомогательные функции пайплайна поиска
# =============================================================================

def _load_neighbor_radius() -> int:
    """
    Читает neighbor_radius из Streamlit session_state (задаётся в UI советчика).
    Fallback: конфиг файл → дефолт 2.
    """
    # session_state доступен только внутри Streamlit-процесса
    try:
        import streamlit as st
        val = st.session_state.get("neighbor_radius")
        if val is not None:
            return int(val)
    except Exception:
        pass
    # Fallback: читаем из конфига (для скриптов вне Streamlit)
    try:
        cfg = os.path.join("config", "chunking_patterns.json")
        if os.path.exists(cfg):
            with open(cfg, "r", encoding="utf-8") as f:
                return int(json.load(f).get("chunking_settings", {}).get("neighbor_radius", 2))
    except Exception:
        pass
    return 2


def _fetch_neighbors(top_candidates: list, collection, radius: int) -> dict:
    """
    Для каждого кандидата из top_candidates подтягивает соседние чанки
    одним батч-запросом к ChromaDB.

    Возвращает словарь:
        (doc_id, chunk_index) → склеенный текст [сосед_л ... ЦЕЛЬ ... сосед_п]

    ВАЖНО: вызывается ПОСЛЕ реранкинга — реранкер уже отработал на коротких
    оригинальных чанках. Соседи нужны только для промпта LLM.
    """
    if radius == 0 or not top_candidates or collection is None:
        return {}

    t0 = time.perf_counter()

    # Собираем уникальные doc_id всех победителей
    doc_ids = list({
        (c.get("meta") or {}).get("doc_id")
        or (c.get("meta") or {}).get("filename", "unknown")
        for c in top_candidates
    })

    # Один батч-запрос — все чанки из этих документов
    try:
        if len(doc_ids) == 1:
            batch = collection.get(
                where={"doc_id": doc_ids[0]},
                include=["documents", "metadatas"],
            )
        else:
            batch = collection.get(
                where={"doc_id": {"$in": doc_ids}},
                include=["documents", "metadatas"],
            )
    except Exception as e:
        print(f"[NEIGHBORS] Батч-запрос не удался: {e}")
        return {}

    # Строим chunk_map: (doc_id, chunk_index) → text
    chunk_map: dict = {}
    for doc, meta in zip(batch.get("documents", []), batch.get("metadatas", [])):
        if not meta:
            continue
        did  = meta.get("doc_id") or meta.get("filename", "unknown")
        cidx = int(meta.get("chunk_index", 0))
        chunk_map[(did, cidx)] = doc

    # Склеиваем контекст для каждого победителя
    result = {}
    for c in top_candidates:
        meta   = c.get("meta") or {}
        doc_id = meta.get("doc_id") or meta.get("filename", "unknown")
        cidx   = int(meta.get("chunk_index", 0))

        parts = []
        for offset in range(-radius, radius + 1):
            text = chunk_map.get((doc_id, cidx + offset))
            if text:
                parts.append(text)

        result[(doc_id, cidx)] = "\n\n".join(parts) if parts else c.get("doc", "")

    n_expanded = sum(1 for v in result.values() if "\n\n" in v)
    print(f"[NEIGHBORS] radius={radius}, расширено {n_expanded}/{len(top_candidates)} "
          f"за {time.perf_counter()-t0:.3f} сек")
    return result


# =============================================================================
# Пайплайн поиска: синонимы → гибридный поиск → реранкинг → соседи
#
#  Шаг 1. QueryExpander расширяет запрос синонимами (ФОТ → фонд оплаты труда…)
#  Шаг 2. HybridRetriever: BM25 + vector по всем вариантам запроса → RRF
#  Шаг 3. CrossEncoder реранкирует кандидатов по ОРИГИНАЛЬНОМУ короткому тексту
#         (не по соседям — иначе будет медленно и точность снизится)
#  Шаг 4. _fetch_neighbors: для топ-K победителей подтягиваем N соседей
#         одним батч-запросом — LLM получает полный контекст вокруг чанка
#  Шаг 5. Fallback: если rank_bm25 не установлен — чистый векторный поиск
#
# Интерфейс не изменён: query + top_k → list[dict] с теми же полями.
# =============================================================================
def search_vector_db(query: str, top_k: int = 5) -> list:
    t0 = time.perf_counter()

    retriever = get_hybrid_retriever()

    if retriever is not None:

        # ── Шаг 1: расширяем запрос синонимами ──────────────────────────────
        # Берём только варианты с реальными заменами аббревиатур/синонимов.
        # Суффикс "тарифное регулирование" из query_expander намеренно
        # отсекаем — он добавляет шум когда вопрос уже про конкретный пункт.
        try:
            from core.query_expander import QueryExpander
            expander = QueryExpander()
            raw_variants = expander.expand(query)
            # Оставляем только варианты где реально что-то заменилось
            # (отличаются от исходного) и не содержат дописанных суффиксов
            synonym_variants = [
                v for v in raw_variants
                if v != query and not v.endswith("тарифное регулирование")
            ]
        except Exception:
            synonym_variants = []

        # Оригинальный запрос первым, синонимы — после, максимум 3 варианта
        unique_variants = [query] + synonym_variants[:2]

        if len(unique_variants) > 1:
            print(f"[SYNONYMS] {len(unique_variants)} вариантов: {unique_variants}")

        # ── Шаг 2: гибридный поиск по всем вариантам ────────────────────────
        # Для каждого варианта запроса делаем поиск и собираем кандидатов.
        # Один кандидат может встретиться в нескольких вариантах — берём
        # лучший (максимальный) RRF-score.
        merged: dict = {}   # id → candidate dict
        for variant in unique_variants:
            for c in retriever.search(variant, top_k=top_k * 2):
                cid = c["id"]
                if cid not in merged or c["score"] > merged[cid]["score"]:
                    merged[cid] = c

        candidates = sorted(merged.values(), key=lambda x: x["score"], reverse=True)

        t1 = time.perf_counter()
        n_overlap = sum(1 for c in candidates if c['in_vector'] and c['in_bm25'])
        print(f"[TIMING] hybrid_search ({len(unique_variants)} вар.): {t1-t0:.3f} сек "
              f"({len(candidates)} уникальных кандидатов, "
              f"vector+bm25={n_overlap} общих)")

        # Если BM25 и вектор не пересекаются совсем — BM25 добавляет шум
        if n_overlap == 0:
            candidates = [c for c in candidates if c['in_vector']]
            print("[HYBRID] Нет пересечений — только векторные результаты")

        # ── Шаг 3: CrossEncoder реранкинг на КОРОТКОМ оригинальном тексте ───
        reranker = get_reranker()
        if reranker and candidates:
            candidates = reranker.rerank(query, candidates, top_n=top_k)
        else:
            candidates = candidates[:top_k]

        # ── Шаг 4: подтягиваем соседей ПОСЛЕ реранкинга ─────────────────────
        radius     = _load_neighbor_radius()
        collection = get_chroma_collection()
        neighbors  = _fetch_neighbors(candidates, collection, radius)

        # ── Шаг 5: форматируем в стандартный формат sources ─────────────────
        sources = []
        for c in candidates:
            meta   = c.get("meta") or {}
            doc_id = meta.get("doc_id") or meta.get("filename", "unknown")
            cidx   = int(meta.get("chunk_index", 0))

            # snippet = расширенный контекст (с соседями) если есть,
            # иначе — оригинальный чанк
            snippet = neighbors.get((doc_id, cidx), c.get("doc", ""))

            pseudo_dist = round(max(0.0, 1.0 - c.get("score", 0.5) * 60), 3)
            sources.append({
                "snippet":      snippet[:2000] + ("..." if len(snippet) > 2000 else ""),
                "file":         meta.get("filename", "Неизвестно"),
                "page":         meta.get("page", ""),
                "category":     meta.get("category", "Общее"),
                "doc_type":     meta.get("doc_type", ""),
                "article":      meta.get("article", ""),
                "chunk_index":  meta.get("chunk_index", ""),
                "distance":     pseudo_dist,
            })

        print(f"[TIMING] search_vector_db итого: {time.perf_counter()-t0:.3f} сек")
        return sources

    # ── Fallback: чистый векторный поиск ────────────────────────────────────
    print("[TIMING] Fallback — чистый векторный поиск (rank_bm25 не установлен)")
    return _pure_vector_search(query, top_k, t0)


def _is_valid_answer(text: str) -> bool:
    """Проверяет что ответ не сломан (петля, пустой, слишком короткий)."""
    if not text or len(text.strip()) < 20:
        return False
    # Петля: один токен повторяется много раз
    words = text.split()
    if len(words) > 10:
        for i in range(len(words) - 10):
            if len(set(words[i:i+10])) <= 2:
                return False
    return True


def _build_context(sources: list, max_chars: int = 3000) -> str:
    """
    Собирает контекст из источников с жёстким ограничением суммарного размера.

    max_chars подобран под qwen3.5-9b (4096 токенов ≈ ~12 000 символов,
    из которых ~6000 оставляем на системный промпт + вопрос + ответ).
    При radius=2 один snippet = ~4500 симв., бюджет позволяет 1-2 источника
    полностью или 3-5 источников с обрезкой — в зависимости от top_k.
    """
    parts = []
    budget = max_chars
    for i, src in enumerate(sources, 1):
        if budget <= 0:
            break
        art     = f", пункт {src['article']}" if src.get('article') else ""
        header  = f"[{i}] {src['file']}{art}:\n"
        snippet = src.get("snippet", "")
        # Если этот источник не влезает целиком — обрезаем, но не пропускаем
        available = budget - len(header)
        if available <= 100:
            break
        if len(snippet) > available:
            snippet = snippet[:available] + "..."
        parts.append(header + snippet)
        budget -= len(header) + len(snippet)
    return "\n\n---\n\n".join(parts)



    """Оригинальный векторный поиск. Используется как fallback."""
    if t0 is None:
        t0 = time.perf_counter()

    collection = get_chroma_collection()
    if collection is None:
        return []

    t1        = time.perf_counter()
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

    print(f"[TIMING] search_vector_db (pure vector) итого: {time.perf_counter()-t0:.3f} сек")

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
            answer = cached.get("answer", "")
            fresh  = datetime.now().timestamp() - cached.get("timestamp", 0) < 604800
            if fresh and _is_valid_answer(answer):
                print(f"[CACHE HIT stream] {model}")
                yield answer
                return
            elif not _is_valid_answer(answer):
                print(f"[CACHE] Сломанный ответ в кэше — удаляем, генерируем заново")
                del _llm_cache[cache_key]

    # Строим промпт (та же логика что в generate_ai_answer)
    try:
        prompts = load_prompts()
        context = _build_context(sources)

        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])
        user_content  = prompts.get("advisor_user",   DEFAULT_PROMPTS["advisor_user"]).format(
            query=query, context=context,
        )

        is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        extra_body = {}
        if is_qwen3:
            user_content = "/no_think\n\n" + user_content
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
            frequency_penalty=1.2,   # штраф за повторения — предотвращает "CL CL CL..."
            stream=True,
        )
        if extra_body:
            kwargs["extra_body"] = extra_body

        print(f"[LLM stream] {model} | max_tokens={max_tokens} | "
              f"промпт ~{len(system_prompt)+len(user_content)} симв. / "
              f"~{(len(system_prompt)+len(user_content))//4} токенов (оценка)")
        t0 = time.perf_counter()

        full_text  = ""
        buf        = ""
        in_think   = False
        # Детектор петли: если один токен повторяется >20 раз подряд — обрываем
        last_token = ""
        repeat_cnt = 0

        response = client.chat.completions.create(**kwargs)

        for chunk in response:
            delta = chunk.choices[0].delta.content
            if not delta:
                continue

            # Проверяем петлю
            stripped = delta.strip()
            if stripped and stripped == last_token:
                repeat_cnt += 1
                if repeat_cnt >= 20:
                    full_text += "\n\n⚠️ [Генерация прервана: модель зациклилась. " \
                                 "Попробуйте переформулировать вопрос или уменьшить " \
                                 "радиус соседних чанков в настройках.]"
                    yield "\n\n⚠️ [Генерация прервана: модель зациклилась. " \
                          "Попробуйте переформулировать вопрос или уменьшить " \
                          "радиус соседних чанков в настройках.]"
                    break
            else:
                last_token = stripped
                repeat_cnt = 0

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
                        buf = ""
                        break
                else:
                    start = buf.find("<think>")
                    if start >= 0:
                        if start > 0:
                            yield buf[:start]
                        in_think = True
                        buf = buf[start + len("<think>"):]
                    else:
                        yield buf
                        buf = ""
                        break

        print(f"[LLM stream] готово за {time.perf_counter()-t0:.2f} сек")

        answer = strip_thinking_blocks(full_text)

        # Не кэшируем сломанные ответы (петли, пустые, слишком короткие)
        is_broken = (
            not answer.strip()
            or len(answer) < 20
            or answer.count("CL ") > 10
            or "зациклилась" in answer
        )
        if not is_broken:
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
    max_tokens  = config.get("max_tokens", 2048)
    timeout     = config.get("timeout_seconds", 300)

    if _SOURCES_ONLY_MODE:
        return "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."

    cache_key = get_cache_key(query, sources, model)
    with _cache_lock:
        if cache_key in _llm_cache:
            cached = _llm_cache[cache_key]
            answer = cached.get("answer", "")
            fresh  = datetime.now().timestamp() - cached.get("timestamp", 0) < 604800
            if fresh and _is_valid_answer(answer):
                print(f"[CACHE HIT] {model}")
                return answer
            elif not _is_valid_answer(answer):
                print(f"[CACHE] Сломанный ответ — удаляем, генерируем заново")
                del _llm_cache[cache_key]

    try:
        prompts = load_prompts()
        context = _build_context(sources)

        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])
        user_content  = prompts.get("advisor_user",   DEFAULT_PROMPTS["advisor_user"]).format(
            query=query, context=context,
        )

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
            frequency_penalty=1.2,   # штраф за повторения
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

        # Не кэшируем сломанные ответы
        is_broken = (
            not answer.strip()
            or len(answer) < 20
            or answer.count("CL ") > 10
        )
        if not is_broken:
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

    # Гибридный поиск (BM25 + vector + reranking)
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