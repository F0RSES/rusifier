# TermRu

**184 tests passing** · **48.7 MiB .deb** · **PDF + DOCX + PPTX** · **Windows / Linux (deb, AUR)**

CLI-приложение для автоматической замены англицизмов в документах **DOCX**,
**PPTX** и **PDF** на русские аналоги с сохранением исходного форматирования.

Технические аббревиатуры и устойчивые токены (`TLS`, `BGP`, `TCP`, `KPI` и др.)
не изменяются — они перечислены в `data/exceptions.json`.

## Требования

- Python 3.12+ (нижняя граница задана зависимостью `pdf-edit-engine`)
- Ключ DeepSeek API для контекстной проверки терминов

## Установка

### Windows (.exe)

Возьмите `termru.exe` из артефактов сборки (CI-задача `windows-exe`, см.
«Сборка дистрибутивов») и положите в любую папку. Ключ DeepSeek задаётся
переменными окружения:

```powershell
setx DEEPSEEK_API_KEY "sk-..."
setx DEEPSEEK_BASE_URL "https://api.deepseek.com"
termru.exe --help
```

### Debian / Ubuntu (.deb)

```bash
sudo dpkg -i termru_0.2.0_amd64.deb
sudo apt-get install -f     # если не хватает зависимостей
termru --help
```

Пакет кладёт приложение в `/opt/termru`, ссылку `/usr/local/bin/termru`,
`.desktop`-файл и иконку в `/usr/share`.

### Arch Linux

```bash
packaging/build-arch.sh                 # соберёт onefile-бинарник и tar.gz
cd packaging/arch && makepkg -si        # установит termru-bin
termru --help
```

### Из исходников

```bash
cd termru
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m src.cli --help
```

Для работы через DeepSeek API заполните `.env`:

```dotenv
DEEPSEEK_API_KEY=<ваш ключ>
DEEPSEEK_BASE_URL=https://api.deepseek.com
```


### PDF и кириллица

Блоки, набранные base-14 шрифтами (Helvetica, Times, Courier), не умеют
кириллицу: такой шрифт — Type1 без встроенного файла, его нельзя расширить.
TermRu собирает собственный Type0/Identity-H subset из TTF и пишет текст тем
же кеглем в тот же bbox.

**Шрифт DejaVu включён в дистрибутив** — `data/fonts/` (Regular, Bold, Oblique,
BoldOblique и `LICENSE-DejaVu.txt`, лицензия Bitstream Vera). Системный шрифт
используется, если доступен, иначе — bundled; порядок поиска «bundled →
системный» даёт одинаковый результат на Linux, Windows и macOS. Устанавливать
`fonts-dejavu` не требуется.

```bash
# своя папка со шрифтами (переопределяет bundled)
python -m src.cli --input report.pdf --pdf-font-dir ./fonts

# если шрифта нет вообще: fail — ошибка по блоку (по умолчанию),
# polydoc — пересборка через polydoc (вёрстка теряется)
python -m src.cli --input report.pdf --pdf-fallback polydoc
```

**Ограничения (замерено на реальных документах, `docs/pdf-hardening-report.md`).**
Кириллица записывается, если блок набран base-14-совместимым шрифтом (Helvetica, Times, Courier,
Arial, Times New Roman — в том числе встроенным сабсетом `ABCDEF+…`) или если шрифт документа уже
содержит нужные глифы (Type0/Identity-H). Встроенные сабсеты нестандартных семейств — LaTeX/TeX
(`F1`, `F55`, `CMR10`) и LibreOffice (`LiberationSerif`) — расширить нельзя: замена по такому блоку
попадает в `errors`, файл при этом не портится. Подмена всегда меняет гарнитуру блока на DejaVu.

**Наследование `Tf`.** Генераторы вроде reportlab задают шрифт отдельным `BT` и не ставят `Tf` в
каждый текстовый блок: после подмены такой блок унаследовал бы CID-шрифт и показал мусор. TermRu
возвращает `Tf` каждому такому блоку, выбирая шрифт по позиции (ближайший блок той же страницы).
Блок без `Tm` остаётся как есть и попадает в предупреждения (`result["warnings"]`).

