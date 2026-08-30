# core/landing_search.py
"""
Мини-RAG для строки поиска на главной странице (лендинге).

Задача этого модуля отличается от Советчика: здесь не нужен точный ответ
со ссылками на НПА, а нужен экспертный совет о том, ЧЕМ система может
помочь пользователю и КУДА в интерфейсе за этим идти. База маленькая
(десяток-два маркетинговых карточек по модулям и сценариям использования),
поэтому HybridRetriever (BM25 + RRF) намеренно не используется — он
оправдан на тысячах чанков tariff_docs, а на 15-20 документах чистый
векторный поиск + CrossEncoder-реранкер топ-N работает быстрее и не хуже
по качеству.

Инфраструктура переиспользуется из core/advisor.py: та же embedding-модель
(intfloat/multilingual-e5-large), тот же ChromaDB-клиент (через
core/indexer._get_chroma_client), тот же CrossEncoder-реранкер и тот же
нативный Ollama-стриминг с think:false. Коллекция — ОТДЕЛЬНАЯ
("landing_marketing"), чтобы не мешать содержимое лендинга с базой НПА.
"""

import os
import sys
import time
import threading
from datetime import datetime
from typing import List, Dict, Optional

# =============================================================================
# Константы
# =============================================================================
COLLECTION_NAME = "landing_marketing"

_DEFAULT_TOP_K        = 3   # сколько карточек попадает в контекст LLM после реранкинга
_DEFAULT_RETRIEVAL_N  = 10  # сколько кандидатов берём из вектора ДО реранкинга

# =============================================================================
# Промпты лендинга — редактируются в админке (раздел «Лендинг» на вкладке
# «Управление промптами»), хранятся в ОБЩЕМ config/prompts.json вместе с
# промптами Советчика/Анализатора/Прогнозиста (см. core/advisor.py::load_prompts).
# Ключи: "landing_system" (системный промпт) и "landing_user" (шаблон
# пользовательского сообщения, переменные {query} и {context}).
#
# DEFAULT_LANDING_PROMPTS — значения по умолчанию, используются пока
# админ ничего не сохранил, и как основа для кнопки «Сбросить к дефолтным»
# в админке. Держим их здесь, а не только в admin_panel.py, чтобы модуль
# оставался рабочим независимо от того, правили промпт или нет.
# =============================================================================
DEFAULT_LANDING_PROMPTS = {
    "landing_system": (
        "Ты — консультант по продукту РЕГУЛА.AI, ИИ-системе для тарифного "
        "регулирования РФ. Пользователь только что зашёл на главную страницу "
        "и написал в строку поиска, что ему нужно. Твоя задача — по описаниям "
        "модулей ниже понять, какой модуль (или модули) ему подходят, и дать "
        "короткую экспертную рекомендацию.\n\n"
        "Правила ответа:\n"
        "1. Обращайся на «вы», дружелюбно, но по делу — без канцелярита.\n"
        "2. Явно назови конкретный модуль (или модули) по имени, например "
        "«Советчик» или «Анализатор заявок».\n"
        "3. Коротко объясни, ПОЧЕМУ этот модуль решает задачу пользователя — "
        "опирайся только на факты из блоков «Модуль» ниже, ничего не выдумывай.\n"
        "4. Обязательно укажи, куда идти: «Откройте раздел «Название модуля» "
        "в меню слева».\n"
        "5. Если по вопросу подходит несколько модулей — перечисли их по "
        "порядку релевантности, но не больше двух-трёх.\n"
        "6. Если ни один модуль явно не подходит — честно скажи, что "
        "уточнить лучше в Советчике или через кнопку «Помощь», не выдумывай.\n"
        "7. Отвечай кратко: 3-6 предложений или короткий маркированный список. "
        "Без вводных фраз вроде «Отличный вопрос».\n"
    ),
    "landing_user": (
        "Запрос пользователя на главной странице: {query}\n\n"
        "Релевантные описания модулей:\n{context}\n\n"
        "Дай короткую экспертную рекомендацию по правилам выше."
    ),
    "landing_system_description": "Системный промпт умного поиска на главной странице.",
    "landing_user_description":   "Шаблон запроса. Переменные: {query}, {context}.",
}


