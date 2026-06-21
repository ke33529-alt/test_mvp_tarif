"""
core/expertise_chunker.py

Чанкер для экспертных заключений (doc_type="expertise").
ПОЛНОСТЬЮ ИЗОЛИРОВАН от существующего протокольного пайплайна
(core/indexer.py, режим "protocol_articles", коллекция "protocols") —
ничего оттуда не импортирует и не переиспользует.

Источник: .txt-файлы, сгенерированные LLM-обработкой сканов экспертных
заключений/протоколов, со структурой:

    ======================================================================
    РЕКВИЗИТЫ ДОКУМЕНТА
    ======================================================================
    Регион:              ...
    Сфера ЖКХ:           ...
    Организация:         ...
    Год:                 ...
    Метод регулирования: ...
    Номер протокола:     ...
    Дата протокола:      ...
    Исходный файл:       ...
    Обработан:           ...
    ======================================================================

    ПОДРОБНЫЙ ПЕРЕСКАЗ
    ----------------------------------------------------------------------
    ### 1 РЕКВИЗИТЫ ДОКУМЕНТА
    <текст>

    ### 2 МЕТОД И НОРМАТИВНАЯ БАЗА
    <текст>
    ...

    ======================================================================
    РАСПОЗНАННЫЙ ТЕКСТ (OCR)
    ----------------------------------------------------------------------
    <сырой OCR-текст>

ВАЖНО: структура сгенерирована LLM и может «плыть» — количество разделов,
их номера и точные названия не стабильны между файлами. Парсер построен
на гибких эвристиках, а не на жёстких позициях/номерах.

Чанкование:
- Источник — блок «ПОДРОБНЫЙ ПЕРЕСКАЗ», разбитый на секции по заголовкам
  "### N[.M] ..." (включая под-разделы вида 5.1, 5.2 — каждый отдельным
  чанком, ничего не объединяется).
- Заголовок секции может содержать "[tag: xxx]" — тег выносится в
  metadata.tag и убирается из текста чанка.
- Пустые/неинформативные разделы («В документе отсутствуют...» и т.п.)
  пропускаются (не индексируются) — гибкая эвристика по эвристическим
  фразам в начале текста секции.
- Раздел про тарифы (название содержит "ТАРИФ") индексируется отдельным
  цельным чанком, без дробления, независимо от его номера.
- Блок «РАСПОЗНАННЫЙ ТЕКСТ (OCR)» индексируется отдельным fallback-чанком
  (block_kind="ocr_raw"), без секционирования.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


# ──────────────────────────────────────────────────────────────────────────
# Константы
# ──────────────────────────────────────────────────────────────────────────

COLLECTION_NAME = "expertise_docs"

SECTION_HEADER_RE = re.compile(
    r'^###\s+(?P<num>\d+(?:\.\d+)?)\s*'
    r'(?:"(?P<name_q>[^"]*)"|(?P<name_plain>[^\[\r\n]*))'
    r'\s*(?:\[tag:\s*(?P<tag>[^\]]*)\])?\s*$',
    re.MULTILINE,
)

PERESKAZ_MARKER = "ПОДРОБНЫЙ ПЕРЕСКАЗ"
OCR_MARKER = "РАСПОЗНАННЫЙ ТЕКСТ (OCR)"
SECTION_DIVIDER_RE = re.compile(r'={10,}')

TARIFF_NAME_HINT = "тариф"  # case-insensitive substring match in section name

# Эвристика "пустого" раздела: текст начинается (после небольшого зазора)
# с одной из этих конструкций. Список открыт — не привязан к конкретной
# формулировке, охватывает варианты "В документе/постановлении/источнике/
# предоставленных данных/выдержках отсутствует(-ют)..." и аналоги "не
# указан(о/ы)"/"не определен(о/а)".
EMPTY_SECTION_RE = re.compile(
    r'^\s*В\s+(документе|постановлении|источнике|предоставленных\s+данных|'
    r'выдержках(\s+документа)?|тексте(\s+постановления)?|приложени\w*)\s+'
    r'отсутств\w*',
    re.IGNORECASE,
)
EMPTY_SECTION_FALLBACK_RE = re.compile(
    r'^\s*(не\s+указан\w*|не\s+определен\w*|не\s+приведен\w*)\b',
    re.IGNORECASE,
)
EMPTY_SECTION_MAX_LEN = 600  # если раздел длиннее — даже с "отсутствует" в начале считаем содержательным (вдруг там ещё что-то по делу)


# ──────────────────────────────────────────────────────────────────────────
# Структуры данных
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class ExpertiseChunk:
    text: str
    section: str            # человекочитаемое название раздела
    article_num: str        # "5", "5.1", "ocr" и т.п.
    tag: str | None
    block_kind: str          # "section" | "ocr_raw"


@dataclass
class ExpertiseDocAttrs:
    region: str = "регион_не_определён"
    sphere: str = "сфера_не_определена"
    year: str = "год_не_определён"
    method: str = "метод_не_определён"
    organization: str = "организация_не_определена"
    protocol_num: str = "номер_не_определён"
    protocol_date: str = ""
    doc_type: str = "expertise"


# ──────────────────────────────────────────────────────────────────────────
# Парсинг блока РЕКВИЗИТЫ ДОКУМЕНТА (для метаданных, если нужно свериться
# с атрибутами из имени файла — на крайний случай используется как fallback)
# ──────────────────────────────────────────────────────────────────────────

REKVIZITY_FIELD_RE = re.compile(r'^([А-Яа-яёЁ ]+?):\s*(.*)$', re.MULTILINE)


def parse_rekvizity_block(text: str) -> dict:
    """
    Парсит блок «РЕКВИЗИТЫ ДОКУМЕНТА» в начале файла (до первого "ПОДРОБНЫЙ
    ПЕРЕСКАЗ"). Возвращает словарь с сырыми ключами на русском — используется
    как дополнительный (не основной) источник атрибутов.
    """
    head = text.split(PERESKAZ_MARKER, 1)[0]
    out = {}
    for m in REKVIZITY_FIELD_RE.finditer(head):
        key = m.group(1).strip()
        val = m.group(2).strip()
        out[key] = val
    return out


# ──────────────────────────────────────────────────────────────────────────
# Разбор основного текста на блоки ПЕРЕСКАЗ / OCR
# ──────────────────────────────────────────────────────────────────────────

def _split_pereskaz_and_ocr(text: str) -> tuple[str, str]:
    """
    Возвращает (pereskaz_text, ocr_text). Если какой-то из блоков
    отсутствует — возвращает пустую строку для него.
    """
    pereskaz = ""
    ocr = ""

    idx_pereskaz = text.find(PERESKAZ_MARKER)
    idx_ocr = text.find(OCR_MARKER)

    if idx_pereskaz != -1:
        start = idx_pereskaz + len(PERESKAZ_MARKER)
        end = idx_ocr if idx_ocr != -1 else len(text)
        pereskaz = text[start:end]
        # убираем ведущую разделительную линию "----..."
        pereskaz = re.sub(r'^\s*-{5,}\s*\r?\n', '', pereskaz)

    if idx_ocr != -1:
        start = idx_ocr + len(OCR_MARKER)
        ocr = text[start:]
        ocr = re.sub(r'^\s*-{5,}\s*\r?\n', '', ocr)

    return pereskaz.strip(), ocr.strip()


# ──────────────────────────────────────────────────────────────────────────
# Разбор блока ПЕРЕСКАЗ на секции
# ──────────────────────────────────────────────────────────────────────────

def _split_into_sections(pereskaz_text: str) -> list[dict]:
    """
    Возвращает список {num, name, tag, body} в порядке появления.
    Каждый заголовок "### N[.M] ..." начинает новую секцию; всё до
    следующего заголовка (или до разделительной линии "===...", которая
    означает конец блока пересказа) — тело секции.
    """
    matches = list(SECTION_HEADER_RE.finditer(pereskaz_text))
    sections = []
    for i, m in enumerate(matches):
        num = m.group("num")
        name = (m.group("name_q") or m.group("name_plain") or "").strip()
        tag = m.group("tag")
        tag = tag.strip() if tag else None

        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(pereskaz_text)
        body = pereskaz_text[body_start:body_end]

        # обрезаем тело на случай если внутри затесалась разделительная линия
        div_match = SECTION_DIVIDER_RE.search(body)
        if div_match:
            body = body[:div_match.start()]

        body = body.strip()
        if not body:
            continue

        sections.append({
            "num": num,
            "name": name,
            "tag": tag,
            "body": body,
        })
    return sections


# ──────────────────────────────────────────────────────────────────────────
# Фильтр пустых разделов
# ──────────────────────────────────────────────────────────────────────────

def is_empty_section(body: str) -> bool:
    """
    Гибкая эвристика: раздел считается «пустым» (неинформативным), если он
    короткий и начинается (после небольшого зазора) с шаблонной фразы об
    отсутствии данных. Длинные разделы не отбрасываются целиком даже если
    содержат такую фразу в начале — велик шанс, что дальше есть полезная
    информация (частично заполненные разделы у LLM-обработки попадаются).

    Важно: фраза вида "Не указано отдельно" внутри одной строки
    структурированного блока (• Заявлено: Не указано отдельно. • Принято:
    2,59 тыс. руб. ...) НЕ должна считаться маркером пустого раздела — там
    есть содержательные цифры дальше. Поэтому fallback-критерий применяется
    только если "не указан/не определен" встречается в самом начале текста
    (а не где-то в середине строки после маркера "•") И раздел в целом
    короткий.
    """
    snippet = body.strip()
    if not snippet:
        return True

    if len(snippet) <= EMPTY_SECTION_MAX_LEN:
        first_chunk = snippet[:300]
        if EMPTY_SECTION_RE.match(first_chunk):
            return True
        # Запасной критерий: текст начинается непосредственно с "не указано/
        # не определено" (без маркеров списка вроде "•" перед ним) —
        # отличает "Не указаны данные о..." (пустой раздел) от
        # "• Заявлено: Не указано отдельно. • Принято: 2,59 тыс. руб." (это
        # содержательная структура с реальными цифрами далее).
        first_line = snippet.split("\n")[0].strip()
        if EMPTY_SECTION_FALLBACK_RE.match(first_line) and len(first_line) < 150:
            return True

    return False


def is_tariff_section(name: str) -> bool:
    return TARIFF_NAME_HINT in name.lower()


# ──────────────────────────────────────────────────────────────────────────
# Основная функция чанкования
# ──────────────────────────────────────────────────────────────────────────

def chunk_expertise_text(raw_text: str) -> list[ExpertiseChunk]:
    """
    Принимает полный текст .txt-файла экспертного заключения/протокола и
    возвращает список чанков (ещё без привязки к doc-level metadata —
    регион/сфера/год/метод добавляются на уровне index_expertise_file).
    """
    pereskaz_text, ocr_text = _split_pereskaz_and_ocr(raw_text)

    chunks: list[ExpertiseChunk] = []

    sections = _split_into_sections(pereskaz_text)
    for sec in sections:
        body = sec["body"]

        # Таблица тарифов — отдельный цельный чанк, не дробится, и не
        # фильтруется по правилу "пустой раздел" (она по определению не
        # пустая, если есть).
        if is_tariff_section(sec["name"]):
            chunks.append(ExpertiseChunk(
                text=body,
                section=sec["name"] or f"Раздел {sec['num']}",
                article_num=sec["num"],
                tag=sec["tag"],
                block_kind="section",
            ))
            continue

        if is_empty_section(body):
            continue

        chunks.append(ExpertiseChunk(
            text=body,
            section=sec["name"] or f"Раздел {sec['num']}",
            article_num=sec["num"],
            tag=sec["tag"],
            block_kind="section",
        ))

    # OCR fallback — отдельный нерасчленённый чанк
    if ocr_text:
        chunks.append(ExpertiseChunk(
            text=ocr_text,
            section="Распознанный текст (OCR)",
            article_num="ocr",
            tag=None,
            block_kind="ocr_raw",
        ))

    return chunks


# ──────────────────────────────────────────────────────────────────────────
# Индексация в ChromaDB (коллекция expertise_docs)
# ──────────────────────────────────────────────────────────────────────────

def get_chroma_embedding_function():
    """
    Возвращает embedding_function из core/indexer.py, обёрнутую для
    совместимости с разными версиями ChromaDB.

    ChromaDB >= 0.6 требует, чтобы embedding_function реализовывала метод
    .name() (часть протокола EmbeddingFunction). E5EmbeddingFunction в
    core/indexer.py написана под более старый стиль (просто __call__) —
    это нормально работает на старых версиях chromadb (которые, по всей
    видимости, используются в проде), но падает на новых с
    AttributeError. Оборачиваем без изменения core/indexer.py.
    """
    from core.indexer import get_embedding_function

    base_ef = get_embedding_function()

    if hasattr(base_ef, "name"):
        return base_ef

    class _CompatWrapper:
        def __init__(self, inner):
            self._inner = inner

        def __call__(self, input):
            return self._inner(input)

        def name(self):
            return "e5_multilingual_compat"

        def embed_documents(self, input):
            # Используется новым API ChromaDB при collection.add()
            return self._inner(input)

        def embed_query(self, input):
            # Используется новым API ChromaDB при collection.query() —
            # без этого метода query() падает с AttributeError на
            # ChromaDB >= 0.6, даже если __call__ присутствует.
            if isinstance(input, str):
                input = [input]
            return self._inner(input)

        @staticmethod
        def is_legacy():
            return True

    return _CompatWrapper(base_ef)


def index_expertise_file(fpath: str, doc_attrs: ExpertiseDocAttrs) -> dict:
    """
    Читает файл по fpath, чанкует, индексирует в коллекцию expertise_docs.
    Возвращает {chunks, collection, status, error_msg, indexed_at}.

    Эта функция — единственная точка входа для UI (заменяет заглушку
    index_expertise_file из streamlit_pages/expertise_panel.py).

    ВАЖНО про embedding: переиспользует тот же синглтон ChromaDB-клиента
    и ту же embedding function (E5EmbeddingFunction, intfloat/multilingual-
    e5-large, с префиксом "passage: ") из core/indexer.py — иначе
    коллекция expertise_docs была бы несовместима по векторному
    пространству с tariff_docs/protocols (Chroma иначе подставляет свою
    дефолтную ONNX MiniLM-модель). Сам индексер НЕ импортируется и не
    вызывается для логики чанкования — только клиент и embedding function.
    """
    from datetime import datetime
    import uuid

    fname = os.path.basename(fpath)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        with open(fpath, "rb") as f:
            raw_bytes = f.read()
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raw_text = raw_bytes.decode("cp1251", errors="replace")
    except Exception as e:
        return {
            "chunks": 0, "collection": COLLECTION_NAME,
            "status": "error", "error_msg": f"Не удалось прочитать файл: {e}",
            "indexed_at": now,
        }

    chunks = chunk_expertise_text(raw_text)

    if not chunks:
        return {
            "chunks": 0, "collection": COLLECTION_NAME,
            "status": "error",
            "error_msg": "Не найдено ни одной секции для индексации (проверьте формат файла)",
            "indexed_at": now,
        }

    try:
        # Переиспользуем синглтон-клиента и embedding function из
        # core/indexer.py — критично для совместимости векторного
        # пространства с остальными коллекциями (tariff_docs, protocols).
        from core.indexer import _get_chroma_client

        client = _get_chroma_client()
        ef = get_chroma_embedding_function()
        collection = client.get_or_create_collection(name=COLLECTION_NAME, embedding_function=ef)

        # Удаляем старые чанки этого файла (на случай переиндексации)
        try:
            collection.delete(where={"filename": fname})
        except Exception:
            pass

        ids, documents, metadatas = [], [], []
        for i, ch in enumerate(chunks):
            ids.append(f"{fname}__{i}__{uuid.uuid4().hex[:8]}")
            documents.append(ch.text)
            metadatas.append({
                "filename": fname,
                "doc_type": doc_attrs.doc_type,
                "region": doc_attrs.region,
                "sphere": doc_attrs.sphere,
                "year": doc_attrs.year,
                "method": doc_attrs.method,
                "organization": doc_attrs.organization,
                "protocol_num": doc_attrs.protocol_num,
                "protocol_date": doc_attrs.protocol_date,
                "section": ch.section,
                "article_num": ch.article_num,
                "tag": ch.tag or "",
                "block_kind": ch.block_kind,
                "chunk_index": i,
                "indexed_at": now,
            })

        collection.add(ids=ids, documents=documents, metadatas=metadatas)

        return {
            "chunks": len(chunks),
            "collection": COLLECTION_NAME,
            "status": "ok",
            "error_msg": None,
            "indexed_at": now,
        }
    except Exception as e:
        return {
            "chunks": 0, "collection": COLLECTION_NAME,
            "status": "error", "error_msg": str(e),
            "indexed_at": now,
        }


def remove_file_from_expertise(fname: str) -> bool:
    """Удаляет все чанки файла из коллекции expertise_docs."""
    try:
        from core.indexer import _get_chroma_client
        client = _get_chroma_client()
        ef = get_chroma_embedding_function()
        collection = client.get_collection(name=COLLECTION_NAME, embedding_function=ef)
        collection.delete(where={"filename": fname})
        return True
    except Exception:
        return False
