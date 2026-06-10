# core/calc_parser.py
"""
Универсальный парсер расчётных файлов тарифных заявок.

Поддерживает два формата:
  1. ЕИАС (признак: строка PJ_YEAR в первых 15 строках любого листа)
     — любая сфера (ТЭ, ГВС, ТКО, водоснабжение и др.)
     — любая версия формы
  2. Произвольные Excel (сметы, УРТ-формы, расчёты организаций)
     — эвристика: строка = имя статьи + числа

Публичный API:
    parse_workbook(bytes_or_path) -> (df, meta)
    to_llm_context(df)            -> str  (только ненулевые статьи)
    to_llm_context_compact(df)    -> str  (компактный формат для промпта)
"""

from __future__ import annotations
import io, re
from typing import Dict, List, Optional, Tuple
import openpyxl
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────────────────────────────────────

_EIAS_SKIP_SHEETS = {
    "Инструкция", "Информация JSON", "Список листов", "Общие сведения",
    "Заявление", "Список территорий", "Список объектов", "Сценарии",
    "Сценарии (МСА)", "Расчет УЕ", "Баланс Пр", "Баланс Тр", "Баланс",
    "Баланс ГВС (Тр)", "Баланс ГВС", "Транспорт", "Комментарии",
    "ТМ", "ДПР", "ДПР-КС", "Экономия_корр", "Корр Факт", "Корр ИП",
    "Корр Факт РО", "ИП источники", "Удельные расходы (МСА)",
    "Базовый уровень (МСА)", "Расчет Индексация (П13)", "Расчет ИК (П14)",
    "Расчет МЭОР (П4)", "НР (П17)", "ОР долгосрочный (П10)",
    "Объем ТКО (П2)", "Масса ТКО (П3)",
    "TEHSHEET", "REESTR_OBJECT", "REESTR_OBJ_MO", "REESTR_OBJ_TRANSP",
    "REESTR_MO", "REESTR_ORG", "REESTR_DOP", "REESTR_SEP_DIV",
    "REESTR_IP_KS", "DICTIONARIES",
}
_EIAS_SKIP_PREFIXES = ("Черновик",)

_EIAS_SERVICE_KEYS = {
    "PJ_YEAR","PJ_PF","PJ_DOP","PJ_PERIOD","PJ_DOP_FIN",
    "PJ_DYN","PJ_DYN_V","PJ_NAME","PJ_NAME_UNIT","PJ_NAME_ED_IZM",
    "PJ_NAME_DOP","PJ_NAME_FUEL","PJ_UNIT","PJ_DOP_FIN",
    "dyn_names","dyn_name","uni_prd_data","uni_org_reg","uni_pf",
    "V_POK","D_YEAR","obj_mo","obj_obj",
}

_PF_NORM = {
    "предложение организации":   "Предложение",
    "предложения организации":   "Предложение",
    "план организации":          "Предложение",
    "принято органом регулирования": "Принято",
    "утверждено":                "Принято",
    "факт по данным организации":"Факт",
    "факт, принятый органом регулирования": "Факт",
    "факт":                      "Факт",
    "план":                      "План",
    "ожидаемое за период по данным организации": "Ожидаемое",
}
_YEAR_MIN, _YEAR_MAX = 2010, 2040


# ─────────────────────────────────────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────────────────────────────────────

def _is_year(v) -> bool:
    try:
        y = int(str(v).strip())
        return _YEAR_MIN <= y <= _YEAR_MAX
    except Exception:
        return False

def _norm_year(v) -> str:
    return str(int(str(v).strip()))

def _norm_pf(v) -> str:
    if not v:
        return ""
    s = str(v).strip().lower()
    for key, norm in _PF_NORM.items():
        if key in s:
            return norm
    return str(v).strip()[:30]

def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def _is_article_name(v) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    if len(s) < 4:
        return False
    if s in _EIAS_SERVICE_KEYS:
        return False
    if re.match(r'^[MmPpDd]?\d+[\d.]*$', s):
        return False
    if re.match(r'^[\d\s.,:;/\\()\-]+$', s):
        return False
    # Исключаем JSON-подобный мусор
    if s.startswith("{") or "funcDyn" in s:
        return False
    # Исключаем технические коды ЕИАС (camelCase без пробелов)
    if re.match(r"^(check|ИТОГО_|et_|DYN_|P_)[A-Za-zА-Яа-я0-9_.]+$", s):
        return False
    # Исключаем составные идентификаторы типа "M11580::ТЭ.50::..."
    if "::" in s:
        return False
    # Исключаем строки-индексы типа "1.1", "2.1.3" (номер без текста)
    if re.match(r"^\d+(\.\d+)*$", s):
        return False
    return True

