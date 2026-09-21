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
# СЛУЖЕБНЫЙ СЛОЙ ЗНАНИЙ («невидимый слой»)
# ─────────────────────────────────────────────────────────────────────────────
# ЧТО ЭТО. Документы с пояснениями, внутренней терминологией («птичьим языком»)
# и общими сведениями, которые ДОЛЖНЫ влиять на ответ, но НИКОГДА не должны
# показываться пользователю как источник и не должны цитироваться моделью.
#
# КАК УСТРОЕНО. Физически чанки лежат в той же коллекции ChromaDB
# «tariff_docs», отличаются значением метаданного поля doc_type == "hidden"
# (проставляется при индексации, см. core/indexer.py и
# config/doc_types_override.json).
#
# ПОЧЕМУ НУЖЕН ЖЁСТКИЙ ГЕЙТ, А НЕ ОБЫЧНЫЙ ФИЛЬТР ПО doc_type.
# Функция _doc_type_match() намеренно пропускает чанки с пустым или "unknown"
# типом — это обратная совместимость со старыми документами. Кроме того,
# фильтр по видам документов применяется ТОЛЬКО когда пользователь явно выбрал
# виды. То есть при выключенном фильтре скрытые чанки прошли бы в обычную
# выдачу и попали бы в список источников. Поэтому служебный слой вырезается из
# основного потока кандидатов ОТДЕЛЬНО и БЕЗУСЛОВНО (см. _is_hidden_chunk и
# места его вызова), независимо от всех остальных фильтров.
#
# КАК ПОПАДАЕТ В ОТВЕТ. Отдельным поиском search_hidden_layer() — прямым
# запросом к ChromaDB с where={"doc_type": "hidden"}. Почему не берём скрытые
# чанки из общего пула кандидатов: скрытых чанков в базе на порядки меньше, чем
# обычных, и в топ-60 общего поиска они просто не попадают, даже когда идеально
# релевантны. Отдельный запрос гарантирует, что мы получим лучшие чанки ИМЕННО
# среди служебного слоя.
#
# Результат уходит в промпт отдельным блоком (см. _build_hidden_context) и
# НЕ попадает в result["sources"].
# =============================================================================
HIDDEN_DOC_TYPE = "hidden"

# Псевдо-значение для мультиселекта «Вид документа» в Советчике: выбран только
# он — значит включён ВНУТРЕННИЙ РЕЖИМ. Поиск по базе НПА и по локальной базе
# сегмента не выполняется вообще, модель отвечает на своих знаниях плюс
# служебный слой. Нужен специалистам для внутренних вопросов, где ссылки на
# нормативку не требуются и только мешают.
HIDDEN_ONLY_DOC_TYPE = "hidden_only"
 
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
    # ── Внутренний режим (только служебный слой, без источников) ─────────────
    # ЗАЧЕМ ОТДЕЛЬНЫЙ ПРОМПТ. Обычный advisor_system требует «опирайся
    # ИСКЛЮЧИТЕЛЬНО на предоставленный контекст, если ответа нет — сообщи об
    # этом». Во внутреннем режиме контекста из НПА нет вовсе, и с обычным
    # промптом модель будет отказываться отвечать на любой вопрос.
    "advisor_internal_system": (
        "Ты — эксперт-консультант по тарифному регулированию в Российской Федерации. "
        "Сейчас ты работаешь во внутреннем режиме: отвечаешь специалисту организации, "
        "поиск по базе нормативных документов отключён.\n\n"
        "ПРАВИЛА ОТВЕТА:\n"
        "1. Отвечай ТОЛЬКО на русском языке.\n"
        "2. Опирайся на свои профессиональные знания и на служебные пояснения, "
        "если они приведены ниже. При расхождении служебные пояснения имеют приоритет.\n"
        "3. НЕ приводи ссылки на конкретные пункты и номера нормативных актов, "
        "если не уверен в них полностью: база НПА сейчас не подключена, "
        "и выдуманная ссылка хуже её отсутствия. Лучше опиши суть требования словами.\n"
        "4. Если вопрос требует точной нормы — прямо скажи, что её нужно проверить "
        "в обычном режиме Советчика с подключённой базой НПА.\n"
        "5. Структурируй ответ: списки для перечислений, таблица Markdown для числовых данных.\n"
        "6. Отвечай по существу, без вводных слов и пересказа вопроса."
    ),
    "advisor_internal_user": (
        "Вопрос специалиста: {query}\n\n"
        "Дай практический ответ по существу."
    ),
    "advisor_system_description": "Системный промпт советчика.",
    "advisor_user_description":   "Шаблон запроса. Переменные: {query}, {context}.",
    "advisor_internal_system_description": (
        "Системный промпт внутреннего режима (только служебный слой, без источников)."
    ),
    "advisor_internal_user_description": (
        "Шаблон запроса внутреннего режима. Переменная: {query}."
    ),
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
# зависнуть надолго дольше заявленного timeout. Это ПОДТВЕРЖДЕНО на проде:
# первая версия этого фикса (с кооперативной проверкой дедлайна ВНУТРИ
# цикла iter_lines()) не сработала — зависание длилось больше 300 сек
# при timeout=300, потому что блокировка происходила уже на самом вызове
# requests.post(..., stream=True, timeout=timeout) — то есть ДО входа в
# цикл, где стояли проверки. Кооперативные проверки бесполезны, если
# поток не доходит до них.
#
# РЕШЕНИЕ. Независимый поток-таймер (не кооперативная проверка, а
# принудительное вмешательство извне): _StreamWatchdog запускается ДО
# requests.post и параллельно с чтением стрима. Если за hard_deadline
# секунд стрим не завершился — таймер сам дёргает resp.close() у ответа
# requests, что обрывает сокет на уровне urllib3/http.client и заставляет
# блокирующий read/iter_lines() выбросить исключение НЕЗАВИСИМО от того,
# сработал ли внутренний timeout requests. Это работает даже если
# requests.post() сама застряла в ожидании заголовков ответа — close()
# на объекте Response, доступном сразу после успешного connect, разрывает
# нижележащее соединение.
#
# Дополнительно chunk_timeout переопределяет дедлайн таймера при каждом
# полученном чанке — так долгая, но ЖИВАЯ генерация не обрывается
# hard_deadline'ом раньше времени, а тихое зависание ловится быстро.
# =============================================================================
class _StreamWatchdog:
    """
    Независимый поток-таймер для принудительного обрыва зависшего
    HTTP-стрима. В отличие от кооперативных проверок внутри цикла чтения,
    это единственный способ гарантированно прервать операцию, которая
    зависла ДО того, как код дошёл до цикла (например, на самом
    requests.post() в ожидании заголовков ответа).

    Использование:
        watchdog = _StreamWatchdog(hard_deadline=300, chunk_timeout=60)
        watchdog.start()
        resp = requests.post(..., stream=True, timeout=...)
        watchdog.attach(resp)          # с этого момента таймер может resp.close()
        for line in resp.iter_lines():
            watchdog.touch()           # сдвигает дедлайн chunk_timeout
            ...
        watchdog.stop()                # обязательно — иначе поток продолжит жить
    """

    def __init__(self, hard_deadline: float, chunk_timeout: float, label: str = ""):
        self._hard_deadline_s  = max(hard_deadline, 10)
        self._chunk_timeout_s  = max(chunk_timeout, 5)
        self._label            = label
        self._resp             = None
        self._resp_lock        = threading.Lock()
        self._last_activity    = time.monotonic()
        self._stop_event       = threading.Event()
        self._fired            = threading.Event()
        self._thread           = threading.Thread(
            target=self._run, daemon=True,
            name=f"stream-watchdog-{label}" if label else "stream-watchdog",
        )
        self._start_time       = time.monotonic()

    def start(self):
        self._thread.start()

    def attach(self, resp):
        """Регистрирует объект requests.Response, который таймер сможет закрыть."""
        with self._resp_lock:
            self._resp = resp

    def touch(self):
        """Сдвигает окно chunk_timeout — вызывать при получении каждого чанка."""
        self._last_activity = time.monotonic()

    def fired(self) -> bool:
        """True, если таймер уже сработал (можно отличить свой обрыв от чужого)."""
        return self._fired.is_set()

    def stop(self):
        """Останавливает поток-таймер. ОБЯЗАТЕЛЬНО вызывать в finally."""
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.wait(timeout=1.0):
            now = time.monotonic()
            elapsed_total = now - self._start_time
            elapsed_since_chunk = now - self._last_activity

            if elapsed_total > self._hard_deadline_s:
                reason = (f"общий лимит {self._hard_deadline_s:.0f} сек "
                          f"(прошло {elapsed_total:.0f} сек)")
            elif elapsed_since_chunk > self._chunk_timeout_s:
                reason = (f"нет данных {self._chunk_timeout_s:.0f} сек подряд "
                          f"(модель, похоже, зависла)")
            else:
                continue

            self._fired.set()
            print(f"[STREAM WATCHDOG{' ' + self._label if self._label else ''}] "
                  f"Принудительный обрыв: {reason}")
            with self._resp_lock:
                if self._resp is not None:
                    try:
                        self._resp.close()
                    except Exception as e:
                        print(f"[STREAM WATCHDOG] Ошибка при resp.close(): {e}")
                    # Дополнительно рвём низкоуровневое соединение urllib3 —
                    # resp.close() иногда не хватает, если поток застрял
                    # внутри http.client на уровне сокета, а не на уровне
                    # буфера requests.
                    try:
                        raw = getattr(self._resp, "raw", None)
                        if raw is not None:
                            sock = getattr(raw, "_fp", None)
                            sock = getattr(sock, "fp", None) or sock
                            if sock is not None and hasattr(sock, "close"):
                                sock.close()
                    except Exception:
                        pass
            return  # таймер сработал один раз — дальше делать нечего


