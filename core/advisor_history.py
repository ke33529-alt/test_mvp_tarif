# core/advisor_history.py
"""
Персистентная история советчика — изолированная по сегменту и пользователю.

ЧТО ИЗМЕНИЛОСЬ И ПОЧЕМУ
────────────────────────
Раньше всё писалось в ОДИН файл data/advisor_history/history.jsonl без пометки
владельца, а load_all() читал его целиком. Вкладка «Все запросы» показывала
каждому пользователю вопросы, ответы и сниппеты источников всех остальных —
включая фрагменты локальных баз чужих сегментов. Теперь история разложена
по каталогам:

    data/advisor_history/
        org_20240115_103000/            <- сегмент
            20240101_120000_000000.jsonl    <- пользователь
            20240102_090000_000000.jsonl
        __superadmin__/
            superadmin.jsonl
        __no_org__/                     <- пользователь без сегмента
            20240103_140000_000000.jsonl
        history.jsonl                   <- LEGACY, см. ниже

Изоляция физическая, а не фильтром по полю: чужой файл просто не читается.
Это дешевле и надёжнее — и заодно чинит производительность: update_clarifications
и delete_entry перезаписывают файл целиком, а теперь файл маленький и личный.

LEGACY. Старый history.jsonl остаётся на диске нетронутым и НЕ показывается
никому: определить владельца задним числом невозможно, а показать всем — то,
что мы и чиним. Если история заведомо принадлежит одному человеку (период
одиночной разработки), её можно присвоить: adopt_legacy(user_id, org_id).
Иначе просто удалите файл или оставьте как архив.

ОБЛАСТИ ВИДИМОСТИ (scope):
    "user" — своя история (по умолчанию для всех)
    "org"  — вся история сегмента (segment_admin и выше)
    "all"  — вообще всё (только superadmin)
Проверку прав делает вызывающий код; сама функция лишь читает то, что просят.
"""

from __future__ import annotations

import os
import re
import json
import threading
from datetime import datetime
from typing import List, Dict, Optional

from core.advisor import get_current_org_id, get_current_user_id

# =============================================================================
# Пути
# =============================================================================
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DIR  = os.path.join(BASE_DIR, "data", "advisor_history")
LEGACY_FILE  = os.path.join(HISTORY_DIR, "history.jsonl")

# Каталоги-заглушки для тех, у кого нет org_id
SUPERADMIN_DIR = "__superadmin__"
NO_ORG_DIR     = "__no_org__"

_write_lock = threading.Lock()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# =============================================================================
# Безопасные имена путей
#
# org_id и user_id приходят из users.json / segments.json и генерируются
# системой (org_YYYYMMDD_HHMMSS и YYYYMMDD_HHMMSS_ffffff), но подставлять их
# в путь без санитизации нельзя: одна кривая запись в JSON — и мы пишем
# в произвольное место диска. Оставляем только безопасный алфавит.
# =============================================================================
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe(part: str, fallback: str) -> str:
    cleaned = _SAFE_RE.sub("_", str(part or "")).strip("._")
    return cleaned or fallback


def _org_dir_name(org_id: str, user_id: str) -> str:
    """Имя каталога сегмента. Суперадмин и бессегментные — в свои каталоги."""
    if user_id == "superadmin":
        return SUPERADMIN_DIR
    if not org_id:
        return NO_ORG_DIR
    return _safe(org_id, NO_ORG_DIR)


def _user_file(org_id: str, user_id: str) -> str:
    """Полный путь к файлу истории конкретного пользователя."""
    org_dir = _org_dir_name(org_id, user_id)
    fname   = _safe(user_id, "unknown") + ".jsonl"
    return os.path.join(HISTORY_DIR, org_dir, fname)


def _resolve_identity(org_id: Optional[str], user_id: Optional[str]) -> tuple:
    """Явные аргументы имеют приоритет; иначе берём текущего пользователя."""
    if user_id is None:
        user_id = get_current_user_id()
    if org_id is None:
        org_id = get_current_org_id()
    return str(org_id or ""), str(user_id or "anonymous")


# =============================================================================
# Генерация ID
# =============================================================================
def _make_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


# =============================================================================
# Урезание источников и уточнений
# =============================================================================
def _slim_sources(srcs) -> List[Dict]:
    if not srcs:
        return []
    return [
        {
            "file":        s.get("file", ""),
            "page":        s.get("page", ""),
            "sphere":      s.get("sphere", ""),
            "snippet":     s.get("snippet", "")[:300],
            # Помечаем происхождение: сниппет из локальной базы сегмента
            # не должен по ошибке уехать в общий экспорт или чужую выдачу.
            "source_kind": s.get("source_kind", "global"),
        }
        for s in srcs
    ]