def _load_workbook(source) -> openpyxl.Workbook:
    if isinstance(source, (bytes, bytearray)):
        return openpyxl.load_workbook(io.BytesIO(source), data_only=True)
    return openpyxl.load_workbook(source, data_only=True)

def _detect_eias(wb: openpyxl.Workbook) -> bool:
    for sname in wb.sheetnames:
        ws = wb[sname]
        for row in ws.iter_rows(min_row=1, max_row=15, values_only=True):
            if row and row[0] == "PJ_YEAR":
                return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# ЕИАС-парсер
# ─────────────────────────────────────────────────────────────────────────────

# Заголовки колонок и единицы измерения — не статьи затрат
_HEADER_STRINGS = {
    "PJ_NAME", "Наименование", "Наименование показателя",
    "№ п/п", "Ед. изм.", "Единица измерения",
}
_UNIT_PATTERN = re.compile(
    r"^(тыс\.?\s*руб|руб\.|Гкал|куб\.?\s*м|кВт|тонн|чел\.|%|руб\./чел\.|тыс\.куб\.м)$",
    re.I
)

def _find_name_col(rows: List, data_start: int, year_cols: Dict[int,Tuple]) -> Optional[int]:
    """
    Автоматически определяет колонку с наименованием статьи.
    Выбирает колонку с наибольшим суммарным числом символов в длинных строках
    (>15 символов), исключая единицы измерения и заголовки.
    """
    col_scores: Dict[int, int] = {}   # сумма длин длинных строк
    col_counts: Dict[int, int] = {}   # количество длинных строк

    for row in rows[data_start: data_start + 40]:
        if not row:
            continue
        for col_idx, v in enumerate(row):
            if col_idx in year_cols:
                continue
            if not isinstance(v, str):
                continue
            s = v.strip()
            if not s or s in _HEADER_STRINGS:
                continue
            if _UNIT_PATTERN.match(s):
                continue
            if not _is_article_name(s):
                continue
            if len(s) > 15:   # только достаточно длинные строки — статьи
                col_scores[col_idx] = col_scores.get(col_idx, 0) + len(s)
                col_counts[col_idx] = col_counts.get(col_idx, 0) + 1

    if not col_scores:
        return None
    # Выбираем колонку с наибольшим суммарным объёмом длинного текста
    return max(col_scores, key=col_scores.__getitem__)


def _find_unit_col(rows: List, data_start: int, name_col: int) -> Optional[int]:
    """Ищет колонку с единицами измерения (тыс.руб., руб., Гкал и т.д.)."""
    unit_pattern = re.compile(
        r'тыс\.?\s*руб|руб\.|Гкал|куб\.?\s*м|кВт|тонн|чел\.|%', re.I
    )
    col_scores: Dict[int, int] = {}
    for row in rows[data_start: data_start + 15]:
        if not row:
            continue
        for col_idx, v in enumerate(row):
            if col_idx == name_col:
                continue
            if isinstance(v, str) and unit_pattern.search(v):
                col_scores[col_idx] = col_scores.get(col_idx, 0) + 1
    if not col_scores:
        return None
    return max(col_scores, key=col_scores.__getitem__)


