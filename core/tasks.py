# core/tasks.py
"""
Модуль «Задачи» — личные текстовые заметки пользователей.

Задача = короткая текстовая заметка (до 500 символов), которую пользователь
ведёт в рамках ежедневной работы, чтобы не забыть, что запросить в системе.

Хранение изолировано по пользователям:
    data/tasks/{segment}/{user_id}.json  — список задач конкретного пользователя.

Права:
    • обычный пользователь — видит и правит только свои задачи;
    • segment_admin        — видит все задачи своего сегмента, удаляет любые в сегменте;
    • superadmin           — видит все задачи всех сегментов, удаляет любые.
Редактирование (текст/статус/приоритет/срок/дата выполнения/резолюция/
ссылки) — всегда только своих задач; все поля правятся одним вызовом update_task.

Каждая задача несёт:
    • status   — этап работы: todo | in_progress | done;
    • priority — приоритет:    low | medium | high (цвет — в PRIORITY_COLORS);
    • due_date — срок исполнения "YYYY-MM-DD" или "" (без срока). Подсветка
      считается по остатку РАБОЧИХ дней (пн–пт) до срока — см. deadline_tier.

Модуль не зависит от Streamlit и auth — работает на примитивах / словаре user.
Схема задачи содержит стабильный id и text, чтобы «Загрузить в Советчик»
подключался одним хуком без переработки хранилища.
"""

import os
import json
import uuid
import tempfile
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────
MAX_CHARS = 500
COMPLETION_NOTE_MAX_CHARS = 2000
MAX_COMPLETION_REFS = 5

ROLE_SUPERADMIN    = "superadmin"
ROLE_SEGMENT_ADMIN = "segment_admin"

# ── Статусы ──────────────────────────────────────────────────────────────────
STATUS_TODO        = "todo"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE        = "done"
STATUS_ORDER  = [STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE]
STATUS_LABELS = {
    STATUS_TODO:        "Сделать",
    STATUS_IN_PROGRESS: "В работе",
    STATUS_DONE:        "Выполнено",
}
DEFAULT_STATUS = STATUS_TODO

# ── Приоритеты ───────────────────────────────────────────────────────────────
PRIORITY_LOW    = "low"
PRIORITY_MEDIUM = "medium"
PRIORITY_HIGH   = "high"
PRIORITY_ORDER  = [PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW]   # для сортировки
PRIORITY_CHOICES = [PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH]  # для выбора в UI
PRIORITY_LABELS = {
    PRIORITY_LOW:    "Низкий",
    PRIORITY_MEDIUM: "Средний",
    PRIORITY_HIGH:   "Высокий",
}
PRIORITY_COLORS = {
    PRIORITY_LOW:    "#5BBCD8",  # голубой
    PRIORITY_MEDIUM: "#E8913B",  # оранжевый
    PRIORITY_HIGH:   "#E24B4A",  # красный
}
DEFAULT_PRIORITY = PRIORITY_MEDIUM

# ── Сроки исполнения (подсветка по остатку рабочих дней) ─────────────────────
# Уровни:
#   ok       — осталось БОЛЬШЕ 5 рабочих дней   → зелёный
#   soon     — осталось 2–5 рабочих дней        → жёлтый
#   urgent   — остался 0–1 рабочий день          → оранжевый
#   overdue  — срок в прошлом (просрочено)       → красный
#   none     — срок не задан
DEADLINE_NONE    = "none"
DEADLINE_OK      = "ok"
DEADLINE_SOON    = "soon"
DEADLINE_URGENT  = "urgent"
DEADLINE_OVERDUE = "overdue"
DEADLINE_COLORS = {
    DEADLINE_OK:      "#27AE60",  # зелёный
    DEADLINE_SOON:    "#EBC13D",  # жёлтый
    DEADLINE_URGENT:  "#E8913B",  # оранжевый
    DEADLINE_OVERDUE: "#E24B4A",  # красный
}
DEADLINE_TEXT_COLORS = {
    DEADLINE_OK:      "#ffffff",
    DEADLINE_SOON:    "#1a2a3a",  # тёмный текст: жёлтый фон светлый
    DEADLINE_URGENT:  "#ffffff",
    DEADLINE_OVERDUE: "#ffffff",
}