### Переполнение и fit

Длинная замена переносится внутри блока, и межстрочный интервал задаётся явно: без него
`pdf_edit_engine` сжимает leading до `bbox_height / len(lines)`, когда сдвигать контент ниже
некуда (последний блок страницы), и строки наезжают друг на друга.

| Режим | Что делает | Когда использовать |
|---|---|---|
| `--pdf-line-height 1.2` (по умолчанию) | шаг строки = кегль × 1.2, кегль не меняется | обычные документы |
| `--pdf-line-height 1.5` (любое > 0) | шаг строки = кегль × значение | нестандартный leading исходника |
| `--pdf-fit none` (по умолчанию) | кегль не меняется; если текст не влез — предупреждение `pdf_overflow` | когда важно сохранить кегль |
| `--pdf-fit shrink` + `--pdf-min-font-size 8` | движок уменьшает кегль, пока текст не влезет; ниже минимума правка откатывается на `fit=none` + `pdf_font_too_small` | плотная вёрстка, где лучше мелкий текст, чем наложение |

Переполнение не подавляется: в `--json` появляются `warnings` с `{"kind": "pdf_overflow",
"block_id": …, "overflow_pt": …}` и счётчик `pdf_overflow_count`; в stderr — одна строка
`PDF: N блоков с переполнением (детали в --json)`.

### Подмена шрифта (`--pdf-substitute-fonts`)

Если шрифт документа не может закодировать кириллицу, TermRu по умолчанию **отказывает** по этому
блоку (файл не портится). Opt-in режимы подставляют встроенный DejaVu:

| Режим | base-14 | LibreOffice / LaTeX | Вёрстка |
|---|---|---|---|
| `strict` (по умолчанию) | работает | отказ, файл цел | 100 % |
| `base14` | работает | простые шрифты (`/Type1`, `/TrueType`): работает, DejaVu вместо Liberation | гарнитура блока меняется; переносы держит `--pdf-line-height` |
| `always` | работает | всё, включая CID-сабсеты (LaTeX `Type0`): DejaVu везде | то же |

```bash
# LibreOffice/Word-экспорт с TrueType-сабсетами без кириллицы
termru --input report.pdf --pdf-substitute-fonts base14

# LaTeX (pdfTeX/dvipdfmx): CID-сабсет без кириллических глифов
termru --input paper.pdf --pdf-substitute-fonts always
```

`--no-pdf-substitute` — алиас `strict` (несовместим с `base14`/`always`, exit 2). До правок
считается preflight: `pdf_preflight: {"substitutable": N, "total": M, "mode": …}` в `--json` и
одна строка INFO в stderr. Каждая подмена попадает в `warnings` как
`{"kind": "pdf_font_substituted", "block_id", "original_font", "reason"}`.

**Рекомендация.** По умолчанию — `strict`. Для LibreOffice/LaTeX используйте `base14`; `always` —
только если понимаете, что гарнитура блока будет заменена на DejaVu.

## Безопасность / Prompt injection

Документ — это недоверенный ввод: в тексте могут быть конструкции вида `EXCEPTIONS:`, «Ignore
previous instructions», ссылки на `system prompt` или длинные base64-блоки. Работают два независимых
механизма:

| Механизм | Что делает | Флаг |
|---|---|---|
| Санитизация (всегда) | разрывает маркеры ZWSP (`E\u200bXCEPTIONS:`), заменяет «ignore … instructions» на `[удалено]`, не трогая код в backticks; факт попадает в `warnings` | `--no-sanitize` отключает |
| Пропуск подозрительных (`--skip-suspicious`) | блок с признаками инъекции **не отправляется в LLM** — только словарная обработка | по умолчанию выключен |

```bash
# чужие документы: не отправляем подозрительные блоки в LLM
termru --input report.docx --use-llm --skip-suspicious --json
```

