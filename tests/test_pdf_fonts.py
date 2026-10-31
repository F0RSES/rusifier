"""Тесты кириллических шрифтов PDF: резолвер, подмена, ошибки, fallback."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest
from pdf_edit_engine.models import TextBlock
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from src.document_processor import DocumentProcessor
from src.pdf_fonts import (
    PdfFontResolver,
    is_base14_font,
    repair_inherited_font,
    text_has_cyrillic,
)

DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
SOURCE_LINE = "The feedback from the meeting was clear."
NEW_LINE = "The обратная связь from the meeting was clear."
OTHER_LINE = "Second line about deadline and roadmap."


def _sha256(path: Path) -> str:
    """SHA-256 файла."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_base14_pdf(path: Path) -> Path:
    """PDF с Helvetica (base-14, шрифт не встроен): две строки."""
    pdf = canvas.Canvas(str(path))
    pdf.setFont("Helvetica", 12)
    pdf.drawString(72, 720, SOURCE_LINE)
    pdf.drawString(72, 700, OTHER_LINE)
    pdf.save()
    return path


def _make_custom_font_pdf(path: Path) -> Path:
    """PDF со встроенным кастомным шрифтом (subset DejaVu, не base-14)."""
    pdfmetrics.registerFont(TTFont("ProbeCustom", str(DEJAVU)))
    pdf = canvas.Canvas(str(path))
    pdf.setFont("ProbeCustom", 12)
    pdf.drawString(72, 720, "Hello custom font block.")
    pdf.save()
    return path


@pytest.fixture()
def base14_pdf(tmp_path: Path) -> Path:
    """PDF с Helvetica."""
    return _make_base14_pdf(tmp_path / "helvetica.pdf")


def test_resolver_finds_dejavu() -> None:
    """Для блока Helvetica резолвер отдаёт DejaVu."""
    block = TextBlock(
        text=SOURCE_LINE, x=72.0, y=717.0, width=288.0, height=12.0,
        font_name="Helvetica", font_size=12.0, page=0,
    )

    path = PdfFontResolver().find_for_block(block)

    assert path is not None, "кириллический шрифт не найден в системе"
    assert "DejaVu" in path.name