def _parse_eias_sheet(ws) -> List[Dict]:
    rows = list(ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True))

    # Найти PJ_YEAR и PJ_PF
    pj_year_idx = pj_pf_idx = None
    for i, row in enumerate(rows):
        if row and row[0] == "PJ_YEAR" and pj_year_idx is None:
            pj_year_idx = i
        if row and row[0] == "PJ_PF" and pj_pf_idx is None:
            pj_pf_idx = i
        if pj_year_idx is not None and pj_pf_idx is not None:
            break

    if pj_year_idx is None:
        return []

    year_row = rows[pj_year_idx]
    pf_row   = rows[pj_pf_idx] if pj_pf_idx is not None else [None] * len(year_row)

    # Маппинг: col_idx → (year, pf_norm)
    col_map: Dict[int, Tuple[str, str]] = {}
    for col_idx, yval in enumerate(year_row):
        if _is_year(yval):
            pf_val = pf_row[col_idx] if col_idx < len(pf_row) else None
            col_map[col_idx] = (_norm_year(yval), _norm_pf(pf_val))

    if not col_map:
        return []

    data_start = (pj_pf_idx if pj_pf_idx is not None else pj_year_idx) + 1

    # Автоопределение колонки с именем статьи
    name_col = _find_name_col(rows, data_start, col_map)
    unit_col = _find_unit_col(rows, data_start, name_col) if name_col is not None else None

    records = []
    sheet_name = ws.title

    for row in rows[data_start:]:
        if not row:
            continue
        key = row[0]
        # Пропускаем явно служебные строки (ключ в первой ячейке)
        if key in _EIAS_SERVICE_KEYS:
            continue
        if isinstance(key, bool) or (isinstance(key, str) and key.startswith("{")):
            continue

        # Получаем имя статьи
        article_name = None
        if name_col is not None and name_col < len(row):
            v = row[name_col]
            if _is_article_name(v):
                article_name = str(v).strip()

        # Fallback: ищем в фиксированных позициях (когда key=None, данные сдвинуты)
        if not article_name:
            for col_idx in (2, 3, 1, 0):
                if col_idx < len(row) and _is_article_name(row[col_idx]):
                    article_name = str(row[col_idx]).strip()
                    break

        if not article_name:
            continue

        # Единица измерения
        unit = ""
        if unit_col is not None and unit_col < len(row):
            u = row[unit_col]
            if isinstance(u, str) and len(u) < 30:
                unit = u.strip()

        # Извлекаем значения
        for col_idx, (year, pf) in col_map.items():
            if col_idx >= len(row):
                continue
            val = row[col_idx]
            if _is_number(val) and val != 0:
                records.append({
                    "sheet":   sheet_name,
                    "article": article_name,
                    "period":  year,
                    "pf":      pf,
                    "value":   float(val),
                    "unit":    unit,
                })

    return records


def _parse_eias(wb: openpyxl.Workbook) -> Tuple[pd.DataFrame, Dict]:
    all_records = []
    skipped = []

    for sname in wb.sheetnames:
        if sname in _EIAS_SKIP_SHEETS:
            skipped.append(sname)
            continue
        if any(sname.startswith(p) for p in _EIAS_SKIP_PREFIXES):
            skipped.append(sname)
            continue
        ws = wb[sname]
        records = _parse_eias_sheet(ws)
        if records:
            all_records.extend(records)

    df = pd.DataFrame(all_records) if all_records else pd.DataFrame(
        columns=["sheet","article","period","pf","value","unit"])
    meta = {
        "format": "ЕИАС",
        "sheets_parsed":  len(wb.sheetnames) - len(skipped),
        "sheets_skipped": len(skipped),
        "records_total":  len(df),
    }
    return df, meta


# ─────────────────────────────────────────────────────────────────────────────
# Эвристический парсер для произвольных файлов
# ─────────────────────────────────────────────────────────────────────────────

def _find_year_cols_heuristic(rows: List, max_scan: int = 20) -> Dict[int, str]:
    year_cols: Dict[int, str] = {}
    for row in rows[:max_scan]:
        found = {i: _norm_year(v) for i, v in enumerate(row)
                 if v is not None and _is_year(v)}
        if len(found) >= 2:
            year_cols.update(found)
    return year_cols


def _parse_arbitrary_sheet(ws) -> List[Dict]:
    rows = list(ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True))
    if not rows:
        return []

    year_cols = _find_year_cols_heuristic(rows)
    sheet_name = ws.title
    records = []

    for row in rows:
        if not row:
            continue
        article_name = None
        name_col_idx = None
        for col_idx in range(min(5, len(row))):
            v = row[col_idx]
            if _is_article_name(v) and len(str(v).strip()) > 5 and not _is_year(v):
                article_name = str(v).strip()
                name_col_idx = col_idx
                break

        if not article_name:
            continue

        num_vals = [
            (i, v) for i, v in enumerate(row)
            if _is_number(v) and v != 0 and i != name_col_idx
        ]
        if not num_vals:
            continue

        if year_cols:
            for col_idx, val in num_vals:
                year = year_cols.get(col_idx, "")
                records.append({
                    "sheet":   sheet_name,
                    "article": article_name,
                    "period":  year,
                    "pf":      "",
                    "value":   float(val),
                    "unit":    "",
                })
        else:
            # Нет явных годов — берём все числа, период пустой
            for col_idx, val in num_vals:
                records.append({
                    "sheet":   sheet_name,
                    "article": article_name,
                    "period":  "",
                    "pf":      "",
                    "value":   float(val),
                    "unit":    "",
                })

    return records


