"""
index_docs.py — v3
Главные улучшения:
  1. Авто-определение кодировки (CP1251/UTF-8) без chardet
  2. Очистка текста: схлопывание пустых строк из таблиц-форм
  3. Умная фильтрация мусорных чанков
  4. Нарезка по абзацам (пустая строка = граница)
  5. Заголовки пунктов сохраняются вместе с содержимым
"""

import chromadb
import os
import re
from datetime import datetime

DB_PATH  = "data/vector_db"
RAW_PATH = "data/raw"
CHUNK_SIZE    = 1200
CHUNK_OVERLAP = 200


# =============================================================================
# Кодировка — без chardet
# =============================================================================
def detect_encoding(path):
    with open(path, 'rb') as f:
        raw = f.read(30000)
    for enc in ('utf-8-sig', 'utf-8', 'cp1251', 'latin-1'):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return 'latin-1'


def read_file(path):
    enc = detect_encoding(path)
    with open(path, 'r', encoding=enc, errors='replace') as f:
        return f.read(), enc


# =============================================================================
# Очистка текста НПА
# =============================================================================
def clean_text(text):
    """
    Убирает типичный мусор из выгрузок КонсультантПлюс:
    - колонтитулы www.consultant.ru
    - строки "Дата сохранения:"
    - серии пустых строк из таблиц-форм (>2 подряд -> 2)
    - строки из одних пробелов/табуляций
    """
    lines = text.splitlines()
    cleaned = []
    prev_empty = 0
    for line in lines:
        stripped = line.strip()
        # Пропускаем служебные строки
        if any(s in stripped for s in ['www.consultant.ru', 'КонсультантПлюс',
                                        'Дата сохранения:', 'Документ предоставлен']):
            continue
        if not stripped:
            prev_empty += 1
            if prev_empty <= 2:
                cleaned.append('')
        else:
            prev_empty = 0
            cleaned.append(stripped)
    return '\n'.join(cleaned)


# =============================================================================
# Оценка качества чанка
# =============================================================================
def chunk_quality_score(text):
    """
    Возвращает 0..1. Чанки ниже порога отфильтровываются.
    Низкое качество: строки таблиц с пустыми ячейками, чанки из одних цифр/единиц.
    """
    if not text or len(text.strip()) < 40:
        return 0.0

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0

    total = len(lines)

    # Считаем "мусорные" строки: только цифра, только "тыс. руб.", пустые значения
    junk_patterns = re.compile(
        r'^(\d+[\d\.,]*'           # только число
        r'|тыс[\.\s]*руб[\.\s]*'  # единица измерения
        r'|руб[\.\s]*'
        r'|%)$',
        re.IGNORECASE
    )
    junk = sum(1 for l in lines if junk_patterns.match(l))
    junk_ratio = junk / total

    # Считаем строки с реальным текстом (>= 20 символов)
    meaningful = sum(1 for l in lines if len(l) >= 20)
    meaningful_ratio = meaningful / total

    # Итоговый скор
    score = meaningful_ratio - junk_ratio
    return max(0.0, min(1.0, score))


# =============================================================================
# Нарезка на чанки по абзацам
# =============================================================================
def extract_article_ref(text):
    """Ищет в начале чанка ссылку на статью/пункт."""
    m = re.match(
        r'^((?:Статья|Пункт|п\.)\s*\d+[\d\.]*'
        r'|\d+[\d\.]+\.?\s)',
        text.strip(), re.IGNORECASE
    )
    return m.group(0).strip() if m else ''


