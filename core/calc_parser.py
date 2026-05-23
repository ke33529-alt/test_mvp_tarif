# core/calc_parser.py
"""
Парсер расчётных Excel-файлов тарифных заявок
──────────────────────────────────────────────
Алгоритм:
  1. classify_sheet(ws)    — тип листа по сигнальным фразам из НПА
  2. find_table_rect(ws)   — прямоугольник таблицы по плотности данных
  3. extract_flat(ws,rect) — unpivot: каждая ячейка → одна запись
  4. parse_workbook(...)   — всё вместе, возвращает DataFrame + контекст
  5. to_llm_context(df)    — форматирует DataFrame для подачи в LLM

Не зависит от имён листов. Работает с любым расположением таблицы.
Пометки на полях отфильтровываются автоматически.
"""

from __future__ import annotations
import io
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Константы классификации листов
# ---------------------------------------------------------------------------
_SHEET_SIGNALS: Dict[str, List[str]] = {
    "смета_главная": [
        "смета затрат",
        "приложение 5.1",
        "базовый уровень операцио",
    ],
    "нвв": [
        "необходимая валовая выручка",
        "расчет необходимой валов",
        "итого необходимая валовая",
    ],
    "операционные": [
        "операционных (подконтрольных)",
        "определение операционных",
        "базовый уровень операционных",
    ],
    "неподконтрольные": [
        "реестр неподконтрольных",
        "неподконтрольные расходы",
    ],
    "энергоресурсы": [
        "реестр расходов на приобретение энергетических",
        "расходы на приобретение энергетических ресурсов",
    ],
    "тарифы": [
        "расчет тарифов",
        "тариф ето",
        "расчет  тарифов",
    ],
    "иные_расходы": [
        "иные расходы",
        "фактические затраты",
    ],
    "баланс": [
        "баланс тепловой энергии",
        "выработка на источнике",
    ],
    "нормативы_численности": [
        "нормативы численности",
        "расчет численности",
    ],
}

# Сигналы заголовков периодов (шапка таблицы)
_PERIOD_KEYWORDS = {
    "план", "факт", "ожид", "рсо", "урт",
    "заяв", "дельта", "откл", "норм",
}
# Слова, запрещающие трактовать ячейку как заголовок периода
_PERIOD_BLACKLIST = {
    "приказ", "методич", "утвержден", "федераль", "постановл",
    "приложен", "указани", "инструкц",
}
_YEAR_RANGE = set(range(2015, 2032))

# Маркеры пометок на полях — такие строки пропускаем
_NOTE_PREFIXES = ("*", "#", "примечание", "см.", "(", "!", "//", "note")

