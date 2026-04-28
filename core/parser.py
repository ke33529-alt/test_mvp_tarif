# core/parser.py
import pandas as pd
import re
import os
import json
from typing import List, Dict, Any
from difflib import SequenceMatcher

# Таксономия статей затрат
COST_TAXONOMY = {
    "PAYROLL": ["оплат.*труд", "ФОТ", "зарплат", "персонал", "АУП", "ППР", "общепроизводствен"],
    "REPAIRS": ["ремонт.*основн", "ТОиР", "восстановительн", "ремонт.*оборуд"],
    "SOFTWARE": ["ПО", "программ.*обеспеч", "1С", "лицензи", "картридж", "компьютер"],
    "ADMIN": ["хозяйствен", "канцеляр", "почтов", "офис", "делопроизводств"],
    "ENERGY": ["энергоресурс", "топливо", "газ", "электроэнерг", "тепло", "вода"],
    "RENT": ["аренд", "лизинг"],
    "TRANSFER": ["передач", "ТТК", "ТТС", "услуг.*передач"],
    "OTHER": ["проч", "иные", "друг"]
}

def normalize_cost_name(name: str) -> str:
    """Приводит название статьи к категории"""
    if not name or pd.isna(name):
        return "UNKNOWN"
    name_lower = str(name).lower()
    for category, keywords in COST_TAXONOMY.items():
        for kw in keywords:
            if re.search(kw, name_lower):
                return category
    return "OTHER"

def extract_all_numbers(text: str) -> List[float]:
    """Извлекает все числа из текста"""
    nums = []
    pattern = r'[\d]+[.,]?[\d]*'
    matches = re.findall(pattern, str(text))
    for m in matches:
        try:
            num = float(m.replace(',', '.'))
            if num > 0:
                nums.append(num)
        except:
            pass
    return nums

def parse_excel_raw(file_path: str) -> List[Dict]:
    """Сырой парсинг Excel — достаём ВСЁ без фильтрации"""
    raw_items = []
    
    try:
        all_sheets = pd.read_excel(file_path, sheet_name=None, header=None)
        
        for sheet_name, df in all_sheets.items():
            for r_idx, row in df.iterrows():
                row_text = " | ".join([str(v) for v in row.values if pd.notna(v)])
                
                if len(row_text) < 5:
                    continue
                
                raw_items.append({
                    "source_file": os.path.basename(file_path),
                    "sheet": str(sheet_name),
                    "row": int(r_idx),
                    "raw_text": row_text[:500],
                    "all_numbers": extract_all_numbers(row_text)
                })
    
    except Exception as e:
        raw_items.append({"error": str(e), "source_file": os.path.basename(file_path)})
    
    return raw_items

def parse_word_raw(file_path: str) -> List[Dict]:
    """Сырой парсинг Word"""
    raw_items = []
    
    try:
        from docx import Document
        doc = Document(file_path)
        
        for idx, para in enumerate(doc.paragraphs):
            text = para.text.strip()
            if len(text) < 5:
                continue
            
            raw_items.append({
                "source_file": os.path.basename(file_path),
                "sheet": "Document",
                "row": idx,
                "raw_text": text[:500],
                "all_numbers": extract_all_numbers(text)
            })
    
    except Exception as e:
        raw_items.append({"error": str(e), "source_file": os.path.basename(file_path)})
    
    return raw_items

def parse_pdf_raw(file_path: str) -> List[Dict]:
    """Сырой парсинг PDF"""
    raw_items = []
    
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                text = page.extract_text()
                if not text:
                    continue
                
                for idx, line in enumerate(text.split('\n')):
                    if len(line) < 5:
                        continue
                    
                    raw_items.append({
                        "source_file": os.path.basename(file_path),
                        "sheet": f"Page {page_num + 1}",
                        "row": idx,
                        "raw_text": line[:500],
                        "all_numbers": extract_all_numbers(line)
                    })
    
    except Exception as e:
        raw_items.append({"error": str(e), "source_file": os.path.basename(file_path)})
    
    return raw_items

def parse_file_raw(file_path: str) -> List[Dict]:
    """Универсальный сырой парсер"""
    if file_path.endswith('.xlsx') or file_path.endswith('.xls'):
        return parse_excel_raw(file_path)
    elif file_path.endswith('.docx'):
        return parse_word_raw(file_path)
    elif file_path.endswith('.pdf'):
        return parse_pdf_raw(file_path)
    else:
        return [{"error": f"Неподдерживаемый формат: {file_path}", "source_file": os.path.basename(file_path)}]

def normalize_with_ai(raw_items: List[Dict], llm_client=None) -> List[Dict]:
    """
    AI-нормализация: превращает сырые данные в структурированный массив
    Использует Llama 3 через Ollama
    """
    if not raw_items or len(raw_items) == 0:
        return []
    
    # Формируем промпт для LLM
    prompt = build_normalization_prompt(raw_items)
    
    # Вызываем LLM
    try:
        import ollama
        response = ollama.chat(model='llama3', messages=[
            {'role': 'system', 'content': 'Ты — эксперт по тарифному регулированию. Твоя задача — извлечь статьи затрат из сырых данных и привести к единому формату.'},
            {'role': 'user', 'content': prompt}
        ])
        
        ai_output = response['message']['content']
        
        # Парсим ответ LLM (ожидаем JSON)
        structured_data = parse_llm_json(ai_output)
        
        return structured_data
    
    except Exception as e:
        # Fallback: возвращаем сырые данные с пометкой
        for item in raw_items:
            item['normalized'] = False
            item['error'] = f"AI-нормализация не удалась: {str(e)}"
        return raw_items

