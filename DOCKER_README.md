# РЕГУЛА.AI — Запуск через Docker

## Быстрый старт

### 1. Подготовка конфига
```bash
cp .env.example .env
# Отредактируйте .env — укажите URL вашего Ollama
```

### 2. Сборка и запуск
```bash
docker compose up -d --build
```

### 3. Открыть приложение
```
http://ваш-сервер:8501
```

---

## Структура файлов

```
regula_ai/
├── app.py                  # Код (внутри контейнера)
├── core/
├── streamlit_pages/
├── Dockerfile              # Инструкция сборки образа
├── docker-compose.yml      # Инструкция запуска
├── .env                    # Ваши секреты (НЕ в git!)
├── .env.example            # Шаблон (в git)
├── .dockerignore           # Что НЕ копировать в образ
│
├── data/                   # Данные (снаружи контейнера, монтируются)
│   ├── claims/             # Реестр заявок
│   ├── vector_db/          # ChromaDB
│   ├── raw/                # НПА документы
│   └── protocol_bot/       # Протоколы
│
└── config/                 # Конфиги (снаружи контейнера)
    ├── prompts.json
    └── doc_spheres.json
```

---

## Ollama — отдельный сервис

Ollama запускается **на хост-машине** (не в контейнере):

```bash
# Установка Ollama на сервере
curl -fsSL https://ollama.ai/install.sh | sh

# Запуск как системный сервис
sudo systemctl enable ollama
sudo systemctl start ollama

# Загрузка модели
ollama pull qwen3:9b

# Проверка
curl http://localhost:11434/api/tags
```

В `.env` укажите:
```
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

---

## Полезные команды

```bash
# Посмотреть логи
docker compose logs -f regula

# Перезапустить после изменений в коде
docker compose up -d --build

# Остановить
docker compose down

# Зайти внутрь контейнера (для отладки)
docker compose exec regula bash

# Посмотреть потребление ресурсов
docker stats regula_ai
```

---

## Обновление приложения

```bash
git pull
docker compose up -d --build
```

Данные в `data/` и `config/` не затрагиваются — они снаружи контейнера.
