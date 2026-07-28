# core/help_requests.py
"""
Запросы помощи от пользователей.
════════════════════════════════════════════════════════════════════════════════

Хранение: data/help/requests.jsonl

Жизненный цикл:
  1. Пользователь отправляет → status=new, reply=null, read_by_user=false
  2. Суперадмин пишет ответ → reply="...", read_by_user сбрасывается в false
  3. Пользователь открывает "Мои обращения" → read_by_user=true, done_seen_by_user=true
  4. Суперадмин нажимает "Отработать" → status=done, done_seen_by_user=false
  5. Суперадмин нажимает "Взять в работу" → status=new
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_BASE_DIR   = Path(__file__).parent.parent.resolve()
_HELP_DIR   = _BASE_DIR / "data" / "help"
_HELP_FILE  = _HELP_DIR / "requests.jsonl"
_write_lock = threading.Lock()

MAX_TEXT_LEN  = 500
MAX_REPLY_LEN = 1000


def _ensure_dir() -> None:
    _HELP_DIR.mkdir(parents=True, exist_ok=True)


def _read_all() -> List[Dict]:
    if not _HELP_FILE.exists():
        return []
    records = []
    with _HELP_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
    return records


def _write_all(records: List[Dict]) -> None:
    _ensure_dir()
    with _write_lock:
        with _HELP_FILE.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _update_record(request_id: str, updates: Dict) -> bool:
    records = _read_all()
    found   = False
    for rec in records:
        if rec.get("id") == request_id:
            rec.update(updates)
            found = True
            break
    if found:
        _write_all(records)
    return found


def submit_request(
    user_id:   str,
    user_name: str,
    org_id:    str,
    org_name:  str,
    text:      str,
) -> str:
    """Сохраняет новый запрос помощи от пользователя."""
    _ensure_dir()
    now = datetime.now()
    rid = now.strftime("%Y%m%d_%H%M%S_%f")

    entry = {
        "id":                rid,
        "ts":                now.isoformat(timespec="seconds"),
        "user_id":           user_id,
        "user_name":         user_name,
        "org_id":            org_id,
        "org_name":          org_name or "—",
        "text":              text.strip()[:MAX_TEXT_LEN],
        "status":            "new",
        "reply":             None,
        "replied_at":        None,
        "read_by_user":      False,
        "done_seen_by_user": False,
        "done_at":           None,
        "done_by":           None,
    }

    with _write_lock:
        with _HELP_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return rid


def get_requests(
    status:  Optional[str] = None,
    org_id:  Optional[str] = None,
    user_id: Optional[str] = None,
    limit:   int = 200,
) -> List[Dict]:
    """Читает запросы с фильтрацией. Возвращает от новых к старым."""
    records = _read_all()
    result  = []
    for rec in records:
        if status  and rec.get("status")  != status:
            continue
        if org_id  and rec.get("org_id")  != org_id:
            continue
        if user_id and rec.get("user_id") != user_id:
            continue
        result.append(rec)
    result.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return result[:limit]


def count_new() -> int:
    """Количество необработанных запросов. Для дашборда суперадмина."""
    return len(get_requests(status="new", limit=10_000))


def has_unread_replies(user_id: str) -> bool:
    """
    Есть ли у пользователя ответы которые он не видел.
    Красный индикатор в сайдбаре.
    """
    records = get_requests(user_id=user_id, limit=10_000)
    return any(
        rec.get("reply") and not rec.get("read_by_user", False)
        for rec in records
    )


def has_unseen_done(user_id: str) -> bool:
    """
    Есть ли у пользователя отработанные запросы которые он не видел.
    Зелёный индикатор в сайдбаре. Показывается только если нет красного.
    """
    records = get_requests(user_id=user_id, limit=10_000)
    return any(
        rec.get("status") == "done" and not rec.get("done_seen_by_user", False)
        for rec in records
    )


def save_reply(request_id: str, reply_text: str) -> bool:
    """
    Сохраняет ответ суперадмина.
    Статус не меняется. Сбрасывает read_by_user чтобы пользователь увидел ответ.
    """
    return _update_record(request_id, {
        "reply":        reply_text.strip()[:MAX_REPLY_LEN],
        "replied_at":   datetime.now().isoformat(timespec="seconds"),
        "read_by_user": False,
    })


def mark_done(request_id: str, done_by: str) -> bool:
    """
    Помечает запрос как отработанный.
    Сбрасывает done_seen_by_user чтобы пользователь увидел зелёный индикатор.
    """
    return _update_record(request_id, {
        "status":            "done",
        "done_at":           datetime.now().isoformat(timespec="seconds"),
        "done_by":           done_by,
        "done_seen_by_user": False,
    })


def reopen_request(request_id: str) -> bool:
    """Возвращает отработанный запрос в статус new."""
    return _update_record(request_id, {
        "status":            "new",
        "done_at":           None,
        "done_by":           None,
        "done_seen_by_user": True,
    })


def mark_read_by_user(user_id: str) -> None:
    """
    Помечает все ответы и отработанные запросы пользователя как просмотренные.
    Вызывается при открытии раздела "Мои обращения".
    После этого оба индикатора (красный и зелёный) гаснут.
    """
    records = _read_all()
    changed = False
    for rec in records:
        if rec.get("user_id") != user_id:
            continue
        if rec.get("reply") and not rec.get("read_by_user", False):
            rec["read_by_user"] = True
            changed = True
        if rec.get("status") == "done" and not rec.get("done_seen_by_user", False):
            rec["done_seen_by_user"] = True
            changed = True
    if changed:
        _write_all(records)