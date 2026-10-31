# PDF reflow: почему строки наезжают друг на друга

Исследование к этапу «восстановить читаемую вёрстку после замен». Код не менялся:
`pdf_edit_engine` не патчится, все выводы — из наблюдений на реальных прогонах.

## TL;DR

| Вопрос | Ответ |
|---|---|
| Что видно | `Second line about крайний срок and` и `дорожная карта.` перекрываются на **7.97 pt** (fitz) |
| Где ломается | `pdf_edit_engine/structural.py:1660` — `line_height = available_height / len(lines)` |
| Почему | две строки × 14.4 pt не влезают в bbox высотой 12 pt, а сдвигать контент ниже **некуда** (блок последний на странице) → движок «втискивает» 2 строки в 12 pt, получая leading **6 pt** |
| Ремонт `Tf` не виноват | `Tm` до/после `repair_inherited_font` идентичны (проверено, 2 вставки `Tf`, 0 сдвигов) |
| Рабочий фикс (гипотеза подтверждена) | передавать в `replace_block(..., line_height=font_size*1.2)`: движок держит leading фиксированным → `0 -14.4 Td`, перекрытие **−0.43 pt** |

## 1. Симптом в цифрах

`fitz page.get_text("dict")` по `/tmp/report_ru.pdf`:

```
y0= 113.75 y1= 127.72 | 'The обратная связь from the встреча was'
y0= 128.15 y1= 142.12 | 'clear.'
y0= 150.55 y1= 164.52 | 'Second line about крайний срок and'
y0= 156.55 y1= 170.52 | 'дорожная карта.'
!! НАЛОЖЕНИЕ 7.97 pt
```

`get_text()` при этом возвращает текст целиком — поэтому текстовые тесты дефект не видели.

## 2. Content stream: до и после

Исходный `/tmp/probe_target.pdf` (211 байт) — одна строка = один `BT`, шаг строк задаётся `Tm`,
`TL` объявлен, но текст его не использует:

```
BT /F1 12 Tf 14.4 TL ET
BT 1 0 0 1 72 720 Tm (The feedback from the meeting was clear.) Tj T* ET
BT 1 0 0 1 72 700 Tm (Second line about deadline and roadmap.) Tj T* ET
```

`/tmp/report_ru.pdf` (845 байт) — после замены:

```
BT /TFCyr0 12 Tf 1 0 0 1 72 717 Tm [ <...> ] TJ 0 -14.4 Td [ <...> ] TJ   ← верхняя: шаг 14.4 ✔
BT /TFCyr0 12 Tf 1 0 0 1 72 680.2 Tm [ <...> ] TJ 0 -6 Td  [ <...> ] TJ   ← нижняя:  шаг 6 ✘
```

`Tm` первой строки в обоих блоках на месте, `TL` остался 14.4 — ломается именно относительный
`Td`, который движок пишет для второй (перенесённой) строки.

## 3. Откуда берётся 6 pt

`_build_replacement_ops` вызывается с `line_height=6.0`. Цепочка (`structural.py`):

1. `_detect_line_height(matched_elems, 12.0)` → **14.4** (`font_size * 1.2`, блок однострочный);
2. `bbox_height = bbox[3] - bbox[1] = 12.0`, `text_height = 2 * 14.4 = 28.8`, `overflow_delta = 16.8`;
3. вертикальный сдвиг контента ниже: `below_ys = [e.bbox[1] for e in elements if e.bbox[1] < bbox[1]]` —
   для **последней** строки страницы список пуст → `overflow_delta = 0.0` (structural.py:1627);
4. компрессия: `available_height = bbox_height + 0 = 12.0`; `text_height (28.8) > 12.0` и `len(lines) > 1`
   → `line_height = available_height / len(lines) = 12.0 / 2 = 6.0` (structural.py:1660).

Верхняя строка не пострадала, потому что под ней есть контент: `below_ys` не пуст →
`overflow_delta = 16.8` остаётся → `available_height = 28.8` → компрессии нет, leading 14.4,
а контент ниже сдвигается.

Итог: **дефект проявляется на самом нижнем текстовом блоке страницы**, где сдвигать вниз нечего.
Движок при этом честно возвращает `warning: "Overflow detected…"` — мы его видим и уже логируем.

## 4. Ремонт `Tf` не при чём

`repair_inherited_font` вставляет только оператор `Tf` после `BT`; `Tm`/`Td` не трогает:

```
вставлено Tf: 2 | проблемы: []
Tm до ремонта : [('72','697'), ('72','677.6'), ('72','657.6')]
Tm после      : [('72','697'), ('72','677.6'), ('72','657.6')]   идентичны
BT без Tf: 2 → 0
```

## 5. Проверенный фикс (гипотеза, ещё не в коде)

Явный `line_height` переключает `caller_line_height = True` → ветка компрессии пропускается:

```python
replace_block(src, page, bbox, text, dst, font_name="TFCyr0", line_height=12.0 * 1.2)
```

Результат на том же дефектном блоке:

```
BT /TFCyr0 12 Tf 1 0 0 1 72 697 Tm [ ... ] TJ 0 -14.4 Td [ ... ] TJ
y0=133.75 y1=147.72 | 'Second line about крайний срок and'
y0=148.15 y1=162.12 | 'дорожная карта.'
перекрытие = -0.43 pt   (строки не пересекаются)
```

Цена: текст может выйти за нижнюю границу bbox (`Overflow detected` остаётся в warnings), но если
ниже действительно пусто — визуально это пустое место, а не наложение. Если ниже есть контент,
сработает штатный сдвиг вниз (как у верхней строки).

