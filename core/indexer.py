# core/indexer.py
import os
import sys
import json
from datetime import datetime
import traceback

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

# =============================================================================
# Функции для работы с метаданными
# =============================================================================
def load_metadata():
    """Загружает метаданные индексации"""
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
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
        return TextLoader(file_path)
    return None

# =============================================================================
# 🆕 НОВЫЕ ФУНКЦИИ ДЛЯ СОВМЕСТИМОСТИ С APP.PY
# =============================================================================
def initialize_db():
    """Инициализирует базу данных ChromaDB (для совместимости с app.py)"""
    import chromadb
    os.makedirs(VECTOR_DB_DIR, exist_ok=True)
    client = chromadb.PersistentClient(path=VECTOR_DB_DIR)
    try:
        collection = client.get_collection(name="tariff_docs")
    except Exception:
        collection = client.create_collection(name="tariff_docs")
    return client, collection

def index_file(file_path, category="npa"):
    """Индексирует один файл (для совместимости с app.py) — С УМНЫМ ЧАНКОВАНИЕМ"""
    try:
        if not LANGCHAIN_AVAILABLE:
            return {"status": "error", "message": "LangChain не установлен"}
        
        loader = get_loader(file_path)
        if loader is None:
            return {"status": "error", "message": "Неподдерживаемый формат"}
        
        docs = loader.load()
        
        # ← УМНОЕ ЧАНКОВАНИЕ: загружаем конфиг и создаём чанкер
        if CHUNKER_AVAILABLE:
            chunker = LegalDocumentChunker(patterns_file=CONFIG_FILE)
        else:
            chunker = None
        
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
            
            if chunker:
                doc_chunks = chunker.chunk_by_structure(doc.page_content, base_metadata)
                chunks.extend(doc_chunks)
            else:
                text_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50, length_function=len)
                sub_chunks = text_splitter.split_text(doc.page_content)
                chunks.extend([{
                    'content': chunk,
                    'metadata': base_metadata
                } for chunk in sub_chunks])
        
        if not chunks:
            return {"status": "error", "message": "Пустой файл"}
        
        client, collection = initialize_db()
        
        for i, chunk in enumerate(chunks):
            doc_id = f"{os.path.basename(file_path)}_{i}"
            collection.upsert(
                ids=[doc_id],
                documents=[chunk['content']],
                metadatas=[{
                    "filename": chunk['metadata'].get('filename', ''),
                    "filepath": chunk['metadata'].get('filepath', ''),
                    "category": chunk['metadata'].get('category', ''),
                    "doc_type": chunk['metadata'].get('doc_type', ''),
                    "doc_number": chunk['metadata'].get('doc_number', ''),
                    "doc_date": chunk['metadata'].get('doc_date', ''),
                    "struct_type": chunk['metadata'].get('struct_type', ''),
                    "struct_text": chunk['metadata'].get('struct_text', ''),
                    "article": chunk['metadata'].get('article', ''),
                    "paragraph": chunk['metadata'].get('paragraph', ''),
                    "chunk_index": i,
                    "indexed_at": datetime.now().isoformat()
                }]
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
            result = index_file(file_path, category)
            results.append({"file": filename, "result": result})
    return {"status": "success", "files": results}

def get_index_stats():
    """Возвращает статистику индекса (для совместимости с app.py)"""
    try:
        client, collection = initialize_db()
        count = collection.count()
        return {"status": "success", "documents": count}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def clear_index():
    """Очищает индекс (для совместимости с app.py)"""
    try:
        client, collection = initialize_db()
        client.delete_collection(name="tariff_docs")
        client.create_collection(name="tariff_docs")
        return {"status": "success"}
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
    
    # ← УМНОЕ ЧАНКОВАНИЕ: загружаем конфиг и создаём чанкер
    if CHUNKER_AVAILABLE:
        chunker = LegalDocumentChunker(patterns_file=CONFIG_FILE)
        print(f"[CONFIG] Конфигурация загружена: {CONFIG_FILE}")
        print(f"[CONFIG] Размер чанка: {chunker.chunk_size}, Перекрытие: {chunker.chunk_overlap}")
    else:
        chunker = None
        print("[WARN] Умный чанкер недоступен, используется стандартное разбиение")
    
    # Собираем документы
    documents = []
    files_processed = 0
    
    print("[DOCS] Сканирование документов...")
    
    for filename in os.listdir(RAW_DIR):
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
                'content': chunk,
                'metadata': base_metadata
            } for chunk in sub_chunks])
    
    print(f"[CHUNKS] Всего чанков: {len(chunks)}")
    
    # Создаём эмбеддинги и векторную базу
    print("[EMBED] Создание эмбеддингов (это может занять время)...")
    
    try:
        embeddings = HuggingFaceEmbeddings(
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True}
        )
        
        # Создаём/пересоздаём базу
        vectorstore = Chroma.from_documents(
            documents=[type('obj', (object,), {'page_content': c['content'], 'metadata': c['metadata']})() for c in chunks],
            embedding=embeddings,
            persist_directory=VECTOR_DB_DIR
        )
        vectorstore.persist()
        
        print("[OK] Векторная база сохранена")
        
    except Exception as e:
        print(f"[ERROR] Ошибка создания эмбеддингов: {e}")
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
        import chromadb
        client = chromadb.PersistentClient(path=VECTOR_DB_DIR)
        
        try:
            collection = client.get_collection(name="tariff_docs")
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
        import chromadb
        client = chromadb.PersistentClient(path=VECTOR_DB_DIR)
        
        try:
            collection = client.get_collection(name="tariff_docs")
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