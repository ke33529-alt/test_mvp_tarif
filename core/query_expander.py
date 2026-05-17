# core/query_expander.py
from typing import List

class QueryExpander:
    """Расширяет запрос пользователя для лучшего поиска"""
    
    # Синонимы для тарифной тематики
    SYNONYMS = {
        "дмс": ["добровольное медицинское страхование", "страхование сотрудников", "медстрахование"],
        "вв": ["валовая выручка", "выручка валовая", "общая выручка"],
        "фап": ["фактические данные", "факт", "фактические показатели"],
        "ауп": ["административно-управленческий персонал", "руководство", "аппарат управления"],
        "ос": ["основные средства", "имущество", "активы"],
        "тариф": ["тарифное регулирование", "ценообразование", "тарифы"],
        "фас": ["Федеральная антимонопольная служба", "антимонопольная служба", "регулятор"],
        "рэк": ["региональная энергетическая комиссия", "региональный регулятор"],
        "рсо": ["ресурсоснабжающая организация", "поставщик ресурсов", "исполнитель"],
        "нпа": ["нормативный правовой акт", "закон", "приказ", "постановление"],
        "фот": ["фонд оплаты труда", "зарплата", "заработная плата"],
        "амортизация": ["аморт", "износ", "амортизационные отчисления"],
    }
    
    # Типичные формулировки из документов
    REPHRASES = {
        "можно ли": ["правомерно ли", "допускается ли", "разрешено ли", "включается ли"],
        "как рассчитать": ["методика расчета", "порядок определения", "формула расчета"],
        "какие документы": ["перечень документов", "состав документов", "комплект документов"],
        "что такое": ["определение", "понятие", "трактовка"],
        "сколько": ["размер", "величина", "сумма"],
    }
    
    def expand(self, query: str) -> List[str]:
        """Возвращает список вариантов запроса для поиска"""
        import re as _re
        variants = [query]
        query_lower = query.lower()

        # 1. Синонимы — только по границе слова, чтобы не ломать окончания
        for short, full_list in self.SYNONYMS.items():
            pattern = _re.compile(r'\b' + _re.escape(short) + r'\b', _re.IGNORECASE)
            if pattern.search(query_lower):
                for full in full_list:
                    variant = pattern.sub(full, query_lower)
                    if variant != query_lower:
                        variants.append(variant)

        # 2. Перефразирования — только по границе слова
        for short, full_list in self.REPHRASES.items():
            pattern = _re.compile(_re.escape(short), _re.IGNORECASE)
            if pattern.search(query_lower):
                for full in full_list:
                    variant = pattern.sub(full, query_lower)
                    if variant != query_lower:
                        variants.append(variant)
        
        # 3. Добавляем ключевые слова тарифной тематики
        tariff_keywords = ["тариф", "регулирование", "фас", "приказ", "расходы", "выручка"]
        if not any(kw in query_lower for kw in tariff_keywords):
            variants.append(f"{query} тарифное регулирование")
        
        # Убираем дубликаты
        return list(set(variants))
    
    def expand_for_faq(self, query: str) -> str:
        """Расширяет запрос специально для FAQ-поиска"""
        variants = self.expand(query)
        return max(variants, key=len)

# Глобальный экземпляр
_expander = None

def get_expander() -> QueryExpander:
    global _expander
    if _expander is None:
        _expander = QueryExpander()
    return _expander

def expand_query(query: str) -> List[str]:
    return get_expander().expand(query)