# core/settings.py
# =============================================================================
# Единое место для всех настроек и путей.
# Значения берутся из переменных окружения (для Docker),
# с разумными дефолтами (для локальной разработки).
#
# Использование в любом модуле:
#   from core.settings import settings
#   path = settings.VECTOR_DB_PATH
# =============================================================================

from __future__ import annotations
import os
from pathlib import Path


# Корень проекта — папка выше core/
_BASE_DIR = Path(__file__).parent.parent.resolve()


class Settings:
    """
    Все настройки приложения в одном месте.
    Читает из переменных окружения — работает и локально, и в Docker.
    """

    # -------------------------------------------------------------------------
    # Корень проекта
    # -------------------------------------------------------------------------
    BASE_DIR: Path = _BASE_DIR

    # -------------------------------------------------------------------------
    # Пути к данным
    # -------------------------------------------------------------------------
    DATA_DIR: Path      = Path(os.getenv("DATA_DIR",      str(_BASE_DIR / "data")))
    CONFIG_DIR: Path    = Path(os.getenv("CONFIG_DIR",    str(_BASE_DIR / "config")))
    RAW_DOCS_PATH: Path = Path(os.getenv("RAW_DOCS_PATH", str(_BASE_DIR / "data" / "raw")))
    LOGS_DIR: Path      = Path(os.getenv("LOGS_DIR",      str(_BASE_DIR / "logs")))
    VECTOR_DB_PATH: Path = Path(os.getenv("VECTOR_DB_PATH", str(_BASE_DIR / "data" / "vector_db")))

    # -------------------------------------------------------------------------
    # LLM
    # -------------------------------------------------------------------------
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str    = os.getenv("OLLAMA_MODEL", "qwen3:9b")
    LLM_TIMEOUT: int     = int(os.getenv("LLM_TIMEOUT", "300"))

    # Алиас для совместимости с кодом использующим LM Studio
    @property
    def LM_STUDIO_URL(self) -> str:
        return self.OLLAMA_BASE_URL

    # -------------------------------------------------------------------------
    # OCR
    # -------------------------------------------------------------------------
    EASYOCR_LANGUAGES: list = os.getenv("EASYOCR_LANGUAGES", "ru,en").split(",")
    EASYOCR_GPU: bool       = os.getenv("EASYOCR_GPU", "false").lower() == "true"

    # -------------------------------------------------------------------------
    # Логирование
    # -------------------------------------------------------------------------
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # -------------------------------------------------------------------------
    # Пути — свойства (вычисляются из DATA_DIR)
    # -------------------------------------------------------------------------
    @property
    def CLAIMS_DIR(self) -> Path:
        return self.DATA_DIR / "claims"

    @property
    def CLAIMS_FILES_DIR(self) -> Path:
        return self.DATA_DIR / "claims" / "files"

    @property
    def CLAIMS_REGISTRY_FILE(self) -> Path:
        return self.DATA_DIR / "claims" / "registry.jsonl"

    @property
    def FEEDBACK_DIR(self) -> Path:
        return self.DATA_DIR / "feedback"

    @property
    def PROTOCOL_BOT_DIR(self) -> Path:
        return self.DATA_DIR / "protocol_bot"

    # -------------------------------------------------------------------------
    # Конфигурационные файлы
    # -------------------------------------------------------------------------
    @property
    def PROMPTS_FILE(self) -> Path:
        return self.CONFIG_DIR / "prompts.json"

    @property
    def DOC_SPHERES_FILE(self) -> Path:
        return self.CONFIG_DIR / "doc_spheres.json"

    @property
    def PROTOCOL_META_FILE(self) -> Path:
        return self.CONFIG_DIR / "protocol_meta.json"

    @property
    def ADVISOR_CONFIG_FILE(self) -> Path:
        return self.CONFIG_DIR / "advisor_config.json"

    @property
    def PREDICTOR_CONFIG_FILE(self) -> Path:
        return self.CONFIG_DIR / "predictor_config.json"

    @property
    def SEARCH_SETTINGS_FILE(self) -> Path:
        return self.CONFIG_DIR / "search_settings.json"

    # -------------------------------------------------------------------------
    # Создание необходимых директорий
    # -------------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        dirs = [
            self.DATA_DIR,
            self.CONFIG_DIR,
            self.CLAIMS_DIR,
            self.CLAIMS_FILES_DIR,
            self.FEEDBACK_DIR,
            self.PROTOCOL_BOT_DIR,
            self.PROTOCOL_BOT_DIR / "protocols",
            self.PROTOCOL_BOT_DIR / "temp",
            self.RAW_DOCS_PATH,
            self.VECTOR_DB_PATH,
            self.LOGS_DIR,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return (
            f"Settings("
            f"BASE={self.BASE_DIR}, "
            f"OLLAMA={self.OLLAMA_BASE_URL}, "
            f"MODEL={self.OLLAMA_MODEL}"
            f")"
        )


# Единственный экземпляр — импортируй его везде
settings = Settings()