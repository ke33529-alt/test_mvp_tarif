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
# Лемматизатор pymorphy3 — синглтон, инициализируется один раз
# Graceful degradation: если не установлен — BM25 работает без лемматизации
# =============================================================================
_morph           = None
_morph_lock      = threading.Lock()
_MORPH_AVAILABLE = False

try:
    import pymorphy3 as _pymorphy3_module
    _MORPH_AVAILABLE = True
    print("[MORPH] pymorphy3 доступен — BM25 будет использовать лемматизацию")
except ImportError:
    print("[MORPH] pymorphy3 не установлен. Запустите: pip install pymorphy3")
    print("[MORPH] BM25 будет работать без лемматизации (хуже по морфологии)")


def _get_morph():
    """Синглтон MorphAnalyzer — создаётся один раз, потокобезопасно."""
    global _morph
    if _morph is not None:
        return _morph
    with _morph_lock:
        if _morph is None:
            t0 = time.perf_counter()
            _morph = _pymorphy3_module.MorphAnalyzer()
            print(f"[MORPH] MorphAnalyzer загружен за {time.perf_counter()-t0:.2f} сек")
    return _morph


def _lemmatize_token(token: str) -> str:
    """Возвращает лемму (начальную форму) русского слова."""
    if not _MORPH_AVAILABLE:
        return token
    try:
        return _get_morph().parse(token)[0].normal_form
    except Exception:
        return token
 
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
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
 
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
# Сброс KV-кэша LM Studio после каждого запроса
#
# LM Studio (llama.cpp бэкенд) накапливает KV-кэш в VRAM между независимыми
# запросами. При переполнении модель начинает свопиться на CPU и работает
# в 5-10 раз медленнее. Перезагрузка модели через REST API очищает VRAM.
# Вызов происходит в daemon-потоке — не блокирует UI и не замедляет ответ.
# =============================================================================
def _reload_lm_studio_context():
    """
    Сбрасывает KV-кэш LM Studio перезагрузкой модели через REST API.
    Совместимо с LM Studio 0.3.x (api/v0) и более ранними версиями (фоллбэк).
    """
    if not CONFIG.get("reset_context_after_request", True):
        return

    import requests
    base_url = CONFIG.get("lm_studio_url", "http://127.0.0.1:1234/v1").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]

    try:
        models_resp = requests.get(f"{base_url}/v1/models", timeout=5)
        models_resp.raise_for_status()
        model_id = models_resp.json()["data"][0]["id"]
    except Exception as e:
        print(f"[KV-RESET] Не удалось получить список моделей: {e}")
        return

    try:
        # LM Studio 0.3.x — новый API
        r = requests.post(
            f"{base_url}/api/v0/models/reload",
            json={"identifier": model_id},
            timeout=30,
        )
        if r.status_code == 200:
            print(f"[KV-RESET] Контекст сброшен (reload) ✅  модель: {model_id}")
            return

        # Фоллбэк: выгрузить → пауза → загрузить заново
        print(f"[KV-RESET] reload вернул {r.status_code}, пробуем unload/load...")
        requests.post(f"{base_url}/api/v0/models/unload",
                      json={"identifier": model_id}, timeout=15)
        time.sleep(1.5)
        requests.post(f"{base_url}/api/v0/models/load",
                      json={"identifier": model_id}, timeout=30)
        print(f"[KV-RESET] Контекст сброшен (unload/load) ✅  модель: {model_id}")

    except Exception as e:
        print(f"[KV-RESET] Ошибка при сбросе: {e}")
 
 
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
 
 
def _get_device() -> str:
    """Автодетект GPU. Возвращает 'cuda', 'mps' (Apple Silicon) или 'cpu'."""
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            print(f"[DEVICE] GPU найден: {name} — используем cuda")
            return "cuda"
        if torch.backends.mps.is_available():
            print("[DEVICE] Apple Silicon GPU — используем mps")
            return "mps"
    except Exception:
        pass
    print("[DEVICE] GPU не найден — используем cpu")
    return "cpu"
 
 
