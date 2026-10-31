"""Регрессия вёрстки PDF: строки не должны наезжать друг на друга.

Ключевое отличие от остальных PDF-тестов: проверяются **bounding box строк**
через ``fitz``, а не ``get_text()``. Текст с наложением извлекается корректно,
поэтому текстовые проверки такой дефект не видят.

Причина дефекта (см. ``docs/pdf-reflow-research.md``): без явного ``line_height``
``pdf_edit_engine`` сжимает межстрочный интервал до ``bbox_height / len(lines)``,
когда сдвигать контент ниже некуда (последний блок страницы).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from reportlab.pdfgen import canvas

from src.cli import EXIT_INVALID_ARGS, EXIT_OK, main
from src.document_processor import DocumentProcessor
from src.pdf_fonts import repair_inherited_font

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Допуск на округление координат в разных просмотрщиках, pt.
EPS = 0.5

#: Замена подобрана так, чтобы строка гарантированно переносилась.
NEEDLE = "roadmap"
REPLACEMENT = "дорожная карта"

_TF_RE = re.compile(r"/([A-Za-z0-9+.\-]+)\s+([\d.]+)\s+Tf")
_TD_RE = re.compile(r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+Td")
_TM_RE = re.compile(
    r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+"
    r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+Tm"
)


# --------------------------------------------------------------------------- #
# Генераторы PDF
# --------------------------------------------------------------------------- #
def _reportlab_pdf(path: Path) -> Path:
    """reportlab: ``Tf`` в отдельном ``BT``, строки — без ``Tf`` (худший случай)."""
    doc = canvas.Canvas(str(path))
    doc.setFont("Helvetica", 12)
    doc.drawString(72, 720, "The feedback from the meeting was clear.")
    doc.drawString(72, 700, "Second line about roadmap and deadline.")
    doc.drawString(72, 680, "Third line stays untouched.")
    doc.save()
    return path


def _pymupdf_pdf(path: Path) -> Path:
    """PyMuPDF: ``Tf`` в каждом ``BT``, текст в hex-``TJ``."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "The feedback from the meeting was clear.", fontsize=12)
    page.insert_text((72, 130), "Second line about roadmap and deadline.", fontsize=12)
    doc.save(str(path))
    doc.close()
    return path


