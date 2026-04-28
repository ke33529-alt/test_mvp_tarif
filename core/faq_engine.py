# core/faq_engine.py
import os
import json
import numpy as np
from typing import List, Dict, Optional, Tuple
from datetime import datetime

class FAQEngine:
    """Движок для обработки частозадаваемых вопросов (v2.0)"""
    
    def __init__(self, faq_path: str = "data/faq.json"):
        self.faq_path = faq_path
        self.faqs = []
        self.embeddings = None
        self.faq_vectors = None
        self._load_faqs()
    
    def _load_faqs(self):
        """Загружает FAQ из JSON"""
        if not os.path.exists(self.faq_path):
            print(f"[FAQ] Файл не найден: {self.faq_path}")
            return
        
        try:
            with open(self.faq_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.faqs = data.get("faqs", [])
                print(f"[FAQ] Загружено {len(self.faqs)} вопросов")
        except Exception as e:
            print(f"[FAQ] Ошибка загрузки: {e}")
    
    def _init_embeddings(self):
        """Инициализирует эмбеддинги (ленивая загрузка)"""
        if self.embeddings is None:
            try:
                from langchain_community.embeddings import HuggingFaceEmbeddings
                self.embeddings = HuggingFaceEmbeddings(
                    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    model_kwargs={"device": "cpu"},
                    encode_kwargs={"normalize_embeddings": True}
                )
                self._vectorize_faqs()
            except Exception as e:
                print(f"[FAQ] Ошибка инициализации эмбеддингов: {e}")
    
    def _vectorize_faqs(self):
        """Векторизует все вопросы для быстрого поиска"""
        if not self.embeddings or not self.faqs:
            return
        
        texts = []
        for faq in self.faqs:
            text = faq["question"] + " " + " ".join(faq.get("variations", []))
            texts.append(text)
        
        try:
            self.faq_vectors = self.embeddings.embed_documents(texts)
            print(f"[FAQ] Векторизовано {len(self.faq_vectors)} вопросов")
        except Exception as e:
            print(f"[FAQ] Ошибка векторизации: {e}")
    
    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Вычисляет косинусное сходство"""
        v1, v2 = np.array(vec1), np.array(vec2)
        return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-8))
    
    def find_match(self, question: str, threshold: float = 0.80) -> Optional[Dict]:
        """Ищет лучший ответ в базе FAQ"""
        if not self.faqs:
            return None
        
        self._init_embeddings()
        if not self.embeddings or not self.faq_vectors:
            return None
        
        try:
            question_vec = self.embeddings.embed_query(question)
            
            best_score = 0
            best_faq = None
            
            for i, faq in enumerate(self.faqs):
                score = self._cosine_similarity(question_vec, self.faq_vectors[i])
                faq_threshold = faq.get("confidence_threshold", threshold)
                
                if score > best_score and score >= faq_threshold:
                    best_score = score
                    best_faq = faq
            
            if best_faq:
                print(f"[FAQ] Найдено совпадение: {best_faq['id']} (score: {best_score:.3f})")
                return {
                    "answer": best_faq["answer"],
                    "source": best_faq.get("source", ""),
                    "category": best_faq.get("category", ""),
                    "confidence": best_score,
                    "from_faq": True
                }
            
            print(f"[FAQ] Не найдено совпадений (лучший: {best_score:.3f} < {threshold})")
            return None
            
        except Exception as e:
            print(f"[FAQ] Ошибка поиска: {e}")
            return None
    
    def add_faq(self, question: str, answer: str, source: str = "", 
                variations: List[str] = None, category: str = "general"):
        """Добавляет новый FAQ"""
        new_faq = {
            "id": f"faq_{len(self.faqs) + 1:03d}",
            "question": question,
            "variations": variations or [],
            "answer": answer,
            "source": source,
            "category": category,
            "confidence_threshold": 0.85
        }
        self.faqs.append(new_faq)
        self._save_faqs()
        self._vectorize_faqs()
    
    def _save_faqs(self):
        """Сохраняет FAQ обратно в файл"""
        try:
            data = {
                "version": "1.0",
                "updated": datetime.now().isoformat(),
                "faqs": self.faqs
            }
            os.makedirs(os.path.dirname(self.faq_path), exist_ok=True)
            with open(self.faq_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[FAQ] Ошибка сохранения: {e}")
    
    def get_stats(self) -> Dict:
        """Возвращает статистику FAQ"""
        categories = {}
        for faq in self.faqs:
            cat = faq.get("category", "other")
            categories[cat] = categories.get(cat, 0) + 1
        
        return {
            "total": len(self.faqs),
            "categories": categories,
            "avg_variations": sum(len(f.get("variations", [])) for f in self.faqs) / max(len(self.faqs), 1)
        }


# =============================================================================
# Глобальный экземпляр (ИСПРАВЛЕНО)
# =============================================================================

_faq_engine = None

def get_faq_engine() -> FAQEngine:
    """Возвращает или создаёт экземпляр советчика"""
    global _faq_engine  # ← global ДОЛЖЕН быть первым в функции!
    if _faq_engine is None:
        _faq_engine = FAQEngine()
    return _faq_engine

def ask_faq(question: str, threshold: float = 0.80) -> Optional[Dict]:
    """Удобная функция для вызова из интерфейса"""
    engine = get_faq_engine()
    return engine.find_match(question, threshold)