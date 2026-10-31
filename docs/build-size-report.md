# Сжатие дистрибутива: 70.5 → 48.7 MiB (-30.8%)

Дата: 31 октября 2026. Область: `unifypy.json`, `termru_entry.py`, сборка `.deb`.
Задача из отчёта 5: «уменьшить можно strip/UPX-настройками и исключением ненужных форматов polydoc».

## TL;DR

| Артефакт | Было | Стало | Δ |
|---|---|---|---|
| `.deb` | 73 874 840 B (**70.5 MiB**) | 51 106 464 B (**48.7 MiB**) | **−30.8 %** |
| Бандл `_internal` (`du -sh`) | 201 M | 86 M | −57 % |
| Бандл без `strip` (контроль) | — | 122 M / 52.0 MiB | — |
| `pytest -q` | 131 passed | **137 passed** (+6 на собранный бинарник) | — |

Критерий «−30 %» выполнен, цель «≤ 50 МБ» достигнута с запасом 1.3 MiB.
Основной вес убрали `numpy` + OpenBLAS (63 МБ) — они тянулись через `onnxruntime`/`pymupdf-layout`,
которые TermRu не использует.

## 1. Откуда были 74 МБ

`du -sh` по `_internal` старого бандла (распакованный `.deb`):

| Компонент | Размер | Кто тянул |
|---|---|---|
| `numpy.libs` | 27 M | `numpy` (OpenBLAS) |
| `libscipy_openblas64_-f48b354e.so` | 24 M | `numpy` 2.x (встроенный BLAS) |
| `pillow.libs` | 15 M | Pillow ← `python-pptx`, `pikepdf` |
| `numpy` | 14 M | `onnxruntime`, `pymupdf-layout` |
| `cryptography` | 14 M | `pdfminer.six` (импорт на верхнем уровне) |
| `fontTools` | 12 M | наш subset CID-шрифта |
| `lxml` | 9.4 M | `python-docx`, `python-pptx`, `pdf_edit_engine` |
| `pdfminer` | 7.9 M | `pdf_edit_engine` (Requires) |
| `pypdfium2_raw` | 7.5 M | `pdfplumber` ← таблицы в polydoc |
| `libpython3.13.so.1.0` | 7.3 M | интерпретатор |
| `pikepdf.libs` / `libqpdf` | 6.8 M / 4.8 M | `pikepdf` |
| `libcrypto.so.3` | 6.4 M | `cryptography` |
| `libavif` | 4.7 M | Pillow (AVIF-плагин) |
| `PIL` | 4.1 M | Pillow |

Итого ≈ 165 МБ из 201 МБ — три «кластера»: **numpy/OpenBLAS (63 МБ)**, Pillow (24 МБ),
cryptography + pdfminer (22 МБ).

## 2. Что проверяли перед исключением

| Проверка | Команда/результат |
|---|---|
| Кто импортирует OCR/ML | `grep -rn 'onnxruntime\|rapidocr\|pytesseract' pdf_edit_engine/ polydoc/` → **ничего** |
| Что реально попадает в `sys.modules` | `import polydoc` → `[]`; `import pdf_edit_engine` → `['pdfminer']`; `import pptx` → `['PIL','lxml']` |
| Кто требует numpy | `pip show numpy` → `Required-by: onnxruntime, pymupdf` |
| Кто требует pypdfium2 | `pip show pypdfium2` → `Required-by: pdfplumber` |
| Критичность `cryptography` | `pdfminer/pdfdocument.py:13` — импорт **без** try/except ⇒ исключать нельзя |
| Критичность Pillow | `python-pptx` импортирует `PIL` на уровне модуля ⇒ исключать нельзя |
| Форматы CLI | `detect_format` допускает только `docx/pptx/pdf` (`SUPPORTED_FORMATS`) |
| Регистрация форматов polydoc | `readers/__init__.py:35` / `writers/__init__.py:32` — `importlib.import_module` внутри `try/except` |

`replace_block` из `pdf_edit_engine` OCR не вызывает: строка `onnxruntime` не встречается
в пакете ни разу, `pdf_edit_engine` требует только `fonttools, lxml, pdfminer-six, pikepdf`.

