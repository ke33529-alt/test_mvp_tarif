# core/local_kb.py
"""
Локальная база знаний сегмента
════════════════════════════════════════════════════════════════════════════════

Каждый сегмент (организация) может завести собственную дополнительную базу
знаний — свои внутренние документы произвольного характера.

Архитектура (физическая изоляция между сегментами):
  - Отдельная ChromaDB-коллекция на КАЖДЫЙ сегмент: local_kb_{org_id}
  - После успешной индексации файл ФИЗИЧЕСКИ УДАЛЯЕТСЯ с сервера — хранится
    только его имя и метаданные индексации в local_kb_meta.json.

Настройки чанкования на уровне сегмента (data/admin/local_kb_chunking.json):
  {
    "org_id_1": {
      "method":     "legal" | "fixed",
      "chunk_size": 1000,      # только для fixed
      "overlap":    100,       # только для fixed
      "word_safe":  true       # только для fixed — не резать по середине слова
    }
  }

  method="legal" — использует LegalDocumentChunker (структурная разбивка НПА).
  method="fixed" — фиксированные чанки с оверлапом.

  Настройки применяются при СЛЕДУЮЩЕЙ загрузке. Ранее проиндексированные
  документы не переиндексируются — их метод хранится в local_kb_meta.json
  вместе с каждой записью документа.
"""

from __future__ import annotations

import os
import json
import shutil
import tempfile
import threading
import traceback as _traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ── Пути ─────────────────────────────────────────────────────────────────────

_BASE_DIR       = Path(__file__).parent.parent.resolve()
_ADMIN_DIR      = _BASE_DIR / "data" / "admin"
_META_FILE      = _ADMIN_DIR / "local_kb_meta.json"
_CHUNK_CFG_FILE = _ADMIN_DIR / "local_kb_chunking.json"

_meta_lock  = threading.Lock()
_chunk_lock = threading.Lock()

# Допустимые расширения для загрузки в локальную базу
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt")

# Дефолтные настройки чанкования — применяются если для сегмента нет записи
DEFAULT_CHUNKING = {
    "method":     "legal",   # legal | fixed
    "chunk_size": 1000,
    "overlap":    100,
    "word_safe":  True,
}


def collection_name(org_id: str) -> str:
    """Имя ChromaDB-коллекции для локальной базы конкретного сегмента."""
    return f"local_kb_{org_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Метаданные документов
# ─────────────────────────────────────────────────────────────────────────────