def _load_landing_prompts() -> dict:
    """
    Читает landing_system/landing_user из общего config/prompts.json через
    load_prompts() из core/advisor.py (та же функция, что у Советчика) —
    единая точка правки промптов для всех модулей. Если ключи ещё не
    сохранялись через админку — используются DEFAULT_LANDING_PROMPTS.
    """
    from core.advisor import load_prompts
    prompts = load_prompts()
    return {
        "landing_system": prompts.get("landing_system", DEFAULT_LANDING_PROMPTS["landing_system"]),
        "landing_user":   prompts.get("landing_user",   DEFAULT_LANDING_PROMPTS["landing_user"]),
    }


# =============================================================================
# ChromaDB — своя коллекция, тот же клиент и embedding function что и в advisor
# =============================================================================
_landing_collection_lock = threading.Lock()
_landing_collection = None


def _get_embedding_function():
    """Переиспользует ту же E5-embedding function (совместимую обёртку),
    что и остальные коллекции проекта — иначе landing_marketing будет
    несовместима по векторному пространству, если ChromaDB подставит
    свою дефолтную ONNX-модель."""
    from core.expertise_chunker import get_chroma_embedding_function
    return get_chroma_embedding_function()


def get_landing_collection():
    """Синглтон коллекции landing_marketing (аналог get_chroma_collection в advisor.py)."""
    global _landing_collection
    with _landing_collection_lock:
        if _landing_collection is not None:
            return _landing_collection
        t0 = time.perf_counter()
        try:
            from core.indexer import _get_chroma_client
            client = _get_chroma_client()
            ef = _get_embedding_function()
            try:
                _landing_collection = client.get_collection(COLLECTION_NAME, embedding_function=ef)
            except Exception:
                _landing_collection = client.create_collection(COLLECTION_NAME, embedding_function=ef)
            print(f"[LANDING] Коллекция «{COLLECTION_NAME}» готова за "
                  f"{time.perf_counter()-t0:.2f} сек ({_landing_collection.count()} документов)")
            return _landing_collection
        except Exception as e:
            print(f"[LANDING ERROR] {e}")
            return None


def invalidate_landing_collection():
    global _landing_collection
    with _landing_collection_lock:
        _landing_collection = None
    print("[LANDING] Коллекция сброшена.")


# =============================================================================
# Поиск: вектор (top-N) → CrossEncoder реранкинг (top-K)
# =============================================================================
def search_landing_content(query: str, top_k: int = _DEFAULT_TOP_K,
                            retrieval_n: int = _DEFAULT_RETRIEVAL_N) -> List[Dict]:
    """
    Возвращает top_k наиболее релевантных карточек модулей/сценариев.
    Каждый элемент: {"snippet", "title", "module", "kind", "distance"}.
    """
    collection = get_landing_collection()
    if collection is None:
        return []

    if collection.count() == 0:
        print("[LANDING] Коллекция пуста — нужно запустить seed_landing_content().")
        return []

    from core.advisor import embed_query, get_reranker

    t0 = time.perf_counter()
    embedding = embed_query(query)

    try:
        if embedding is not None:
            results = collection.query(
                query_embeddings=embedding,
                n_results=min(retrieval_n, collection.count()),
                include=["documents", "metadatas", "distances"],
            )
        else:
            results = collection.query(
                query_texts=[query],
                n_results=min(retrieval_n, collection.count()),
                include=["documents", "metadatas", "distances"],
            )
    except Exception as e:
        print(f"[LANDING ERROR] Поиск не удался: {e}")
        return []

    if not results or not results.get("documents") or not results["documents"][0]:
        return []

    candidates = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        meta = meta or {}
        candidates.append({
            "doc":      doc,
            "meta":     meta,
            "distance": dist,
        })

    print(f"[LANDING] Векторный поиск: {len(candidates)} кандидатов за "
          f"{time.perf_counter()-t0:.3f} сек")

    # CrossEncoder реранкинг топ-N → топ-K (тот же реранкер, что у Советчика)
    reranker = get_reranker()
    if reranker and candidates:
        candidates = reranker.rerank(query, candidates, top_n=top_k)
    else:
        candidates = candidates[:top_k]

    sources = []
    for c in candidates:
        meta = c.get("meta", {})
        sources.append({
            "snippet":  c.get("doc", ""),
            "title":    meta.get("title", ""),
            "module":   meta.get("module", ""),
            "kind":     meta.get("kind", ""),
            "distance": round(c.get("distance", 0.0), 3) if "distance" in c else None,
        })
    return sources


