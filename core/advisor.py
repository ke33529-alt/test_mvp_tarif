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
# Идентичность: пользователь, сегмент и неймспейс кэша
#
# ЗАЧЕМ. _llm_cache — процессный словарь, общий для ВСЕХ пользователей всех
# сегментов (один Streamlit-процесс обслуживает всех). До этой правки ключ
# кэша не содержал сегмент, поэтому ответ, сгенерированный с подмешиванием
# ЛОКАЛЬНОЙ базы сегмента A (doc_types=["local"], см. core/local_kb.py),
# при совпадении вопроса/модели/хэша источников мог быть отдан пользователю
# сегмента B — межсегментная утечка данных.
#
# ГДЕ ЛЕЖИТ ЛИЧНОСТЬ. core/auth.py::get_current_user() кладёт словарь
# пользователя в st.session_state["_auth_user"]:
#     {"user_id", "org_id", "role", "name", "login", "status", ...}
# Это ЕДИНСТВЕННЫЙ источник истины. Ключ с подчёркиванием — не описка.
# Если auth.py когда-нибудь переименует ключ, менять здесь _AUTH_USER_KEY.
#
# ГРАНУЛЯРНОСТЬ КЭША — неймспейс, а не голый org_id. Причина: у суперадмина
# org_id = None, у пользователя без сегмента org_id = "". Оба falsy, и при
# наивном подходе они схлопнулись бы в один общий котёл с кем угодно.
# Правила деградации — всегда В СТОРОНУ ИЗОЛЯЦИИ:
#     роль superadmin        -> "org::__superadmin__"
#     есть org_id            -> "org::{org_id}"        (общий на сегмент)
#     есть user_id без org   -> "user::{user_id}"      (личный, не общий)
#     ничего нет / не в Streamlit -> "anon::public"
#
# Внутри сегмента кэш общий — это осознанно: база знаний у них одна, ответы
# идентичны, общий кэш даёт скорость без потери изоляции.
# =============================================================================
_AUTH_USER_KEY  = "_auth_user"      # ключ session_state из core/auth.py
ANON_NAMESPACE  = "anon::public"
SUPERADMIN_ORG  = "__superadmin__"
ANON_USER_ID    = "anonymous"


def _session_state():
    """st.session_state или None, если код выполняется вне Streamlit-процесса."""
    try:
        import streamlit as st
        return st.session_state
    except Exception:
        return None


def get_auth_user() -> Dict:
    """
    Словарь текущего пользователя из session_state или {}.

    Намеренно НЕ вызывает core.auth.get_current_user(): тот читает файлы
    сессий с диска и может вызвать logout. Здесь нужен только быстрый
    read-only-снимок уже провалидированной личности. Если пользователь не
    авторизован, вернётся {} — и вызывающий код уйдёт в анонимный неймспейс.
    """
    ss = _session_state()
    if ss is None:
        return {}
    try:
        user = ss.get(_AUTH_USER_KEY)
    except Exception:
        return {}
    return user if isinstance(user, dict) else {}


def get_current_org_id(default: str = "") -> str:
    """
    СЫРОЙ org_id текущего пользователя ("" если нет сегмента).

    Именно это значение уходит в core.local_kb.search_local_kb() как имя
    коллекции local_kb_{org_id} — поэтому подменять его на неймспейс или
    на "public" нельзя, иначе поиск уйдёт в несуществующую коллекцию.
    Для ключа кэша используется get_cache_namespace(), а не эта функция.
    """
    org_id = get_auth_user().get("org_id")
    return str(org_id) if org_id else default


def get_current_user_id(default: str = ANON_USER_ID) -> str:
    """Идентификатор текущего пользователя ("superadmin" для суперадмина)."""
    user_id = get_auth_user().get("user_id")
    return str(user_id) if user_id else default


def get_current_role(default: str = "") -> str:
    """Роль текущего пользователя: superadmin | segment_admin | superuser | user."""
    role = get_auth_user().get("role")
    return str(role) if role else default


def _ns_from_parts(role: str, org_id: str, user_id: str) -> str:
    """Собирает неймспейс кэша по правилам деградации (см. комментарий выше)."""
    if role == "superadmin":
        return f"org::{SUPERADMIN_ORG}"
    if org_id:
        return f"org::{org_id}"
    if user_id and user_id != ANON_USER_ID:
        return f"user::{user_id}"
    return ANON_NAMESPACE


def get_cache_namespace() -> str:
    """Неймспейс кэша LLM для текущего пользователя."""
    user = get_auth_user()
    return _ns_from_parts(
        role=str(user.get("role") or ""),
        org_id=str(user.get("org_id") or ""),
        user_id=str(user.get("user_id") or ""),
    )


def namespace_for_org(org_id: Optional[str]) -> str:
    """
    Неймспейс по явно переданному org_id — для вызовов из фоновых потоков
    и скриптов, где session_state недоступен.
    """
    if not org_id:
        return ANON_NAMESPACE
    return f"org::{org_id}"


def get_identity() -> Dict[str, str]:
    """Личность одним вызовом — для session_scope, истории и логов."""
    user = get_auth_user()
    return {
        "user_id":   str(user.get("user_id") or ANON_USER_ID),
        "org_id":    str(user.get("org_id") or ""),
        "role":      str(user.get("role") or ""),
        "namespace": get_cache_namespace(),
    }


