# core/archive_extractor.py
"""
Распаковка ZIP-архивов тарифных заявок
──────────────────────────────────────────────────────────────────────────────
Используется Анализатором заявок: архив разворачивается в плоский список
файлов (путь_внутри_архива, байты), дальше они обрабатываются так же,
как файлы, загруженные по одному.

Особенности:
  • кириллические имена из проводника Windows (cp866 без UTF-8-флага);
  • вложенные ZIP-архивы — до глубины _MAX_DEPTH;
  • служебный мусор (__MACOSX, Thumbs.db, ~$временные.xlsx) отбрасывается;
  • защита от zip-бомб: лимиты на число файлов, размер файла и общий объём;
  • зашифрованные файлы и неподдерживаемые форматы попадают в список
    пропущенных с указанием причины;
  • read_member() достаёт один файл из сохранённого архива (для скачивания
    документа из реестра без распаковки всего архива).

Только стандартная библиотека — пересборка контейнера не требуется.
"""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

# ── Лимиты ───────────────────────────────────────────────────────────────────
_MAX_DEPTH       = 2                    # вложенность архивов в архиве
_MAX_FILES       = 500                  # максимум файлов из одного архива
_MAX_FILE_BYTES  = 200 * 1024 * 1024    # максимум на один распакованный файл
_MAX_TOTAL_BYTES = 800 * 1024 * 1024    # максимум суммарно на один архив

# Форматы, которые анализатор умеет обрабатывать
SUPPORTED_EXTS = (".xlsx", ".xls", ".pdf", ".docx", ".doc", ".txt")

_JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


@dataclass
class ArchiveResult:
    # Пути — относительно корня архива, через «/»; для вложенных архивов
    # имя вложенного архива входит в путь: «Вложения.zip/Договоры/Договор.pdf»
    files:   List[Tuple[str, bytes]]  = field(default_factory=list)  # (путь, байты)
    skipped: List[Tuple[str, str]]    = field(default_factory=list)  # (путь, причина)
    errors:  List[str]                = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(len(b) for _, b in self.files)


def is_archive(name: str) -> bool:
    return os.path.splitext(name.lower())[1] == ".zip"


