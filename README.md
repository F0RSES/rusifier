# TermRu

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
sudo dpkg -i termru_0.1.0_amd64.deb
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
            "llm_replacements":0,"duration_ms":142},
 "changes":[{"block_id":1,"original":"feedback","replacement":"обратная связь",
             "source":"dict","position":4}],
 "warnings":[]}
```

При ошибке: exit=1 и `{"status":"error","error":"...","errors":[...]}`.
Поле `position` (индекс в исходном тексте блока) есть только при `--json`;
обычный отчёт его не содержит.

Коды возврата: `0` — успех, `1` — ошибка обработки, `2` — невалидные аргументы.
Логи идут в `stderr` через `logging`: по умолчанию INFO, `--verbose` → DEBUG,
`--quiet` → только WARNING и ERROR. `--json` не подавляется `--quiet` — это
данные, а не лог.

## Статус

Реализованы этапы 1–4:

| Модуль | Назначение |
|---|---|
| `src/document_processor.py` | чтение/запись DOCX, PPTX, PDF с сохранением runs и стилей |
| `src/glossary_manager.py` | загрузка и валидация глоссария и исключений |
| `src/replacement_engine.py` | детерминированная словарная замена (LLM-режим — заглушка) |
| `src/deepseek_client.py` | клиент DeepSeek: retry, валидация ответа, учёт токенов |
| `src/cli.py` | `argparse`-интерфейс, батчинг блоков, отчёт |

Тесты: `pytest tests/ -q`.

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


