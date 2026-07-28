# core/entity_picker.py
"""
Агрегатор «последних записей» из других модулей — для модалки завершения
задачи (core/tasks.complete_task): пользователь может привязать к
выполненной задаче конкретную запись из истории Советчика, Протоколов,
Прогнозиста, Заявок или Сканера документов.

Модуль read-only и полностью изолирован: не импортирует streamlit_pages.*
(чтобы не тянуть за собой тяжёлые инициализации вроде OCR), а читает файлы
хранилищ напрямую по путям, задокументированным в соответствующих модулях.
Исключение — core.advisor_history и core.claim_registry: это уже готовые
core-модули с чистым API, их переиспользуем напрямую.

Любая ошибка (файла нет, битый JSON, модуль недоступен) → пустой список,
а не исключение: пикер должен деградировать незаметно для пользователя.
"""

import os
import json
from typing import Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCE_ADVISOR   = "advisor"
SOURCE_PROTOCOL  = "protocol"
SOURCE_PREDICTOR = "predictor"
SOURCE_CLAIM     = "claim"
SOURCE_SCANNER   = "scanner"

SOURCE_LABELS = {
    SOURCE_ADVISOR:   "Советчик",
    SOURCE_PROTOCOL:  "Протоколы",
    SOURCE_PREDICTOR: "Прогнозист решений",
    SOURCE_CLAIM:     "Заявки",
    SOURCE_SCANNER:   "Сканер документов",
}
SOURCE_ORDER = [SOURCE_ADVISOR, SOURCE_PROTOCOL, SOURCE_PREDICTOR, SOURCE_CLAIM, SOURCE_SCANNER]


def _safe_json(path: str) -> Optional[Dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _list_advisor(user: Dict, limit: int) -> List[Dict]:
    """История Советчика — только своя (core/advisor_history.py, файл на пользователя)."""
    try:
        from core.advisor_history import load_all
        entries = load_all(
            scope="user",
            org_id=user.get("org_id", ""),
            user_id=user.get("user_id", ""),
        )
    except Exception:
        return []
    out = []
    for e in entries[:limit]:
        query = (e.get("query", "") or "").strip()
        ts = (e.get("ts", "") or "")[:16].replace("T", " ")
        label = (query[:70] + ("…" if len(query) > 70 else "")) or "(без текста)"
        if ts:
            label = f"{ts} · {label}"
        out.append({"id": e.get("id", ""), "label": label})
    return out


def _list_protocol(user: Dict, limit: int) -> List[Dict]:
    """
    База протоколов (streamlit_pages/protocol_bot.py) — общий JSON-индекс,
    без разделения по пользователям/сегментам (как и сам модуль).
    Путь: data/protocol_bot/protocols_db.json, ключ "protocols".
    """
    path = os.path.join(_ROOT, "data", "protocol_bot", "protocols_db.json")
    db = _safe_json(path)
    if not db:
        return []
    protos = db.get("protocols", [])
    protos = sorted(protos, key=lambda p: p.get("created_at", ""), reverse=True)
    out = []
    for p in protos[:limit]:
        name = p.get("meeting_name") or "Протокол без названия"
        date = p.get("meeting_date", "")
        org = p.get("organization", "")
        bits = [b for b in [name, date, org] if b]
        out.append({"id": p.get("id", ""), "label": " · ".join(bits)})
    return out


def _list_predictor(user: Dict, limit: int) -> List[Dict]:
    """
    Реестр прогнозов (streamlit_pages/predictor.py) — jsonl-файлы с ротацией
    по 1000 записей: data/predictor/registry_NNNN.jsonl. Записи без
    стабильного id — идентифицируем по (файл, номер_строки).
    """
    reg_dir = os.path.join(_ROOT, "data", "predictor")
    if not os.path.isdir(reg_dir):
        return []
    try:
        files = sorted(
            [f for f in os.listdir(reg_dir) if f.startswith("registry_") and f.endswith(".jsonl")],
            reverse=True,
        )
    except Exception:
        return []
    out: List[Dict] = []
    for fname in files:
        if len(out) >= limit:
            break
        path = os.path.join(reg_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
        except Exception:
            continue
        for i, line in enumerate(reversed(lines)):
            if len(out) >= limit:
                break
            try:
                rec = json.loads(line)
            except Exception:
                continue
            article = rec.get("article", "") or "(без статьи)"
            ts = (rec.get("timestamp", "") or "")[:16].replace("T", " ")
            label = f"{ts} · {article}" if ts else article
            rec_id = f"{fname}:{len(lines) - 1 - i}"
            out.append({"id": rec_id, "label": label})
    return out


def _list_claim(user: Dict, limit: int) -> List[Dict]:
    """Реестр тарифных заявок — core/claim_registry.py (полноценный core-модуль)."""
    try:
        from core.claim_registry import list_projects
        projects = list_projects()
    except Exception:
        return []
    out = []
    for p in projects[:limit]:
        org = p.get("org", "") or "(без организации)"
        period = p.get("period", "")
        status = p.get("status", "")
        bits = [b for b in [org, period, status] if b]
        out.append({"id": p.get("id", ""), "label": " · ".join(bits)})
    return out


def _list_scanner(user: Dict, limit: int) -> List[Dict]:
    """
    База сканов (streamlit_pages/doc_scanner.py).
    Путь: data/doc_scanner/scans_db.json, ключ "documents".
    """
    path = os.path.join(_ROOT, "data", "doc_scanner", "scans_db.json")
    db = _safe_json(path)
    if not db:
        return []
    docs = db.get("documents", [])
    docs = sorted(docs, key=lambda d: d.get("processed_at", ""), reverse=True)
    out = []
    for d in docs[:limit]:
        name = d.get("filename") or d.get("file_name") or "Документ без имени"
        date = (d.get("processed_at", "") or "")[:10]
        bits = [b for b in [name, date] if b]
        out.append({"id": d.get("id", ""), "label": " · ".join(bits)})
    return out


_LISTERS = {
    SOURCE_ADVISOR:   _list_advisor,
    SOURCE_PROTOCOL:  _list_protocol,
    SOURCE_PREDICTOR: _list_predictor,
    SOURCE_CLAIM:     _list_claim,
    SOURCE_SCANNER:   _list_scanner,
}


def list_recent(source: str, user: Dict, limit: int = 30) -> List[Dict]:
    """
    Возвращает последние записи источника: [{"id": str, "label": str}, ...].
    Всегда список (может быть пустым) — исключения не пробрасываются наружу.
    """
    fn = _LISTERS.get(source)
    if fn is None:
        return []
    try:
        return fn(user, limit)
    except Exception:
        return []