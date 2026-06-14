# =============================================================================
# core/settings.py
# =============================================================================
# Единое место для всех настроек и путей.
# Значения берутся из переменных окружения (для Docker),
# с разумными дефолтами (для локальной разработки).
#
# Использование в любом модуле:
#   from core.settings import settings
#   url = settings.OLLAMA_BASE_URL
# =============================================================================

from __future__ import annotations
import os
from pathlib import Path


class Settings:
    """
    Все настройки приложения в одном месте.
    Читает из переменных окружения — работает и локально, и в Docker.
    """

    # -------------------------------------------------------------------------
    # Корень проекта — определяем автоматически
    # -------------------------------------------------------------------------
    BASE_DIR: Path = Path(__file__).parent.parent.resolve()

    # -------------------------------------------------------------------------
    # Пути к данным
    # В Docker они приходят из .env и монтируются через volumes.
    # Локально — дефолты относительно BASE_DIR.
    # -------------------------------------------------------------------------
    DATA_DIR: Path      = Path(os.getenv("DATA_DIR",      str(BASE_DIR / "data")))
    CONFIG_DIR: Path    = Path(os.getenv("CONFIG_DIR",    str(BASE_DIR / "config")))
    RAW_DOCS_PATH: Path = Path(os.getenv("RAW_DOCS_PATH", str(BASE_DIR / "data" / "raw")))
    LOGS_DIR: Path      = Path(os.getenv("LOGS_DIR",      str(BASE_DIR / "logs")))

    # Пути к конкретным данным
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
    # ChromaDB
    # -------------------------------------------------------------------------
    VECTOR_DB_PATH: Path = Path(os.getenv("VECTOR_DB_PATH", str(BASE_DIR / "data" / "vector_db")))
    CHROMA_PERSIST_DIR: Path = Path(os.getenv("CHROMA_PERSIST_DIR", str(BASE_DIR / "data" / "vector_db")))

    # -------------------------------------------------------------------------
    # LLM — Ollama
    # -------------------------------------------------------------------------
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    OLLAMA_MODEL: str    = os.getenv("OLLAMA_MODEL", "qwen3:9b")
    LLM_TIMEOUT: int     = int(os.getenv("LLM_TIMEOUT", "300"))

    # Для обратной совместимости с кодом, использующим LM Studio URL
    @property
    def LM_STUDIO_URL(self) -> str:
        """Алиас для OLLAMA_BASE_URL — упрощает миграцию с LM Studio."""
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

    # -------------------------------------------------------------------------
    # Создание необходимых директорий
    # -------------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        """Создаёт все нужные директории если они не существуют."""
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
            f"OLLAMA={self.OLLAMA_BASE_URL}, "
            f"MODEL={self.OLLAMA_MODEL}, "
            f"DATA={self.DATA_DIR}"
            f")"
        )


# Единственный экземпляр — импортируй его везде
settings = Settings()