В `--json` такие блоки перечислены в `skipped_for_llm` (идентификаторы), а в `warnings` — запись
`{"kind": "llm_skipped_suspicious", "block_id": N, "reasons": [...]}`. С `--no-sanitize` в записи
появляется `"sanitize_disabled": true`: блок не идёт в LLM и не санитизируется.

Признаки, по которым блок пропускается: маркеры `EXCEPTIONS:`/`GLOSSARY:`/`Текст:` в начале строки,
`ignore (all) previous/above instructions`, упоминания `system prompt`/`instructions above`, ролевые
конструкции (`You are a …`, `Act as …`, `Pretend to be …`), base64-подобная строка длиннее 100
символов, упоминания `API key`/`password`/`secret`.

**Это не полноценная защита, а снижение риска.** Пропуск не мешает модели увидеть остальной
документ, а санитизация — эвристика; для документов из недоверенных источников дополнительно
ограничивайте права и проверяйте результат.

## Структура проекта

```
termru/
├── .env                     # локальные переменные окружения (в git не попадает)
├── requirements.txt
├── src/
│   ├── cli.py               # CLI на argparse (этап 5)
│   ├── document_processor.py# чтение/запись DOCX, PPTX, PDF (этап 1)
│   ├── glossary_manager.py  # глоссарий и исключения (этап 2)
│   ├── deepseek_client.py   # клиент DeepSeek API (этап 3)
│   └── replacement_engine.py# движок замены (этап 4)
├── data/
│   ├── glossary.json        # англицизм → русский аналог
│   └── exceptions.json      # аббревиатуры, которые не заменяются
├── prompts/
│   └── system_prompt.txt    # системный промпт (этап 3)
└── tests/
    └── fixtures/            # тестовые документы
```

## Использование

```bash
# Записать результат (по умолчанию в report_ru.docx)
python -m src.cli --input report.docx

# Машиночитаемая квитанция для CI: одна строка JSON в stdout
python -m src.cli --input report.docx --json > receipt.json

# Только отчёт без записи файла (JSON в stdout)
python -m src.cli --input report.docx --dry-run

# Тихий режим: только WARN/ERROR в stderr
python -m src.cli --input report.docx --quiet

# Подробный лог (DEBUG) — несовместим с --quiet
python -m src.cli --input report.docx --verbose

# Включить DeepSeek для блоков, где остались англицизмы.
# --batch-size необязателен: по умолчанию max(1, min(20, блоков // 20))
python -m src.cli --input report.docx --use-llm --batch-size 5

# Оценить стоимость до запросов (ничего не отправляет и не пишет файл)
python -m src.cli --input report.docx --use-llm --estimate-cost
# → {"estimated_input_tokens": 4976, "estimated_batches": 12}

# LLM-режим обезвреживает prompt-injection из текста документа: маркеры
# EXCEPTIONS:/GLOSSARY:/Текст: разрываются zero-width space, а «ignore previous
# instructions» заменяется на [удалено]; код в `...` и ```...``` не трогается.
# --no-sanitize отключает это (только для отладки)
python -m src.cli --input report.docx --use-llm --no-sanitize
```

`--json` печатает в stdout одну строку:

```json
{"status":"ok","output":"/abs/report_ru.docx","input":"/abs/report.docx",
 "summary":{"total_blocks":6,"changed_blocks":4,"dict_replacements":7,
            "llm_replacements":0,"duration_ms":142,"llm":null},
 "changes":[{"block_id":1,"original":"feedback","replacement":"обратная связь",
             "source":"dict","position":4}],
 "warnings":[]}
```

С `--use-llm` в `summary.llm` появляется расход токенов:

```json
"llm":{"prompt_tokens":2742,"completion_tokens":114,
       "cache_hit_tokens":1280,"cache_miss_tokens":1462,
       "cache_hit_ratio":0.467}
```

`cache_hit_ratio` округляется до 3 знаков; если API не вернул cache-поля
(старые прокси, `deepseek-reasoner`) — `null`, а не `0`.

### Примеры

