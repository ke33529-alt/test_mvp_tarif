# =============================================================================
# РЕГУЛА.AI — Dockerfile
# =============================================================================
# Базовый образ: Python 3.11 на Debian Slim (лёгкий, без лишнего)
FROM python:3.11-slim

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