_ROOT         = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR     = os.path.join(_ROOT, "data", "tasks")
SEGMENTS_FILE = os.path.join(_ROOT, "data", "admin", "segments.json")

_NO_SEGMENT = "_nosegment"


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe(value: str) -> str:
    """Приводит идентификатор к безопасному имени папки/файла."""
    value = (value or "").strip()
    if not value:
        return ""
    return "".join(ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in value)


def _role(user: Optional[Dict]) -> str:
    return (user or {}).get("role", "") or ""


def _uid(user: Optional[Dict]) -> str:
    return (user or {}).get("user_id", "") or ""


def _seg(user: Optional[Dict]) -> str:
    return (user or {}).get("org_id", "") or ""


def _name(user: Optional[Dict]) -> str:
    return (user or {}).get("name", "") or ""


def _seg_folder(segment: str) -> str:
    s = _safe(segment)
    return s if s else _NO_SEGMENT


def _user_file(segment: str, user_id: str) -> str:
    return os.path.join(TASKS_DIR, _seg_folder(segment), f"{_safe(user_id)}.json")


def _norm_status(value: str) -> str:
    return value if value in STATUS_LABELS else DEFAULT_STATUS


def _norm_priority(value: str) -> str:
    return value if value in PRIORITY_LABELS else DEFAULT_PRIORITY


def _norm_due(value) -> str:
    """Нормализует срок к 'YYYY-MM-DD' (дата, без времени) или '' (пусто/некорректно)."""
    if not value:
        return ""
    try:
        s = str(value)[:10]
        date.fromisoformat(s)
        return s
    except Exception:
        return ""


def _norm_timestamp(value) -> str:
    """Нормализует метку времени (дата+время) к строке ISO или '' — для completed_at."""
    if not value:
        return ""
    try:
        s = str(value)
        date.fromisoformat(s[:10])   # валидируем хотя бы дату
        return s
    except Exception:
        return ""


def _norm_note(value: str) -> str:
    value = (value or "").strip()
    if len(value) > COMPLETION_NOTE_MAX_CHARS:
        value = value[:COMPLETION_NOTE_MAX_CHARS]
    return value


def _norm_ref(value) -> Optional[Dict]:
    """
    Нормализует ОДНУ ссылку на сущность из другой истории (Советчик/
    Протоколы/Прогнозист/Заявки/Сканер). Хранится как снапшот
    {source, id, label} — не живая ссылка: если исходная запись позже
    удалится, здесь останется её последнее известное название, это
    осознанно (история выполнения не должна ломаться из-за чужих изменений).
    """
    if not isinstance(value, dict):
        return None
    if not value.get("id"):
        return None
    return {
        "source": str(value.get("source", ""))[:40],
        "id":     str(value.get("id", ""))[:200],
        "label":  str(value.get("label", ""))[:300],
    }


