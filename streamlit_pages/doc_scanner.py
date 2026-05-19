# streamlit_pages/doc_scanner.py
"""
AI-Сканер документов
Поддерживаемые форматы: PDF (текстовый и скан), DOCX, DOC, XLSX, JPG, PNG
OCR: EasyOCR (основной) → Tesseract (fallback)
Пересказ: LM Studio
Поиск: текстовый с синонимами через QueryExpander
"""
import os
import io
import re
import json
import time
import hashlib
import threading
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import streamlit as st

# =============================================================================
# Утилиты — загрузка/сохранение базы сканов
# =============================================================================
_DB_PATH = os.path.join("data", "doc_scanner", "scans_db.json")
_DB_LOCK = threading.Lock()


def _ensure_dir():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)


def _fname(doc: Dict) -> str:
    """Совместимость: старые документы используют 'file_name', новые — 'filename'."""
    return doc.get("filename") or doc.get("file_name") or "неизвестный файл"


def load_db() -> Dict:
    _ensure_dir()
    if os.path.exists(_DB_PATH):
        try:
            with open(_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"documents": [], "stats": {"total": 0, "last_scan": None}}


def save_db(db: Dict):
    _ensure_dir()
    with _DB_LOCK:
        with open(_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)


# =============================================================================
# OCR — EasyOCR с fallback на Tesseract
# =============================================================================
_easyocr_reader      = None
_easyocr_lock        = threading.Lock()
_EASYOCR_AVAILABLE   = False
_TESSERACT_AVAILABLE = False


def _init_ocr():
    global _easyocr_reader, _EASYOCR_AVAILABLE, _TESSERACT_AVAILABLE
    # EasyOCR
    try:
        import easyocr
        with _easyocr_lock:
            if _easyocr_reader is None:
                _easyocr_reader = easyocr.Reader(
                    ["ru", "en"],
                    gpu=True,
                    verbose=False,
                )
        _EASYOCR_AVAILABLE = True
        print("[OCR] EasyOCR инициализирован")
    except ImportError:
        print("[OCR] EasyOCR не установлен: pip install easyocr")
    except Exception as e:
        print(f"[OCR] EasyOCR ошибка инициализации: {e}")
        try:
            import streamlit as _st
            _st.session_state["ocr_init_error"] = str(e)
        except Exception:
            pass

    # Tesseract fallback
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        _TESSERACT_AVAILABLE = True
        print("[OCR] Tesseract доступен как fallback")
    except Exception:
        pass


def _ocr_image(image) -> str:
    """
    Распознаёт текст из PIL Image.
    Стратегия: EasyOCR → Tesseract → пустая строка.
    """
    # EasyOCR
    if _EASYOCR_AVAILABLE and _easyocr_reader is not None:
        try:
            import numpy as np
            img_array = np.array(image.convert("RGB"))
            result = _easyocr_reader.readtext(img_array, detail=0, paragraph=True)
            return "\n".join(result)
        except Exception as e:
            print(f"[OCR] EasyOCR ошибка: {e}")

    # Tesseract fallback
    if _TESSERACT_AVAILABLE:
        try:
            import pytesseract
            return pytesseract.image_to_string(image, lang="rus+eng")
        except Exception as e:
            print(f"[OCR] Tesseract ошибка: {e}")

    return ""


# =============================================================================
# Извлечение текста по типу файла
# =============================================================================
def _extract_pdf(file_bytes: bytes, filename: str) -> List[Dict]:
    """
    PDF → список страниц [{page, text, method}].
    Сначала пробует извлечь текст напрямую (для текстовых PDF),
    если страница пустая — применяет PaddleOCR.
    """
    pages = []
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for i, page in enumerate(doc, 1):
            # Попытка прямого извлечения текста
            text = page.get_text("text").strip()
            method = "direct"

            # Если текста мало — скан → OCR
            if len(text) < 50:
                try:
                    from PIL import Image
                    pix = page.get_pixmap(dpi=200)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    text = _ocr_image(img)
                    method = "ocr"
                except Exception as e:
                    text = f"[Ошибка OCR стр.{i}: {e}]"
                    method = "error"

            pages.append({
                "page": i,
                "text": text,
                "method": method,
                "word_count": len(text.split()),
            })
        doc.close()
    except ImportError:
        pages.append({"page": 1, "text": "[PyMuPDF не установлен: pip install pymupdf]",
                       "method": "error", "word_count": 0})
    except Exception as e:
        pages.append({"page": 1, "text": f"[Ошибка PDF: {e}]",
                       "method": "error", "word_count": 0})
    return pages


def _extract_docx(file_bytes: bytes) -> List[Dict]:
    """DOCX → список страниц (по 100 строк каждая)."""
    try:
        import docx
        doc = docx.Document(io.BytesIO(file_bytes))
        full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        # Таблицы
        for table in doc.tables:
            for row in table.rows:
                full_text += "\n" + " | ".join(c.text.strip() for c in row.cells)
        return _split_into_pages(full_text, method="direct")
    except ImportError:
        return [{"page": 1, "text": "[python-docx не установлен: pip install python-docx]",
                 "method": "error", "word_count": 0}]
    except Exception as e:
        return [{"page": 1, "text": f"[Ошибка DOCX: {e}]",
                 "method": "error", "word_count": 0}]