def _two_columns_pdf(path: Path) -> Path:
    """Две колонки: замена в левой, правая должна остаться на месте."""
    doc = pymupdf.open()
    page = doc.new_page()
    left = ("Left roadmap line one.", "Left column second line.", "Left column third line.")
    right = ("Right column first line.", "Right column second line.", "Right column third line.")
    for index, (left_line, right_line) in enumerate(zip(left, right)):
        page.insert_text((72, 100 + index * 20), left_line, fontsize=11)
        page.insert_text((330, 100 + index * 20), right_line, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


BUILDERS = (
    pytest.param(_reportlab_pdf, id="reportlab"),
    pytest.param(_pymupdf_pdf, id="pymupdf"),
    pytest.param(_two_columns_pdf, id="two_columns"),
)


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #
def _lines(path: Path, page_index: int = 0) -> list[dict]:
    """Строки страницы с bbox, отсортированные по ``(y0, x0)``.

    Args:
        path: PDF-файл.
        page_index: Номер страницы (0-based).

    Returns:
        list[dict]: ``{"bbox": (x0, y0, x1, y1), "text": str}`` по строкам.
    """
    with pymupdf.open(str(path)) as doc:
        data = doc[page_index].get_text("dict")
    rows = []
    for block in data["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            rows.append(
                {
                    "bbox": tuple(line["bbox"]),
                    "text": "".join(span["text"] for span in line["spans"]),
                }
            )
    rows.sort(key=lambda item: (round(item["bbox"][1], 1), item["bbox"][0]))
    return rows


def _assert_no_overlap(path: Path, page_index: int = 0) -> None:
    """Проверить, что строки не пересекаются по вертикали.

    Сравниваются только строки, перекрывающиеся по горизонтали: две колонки или
    два inline-фрагмента на одной базовой линии — не наложение. Настоящий дефект
    (сжатый leading) даёт строки «друг под другом» с общим диапазоном x.

    Args:
        path: PDF-файл.
        page_index: Номер страницы (0-based).

    Raises:
        AssertionError: если низ предыдущей строки заходит на верх следующей
        при пересечении по x.
    """
    rows = _lines(path, page_index)
    for previous, current in zip(rows, rows[1:]):
        left = max(previous["bbox"][0], current["bbox"][0])
        right = min(previous["bbox"][2], current["bbox"][2])
        if right <= left:
            continue  # разные колонки / соседние inline-фрагменты
        overlap = previous["bbox"][3] - current["bbox"][1]
        assert overlap <= EPS, (
            f"строки пересекаются на {overlap:.2f} pt:\n"
            f"  {previous['bbox']} {previous['text']!r}\n"
            f"  {current['bbox']} {current['text']!r}"
        )


def _assert_no_overlap_for_text(path: Path, marker: str, page_index: int = 0) -> int:
    """Проверить наложения только вокруг строк с ``marker`` (наши замены).

    Полная проверка всех строк на реальных документах даёт ложные срабатывания:
    декоративные заголовки крупным кеглем перекрываются по bbox ещё до обработки.
    Поэтому проверяются пары, где хотя бы одна строка содержит нашу кириллицу.

    Args:
        path: PDF-файл.
        marker: Подстрока замены (например «это»).
        page_index: Номер страницы (0-based).

    Returns:
        int: Сколько строк с ``marker`` проверено.

    Raises:
        AssertionError: если строка с заменой наезжает на соседнюю.
    """
    rows = _lines(path, page_index)
    checked = 0
    for previous, current in zip(rows, rows[1:]):
        if marker not in previous["text"] and marker not in current["text"]:
            continue
        left = max(previous["bbox"][0], current["bbox"][0])
        right = min(previous["bbox"][2], current["bbox"][2])
        if right <= left:
            continue
        overlap = previous["bbox"][3] - current["bbox"][1]
        assert overlap <= EPS, (
            f"строка с заменой пересекается на {overlap:.2f} pt:\n"
            f"  {previous['bbox']} {previous['text']!r}\n"
            f"  {current['bbox']} {current['text']!r}"
        )
        checked += 1
    return checked


def _page_stream(path: Path, page_index: int = 0) -> str:
    """Декодированный content stream страницы."""
    with pikepdf.open(str(path)) as pdf:
        contents = pdf.pages[page_index].get("/Contents")
        if contents is None:
            return ""
        if isinstance(contents, pikepdf.Array):
            raw = b"\n".join(stream.read_bytes() for stream in contents)
        else:
            raw = contents.read_bytes()
    return raw.decode("latin-1")


def _td_steps(path: Path, page_index: int = 0) -> list[float]:
    """Абсолютные значения ``dy`` из операторов ``Td`` (шаг перенесённых строк)."""
    return [
        abs(float(match.group(2))) for match in _TD_RE.finditer(_page_stream(path, page_index))
    ]


def _font_sizes(path: Path, page_index: int = 0) -> list[float]:
    """Кегли из операторов ``Tf`` в content stream."""
    return [float(match.group(2)) for match in _TF_RE.finditer(_page_stream(path, page_index))]


def _plan(path: Path, needle: str, replacement: str) -> dict[int, str]:
    """Замены ``{block_id: текст}`` для блоков, содержащих ``needle``."""
    import pdf_edit_engine as engine

    plan: dict[int, str] = {}
    for block_id, block in enumerate(engine.get_text_layout(str(path))):
        if needle in block.text:
            plan[block_id] = block.text.replace(needle, replacement)
    return plan


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Вызвать ``main`` и вернуть (код, stdout, stderr)."""
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _replace(path: Path, output: Path, **options: object) -> dict:
    """Применить замену ``roadmap`` и вернуть результат процессора."""
    plan = _plan(path, NEEDLE, REPLACEMENT)
    assert plan, f"в {path.name} нет блока с {NEEDLE!r}"
    return DocumentProcessor(**options).apply_replacements(str(path), plan, str(output))


# --------------------------------------------------------------------------- #
# Тесты
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("builder", BUILDERS)
def test_pdf_lines_do_not_overlap(builder, tmp_path: Path) -> None:
    """После переноса строки не пересекаются (bbox из fitz, не текст)."""
    source = builder(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    result = _replace(source, output)

    assert result["success"] is True, result["errors"]
    assert result["replaced_count"] >= 1

    with pymupdf.open(str(output)) as doc:
        pages = doc.page_count
    for page_index in range(pages):
        _assert_no_overlap(output, page_index)


def test_reflow_leading_applied(tmp_path: Path) -> None:
    """Шаг перенесённой строки равен кегль × 1.2 (14.4 pt при 12 pt)."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    result = _replace(source, output)
    assert result["success"] is True, result["errors"]

    steps = _td_steps(output)
    assert steps, "в content stream нет Td — переноса не было"
    assert steps[0] == pytest.approx(14.4, abs=EPS), steps


def test_no_overlap_on_real_pdfs(tmp_path: Path) -> None:
    """Реальные PDF из /usr/share/doc: строки с нашими заменами не наезжают.

    Полная проверка всех строк на таких документах ловит исходные декоративные
    наложения (крупные заголовки перекрываются по bbox ещё до обработки),
    поэтому проверяются только пары вокруг заменённых строк. Документы, где
    замена невозможна (Type1-сабсеты без кириллицы), пропускаются: там нет
    нашего текста, и проверять нечего.
    """
    candidates = sorted(
        (
            path
            for path in Path("/usr/share/doc").rglob("*.pdf")
            if path.is_file() and path.stat().st_size < 1_500_000
        ),
        key=lambda item: item.stat().st_size,
    )
    if not candidates:
        pytest.skip("в /usr/share/doc нет PDF")

    marker = "это"
    checked_docs = 0
    checked_lines = 0
    for source in candidates[:12]:
        plan = _plan(source, "the", marker)
        if not plan:
            continue
        output = tmp_path / f"real_{checked_docs}.pdf"
        result = DocumentProcessor().apply_replacements(str(source), plan, str(output))
        if result["replaced_count"] == 0 or not output.exists():
            continue
        with pymupdf.open(str(output)) as doc:
            for page_index in range(min(doc.page_count, 3)):
                checked_lines += _assert_no_overlap_for_text(output, marker, page_index)
        checked_docs += 1
        if checked_docs == 3:
            break

    assert checked_docs >= 1, "ни один реальный PDF не удалось обработать"
    assert checked_lines >= 1, "ни одна заменённая строка не проверена"


def test_repair_inherited_font_does_not_shift_tm(tmp_path: Path) -> None:
    """Ремонт ``Tf`` не меняет ``Tm`` (позиционирование строк не сдвигается).

    Поток берётся в состоянии **до** ремонта: prepared-файл с внедрённым
    CID-шрифтом + один ``replace_block``. На выходе процессора ремонт уже
    применён, поэтому там проверять нечего.
    """
    import pdf_edit_engine as engine

    from src.pdf_fonts import add_cyrillic_font

    source = _reportlab_pdf(tmp_path / "in.pdf")
    chars = "".join(sorted(set(REPLACEMENT + " " + NEEDLE)))
    prepared = tmp_path / "prepared.pdf"
    edited = tmp_path / "edited.pdf"

    with pikepdf.open(str(source)) as pdf:
        add_cyrillic_font(
            pdf,
            pdf.pages[0],
            PROJECT_ROOT / "data" / "fonts" / "DejaVuSans.ttf",
            chars,
            "TFCyr0",
        )
        pdf.save(str(prepared))

    blocks = [block for block in engine.get_text_layout(str(source)) if block.text.strip()]
    target = next(block for block in blocks if NEEDLE in block.text)
    bbox = (target.x, target.y, target.x + target.width, target.y + target.height)
    engine.replace_block(
        str(prepared),
        0,
        bbox,
        target.text.replace(NEEDLE, REPLACEMENT),
        str(edited),
        font_name="TFCyr0",
        line_height=14.4,
    )

    before = _page_stream(edited)
    fallback = {(700.0, 72.0): "/F1 12 Tf", (680.0, 72.0): "/F1 12 Tf"}
    patched, fixed, problems = repair_inherited_font(before, fallback)

    assert fixed >= 1, "ремонт ничего не вставил — тест не про то"
    assert problems == []
    assert _TM_RE.findall(before) == _TM_RE.findall(patched)


def test_pdf_fit_shrink_respects_min_font_size(tmp_path: Path) -> None:
    """shrink ниже ``--pdf-min-font-size`` откатывается на fit=none + warning."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    result = _replace(source, output, pdf_fit="shrink", pdf_min_font_size=10.0)

    assert result["success"] is True, result["errors"]
    too_small = [
        item for item in result["warnings"] if item["kind"] == "pdf_font_too_small"
    ]
    assert too_small, result["warnings"]
    assert too_small[0]["requested_min"] == 10.0
    assert too_small[0]["actual"] < 10.0

    # Откат вернул исходный кегль: уменьшенного Tf (6 pt) в потоке нет.
    assert min(_font_sizes(output)) == pytest.approx(12.0, abs=EPS)


def test_pdf_fit_shrink_success(tmp_path: Path) -> None:
    """Разрешённый shrink уменьшает кегль, текст остаётся без наложений."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    result = _replace(source, output, pdf_fit="shrink", pdf_min_font_size=4.0)

    assert result["success"] is True, result["errors"]
    assert not [item for item in result["warnings"] if item["kind"] == "pdf_font_too_small"]
    assert min(_font_sizes(output)) < 12.0, "кегль не уменьшился"
    _assert_no_overlap(output)


def test_pdf_line_height_override(tmp_path: Path) -> None:
    """``--pdf-line-height=2.0`` даёт шаг строки ``font_size * 2.0``."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    result = _replace(source, output, pdf_line_height=2.0)

    assert result["success"] is True, result["errors"]
    steps = _td_steps(output)
    assert steps, "в content stream нет Td — переноса не было"
    assert steps[0] == pytest.approx(24.0, abs=EPS), steps


def test_default_line_height_is_1_2(tmp_path: Path) -> None:
    """Без флага шаг строки — регрессия на ``font_size * 1.2``."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    _replace(source, output)

    with pikepdf.open(str(output)):
        pass
    assert _td_steps(output)[0] == pytest.approx(14.4, abs=EPS)


def test_overflow_warning_in_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json``: overflow попадает в warnings структурной записью."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    code, out, _ = _run(
        ["--input", str(source), "--output", str(output), "--json"], capsys
    )

    assert code == EXIT_OK
    payload = json.loads(out)
    overflow = [item for item in payload["warnings"] if item.get("kind") == "pdf_overflow"]
    assert overflow, payload["warnings"]
    assert isinstance(overflow[0]["block_id"], int)
    assert "overflow_pt" in overflow[0]


def test_overflow_count_in_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``pdf_overflow_count`` совпадает с числом записей ``pdf_overflow``."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    code, out, _ = _run(
        ["--input", str(source), "--output", str(output), "--json"], capsys
    )

    assert code == EXIT_OK
    payload = json.loads(out)
    expected = len(
        [item for item in payload["warnings"] if item.get("kind") == "pdf_overflow"]
    )
    assert payload["pdf_overflow_count"] == expected
    assert expected >= 1


def test_pdf_min_font_size_must_be_positive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--pdf-min-font-size <= 0`` — ошибка аргументов (exit=2)."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    code, _, err = _run(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--pdf-fit",
            "shrink",
            "--pdf-min-font-size",
            "0",
        ],
        capsys,
    )

    assert code == EXIT_INVALID_ARGS
    assert "pdf-min-font-size" in err


def test_pdf_line_height_flag_reaches_processor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--pdf-line-height`` из CLI доходит до вёрстки (шаг Td меняется)."""
    source = _reportlab_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    code, _, _ = _run(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--pdf-line-height",
            "1.5",
        ],
        capsys,
    )

    assert code == EXIT_OK
    assert _td_steps(output)[0] == pytest.approx(18.0, abs=EPS)
