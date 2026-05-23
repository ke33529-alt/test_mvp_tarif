# core/claim_registry.py
"""
Реестр тарифных заявок
──────────────────────────────────────────────────────────────────────────────
Хранение:
  data/claims/registry.jsonl   — метаданные + тексты (резюме, риски)
  data/claims/files/{id}/      — оригинальные файлы заявки

Структура записи:
  {
    "id":           "20260522_143012_ао-гт-энерго",
    "created_at":   "2026-05-22T14:30:12",
    "updated_at":   "2026-05-22T14:35:00",
    "org":          "АО «ГТ Энерго»",
    "period":       "2025 год",
    "status":       "анализ / на доработке / подана / принята / отклонена",
    "files":        [{"name": "расчет.xlsx", "size": 45312, "saved": true}],
    "calc_context": "...",   # плоский вид расчётного файла
    "summary":      "...",   # резюме Map-Reduce
    "risks":        "...",   # анализ рисков
    "tags":         [],
    "notes":        ""
  }
"""

from __future__ import annotations
import os
import re
import json
import shutil
from datetime import datetime
from typing import Dict, List, Optional

REGISTRY_DIR  = os.path.join("data", "claims")
REGISTRY_FILE = os.path.join(REGISTRY_DIR, "registry.jsonl")
FILES_DIR     = os.path.join(REGISTRY_DIR, "files")

STATUSES = [
    "анализ",
    "на доработке",
    "подана",
    "принята",
    "отклонена",
]