def _parse_arbitrary(wb: openpyxl.Workbook) -> Tuple[pd.DataFrame, Dict]:
    all_records = []
    skip = {"Инструкция","TEHSHEET","DICTIONARIES"}
    for sname in wb.sheetnames:
        if sname in skip:
            continue
        ws = wb[sname]
        if ws.max_row < 3 or ws.max_column < 2:
            continue
        all_records.extend(_parse_arbitrary_sheet(ws))

    df = pd.DataFrame(all_records) if all_records else pd.DataFrame(
        columns=["sheet","article","period","pf","value","unit"])
    meta = {
        "format":        "Произвольный",
        "sheets_parsed": len(wb.sheetnames),
        "records_total": len(df),
    }
    return df, meta


# ─────────────────────────────────────────────────────────────────────────────
# Постобработка
# ─────────────────────────────────────────────────────────────────────────────

def _deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    pf_priority = {"Принято":0,"Предложение":1,"Факт":2,"План":3,"Ожидаемое":4,"":5}
    df = df.copy()
    df["_rank"] = df["pf"].map(lambda x: pf_priority.get(x, 6))
    df = df.sort_values("_rank").drop_duplicates(
        subset=["article","period","pf"], keep="first"
    ).drop(columns=["_rank"])
    return df.reset_index(drop=True)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["article"] = df["article"].str.strip().str[:200]
    # Убираем строки где article — только цифры/короткое
    df = df[~(df["article"].str.match(r'^\d') & (df["article"].str.len() < 5))]
    # Убираем явный мусор
    df = df[~df["article"].str.startswith("{")]
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Публичный API
# ─────────────────────────────────────────────────────────────────────────────

def parse_workbook(source) -> Tuple[pd.DataFrame, Dict]:
    """
    Главная точка входа.
    source: bytes | str (путь)
    Возвращает (df, meta) — только ненулевые статьи затрат.
    """
    wb = _load_workbook(source)
    if _detect_eias(wb):
        df, meta = _parse_eias(wb)
    else:
        df, meta = _parse_arbitrary(wb)

    if not df.empty:
        df = _clean(df)
        df = _deduplicate(df)
        df = df[df["value"] != 0].reset_index(drop=True)

    meta["records_final"] = len(df)
    return df, meta


def to_llm_context(df: pd.DataFrame, max_articles: int = 0) -> str:
    """
    Полный формат для LLM — сгруппирован по листу и статье.
    Только ненулевые статьи, отсортированы по убыванию значения.
    max_articles=0 — без ограничений (отбор делается на этапе апрува заявки).
    """
    if df.empty:
        return "Расчётный файл не содержит данных."

    lines = []
    for sheet, sdf in df.groupby("sheet", sort=False):
        lines.append(f"\n# {sheet}")
        ranked = sdf.groupby("article")["value"].max().abs().sort_values(ascending=False)
        top = ranked.head(max_articles).index if max_articles > 0 else ranked.index
        for article in top:
            adf = sdf[sdf["article"] == article].sort_values("period")
            lines.append(f"\n★ {article}")
            for _, row in adf.iterrows():
                period = row["period"] or "—"
                pf     = f" ({row['pf']})" if row["pf"] else ""
                val    = row["value"]
                unit   = row["unit"] or "тыс.руб."
                if abs(val) >= 1_000_000:
                    vs = f"{val/1_000_000:.2f} млн"
                elif abs(val) >= 1_000:
                    vs = f"{val:,.0f}"
                else:
                    vs = f"{val:.2f}"
                lines.append(f"  {period}{pf}: {vs} {unit}")
    return "\n".join(lines)


def to_llm_context_compact(df: pd.DataFrame, max_articles: int = 0) -> str:
    """
    Компактный формат для промпта (меньше токенов).
    Статья: год/тип=значение, ...
    max_articles=0 — без ограничений.
    """
    if df.empty:
        return "Нет данных."

    ranked = df.groupby("article")["value"].max().abs().sort_values(ascending=False)
    top = ranked.head(max_articles).index if max_articles > 0 else ranked.index
    lines = []
    for article in top:
        adf = df[df["article"] == article].sort_values("period")
        parts = []
        for _, row in adf.iterrows():
            period = row["period"] or "—"
            pf     = f"/{row['pf'][:3]}" if row["pf"] else ""
            val    = row["value"]
            vs = f"{val/1000:.0f}млн" if abs(val) >= 1_000_000 else f"{val:,.0f}"
            parts.append(f"{period}{pf}={vs}")
        lines.append(f"  {article}: {', '.join(parts)}")
    return "\n".join(lines)