def _slim_clars(clars) -> List[Dict]:
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
    org_id: str = None,
    user_id: str = None,
) -> str:
    """
    Записывает запрос+ответ в личный файл истории пользователя.
    Возвращает ID записи (для последующего update_clarifications).

    org_id/user_id: если не переданы — берутся из session_state.
    """
    org_id, user_id = _resolve_identity(org_id, user_id)
    now = datetime.now()
    eid = entry_id or _make_id()

    entry = {
        "id":             eid,
        "ts":             now.isoformat(timespec="seconds"),
        "date":           now.strftime("%Y-%m-%d"),
        "org_id":         org_id,
        "user_id":        user_id,
        "query":          query,
        "answer":         answer,
        "model":          model or "",
        "spheres":        spheres or [],
        "sources":        _slim_sources(sources),
        "from_faq":       bool(from_faq),
        "clarifications": _slim_clars(clarifications),
    }

    path = _user_file(org_id, user_id)
    _ensure_dir(os.path.dirname(path))
    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return eid


# =============================================================================
# Обновление уточнений у существующей записи
# =============================================================================
def update_clarifications(
    entry_id: str,
    clarifications: list,
    org_id: str = None,
    user_id: str = None,
) -> bool:
    """
    Перезаписывает уточнения у записи entry_id в файле ТЕКУЩЕГО пользователя.
    Чужие файлы не открываются вообще — подделать entry_id и дописать
    в чужую историю невозможно. Возвращает True если запись найдена.
    """
    org_id, user_id = _resolve_identity(org_id, user_id)
    path = _user_file(org_id, user_id)
    if not os.path.exists(path):
        return False

    slim    = _slim_clars(clarifications)
    lines   = []
    updated = False

    with _write_lock:
        with open(path, "r", encoding="utf-8") as f:
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
                    rec["clarifications"] = slim
                    line = json.dumps(rec, ensure_ascii=False)
                    updated = True
                lines.append(line)

        if updated:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    return updated


# =============================================================================
# Чтение
# =============================================================================
def _read_file(path: str) -> List[Dict]:
    """Читает один JSONL-файл истории. Битые строки пропускает."""
    if not os.path.exists(path):
        return []
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        print(f"[HIST] Ошибка чтения {path}: {e}")
    return records


def load_all(
    scope: str = "user",
    org_id: str = None,
    user_id: str = None,
) -> List[Dict]:
    """
    Возвращает записи истории от новых к старым.

    scope="user" (по умолчанию) — только своя история.
    scope="org"  — вся история своего сегмента. Права проверяет вызывающий код
                   (segment_admin и выше); функция сама прав не проверяет.
    scope="all"  — вся история всех сегментов. Только для суперадмина.

    LEGACY-файл history.jsonl не читается ни при одном scope — у его записей
    нет владельца, показывать их кому-либо небезопасно. См. adopt_legacy().
    """
    org_id, user_id = _resolve_identity(org_id, user_id)
    records: List[Dict] = []

    if scope == "user":
        records = _read_file(_user_file(org_id, user_id))

    elif scope == "org":
        org_dir = os.path.join(HISTORY_DIR, _org_dir_name(org_id, user_id))
        if os.path.isdir(org_dir):
            for fname in sorted(os.listdir(org_dir)):
                if fname.endswith(".jsonl"):
                    records.extend(_read_file(os.path.join(org_dir, fname)))

    elif scope == "all":
        if os.path.isdir(HISTORY_DIR):
            for dname in sorted(os.listdir(HISTORY_DIR)):
                dpath = os.path.join(HISTORY_DIR, dname)
                if not os.path.isdir(dpath):
                    continue   # пропускаем legacy history.jsonl в корне
                for fname in sorted(os.listdir(dpath)):
                    if fname.endswith(".jsonl"):
                        records.extend(_read_file(os.path.join(dpath, fname)))
    else:
        raise ValueError(f"Неизвестный scope: {scope!r}. Ожидается user|org|all")

    # Сортировка по ts — при scope=org/all записи склеены из разных файлов
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return records