def _ollama_chat_native_stream(model, messages, temperature, max_tokens, timeout,
                                chunk_timeout: float = 60.0):
    """
    Генератор токенов через нативный Ollama /api/chat с think:false.

    timeout        — общий жёсткий лимит на весь стрим (секунд). Обеспечивается
                     НЕ через requests timeout= (ненадёжно при зависшем сервере,
                     см. комментарий выше _StreamWatchdog), а через независимый
                     поток-таймер, который принудительно рвёт соединение.
    chunk_timeout  — сторожевой лимит паузы между чанками. По умолчанию 60 сек.
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

    watchdog = _StreamWatchdog(hard_deadline=timeout, chunk_timeout=chunk_timeout,
                                label=model)
    watchdog.start()

    resp = None
    try:
        # requests timeout= оставлен как первая (но не единственная) линия
        # защиты — сработает в типичных случаях сама. watchdog — вторая,
        # надёжная линия защиты на случай, когда requests timeout не
        # срабатывает (подтверждённый на проде сценарий).
        resp = requests.post(url, json=payload, stream=True, timeout=timeout)
        watchdog.attach(resp)
        resp.raise_for_status()

        for line in resp.iter_lines():
            watchdog.touch()

            if watchdog.fired():
                raise TimeoutError(
                    f"Ollama stream timeout: соединение принудительно оборвано "
                    f"сторожевым таймером (модель: {model})"
                )

            if not line:
                continue
            data = json.loads(line)
            msg = data.get("message") or {}
            content = msg.get("content", "")
            if content:
                yield content
            if data.get("done"):
                break

    except (requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout) as e:
        # Если это watchdog оборвал соединение — даём понятное сообщение.
        # Если оборвалось само по другой причине — тоже сообщаем как timeout,
        # чтобы обработчик выше по стеку (ловит по "timeout" in err.lower())
        # показал пользователю осмысленный текст, а не голый traceback.
        if watchdog.fired():
            raise TimeoutError(
                f"Ollama stream timeout: сервер завис и соединение было "
                f"принудительно оборвано (модель: {model}): {e}"
            )
        raise TimeoutError(f"Ollama stream timeout: соединение оборвано ({e})")
    finally:
        watchdog.stop()
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def _ollama_chat_native(model, messages, temperature, max_tokens, timeout) -> dict:
    """
    Нестриминговый вызов нативного Ollama /api/chat с think:false.
    Возвращает {"content": str, "done_reason": str}.

    Тот же риск зависания, что и в стриминговом варианте (см. комментарий
    выше _StreamWatchdog) — requests timeout= здесь тоже ненадёжен как
    единственная защита, поэтому используем тот же сторожевой таймер.
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

    watchdog = _StreamWatchdog(hard_deadline=timeout, chunk_timeout=timeout,
                                label=model)
    watchdog.start()
    resp = None
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        watchdog.attach(resp)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message") or {}
        return {
            "content": msg.get("content", ""),
            "done_reason": data.get("done_reason", "stop"),
        }
    except (requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout) as e:
        if watchdog.fired():
            raise TimeoutError(
                f"Ollama timeout: сервер завис и соединение было "
                f"принудительно оборвано (модель: {model}): {e}"
            )
        raise TimeoutError(f"Ollama timeout: соединение оборвано ({e})")
    finally:
        watchdog.stop()
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


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

    При бэкенде Ollama не делает ничего: Ollama управляет KV-кэшем сама,
    у неё нет ни /v1/models в этом смысле, ни api/v0/models/reload. Без
    этой проверки функция после КАЖДОГО запроса стучалась на порт LM Studio
    (127.0.0.1:1234), которого в конфигурации с Ollama просто нет, висела
    там до истечения таймаута и писала в лог бесполезное
    "[KV-RESET] Не удалось получить список моделей: ...".
    """
    if _is_ollama_backend():
        return

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


# =============================================================================
# ФИЛЬТР ПО КОНКРЕТНЫМ ДОКУМЕНТАМ (уточнение перечня НПА в Советчике)
#
# ПОЧЕМУ ЭТО НЕ ПОСТФИЛЬТР. Раньше параметр filenames в search_vector_db
# применялся уже ПОСЛЕ гибридного поиска — к списку кандидатов, отобранных
# по всей базе. На корпусе в 12k+ чанков это не работает: пул кандидатов —
# топ-30 по всей базе, и если пользователь оставил 3 документа из 500, их
# чанков в этом пуле обычно нет вовсе. Постфильтр вырезал всё, поиск
# возвращал пустоту, и пользователь получал «не найдено релевантных
# документов» на вопрос, ответ на который в выбранном документе есть.
#
# ПРАВИЛЬНО — сузить пространство поиска ДО ранжирования:
#   вектор : where={"filename": {"$in": [...]}} прямо в collection.query()
#   BM25   : ограничить перебор позициями чанков выбранных документов
# Тогда топ-K берётся ВНУТРИ выбранных документов, и RRF сливает два
# полноценных списка, а не два обрубка.
#
# Постфильтр в search_vector_db оставлен как вторая линия защиты — он
# дешёвый и ловит случай, когда фильтр по какой-то причине не доехал до
# ретривера (например, fallback-ветка без rank_bm25).
# =============================================================================
def _filenames_where(filenames: Optional[List[str]]) -> Optional[dict]:
    """
    Собирает where-условие ChromaDB по списку имён файлов.

    Для одного файла — {"filename": "x"}, для нескольких — оператор $in.
    ChromaDB не принимает $in со списком из одного элемента в старых версиях,
    поэтому случай одного файла обрабатывается отдельно.
    """
    if not filenames:
        return None
    uniq = list(dict.fromkeys([f for f in filenames if f]))
    if not uniq:
        return None
    if len(uniq) == 1:
        return {"filename": uniq[0]}
    return {"filename": {"$in": uniq}}


class HybridRetriever:
    """BM25 + векторный поиск с Reciprocal Rank Fusion."""
 
    def __init__(self, collection):
        self.collection = collection
        self.all_docs:  list = []
        self.all_ids:   list = []
        self.all_meta:  list = []
        # filename → список позиций его чанков в self.all_* (для BM25-фильтра)
        self._fname_to_idx: dict = {}
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

        ПРО СЛУЖЕБНЫЙ СЛОЙ. Скрытые чанки (doc_type == "hidden") тоже попадают
        в этот индекс — это нормально и намеренно: индекс общий, а вырезаются
        они позже, на этапе формирования результатов (см. _is_hidden_chunk).
        Исключать их здесь нельзя: тогда сломается search_hidden_layer, которому
        нужен доступ к тем же метаданным.

        ПРО _fname_to_idx. Карта filename → позиции чанков строится здесь же,
        одним проходом по уже загруженным метаданным. Она нужна фильтру по
        конкретным документам: BM25 не умеет where-условий, и без карты
        пришлось бы на каждый запрос линейно перебирать 12k метаданных.
        Карта строится ДО проверки BM25_AVAILABLE — она может понадобиться
        и без BM25 (например, list_documents).
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

            # ── Карта filename → позиции чанков (для фильтра по документам) ──
            tm = time.perf_counter()
            self._fname_to_idx = {}
            for i, m in enumerate(self.all_meta):
                fname = (m or {}).get("filename", "")
                if fname:
                    self._fname_to_idx.setdefault(fname, []).append(i)
            print(f"[HYBRID] Карта документов: {len(self._fname_to_idx)} файлов "
                  f"за {time.perf_counter()-tm:.2f} сек")

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
    # Позиции чанков выбранных документов — для BM25-фильтра
    # ------------------------------------------------------------------
    def indices_for_filenames(self, filenames: List[str]) -> set:
        """Множество позиций в self.all_* для перечисленных файлов."""
        idx = set()
        for fname in filenames:
            idx.update(self._fname_to_idx.get(fname, []))
        return idx

    def known_filenames(self) -> list:
        """Список всех имён файлов, известных индексу."""
        return list(self._fname_to_idx.keys())
 
    # ------------------------------------------------------------------
    # Основной метод поиска
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: int = 20,
               filenames: Optional[List[str]] = None) -> list:
        """
        Возвращает список кандидатов, отсортированных по RRF-score.
        Каждый кандидат: {"id", "doc", "meta", "score", "in_vector", "in_bm25"}

        filenames — если передан непустой список, поиск ведётся ТОЛЬКО внутри
        этих документов (см. большой комментарий у _filenames_where). Фильтр
        применяется внутри обеих веток поиска, а не после слияния.
        """
        where       = _filenames_where(filenames)
        allowed_idx = None

        if filenames:
            allowed_idx = self.indices_for_filenames(filenames)
            if not allowed_idx:
                print(f"[FILE FILTER] Ни одного чанка по {len(filenames)} "
                      f"выбранным документам — поиск пуст")
                return []
            print(f"[FILE FILTER] Поиск ограничен {len(filenames)} документами "
                  f"({len(allowed_idx)} чанков)")

        vector_hits = self._vector_search(query, top_k, where=where)
        bm25_hits   = (self._bm25_search(query, top_k, allowed_idx=allowed_idx)
                       if self.bm25 else {})
        _bw = _load_search_settings().get("bm25_weight", 1.5)
        return self._rrf_merge(vector_hits, bm25_hits, bm25_weight=_bw)
 
    def _vector_search(self, query: str, top_k: int,
                       where: Optional[dict] = None) -> dict:
        """Возвращает {id: {"doc", "meta", "vector_rank"}}"""
        try:
            kwargs = dict(
                n_results=top_k,
                include=["documents", "metadatas", "distances"],
            )
            if where:
                kwargs["where"] = where

            embedding = embed_query(query)
            if embedding is not None:
                results = self.collection.query(query_embeddings=embedding, **kwargs)
            else:
                results = self.collection.query(query_texts=[query], **kwargs)
 
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
 
    def _bm25_search(self, query: str, top_k: int,
                     allowed_idx: Optional[set] = None) -> dict:
        """
        Возвращает {id: {"doc", "meta", "bm25_rank"}}

        allowed_idx — если задано, ранжирование идёт только по этим позициям
        (фильтр по конкретным документам). BM25 не поддерживает where-условий,
        поэтому ограничиваем сам перебор, а не результат.
        """
        try:
            tokens = self._tokenize(query)
            scores = self.bm25.get_scores(tokens)

            pool = allowed_idx if allowed_idx is not None else range(len(scores))
            # Нулевые совпадения отбрасываем сразу — они всё равно не нужны
            candidates_idx = [i for i in pool if scores[i] > 0]
            top_idx = sorted(candidates_idx,
                             key=lambda i: scores[i], reverse=True)[:top_k]

            return {
                self.all_ids[i]: {
                    "doc":      self.all_docs[i],
                    "meta":     self.all_meta[i] or {},
                    "bm25_rank": rank,
                }
                for rank, i in enumerate(top_idx)
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
    invalidate_documents_cache()
    print("[HYBRID] Индекс сброшен. Будет перестроен при следующем запросе.")


# =============================================================================
# РЕЕСТР ДОКУМЕНТОВ — источник данных для датагрида «Уточнение перечня НПА»
#
# ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ. Диалогу уточнения нужен список ДОКУМЕНТОВ, а в
# ChromaDB лежат ЧАНКИ. Тянуть ради этого ещё один полный collection.get()
# на каждое открытие диалога — это несколько секунд и лишняя память, тогда
# как HybridRetriever уже держит все метаданные в self.all_meta. Поэтому
# реестр агрегируется из них, а прямой запрос к ChromaDB — только фоллбэк
# на случай, когда rank_bm25 не установлен и ретривера нет вовсе.
#
# КЭШ. Результат кэшируется по (число_чанков, фильтры): пока база не
# переиндексирована, ответ не меняется. Сбрасывается вместе с BM25-индексом
# (см. invalidate_hybrid_retriever) — они инвалидируются по одной причине.
#
# СЛУЖЕБНЫЙ СЛОЙ в реестр НЕ попадает никогда: пользователь не должен ни
# видеть его документы, ни иметь возможность отфильтровать по ним ответ.
# =============================================================================
_DOCS_CACHE_KEY = "__regula_ai_docs_registry__"
_docs_lock      = threading.Lock()


def invalidate_documents_cache():
    """Сбрасывает кэш реестра документов (после переиндексации)."""
    sys.modules.pop(_DOCS_CACHE_KEY, None)


def _iter_all_metadatas() -> list:
    """
    Все метаданные чанков коллекции.

    Сначала пытаемся взять у HybridRetriever (они там уже загружены),
    иначе — прямой запрос к ChromaDB.
    """
    retriever = get_hybrid_retriever()
    if retriever is not None and retriever.all_meta:
        return retriever.all_meta

    collection = get_chroma_collection()
    if collection is None:
        return []
    try:
        res = collection.get(include=["metadatas"])
        return res.get("metadatas") or []
    except Exception as e:
        print(f"[DOCS] Не удалось загрузить метаданные: {e}")
        return []


def list_documents(spheres: Optional[List[str]] = None,
                   doc_types: Optional[List[str]] = None,
                   doc_status: Optional[str] = "active") -> List[Dict]:
    """
    Реестр документов базы НПА для диалога уточнения.

    Фильтры работают ровно по тем же правилам, что и в поиске
    (_sphere_match / _doc_type_match / _status_match): документы без
    проставленной сферы, вида или статуса проходят фильтр — обратная
    совместимость со старыми документами.

    Возвращает список словарей, отсортированный по имени файла:
        {"filename", "sphere", "doc_type", "doc_status",
         "valid_from", "valid_to", "category", "chunks"}

    chunks — число чанков документа, ПРОШЕДШИХ фильтр. Полезно в UI:
    документ с одним чанком почти наверняка недоиндексирован.
    """
    _regular_doc_types, _, _ = _split_doc_types(doc_types)

    cache_key = json.dumps(
        {"s": sorted(spheres or []), "d": sorted(_regular_doc_types or []),
         "st": doc_status},
        ensure_ascii=False, sort_keys=True,
    )

    metas = _iter_all_metadatas()
    n_chunks = len(metas)

    with _docs_lock:
        cached = sys.modules.get(_DOCS_CACHE_KEY)
        if (isinstance(cached, dict)
                and cached.get("n_chunks") == n_chunks
                and cache_key in cached.get("data", {})):
            return cached["data"][cache_key]

    t0 = time.perf_counter()
    docs: Dict[str, Dict] = {}

    for meta in metas:
        meta = meta or {}

        # Служебный слой — безусловно вне реестра
        if _is_hidden_chunk(meta):
            continue

        fname = meta.get("filename", "")
        if not fname:
            continue

        if spheres and not _sphere_match(meta.get("sphere", ""), spheres):
            continue
        if _regular_doc_types and not _doc_type_match(meta.get("doc_type", ""),
                                                      _regular_doc_types):
            continue
        if doc_status is not None and not _status_match(meta.get("doc_status", ""),
                                                        doc_status):
            continue

        entry = docs.get(fname)
        if entry is None:
            docs[fname] = {
                "filename":   fname,
                "sphere":     meta.get("sphere", ""),
                "doc_type":   meta.get("doc_type", ""),
                "doc_status": meta.get("doc_status", ""),
                "valid_from": meta.get("valid_from", ""),
                "valid_to":   meta.get("valid_to", ""),
                "category":   meta.get("category", ""),
                "chunks":     1,
            }
        else:
            entry["chunks"] += 1
            # Метаданные документа берём из первого чанка, но пустые поля
            # добираем из последующих: индексация не всегда проставляет
            # всё в каждый чанк.
            for field in ("sphere", "doc_type", "doc_status",
                          "valid_from", "valid_to", "category"):
                if not entry[field] and meta.get(field):
                    entry[field] = meta.get(field)

    result = sorted(docs.values(), key=lambda d: d["filename"].lower())

    with _docs_lock:
        cached = sys.modules.get(_DOCS_CACHE_KEY)
        if not isinstance(cached, dict) or cached.get("n_chunks") != n_chunks:
            cached = {"n_chunks": n_chunks, "data": {}}
        cached["data"][cache_key] = result
        sys.modules[_DOCS_CACHE_KEY] = cached

    print(f"[DOCS] Реестр: {len(result)} документов из {n_chunks} чанков "
          f"за {time.perf_counter()-t0:.2f} сек "
          f"(сферы={spheres}, виды={_regular_doc_types}, статус={doc_status})")
    return result
 
 
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
                  namespace: Optional[str] = None,
                  hidden_sources: Optional[list] = None,
                  internal_mode: bool = False) -> str:
    """
    Ключ кэша LLM-ответа.

    namespace ОБЯЗАТЕЛЬНО входит в хэш: без него ответ, построенный на
    локальной базе одного сегмента, мог бы вернуться пользователю другого.
    Если не передан — определяется автоматически (get_cache_namespace).

    hidden_sources — чанки служебного слоя, подмешанные в промпт. Они ТОЖЕ
    обязаны входить в ключ, иначе первый же ответ, сгенерированный без слоя
    (или со старой его редакцией), закэшируется и будет возвращаться вместо
    ответа со слоем — слой перестанет влиять на всё, кроме самого первого
    запроса. Скрытый слой невидим в интерфейсе, поэтому такую поломку никто
    бы не заметил.

    internal_mode — во внутреннем режиме системный промпт другой, значит и
    ответ другой. Без этого флага ответы двух режимов делили бы один ключ.

    ПРО ФИЛЬТР ДОКУМЕНТОВ. Отдельный компонент ключа для него не нужен:
    фильтр меняет состав sources, а sources уже входит в хэш. Два разных
    уточнения перечня НПА дадут разные источники → разные ключи.
    """
    if namespace is None:
        namespace = get_cache_namespace()
    s = json.dumps(
        sorted([x.get('file', '') + x.get('snippet', '')[:100] for x in sources]),
        sort_keys=True,
    )
    h = json.dumps(
        sorted([x.get('file', '') + x.get('snippet', '')[:100]
                for x in (hidden_sources or [])]),
        sort_keys=True,
    )
    mode = "internal" if internal_mode else "normal"
    raw = f"{namespace}|||{query}|||{s}|||{h}|||{mode}|||{model}"
    return hashlib.md5(raw.encode()).hexdigest()


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
    # ── Служебный слой знаний ────────────────────────────────────────────────
    # hidden_layer_enabled     — главный выключатель слоя целиком
    # hidden_top_k             — сколько фрагментов слоя максимум идёт в промпт
    # hidden_min_score         — порог релевантности по скору реранкера.
    #                            Скор CrossEncoder — это логит, примерно от -10
    #                            до +10; 0.0 ≈ «модель считает фрагмент скорее
    #                            подходящим, чем нет». Без порога слой лез бы
    #                            в КАЖДЫЙ ответ независимо от темы вопроса.
    # hidden_neighbor_radius   — соседние чанки вокруг найденного фрагмента
    #                            слоя (пояснение часто не влезает в один чанк)
    # hidden_max_chars         — бюджет символов на блок слоя в промпте
    # hidden_query_expansion   — искать ли нормативку дополнительно по тексту
    #                            найденного пояснения (см. search_vector_db)
    "hidden_layer_enabled":  True,
    "hidden_top_k":          3,
    "hidden_min_score":      0.0,
    "hidden_neighbor_radius": 1,
    "hidden_max_chars":      2500,
    "hidden_query_expansion": False,
}
def _load_search_settings() -> dict:
    """Загружает настройки поиска из конфига. Fallback → DEFAULT_SEARCH_SETTINGS."""
    try:
        import streamlit as st
        ss = st.session_state.get("_search_settings")
        if ss:
            return {**DEFAULT_SEARCH_SETTINGS, **ss}
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


def _is_hidden_chunk(meta) -> bool:
    """
    True, если чанк принадлежит СЛУЖЕБНОМУ СЛОЮ (см. большой комментарий
    вверху файла). Такие чанки обязаны вырезаться из любой пользовательской
    выдачи безусловно — независимо от выбранных фильтров.
    """
    return (meta or {}).get("doc_type", "") == HIDDEN_DOC_TYPE


def _strip_hidden(candidates: list) -> list:
    """
    Жёсткий гейт служебного слоя для списка кандидатов гибридного поиска
    (элементы вида {"id","doc","meta",...}).
    """
    if not candidates:
        return candidates
    pre = len(candidates)
    out = [c for c in candidates if not _is_hidden_chunk(c.get("meta"))]
    if pre != len(out):
        print(f"[HIDDEN GATE] Вырезано из обычной выдачи: {pre - len(out)} чанков слоя")
    return out


def _doc_type_match(chunk_doc_type: str, selected_doc_types: list) -> bool:
    """
    Проверяет, подходит ли чанк под фильтр видов документов.
    Чанки без поля doc_type (старые документы / неопределённый тип)
    всегда проходят фильтр — обратная совместимость.
    Значения doc_type: 'npa', 'fas', 'court', 'methodics', 'unknown'.

    Служебный слой ('hidden') не проходит НИКОГДА — даже если кто-то
    передаст его в selected_doc_types. Это вторая линия защиты; основная —
    _strip_hidden(), вызываемая безусловно.
    """
    if chunk_doc_type == HIDDEN_DOC_TYPE:
        return False
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
    Разбивает список doc_types на
    (обычные_типы_для_tariff_docs, нужна_ли_локальная_база, внутренний_режим).

    Примеры:
      None                   -> (None,  False, False)  — фильтр выключен
      ["npa"]                -> (["npa"], False, False)
      ["local"]              -> (None,  True,  False)  — только локальная база
      ["npa", "local"]       -> (["npa"], True, False) — и там, и там
      ["hidden_only"]        -> (None,  False, True)   — ВНУТРЕННИЙ РЕЖИМ:
                                                         поиск по документам
                                                         не выполняется вообще
    """
    if not doc_types:
        return None, False, False
    include_local = LOCAL_KB_DOC_TYPE in doc_types
    hidden_only   = HIDDEN_ONLY_DOC_TYPE in doc_types
    regular = [
        dt for dt in doc_types
        if dt not in (LOCAL_KB_DOC_TYPE, HIDDEN_ONLY_DOC_TYPE, HIDDEN_DOC_TYPE)
    ]
    return (regular if regular else None), include_local, hidden_only


