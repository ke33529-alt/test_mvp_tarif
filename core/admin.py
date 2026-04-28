# core/admin.py
import os
import json
import shutil
import subprocess
import sys
from datetime import datetime
from typing import List, Dict

# =============================================================================
# 🔐 Настройки
# =============================================================================
ADMIN_PASSWORD = "admin123"  # 🔒 Поменяй на свой пароль

DATA_DIR = "data"
FILES_DIR = os.path.join(DATA_DIR, "test_files")
RAW_DOCS_DIR = os.path.join(DATA_DIR, "raw")
FEEDBACK_FILE = os.path.join(DATA_DIR, "feedback", "feedback_log.json")
METADATA_FILE = os.path.join(DATA_DIR, "vector_db", "indexing_metadata.json")

# =============================================================================
# 🔐 Проверка пароля
# =============================================================================
def check_admin(password: str) -> bool:
    """Проверяет пароль администратора"""
    return password == ADMIN_PASSWORD

# =============================================================================
# 📁 Файлы
# =============================================================================
def get_all_files() -> List[Dict]:
    """Возвращает список всех загруженных файлов (test_files)"""
    files = []
    if os.path.exists(FILES_DIR):
        for f in os.listdir(FILES_DIR):
            if f.startswith('.'):
                continue
            path = os.path.join(FILES_DIR, f)
            stat = os.stat(path)
            files.append({
                "name": f,
                "size": stat.st_size,
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "path": path
            })
    return files

def delete_file(file_path: str) -> bool:
    """Удаляет файл"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
    except Exception as e:
        print(f"Error deleting file: {e}")
    return False

# =============================================================================
# 📚 Документы базы знаний (raw)
# =============================================================================
def get_raw_files_with_status() -> List[Dict]:
    """Возвращает список raw-файлов со статусом индексации"""
    metadata = load_indexing_metadata()
    
    files = []
    if os.path.exists(RAW_DOCS_DIR):
        for f in os.listdir(RAW_DOCS_DIR):
            if f.startswith('.'):
                continue
            path = os.path.join(RAW_DOCS_DIR, f)
            stat = os.stat(path)
            
            meta = metadata.get(f, {})
            
            files.append({
                "name": f,
                "size": stat.st_size,
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "indexed": meta.get("indexed", False),
                "indexed_at": meta.get("indexed_at", None),
                "in_training": meta.get("in_training", True),
                "chunks": meta.get("chunks", 0),
                "path": path
            })
    
    return files

def add_document(file_path: str, category: str = "general") -> bool:
    """Добавляет документ в базу знаний"""
    try:
        os.makedirs(RAW_DOCS_DIR, exist_ok=True)
        dest = os.path.join(RAW_DOCS_DIR, os.path.basename(file_path))
        shutil.copy(file_path, dest)
        return True
    except Exception as e:
        print(f"Error adding document: {e}")
        return False

# =============================================================================
# 📊 Метаданные индексации
# =============================================================================
def load_indexing_metadata() -> Dict:
    """Загружает метаданные индексации"""
    if os.path.exists(METADATA_FILE):
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_indexing_metadata(metadata: Dict) -> bool:
    """Сохраняет метаданные индексации"""
    try:
        os.makedirs(os.path.dirname(METADATA_FILE), exist_ok=True)
        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"Error saving metadata: {e}")
        return False

def toggle_file_training(file_name: str, in_training: bool) -> bool:
    """Включает/выключает файл из обучения"""
    metadata = load_indexing_metadata()
    
    if file_name not in metadata:
        metadata[file_name] = {}
    
    metadata[file_name]["in_training"] = in_training
    metadata[file_name]["updated_at"] = datetime.now().isoformat()
    
    return save_indexing_metadata(metadata)

def mark_as_indexed(file_name: str, chunks: int = 0) -> bool:
    """Помечает файл как проиндексированный"""
    metadata = load_indexing_metadata()
    
    if file_name not in metadata:
        metadata[file_name] = {}
    
    metadata[file_name]["indexed"] = True
    metadata[file_name]["indexed_at"] = datetime.now().isoformat()
    metadata[file_name]["chunks"] = chunks
    
    return save_indexing_metadata(metadata)

def reset_indexing_status(file_name: str) -> bool:
    """Сбрасывает статус индексации файла"""
    metadata = load_indexing_metadata()
    
    if file_name in metadata:
        metadata[file_name]["indexed"] = False
        metadata[file_name]["indexed_at"] = None
        metadata[file_name]["chunks"] = 0
        return save_indexing_metadata(metadata)
    
    return True

# =============================================================================
# 📝 Отзывы
# =============================================================================
def get_feedback() -> List[Dict]:
    """Возвращает все отзывы"""
    if not os.path.exists(FEEDBACK_FILE):
        return []
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

# =============================================================================
# 📊 Статистика
# =============================================================================
def get_stats() -> Dict:
    """Возвращает статистику системы"""
    files = get_all_files()
    feedback = get_feedback()
    raw_docs = get_raw_files_with_status()
    
    return {
        "total_files": len(files),
        "total_feedback": len(feedback),
        "new_feedback": len([f for f in feedback if f.get("status") == "new"]),
        "raw_docs": len(raw_docs)
    }

# =============================================================================
# 🔄 Переиндексация
# =============================================================================
def trigger_reindex() -> Dict:
    """Запускает переиндексацию базы знаний в том же venv"""
    import sys
    
    try:
        # Используем тот же Python, что и текущий процесс (venv)
        python_executable = sys.executable
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        print(f"[DEBUG] Python: {python_executable}")
        print(f"[DEBUG] Проект: {project_root}")
        
        # Копируем окружение + добавляем PYTHONPATH
        env = os.environ.copy()
        env["PYTHONPATH"] = project_root
        
        result = subprocess.run(
            [python_executable, "core/indexer.py"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=300,
            env=env
        )
        
        # Выводим всё для отладки
        print(f"[DEBUG] Return code: {result.returncode}")
        print(f"[DEBUG] STDOUT:\n{result.stdout}")
        print(f"[DEBUG] STDERR:\n{result.stderr}")
        
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "error": result.stderr
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Превышено время ожидания (300 сек)"}
    except Exception as e:
        return {"success": False, "error": f"Исключение: {str(e)}\n{repr(e)}"}