# ─────────────────────────────────────────────────────────────────────────────
# Имена файлов
# ─────────────────────────────────────────────────────────────────────────────
def _decode_name(info: zipfile.ZipInfo) -> str:
    """
    Восстанавливает имя файла.
    Если UTF-8-флаг (бит 11) не выставлен, zipfile декодирует имя как cp437.
    Проводник Windows и старые архиваторы пишут имена в cp866 (русская
    DOS-кодировка) — отсюда «кракозябры». Возвращаем исходные байты
    и пробуем UTF-8 (некоторые архиваторы пишут его без флага), затем cp866.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for enc in ("utf-8", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _is_junk(path: str) -> bool:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return True
    if any(p == "__MACOSX" for p in parts):
        return True
    base = parts[-1]
    if base.lower() in _JUNK_NAMES:
        return True
    if base.startswith("~$") or base.startswith("._"):
        return True
    return False


def _unique(path: str, used: Set[str]) -> str:
    """Делает имя уникальным: «файл.pdf» → «файл (2).pdf»."""
    if path not in used:
        used.add(path)
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while f"{stem} ({i}){ext}" in used:
        i += 1
    new = f"{stem} ({i}){ext}"
    used.add(new)
    return new


def common_root(paths: List[str]) -> str:
    """
    Общая корневая папка всех файлов архива («Заявка ТС 2027/») или "".

    Архив, упакованный из папки, обычно целиком лежит в одной корневой
    папке. Её имя одинаково для всех файлов и не помогает понять, к какой
    статье относится документ, — а при сопоставлении по ключевым словам
    даже мешает: одно слово статьи в имени корня «притягивает» к статье
    сразу все файлы архива.
    """
    if not paths:
        return ""
    dirs = [p.split("/")[:-1] for p in paths]
    prefix: List[str] = []
    for parts in zip(*dirs):
        if all(x == parts[0] for x in parts):
            prefix.append(parts[0])
        else:
            break
    return "/".join(prefix) + "/" if prefix else ""


# ─────────────────────────────────────────────────────────────────────────────
# Распаковка
# ─────────────────────────────────────────────────────────────────────────────
def expand_archive(data: bytes, archive_name: str) -> ArchiveResult:
    """Разворачивает ZIP-архив в плоский список файлов (пути от корня архива)."""
    res = ArchiveResult()
    used: Set[str] = set()
    budget = {"bytes": 0, "files": 0}
    _expand(data, archive_name, "", 0, res, used, budget)
    return res


def _expand(data: bytes, label: str, prefix: str, depth: int,
            res: ArchiveResult, used: Set[str], budget: dict) -> None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        res.errors.append(f"{label}: файл повреждён или не является ZIP-архивом")
        return
    except Exception as e:
        res.errors.append(f"{label}: ошибка открытия архива — {e}")
        return

    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            inner = _norm(_decode_name(info))
            full  = f"{prefix}/{inner}" if prefix else inner

            if _is_junk(inner):
                continue

            if budget["files"] >= _MAX_FILES:
                res.errors.append(
                    f"{label}: превышен лимит {_MAX_FILES} файлов — "
                    f"остальное содержимое пропущено"
                )
                return

            if info.flag_bits & 0x1:
                res.skipped.append((full, "зашифрован паролем"))
                continue

            ext = os.path.splitext(inner.lower().rstrip())[1].strip()
            nested = ext == ".zip"
            if not nested and ext not in SUPPORTED_EXTS:
                res.skipped.append((full, f"формат {ext or 'без расширения'} не поддерживается"))
                continue
            if nested and depth + 1 > _MAX_DEPTH:
                res.skipped.append((full, "слишком глубокая вложенность архивов"))
                continue

            if info.file_size > _MAX_FILE_BYTES:
                res.skipped.append((full, "файл слишком большой"))
                continue
            if budget["bytes"] + info.file_size > _MAX_TOTAL_BYTES:
                res.errors.append(
                    f"{label}: превышен общий лимит распаковки "
                    f"{_MAX_TOTAL_BYTES // (1024 * 1024)} МБ — остальное пропущено"
                )
                return

            # Читаем с ограничением: заявленный file_size может не совпадать
            # с реальным объёмом (zip-бомба)
            try:
                with zf.open(info) as fh:
                    blob = fh.read(_MAX_FILE_BYTES + 1)
            except RuntimeError:
                res.skipped.append((full, "зашифрован паролем"))
                continue
            except Exception as e:
                res.skipped.append((full, f"ошибка распаковки: {e}"))
                continue
            if len(blob) > _MAX_FILE_BYTES:
                res.skipped.append((full, "файл слишком большой"))
                continue

            budget["bytes"] += len(blob)

            if nested:
                _expand(blob, f"{label}/{inner}", full, depth + 1, res, used, budget)
                continue

            budget["files"] += 1
            res.files.append((_unique(full, used), blob))


# ─────────────────────────────────────────────────────────────────────────────
# Чтение одного файла из архива
# ─────────────────────────────────────────────────────────────────────────────
def _read_limited(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Optional[bytes]:
    try:
        with zf.open(info) as fh:
            blob = fh.read(_MAX_FILE_BYTES + 1)
    except Exception:
        return None
    return None if len(blob) > _MAX_FILE_BYTES else blob


def read_member(data: bytes, inner_path: str, _depth: int = 0) -> Optional[bytes]:
    """
    Достаёт из ZIP один файл по пути, который вернул expand_archive()
    (в том числе из вложенного архива: «Вложения.zip/Договоры/Договор.pdf»).
    Возвращает None, если файл не найден или не читается.
    """
    if _depth > _MAX_DEPTH or not data:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return None

    target = _norm(inner_path)
    with zf:
        members = {}
        for info in zf.infolist():
            if not info.is_dir():
                members[_norm(_decode_name(info))] = info

        info = members.get(target)
        if info is not None:
            return _read_limited(zf, info)

        # Вложенный архив: ищем самый короткий префикс «….zip»
        parts = target.split("/")
        for i in range(1, len(parts)):
            head = "/".join(parts[:i])
            if head.lower().endswith(".zip") and head in members:
                blob = _read_limited(zf, members[head])
                if blob is None:
                    return None
                return read_member(blob, "/".join(parts[i:]), _depth + 1)
    return None