# =============================================================================
# Нативный Ollama /api/chat — надёжное отключение thinking-режима
#
# ПРОБЛЕМА: параметр "think": false, переданный через extra_body в
# OpenAI-совместимый эндпоинт (/v1/chat/completions), на некоторых версиях
# Ollama НЕ транслируется в нативный формат запроса — модель qwen3.5 всё
# равно уходит в режим рассуждения (см. github.com/ollama/ollama/issues/14809).
# Единственный надёжный способ — обращаться к нативному /api/chat напрямую,
# где think:false — задокументированный top-level параметр.
#
# Используется ТОЛЬКО когда backend — Ollama (llm_backend="ollama" в конфиге,
# по умолчанию true). Если когда-нибудь вернётесь на LM Studio — переключите
# "llm_backend": "lm_studio" в config/advisor_config.json, тогда код пойдёт
# через прежний OpenAI-клиент с enable_thinking-параметрами LM Studio.
# =============================================================================
def _ollama_native_base_url() -> str:
    """Базовый URL Ollama БЕЗ суффикса /v1 — для нативного /api/chat."""
    url = CONFIG.get("lm_studio_url", "http://127.0.0.1:1234/v1").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def _is_ollama_backend() -> bool:
    return CONFIG.get("llm_backend", "ollama") == "ollama"


def _ollama_native_options(temperature: float, max_tokens: int) -> dict:
    """
    Собирает словарь "options" для нативного Ollama API из конфига.

    ВАЖНО про num_ctx: у Ollama дефолт всего 2048 токенов контекста, если
    явно не задать num_ctx — это НАМНОГО меньше лимита в 20 000 токенов,
    под который спроектирован весь RAG-пайплайн (top_k источников + соседи).
    Без явного num_ctx длинные промпты будут незаметно обрезаться Ollama.
    """
    return {
        "temperature":    temperature,
        "num_predict":    max_tokens,
        "num_ctx":        CONFIG.get("num_ctx", 20000),
        "top_p":          CONFIG.get("top_p", 0.9),
        "top_k":          CONFIG.get("top_k", 40),
        "repeat_penalty": CONFIG.get("repeat_penalty", 1.1),
    }


# =============================================================================
# Сторожевой дедлайн для стрима Ollama
#
# ПОЧЕМУ ЭТОГО НЕДОСТАТОЧНО БЕЗ ЭТОГО БЛОКА. requests timeout=N при
# stream=True — это НЕ общий лимит на всю операцию. Это (connect_timeout,
# read_timeout), где read_timeout — время ожидания МЕЖДУ последовательными
# чтениями сокета. Если TCP-соединение остаётся технически открытым (сервер
# не рвёт сокет, ОС не шлёт RST/FIN), а llama-server внутри Ollama завис
# (deadlock на GPU, застрял в очереди, упал в busy-loop) — requests может
# зависнуть на iter_lines() надолго дольше заявленного timeout, потому что
# read-вызов формально не блокируется бесконечно на одном чтении, а просто
# следующая итерация начинается заново. Наблюдалось на проде: [LLM stream]
# залогирован, дальше тишина до docker restart (инцидент 2026-08-29 21:17 —
# зависание сразу после hybrid_search, на самом вызове к Ollama).
#
# РЕШЕНИЕ. Два независимых жёстких лимита поверх requests timeout:
#   1) hard_deadline   — абсолютное время на весь стрим от первого байта
#                        до последнего токена.
#   2) chunk_timeout   — максимальная пауза МЕЖДУ двумя последовательными
#                        чанками. Если Ollama замолчала посреди генерации —
#                        не ждём hard_deadline целиком, обрываем раньше.
# Оба кидают TimeoutError с понятным сообщением, которое выше по стеку
# (stream_ai_answer / generate_ai_answer) превращается в текст для
# пользователя, а не в бесконечную загрузку Streamlit.
# =============================================================================
def _ollama_chat_native_stream(model, messages, temperature, max_tokens, timeout,
                                chunk_timeout: float = 60.0):
    """
    Генератор токенов через нативный Ollama /api/chat с think:false.

    timeout        — таймаут requests (connect + per-read), передаётся как есть.
    chunk_timeout  — сторожевой лимит: если между двумя чанками от Ollama
                     проходит больше этого времени, генератор кидает
                     TimeoutError и не виснет бесконечно. По умолчанию 60 сек —
                     для нормальной генерации токен приходит намного чаще;
                     если пауза дольше — что-то у Ollama застряло.
    """
    import requests
    url = _ollama_native_base_url() + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": False,
        "options": _ollama_native_options(temperature, max_tokens),
    }

    t_start = time.monotonic()
    hard_deadline = t_start + max(timeout, 60)  # не короче явного timeout

    resp = requests.post(url, json=payload, stream=True, timeout=timeout)
    resp.raise_for_status()

    line_iter = resp.iter_lines()
    last_chunk_at = time.monotonic()

    try:
        while True:
            now = time.monotonic()
            if now > hard_deadline:
                # Текст намеренно содержит "timeout" — выше по стеку
                # (stream_ai_answer/generate_ai_answer/stream_clarification_answer)
                # обработчик ловит по подстроке "timeout" in err.lower()
                # и превращает это в понятное сообщение пользователю.
                raise TimeoutError(
                    f"Ollama stream timeout: превышен общий лимит {timeout} сек "
                    f"(модель: {model})"
                )
            if now - last_chunk_at > chunk_timeout:
                raise TimeoutError(
                    f"Ollama stream timeout: нет данных от сервера {chunk_timeout:.0f} сек "
                    f"подряд (модель: {model}, похоже сервер завис)"
                )

            try:
                line = next(line_iter)
            except StopIteration:
                break
            except requests.exceptions.ChunkedEncodingError as e:
                raise TimeoutError(f"Ollama stream timeout: соединение оборвано ({e})")

            last_chunk_at = time.monotonic()

            if not line:
                continue
            data = json.loads(line)
            msg = data.get("message") or {}
            content = msg.get("content", "")
            if content:
                yield content
            if data.get("done"):
                break
    finally:
        resp.close()