def chunk_by_paragraphs(text, doc_id, base_meta):
    """
    Делит текст на абзацы (разделитель — пустая строка).
    Склеивает короткие абзацы до CHUNK_SIZE.
    Добавляет перекрытие: последний абзац предыдущего чанка.
    """
    paragraphs = re.split(r'\n{2,}', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks = []
    current_parts = []
    current_len   = 0
    chunk_idx     = 0
    last_para     = ''   # для перекрытия

    def flush():
        nonlocal chunk_idx, last_para
        if not current_parts:
            return
        body = '\n\n'.join(current_parts)
        score = chunk_quality_score(body)
        if score >= 0.25:   # фильтр мусора
            article = extract_article_ref(current_parts[0])
            chunks.append({
                'text': body,
                'metadata': {
                    **base_meta,
                    'doc_id': doc_id,
                    'chunk_index': chunk_idx,
                    'article': article,
                    'quality': round(score, 2),
                }
            })
            chunk_idx += 1
        last_para = current_parts[-1] if current_parts else ''

    for para in paragraphs:
        # Слишком длинный абзац — нарежем по предложениям
        if len(para) > CHUNK_SIZE:
            flush()
            current_parts = [last_para] if last_para else []
            current_len   = len(last_para)

            sentences = re.split(r'(?<=[.!?])\s+', para)
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                if current_len + len(sent) > CHUNK_SIZE and current_parts:
                    flush()
                    current_parts = [last_para] if last_para else []
                    current_len   = len(last_para)
                current_parts.append(sent)
                current_len += len(sent) + 1
            continue

        if current_len + len(para) > CHUNK_SIZE and current_parts:
            flush()
            # Перекрытие — берём последний абзац прошлого чанка
            current_parts = [last_para] if last_para else []
            current_len   = len(last_para)

        current_parts.append(para)
        current_len += len(para) + 2

    flush()
    return chunks


# =============================================================================
# Определение типа документа
# =============================================================================
def doc_type(filename):
    fn = filename.lower()
    if any(k in fn for k in ['постановление', 'приказ', 'фз', 'распоряжение']):
        return 'npa'
    if any(k in fn for k in ['фас', 'предписание']):
        return 'fas'
    if any(k in fn for k in ['суд', 'арбитраж', 'апелляц']):
        return 'court'
    if any(k in fn for k in ['методич', 'разъясн', 'письмо']):
        return 'methodics'
    return 'unknown'


# =============================================================================
# Основная функция
# =============================================================================
def index_documents():
    print("Инициализация ChromaDB...")
    client = chromadb.PersistentClient(path=DB_PATH)

    # Пересоздаём коллекцию для чистой переиндексации
    try:
        client.delete_collection("tariff_docs")
        print("Старая коллекция удалена")
    except Exception:
        pass

    collection = client.create_collection(
        name="tariff_docs",
        metadata={"hnsw:space": "cosine"},
    )
    print("Коллекция создана")

    if not os.path.exists(RAW_PATH):
        print(f"Папка {RAW_PATH} не найдена!")
        return

    total_added = 0
    total_filtered = 0

    for category in os.listdir(RAW_PATH):
        cat_path = os.path.join(RAW_PATH, category)
        if not os.path.isdir(cat_path):
            continue
        print(f"\n[{category}]")

        for filename in sorted(os.listdir(cat_path)):
            fpath = os.path.join(cat_path, filename)
            if (not os.path.isfile(fpath)
                    or filename.endswith('.indexed')
                    or filename.startswith('.')):
                continue

            try:
                raw_text, enc = read_file(fpath)
                if len(raw_text.strip()) < 100:
                    continue

                text = clean_text(raw_text)
                doc_id = f"{category}_{os.path.splitext(filename)[0]}"

                base_meta = {
                    'filename': filename,
                    'category': category,
                    'doc_type': doc_type(filename),
                    'doc_id':   doc_id,
                    'encoding': enc,
                    'indexed_at': datetime.now().isoformat(),
                }

                chunks = chunk_by_paragraphs(text, doc_id, base_meta)
                filtered = len([c for c in chunks
                                 if chunk_quality_score(c['text']) < 0.25])
                total_filtered += filtered

                if not chunks:
                    print(f"  -- {filename}: нет годных чанков")
                    continue

                # Удаляем старые данные файла
                try:
                    collection.delete(where={'filename': filename})
                except Exception:
                    pass

                # Добавляем батчами
                BATCH = 100
                for i in range(0, len(chunks), BATCH):
                    batch = chunks[i:i+BATCH]
                    collection.add(
                        documents=[c['text'] for c in batch],
                        metadatas=[c['metadata'] for c in batch],
                        ids=[f"{doc_id}_c{c['metadata']['chunk_index']}"
                             for c in batch],
                    )

                total_added += len(chunks)
                print(f"  OK {filename} [{enc}]"
                      f" -> {len(chunks)} чанков (отфильтровано: {filtered})")

                # Метка
                with open(fpath + '.indexed', 'w', encoding='utf-8') as f:
                    f.write(f"v3 | {datetime.now().isoformat()}"
                            f" | chunks={len(chunks)} | enc={enc}")

            except Exception as e:
                print(f"  ERR {filename}: {e}")
                import traceback; traceback.print_exc()

    print(f"\n{'='*55}")
    print(f"Готово! Добавлено чанков: {total_added}")
    print(f"Всего в базе:            {collection.count()}")
    print(f"Отфильтровано мусора:    {total_filtered}")
    print(f"{'='*55}")

    # Тестовый поиск
    if collection.count() > 0:
        print("\nТест: 'необходимая валовая выручка НВВ'")
        res = collection.query(
            query_texts=["необходимая валовая выручка НВВ определение"],
            n_results=3
        )
        for doc, meta in zip(res['documents'][0], res['metadatas'][0]):
            print(f"  {meta['filename']} | q={meta.get('quality','?')} | {doc[:120]}...")


if __name__ == '__main__':
    index_documents()