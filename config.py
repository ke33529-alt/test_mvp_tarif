# config.py
# =============================================================================
# Обёртка для обратной совместимости.
# Все реальные настройки живут в core/settings.py
# Этот файл НЕ удалять — он нужен пока везде не заменим импорты.
# =============================================================================
from core.settings import settings

BASE_DIR        = str(settings.BASE_DIR)
DATA_DIR        = str(settings.DATA_DIR)
RAW_DOCS_DIR    = str(settings.RAW_DOCS_PATH)
VECTOR_DB_DIR   = str(settings.VECTOR_DB_PATH)
TEST_FILES_DIR  = str(settings.DATA_DIR / "test_files")
LLM_MODEL       = settings.OLLAMA_MODEL
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"