def _build_context(sources: List[Dict]) -> str:
    if not sources:
        return "(релевантных описаний не найдено)"
    parts = []
    for i, s in enumerate(sources, 1):
        title = s.get("title") or s.get("module") or f"Фрагмент {i}"
        parts.append(f"### Модуль: {title}\n{s.get('snippet', '')}")
    return "\n\n".join(parts)


# =============================================================================
# Стриминг ответа — переиспользует нативный Ollama-путь из advisor.py
# =============================================================================
def stream_landing_answer(query: str, sources: List[Dict], model: str = None,
                           temperature: float = 0.3):
    """
    Генератор токенов для st.write_stream(). При недоступности нативного
    Ollama-стриминга — мягкий fallback на OpenAI-совместимый клиент advisor.
    """
    from core.advisor import (
        load_config, client, _is_ollama_backend, _ollama_chat_native_stream,
    )

    config     = load_config()
    model      = model or config.get("default_model", "qwen/qwen3.5-9b")
    max_tokens = min(config.get("max_tokens", 2048), 700)  # ответ короткий — не нужен полный бюджет
    timeout    = config.get("timeout_seconds", 300)

    context = _build_context(sources)
    _prompts = _load_landing_prompts()
    system_prompt = (
        _prompts["landing_system"]
        + "\n\nТекущая дата: " + datetime.now().strftime("%d.%m.%Y") + "."
    )
    try:
        user_content = _prompts["landing_user"].format(query=query, context=context)
    except (KeyError, IndexError):
        # Админ сохранил шаблон без {query}/{context} — не роняем генерацию,
        # откатываемся на дефолтный шаблон для этого конкретного вызова.
        print("[LANDING] landing_user не содержит {query}/{context} — используется дефолт")
        user_content = DEFAULT_LANDING_PROMPTS["landing_user"].format(query=query, context=context)

    is_qwen3   = "qwen3" in model.lower() or "qwen/qwen3" in model.lower()
    use_native = is_qwen3 and _is_ollama_backend()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]

    t0 = time.perf_counter()
    print(f"[LANDING LLM] {model} | native={use_native} | "
          f"промпт ~{len(system_prompt)+len(user_content)} симв.")

    try:
        if use_native:
            for chunk in _ollama_chat_native_stream(model, messages, temperature, max_tokens, timeout):
                yield chunk
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
                stream=True,
            )
            if extra_body:
                kwargs["extra_body"] = extra_body
            stream = client.chat.completions.create(**kwargs)
            for part in stream:
                delta = part.choices[0].delta.content or ""
                if delta:
                    yield delta

        print(f"[LANDING LLM] готово за {time.perf_counter()-t0:.2f} сек")

    except Exception as e:
        err = str(e)
        if "Connection" in err or "refused" in err:
            yield "\n🔌 Не удалось подключиться к LLM. Попробуйте позже или откройте Советчика."
        elif "timeout" in err.lower():
            yield f"\n⏱️ Таймаут ({timeout} сек)."
        else:
            yield f"\n❌ Ошибка: {err}"


