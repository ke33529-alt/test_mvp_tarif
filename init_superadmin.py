#!/usr/bin/env python3
# init_superadmin.py
"""
Скрипт первичной инициализации суперадмина РЕГУЛА.AI.
Запускается ОДИН РАЗ при первом деплое на сервере.

Использование:
  python init_superadmin.py

Что делает:
  1. Создаёт config/superadmin.json с хэшированным паролем
  2. Создаёт необходимые директории (data/admin/, data/audit/ и т.д.)
  3. Если суперадмин уже существует — выводит предупреждение и выходит

После запуска:
  - Войди в систему с указанными логином и паролем
  - Смени пароль через интерфейс (рекомендуется)
  - Создай первый сегмент и пользователей

ВНИМАНИЕ: Не удаляй config/superadmin.json — это единственный способ
войти в систему если все пользователи заблокированы.
"""

import sys
from pathlib import Path

# Добавляем корень проекта в PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from core.auth import init_superadmin, SUPERADMIN_FILE


def main():
    print("=" * 60)
    print("  РЕГУЛА.AI — Инициализация суперадмина")
    print("=" * 60)

    # Проверяем что суперадмин не существует
    if SUPERADMIN_FILE.exists():
        import json
        try:
            data = json.loads(SUPERADMIN_FILE.read_text(encoding="utf-8"))
            if data.get("login"):
                print(f"\n[!] Суперадмин уже существует: {data['login']}")
                print("    Для сброса пароля используй страницу Управление в интерфейсе.")
                print("    Для полного сброса удали config/superadmin.json и запусти снова.")
                sys.exit(0)
        except Exception:
            pass

    print("\nВведите данные суперадмина:\n")

    name = input("  Имя (отображается в интерфейсе): ").strip()
    if not name:
        print("[!] Имя не может быть пустым")
        sys.exit(1)

    login = input("  Логин (email или любой): ").strip()
    if not login:
        print("[!] Логин не может быть пустым")
        sys.exit(1)

    import getpass
    password = getpass.getpass("  Пароль (минимум 8 символов): ")
    if len(password) < 8:
        print("[!] Пароль слишком короткий")
        sys.exit(1)

    password2 = getpass.getpass("  Повторите пароль: ")
    if password != password2:
        print("[!] Пароли не совпадают")
        sys.exit(1)

    ok = init_superadmin(name=name, login_str=login, password=password)

    if ok:
        print(f"\n✓ Суперадмин создан: {login}")
        print(f"  Файл: config/superadmin.json")
        print("\n  Теперь запусти приложение:")
        print("  streamlit run app.py\n")
    else:
        print("\n[!] Не удалось создать суперадмина")
        sys.exit(1)


if __name__ == "__main__":
    main()