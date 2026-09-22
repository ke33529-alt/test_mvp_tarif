# streamlit_pages/predictor.py
"""
Прогноз решения регулятора
──────────────────────────────────────────────────────────────────────────────
Логика:
  1. Пользователь вводит статью затрат + описание/документы-обоснования
  2. Запрос расширяется через QueryExpander (синонимы статей затрат)
  3. Если приложен файл — сжимается через Map-Reduce (как в doc_scanner)
  4. Векторный поиск по коллекции протоколов в ChromaDB (top-K чанков)
  5. LLM классифицирует каждый чанк: положительное / отрицательное / нейтральное
  6. Агрегация по файлам (1 файл = 1 голос, по большинству чанков)
  7. Результат: счётчик за/против/нейтр + цитаты со свёрнутыми источниками
  8. Итоговое резюме (опционально): поиск применимых НПА по статье и выбранной
     сфере (та же гибридная инфраструктура, что у Советчика, core/advisor.py)
     + LLM-сводка, опирающаяся одновременно на найденные НПА и на практику
     регуляторов (за/против из уже классифицированных прецедентов)
  9. Сохранение в реестр прогнозов (data/predictor/registry_NNNN.jsonl)
──────────────────────────────────────────────────────────────────────────────
"""
import io
import json
import os
import re
import time
import threading
import difflib
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import streamlit as st

# Usage tracker — с защитой: если модуль ещё не создан, трекинг просто отключается
try:
    from core.usage_tracker import log_event as _log_usage
except Exception:
    def _log_usage(*a, **kw): pass  # noqa: E731

# =============================================================================
# Константы
# =============================================================================
_BASE_DIR        = os.path.join("data", "predictor")
_REGISTRY_DIR    = _BASE_DIR
_MAX_PER_FILE    = 1000
_CHROMA_DIR      = os.path.join("data", "vector_db")
_PROTOCOLS_COLLECTION = "protocols"
_EXPERTISE_COLLECTION = "expertise_docs"

_LARGE_DOC_THRESHOLD = 12_000
_CHUNK_SIZE          = 6_000
_CHUNK_OVERLAP       = 300

_DEFAULT_TOP_K = 30
_RAG_CONTEXT_CHAR_BUDGET = 35_000  # суммарный лимит символов по всем найденным чанкам перед классификацией
_RAW_CHUNK_MAX_CHARS = 2500  # жёсткий потолок сырого чанка сразу после поиска — Прогнозисту соседние
                              # чанки не нужны никогда, это защита от их "протечки" независимо от причины

_REGISTRY_LOCK = threading.Lock()


# =============================================================================
# Загрузка настроек прогнозиста из Админки
# =============================================================================
_PRED_CFG_FILE = os.path.join("config", "predictor_config.json")
_PRED_CFG_DEFAULTS = {
    "chunk_chars_to_llm":  1500,
    "justification_chars": 200,
    "classify_max_tokens": 350,
    "default_top_k":       30,
    "disable_thinking":    True,
    # ВТОРОЙ ЭТАП независимой проверки (quote vs позиция пользователя).
    # Подозревается в чрезмерном понижении positive/negative → neutral
    # (модель слишком часто находит "противоречие" там, где его нет).
    # Можно отключить здесь или переключателем в интерфейсе прогнозиста,
    # чтобы сравнить результат с выключенной верификацией.
    "enable_verification": True,
    # ── Итоговое резюме (НПА + практика) ─────────────────────────────────
    # Отдельный шаг после классификации: поиск применимых НПА по статье и
    # выбранной сфере через ту же инфраструктуру, что у Советчика
    # (core.advisor.search_vector_db), плюс LLM-сводка НПА + найденной
    # практики. Сбой здесь мягкий — не роняет уже посчитанный прогноз
    # (см. generate_prediction_summary).
    "enable_summary":      True,
    "summary_npa_top_k":   6,
    "summary_max_sources": 6,   # источников с каждой стороны (за/против) в промпте резюме
    "summary_max_tokens":  900,
    # ── Прикреплённые документы-обоснования ──────────────────────────────
    # Документы живут ТОЛЬКО в рамках одного прогноза (st.session_state
    # ["pred_docs"]) и намеренно НЕ сохраняются в базу Сканера документов:
    # обоснование тарифной заявки — разовый рабочий материал, засорять им
    # общую базу сканов не нужно.
    "max_upload_mb":            50,    # потолок на один файл; не должен превышать
                                       # maxUploadSize в .streamlit/config.toml,
                                       # иначе Streamlit отрежет файл раньше нашей
                                       # проверки и пользователь увидит не наше
                                       # сообщение, а внутреннюю ошибку виджета
    "max_docs":                 10,    # сколько документов можно приложить за раз
    "head_pages":               2,     # страниц для верхнеуровневого чтения (шапка)
    "head_summary_max_tokens":  200,
    "min_readable_chars":       200,   # меньше — считаем документ нечитаемым
                                       # (скан без OCR, пустой файл, битый PDF)
    # ── Немашиночитаемые документы (сканы) ───────────────────────────────
    # Распознавание делает doc_scanner (EasyOCR → Tesseract), но качество
    # OCR напрямую влияет на вердикт прогноза: распознанный в мусор скан
    # даёт уверенное, но неверное сопоставление. Поэтому OCR-документы
    # помечаются, а подозрительное качество выносится предупреждением —
    # решение оставить документ или заменить принимает эксперт.
    "ocr_warn_page_ratio":      0.5,   # доля OCR-страниц, выше которой предупреждаем
    "ocr_min_good_char_ratio":  0.85,  # доля осмысленных символов в распознанном
    "ocr_max_tiny_token_ratio": 0.25,  # доля одиночных букв — признак мусорного OCR
    "doc_full_text_budget":     8000,  # суммарный потолок текста всех документов,
                                       # уходящего в обоснование
    # Когда документы приложены, урезать позицию пользователя до
    # justification_chars (200 симв. по умолчанию) нельзя — от обоснования
    # не остаётся ничего. Отдельный, более щедрый лимит для этого случая.
    "justification_chars_with_docs": 2000,
    # Привязка каждого вердикта к конкретному приложенному документу.
    # Реализована ВНУТРИ основного вызова классификации (дополнительное поле
    # "doc" в JSON-ответе), а не отдельным LLM-вызовом на чанк — стоимость
    # прогноза не удваивается. Выключается здесь, если добавочная инструкция
    # в промпте начнёт заметно портить сам вердикт.
    "doc_attribution":          True,
}


def load_predictor_config() -> dict:
    """Читает config/predictor_config.json, возвращает merged с defaults."""
    if os.path.exists(_PRED_CFG_FILE):
        try:
            with open(_PRED_CFG_FILE, "r", encoding="utf-8") as f:
                return {**_PRED_CFG_DEFAULTS, **json.load(f)}
        except Exception:
            pass
    return dict(_PRED_CFG_DEFAULTS)


_PROMPTS_FILE = os.path.join("config", "prompts.json")
_PRED_PROMPT_DEFAULTS = {
    "predictor_classify_system": (
        "Ты — тарифный эксперт РФ. ГЛАВНОЕ ПРАВИЛО, определяющее всё "
        "остальное: тебя интересует, какой ПРИНЦИП ОБОСНОВАНИЯ статьи "
        "затрат избрал регулятор — а НЕ то, увеличилась или уменьшилась "
        "сумма по статье, и НЕ то, была ли заявка организации в целом "
        "одобрена или отклонена. Регулятор должен ВЫБРАТЬ ПРИНЦИП — вот "
        "что нужно определить.\n"
        "- Если из текста ясно, что регулятор в отношении этой статьи "
        "руководствовался ТЕМ ЖЕ обоснованием/принципом, что указывает "
        "пользователь — это 'за' (positive). Неважно, поддержал регулятор "
        "позицию той, другой организации из прецедента или отклонил её по "
        "цифрам — главное, что регулятор избрал ТОТ ЖЕ подход/принцип, что "
        "и пользователь.\n"
        "- Если из текста ясно, что регулятор явно ОТКЛОНИЛ именно этот "
        "принцип обоснования (избрал другой, противоположный принцип) — "
        "это 'против' (negative).\n"
        "- Если статья просто упомянута с ДРУГИМ, не связанным по "
        "существу обоснованием (другой принцип, другая логика) — это "
        "'нейтрально' (neutral).\n\n"
        "Сравнивай ИМЕННО ПОДХОД: каким способом регулятор определяет "
        "значение. Совпадение ОБЩЕЙ ТЕМЫ подхода (например, оба случая "
        "касаются 'срока полезного использования' или 'выбора варианта из "
        "диапазона') ещё не значит совпадение позиции — если регулятор и "
        "пользователь выбирают ПРОТИВОПОЛОЖНЫЕ варианты внутри этой темы "
        "(например, один настаивает на максимальном значении, другой — на "
        "минимальном; один — на фактических данных, другой — на нормативе), "
        "это ПРОТИВОРЕЧИЕ (negative), а не совпадение. Числовой результат "
        "(сумма выросла или снизилась) сам по себе НИКОГДА не определяет "
        "positive/negative — важно ТОЛЬКО, выбрал ли регулятор ТОТ ЖЕ "
        "принцип решения, что и пользователь, или ПРОТИВОПОЛОЖНЫЙ.\n\n"
        "ОСОБО ВАЖНАЯ ОШИБКА, КОТОРУЮ НУЖНО ИЗБЕГАТЬ: не приравнивай два "
        "РАЗНЫХ КОНКРЕТНЫХ ИСТОЧНИКА/ДОКУМЕНТА только потому, что оба "
        "можно назвать 'официальными' или 'нормативными'. Например, "
        "'штатное расписание' и 'приказы об изменении ФОТ' — это ДВА "
        "РАЗНЫХ конкретных документа, и то, что оба являются формальными/"
        "нормативными по своей природе, НЕ делает их совпадающим вариантом "
        "решения. Совпадением считается только использование ОДНОГО И "
        "ТОГО ЖЕ конкретного источника/показателя (оба — штатное "
        "расписание, оба — приказ, оба — фактическая отчётность и т.п.), "
        "а не абстрактной надкатегории вроде 'использование официальных/"
        "нормативных документов' или 'методология опоры на документы'. "
        "Если пользователь ссылается на конкретный документ X, а в "
        "прецеденте регулятор упоминает другой конкретный документ Y (даже "
        "того же типа — 'тоже официальный', 'тоже нормативный') — по "
        "умолчанию это НЕ positive; ставь positive только если это "
        "буквально тот же документ/показатель или его прямой синоним.\n\n"
        "КРИТИЧЕСКИ ВАЖНО: в материалах ДВА РАЗНЫХ ИСТОЧНИКА текста — "
        "позиция ТЕКУЩЕГО пользователя (помечена '=== ПОЗИЦИЯ ТЕКУЩЕГО "
        "ПОЛЬЗОВАТЕЛЯ ===') и решение регулятора по ДРУГОЙ организации из "
        "прецедента (помечено '=== РЕШЕНИЕ ИЗ ПРЕЦЕДЕНТА ==='). Внутри "
        "блока прецедента может быть фраза 'заявлено предприятием X тыс. "
        "руб.' — это позиция ДРУГОЙ организации из прецедента, а НЕ "
        "текущего пользователя. Не путай их.\n\n"
        "ВАЖНАЯ АКСИОМА ОБ ИСТОЧНИКЕ ДОКУМЕНТОВ ПРЕЦЕДЕНТА: все документы "
        "в блоке '=== РЕШЕНИЕ ИЗ ПРЕЦЕДЕНТА ===' — это протоколы заседаний "
        "РЭК (регионального регулятора) или экспертные заключения, "
        "подготовленные для обоснования решения РЭК. Это значит, что "
        "ЛЮБОЕ утверждение о принятии, непринятии или корректировке "
        "затрат в этих документах ПО УМОЛЧАНИЮ является позицией именно "
        "РЭКа (регулятора) — а не организации, обратившейся за тарифом "
        "(РСО), даже если документ называется 'экспертное заключение', а "
        "не 'протокол'. Единственное исключение — явно помеченные фразы "
        "вида 'заявлено предприятием', 'по расчётам организации', "
        "'организация настаивает' — вот это действительно позиция РСО, а "
        "не РЭКа. Во всех остальных случаях не сомневайся в том, чья это "
        "позиция: если формулировка не помечена как позиция заявителя — "
        "перед тобой решение/вывод регулятора.\n\n"
        "ФОРМАТ ОТВЕТА — СТРОГО ВАЖНО: ты должен выдать ТОЛЬКО готовый "
        "финальный результат сравнения, БЕЗ цепочки рассуждений, БЕЗ "
        "цитирования инструкции, БЕЗ слов 'перечитаем', 'однако', 'но "
        "инструкция гласит', БЕЗ промежуточных вопросов самому себе "
        "(например 'значит decision должен быть X?'). Сравнение "
        "методологии должно происходить у тебя ДО генерации ответа, "
        "а не внутри текста поля reason. Поле reason — это ИТОГ "
        "сравнения в одном утвердительном предложении (до 120 символов), "
        "а не процесс рассуждения. decision должен точно соответствовать "
        "этому итоговому reason. Отвечай только JSON, без какого-либо "
        "текста до или после него."
    ),
    "predictor_classify_user": (
        "СТАТЬЯ ЗАТРАТ: {article_name}\n"
        "{justification_line}"
        "\n"
        "НАЙДЕННЫЙ ПРЕЦЕДЕНТ:\n{chunk}\n\n"
        "ЗАДАЧА: определи, КАКОЙ ИМЕННО ВАРИАНТ/ПОДХОД выбрал регулятор в "
        "блоке '=== РЕШЕНИЕ ИЗ ПРЕЦЕДЕНТА ===' (не саму цифру и не итог "
        "'больше/меньше', а суть решения — например 'применил максимальный "
        "срок', 'применил минимальный срок', 'учёл фактические расходы', "
        "'применил норматив вместо факта') и сравни этот ВЫБОР с тем, что "
        "заявляет пользователь в блоке '=== ПОЗИЦИЯ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ "
        "==='.\n\n"
        "НАПОМИНАНИЕ: тебя интересует только ПРИНЦИП, который избрал "
        "регулятор — а не итог заявки той организации из прецедента "
        "(одобрена целиком/отклонена/скорректирована) и не то, выросла "
        "или снизилась у неё сумма. Регулятор мог полностью отклонить "
        "заявку той организации по цифрам, но при этом руководствоваться "
        "ТЕМ ЖЕ принципом, который сейчас заявляет пользователь — это всё "
        "равно positive.\n\n"
        "Определи decision:\n"
        "- positive — регулятор выбрал ТОТ ЖЕ вариант/подход, что заявляет "
        "пользователь (например, оба настаивают на максимальном сроке, оба "
        "— на минимальном, оба — на учёте фактических расходов, оба — на "
        "применении норматива). Засчитывай как positive, ДАЖЕ ЕСЛИ в "
        "прецеденте этот выбор привёл к снижению суммы у той организации — "
        "важно совпадение выбранного варианта, а не итоговое движение "
        "цифры\n"
        "- negative — регулятор выбрал ДРУГОЙ или ПРОТИВОПОЛОЖНЫЙ вариант "
        "внутри той же темы (например, пользователь настаивает на "
        "минимальном сроке, а регулятор в прецеденте применяет максимальный "
        "— это противоположные значения одного и того же параметра, не "
        "совпадение; или пользователь просит фактические расходы, а "
        "регулятор применяет норматив)\n"
        "- neutral — в прецеденте нет решения по этой статье, ИЛИ "
        "невозможно определить выбранный вариант регулятора из фрагмента, "
        "ИЛИ тема прецедента не связана по существу с тем, что заявляет "
        "пользователь\n\n"
        "ВАЖНО: 'максимальный' и 'минимальный' (как и 'факт' и 'норматив', "
        "'включить' и 'исключить') — это ПРОТИВОПОЛОЖНЫЕ варианты одной "
        "темы. Если ОБА (и регулятор, и пользователь) выбрали ОДИНАКОВЫЙ "
        "вариант (например, оба — 'норматив', оба — 'максимальный срок') — "
        "это ВСЕГДА positive, без исключений и без дополнительных "
        "рассуждений. Не путай тематическое совпадение (оба текста "
        "говорят о 'сроке полезного использования') с совпадением позиции "
        "(выбран ли тот же конкретный вариант) — но если конкретный "
        "вариант СОВПАЛ, сомнений быть не должно. Не путай 'заявлено "
        "предприятием' внутри блока ПРЕЦЕДЕНТА (это другая организация) с "
        "позицией ТЕКУЩЕГО пользователя. Конкретные числа (года, проценты, "
        "суммы) сравнивать не нужно — сравнивай только то, какой "
        "вариант/подход выбран.\n\n"
        "ОТДЕЛЬНО ПРОВЕРЬ СЕБЯ ПЕРЕД ОТВЕТОМ: если пользователь называет "
        "конкретный документ/источник (например, 'штатное расписание'), а "
        "регулятор в прецеденте называет ДРУГОЙ конкретный документ/"
        "источник (например, 'приказы', 'норматив численности', 'акт "
        "сверки') — это РАЗНЫЕ варианты, даже если оба формально можно "
        "назвать 'официальными' или 'нормативными'. Не ставь positive "
        "только на основании того, что 'оба опираются на официальные "
        "документы' — это тематическое, а не конкретное совпадение. "
        "positive допустим только если названы буквально один и тот же "
        "документ/показатель (или явный синоним одного и того же), а не "
        "просто одна и та же общая категория документов.\n\n"
        "Ответь СРАЗУ готовым JSON без рассуждений, без вопросов самому "
        "себе, без цитирования этой инструкции в ответе:\n"
        'JSON: {{"decision":"positive|negative|neutral","quote":"цитата, какой вариант выбрал регулятор, до 120 симв.","reason":"краткий итоговый вывод одним предложением: совпадают варианты или нет, до 120 симв."}}'
    ),
    # ── Прогнозист решений: итоговое резюме (НПА + практика) ─────────────
    "predictor_summary_system": (
        "Ты — тарифный эксперт РФ. Твоя задача — по уже готовым материалам "
        "подготовить краткое итоговое резюме для специалиста, работающего "
        "над обоснованием статьи затрат в тарифной заявке.\n\n"
        "У тебя есть два независимых источника, оба уже собраны и "
        "предоставлены ниже — сам поиск не твоя задача:\n"
        "1. ПРИМЕНИМЫЕ НПА — нормативные акты и методические документы по "
        "данной статье затрат и сфере регулирования, найденные в базе.\n"
        "2. ПРАКТИКА РЕГУЛЯТОРОВ — прецеденты (протоколы/экспертные "
        "заключения РЭК), уже классифицированные как «за» или «против» той "
        "же логики обоснования, которую заявляет пользователь.\n\n"
        "ПРАВИЛА:\n"
        "- Отвечай только на русском языке, связным текстом (не JSON, без "
        "списков-буллетов), 3–5 предложений.\n"
        "- Сначала кратко скажи, что требует НПА по этой статье. Если НПА "
        "не найдены — прямо укажи это одним предложением и не выдумывай "
        "нормы.\n"
        "- Затем скажи, что показывает практика регуляторов: согласуется "
        "ли она с требованиями НПА, преобладает «за» или «против», и по "
        "какой причине (опирайся на quote/reason источников практики).\n"
        "- Если НПА и практика расходятся — явно укажи это как отдельный "
        "риск для заявителя.\n"
        "- Не повторяй источники дословно — обобщай.\n"
        "- Не упоминай процент вероятности одобрения — он уже показан "
        "пользователю отдельно, дублировать не нужно.\n"
        "- Если оба источника пусты — сообщи об этом одним предложением и "
        "не придумывай содержание.\n"
        "Ответь сразу текстом резюме, без вступления и без цитирования "
        "этой инструкции."
    ),
    "predictor_summary_user": (
        "СТАТЬЯ ЗАТРАТ: {article_name}\n"
        "{justification_line}\n"
        "=== ПРИМЕНИМЫЕ НПА ===\n"
        "{npa_context}\n"
        "=== КОНЕЦ НПА ===\n\n"
        "=== ПРАКТИКА РЕГУЛЯТОРОВ (найденные прецеденты) ===\n"
        "За: {n_positive} · Против: {n_negative} · Нейтрально (не "
        "учитываются в оценке): {n_neutral}\n"
        "{expertise_context}\n"
        "=== КОНЕЦ ПРАКТИКИ ===\n\n"
        "Составь итоговое резюме по правилам из системного промпта."
    ),
    # ── Верхнеуровневое чтение приложенного документа ────────────────────
    # Никакой классификации по видам документов здесь намеренно нет: нужно
    # одно-два предложения о том, что это за бумага и о чём она, чтобы
    # пользователь видел, что именно система прочитала, а поиск получил
    # осмысленный контекст. Читается только шапка (первые head_pages
    # страниц) — этого достаточно и это не стоит полного прохода по файлу.
    "predictor_doc_head_system": (
        "Ты — тарифный эксперт РФ. По началу документа (шапка, титульный "
        "лист, первые строки) коротко определи, что это за документ и о чём "
        "он. Отвечай ОДНИМ-ДВУМЯ предложениями на русском языке, без "
        "вступлений, без списков, без рассуждений и без цитирования этой "
        "инструкции. Если по фрагменту понять невозможно — так и напиши "
        "одним предложением, ничего не выдумывая."
    ),
    "predictor_doc_head_user": (
        "ИМЯ ФАЙЛА: {filename}\n"
        "СТАТЬЯ ЗАТРАТ, по которой готовится обоснование: {article_name}\n\n"
        "НАЧАЛО ДОКУМЕНТА:\n{head_text}\n\n"
        "Одним-двумя предложениями: что это за документ и о чём он? Если "
        "видно, как он связан со статьёй затрат — укажи это."
    ),
}




