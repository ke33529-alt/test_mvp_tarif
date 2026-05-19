# core/indexer.py
import os
import sys
import json
from datetime import datetime
import traceback

# Добавляем корень проекта в путь для корректного импорта модуля core
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# =============================================================================
# Настройки
# =============================================================================
DATA_DIR = "data"
RAW_DIR = os.path.join(DATA_DIR, "raw")
VECTOR_DB_DIR = os.path.join(DATA_DIR, "vector_db")
METADATA_FILE = os.path.join(VECTOR_DB_DIR, "indexing_metadata.json")
CONFIG_FILE = os.path.join("config", "chunking_patterns.json")

# =============================================================================
# Импорты с обработкой ошибок
# =============================================================================
try:
    from langchain_community.embeddings import HuggingFaceEmbeddings
    from langchain_community.vectorstores import Chroma
    from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, TextLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    LANGCHAIN_AVAILABLE = True
except ImportError as e:
    print(f"[ERROR] Не установлены зависимости langchain: {e}")
    print("[HINT] Выполните: pip install langchain-community langchain chromadb sentence-transformers")
    LANGCHAIN_AVAILABLE = False

# =============================================================================
# Импорт умного чанкера
# =============================================================================
try:
    from core.chunker import LegalDocumentChunker, detect_doc_type, extract_metadata_from_filename
    CHUNKER_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Не удалось загрузить умный чанкер: {e}")
    print("[INFO] Будет использоваться стандартное разбиение")
    CHUNKER_AVAILABLE = False
    
    # Определяем заглушки для совместимости
    def detect_doc_type(filepath: str, config_file: str = None) -> str:
        """Заглушка: возвращает unknown, если чанкер недоступен"""
        return "unknown"

    def extract_metadata_from_filename(filepath: str, config_file: str = None) -> dict:
        """Заглушка: возвращает пустой dict, если чанкер недоступен"""
        return {}

# =============================================================================
# Функции для работы с метаданными
# =============================================================================
def load_metadata():
    """Загружает метаданные индексации"""
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_metadata(metadata):
    """Сохраняет метаданные"""
    os.makedirs(os.path.dirname(METADATA_FILE), exist_ok=True)
    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

# =============================================================================
# Функции для загрузки файлов
# =============================================================================
def _detect_encoding(file_path: str) -> str:
    """
    Автоопределение кодировки файла без внешних библиотек.
    Перебирает кодировки в порядке приоритета: UTF-8 → CP1251 → Latin-1.
    Latin-1 используется как fallback — никогда не падает.
    """
    with open(file_path, "rb") as f:
        raw = f.read(32768)  # читаем первые 32KB для определения
    for enc in ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1"


def get_loader(file_path):
    """Возвращает загрузчик для типа файла"""
    if file_path.endswith(".pdf"):
        return PyPDFLoader(file_path)
    elif file_path.endswith(".docx"):
        try:
            import docx2txt
            return Docx2txtLoader(file_path)
        except ImportError:
            print(f"[WARN] docx2txt не установлен, пропускаем {file_path}")
            return None
    elif file_path.endswith(".txt"):
        enc = _detect_encoding(file_path)
        print(f"[ENCODE] {os.path.basename(file_path)}: определена кодировка {enc}")
        return TextLoader(file_path, encoding=enc)
    return None

# =============================================================================
# 🆕 НОВЫЕ ФУНКЦИИ ДЛЯ СОВМЕСТИМОСТИ С APP.PY
# =============================================================================
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"