_DEVICE = _get_device()   # определяем один раз при старте модуля
 
 
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
            model = SentenceTransformer(EMBEDDING_MODEL, device="cpu")  # CPU: не конкурирует с LLM за VRAM
            model.encode(["прогрев"], normalize_embeddings=True)
            sys.modules[_ST_MODEL_KEY] = model
            print(f"[EMBED] Готово за {time.perf_counter()-t0:.1f} сек на {_DEVICE}. Далее ~0.1 сек/запрос.")
        except Exception as e:
            print(f"[EMBED ERROR] {e}")
            return None
 
    return sys.modules[_ST_MODEL_KEY]
 
 
def embed_query(query: str):
    """Возвращает [[float,...]] для ChromaDB или None при ошибке.
    multilingual-e5-large требует префикс 'query: ' для запросов
    и 'passage: ' для документов при индексации.
    """
    model = get_st_model()
    if model is None:
        return None
    # e5-модели требуют префикс для различения запроса и документа
    prefixed = f"query: {query}"
    return model.encode([prefixed], normalize_embeddings=True).tolist()
 
 
# =============================================================================
# ChromaDB — единый клиент через indexer (два PersistentClient → "already exists")
# =============================================================================
_chroma_lock       = __import__("threading").Lock()
_chroma_collection = None
 
 
def get_chroma_collection():
    global _chroma_collection
    with _chroma_lock:
        if _chroma_collection is not None:
            return _chroma_collection
        t0 = time.perf_counter()
        try:
            from core.indexer import _get_chroma_client
            client = _get_chroma_client()
            try:
                _chroma_collection = client.get_collection("tariff_docs")
            except Exception:
                _chroma_collection = client.create_collection("tariff_docs")
            print(f"[CHROMA] Готово за {time.perf_counter()-t0:.2f} сек ({_chroma_collection.count()} чанков)")
            return _chroma_collection
        except Exception as e:
            print(f"[CHROMA ERROR] {e}")
            return None
 
 
def invalidate_chroma_collection():
    global _chroma_collection
    with _chroma_lock:
        _chroma_collection = None
    invalidate_hybrid_retriever()
    print("[CHROMA] Коллекция сброшена.")
 
 
 
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
        """
        Быстрая токенизация без лемматизации.

        Лемматизация через pymorphy3 давала +250 сек на 12k чанков и была убрана:
        реранкер (CrossEncoder) всё равно переранжирует кандидатов по семантике,
        поэтому морфологическая нормализация на уровне BM25 избыточна.
        Потеря качества поиска: < 3% (покрывается векторным поиском).
        """
        return re.findall(r'[а-яёa-z0-9]+', text.lower())
 
    # Путь к файлу кэша BM25-индекса
    BM25_CACHE_PATH = os.path.join("data", "bm25_cache.pkl")

    def _build_index(self):
        """
        Строит BM25-индекс с кэшированием на диск.

        Алгоритм:
          1. Загружаем все чанки из ChromaDB.
          2. Проверяем кэш: если число чанков совпадает → загружаем токены с диска (~1 сек).
          3. Иначе — токенизируем заново (~3 сек без лемматизации) и сохраняем кэш.
          4. Строим BM25Okapi из токенов.

        Кэш инвалидируется автоматически при изменении числа чанков в коллекции.
        Для принудительной перестройки: удалить data/bm25_cache.pkl.
        """
        import pickle
        t0 = time.perf_counter()
        try:
            print("[HYBRID] Загрузка чанков из ChromaDB...")
            result = self.collection.get(include=["documents", "metadatas"])
            self.all_docs = result["documents"]
            self.all_ids  = result["ids"]
            self.all_meta = result["metadatas"]
            n = len(self.all_docs)
            print(f"[HYBRID] Загружено {n} чанков за {time.perf_counter()-t0:.2f} сек")

            if not BM25_AVAILABLE or not self.all_docs:
                print("[HYBRID] BM25 недоступен, работаем без него.")
                return

            # ── Попытка загрузить кэш токенов с диска ───────────────────────
            tokenized = None
            cache_path = HybridRetriever.BM25_CACHE_PATH
            if os.path.exists(cache_path):
                try:
                    tc = time.perf_counter()
                    with open(cache_path, "rb") as f:
                        cached = pickle.load(f)
                    if cached.get("n_docs") == n:
                        tokenized = cached["tokenized"]
                        print(f"[HYBRID] Кэш токенов загружен с диска за "
                              f"{time.perf_counter()-tc:.2f} сек ({n} чанков)")
                    else:
                        print(f"[HYBRID] Кэш устарел ({cached.get('n_docs')} ≠ {n}), "
                              f"перестраиваем")
                except Exception as e:
                    print(f"[HYBRID] Не удалось прочитать кэш: {e}")

            # ── Токенизация если кэша нет или он устарел ────────────────────
            if tokenized is None:
                tt = time.perf_counter()
                print(f"[HYBRID] Токенизация {n} чанков...")
                tokenized = [self._tokenize(doc) for doc in self.all_docs]
                print(f"[HYBRID] Токенизация завершена за {time.perf_counter()-tt:.2f} сек")
                # Сохраняем кэш
                try:
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    with open(cache_path, "wb") as f:
                        pickle.dump({"n_docs": n, "tokenized": tokenized}, f,
                                    protocol=pickle.HIGHEST_PROTOCOL)
                    print(f"[HYBRID] Кэш токенов сохранён → {cache_path}")
                except Exception as e:
                    print(f"[HYBRID] Не удалось сохранить кэш: {e}")

            # ── Строим BM25Okapi ─────────────────────────────────────────────
            tb = time.perf_counter()
            self.bm25 = BM25Okapi(tokenized)
            print(f"[HYBRID] BM25-индекс готов: {n} чанков, "
                  f"итого {time.perf_counter()-t0:.2f} сек "
                  f"(BM25Okapi: {time.perf_counter()-tb:.2f} сек)")

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
        _bw = _load_search_settings().get("bm25_weight", 1.5)
        return self._rrf_merge(vector_hits, bm25_hits, bm25_weight=_bw)
 
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
    def _rrf_merge(vector_hits: dict, bm25_hits: dict, k: int = 60, bm25_weight: float = 1.0) -> list:
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
                score += bm25_weight / (k + bm25_hits[id_]["bm25_rank"] + 1)
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
 