def _ollama_chat_native(model, messages, temperature, max_tokens, timeout) -> dict:
    """
    Нестриминговый вызов нативного Ollama /api/chat с think:false.
    Возвращает {"content": str, "done_reason": str}.

    Здесь достаточно обычного requests timeout: при stream=False requests
    ждёт единственный полный ответ одним блоком, поэтому read_timeout
    действительно покрывает всё время ожидания — доп. дедлайн не нужен,
    в отличие от стримингового варианта выше.
    """
    import requests
    url = _ollama_native_base_url() + "/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": False,
        "options": _ollama_native_options(temperature, max_tokens),
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("message") or {}
    return {
        "content": msg.get("content", ""),
        "done_reason": data.get("done_reason", "stop"),
    }


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
 
 
# Поток предзагрузки запускается В КОНЦЕ ФАЙЛА, а не здесь.
# Причина: _preload_models_background → get_reranker() → _get_reranker_models_list()
# → _load_search_settings(), а эта функция и AVAILABLE_RERANKER_MODELS определены
# НИЖЕ по файлу. Запуск потока отсюда — гонка с исполнением самого модуля:
# поток обычно успевает раньше и падает с NameError, предзагрузка реранкера
# тихо не выполняется, и первые 12-30 сек загрузки оплачивает первый же
# пользовательский запрос. Проявлялось как «первый вопрос всегда долгий».
 
 
# =============================================================================
# Кэш LLM — изолирован по неймспейсу (сегмент / пользователь)
#
# Структура файла data/cache/llm_cache.json не изменилась: это по-прежнему
# плоский словарь {cache_key: entry}. Изоляция достигается тем, что неймспейс
# входит в ХЭШ ключа (см. get_cache_key) — записи разных сегментов физически
# не могут совпасть. В entry дополнительно пишется поле "ns" — оно нужно
# только для селективной очистки и статистики, на поиск по кэшу не влияет.
#
# СОВМЕСТИМОСТЬ: старые записи (созданные до этой правки) не содержат
# неймспейс в ключе и никогда не будут найдены — они мертвы и вычищаются
# purge_expired_cache(). Ключи ломать безопасно: кэш не источник истины,
# максимальная потеря — один повторный вызов LLM.
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
 
 
def _cache_ttl_seconds() -> int:
    """TTL кэша в секундах из конфига (cache_ttl_days, по умолчанию 7 дней)."""
    try:
        return int(float(load_config().get("cache_ttl_days", 7)) * 86400)
    except Exception:
        return 7 * 86400


def get_cache_key(query: str, sources: list, model: str,
                  namespace: Optional[str] = None) -> str:
    """
    Ключ кэша LLM-ответа.

    namespace ОБЯЗАТЕЛЬНО входит в хэш: без него ответ, построенный на
    локальной базе одного сегмента, мог бы вернуться пользователю другого.
    Если не передан — определяется автоматически (get_cache_namespace).
    """
    if namespace is None:
        namespace = get_cache_namespace()
    s = json.dumps(
        sorted([x.get('file', '') + x.get('snippet', '')[:100] for x in sources]),
        sort_keys=True,
    )
    return hashlib.md5(f"{namespace}|||{query}|||{s}|||{model}".encode()).hexdigest()


def _cache_get(cache_key: str) -> Optional[str]:
    """
    Читает валидный ответ из кэша или None.
    Сломанные записи (петли, обрезки) удаляются на месте.
    """
    ttl = _cache_ttl_seconds()
    with _cache_lock:
        cached = _llm_cache.get(cache_key)
        if not cached:
            return None
        answer = cached.get("answer", "")
        fresh  = datetime.now().timestamp() - cached.get("timestamp", 0) < ttl
        if fresh and _is_valid_answer(answer):
            return answer
        if not _is_valid_answer(answer):
            print("[CACHE] Сломанный ответ в кэше — удаляем, генерируем заново")
            _llm_cache.pop(cache_key, None)
        return None


def _cache_put(cache_key: str, answer: str, query: str, model: str, namespace: str):
    """
    Кладёт ответ в кэш и сохраняет на диск.

    Поля ns/user_id нужны только для селективной очистки и статистики —
    на поиск по кэшу они не влияют (изоляция уже вшита в хэш ключа).
    Текст запроса храним для админ-статистики; ответ и так лежит рядом.
    """
    with _cache_lock:
        _llm_cache[cache_key] = {
            "answer":    answer,
            "timestamp": datetime.now().timestamp(),
            "query":     query,
            "model":     model,
            "ns":        namespace,
            "user_id":   get_current_user_id(),
        }
        save_llm_cache()