```bash
# 1. Словарный режим: быстро, без LLM и сети
termru --input report.docx --output report_ru.docx --json

# 2. LLM + PDF с подменой шрифта + защита от prompt injection
termru --input report.pdf --output report_ru.pdf \
       --use-llm --pdf-substitute-fonts=base14 \
       --skip-suspicious --json

# 3. Dry-run и оценка стоимости запросов
termru --input doc.docx --use-llm --estimate-cost
```

При ошибке: exit=1 и `{"status":"error","error":"...","errors":[...]}`.
Поле `position` (индекс в исходном тексте блока) есть только при `--json`;
обычный отчёт его не содержит.

Коды возврата: `0` — успех, `1` — ошибка обработки, `2` — невалидные аргументы.
Логи идут в `stderr` через `logging`: по умолчанию INFO, `--verbose` → DEBUG,
`--quiet` → только WARNING и ERROR. `--json` не подавляется `--quiet` — это
данные, а не лог.

## Известные ограничения

**PDF**

* В режиме `strict` (по умолчанию) не-base-14 шрифты не поддерживаются: LaTeX (pdfTeX, dvipdfmx) и
  LibreOffice получают отказ по блоку, файл при этом не портится. Решение —
  `--pdf-substitute-fonts=base14` (простые `/Type1` и `/TrueType`).
* Сабсет `Type0` без кириллических глифов (типичный LaTeX) движок пытается расширить сам и терпит
  неудачу — помогает только `--pdf-substitute-fonts=always`.
* Подмена всегда **меняет гарнитуру** блока на DejaVu: метрики шире исходных, текст может
  занимать больше места. Межстрочный интервал фиксируется (`--pdf-line-height`), поэтому строки не
  наезжают, но блок может выйти за исходный bbox — это видно как `pdf_overflow`.
* Визуальная проверка итоговой вёрстки на сложных документах остаётся за пользователем: тесты
  сравнивают bounding box строк, а не пиксели.

**LLM**

* Детектор prompt injection (`--skip-suspicious`) — **эвристика**, а не защита: он снижает риск,
  но не гарантирует, что модель не увидит опасный текст в других блоках.
* `deepseek-reasoner` не поддерживается: клиент передаёт `temperature`, который эта модель не
  принимает.

## GUI (PySide6)

Графический редактор: `python -m src.gui.main`.

```bash
python -m src.gui.main            # тёмная тема, меню, панель инструментов
```

* `src/gui/main.py` — точка входа: `QApplication`, высокий DPI, тема qdarkstyle;
* `src/gui/main_window.py` — окно: две вкладки (Word / PowerPoint), меню
  (Файл/Правка/Инструменты/Справка), две панели (файлы + форматирование), статус-бар
  (путь + счётчики), диалог сохранения при закрытии;
* `src/gui/docx_editor.py` — редактор DOCX: DOCX → HTML (mammoth) → `QTextEdit` → DOCX
  (html2docx), флаг несохранённых изменений;
* `src/gui/pptx_editor.py` — редактор PPTX: список слайдов слева, текстовые блоки справа;
* `src/gui/rusify.py` — `RusifyWorker` (QThread) и `ReplacementsDialog` (предпросмотр замен);
* `src/gui/settings.py` — настройки DeepSeek в `QSettings`.

**Вкладки**. «Файл → Открыть» подстраивает фильтр под активную вкладку, а `MainWindow.open_path()`
выбирает вкладку по расширению (`.docx` / `.pptx`). На вкладке PowerPoint панель
форматирования скрыта, а пункт меню называется «Русифицировать слайд».

**PowerPoint (PPTX)** — текстовый редактор слайдов, без рендеринга страниц:

* слева список слайдов: «Слайд 3», а при наличии заголовка — «Слайд 2: Product feedback»;
* справа по одному `QTextEdit` на текстовый блок выбранного слайда; подписи — из типа
  места-заполнителя и имени фигуры («Заголовок · Title 1», «Содержимое · Content Placeholder 2»),
  ячейки таблиц адресуются как «Table 1 · R1C2»;
