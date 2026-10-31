# PDF: замена кириллицей в блоках с base-14 шрифтом

Исследование для `pdf_edit_engine 0.2.0` (Python 3.13, Linux). Проверка — `scratch/pdf_font_probe.py`.

## TL;DR

| Подход | Плюсы | Минусы | Вердикт |
|---|---|---|---|
| **A. Внедрить свой Type0/Identity-H** (fontTools subset + pikepdf) и передать имя ресурса в `replace_block(font_name=...)` | Работает: `success=True`, кириллица записана, кегль/позиция сохранены, остальные блоки не перерисовываются | +150 строк своего кода; нужен TTF; нужен пост-ремонт `Tf`; шрифт блока визуально меняется | **Рекомендую** |
| **B. Переиспользовать уже существующий Type0 в документе** | Ноль эмбеддинга, 5 строк | Работает только если в PDF есть Type0 с нужными глифами (в наших — нет) | Дешёвый pre-check |
| **C. Fallback через polydoc (PDF-writer)** | Просто | Вёрстка пересобирается ReportLab'ом — теряется | Крайний случай |

Итог: A реализуемо за **1 день** (включая ремонт и тесты); C как аварийный путь оставить.

## 1. Что на самом деле умеет API (по исходникам)

| Место | Что делает |
|---|---|
| `structural.py:1800` | `replace_block(..., font_name=None, font_size=None, line_height=None, fit="none")` |
| `structural.py:1272` | `if font_name is None: font_name = det_name` — явное имя ресурса уважается |
| `structural.py:1306-1317` | если текст не кодируется, ищет **CID-шрифт среди элементов этого же bbox** и переключается на него |
| `structural.py:1321-1331` | для **не-CID** шрифта расширения не пытается: сразу `success=False`, `"Font cannot encode text and no extensible (Type0/Identity-H) font available in bbox"` |
| `fonts.py:1612` | `extend_subset(pdf, page, font_name, chars, full_font_path=None, ...)` — расширение **CID**-шрифта; `full_font_path` (`fonts.py:1902`) подменяет системный шрифт |
| `fonts.py:843`, `encoding.py:231` | `_parse_existing_tounicode` / `_init_identity_h`: движок строит `unicode → CID` из `/ToUnicode` |
| `encoding.py:691` | `can_encode(text) -> (bool, missing)` — главный гейт записи |
| `system_fonts.py:317` | `find_font(postscript_name)` — поиск системного шрифта по PostScript-имени |

Ключевое: **API создания шрифтового ресурса отсутствует** (`register_font`/`embed_font`/`add_font` не существуют — проверено grep по пакету). `insert_text_block` тоже принимает только имя уже существующего ресурса. Плюс `full_font_path` никем из write-путей не передаётся: расширить base-14 Helvetica нельзя в принципе — это Type1 **без** `/FontDescriptor` и `/FontFile*` (наш случай подтверждён: `Subtype=/Type1, FontDescriptor=False`).

## 2. Что доказано эмпирически

`scratch/pdf_font_probe.py` на PDF с Helvetica:

1. **Baseline** — `success=False`, `font_action=failed`, `glyphs_missing=['о','б','р',…]`.
2. **Simple TrueType** (subset из reportlab, `/TrueType` + `/FontFile2`) — тоже `success=False`: движок расширяет только CID (см. `structural.py:1321`).
3. **Type0/Identity-H** (subset DejaVu + `/CIDToGIDMap /Identity` + `/W` + ToUnicode) → **`success=True`, `font_action=kept`**, в поток записано `[ <0003> <0009> … ] TJ` с 2-байтовыми CID.
4. Кегль сохраняется (`12.0 → 12.0`), левая граница — тоже; при переполнении bbox текст переносится, а контент ниже сдвигается вниз (штатное поведение `replace_block`, предупреждение `Overflow detected`).
5. **Остальные блоки не перерисовываются**, но есть ловушка: `BT`-блок без `Tf` наследует текстовое состояние — после подмены он получит наш CID-шрифт и покажет мусор. Лечится пост-обработкой: вернуть `Tf` в такие блоки (в пробе — 1 блок, после ремонта вторая строка снова `F1` и читается).

Работающий вызов (упрощённо; полный код — в `scratch/pdf_font_probe.py`):

```python
with pikepdf.open(src) as pdf:                    # 1. внедряем ресурс
    pdf.pages[0]['/Resources']['/Font']['/TFDejaVu'] = build_type0(pdf, DEJAVU, new_text)
    pdf.save(step1)
block = pe.get_text_layout(step1)[0]              # 2. bbox исходного блока
bbox = (block.x, block.y, block.x + block.width, block.y + block.height)
res = pe.replace_block(step1, 0, bbox, new_text, step2, font_name='TFDejaVu')
assert res.success                                # 3. True (проверено)
repair_inherited_font(step2, step3, fallback_tf=f'/{block.font_name} {block.font_size:g} Tf')
```

Обязательные детали конструктора: `/W` только в виде `[gid [width] …]` (плоские пары `[gid width]` дают ширину 7 pt вместо 260 pt), ToUnicode — стандартный `beginbfchar`-CMap (его парсит `fonts.py:843`), `/Length1` у `/FontFile2`.

## 3. Шрифты в системе (проверено через fontTools cmap)

| Путь | Семейство | Кириллица | Latin-1 |
|---|---|---|---|
| `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | DejaVu Sans | 66/66 | 159/159 |
| `/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf` | Liberation Sans | 66/66 | 159/159 |
| `/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf` | Noto Sans Mono | 66/66 | 159/159 |
| `/usr/share/fonts/truetype/freefont/FreeSerif.ttf` | FreeSerif | 66/66 | 159/159 |

Замечание: пропорционального **Noto Sans в системе нет** — только `NotoSansMono`. `NimbusSans.otf` (CFF) не подходит: конструктор рассчитан на TrueType-контуры (`/FontFile2`).

## 4. Ограничения и риски

- **Только TTF (`glyf`)**. OTF/CFF требует `/FontFile3` + CIDFontType0 — отдельная ветка, не покрыта.
- **Шрифт блока меняется визуально** (Helvetica → DejaVu). Метрики другие → возможен перенос строки и сдвиг контента ниже. Полностью незаметной подмены не будет, пока не подберём метрический аналог (как Carlito для Calibri — `fonts.py:1847`).
- **Бандл шрифта обязателен для дистрибутива**: на Windows нет DejaVu; кладём `data/fonts/DejaVuSans.ttf` (759 КБ, лицензия Bitstream Vera — permissive) и ищем в порядке: system → bundled.
- **Ремонт `Tf` обязателен** — без него PDF формально валиден, но последующий текст рисуется чужим шрифтом.
- Сканы без текстового слоя и зашифрованные PDF этой правкой не решаются.

## 5. Оценка

1 рабочий день: `src/pdf_font.py` (~150 строк: subset, Type0, ToUnicode, `/W`), `repair_inherited_font` (~40), правка `DocumentProcessor._apply_pdf_engine` (~30: инжект один раз на документ + передача `font_name` + ремонт), тесты на Helvetica-PDF и на PDF с `Tf` в каждом `BT`, бандл шрифта в `packaging`. Fallback на polydoc остаётся для CFF-шрифтов и экзотики.