def clear_llm_cache(namespace: Optional[str] = None) -> int:
    """
    Очищает кэш LLM.

    namespace=None  → ВЕСЬ кэш всех сегментов. Только для суперадмина.
    namespace="..." → только записи этого неймспейса.

    Возвращает число удалённых записей.
    """
    with _cache_lock:
        if namespace is None:
            removed = len(_llm_cache)
            _llm_cache.clear()
        else:
            victims = [k for k, v in _llm_cache.items()
                       if (v or {}).get("ns") == namespace]
            for k in victims:
                _llm_cache.pop(k, None)
            removed = len(victims)
        save_llm_cache()
    print(f"[CACHE] Удалено записей: {removed} (неймспейс: {namespace or 'ВСЕ'})")
    return removed


def clear_llm_cache_for_current_user() -> tuple:
    """
    Очистка кэша с учётом роли — для кнопки в UI Советчика.

    Раньше кнопка «Очистить кэш LLM» вызывала _llm_cache.clear() и была
    доступна каждому: рядовой пользователь одним нажатием вайпил кэш всех
    сегментов сразу. Теперь:
        superadmin -> весь кэш
        остальные  -> только свой неймспейс

    Возвращает (число_удалённых, человекочитаемая_область).
    """
    if get_current_role() == "superadmin":
        return clear_llm_cache(None), "все сегменты"
    ns = get_cache_namespace()
    return clear_llm_cache(ns), "ваш сегмент"


def purge_expired_cache() -> int:
    """
    Удаляет протухшие, сломанные и legacy-записи (созданные до введения
    изоляции — у них нет поля "ns", найти их по ключу всё равно невозможно).
    Безопасно вызывать при старте приложения.
    """
    ttl = _cache_ttl_seconds()
    now = datetime.now().timestamp()
    with _cache_lock:
        victims = [
            k for k, v in _llm_cache.items()
            if not isinstance(v, dict)
            or "ns" not in v                           # запись до введения изоляции
            or now - v.get("timestamp", 0) >= ttl      # протухла
            or not _is_valid_answer(v.get("answer", ""))
        ]
        for k in victims:
            _llm_cache.pop(k, None)
        if victims:
            save_llm_cache()
    if victims:
        print(f"[CACHE] Очищено устаревших/legacy-записей: {len(victims)}")
    return len(victims)


def get_cache_stats(namespace: Optional[str] = None) -> Dict:
    """
    Статистика кэша для админ-панели.
    namespace=None → всего + разбивка по неймспейсам; иначе — только по нему.
    """
    with _cache_lock:
        entries = list(_llm_cache.values())
    by_ns: Dict[str, int] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = e.get("ns", "legacy")
        by_ns[key] = by_ns.get(key, 0) + 1
    if namespace is not None:
        return {"total": by_ns.get(namespace, 0), "namespace": namespace}
    return {"total": len(entries), "by_ns": by_ns}
 
 
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


def _doc_type_match(chunk_doc_type: str, selected_doc_types: list) -> bool:
    """
    Проверяет, подходит ли чанк под фильтр видов документов.
    Чанки без поля doc_type (старые документы / неопределённый тип)
    всегда проходят фильтр — обратная совместимость.
    Значения doc_type: 'npa', 'fas', 'court', 'methodics', 'unknown'.
    """
    if not chunk_doc_type or chunk_doc_type == "unknown":
        return True
    return chunk_doc_type in selected_doc_types


# =============================================================================
# Фильтр по статусу действия документа
#
# Статусы (поле "doc_status" в метаданных чанка ChromaDB):
#   "active"  — действующая редакция
#   "pending" — утверждён, не вступил в силу
#   "expired" — утратил силу
#   ""        — даты не проставлены (всегда проходит — обратная совместимость)
# =============================================================================
def _status_match(chunk_doc_status: str, doc_status_filter: str) -> bool:
    """
    Проверяет, подходит ли чанк под фильтр статуса документа.
    Чанки без doc_status (даты не проставлены) всегда проходят.
    doc_status_filter: "active" | "pending" | "expired" | None (все).
    """
    if not chunk_doc_status:       # даты не проставлены — пропускаем всегда
        return True
    if doc_status_filter is None:  # фильтр отключён — пропускаем всегда
        return True
    return chunk_doc_status == doc_status_filter


# =============================================================================
# Локальная база знаний сегмента — константа-ключ и вспомогательная функция
#
# "local" — специальное псевдо-значение внутри параметра doc_types мультиселекта
# «Вид документа» в Советчике. В отличие от npa/fas/court/methodics оно не
# фильтрует tariff_docs, а подключает ОТДЕЛЬНУЮ ChromaDB-коллекцию сегмента
# (local_kb_{org_id}, см. core/local_kb.py) — физически изолированную от
# остальных сегментов и от общей базы.
# =============================================================================
LOCAL_KB_DOC_TYPE = "local"