def _extract_doc(file_bytes: bytes) -> List[Dict]:
    """DOC (старый формат) → текст через mammoth."""
    try:
        import mammoth
        result = mammoth.extract_raw_text(io.BytesIO(file_bytes))
        return _split_into_pages(result.value, method="direct")
    except ImportError:
        return [{"page": 1, "text": "[mammoth не установлен: pip install mammoth]",
                 "method": "error", "word_count": 0}]
    except Exception as e:
        return [{"page": 1, "text": f"[Ошибка DOC: {e}]",
                 "method": "error", "word_count": 0}]


def _extract_xlsx(file_bytes: bytes) -> List[Dict]:
    """XLSX → текст из всех листов."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        lines = []
        for sheet in wb.worksheets:
            lines.append(f"=== Лист: {sheet.title} ===")
            for row in sheet.iter_rows(values_only=True):
                row_text = " | ".join(str(c) for c in row if c is not None)
                if row_text.strip():
                    lines.append(row_text)
        return _split_into_pages("\n".join(lines), method="direct")
    except ImportError:
        return [{"page": 1, "text": "[openpyxl не установлен: pip install openpyxl]",
                 "method": "error", "word_count": 0}]
    except Exception as e:
        return [{"page": 1, "text": f"[Ошибка XLSX: {e}]",
                 "method": "error", "word_count": 0}]


def _extract_image(file_bytes: bytes) -> List[Dict]:
    """JPG/PNG → OCR."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(file_bytes))
        text = _ocr_image(img)
        return [{"page": 1, "text": text, "method": "ocr",
                 "word_count": len(text.split())}]
    except Exception as e:
        return [{"page": 1, "text": f"[Ошибка изображения: {e}]",
                 "method": "error", "word_count": 0}]


def _split_into_pages(text: str, method: str = "direct", lines_per_page: int = 80) -> List[Dict]:
    """Разбивает длинный текст на условные страницы."""
    lines = text.splitlines()
    pages = []
    for i in range(0, max(1, len(lines)), lines_per_page):
        chunk = "\n".join(lines[i:i + lines_per_page]).strip()
        pages.append({
            "page": len(pages) + 1,
            "text": chunk,
            "method": method,
            "word_count": len(chunk.split()),
        })
    return pages if pages else [{"page": 1, "text": text, "method": method,
                                  "word_count": len(text.split())}]


def extract_text(file_bytes: bytes, filename: str) -> List[Dict]:
    """Универсальный экстрактор — выбирает метод по расширению файла."""
    ext = os.path.splitext(filename.lower())[1]
    if ext == ".pdf":
        return _extract_pdf(file_bytes, filename)
    elif ext == ".docx":
        return _extract_docx(file_bytes)
    elif ext in (".doc",):
        return _extract_doc(file_bytes)
    elif ext in (".xlsx", ".xls"):
        return _extract_xlsx(file_bytes)
    elif ext in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"):
        return _extract_image(file_bytes)
    else:
        return [{"page": 1, "text": f"[Формат {ext} не поддерживается]",
                 "method": "error", "word_count": 0}]


# =============================================================================
# База документов
# =============================================================================
def add_to_db(db: Dict, filename: str, pages: List[Dict]) -> str:
    doc_id = hashlib.md5(
        f"{filename}_{datetime.now().isoformat()}".encode()
    ).hexdigest()[:12]

    full_text = "\n".join(p["text"] for p in pages)
    ocr_pages = sum(1 for p in pages if p.get("method") == "ocr")

    doc = {
        "id": doc_id,
        "filename": filename,
        "pages": pages,
        "full_text": full_text,
        "word_count": sum(p["word_count"] for p in pages),
        "page_count": len(pages),
        "ocr_pages": ocr_pages,
        "processed_at": datetime.now().isoformat(),
    }
    db["documents"].append(doc)
    db["stats"]["total"] = len(db["documents"])
    db["stats"]["last_scan"] = doc["processed_at"]
    return doc_id