def load_predictor_prompts() -> dict:
    """Читает промпты прогнозиста из config/prompts.json."""
    if os.path.exists(_PROMPTS_FILE):
        try:
            with open(_PROMPTS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**_PRED_PROMPT_DEFAULTS,
                    **{k: saved[k] for k in _PRED_PROMPT_DEFAULTS if k in saved}}
        except Exception:
            pass
    return dict(_PRED_PROMPT_DEFAULTS)


# =============================================================================
# Утилиты реестра (jsonl, ротация по 1000 записей)
# =============================================================================
def _ensure_dirs():
    os.makedirs(_REGISTRY_DIR, exist_ok=True)


def _current_registry_path() -> str:
    """Возвращает путь к активному файлу реестра, создаёт новый при переполнении."""
    _ensure_dirs()
    idx = 1
    while True:
        path = os.path.join(_REGISTRY_DIR, f"registry_{idx:04d}.jsonl")
        if not os.path.exists(path):
            return path
        # Считаем строки
        with open(path, "r", encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
        if count < _MAX_PER_FILE:
            return path
        idx += 1


def save_to_registry(record: Dict):
    """Дозаписывает запись в активный файл реестра."""
    _ensure_dirs()
    path = _current_registry_path()
    with _REGISTRY_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_registry(max_records: int = 200) -> List[Dict]:
    """Загружает последние max_records записей из всех файлов реестра."""
    _ensure_dirs()
    all_records = []
    files = sorted(
        [f for f in os.listdir(_REGISTRY_DIR) if f.startswith("registry_") and f.endswith(".jsonl")],
        reverse=True,
    )
    for fname in files:
        path = os.path.join(_REGISTRY_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            for line in reversed(lines):
                try:
                    all_records.append(json.loads(line))
                except Exception:
                    pass
            if len(all_records) >= max_records:
                break
        except Exception:
            pass
    return all_records[:max_records]


# =============================================================================
# Регистрация ручных правок эксперта (аудит: где пользователь исправил ИИ)
# =============================================================================
_OVERRIDE_LOG_FILE  = os.path.join(_BASE_DIR, "expert_overrides.jsonl")
_OVERRIDE_LOG_LOCK  = threading.Lock()

_DECISION_RU = {"positive": "за", "negative": "против", "neutral": "нейтрально"}


def _log_expert_override(
    article: str, fkey: str, ai_decision: str,
    previous_decision: str, new_decision: str, quote: str = "",
) -> None:
    """
    Дописывает запись о ручной правке эксперта в постоянный лог
    (data/predictor/expert_overrides.jsonl) — отдельно от сессии, чтобы
    сохранялась история даже после сброса прогноза/session_state.
    Каждая строка — один факт "эксперт вручную изменил вердикт ИИ",
    с исходным вердиктом модели и итоговым решением эксперта.
    """
    _ensure_dirs()
    record = {
        "timestamp":          datetime.now().isoformat(),
        "article":            article,
        "file":               fkey,
        "ai_decision":        ai_decision,
        "previous_decision":  previous_decision,
        "new_decision":       new_decision,
        "quote":              (quote or "")[:200],
    }
    try:
        with _OVERRIDE_LOG_LOCK:
            with open(_OVERRIDE_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[PREDICTOR] Не удалось записать лог правки эксперта: {e}", flush=True)


def load_expert_overrides_log(max_records: int = 500) -> List[Dict]:
    """Загружает последние правки эксперта из постоянного лога (для аудита)."""
    if not os.path.exists(_OVERRIDE_LOG_FILE):
        return []
    try:
        with open(_OVERRIDE_LOG_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        records = []
        for line in reversed(lines):
            try:
                records.append(json.loads(line))
            except Exception:
                pass
            if len(records) >= max_records:
                break
        return records
    except Exception:
        return []


# =============================================================================
# LM Studio — переиспользуем логику doc_scanner
# =============================================================================
def _load_lm_config() -> Tuple[str, str]:
    config_path = os.path.join("config", "advisor_config.json")
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    lm_url = config.get("lm_studio_url", "http://127.0.0.1:1234/v1")
    model  = config.get("default_model", "qwen/qwen3.5-9b")
    return lm_url, model


def _load_lm_full_config() -> dict:
    """Возвращает полный конфиг advisor_config.json (нужен для нативного Ollama API)."""
    config_path = os.path.join("config", "advisor_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _ollama_native_base_url(lm_url: str) -> str:
    """Базовый URL Ollama БЕЗ суффикса /v1 — для нативного /api/chat."""
    url = (lm_url or "").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def _ollama_chat_native(lm_url: str, model: str, system: str, user: str,
                        max_tokens: int, cfg: dict, temperature: float = 0.1,
                        timeout: float = 180.0) -> dict:
    """
    Нестриминговый вызов нативного Ollama /api/chat с think:false.

    ВАЖНО: "think": false здесь — единственный надёжный способ отключить
    режим рассуждения Qwen3.5. Прежний способ через extra_body={"thinking":
    {"type": "disabled"}} на OpenAI-совместимом /v1/chat/completions — не
    формат Ollama и не отключал thinking надёжно: модель уходила в
    незакрытый <think>-блок, весь max_tokens тратился на рассуждения, и
    classify_chunk/_verify_regulator_choice_vs_user_position получали
    пустую строку вместо JSON — что и приводило к массовому "нейтрально"
    (см. тот же фикс в core/advisor.py, doc_scanner.py, core/predictor.py).
    """
    import requests
    url = _ollama_native_base_url(lm_url) + "/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": False,
        "think": False,
        "options": {
            "temperature":    temperature,
            "num_predict":    max_tokens,
            "num_ctx":        cfg.get("num_ctx", 20000),
            "top_p":          cfg.get("top_p", 0.9),
            "top_k":          cfg.get("top_k", 40),
            "repeat_penalty": cfg.get("repeat_penalty", 1.1),
        },
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    msg = data.get("message") or {}
    return {
        "content": msg.get("content", ""),
        "done_reason": data.get("done_reason", "stop"),
    }


def _strip_thinking(text: str) -> str:
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return re.sub(r'\n{3,}', '\n\n', cleaned).strip()


def _lm_call(client, model: str, system: str, user: str, max_tokens: int = 600) -> str:
    """
    Один вызов LLM; возвращает текст ответа или строку с ошибкой.

    Для Ollama (llm_backend="ollama" в config/advisor_config.json, по
    умолчанию) используется нативный /api/chat с think:false — единственный
    надёжный способ отключить thinking-режим Qwen3.5 (extra_body/"thinking"
    на OpenAI-совместимом /v1/chat/completions ненадёжен — см.
    core/advisor.py, doc_scanner.py, core/predictor.py, где та же проблема
    давала пустой ответ). base_url берём прямо из уже созданного client —
    не нужно менять сигнатуру и пробрасывать lm_url через все вызовы
    (classify_chunk, _verify_regulator_choice_vs_user_position и т.д.).
    """
    cfg               = load_predictor_config()
    _disable_thinking = bool(cfg.get("disable_thinking", True))
    _lm_cfg           = _load_lm_full_config()
    _is_ollama        = _lm_cfg.get("llm_backend", "ollama") == "ollama"

    if _is_ollama:
        try:
            lm_url = str(client.base_url)
            result = _ollama_chat_native(
                lm_url, model, system, user, max_tokens, _lm_cfg,
            )
            raw = (result.get("content") or "").strip()
            return _strip_thinking(raw)
        except Exception as e:
            print(f"[LM] Нативный Ollama-вызов не удался, фоллбек на OpenAI-клиент: {e}", flush=True)
            # падаем в старый путь ниже (например, если requests недоступен
            # или сервер временно не отвечает на нативный эндпоинт)

    _kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
        temperature=0.1,
    )
    # cache_prompt: false — запрещаем llama.cpp кэшировать KV между запросами.
    # Без этого после 5-10 последовательных вызовов слот переполняется,
    # llama.cpp перестраивает кэш и часть вычислений падает на CPU.
    extra = {"cache_prompt": False}
    if _disable_thinking:
        extra["thinking"] = {"type": "disabled"}
    _kwargs["extra_body"] = extra
    try:
        resp = client.chat.completions.create(**_kwargs)
        raw = (resp.choices[0].message.content or "").strip()
        return _strip_thinking(raw)
    except Exception:
        # Fallback без extra_body если модель не поддерживает параметр
        _kwargs.pop("extra_body", None)
        try:
            resp = client.chat.completions.create(**_kwargs)
            raw = (resp.choices[0].message.content or "").strip()
            return _strip_thinking(raw)
        except Exception as e2:
            return f"[Ошибка LM: {e2}]"


def _split_text_chunks(text: str) -> List[str]:
    """Разбивает текст на чанки по границам абзацев (из doc_scanner)."""
    if len(text) <= _CHUNK_SIZE:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = start + _CHUNK_SIZE
        if end >= len(text):
            tail = text[start:].strip()
            if tail:
                chunks.append(tail)
            break
        min_pos = start + _CHUNK_SIZE * 3 // 4
        split_end = end
        for sep in ('\n\n', '. ', ' '):
            pos = text.rfind(sep, min_pos, end)
            if pos != -1:
                split_end = pos + len(sep)
                break
        chunk = text[start:split_end].strip()
        if chunk:
            chunks.append(chunk)
        next_start = split_end - _CHUNK_OVERLAP
        start = max(start + _CHUNK_SIZE // 2, next_start)
    return chunks


def compress_document(text: str, article_name: str, _progress_cb=None) -> str:
    """
    Сжимает текст документа-обоснования до краткого резюме через Map-Reduce.
    Акцентирует на связи с конкретной статьёй затрат.
    """
    if not text.strip():
        return ""
    try:
        from openai import OpenAI
        lm_url, model = _load_lm_config()
        client = OpenAI(base_url=lm_url, api_key="lm-studio", timeout=180.0)
        system_msg = (
            "Ты эксперт по тарифному регулированию. "
            "Кратко извлекай только факты, суммы и показатели, "
            "относящиеся к статье затрат."
        )

        if len(text) <= _LARGE_DOC_THRESHOLD:
            prompt = (
                f"Извлеки ключевые факты из документа, относящиеся к статье затрат "
                f'«{article_name}»: суммы, обоснования, нормативы, методику расчёта. '
                f"Ответь кратко, 3-7 пунктов. Без вступлений.\n\nДОКУМЕНТ:\n{text}"
            )
            return _lm_call(client, model, system_msg, prompt, max_tokens=500)

        # Map-Reduce для большого документа
        chunks = _split_text_chunks(text)
        total  = len(chunks)
        mini_summaries = []
        for i, chunk in enumerate(chunks, 1):
            if _progress_cb:
                _progress_cb((i - 1) / (total + 1), f"Сжатие документа: часть {i}/{total}…")
            map_prompt = (
                f"Часть {i} из {total} документа-обоснования.\n"
                f'Извлеки только факты, относящиеся к статье затрат «{article_name}»: '
                f"суммы, нормативы, методику. Если нет — напиши «нет данных».\n\n"
                f"ЧАСТЬ:\n{chunk}"
            )
            mini = _lm_call(client, model, system_msg, map_prompt, max_tokens=300)
            mini_summaries.append(f"=== Часть {i}/{total} ===\n{mini}")

        if _progress_cb:
            _progress_cb(total / (total + 1), "Финальный синтез…")

        combined = "\n\n".join(mini_summaries)
        reduce_prompt = (
            f"Ниже — резюме частей документа. Создай единое краткое резюме (5-10 пунктов) "
            f'по статье затрат «{article_name}». Сохрани все суммы и нормативы.\n\n'
            f"РЕЗЮМЕ ЧАСТЕЙ:\n{combined}"
        )
        return _lm_call(client, model, system_msg, reduce_prompt, max_tokens=600)

    except Exception as e:
        return f"[Ошибка сжатия: {e}]"


# =============================================================================
# Извлечение текста из загруженного файла (переиспользуем doc_scanner)
# =============================================================================
def extract_file_text(file_bytes: bytes, filename: str) -> str:
    """Извлекает текст из файла через логику doc_scanner."""
    try:
        from streamlit_pages.doc_scanner import extract_text
        pages = extract_text(file_bytes, filename)
        return "\n".join(p.get("text", "") for p in pages if p.get("text"))
    except Exception as e:
        return f"[Ошибка извлечения текста: {e}]"


# =============================================================================
# Прикреплённые документы-обоснования
# =============================================================================
# Документ живёт только в рамках одного прогноза, в st.session_state
# ["pred_docs"], и в базу Сканера документов НЕ пишется. Структура записи:
#
#   {
#     "id":           "a1b2c3d4",   # стабильный ключ виджетов Streamlit
#     "sig":          "имя:размер", # для сопоставления с file_uploader
#     "filename":     "Штатное расписание 2026.pdf",
#     "size":         1048576,
#     "ok":           True,         # прошёл ли валидацию
#     "error":        "",           # причина отказа, если не прошёл
#     "pages":        [...],        # как отдаёт doc_scanner.extract_text
#     "full_text":    "...",
#     "head_text":    "...",        # первые head_pages страниц (шапка)
#     "head_summary": "...",        # верхнеуровневое чтение, 1–2 предложения
#     "source":       "upload",     # upload | scanner
#   }
#
# Расширения — ровно те, что реально умеет разбирать doc_scanner.extract_text;
# всё остальное этот экстрактор вернёт как "[Формат ... не поддерживается]",
# поэтому отсекаем заранее и говорим пользователю понятным языком.
_SUPPORTED_EXTS = (
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt",
    ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp",
)


def _new_doc_id() -> str:
    """Короткий стабильный идентификатор приложенного документа."""
    import uuid
    return uuid.uuid4().hex[:8]


def ensure_ocr_ready() -> Dict:
    """
    Поднимает OCR, если он ещё не поднят, и возвращает его состояние:
    {"available": bool, "engine": "easyocr"|"tesseract"|"", "error": str}.

    ЗАЧЕМ ЭТО ЗДЕСЬ. doc_scanner._init_ocr() вызывается только внутри
    show_doc_scanner() под флагом st.session_state["ocr_initialized"].
    Пользователь, который открыл Прогнозист, не заходя в «Сканер
    документов», получал _EASYOCR_AVAILABLE = False — _ocr_image()
    молча возвращал пустую строку, и ЛЮБОЙ скан отклонялся как
    нечитаемый, хотя сам документ был в порядке, а не поднят был движок.
    Инициализируем под тем же самым ключом session_state, что и Сканер,
    поэтому повторной загрузки модели в VRAM не происходит — кто первым
    открыл раздел, тот её и поднял.
    """
    state = {"available": False, "engine": "", "error": ""}
    try:
        from streamlit_pages import doc_scanner as _ds
    except Exception as e:
        state["error"] = f"модуль Сканера недоступен: {e}"
        return state

    try:
        if not st.session_state.get("ocr_initialized"):
            _ds._init_ocr()
            st.session_state["ocr_initialized"] = True
    except Exception as e:
        state["error"] = str(e)

    if getattr(_ds, "_EASYOCR_AVAILABLE", False):
        state.update(available=True, engine="easyocr")
    elif getattr(_ds, "_TESSERACT_AVAILABLE", False):
        state.update(available=True, engine="tesseract")
    else:
        state["error"] = state["error"] or st.session_state.get("ocr_init_error", "")
    return state


# Символы, которые считаем осмысленными в распознанном тексте: буквы, цифры,
# пробелы и обычная деловая пунктуация. Всё остальное в товарных количествах —
# признак того, что OCR выдал мусор.
_OCR_GOOD_PUNCT = set(".,;:!?()[]-—–«»\"'%№/\\+=*§°  \t\n\r")


def assess_ocr_quality(text: str) -> Tuple[bool, str]:
    """
    Грубая оценка качества распознанного текста. Возвращает
    (выглядит_нормально, пояснение_для_пользователя).

    Проверяется два признака мусорного OCR:
      1. доля осмысленных символов — плохое распознавание даёт много
         посторонних глифов;
      2. доля одиночных букв среди токенов — характерный след, когда OCR
         рассыпает слова на отдельные символы.

    Документ по результатам этой проверки НЕ отклоняется: OCR-распознавание
    сканов — штатный сценарий, а порог здесь эвристический. Задача проверки —
    предупредить эксперта, что вердикт построен на сомнительном тексте.
    """
    text = (text or "").strip()
    if not text:
        return False, "распознанный текст пуст"

    cfg          = load_predictor_config()
    min_good     = float(cfg.get("ocr_min_good_char_ratio", 0.85))
    max_tiny     = float(cfg.get("ocr_max_tiny_token_ratio", 0.25))

    good = sum(1 for ch in text if ch.isalnum() or ch in _OCR_GOOD_PUNCT)
    good_ratio = good / len(text)

    tokens = [t for t in re.split(r"\s+", text) if t]
    tiny_ratio = (
        sum(1 for t in tokens if len(t) == 1 and t.isalpha()) / len(tokens)
        if tokens else 1.0
    )

    problems = []
    if good_ratio < min_good:
        problems.append(f"посторонних символов {(1 - good_ratio) * 100:.0f}%")
    if tiny_ratio > max_tiny:
        problems.append(f"текст рассыпан на отдельные буквы ({tiny_ratio * 100:.0f}%)")

    if problems:
        return False, "; ".join(problems)
    return True, ""


def _doc_sig(filename: str, size: int) -> str:
    """Подпись файла для сопоставления с содержимым st.file_uploader."""
    return f"{filename}:{size}"


def validate_and_read_upload(file_bytes: bytes, filename: str) -> Dict:
    """
    Проверяет пригодность файла и читает его. Возвращает запись документа
    (см. структуру выше) — в том числе и при отказе, с ok=False и
    заполненным error, чтобы интерфейс мог показать причину рядом с самим
    файлом и предложить заменить его, не роняя остальные приложенные
    документы.

    Три причины отказа:
      1. расширение вне списка поддерживаемых doc_scanner;
      2. размер больше max_upload_mb;
      3. текст не извлёкся — экстрактор вернул ошибку или получилось
         меньше min_readable_chars символов (скан без OCR, пустой или
         битый файл).
    """
    cfg        = load_predictor_config()
    max_bytes  = int(cfg.get("max_upload_mb", 50)) * 1024 * 1024
    min_chars  = int(cfg.get("min_readable_chars", 200))
    head_pages = int(cfg.get("head_pages", 2))

    size = len(file_bytes or b"")
    doc = {
        "id":           _new_doc_id(),
        "sig":          _doc_sig(filename, size),
        "filename":     filename,
        "size":         size,
        "ok":           False,
        "error":        "",
        "pages":        [],
        "full_text":    "",
        "head_text":    "",
        "head_summary": "",
        "source":       "upload",
        # Немашиночитаемые документы: сколько страниц прошло через OCR и как
        # выглядит качество распознавания (см. assess_ocr_quality).
        "ocr_pages":       0,
        "ocr_ratio":       0.0,
        "ocr_quality_ok":  True,
        "ocr_quality_note": "",
    }

    ext = os.path.splitext(filename.lower())[1]
    if ext not in _SUPPORTED_EXTS:
        doc["error"] = (
            f"Формат {ext or '—'} не поддерживается. "
            f"Допустимы: {', '.join(e.lstrip('.') for e in _SUPPORTED_EXTS)}."
        )
        return doc

    if size > max_bytes:
        doc["error"] = (
            f"Файл {size / 1024 / 1024:.1f} МБ — больше лимита "
            f"{cfg.get('max_upload_mb', 50)} МБ."
        )
        return doc

    if size == 0:
        doc["error"] = "Файл пустой."
        return doc

    try:
        from streamlit_pages.doc_scanner import extract_text
    except Exception as e:
        doc["error"] = f"Модуль Сканера документов недоступен: {e}"
        return doc

    # OCR поднимаем ДО разбора: для немашиночитаемого документа (скан без
    # текстового слоя) doc_scanner молча вернёт пустой текст, если движок
    # распознавания не инициализирован — и файл будет отклонён как
    # «нечитаемый», хотя с ним всё в порядке. См. ensure_ocr_ready.
    _ocr = ensure_ocr_ready()

    # Полный разбор. Для скана это OCR всех страниц — может быть долгим,
    # поэтому вызывается один раз при прикреплении и результат остаётся
    # в session_state до конца прогноза.
    try:
        pages = extract_text(file_bytes, filename)
    except Exception as e:
        doc["error"] = f"Не удалось прочитать файл: {e}"
        return doc

    full_text = "\n".join(p.get("text", "") for p in pages if p.get("text")).strip()

    _ocr_pages = sum(1 for p in pages if p.get("method") == "ocr")
    doc["ocr_pages"] = _ocr_pages
    doc["ocr_ratio"] = (_ocr_pages / len(pages)) if pages else 0.0

    if not pages or all(p.get("method") == "error" for p in pages):
        doc["error"] = (
            "Файл не читается — вероятно, повреждён или защищён от "
            "извлечения текста."
        )
        doc["pages"] = pages
        return doc

    if len(full_text) < min_chars:
        # Различаем две принципиально разные причины. Раньше обе давали
        # текст «проверьте качество скана» — и пользователь шёл
        # пересканировать нормальный документ, хотя на деле на сервере
        # просто не поднялся OCR.
        if _ocr_pages and not _ocr.get("available"):
            _why = _ocr.get("error") or "модуль распознавания не установлен"
            doc["error"] = (
                f"Документ немашиночитаемый (скан), а распознавание текста "
                f"недоступно: {_why}. С самим файлом, скорее всего, всё в "
                f"порядке — нужно поднять OCR на сервере либо приложить "
                f"текстовую версию документа."
            )
        elif _ocr_pages:
            doc["error"] = (
                f"Скан распознан лишь частично — извлечено {len(full_text)} симв. "
                f"Проверьте качество скана (разрешение, контраст, перекос) "
                f"или приложите текстовую версию."
            )
        else:
            doc["error"] = (
                f"Извлечено всего {len(full_text)} симв. — документ пуст или "
                f"нечитаем. Приложите текстовую версию."
            )
        doc["pages"]     = pages
        doc["full_text"] = full_text
        return doc

    # Качество OCR оценивается только по распознанной части: для документа
    # с текстовым слоем эта проверка бессмысленна и не проводится.
    if _ocr_pages:
        _ocr_text = "\n".join(
            p.get("text", "") for p in pages if p.get("method") == "ocr"
        )
        _q_ok, _q_note = assess_ocr_quality(_ocr_text)
        doc["ocr_quality_ok"]   = _q_ok
        doc["ocr_quality_note"] = _q_note

    # Шапка для верхнеуровневого чтения — первые head_pages страниц уже
    # разобранного документа (повторно файл не парсим).
    head_text = "\n".join(
        p.get("text", "") for p in pages[:max(1, head_pages)] if p.get("text")
    ).strip()

    doc.update({
        "ok":        True,
        "pages":     pages,
        "full_text": full_text,
        "head_text": head_text,
    })
    return doc


def doc_from_scanner(scan_doc: Dict) -> Dict:
    """
    Оборачивает документ из базы Сканера в ту же структуру, что и
    приложенный с рабочей машины. Файл уже разобран Сканером — повторное
    извлечение текста и валидация формата не нужны.
    """
    from streamlit_pages.doc_scanner import _fname as _scan_fname

    cfg        = load_predictor_config()
    head_pages = int(cfg.get("head_pages", 2))

    pages     = scan_doc.get("pages", []) or []
    full_text = scan_doc.get("full_text", "") or "\n".join(
        p.get("text", "") for p in pages if p.get("text")
    )
    head_text = "\n".join(
        p.get("text", "") for p in pages[:max(1, head_pages)] if p.get("text")
    ).strip() or full_text[:3000]

    filename = _scan_fname(scan_doc)

    # Сканер уже посчитал, сколько страниц прошло через OCR (add_to_db,
    # поле ocr_pages) — берём оттуда, а при отсутствии считаем сами по
    # методам страниц: документы, загруженные старыми версиями Сканера,
    # этого поля могут не иметь.
    _ocr_pages = scan_doc.get("ocr_pages")
    if _ocr_pages is None:
        _ocr_pages = sum(1 for p in pages if p.get("method") == "ocr")

    doc = {
        "id":           _new_doc_id(),
        "sig":          f"scanner:{scan_doc.get('id', filename)}",
        "filename":     filename,
        "size":         len(full_text.encode("utf-8", errors="ignore")),
        "ok":           bool(full_text.strip()),
        "error":        "" if full_text.strip() else "Документ Сканера пуст.",
        "pages":        pages,
        "full_text":    full_text,
        "head_text":    head_text,
        "head_summary": "",
        "source":       "scanner",
        "ocr_pages":       _ocr_pages,
        "ocr_ratio":       (_ocr_pages / len(pages)) if pages else 0.0,
        "ocr_quality_ok":  True,
        "ocr_quality_note": "",
    }

    if _ocr_pages:
        _ocr_text = "\n".join(
            p.get("text", "") for p in pages if p.get("method") == "ocr"
        ) or full_text
        doc["ocr_quality_ok"], doc["ocr_quality_note"] = assess_ocr_quality(_ocr_text)

    return doc


def read_document_head(doc: Dict, article_name: str,
                       client=None, model: str = None) -> str:
    """
    Верхнеуровневое чтение документа: один LLM-вызов по шапке (первые
    head_pages страниц) → одно-два предложения о том, что это за документ.

    Справочника видов документов здесь намеренно нет — система не
    классифицирует бумагу по типу, а просто коротко пересказывает, что
    прочитала. Сбой мягкий: возвращается пустая строка, документ остаётся
    пригодным для прогноза.
    """
    if not doc.get("ok") or not doc.get("head_text"):
        return ""

    cfg        = load_predictor_config()
    prompts    = load_predictor_prompts()
    max_tokens = int(cfg.get("head_summary_max_tokens", 200))

    try:
        if client is None:
            from openai import OpenAI
            lm_url, _model = _load_lm_config()
            client = OpenAI(base_url=lm_url, api_key="lm-studio", timeout=180.0)
            model = model or _model
        user_prompt = (
            prompts["predictor_doc_head_user"]
            .replace("{filename}",     doc.get("filename", ""))
            .replace("{article_name}", article_name or "(не указана)")
            .replace("{head_text}",    (doc.get("head_text") or "")[:6000])
        )
        raw = _lm_call(
            client, model, prompts["predictor_doc_head_system"],
            user_prompt, max_tokens=max_tokens,
        )
        if raw.startswith("[Ошибка LM:"):
            print(f"[PREDICTOR DOCS] Верхнеуровневое чтение не удалось: {raw}", flush=True)
            return ""
        return raw.strip()
    except Exception as e:
        print(f"[PREDICTOR DOCS] Верхнеуровневое чтение не удалось: {e}", flush=True)
        return ""


def ok_docs(docs: Optional[List[Dict]]) -> List[Dict]:
    """Только те документы, что прошли валидацию — непригодные в прогноз не идут."""
    return [d for d in (docs or []) if d.get("ok")]


def build_docs_context(docs: List[Dict], article_name: str = "",
                       budget: int = None, _progress_cb=None) -> str:
    """
    Собирает текст приложенных документов в единый блок обоснования с явной
    маркировкой источника каждого фрагмента:

        === ДОКУМЕНТ 1: Штатное расписание 2026.pdf ===
        <текст или сжатое резюме>

    Маркировка нужна классификатору, чтобы он мог указать, ИЗ КАКОГО именно
    документа взята сопоставляемая позиция (см. classify_chunk, поле "doc").

    Бюджет делится поровну между документами; тот, чей текст в свою долю не
    помещается, сжимается через compress_document (Map-Reduce по статье
    затрат), а не обрезается посередине — обрезка по символу выбрасывает
    именно концовку, где у регуляторных документов обычно и стоят выводы.
    """
    docs = ok_docs(docs)
    if not docs:
        return ""

    if budget is None:
        budget = int(load_predictor_config().get("doc_full_text_budget", 8000))

    per_doc = max(1500, budget // max(1, len(docs)))
    blocks  = []
    total   = len(docs)

    for i, doc in enumerate(docs, 1):
        body = (doc.get("full_text") or "").strip()
        if len(body) > per_doc:
            if _progress_cb:
                _progress_cb(
                    (i - 1) / total,
                    f"Сжатие документа {i}/{total}: {doc.get('filename', '')}…",
                )
            compressed = compress_document(body, article_name or "")
            if compressed and not compressed.startswith("[Ошибка сжатия:"):
                body = compressed
            else:
                # Сжатие не удалось — берём начало и конец, а не только
                # начало: выводы регуляторных документов обычно в конце.
                head_part = body[: per_doc // 2]
                tail_part = body[-(per_doc // 2):]
                body = f"{head_part}\n…\n{tail_part}"
        blocks.append(
            f"=== ДОКУМЕНТ {i}: {doc.get('filename', 'без имени')} ===\n{body}"
        )

    return "\n\n".join(blocks)


def build_doc_labels(docs: List[Dict], per_doc_chars: int = 200) -> List[str]:
    """
    Компактный нумерованный перечень приложенных документов для промпта
    классификации: «1 — имя файла: о чём документ».

    Отдельно от build_docs_context намеренно: полный текст документов в
    промпте классификации урезается по justification_chars_with_docs и
    последние документы из него могут выпасть целиком. Этот короткий
    перечень выпасть не может, поэтому привязка вердикта к документу
    остаётся возможной независимо от длины обоснования.
    """
    labels = []
    for i, doc in enumerate(ok_docs(docs), 1):
        head = (doc.get("head_summary") or "").strip().replace("\n", " ")
        line = f"{i} — {doc.get('filename', 'без имени')}"
        if head:
            line += f": {head[:per_doc_chars]}"
        labels.append(line)
    return labels


# =============================================================================
# ChromaDB — поиск по коллекциям протоколов / экспертных заключений
# =============================================================================
def _get_chroma_collection(collection_name: str):
    """
    Возвращает коллекцию ChromaDB по имени (protocols | expertise_docs).
    Использует ту же E5EmbeddingFunction что и indexer.py —
    без этого query-векторы несовместимы с индексированными passage-векторами.
    """
    try:
        from core.indexer import _get_chroma_client
        client = _get_chroma_client()

        # expertise_docs создаётся через core/expertise_chunker.py, который
        # оборачивает embedding function в совместимую с разными версиями
        # ChromaDB обёртку (см. get_chroma_embedding_function). Для protocols
        # используем embedding function как раньше, напрямую из indexer.py.
        if collection_name == _EXPERTISE_COLLECTION:
            from core.expertise_chunker import get_chroma_embedding_function
            ef = get_chroma_embedding_function()
        else:
            from core.indexer import get_embedding_function
            ef = get_embedding_function()

        try:
            return client.get_collection(name=collection_name, embedding_function=ef)
        except Exception:
            return None
    except Exception as e:
        print(f"[PREDICTOR] Ошибка подключения к ChromaDB ({collection_name}): {e}")
        return None


def _build_where_clause(filters: Optional[Dict]) -> dict:
    """
    Собирает ChromaDB `where`-выражение из словаря фильтров.
    Поддерживаемые ключи: spheres, regions, years, methods — каждый список
    строк (без эмодзи), $in для нескольких значений, $eq для одного.
    """
    if not filters:
        return {}

    clauses = []

    def _add_clause(field: str, values_key: str, legacy_key: str = None):
        values = filters.get(values_key, filters.get(legacy_key, []) if legacy_key else [])
        if isinstance(values, str) and values:
            values = [values]
        if values:
            if len(values) == 1:
                clauses.append({field: {"$eq": values[0]}})
            else:
                clauses.append({field: {"$in": values}})

    _add_clause("sphere", "spheres")
    _add_clause("region", "regions", "region")
    _add_clause("year", "years")
    _add_clause("method", "methods")

    if len(clauses) == 1:
        return clauses[0]
    elif len(clauses) > 1:
        return {"$and": clauses}
    return {}


def _cap_raw_chunk_text(text: str, source_label: str = "") -> str:
    """
    Жёстко обрезает сырой текст чанка до _RAW_CHUNK_MAX_CHARS сразу при
    получении из поиска. Прогнозисту соседние чанки не нужны никогда —
    единая точка защиты от любой инфляции чанка (neighbor-expansion,
    протёкшая настройка Советчика, изменение chunk_size при индексации и
    т.п.), независимо от конкретной причины.
    """
    text = text or ""
    if len(text) > _RAW_CHUNK_MAX_CHARS:
        print(
            f"[PREDICTOR] ⚠️ Чанк {('из ' + source_label) if source_label else ''} — "
            f"{len(text)} симв., обрезан до {_RAW_CHUNK_MAX_CHARS}.",
            flush=True,
        )
        return text[:_RAW_CHUNK_MAX_CHARS]
    return text


def search_documents(query: str, top_k: int = _DEFAULT_TOP_K,
                      filters: Optional[Dict] = None,
                      sources: Optional[List[str]] = None) -> List[Dict]:
    """
    Векторный поиск по одной или нескольким коллекциям (protocols,
    expertise_docs). `sources` — список из {"protocols", "expertise"};
    по умолчанию — только expertise.

    ВАЖНО: при поиске и в protocols, и в expertise одновременно, фильтры
    year/method применяются ТОЛЬКО к expertise_docs — коллекция protocols
    исторически индексировалась без этих полей в metadata (старые чанки их
    не содержат), поэтому year/method-фильтр на ней просто не будет давать
    результатов. Если в фильтрах заданы years/methods, а источник включает
    protocols, year/method для protocols-части запроса не передаются.
    """
    if sources is None:
        sources = ["expertise"]

    where = _build_where_clause(filters)
    has_year_or_method = bool(filters and (filters.get("years") or filters.get("methods")))

    all_chunks: List[Dict] = []

    if "expertise" in sources:
        collection = _get_chroma_collection(_EXPERTISE_COLLECTION)
        if collection is not None:
            try:
                kwargs = dict(query_texts=[query], n_results=top_k,
                              include=["documents", "metadatas", "distances"])
                if where:
                    kwargs["where"] = where
                results = collection.query(**kwargs)
                docs      = results.get("documents", [[]])[0]
                metas     = results.get("metadatas",  [[]])[0]
                distances = results.get("distances",  [[]])[0]
                for doc, meta, dist in zip(docs, metas, distances):
                    all_chunks.append({
                        "text":          _cap_raw_chunk_text(doc, meta.get("filename", "")),
                        "file":          meta.get("filename", ""),
                        "date":          meta.get("protocol_date", ""),
                        "sphere":        meta.get("sphere", ""),
                        "region":        meta.get("region", ""),
                        "year":          meta.get("year", ""),
                        "method":        meta.get("method", ""),
                        "organization":  meta.get("organization", ""),
                        "section":       meta.get("section", ""),
                        "source":        "expertise",
                        "distance":      dist,
                    })
            except Exception as e:
                print(f"[PREDICTOR] Ошибка поиска в expertise_docs: {e}")

    if "protocols" in sources:
        # При комбинированном поиске year/method не применяются к protocols
        # (исторические данные без этих полей в metadata) — собираем
        # отдельный where без year/method для этой коллекции.
        protocols_filters = dict(filters or {})
        protocols_filters.pop("years", None)
        protocols_filters.pop("methods", None)
        protocols_where = _build_where_clause(protocols_filters)

        collection = _get_chroma_collection(_PROTOCOLS_COLLECTION)
        if collection is not None:
            try:
                kwargs = dict(query_texts=[query], n_results=top_k,
                              include=["documents", "metadatas", "distances"])
                if protocols_where:
                    kwargs["where"] = protocols_where
                results = collection.query(**kwargs)
                docs      = results.get("documents", [[]])[0]
                metas     = results.get("metadatas",  [[]])[0]
                distances = results.get("distances",  [[]])[0]
                for doc, meta, dist in zip(docs, metas, distances):
                    all_chunks.append({
                        "text":         _cap_raw_chunk_text(doc, meta.get("file", "")),
                        "file":         meta.get("file", ""),
                        "date":         meta.get("date", ""),
                        "sphere":       meta.get("sphere", ""),
                        "region":       meta.get("region", ""),
                        "year":         "",   # отсутствует в исторических metadata
                        "method":       "",   # отсутствует в исторических metadata
                        "organization": meta.get("organization", ""),
                        "section":      "",
                        "source":       "protocols",
                        "distance":     dist,
                    })
            except Exception as e:
                print(f"[PREDICTOR] Ошибка поиска в protocols: {e}")

    # Сортируем общий пул по близости (меньше distance = релевантнее) и
    # обрезаем до top_k — иначе при двух источниках результатов будет до 2×top_k.
    all_chunks.sort(key=lambda c: c.get("distance", 1.0))
    return all_chunks[:top_k]


def search_protocols(query: str, top_k: int = _DEFAULT_TOP_K,
                     filters: Optional[Dict] = None) -> List[Dict]:
    """
    DEPRECATED: оставлена для обратной совместимости с любым внешним кодом,
    который мог импортировать именно эту функцию. Эквивалентна
    search_documents(..., sources=["expertise"]).
    """
    return search_documents(query, top_k=top_k, filters=filters, sources=["expertise"])


# =============================================================================
# Гибридный поиск (BM25 + векторный + CrossEncoder reranking) для expertise_docs
#
# Переиспользует инфраструктуру core/advisor.py (HybridRetriever, Reranker) —
# тот же подход, что и в Советчике для НПА — вместо чистого векторного
# cosine-similarity поиска, который на коротких запросах (название статьи
# затрат) даёт заметно более слабые результаты.
#
# ВАЖНО: создаётся ОТДЕЛЬНЫЙ синглтон HybridRetriever для expertise_docs —
# не путать с тем, что использует Советчик для tariff_docs (НПА). Coллекция
# передаётся в конструктор HybridRetriever явно.
# =============================================================================
_EXPERTISE_HYBRID_RETRIEVER_KEY = "__regula_ai_expertise_hybrid_retriever__"


def _get_expertise_hybrid_retriever():
    """
    Синглтон HybridRetriever для коллекции expertise_docs. Использует
    класс HybridRetriever из core/advisor.py напрямую (тот же BM25 + RRF
    fusion алгоритм), но со своим кэшем и своей коллекцией — изолирован
    от ретривера НПА.
    """
    import sys
    try:
        from core.advisor import HybridRetriever, BM25_AVAILABLE
    except Exception as e:
        print(f"[PREDICTOR] core.advisor недоступен для гибридного поиска: {e}")
        return None

    if not BM25_AVAILABLE:
        return None

    collection = _get_chroma_collection(_EXPERTISE_COLLECTION)
    if collection is None:
        return None

    try:
        current_count = collection.count()
    except Exception:
        current_count = -1

    existing = sys.modules.get(_EXPERTISE_HYBRID_RETRIEVER_KEY)
    if existing is not None and getattr(existing, "_collection_count", -1) == current_count:
        return existing

    try:
        retriever = HybridRetriever(collection)
        retriever._collection_count = current_count
        sys.modules[_EXPERTISE_HYBRID_RETRIEVER_KEY] = retriever
        return retriever
    except Exception as e:
        print(f"[PREDICTOR] Не удалось построить HybridRetriever для expertise_docs: {e}")
        return None


def invalidate_expertise_hybrid_retriever():
    """Сбрасывает BM25-индекс expertise_docs — вызывать после переиндексации."""
    import sys
    sys.modules.pop(_EXPERTISE_HYBRID_RETRIEVER_KEY, None)


def _filter_candidates_by_where(candidates: List[Dict], filters: Optional[Dict]) -> List[Dict]:
    """
    HybridRetriever.search() не поддерживает ChromaDB `where` напрямую
    (BM25-часть ищет по всем чанкам в памяти), поэтому фильтрацию по
    sphere/region/year/method применяем после получения кандидатов —
    на полном пуле документов это недорого (тысячи, не миллионы записей).
    """
    if not filters:
        return candidates

    def _matches(meta: dict) -> bool:
        for key, field in [("spheres", "sphere"), ("regions", "region"),
                            ("years", "year"), ("methods", "method")]:
            values = filters.get(key)
            if values and meta.get(field) not in values:
                return False
        return True

    return [c for c in candidates if _matches(c.get("meta", {}))]


def search_documents_hybrid(
    query: str,
    article_name: str = "",
    top_k: int = _DEFAULT_TOP_K,
    filters: Optional[Dict] = None,
    retrieval_pool: int = 60,
) -> List[Dict]:
    """
    Гибридный поиск по expertise_docs: BM25 + векторный (RRF fusion) →
    CrossEncoder reranking относительно `article_name` (или `query`, если
    article_name не передан) → top_k финальных результатов.

    `retrieval_pool` — сколько кандидатов берёт HybridRetriever ДО
    реранкинга (с запасом, т.к. фильтры sphere/region/year/method и
    последующий реранкинг могут уменьшить пул).

    При недоступности BM25/reranker — мягкий fallback на обычный
    векторный поиск (search_documents).

    ПРОГНОЗИСТУ СОСЕДНИЕ ЧАНКИ НЕ НУЖНЫ НИКОГДА — только сам целевой
    чанк с конкретным решением регулятора; окружающий контекст соседних
    фрагментов только размывает узкую задачу классификации "какой вариант
    выбран" и может приводить к раздутым (35 000+ симв.) чанкам,
    съедающим весь бюджет контекста (см. _truncate_chunks_by_char_budget).
    HybridRetriever — общий с Советчиком (core/advisor.py), который умеет
    подтягивать соседей через st.session_state["neighbor_radius"]
    (настройка на странице Советчика, session_state общий на весь процесс
    Streamlit) — если пользователь недавно выставил её в Советчике, она
    могла "протечь" и в поиск Прогнозиста в той же сессии. Поэтому здесь
    временно форсируем 0 на время вызова и восстанавливаем прежнее
    значение сразу после — чтобы не сбить настройку Советчика.
    """
    retriever = _get_expertise_hybrid_retriever()
    if retriever is None:
        print("[PREDICTOR] Гибридный поиск недоступен, fallback на векторный поиск.")
        return search_documents(query, top_k=top_k, filters=filters, sources=["expertise"])

    _had_neighbor_key = "neighbor_radius" in st.session_state
    _prev_neighbor_radius = st.session_state.get("neighbor_radius")
    st.session_state["neighbor_radius"] = 0
    try:
        candidates = retriever.search(query, top_k=retrieval_pool)
    finally:
        if _had_neighbor_key:
            st.session_state["neighbor_radius"] = _prev_neighbor_radius
        else:
            st.session_state.pop("neighbor_radius", None)

    candidates = _filter_candidates_by_where(candidates, filters)

    if not candidates:
        return []

    try:
        from core.advisor import get_reranker
        reranker = get_reranker()
    except Exception:
        reranker = None

    rerank_query = article_name.strip() if article_name and article_name.strip() else query

    if reranker is not None:
        reranked = reranker.rerank(rerank_query, candidates, top_n=top_k)
    else:
        # Без реранкера — берём по RRF-score (уже отсортированы HybridRetriever.search)
        reranked = candidates[:top_k]

    results: List[Dict] = []
    for c in reranked:
        meta = c.get("meta", {})
        results.append({
            "text":          _cap_raw_chunk_text(c.get("doc", ""), meta.get("filename", "")),
            "file":          meta.get("filename", ""),
            "date":          meta.get("protocol_date", ""),
            "sphere":        meta.get("sphere", ""),
            "region":        meta.get("region", ""),
            "year":          meta.get("year", ""),
            "method":        meta.get("method", ""),
            "organization":  meta.get("organization", ""),
            "section":       meta.get("section", ""),
            "tag":           meta.get("tag", ""),
            "source":        "expertise",
            "rerank_score":  c.get("rerank_score"),
            "distance":      1.0 - (c.get("rerank_score") or 0) if c.get("rerank_score") is not None else c.get("score", 1.0),
        })
    return results


def _truncate_chunks_by_char_budget(
    chunks: List[Dict], budget: int = _RAG_CONTEXT_CHAR_BUDGET,
) -> Tuple[List[Dict], int]:
    """
    Обрезает список чанков (уже отсортированных по релевантности) так,
    чтобы суммарная длина их текста не превышала `budget` символов.
    Берём чанки по порядку, пока сумма не превысит лимит — остальные
    отбрасываем целиком (не дробим текст внутри чанка).

    Защищает от случаев, когда большое top_k (например 30) с длинными
    чанками создаёт огромный совокупный контекст для последовательной
    LLM-классификации — это и замедляет генерацию (особенно при
    включённом Unified KV Cache в LM Studio, который накапливает
    контекст между последовательными вызовами).

    ВАЖНО — ФИКС "ОСТАЁТСЯ ТОЛЬКО 1 ИСТОЧНИК": раньше в сумму бюджета
    считалась ПОЛНАЯ сырая длина текста чанка, как его вернул поиск. Но
    каждый чанк и так обрезается до `chunk_chars_to_llm` (по умолчанию
    1500) непосредственно перед отправкой в LLM — см. classify_chunk.
    Если ретривер начинает возвращать раздутые чанки (например, из-за
    neighbor-expansion — известная проблема, см. заметку про
    debug_search_candidates в claim_analyzer: один чанк может раздуться с
    ~1750 до 35 000+ символов), ОДИН такой чанк сам по себе исчерпывал
    весь 35-тысячный бюджет — и все остальные источники отбрасывались,
    сколько бы top-K ни было выбрано. Теперь бюджет считается по той же
    ЭФФЕКТИВНОЙ длине (после обрезки до chunk_chars_to_llm), что реально
    уйдёт в LLM — а не по сырой длине из поиска.

    Возвращает (обрезанный_список, отброшено_чанков).
    """
    _chunk_chars_to_llm = int(load_predictor_config().get("chunk_chars_to_llm", 1500))

    kept: List[Dict] = []
    total_chars = 0
    for chunk in chunks:
        raw_len = len(chunk.get("text", "") or "")
        effective_len = min(raw_len, _chunk_chars_to_llm)
        if raw_len > _chunk_chars_to_llm * 3:
            print(
                f"[BUDGET] ⚠️ Чанк из '{chunk.get('file', '?')}' — {raw_len} симв. сырых "
                f"(>{_chunk_chars_to_llm * 3}), похоже на инфляцию чанка (neighbor-expansion?). "
                f"Учтён как {effective_len} симв. (после обрезки до chunk_chars_to_llm).",
                flush=True,
            )
        if kept and total_chars + effective_len > budget:
            break
        kept.append(chunk)
        total_chars += effective_len
    dropped = len(chunks) - len(kept)
    return kept, dropped


# =============================================================================
# Классификация чанка через LLM
# =============================================================================

_DECISION_FIELDS_RE = re.compile(
    r'(?:[*•]\s*)?\*{0,2}Заявлено(?:\s+предприятием)?\*{0,2}\s*:?\s*\*{0,2}\s*(?P<claimed>[^\n\r]+)|'
    r'(?:[*•]\s*)?\*{0,2}Принято(?:\s+экспертами)?\*{0,2}\s*:?\s*\*{0,2}\s*(?P<accepted>[^\n\r]+)|'
    r'(?:[*•]\s*)?\*{0,2}Корректировка\*{0,2}\s*(?:\+/-)?\s*:?\s*\*{0,2}\s*(?P<adjustment>[^\n\r]+)|'
    r'(?:[*•]\s*)?\*{0,2}ОБОСНОВАНИЕ\*{0,2}\s*:?\s*\*{0,2}\s*(?P<rationale>[^\n\r]+)',
    re.IGNORECASE,
)


def extract_decision_fields(chunk_text: str) -> Dict[str, str]:
    """
    Извлекает структурированные поля "Заявлено / Принято / Корректировка /
    ОБОСНОВАНИЕ" из текста чанка (формат экспертных заключений). Эти поля
    содержат ГОТОВОЕ решение регулятора по статье — не нужно угадывать
    тон текста, решение уже есть в цифрах.

    Формат варьируется между документами (с markdown-bold, со
    звёздочками/буллетами, с разбивкой по годам), поэтому используется
    гибкая регулярка без строгой привязки к одной форме записи. Если
    поля не найдены — возвращает пустой словарь (не пытаемся "придумать"
    структуру там, где её нет в тексте).
    """
    found: Dict[str, str] = {}
    for m in _DECISION_FIELDS_RE.finditer(chunk_text):
        for key in ("claimed", "accepted", "adjustment", "rationale"):
            val = m.group(key)
            if val and key not in found:
                val = val.strip().rstrip(".,;").strip("*").strip()
                found[key] = val
    return found


# Маркеры явного согласия/несогласия в тексте reason — используются для
# программной проверки согласованности с decision (модель иногда пишет
# текстом верный вывод "совпадение позиции", но ставит decision=negative
# по инерции — это противоречие, и оно перебивает decision модели,
# поскольку текстовый вывод reason надёжнее одного отдельного поля).
_REASON_AGREE_RE = re.compile(
    r'совпадени[ея]\s+(позици|подход|вариант|метод)|'
    r'(тот\s+же|такой\s+же|один\s+и\s+тот\s+же)\s+(вариант|подход|принцип|срок|метод)|'
    r'совпадает\s+с\s+позицией|регулятор\s+(также|тоже)\s+(выбрал|применил|использовал)',
    re.IGNORECASE,
)
_REASON_DISAGREE_RE = re.compile(
    r'противоречи[ея]|не\s+совпадает|противоположн|расхожд|'
    r'(другой|иной)\s+(вариант|подход|принцип)|'
    r'регулятор\s+(не\s+согласен|отклонил|отказал)',
    re.IGNORECASE,
)


def _reconcile_decision_with_reason(decision: str, reason: str) -> tuple[str, bool]:
    """
    Программная проверка согласованности decision с текстом reason.
    Модель иногда формулирует в reason явный и корректный вывод
    ("это совпадение позиции"), но всё равно выставляет противоречащий
    decision (например negative). Полагаться на то, что модель сама себя
    проверит в один проход, ненадёжно — поэтому здесь явный пост-фактум
    разбор reason по маркерам согласия/несогласия.

    При обнаруженном противоречии текстовый вывод reason считается более
    надёжным сигналом (модель его явно сформулировала словами) и
    перебивает decision. Возвращает (итоговый_decision, был_ли_исправлен).
    """
    agree   = bool(_REASON_AGREE_RE.search(reason))
    disagree = bool(_REASON_DISAGREE_RE.search(reason))

    # Однозначный сигнал согласия в reason, а decision говорит об обратном
    if agree and not disagree and decision == "negative":
        return "positive", True
    # Однозначный сигнал несогласия в reason, а decision говорит о позитиве
    if disagree and not agree and decision == "positive":
        return "negative", True

    return decision, False


_GENERIC_CHOICE_FILLER_WORDS = {
    "данные", "данных", "данным", "данными", "данные,",
    "сведения", "сведений", "сведениям", "сведениями",
    "информация", "информации", "информацию",
    "показатели", "показателей", "показателям", "показателями",
    "документ", "документа", "документу", "документе",
}

# Известные пары ПРОТИВОПОЛОЖНЫХ по смыслу вариантов в этой предметной
# области (тарифное регулирование). Расширенный список: сроки/величины,
# методология расчёта, итоговое решение по заявке, объём удовлетворения.
#
# ВАЖНО про формат: каждый элемент — (regex_1, regex_2). Используется
# re.search, а не plain-substring — это принципиально для пар вида
# "обоснован"/"необоснован", где второе слово ЛИТЕРАЛЬНО содержит первое
# как подстроку ("не"+"обоснованный"). При обычной substring-проверке
# "необоснованный" ложно засчитывался бы одновременно и за "обоснован", и
# за "необоснован", создавая ложный конфликт даже при сравнении слова
# самого с собой. Для таких пар позитивный вариант помечен
# (?<!не) — "не встречается сразу после 'не'".
_ANTONYM_MARKER_PAIRS = [
    # Сроки / величины
    (r"максимальн", r"минимальн"),
    (r"максимум", r"минимум"),
    (r"верхн", r"нижн"),
    (r"остаточн", r"первоначальн"),
    # Методология расчёта: факт vs норматив/план
    (r"фактическ", r"норматив"),
    (r"\bфакт", r"норматив"),
    (r"(?<!не)план", r"\bфакт"),
    (r"(?<!не)расчётн", r"фактическ"),
    (r"(?<!не)расчетн", r"фактическ"),
    # Включение / исключение из расчёта
    (r"включ", r"исключ"),
    (r"(?<!не)учит", r"неучит"),
    (r"(?<!не)учит", r"не\s+учит"),
    # Итоговое решение регулятора по заявке/подходу
    (r"принят", r"отклон"),
    (r"принят", r"отказ"),
    (r"одобр", r"отказ"),
    (r"одобр", r"отклон"),
    (r"удовлетвор", r"отказ"),
    (r"согласи", r"не\s+согласи"),
    (r"поддерж", r"отклон"),
    # Объём удовлетворения
    (r"полност", r"частичн"),
    (r"в\s+полном\s+объ[её]ме", r"частичн"),
    (r"цели[кч]ом", r"частичн"),
    # Динамика величины (используется реже, но встречается в reason)
    (r"повышен", r"понижен"),
    (r"увеличен", r"уменьшен"),
    (r"увеличен", r"снижен"),
    (r"рост", r"снижен"),
    # Обоснованность/целесообразность/правомерность — частые формулировки в
    # экспертных заключениях ("расходы признаны необоснованными" и т.п.)
    (r"(?<!не)обоснован", r"необоснован"),
    (r"(?<!не)эффективн", r"неэффективн"),
    (r"(?<!не)целесообразн", r"нецелесообразн"),
    (r"(?<!не)правомерн", r"неправомерн"),
    (r"(?<!не)корректн", r"некорректн"),
    (r"(?<!не)обусловлен", r"необусловлен"),
    # Линейный/нелинейный метод амортизации (типичная развилка по статье
    # "Амортизация") — lookbehind нужен по той же причине, что и выше
    (r"(?<!не)линейн", r"нелинейн"),
]


def _has_antonym_conflict(a: str, b: str) -> bool:
    """True, если в двух нормализованных строках обнаружены маркеры
    противоположных по смыслу вариантов из разных пар _ANTONYM_MARKER_PAIRS
    (в любом порядке сторон). Использует re.search (не substring) — см.
    комментарий к _ANTONYM_MARKER_PAIRS про lookbehind для пар вида
    "обоснован"/"необоснован"."""
    for pat1, pat2 in _ANTONYM_MARKER_PAIRS:
        a1, a2 = re.search(pat1, a), re.search(pat2, a)
        b1, b2 = re.search(pat1, b), re.search(pat2, b)
        if (a1 and b2) or (a2 and b1):
            return True
    return False


def _normalize_choice_text(s: str) -> str:
    """Убирает пунктуацию и общие слова-обвязки ('данные X' → 'X'), чтобы
    сравнение не спотыкалось на чисто стилистической разнице формулировок."""
    s = (s or "").lower().strip()
    s = re.sub(r'[«»"\'.,;:!?()]', '', s)
    words = [w for w in s.split() if w not in _GENERIC_CHOICE_FILLER_WORDS]
    return " ".join(words).strip()


def _choices_look_same(reg_choice: str, user_choice: str) -> bool:
    """
    Детерминированная страховка ПОВЕРХ LLM-сравнения независимого
    верификатора. Небольшая модель на узкой задаче сравнения двух
    коротких формулировок иногда слишком буквально придирается к разнице
    в словах и объявляет same_choice=false там, где по сути один и тот же
    документ/показатель просто сформулирован чуть иначе — например,
    "данные штатного расписания" и "штатное расписание" (реальный кейс,
    из-за которого верный positive ошибочно понижался до neutral).

    ВАЖНО: сначала проверяется _has_antonym_conflict — "максимальный срок"
    и "минимальный срок" совпадают на 85% посимвольно, но это семантические
    противоположности (реальный кейс ложного positive). Явный антоним
    перебивает любую строковую похожесть и сразу даёт "не совпадают".

    Не заменяет LLM-сравнение полностью (для действительно разных
    документов эвристика ничего не даст — ratio будет низким), а лишь
    ловит явные случаи почти дословного совпадения, которые модель могла
    пропустить.
    """
    a = _normalize_choice_text(reg_choice)
    b = _normalize_choice_text(user_choice)
    if not a or not b:
        return False
    if _has_antonym_conflict(a, b):
        return False
    if a == b or a in b or b in a:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= 0.65


def _verify_regulator_choice_vs_user_position(
    quote: str, justification: str, client, model: str,
) -> tuple[bool, str]:
    """
    ВТОРОЙ ЭТАП проверки (отдельный, узкий LLM-вызов) — ПЕРЕРАБОТАННАЯ ВЕРСИЯ.

    Прежняя версия сравнивала quote с reason первого этапа — но reason сам
    может быть сформулирован размыто/двусмысленно ("совпадает с позицией
    пользователя о фактических потерях в контексте применения
    нормативного подхода" — формально не повторяет противоречащее слово
    напрямую, поэтому узкая текстовая сверка quote↔reason такое
    пропускает).

    Новая версия убирает reason как посредника: верификатор САМ читает
    quote (что реально выбрал регулятор) и justification (позицию
    текущего пользователя), и САМ определяет совпадение или
    противоречие — независимо от того, как это сформулировал первый
    этап. Это устраняет риск унаследовать путаную формулировку.

    Возвращает (is_consistent, verification_note).
    Если проверка сама не удалась технически — возвращает (True, "") —
    не блокирует результат первого этапа при сбое самой проверки.
    """
    if not quote or not justification:
        return True, ""

    system_prompt = (
        "Ты определяешь, какой конкретный вариант/подход выбран в двух "
        "текстах, и совпадают ли эти варианты. Отвечай только JSON, без "
        "рассуждений."
    )
    user_prompt = (
        f"ТЕКСТ 1 (решение регулятора, цитата из документа): {quote}\n"
        f"ТЕКСТ 2 (позиция пользователя): {justification}\n\n"
        f"Помни: ТЕКСТ 1 взят из протокола РЭК или экспертного заключения, "
        f"подготовленного для обоснования решения РЭК — то есть по "
        f"умолчанию это позиция самого регулятора, а не организации-"
        f"заявителя (если только в тексте прямо не сказано 'заявлено "
        f"предприятием'/'по расчётам организации').\n\n"
        f"Шаг 1: определи, какой конкретный вариант/подход выбран в "
        f"ТЕКСТЕ 1 (например 'норматив', 'фактические показатели', "
        f"'максимальный срок', 'минимальный срок', 'штатное расписание', "
        f"'приказ об изменении ФОТ' — конкретный документ/значение, а не "
        f"общая тема).\n"
        f"Шаг 2: определи, какой конкретный вариант/подход заявлен в "
        f"ТЕКСТЕ 2.\n"
        f"Шаг 3: сравни — это ОДИН И ТОТ ЖЕ вариант, или ПРОТИВОПОЛОЖНЫЕ/"
        f"РАЗНЫЕ варианты внутри одной темы (например 'норматив' и "
        f"'фактические показатели' — противоположны; 'максимальный' и "
        f"'минимальный' — противоположны; 'штатное расписание' и 'приказы "
        f"об изменении ФОТ' — это РАЗНЫЕ конкретные документы, даже если "
        f"оба формально официальные/нормативные)?\n\n"
        f"ВАЖНО: не считай совпадением то, что оба текста просто "
        f"'опираются на официальные/нормативные документы' — это "
        f"абстрактная категория, а не конкретный вариант. same_choice=true "
        f"ставь только если названы буквально один и тот же документ/"
        f"показатель (или явный синоним), а не просто оба относятся к "
        f"одному типу источников.\n\n"
        'JSON: {{"same_choice": true|false, "regulator_choice": "вариант из ТЕКСТА 1, до 40 симв.", "user_choice": "вариант из ТЕКСТА 2, до 40 симв."}}'
    )

    print(f"[VERIFY] Независимая проверка: quote='{quote[:60]}...' justification='{justification[:60]}...'", flush=True)
    try:
        raw = _lm_call(client, model, system_prompt, user_prompt, max_tokens=150)
        if raw.startswith("[Ошибка LM:"):
            print(f"[VERIFY] LM-вызов завершился ошибкой: {raw}", flush=True)
            return True, ""
        clean = re.sub(r'```json|```', '', raw).strip()
        data = json.loads(clean)
        same_choice = data.get("same_choice")
        if same_choice is None:
            # Модель не дала однозначный ответ — не блокируем
            return True, ""
        reg_choice = (data.get("regulator_choice") or "").strip()
        user_choice = (data.get("user_choice") or "").strip()

        # ВАЖНО: если верификатор сам не смог извлечь один из двух вариантов
        # (пустая строка на месте regulator_choice или user_choice) — это
        # означает СБОЙ ИЗВЛЕЧЕНИЯ самого верификатора, а не реальное
        # противоречие позиций. Раньше в этом случае same_choice=false
        # (пустое ≠ непустое) неправомерно понижал корректный
        # positive/negative первого этапа до neutral — именно так терялись
        # результаты вида "регулятор: «штатное расписание», пользователь:
        # «»". Считаем такую проверку неинформативной и НЕ блокируем
        # результат первого этапа.
        if not reg_choice or not user_choice:
            print(
                f"[VERIFY] Пустой вариант при извлечении "
                f"(регулятор='{reg_choice}', пользователь='{user_choice}') — "
                f"проверка неинформативна, не понижаем decision",
                flush=True,
            )
            return True, ""

        same_choice = bool(same_choice)

        # ДЕТЕРМИНИРОВАННАЯ СТРАХОВКА: если LLM сказала same_choice=false,
        # но по факту извлечённые формулировки почти дословно совпадают
        # (например, "данные штатного расписания" и "штатное расписание" —
        # реальный кейс), не доверяем решению модели вслепую и признаём
        # совпадение эвристически. Модель на этой узкой задаче иногда
        # слишком буквально придирается к стилистической разнице
        # формулировок вместо содержательного сравнения.
        if not same_choice and _choices_look_same(reg_choice, user_choice):
            print(
                f"[VERIFY] LLM вернула same_choice=false, но формулировки "
                f"почти совпадают ('{reg_choice}' ~ '{user_choice}') — "
                f"эвристика перебивает вердикт на 'совпадают', не понижаем",
                flush=True,
            )
            same_choice = True

        note = f"регулятор: «{reg_choice}», пользователь: «{user_choice}»"
        print(f"[VERIFY] Независимый результат: same_choice={same_choice} ({note})", flush=True)
        return same_choice, note
    except Exception as e:
        print(f"[VERIFY] Сбой независимой проверки (raw='{raw if 'raw' in dir() else '?'}'): {e}", flush=True)
        return True, ""


def classify_chunk(chunk_text: str, article_name: str, justification_summary: str,
                   client, model: str, force_verification: Optional[bool] = None,
                   doc_labels: Optional[List[str]] = None) -> Dict:
    """
    Классифицирует один чанк протокола.
    Возвращает: {"decision": "positive"|"negative"|"neutral", "quote": str, "reason": str}
    Параметры читаются из config/predictor_config.json,
    промпты — из config/prompts.json (настраиваются в Админке).

    `force_verification` — если передан (True/False), перебивает значение
    "enable_verification" из конфига для этого конкретного запуска
    (используется переключателем в интерфейсе, чтобы можно было быстро
    сравнить результат с включённой/выключенной второй проверкой без
    правки config-файла).

    `doc_labels` — компактный перечень приложенных пользователем документов
    (см. build_doc_labels). Если передан и включён doc_attribution, к
    промпту добавляется требование указать в поле "doc" номер документа,
    из которого взята сопоставляемая позиция пользователя — тогда в
    результате появляется "source_doc_idx". Привязка делается ВНУТРИ этого
    же вызова, без отдельного LLM-запроса на чанк: стоимость прогноза не
    удваивается, добавляется лишь несколько токенов в ответе.

    ВАЖНО: когда документов нет (doc_labels пуст), промпт и лимиты
    остаются ровно теми же, что и до появления этой возможности —
    поведение прежнего сценария «только текстом» не меняется вообще.
    """
    cfg     = load_predictor_config()
    prompts = load_predictor_prompts()

    _doc_labels     = [l for l in (doc_labels or []) if l]
    _attribution_on = bool(_doc_labels) and bool(cfg.get("doc_attribution", True))

    _chunk_chars   = int(cfg["chunk_chars_to_llm"])
    # С приложенными документами обоснование урезать до justification_chars
    # (200 симв. по умолчанию) нельзя — от позиции пользователя не остаётся
    # ничего, и сравнивать становится не с чем.
    _justify_chars = int(
        cfg.get("justification_chars_with_docs", 2000) if _doc_labels
        else cfg["justification_chars"]
    )
    _max_tokens    = int(cfg["classify_max_tokens"])

    _chunk   = chunk_text[:_chunk_chars]

    # Извлекаем структурированное решение (Заявлено/Принято/Корректировка/
    # ОБОСНОВАНИЕ), если оно присутствует, и явно подсвечиваем его перед
    # текстом чанка — чтобы модель опиралась на цифры решения, а не
    # угадывала тональность по формулировкам.
    #
    # ВАЖНО: и "Заявлено предприятием" из decision_fields (то, что просила
    # ДРУГАЯ организация в прецеденте), и "Обоснование решения" из
    # decision_fields (причина регулятора по ТОЙ организации) — это текст
    # из найденного прецедента, не имеющий отношения к текущему
    # пользователю. Чтобы модель не путала это с позицией ТЕКУЩЕГО
    # пользователя (justification_line), оборачиваем оба источника в явные
    # блочные метки.
    decision_fields = extract_decision_fields(_chunk)
    if decision_fields:
        _hint_lines = ["[Структурированное решение по статье из ПРЕЦЕДЕНТА (другая организация, не текущий пользователь):]"]
        if "claimed" in decision_fields:
            _hint_lines.append(f"В прецеденте заявлено той организацией: {decision_fields['claimed']}")
        if "accepted" in decision_fields:
            _hint_lines.append(f"В прецеденте принято экспертами/регулятором: {decision_fields['accepted']}")
        if "adjustment" in decision_fields:
            _hint_lines.append(f"Корректировка в прецеденте: {decision_fields['adjustment']}")
        if "rationale" in decision_fields:
            _hint_lines.append(f"Причина решения регулятора в прецеденте: {decision_fields['rationale']}")
        _chunk = "\n".join(_hint_lines) + "\n\n" + _chunk

    _justify = justification_summary[:_justify_chars] if justification_summary and _justify_chars > 0 else ""
    _justify_line = (
        f"=== ПОЗИЦИЯ ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ (то, что он обосновывает сейчас) ===\n"
        f"{_justify}\n"
        f"=== КОНЕЦ ПОЗИЦИИ ПОЛЬЗОВАТЕЛЯ ===\n"
    ) if _justify else ""

    _chunk = (
        f"=== РЕШЕНИЕ ИЗ ПРЕЦЕДЕНТА (другая организация, другой случай) ===\n"
        f"{_chunk}\n"
        f"=== КОНЕЦ РЕШЕНИЯ ИЗ ПРЕЦЕДЕНТА ==="
    )

    system_prompt = prompts["predictor_classify_system"]
    user_template = prompts["predictor_classify_user"]

    prompt = (
        user_template
        .replace("{article_name}",      article_name)
        .replace("{justification_line}", _justify_line)
        .replace("{chunk}",             _chunk)
    )

    # ── Привязка вердикта к конкретному приложенному документу ───────────
    # Блок добавляется в самый конец промпта и ЯВНО отменяет формат ответа,
    # заданный выше в шаблоне — иначе модель, увидев два описания JSON,
    # выбирает первое и поле "doc" не возвращает. Основная инструкция по
    # классификации при этом не трогается: добавочный текст говорит только
    # о том, ОТКУДА взята позиция пользователя, и не вмешивается в правила
    # определения decision.
    if _attribution_on:
        _docs_list = "\n".join(_doc_labels)
        prompt += (
            "\n\nДОПОЛНИТЕЛЬНО. Пользователь приложил документы-обоснования:\n"
            f"{_docs_list}\n\n"
            "Укажи в поле \"doc\" номер ТОГО документа, в котором заявлена "
            "позиция пользователя, которую ты сейчас сравнивал с решением "
            "регулятора. Если позиция взята из текстового описания, а не из "
            "документа, либо определить источник невозможно — укажи 0. Не "
            "угадывай: 0 — нормальный ответ.\n"
            "ИТОГОВЫЙ ФОРМАТ ОТВЕТА (заменяет указанный выше; поле \"doc\" "
            "обязательно):\n"
            '{"decision":"positive|negative|neutral","quote":"…","reason":"…","doc":0}'
        )
        _max_tokens += 20  # запас на добавочное поле, чтобы не обрезать JSON

    raw = _lm_call(client, model, system_prompt, prompt, max_tokens=_max_tokens)
    # Парсим JSON
    try:
        # Убираем возможные обёртки ```json
        clean = re.sub(r'```json|```', '', raw).strip()
        data = json.loads(clean)
        decision = data.get("decision", "neutral")
        if decision not in ("positive", "negative", "neutral"):
            decision = "neutral"
        reason = data.get("reason", "")
        quote  = data.get("quote", chunk_text[:150])
        decision, _was_fixed = _reconcile_decision_with_reason(decision, reason)

        # Номер приложенного документа, давшего основание для сравнения.
        # 0 / отсутствие поля / мусор / номер вне диапазона → None: привязка
        # просто не показывается, на сам вердикт это не влияет.
        _source_doc_idx = None
        if _attribution_on:
            try:
                _raw_doc = data.get("doc", 0)
                _doc_num = int(str(_raw_doc).strip())
                if 1 <= _doc_num <= len(_doc_labels):
                    _source_doc_idx = _doc_num
            except Exception:
                _source_doc_idx = None

        needs_expert_review = False
        original_decision = None

        # ВТОРОЙ ЭТАП: НЕЗАВИСИМАЯ проверка quote vs justification_summary
        # (позиция пользователя), без посредника reason.
        #
        # ВАЖНО — НАПРАВЛЕННАЯ ПРОВЕРКА (правило: neutral только если этапы
        # РАСХОДЯТСЯ): same_choice сам по себе НЕ говорит, верен ли decision
        # — это зависит от того, что предсказал ПЕРВЫЙ этап:
        #   - decision == "positive" ожидает same_choice == True (регулятор
        #     должен был выбрать ТОТ ЖЕ вариант, что и пользователь).
        #     same_choice == False здесь означает: 1-й этап сказал "за",
        #     2-й — "против" → это и есть расхождение этапов → neutral.
        #   - decision == "negative" ожидает same_choice == False (регулятор
        #     должен был выбрать ПРОТИВОПОЛОЖНЫЙ вариант — это и делает его
        #     "против"). same_choice == True здесь означает: 1-й этап сказал
        #     "против", а 2-й, наоборот, нашёл совпадение → зеркальное
        #     расхождение → тоже neutral.
        #
        # РАНЬШЕ код всегда требовал same_choice == True для ЛЮБОГО decision
        # (в т.ч. для negative) — из-за этого КАЖДЫЙ верно определённый
        # "против" результат ошибочно понижался в neutral: для negative
        # same_choice ПРАВИЛЬНО должен быть False, а старый код трактовал
        # такой (корректный!) False как "проверка провалилась". Это было
        # главной причиной того, что почти все результаты уходили в
        # нейтраль.
        _verification_on = (
            force_verification if force_verification is not None
            else bool(cfg.get("enable_verification", True))
        )
        if decision != "neutral" and _verification_on:
            print(f"[VERIFY] Запуск второго этапа для decision={decision}", flush=True)
            same_choice, verify_note = _verify_regulator_choice_vs_user_position(
                quote, justification_summary, client, model,
            )
            expected_same_choice = (decision == "positive")
            if same_choice != expected_same_choice:
                _stage1_label = "за" if decision == "positive" else "против"
                _stage2_label = "за" if same_choice else "против"
                print(
                    f"[VERIFY] ⚠️ Расхождение этапов: 1-й этап='{_stage1_label}', "
                    f"2-й этап='{_stage2_label}' — понижаем decision в neutral",
                    flush=True,
                )
                original_decision = decision
                decision = "neutral"
                needs_expert_review = True
                reason = (
                    f"Требует проверки эксперта: первый этап определил "
                    f"«{_stage1_label}», независимая проверка — "
                    f"«{_stage2_label}» ({verify_note})"
                )

        return {
            "decision": decision,
            "quote":    quote,
            "reason":   reason,
            "decision_fields": decision_fields,
            "needs_expert_review": needs_expert_review,
            "original_decision": original_decision,
            "source_doc_idx": _source_doc_idx,
        }
    except Exception:
        # Fallback: пробуем угадать по ключевым словам
        text_lower = raw.lower()
        if "positive" in text_lower:
            decision = "positive"
        elif "negative" in text_lower:
            decision = "negative"
        else:
            decision = "neutral"
        decision, _was_fixed = _reconcile_decision_with_reason(decision, raw)
        return {"decision": decision, "quote": chunk_text[:150], "reason": raw[:100],
                "decision_fields": decision_fields, "source_doc_idx": None}


# =============================================================================
# Агрегация: 1 файл = 1 голос (по большинству чанков внутри файла)
# =============================================================================
def aggregate_by_file(classified_chunks: List[Dict]) -> Dict:
    """
    Группирует чанки по файлу и определяет решение каждого файла
    по большинству голосов среди его чанков.
    Возвращает:
      {
        "positive": [{"file": ..., "quote": ..., "reason": ..., "date": ..., ...}, ...],
        "negative": [...],
        "neutral":  [...],
        "total_files": int,
      }
    """
    from collections import defaultdict, Counter

    # Группировка по файлу
    by_file: Dict[str, List[Dict]] = defaultdict(list)
    for chunk in classified_chunks:
        fname = chunk.get("file") or "неизвестный файл"
        by_file[fname].append(chunk)

    result = {
        "positive": [], "negative": [], "neutral": [], "total_files": 0,
        # Сколько файлов попало в neutral именно из-за понижения второй
        # проверкой (а не потому что прецедент по существу нейтрален) —
        # ключевая диагностика при подозрении на завышенную строгость
        # верификатора.
        "verifier_downgraded_files": 0,
    }

    for fname, chunks in by_file.items():
        # Считаем голоса
        counter = Counter(c["decision"] for c in chunks)
        # Определяем победившее решение
        decision = counter.most_common(1)[0][0]

        # Берём лучшую цитату — от чанка с победившим решением.
        # Если среди чанков с этим decision есть понижённые верификатором
        # (needs_expert_review), предпочитаем такой чанк — эксперту нужно
        # видеть именно спорный случай, а не случайный "тихо нейтральный".
        candidates = [c for c in chunks if c["decision"] == decision]
        best_chunk = next(
            (c for c in candidates if c.get("needs_expert_review")), candidates[0]
        ) if candidates else chunks[0]

        _file_needs_review = any(c.get("needs_expert_review") for c in chunks)

        # Приложенные документы, на которые сослались чанки этого файла
        # (см. classify_chunk, поле "doc"). Берём только чанки с
        # победившим решением — ссылка из отброшенного меньшинства к
        # итоговому вердикту файла отношения не имеет. Сохраняем как
        # отсортированный список номеров: один прецедент вполне может
        # сопоставляться сразу с несколькими документами пользователя.
        _doc_refs = sorted({
            c["source_doc_idx"] for c in candidates
            if c.get("source_doc_idx")
        })

        file_record = {
            "file":         fname,
            "date":         best_chunk.get("date", ""),
            "sphere":       best_chunk.get("sphere", ""),
            "region":       best_chunk.get("region", ""),
            "year":         best_chunk.get("year", ""),
            "method":       best_chunk.get("method", ""),
            "organization": best_chunk.get("organization", ""),
            "section":      best_chunk.get("section", ""),
            "source":       best_chunk.get("source", ""),
            "quote":        best_chunk.get("quote", ""),
            "reason":       best_chunk.get("reason", ""),
            "decision_fields": best_chunk.get("decision_fields", {}),
            "needs_expert_review": _file_needs_review,
            "original_decision": best_chunk.get("original_decision"),
            "chunks_total": len(chunks),
            "chunks_decision": dict(counter),
            "source_doc_idx":  best_chunk.get("source_doc_idx"),
            "source_doc_refs": _doc_refs,
        }
        result[decision].append(file_record)
        result["total_files"] += 1
        if decision == "neutral" and _file_needs_review:
            result["verifier_downgraded_files"] += 1

    return result


# =============================================================================
# Итоговое резюме: НПА (по сфере) + практика регуляторов
# =============================================================================
#
# ВАЖНО: сферы экспертных заключений (см. streamlit_pages/expertise_panel.py
# SPHERES: "Теплоснабжение", "Водоснабжение", "Водоотведение", "ТКО",
# "Электроэнергетика", "Газоснабжение", "Иное") и сферы НПА (metadata
# tariff_docs, назначаются в Админке через config/doc_spheres.json —
# ЭМОДЗИ-ПРЕФИКСНЫЕ комбинированные значения вида "🔥 Теплоснабжение",
# "💧 Водоснабжение/водоотведение", см. core.indexer._get_sphere_str и
# streamlit_pages/advisor_page.py _ADV_SPHERES) — ДВЕ РАЗНЫЕ, независимо
# развивавшиеся системы обозначений одной предметной области. Простой
# передачей выбранной сферы Прогнозиста напрямую в search_vector_db()
# ничего не найдётся почти никогда (см. core.advisor._sphere_match —
# подстрочное сравнение, а строки в принципе разные). Поэтому перед
# поиском по НПА сферу нужно явно перевести в словарь Советчика.
_EXPERTISE_TO_ADVISOR_SPHERE = {
    "Теплоснабжение":               "🔥 Теплоснабжение",
    "Водоснабжение/водоотведение":  "💧 Водоснабжение/водоотведение",
    "ТКО":                          "🗑️ Обращение с ТКО",
    "Электроэнергетика":            "⚡ Электрика",
    "Газоснабжение":                "🔵 Газ",
    "Иное":                         "📁 Иные сферы",
}


def _to_advisor_spheres(expertise_spheres: Optional[List[str]]) -> Optional[List[str]]:
    """Переводит выбранные сферы Прогнозиста (канонические, см. выше) в
    словарь сфер Советчика/НПА — для поиска применимых НПА по той же сфере.
    Значения без соответствия (не должно случаться при штатном списке
    _EXPERTISE_TO_ADVISOR_SPHERE, но на всякий случай) отбрасываются, а не
    ломают поиск."""
    if not expertise_spheres:
        return None
    mapped = [_EXPERTISE_TO_ADVISOR_SPHERE[s] for s in expertise_spheres
              if s in _EXPERTISE_TO_ADVISOR_SPHERE]
    return mapped or None


def fetch_npa_context(article_name: str, justification_summary: str,
                      spheres: Optional[List[str]] = None,
                      top_k: int = None) -> List[Dict]:
    """
    Ищет применимые НПА по статье затрат (и обоснованию), уточняя по
    выбранной сфере регулирования — через ту же гибридную инфраструктуру,
    что использует Советчик (core.advisor.search_vector_db: BM25 + вектор +
    CrossEncoder reranking). Отдельного диалога с Советчиком не открываем —
    берём его поисковый слой напрямую, это быстрее и не тянет за собой
    кэш/сессионные допущения диалогового режима.

    doc_types ограничены НПА-видами (npa/fas/methodics) — "court" (судебная
    практика) и "local" (закрытая база сегмента) сюда намеренно не входят:
    итоговое резюме прогноза не должно зависеть от того, что видно только
    одному сегменту.

    Мягкий отказ: при любой ошибке (Советчик/база недоступны) возвращает
    [] — резюме в этом случае строится только по найденной практике
    регуляторов, с явной пометкой об отсутствии НПА (см.
    _format_npa_context).
    """
    if top_k is None:
        top_k = int(load_predictor_config().get("summary_npa_top_k", 6))

    _query_chars = 400
    query = article_name
    if justification_summary:
        query = f"{article_name} {justification_summary[:_query_chars]}"

    try:
        from core.advisor import search_vector_db
        sources = search_vector_db(
            query, top_k=top_k, spheres=spheres or None,
            doc_types=["npa", "fas", "methodics"],
        )
        return sources or []
    except Exception as e:
        print(f"[PREDICTOR SUMMARY] Поиск по НПА не удался: {e}")
        return []


def _format_npa_context(npa_sources: List[Dict], max_chars: int = 6000) -> str:
    """Форматирует найденные НПА-источники в текстовый блок для промпта резюме."""
    if not npa_sources:
        return "(по данной статье и выбранной сфере в базе НПА ничего не найдено)"
    parts = []
    budget = max_chars
    for i, src in enumerate(npa_sources, 1):
        art     = f", п. {src['article']}" if src.get("article") else ""
        header  = f"[{i}] {src.get('file', 'Неизвестно')}{art}:\n"
        snippet = src.get("snippet", "")
        available = budget - len(header)
        if available <= 100:
            break
        if len(snippet) > available:
            snippet = snippet[:available] + "…"
        parts.append(header + snippet)
        budget -= len(header) + len(snippet)
        if budget <= 0:
            break
    return "\n\n---\n\n".join(parts) if parts else "(по данной статье и выбранной сфере в базе НПА ничего не найдено)"


def _format_expertise_context(agg: Dict, max_per_side: int = 6, max_chars: int = 6000) -> str:
    """
    Форматирует уже найденную практику (positive/negative из aggregate_by_file,
    топ max_per_side с каждой стороны) в текстовый блок для промпта резюме.
    Нейтральные источники не включаются — как и в compute_approval_score, они
    не содержат решения регулятора по заявленной пользователем логике и
    только размыли бы резюме.
    """
    lines = []
    budget = max_chars
    for label, key in (("ЗА", "positive"), ("ПРОТИВ", "negative")):
        all_records = agg.get(key, [])
        if not all_records:
            continue
        lines.append(f"— {label} ({len(all_records)} источник(ов) всего, показаны релевантные):")
        for rec in all_records[:max_per_side]:
            quote  = (rec.get("quote") or "").strip()
            reason = (rec.get("reason") or "").strip()
            entry  = f"  · {rec.get('file', '—')}: {quote}"
            if reason:
                entry += f" ({reason})"
            if budget - len(entry) <= 0:
                break
            lines.append(entry)
            budget -= len(entry)
    if not lines:
        return "(источников практики «за» или «против» не найдено — только нейтральные упоминания либо ничего)"
    return "\n".join(lines)


def generate_prediction_summary(
    article_name: str,
    justification_summary: str,
    agg: Dict,
    npa_sources: List[Dict],
    client,
    model: str,
) -> Dict:
    """
    Формирует краткое итоговое резюме прогноза, опирающееся ОДНОВРЕМЕННО на
    применимые НПА (fetch_npa_context, уже отфильтрованные по выбранной
    сфере) и на практику регуляторов (агрегированные за/против из уже
    классифицированных прецедентов). Сбой LLM здесь не должен ронять уже
    посчитанный прогноз — см. вызов в run_prediction.

    Возвращает {"text": str} при успехе или {"text": "", "error": str} при
    сбое.
    """
    cfg          = load_predictor_config()
    prompts      = load_predictor_prompts()
    max_tokens   = int(cfg.get("summary_max_tokens", 900))
    max_per_side = int(cfg.get("summary_max_sources", 6))

    npa_context        = _format_npa_context(npa_sources)
    expertise_context   = _format_expertise_context(agg, max_per_side=max_per_side)

    n_positive = len(agg.get("positive", []))
    n_negative = len(agg.get("negative", []))
    n_neutral  = len(agg.get("neutral", []))

    _justify      = (justification_summary or "")[:600]
    _justify_line = f"Обоснование пользователя: {_justify}\n" if _justify else ""

    system_prompt = prompts["predictor_summary_system"]
    user_prompt = (
        prompts["predictor_summary_user"]
        .replace("{article_name}",       article_name)
        .replace("{justification_line}", _justify_line)
        .replace("{npa_context}",        npa_context)
        .replace("{n_positive}",         str(n_positive))
        .replace("{n_negative}",         str(n_negative))
        .replace("{n_neutral}",          str(n_neutral))
        .replace("{expertise_context}",  expertise_context)
    )

    raw = _lm_call(client, model, system_prompt, user_prompt, max_tokens=max_tokens)
    if raw.startswith("[Ошибка LM:"):
        return {"text": "", "error": raw}
    return {"text": raw.strip()}


# =============================================================================
# Основная функция прогноза
# =============================================================================
def run_prediction(
    article_name: str,
    justification_text: str,
    top_k: int = None,
    filters: Optional[Dict] = None,
    sources: Optional[List[str]] = None,
    _progress_cb=None,
    force_verification: Optional[bool] = None,
    with_summary: bool = True,
    docs: Optional[List[Dict]] = None,
) -> Optional[Dict]:
    """
    Запускает полный цикл прогноза. Возвращает dict с результатами или None при ошибке.
    `sources` — список из {"protocols", "expertise"}; по умолчанию ["expertise"].
    `force_verification` — переключатель второй ("верификационной") ступени
    классификации; см. classify_chunk. None — берётся из конфига.
    `with_summary` — строить ли итоговое резюме (НПА + практика) после
    классификации; см. generate_prediction_summary. Управляется чекбоксом в
    интерфейсе и общим выключателем "enable_summary" в конфиге (оба должны
    разрешать резюме).
    `docs` — приложенные документы-обоснования (см. validate_and_read_upload).
    Их текст добавляется к обоснованию с маркировкой источника, а каждый
    вердикт получает привязку к конкретному документу (source_doc_idx).
    При docs=None поведение полностью совпадает с прежним — сценарий
    «только текстом» не затронут.
    """
    if not article_name.strip():
        return None

    # ── Приложенные документы ────────────────────────────────────────────
    # Непригодные (не прошедшие валидацию) сюда не попадают — интерфейс
    # показывает их отдельно с причиной отказа, но в прогноз не отдаёт.
    _docs        = ok_docs(docs)
    _doc_labels  = build_doc_labels(_docs)
    _docs_context = ""
    # Текстовое описание пользователя ДО подмешивания документов — именно оно
    # (а не текст документов) идёт в поисковый запрос ниже. Без отдельной
    # переменной в запрос утекал весь контекст документов целиком.
    _user_text   = (justification_text or "").strip()
    if _docs:
        if _progress_cb:
            _progress_cb(0.02, f"Подготовка приложенных документов ({len(_docs)} шт.)…")
        _docs_context = build_docs_context(
            _docs, article_name,
            _progress_cb=lambda p, m: _progress_cb(0.02 + p * 0.06, m) if _progress_cb else None,
        )
        justification_text = (
            f"{justification_text}\n\n{_docs_context}".strip()
            if (justification_text or "").strip() else _docs_context
        )

    if sources is None:
        sources = ["expertise"]

    # Читаем top_k из конфига если не передан явно
    if top_k is None:
        top_k = int(load_predictor_config().get("default_top_k", _DEFAULT_TOP_K))

    # 1. Формируем поисковый запрос.
    # ИСПРАВЛЕНО: раньше при длинном обосновании (>500 симв.) article_name
    # полностью выбрасывался из запроса, заменяясь только синонимами от
    # expand_query — поиск уходил в сторону от реальной статьи затрат.
    # Теперь article_name участвует в запросе всегда; для длинного
    # обоснования берём только начальный фрагмент (для контекста), не весь
    # текст целиком — он и так дальше используется отдельно при
    # классификации каждого чанка (justification_summary).
    if _progress_cb:
        _progress_cb(0.05, "Формирование поискового запроса…")
    _JUSTIFICATION_QUERY_CHARS = 400
    if _docs:
        # С приложенными документами в запрос идёт НЕ начало их текста —
        # первая страница регуляторного документа это обычно шапка с
        # реквизитами, по которой поиск уходит в сторону. Берём
        # верхнеуровневые описания документов (о чём они) плюс текстовое
        # описание пользователя, если оно есть.
        _heads = " ".join(
            (d.get("head_summary") or "").strip() for d in _docs
        ).strip()
        _query_tail = " ".join(x for x in (_heads, _user_text) if x)
        search_query = f"{article_name} {_query_tail[:_JUSTIFICATION_QUERY_CHARS]}".strip()
    elif justification_text:
        search_query = f"{article_name} {justification_text[:_JUSTIFICATION_QUERY_CHARS]}"
    else:
        search_query = article_name

    # 2. Сжимаем обоснование если длинное.
    # С приложенными документами глобальное сжатие НЕ применяется: текст уже
    # ограничен бюджетом doc_full_text_budget в build_docs_context, а проход
    # через compress_document уничтожил бы маркеры «=== ДОКУМЕНТ N: … ===»,
    # без которых невозможна привязка вердикта к документу.
    justification_summary = justification_text
    if (not _docs) and justification_text and len(justification_text) > _LARGE_DOC_THRESHOLD:
        if _progress_cb:
            _progress_cb(0.1, "Сжатие документа-обоснования…")
        justification_summary = compress_document(
            justification_text, article_name,
            _progress_cb=lambda p, m: _progress_cb(0.1 + p * 0.2, m) if _progress_cb else None,
        )

    # 3. Поиск
    # Для expertise — гибридный поиск (BM25 + векторный + CrossEncoder
    # reranking относительно article_name), та же инфраструктура, что и в
    # Советчике (core/advisor.py). Заметно точнее на коротких запросах
    # (название статьи затрат), чем чистый векторный cosine similarity.
    # Для protocols (или их комбинации с expertise) — оставляем обычный
    # векторный поиск через search_documents, т.к. protocols пока не имеет
    # отдельного BM25-индекса.
    if _progress_cb:
        _src_label = " + ".join(sources)
        _progress_cb(0.32, f"Поиск по базе ({_src_label}, top-{top_k})…")

    if sources == ["expertise"]:
        chunks = search_documents_hybrid(
            search_query, article_name=article_name, top_k=top_k, filters=filters,
        )
    else:
        chunks = search_documents(search_query, top_k=top_k, filters=filters, sources=sources)

    if not chunks:
        return {
            "article":    article_name,
            "query":      search_query,
            "chunks":     [],
            "aggregated": {"positive": [], "negative": [], "neutral": [], "total_files": 0},
            "error":      "Документы не найдены. Проверьте, загружена ли коллекция в Админке, и не слишком ли узкие фильтры.",
        }

    # 3.5. Обрезаем по общему бюджету символов — защита от слишком
    # длинной последовательной классификации (особенно при включённом
    # Unified KV Cache в LM Studio, который накапливает контекст между
    # вызовами и резко замедляет генерацию на больших top_k).
    chunks, dropped_count = _truncate_chunks_by_char_budget(chunks, _RAG_CONTEXT_CHAR_BUDGET)
    if dropped_count and _progress_cb:
        _progress_cb(
            0.35,
            f"Контекст обрезан до {_RAG_CONTEXT_CHAR_BUDGET:,} симв. "
            f"(отброшено {dropped_count} наименее релевантных фрагментов)…".replace(",", " "),
        )

    # 4. Классификация чанков через LLM
    if _progress_cb:
        _progress_cb(0.40, f"Классификация {len(chunks)} фрагментов…")
    try:
        from openai import OpenAI
        lm_url, model = _load_lm_config()
        client = OpenAI(base_url=lm_url, api_key="lm-studio", timeout=180.0)
    except Exception as e:
        return {"error": f"LM Studio недоступен: {e}", "article": article_name}

    classified = []
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        if _progress_cb:
            pct = 0.40 + (i / total) * 0.50
            _progress_cb(pct, f"Классифицирую фрагмент {i + 1} / {total}…")
        classification = classify_chunk(
            chunk["text"], article_name, justification_summary, client, model,
            force_verification=force_verification,
            doc_labels=_doc_labels,
        )
        classified.append({**chunk, **classification})

    # 5. Агрегация по файлам
    if _progress_cb:
        _progress_cb(0.92, "Агрегация результатов…")
    aggregated = aggregate_by_file(classified)

    # 6. Итоговое резюме (НПА по выбранной сфере + практика) — необязательный
    # шаг, сбой здесь не должен ронять уже посчитанный прогноз (см.
    # generate_prediction_summary — при ошибке LLM возвращает {"error": ...},
    # а не бросает исключение).
    npa_sources    = []
    summary_result = None
    _summary_cfg_on = bool(load_predictor_config().get("enable_summary", True))
    if with_summary and _summary_cfg_on:
        try:
            if _progress_cb:
                _progress_cb(0.94, "Поиск применимых НПА по выбранной сфере…")
            _summary_spheres = _to_advisor_spheres((filters or {}).get("spheres"))
            npa_sources = fetch_npa_context(
                article_name, justification_summary, spheres=_summary_spheres,
            )
            if _progress_cb:
                _progress_cb(0.97, "Формирование краткого резюме…")
            summary_result = generate_prediction_summary(
                article_name, justification_summary, aggregated, npa_sources,
                client, model,
            )
        except Exception as e:
            summary_result = {"text": "", "error": f"[Резюме не сформировано: {e}]"}

    return {
        "article":              article_name,
        "query":                search_query,
        "justification_summary": justification_summary,
        "chunks_raw":           len(chunks),
        "chunks_dropped_budget": dropped_count,
        "aggregated":           aggregated,
        "timestamp":            datetime.now().isoformat(),
        "top_k":                top_k,
        "filters":              filters or {},
        "sources":              sources,
        "npa_sources":          npa_sources,
        "summary":              summary_result,
        # Приложенные документы — без текста (он уже в justification_summary):
        # результат прогноза кладётся в session_state, и таскать в нём
        # полные тексты всех документов незачем.
        "user_docs": [
            {
                "idx":          i,
                "filename":     d.get("filename", ""),
                "head_summary": d.get("head_summary", ""),
                "chars":        len(d.get("full_text", "") or ""),
                "pages":        len(d.get("pages", []) or []),
                "source":       d.get("source", "upload"),
                # Немашиночитаемые документы прослеживаются до самого
                # результата: эксперт должен видеть, что вердикт опирается
                # на распознанный, а не на исходный текст.
                "ocr_pages":        d.get("ocr_pages", 0),
                "ocr_quality_ok":   d.get("ocr_quality_ok", True),
                "ocr_quality_note": d.get("ocr_quality_note", ""),
            }
            for i, d in enumerate(_docs, 1)
        ],
    }


# =============================================================================
# UI — счётчик-бейдж (цветной)
# =============================================================================
def _badge(label: str, count: int, color: str) -> str:
    return (
        f"<span style='display:inline-block;padding:3px 12px;border-radius:12px;"
        f"background:{color};color:#fff;font-weight:600;font-size:0.9rem;margin-right:6px'>"
        f"{label}: {count}</span>"
    )


_NEGATIVE_WEIGHT = 1.5  # negative весит сильнее positive — принцип осторожности
_HIGH_CONFIDENCE_THRESHOLD = 5  # содержательных источников (positive+negative) для "высокой" уверенности


def compute_approval_score(n_positive: int, n_negative: int, n_neutral: int) -> Dict:
    """
    Взвешенная агрегирующая оценка вероятности одобрения статьи затрат.

    Методология:
    - Учитываются только содержательные источники (positive + negative);
      neutral в сам процент не входит — они ничего не говорят по существу
      о позиции регулятора в отношении конкретной заявленной логики.
    - negative весит в _NEGATIVE_WEIGHT раз сильнее positive (принцип
      осторожности: ошибочно успокоить заявителя дороже, чем ошибочно
      насторожить — отказ в тарифной заявке создаёт больше риска для
      бизнеса, чем избыточная осторожность).
    - Если содержательных источников нет вообще (все найденные —
      neutral) — возвращается 50% с явной пометкой "низкая уверенность":
      это означает, что регуляторы, по всей видимости, ещё не
      сталкивались именно с такой комбинацией статьи и обоснования,
      а не что у организации есть основания на одобрение или отказ.
    - Уверенность (confidence) считается по числу содержательных
      источников: >= _HIGH_CONFIDENCE_THRESHOLD — высокая, 1..4 —
      средняя, 0 — низкая.
    """
    weighted_pos = n_positive
    weighted_neg = n_negative * _NEGATIVE_WEIGHT
    total_weighted = weighted_pos + weighted_neg
    n_substantive = n_positive + n_negative

    if total_weighted <= 0:
        approval_pct = 50.0
    else:
        approval_pct = (weighted_pos / total_weighted) * 100

    if n_substantive >= _HIGH_CONFIDENCE_THRESHOLD:
        confidence = "high"
    elif n_substantive >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "approval_pct": round(approval_pct, 1),
        "confidence": confidence,
        "n_substantive": n_substantive,
        "n_positive": n_positive,
        "n_negative": n_negative,
        "n_neutral": n_neutral,
        "all_neutral": n_substantive == 0 and n_neutral > 0,
    }


def _source_card(record: Dict, idx: int, decision: str, article: str = "",
                 user_docs: Optional[List[Dict]] = None) -> None:
    """Отображает одну карточку-источник в свёрнутом виде (как в советчике).

    `user_docs` — приложенные пользователем документы (result["user_docs"]).
    Если передан, в карточке показывается, ИЗ КАКОГО именно документа взята
    позиция, которую модель сопоставляла с этим прецедентом (см.
    classify_chunk, поле "doc" → source_doc_idx).
    """
    color_map = {"positive": "#2e7a50", "negative": "#b33a3a", "neutral": "#888"}
    border_color = color_map.get(decision, "#888")

    header = record.get("file", "неизвестный файл")
    if record.get("date"):
        header += f"  ·  {record['date']}"
    if record.get("organization"):
        header += f"  ·  {record['organization']}"
    if record.get("source"):
        _src_badge = "экспертное" if record["source"] == "expertise" else "протокол"
        header += f"  ·  {_src_badge}"

    with st.expander(header, expanded=False):
        # Просмотр исходного txt-файла источника (скачивание — внутри диалога)
        _fname = record.get("file", "")
        _doc_type = "expertise" if record.get("source") == "expertise" else "protocol"
        _src_fpath = os.path.join("data", "raw", f"{_doc_type}_docs", _fname)
        if _fname and os.path.exists(_src_fpath):
            try:
                with open(_src_fpath, "rb") as _f:
                    _file_bytes = _f.read()
                _file_text = _file_bytes.decode("utf-8", errors="replace")

                if st.button(
                    "Просмотреть",
                    key=f"preview_{decision}_{idx}_{_fname}",
                ):
                    st.session_state["_preview_file"] = {
                        "name": _fname, "text": _file_text, "bytes": _file_bytes,
                    }
                    _log_usage("predictor", "source_viewed", meta={
                        "file":     _fname,
                        "decision": decision,
                    })
                    st.rerun()
            except Exception:
                pass
            st.markdown("")

        # Цитата
        quote = record.get("quote", "")
        if quote:
            st.markdown(
                f"<div style='border-left:3px solid {border_color};"
                f"padding:8px 12px;background:#f8f9fa;"
                f"border-radius:0 6px 6px 0;font-style:italic;"
                f"font-size:0.88rem;margin-bottom:8px;'>"
                f"{quote}</div>",
                unsafe_allow_html=True,
            )
        # Причина (от LLM-классификатора — почему отнесён к за/против/нейтрально)
        if record.get("reason"):
            st.caption(f"Оценка системы: {record['reason']}")

        # ── Привязка к приложенному документу ────────────────────────────
        # Показывает, текст какого именно документа пользователя послужил
        # основанием для сопоставления с этим прецедентом. Отсутствие
        # привязки — штатная ситуация (позиция взята из текстового описания
        # либо модель не смогла определить источник), поэтому при пустом
        # source_doc_refs просто ничего не показываем.
        if user_docs:
            _by_idx = {d.get("idx"): d for d in user_docs}
            _refs   = record.get("source_doc_refs") or (
                [record["source_doc_idx"]] if record.get("source_doc_idx") else []
            )
            _names = [
                _by_idx[r].get("filename", "")
                for r in _refs if r in _by_idx
            ]
            if _names:
                st.caption("Сопоставлено с вашим документом: " + "  ·  ".join(_names))

        # ── Ручная коррекция эксперта — доступна для ЛЮБОЙ категории ────────
        # Раньше кнопки "за/против" показывались только для нейтральных
        # источников. Эксперт может ошибочно доверять ИИ и там, где ИИ сам
        # ошибся на "за" или "против" — поэтому коррекция теперь доступна
        # везде, всегда в виде трёх кнопок (за / против / нейтрально), а
        # текущий активный вариант подсвечен. Каждое изменение — включая
        # исходное решение эксперта поправить ИИ и возврат к вердикту ИИ —
        # регистрируется в постоянном логе (data/predictor/expert_overrides.jsonl)
        # через _log_expert_override, независимо от session_state.
        _fkey = record.get("file", "")
        _ai_decision = record.get("_ai_decision", decision)
        _overrides = st.session_state.setdefault("pred_expert_overrides", {})
        _current_final = _overrides.get(_fkey, _ai_decision)  # то, что показано сейчас (decision параметр)

        if record.get("needs_expert_review") and _ai_decision == "neutral":
            _orig = record.get("original_decision")
            _orig_label = _DECISION_RU.get(_orig, "")
            if _orig_label:
                st.caption(
                    f"Первый этап определил это как «{_orig_label}», но вторая "
                    f"проверка нашла несовпадение позиций и понизила до "
                    f"нейтрального — уточните вручную при необходимости:"
                )
            else:
                st.caption("Модель не смогла однозначно определить позицию — при необходимости уточните вручную:")
        else:
            st.caption(
                f"ИИ определил как «{_DECISION_RU.get(_ai_decision, _ai_decision)}» — "
                f"при необходимости исправьте вручную:"
            )

        ec1, ec2, ec3, ec4 = st.columns(4)
        _btn_specs = [
            (ec1, "За",        "positive", "override_pos_"),
            (ec2, "Против",    "negative", "override_neg_"),
            (ec3, "Нейтрально", "neutral",  "override_neu_"),
        ]
        for _col, _label, _value, _key_prefix in _btn_specs:
            with _col:
                if st.button(
                    _label, key=f"{_key_prefix}{idx}_{_fkey}",
                    type="primary" if _current_final == _value else "secondary",
                    use_container_width=True,
                ):
                    if _value != _current_final:
                        _log_expert_override(
                            article, _fkey, _ai_decision, _current_final, _value, quote,
                        )
                        _log_usage("predictor", "expert_override", meta={
                            "article":       article[:80],
                            "ai_decision":   _ai_decision,
                            "from_decision": _current_final,
                            "to_decision":   _value,
                        })
                    if _value == _ai_decision:
                        _overrides.pop(_fkey, None)  # совпало с ИИ — override не нужен
                    else:
                        _overrides[_fkey] = _value
                    st.rerun()
        with ec4:
            if _fkey in _overrides and st.button(
                "Сбросить", key=f"override_reset_{idx}_{_fkey}",
                use_container_width=True,
            ):
                _log_expert_override(article, _fkey, _ai_decision, _current_final, _ai_decision, quote)
                _overrides.pop(_fkey, None)
                st.rerun()

        if _fkey in _overrides:
            st.caption(
                f"✓ Исправлено вручную: ИИ определил «{_DECISION_RU.get(_ai_decision, _ai_decision)}», "
                f"эксперт — «{_DECISION_RU.get(_current_final, _current_final)}»"
            )

        # Метаданные
        meta_parts = []
        if record.get("sphere"):
            meta_parts.append(f"Сфера: {record['sphere']}")
        if record.get("region"):
            meta_parts.append(f"Регион: {record['region']}")
        if record.get("year"):
            meta_parts.append(f"Год: {record['year']}")
        if record.get("method"):
            meta_parts.append(f"Метод: {record['method']}")
        if record.get("section"):
            meta_parts.append(f"Раздел: {record['section']}")
        chunks_info = record.get("chunks_decision", {})
        if chunks_info:
            parts_str = " / ".join(
                f"{k}: {v}" for k, v in chunks_info.items()
            )
            meta_parts.append(f"Фрагментов ({parts_str})")
        if meta_parts:
            st.caption("  ·  ".join(meta_parts))


# =============================================================================
# Страница реестра
# =============================================================================
def _show_registry():
    st.subheader("Реестр прогнозов")

    records = load_registry(max_records=500)
    if not records:
        st.info("Реестр пуст — запустите первый прогноз.")
        return

    # Фильтры
    col1, col2, col3 = st.columns(3)
    with col1:
        filter_article = st.text_input("Фильтр по статье", key="reg_filter_article")
    with col2:
        filter_org = st.text_input("Фильтр по организации", key="reg_filter_org")
    with col3:
        filter_date = st.text_input("Фильтр по дате (ГГГГ-ММ)", key="reg_filter_date")

    # Применяем фильтры
    filtered = records
    if filter_article.strip():
        filtered = [r for r in filtered if filter_article.lower() in r.get("article", "").lower()]
    if filter_org.strip():
        filtered = [
            r for r in filtered
            if filter_org.lower() in json.dumps(r.get("sources", r.get("aggregated", {})), ensure_ascii=False).lower()
        ]
    if filter_date.strip():
        filtered = [r for r in filtered if r.get("timestamp", "").startswith(filter_date)]

    st.caption(f"Показано: {len(filtered)} из {len(records)}")
    st.divider()

    # Пагинация (по 20 записей)
    page_size = 20
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page_num = st.number_input("Страница", min_value=1, max_value=total_pages,
                                value=1, key="reg_page")
    start = (page_num - 1) * page_size
    page_records = filtered[start: start + page_size]

    for rec in page_records:
        agg = rec.get("aggregated_summary", rec.get("aggregated", {}))
        # aggregated_summary хранит целые числа; aggregated — legacy списки
        pos = agg.get("positive", 0) if isinstance(agg.get("positive", 0), int) else len(agg.get("positive", []))
        neg = agg.get("negative", 0) if isinstance(agg.get("negative", 0), int) else len(agg.get("negative", []))
        neu = agg.get("neutral",  0) if isinstance(agg.get("neutral",  0), int) else len(agg.get("neutral",  []))

        ts  = rec.get("timestamp", "")[:16].replace("T", " ")
        lbl = f"{ts}  ·  {rec.get('article', '—')}"

        with st.expander(lbl, expanded=False):
            st.markdown(
                _badge("За", pos, "#2e7a50")
                + _badge("Против", neg, "#b33a3a")
                + _badge("Нейтр.", neu, "#888"),
                unsafe_allow_html=True,
            )
            st.caption(f"Запрос: {rec.get('query', '—')[:120]}")
            if rec.get("filters"):
                st.caption(f"Фильтры: {rec['filters']}")


# =============================================================================
# Главная страница прогнозиста
# =============================================================================
def show_predictor():
    st.header("Прогноз решения регулятора")
    st.info(
        "Введите статью затрат и обоснование — текстом и/или приложенными "
        "документами. Система прочитает приложенное, найдёт аналогичные случаи "
        "в протоколах и экспертных заключениях регуляторов, оценит вероятность "
        "одобрения и покажет, какой из ваших документов с чем сопоставлен."
    )

    # ── session_state ────────────────────────────────────────────────────────
    for key, val in [
        ("pred_result",       None),
        ("pred_running",      False),
        ("pred_doc_text",     ""),
        ("pred_docs",         []),   # приложенные документы текущего прогноза
    ]:
        if key not in st.session_state:
            st.session_state[key] = val

    # ── Вкладки ──────────────────────────────────────────────────────────────
    tab_predict, tab_registry = st.tabs(["Прогноз", "Реестр прогнозов"])

    with tab_predict:
        _show_predict_tab()

    with tab_registry:
        _show_registry()


# =============================================================================
# Вкладка «Прогноз»
# =============================================================================
def _show_file_preview_dialog():
    """
    Показывает содержимое исходного txt-файла во всплывающем окне, если
    пользователь нажал «Просмотреть» на одной из карточек источников.

    Поиск реализован как самодостаточный HTML/JS-компонент (через
    st.components.v1.html): JS сам подсвечивает совпадения и прокручивает
    к активному при нажатии «Далее»/«Назад» — это единственный способ
    физически проскроллить к найденному фрагменту, чистый Streamlit
    скроллом управлять не может. Сам текст экранируется от HTML-инъекций
    перед вставкой (документы пользовательские, могут случайно содержать
    символы вроде "<").

    ВАЖНО — ФИКС "МОДАЛКА САМА ОТКРЫВАЕТСЯ СНОВА": раньше _preview_file
    оставался в session_state до нажатия именно НАШЕЙ кнопки «Закрыть».
    Но встроенный крестик «×» в углу st.dialog закрывает окно чисто
    визуально, БЕЗ выполнения нашего Python-кода — _preview_file
    оставался установленным. На следующем любом действии на странице
    (клик по фильтру, разворачивание другой карточки и т.п.) скрипт
    перезапускался, видел, что _preview_file всё ещё стоит, и Streamlit
    открывал диалог заново "с нуля" — то самое неожиданное повторное
    появление модалки.

    Фикс: используем _preview_file как ОДНОРАЗОВЫЕ данные — забираем их
    (pop) из session_state СРАЗУ при показе, а не при закрытии. Тогда на
    любом следующем перезапуске скрипта (по любой причине, включая
    закрытие через «×») флага уже не будет, и модалка не появится снова
    сама по себе. Побочный эффект: клик по «Скачать» внутри диалога тоже
    закроет модалку (это вызывает свой rerun) — приемлемый компромисс
    ради того, чтобы модалка больше не всплывала непредсказуемо.
    """
    preview = st.session_state.pop("_preview_file", None)
    if not preview:
        return

    @st.dialog(preview["name"], width="large")
    def _dialog():
        import html as _html
        import streamlit.components.v1 as components

        full_text = preview["text"]
        escaped_text = _html.escape(full_text)
        text_for_js = json.dumps(escaped_text.replace("\n", "<br>"))

        html_block = f"""
        <div style="font-family: -apple-system, sans-serif;">
          <div style="display:flex; gap:8px; margin-bottom:8px; align-items:center;">
            <input id="pv-search" type="text" placeholder="Поиск по тексту…"
                   style="flex:1; padding:8px 10px; border:1px solid #ccc;
                          border-radius:6px; font-size:0.9rem;" />
            <button id="pv-prev" style="padding:8px 12px; border:1px solid #ccc;
                    border-radius:6px; background:#f5f5f5; cursor:pointer;">‹ Назад</button>
            <button id="pv-next" style="padding:8px 12px; border:1px solid #ccc;
                    border-radius:6px; background:#f5f5f5; cursor:pointer;">Далее ›</button>
          </div>
          <div id="pv-count" style="color:#666; font-size:0.82rem; margin-bottom:6px;"></div>
          <div id="pv-content" style="height:460px; overflow-y:auto; padding:12px;
               border:1px solid #ddd; border-radius:6px; font-family:monospace;
               font-size:0.85rem; white-space:pre-wrap; line-height:1.5;"></div>
        </div>
        <script>
          const rawHtml = {text_for_js};
          const contentEl = document.getElementById('pv-content');
          const searchEl = document.getElementById('pv-search');
          const countEl = document.getElementById('pv-count');
          const prevBtn = document.getElementById('pv-prev');
          const nextBtn = document.getElementById('pv-next');

          contentEl.innerHTML = rawHtml;
          let matches = [];
          let activeIndex = -1;

          function escapeRegExp(s) {{
            return s.replace(/[.*+?^${{}}()|[\\]\\\\]/g, '\\\\$&');
          }}

          function runSearch() {{
            const query = searchEl.value.trim();
            contentEl.innerHTML = rawHtml;
            matches = [];
            activeIndex = -1;

            if (!query) {{
              countEl.textContent = '';
              return;
            }}

            const re = new RegExp(escapeRegExp(query), 'gi');
            const walker = document.createTreeWalker(contentEl, NodeFilter.SHOW_TEXT, null);
            const textNodes = [];
            let node;
            while (node = walker.nextNode()) {{ textNodes.push(node); }}

            textNodes.forEach(function(textNode) {{
              const text = textNode.nodeValue;
              let lastIndex = 0;
              let m;
              re.lastIndex = 0;
              const frag = document.createDocumentFragment();
              let found = false;
              while ((m = re.exec(text)) !== null) {{
                found = true;
                frag.appendChild(document.createTextNode(text.slice(lastIndex, m.index)));
                const mark = document.createElement('mark');
                mark.style.background = '#fff3a0';
                mark.textContent = m[0];
                frag.appendChild(mark);
                matches.push(mark);
                lastIndex = m.index + m[0].length;
                if (m.index === re.lastIndex) re.lastIndex++;
              }}
              if (found) {{
                frag.appendChild(document.createTextNode(text.slice(lastIndex)));
                textNode.parentNode.replaceChild(frag, textNode);
              }}
            }});

            countEl.textContent = matches.length
              ? ('Найдено совпадений: ' + matches.length)
              : 'Совпадений не найдено';

            if (matches.length) {{
              activeIndex = 0;
              highlightActive();
            }}
          }}

          function highlightActive() {{
            matches.forEach(function(m, i) {{
              m.style.background = (i === activeIndex) ? '#ffa500' : '#fff3a0';
            }});
            if (matches[activeIndex]) {{
              matches[activeIndex].scrollIntoView({{ block: 'center', behavior: 'smooth' }});
              countEl.textContent = 'Совпадение ' + (activeIndex + 1) + ' из ' + matches.length;
            }}
          }}

          searchEl.addEventListener('input', runSearch);
          nextBtn.addEventListener('click', function() {{
            if (!matches.length) return;
            activeIndex = (activeIndex + 1) % matches.length;
            highlightActive();
          }});
          prevBtn.addEventListener('click', function() {{
            if (!matches.length) return;
            activeIndex = (activeIndex - 1 + matches.length) % matches.length;
            highlightActive();
          }});
        </script>
        """
        components.html(html_block, height=560, scrolling=False)

        dc1, dc2 = st.columns(2)
        with dc1:
            st.download_button(
                "Скачать",
                data=preview.get("bytes", full_text.encode("utf-8")),
                file_name=preview["name"],
                mime="text/plain",
                use_container_width=True,
            )
        with dc2:
            if st.button("Закрыть", use_container_width=True):
                st.rerun()

    _dialog()


def _show_predict_tab():
    _show_file_preview_dialog()

    # ── Шаг 1: Статья затрат ─────────────────────────────────────────────────
    st.subheader("1. Статья затрат")
    article_name = st.text_input(
        "Наименование статьи",
        placeholder="Например: Заработная плата, Амортизация, Расходы на ремонт ОС",
        key="pred_article",
    )

    # ── Шаг 2: Документы-обоснования ─────────────────────────────────────────
    # ОБЪЕДИНЁННЫЙ РЕЖИМ. Раньше здесь была радиокнопка «Текстом / Загрузить
    # файл / Из Сканера» — три взаимоисключающих способа, по одному файлу за
    # раз. Теперь текстовое описание, файлы с рабочей машины и документы из
    # базы Сканера можно сочетать в одном прогнозе, а приложить — сразу
    # несколько. Документы живут только в рамках текущего прогноза
    # (st.session_state["pred_docs"]) и в базу Сканера не записываются.
    st.subheader("2. Документы-обоснования")
    st.caption(
        "Опишите обоснование текстом и/или приложите документы — с рабочей "
        "машины или из базы Сканера. Способы можно сочетать: в прогноз "
        "уйдёт всё приложенное вместе."
    )

    _cfg_ui   = load_predictor_config()
    _max_docs = int(_cfg_ui.get("max_docs", 10))
    _max_mb   = int(_cfg_ui.get("max_upload_mb", 50))

    docs: List[Dict] = st.session_state.setdefault("pred_docs", [])

    # ── Состояние распознавания немашиночитаемых документов ──────────────
    # Показываем ДО прикрепления: если OCR не поднят, скан не прочитается,
    # и лучше сказать об этом сразу, чем после долгой обработки файла.
    # Инициализация идёт под тем же ключом session_state, что и в Сканере,
    # поэтому модель в VRAM не грузится повторно.
    _ocr_state = ensure_ocr_ready()
    if not _ocr_state.get("available"):
        st.warning(
            "Распознавание текста (OCR) недоступно"
            + (f": {_ocr_state['error']}" if _ocr_state.get("error") else "")
            + ". Документы с текстовым слоем (PDF, DOCX, XLSX, TXT) читаются "
              "как обычно, а сканы и фотографии приложить не получится."
        )
    elif _ocr_state.get("engine") == "tesseract":
        st.caption(
            "⚠️ Основной модуль распознавания недоступен, работает резервный "
            "(Tesseract) — качество распознавания сканов будет ниже."
        )

    justification_text = st.text_area(
        "Описание обоснования (опционально, если приложены документы)",
        height=140,
        placeholder=(
            "Опишите суть обоснования: какие нормативы применялись, "
            "какие расчёты выполнены, на какие документы опираетесь..."
        ),
        key="pred_justification_text",
    )

    # ── Прикрепление с рабочей машины ────────────────────────────────────
    _uploaded = st.file_uploader(
        f"Приложить документы с рабочей машины "
        f"(до {_max_docs} шт., до {_max_mb} МБ каждый)",
        type=[e.lstrip(".") for e in _SUPPORTED_EXTS],
        accept_multiple_files=True,
        key="pred_upload",
        help=(
            "PDF (в том числе сканы — распознаются через OCR), DOCX, DOC, "
            "XLSX, TXT, изображения. Каждый документ читается один раз при "
            "прикреплении, повторно при пересчёте прогноза не разбирается."
        ),
    )

    # Синхронизация с виджетом: файл, убранный пользователем из загрузчика,
    # убираем и из списка. Документы из Сканера виджету не принадлежат —
    # их эта синхронизация не трогает.
    _widget_sigs = {_doc_sig(f.name, f.size) for f in (_uploaded or [])}
    _kept = [
        d for d in docs
        if d.get("source") != "upload" or d.get("sig") in _widget_sigs
    ]
    if len(_kept) != len(docs):
        docs[:] = _kept

    _known_sigs = {d.get("sig") for d in docs}
    _new_files  = [
        f for f in (_uploaded or [])
        if _doc_sig(f.name, f.size) not in _known_sigs
    ]

    if _new_files:
        _free = max(0, _max_docs - len(docs))
        if len(_new_files) > _free:
            st.warning(
                f"Приложить можно не больше {_max_docs} документов — "
                f"лишние файлы пропущены."
            )
        # Один клиент на всю пачку — не поднимаем соединение на каждый файл.
        _head_client, _head_model = None, None
        try:
            from openai import OpenAI
            _lm_url, _head_model = _load_lm_config()
            _head_client = OpenAI(base_url=_lm_url, api_key="lm-studio", timeout=180.0)
        except Exception as _e:
            st.caption(
                f"⚠️ LLM недоступна ({_e}) — документы будут приложены без "
                f"верхнеуровневого описания."
            )
        for _f in _new_files[:_free]:
            with st.spinner(f"Читаю «{_f.name}»…"):
                _d = validate_and_read_upload(_f.getvalue(), _f.name)
                if _d.get("ok"):
                    _d["head_summary"] = read_document_head(
                        _d, article_name, _head_client, _head_model,
                    )
            docs.append(_d)
            _log_usage("predictor", "doc_attached", meta={
                "filename": _d.get("filename", "")[:120],
                "source":   "upload",
                "ok":       bool(_d.get("ok")),
                "chars":    len(_d.get("full_text", "") or ""),
                "error":    (_d.get("error") or "")[:120],
            })
        st.rerun()

    # ── Выбор из базы Сканера документов ─────────────────────────────────
    with st.expander("Выбрать документы из Сканера", expanded=False):
        try:
            from streamlit_pages.doc_scanner import (
                load_db as _load_scan_db, _fname as _scan_fname,
            )
            _scan_docs = _load_scan_db().get("documents", [])
        except Exception:
            _scan_docs = []

        if not _scan_docs:
            st.caption(
                "База Сканера пуста — загрузите документы в разделе "
                "«Сканер документов»."
            )
        else:
            _scan_opts = {_scan_fname(d): d for d in _scan_docs}
            _picked = st.multiselect(
                "Документы из базы Сканера",
                list(_scan_opts.keys()),
                key="pred_scanner_pick",
                placeholder="Ничего не выбрано",
            )
            if st.button(
                "Приложить выбранные",
                key="pred_scanner_add",
                disabled=not _picked,
                use_container_width=True,
            ):
                _free = max(0, _max_docs - len(docs))
                _already = {d.get("filename") for d in docs}
                _head_client, _head_model = None, None
                try:
                    from openai import OpenAI
                    _lm_url, _head_model = _load_lm_config()
                    _head_client = OpenAI(
                        base_url=_lm_url, api_key="lm-studio", timeout=180.0,
                    )
                except Exception:
                    _head_client = None
                _added = 0
                for _name in _picked:
                    if _added >= _free or _name in _already:
                        continue
                    with st.spinner(f"Читаю «{_name}»…"):
                        _d = doc_from_scanner(_scan_opts[_name])
                        if _d.get("ok"):
                            _d["head_summary"] = read_document_head(
                                _d, article_name, _head_client, _head_model,
                            )
                    docs.append(_d)
                    _added += 1
                    _log_usage("predictor", "doc_attached", meta={
                        "filename": _d.get("filename", "")[:120],
                        "source":   "scanner",
                        "ok":       bool(_d.get("ok")),
                        "chars":    len(_d.get("full_text", "") or ""),
                    })
                st.rerun()

    # ── Карточки приложенных документов ──────────────────────────────────
    if docs:
        _n_ok  = len(ok_docs(docs))
        _n_bad = len(docs) - _n_ok
        _hdr = f"Приложено документов: {_n_ok}"
        if _n_bad:
            _hdr += f"  ·  отклонено: {_n_bad}"
        st.markdown(f"**{_hdr}**")

        for _i, _d in enumerate(docs):
            _is_ok   = bool(_d.get("ok"))
            _mark    = "✅" if _is_ok else "⚠️"
            _src_lbl = "Сканер" if _d.get("source") == "scanner" else "с машины"
            _size_kb = (_d.get("size", 0) or 0) / 1024

            _c1, _c2 = st.columns([11, 1])
            with _c1:
                st.markdown(
                    f"{_mark} **{_d.get('filename', 'без имени')}**  "
                    f"<span style='color:#666;font-size:0.82rem'>"
                    f"{_src_lbl} · {_size_kb:,.0f} КБ · "
                    f"{len(_d.get('pages', []) or [])} стр. · "
                    f"{len(_d.get('full_text', '') or ''):,} симв.</span>".replace(",", " "),
                    unsafe_allow_html=True,
                )
                if _is_ok:
                    if _d.get("head_summary"):
                        st.caption(f"Система прочитала: {_d['head_summary']}")
                    else:
                        st.caption(
                            "Верхнеуровневое описание не получено — на сам "
                            "прогноз это не влияет, документ используется целиком."
                        )
                    with st.expander("Показать текст документа", expanded=False):
                        for _p in (_d.get("pages") or [])[:50]:
                            _ptxt = (_p.get("text") or "").strip()
                            if not _ptxt:
                                continue
                            _pm = " · OCR" if _p.get("method") == "ocr" else ""
                            st.caption(f"Страница {_p.get('page', '?')}{_pm}")
                            st.text(_ptxt[:3000] + ("…" if len(_ptxt) > 3000 else ""))
                        if len(_d.get("pages") or []) > 50:
                            st.caption("… показаны первые 50 страниц.")
                else:
                    st.caption(f"Не принят: {_d.get('error', 'причина не определена')}")
            with _c2:
                if st.button(
                    "✕", key=f"pred_doc_del_{_d.get('id', _i)}",
                    help="Убрать документ",
                ):
                    docs[:] = [x for x in docs if x.get("id") != _d.get("id")]
                    st.rerun()

        if _n_bad:
            st.caption(
                "Отклонённые документы в прогноз не идут. Замените их "
                "читаемой версией или уберите — остальные приложенные "
                "документы это не затрагивает."
            )
        st.markdown("")

    # ── Шаг 3: Источник поиска ───────────────────────────────────────────────
    st.subheader("3. Источник поиска")
    _SOURCE_OPTIONS = {
        "Только экспертные заключения": ["expertise"],
        "Только протоколы":             ["protocols"],
        "Оба источника":                ["expertise", "protocols"],
    }
    source_label = st.radio(
        "Где искать аналогичные случаи",
        list(_SOURCE_OPTIONS.keys()),
        index=0,  # по умолчанию — только экспертные
        horizontal=True,
        key="pred_source_radio",
        help=(
            "Экспертные заключения — новая база с полным набором атрибутов "
            "(регион, сфера, год, метод). Протоколы — старая коллекция; "
            "фильтры по году и методу регулирования к ней не применяются, "
            "так как эти поля отсутствуют в её метаданных."
        ),
    )
    selected_sources = _SOURCE_OPTIONS[source_label]
    if selected_sources == ["protocols"]:
        st.caption(
            "В коллекции протоколов нет полей «год» и «метод регулирования» — "
            "соответствующие фильтры ниже будут проигнорированы для этого источника."
        )
    elif "protocols" in selected_sources:
        st.caption(
            "Фильтры «год» и «метод» при комбинированном поиске применяются "
            "только к экспертным заключениям — у протоколов этих полей нет."
        )

    # ── Шаг 4: Фильтры ───────────────────────────────────────────────────────
    st.subheader("4. Фильтры (опционально)")

    def _collect_available_years() -> List[str]:
        """
        Возвращает текущий год и 4 предыдущих в виде строк
        (например: 2026, 2025, 2024, 2023, 2022).
        Ранее читалось из documents_registry.json, где встречались мусорные
        значения (диапазоны "2025-2029", числа "7433" и т.п.).
        """
        current_year = datetime.now().year
        return [str(current_year - i) for i in range(5)]

    _PRED_YEARS = _collect_available_years()

    # ── Список сфер: берём ДОСЛОВНО из streamlit_pages.expertise_panel.SPHERES
    # (а не отдельный список меток Советчика/НПА).
    #
    # БЫЛО (баг): здесь был свой список меток ("Обращение с ТКО", "Электрика",
    # "Водоснабжение/водоотведение" и т.п.) — он визуально копировал список
    # Советчика (_ADV_SPHERES в advisor_page.py), но НЕ совпадал со
    # значениями, которые реально сохраняются в metadata экспертных
    # документов (see expertise_panel.SPHERES: "ТКО", "Электроэнергетика",
    # "Водоснабжение"/"Водоотведение" раздельно). Фильтрация чанков
    # экспертных заключений — точное совпадение строк
    # (_filter_candidates_by_where в этом файле), поэтому из шести пунктов
    # реально совпадало дословно только "Теплоснабжение" — остальные пять
    # не находили ничего, сколько бы документов этой сферы ни было
    # загружено. Используя тот же список, что и при индексации, гарантируем
    # точное совпадение всегда.
    try:
        from streamlit_pages.expertise_panel import SPHERES as _EXPERTISE_SPHERES
    except Exception:
        _EXPERTISE_SPHERES = [
            "Теплоснабжение", "Водоснабжение/водоотведение", "ТКО",
            "Электроэнергетика", "Газоснабжение", "Иное",
        ]
    _SPHERE_ICONS = {
        "Теплоснабжение": "🔥", "Водоснабжение/водоотведение": "💧",
        "ТКО": "🗑️", "Электроэнергетика": "⚡", "Газоснабжение": "🔵", "Иное": "📁",
    }
    # label (с иконкой, для UI) → каноническое значение (как в metadata)
    _sphere_label_to_canonical = {
        f"{_SPHERE_ICONS.get(s, '📁')} {s}": s for s in _EXPERTISE_SPHERES
    }
    _PRED_SPHERES = list(_sphere_label_to_canonical.keys())

    filter_spheres_raw = st.multiselect(
        "Сфера регулирования",
        options=_PRED_SPHERES,
        default=[],
        key="pred_filter_spheres",
        placeholder="Все сферы — фильтр не применяется",
        help=(
            "Ограничивает поиск экспертными заключениями выбранных сфер "
            "(точное совпадение со значением, сохранённым при загрузке "
            "документа — см. вкладку «Протоколы/Экспертные» в Админке). "
            "Если не выбрано — поиск по всем сферам, включая документы, "
            "где сфера не была распознана при загрузке."
        ),
    )
    filter_spheres = [_sphere_label_to_canonical[s] for s in filter_spheres_raw]
    if filter_spheres:
        st.caption(f"Фильтр: **{'  ·  '.join(filter_spheres_raw)}**")

    _PRED_REGIONS = [
        # Центральный федеральный округ
        "Белгородская область", "Брянская область", "Владимирская область",
        "Воронежская область", "Ивановская область", "Калужская область",
        "Костромская область", "Курская область", "Липецкая область",
        "Московская область", "Орловская область", "Рязанская область",
        "Смоленская область", "Тамбовская область", "Тверская область",
        "Тульская область", "Ярославская область", "Москва",
        # Северо-Западный федеральный округ
        "Республика Карелия", "Республика Коми", "Архангельская область",
        "Ненецкий автономный округ", "Вологодская область",
        "Калининградская область", "Ленинградская область",
        "Мурманская область", "Новгородская область", "Псковская область",
        "Санкт-Петербург",
        # Южный федеральный округ
        "Республика Адыгея", "Республика Калмыкия", "Республика Крым",
        "Краснодарский край", "Астраханская область", "Волгоградская область",
        "Ростовская область", "Севастополь",
        # Северо-Кавказский федеральный округ
        "Республика Дагестан", "Республика Ингушетия",
        "Кабардино-Балкарская Республика", "Республика Северная Осетия — Алания",
        "Карачаево-Черкесская Республика", "Чеченская Республика",
        "Ставропольский край",
        # Приволжский федеральный округ
        "Республика Башкортостан", "Республика Марий Эл", "Республика Мордовия",
        "Республика Татарстан", "Удмуртская Республика", "Чувашская Республика",
        "Пермский край", "Кировская область", "Нижегородская область",
        "Оренбургская область", "Пензенская область", "Самарская область",
        "Саратовская область", "Ульяновская область",
        # Уральский федеральный округ
        "Курганская область", "Свердловская область", "Тюменская область",
        "Челябинская область", "Ханты-Мансийский автономный округ — Югра",
        "Ямало-Ненецкий автономный округ",
        # Сибирский федеральный округ
        "Республика Алтай", "Республика Бурятия", "Республика Тыва",
        "Республика Хакасия", "Алтайский край", "Красноярский край",
        "Иркутская область", "Кемеровская область", "Новосибирская область",
        "Омская область", "Томская область", "Забайкальский край",
        # Дальневосточный федеральный округ
        "Республика Саха (Якутия)", "Камчатский край", "Приморский край",
        "Хабаровский край", "Амурская область", "Магаданская область",
        "Сахалинская область", "Еврейская автономная область",
        "Чукотский автономный округ",
        # Новые регионы
        "Донецкая Народная Республика", "Луганская Народная Республика",
        "Запорожская область", "Херсонская область",
    ]

    filter_regions = st.multiselect(
        "Регион",
        options=_PRED_REGIONS,
        default=[],
        key="pred_filter_regions",
        placeholder="Все регионы — фильтр не применяется",
        help="Ограничивает поиск документами выбранных регионов.",
    )
    if filter_regions:
        st.caption(f"Фильтр: **{'  ·  '.join(filter_regions)}**")

    _PRED_METHODS = [
        "Индексация",
        "Метод экономически обоснованных расходов (ЭОЗ)",
        "RAB",
    ]

    # Алиасы для ChromaDB: одно отображаемое значение → несколько вариантов
    # индексации, чтобы $in-запрос находил документы с любым из них.
    _METHOD_ALIASES: Dict[str, List[str]] = {
        "Метод экономически обоснованных расходов (ЭОЗ)": [
            "ЭОЗ",
            "Метод экономически обоснованных расходов",
        ],
    }

    fcol1, fcol2 = st.columns(2)
    with fcol1:
        filter_years = st.multiselect(
            "Год регулирования",
            options=_PRED_YEARS,
            default=[],
            key="pred_filter_years",
            placeholder="Все годы — фильтр не применяется",
            help="Ограничивает поиск документами с указанным годом регулирования.",
        )
        if filter_years:
            st.caption(f"Фильтр: **{'  ·  '.join(filter_years)}**")
    with fcol2:
        filter_methods = st.multiselect(
            "Метод регулирования",
            options=_PRED_METHODS,
            default=[],
            key="pred_filter_methods",
            placeholder="Все методы — фильтр не применяется",
            help="Ограничивает поиск документами с указанным методом регулирования.",
        )
        if filter_methods:
            st.caption(f"Фильтр: **{'  ·  '.join(filter_methods)}**")

    # ── Шаг 5: Настройки поиска ───────────────────────────────────────────────
    with st.expander("Настройки поиска", expanded=False):
        top_k = st.slider(
            "Количество источников (top-K)",
            min_value=5,
            max_value=100,
            value=_DEFAULT_TOP_K,
            step=5,
            key="pred_top_k",
            help="Сколько фрагментов протоколов извлекается перед классификацией",
        )
        enable_verification = st.checkbox(
            "Вторая (верификационная) проверка позиции",
            value=bool(load_predictor_config().get("enable_verification", True)),
            key="pred_enable_verification",
            help=(
                "Независимая перепроверка каждого предварительного "
                "positive/negative результата. Если результаты почти всегда "
                "уходят в «нейтрально» — отключите здесь и запустите прогноз "
                "заново, чтобы проверить, не она ли тому причина."
            ),
        )
        if not enable_verification:
            st.caption(
                "⚠️ Вторая проверка отключена — positive/negative от первого "
                "этапа не будут понижаться до нейтральных."
            )
        enable_summary = st.checkbox(
            "Формировать краткое резюме (НПА + практика)",
            value=bool(load_predictor_config().get("enable_summary", True)),
            key="pred_enable_summary",
            help=(
                "После расчёта прогноза дополнительно ищутся применимые НПА "
                "по выбранной сфере регулирования и формируется краткое "
                "резюме, опирающееся одновременно на найденные нормы и на "
                "практику регуляторов (источники «за»/«против» из "
                "результатов ниже). Увеличивает время расчёта на 15–40 сек."
            ),
        )

    # ── Кнопка запуска ────────────────────────────────────────────────────────
    st.divider()
    run_disabled = not article_name.strip()
    if st.button(
        "Рассчитать прогноз",
        type="primary",
        disabled=run_disabled,
        use_container_width=True,
        key="pred_run_btn",
    ):
        if not article_name.strip():
            st.warning("Введите наименование статьи затрат.")
        else:
            # Сохраняем параметры для запуска
            st.session_state.pred_result  = None
            st.session_state.pred_running = True
            st.session_state.pred_expert_overrides = {}  # сброс ручных правок предыдущего прогноза
            st.session_state._pred_params = {
                "article":       article_name,
                "justification": justification_text,
                # Документы намеренно НЕ копируются в _pred_params: их полный
                # текст уже лежит в st.session_state["pred_docs"], и дублировать
                # его во втором ключе session_state незачем — читаем оттуда
                # напрямую в момент запуска.
                "top_k":         top_k,
                "sources":       selected_sources,
                "enable_verification": enable_verification,
                "enable_summary": enable_summary,
                "filters": {
                    k: v for k, v in {
                        "spheres":  filter_spheres,   # список без эмодзи
                        "regions":  filter_regions,   # список регионов
                        "years":    filter_years,     # список годов
                        # Разворачиваем алиасы: "Метод ЭОЗ (ЭОЗ)" → ["ЭОЗ", "Метод ..."]
                        "methods":  [
                            alias
                            for m in filter_methods
                            for alias in _METHOD_ALIASES.get(m, [m])
                        ],
                    }.items() if v
                },
            }
            st.rerun()

    # ── Запуск прогноза ───────────────────────────────────────────────────────
    if st.session_state.get("pred_running") and st.session_state.get("_pred_params"):
        params = st.session_state._pred_params
        st.warning("Идёт анализ протоколов — не переключайте раздел и не закрывайте вкладку")
        progress_bar  = st.progress(0.0, text="Запуск…")
        status_text   = st.empty()

        def _progress(pct: float, msg: str):
            progress_bar.progress(min(pct, 0.99), text=msg)
            status_text.caption(msg)

        # Событие: прогноз запущен
        _log_usage("predictor", "prediction_started", meta={
            "article":          params["article"][:80],
            "top_k":            params["top_k"],
            "sources":          params.get("sources"),
            "has_filters":      bool(params.get("filters")),
            "has_justification": bool((params.get("justification") or "").strip()),
            "n_docs":           len(ok_docs(st.session_state.get("pred_docs", []))),
        })

        with st.spinner("Анализирую протоколы и экспертные заключения…"):
            result = run_prediction(
                article_name      = params["article"],
                justification_text= params["justification"],
                top_k             = params["top_k"],
                filters           = params["filters"],
                sources           = params.get("sources", ["expertise"]),
                _progress_cb      = _progress,
                force_verification= params.get("enable_verification"),
                with_summary      = params.get("enable_summary", True),
                docs              = st.session_state.get("pred_docs", []),
            )

        progress_bar.progress(1.0, text="Готово")
        st.session_state.pred_result  = result
        st.session_state.pred_running = False

        # Сохраняем в реестр
        if result and not result.get("error"):
            registry_record = {
                "timestamp": result.get("timestamp", datetime.now().isoformat()),
                "article":   result.get("article", ""),
                "query":     result.get("query", ""),
                "filters":   result.get("filters", {}),
                "aggregated_summary": {
                    "positive": len(result["aggregated"]["positive"]),
                    "negative": len(result["aggregated"]["negative"]),
                    "neutral":  len(result["aggregated"]["neutral"]),
                    "total_files": result["aggregated"]["total_files"],
                },
                "sources": [
                    {"file": r["file"], "decision": d}
                    for d in ("positive", "negative", "neutral")
                    for r in result["aggregated"].get(d, [])
                ],
                # Имена приложенных документов — без текста: реестр должен
                # оставаться компактным, тексты обоснований в нём не хранятся.
                "user_docs": [
                    d.get("filename", "") for d in result.get("user_docs", [])
                ],
            }
            save_to_registry(registry_record)

            # Событие: прогноз завершён успешно
            _agg = result.get("aggregated", {})
            _score = compute_approval_score(
                len(_agg.get("positive", [])),
                len(_agg.get("negative", [])),
                len(_agg.get("neutral",  [])),
            )
            _log_usage("predictor", "prediction_completed", meta={
                "article":    result.get("article", "")[:80],
                "n_positive": len(_agg.get("positive", [])),
                "n_negative": len(_agg.get("negative", [])),
                "n_neutral":  len(_agg.get("neutral",  [])),
                "score_pct":  _score["approval_pct"],
                "confidence": _score["confidence"],
                "sources":    result.get("sources"),
            })
        elif result and result.get("error"):
            # Событие: прогноз завершился ошибкой (нет документов / LM недоступен)
            _log_usage("predictor", "prediction_completed", meta={
                "article": params["article"][:80],
                "error":   result["error"][:120],
            })

        st.rerun()

    # ── Отображение результатов ───────────────────────────────────────────────
    result = st.session_state.get("pred_result")
    if result is None:
        st.info("Введите данные и нажмите «Рассчитать прогноз».")
        return

    if result.get("error"):
        st.error(result["error"])
        return

    agg  = result["aggregated"]
    total = agg.get("total_files", 0)

    # ── Применяем ручные правки эксперта — теперь для ЛЮБОЙ категории ───────
    # (раньше override можно было поставить только источникам, попавшим в
    # "нейтрально"; теперь эксперт может исправить и "за", и "против" —
    # каждая правка регистрируется в data/predictor/expert_overrides.jsonl).
    # Хранится отдельно от result, чтобы не модифицировать исходные данные
    # прогноза — override применяется только к отображению/подсчёту.
    if "pred_expert_overrides" not in st.session_state:
        st.session_state["pred_expert_overrides"] = {}
    _overrides = st.session_state["pred_expert_overrides"]

    # Помечаем каждую запись её ИСХОДНЫМ вердиктом ИИ (_ai_decision) ДО
    # применения ручных правок — нужно для отображения "ИИ определил как X,
    # эксперт исправил на Y" внутри карточки, независимо от того, в какой
    # финальный список запись в итоге попадёт.
    pos, neg, neu = [], [], []
    for _ai_decision, _records in (
        ("positive", agg.get("positive", [])),
        ("negative", agg.get("negative", [])),
        ("neutral",  agg.get("neutral",  [])),
    ):
        for _rec in _records:
            _rec = dict(_rec)
            _rec["_ai_decision"] = _ai_decision
            _fkey = _rec.get("file", "")
            _final = _overrides.get(_fkey, _ai_decision)
            {"positive": pos, "negative": neg, "neutral": neu}[_final].append(_rec)

    st.divider()
    st.subheader("Результаты")

    # ── Счётчик ───────────────────────────────────────────────────────────────
    st.markdown(
        _badge("За", len(pos), "#2e7a50")
        + _badge("Против", len(neg), "#b33a3a")
        + _badge("Нейтрально", len(neu), "#888")
        + f"<span style='color:#666;font-size:0.85rem;margin-left:8px'>"
        f"Уникальных источников: {total} · Фрагментов в поиске: {result.get('chunks_raw', 0)}</span>",
        unsafe_allow_html=True,
    )
    if result.get("chunks_dropped_budget"):
        st.caption(
            f"Контекст ограничен бюджетом {_RAG_CONTEXT_CHAR_BUDGET:,} символов — "
            f"{result['chunks_dropped_budget']} наименее релевантных фрагментов "
            f"не учитывались в анализе.".replace(",", " ")
        )
    _n_downgraded = agg.get("verifier_downgraded_files", 0)
    if _n_downgraded:
        st.caption(
            f"ℹ️ Из нейтральных — {_n_downgraded} понижены со второго этапа "
            f"проверки (были предварительно за/против, но вторая проверка "
            f"нашла несовпадение позиций). Разверните карточку источника, "
            f"чтобы увидеть исходную оценку и при необходимости выбрать "
            f"вручную."
        )
    st.markdown("")

    # ── Итоговая взвешенная оценка ──────────────────────────────────────────
    score = compute_approval_score(len(pos), len(neg), len(neu))

    _conf_label = {"high": "Высокая", "medium": "Средняя", "low": "Низкая"}[score["confidence"]]
    _conf_color = {"high": "#2e7a50", "medium": "#b8860b", "low": "#888"}[score["confidence"]]

    if score["all_neutral"]:
        st.info(
            "Все найденные источники нейтральны — ни один не содержит явного "
            "решения регулятора по схожей логике обоснования. Это говорит не "
            "о шансах на одобрение или отказ, а о том, что РЭКи, вероятно, "
            "ещё не сталкивались именно с такой комбинацией статьи затрат и "
            "обоснования. Показан нейтральный результат 50% с низкой "
            "уверенностью."
        )

    sc1, sc2 = st.columns([2, 1])
    with sc1:
        st.markdown(
            f"<div style='font-size:2.2rem;font-weight:700;color:#1a1a1a'>"
            f"{score['approval_pct']:.0f}% <span style='font-size:1rem;font-weight:400;color:#666'>"
            f"вероятность одобрения</span></div>",
            unsafe_allow_html=True,
        )
    with sc2:
        st.markdown(
            f"<div style='text-align:right'>"
            f"<span style='display:inline-block;padding:4px 14px;border-radius:14px;"
            f"background:{_conf_color};color:#fff;font-weight:600;font-size:0.85rem'>"
            f"Уверенность: {_conf_label}</span></div>",
            unsafe_allow_html=True,
        )

    st.progress(score["approval_pct"] / 100)

    with st.expander("Как считается эта оценка"):
        st.markdown(
            f"""
**Методология расчёта:**

1. Учитываются только содержательные источники — **{score['n_positive']} «за»** и
   **{score['n_negative']} «против»**. Нейтральные источники ({score['n_neutral']})
   в сам процент не входят: они не содержат решения регулятора по той же
   логике, что заявляет пользователь, и не должны размывать оценку.
2. «Против» весит сильнее «за» — в **{_NEGATIVE_WEIGHT}×**. Это сознательный
   перекос в сторону осторожности: ошибочно успокоить заявителя в случае
   риска отказа дороже, чем ошибочно насторожить при реальных шансах на
   одобрение.
3. Формула: `за / (за + против × {_NEGATIVE_WEIGHT}) × 100%`.
4. Если содержательных источников нет вообще (все найденные — нейтральны),
   показывается 50% с пометкой «низкая уверенность» — это не означает
   нейтральный шанс, а означает отсутствие данных по такой комбинации
   статьи и обоснования.
5. **Уверенность** оценки зависит от числа содержательных источников:
   {_HIGH_CONFIDENCE_THRESHOLD}+ — высокая, 1–{_HIGH_CONFIDENCE_THRESHOLD - 1} — средняя,
   0 — низкая.
            """
        )

    st.markdown("")

    # ── Краткое резюме (НПА по выбранной сфере + практика) ──────────────────
    _summary = result.get("summary")
    if _summary is not None:
        st.divider()
        st.markdown("#### Краткое резюме")
        if _summary.get("error"):
            st.caption(f"⚠️ Резюме не сформировано: {_summary['error']}")
        elif _summary.get("text"):
            st.markdown(_summary["text"])
            _npa_srcs = result.get("npa_sources") or []
            with st.expander(
                f"На чём основано резюме ({len(_npa_srcs)} НПА · "
                f"{len(pos)} «за» · {len(neg)} «против»)",
                expanded=False,
            ):
                if _npa_srcs:
                    st.markdown("**Применимые НПА:**")
                    for src in _npa_srcs:
                        _art = f", п. {src['article']}" if src.get("article") else ""
                        st.caption(f"· {src.get('file', '—')}{_art}")
                else:
                    st.caption(
                        "По данной статье и выбранной сфере в базе НПА "
                        "ничего не найдено — резюме опирается только на "
                        "практику регуляторов."
                    )
        else:
            st.caption("Резюме получилось пустым — попробуйте пересчитать прогноз.")

    st.divider()

    # ── Приложенные документы и их охват ─────────────────────────────────────
    # Прослеживаемость: видно, что именно система прочитала и на сколько
    # найденных прецедентов каждый документ реально повлиял. Документ с
    # нулевым охватом — сигнал, что он к этой статье затрат отношения не
    # имеет либо прочитан плохо (например, скан низкого качества).
    _user_docs = result.get("user_docs") or []
    if _user_docs:
        _ref_counts = {d["idx"]: 0 for d in _user_docs}
        for _rec in list(pos) + list(neg) + list(neu):
            for _r in (_rec.get("source_doc_refs") or []):
                if _r in _ref_counts:
                    _ref_counts[_r] += 1

        st.markdown(
            "<div style='font-weight:600;font-size:1rem;margin-bottom:6px'>"
            "Приложенные документы</div>",
            unsafe_allow_html=True,
        )
        for _d in _user_docs:
            _cnt = _ref_counts.get(_d["idx"], 0)
            _src_lbl = "Сканер" if _d.get("source") == "scanner" else "с машины"
            st.markdown(
                f"**{_d['idx']}. {_d.get('filename', 'без имени')}**  "
                f"<span style='color:#666;font-size:0.82rem'>{_src_lbl} · "
                f"{_d.get('pages', 0)} стр. · {_d.get('chars', 0):,} симв. · "
                f"сопоставлен с {_cnt} источник(ами)</span>".replace(",", " "),
                unsafe_allow_html=True,
            )
            if _d.get("head_summary"):
                st.caption(_d["head_summary"])
            if _cnt == 0:
                st.caption(
                    "⚠️ Ни один найденный прецедент не был сопоставлен с этим "
                    "документом — возможно, он не относится к данной статье "
                    "затрат либо прочитан некачественно."
                )
        st.markdown("")
        st.divider()

    # ── Источники — положительные ─────────────────────────────────────────────
    if pos:
        st.markdown(
            "<div style='color:#2e7a50;font-weight:600;font-size:1rem;margin-bottom:6px'>"
            "Одобрено / включено</div>",
            unsafe_allow_html=True,
        )
        for i, rec in enumerate(pos):
            _source_card(rec, i, "positive", article=result.get("article", ""),
                         user_docs=result.get("user_docs"))
        st.markdown("")

    # ── Источники — отрицательные ─────────────────────────────────────────────
    if neg:
        st.markdown(
            "<div style='color:#b33a3a;font-weight:600;font-size:1rem;margin-bottom:6px'>"
            "Отклонено / снижено</div>",
            unsafe_allow_html=True,
        )
        for i, rec in enumerate(neg):
            _source_card(rec, i, "negative", article=result.get("article", ""),
                         user_docs=result.get("user_docs"))
        st.markdown("")

    # ── Источники — нейтральные ───────────────────────────────────────────────
    if neu:
        st.markdown(
            "<div style='color:#888;font-weight:600;font-size:1rem;margin-bottom:6px'>"
            "Нейтральные упоминания</div>",
            unsafe_allow_html=True,
        )
        for i, rec in enumerate(neu):
            _source_card(rec, i, "neutral", article=result.get("article", ""),
                         user_docs=result.get("user_docs"))

    if not pos and not neg and not neu:
        st.warning("По данной статье затрат не найдено релевантных фрагментов в протоколах.")

    # ── Запрос и сжатое обоснование ───────────────────────────────────────────
    with st.expander("Детали поиска", expanded=False):
        st.caption(f"Поисковый запрос: {result.get('query', '—')}")
        if result.get("user_docs"):
            st.caption(
                "Обоснование собрано из приложенных документов с маркировкой "
                "источника («=== ДОКУМЕНТ N: … ===») — по ней модель и "
                "определяет, с каким вашим документом сопоставлен прецедент."
            )
        if result.get("justification_summary") and result["justification_summary"] != result.get("justification_text"):
            st.markdown("**Обоснование, ушедшее в анализ:**")
            st.text(result["justification_summary"][:800])

    # ── Сброс ─────────────────────────────────────────────────────────────────
    st.divider()
    if st.button("Новый прогноз", key="pred_reset_btn"):
        # pred_docs и ключи виджетов прикрепления тоже сбрасываем: документы
        # живут ровно один прогноз (решение по архитектуре — в базу Сканера
        # они не сохраняются), поэтому «Новый прогноз» должен начинать с
        # чистого листа, а не тянуть за собой файлы прошлой заявки.
        for k in [
            "pred_result", "pred_running", "_pred_params", "pred_doc_text",
            "pred_docs", "pred_upload", "pred_scanner_pick",
            "pred_expert_overrides",
        ]:
            st.session_state.pop(k, None)
        st.rerun()


# =============================================================================
# Точка входа
# =============================================================================
if __name__ == "__main__":
    show_predictor()