# =============================================================================
# Кастомная embedding function с префиксами для multilingual-e5-large.
# e5-модели требуют:
#   "passage: <текст>"  — при индексации документов
#   "query: <текст>"    — при поиске (в advisor.py, embed_query)
# Без префиксов качество поиска значительно хуже.
# =============================================================================
class E5EmbeddingFunction:
    """ChromaDB-совместимая embedding function для intfloat/multilingual-e5-large."""

    def __init__(self, model_name: str = EMBEDDING_MODEL):
        from sentence_transformers import SentenceTransformer
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = SentenceTransformer(model_name, device=device)
        print(f"[EMBED] E5EmbeddingFunction загружена: {model_name} на {device}")

    def __call__(self, input: list) -> list:
        """ChromaDB вызывает эту функцию при upsert/query."""
        prefixed = [f"passage: {text}" for text in input]
        embeddings = self.model.encode(
            prefixed,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()


_ef_instance = None
_ef_lock     = __import__("threading").Lock()


def get_embedding_function():
    """Синглтон E5EmbeddingFunction — создаётся один раз."""
    global _ef_instance
    if _ef_instance is not None:
        return _ef_instance
    with _ef_lock:
        if _ef_instance is None:
            _ef_instance = E5EmbeddingFunction(EMBEDDING_MODEL)
    return _ef_instance

# ── Синглтон клиента ChromaDB ─────────────────────────────────────────────────
# PersistentClient нельзя открывать несколько раз на одну папку —
# это вызывает "already exists" и конфликты. Один клиент на весь процесс.
_chroma_client = None
_chroma_client_lock = __import__('threading').Lock()

def _get_chroma_client():
    global _chroma_client
    if _chroma_client is not None:
        return _chroma_client
    with _chroma_client_lock:
        if _chroma_client is None:
            import chromadb
            os.makedirs(VECTOR_DB_DIR, exist_ok=True)
            _chroma_client = chromadb.PersistentClient(path=VECTOR_DB_DIR)
        return _chroma_client

def initialize_db():
    """Инициализирует базу данных ChromaDB с правильной embedding function"""
    client = _get_chroma_client()
    ef = get_embedding_function()
    try:
        collection = client.get_collection(name="tariff_docs", embedding_function=ef)
    except Exception:
        collection = client.create_collection(name="tariff_docs", embedding_function=ef)
    return client, collection

def load_chunking_settings() -> dict:
    """Загружает настройки чанкования из конфига"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                return cfg.get("chunking_settings", {})
        except Exception:
            pass
    return {}

def apply_chunking(text: str, base_metadata: dict, settings: dict, chunker) -> list:
    """Разбивает текст на чанки согласно настройкам режима"""
    mode = settings.get("chunking_mode", "structural")
    overlap = settings.get("chunk_overlap", 0)
    min_len = settings.get("min_chunk_length", 50)

    # ── Режим 0: юридический чанкер по пунктам НПА ──────────────────────────
    if mode == "legal" and chunker:
        doc_id  = base_metadata.get("filename", "unknown")
        max_len = settings.get("max_chunk_length", 3000)
        if hasattr(chunker, "max_chunk_chars"):
            chunker.max_chunk_chars = max_len
        min_len_legal = settings.get("min_chunk_length", 50)
        if hasattr(chunker, "chunk_by_legal_structure"):
            chunks = chunker.chunk_by_legal_structure(text, doc_id, base_metadata)
        else:
            chunks = chunker.chunk_by_structure(text, base_metadata)
        chunks = [c for c in chunks if len(c.get("text", "")) >= min_len_legal]
        if len(chunks) > MAX_CHUNKS_PER_FILE:
            chunks = chunks[:MAX_CHUNKS_PER_FILE]
        return chunks

    # ── Режим 1: умный структурный чанкер ───────────────────────────────────
    elif mode == "structural" and chunker:
        chunks = chunker.chunk_by_structure(text, base_metadata)
        if overlap > 0:
            chunks = _apply_overlap(chunks, overlap)
        chunks = [c for c in chunks if len(c.get('text', '')) >= min_len]
        if len(chunks) > MAX_CHUNKS_PER_FILE:
            print(f"[WARN] Структурный чанкер: {len(chunks)} чанков — обрезаем до {MAX_CHUNKS_PER_FILE}")
            chunks = chunks[:MAX_CHUNKS_PER_FILE]
        return chunks

    # ── Режим 2: по разделителю ──────────────────────────────────────────────
    elif mode == "separator":
        separator = settings.get("separator", "&&")
        max_len = settings.get("max_chunk_length", 2000)
        parts = text.split(separator)
        raw_chunks = []
        for part in parts:
            part = part.strip()
            if len(part) < min_len:
                continue
            if len(part) > max_len:
                sub = _split_fixed(part, max_len, overlap, settings)
                raw_chunks.extend(sub)
            else:
                raw_chunks.append(part)
        if overlap > 0:
            raw_chunks = _apply_overlap_texts(raw_chunks, overlap)
        return [{'text': t, 'metadata': base_metadata} for t in raw_chunks if len(t) >= min_len]

    # ── Режим 3: фиксированная длина ─────────────────────────────────────────
    elif mode == "fixed":
        fixed_len = settings.get("fixed_chunk_length", 1000)
        parts = _split_fixed(text, fixed_len, overlap, settings)
        return [{'text': t, 'metadata': base_metadata} for t in parts if len(t) >= min_len]

    # ── Fallback: стандартный RecursiveCharacterTextSplitter ─────────────────
    else:
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.get("max_chunk_length", 500),
            chunk_overlap=overlap,
            length_function=len
        )
        parts = text_splitter.split_text(text)
        return [{'text': t, 'metadata': base_metadata} for t in parts if len(t) >= min_len]

def _snap_to_boundary(text: str, start: int, end: int, settings: dict) -> int:
    """
    Стратегия: сначала ищем границу НАЗАД в «хорошей» зоне [min_back..end],
    не нашли — ищем ВПЕРЁД от end. Чанк растянется, но не разрежется.

    min_back = start + 3/4 окна — достаточно близко к концу окна,
    чтобы rfind не поймал точку в дате у самого начала окна.
    """
    if end >= len(text):
        return len(text)

    window = end - start
    # Ищем назад только в последней четверти окна — исключаем точки в датах
    min_back = start + (window * 3 // 4)

    # ── Абзац: \n\n ──────────────────────────────────────────────────────────
    if settings.get("no_cut_paragraph", False):
        pos = text.rfind("\n\n", min_back, end)
        if pos >= min_back:
            return pos + 2
        pos = text.find("\n\n", end)                   # → вперёд
        if pos != -1:
            return pos + 2

    # ── Предложение: . ! ? ───────────────────────────────────────────────────
    if settings.get("no_cut_sentence", True):
        best_back = -1
        for punct in (".", "!", "?"):
            p = text.rfind(punct, min_back, end)
            if p > best_back:
                best_back = p
        if best_back >= min_back:
            return best_back + 1
        # Не нашли в хорошей зоне — ищем вперёд (чанк станет немного больше)
        best_fwd = len(text)
        for punct in (".", "!", "?"):
            p = text.find(punct, end)
            if p != -1 and p < best_fwd:
                best_fwd = p
        if best_fwd < len(text):
            return best_fwd + 1

    # ── Слово: пробел ────────────────────────────────────────────────────────
    if settings.get("no_cut_word", True):
        pos = text.rfind(" ", min_back, end)
        if pos >= min_back:
            return pos + 1
        pos = text.find(" ", end)                         # → вперёд
        if pos != -1:
            return pos + 1

    # Все опции выключены или граница не найдена — жёсткий разрез
    return end


MAX_CHUNKS_PER_FILE = 2000   # жёсткий лимит: защита от утечки памяти


def _split_fixed(text: str, size: int, overlap: int = 0, settings: dict = None) -> list:
    """Разбивает текст на куски с умными границами. Гарантирует завершение."""
    if settings is None:
        settings = {}
    if overlap >= size:
        overlap = 0

    chunks = []
    pos = 0
    while pos < len(text) and len(chunks) < MAX_CHUNKS_PER_FILE:
        end = min(pos + size, len(text))
        end = _snap_to_boundary(text, pos, end, settings)

        # Гарантируем продвижение: если end не ушёл вперёд — форсируем
        if end <= pos:
            end = min(pos + size, len(text))

        chunk = text[pos:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break
        pos = max(end - overlap, end - size + 1) if overlap else end

    if len(chunks) >= MAX_CHUNKS_PER_FILE:
        print(f"[WARN] _split_fixed: достигнут лимит {MAX_CHUNKS_PER_FILE} чанков, "
              f"остаток текста ({len(text) - pos} симв.) пропущен")

    return chunks

def _apply_overlap(chunks: list, overlap: int) -> list:
    """Добавляет перекрытие к структурным чанкам (словари с ключом 'text')"""
    result = []
    for i, chunk in enumerate(chunks):
        if i > 0 and overlap > 0:
            prev_text = chunks[i-1].get('text', '')
            suffix = prev_text[-overlap:] if len(prev_text) > overlap else prev_text
            chunk = dict(chunk)
            chunk['text'] = suffix + chunk.get('text', '')
        result.append(chunk)
    return result

def _apply_overlap_texts(texts: list, overlap: int) -> list:
    """Добавляет перекрытие к списку строк"""
    result = []
    for i, text in enumerate(texts):
        if i > 0 and overlap > 0:
            prev = texts[i-1]
            suffix = prev[-overlap:] if len(prev) > overlap else prev
            text = suffix + text
        result.append(text)
    return result

def index_file(file_path, category="npa"):
    """Индексирует один файл с учётом настроек чанкования"""
    try:
        if not LANGCHAIN_AVAILABLE:
            return {"status": "error", "message": "LangChain не установлен"}

        loader = get_loader(file_path)
        if loader is None:
            return {"status": "error", "message": "Неподдерживаемый формат"}

        docs = loader.load()
        settings = load_chunking_settings()

        chunker = None
        if CHUNKER_AVAILABLE:
            try:
                chunker = LegalDocumentChunker(
                    max_chunk_chars=settings.get("max_chunk_length", 900),
                    neighbor_radius=settings.get("neighbor_radius", 2),
                )
                print(f"[CONFIG] Чанкер: max={chunker.max_chunk_chars} симв., "
                      f"radius={chunker.neighbor_radius}, "
                      f"overlap={settings.get('chunk_overlap', 0)}")
            except Exception as e:
                print(f"[WARN] Ошибка инициализации чанкера: {e}")
        print(f"[DIAG] === НАСТРОЙКИ ЧАНКОВАНИЯ ===")
        print(f"[DIAG] mode={settings.get('chunking_mode','structural')} | max={settings.get('max_chunk_length',900)} | min={settings.get('min_chunk_length',50)} | overlap={settings.get('chunk_overlap',0)}")
        print(f"[DIAG] no_cut_word={settings.get('no_cut_word',True)} | no_cut_sentence={settings.get('no_cut_sentence',True)} | no_cut_paragraph={settings.get('no_cut_paragraph',False)}")
        print(f"[DIAG] Документов от загрузчика: {len(docs)}")
        chunks = []
        for doc in docs:
            base_metadata = {
                'filename': os.path.basename(doc.metadata.get('source', '')),
                'filepath': doc.metadata.get('source', ''),
                'category': category,
                'doc_type': detect_doc_type(doc.metadata.get('source', ''), CONFIG_FILE),
                'indexed_at': datetime.now().isoformat()
            }
            file_metadata = extract_metadata_from_filename(doc.metadata.get('source', ''), CONFIG_FILE)
            base_metadata.update(file_metadata)

            doc_chunks = apply_chunking(doc.page_content, base_metadata, settings, chunker)
            print(f"[DIAG] Документ: {len(doc.page_content)} симв. → {len(doc_chunks)} чанков")
            if doc_chunks:
                print(f"[DIAG] Первый чанк ({len(doc_chunks[0]['text'])} симв.): {repr(doc_chunks[0]['text'][:120])}")
                print(f"[DIAG] Последний чанк ({len(doc_chunks[-1]['text'])} симв.): {repr(doc_chunks[-1]['text'][:120])}")
            chunks.extend(doc_chunks)

        if not chunks:
            return {"status": "error", "message": "Пустой файл или нет чанков после разбивки"}

        client, collection = initialize_db()

        ids, documents, metadatas = [], [], []
        for i, chunk in enumerate(chunks):
            doc_id = f"{os.path.basename(file_path)}_{i}"
            ids.append(doc_id)
            documents.append(chunk['text'])
            metadatas.append({
                "filename":      chunk['metadata'].get('filename', ''),
                "filepath":      chunk['metadata'].get('filepath', ''),
                "category":      chunk['metadata'].get('category', ''),
                "doc_type":      chunk['metadata'].get('doc_type', ''),
                "doc_number":    chunk['metadata'].get('doc_number', ''),
                "doc_date":      chunk['metadata'].get('doc_date', ''),
                "struct_type":   chunk['metadata'].get('struct_type', ''),
                "struct_text":   chunk['metadata'].get('text_preview', chunk['metadata'].get('struct_text', '')),
                "article":       chunk['metadata'].get('article', ''),
                "paragraph":     chunk['metadata'].get('paragraph', ''),
                "section":       chunk['metadata'].get('section', ''),
                "document_part": chunk['metadata'].get('document_part', ''),
                "text_preview":  chunk['metadata'].get('text_preview', ''),
                "chunk_index":   i,
                "indexed_at":    datetime.now().isoformat()
            })

        BATCH = 100
        for start in range(0, len(ids), BATCH):
            collection.upsert(
                ids=ids[start:start+BATCH],
                documents=documents[start:start+BATCH],
                metadatas=metadatas[start:start+BATCH]
            )

        return {"status": "success", "chunks": len(chunks)}

    except Exception as e:
        return {"status": "error", "message": str(e)}

def index_category(category="npa"):
    """Индексирует всю категорию документов (для совместимости с app.py)"""
    folder = os.path.join(RAW_DIR, category)
    if not os.path.exists(folder):
        return {"status": "error", "message": f"Папка не найдена: {folder}"}
    results = []
    for filename in os.listdir(folder):
        if filename.startswith('.'):
            continue
        file_path = os.path.join(folder, filename)
        if os.path.isfile(file_path):
            # Удаляем старые чанки файла перед переиндексацией —
            # иначе при смене настроек чанкования старые чанки накапливаются
            try:
                remove_file_from_index(filename)
            except Exception:
                pass
            result = index_file(file_path, category)
            results.append({"file": filename, "result": result})
    return {"status": "success", "files": results}

def get_index_stats():
    """Возвращает статистику индекса (для совместимости с app.py)"""
    try:
        _, collection = initialize_db()
        count = collection.count()
        return {"status": "success", "documents": count}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def clear_index():
    """
    Полностью очищает индекс: сносит папку vector_db и пересоздаёт её.
    Это гарантирует что ChromaDB не сохраняет старую размерность эмбеддингов
    (актуально при смене модели, например MiniLM-384 → e5-large-1024).
    """
    import shutil
    global _chroma_client

    # 1. Закрываем синглтон клиента перед удалением папки
    with _chroma_client_lock:
        _chroma_client = None

    # 2. Сбрасываем коллекцию в advisor (если загружена)
    try:
        from core.advisor import invalidate_chroma_collection
        invalidate_chroma_collection()
    except Exception:
        pass

    # 3. Физически сносим папку vector_db
    vector_db_path = os.path.join("data", "vector_db")
    try:
        if os.path.exists(vector_db_path):
            shutil.rmtree(vector_db_path)
            print(f"[INDEX] Папка {vector_db_path} удалена")
    except Exception as e:
        return {"status": "error", "message": f"Не удалось удалить папку: {e}"}

    # 4. Пересоздаём пустую папку и новую коллекцию
    try:
        os.makedirs(vector_db_path, exist_ok=True)
        client = _get_chroma_client()
        client.create_collection(name="tariff_docs")
        print("[INDEX] Индекс пересоздан с нуля")
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def remove_file_from_index(filename: str):
    """Удаляет все чанки файла из индекса по имени файла"""
    try:
        client = _get_chroma_client()
        ef = get_embedding_function()
        try:
            collection = client.get_collection(name="tariff_docs", embedding_function=ef)
        except Exception:
            return {"status": "ok", "message": "Коллекция не существует, нечего удалять"}

        # Получаем все ID чанков с этим именем файла
        results = collection.get(
            where={"filename": os.path.basename(filename)},
            include=[]
        )
        ids_to_delete = results.get("ids", [])

        if ids_to_delete:
            collection.delete(ids=ids_to_delete)
            return {"status": "success", "deleted": len(ids_to_delete)}
        return {"status": "success", "deleted": 0}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =============================================================================
# ОРИГИНАЛЬНАЯ ФУНКЦИЯ REBUILD_INDEX (ОБНОВЛЕНА С УМНЫМ ЧАНКОВАНИЕМ)
# =============================================================================
def rebuild_index():
    """Перестраивает векторную базу — С УМНЫМ ЧАНКОВАНИЕМ"""
    if not LANGCHAIN_AVAILABLE:
        print("[ERROR] Индексация невозможна: не установлены зависимости")
        return False
    
    print("[INDEX] Начало индексации...")
    
    # Создаём папки
    os.makedirs(VECTOR_DB_DIR, exist_ok=True)
    os.makedirs(RAW_DIR, exist_ok=True)
    
    # Загружаем метаданные
    metadata = load_metadata()
    
    # ← УМНОЕ ЧАНКОВАНИЕ: создаём чанкер
    chunker = None
    if CHUNKER_AVAILABLE:
        try:
            settings_for_rebuild = load_chunking_settings()
            chunker = LegalDocumentChunker(
                max_chunk_chars=settings_for_rebuild.get("max_chunk_length", 900),
                neighbor_radius=settings_for_rebuild.get("neighbor_radius", 2),
            )
            print(f"[CONFIG] Умный чанкер инициализирован")
            print(f"[CONFIG] Размер чанка: {chunker.max_chunk_chars} символов")
            print(f"[CONFIG] Радиус соседей: {chunker.neighbor_radius}")
            print(f"[CONFIG] Перекрытие: {settings_for_rebuild.get('chunk_overlap', 0)} символов")
        except Exception as e:
            print(f"[WARN] Ошибка инициализации чанкера: {e}")
            chunker = None
    else:
        print("[WARN] Умный чанкер недоступен, используется стандартное разбиение")
    
    # Собираем документы
    documents = []
    files_processed = 0
    
    print("[DOCS] Сканирование документов...")
    
    # Проверяем наличие файлов
    if not os.path.exists(RAW_DIR):
        print(f"[ERROR] Папка не найдена: {RAW_DIR}")
        return False
        
    files_list = os.listdir(RAW_DIR)
    if not files_list:
        print(f"[WARN] Папка пуста: {RAW_DIR}")
        # Продолжаем, но предупреждаем
    
    for filename in files_list:
        if filename.startswith('.'):
            continue
        
        file_path = os.path.join(RAW_DIR, filename)
        
        # Пропускаем, если файл исключён из обучения
        if filename in metadata and not metadata[filename].get("in_training", True):
            print(f"[SKIP] Исключён из обучения: {filename}")
            continue
        
        loader = get_loader(file_path)
        if loader is None:
            continue
        
        try:
            docs = loader.load()
            documents.extend(docs)
            files_processed += 1
            print(f"[OK] Загружен: {filename} ({len(docs)} страниц/чанков)")
        except Exception as e:
            print(f"[ERROR] Ошибка загрузки {filename}: {e}")
    
    if not documents:
        print("[WARN] Нет документов для индексации")
        save_metadata(metadata)
        return True
    
    # ← УМНОЕ ЧАНКОВАНИЕ: разбиваем по структуре вместо фиксированного размера
    print("[CHUNKS] Разбиение на чанки...")
    
    chunks = []
    for doc in documents:
        base_metadata = {
            'filename': os.path.basename(doc.metadata.get('source', '')),
            'filepath': doc.metadata.get('source', ''),
            'doc_type': detect_doc_type(doc.metadata.get('source', ''), CONFIG_FILE),
            'indexed_at': datetime.now().isoformat()
        }
        
        file_metadata = extract_metadata_from_filename(doc.metadata.get('source', ''), CONFIG_FILE)
        base_metadata.update(file_metadata)
        
        if chunker:
            doc_chunks = chunker.chunk_by_structure(doc.page_content, base_metadata)
            chunks.extend(doc_chunks)
        else:
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50, length_function=len)
            sub_chunks = text_splitter.split_text(doc.page_content)
            chunks.extend([{
                'text': chunk,  # Ключ 'text'
                'metadata': base_metadata
            } for chunk in sub_chunks])
    
    print(f"[CHUNKS] Всего чанков: {len(chunks)}")
    
    print("[EMBED] Создание эмбеддингов и сохранение в базу...")
    
    try:
        client, collection = initialize_db()

        # Пакетная вставка
        ids = []
        documents_list = []
        metadatas_list = []

        for i, chunk in enumerate(chunks):
            source = chunk['metadata'].get('filepath', '')
            fname = os.path.basename(source) if source else f"doc_{i}"
            doc_id = f"{fname}_{i}"
            ids.append(doc_id)
            documents_list.append(chunk['text'])
            metadatas_list.append({
                "filename":      chunk['metadata'].get('filename', ''),
                "filepath":      chunk['metadata'].get('filepath', ''),
                "category":      chunk['metadata'].get('category', ''),
                "doc_type":      chunk['metadata'].get('doc_type', ''),
                "doc_number":    chunk['metadata'].get('doc_number', ''),
                "doc_date":      chunk['metadata'].get('doc_date', ''),
                "struct_type":   chunk['metadata'].get('struct_type', ''),
                "struct_text":   chunk['metadata'].get('text_preview', chunk['metadata'].get('struct_text', '')),
                "article":       chunk['metadata'].get('article', ''),
                "paragraph":     chunk['metadata'].get('paragraph', ''),
                "section":       chunk['metadata'].get('section', ''),
                "document_part": chunk['metadata'].get('document_part', ''),
                "text_preview":  chunk['metadata'].get('text_preview', ''),
                "chunk_index":   i,
                "indexed_at":    datetime.now().isoformat()
            })

        # Вставляем батчами по 100
        BATCH = 100
        for start in range(0, len(ids), BATCH):
            collection.upsert(
                ids=ids[start:start+BATCH],
                documents=documents_list[start:start+BATCH],
                metadatas=metadatas_list[start:start+BATCH]
            )

        print("[OK] Векторная база сохранена")

    except Exception as e:
        print(f"[ERROR] Ошибка создания эмбеддингов: {e}")
        traceback.print_exc()
        return False
    
    # Обновляем метаданные
    for chunk in chunks:
        source = chunk['metadata'].get('filepath', '')
        if source:
            filename = os.path.basename(source)
            if filename not in metadata:
                metadata[filename] = {}
            metadata[filename]["indexed"] = True
            metadata[filename]["indexed_at"] = datetime.now().isoformat()
            metadata[filename]["chunks"] = metadata[filename].get("chunks", 0) + 1
    
    save_metadata(metadata)
    
    print("[OK] Индексация завершена")
    print(f"[STATS] Файлов: {files_processed}, Чанков: {len(chunks)}")
    
    return True

# =============================================================================
# 🆕 ФУНКЦИИ ДЛЯ ПРОСМОТРА ЧАНКОВ (ДЛЯ ADMINKA)
# =============================================================================
def get_chunks_by_file(limit_per_file: int = 10) -> dict:
    """Возвращает информацию о чанках в разрезе файлов"""
    try:
        client = _get_chroma_client()
        ef = get_embedding_function()
        try:
            collection = client.get_collection(name="tariff_docs", embedding_function=ef)
        except Exception:
            return {"status": "error", "message": "Индекс не существует"}
        
        all_data = collection.get(include=["documents", "metadatas"])
        
        files_dict = {}
        for i, (doc_id, doc_content, metadata) in enumerate(zip(
            all_data["ids"], 
            all_data["documents"], 
            all_data["metadatas"]
        )):
            filename = metadata.get("filename", "Неизвестно")
            
            if filename not in files_dict:
                files_dict[filename] = {
                    "filename": filename,
                    "total_chunks": 0,
                    "doc_type": metadata.get("doc_type", ""),
                    "doc_number": metadata.get("doc_number", ""),
                    "doc_date": metadata.get("doc_date", ""),
                    "category": metadata.get("category", ""),
                    "chunks": []
                }
            
            files_dict[filename]["total_chunks"] += 1
            
            if len(files_dict[filename]["chunks"]) < limit_per_file:
                files_dict[filename]["chunks"].append({
                    "id": doc_id,
                    "content": doc_content[:500] + "..." if len(doc_content) > 500 else doc_content,
                    "metadata": {
                        "struct_type": metadata.get("struct_type", ""),
                        "struct_text": metadata.get("struct_text", ""),
                        "article": metadata.get("article", ""),
                        "paragraph": metadata.get("paragraph", ""),
                        "category": metadata.get("category", "")
                    }
                })
        
        files_list = list(files_dict.values())
        total_chunks = sum(f["total_chunks"] for f in files_list)
        
        return {
            "status": "success",
            "files": files_list,
            "total_files": len(files_list),
            "total_chunks": total_chunks
        }
    
    except Exception as e:
        return {"status": "error", "message": str(e)}

def get_chunk_stats() -> dict:
    """Возвращает общую статистику по чанкам"""
    try:
        client = _get_chroma_client()
        ef = get_embedding_function()
        try:
            collection = client.get_collection(name="tariff_docs", embedding_function=ef)
            count = collection.count()
        except Exception:
            return {"status": "error", "message": "Индекс не существует"}
        
        all_data = collection.get(include=["metadatas"])
        
        doc_types = {}
        categories = {}
        
        for metadata in all_data["metadatas"]:
            doc_type = metadata.get("doc_type", "other")
            category = metadata.get("category", "other")
            
            doc_types[doc_type] = doc_types.get(doc_type, 0) + 1
            categories[category] = categories.get(category, 0) + 1
        
        return {
            "status": "success",
            "total_chunks": count,
            "doc_types": doc_types,
            "categories": categories
        }
    
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =============================================================================
# Запуск
# =============================================================================
if __name__ == "__main__":
    try:
        success = rebuild_index()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"[FATAL] Критическая ошибка: {e}")
        print(f"[TRACEBACK]\n{traceback.format_exc()}")
        sys.exit(1)