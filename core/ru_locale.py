"""
ru_locale.py — русификация встроенных надписей Streamlit.

Streamlit не поддерживает смену языка своего интерфейса, поэтому модуль
внедряет в страницу скрипт, который следит за DOM (MutationObserver)
и подменяет английские служебные строки по словарю: подсказки
"Press Enter to apply", загрузчик файлов, меню, статус "Running...",
тулбар таблиц и т.д.

Расположение: core/ru_locale.py (папка core примонтирована томом).

Использование (app.py, сразу после st.set_page_config):

    from core.ru_locale import apply_russian_ui
    apply_russian_ui()

Скрипт ставится один раз на вкладку браузера и переживает реруны.
После обновления Streamlit стоит пройтись по UI: служебные строки
иногда меняются — недостающие добавляются в RU_EXACT / RU_REGEX.
"""

import json

import streamlit.components.v1 as components

# Точные совпадения (сравнение по тексту без пробелов по краям).
RU_EXACT: dict[str, str] = {
    # Подсказки полей ввода и форм
    "Press Enter to apply": "Нажмите Enter, чтобы применить",
    "Press ⌘+Enter to apply": "Нажмите ⌘+Enter, чтобы применить",
    "Press Ctrl+Enter to apply": "Нажмите Ctrl+Enter, чтобы применить",
    "Press Enter to submit form": "Нажмите Enter, чтобы отправить форму",
    "Press ⌘+Enter to submit form": "Нажмите ⌘+Enter, чтобы отправить форму",
    "Press Ctrl+Enter to submit form": "Нажмите Ctrl+Enter, чтобы отправить форму",
    "Show password text": "Показать пароль",
    "Hide password text": "Скрыть пароль",
    "Increment": "Увеличить",
    "Decrement": "Уменьшить",
    # Загрузчик файлов
    "Drag and drop file here": "Перетащите файл сюда",
    "Drag and drop a file here": "Перетащите файл сюда",
    "Drag and drop files here": "Перетащите файлы сюда",
    "Drag and drop directories here": "Перетащите папки сюда",
    "Browse files": "Выбрать файлы",
    "Upload": "Загрузить",
    "Add files": "Добавить файлы",
    "Upload directories": "Загрузить папки",
    # Выпадающие списки и мультиселект
    "Choose an option": "Выберите вариант",
    "Choose options": "Выберите варианты",
    "Choose or add an option": "Выберите или добавьте вариант",
    "Choose or add options": "Выберите или добавьте варианты",
    "Add an option": "Добавьте вариант",
    "Add options": "Добавьте варианты",
    "No options to select.": "Нет вариантов для выбора.",
    "No options to select": "Нет вариантов для выбора",
    "No options": "Нет вариантов",
    "No results": "Ничего не найдено",
    "Select all": "Выбрать все",
    "Clear all": "Очистить всё",
    "Clear value": "Очистить",
    "Selected values": "Выбранные значения",
    "Multiselect options": "Варианты выбора",
    "All selected options have been cleared.": "Выбор очищен.",
    # Статус выполнения и перезапуск
    "Running...": "Выполняется...",
    "Stop": "Остановить",
    "Rerun": "Перезапустить",
    "Always rerun": "Всегда перезапускать",
    "Source file changed.": "Исходный файл изменён.",
    "Connecting": "Подключение",
    "Please wait...": "Подождите...",
    "Connection error": "Ошибка соединения",
    # Сообщение об ошибке при showErrorDetails = false
    "This app has encountered an error. The original error message is redacted "
    "to prevent data leaks. Full error details have been recorded in the logs "
    "(if you're on Streamlit Cloud, click on 'Manage app' in the lower right of "
    "your app).":
        "В приложении произошла ошибка. Подробности записаны в журнал сервера. "
        "Обратитесь в поддержку через кнопку «Помощь».",
    "Traceback:": "Где произошла ошибка:",
    # Главное меню и настройки
    "Main menu": "Главное меню",
    "Clear cache": "Очистить кэш",
    "Settings": "Настройки",
    "Print": "Печать",
    "Record a screencast": "Записать экран",
    "About": "О программе",
    "Deploy": "Развернуть",
    "Developer options": "Для разработчика",
    "Wide mode": "Широкий режим",
    "Run on save": "Перезапуск при сохранении",
    "Use system setting": "Как в системе",
    "Light": "Светлая",
    "Dark": "Тёмная",
    "Close": "Закрыть",
    # Боковая панель и навигация
    "Collapse sidebar": "Свернуть панель",
    "Expand sidebar": "Развернуть панель",
    "View more": "Показать ещё",
    "View less": "Свернуть",
    # Таблицы (st.dataframe / st.data_editor), графики, код
    "Download as CSV": "Скачать CSV",
    "Search": "Поиск",
    "Close Search": "Закрыть поиск",
    "Next Result": "Следующий",
    "Previous Result": "Предыдущий",
    "Type to search": "Введите для поиска",
    "Show/hide columns": "Показать/скрыть столбцы",
    "Add row": "Добавить строку",
    "Delete row(s)": "Удалить строки",
    "Clear selection": "Снять выделение",
    "Copy column name": "Копировать название столбца",
    "Hide column": "Скрыть столбец",
    "Pin column": "Закрепить столбец",
    "Unpin column": "Открепить столбец",
    "Sort ascending": "По возрастанию",
    "Sort descending": "По убыванию",
    "Autosize": "Автоширина",
    "Format": "Формат",
    "Rename": "Переименовать",
    "Statistics": "Статистика",
    "No data": "Нет данных",
    "Fullscreen": "Во весь экран",
    "Close fullscreen": "Выйти из полноэкранного режима",
    "Copy to clipboard": "Копировать",
    # Чат и камера
    "Your message": "Ваше сообщение",
    "Take Photo": "Сделать снимок",
    "Clear photo": "Удалить снимок",
    "Switch camera": "Сменить камеру",
    "This app would like to use your camera.": "Приложению нужен доступ к камере.",
    "Learn how to allow access.": "Как разрешить доступ.",
}