def build_normalization_prompt(raw_items: List[Dict]) -> str:
    """Строит промпт для LLM"""
    # Берём первые 50 строк (чтобы не превысить лимит токенов)
    sample = raw_items[:50]
    
    prompt = """
Ты — эксперт по тарифному регулированию в РФ.

ЗАДАЧА:
Извлеки статьи затрат из сырых данных и приведи к единому формату JSON.

ФОРМАТ ОТВЕТА (строго JSON массив):
[
  {
    "name": "Наименование статьи затрат",
    "unit": "Ед. измерения (тыс. руб., гкал, чел. и т.д.)",
    "years": [2024, 2025, 2026],
    "values": [1000, 1100, 1200],
    "category": "PAYROLL|REPAIRS|SOFTWARE|ENERGY|OTHER"
  }
]

ПРАВИЛА:
1. Извлекай только статьи затрат в соответствии с нормативкой (расходы, услуги, налоги и т.п.)
2. Игнорируй служебные строки (ИТОГО, Приложение, № п/п)
3. Определяй годы из контекста (2024, 2025, 2026 или другие)
4. Если значений несколько — сопоставь их с годами по порядку
5. Удаляй дубликаты (одинаковые названия + одинаковые суммы)
6. Категория должна быть одной из: PAYROLL, REPAIRS, SOFTWARE, ENERGY, ADMIN, TRANSFER, OTHER

СЫРЫЕ ДАННЫЕ:
"""
    
    for item in sample:
        prompt += f"\n- {item.get('raw_text', '')[:200]}"
    
    prompt += "\n\nВЕРНИ ТОЛЬКО JSON МАССИВ (без пояснений):"
    
    return prompt

def parse_llm_json(text: str) -> List[Dict]:
    """Парсит JSON из ответа LLM"""
    # Ищем JSON в тексте
    import re
    json_match = re.search(r'\[[\s\S]*\]', text)
    
    if json_match:
        try:
            data = json.loads(json_match.group())
            for item in data:
                item['normalized'] = True
                item['category'] = normalize_cost_name(item.get('name', ''))
            return data
        except:
            pass
    
    return []

def detect_duplicates(items: List[Dict], threshold: float = 0.85) -> List[Dict]:
    """Находит и удаляет дубликаты"""
    for i, item in enumerate(items):
        item['is_duplicate'] = False
        
        for j, other in enumerate(items):
            if i >= j:
                continue
            
            # Сравниваем названия
            name_sim = SequenceMatcher(None, 
                str(item.get('name', '')).lower(), 
                str(other.get('name', '')).lower()
            ).ratio()
            
            # Сравниваем суммы
            values_sim = (item.get('values') == other.get('values'))
            
            if name_sim >= threshold and values_sim:
                item['is_duplicate'] = True
                break
            elif name_sim >= 0.95:
                item['is_duplicate'] = True
                break
    
    return [item for item in items if not item.get('is_duplicate', False)]

def calculate_gross_revenue(items: List[Dict]) -> Dict[str, Any]:
    """Считает валовую выручку по годам"""
    # Динамически определяем годы из данных
    all_years = set()
    for item in items:
        years = item.get('years', [])
        if isinstance(years, list):
            all_years.update(years)
    
    revenue = {str(year): 0 for year in sorted(all_years)}
    
    for item in items:
        years = item.get('years', [])
        values = item.get('values', [])
        
        for i, year in enumerate(years):
            if i < len(values):
                revenue[str(year)] += values[i]
    
    return {
        "gross_revenue": revenue,
        "total_items": len(items),
        "years": sorted(list(all_years))
    }

def parse_file(file_path: str, use_ai: bool = True) -> Dict[str, Any]:
    """
    Полный пайплайн: сырой парсинг → AI-нормализация → дедупликация → итог
    """
    # Этап 1: Сырой парсинг
    raw_items = parse_file_raw(file_path)
    
    # Фильтруем ошибки
    raw_items = [item for item in raw_items if 'error' not in item]
    
    if not raw_items:
        return {"items": [], "errors": [{"message": "Нет данных для парсинга"}], "summary": {}}
    
    # Этап 2: AI-нормализация
    if use_ai:
        normalized_items = normalize_with_ai(raw_items)
    else:
        normalized_items = raw_items
    
    # Этап 3: Дедупликация
    unique_items = detect_duplicates(normalized_items)
    
    # Этап 4: Считаем валовую выручку
    summary = calculate_gross_revenue(unique_items)
    
    return {
        "items": unique_items,
        "raw_count": len(raw_items),
        "normalized_count": len(unique_items),
        "duplicates_removed": len(normalized_items) - len(unique_items),
        "summary": summary
    }