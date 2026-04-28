# core/summarizer.py
import os
import json
import hashlib
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List

# =============================================================================
# Конфигурация
# =============================================================================
CONFIG_FILE = os.path.join("config", "summarizer_config.json")
DEFAULT_CONFIG = {
    "ollama_url": "http://localhost:11434",
    "default_model": "phi3",  # ✅ phi3 для 4GB VRAM
    "cache_dir": os.path.join("data", "doc_scanner", "summaries"),
    "cache_ttl_days": 7,
    "max_text_length": 8000,
    "timeout_seconds": 300,
    "folder_timeout_seconds": 600,
    "gpu_enabled": True,
    "fallback_to_cpu": True
}

def load_config() -> Dict:
    """Загружает конфигурацию суммаризатора"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return {**DEFAULT_CONFIG, **config}
        except:
            pass
    return DEFAULT_CONFIG

def save_config(config: Dict):
    """Сохраняет конфигурацию"""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

CONFIG = load_config()

# =============================================================================
# Управление моделями
# =============================================================================
def get_available_models() -> List[Dict]:
    """
    Получает список доступных моделей от Ollama
    Returns:
        list: [{"name": "llama3", "size": "4.9 GB", ...}, ...]
    """
    try:
        config = load_config()
        ollama_url = config.get("ollama_url", "http://localhost:11434")
        
        response = requests.get(f"{ollama_url}/api/tags", timeout=10)
        
        if response.status_code == 200:
            models = response.json().get("models", [])
            return [
                {
                    "name": m.get("name", "unknown"),
                    "size": _format_size(m.get("size", 0)),
                    "family": m.get("details", {}).get("family", "unknown")
                }
                for m in models
            ]
        return []
    except:
        return []

def _format_size(size_bytes: int) -> str:
    """Форматирует размер модели в GB"""
    if size_bytes == 0:
        return "N/A"
    size_gb = size_bytes / (1024 ** 3)
    return f"{size_gb:.1f} GB"

def check_model_available(model_name: str) -> bool:
    """Проверяет, доступна ли модель"""
    models = get_available_models()
    return any(m["name"] == model_name for m in models)

# =============================================================================
# Проверка GPU статуса
# =============================================================================
def check_gpu_status() -> Dict:
    """
    Проверяет, использует ли Ollama GPU
    Returns:
        dict: {"gpu_available": bool, "gpu_model": str, "message": str}
    """
    try:
        config = load_config()
        ollama_url = config.get("ollama_url", "http://localhost:11434")
        
        response = requests.get(f"{ollama_url}/api/tags", timeout=5)
        
        if response.status_code == 200:
            test_response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": CONFIG["default_model"],
                    "prompt": "test",
                    "stream": False
                },
                timeout=10
            )
            
            if test_response.status_code == 200:
                return {
                    "gpu_available": True,
                    "gpu_model": "NVIDIA CUDA (через Ollama)",
                    "message": "✅ Ollama работает. GPU используется автоматически если доступен."
                }
        
        return {
            "gpu_available": False,
            "gpu_model": "CPU",
            "message": "⚠️ Ollama работает на CPU. Для GPU установите NVIDIA драйверы."
        }
    
    except requests.exceptions.ConnectionError:
        return {
            "gpu_available": False,
            "gpu_model": "None",
            "message": "❌ Ollama Server не запущен. Выполните: ollama serve"
        }
    except Exception as e:
        return {
            "gpu_available": False,
            "gpu_model": "Unknown",
            "message": f"⚠️ Ошибка проверки: {str(e)}"
        }

# =============================================================================
# Кэширование
# =============================================================================
def get_cache_key(text: str, max_length: int, model: str) -> str:
    """Генерирует уникальный ключ кэша для текста"""
    cache_string = f"{text[:1000]}|||{max_length}|||{model}"
    return hashlib.md5(cache_string.encode()).hexdigest()

def get_cached_summary(doc_id: str, text: str, max_length: int, model: str) -> Optional[str]:
    """Проверяет кэш суммаризаций"""
    cache_dir = CONFIG["cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{doc_id}_{model}.json")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            
            cached_time = datetime.fromisoformat(cached.get("created_at", ""))
            ttl = timedelta(days=CONFIG.get("cache_ttl_days", 7))
            
            if datetime.now() - cached_time < ttl:
                print(f"[CACHE HIT] {doc_id} ({model})")
                return cached.get("summary", "")
        except:
            pass
    
    return None

def save_summary_to_cache(doc_id: str, summary: str, model: str):
    """Сохраняет суммаризацию в кэш"""
    cache_dir = CONFIG["cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{doc_id}_{model}.json")
    
    cache_data = {
        "doc_id": doc_id,
        "model": model,
        "summary": summary,
        "created_at": datetime.now().isoformat()
    }
    
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(cache_data, f, ensure_ascii=False, indent=2)

# =============================================================================
# Суммаризация текста
# =============================================================================
def summarize_text(
    text: str,
    model: str = None,
    temperature: float = 0.3,
    max_length: int = 1024,
    language: str = "ru"
) -> str:
    """
    Генерирует краткий пересказ текста через локальную LLM
    
    Args:
        text: Исходный текст
        model: Модель Ollama (по умолчанию из конфига)
        temperature: Креативность (0.0-1.0)
        max_length: Максимальная длина ответа
        language: Язык ответа ('ru' или 'en')
    
    Returns:
        str: Сгенерированный пересказ
    """
    config = load_config()
    model = model or config.get("default_model", "phi3")  # ✅ phi3 по умолчанию
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    
    # Проверка GPU статуса (логирование)
    gpu_status = check_gpu_status()
    print(f"[GPU] {gpu_status['message']}")
    print(f"[MODEL] Используемая модель: {model}")
    
    # Ограничение текста
    max_len = config.get("max_text_length", 8000)
    truncated_text = text[:max_len] if len(text) > max_len else text
    
    lang_instruction = "на русском языке" if language == "ru" else "in English"
    
    # Промт для НЕЙТРАЛЬНОГО пересказа
    prompt = f"""Ты — профессиональный редактор. Сделай краткий, структурированный пересказ документа {lang_instruction}.

