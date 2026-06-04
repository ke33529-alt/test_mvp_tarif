# core/advisor_history.py
"""
Персистентная история советчика.

Формат хранения: JSONL, одна запись = один вопрос + ответ + уточнения.
Файл: data/advisor_history/history.jsonl

Запись:
{
    "id":             "20240604_143022_481234",   # уникальный ID
    "ts":             "2024-06-04T14:30:22",      # ISO-datetime
    "date":           "2024-06-04",               # для фильтрации по дате
    "query":          "...",
    "answer":         "...",
    "model":          "qwen/qwen3.5-9b",
    "spheres":        ["🔥 Теплоснабжение"],
    "sources":        [...],
    "from_faq":       false,
    "clarifications": [{"query": "...", "answer": "...", "sources": [...]}, ...]
}
"""
import os
import json
from datetime import datetime
from typing import List, Dict, Optional

# =============================================================================
# Путь к файлу истории
# =============================================================================
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DIR  = os.path.join(BASE_DIR, "data", "advisor_history")
HISTORY_FILE = os.path.join(HISTORY_DIR, "history.jsonl")


def _ensure_dir():
    os.makedirs(HISTORY_DIR, exist_ok=True)


# =============================================================================
# Генерация ID
# =============================================================================
def _make_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


# =============================================================================
# Сохранение одной записи
# =============================================================================
def save_entry(
    query: str,
    answer: str,
    model: str = "",
    spheres: list = None,
    sources: list = None,
    from_faq: bool = False,
    clarifications: list = None,
    entry_id: str = None,
) -> str:
    """
    Записывает запрос+ответ в JSONL.
    Возвращает ID записи (для обновления уточнений).
    """
    _ensure_dir()
    now = datetime.now()
    eid = entry_id or _make_id()

    # Источники — хранить только нужные поля (без лишних данных)
    def _slim_sources(srcs):
        if not srcs:
            return []
        return [
            {
                "file":     s.get("file", ""),
                "page":     s.get("page", ""),
                "sphere":   s.get("sphere", ""),
                "snippet":  s.get("snippet", "")[:300],
            }
            for s in srcs
        ]

    def _slim_clars(clars):
        if not clars:
            return []
        return [
            {
                "query":   c.get("query", ""),
                "answer":  c.get("answer", ""),
                "sources": _slim_sources(c.get("sources", [])),
            }
            for c in clars
        ]

    entry = {
        "id":             eid,
        "ts":             now.isoformat(timespec="seconds"),
        "date":           now.strftime("%Y-%m-%d"),
        "query":          query,
        "answer":         answer,
        "model":          model or "",
        "spheres":        spheres or [],
        "sources":        _slim_sources(sources),
        "from_faq":       bool(from_faq),
        "clarifications": _slim_clars(clarifications),
    }

    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return eid


# =============================================================================
# Обновление уточнений у существующей записи
# =============================================================================
def update_clarifications(entry_id: str, clarifications: list):
    """
    Перезаписывает файл, обновляя уточнения у записи с entry_id.
    Вызывается при каждом добавлении уточнения.
    """
    _ensure_dir()
    if not os.path.exists(HISTORY_FILE):
        return

    lines = []
    updated = False
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                lines.append(line)
                continue
            if rec.get("id") == entry_id:
                rec["clarifications"] = [
                    {
                        "query":   c.get("query", ""),
                        "answer":  c.get("answer", ""),
                        "sources": [
                            {
                                "file":    s.get("file", ""),
                                "page":    s.get("page", ""),
                                "sphere":  s.get("sphere", ""),
                                "snippet": s.get("snippet", "")[:300],
                            }
                            for s in c.get("sources", [])
                        ],
                    }
                    for c in clarifications
                ]
                line = json.dumps(rec, ensure_ascii=False)
                updated = True
            lines.append(line)

    if updated:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# =============================================================================
# Загрузка всех записей (новые сверху)
# =============================================================================
def load_all() -> List[Dict]:
    """Возвращает список записей, отсортированных от новых к старым."""
    _ensure_dir()
    if not os.path.exists(HISTORY_FILE):
        return []
    records = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return list(reversed(records))


# =============================================================================
# Поиск по истории
# =============================================================================
def search_history(
    records: List[Dict],
    query: str,
    match_type: str = "По словам",   # "Точное" | "По словам"
    scope: str = "Везде",            # "Вопрос" | "Ответ" | "Везде"
    date_from: Optional[str] = None, # "YYYY-MM-DD"
    date_to: Optional[str] = None,   # "YYYY-MM-DD"
    sphere: str = "",
) -> List[Dict]:
    """
    Фильтрует записи по параметрам.
    Возвращает список совпадающих записей, каждая дополнена полем 'match_snippets'.
    """
    q = query.strip().lower()

    def _text_matches(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        if match_type == "Точное":
            return q in t
        # По словам — все слова должны присутствовать
        words = q.split()
        return all(w in t for w in words)

    def _get_search_text(rec: Dict) -> str:
        parts = []
        if scope in ("Вопрос", "Везде"):
            parts.append(rec.get("query", ""))
            for c in rec.get("clarifications", []):
                parts.append(c.get("query", ""))
        if scope in ("Ответ", "Везде"):
            parts.append(rec.get("answer", ""))
            for c in rec.get("clarifications", []):
                parts.append(c.get("answer", ""))
        return " ".join(parts)

    def _make_snippet(text: str, max_len: int = 200) -> str:
        """Возвращает фрагмент вокруг первого вхождения запроса."""
        t = text.lower()
        pos = t.find(q) if match_type == "Точное" else -1
        if pos == -1 and match_type == "По словам":
            for w in q.split():
                pos = t.find(w)
                if pos != -1:
                    break
        if pos == -1:
            return text[:max_len] + ("..." if len(text) > max_len else "")
        start = max(0, pos - 60)
        end   = min(len(text), pos + max_len)
        snippet = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
        return snippet

    results = []
    for rec in records:
        # Фильтр по дате
        rec_date = rec.get("date", "")
        if date_from and rec_date < date_from:
            continue
        if date_to and rec_date > date_to:
            continue

        # Фильтр по сфере
        if sphere:
            rec_spheres = " ".join(rec.get("spheres", [])).lower()
            if sphere.lower() not in rec_spheres:
                continue

        # Текстовый поиск (если запрос задан)
        if q:
            search_text = _get_search_text(rec)
            if not _text_matches(search_text):
                continue
            snippet = _make_snippet(_get_search_text(rec))
        else:
            snippet = rec.get("query", "")[:200]

        results.append({**rec, "_snippet": snippet})

    return results


# =============================================================================
# Удаление записи по ID
# =============================================================================
def delete_entry(entry_id: str):
    """Удаляет запись из файла по ID."""
    if not os.path.exists(HISTORY_FILE):
        return
    lines = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("id") == entry_id:
                    continue
            except Exception:
                pass
            lines.append(line)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# =============================================================================
# Статистика
# =============================================================================
def get_stats(records: List[Dict]) -> Dict:
    if not records:
        return {"total": 0, "today": 0, "with_clarifications": 0, "dates": []}
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "total":              len(records),
        "today":              sum(1 for r in records if r.get("date") == today),
        "with_clarifications": sum(1 for r in records if r.get("clarifications")),
        "oldest_date":        records[-1].get("date", ""),
    }