def _norm_refs(value) -> List[Dict]:
    """
    Нормализует СПИСОК ссылок. Принимает как список ref-словарей, так и
    один словарь (для миграции старого единичного поля completion_ref).
    Дедуплицирует по (source, id), обрезает до MAX_COMPLETION_REFS.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    out: List[Dict] = []
    seen = set()
    for item in value:
        r = _norm_ref(item)
        if not r:
            continue
        key = (r["source"], r["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= MAX_COMPLETION_REFS:
            break
    return out


def _normalize_task(t: Dict) -> Dict:
    """
    Гарантирует наличие полей status/priority/due_date/completed_at/
    completion_* (миграция старых записей).

    completion_ref (одиночный, старая схема) → completion_refs (список,
    новая схема): если у задачи есть старое поле, оно переносится в список
    из одного элемента и удаляется — так завершённые до этой правки задачи
    не теряют привязанную запись.
    """
    if not isinstance(t, dict):
        return t
    t["status"]         = _norm_status(t.get("status", DEFAULT_STATUS))
    t["priority"]       = _norm_priority(t.get("priority", DEFAULT_PRIORITY))
    t["due_date"]       = _norm_due(t.get("due_date", ""))
    t["completed_at"]   = _norm_timestamp(t.get("completed_at", ""))
    t["completion_note"] = _norm_note(t.get("completion_note", ""))
    if "completion_refs" in t:
        t["completion_refs"] = _norm_refs(t.get("completion_refs"))
    elif "completion_ref" in t:
        t["completion_refs"] = _norm_refs(t.get("completion_ref"))
    else:
        t["completion_refs"] = []
    t.pop("completion_ref", None)
    return t


def _read_file(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [_normalize_task(t) for t in data if isinstance(t, dict)]
    except Exception:
        pass
    return []


def _write_file(path: str, tasks: List[Dict]) -> None:
    """Атомарная запись: temp в той же папке + os.replace (last-write-wins)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def _norm_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Сроки: остаток рабочих дней и уровень подсветки
# ─────────────────────────────────────────────────────────────────────────────
def business_days_remaining(due_date, today: Optional[date] = None) -> Optional[int]:
    """
    Остаток РАБОЧИХ дней (пн–пт) от сегодня до срока.

    Возвращает:
        None  — срок не задан или некорректен;
        -1    — срок в прошлом (просрочено);
        0     — срок сегодня;
        N>0   — сколько рабочих дней остаётся (сегодня не считается,
                срок засчитывается).
    Выходные (сб/вс) не учитываются. Праздники не учитываются — производственный
    календарь можно подключить позже, интерфейс расчёта не изменится.
    """
    if not due_date:
        return None
    try:
        due = date.fromisoformat(str(due_date)[:10])
    except Exception:
        return None
    today = today or date.today()
    if due < today:
        return -1
    count = 0
    d = today
    while d < due:
        d += timedelta(days=1)
        if d.weekday() < 5:   # 0..4 = пн..пт
            count += 1
    return count


def deadline_tier(due_date, today: Optional[date] = None) -> str:
    """Уровень подсветки срока: none | overdue | urgent | soon | ok."""
    r = business_days_remaining(due_date, today)
    if r is None:
        return DEADLINE_NONE
    if r < 0:
        return DEADLINE_OVERDUE
    if r <= 1:            # 0 (сегодня) или 1 рабочий день
        return DEADLINE_URGENT
    if r <= 5:            # 2–5 рабочих дней
        return DEADLINE_SOON
    return DEADLINE_OK    # больше 5 рабочих дней


def format_due_ru(due_date) -> str:
    try:
        return date.fromisoformat(str(due_date)[:10]).strftime("%d.%m.%Y")
    except Exception:
        return str(due_date or "")


def format_completed_ru(completed_at) -> str:
    """Дата выполнения в человекочитаемом виде (без времени)."""
    return format_due_ru(completed_at)


def deadline_label(due_date, today: Optional[date] = None) -> str:
    """Человекочитаемая подпись срока для бейджа."""
    r = business_days_remaining(due_date, today)
    if r is None:
        return ""
    ds = format_due_ru(due_date)
    if r < 0:
        return f"Просрочено · {ds}"
    if r == 0:
        return f"Срок сегодня · {ds}"
    return f"{ds} · осталось {r} р.д."


# ─────────────────────────────────────────────────────────────────────────────
# Сортировка отображения
# ─────────────────────────────────────────────────────────────────────────────
def _status_idx(t: Dict) -> int:
    s = _norm_status(t.get("status"))
    return STATUS_ORDER.index(s) if s in STATUS_ORDER else 99


def _priority_idx(t: Dict) -> int:
    p = _norm_priority(t.get("priority"))
    return PRIORITY_ORDER.index(p) if p in PRIORITY_ORDER else 99