# Единицы измерения — признак строки данных
_UNIT_PATTERNS = re.compile(
    r"тыс\.?\s*руб|руб\./|тыс\.\s*гкал|тыс\.\s*квт|ед\.|чел\.|%|гкал|квт",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 1. Классификация листа
# ---------------------------------------------------------------------------
def classify_sheet(ws) -> Tuple[str, str]:
    """
    Возвращает (тип_листа, сигнальная_фраза).
    Читает первые 80 строк, собирает весь текст, ищет сигналы.
    """
    texts: List[str] = []
    for row in ws.iter_rows(max_row=80, values_only=True):
        for cell in row:
            if isinstance(cell, str) and cell.strip():
                texts.append(cell.lower().strip())

    joined = " ".join(texts)

    for sheet_type, signals in _SHEET_SIGNALS.items():
        for sig in signals:
            if sig in joined:
                return sheet_type, sig

    # Fallback по признакам
    has_tys = "тыс. руб" in joined or "тыс.руб" in joined
    year_cnt = sum(1 for t in texts
                   if t.strip().isdigit() and int(t.strip()) in _YEAR_RANGE)
    if has_tys and year_cnt >= 2:
        return "смета_неизвестная", "по признакам"
    if len(texts) < 3:
        return "пустой", ""
    return "неизвестный", texts[0][:40] if texts else ""


# ---------------------------------------------------------------------------
# 2. Поиск прямоугольника таблицы
# ---------------------------------------------------------------------------
def find_table_rect(ws) -> Optional[Dict]:
    """
    Находит прямоугольник таблицы на листе по плотности данных.

    Алгоритм:
      A) Строит карту заполненных ячеек
      B) Ищет строку-шапку по годам (приоритет) или ключевым словам периодов.
         Требует ≥2 найденных столбцов периодов ИЛИ ≥1 год-число + данные ниже.
      C) Определяет столбец боковика: предпочитает длинные тексты перед числами.
      D) Определяет правую и нижнюю границы.
    """
    # ── A: карта ячеек ──────────────────────────────────────────────────────
    cells: Dict[Tuple[int, int], object] = {}
    max_r = max_c = 0

    for row in ws.iter_rows():
        for cell in row:
            v = cell.value
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            cells[(cell.row, cell.column)] = v
            max_r = max(max_r, cell.row)
            max_c = max(max_c, cell.column)

    if not cells:
        return None

    # ── B: строка шапки ─────────────────────────────────────────────────────
    # Два прохода: сначала ищем строку с ≥2 годами-числами (надёжно),
    # затем fallback — строка с ≥2 текстовыми ключевыми словами периодов.
    header_row: Optional[int] = None
    header_cols: Dict[int, str] = {}

    def _has_data_below(r: int, period_cols: Dict[int, str]) -> bool:
        for r2 in range(r + 1, min(r + 20, max_r + 1)):
            for c in period_cols:
                v2 = cells.get((r2, c))
                if isinstance(v2, (int, float)):
                    return True
        return False

    def _is_blacklisted(text: str) -> bool:
        tl = text.lower()
        return any(bl in tl for bl in _PERIOD_BLACKLIST)

    # Проход 1: строки с ≥2 year-числами
    for r in range(1, min(max_r, 60)):
        year_cols: Dict[int, str] = {}
        for c in range(1, max_c + 1):
            v = cells.get((r, c))
            if isinstance(v, (int, float)) and int(v) in _YEAR_RANGE:
                year_cols[c] = str(int(v))
        if len(year_cols) >= 2 and _has_data_below(r, year_cols):
            header_row = r
            header_cols = year_cols
            break
        # Одиночный год — запоминаем как кандидата, но продолжаем искать лучше
        if len(year_cols) == 1 and header_row is None:
            if _has_data_below(r, year_cols):
                header_row = r
                header_cols = year_cols

    # Проход 2: если год не нашли — ищем по ключевым словам периодов (≥2)
    if not header_cols:
        for r in range(1, min(max_r, 60)):
            kw_cols: Dict[int, str] = {}
            for c in range(1, max_c + 1):
                v = cells.get((r, c))
                if not isinstance(v, str):
                    continue
                vl = v.lower().strip()
                if _is_blacklisted(vl):
                    continue
                if len(v.strip()) > 35:  # слишком длинный — не заголовок
                    continue
                for kw in _PERIOD_KEYWORDS:
                    if kw in vl:
                        kw_cols[c] = v.strip()[:25]
                        break
            if len(kw_cols) >= 2 and _has_data_below(r, kw_cols):
                header_row = r
                header_cols = kw_cols
                break

    if header_row is None or not header_cols:
        return None

    # ── C: столбец боковика ─────────────────────────────────────────────────
    # Боковик = столбец левее столбцов значений, у которого:
    #   - ниже шапки есть текстовые строки (≥3)
    #   - средняя длина текста > 5 символов (чтобы не взять столбец с "1","2","3")
    min_period_col = min(header_cols.keys())
    article_col: Optional[int] = None
    best_score = -1

    for c in range(1, min_period_col + 1):
        texts_below = [
            str(cells.get((r2, c)))
            for r2 in range(header_row + 1, min(header_row + 25, max_r + 1))
            if isinstance(cells.get((r2, c)), str)
            and len(str(cells.get((r2, c))).strip()) > 1
        ]
        if len(texts_below) < 3:
            continue
        avg_len = sum(len(t) for t in texts_below) / len(texts_below)
        # Предпочитаем столбец с более длинными текстами
        score = len(texts_below) * avg_len
        if score > best_score:
            best_score = score
            article_col = c

    if article_col is None:
        article_col = 1

    # ── D: границы прямоугольника ───────────────────────────────────────────
    right_col = max(header_cols.keys())
    bottom_row = header_row
    for r in range(header_row + 1, max_r + 1):
        for c in header_cols:
            v = cells.get((r, c))
            if isinstance(v, (int, float)):
                bottom_row = r
                break

    return {
        "header_row":  header_row,
        "article_col": article_col,
        "value_cols":  header_cols,
        "top":         header_row + 1,
        "bottom":      bottom_row,
        "left":        article_col,
        "right":       right_col,
    }


# ---------------------------------------------------------------------------
# 3. Unpivot: двумерная таблица → плоский DataFrame
# ---------------------------------------------------------------------------
def extract_flat(ws, rect: Dict, sheet_name: str = "",
                 sheet_type: str = "") -> pd.DataFrame:
    """
    Извлекает данные из прямоугольника таблицы в плоский вид.

    Возвращает DataFrame с колонками:
      sheet, sheet_type, article, period, value, is_total, section
    """
    rows_out: List[Dict] = []
    current_section = ""

    for r in range(rect["top"], rect["bottom"] + 1):
        # Боковик
        raw_article = ws.cell(row=r, column=rect["article_col"]).value
        if raw_article is None:
            # Попробуем соседние столбцы (объединённые ячейки)
            for dc in (1, 2, -1):
                nc = rect["article_col"] + dc
                if nc < 1:
                    continue
                alt = ws.cell(row=r, column=nc).value
                if alt is not None:
                    raw_article = alt
                    break
        if raw_article is None:
            continue

        article_str = str(raw_article).strip()
        if not article_str:
            continue

        # ── Пометка на полях ────────────────────────────────────────────────
        article_lower = article_str.lower()
        if any(article_lower.startswith(p) for p in _NOTE_PREFIXES):
            continue


        # ── Строка-раздел (заголовок группы, нет чисел) ─────────────────────
        has_any_number = any(
            isinstance(ws.cell(row=r, column=c).value, (int, float))
            for c in rect["value_cols"]
        )
        if not has_any_number:
            # Запоминаем как текущий раздел
            current_section = article_str
            continue

        # ── Итоговая строка ──────────────────────────────────────────────────
        is_total = any(
            kw in article_lower
            for kw in ("итого", "всего", "total", "нвв", "sum")
        )

        # ── Собираем значения по периодам ────────────────────────────────────
        for col_idx, period_label in rect["value_cols"].items():
            v = ws.cell(row=r, column=col_idx).value
            if not isinstance(v, (int, float)):
                continue
            rows_out.append({
                "sheet":       sheet_name,
                "sheet_type":  sheet_type,
                "section":     current_section,
                "article":     article_str,
                "period":      period_label,
                "value":       v,
                "is_total":    is_total,
            })

    if not rows_out:
        return pd.DataFrame()

    return pd.DataFrame(rows_out)


# ---------------------------------------------------------------------------
# 4. Основная функция: разбор всей книги
# ---------------------------------------------------------------------------
def parse_workbook(source) -> Tuple[pd.DataFrame, str]:
    """
    Разбирает Excel-файл (путь или bytes) по всем листам.

    Возвращает:
      df   — плоский DataFrame всех данных
      info — строка с диагностикой (какие листы нашли, сколько строк)
    """
    try:
        import openpyxl
    except ImportError:
        return pd.DataFrame(), "[calc_parser] openpyxl не установлен"

    try:
        if isinstance(source, (bytes, bytearray)):
            wb = openpyxl.load_workbook(
                io.BytesIO(source), data_only=True
            )
        else:
            wb = openpyxl.load_workbook(source, data_only=True)
    except Exception as e:
        return pd.DataFrame(), f"[calc_parser] Ошибка открытия файла: {e}"

    all_frames: List[pd.DataFrame] = []
    info_lines: List[str] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]

        # Классификация
        sheet_type, signal = classify_sheet(ws)
        if sheet_type in ("пустой", "неизвестный",
                          "нормативы_численности", "баланс"):
            info_lines.append(f"  [{sheet_name}] → пропущен ({sheet_type})")
            continue

        # Поиск прямоугольника
        rect = find_table_rect(ws)
        if rect is None:
            info_lines.append(
                f"  [{sheet_name}] ({sheet_type}) → таблица не найдена"
            )
            continue

        # Извлечение
        df = extract_flat(ws, rect, sheet_name=sheet_name,
                          sheet_type=sheet_type)
        if df.empty:
            info_lines.append(
                f"  [{sheet_name}] ({sheet_type}) → нет данных"
            )
            continue

        all_frames.append(df)
        info_lines.append(
            f"  [{sheet_name}] ({sheet_type}) "
            f"→ {len(df)} записей, "
            f"периоды: {sorted(df['period'].unique().tolist())}"
        )

    info = "\n".join(info_lines)
    if not all_frames:
        return pd.DataFrame(), info

    result = pd.concat(all_frames, ignore_index=True)
    return result, info