### 5.1 Пиксельная проверка рендера (вместо скриншота)

Рендер области `Rect(50, 90, 400, 190)` при 140 dpi, поиск горизонтальных полос текста
(ряд пикселей содержит тёмную точку):

| Файл | Полосы (y-диапазоны) | Зазоры, px |
|---|---|---|
| `/tmp/report_ru.pdf` (дефект) | `(50,72) (78,96) **(121,155)**` | 5, — |
| `/tmp/fix_try.pdf` (фикс) | `(45,66) (89,111) (122,139)` | **22**, 10 |

В дефектном файле две нижние строки слиты в **одну полосу высотой 35 px** (обычная строка — 19–23 px):
между ними нет ни одного пустого ряда пикселей. С явным `line_height` каждая строка — отдельная
полоса, зазор 10 px. Внешний vision-сервис для скриншота недоступен (401), поэтому метрика
считается локально по пикселям — она строже визуальной оценки «на глаз».

## 6. Что предлагается сделать (этап 2)

1. `DocumentProcessor._replace_pdf_block(...)` передаёт `line_height=self._pdf_line_height * font_size`
   (по умолчанию 1.2); значение берётся из `block.font_size`, фолбэк — `block.height`.
2. CLI: `--pdf-line-height FLOAT` (по умолчанию 1.2) для документов с нестандартным leading.
3. Тесты через fitz (`bbox` строк), а не `get_text()`:
   `test_pdf_lines_do_not_overlap` (3 генератора), `test_reflow_leading_applied`,
   `test_no_overlap_on_real_pdfs`, `test_repair_inherited_font_does_not_shift_tm`.

Открытые вопросы к подтверждению:

* оставлять `warning: Overflow detected` как есть (он уже логируется) или поднимать его в
  `result["warnings"]`, чтобы пользователь знал, что блок «вылез» за bbox;
* нужно ли уменьшать кегль (`fit="shrink"`), когда текст всё равно не влезает в ширину — это
  отдельное поведение (`font_size_reduced`), по умолчанию не включаем.

## 7. Реализация (этап 2)

Решения по открытым вопросам: overflow **поднимается** в `result["warnings"]` структурной записью
(без порога, одна INFO-строка в stderr на документ); `fit="shrink"` — только opt-in.

### 7.1 Изменения

| Место | Что сделано |
|---|---|
| `DocumentProcessor._block_line_height` | `font_size * pdf_line_height` (по умолчанию 1.2) → `block.height` → `None` |
| `DocumentProcessor._call_replace_block` | единая обёртка над `replace_block(..., line_height=…, fit=…)` |
| `DocumentProcessor._collect_overflow` | `fidelity_report.overflow_detected` / `warnings` → `{"kind": "pdf_overflow", "block_id", "overflow_pt", "message"}` |
| `DocumentProcessor._applied_font_size` | кегль из `Degradation(kind="font_size_reduced", detail="applied=…")` |
| откат shrink | если `applied < pdf_min_font_size` → повтор с `fit="none"` + `{"kind": "pdf_font_too_small", "requested_min", "actual"}` |
| `DocumentProcessor.warnings` | свойство: `list[dict]` последнего прогона (это же в `result["warnings"]`) |
| CLI | `--pdf-line-height` (1.2), `--pdf-fit {none,shrink}`, `--pdf-min-font-size` (8.0); `<= 0` → exit 2 |
| CLI `--json` | `pdf_overflow_count` + структурные warnings в общем списке |

**`fit="shrink"` поддерживается движком нативно** (`structural.py:1810`,
`fit: Literal["none", "shrink"]`). Свой цикл уменьшения кегля не писали: движок сам бинарным
поиском подбирает кегль с floor `max(4.0, original * 0.5)`; наш код проверяет фактический кегль и
откатывает правку, если он ниже пользовательского минимума.

### 7.2 Проверка `report_ru.pdf`

| Метрика | До | После |
|---|---|---|
| Максимальное перекрытие строк (fitz) | **+7.97 pt** | **−0.43 pt** |
| Полосы рендера (140 dpi, x 50–400) | `(50,72) (78,96) **(121,155)**` — две строки слиты | `(50,72) (78,96) (121,143) (154,171)` |
| Зазоры между полосами, px | 5, — | 5, 24, **10** |
| `Td` перенесённой строки | `0 -6 Td` | `0 -14.4 Td` |
| `pdf_overflow_count` | — (поля не было) | 2 |

### 7.3 Тесты

`tests/test_pdf_reflow.py`, 14 тестов (проверяют bbox через fitz, а не `get_text`):
`test_pdf_lines_do_not_overlap` (reportlab / PyMuPDF / две колонки), `test_reflow_leading_applied`,
`test_no_overlap_on_real_pdfs`, `test_repair_inherited_font_does_not_shift_tm`,
`test_pdf_fit_shrink_respects_min_font_size`, `test_pdf_fit_shrink_success`,
`test_pdf_line_height_override`, `test_default_line_height_is_1_2`,
`test_overflow_warning_in_json`, `test_overflow_count_in_json`,
`test_pdf_min_font_size_must_be_positive`, `test_pdf_line_height_flag_reaches_processor`.

Две особенности проверки, найденные на реальных документах:

1. сравнивать строки нужно только при пересечении **по x** — иначе две колонки на одной базовой
   линии дают ложное «наложение»;
2. в реальных PDF уже есть исходные перекрытия bbox (заголовок 60 pt и подзаголовок), поэтому
   проверяются пары вокруг **заменённых** строк, а не все строки документа.