Требования к пересказу:
1. Сохрани ключевые факты, цифры, даты, названия документов
2. Используй нейтральный, деловой стиль
3. Структурируй ответ: кратко выдели суть в 3-5 пунктах
4. НЕ добавляй советы, рекомендации или интерпретации
5. НЕ упоминай, что ты ИИ-ассистент
6. НЕ ссылайся на другие разделы или документы
7. Если текст слишком короткий для пересказа — просто воспроизведи его

Документ для пересказа:
{truncated_text}

Пересказ:"""
    
    timeout = config.get("timeout_seconds", 300)
    
    try:
        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_length
                }
            },
            timeout=timeout
        )
        
        if response.status_code == 200:
            result = response.json().get("response", "❌ Не удалось сгенерировать пересказ")
            print(f"[LLM] Ответ сгенерирован ({len(result)} символов)")
            return result
        else:
            print(f"[LLM ERROR] Status {response.status_code}")
            return f"❌ Ошибка LLM: {response.status_code}"
    
    except requests.exceptions.Timeout:
        print(f"[TIMEOUT] Превышено время ожидания ({timeout} сек)")
        return f"⏱️ Таймаут: пересказ занимает больше {timeout//60} минут."
    
    except requests.exceptions.ConnectionError:
        print(f"[CONNECTION] Ollama Server недоступен")
        return "🔌 Ошибка подключения к Ollama. Убедитесь, что сервис запущен: ollama serve"
    
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {str(e)}")
        return f"❌ Ошибка: {type(e).__name__}: {str(e)}"

# =============================================================================
# Суммаризация документа (с кэшем)
# =============================================================================
def summarize_document(
    doc_id: str,
    text: str,
    model: str = None,
    temperature: float = 0.3,
    max_length: int = 1024,
    use_cache: bool = True
) -> dict:
    """
    Читает кэш или генерирует новую суммаризацию
    
    Returns:
        dict: {
            "status": "success" | "error",
            "summary": str | None,
            "from_cache": bool,
            "error": str | None,
            "model": str
        }
    """
    config = load_config()
    model = model or config.get("default_model", "phi3")
    
    result = {
        "status": "error",
        "summary": None,
        "from_cache": False,
        "error": None,
        "model": model
    }
    
    # Проверка кэша
    if use_cache:
        cached = get_cached_summary(doc_id, text, max_length, model)
        if cached:
            result["summary"] = cached
            result["from_cache"] = True
            result["status"] = "success"
            return result
    
    # Генерация новой суммаризации
    print(f"[SUMMARIZE] {doc_id} ({len(text)} символов, модель: {model})")
    summary = summarize_text(text, model, temperature, max_length)
    
    if summary.startswith("❌") or summary.startswith("⏱️") or summary.startswith("🔌"):
        result["error"] = summary
        return result
    
    # Сохранение в кэш
    if use_cache:
        save_summary_to_cache(doc_id, summary, model)
    
    result["summary"] = summary
    result["status"] = "success"
    return result

# =============================================================================
# Суммаризация папки документов
# =============================================================================
def summarize_folder(
    folder_id: str,
    documents: list,
    model: str = None,
    temperature: float = 0.3,
    max_length: int = 1500,
    use_cache: bool = True
) -> dict:
    """
    Генерирует сводное резюме по папке документов
    С указанием что в каком файле находится
    """
    config = load_config()
    model = model or config.get("default_model", "phi3")
    
    result = {
        "status": "error",
        "summary": None,
        "from_cache": False,
        "error": None,
        "model": model
    }
    
    # Проверка кэша
    if use_cache:
        cache_dir = os.path.join("data", "doc_scanner", "folder_summaries")
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, f"{folder_id}_{model}.json")
        
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                cached_time = datetime.fromisoformat(cached.get("created_at", ""))
                if datetime.now() - cached_time < timedelta(days=7):
                    result["summary"] = cached["summary"]
                    result["from_cache"] = True
                    result["status"] = "success"
                    print(f"[FOLDER CACHE HIT] {folder_id} ({model})")
                    return result
            except:
                pass
    
    # Формируем контекст по каждому документу (уменьшено для CPU)
    doc_contexts = []
    for doc in documents[:5]:  # Максимум 5 документов
        doc_name = doc.get("file_name", "Неизвестно")
        doc_text = "\n".join([page.get("text", "") for page in doc.get("pages", [])])[:1000]
        doc_contexts.append(f"📄 {doc_name}:\n{doc_text}")
    
    combined_context = "\n\n---\n\n".join(doc_contexts)
    
    prompt = f"""Ты — профессиональный редактор. Создай сводное резюме по папке документов на русском языке.