# ---------------------------------------------------------------------------
# 5. Форматирование для LLM
# ---------------------------------------------------------------------------
def to_llm_context(df: pd.DataFrame, max_chars: int = 8000) -> str:
    """
    Превращает плоский DataFrame в читаемый текст для LLM-анализа.

    Формат:
      [тип_листа: имя_листа]
      Статья | период1=значение, период2=значение, ...
      ...

    Итоговые строки помечаются ★
    """
    if df.empty:
        return ""

    lines: List[str] = []

    for (sheet_type, sheet_name), group in df.groupby(
        ["sheet_type", "sheet"], sort=False
    ):
        lines.append(f"\n[{sheet_type}: {sheet_name}]")
        current_section = ""

        # Pivot: article × period → values
        pivot = (
            group.groupby(["section", "article", "period", "is_total"])["value"]
            .first()
            .reset_index()
        )

        for _, row_meta in (
            pivot.groupby(["section", "article", "is_total"], sort=False)
            .first()
            .reset_index()[["section", "article", "is_total"]]
            .drop_duplicates()
            .iterrows()
        ):
            sec    = row_meta["section"]
            art    = row_meta["article"]
            total  = row_meta["is_total"]

            # Заголовок раздела
            if sec and sec != current_section:
                lines.append(f"  # {sec}")
                current_section = sec

            # Значения по периодам
            vals = pivot[
                (pivot["article"] == art) & (pivot["is_total"] == total)
            ][["period", "value"]].set_index("period")["value"].to_dict()

            vals_str = ", ".join(
                f"{p}={v:,.0f}" for p, v in sorted(vals.items(), key=lambda x: str(x[0]))
            )
            prefix = "  ★ " if total else "    "
            lines.append(f"{prefix}{art}: {vals_str}")

        # Ограничение по символам
        if sum(len(l) for l in lines) > max_chars:
            lines.append("  ... [обрезано по лимиту символов]")
            break

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. Быстрая диагностика (для отладки)
# ---------------------------------------------------------------------------
def diagnose(source) -> str:
    """Возвращает краткий отчёт о структуре файла — для вывода в UI."""
    df, info = parse_workbook(source)
    if df.empty:
        return f"Данные не извлечены.\n{info}"

    sheet_counts = df.groupby(["sheet_type", "sheet"]).size().reset_index(name="записей")
    summary = sheet_counts.to_string(index=False)
    periods = sorted(df["period"].unique().tolist())
    articles_count = df["article"].nunique()

    return (
        f"Извлечено: {len(df)} записей, "
        f"{articles_count} уникальных статей, "
        f"периоды: {periods}\n\n"
        f"Листы:\n{summary}\n\n"
        f"Диагностика:\n{info}"
    )