# =============================================================================
# СЛУЖЕБНЫЙ СЛОЙ: поиск
#
# Отдельный прямой запрос к ChromaDB с where={"doc_type": "hidden"}.
# Почему не берём скрытые чанки из общего пула кандидатов — см. большой
# комментарий вверху файла: их на порядки меньше, и в топ общего поиска они
# не попадают даже при идеальной релевантности.
#
# ПРО ФИЛЬТР ДОКУМЕНТОВ. Уточнение перечня НПА на служебный слой НЕ влияет:
# слой — это внутренние методические пояснения организации, а не источник,
# который пользователь выбирает в диалоге. Он и в списке документов не
# показывается (см. list_documents).
# =============================================================================
def search_hidden_layer(query: str, top_k: Optional[int] = None) -> list:
    """
    Возвращает список фрагментов служебного слоя, релевантных запросу,
    в том же формате, что и обычные источники, но с source_kind="hidden".

    Пустой список — нормальная ситуация: слой выключен, пуст или ни один
    фрагмент не прошёл порог релевантности.
    """
    ss = _load_search_settings()
    if not ss.get("hidden_layer_enabled", True):
        return []

    if top_k is None:
        top_k = int(ss.get("hidden_top_k", 3))
    if top_k <= 0:
        return []

    collection = get_chroma_collection()
    if collection is None:
        return []

    t0 = time.perf_counter()

    # Берём с запасом: часть кандидатов отсеет порог релевантности.
    pool = max(top_k * 4, 10)
    try:
        embedding = embed_query(query)
        if embedding is not None:
            res = collection.query(
                query_embeddings=embedding,
                n_results=pool,
                where={"doc_type": HIDDEN_DOC_TYPE},
                include=["documents", "metadatas", "distances"],
            )
        else:
            res = collection.query(
                query_texts=[query],
                n_results=pool,
                where={"doc_type": HIDDEN_DOC_TYPE},
                include=["documents", "metadatas", "distances"],
            )
    except Exception as e:
        # Коллекция пуста или в ней вообще нет чанков слоя — не ошибка
        print(f"[HIDDEN] Запрос к слою не выполнен: {e}")
        return []

    docs  = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    ids   = (res.get("ids") or [[]])[0]

    if not docs:
        print("[HIDDEN] В служебном слое нет подходящих фрагментов")
        return []

    candidates = []
    for _id, doc, meta, dist in zip(ids, docs, metas, dists):
        candidates.append({
            "id":    _id,
            "doc":   doc,
            "meta":  meta or {},
            "score": 1.0 - float(dist),
        })

    # ── Реранкинг и порог релевантности ─────────────────────────────────────
    # Без порога слой подмешивался бы в КАЖДЫЙ ответ: векторный поиск всегда
    # что-нибудь возвращает, даже когда тема вопроса никак не связана со слоем.
    reranker = get_reranker() if ss.get("reranker_enabled", True) else None
    if reranker:
        candidates = reranker.rerank(query, candidates, top_n=max(top_k * 2, top_k))
        min_score  = float(ss.get("hidden_min_score", 0.0))
        before     = len(candidates)
        candidates = [c for c in candidates
                      if float(c.get("rerank_score", 0.0)) >= min_score][:top_k]
        print(f"[HIDDEN] Порог {min_score}: {before} → {len(candidates)} фрагментов")
    else:
        # Реранкер выключен — порог применить не к чему (RRF-скор несопоставим
        # с логитом CrossEncoder). Берём топ по дистанции без отсечения.
        candidates = candidates[:top_k]
        print(f"[HIDDEN] Реранкер выключен — взяли топ-{len(candidates)} по вектору")

    if not candidates:
        return []

    # ── Соседние чанки ──────────────────────────────────────────────────────
    # Пояснение редко укладывается в один чанк: без соседей модель получает
    # обрывок фразы и толку от слоя мало.
    radius    = int(ss.get("hidden_neighbor_radius", 1))
    neighbors = _fetch_neighbors(candidates, collection, radius)

    sources = []
    for c in candidates:
        meta  = c.get("meta") or {}
        fname = meta.get("filename", "unknown")
        cidx  = int(meta.get("chunk_index", 0))
        snippet = neighbors.get((fname, cidx)) or c.get("doc", "")
        sources.append({
            "snippet":      snippet,
            "file":         meta.get("filename", "Служебный слой"),
            "page":         meta.get("page", ""),
            "category":     "Служебный слой",
            "doc_type":     HIDDEN_DOC_TYPE,
            "doc_status":   "",
            "article":      meta.get("article", ""),
            "chunk_index":  meta.get("chunk_index", ""),
            "distance":     round(max(0.0, 1.0 - c.get("score", 0.0)), 3),
            "rerank_score": round(float(c.get("rerank_score", 0.0)), 3),
            "sphere":       "",
            "source_kind":  "hidden",
        })

    print(f"[HIDDEN] Подобрано {len(sources)} фрагментов слоя "
          f"за {time.perf_counter()-t0:.3f} сек")
    return sources