## 3. Таблица исключений

| Модуль | Зачем он в зависимостях | Нужен для DOCX/PPTX/PDF | Решение | Экономия (несжато) |
|---|---|---|---|---|
| `numpy` | `onnxruntime`, `pymupdf-layout` | нет | исключён | 13.7 M |
| `numpy.libs` | OpenBLAS внутри numpy | нет | исключён | 26.2 M |
| `libscipy_openblas64_` | BLAS внутри numpy | нет | исключён (вместе с numpy) | 23.2 M |
| `onnxruntime` | ML-рантайм OCR | нет | исключён | в бандл не попадал |
| `pymupdf`, `fitz`, `pymupdf_layout`, `pymupdf4llm` | только ленивый PDF-reader polydoc | нет (наш PDF идёт через `pdf_edit_engine`) | исключены | в бандл не попадали |
| `pypdfium2`, `pypdfium2_raw`, `pdfplumber` | распознавание таблиц в polydoc (опция) | нет | исключены | 7.5 M |
| `yaml` | зависимость `pymupdf4llm` | нет | исключён | 2.5 M |
| `polydoc.formats.readers.{plaintext,markdown,html,json_native,delimited,xlsx}` и те же `writers` | регистрируются через `importlib` | нет (только docx/pptx/pdf) | исключены | < 0.1 M |
| `tkinter`, `matplotlib`, `IPython`, `jupyter`, `notebook` | не ставились | нет | исключены | 0 (страховка) |
| `rapidocr_onnxruntime`, `pytesseract` | не ставились | нет | исключены | 0 (страховка) |
| `cryptography` | `pdfminer.pdfdocument` (верхний уровень) | **да** | оставлен | — |
| `Pillow` / `PIL` | `python-pptx`, `pikepdf` | **да** | оставлен | — |
| `lxml` | `docx`, `pptx`, `pdf_edit_engine` | **да** | оставлен | — |
| `pdfminer` | `pdf_edit_engine` | **да** | оставлен | — |
| `fontTools`, `pikepdf` | CID-шрифт, запись PDF | **да** | оставлены | — |

Исключённые форматы polydoc: `plaintext`, `markdown`, `html`, `json_native`, `delimited` (csv),
`xlsx` — и readers, и writers. `docx`, `pptx`, `pdf` не тронуты.

### 3.1 Вклад `strip`

| Сборка | `.deb` | `_internal` |
|---|---|---|
| Без `exclude_module`, без `strip` | 70.5 MiB | 201 M |
| С `exclude_module`, без `strip` | 52.0 MiB | 122 M |
| С `exclude_module` + `strip: true` | **48.7 MiB** | **86 M** |

* `exclude_module` даёт **−18.5 MiB** (70.5 → 52.0);
* `strip: true` — ещё **−3.4 MiB** (52.0 → 48.7): снимаются символы из `.so`
  (`fontTools/*.so` 11.5 → 1.3 M, `cryptography/_rust.abi3.so` 13.8 → 11.0 M, `PIL` 4.0 → 1.0 M,
  `pikepdf.libs` 6.8 → 5.5 M).

**UPX не дал ничего**: он установлен (`/usr/bin/upx` 4.2.4), но PyInstaller его не применил —
`upx -l` на бинарнике и на `.so` отвечает `NotPackedException: not packed by UPX`.
Настройку `noupx` не трогал: экономии нет ни в одном варианте, а UPX на `.so` — лишний риск.

## 4. Побочные эффекты и что с ними сделано

**Шум в stderr.** Исключённые форматы polydoc регистрируются «мягко»: каждый отсутствующий модуль
пишет `RuntimeWarning: polydoc: the 'xlsx' reader is unavailable` — 12 строк при **каждом** запуске
(в том числе при `--help`). Подавлено точечным фильтром в `termru_entry.py` (до импорта `polydoc`):

```python
warnings.filterwarnings(
    "ignore",
    message=r"polydoc: the '(?:txt|markdown|html|json|csv|xlsx)' (?:reader|writer) is unavailable",
    category=RuntimeWarning,
)
```

