import os

# Пути
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
RAW_DOCS_DIR = os.path.join(DATA_DIR, "raw")
VECTOR_DB_DIR = os.path.join(DATA_DIR, "vector_db")
TEST_FILES_DIR = os.path.join(DATA_DIR, "test_files")

# Модель (должна быть скачана в Ollama)
LLM_MODEL = "llama3" 
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Создаем папки, если нет
for dir_path in [DATA_DIR, RAW_DOCS_DIR, VECTOR_DB_DIR, TEST_FILES_DIR]:
    os.makedirs(dir_path, exist_ok=True)