def _build_hidden_context(hidden_sources: Optional[list],
                          max_chars: Optional[int] = None) -> str:
    """
    Собирает блок служебных пояснений для промпта.

    Фрагменты НЕ нумеруются и НЕ подписываются именами файлов — намеренно:
    любая подпись провоцирует модель сослаться на «документ №2», а этот блок
    по определению не является цитируемым источником. Вместо этого блок
    открывается жёсткой инструкцией о том, как им пользоваться.
    """
    if not hidden_sources:
        return ""

    if max_chars is None:
        max_chars = int(_load_search_settings().get("hidden_max_chars", 2500))

    header = (
        "СЛУЖЕБНЫЕ ПОЯСНЕНИЯ (внутренняя методическая информация организации).\n"
        "Как их использовать:\n"
        "- учитывай их содержание при подготовке ответа наравне с нормативными документами;\n"
        "- при расхождении с твоими общими знаниями верь этим пояснениям;\n"
        "- излагай их содержание СВОИМИ СЛОВАМИ, как собственное профессиональное знание.\n"
        "СТРОГО ЗАПРЕЩЕНО: ссылаться на этот блок, цитировать его, нумеровать как источник, "
        "упоминать сам факт его существования, писать «согласно пояснению», "
        "«во внутренней информации указано» и подобные обороты.\n"
        "---"
    )

    parts, budget = [], max_chars
    for src in hidden_sources:
        if budget <= 0:
            break
        text = (src.get("snippet") or "").strip()
        if not text:
            continue
        if len(text) > budget:
            text = text[:budget] + "..."
        parts.append(text)
        budget -= len(text)

    if not parts:
        return ""

    return header + "\n" + "\n\n".join(parts) + "\n---"