def ask_landing(query: str, model: str = None, temperature: float = 0.3,
                 top_k: int = _DEFAULT_TOP_K):
    """Удобный неструктурированный вход: ищет + возвращает генератор ответа.
    Возвращает (sources, generator)."""
    sources = search_landing_content(query, top_k=top_k)
    return sources, stream_landing_answer(query, sources, model=model, temperature=temperature)


# =============================================================================
# Сидинг контента — маркетинговые карточки модулей и сценариев использования
# =============================================================================
def seed_landing_content(force: bool = False) -> dict:
    """
    Индексирует стартовый набор маркетинговых карточек в коллекцию
    landing_marketing. Идемпотентно: если коллекция не пуста и force=False —
    ничего не делает. Для обновления контента — force=True (пересоздаёт всё).

    Каждая карточка — либо описание модуля («module»), либо готовый сценарий
    использования / юзкейс («usecase»). LLM получает snippet как есть, поэтому
    тексты написаны развёрнуто и «по-человечески», а не сухими тезисами.
    """
    collection = get_landing_collection()
    if collection is None:
        return {"status": "error", "message": "Не удалось получить коллекцию"}

    if collection.count() > 0 and not force:
        return {"status": "skipped", "message": f"Коллекция уже содержит {collection.count()} документов"}

    if force and collection.count() > 0:
        all_ids = collection.get(include=[])["ids"]
        if all_ids:
            collection.delete(ids=all_ids)

    cards = _LANDING_CARDS

    ids, documents, metadatas = [], [], []
    for i, card in enumerate(cards):
        ids.append(f"landing__{i}__{card['module_key']}")
        documents.append(card["text"])
        metadatas.append({
            "title":  card["title"],
            "module": card["module"],
            "kind":   card["kind"],
            "indexed_at": datetime.now().isoformat(),
        })

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    print(f"[LANDING] Проиндексировано {len(ids)} карточек в «{COLLECTION_NAME}»")
    return {"status": "success", "count": len(ids)}


