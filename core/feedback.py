# core/feedback.py
import os
import sys
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# =============================================================================
# 📁 Пути (абсолютные, надёжные)
# =============================================================================

# Корень проекта: C:\tariff_ai_mvp
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDBACK_FILE = os.path.join(BASE_DIR, "data", "feedback", "feedback_log.jsonl")

# Гарантируем существование папки
os.makedirs(os.path.dirname(FEEDBACK_FILE), exist_ok=True)

# =============================================================================
# 📝 Сохранение отзыва
# =============================================================================

def submit_feedback(
    user_type: str,
    feedback_type: str,
    description: str,
    file_name: str = None,
    expected_result: str = None,
    question: str = None,
    answer: str = None,
    rating: int = None,
    category: str = None
):
    """
    Сохраняет отзыв пользователя в JSONL-файл
    
    Параметры:
    - rating: 3 = 👍 Полезно, 2 = 😐 Нормально, 1 = 👎 Не помогло
    """
    try:
        entry = {
            "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "timestamp": datetime.now().isoformat(),
            "user_type": user_type,
            "feedback_type": feedback_type,
            "description": description,
            "file_name": file_name,
            "expected_result": expected_result,
            "question": question,
            "answer": answer,
            "rating": rating,
            "category": category
        }
        
        # Пишем в файл (append mode)
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        
        # 🔍 DEBUG: лог в консоль
        print(f"[FEEDBACK SAVED] id={entry['id'][:12]}... rating={rating} type={feedback_type}")
        
        return entry["id"]
        
    except Exception as e:
        print(f"[FEEDBACK ERROR] Не удалось сохранить: {e}")
        import traceback
        traceback.print_exc()
        return None

# =============================================================================
# 📖 Чтение отзывов
# =============================================================================

def get_feedback(
    limit: int = 100,
    feedback_type: str = None,
    rating: int = None
) -> List[Dict]:
    """
    Загружает отзывы из файла с фильтрацией
    
    Returns:
        Список словарей с отзывами (новые первые)
    """
    if not os.path.exists(FEEDBACK_FILE):
        print(f"[FEEDBACK] Файл не найден: {FEEDBACK_FILE}")
        return []
    
    feedbacks = []
    
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    
                    # Фильтр по типу
                    if feedback_type and entry.get("feedback_type") != feedback_type:
                        continue
                    
                    # Фильтр по рейтингу
                    if rating is not None and entry.get("rating") != rating:
                        continue
                    
                    feedbacks.append(entry)
                    
                except json.JSONDecodeError as e:
                    print(f"[FEEDBACK] Ошибка парсинга строки {line_num}: {e}")
                    continue
                    
    except Exception as e:
        print(f"[FEEDBACK ERROR] Не удалось прочитать файл: {e}")
        return []
    
    # Сортировка: новые первые
    feedbacks.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    
    return feedbacks[:limit]

# =============================================================================
# 📊 Статистика по оценкам ответов
# =============================================================================

def get_answer_stats(days: int = 7) -> Dict:
    """
    Считает статистику по оценкам ответов ИИ
    
    Returns:
        Dict с метриками: total, rating_3/2/1, avg_rating, top_bad_questions...
    """
    cutoff = datetime.now() - timedelta(days=days)
    
    stats = {
        "total": 0,
        "rating_3": 0,  # 👍
        "rating_2": 0,  # 😐
        "rating_1": 0,  # 👎
        "with_comment": 0,
        "by_category": {},
        "top_bad_questions": [],
        "avg_rating": 0
    }
    
    # Получаем все отзывы
    feedbacks = get_feedback()
    
    for fb in feedbacks:
        # 🔧 БЕЗ фильтра по дате — считаем ВСЕ записи типа answer_rating
        # (фильтр по дате ненадёжен из-за формата timestamp)
        
        # Только оценки ответов ИИ
        if fb.get("feedback_type") != "answer_rating":
            continue
        
        stats["total"] += 1
        
        rating = fb.get("rating")
        
        if rating == 3:
            stats["rating_3"] += 1
        elif rating == 2:
            stats["rating_2"] += 1
        elif rating == 1:
            stats["rating_1"] += 1
            # Сохраняем проблемные вопросы для улучшения
            if fb.get("question"):
                stats["top_bad_questions"].append({
                    "question": fb["question"][:100],
                    "answer": fb.get("answer", "")[:200],
                    "comment": fb.get("description", ""),
                    "timestamp": fb["timestamp"]
                })
        
        # Считаем отзывы с содержательным комментарием
        desc = fb.get("description", "")
        if desc and len(desc) > 20 and desc not in [
            "Пользователь оценил ответ как полезный",
            "Пользователь оценил ответ как нормальный", 
            "Пользователь оценил ответ как бесполезный"
        ]:
            stats["with_comment"] += 1
        
        # Группировка по категориям (если есть)
        category = fb.get("category", "general")
        if category not in stats["by_category"]:
            stats["by_category"][category] = {"3": 0, "2": 0, "1": 0}
        stats["by_category"][category][str(rating)] = stats["by_category"][category].get(str(rating), 0) + 1
    
    # Средний рейтинг
    if stats["total"] > 0:
        stats["avg_rating"] = round(
            (stats["rating_3"] * 3 + stats["rating_2"] * 2 + stats["rating_1"] * 1) / stats["total"],
            2
        )
    
    # Ограничиваем список проблемных вопросов
    stats["top_bad_questions"] = stats["top_bad_questions"][:10]
    
    # 🔍 DEBUG в консоль
    print(f"[STATS] total={stats['total']}, 👍={stats['rating_3']}, 😐={stats['rating_2']}, 👎={stats['rating_1']}, avg={stats['avg_rating']}")
    
    return stats

# =============================================================================
# 🧹 Утилиты
# =============================================================================

def clear_feedback_file():
    """Очищает файл отзывов (для тестов)"""
    if os.path.exists(FEEDBACK_FILE):
        open(FEEDBACK_FILE, "w").close()
        print(f"[FEEDBACK] Файл очищен: {FEEDBACK_FILE}")

def get_feedback_count() -> int:
    """Возвращает общее количество записей в файле"""
    if not os.path.exists(FEEDBACK_FILE):
        return 0
    with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())

# =============================================================================
# 🧪 Тест при прямом запуске
# =============================================================================

if __name__ == "__main__":
    print("🔍 Тест core/feedback.py\n")
    
    # 1. Сохраняем тестовую оценку
    print("1. Сохраняем оценку 👍...")
    submit_feedback(
        user_type="test",
        feedback_type="answer_rating",
        description="Тестовый комментарий",
        question="Тест: что такое тариф?",
        answer="Тестовый ответ",
        rating=3
    )
    
    # 2. Показываем статистику
    print("\n2. Статистика:")
    stats = get_answer_stats(days=7)
    print(f"   Всего: {stats['total']}")
    print(f"   👍: {stats['rating_3']}, 😐: {stats['rating_2']}, 👎: {stats['rating_1']}")
    print(f"   Средний рейтинг: {stats['avg_rating']}")
    
    # 3. Показываем количество записей
    print(f"\n3. Записей в файле: {get_feedback_count()}")
    
    print("\n✅ Тест завершён!")