def _load_meta() -> Dict[str, Dict]:
    if not _META_FILE.exists():
        return {}
    try:
        data = json.loads(_META_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_meta(meta: Dict[str, Dict]) -> None:
    _ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    with _meta_lock:
        _META_FILE.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def get_segment_docs(org_id: str) -> Dict[str, Dict]:
    """Возвращает {filename: {indexed_at, chunks, ext, size_kb_original, chunk_method, ...}}."""
    meta = _load_meta()
    return meta.get(org_id, {})


def _set_segment_doc(org_id: str, filename: str, info: Dict) -> None:
    meta = _load_meta()
    meta.setdefault(org_id, {})[filename] = info
    _save_meta(meta)


def _remove_segment_doc(org_id: str, filename: str) -> None:
    meta = _load_meta()
    if org_id in meta and filename in meta[org_id]:
        del meta[org_id][filename]
        _save_meta(meta)


def get_segment_doc_count(org_id: str) -> int:
    return len(get_segment_docs(org_id))


def get_segment_chunk_count(org_id: str) -> int:
    """Возвращает реальное число чанков в коллекции сегмента."""
    try:
        from core.indexer import _get_chroma_client, get_embedding_function
        client = _get_chroma_client()
        ef     = get_embedding_function()
        try:
            col = client.get_collection(name=collection_name(org_id), embedding_function=ef)
            return col.count()
        except Exception:
            return 0
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Настройки чанкования (per-сегмент)
# ─────────────────────────────────────────────────────────────────────────────

def _load_chunk_cfg() -> Dict[str, Dict]:
    """Загружает конфиг чанкования всех сегментов."""
    if not _CHUNK_CFG_FILE.exists():
        return {}
    try:
        data = json.loads(_CHUNK_CFG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_chunk_cfg(cfg: Dict[str, Dict]) -> None:
    _ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    with _chunk_lock:
        _CHUNK_CFG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def get_chunking_settings(org_id: str) -> Dict:
    """
    Возвращает настройки чанкования для сегмента.
    Если для сегмента настроек нет — возвращает дефолтные.
    """
    cfg = _load_chunk_cfg()
    settings = cfg.get(org_id, {})
    # Сливаем с дефолтами чтобы гарантировать все поля
    return {**DEFAULT_CHUNKING, **settings}


def save_chunking_settings(org_id: str, settings: Dict) -> None:
    """Сохраняет настройки чанкования для сегмента."""
    cfg = _load_chunk_cfg()

    # Валидация
    method     = settings.get("method", "legal")
    chunk_size = max(200, min(5000, int(settings.get("chunk_size", 1000))))
    overlap    = max(0,   min(chunk_size // 2, int(settings.get("overlap", 100))))
    word_safe  = bool(settings.get("word_safe", True))

    if method not in ("legal", "fixed"):
        method = "legal"

    cfg[org_id] = {
        "method":     method,
        "chunk_size": chunk_size,
        "overlap":    overlap,
        "word_safe":  word_safe,
    }
    _save_chunk_cfg(cfg)


def reset_chunking_settings(org_id: str) -> None:
    """Сбрасывает настройки сегмента к дефолтным (удаляет запись)."""
    cfg = _load_chunk_cfg()
    if org_id in cfg:
        del cfg[org_id]
        _save_chunk_cfg(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Фиксированное чанкование с оверлапом
# ─────────────────────────────────────────────────────────────────────────────

def _fixed_chunk_text(
    text: str,
    chunk_size: int,
    overlap: int,
    word_safe: bool,
) -> List[str]:
    """
    Разбивает текст на чанки фиксированного размера с оверлапом.

    word_safe=True — при обрезке ищет ближайшую границу слова СЛЕВА от лимита,
    чтобы не разрывать слово посередине. Если границу найти не удалось
    (слово длиннее чанка) — режем как есть.
    """
    if not text or chunk_size <= 0:
        return []

    text = text.strip()
    if len(text) <= chunk_size:
        return [text]

    # Оверлап не может быть больше/равен размеру чанка — иначе окно не сдвинется
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_size:
        overlap = 0

    chunks = []
    i      = 0

    while i < len(text):
        end = min(i + chunk_size, len(text))

        if word_safe and end < len(text):
            # Ищем ближайший разделитель слова слева от end
            # Разделители: пробел, перенос строки, знак препинания
            search_start = max(i + 1, end - 100)  # не отступаем слишком далеко
            best_pos     = -1
            for pos in range(end, search_start, -1):
                if pos > 0 and text[pos - 1] in " \n\t.,;!?:)»\"":
                    best_pos = pos
                    break
            if best_pos > i:
                end = best_pos

        chunk = text[i:end].strip()
        if chunk:
            chunks.append(chunk)

        # Переход к следующему окну — с учётом оверлапа
        if end >= len(text):
            break

        prev_i = i
        i = end - overlap if overlap > 0 else end

        # Защита от зацикливания: гарантируем строгое продвижение вперёд.
        # Раньше здесь было сравнение `i <= chunks` (int с list) — баг.
        if i <= prev_i:
            i = prev_i + 1

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Индексация — точка входа из UI
# ─────────────────────────────────────────────────────────────────────────────

def index_uploaded_file(uploaded_file, org_id: str) -> dict:
    """
    Индексирует загруженный файл в локальную базу знаний сегмента.
    Использует настройки чанкования сегмента (legal или fixed).
    После успешной индексации ФАЙЛ УДАЛЯЕТСЯ с диска.
    """
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return {
            "status": "error",
            "message": f"Формат {ext or '?'} не поддерживается. Допустимо: PDF, DOCX, TXT.",
        }

    settings = get_chunking_settings(org_id)
    method   = settings["method"]

    tmp_dir  = tempfile.mkdtemp(prefix="local_kb_")
    tmp_path = os.path.join(tmp_dir, uploaded_file.name)

    try:
        with open(tmp_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        size_kb_original = os.path.getsize(tmp_path) / 1024

        if method == "fixed":
            # Собственный путь: извлекаем текст → рубим по chunk_size → upsert
            result = _index_fixed(
                tmp_path,
                org_id=org_id,
                original_name=uploaded_file.name,
                settings=settings,
            )
        else:
            # method == "legal" — используем существующий пайплайн из core.indexer
            from core.indexer import index_file_to_collection
            result = index_file_to_collection(
                tmp_path,
                collection_name=collection_name(org_id),
                extra_metadata={"file": uploaded_file.name, "org_id": org_id},
            )

        if result.get("status") == "success":
            _set_segment_doc(org_id, uploaded_file.name, {
                "indexed_at":       datetime.now().isoformat(timespec="seconds"),
                "chunks":           result.get("chunks", 0),
                "ext":              ext.lstrip(".").upper(),
                "size_kb_original": round(size_kb_original, 1),
                # Метод чанкования и параметры сохраняем в записи документа —
                # чтобы можно было увидеть в UI как именно был проиндексирован
                # каждый конкретный файл (настройки могут меняться со временем).
                "chunk_method":     method,
                "chunk_size":       settings.get("chunk_size") if method == "fixed" else None,
                "overlap":          settings.get("overlap")    if method == "fixed" else None,
                "word_safe":        settings.get("word_safe")  if method == "fixed" else None,
            })
            invalidate_segment_retriever(org_id)

        return result

    except Exception as e:
        print(f"[LOCAL_KB] Ошибка индексации {uploaded_file.name}: {e}")
        _traceback.print_exc()
        return {"status": "error", "message": str(e)}

    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _index_fixed(
    file_path:     str,
    org_id:        str,
    original_name: str,
    settings:      Dict,
) -> dict:
    """
    Индексация файла в фиксированные чанки с оверлапом.
    Извлекает текст любым доступным способом (PDF/DOCX/TXT) → рубит → upsert.
    """
    try:
        # 1. Извлекаем текст файла через существующий loader из core.indexer
        from core.indexer import get_loader, _get_chroma_client, get_embedding_function
        loader = get_loader(file_path)
        if loader is None:
            return {"status": "error", "message": "Не удалось загрузить файл (формат)"}

        docs = loader.load()
        full_text = "\n\n".join(d.page_content for d in docs if d.page_content)
        if not full_text.strip():
            return {"status": "error", "message": "Пустой файл или не удалось извлечь текст"}

        # 2. Рубим фиксированными чанками
        chunks = _fixed_chunk_text(
            full_text,
            chunk_size=int(settings["chunk_size"]),
            overlap=int(settings["overlap"]),
            word_safe=bool(settings["word_safe"]),
        )
        if not chunks:
            return {"status": "error", "message": "После чанкования не осталось фрагментов"}

        # 3. Готовим batch для ChromaDB
        client = _get_chroma_client()
        ef     = get_embedding_function()
        col    = client.get_or_create_collection(
            name=collection_name(org_id),
            embedding_function=ef,
        )

        cname = collection_name(org_id)
        now   = datetime.now().isoformat()
        ids, docs_out, metas = [], [], []

        for i, chunk in enumerate(chunks):
            ids.append(f"{cname}__{original_name}__fixed__{i}")
            docs_out.append(chunk)
            metas.append({
                "filename":     original_name,
                "file":         original_name,
                "filepath":     file_path,
                "category":     cname,
                "chunk_index":  i,
                "chunk_method": "fixed",
                "chunk_size":   int(settings["chunk_size"]),
                "overlap":      int(settings["overlap"]),
                "word_safe":    bool(settings["word_safe"]),
                "indexed_at":   now,
                "org_id":       org_id,
            })

        # 4. Пишем батчами
        BATCH = 100
        for start in range(0, len(ids), BATCH):
            col.upsert(
                ids=ids[start:start+BATCH],
                documents=docs_out[start:start+BATCH],
                metadatas=metas[start:start+BATCH],
            )

        print(f"[LOCAL_KB FIXED] {original_name} → {len(chunks)} чанков "
              f"(size={settings['chunk_size']}, overlap={settings['overlap']})")
        return {"status": "success", "chunks": len(chunks)}

    except Exception as e:
        print(f"[LOCAL_KB FIXED] Ошибка: {e}")
        _traceback.print_exc()
        return {"status": "error", "message": str(e)}


def remove_document(org_id: str, filename: str) -> dict:
    """Удаляет все чанки документа из коллекции сегмента и его запись из метаданных."""
    try:
        from core.indexer import _get_chroma_client, get_embedding_function
        client = _get_chroma_client()
        ef     = get_embedding_function()
        try:
            col = client.get_collection(name=collection_name(org_id), embedding_function=ef)
        except Exception:
            _remove_segment_doc(org_id, filename)
            return {"status": "success", "deleted": 0}

        results = col.get(where={"file": filename}, include=[])
        ids_to_delete = results.get("ids", [])
        if not ids_to_delete:
            results2 = col.get(where={"filename": filename}, include=[])
            ids_to_delete = results2.get("ids", [])

        if ids_to_delete:
            col.delete(ids=ids_to_delete)

        _remove_segment_doc(org_id, filename)
        invalidate_segment_retriever(org_id)
        return {"status": "success", "deleted": len(ids_to_delete)}

    except Exception as e:
        return {"status": "error", "message": str(e)}


def clear_segment_collection(org_id: str) -> dict:
    """Полностью очищает локальную базу сегмента."""
    try:
        from core.indexer import _get_chroma_client
        client = _get_chroma_client()
        try:
            client.delete_collection(collection_name(org_id))
        except Exception:
            pass
        meta = _load_meta()
        meta[org_id] = {}
        _save_meta(meta)
        invalidate_segment_retriever(org_id)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Поиск по локальной базе сегмента
# ─────────────────────────────────────────────────────────────────────────────

_retriever_cache: Dict[str, dict] = {}
_retriever_lock = threading.Lock()


def invalidate_segment_retriever(org_id: str) -> None:
    with _retriever_lock:
        _retriever_cache.pop(org_id, None)


def _get_segment_collection(org_id: str):
    try:
        from core.indexer import _get_chroma_client, get_embedding_function
        client = _get_chroma_client()
        ef     = get_embedding_function()
        try:
            return client.get_collection(name=collection_name(org_id), embedding_function=ef)
        except Exception:
            return None
    except Exception:
        return None


def search_local_kb(query: str, org_id: str, top_k: int = 10) -> List[Dict]:
    if not org_id:
        return []

    collection = _get_segment_collection(org_id)
    if collection is None:
        return []

    try:
        from core.advisor import embed_query
        embedding = embed_query(query)
    except Exception:
        embedding = None

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
        print(f"[LOCAL_KB] Ошибка поиска ({org_id}): {e}")
        return []

    docs  = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    if not docs:
        return []

    sources = []
    for doc, meta, dist in zip(docs, metas, dists):
        meta = meta or {}
        sources.append({
            "snippet":     doc,
            "file":        meta.get("file", meta.get("filename", "Локальный документ")),
            "page":        meta.get("page", ""),
            "category":    "Локальная база",
            "doc_type":    "local",
            "doc_status":  "",
            "article":     "",
            "chunk_index": meta.get("chunk_index", ""),
            "distance":    round(dist, 3),
            "sphere":      "",
            "source_kind": "local",
        })
    return sources


def local_kb_exists(org_id: str) -> bool:
    return get_segment_doc_count(org_id) > 0


LOCAL_DOC_TYPE_KEY = "local"