Требования к резюме:
1. Для КАЖДОГО файла укажи: название, тип, ключевое содержание
2. Выдели общие темы и связи между документами
3. Сохрани ключевые факты, цифры, даты, номера документов
4. Используй нейтральный, деловой стиль
5. Структурируй ответ по файлам
6. НЕ добавляй советы или рекомендации

Документы в папке:
{combined_context}

Сводное резюме:"""
    
    try:
        ollama_url = config.get("ollama_url", "http://localhost:11434")
        timeout = config.get("folder_timeout_seconds", 600)
        
        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_length
                }
            },
            timeout=timeout
        )
        
        if response.status_code == 200:
            summary = response.json().get("response", "❌ Не удалось сгенерировать резюме")
            
            # Сохранение в кэш
            if use_cache:
                cache_dir = os.path.join("data", "doc_scanner", "folder_summaries")
                os.makedirs(cache_dir, exist_ok=True)
                cache_file = os.path.join(cache_dir, f"{folder_id}_{model}.json")
                
                cache_data = {
                    "folder_id": folder_id,
                    "model": model,
                    "summary": summary,
                    "created_at": datetime.now().isoformat(),
                    "doc_count": len(documents)
                }
                
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)
            
            result["summary"] = summary
            result["status"] = "success"
            print(f"[FOLDER SUMMARIZE] {folder_id} ({len(documents)} документов, модель: {model})")
            return result
        else:
            result["error"] = f"❌ Ошибка LLM: {response.status_code}"
            return result
    
    except requests.exceptions.Timeout:
        result["error"] = f"⏱️ Таймаут: превышено {timeout} сек"
        return result
    except Exception as e:
        result["error"] = f"❌ Ошибка: {type(e).__name__}: {str(e)}"
        return result

# =============================================================================
# Утилиты
# =============================================================================
def clear_cache():
    """Очищает кэш суммаризаций"""
    cache_dir = CONFIG["cache_dir"]
    folder_cache_dir = os.path.join("data", "doc_scanner", "folder_summaries")
    
    for dir_path in [cache_dir, folder_cache_dir]:
        if os.path.exists(dir_path):
            import shutil
            shutil.rmtree(dir_path)
            os.makedirs(dir_path, exist_ok=True)
            print(f"[CACHE CLEARED] {dir_path}")

def get_cache_stats() -> Dict:
    """Возвращает статистику кэша"""
    cache_dir = CONFIG["cache_dir"]
    folder_cache_dir = os.path.join("data", "doc_scanner", "folder_summaries")
    
    total_files = 0
    total_size = 0
    
    for dir_path in [cache_dir, folder_cache_dir]:
        if os.path.exists(dir_path):
            files = os.listdir(dir_path)
            total_files += len(files)
            total_size += sum(os.path.getsize(os.path.join(dir_path, f)) for f in files if os.path.isfile(os.path.join(dir_path, f)))
    
    return {
        "total_files": total_files,
        "total_size_mb": round(total_size / 1024 / 1024, 2)
    }

# =============================================================================
# Запуск для теста
# =============================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🧪 Тест суммаризатора")
    print("=" * 60)
    
    # Проверка доступных моделей
    print("\n📦 Доступные модели:")
    models = get_available_models()
    if models:
        for m in models:
            print(f"  • {m['name']} ({m['size']}, {m['family']})")
    else:
        print("  ⚠️ Не удалось получить список моделей")
    
    # Проверка GPU
    gpu = check_gpu_status()
    print(f"\n📊 GPU Статус: {gpu['message']}")
    
    # Статистика кэша
    stats = get_cache_stats()
    print(f"\n📁 Кэш: {stats['total_files']} файлов, {stats['total_size_mb']} MB")
    
    # Тест суммаризации
    test_text = "Приказ ФАС №1746-э устанавливает методические указания по регулированию тарифов..."
    result = summarize_document("test_doc", test_text, model="phi3", max_length=200)
    
    print(f"\n✅ Результат: {result['status']}")
    print(f"📦 Модель: {result['model']}")
    if result['summary']:
        print(f"📝 Пересказ: {result['summary'][:100]}...")