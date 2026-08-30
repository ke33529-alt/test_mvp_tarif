# =============================================================================
# РЕГУЛА.AI — Dockerfile
# =============================================================================
# Multi-stage: два независимых образа из одного файла.
#   docker build --target app      -t regula-app .       (по умолчанию)
#   docker build --target watchdog -t regula-watchdog .
#
# ЗАЧЕМ ОБЪЕДИНЕНО. Ollama-watchdog (см. docker-compose.yml, сервис
# ollama-watchdog) — крошечный скрипт на curl+sh, который следит за
# зависаниями Ollama runner'а и рестартует контейнер ollama через Docker
# API. Ему не нужен ни Python, ни зависимости основного приложения —
# отдельный stage не тянет их в его образ, при этом всё лежит в одном
# файле рядом, а не в отдельном Dockerfile.watchdog.
#
# docker-compose.yml должен указывать target на каждый сервис:
#   regula:          build: {context: ., dockerfile: Dockerfile, target: app}
#   ollama-watchdog: build: {context: ., dockerfile: Dockerfile, target: watchdog}
# =============================================================================


# =============================================================================
# STAGE 1: app — основное приложение (Streamlit)
# =============================================================================
FROM python:3.11-slim AS app

# Метаданные
LABEL maintainer="REGULA.AI"
LABEL description="Платформа анализа тарифного регулирования"

# -----------------------------------------------------------------------------
# Системные зависимости
# Нужны для: EasyOCR (OpenCV), PyMuPDF, python-docx, lxml, faster-whisper (ffmpeg)
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Для OpenCV / EasyOCR
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    # Для Tesseract (резервный OCR)
    tesseract-ocr \
    tesseract-ocr-rus \
    # Для работы с документами
    poppler-utils \
    # Для Протокольщика — конвертация аудио (M4A/iPhone, MP3 и др. в WAV)
    ffmpeg \
    # Утилиты
    curl \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Рабочая директория внутри контейнера
# Весь код приложения будет здесь
# -----------------------------------------------------------------------------
WORKDIR /app

# -----------------------------------------------------------------------------
# Устанавливаем зависимости Python
# Сначала только requirements.txt — Docker кэширует этот слой.
# Если код изменился, но зависимости не менялись — пересборка быстрая.
# -----------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# CUDA-библиотеки для faster-whisper (CTranslate2 backend)
#
# CTranslate2 требует системные libcublas.so.12 / libcudnn, которых нет
# в python:3.11-slim (нет CUDA-рантайма, только GPU-драйвер через
# nvidia-container-toolkit). Пакеты nvidia-cublas-cu12/nvidia-cudnn-cu12
# из requirements.txt кладут нужные .so-файлы в site-packages — здесь
# просто добавляем эти пути в LD_LIBRARY_PATH, чтобы динамический
# линковщик их находил при запуске.
# -----------------------------------------------------------------------------
ENV LD_LIBRARY_PATH="/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib:${LD_LIBRARY_PATH}"

# -----------------------------------------------------------------------------
# Копируем код приложения
# (данные, модели, конфиги — НЕ копируем, они монтируются через volumes)
# .streamlit/ — тема оформления Streamlit (primaryColor и т.д.), нужна
# внутри образа, иначе Streamlit использует дефолтную (не фирменную) тему.
# -----------------------------------------------------------------------------
COPY app.py .
COPY core/ ./core/
COPY streamlit_pages/ ./streamlit_pages/
COPY .streamlit/ ./.streamlit/

# -----------------------------------------------------------------------------
# Создаём папки, которые нужны приложению
# Реальные данные придут через volume-монтирование
# -----------------------------------------------------------------------------
RUN mkdir -p \
    data/claims/files \
    data/feedback \
    data/protocol_bot/protocols \
    data/protocol_bot/temp \
    data/raw \
    data/vector_db \
    config \
    logs

# -----------------------------------------------------------------------------
# Порт Streamlit
# -----------------------------------------------------------------------------
EXPOSE 8501

# -----------------------------------------------------------------------------
# Healthcheck — Docker проверяет, жив ли контейнер каждые 30 секунд
# -----------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# -----------------------------------------------------------------------------
# Запуск приложения
# --server.address=0.0.0.0  — слушаем все интерфейсы (не только localhost)
# --server.headless=true    — без попытки открыть браузер
# --server.fileWatcherType=none — отключаем авто-перезагрузку (для продакшна)
# -----------------------------------------------------------------------------
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.fileWatcherType=none", \
     "--browser.gatherUsageStats=false"]


# =============================================================================
# STAGE 2: watchdog — сторож для зависаний Ollama runner'а
# =============================================================================
# Отдельный минимальный образ (alpine + curl), не связан с зависимостями
# основного приложения. Логика скрипта — см. ollama_watchdog.sh:
# раз в CHECK_INTERVAL секунд шлёт реальный /api/generate (не /api/tags,
# который не ловит зависший runner — известное ограничение Ollama, демон
# и runner конкретной модели это разные процессы) и после FAIL_THRESHOLD
# подряд неудач рестартует контейнер ollama через Docker API.
# =============================================================================
FROM alpine:3.20 AS watchdog

RUN apk add --no-cache curl

COPY ollama_watchdog.sh /usr/local/bin/ollama_watchdog.sh
RUN chmod +x /usr/local/bin/ollama_watchdog.sh

ENTRYPOINT ["/usr/local/bin/ollama_watchdog.sh"]