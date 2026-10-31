"""Real-world harness: PDF от разных инструментов — замена на кириллицу.

Каждый кейс генерируется программно при прогоне в ``tests/fixtures/pdf/``
(каталог в git не попадает, см. ``.gitignore``):

* (a) reportlab — ``Tf`` задаётся отдельным ``BT``, текст без ``Tf``;
* (b) PyMuPDF — ``Tf`` в каждом ``BT``, текст в hex-TJ;
* (c) две колонки — замена в левой и правой, порядок чтения сохраняется;
* (d) смешанные гарнитуры (Helvetica + Times) на одной странице;
* (e) готовый Type0/Identity-H с кириллицей — подмены быть не должно;
* (f) пять страниц — замена на каждой.

Для всех кейсов проверяется: ``success``, ``replaced_count``, кириллица в
извлечённом тексте, сохранность незамененных строк, неизменное число текстовых
блоков и структурная целостность результата (``pikepdf``).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pikepdf
import pymupdf
import pytest
from fontTools.ttLib import TTFont
from reportlab.pdfgen import canvas

import pdf_edit_engine as pe
from src.document_processor import DocumentProcessor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "pdf"
DEJAVU = PROJECT_ROOT / "data" / "fonts" / "DejaVuSans.ttf"

#: ``/Name 12 Tf`` — оператор выбора шрифта.
_TF_RE = re.compile(r"/([A-Za-z0-9+.\-]+)\s+([\d.]+)\s+Tf")
#: Последняя пара чисел перед ``Tm`` — это ``e`` (x) и ``f`` (y).
_TM_RE = re.compile(r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+Tm")
_TEXT_OP_RE = re.compile(r"\bTj\b|\bTJ\b")
#: Пары ``<CID> <unicode...>`` из ToUnicode-CMap.
_BFCHAR_RE = re.compile(r"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]+)>")
#: Глифы, реально использованные в content stream: ``<000b>`` внутри ``TJ``.
_HEX_GLYPH_RE = re.compile(r"<([0-9A-Fa-f]{4})>")


# --------------------------------------------------------------------------- #
# Генераторы PDF (кейсы a-f)
# --------------------------------------------------------------------------- #
def _build_reportlab(path: Path) -> Path:
    """reportlab: ``Tf`` в отдельном ``BT``, строки текста — без ``Tf``."""
    doc = canvas.Canvas(str(path))
    doc.setFont("Helvetica", 12)
    doc.drawString(72, 700, "The feedback from the meeting was clear.")
    doc.drawString(72, 680, "Second line about deadline and roadmap.")
    doc.setFont("Times-Roman", 12)
    doc.drawString(72, 640, "Times line about the roadmap and deadline.")
    doc.save()
    return path


def _build_pymupdf(path: Path) -> Path:
    """PyMuPDF: ``Tf`` в каждом ``BT``, текст упакован в hex-TJ."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "The feedback from the meeting was clear.", fontsize=12)
    page.insert_text((72, 130), "Second line about deadline and roadmap.", fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


def _build_two_columns(path: Path) -> Path:
    """Две колонки по три строки; ``first`` есть в обеих."""
    doc = pymupdf.open()
    page = doc.new_page()
    left = ("Left column first line.", "Left column second line.", "Left column third line.")
    right = ("Right column first line.", "Right column second line.", "Right column third line.")
    for index, (left_line, right_line) in enumerate(zip(left, right)):
        page.insert_text((72, 100 + index * 20), left_line, fontsize=11)
        page.insert_text((330, 100 + index * 20), right_line, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _build_mixed_fonts(path: Path) -> Path:
    """Helvetica и Times-Roman на одной странице (кейс регрессии Tf-ремонта)."""
    doc = canvas.Canvas(str(path))
    doc.setFont("Helvetica", 12)
    doc.drawString(72, 700, "The feedback from the meeting was clear.")
    doc.drawString(72, 680, "Second helvetica line stays untouched.")
    doc.setFont("Times-Roman", 12)
    doc.drawString(72, 500, "The deadline and roadmap are clear.")
    doc.drawString(72, 480, "Second times line stays untouched.")
    doc.save()
    return path


def _build_existing_type0(path: Path) -> Path:
    """Готовый Type0/Identity-H (PyMuPDF + TTF) с полным покрытием кириллицы."""
    doc = pymupdf.open()
    page = doc.new_page()
    for y, text in (
        (100, "The feedback from the meeting was clear."),
        (130, "Second line about deadline and roadmap."),
    ):
        page.insert_text(
            (72, y), text, fontsize=12, fontname="DejaVuX", fontfile=str(DEJAVU)
        )
    doc.save(str(path))
    doc.close()
    return path


def _build_five_pages(path: Path) -> Path:
    """Пять страниц, на каждой — строка с ``feedback``."""
    doc = pymupdf.open()
    for number in range(1, 6):
        page = doc.new_page()
        page.insert_text(
            (72, 100), f"Page {number}: the feedback from the meeting was clear.", fontsize=12
        )
        page.insert_text((72, 130), f"Page {number}: second line about deadline.", fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def _fixture(name: str) -> Path:
    """Путь к PDF-фикстуре кейса (каталог создаётся при необходимости)."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    return FIXTURE_DIR / f"{name}.pdf"


def _text_blocks(path: Path) -> list:
    """Непустые блоки в порядке ``block_id`` — так же, как их видит обработчик."""
    return [block for block in pe.get_text_layout(str(path)) if block.text.strip()]


def _plan(path: Path, needle: str, replacement: str) -> dict[int, str]:
    """Замены ``{block_id: новый текст}`` для блоков, содержащих ``needle``."""
    plan: dict[int, str] = {}
    for block_id, block in enumerate(_text_blocks(path)):
        if needle in block.text:
            plan[block_id] = block.text.replace(needle, replacement)
    return plan


def _page_text(path: Path) -> str:
    """Текст всех страниц через PyMuPDF (как его увидит читатель)."""
    with pymupdf.open(str(path)) as doc:
        return "\n".join(page.get_text() for page in doc)


def _normalized(text: str) -> str:
    """Текст без различий в пробелах и переводах строк."""
    return " ".join(text.split())


def _page_fonts(path: Path, index: int = 0) -> dict[str, str]:
    """``{имя ресурса: подтип}`` из ``/Resources /Font`` страницы."""
    with pikepdf.open(str(path)) as pdf:
        fonts = pdf.pages[index].get("/Resources", {}).get("/Font", {})
        return {str(name): str(font.get("/Subtype")) for name, font in fonts.items()}


def _page_stream(path: Path, index: int = 0) -> str:
    """Декодированный content stream страницы (массив склеивается)."""
    with pikepdf.open(str(path)) as pdf:
        contents = pdf.pages[index].get("/Contents")
        if contents is None:
            return ""
        if isinstance(contents, pikepdf.Array):
            raw = b"\n".join(stream.read_bytes() for stream in contents)
        else:
            raw = contents.read_bytes()
    return raw.decode("latin-1")


def _bt_rows(path: Path, index: int = 0) -> list[dict]:
    """``BT``-блоки страницы с фактически действующим шрифтом.

    Шрифт берётся с учётом наследования текстового состояния: если у блока нет
    своего ``Tf``, действует последний ``Tf`` из предыдущих блоков — ровно так
    поток читает просмотрщик PDF.

    Args:
        path: PDF-файл.
        index: Номер страницы (0-based).

    Returns:
        list[dict]: ``{"y": float, "font": str, "body": str}`` по блокам с
        текстовыми операциями.
    """
    rows: list[dict] = []
    current = ""
    for body in re.findall(r"BT(.*?)ET", _page_stream(path, index), re.S):
        for match in _TF_RE.finditer(body):
            current = match.group(1)
        if not _TEXT_OP_RE.search(body):
            continue
        positions = _TM_RE.findall(body)
        y = float(positions[-1][1]) if positions else float("nan")
        rows.append({"y": y, "font": current, "body": body})
    return rows


def _save_roundtrip(path: Path) -> None:
    """Полная пересборка файла в память: проверка связности объектного графа."""
    with pikepdf.open(str(path)) as pdf:
        buffer = io.BytesIO()
        pdf.save(buffer)
    assert buffer.tell() > 0


def _assert_fonts_kept(source: Path, output: Path, expected: dict[str, str]) -> None:
    """Каждый незамененный блок сохранил свой исходный шрифт.

    Args:
        source: Исходный PDF (для исходных имён шрифтов).
        output: Результат обработки.
        expected: ``{фрагмент текста: имя исходного шрифта}`` — ожидаемая
            привязка «текст → шрифт» (``font_name`` из ``get_text_layout``).
    """
    before = {block.text: block.font_name for block in _text_blocks(source)}
    after = {block.text: block.font_name for block in _text_blocks(output)}
    for fragment, expected_font in expected.items():
        assert before[fragment] == expected_font, (
            f"исходный шрифт {fragment!r} — {before[fragment]!r}"
        )
        assert fragment in after, f"потерян блок {fragment!r}"
        assert after[fragment] == before[fragment], (
            f"{fragment!r}: шрифт {after[fragment]!r} вместо {before[fragment]!r}"
        )


# --------------------------------------------------------------------------- #
# Проверки, специфичные для отдельных кейсов
# --------------------------------------------------------------------------- #
def _check_two_columns(source: Path, output: Path) -> None:
    """(c) Обе колонки на своих местах, порядок строк внутри колонки сохранён."""
    blocks = _text_blocks(output)
    left = [block for block in blocks if abs(block.x - 72.0) < 1.0]
    right = [block for block in blocks if abs(block.x - 330.0) < 1.0]
    assert len(left) == 3, [(block.x, block.text) for block in blocks]
    assert len(right) == 3, [(block.x, block.text) for block in blocks]
    for column in (left, right):
        ys = [block.y for block in column]
        assert ys == sorted(ys, reverse=True), f"порядок строк сбит: {ys}"


def _check_mixed_fonts(source: Path, output: Path) -> None:
    """(d) Незамененные блоки сохранили Helvetica и Times соответственно."""
    _assert_fonts_kept(
        source,
        output,
        {
            "Second helvetica line stays untouched.": "F1",
            "The deadline and roadmap are clear.": "F2",
            "Second times line stays untouched.": "F2",
        },
    )


def _check_existing_type0(source: Path, output: Path) -> None:
    """(e) Готовый Type0 не подменён нашим CID-шрифтом."""
    fonts = _page_fonts(output)
    assert "/TFCyr0" not in fonts, f"кастомный шрифт подменён: {fonts}"
    assert set(fonts) == set(_page_fonts(source)), f"набор ресурсов изменился: {fonts}"
    assert set(fonts.values()) == {"/Type0"}, fonts


def _check_five_pages(source: Path, output: Path) -> None:
    """(f) Кириллица есть на каждой из пяти страниц."""
    with pymupdf.open(str(output)) as doc:
        assert doc.page_count == 5
        for number, page in enumerate(doc, start=1):
            assert "обратная связь" in page.get_text(), f"страница {number} без замены"


# --------------------------------------------------------------------------- #
# Кейсы
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Case:
    """Один real-world кейс.

    Attributes:
        name: Идентификатор кейса (он же id параметра pytest).
        build: Генератор исходного PDF.
        needle: Подстрока, по которой выбираются блоки для замены.
        replacement: Чем заменяется ``needle``.
        expected_replaced: Ожидаемое ``replaced_count``.
        untouched: Фрагменты, которые обязаны остаться в тексте дословно.
        extras: Дополнительные проверки ``(source, output) -> None``.
    """

    name: str
    build: Callable[[Path], Path]
    needle: str
    replacement: str
    expected_replaced: int
    untouched: tuple[str, ...] = ()
    extras: Callable[[Path, Path], None] | None = None


CASES: tuple[Case, ...] = (
    Case(
        name="reportlab",
        build=_build_reportlab,
        needle="feedback",
        replacement="обратная связь",
        expected_replaced=1,
        untouched=(
            "Second line about deadline and roadmap.",
            "Times line about the roadmap and deadline.",
        ),
    ),
    Case(
        name="pymupdf",
        build=_build_pymupdf,
        needle="feedback",
        replacement="обратная связь",
        expected_replaced=1,
        untouched=("Second line about deadline and roadmap.",),
    ),
    Case(
        name="two_columns",
        build=_build_two_columns,
        needle="first",
        replacement="первая",
        expected_replaced=2,
        untouched=("Left column second line.", "Right column third line."),
        extras=_check_two_columns,
    ),
    Case(
        name="mixed_fonts",
        build=_build_mixed_fonts,
        needle="feedback",
        replacement="обратная связь",
        expected_replaced=1,
        untouched=(
            "Second helvetica line stays untouched.",
            "The deadline and roadmap are clear.",
            "Second times line stays untouched.",
        ),
        extras=_check_mixed_fonts,
    ),
    Case(
        name="existing_type0",
        build=_build_existing_type0,
        needle="feedback",
        replacement="обратная связь",
        expected_replaced=1,
        untouched=("Second line about deadline and roadmap.",),
        extras=_check_existing_type0,
    ),
    Case(
        name="five_pages",
        build=_build_five_pages,
        needle="feedback",
        replacement="обратная связь",
        expected_replaced=5,
        untouched=(
            "Page 1: second line about deadline.",
            "Page 5: second line about deadline.",
        ),
        extras=_check_five_pages,
    ),
)


# --------------------------------------------------------------------------- #
# Тесты
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_realworld_case(case: Case, tmp_path: Path) -> None:
    """Кейс проходит целиком: замена, кириллица, сохранность остального текста."""
    source = case.build(_fixture(case.name))
    output = tmp_path / f"{case.name}_ru.pdf"
    before = _text_blocks(source)
    plan = _plan(source, case.needle, case.replacement)
    assert plan, f"{case.name}: не найдено блоков с {case.needle!r}"

    result = DocumentProcessor().apply_replacements(str(source), plan, str(output))

    assert result["success"] is True, result["errors"]
    assert result["errors"] == []
    assert result["replaced_count"] == case.expected_replaced

    text = _page_text(output)
    assert case.replacement in text, f"{case.name}: кириллицы нет в тексте"
    for fragment in case.untouched:
        assert fragment in text, f"{case.name}: потерян текст {fragment!r}"

    # Ни один незамененный блок не потерян целиком (перенос строк внутри
    # замененного блока — не потеря: блоков может стать больше).
    flat = _normalized(text)
    for block_id, block in enumerate(before):
        if block_id in plan:
            continue
        assert _normalized(block.text) in flat, (
            f"{case.name}: блок {block_id} потерян: {block.text!r}"
        )

    after = _text_blocks(output)
    assert len(after) >= len(before), (
        f"{case.name}: блоков было {len(before)}, стало {len(after)}"
    )

    with pikepdf.open(str(output)) as pdf, pikepdf.open(str(source)) as original:
        assert len(pdf.pages) == len(original.pages)
        for page in pdf.pages:
            assert page.get("/Contents") is not None
    _save_roundtrip(output)

    if case.extras is not None:
        case.extras(source, output)


def test_tf_per_page_map_used_when_mixed_fonts(tmp_path: Path) -> None:
    """(d) Tf-карта страницы: каждый ``BT`` получает шрифт своей строки.

    До рефакторинга на страницу брался один ``Tf`` (самый частый среди
    замененных), поэтому Times-строки получали Helvetica. Теперь для каждого
    ``BT`` без ``Tf`` выбирается ближайший по позиции блок той же страницы.
    """
    source = _build_mixed_fonts(_fixture("mixed_fonts"))
    output = tmp_path / "mixed_fonts_tf_map.pdf"
    plan = _plan(source, "feedback", "обратная связь")
    result = DocumentProcessor().apply_replacements(str(source), plan, str(output))
    assert result["success"] is True, result["errors"]

    rows = _bt_rows(output)

    def font_of(fragment: str) -> str:
        """Шрифт единственного ``BT``, содержащего ``fragment``."""
        fonts = [row["font"] for row in rows if fragment in row["body"]]
        assert len(fonts) == 1, f"{fragment!r}: найдено BT-блоков — {len(fonts)}"
        return fonts[0]

    assert font_of("Second helvetica line stays untouched.") == "F1"
    assert font_of("The deadline and roadmap are clear.") == "F2"
    assert font_of("Second times line stays untouched.") == "F2"

    # Ремонт проставил ``Tf`` каждому текстовому блоку: наследовать больше нечего.
    without_tf = [row for row in rows if not _TF_RE.search(row["body"])]
    assert without_tf == [], f"блоки без Tf остались: {[row['y'] for row in without_tf]}"

    # CID-шрифт стоит ровно на одном (заменённом) блоке.
    assert [row["font"] for row in rows].count("TFCyr0") == 1


def test_integrity_after_processing(tmp_path: Path) -> None:
    """(3) Целостность: pikepdf открывает, subset содержит нужные глифы."""
    source = _build_reportlab(_fixture("reportlab"))
    output = tmp_path / "integrity.pdf"
    new_text = "Обратная связь from the meeting was clear."

    result = DocumentProcessor().apply_replacements(str(source), {0: new_text}, str(output))
    assert result["success"] is True, result["errors"]

    with pikepdf.open(str(output)) as pdf:
        assert len(pdf.pages) == 1
        fonts = pdf.pages[0]["/Resources"]["/Font"]
        assert "/TFCyr0" in fonts, list(fonts.keys())

        embedded = fonts["/TFCyr0"]
        assert str(embedded["/Subtype"]) == "/Type0"
        assert str(embedded["/Encoding"]) == "/Identity-H"

        mapping: dict[int, str] = {}
        cmap = embedded["/ToUnicode"].read_bytes().decode("latin-1")
        for cid, unicode_hex in _BFCHAR_RE.findall(cmap):
            mapping[int(cid, 16)] = "".join(
                chr(int(unicode_hex[index : index + 4], 16))
                for index in range(0, len(unicode_hex), 4)
            )
        covered = set("".join(mapping.values()))
        assert set(new_text) <= covered, f"нет в ToUnicode: {sorted(set(new_text) - covered)}"

        descriptor = embedded["/DescendantFonts"][0]["/FontDescriptor"]
        font_bytes = descriptor["/FontFile2"].read_bytes()

    ttf = TTFont(io.BytesIO(font_bytes))
    glyph_count = ttf["maxp"].numGlyphs
    assert glyph_count > 1, "subset пуст"

    used = {int(match, 16) for match in _HEX_GLYPH_RE.findall(_page_stream(output))}
    assert used, "в content stream нет глифов CID-шрифта"
    assert max(used) < glyph_count, f"CID {max(used)} вне subset ({glyph_count} глифов)"