# =============================================================================
# Поиск по истории (работает над уже отфильтрованным по владельцу списком)
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
    Фильтрует записи по параметрам. Каждая дополняется полем '_snippet'.
    Внимание: параметр scope здесь — область ТЕКСТА (вопрос/ответ),
    он не имеет отношения к scope в load_all (области видимости).
    """
    q = query.strip().lower()

    def _text_matches(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        if match_type == "Точное":
            return q in t
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
        return (("..." if start > 0 else "") + text[start:end]
                + ("..." if end < len(text) else ""))

    results = []
    for rec in records:
        rec_date = rec.get("date", "")
        if date_from and rec_date < date_from:
            continue
        if date_to and rec_date > date_to:
            continue

        if sphere:
            rec_spheres = " ".join(rec.get("spheres", [])).lower()
            if sphere.lower() not in rec_spheres:
                continue

        if q:
            search_text = _get_search_text(rec)
            if not _text_matches(search_text):
                continue
            snippet = _make_snippet(search_text)
        else:
            snippet = rec.get("query", "")[:200]

        results.append({**rec, "_snippet": snippet})

    return results


# =============================================================================
# Удаление записи
# =============================================================================
def delete_entry(entry_id: str, org_id: str = None, user_id: str = None) -> bool:
    """
    Удаляет запись из файла ТЕКУЩЕГО пользователя.
    Чужие файлы не трогаются — удалить чужую запись, подставив ID, нельзя.
    """
    org_id, user_id = _resolve_identity(org_id, user_id)
    path = _user_file(org_id, user_id)
    if not os.path.exists(path):
        return False

    lines   = []
    removed = False
    with _write_lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("id") == entry_id:
                        removed = True
                        continue
                except Exception:
                    pass
                lines.append(line)

        if removed:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))

    return removed


def clear_user_history(org_id: str = None, user_id: str = None) -> int:
    """Полностью очищает историю пользователя. Возвращает число удалённых."""
    org_id, user_id = _resolve_identity(org_id, user_id)
    path = _user_file(org_id, user_id)
    if not os.path.exists(path):
        return 0
    n = len(_read_file(path))
    with _write_lock:
        try:
            os.remove(path)
        except Exception as e:
            print(f"[HIST] Не удалось удалить {path}: {e}")
            return 0
    return n


# =============================================================================
# LEGACY: старый общий history.jsonl
# =============================================================================
def legacy_count() -> int:
    """Сколько записей осталось в старом общем файле (0 если файла нет)."""
    return len(_read_file(LEGACY_FILE))


def adopt_legacy(user_id: str, org_id: str) -> int:
    """
    Присваивает ВСЕ записи старого общего history.jsonl одному владельцу
    и переносит их в его личный файл. Исходник переименовывается в
    history.jsonl.adopted — на случай если присвоение было ошибкой.

    Осмысленно только если известно, что история накоплена одним человеком
    (например, период одиночной разработки до подключения пользователей).
    Автоматически определить владельца задним числом невозможно — в старых
    записях его просто нет.

    Возвращает число перенесённых записей.
    """
    records = _read_file(LEGACY_FILE)
    if not records:
        return 0

    path = _user_file(org_id, user_id)
    _ensure_dir(os.path.dirname(path))

    with _write_lock:
        with open(path, "a", encoding="utf-8") as f:
            for rec in records:
                rec["org_id"]  = org_id
                rec["user_id"] = user_id
                rec.setdefault("id", _make_id())
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        try:
            os.rename(LEGACY_FILE, LEGACY_FILE + ".adopted")
        except Exception as e:
            print(f"[HIST] Записи перенесены, но переименовать legacy не вышло: {e}")

    print(f"[HIST] Перенесено {len(records)} legacy-записей → {user_id} ({org_id})")
    return len(records)


def purge_legacy() -> int:
    """Удаляет старый общий файл истории безвозвратно. Возвращает число записей."""
    n = legacy_count()
    try:
        if os.path.exists(LEGACY_FILE):
            os.remove(LEGACY_FILE)
    except Exception as e:
        print(f"[HIST] Не удалось удалить legacy: {e}")
        return 0
    return n


# =============================================================================
# Статистика
# =============================================================================
def get_stats(records: List[Dict]) -> Dict:
    if not records:
        return {"total": 0, "today": 0, "with_clarifications": 0,
                "oldest_date": "—"}
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "total":               len(records),
        "today":               sum(1 for r in records if r.get("date") == today),
        "with_clarifications": sum(1 for r in records if r.get("clarifications")),
        "oldest_date":         records[-1].get("date", ""),
    }