# Строки с переменной частью: (регулярное выражение JS, замена).
RU_REGEX: list[tuple[str, str]] = [
    (r"^Limit (\d+)\s?MB per file(.*)$", "Не более $1 МБ на файл$2"),
    (r"^(\d+)\s?MB per file(.*)$", "До $1 МБ на файл$2"),
    (r"^(\d+(?:\.\d+)?)\s?MB$", "$1 МБ"),
    (r"^(\d+(?:\.\d+)?)\s?KB$", "$1 КБ"),
    (r"^Showing page (\d+) of (\d+)$", "Страница $1 из $2"),
    (r"^Select (\d+) matches$", "Выбрать найденные ($1)"),
    (r"^(\d+) options? available\.?$", "Доступно вариантов: $1"),
    (r"^Remove (.+)$", "Удалить «$1»"),
    (r"^Cancel upload of (.+)$", "Отменить загрузку «$1»"),
]

# Код, который выполняется в контексте основной страницы Streamlit.
_INSTALLER_JS = r"""
(function (EXACT, REGEX) {
  if (window.__ruLocaleInstalled) return;
  window.__ruLocaleInstalled = true;
  document.documentElement.lang = 'ru';

  const RX = REGEX.map(([p, r]) => [new RegExp(p), r]);
  const ATTRS = ['placeholder', 'aria-label', 'title'];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'CODE', 'PRE', 'INPUT']);

  function tr(str) {
    if (!str) return null;
    const t = str.trim();
    if (!t) return null;
    if (Object.prototype.hasOwnProperty.call(EXACT, t)) {
      return str.replace(t, EXACT[t]);
    }
    for (const [re, r] of RX) {
      if (re.test(t)) return str.replace(t, t.replace(re, r));
    }
    return null;
  }

  function fixText(node) {
    const p = node.parentElement;
    if (p && (SKIP.has(p.tagName) || p.isContentEditable)) return;
    const v = tr(node.nodeValue);
    if (v !== null && v !== node.nodeValue) node.nodeValue = v;
  }

  function fixAttrs(el) {
    for (const a of ATTRS) {
      const cur = el.getAttribute && el.getAttribute(a);
      if (!cur) continue;
      const v = tr(cur);
      if (v !== null && v !== cur) el.setAttribute(a, v);
    }
  }

  function walk(root) {
    if (root.nodeType === Node.TEXT_NODE) { fixText(root); return; }
    if (root.nodeType !== Node.ELEMENT_NODE) return;
    fixAttrs(root);
    const w = document.createTreeWalker(
      root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT
    );
    let n;
    while ((n = w.nextNode())) {
      if (n.nodeType === Node.TEXT_NODE) fixText(n);
      else fixAttrs(n);
    }
  }

  const obs = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === 'childList') m.addedNodes.forEach(walk);
      else if (m.type === 'characterData') fixText(m.target);
      else if (m.type === 'attributes') fixAttrs(m.target);
    }
  });

  obs.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ATTRS,
  });
  walk(document.body);
  console.info('ru_locale: русификатор установлен');
})(__EXACT__, __REGEX__);
"""


def _build_installer() -> str:
    return (
        _INSTALLER_JS
        .replace("__EXACT__", json.dumps(RU_EXACT, ensure_ascii=False))
        .replace("__REGEX__", json.dumps(RU_REGEX, ensure_ascii=False))
    )


def apply_russian_ui() -> None:
    """Внедряет русификатор в основную страницу (один раз на вкладку)."""
    installer = json.dumps(_build_installer(), ensure_ascii=False)
    components.html(
        f"""
<script>
(function () {{
  try {{
    // Прячем контейнер этого служебного iframe, чтобы не было пустого отступа.
    const fe = window.frameElement;
    if (fe) {{
      const box = fe.closest('[data-testid="stElementContainer"], .element-container');
      (box || fe).style.display = 'none';
    }}
    const P = window.parent;
    if (!P || P.__ruLocaleInstalled) return;
    const s = P.document.createElement('script');
    s.textContent = {installer};
    P.document.head.appendChild(s);
  }} catch (e) {{
    console.warn('ru_locale: не удалось внедрить скрипт', e);
  }}
}})();
</script>
""",
        height=0,
    )