def _merge_ranked_lists(primary: list, secondary: list, top_k: int, k: int = 60) -> list:
    """
    Сливает два УЖЕ отсортированных списка источников по рангу (RRF).

    ЗАЧЕМ НЕ ПО distance. У общей базы "distance" — это pseudo_dist,
    производная от RRF-скора (1 - score*60), где score ≈ 0.02…0.04: у топовых
    чанков он схлопывается в 0.0. У локальной базы (core/local_kb.py)
    distance — сырая косинусная дистанция ChromaDB, обычно 0.2…0.6.
    Шкалы несопоставимы: при сортировке общим ключом локальные документы
    систематически проигрывали бы общей базе независимо от релевантности.

    Ранг свободен от шкалы: берём позицию внутри своего списка, где каждый
    ранжирован своим — корректным для него — механизмом. При равенстве
    скоров стабильная сортировка оставляет впереди primary (общую базу).
    """
    scored = []
    for rank, src in enumerate(primary):
        scored.append((1.0 / (k + rank + 1), src))
    for rank, src in enumerate(secondary):
        scored.append((1.0 / (k + rank + 1), src))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [src for _, src in scored[:top_k]]


def _split_doc_types(doc_types: Optional[List[str]]) -> tuple:
    """
    Разбивает список doc_types на (обычные_типы_для_tariff_docs, нужна_ли_локальная_база).

    Примеры:
      None                       -> (None,  False)  — фильтр выключен, локальная база не запрашивается
      ["npa"]                    -> (["npa"], False)
      ["local"]                  -> (None,  True)   — искать ТОЛЬКО в локальной базе сегмента
      ["npa", "local"]           -> (["npa"], True) — искать и там, и там
    """
    if not doc_types:
        return None, False
    include_local = LOCAL_KB_DOC_TYPE in doc_types
    regular = [dt for dt in doc_types if dt != LOCAL_KB_DOC_TYPE]
    return (regular if regular else None), include_local


def search_vector_db(query: str, top_k: int = 5, spheres: list = None,
                     doc_types: list = None, doc_status: str = "active",
                     filenames: list = None, org_id: str = None) -> list:
    """
    org_id — идентификатор сегмента текущего пользователя. Требуется только
    когда doc_types содержит "local" (запрос к локальной базе сегмента);
    для обычного поиска по tariff_docs не используется.

    Если org_id не передан — определяется автоматически из session_state
    (get_current_org_id). Явный аргумент имеет приоритет: он нужен для вызовов
    из фоновых потоков, где session_state недоступен.
    """
    t0 = time.perf_counter()

    # Явный аргумент имеет приоритет; иначе берём сегмент из session_state.
    # "" превращаем в None — у пользователя без сегмента локальной базы нет.
    org_id = org_id or get_current_org_id() or None

    _regular_doc_types, _include_local = _split_doc_types(doc_types)

    # ── Локальная база сегмента ─────────────────────────────────────────────
    # Если выбрано ТОЛЬКО "Локальная база" (без npa/fas/court/methodics и без
    # общего "все виды" = doc_types is None) — ищем исключительно в ней и не
    # трогаем tariff_docs вообще.
    _local_only = bool(doc_types) and _regular_doc_types is None and _include_local

    if _local_only:
        if not org_id:
            print("[LOCAL_KB] org_id не передан — локальная база недоступна")
            return []
        try:
            from core.local_kb import search_local_kb
        except Exception as e:
            print(f"[LOCAL_KB] Импорт не удался: {e}")
            return []
        local_sources = search_local_kb(query, org_id, top_k=top_k)
        print(f"[TIMING] search_vector_db (только локальная база): "
              f"{time.perf_counter()-t0:.3f} сек, {len(local_sources)} источников")
        return local_sources

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
        # При активном фильтре по сфере или виду документа запрашиваем вдвое больше
        # кандидатов, чтобы компенсировать потери от постфильтрации.
        if spheres or _regular_doc_types:
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

        # ── Фильтрация по виду документа (до реранкинга) ────────────────────
        # _regular_doc_types — только npa/fas/court/methodics, "local" уже вырезан.
        if _regular_doc_types:
            pre_count  = len(candidates)
            candidates = [
                c for c in candidates
                if _doc_type_match(c.get("meta", {}).get("doc_type", ""), _regular_doc_types)
            ]
            print(f"[DOCTYPE FILTER] {pre_count} → {len(candidates)} кандидатов "
                  f"по видам: {_regular_doc_types}")

        # ── Фильтрация по статусу документа (до реранкинга) ─────────────────
        # doc_status="active" по умолчанию — утратившие силу исключаются.
        # Чанки без doc_status (даты не проставлены) всегда проходят.
        if doc_status is not None:
            pre_count  = len(candidates)
            candidates = [
                c for c in candidates
                if _status_match(c.get("meta", {}).get("doc_status", ""), doc_status)
            ]
            if pre_count != len(candidates):
                print(f"[STATUS FILTER] {pre_count} → {len(candidates)} кандидатов "
                      f"статус={doc_status}")

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
                "doc_status":   meta.get("doc_status", ""),
                "valid_from":   meta.get("valid_from", ""),
                "valid_to":     meta.get("valid_to", ""),
                "article":      meta.get("article", ""),
                "chunk_index":  meta.get("chunk_index", ""),
                "distance":     pseudo_dist,
                "sphere":       meta.get("sphere", ""),
                "source_kind":  "global",
            })

        # ── Шаг 6: если локальная база выбрана ДОПОЛНИТЕЛЬНО к обычным видам —
        # подмешиваем её результаты по РАНГУ (см. _merge_ranked_lists), а не по
        # distance: шкалы pseudo_dist и косинусной дистанции несопоставимы.
        if _include_local and org_id:
            try:
                from core.local_kb import search_local_kb
                local_sources = search_local_kb(query, org_id, top_k=top_k)
                if local_sources:
                    sources = _merge_ranked_lists(sources, local_sources, top_k)
                    _n_local = sum(1 for s in sources if s.get("source_kind") == "local")
                    print(f"[LOCAL_KB] Найдено {len(local_sources)} локальных, "
                          f"в итоговый топ-{top_k} попало {_n_local} "
                          f"(сегмент {org_id})")
            except Exception as e:
                print(f"[LOCAL_KB] Ошибка подмешивания: {e}")

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
    if _regular_doc_types:
        _fallback_sources = [
            s for s in _fallback_sources
            if _doc_type_match(s.get("doc_type", ""), _regular_doc_types)
        ]
    if doc_status is not None:
        _fallback_sources = [
            s for s in _fallback_sources
            if _status_match(s.get("doc_status", ""), doc_status)
        ]
    if _include_local and org_id:
        try:
            from core.local_kb import search_local_kb
            local_sources = search_local_kb(query, org_id, top_k=top_k)
            if local_sources:
                # Здесь обе шкалы — сырая косинусная дистанция ChromaDB,
                # поэтому сортировка по distance корректна.
                _fallback_sources = sorted(
                    _fallback_sources + local_sources,
                    key=lambda s: s.get("distance", 1.0),
                )[:top_k]
        except Exception as e:
            print(f"[LOCAL_KB] Ошибка подмешивания (fallback): {e}")
    return _fallback_sources
 
 