STATUS_COLORS = {
    "анализ":       ("var(--color-background-secondary)", "var(--color-text-secondary)"),
    "на доработке": ("#FAEEDA", "#633806"),
    "подана":       ("#E6F1FB", "#0C447C"),
    "принята":      ("#EAF3DE", "#27500A"),
    "отклонена":    ("#FCEBEB", "#791F1F"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _slug(text: str) -> str:
    """Транслитерирует и очищает строку для использования в ID."""
    tr = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo',
        'ж':'zh','з':'z','и':'i','й':'j','к':'k','л':'l','м':'m',
        'н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u',
        'ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
        'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
        ' ':'-', '«':'', '»':'', '"':'', "'":''
    }
    s = text.lower()
    s = ''.join(tr.get(c, c) for c in s)
    s = re.sub(r'[^a-z0-9\-]', '', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s[:40]


def _make_id(org: str) -> str:
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slug(org) if org else "zaявka"
    return f"{ts}_{slug}"


def _ensure_dirs():
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    os.makedirs(FILES_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────
def save_project(
    org:          str,
    period:       str,
    files_data:   List[Dict],        # [{"name": str, "bytes": bytes}]
    calc_context: str = "",
    summary:      str = "",
    risks:        str = "",
    status:       str = "анализ",
    tags:         Optional[List[str]] = None,
    notes:        str = "",
    project_id:   Optional[str] = None,  # если передан — обновляем
) -> str:
    """
    Сохраняет или обновляет проект в реестре.
    Возвращает ID проекта.
    """
    _ensure_dirs()
    now = datetime.now().isoformat()
    pid = project_id or _make_id(org)

    # Сохраняем файлы на диск
    saved_files = []
    if files_data:
        project_files_dir = os.path.join(FILES_DIR, pid)
        os.makedirs(project_files_dir, exist_ok=True)
        for fd in files_data:
            name  = fd.get("name", "file")
            data  = fd.get("bytes", b"")
            fpath = os.path.join(project_files_dir, name)
            try:
                with open(fpath, "wb") as f:
                    f.write(data)
                saved_files.append({
                    "name":  name,
                    "size":  len(data),
                    "saved": True,
                    "path":  fpath,
                })
            except Exception:
                saved_files.append({
                    "name":  name,
                    "size":  len(data),
                    "saved": False,
                    "path":  "",
                })
    else:
        # Нет новых файлов — берём из существующей записи если обновляем
        existing = get_project(pid)
        if existing:
            saved_files = existing.get("files", [])

    entry = {
        "id":           pid,
        "created_at":   project_id and get_project(pid, "created_at") or now,
        "updated_at":   now,
        "org":          org,
        "period":       period,
        "status":       status if status in STATUSES else "анализ",
        "files":        saved_files,
        "calc_context": calc_context,
        "summary":      summary,
        "risks":        risks,
        "tags":         tags or [],
        "notes":        notes,
    }

    if project_id:
        # Обновление: перезаписываем строку в JSONL
        _update_entry(pid, entry)
    else:
        # Новая запись
        with open(REGISTRY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return pid


def list_projects(
    search: str = "",
    status_filter: str = "",
) -> List[Dict]:
    """
    Возвращает все проекты из реестра, опционально фильтруя по строке поиска
    и статусу. Сортировка: новые сначала.
    """
    if not os.path.exists(REGISTRY_FILE):
        return []

    projects = []
    seen_ids = set()

    # Читаем все строки — последняя запись с данным ID побеждает
    raw: Dict[str, Dict] = {}
    with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                pid   = entry.get("id", "")
                if pid:
                    raw[pid] = entry   # последняя версия перезаписывает
            except Exception:
                continue

    projects = list(raw.values())

    # Фильтрация
    if search:
        sl = search.lower()
        projects = [
            p for p in projects
            if sl in p.get("org", "").lower()
            or sl in p.get("period", "").lower()
            or sl in p.get("notes", "").lower()
            or any(sl in t.lower() for t in p.get("tags", []))
        ]
    if status_filter and status_filter != "все":
        projects = [p for p in projects if p.get("status") == status_filter]

    # Сортировка: новые сначала
    projects.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    return projects


def get_project(project_id: str, field: Optional[str] = None):
    """
    Возвращает запись проекта по ID.
    Если field задан — возвращает только это поле.
    """
    if not os.path.exists(REGISTRY_FILE):
        return None
    result = None
    with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("id") == project_id:
                    result = entry   # берём последнюю версию
            except Exception:
                continue
    if result is None:
        return None
    return result.get(field) if field else result


def update_status(project_id: str, status: str) -> bool:
    """Обновляет только статус проекта."""
    project = get_project(project_id)
    if not project:
        return False
    project["status"]     = status
    project["updated_at"] = datetime.now().isoformat()
    _update_entry(project_id, project)
    return True


def update_notes(project_id: str, notes: str) -> bool:
    """Обновляет заметки проекта."""
    project = get_project(project_id)
    if not project:
        return False
    project["notes"]      = notes
    project["updated_at"] = datetime.now().isoformat()
    _update_entry(project_id, project)
    return True


def delete_project(project_id: str) -> bool:
    """Удаляет проект из реестра и его файлы с диска."""
    if not os.path.exists(REGISTRY_FILE):
        return False

    # Удаляем из JSONL
    lines_kept = []
    found = False
    with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("id") == project_id:
                    found = True
                    continue   # пропускаем эту запись
            except Exception:
                pass
            lines_kept.append(line)

    if not found:
        return False

    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_kept) + ("\n" if lines_kept else ""))

    # Удаляем файлы
    project_files_dir = os.path.join(FILES_DIR, project_id)
    if os.path.exists(project_files_dir):
        shutil.rmtree(project_files_dir, ignore_errors=True)

    return True


def get_file_path(project_id: str, filename: str) -> Optional[str]:
    """Возвращает путь к файлу проекта если он существует."""
    path = os.path.join(FILES_DIR, project_id, filename)
    return path if os.path.exists(path) else None


# ─────────────────────────────────────────────────────────────────────────────
# Internal
# ─────────────────────────────────────────────────────────────────────────────
def _update_entry(project_id: str, new_entry: Dict):
    """
    Дописывает обновлённую запись в конец JSONL.
    Старые записи с тем же ID остаются — при чтении побеждает последняя.
    """
    _ensure_dirs()
    with open(REGISTRY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(new_entry, ensure_ascii=False) + "\n")
