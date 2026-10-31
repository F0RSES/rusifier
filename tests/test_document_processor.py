"""Тесты :class:`src.document_processor.DocumentProcessor`.

Покрывают определение формата, извлечение блоков, запись замен с сохранением
форматирования и неизменность исходного файла.

Фикстуры создаются программно в ``tmp_path``: бинарные документы в репозитории
не хранятся, поэтому тесты не зависят от состояния ``tests/fixtures``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from pptx import Presentation

from src.document_processor import (
    DocumentProcessingError,
    DocumentProcessor,
    UnsupportedFormatError,
)


def _sha256(path: Path) -> str:
    """Вернуть SHA-256 файла."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_docx_runs(path: Path) -> list[list[tuple[str, bool | None]]]:
    """Вернуть runs первых абзацев DOCX как ``(текст, bold)``."""
    document = DocxDocument(str(path))
    return [
        [(run.text, run.bold) for run in paragraph.runs] for paragraph in document.paragraphs
    ]


@pytest.fixture()
def docx_file(tmp_path: Path) -> Path:
    """Минимальный DOCX: абзац с bold-run внутри, второй абзац и таблица."""
    path = tmp_path / "sample.docx"
    document = DocxDocument()
    paragraph = document.add_paragraph("Report for ")
    bold_run = paragraph.add_run("FY2024")
    bold_run.bold = True
    paragraph.add_run(" quarter, feedback and KPI are here.")
    document.add_paragraph("Second paragraph with deadline.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Cell TLS one"
    table.cell(0, 1).text = "Cell two"
    document.save(str(path))
    return path


@pytest.fixture()
def pptx_file(tmp_path: Path) -> Path:
    """Минимальный PPTX: заголовок слайда и два пункта списка."""
    path = tmp_path / "sample.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Roadmap review"
    text_frame = slide.placeholders[1].text_frame
    text_frame.text = "Onboarding and feedback"
    text_frame.add_paragraph().text = "Second bullet with milestone"
    presentation.save(str(path))
    return path


@pytest.fixture()
def pdf_file(tmp_path: Path) -> Path:
    """Минимальный PDF с текстовым слоем (две строки)."""
    canvas_module = pytest.importorskip(
        "reportlab.pdfgen.canvas", reason="для PDF-фикстуры нужен reportlab"
    )
    path = tmp_path / "sample.pdf"
    canvas = canvas_module.Canvas(str(path))
    canvas.setFont("Helvetica", 12)
    canvas.drawString(72, 720, "The feedback from the meeting was clear.")
    canvas.drawString(72, 700, "Second line about deadline and roadmap.")
    canvas.save()
    return path


@pytest.fixture()
def txt_file(tmp_path: Path) -> Path:
    """Файл неподдерживаемого формата."""
    path = tmp_path / "notes.txt"
    path.write_text("plain text", encoding="utf-8")
    return path


@pytest.fixture()
def empty_docx(tmp_path: Path) -> Path:
    """DOCX без единого абзаца с текстом."""
    path = tmp_path / "empty.docx"
    document = DocxDocument()
    document.add_paragraph("")
    document.add_paragraph("   ")
    document.save(str(path))
    return path


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("docx_file", "docx"),
        ("pptx_file", "pptx"),
        ("pdf_file", "pdf"),
    ],
)
def test_detect_format(fixture_name: str, expected: str, request: pytest.FixtureRequest) -> None:
    """Формат определяется по расширению поддерживаемых файлов."""
    path = request.getfixturevalue(fixture_name)
    assert DocumentProcessor().detect_format(str(path)) == expected


def test_detect_format_txt_raises(txt_file: Path) -> None:
    """Неподдерживаемое расширение даёт UnsupportedFormatError."""
    with pytest.raises(UnsupportedFormatError):
        DocumentProcessor().detect_format(str(txt_file))


def test_detect_format_missing_file_raises(tmp_path: Path) -> None:
    """Отсутствующий файл даёт DocumentProcessingError, а не UnsupportedFormatError."""
    with pytest.raises(DocumentProcessingError):
        DocumentProcessor().detect_format(str(tmp_path / "missing.docx"))


def test_extract_text_docx(docx_file: Path) -> None:
    """Блоки DOCX идут подряд с нуля, включая текст ячеек таблицы."""
    blocks = DocumentProcessor().extract_text(str(docx_file))

    assert [block["block_id"] for block in blocks] == [0, 1, 2, 3]
    assert {block["source"] for block in blocks} == {"docx"}
    assert "feedback" in blocks[0]["text"]
    assert blocks[2]["text"] == "Cell TLS one"


def test_extract_text_skips_blank_blocks(empty_docx: Path) -> None:
    """Пустые и whitespace-only абзацы не попадают в результат."""
    assert DocumentProcessor().extract_text(str(empty_docx)) == []


def test_extract_text_pptx_includes_slide_title(pptx_file: Path) -> None:
    """Заголовок слайда (плоское поле модели) попадает в блоки."""
    blocks = DocumentProcessor().extract_text(str(pptx_file))

    assert [block["block_id"] for block in blocks] == [0, 1, 2]
    assert blocks[0]["text"] == "Roadmap review"
    assert {block["source"] for block in blocks} == {"pptx"}