* текст пишется обратно с сохранением структуры абзацев и формата первого run в каждом
  абзаце (жирный, курсив, размер, цвет, маркеры и отступы абзаца); мягкий перевод строки
  (`a:br`) показывается как обычный;
* правка переносится в фигуры при смене слайда и при сохранении, поэтому переключение
  слайдов не теряет введённый текст;
* пустой слайд показывает «Нет текстовых блоков», битый файл — диалог ошибки (открытая
  презентация при этом остаётся на месте).

Ограничения PPTX: не редактируются заметки докладчика, диаграммы, рисунки и порядок
слайдов; оформление макета/темы не меняется — правится только текст.

**Форматирование** (вторая панель): жирный/курсив/подчёркивание (Ctrl+B/I/U), выравнивание
влево/по центру/вправо, маркированный и нумерованный списки, вставка таблицы. Состояние кнопок
синхронизируется с форматом под курсором.

**Русификация**: «Инструменты → Русифицировать документ» запускает обработку в фоновом потоке
(окно не замирает, виден прогресс и редактор заблокирован). Затем открывается предпросмотр
«Оригинал / Замена / Источник / Применить» — ненужные замены снимаются галочками, применение
идёт через `QTextCursor`, поэтому жирный/курсив внутри заменяемого фрагмента сохраняются.

**Режимы русификации**:

| Режим | Что делает | Как включить |
|---|---|---|
| Словарь | замены по `data/glossary.json` | по умолчанию |
| ИИ + словарь | словарь, затем DeepSeek по абзацам; правки ИИ показываются как diff-пары в таблице | «Инструменты → Настройки…»: ввести ключ и включить «Использовать ИИ» |

Ключ хранится в `QSettings` (`~/.config/TermRu/Editor.conf` на Linux), без ключа чекбокс ИИ
недоступен. Кнопка «Проверить ключ» делает тестовый запрос и показывает результат; ошибки API
(`DeepSeekAPIError`) превращаются в сообщение, приложение не падает.



Библиотеки: **PySide6-Essentials**, **mammoth**, **html2docx**, **python-pptx**, **qdarkstyle**.
Заявленные в задании `docwow` и `qdarktheme` не используются: их версии ограничены
Python < 3.11 и < 3.12 соответственно, а проект работает на 3.13.

Ограничения конвертации (проверены тестами `tests/test_gui_smoke.py`): для DOCX сохраняются
таблицы, жирный/курсив/подчёркивание и заголовки; сложные элементы Word (колонтитулы,
рисунки, стили абзацев сверх Heading) при round-trip не гарантируются. Для PPTX проверены
открытие (список слайдов, блоки, табличные ячейки, пустой слайд, битый файл), сохранение
правки, сохранение жирного начертания и путь русификации слайда.

## Статус

Журнал работ (этапы 1–20 закрыты, текущая версия — `v0.2.0-mvp`):

| № | Этап | Итог |
|---|---|---|
| 1 | Парсинг/запись DOCX, PPTX, PDF | `document_processor`, сохранение runs и стилей |
| 2 | Глоссарий и исключения | `glossary_manager`, `data/*.json` |
| 3 | Клиент DeepSeek | `deepseek_client`: retry, валидация ответа, учёт токенов |
| 4 | Движок замены | `replacement_engine`: словарь + LLM-доработка |
| 5 | CLI | `argparse`, батчинг, отчёт |
| 6 | Упаковка | unifypy: `.exe`, `.deb`, AUR (`termru-bin`) |
| 7 | Машиночитаемая квитанция | `--json` одной строкой, `--quiet`, exit-коды 0/1/2 |
| 8 | Prompt caching | стабильный системный префикс, `cache_hit` со 2-го батча |
| 9 | Санитизация | ZWSP в маркерах, `[удалено]` для «ignore … instructions» |
| 10 | PDF: кириллица | свой Type0/Identity-H subset вместо base-14 |
| 11 | Git-репозиторий | `.gitignore`, тесты `test_git_repo.sh` |
| 12 | Шрифты в дистрибутиве | DejaVu в `data/fonts`, работает без системных шрифтов |
| 13 | Tf-ремонт | `страница → {block_id → Tf}`, выбор по позиции, предупреждения |
| 14 | Real-world harness | 6 генераторов PDF, fitz-bbox проверки, отчёт `docs/pdf-hardening-report.md` |
| 15 | Тишина в stderr | подавление внутренних трейсбеков `pdf_edit_engine` |
| 16 | Размер бандла | 70.5 → 48.7 MiB (−30.8 %), `docs/build-size-report.md` |
| 17 | PDF reflow + подмена шрифта | `--pdf-line-height`, `--pdf-fit`, `--pdf-substitute-fonts` |
| 18 | Защита от injection | `--skip-suspicious`: подозрительные блоки не идут в LLM |
| 19 | Cache-метрики в `--json` | `summary.llm` с токенами и `cache_hit_ratio` |
| 20 | Финализация | тег `v0.2.0-mvp`, README, **production-ready MVP** |