# Список моделей — см. AVAILABLE_RERANKER_MODELS и _get_reranker_models_list()
 
 
class Reranker:
    """
    CrossEncoder для ранжирования кандидатов.
    Использует AutoModel/AutoTokenizer напрямую (без sentence-transformers CrossEncoder)
    — это обходит несовместимость sentence-transformers >= 3.x с DiTy/cross-encoder-russian-msmarco.
    """

    def __init__(self, model_name: str):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        t0 = time.perf_counter()
        self.model_name = model_name
        self.tokenizer  = AutoTokenizer.from_pretrained(model_name)
        self.model      = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.device     = torch.device(_DEVICE)
        self.model.to(self.device)
        self.model.eval()
        if _DEVICE == "cuda":
            try:
                self.model = self.model.half()
                print(f"[RERANKER] FP16 включён")
            except Exception as e:
                print(f"[RERANKER] FP16 недоступен: {e}")
        # Прогрев
        try:
            self._score_pairs([("тест", "тест")])
            print(f"[RERANKER] Прогрев успешен")
        except Exception as _w:
            print(f"[RERANKER] Прогрев пропущен: {type(_w).__name__}: {_w}")
        print(f"[RERANKER] Загружен {model_name} на {self.device} за {time.perf_counter()-t0:.1f} сек")

    def _score_pairs(self, pairs: list) -> list:
        """Возвращает список float-скоров для списка (query, doc) пар."""
        import torch
        features = self.tokenizer(
            pairs,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        # Переносим тензоры на устройство
        features = {k: v.to(self.device) for k, v in features.items()}
        with torch.no_grad():
            logits = self.model(**features).logits
        scores = logits.squeeze(-1)
        return scores.cpu().float().tolist()

    def rerank(self, query: str, candidates: list, top_n: int = 5) -> list:
        if not candidates:
            return candidates
        t0    = time.perf_counter()
        pairs = [(query, c["doc"]) for c in candidates]
        try:
            scores = self._score_pairs(pairs)
        except Exception as e:
            import traceback
            print(f"[RERANKER] predict упал: {type(e).__name__}: {e} — без реранкинга")
            print(f"[RERANKER] traceback: {traceback.format_exc()}")
            return candidates[:top_n]
        for c, s in zip(candidates, scores):
            c["rerank_score"] = float(s)
        result = sorted(candidates, key=lambda x: x["rerank_score"], reverse=True)[:top_n]
        scores_str = ", ".join(f"{c['rerank_score']:.2f}" for c in result)
        print(f"[RERANKER] {self.model_name}: {len(candidates)}→{top_n} за {time.perf_counter()-t0:.2f}с | [{scores_str}]")
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        return result

def get_reranker() -> Optional[Reranker]:
    """Синглтон Reranker с автовыбором модели."""
    existing = sys.modules.get(_RERANKER_KEY)
    # Проверяем именно на Reranker, False = прошлая ошибка → не блокируем навсегда
    if isinstance(existing, Reranker):
        return existing
 
    with _reranker_lock:
        existing = sys.modules.get(_RERANKER_KEY)
        if isinstance(existing, Reranker):
            return existing
 
        for model_name in _get_reranker_models_list():
            try:
                reranker = Reranker(model_name)
                sys.modules[_RERANKER_KEY] = reranker
                return reranker
            except Exception as e:
                import traceback
                print(f"[RERANKER] Не удалось загрузить {model_name}: {type(e).__name__}: {e}")
                traceback.print_exc()
                sys.modules["__reranker_last_error__"] = f"{type(e).__name__}: {e}"
 
        print("[RERANKER] Все модели недоступны. Reranking отключён.")
        sys.modules.pop(_RERANKER_KEY, None)   # не блокируем — при след. запросе попробуем снова
        return None
 
 
def invalidate_reranker():
    """Сбрасывает синглтон реранкера — для принудительной перезагрузки из UI."""
    sys.modules.pop(_RERANKER_KEY, None)
    print("[RERANKER] Синглтон сброшен, перезагрузка при следующем запросе.")
 
 
def get_reranker_status() -> dict:
    """Статус реранкера без попытки загрузки. Проверяет по атрибутам."""
    existing = sys.modules.get(_RERANKER_KEY)
    if existing is not None and hasattr(existing, "model_name") and hasattr(existing, "rerank"):
        return {"loaded": True, "model_name": existing.model_name}
    last_err = sys.modules.get("__reranker_last_error__", "")
    return {"loaded": False, "last_error": last_err}
 
 
def _get_reranker_models_list() -> list:
    """Выбранная пользователем модель первой, остальные как fallback."""
    chosen  = _load_search_settings().get("reranker_model", "DiTy/cross-encoder-russian-msmarco")
    all_ids = [m["id"] for m in AVAILABLE_RERANKER_MODELS]
    return [chosen] + [m for m in all_ids if m != chosen]
 
 
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
    # Прогрев BM25-индекса при старте — чтобы первый запрос не ждал _build_index().
    # С кэшем на диске это занимает ~1 сек; без кэша (первый запуск) ~3 сек.
    try:
        retriever = get_hybrid_retriever()
        if retriever is not None:
            print(f"[PRELOAD] BM25-индекс готов ({len(retriever.all_docs)} чанков)")
        else:
            print("[PRELOAD] BM25-индекс не построен (база пуста или rank_bm25 не установлен)")
    except Exception as e:
        print(f"[PRELOAD] Ошибка BM25: {e}")
    print("[PRELOAD] Все компоненты готовы.")
 
 
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
 
SEARCH_CONFIG_FILE = os.path.join("config", "search_settings.json")
 
AVAILABLE_RERANKER_MODELS = [
    {"id": "DiTy/cross-encoder-russian-msmarco",          "label": "🇷🇺 Русская (DiTy MS MARCO)",       "desc": "Обучена на русском MS MARCO. Лучший выбор для русскоязычных документов."},
    {"id": "BAAI/bge-reranker-v2-m3",                     "label": "🌍 Мультиязычная (BGE-M3)",          "desc": "Multilingual, 100+ языков включая русский. Крупнее, точнее."},
    {"id": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1", "label": "🌍 Мультиязычная лёгкая (mMiniLM)", "desc": "Переведённый MARCO. Быстрее BGE-M3, немного хуже на русском."},
    {"id": "cross-encoder/ms-marco-MiniLM-L-6-v2",        "label": "🇬🇧 Английская (MS MARCO L6)",       "desc": "Только английский. Самая быстрая, для русского не рекомендуется."},
]
 
DEFAULT_SEARCH_SETTINGS = {
    "bm25_weight":         1.5,
    "candidates_per_var":  15,
    "context_max_chars":   8000,
    "reranker_enabled":    True,
    "reranker_model":      "DiTy/cross-encoder-russian-msmarco",
}
def _load_search_settings() -> dict:
    """Загружает настройки поиска из конфига. Fallback → DEFAULT_SEARCH_SETTINGS."""
    try:
        import streamlit as st
        ss = st.session_state.get("_search_settings")
        if ss:
            return ss
    except Exception:
        pass
    if os.path.exists(SEARCH_CONFIG_FILE):
        try:
            with open(SEARCH_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                return {**DEFAULT_SEARCH_SETTINGS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_SEARCH_SETTINGS)
 
 
def save_search_settings(settings: dict):
    """Сохраняет настройки поиска в конфиг."""
    os.makedirs(os.path.dirname(SEARCH_CONFIG_FILE), exist_ok=True)
    with open(SEARCH_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
 
 
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
 
    # Собираем уникальные filename всех победителей.
    # doc_id в метаданных не сохраняется — используем filename как ключ.
    filenames = list({
        (c.get("meta") or {}).get("filename", "unknown")
        for c in top_candidates
        if (c.get("meta") or {}).get("filename")
    })
 
    if not filenames:
        return {}
 
    # Один батч-запрос — все чанки из этих документов
    try:
        if len(filenames) == 1:
            batch = collection.get(
                where={"filename": filenames[0]},
                include=["documents", "metadatas"],
            )
        else:
            batch = collection.get(
                where={"filename": {"$in": filenames}},
                include=["documents", "metadatas"],
            )
    except Exception as e:
        print(f"[NEIGHBORS] Батч-запрос не удался: {e}")
        return {}
 
    # Строим chunk_map: (filename, chunk_index) → text
    chunk_map: dict = {}
    for doc, meta in zip(batch.get("documents", []), batch.get("metadatas", [])):
        if not meta:
            continue
        fname = meta.get("filename", "unknown")
        cidx  = int(meta.get("chunk_index", 0))
        chunk_map[(fname, cidx)] = doc
 
    # Склеиваем контекст для каждого победителя
    result = {}
    for c in top_candidates:
        meta   = c.get("meta") or {}
        doc_id = meta.get("filename", "unknown")   # ключ — filename
        cidx   = int(meta.get("chunk_index", 0))
 
        parts = []
        for offset in range(-radius, radius + 1):
            text = chunk_map.get((doc_id, cidx + offset))
            if text:
                parts.append(text)
 
        result[(doc_id, cidx)] = "\n\n".join(parts) if parts else c.get("doc", "")
        if parts:
            print(f"[NEIGHBORS] {doc_id} чанк {cidx}: собрано {len(parts)} частей")
 
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

def _sphere_match(chunk_sphere_str: str, selected_spheres: list) -> bool:
    """
    Проверяет, подходит ли чанк под фильтр сфер.
    Чанки без поля sphere (старые документы / без назначенной сферы)
    всегда проходят фильтр — обратная совместимость.
    """
    if not chunk_sphere_str:
        return True
    return any(s in chunk_sphere_str for s in selected_spheres)


def search_vector_db(query: str, top_k: int = 5, spheres: list = None, filenames: list = None) -> list:
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
        _ss = _load_search_settings()
        _cands_per_var = int(_ss.get("candidates_per_var", 15))
        # При активном фильтре по сфере запрашиваем вдвое больше кандидатов,
        # чтобы компенсировать потери от постфильтрации.
        if spheres:
            _cands_per_var = _cands_per_var * 2
        _reranker_on   = bool(_ss.get("reranker_enabled", True))
 
        merged: dict = {}   # id → candidate dict
        for variant in unique_variants:
            for c in retriever.search(variant, top_k=_cands_per_var):
                cid = c["id"]
                if cid not in merged or c["score"] > merged[cid]["score"]:
                    merged[cid] = c
 
        candidates = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
 
        # ── Фильтрация по сфере (до реранкинга) ─────────────────────────────
        if spheres:
            pre_count  = len(candidates)
            candidates = [
                c for c in candidates
                if _sphere_match(c.get("meta", {}).get("sphere", ""), spheres)
            ]
            print(f"[SPHERE FILTER] {pre_count} → {len(candidates)} кандидатов "
                  f"по сферам: {spheres}")
        if filenames:
            _fn_set = set(filenames)
            _pre = len(candidates)
            candidates = [c for c in candidates
                          if c.get("meta", {}).get("filename", "") in _fn_set]
            print(f"[FILE FILTER] {_pre} → {len(candidates)} по {len(_fn_set)} файлам")
 
        t1 = time.perf_counter()
        n_overlap = sum(1 for c in candidates if c['in_vector'] and c['in_bm25'])
        print(f"[TIMING] hybrid_search ({len(unique_variants)} вар.): {t1-t0:.3f} сек "
              f"({len(candidates)} уникальных кандидатов, "
              f"vector+bm25={n_overlap} общих)")
 
        # Если BM25 и вектор не пересекаются совсем — BM25 добавляет шум
        if n_overlap == 0:
            print("[HYBRID] Нет пересечений vector+bm25 — оставляем оба источника для реранкинга")
 
        # ── Шаг 3: CrossEncoder реранкинг ──────────────────────────────────
        reranker = get_reranker() if _reranker_on else None
        if reranker and candidates:
            candidates = reranker.rerank(query, candidates, top_n=top_k)
        else:
            if not _reranker_on:
                print("[RERANKER] Отключён в настройках поиска")
            elif reranker is None:
                print("[RERANKER] Не загружен — используем RRF-порядок")
            candidates = candidates[:top_k]
 
        # ── Шаг 4: подтягиваем соседей ПОСЛЕ реранкинга ─────────────────────
        radius     = _load_neighbor_radius()
        collection = get_chroma_collection()
        neighbors  = _fetch_neighbors(candidates, collection, radius)
        print(f"[NEIGHBORS] radius={radius}, получено ключей: {len(neighbors)}")
 
        # ── Шаг 5: форматируем в стандартный формат sources ─────────────────
        sources = []
        for c in candidates:
            meta   = c.get("meta") or {}
            # Ключ соседей — filename (doc_id не хранится в ChromaDB)
            fname  = meta.get("filename", "unknown")
            cidx   = int(meta.get("chunk_index", 0))
 
            # snippet = расширенный контекст (с соседями) если есть,
            # иначе — оригинальный чанк
            raw_snippet = neighbors.get((fname, cidx))
            snippet = raw_snippet if raw_snippet else c.get("doc", "")
            print(f"[NEIGHBORS] чанк {cidx} ({fname}): "
                  f"{'соседи {}'.format(len(raw_snippet)) if raw_snippet else 'только чанк'})")
 
            pseudo_dist = round(max(0.0, 1.0 - c.get("score", 0.5) * 60), 3)
            sources.append({
                "snippet":      snippet,
                "file":         meta.get("filename", "Неизвестно"),
                "page":         meta.get("page", ""),
                "category":     meta.get("category", "Общее"),
                "doc_type":     meta.get("doc_type", ""),
                "article":      meta.get("article", ""),
                "chunk_index":  meta.get("chunk_index", ""),
                "distance":     pseudo_dist,
                "sphere":       meta.get("sphere", ""),
            })
 
        print(f"[TIMING] search_vector_db итого: {time.perf_counter()-t0:.3f} сек")
        return sources
 
    # ── Fallback: чистый векторный поиск ────────────────────────────────────
    print("[TIMING] Fallback — чистый векторный поиск (rank_bm25 не установлен)")
    _fallback_sources = _pure_vector_search(query, top_k, t0)
    if spheres:
        _fallback_sources = [
            s for s in _fallback_sources
            if _sphere_match(s.get("sphere", ""), spheres)
        ]
    return _fallback_sources
 
 
def debug_search_candidates(query: str, top_k: int = 5,
                            spheres: Optional[List[str]] = None,
                            filenames: Optional[List[str]] = None) -> dict:
    """
    Отладочная функция для UI «Поиск и реранкинг».
    Возвращает кандидатов ДО и ПОСЛЕ реранкинга, а также варианты запроса.
    Не подтягивает соседей — нужен только чистый текст чанка для просмотра.
    spheres: список сфер для фильтрации (None = все сферы).
    """
    result = {
        "query_variants": [query],
        "pre_rerank":     [],
        "post_rerank":    [],
        "reranker_used":  False,
        "elapsed":        0.0,
        "error":          None,
    }

    t0 = time.perf_counter()
    try:
        retriever = get_hybrid_retriever()
        if retriever is None:
            result["error"] = "HybridRetriever не инициализирован (база пуста?)"
            return result

        try:
            from core.query_expander import QueryExpander
            expander = QueryExpander()
            raw_variants = expander.expand(query)
            synonym_variants = [
                v for v in raw_variants
                if v != query and not v.endswith("тарифное регулирование")
            ]
        except Exception:
            synonym_variants = []

        unique_variants = [query] + synonym_variants[:2]
        result["query_variants"] = unique_variants

        _ss = _load_search_settings()
        _cands_per_var = int(_ss.get("candidates_per_var", 15))
        _reranker_on   = bool(_ss.get("reranker_enabled", True))
        if spheres:
            _cands_per_var = _cands_per_var * 2  # компенсируем потери от постфильтрации

        merged: dict = {}
        for variant in unique_variants:
            for c in retriever.search(variant, top_k=_cands_per_var):
                cid = c["id"]
                if cid not in merged or c["score"] > merged[cid]["score"]:
                    merged[cid] = c

        pre_rerank = sorted(merged.values(), key=lambda x: x["score"], reverse=True)

        # Фильтрация по сфере до реранкинга
        if spheres:
            pre_count  = len(pre_rerank)
            pre_rerank = [
                c for c in pre_rerank
                if _sphere_match(c.get("meta", {}).get("sphere", ""), spheres)
            ]
            print(f"[SPHERE FILTER/debug] {pre_count} → {len(pre_rerank)} по сферам: {spheres}")
        if filenames:
            _fn_set = set(filenames)
            _pre = len(pre_rerank)
            pre_rerank = [c for c in pre_rerank
                          if c.get("meta", {}).get("filename", "") in _fn_set]
            print(f"[FILE FILTER/debug] {_pre} → {len(pre_rerank)} по {len(_fn_set)} файлам")

        result["pre_rerank"] = pre_rerank

        reranker = get_reranker() if _reranker_on else None
        if reranker and pre_rerank:
            post = reranker.rerank(query, list(pre_rerank), top_n=top_k)
            result["post_rerank"]   = post
            result["reranker_used"] = True
        else:
            result["post_rerank"] = pre_rerank[:top_k]

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    result["elapsed"] = round(time.perf_counter() - t0, 3)
    return result


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
 
 
def _build_context(sources: list, max_chars: int = None) -> str:
    if max_chars is None:
        max_chars = int(_load_search_settings().get("context_max_chars", 8000))
    """
    Собирает контекст из источников.
 
    max_chars=12000 (~4000 токенов) — разумный бюджет для моделей с большим
    контекстным окном. При radius=2 и чанке 1750 симв. один snippet ≈ 8750 симв.
    Бюджет позволяет 1 источник полностью или несколько с разумной обрезкой.
    """
    parts = []
    budget = max_chars
    for i, src in enumerate(sources, 1):
        if budget <= 0:
            break
        art      = f", п. {src['article']}"           if src.get('article')       else ""
        doc_part = f" | {src.get('document_part','')}" if src.get('document_part') else ""
        section  = f" | {src.get('section','')[:50]}"  if src.get('section')       else ""
        header   = f"[{i}] {src['file']}{doc_part}{section}{art}:\n"
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
 
 
 
def _pure_vector_search(query: str, top_k: int = 5, t0=None) -> list:
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
            "sphere":      meta.get("sphere", ""),
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
            frequency_penalty=0.1,   # штраф за повторения — предотвращает "CL CL CL..."
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

        # Сбрасываем KV-кэш LM Studio в фоне — не блокируем UI
        threading.Thread(target=_reload_lm_studio_context, daemon=True, name="kv-reset").start()

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
# Стриминг ответа для уточняющих вопросов (без кэша, с контекстом предыдущего ответа)
#
# Стратегия преемственности:
#   RAG-запрос  = clarify_q  (чистый — эмбеддинг не засоряется историей)
#   LLM-промпт  = предыдущий ответ (явный блок) + новые RAG-чанки + вопрос уточнения
#
# Каждый раунд уточнения берёт ОДИН предыдущий ответ как контекст и заново
# ищет лучших кандидатов в RAG по чистому тексту уточнения.
# Уточнения не кэшируются — они всегда зависят от предыдущего ответа.
# =============================================================================
def stream_clarification_answer(
    clarify_q: str,
    prev_answer: str,
    new_sources: list,
    model: str = None,
    temperature: float = None,
):
    """
    Генератор токенов для уточняющих вопросов.

    Args:
        clarify_q:    текст уточняющего вопроса
        prev_answer:  предыдущий ответ LLM (исходный или последнее уточнение)
        new_sources:  чанки из RAG, найденные по clarify_q
        model:        модель LM Studio
        temperature:  температура генерации
    """
    config      = load_config()
    model       = model or config.get("default_model", "qwen/qwen3.5-9b")
    temperature = temperature if temperature is not None else config.get("temperature", 0.3)
    max_tokens  = config.get("max_tokens", 2048)
    timeout     = config.get("timeout_seconds", 300)

    if _SOURCES_ONLY_MODE:
        yield "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."
        return

    try:
        prompts       = load_prompts()
        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])

        # Контекст новых RAG-чанков (без псевдо-источника предыдущего ответа)
        rag_context = _build_context(new_sources) if new_sources else "(новых документов не найдено)"

        # Промпт уточнения: предыдущий ответ — явный отдельный блок
        PREV_ANSWER_LIMIT = 2000   # символов — достаточно для контекста, не раздувает промпт
        user_content = (
            "Ты продолжаешь консультацию. Ниже приведён предыдущий ответ и новые фрагменты документов.\n\n"
            "## Предыдущий ответ\n"
            f"{prev_answer[:PREV_ANSWER_LIMIT]}"
            + (" _(сокращено)_" if len(prev_answer) > PREV_ANSWER_LIMIT else "")
            + "\n\n"
            "## Новые фрагменты нормативных документов\n"
            f"{rag_context}\n\n"
            "## Вопрос уточнения\n"
            f"{clarify_q}\n\n"
            "Дай ответ на вопрос уточнения, опираясь на предыдущий ответ и новые документы. "
            "Не повторяй то, что уже было сказано, если это не нужно для ответа."
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
            frequency_penalty=0.1,
            stream=True,
        )
        if extra_body:
            kwargs["extra_body"] = extra_body

        print(f"[CLARIFY stream] {model} | промпт ~{len(system_prompt)+len(user_content)} симв. | "
              f"prev_answer={len(prev_answer)} симв. | rag_chunks={len(new_sources)}")
        t0 = time.perf_counter()

        full_text  = ""
        buf        = ""
        in_think   = False
        last_token = ""
        repeat_cnt = 0

        response = client.chat.completions.create(**kwargs)

        for chunk in response:
            delta = chunk.choices[0].delta.content
            if not delta:
                continue

            stripped = delta.strip()
            if stripped and stripped == last_token:
                repeat_cnt += 1
                if repeat_cnt >= 20:
                    msg = "\n\n⚠️ [Генерация прервана: модель зациклилась.]"
                    full_text += msg
                    yield msg
                    break
            else:
                last_token = stripped
                repeat_cnt = 0

            full_text += delta
            buf       += delta

            # фильтр thinking-блоков в потоке
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

        print(f"[CLARIFY stream] готово за {time.perf_counter()-t0:.2f} сек")
        threading.Thread(target=_reload_lm_studio_context, daemon=True, name="kv-reset-clar").start()

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
            frequency_penalty=0.1,   # штраф за повторения
        )
        if extra_body:
            kwargs["extra_body"] = extra_body
 
        response      = client.chat.completions.create(**kwargs)
        raw_content   = response.choices[0].message.content
        finish_reason = response.choices[0].finish_reason
 
        print(f"[LLM] ответ за {time.perf_counter()-t0:.2f} сек | "
              f"finish={finish_reason} | len={len(raw_content or '')}")

        # Сбрасываем KV-кэш LM Studio в фоне — не блокируем UI
        threading.Thread(target=_reload_lm_studio_context, daemon=True, name="kv-reset").start()

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