def search_vector_db(query: str, top_k: int = 5, spheres: list = None,
                     doc_types: list = None, doc_status: str = "active",
                     filenames: list = None, org_id: str = None,
                     hidden_sources: list = None) -> list:
    """
    org_id — идентификатор сегмента текущего пользователя. Требуется только
    когда doc_types содержит "local" (запрос к локальной базе сегмента);
    для обычного поиска по tariff_docs не используется.

    Если org_id не передан — определяется автоматически из session_state
    (get_current_org_id). Явный аргумент имеет приоритет: он нужен для вызовов
    из фоновых потоков, где session_state недоступен.

    filenames — уточнение перечня документов из диалога Советчика. Непустой
    список означает: искать ТОЛЬКО внутри этих документов. Фильтр уходит
    внутрь HybridRetriever (where в ChromaDB + ограничение пула BM25), а не
    применяется к готовому списку кандидатов — иначе на корпусе в 12k чанков
    выдача почти всегда оказывалась бы пустой (см. комментарий у
    _filenames_where). Постфильтр ниже оставлен как вторая линия защиты.

    hidden_sources — уже найденные фрагменты служебного слоя (см.
    search_hidden_layer). Сами они в результат НЕ попадают никогда. Они
    используются только если включена настройка hidden_query_expansion: их
    текст добавляется как дополнительный ВАРИАНТ ЗАПРОСА к обычному поиску.
    Смысл: если в слое написано «ФОТ считаем по 760-э, форма 4.2», то поиск
    по НПА подтянет 760-э даже когда пользователь спросил своими словами
    («сколько людей закладывать»). Это даёт эффект второго прохода без
    второго вызова LLM и без перефраза, который терял бы точные названия.
    """
    t0 = time.perf_counter()

    # Явный аргумент имеет приоритет; иначе берём сегмент из session_state.
    # "" превращаем в None — у пользователя без сегмента локальной базы нет.
    org_id = org_id or get_current_org_id() or None

    _regular_doc_types, _include_local, _hidden_only = _split_doc_types(doc_types)

    # Нормализуем список файлов: пустой список — это «фильтр не задан»,
    # а не «искать в нуле документов».
    filenames = [f for f in (filenames or []) if f] or None

    # ── Внутренний режим ────────────────────────────────────────────────────
    # Поиск по документам не выполняется вообще: ни tariff_docs, ни локальная
    # база. Ответ строится на знаниях модели плюс служебный слой, который
    # вызывающий код получает отдельно через search_hidden_layer().
    if _hidden_only:
        print("[INTERNAL MODE] Поиск по документам отключён — источников нет")
        return []

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

        # ── Шаг 1б: подсказки из служебного слоя как варианты запроса ───────
        _ss_pre = _load_search_settings()
        _hidden_variants = []
        if hidden_sources and _ss_pre.get("hidden_query_expansion", False):
            for h in hidden_sources[:2]:
                _ht = (h.get("snippet") or "").strip()
                if _ht:
                    # 400 символов — достаточно, чтобы попали названия
                    # документов и форм, и мало, чтобы не размыть эмбеддинг.
                    _hidden_variants.append(_ht[:400])
            if _hidden_variants:
                print(f"[HIDDEN EXPANSION] Добавлено вариантов запроса из слоя: "
                      f"{len(_hidden_variants)}")
 
        # Оригинальный запрос первым, синонимы — после, максимум 3 варианта
        unique_variants = [query] + synonym_variants[:2] + _hidden_variants
 
        if len(unique_variants) > 1:
            print(f"[SYNONYMS] {len(unique_variants)} вариантов: "
                  f"{[v[:60] for v in unique_variants]}")
 
        # ── Шаг 2: гибридный поиск по всем вариантам ────────────────────────
        # Для каждого варианта запроса делаем поиск и собираем кандидатов.
        # Один кандидат может встретиться в нескольких вариантах — берём
        # лучший (максимальный) RRF-score.
        _ss = _ss_pre
        _cands_per_var = int(_ss.get("candidates_per_var", 15))
        # При активном фильтре по сфере или виду документа запрашиваем вдвое больше
        # кандидатов, чтобы компенсировать потери от постфильтрации.
        if spheres or _regular_doc_types:
            _cands_per_var = _cands_per_var * 2
        _reranker_on   = bool(_ss.get("reranker_enabled", True))
 
        merged: dict = {}   # id → candidate dict
        for variant in unique_variants:
            # filenames уходит ВНУТРЬ поиска — пул кандидатов сразу строится
            # только из выбранных документов.
            for c in retriever.search(variant, top_k=_cands_per_var,
                                      filenames=filenames):
                cid = c["id"]
                if cid not in merged or c["score"] > merged[cid]["score"]:
                    merged[cid] = c
 
        candidates = sorted(merged.values(), key=lambda x: x["score"], reverse=True)

        # ── Гейт служебного слоя (БЕЗУСЛОВНО, до всех остальных фильтров) ───
        # См. большой комментарий вверху файла: обычный фильтр по doc_type
        # здесь не помогает, потому что он применяется только при явно
        # выбранных видах документов и пропускает пустой/unknown тип.
        candidates = _strip_hidden(candidates)
 
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
        # _regular_doc_types — только npa/fas/court/methodics, служебные
        # псевдотипы ("local", "hidden_only") уже вырезаны _split_doc_types.
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

        # Постфильтр по файлам — вторая линия защиты. Основная фильтрация уже
        # произошла внутри retriever.search(), здесь ловим только случай, когда
        # в пул каким-то образом просочился чужой чанк.
        if filenames:
            _fn_set = set(filenames)
            _pre = len(candidates)
            candidates = [c for c in candidates
                          if c.get("meta", {}).get("filename", "") in _fn_set]
            if _pre != len(candidates):
                print(f"[FILE FILTER] постфильтр: {_pre} → {len(candidates)} "
                      f"по {len(_fn_set)} файлам")
 
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
        #
        # При активном уточнении перечня НПА локальная база НЕ подмешивается:
        # пользователь явно ограничил ответ конкретными документами общей базы,
        # и подмешивать туда документы сегмента — прямое нарушение этого
        # ограничения (в диалоге уточнения их не было и снять галку было
        # невозможно).
        if _include_local and org_id and not filenames:
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
        elif _include_local and org_id and filenames:
            print("[LOCAL_KB] Пропущена: активно уточнение перечня НПА")

        print(f"[TIMING] search_vector_db итого: {time.perf_counter()-t0:.3f} сек")
        return sources
 
    # ── Fallback: чистый векторный поиск ────────────────────────────────────
    print("[TIMING] Fallback — чистый векторный поиск (rank_bm25 не установлен)")
    # Фильтр по файлам уходит прямо в where ChromaDB — постфильтр здесь так же
    # бесполезен, как и в основной ветке.
    _fallback_sources = _pure_vector_search(
        query, top_k, t0, where=_filenames_where(filenames),
    )
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
    if _include_local and org_id and not filenames:
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
    filenames: конкретные документы (уточнение перечня НПА). Как и в боевом
               поиске, уходит внутрь ретривера, а не применяется постфактум.

    Служебный слой в эту выдачу НЕ попадает — для его проверки есть отдельный
    тест на вкладке «Служебный слой» в Админке.
    """
    result = {
        "query_variants": [query],
        "pre_rerank":     [],
        "post_rerank":    [],
        "reranker_used":  False,
        "elapsed":        0.0,
        "error":          None,
    }

    _regular_doc_types, _, _ = _split_doc_types(doc_types)
    filenames = [f for f in (filenames or []) if f] or None

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
            for c in retriever.search(variant, top_k=_cands_per_var,
                                      filenames=filenames):
                cid = c["id"]
                if cid not in merged or c["score"] > merged[cid]["score"]:
                    merged[cid] = c

        pre_rerank = sorted(merged.values(), key=lambda x: x["score"], reverse=True)

        # Жёсткий гейт служебного слоя — до всех остальных фильтров
        pre_rerank = _strip_hidden(pre_rerank)

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
            if _pre != len(pre_rerank):
                print(f"[FILE FILTER/debug] постфильтр: {_pre} → {len(pre_rerank)} "
                      f"по {len(_fn_set)} файлам")

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


def debug_hidden_layer(query: str, top_k: Optional[int] = None) -> dict:
    """
    Диагностика служебного слоя для вкладки «Служебный слой» в Админке.

    Возвращает:
      {"total_chunks": сколько чанков слоя в базе,
       "found": [фрагменты, прошедшие порог],
       "settings": актуальные настройки слоя,
       "elapsed": время, "error": текст ошибки или None}
    """
    out = {"total_chunks": 0, "found": [], "settings": {},
           "elapsed": 0.0, "error": None}
    t0 = time.perf_counter()
    try:
        out["settings"] = {
            k: v for k, v in _load_search_settings().items()
            if k.startswith("hidden_") or k == "reranker_enabled"
        }
        collection = get_chroma_collection()
        if collection is None:
            out["error"] = "Коллекция tariff_docs недоступна"
            return out
        try:
            res = collection.get(where={"doc_type": HIDDEN_DOC_TYPE}, include=[])
            out["total_chunks"] = len(res.get("ids", []))
        except Exception:
            out["total_chunks"] = 0

        if query and query.strip():
            out["found"] = search_hidden_layer(query.strip(), top_k=top_k)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    out["elapsed"] = round(time.perf_counter() - t0, 3)
    return out


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
 
 
 
def _pure_vector_search(query: str, top_k: int = 5, t0=None,
                        where: Optional[dict] = None) -> list:
    """
    Оригинальный векторный поиск. Используется как fallback.

    where — необязательное условие ChromaDB (например, фильтр по конкретным
    документам из диалога уточнения). Уходит прямо в query(), чтобы сужение
    работало ДО отбора топ-K, а не после.
    """
    if t0 is None:
        t0 = time.perf_counter()
 
    collection = get_chroma_collection()
    if collection is None:
        return []
 
    t1        = time.perf_counter()
    embedding = embed_query(query)
    print(f"[TIMING] embed_query: {time.perf_counter()-t1:.3f} сек")
 
    try:
        kwargs = dict(
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        if where:
            kwargs["where"] = where
        if embedding is not None:
            results = collection.query(query_embeddings=embedding, **kwargs)
        else:
            results = collection.query(query_texts=[query], **kwargs)
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
        # Жёсткий гейт служебного слоя — и в fallback-ветке тоже
        if _is_hidden_chunk(meta):
            continue
        sources.append({
            "snippet":     doc[:800] + ("..." if len(doc) > 800 else ""),
            "file":        meta.get("filename", "Неизвестно"),
            "page":        meta.get("page", ""),
            "category":    meta.get("category", "Общее"),
            "doc_type":    meta.get("doc_type", ""),
            "doc_status":  meta.get("doc_status", ""),
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
# Сборка системного промпта — общая для обычного и внутреннего режима
# =============================================================================
_LENGTH_INSTRUCTIONS = {
    "short":    "8. Отвечай КРАТКО: максимум 3–5 предложений или маркированный список до 5 пунктов. "
                "Без вводных слов и пересказа вопроса.",
    "detailed": "8. Отвечай РАЗВЁРНУТО: подробно раскрой тему, приведи все релевантные нормы, "
                "условия применения и исключения. Используй подзаголовки если тем несколько.",
}


def _build_system_prompt(prompts: dict, user_context: str = "",
                         answer_length: str = "short",
                         internal_mode: bool = False) -> str:
    """
    Собирает системный промпт: базовый текст + контекст пользователя +
    инструкция по длине ответа + текущая дата.

    internal_mode переключает базовый текст на advisor_internal_system —
    обычный промпт требует опираться исключительно на контекст из документов,
    которого во внутреннем режиме нет вовсе, и модель отказывалась бы отвечать.
    """
    key = "advisor_internal_system" if internal_mode else "advisor_system"
    system_prompt = prompts.get(key, DEFAULT_PROMPTS[key])

    if user_context and user_context.strip():
        system_prompt = (
            system_prompt
            + "\n\n---\nКонтекст пользователя:\n"
            + user_context.strip()
        )

    system_prompt = system_prompt + "\n" + _LENGTH_INSTRUCTIONS.get(
        answer_length, _LENGTH_INSTRUCTIONS["short"]
    )
    system_prompt = (
        system_prompt
        + "\n\nТекущая дата и время: "
        + datetime.now().strftime("%d.%m.%Y, %H:%M")
        + "."
    )
    return system_prompt
 
 
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
    hidden_sources: list = None,
    internal_mode: bool = False,
):
    """
    Генератор токенов для Streamlit st.write_stream().
    Если ответ есть в кэше — возвращает его сразу одним куском.
    Иначе стримит токены по мере генерации LLM.
    Автоматически сохраняет ответ в кэш после завершения.
    answer_length: "short" — кратко по существу, "detailed" — развёрнуто с пояснениями.
    org_id: сегмент пользователя. Если None — неймспейс кэша определяется
            автоматически из session_state (см. get_cache_namespace).

    hidden_sources: фрагменты служебного слоя. Уходят в промпт отдельным
            блоком (см. _build_hidden_context) и входят в ключ кэша, но
            НИКОГДА не показываются пользователю как источник.
    internal_mode: внутренний режим — без источников, другой системный промпт.
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
    cache_key = get_cache_key(query, sources, model, namespace=namespace,
                              hidden_sources=hidden_sources,
                              internal_mode=internal_mode)
    cached_answer = _cache_get(cache_key)
    if cached_answer is not None:
        print(f"[CACHE HIT stream] {model} | ns={namespace}")
        yield cached_answer
        return
 
    # Строим промпт (та же логика что в generate_ai_answer)
    try:
        prompts = load_prompts()

        system_prompt = _build_system_prompt(
            prompts, user_context=user_context,
            answer_length=answer_length, internal_mode=internal_mode,
        )

        if internal_mode:
            user_content = prompts.get(
                "advisor_internal_user", DEFAULT_PROMPTS["advisor_internal_user"]
            ).format(query=query)
        else:
            context = _build_context(sources)
            user_content = prompts.get(
                "advisor_user", DEFAULT_PROMPTS["advisor_user"]
            ).format(query=query, context=context)

        # Блок служебных пояснений идёт ПЕРЕД основным контекстом: так модель
        # читает правила обращения с ним до того, как увидит нормативку.
        hidden_block = _build_hidden_context(hidden_sources)
        if hidden_block:
            user_content = hidden_block + "\n\n" + user_content
            print(f"[HIDDEN] В промпт добавлен блок слоя: "
                  f"{len(hidden_block)} симв., {len(hidden_sources or [])} фрагм.")
 
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
              f"native={use_native} | internal={internal_mode} | "
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

        # Сбрасываем KV-кэш LM Studio в фоне — не блокируем UI.
        # При Ollama поток не создаём вовсе: сбрасывать нечего (Ollama ведёт
        # KV-кэш сама), а лишний поток на каждый запрос — только мусор.
        if not _is_ollama_backend():
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
    hidden_sources: list = None,
    internal_mode: bool = False,
):
    """
    Генератор токенов для уточняющих вопросов.

    Args:
        clarify_q:      текст уточняющего вопроса
        prev_answer:    предыдущий ответ LLM (исходный или последнее уточнение)
        new_sources:    чанки из RAG, найденные по clarify_q
        model:          модель LM Studio
        temperature:    температура генерации
        user_context:   контекст пользователя (роль, организация)
        answer_length:  "short" | "detailed"
        hidden_sources: фрагменты служебного слоя, найденные по clarify_q
        internal_mode:  внутренний режим (без источников)
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
        system_prompt = _build_system_prompt(
            prompts, user_context=user_context,
            answer_length=answer_length, internal_mode=internal_mode,
        )

        # Контекст новых RAG-чанков (без псевдо-источника предыдущего ответа)
        if internal_mode:
            rag_context = ""
        else:
            rag_context = _build_context(new_sources) if new_sources \
                          else "(новых документов не найдено)"

        # Промпт уточнения: предыдущий ответ — явный отдельный блок
        PREV_ANSWER_LIMIT = 2000   # символов — достаточно для контекста, не раздувает промпт
        user_content = (
            "Ты продолжаешь консультацию. Ниже приведён предыдущий ответ"
            + ("." if internal_mode else " и новые фрагменты документов.")
            + "\n\n"
            "## Предыдущий ответ\n"
            f"{prev_answer[:PREV_ANSWER_LIMIT]}"
            + (" _(сокращено)_" if len(prev_answer) > PREV_ANSWER_LIMIT else "")
            + "\n\n"
        )
        if not internal_mode:
            user_content += (
                "## Новые фрагменты нормативных документов\n"
                f"{rag_context}\n\n"
            )
        user_content += (
            "## Вопрос уточнения\n"
            f"{clarify_q}\n\n"
            "Дай ответ на вопрос уточнения, опираясь на предыдущий ответ"
            + ("." if internal_mode else " и новые документы.")
            + " Не повторяй то, что уже было сказано, если это не нужно для ответа."
        )

        # Служебный слой — тем же блоком, что и в основном ответе
        hidden_block = _build_hidden_context(hidden_sources)
        if hidden_block:
            user_content = hidden_block + "\n\n" + user_content

        # См. комментарий в stream_ai_answer — надёжный think:false только
        # через нативный Ollama /api/chat, extra_body на /v1 ненадёжен.
        is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
        use_native = is_qwen3 and _is_ollama_backend()

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ]

        print(f"[CLARIFY stream] {model} | native={use_native} | internal={internal_mode} | "
              f"промпт ~{len(system_prompt)+len(user_content)} симв. | "
              f"prev_answer={len(prev_answer)} симв. | rag_chunks={len(new_sources or [])} | "
              f"hidden={len(hidden_sources or [])}")
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
        if not _is_ollama_backend():
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
    hidden_sources: list = None,
    internal_mode: bool = False,
) -> str:
    config      = load_config()
    model       = model or config.get("default_model", "qwen/qwen3.5-9b")
    temperature = temperature if temperature is not None else config.get("temperature", 0.3)
    max_tokens  = config.get("max_tokens", 2048)
    timeout     = config.get("timeout_seconds", 300)
    namespace   = namespace_for_org(org_id) if org_id else get_cache_namespace()
 
    if _SOURCES_ONLY_MODE:
        return "[РЕЖИМ ТЕСТА ЧАНКОВ] LLM отключен."
 
    cache_key = get_cache_key(query, sources, model, namespace=namespace,
                              hidden_sources=hidden_sources,
                              internal_mode=internal_mode)
    cached_answer = _cache_get(cache_key)
    if cached_answer is not None:
        print(f"[CACHE HIT] {model} | ns={namespace}")
        return cached_answer
 
    try:
        prompts = load_prompts()

        _sys_key = "advisor_internal_system" if internal_mode else "advisor_system"
        system_prompt = prompts.get(_sys_key, DEFAULT_PROMPTS[_sys_key])

        if internal_mode:
            user_content = prompts.get(
                "advisor_internal_user", DEFAULT_PROMPTS["advisor_internal_user"]
            ).format(query=query)
        else:
            context = _build_context(sources)
            user_content = prompts.get(
                "advisor_user", DEFAULT_PROMPTS["advisor_user"]
            ).format(query=query, context=context)

        hidden_block = _build_hidden_context(hidden_sources)
        if hidden_block:
            user_content = hidden_block + "\n\n" + user_content
 
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
              f"native={use_native} | internal={internal_mode} | "
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

        # Сбрасываем KV-кэш LM Studio в фоне — не блокируем UI.
        # При Ollama поток не создаём вовсе: сбрасывать нечего (Ollama ведёт
        # KV-кэш сама), а лишний поток на каждый запрос — только мусор.
        if not _is_ollama_backend():
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
    filenames: list = None,
    org_id: str = None,
    use_hidden_layer: bool = True,
) -> dict:
    """
    org_id — сегмент пользователя. Пробрасывается и в поиск (локальная база),
    и в ключ кэша LLM. Если None — берётся из session_state автоматически.

    filenames — уточнение перечня НПА из диалога Советчика: ответ строится
    ТОЛЬКО по чанкам перечисленных документов. Пустой список и None
    равнозначны «фильтр не задан».

    use_hidden_layer — подмешивать ли служебный слой (см. комментарий вверху
    файла). Фрагменты слоя в result["sources"] НЕ попадают: в результате
    возвращается только их количество (result["hidden_used"]).
    """
    t_start   = time.perf_counter()
    config    = load_config()
    model     = model or config.get("default_model", "qwen/qwen3.5-9b")
    org_id    = org_id or get_current_org_id() or None
    namespace = namespace_for_org(org_id) if org_id else get_cache_namespace()

    filenames = [f for f in (filenames or []) if f] or None

    _, _, _internal_mode = _split_doc_types(doc_types)
 
    if not _llm_cache:
        load_llm_cache()
 
    print(f"\n{'='*55}\n[ASK] «{query[:70]}» | {model} | ns={namespace} | "
          f"internal={_internal_mode} | "
          f"файлов={len(filenames) if filenames else 'все'}\n{'='*55}")
 
    result = {
        "answer": "", "sources": [], "redirect": None,
        "redirect_reason": None, "from_faq": False,
        "from_cache": False, "model": model, "org_id": org_id,
        "hidden_used": 0, "internal_mode": _internal_mode,
        "filenames": filenames or [],
    }
 
    # FAQ
    # При активном уточнении перечня НПА FAQ пропускается: пользователь явно
    # потребовал ответ по конкретным документам, а готовый ответ из FAQ к ним
    # отношения не имеет и выглядел бы как игнорирование фильтра.
    if use_faq and not _internal_mode and not filenames:
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

    # Служебный слой — отдельным поиском, в sources не попадает
    hidden_sources = search_hidden_layer(query) if use_hidden_layer else []
    result["hidden_used"] = len(hidden_sources)
 
    # Гибридный поиск (BM25 + vector + reranking)
    sources = search_vector_db(
        query, top_k=top_k, spheres=spheres, doc_types=doc_types,
        doc_status=doc_status, filenames=filenames, org_id=org_id,
        hidden_sources=hidden_sources,
    )
    result["sources"] = sources
 
    if sources or _internal_mode:
        cache_key  = get_cache_key(query, sources, model, namespace=namespace,
                                   hidden_sources=hidden_sources,
                                   internal_mode=_internal_mode)
        was_cached = _cache_get(cache_key) is not None
        result["answer"]     = generate_ai_answer(
            query, sources, model, temperature, org_id=org_id,
            hidden_sources=hidden_sources, internal_mode=_internal_mode,
        )
        result["from_cache"] = was_cached
    elif filenames:
        # Отдельное сообщение: пустая выдача при активном фильтре почти всегда
        # означает не «нет ответа в базе», а «не в этих документах».
        result["answer"] = (
            f"❌ В выбранных документах ({len(filenames)} шт.) не найдено "
            "фрагментов по вашему вопросу. Расширьте перечень в уточнении "
            "или переформулируйте запрос."
        )
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