# =============================================================================
# Поиск с синонимами
# =============================================================================
def search_documents(db: Dict, query: str) -> List[Dict]:
    """Поиск по всем документам с синонимами через QueryExpander."""
    if not query.strip():
        return []

    # Расширяем запрос синонимами
    search_terms = [query.lower()]
    try:
        from core.query_expander import QueryExpander
        variants = QueryExpander().expand(query)
        # Берём только словарные варианты (без тарифного суффикса)
        search_terms += [
            v.lower() for v in variants
            if v != query and not v.endswith("тарифное регулирование")
        ]
    except Exception:
        pass

    results = []
    for doc in db.get("documents", []):
        matches = []
        for page in doc.get("pages", []):
            page_text = page.get("text", "").lower()
            matched_terms = [t for t in search_terms if t in page_text]
            if matched_terms:
                # Находим контекст вокруг первого совпадения
                term = matched_terms[0]
                idx = page_text.find(term)
                start = max(0, idx - 100)
                end = min(len(page_text), idx + 200)
                context = "..." + page_text[start:end].strip() + "..."
                # Подсвечиваем все совпавшие термины
                for t in matched_terms:
                    context = re.sub(
                        re.escape(t), f"**{t.upper()}**", context, flags=re.IGNORECASE
                    )
                matches.append({
                    "page": page["page"],
                    "context": context,
                    "matched_terms": matched_terms,
                })

        if matches:
            results.append({
                "doc": doc,
                "matches": matches,
                "total_matches": len(matches),
            })

    results.sort(key=lambda x: x["total_matches"], reverse=True)
    return results


# =============================================================================
# Пересказ через LM Studio — Map-Reduce для больших документов
# =============================================================================

# Порог (символов): документы длиннее этого значения обрабатываются чанками
_LARGE_DOC_THRESHOLD = 12_000
# Размер одного чанка (символов) — ~1500 токенов, комфортно для 8B модели
_CHUNK_SIZE          = 6_000
# Перекрытие между чанками — модель не теряет контекст на стыке частей
_CHUNK_OVERLAP       = 300


def _safe_print(msg: str):
    """print(), безопасный для Windows-консоли (cp1252 и др.)."""    
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def _load_lm_config() -> tuple:
    """Возвращает (lm_url, model) из advisor_config.json."""
    config_path = os.path.join("config", "advisor_config.json")
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    lm_url = config.get("lm_studio_url", "http://127.0.0.1:1234/v1")
    model  = config.get("default_model", "qwen/qwen3.5-9b")
    return lm_url, model