Фильтр не глобальный (`category=RuntimeWarning` + точный текст сообщения), реальные проблемы
регистрации `docx/pptx/pdf` через него не скрываются — это проверяет
`test_deb_formats_registered`. Было 12 строк в stderr, стало 0.

**`--pdf-fallback polydoc` для PDF.** polydoc-овский PDF-reader лениво импортирует `pymupdf`/`fitz`
(`readers/pdf.py:84`), а в бандле `pymupdf` не было **и до этих изменений** (проверено: в старом
`.deb` нет каталогов `pymupdf*`/`fitz*`). То есть исключение ничего не сломало — путь был недоступен
и раньше. Наш штатный PDF-путь (`pdf_edit_engine` + внедрение CID-шрифта) работает.
Зафиксировано как известное ограничение сборки, а не регресс.

## 5. Проверки после сборки

| Проверка | Результат |
|---|---|
| `pytest -q` | **137 passed** (131 + 6 новых) |
| `test_packaged_binary.py::test_deb_opens_docx` | exit=0, `status="ok"`, `changed_blocks > 0` |
| `test_packaged_binary.py::test_deb_opens_pptx` | exit=0, `status="ok"`, `changed_blocks > 0` |
| `test_packaged_binary.py::test_deb_opens_pdf` | exit=0, кириллица в тексте PDF |
| `test_deb_formats_registered` | `docx/pptx/pdf` не в `unavailable`, шума нет |
| Ручной прогон DOCX / PPTX / PDF | `exit=0` во всех трёх, замены на месте (см. ниже) |
| `strip`/`upx` | бинарник `stripped`; UPX не применён |

Ручной прогон собранного бинарника (`dist/termru/termru`):

```
DOCX: The обратная связь and крайний срок from the встреча.
PPTX: Дорожная карта / Обратная связь and адаптация новых сотрудников
PDF:  The обратная связь from the встреча was
      clear.
```

## 6. Что осталось тяжёлым и почему

Ниже порог 48.7 MiB без потери форматов не пройти: всё перечисленное нужно в рантайме.

| Компонент | Размер | Почему нужен |
|---|---|---|
| `pillow.libs` | 13.6 M | Pillow ← `python-pptx` (картинки в слайдах), `pikepdf` |
| `cryptography` | 11.0 M | `pdfminer` импортирует на верхнем уровне — исключение ломает PDF |
| `lxml` | 9.3 M | `python-docx`, `python-pptx`, `pdf_edit_engine` |
| `pdfminer` | 7.5 M | `pdf_edit_engine` (обязательная зависимость) |
| `libpython3.13.so.1.0` | 7.3 M | интерпретатор |
| `libcrypto.so.3` | 6.4 M | `cryptography` |
| `pikepdf.libs` + `libqpdf` | 5.5 + 3.5 M | запись PDF |
| `libavif` | 4.7 M | Pillow AVIF-плагин (в DOCX/PPTX не встречается) |

Возможные следующие шаги (не делались — вне скоупа задачи):

1. `libavif` + `PIL.AvifImagePlugin` — ~4.7 МБ. Нужно точечно исключить бинарник через
   `hook-PIL`; риск сломать чтение картинок в DOCX/PPTX, поэтому только под тестом
   «документ с PNG/JPEG».
2. `pdfminer` невозможно убрать, но `pdf_edit_engine` использует его частично — без форка
   зависимости не обойтись.
3. UPX на `.so` вручную (`upx --best`), если политика дистрибутива допускает упакованные
   библиотеки; в PyInstaller-пайплайне он не применился.

## 7. Как воспроизвести

```bash
cd termru
bash packaging/build-deb.sh                          # .deb в dist/
.venv/bin/python -m pytest tests/test_packaged_binary.py -v
ls -l dist/*.deb                                     # ~51 МБ
```

Контрольная сборка без `strip` (для сравнения): скопировать `unifypy.json`, выставить
`"strip": false`, удалить `termru.spec` (unifypy переиспользует spec от прошлой сборки и
проигнорирует новый конфиг) и собрать с `--config`.