def sort_for_display(tasks: List[Dict]) -> List[Dict]:
    """
    Порядок: сначала по статусу (Сделать → В работе → Выполнено),
    внутри — по приоритету (Высокий → Средний → Низкий),
    внутри — свежие сверху. Сортировка стабильная, поэтому два прохода.
    """
    ts = sorted(tasks, key=lambda t: t.get("updated_at", ""), reverse=True)
    ts = sorted(ts, key=lambda t: (_status_idx(t), _priority_idx(t)))
    return ts


# ─────────────────────────────────────────────────────────────────────────────
# Сегменты (для фильтра суперадмина и подписи автора)
# ─────────────────────────────────────────────────────────────────────────────
def list_segments() -> Dict[str, str]:
    """Возвращает {org_id: название сегмента} из data/admin/segments.json."""
    if not os.path.exists(SEGMENTS_FILE):
        return {}
    try:
        with open(SEGMENTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result: Dict[str, str] = {}
        for org_id, meta in (raw or {}).items():
            if isinstance(meta, dict):
                result[org_id] = meta.get("name", org_id) or org_id
            else:
                result[org_id] = org_id
        return result
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Права
# ─────────────────────────────────────────────────────────────────────────────
def can_delete(user: Optional[Dict], owner_id: str, segment: str) -> bool:
    role = _role(user)
    if role == ROLE_SUPERADMIN:
        return True
    if role == ROLE_SEGMENT_ADMIN and _seg(user) == segment:
        return True
    return owner_id == _uid(user)


def can_edit(user: Optional[Dict], owner_id: str) -> bool:
    """Редактирование текста/статуса/приоритета/срока — только свои задачи."""
    return owner_id == _uid(user)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────────────────────────────────────
def add_task(user: Dict, text: str,
             priority: str = DEFAULT_PRIORITY,
             status: str = DEFAULT_STATUS,
             due_date: str = "") -> Optional[Dict]:
    text = _norm_text(text)
    if not text:
        return None
    uid = _uid(user)
    if not uid:
        return None
    segment = _seg(user)
    path = _user_file(segment, uid)
    tasks = _read_file(path)
    now = _now()
    _status = _norm_status(status)
    task = {
        "id":              uuid.uuid4().hex,
        "text":            text,
        "owner_id":        uid,
        "owner_name":      _name(user),
        "segment":         segment,
        "status":          _status,
        "priority":        _norm_priority(priority),
        "due_date":        _norm_due(due_date),
        # Срок исполнения теряет смысл для уже выполненной задачи — дата
        # выполнения приравнивается к моменту создания в этом редком случае.
        "completed_at":    now if _status == STATUS_DONE else "",
        "completion_note": "",
        "completion_refs": [],
        "created_at":      now,
        "updated_at":      now,
    }
    tasks.append(task)
    _write_file(path, tasks)
    return task


def _norm_completed_input(value, prev: str) -> str:
    """
    Дата выполнения, введённая вручную в режиме «Правка» ('YYYY-MM-DD' или date).

    • пусто/некорректно      → прежнее значение (или «сейчас», если его не было);
    • дата в будущем         → обрезается до сегодняшней (выполнить «завтра» нельзя);
    • дата совпадает с прежней → прежнее значение целиком (не теряем время);
    • сегодня                → текущий момент;
    • иная прошедшая дата    → эта дата с временем 00:00:00.
    """
    d = _norm_due(value)
    if not d:
        return prev or _now()
    today = date.today().isoformat()
    if d > today:
        d = today
    if prev and prev[:10] == d:
        return prev
    if d == today:
        return _now()
    return f"{d}T00:00:00"


def update_task(user: Dict, task_id: str,
                text: Optional[str] = None,
                status: Optional[str] = None,
                priority: Optional[str] = None,
                due_date: Optional[str] = None,
                completed_at: Optional[str] = None,
                completion_note: Optional[str] = None,
                completion_refs: Optional[List[Dict]] = None) -> bool:
    """
    Обновляет свою задачу. Любой из параметров можно передать по отдельности;
    None — «не трогать». Все переданные изменения пишутся ОДНОЙ атомарной
    записью (используется режимом «Правка», где правятся все атрибуты сразу).
    Передача due_date="" очищает срок. Чужие задачи не редактируются.

    Статус и дата выполнения (completed_at):
      • переход в «Выполнено» — completed_at = переданная дата или «сейчас»;
      • уход из «Выполнено»   — completed_at, резолюция и ссылки очищаются
        (они относились к завершению, которого больше нет);
      • задача остаётся/становится «Выполнено» и передан completed_at —
        дата выполнения исправляется вручную (см. _norm_completed_input).

    completion_note / completion_refs применяются, только если ИТОГОВЫЙ
    статус — «Выполнено»; для незавершённой задачи они игнорируются.
    Список ссылок целиком ЗАМЕНЯЕТ прежний.

    Возвращает True, если что-то реально изменилось и записано на диск.
    """
    uid = _uid(user)
    segment = _seg(user)
    path = _user_file(segment, uid)
    tasks = _read_file(path)
    changed = False
    for t in tasks:
        if t.get("id") == task_id and t.get("owner_id") == uid:
            before = json.dumps(t, ensure_ascii=False, sort_keys=True)

            if text is not None:
                nt = _norm_text(text)
                if nt:
                    t["text"] = nt

            if status is not None:
                new_status = _norm_status(status)
                prev_status = _norm_status(t.get("status"))
                if new_status != prev_status:
                    if new_status == STATUS_DONE:
                        t["completed_at"] = (
                            _norm_completed_input(completed_at, "")
                            if completed_at is not None else _now()
                        )
                    elif prev_status == STATUS_DONE:
                        t["completed_at"]     = ""
                        t["completion_note"]  = ""
                        t["completion_refs"]  = []
                t["status"] = new_status

            is_done = _norm_status(t.get("status")) == STATUS_DONE
            if is_done:
                if completed_at is not None:
                    t["completed_at"] = _norm_completed_input(completed_at, t.get("completed_at", ""))
                if completion_note is not None:
                    t["completion_note"] = _norm_note(completion_note)
                if completion_refs is not None:
                    t["completion_refs"] = _norm_refs(completion_refs)

            if priority is not None:
                t["priority"] = _norm_priority(priority)
            if due_date is not None:
                t["due_date"] = _norm_due(due_date)

            if json.dumps(t, ensure_ascii=False, sort_keys=True) != before:
                t["updated_at"] = _now()
                changed = True
            break
    if changed:
        _write_file(path, tasks)
    return changed


def complete_task(user: Dict, task_id: str, note: str = "",
                  refs: Optional[List[Dict]] = None) -> bool:
    """
    Переводит СВОЮ задачу в статус «Выполнено» вместе с заметкой о результате
    и (опционально) списком ссылок на связанные записи из истории других
    модулей (Советчик/Протоколы/Прогнозист/Заявки/Сканер — см.
    core/entity_picker.py). До MAX_COMPLETION_REFS штук за раз.

    Используется UI-модалкой завершения задачи вместо обычного update_task,
    потому что здесь нужно атомарно зафиксировать статус + заметку + ссылки
    одним действием после подтверждения в диалоге.
    """
    uid = _uid(user)
    segment = _seg(user)
    path = _user_file(segment, uid)
    tasks = _read_file(path)
    changed = False
    for t in tasks:
        if t.get("id") == task_id and t.get("owner_id") == uid:
            t["status"]          = STATUS_DONE
            t["completed_at"]    = _now()
            t["completion_note"] = _norm_note(note)
            t["completion_refs"] = _norm_refs(refs or [])
            t["updated_at"]      = _now()
            changed = True
            break
    if changed:
        _write_file(path, tasks)
    return changed


def update_completion_note(user: Dict, task_id: str, note: str = "") -> bool:
    """
    Правит ТОЛЬКО текст резолюции уже завершённой задачи — не трогает
    status/completed_at/completion_refs. В отличие от complete_task (которая
    переводит статус в «Выполнено» и обновляет дату выполнения), эта функция
    для случая «задача уже выполнена, нужно поправить текст резолюции» —
    вызывается из режима «Правка» на карточке.
    """
    uid = _uid(user)
    segment = _seg(user)
    path = _user_file(segment, uid)
    tasks = _read_file(path)
    changed = False
    for t in tasks:
        if t.get("id") == task_id and t.get("owner_id") == uid:
            t["completion_note"] = _norm_note(note)
            t["updated_at"]      = _now()
            changed = True
            break
    if changed:
        _write_file(path, tasks)
    return changed


def update_completion_refs(user: Dict, task_id: str, refs: Optional[List[Dict]] = None) -> bool:
    """
    Правит ТОЛЬКО список ссылок на связанные записи (Советчик/Протоколы/
    Прогнозист/Заявки/Сканер) уже завершённой задачи — не трогает
    status/completed_at/completion_note. Симметрична update_completion_note,
    вызывается из того же режима «Правка» при редактировании завершённой
    задачи. Список полностью ЗАМЕНЯЕТ старый (не дописывает) — UI режима
    правки сам собирает актуальный список перед сохранением.
    """
    uid = _uid(user)
    segment = _seg(user)
    path = _user_file(segment, uid)
    tasks = _read_file(path)
    changed = False
    for t in tasks:
        if t.get("id") == task_id and t.get("owner_id") == uid:
            t["completion_refs"] = _norm_refs(refs or [])
            t["updated_at"]      = _now()
            changed = True
            break
    if changed:
        _write_file(path, tasks)
    return changed


def delete_task(user: Dict, task_id: str, owner_id: str, segment: str) -> bool:
    """Удаление задачи с проверкой прав по роли."""
    if not can_delete(user, owner_id, segment):
        return False
    path = _user_file(segment, owner_id)
    tasks = _read_file(path)
    new_tasks = [t for t in tasks if t.get("id") != task_id]
    if len(new_tasks) == len(tasks):
        return False
    _write_file(path, new_tasks)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Чтение
# ─────────────────────────────────────────────────────────────────────────────
def _read_all_in_folder(folder_path: str) -> List[Dict]:
    tasks: List[Dict] = []
    if not os.path.isdir(folder_path):
        return tasks
    for fname in os.listdir(folder_path):
        if not fname.endswith(".json"):
            continue
        tasks.extend(_read_file(os.path.join(folder_path, fname)))
    return tasks


def list_own_tasks(user: Dict) -> List[Dict]:
    """Только задачи текущего пользователя — для модалки быстрого выбора."""
    return _read_file(_user_file(_seg(user), _uid(user)))


def list_tasks(user: Dict, segment_filter: Optional[str] = None) -> List[Dict]:
    """
    Возвращает задачи, видимые текущему пользователю:
      • superadmin    — все задачи всех сегментов (или одного, если задан segment_filter);
      • segment_admin — все задачи своего сегмента;
      • обычный       — только свои.
    Без сортировки — вызывающий код применяет sort_for_display() при выводе.
    """
    role = _role(user)
    tasks: List[Dict] = []

    if role == ROLE_SUPERADMIN:
        if segment_filter:
            tasks = _read_all_in_folder(os.path.join(TASKS_DIR, _seg_folder(segment_filter)))
        elif os.path.isdir(TASKS_DIR):
            for seg_folder in os.listdir(TASKS_DIR):
                tasks.extend(_read_all_in_folder(os.path.join(TASKS_DIR, seg_folder)))
    elif role == ROLE_SEGMENT_ADMIN:
        tasks = _read_all_in_folder(os.path.join(TASKS_DIR, _seg_folder(_seg(user))))
    else:
        tasks = _read_file(_user_file(_seg(user), _uid(user)))

    return tasks