# Контент карточек. module_key — стабильный идентификатор для id документа.
# module — точное имя раздела в меню (используется в подсказке «куда идти»).
_LANDING_CARDS = [
    # ── Модуль: Советчик ────────────────────────────────────────────────
    {
        "module_key": "advisor",
        "module": "Советчик",
        "title": "Советчик — консультации по нормативной базе",
        "kind": "module",
        "text": (
            "Советчик — это ИИ-эксперт по тарифному регулированию, который отвечает "
            "на любой вопрос по нормативной базе (НПА) со ссылками на конкретные "
            "документы и статьи. Специалист вводит вопрос обычным языком — "
            "например, «можно ли включать расходы на ДМС в тариф при превышении "
            "6% от ФОТ» — и получает развёрнутый ответ с цитатами из актуальных "
            "приказов, постановлений и методических указаний, без необходимости "
            "искать документ вручную. Советчик снижает нагрузку на специалистов "
            "тарифного отдела примерно на 30%, потому что заменяет часы поиска "
            "по разрозненным НПА одним быстрым запросом. Ссылается только на "
            "актуальные, не утратившие силу редакции документов. Подходит, когда "
            "нужно быстро разобраться в правовом основании для конкретной статьи "
            "затрат, проверить правомерность решения или подготовиться к спору "
            "с регулятором."
        ),
    },
    {
        "module_key": "advisor_usecase",
        "module": "Советчик",
        "title": "Сценарий: спорная статья затрат, нужна правовая опора",
        "kind": "usecase",
        "text": (
            "Если у специалиста есть конкретная статья затрат, включение которой "
            "в тариф выглядит спорным, и нужно быстро найти нормативное основание "
            "«за» или «против» — это прямая задача для Советчика. Достаточно "
            "описать ситуацию словами, и система найдёт релевантные пункты НПА, "
            "процитирует их и объяснит, как они применяются к описанной ситуации."
        ),
    },

    # ── Модуль: AI-Сканер документов ────────────────────────────────────
    {
        "module_key": "doc_scanner",
        "module": "Сканер документов",
        "title": "Сканер документов — распознавание и база знаний из своих файлов",
        "kind": "module",
        "text": (
            "Сканер документов распознаёт текст из PDF, DOCX и сканированных "
            "изображений (через OCR) и превращает загруженные файлы в личную "
            "базу знаний с полнотекстовым поиском и пересказом. Это удобно, "
            "когда нужно быстро разобраться в большом объёме собственных "
            "документов — заявок, актов, переписки с регулятором — без ручного "
            "чтения каждой страницы. Модуль поддерживает как чистый текст, так "
            "и сканы низкого качества. После загрузки документы можно "
            "спрашивать так же, как Советчика: «о чём этот файл» или «найди "
            "пункт про амортизацию» — и получить точную цитату с указанием "
            "страницы."
        ),
    },
    {
        "module_key": "doc_scanner_usecase",
        "module": "Сканер документов",
        "title": "Сценарий: большая пачка сканов, нужно быстро понять содержание",
        "kind": "usecase",
        "text": (
            "Если пришла пачка отсканированных документов — например, старая "
            "переписка или бумажные акты — и нужно быстро понять, что в них "
            "написано, не читая каждую страницу вручную, стоит загрузить их в "
            "Сканер документов. Он распознает текст, даст краткий пересказ по "
            "каждому файлу и позволит искать по содержимому обычным вопросом."
        ),
    },

    # ── Модуль: Анализатор заявок ───────────────────────────────────────
    {
        "module_key": "claim_analyzer",
        "module": "Анализатор заявок",
        "title": "Анализатор заявок — риски и комплектность тарифной заявки",
        "kind": "module",
        "text": (
            "Анализатор заявок проверяет комплектность тарифной заявки перед "
            "подачей регулятору и подсвечивает риски по каждой статье затрат "
            "отдельно: где недостаточно обоснования, где формулировка похожа "
            "на уже отклонённые регулятором позиции, где не хватает "
            "подтверждающих документов. Цель модуля — повысить проходимость "
            "заявки с первого раза, ещё до того как её увидит регулятор, и "
            "избежать долгой переписки с доработками. Особенно полезен на "
            "финальном этапе подготовки заявки, когда важно перепроверить "
            "каждую статью затрат перед отправкой."
        ),
    },
    {
        "module_key": "claim_analyzer_usecase",
        "module": "Анализатор заявок",
        "title": "Сценарий: заявка готова, нужна финальная проверка перед подачей",
        "kind": "usecase",
        "text": (
            "Если тарифная заявка почти готова к отправке и хочется заранее "
            "понять, какие статьи затрат регулятор может отклонить или "
            "запросить по ним дополнительное обоснование — стоит прогнать "
            "заявку через Анализатор заявок. Он покажет риски по каждой статье "
            "и даст ориентир, что стоит усилить до подачи, а не после первого "
            "отказа."
        ),
    },

    # ── Модуль: Прогнозист решений ──────────────────────────────────────
    {
        "module_key": "predictor",
        "module": "Прогноз решения регулятора",
        "title": "Прогнозист решений — вероятность одобрения на основе прецедентов",
        "kind": "module",
        "text": (
            "Прогнозист решений оценивает вероятность одобрения конкретной "
            "позиции заявки, опираясь на базу из тысяч реальных решений "
            "региональных энергетических комиссий (РЭК) по похожим статьям "
            "затрат, методам и регионам. Вместо догадок «одобрят или нет» "
            "специалист получает прецедентную оценку: как регуляторы решали "
            "похожие вопросы раньше. Это снижает риск отклонения статей "
            "затрат, потому что позволяет заранее скорректировать позицию "
            "под то, что реально проходит у регулятора, а не только под "
            "формальные требования методики."
        ),
    },
    {
        "module_key": "predictor_usecase",
        "module": "Прогноз решения регулятора",
        "title": "Сценарий: неуверенность, одобрит ли регулятор конкретную позицию",
        "kind": "usecase",
        "text": (
            "Если есть сомнения, как регулятор отнесётся к конкретной "
            "формулировке или сумме по статье затрат, и хочется опереться не "
            "на интуицию, а на прецеденты — это задача для Прогнозиста "
            "решений. Он покажет, как похожие случаи решались в реальных "
            "протоколах РЭК, и даст оценку вероятности одобрения."
        ),
    },

    # ── Модуль: Протокольщик ────────────────────────────────────────────
    {
        "module_key": "protocol_bot",
        "module": "Протокольщик",
        "title": "Протокольщик — автоматические протоколы заседаний",
        "kind": "module",
        "text": (
            "Протокольщик составляет официальные протоколы заседаний "
            "автоматически — из аудиозаписи встречи или из текстового "
            "конспекта. Модуль сам структурирует и форматирует содержание по "
            "нужным разделам: повестка, участники, решения, поручения — и "
            "сокращает время подготовки протокола в разы по сравнению с "
            "ручным набором. Полезен сразу после совещаний с регулятором или "
            "внутренних рабочих встреч, когда нужен формальный документ "
            "быстро и без ошибок в структуре."
        ),
    },
    {
        "module_key": "protocol_bot_usecase",
        "module": "Протокольщик",
        "title": "Сценарий: только что закончилось совещание, нужен протокол",
        "kind": "usecase",
        "text": (
            "Если совещание с регулятором или внутренняя рабочая встреча "
            "только что закончились, и нужно быстро оформить официальный "
            "протокол — загрузите аудиозапись или текстовый конспект в "
            "Протокольщик. Он сам разложит содержание по нужным разделам и "
            "избавит от ручного форматирования."
        ),
    },

    # ── Модуль: Задачник ────────────────────────────────────────────────
    {
        "module_key": "tasks",
        "module": "Задачи",
        "title": "Задачник — короткие задачи, связанные со всеми модулями",
        "kind": "module",
        "text": (
            "Задачник — это простой трекер коротких заметок (до 500 символов) "
            "о том, что нужно запросить или проверить в системе. У каждой "
            "задачи есть статус, приоритет и срок исполнения с цветовой "
            "подсветкой, поэтому важные дедлайны видно сразу. Задачу можно "
            "быстро загрузить прямо в Советчик как готовый вопрос — это "
            "убирает разрыв между «я вспомнил, что надо проверить» и "
            "«получил ответ». Удобен как связующее звено между всеми "
            "остальными модулями, когда в процессе работы накапливаются "
            "мелкие пункты, которые легко забыть."
        ),
    },

    # ── Общее / навигация по продукту ───────────────────────────────────
    {
        "module_key": "overview",
        "module": "Советчик",
        "title": "РЕГУЛА.AI — что за система и с чего начать",
        "kind": "module",
        "text": (
            "РЕГУЛА.AI — ИИ-система для автоматизации работы с тарифным "
            "регулированием в России. Она объединяет шесть модулей: "
            "Советчик (вопросы по НПА), Сканер документов (распознавание и "
            "поиск по своим файлам), Анализатор заявок (проверка "
            "комплектности и рисков перед подачей), Прогнозист решений "
            "(вероятность одобрения на основе прецедентов РЭК), Протокольщик "
            "(автоматические протоколы встреч) и Задачник (короткие рабочие "
            "заметки). Если не уверены, с какого модуля начать — опишите "
            "задачу своими словами в строке поиска, и система подскажет "
            "подходящий раздел. Для произвольных вопросов по нормативной "
            "базе всегда можно напрямую открыть Советчика в меню слева."
        ),
    },
]


if __name__ == "__main__":
    # Ручной запуск сидинга: python -m core.landing_search
    # python -m core.landing_search --force — принудительно пересоздать карточки
    result = seed_landing_content(force="--force" in sys.argv)
    print(result)