def test_extract_text_pdf(pdf_file: Path) -> None:
    """PDF даёт блоки уровня строки с текстовым слоем."""
    blocks = DocumentProcessor().extract_text(str(pdf_file))

    assert blocks, "ожидался хотя бы один текстовый блок"
    assert {block["source"] for block in blocks} == {"pdf"}
    assert any("feedback" in block["text"] for block in blocks)


def test_extract_text_never_returns_whitespace_only(docx_file: Path, empty_docx: Path) -> None:
    """Ни один блок не состоит из пробелов."""
    processor = DocumentProcessor()
    for path in (docx_file, empty_docx):
        for block in processor.extract_text(str(path)):
            assert block["text"].strip()


def test_apply_replacements_keeps_source_untouched(docx_file: Path, tmp_path: Path) -> None:
    """Исходный файл не меняется, результат пишется в output_path."""
    processor = DocumentProcessor()
    before = _sha256(docx_file)
    output = tmp_path / "result.docx"
    new_text = "Отчёт за FY2024 квартал, обратная связь и KPI здесь."

    result = processor.apply_replacements(str(docx_file), {0: new_text}, str(output))

    assert result["success"] is True
    assert result["errors"] == []
    assert result["replaced_count"] == 1
    assert _sha256(docx_file) == before, "исходный файл изменился"
    assert output.exists()

    blocks = processor.extract_text(str(output))
    assert blocks[0]["text"] == new_text


def test_apply_replacements_preserves_run_formatting(docx_file: Path, tmp_path: Path) -> None:
    """Bold-run внутри абзаца сохраняется после замены остального текста."""
    output = tmp_path / "formatted.docx"
    new_text = "Отчёт за FY2024 квартал, обратная связь и KPI здесь."

    DocumentProcessor().apply_replacements(str(docx_file), {0: new_text}, str(output))

    runs = _read_docx_runs(output)[0]
    assert ("FY2024", True) in runs, f"bold-run потерян: {runs}"
    assert "".join(text for text, _ in runs) == new_text


def test_apply_replacements_pptx(pptx_file: Path, tmp_path: Path) -> None:
    """Заменяются и заголовок слайда, и текст пункта списка."""
    output = tmp_path / "result.pptx"
    replacements = {0: "Обзор дорожной карты", 1: "Адаптация и обратная связь"}

    result = DocumentProcessor().apply_replacements(str(pptx_file), replacements, str(output))

    assert result["success"] is True
    assert result["replaced_count"] == 2

    presentation = Presentation(str(output))
    slide = presentation.slides[0]
    assert slide.shapes.title.text == "Обзор дорожной карты"
    assert slide.placeholders[1].text_frame.text.splitlines()[0] == (
        "Адаптация и обратная связь"
    )


def test_apply_replacements_unknown_block_id(docx_file: Path, tmp_path: Path) -> None:
    """Несуществующий block_id попадает в errors, исходник не меняется."""
    before = _sha256(docx_file)
    result = DocumentProcessor().apply_replacements(
        str(docx_file), {99: "текст"}, str(tmp_path / "result.docx")
    )

    assert result["success"] is False
    assert result["replaced_count"] == 0
    assert result["errors"]
    assert result["errors"][0]["block_id"] == 99
    assert _sha256(docx_file) == before


def test_apply_replacements_refuses_to_overwrite_source(docx_file: Path) -> None:
    """Сохранение поверх исходного файла запрещено."""
    before = _sha256(docx_file)
    result = DocumentProcessor().apply_replacements(
        str(docx_file), {0: "новый текст"}, str(docx_file)
    )

    assert result["success"] is False
    assert result["errors"]
    assert _sha256(docx_file) == before


def test_apply_replacements_empty_mapping_copies_source(docx_file: Path, tmp_path: Path) -> None:
    """Пустой набор замен даёт копию исходника без изменений."""
    output = tmp_path / "copy.docx"

    result = DocumentProcessor().apply_replacements(str(docx_file), {}, str(output))

    assert result["success"] is True
    assert result["replaced_count"] == 0
    assert _sha256(output) == _sha256(docx_file)


def test_apply_replacements_pdf_handles_font_limitation(pdf_file: Path, tmp_path: Path) -> None:
    """PDF не падает: либо замена, либо явная ошибка про глифы шрифта."""
    pytest.importorskip("pdf_edit_engine", reason="нужен pdf-edit-engine")
    output = tmp_path / "result.pdf"

    result = DocumentProcessor().apply_replacements(
        str(pdf_file), {0: "Обратная связь из встречи была ясной."}, str(output)
    )

    assert set(result) == {"success", "errors", "replaced_count", "warnings"}
    assert isinstance(result["warnings"], list)
    assert output.exists(), "результат должен быть записан даже при ошибке шрифта"
    if not result["success"]:
        assert result["errors"]
        assert result["replaced_count"] == 0