def test_resolver_returns_none_for_bold_when_no_bold_font(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если bold-кандидатов нет — для bold-блока возвращается None."""
    monkeypatch.setattr(
        PdfFontResolver,
        "CANDIDATES",
        [("regular", [str(DEJAVU)])],
    )
    block = TextBlock(
        text="Bold block", x=72.0, y=717.0, width=100.0, height=12.0,
        font_name="Helvetica-Bold", font_size=12.0, page=0,
    )

    assert PdfFontResolver().find_for_block(block) is None


def test_pdf_cyrillic_replacement_succeeds(base14_pdf: Path, tmp_path: Path) -> None:
    """Кириллица в base-14 блоке: success, replaced_count=1, текст читается."""
    before = _sha256(base14_pdf)
    output = tmp_path / "result.pdf"

    result = DocumentProcessor().apply_replacements(
        str(base14_pdf), {0: NEW_LINE}, str(output)
    )

    assert result["success"] is True
    assert result["errors"] == []
    assert result["replaced_count"] == 1
    assert _sha256(base14_pdf) == before, "исходный PDF изменился"

    text = pymupdf.open(str(output))[0].get_text()
    assert "обратная связь" in text
    assert OTHER_LINE in text, "посторонний блок потерян"


def test_pdf_latin_replacement_preserves_original_font(tmp_path: Path) -> None:
    """Для латиницы кастомный шрифт не подменяется."""
    source = _make_custom_font_pdf(tmp_path / "custom.pdf")
    processor = DocumentProcessor()
    blocks = processor.extract_text(str(source))
    output = tmp_path / "custom_out.pdf"

    result = processor.apply_replacements(
        str(source), {0: "Hi custom font block."}, str(output)
    )

    assert result["success"] is True
    fonts = _page_font_names(output)
    assert not any(name.startswith("/TFCyr") for name in fonts), fonts
    assert "Hi custom font block." in pymupdf.open(str(output))[0].get_text()
    assert blocks[0]["text"] == "Hello custom font block."


def test_pdf_cyrillic_without_font_goes_to_errors(
    base14_pdf: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Если шрифт не найден — success=False и понятное сообщение."""
    monkeypatch.setattr(PdfFontResolver, "find_for_block", lambda self, block, base_font=None: None)

    result = DocumentProcessor().apply_replacements(
        str(base14_pdf), {0: NEW_LINE}, str(tmp_path / "no_font.pdf")
    )

    assert result["success"] is False
    assert result["replaced_count"] == 0
    messages = " ".join(error["message"] for error in result["errors"])
    assert "Кириллический шрифт" in messages


def _page_font_names(path: Path) -> list[str]:
    """Имена ресурсов шрифтов первой страницы."""
    import pikepdf

    with pikepdf.open(str(path)) as pdf:
        return [str(name) for name in pdf.pages[0]["/Resources"]["/Font"].keys()]


def test_pdf_font_dir_is_used(
    base14_pdf: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--pdf-font-dir: шрифт берётся из указанной папки, а не из системы."""
    import shutil

    fonts_dir = tmp_path / "fonts"
    fonts_dir.mkdir()
    shutil.copy2(DEJAVU, fonts_dir / "MyCyr.ttf")
    monkeypatch.setattr(
        PdfFontResolver, "CANDIDATES", [("regular", [])]
    )
    output = tmp_path / "from_dir.pdf"

    result = DocumentProcessor(pdf_font_dir=fonts_dir).apply_replacements(
        str(base14_pdf), {0: NEW_LINE}, str(output)
    )

    assert result["success"] is True, result["errors"]
    assert "обратная связь" in pymupdf.open(str(output))[0].get_text()


def test_cli_exposes_pdf_font_flags() -> None:
    """CLI принимает --pdf-font-dir и --pdf-fallback."""
    from src.cli import build_parser

    args = build_parser().parse_args(
        ["--input", "x.pdf", "--pdf-font-dir", "/tmp/fonts", "--pdf-fallback", "polydoc"]
    )

    assert args.pdf_font_dir == "/tmp/fonts"
    assert args.pdf_fallback == "polydoc"


def test_invalid_pdf_fallback_rejected() -> None:
    """Неизвестный режим fallback — ValueError."""
    with pytest.raises(ValueError):
        DocumentProcessor(pdf_fallback="unknown")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("/Helvetica", True),
        ("Helvetica-Bold", True),
        ("/Times-Roman", True),
        ("/Courier", True),
        ("/Arial", True),
        ("/AAAAAA+DejaVuSans", False),
        ("/F1", False),
        (None, False),
    ],
)
def test_is_base14_font(name: str | None, expected: bool) -> None:
    """Классификация base-14 по имени шрифта."""
    assert is_base14_font(name) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("feedback", False), ("обратная связь", True), ("Ёлка", True), ("", False)],
)
def test_text_has_cyrillic(text: str, expected: bool) -> None:
    """Детектор кириллицы."""
    assert text_has_cyrillic(text) is expected


def test_repair_inherited_font_inserts_tf() -> None:
    """BT без Tf получает исходный шрифт, BT с Tf не трогается."""
    stream = (
        "BT\n/TFCyr0 12 Tf\n(a) Tj\nET\n"
        "BT\n1 0 0 1 72 700 Tm\n(b) Tj\nET\n"
        "BT\n/F1 12 Tf\n(c) Tj\nET\n"
    )

    patched, fixed = repair_inherited_font(stream, "/F1 12 Tf")

    assert fixed == 1
    assert patched.count("/F1 12 Tf") == 2
    assert "BT\n/F1 12 Tf\n1 0 0 1 72 700 Tm" in patched
