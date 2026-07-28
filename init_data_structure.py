#!/usr/bin/env python3
# init_data_structure.py
"""
Создаёт файловую структуру данных для РЕГУЛА.AI если её нет.
Безопасно запускать повторно — не затирает существующие данные.

Использование:
  python init_data_structure.py

Создаёт:
  data/admin/segments.json   — пустой список сегментов
  data/admin/users.json      — пустой список пользователей
  data/audit/                — папка для аудит логов
  config/                    — папка для конфигов (суперадмин создаётся отдельно)
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()


def ensure_json(path: Path, default) -> None:
    """Создаёт JSON-файл с дефолтным значением если не существует."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ Создан: {path.relative_to(BASE_DIR)}")
    else:
        print(f"  · Существует: {path.relative_to(BASE_DIR)}")


def ensure_dir(path: Path) -> None:
    """Создаёт папку если не существует."""
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        print(f"  ✓ Создана папка: {path.relative_to(BASE_DIR)}")
    else:
        print(f"  · Папка существует: {path.relative_to(BASE_DIR)}")


def main():
    print("=" * 60)
    print("  РЕГУЛА.AI — Инициализация файловой структуры")
    print("=" * 60)
    print()

    # Папки
    ensure_dir(BASE_DIR / "data" / "admin")
    ensure_dir(BASE_DIR / "data" / "audit")
    ensure_dir(BASE_DIR / "config")

    # Файлы данных
    # users.json — dict {user_id: user_record}
    ensure_json(BASE_DIR / "data" / "admin" / "users.json", {})

    # segments.json — dict {org_id: segment_record}
    ensure_json(BASE_DIR / "data" / "admin" / "segments.json", {})

    print()
    print("Готово. Следующий шаг:")
    print("  python init_superadmin.py")
    print()


if __name__ == "__main__":
    main()