GUI (этапы 1–4): редактор DOCX в PySide6, панель форматирования, настройки DeepSeek с
diff-предпросмотром замен, вкладка PowerPoint с текстовым редактором слайдов.

Тесты: `pytest tests/ -q` (204), `bash tests/test_deb_desktop.sh` (15), `bash tests/test_git_repo.sh` (7).

## Сборка дистрибутивов

Конфигурация — `unifypy.json`, хуки PyInstaller — `packaging/hooks/`
(они собирают подмодули `polydoc`, который регистрирует readers/writers
динамически). Точка входа для сборки — `termru_entry.py`: PyInstaller запускает
скрипт без пакета, поэтому относительные импорты `src/cli.py` в нём не работают.

Размер `.deb` — **~49 МиБ** (см. `docs/build-size-report.md`). Из сборки исключены
`numpy`/`onnxruntime`/`pymupdf`-layout, `pypdfium2`/`pdfplumber` и форматы polydoc, которые CLI
не поддерживает (`txt/markdown/html/json/csv/xlsx`). Если добавляете формат в
`SUPPORTED_FORMATS`, не забудьте убрать его модуль из `exclude_module` — иначе он попадёт в
`unavailable` и не откроется; `tests/test_packaged_binary.py` проверяет DOCX/PPTX/PDF из
собранного бинарника.

```bash
# Ubuntu/Debian: .deb (нужны dpkg-deb, dpkg-dev и fakeroot)
PATH=.venv/bin:$PATH python -m unifypy . --config unifypy.json --format deb

# unifypy.json отключает собственный .desktop unifypy (Terminal=false, клик
# ничего не открывает) и вместе с ним иконку — скрипт ставит наш .desktop
# и иконку, затем пересобирает пакет
bash packaging/fix-deb-desktop.sh installer/termru_*.deb dist/termru_*.deb

# либо одной командой: сборка + замена .desktop + копирование в dist/
packaging/build-deb.sh

# Linux onefile-бинарник (используется пакетом для Arch)
python -m unifypy . --config unifypy.json --onefile --skip-installer

# Arch-пакет: tar.gz + PKGBUILD → makepkg (свой .desktop ставится напрямую)
packaging/build-arch.sh
```

Скрипты удаляют `termru.spec` перед сборкой: unifypy кеширует spec по конфигу и
не учитывает `--format`/`--onefile`, из-за чего после onefile-сборки deb-этап
падает на «PyInstaller输出文件不存在».

Windows `.exe` **нельзя собрать на Linux**: PyInstaller не поддерживает
кросс-компиляцию, и unifypy для цели `exe` просто вызывает PyInstaller, который
на Linux выдаёт ELF. Собирайте на Windows или через CI
(`.github/workflows/build.yml`, задача `windows-exe`):

```powershell
pip install -r requirements.txt
python -m unifypy . --config unifypy.json --format exe --onefile
```

## TODO

- [ ] **Настроить remote** (сейчас репозиторий локальный):

  ```bash
  git remote add origin <url>
  git push -u origin main --tags
  ```