def _split_text_chunks(text: str, chunk_size: int = _CHUNK_SIZE,
                       overlap: int = _CHUNK_OVERLAP) -> List[str]:
    """
    Разбивает текст на чанки по границам абзацев/предложений.
    Перекрытие `overlap` сохраняет контекст между соседними частями.

    Ключевое правило: разделитель принимается только если он находится
    в ПОСЛЕДНЕЙ ЧЕТВЕРТИ окна [start..end] — иначе чанк получался бы
    крошечным (когда \n\n стоит близко к началу диапазона rfind).
    """
    if len(text) <= chunk_size:
        return [text]

    chunks, start = [], 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            tail = text[start:].strip()
            if tail:
                chunks.append(tail)
            break

        # Ищем точку разрыва в последних 25% окна, чтобы чанк
        # был не короче 75% chunk_size
        min_pos = start + chunk_size * 3 // 4
        split_end = end
        for sep in ('\n\n', '. ', ' '):
            pos = text.rfind(sep, min_pos, end)
            if pos != -1:
                split_end = pos + len(sep)
                break

        chunk = text[start:split_end].strip()
        if chunk:
            chunks.append(chunk)

        # Следующий чанк начинается с перекрытием, но не раньше split_end//2
        next_start = split_end - overlap
        start = max(start + chunk_size // 2, next_start)

    return chunks


def _lm_call(client, model: str, system: str, user: str, max_tokens: int) -> str:
    """Один вызов LM Studio; возвращает текст ответа или строку с ошибкой."""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        return f"[Ошибка LM: {e}]"


def summarize_document(text: str, length: str = "средний", model: str = None, _progress_cb=None) -> str:
    """
    Пересказывает текст через LM Studio.

    Для коротких документов (< _LARGE_DOC_THRESHOLD символов) — один запрос.
    Для длинных — Map-Reduce:
      MAP    : каждый чанк (~6 000 симв.) суммаризируется отдельно
      REDUCE : мини-резюме объединяются в итоговый пересказ

    """
    # length может быть строкой-ключом ИЛИ числом слов (int/str с цифрой)
    # Строковые ключи → фиксированные пресеты
    _PRESETS = {
        "1 страница":  (500,  "примерно на 1 страницу (~250 слов). Только самое главное"),
        "2 страницы":  (1000, "примерно на 2 страницы (~500 слов). Ключевые факты и детали"),
        "5 страниц":   (2500, "примерно на 5 страниц (~1250 слов). Подробно, все важные пункты"),
        "10 страниц":  (5000, "примерно на 10 страниц (~2500 слов). Максимально подробно"),
        # Обратная совместимость со старыми ключами
        "краткий":     (400,  "кратко, в 3-5 предложениях, только главное"),
        "средний":     (1000, "в 1-2 абзацах с ключевыми деталями (~500 слов)"),
        "подробный":   (2500, "подробно, с перечислением всех важных пунктов (~1000 слов)"),
    }
    # Числовой режим: length == "NNN слов" или int
    _word_target = None
    try:
        _word_target = int(str(length).replace("слов", "").replace("слова", "").strip())
    except (ValueError, TypeError):
        pass

    if _word_target:
        # ~1.5 токена на слово для русского текста
        max_tokens  = int(_word_target * 1.7)
        instruction = f"ровно примерно {_word_target} слов. Следи за объёмом"
    else:
        max_tokens, instruction = _PRESETS.get(length, _PRESETS["2 страницы"])


    try:
        from openai import OpenAI
        lm_url, default_model = _load_lm_config()
        model  = model or default_model
        client = OpenAI(base_url=lm_url, api_key="lm-studio", timeout=180.0)

        system_msg = "Ты помощник для анализа документов. Отвечаешь кратко и по делу на русском языке."

        # ── Короткий документ: один запрос ───────────────────────────────
        if len(text) <= _LARGE_DOC_THRESHOLD:
            prompt = (
                f"Перескажи содержание документа {instruction}. "
                f"Отвечай на русском языке, без лишних вступлений.\n\n"
                f"ДОКУМЕНТ:\n{text}"
            )
            result = _lm_call(client, model, system_msg, prompt, max_tokens)
            return result

        # ── Большой документ: Map-Reduce ─────────────────────────────────
        chunks   = _split_text_chunks(text)
        total    = len(chunks)
        t_start  = time.perf_counter()
        _safe_print(f"[MAP-REDUCE] {len(text)} chars -> {total} chunks, model: {model}")

        # MAP: резюме каждого чанка
        mini_summaries = []
        chunk_times    = []   # храним время каждого чанка для ETA

        for i, chunk in enumerate(chunks, 1):
            t_chunk = time.perf_counter()

            # Статус перед запросом
            elapsed  = t_chunk - t_start
            elapsed_fmt = f"{int(elapsed // 60)}м {int(elapsed % 60)}с"
            if chunk_times:
                avg_sec  = sum(chunk_times) / len(chunk_times)
                remaining = avg_sec * (total - i + 1)
                eta_fmt  = f"{int(remaining // 60)}м {int(remaining % 60)}с"
                eta_str  = f"ещё ~{eta_fmt}"
            else:
                eta_str  = "считаю время..."

            if _progress_cb:
                _pct = (i - 1) / (total + 1)
                _progress_cb(_pct, f"MAP — часть {i} / {total} | прошло: {elapsed_fmt} | {eta_str}")

            map_prompt = (
                f"Это часть {i} из {total} одного документа.\n"
                f"Сделай краткое резюме этой части (3-5 пунктов), "
                f"сохрани все факты, цифры, даты, названия.\n"
                f"Отвечай на русском, без вступлений.\n\n"
                f"ЧАСТЬ ДОКУМЕНТА:\n{chunk}"
            )
            mini = _lm_call(client, model, system_msg, map_prompt, 400)

            chunk_done = time.perf_counter()
            chunk_times.append(chunk_done - t_chunk)

            if mini.startswith("[Ошибка"):
                _safe_print(f"[MAP] chunk {i} error: {mini}")
                mini = f"[Часть {i}: данные недоступны]"

            mini_summaries.append(f"=== Часть {i}/{total} ===\n{mini}")
            _safe_print(f"[MAP] chunk {i}/{total} done ({len(mini)} chars, {chunk_times[-1]:.1f}s)")

        # REDUCE: финальный синтез
        elapsed_map = time.perf_counter() - t_start
        elapsed_fmt = f"{int(elapsed_map // 60)}м {int(elapsed_map % 60)}с"
        _safe_print(f"[MAP-REDUCE] REDUCE start, map took {elapsed_fmt}")
        if _progress_cb:
            _progress_cb(total / (total + 1), f"REDUCE — финальный синтез {total} частей | MAP: {elapsed_fmt}")

        combined = "\n\n".join(mini_summaries)
        reduce_prompt = (
            f"Ниже — резюме отдельных частей одного документа. "
            f"Создай единый итоговый пересказ {instruction}.\n"
            f"Объедини все части, устрани дублирование, сохрани все ключевые факты, "
            f"цифры, даты, названия документов.\n"
            f"Отвечай на русском, без вступлений и мета-комментариев.\n\n"
            f"РЕЗЮМЕ ЧАСТЕЙ:\n{combined}"
        )
        result = _lm_call(client, model, system_msg, reduce_prompt, max_tokens)

        total_fmt = f"{int((time.perf_counter()-t_start)//60)}м {int((time.perf_counter()-t_start)%60)}с"
        _safe_print(f"[MAP-REDUCE] done in {total_fmt}. result: {len(result)} chars")
        return result

    except Exception as e:
        return f"[Ошибка пересказа: {e}]"


# =============================================================================
# Экспорт результатов
# =============================================================================
def export_txt(doc: Dict) -> bytes:
    """Экспорт в TXT."""
    _pc = doc.get("page_count") or len(doc.get("pages", []))
    lines = [
        f"Документ: {_fname(doc)}",
        f"Обработан: {(doc.get('processed_at') or '')[:16]}",
        f"Страниц: {_pc} | Слов: {doc.get('word_count', 0)}",
        "=" * 60,
        "",
    ]
    for page in doc.get("pages", []):
        lines.append(f"--- Страница {page.get('page',1)} ({page.get('method','')}) ---")
        lines.append(page.get("text", ""))
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def export_docx(doc: Dict, summary: str = None) -> io.BytesIO:
    """Экспорт в DOCX."""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH

        d = Document()
        _pc = doc.get("page_count") or len(doc.get("pages", []))

        h = d.add_heading(_fname(doc), level=1)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

        d.add_paragraph(f"Обработан: {(doc.get('processed_at') or '')[:16]}")
        d.add_paragraph(f"Страниц: {_pc} | Слов: {doc.get('word_count', 0)}")
        d.add_paragraph(f"OCR-страниц: {doc.get('ocr_pages', 0)}")

        if summary:
            d.add_heading("Краткое содержание", level=2)
            d.add_paragraph(summary)

        d.add_heading("Текст документа", level=2)
        for page in doc.get("pages", []):
            d.add_heading(f"Страница {page.get('page',1)}", level=3)
            d.add_paragraph(page.get("text", ""))

        buf = io.BytesIO()
        d.save(buf)
        buf.seek(0)
        return buf
    except ImportError:
        raise RuntimeError("python-docx не установлен: pip install python-docx")


# =============================================================================
# UI — главная страница сканера
# =============================================================================
def show_doc_scanner():
    st.header("📸 AI-Сканер документов")
    st.caption("EasyOCR · PDF · DOCX · DOC · XLSX · JPG · PNG · Пересказ · Поиск")

    # Инициализация OCR один раз
    if "ocr_initialized" not in st.session_state:
        with st.spinner("⚙️ Инициализация OCR..."):
            _init_ocr()
        st.session_state["ocr_initialized"] = True

    # Статус OCR — показываем что активно
    if _EASYOCR_AVAILABLE:
        st.success("✅ OCR: EasyOCR активен", icon="🔍")
    elif _TESSERACT_AVAILABLE:
        st.warning("⚠️ OCR: PaddleOCR недоступен, используется Tesseract (хуже качество). Установите: `pip install paddlepaddle paddleocr`")
    else:
        _ocr_err = st.session_state.get("ocr_init_error")
        if _ocr_err:
            st.error(f"❌ OCR ошибка инициализации:\n```\n{_ocr_err}\n```")
        else:
            st.error("❌ OCR не найден. Установите: `pip install paddlepaddle paddleocr pillow`")

    # Загрузка базы
    if "scanner_db" not in st.session_state:
        st.session_state["scanner_db"] = load_db()

    db = st.session_state["scanner_db"]

    # ── Вкладки ──────────────────────────────────────────────────────────
    tab_scan, tab_search, tab_docs = st.tabs([
        "📤 Загрузка и распознавание",
        "🔍 Поиск по содержимому",
        "📚 База документов",
    ])

    # =========================================================================
    # Вкладка 1 — Загрузка и распознавание
    # =========================================================================
    with tab_scan:
        st.subheader("Загрузка файлов")

        SUPPORTED = ["pdf", "docx", "doc", "xlsx", "xls", "jpg", "jpeg", "png", "bmp", "tiff"]
        uploaded = st.file_uploader(
            "Перетащите файлы или выберите из папки",
            type=SUPPORTED,
            accept_multiple_files=True,
            help="Поддерживаются: PDF (текстовые и сканы), DOCX, DOC, XLSX, JPG, PNG",
        )

        if uploaded:
            st.info(f"📎 Загружено файлов: **{len(uploaded)}**")

            # Кнопка запуска
            if st.button("🚀 Запустить распознавание", type="primary", key="scan_btn"):
                progress = st.progress(0)
                status   = st.empty()
                new_ids  = []

                for i, f in enumerate(uploaded):
                    status.text(f"⏳ Обрабатываю {f.name} ({i+1}/{len(uploaded)})...")
                    progress.progress((i) / len(uploaded))

                    file_bytes = f.read()
                    t0 = time.perf_counter()
                    pages = extract_text(file_bytes, f.name)
                    elapsed = time.perf_counter() - t0

                    doc_id = add_to_db(db, f.name, pages)
                    new_ids.append(doc_id)

                    ocr_count = sum(1 for p in pages if p.get("method") == "ocr")
                    words     = sum(p["word_count"] for p in pages)
                    st.success(
                        f"✅ **{f.name}** — {len(pages)} стр., {words} слов, "
                        f"{ocr_count} OCR-стр., {elapsed:.1f} сек"
                    )

                progress.progress(1.0)
                status.text("✅ Все файлы обработаны!")
                save_db(db)
                st.session_state["scanner_db"] = db
                st.session_state["last_scanned_ids"] = new_ids

        # ── Результаты последней обработки ──────────────────────────────
        last_ids = st.session_state.get("last_scanned_ids", [])
        if last_ids:
            st.divider()
            st.subheader("📄 Результаты распознавания")

            last_docs = [d for d in db["documents"] if d["id"] in last_ids]

            for doc in last_docs:
                _pc2 = doc.get("page_count") or len(doc.get("pages", []))
                _wc2 = doc.get("word_count", 0)
                with st.expander(f"📄 {_fname(doc)} — {_pc2} стр., {_wc2} слов"):

                    # ── Навигация по страницам ───────────────────────────
                    pages_list = doc.get("pages", [])
                    page_key   = f"page_idx_{doc['id']}"
                    if page_key not in st.session_state:
                        st.session_state[page_key] = 0

                    cur_idx  = st.session_state[page_key]
                    cur_idx  = max(0, min(cur_idx, len(pages_list) - 1))
                    cur_page = pages_list[cur_idx] if pages_list else {}

                    # Навигационная панель
                    nav1, nav2, nav3 = st.columns([1, 4, 1])
                    with nav1:
                        if st.button("◀ Пред.", key=f"prev_btn_{doc['id']}",
                                     disabled=(cur_idx == 0)):
                            st.session_state[page_key] = cur_idx - 1
                            st.rerun()
                    with nav2:
                        st.markdown(
                            f"<div style='text-align:center; padding-top:6px;'>"
                            f"Страница <b>{cur_idx+1}</b> из <b>{len(pages_list)}</b> "
                            f"<span style='color:grey;font-size:0.85em;'>"
                            f"({cur_page.get('method','')} · "
                            f"{cur_page.get('word_count') or len(cur_page.get('text','').split())} слов)"
                            f"</span></div>",
                            unsafe_allow_html=True,
                        )
                    with nav3:
                        if st.button("След. ▶", key=f"next_btn_{doc['id']}",
                                     disabled=(cur_idx >= len(pages_list) - 1)):
                            st.session_state[page_key] = cur_idx + 1
                            st.rerun()

                    # Текст текущей страницы — уменьшенный шрифт
                    st.markdown(
                        f"<div style='font-size:0.87em; line-height:1.55; "
                        f"background:#f8f9fa; border:1px solid #e0e0e0; "
                        f"border-radius:6px; padding:12px 14px; "
                        f"max-height:320px; overflow-y:auto; "
                        f"white-space:pre-wrap; word-break:break-word;'>"
                        f"{cur_page.get('text','').replace('<','&lt;').replace('>','&gt;')}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

                    st.divider()

                    # ── Пересказ ─────────────────────────────────────────
                    summary_key    = f"summary_{doc['id']}"
                    sum_mode_key   = f"sum_mode_{doc['id']}"
                    sum_words_key  = f"sum_words_{doc['id']}"

                    st.markdown("**📝 Пересказ**")

                    # Выбор объёма — вертикально (без колонок, иначе Streamlit
                    # рендерит пересказ внутри одного из столбцов)
                    sum_mode = st.selectbox(
                        "Объём пересказа",
                        ["1 страница", "2 страницы", "5 страниц", "10 страниц",
                         "Точное кол-во слов"],
                        index=1,
                        key=sum_mode_key,
                    )
                    if sum_mode == "Точное кол-во слов":
                        sum_words = st.number_input(
                            "Количество слов",
                            min_value=50, max_value=5000, value=300, step=50,
                            key=sum_words_key,
                        )
                        summary_length = str(int(sum_words))
                    else:
                        summary_length = sum_mode

                    # Кнопка строго по центру
                    _bL, _bC, _bR = st.columns([2, 3, 2])
                    with _bC:
                        _do_summary = st.button(
                            "▶ Сгенерировать пересказ",
                            key=f"sum_btn_{doc['id']}", type="primary",
                            use_container_width=True,
                        )

                    if _do_summary:
                        _full_text = doc.get("full_text", "")
                        _n_chunks  = max(1, len(_full_text) // _CHUNK_SIZE + 1)
                        _is_large  = len(_full_text) > _LARGE_DOC_THRESHOLD

                        if _is_large:
                            st.info(
                                f"📄 Документ **{len(_full_text):,}** символов — "
                                f"Map-Reduce: ~**{_n_chunks}** частей. "
                                f"Ориентировочно 1-3 минуты."
                            )

                        _spinner_msg = (
                            f"🤖 Map-Reduce: {_n_chunks} частей..."
                            if _is_large else "🤖 Генерирую пересказ..."
                        )

                        # st.spinner() — гарантированно виден во время
                        # блокирующей операции. Внутри него запускаем
                        # summarize_document в потоке, а главный поток
                        # обновляет прогресс-бар каждую секунду через
                        # while-loop (time.sleep даёт Streamlit слот
                        # для отправки обновлений браузеру).
                        _state = {
                            "done": False, "result": None,
                            "pct": 0.0,   "msg": "⏳ Инициализация..."
                        }

                        def _run_in_thread():
                            def _cb(pct, msg):
                                _state["pct"] = pct
                                _state["msg"] = msg
                            _state["result"] = summarize_document(
                                _full_text, summary_length, _progress_cb=_cb
                            )
                            _state["done"] = True

                        _thread = threading.Thread(target=_run_in_thread, daemon=True)
                        _thread.start()

                        with st.spinner(_spinner_msg):
                            _ph = st.empty()
                            _t0 = time.perf_counter()
                            while not _state["done"]:
                                _el  = time.perf_counter() - _t0
                                _em  = f"{int(_el//60)}м {int(_el%60)}с"
                                _pct = int(min(_state["pct"], 1.0) * 100)
                                _ph.markdown(
                                    f"<div style='font-family:sans-serif;padding:4px 0'>"
                                    f"<div style='background:#e8e8e8;border-radius:6px;"
                                    f"height:10px;margin-bottom:6px'>"
                                    f"<div style='background:#4c8ef5;width:{_pct}%;"
                                    f"height:10px;border-radius:6px'></div></div>"
                                    f"<div style='font-size:0.85em;color:#444'>"
                                    f"{_state['msg']}</div>"
                                    f"<div style='font-size:0.78em;color:#888;margin-top:2px'>"
                                    f"прошло: {_em}</div></div>",
                                    unsafe_allow_html=True,
                                )
                                time.sleep(1)
                            _thread.join()
                            _ph.empty()

                        st.session_state[summary_key] = _state["result"]


                    # ── Отображение пересказа ─────────────────────────────
                    _summary_text = st.session_state.get(summary_key)
                    if _summary_text:
                        import streamlit.components.v1 as _stc
                        _wc = len(_summary_text.split())

                        # Заголовок со счётчиком слов
                        st.markdown(
                            "<div style='display:flex;align-items:center;"
                            "justify-content:space-between;margin-bottom:4px;'>"
                            "<span style='font-weight:600;'>🤖 Пересказ</span>"
                            f"<span style='color:#888;font-size:0.85em;'>{_wc} слов</span>"
                            "</div>",
                            unsafe_allow_html=True,
                        )

                        # Текст в одном прокручиваемом блоке
                        _sum_h = max(200, min(600, _wc * 6))
                        _body  = _summary_text.replace("<", "&lt;").replace(">", "&gt;")
                        st.markdown(
                            f"<div style='font-size:0.87em;line-height:1.65;"
                            f"background:#f0f4f8;border:1px solid #d0d8e4;"
                            f"border-radius:6px;padding:14px 16px;"
                            f"max-height:{_sum_h}px;overflow-y:auto;"
                            f"white-space:pre-wrap;word-break:break-word;'>"
                            f"{_body}</div>",
                            unsafe_allow_html=True,
                        )

                        # Кнопка копирования через iframe-компонент.
                        # Текст хранится в скрытом <span> — кнопка читает textContent.
                        # Безопаснее inline JS с backtick-строкой.
                        _safe = (
                            _summary_text
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;")
                            .replace('"', "&quot;")
                        )
                        _stc.html(
                            "<div style='font-family:sans-serif'>"
                            f"<span id='t' style='display:none'>{_safe}</span>"
                            "<button onclick=\"navigator.clipboard.writeText("
                            "document.getElementById('t').textContent).then(()=>{"
                            "this.textContent='✅ Скопировано';"
                            "setTimeout(()=>this.textContent='📋 Скопировать пересказ',2000)})\" "
                            "style='font-size:13px;padding:5px 16px;cursor:pointer;"
                            "border:1px solid #d0d8e4;border-radius:5px;"
                            "background:#fff;color:#333;margin-top:6px'>"
                            "📋 Скопировать пересказ</button></div>",
                            height=48,
                        )
                    st.divider()

                    # ── Экспорт ──────────────────────────────────────────
                    st.markdown("**💾 Сохранить результат:**")
                    exp_col1, exp_col2 = st.columns(2)

                    with exp_col1:
                        txt_data = export_txt(doc)
                        st.download_button(
                            label="📄 Скачать TXT",
                            data=txt_data,
                            file_name=f"{os.path.splitext(_fname(doc))[0]}_распознан.txt",
                            mime="text/plain",
                            width="stretch",
                            key=f"dl_txt_{doc['id']}",
                        )

                    with exp_col2:
                        try:
                            summary_text = st.session_state.get(f"summary_{doc['id']}")
                            docx_buf = export_docx(doc, summary=summary_text)
                            st.download_button(
                                label="📝 Скачать DOCX",
                                data=docx_buf,
                                file_name=f"{os.path.splitext(_fname(doc))[0]}_распознан.docx",
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                width="stretch",
                                key=f"dl_docx_{doc['id']}",
                            )
                        except Exception as e:
                            st.warning(f"DOCX недоступен: {e}")

    # =========================================================================
    # Вкладка 2 — Поиск
    # =========================================================================
    with tab_search:
        st.subheader("🔍 Поиск по содержимому")

        if not db.get("documents"):
            st.info("📭 Сначала распознайте хотя бы один документ на вкладке «Загрузка».")
        else:
            search_col, btn_col = st.columns([5, 1])
            with search_col:
                search_query = st.text_input(
                    "Введите запрос",
                    placeholder="например: неподконтрольные расходы, ДМС, НДС",
                    key="search_input",
                    label_visibility="collapsed",
                )
            with btn_col:
                do_search = st.button("🔍 Найти", type="primary", key="search_exec_btn", width="stretch")

            if do_search and search_query.strip():
                results = search_documents(db, search_query)
                st.session_state["search_results"] = results
                st.session_state["search_query"]   = search_query

            results = st.session_state.get("search_results", [])
            query   = st.session_state.get("search_query", "")

            if results:
                st.success(f"Найдено совпадений в **{len(results)}** документах по запросу: _{query}_")
                for res in results:
                    doc = res["doc"]
                    with st.expander(
                        f"📄 {_fname(doc)} — {res['total_matches']} совпадений",
                        expanded=(res == results[0]),
                    ):
                        for match in res["matches"]:
                            st.markdown(f"**Стр. {match['page']}:** {match['context']}")
                            st.caption(f"Найдено: {', '.join(match['matched_terms'])}")
                            st.divider()
            elif do_search and search_query.strip():
                st.warning("🔎 Совпадений не найдено. Попробуйте другой запрос.")

    # =========================================================================
    # Вкладка 3 — База документов
    # =========================================================================
    with tab_docs:
        st.subheader("📚 База распознанных документов")

        docs = db.get("documents", [])
        stats = db.get("stats", {})

        # Метрики
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("📊 Документов", stats.get("total", 0))
        m2.metric("📄 Страниц всего", sum(d.get("page_count", 0) for d in docs))
        m3.metric("🔤 Слов всего", f"{sum(d.get('word_count', 0) for d in docs):,}")
        m4.metric("🕐 Последний", (stats.get("last_scan") or "—")[:10])

        if docs:
            import pandas as pd
            df = pd.DataFrame([
                {
                    "Файл": _fname(d),
                    "Стр.": d.get("page_count") or len(d.get("pages", [])),
                    "Слов": d.get("word_count", 0),
                    "OCR":  d.get("ocr_pages", 0),
                    "Дата": (d.get("processed_at") or d.get("scan_date") or "—")[:16],
                }
                for d in reversed(docs)
            ])
            st.dataframe(df, width="stretch", hide_index=True)

            st.divider()

            # Просмотр конкретного документа
            doc_names = {d["id"]: _fname(d) for d in docs}
            selected_id = st.selectbox(
                "Просмотреть документ",
                options=list(doc_names.keys()),
                format_func=lambda x: doc_names[x],
                key="db_doc_select",
            )

            if selected_id:
                sel_doc = next((d for d in docs if d["id"] == selected_id), None)
                if sel_doc:
                    _pc = sel_doc.get("page_count") or len(sel_doc.get("pages", []))
                    _wc = sel_doc.get("word_count", 0)
                    _fn = _fname(sel_doc)
                    st.markdown(f"**{_fn}** · {_pc} стр. · {_wc} слов")
                    for page in sel_doc.get("pages", []):
                        _pw = page.get("word_count") or len(page.get("text","").split())
                        with st.expander(f"Страница {page.get('page',1)} ({page.get('method','')}) — {_pw} слов"):
                            st.text_area("Текст", page.get("text",""), height=200, disabled=True,
                                         key=f"db_view_{selected_id}_{page.get('page',1)}")

                    # Экспорт из базы
                    db_exp1, db_exp2 = st.columns(2)
                    with db_exp1:
                        st.download_button(
                            "📄 Скачать TXT",
                            data=export_txt(sel_doc),
                            file_name=f"{os.path.splitext(_fn)[0]}.txt",
                            mime="text/plain",
                            key=f"db_dl_txt_{selected_id}",
                        )
                    with db_exp2:
                        try:
                            docx_buf = export_docx(sel_doc)
                            st.download_button(
                                "📝 Скачать DOCX",
                                data=docx_buf,
                                file_name=f"{os.path.splitext(sel__fname(doc))[0]}.docx",
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                key=f"db_dl_docx_{selected_id}",
                            )
                        except Exception as e:
                            st.warning(str(e))

            st.divider()

            # Очистка базы
            if st.button("🗑️ Очистить базу сканов", type="secondary", key="clear_scan_db"):
                st.session_state["scanner_db"] = {
                    "documents": [], "stats": {"total": 0, "last_scan": None}
                }
                save_db(st.session_state["scanner_db"])
                st.session_state.pop("last_scanned_ids", None)
                st.session_state.pop("search_results", None)
                st.success("✅ База очищена")
                st.rerun()
        else:
            st.info("📭 База пуста. Загрузите документы на вкладке «Загрузка».")