def debug_search_candidates(query: str, top_k: int = 5,
                            spheres: Optional[List[str]] = None,
                            doc_types: Optional[List[str]] = None,
                            doc_status: Optional[str] = "active",
                            filenames: Optional[List[str]] = None) -> dict:
    """
    Отладочная функция для UI «Поиск и реранкинг».
    Возвращает кандидатов ДО и ПОСЛЕ реранкинга, а также варианты запроса.
    Не подтягивает соседей — нужен только чистый текст чанка для просмотра.
    spheres: список сфер для фильтрации (None = все сферы).
    doc_types: список видов документов для фильтрации (None = все виды).
               Значение "local" (локальная база сегмента) в этой отладочной
               функции игнорируется — она предназначена только для tariff_docs.
    doc_status: "active" | "pending" | "expired" | None (все).
    """
    result = {
        "query_variants": [query],
        "pre_rerank":     [],
        "post_rerank":    [],
        "reranker_used":  False,
        "elapsed":        0.0,
        "error":          None,
    }

    _regular_doc_types, _ = _split_doc_types(doc_types)

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
        if spheres or _regular_doc_types or doc_status:
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

        # Фильтрация по виду документа до реранкинга
        if _regular_doc_types:
            pre_count  = len(pre_rerank)
            pre_rerank = [
                c for c in pre_rerank
                if _doc_type_match(c.get("meta", {}).get("doc_type", ""), _regular_doc_types)
            ]
            print(f"[DOCTYPE FILTER/debug] {pre_count} → {len(pre_rerank)} по видам: {_regular_doc_types}")

        # Фильтрация по статусу до реранкинга
        if doc_status is not None:
            pre_count  = len(pre_rerank)
            pre_rerank = [
                c for c in pre_rerank
                if _status_match(c.get("meta", {}).get("doc_status", ""), doc_status)
            ]
            if pre_count != len(pre_rerank):
                print(f"[STATUS FILTER/debug] {pre_count} → {len(pre_rerank)} статус={doc_status}")

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
    """
    Собирает контекст из источников.

    max_chars по умолчанию берётся из настроек поиска (context_max_chars, 8000).
    При radius=2 и чанке 1750 симв. один snippet ≈ 8750 симв., поэтому бюджет
    позволяет 1 источник полностью или несколько с разумной обрезкой.
    """
    if max_chars is None:
        max_chars = int(_load_search_settings().get("context_max_chars", 8000))
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
            "source_kind": "global",
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
    user_context: str = "",
    answer_length: str = "short",
    org_id: str = None,
):
    """
    Генератор токенов для Streamlit st.write_stream().
    Если ответ есть в кэше — возвращает его сразу одним куском.
    Иначе стримит токены по мере генерации LLM.
    Автоматически сохраняет ответ в кэш после завершения.
    answer_length: "short" — кратко по существу, "detailed" — развёрнуто с пояснениями.
    org_id: сегмент пользователя. Если None — неймспейс кэша определяется
            автоматически из session_state (см. get_cache_namespace).
    """
    config      = load_config()
    model       = model or config.get("default_model", "qwen/qwen3.5-9b")
    temperature = temperature if temperature is not None else config.get("temperature", 0.3)
    max_tokens  = config.get("max_tokens", 2048)
    timeout     = config.get("timeout_seconds", 300)
    namespace   = namespace_for_org(org_id) if org_id else get_cache_namespace()
 
    if _SOURCES_ONLY_MODE:
        yield "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."
        return
 
    # Кэш — возвращаем сразу без стриминга (неймспейс вшит в хэш ключа →
    # чужой сегмент физически не может попасть в выдачу)
    cache_key = get_cache_key(query, sources, model, namespace=namespace)
    cached_answer = _cache_get(cache_key)
    if cached_answer is not None:
        print(f"[CACHE HIT stream] {model} | ns={namespace}")
        yield cached_answer
        return
 
    # Строим промпт (та же логика что в generate_ai_answer)
    try:
        prompts = load_prompts()
        context = _build_context(sources)
 
        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])
        if user_context and user_context.strip():
            system_prompt = (
                system_prompt
                + "\n\n---\nКонтекст пользователя:\n"
                + user_context.strip()
            )
        _LENGTH_INSTRUCTIONS = {
            "short":    "8. Отвечай КРАТКО: максимум 3–5 предложений или маркированный список до 5 пунктов. "
                        "Без вводных слов и пересказа вопроса.",
            "detailed": "8. Отвечай РАЗВЁРНУТО: подробно раскрой тему, приведи все релевантные нормы, "
                        "условия применения и исключения. Используй подзаголовки если тем несколько.",
        }
        _len_instr = _LENGTH_INSTRUCTIONS.get(answer_length, _LENGTH_INSTRUCTIONS["short"])
        system_prompt = system_prompt + "\n" + _len_instr
        system_prompt = (
            system_prompt
            + "\n\nТекущая дата и время: "
            + datetime.now().strftime("%d.%m.%Y, %H:%M")
            + "."
        )
        user_content  = prompts.get("advisor_user",   DEFAULT_PROMPTS["advisor_user"]).format(
            query=query, context=context,
        )
 
        # ---------------------------------------------------------------
        # Отключение thinking-режима для моделей семейства Qwen3 / Qwen3.5
        #
        # ВАЖНО: extra_body={"think": False} через OpenAI-совместимый
        # /v1/chat/completions ненадёжен — на части версий Ollama это поле
        # не транслируется в нативный запрос (ollama/ollama issue #14809),
        # и модель всё равно уходит в незакрытый <think>-блок, съедая весь
        # max_tokens на рассуждения — пользователь получает пустой ответ.
        # Поэтому для Ollama-бэкенда используем нативный /api/chat напрямую
        # (_ollama_chat_native_stream), где think:false — надёжный top-level
        # параметр. OpenAI-клиент остаётся как fallback для LM Studio.
        # ---------------------------------------------------------------
        is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        use_native = is_qwen3 and _is_ollama_backend()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]

        print(f"[LLM stream] {model} | ns={namespace} | max_tokens={max_tokens} | "
              f"native={use_native} | "
              f"промпт ~{len(system_prompt)+len(user_content)} симв. / "
              f"~{(len(system_prompt)+len(user_content))//4} токенов (оценка)")
        t0 = time.perf_counter()
 
        full_text  = ""
        buf        = ""
        in_think   = False
        # Детектор петли: если один токен повторяется >20 раз подряд — обрываем
        last_token = ""
        repeat_cnt = 0
 
        if use_native:
            response = _ollama_chat_native_stream(
                model, messages, temperature, max_tokens, timeout,
            )
            response_iter = ({"delta": chunk} for chunk in response)
        else:
            extra_body = {}
            if is_qwen3:
                messages[-1]["content"] = "/no_think\n\n" + user_content
                extra_body = {
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            kwargs = dict(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                frequency_penalty=0.1,   # штраф за повторения — предотвращает "CL CL CL..."
                stream=True,
            )
            if extra_body:
                kwargs["extra_body"] = extra_body
            raw_response = client.chat.completions.create(**kwargs)
            response_iter = ({"delta": c.choices[0].delta.content} for c in raw_response)
 
        for chunk in response_iter:
            delta = chunk["delta"]
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
            _cache_put(cache_key, answer, query, model, namespace)
 
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
# Уточнения не кэшируются — они всегда зависят от предыдущего ответа,
# поэтому вопрос изоляции кэша по сегментам здесь не возникает вовсе.
# =============================================================================
def stream_clarification_answer(
    clarify_q: str,
    prev_answer: str,
    new_sources: list,
    model: str = None,
    temperature: float = None,
    user_context: str = "",
    answer_length: str = "short",
):
    """
    Генератор токенов для уточняющих вопросов.

    Args:
        clarify_q:     текст уточняющего вопроса
        prev_answer:   предыдущий ответ LLM (исходный или последнее уточнение)
        new_sources:   чанки из RAG, найденные по clarify_q
        model:         модель LM Studio
        temperature:   температура генерации
        user_context:  контекст пользователя (роль, организация)
        answer_length: "short" | "detailed"
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
        if user_context and user_context.strip():
            system_prompt = (
                system_prompt
                + "\n\n---\nКонтекст пользователя:\n"
                + user_context.strip()
            )
        _LENGTH_INSTRUCTIONS = {
            "short":    "8. Отвечай КРАТКО: максимум 3–5 предложений или маркированный список до 5 пунктов. "
                        "Без вводных слов и пересказа вопроса.",
            "detailed": "8. Отвечай РАЗВЁРНУТО: подробно раскрой тему, приведи все релевантные нормы, "
                        "условия применения и исключения. Используй подзаголовки если тем несколько.",
        }
        system_prompt = system_prompt + "\n" + _LENGTH_INSTRUCTIONS.get(
            answer_length, _LENGTH_INSTRUCTIONS["short"]
        )
        system_prompt = (
            system_prompt
            + "\n\nТекущая дата и время: "
            + datetime.now().strftime("%d.%m.%Y, %H:%M")
            + "."
        )

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

        # См. комментарий в stream_ai_answer — надёжный think:false только
        # через нативный Ollama /api/chat, extra_body на /v1 ненадёжен.
        is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        use_native = is_qwen3 and _is_ollama_backend()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]

        print(f"[CLARIFY stream] {model} | native={use_native} | "
              f"промпт ~{len(system_prompt)+len(user_content)} симв. | "
              f"prev_answer={len(prev_answer)} симв. | rag_chunks={len(new_sources)}")
        t0 = time.perf_counter()

        full_text  = ""
        buf        = ""
        in_think   = False
        last_token = ""
        repeat_cnt = 0

        if use_native:
            response = _ollama_chat_native_stream(
                model, messages, temperature, max_tokens, timeout,
            )
            response_iter = ({"delta": chunk} for chunk in response)
        else:
            extra_body = {}
            if is_qwen3:
                messages[-1]["content"] = "/no_think\n\n" + user_content
                extra_body = {
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            kwargs = dict(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                frequency_penalty=0.1,
                stream=True,
            )
            if extra_body:
                kwargs["extra_body"] = extra_body
            raw_response = client.chat.completions.create(**kwargs)
            response_iter = ({"delta": c.choices[0].delta.content} for c in raw_response)

        for chunk in response_iter:
            delta = chunk["delta"]
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
    org_id: str = None,
) -> str:
    config      = load_config()
    model       = model or config.get("default_model", "qwen/qwen3.5-9b")
    temperature = temperature if temperature is not None else config.get("temperature", 0.3)
    max_tokens  = config.get("max_tokens", 2048)
    timeout     = config.get("timeout_seconds", 300)
    namespace   = namespace_for_org(org_id) if org_id else get_cache_namespace()
 
    if _SOURCES_ONLY_MODE:
        return "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."
 
    cache_key = get_cache_key(query, sources, model, namespace=namespace)
    cached_answer = _cache_get(cache_key)
    if cached_answer is not None:
        print(f"[CACHE HIT] {model} | ns={namespace}")
        return cached_answer
 
    try:
        prompts = load_prompts()
        context = _build_context(sources)
 
        system_prompt = prompts.get("advisor_system", DEFAULT_PROMPTS["advisor_system"])
        user_content  = prompts.get("advisor_user",   DEFAULT_PROMPTS["advisor_user"]).format(
            query=query, context=context,
        )
 
        # См. комментарий в stream_ai_answer — надёжный think:false только
        # через нативный Ollama /api/chat, extra_body на /v1 ненадёжен.
        is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        use_native = is_qwen3 and _is_ollama_backend()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]
 
        t0 = time.perf_counter()
        print(f"[LLM] {model} | ns={namespace} | max_tokens={max_tokens} | "
              f"native={use_native} | "
              f"промпт ~{len(system_prompt)+len(user_content)} симв.")
 
        if use_native:
            native_result = _ollama_chat_native(
                model, messages, temperature, max_tokens, timeout,
            )
            raw_content   = native_result["content"]
            # Ollama: "stop" | "length" | ... → приводим к OpenAI-совместимому виду
            finish_reason = "length" if native_result["done_reason"] == "length" else "stop"
        else:
            extra_body = {}
            if is_qwen3:
                messages[-1]["content"] = "/no_think\n\n" + user_content
                extra_body = {
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
            kwargs = dict(
                model=model,
                messages=messages,
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
            _cache_put(cache_key, answer, query, model, namespace)
 
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
    spheres: list = None,
    doc_types: list = None,
    doc_status: str = "active",
    org_id: str = None,
) -> dict:
    """
    org_id — сегмент пользователя. Пробрасывается и в поиск (локальная база),
    и в ключ кэша LLM. Если None — берётся из session_state автоматически.
    """
    t_start   = time.perf_counter()
    config    = load_config()
    model     = model or config.get("default_model", "qwen/qwen3.5-9b")
    org_id    = org_id or get_current_org_id() or None
    namespace = namespace_for_org(org_id) if org_id else get_cache_namespace()
 
    if not _llm_cache:
        load_llm_cache()
 
    print(f"\n{'='*55}\n[ASK] «{query[:70]}» | {model} | ns={namespace}\n{'='*55}")
 
    result = {
        "answer": "", "sources": [], "redirect": None,
        "redirect_reason": None, "from_faq": False,
        "from_cache": False, "model": model, "org_id": org_id,
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
    sources = search_vector_db(
        query, top_k=top_k, spheres=spheres, doc_types=doc_types,
        doc_status=doc_status, org_id=org_id,
    )
    result["sources"] = sources
 
    if sources:
        cache_key  = get_cache_key(query, sources, model, namespace=namespace)
        was_cached = _cache_get(cache_key) is not None
        result["answer"]     = generate_ai_answer(query, sources, model, temperature,
                                                  org_id=org_id)
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


# =============================================================================
# Старт фоновой предзагрузки — ПОСЛЕДНЯЯ строка модуля
#
# Только здесь все зависимости потока уже определены: _load_search_settings,
# AVAILABLE_RERANKER_MODELS, get_hybrid_retriever. Раньше поток стартовал из
# середины файла и выигрывал гонку у интерпретатора → NameError → реранкер
# грузился лениво на первом запросе пользователя.
# =============================================================================
threading.Thread(
    target=_preload_models_background,
    daemon=True,
    name="model-preload",
).start()