"""Тесты кириллических шрифтов PDF: резолвер, подмена, ошибки, fallback."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pymupdf
import pytest
from pdf_edit_engine.models import TextBlock
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from src.document_processor import DocumentProcessor
from src.pdf_fonts import (
    DEFAULT_FONT_DIR,
    PdfFontResolver,
    is_base14_font,
    repair_inherited_font,
    text_has_cyrillic,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEJAVU = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
#: Размер .deb до бандлинга шрифтов (stage 5) — база для проверки +5 %.
DEB_BASELINE_BYTES = 73_015_004
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
    """BT без Tf получает шрифт ближайшего предшествующего блока страницы."""
    stream = (
        "BT\n/TFCyr0 12 Tf\n1 0 0 1 72 720 Tm\n(a) Tj\nET\n"
        "BT\n1 0 0 1 72 700 Tm\n(b) Tj\nET\n"
        "BT\n/F1 12 Tf\n1 0 0 1 72 680 Tm\n(c) Tj\nET\n"
    )
    fallback_map = {(720.0, 72.0): "/F1 12 Tf", (680.0, 72.0): "/F1 12 Tf"}

    patched, fixed, problems = repair_inherited_font(stream, fallback_map)

    assert fixed == 1
    assert problems == []
    assert patched.count("/F1 12 Tf") == 2
    assert "BT\n/F1 12 Tf\n1 0 0 1 72 700 Tm" in patched


def test_repair_inherited_font_picks_nearest_preceding() -> None:
    """На странице с двумя гарнитурами каждый BT получает свой шрифт."""
    stream = (
        "BT\n/Helv 10 Tf\n1 0 0 1 72 700 Tm\n(helv) Tj\nET\n"
        "BT\n1 0 0 1 72 680 Tm\n(after helv) Tj\nET\n"
        "BT\n/Times 10 Tf\n1 0 0 1 72 400 Tm\n(times) Tj\nET\n"
        "BT\n1 0 0 1 72 380 Tm\n(after times) Tj\nET\n"
    )
    fallback_map = {(700.0, 72.0): "/Helv 10 Tf", (400.0, 72.0): "/Times 10 Tf"}

    patched, fixed, problems = repair_inherited_font(stream, fallback_map)

    assert fixed == 2
    assert problems == []
    assert "BT\n/Helv 10 Tf\n1 0 0 1 72 680 Tm" in patched
    assert "BT\n/Times 10 Tf\n1 0 0 1 72 380 Tm" in patched


def test_repair_inherited_font_reports_unknown_position() -> None:
    """BT без Tm и без Tf остаётся как есть, но попадает в предупреждения."""
    stream = "BT\n(no position) Tj\nET\n"

    patched, fixed, problems = repair_inherited_font(stream, {(700.0, 72.0): "/F1 10 Tf"})

    assert fixed == 0
    assert patched == stream
    assert len(problems) == 1
    assert "не удалось определить шрифт" in problems[0]


def test_repair_inherited_font_without_map_is_noop() -> None:
    """Пустая карта: ни одной вставки и ни одного предупреждения."""
    stream = "BT\n1 0 0 1 72 700 Tm\n(a) Tj\nET\n"

    patched, fixed, problems = repair_inherited_font(stream, {})

    assert (patched, fixed, problems) == (stream, 0, [])


def test_bundled_files_exist() -> None:
    """Шрифты с кириллицей поставляются в git (data/fonts)."""
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        path = PROJECT_ROOT / "data" / "fonts" / name
        assert path.is_file(), f"нет {path}"
        assert path.stat().st_size > 100_000


def test_license_file_present() -> None:
    """Рядом со шрифтами лежит лицензия Bitstream Vera."""
    license_path = PROJECT_ROOT / "data" / "fonts" / "LICENSE-DejaVu.txt"
    assert license_path.is_file()
    text = license_path.read_text(encoding="utf-8")
    assert "Bitstream Vera" in text
    assert "Permission is hereby granted" in text


def test_resolver_prefers_bundled_over_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При рабочем bundled-каталоге системные пути не используются."""
    monkeypatch.setattr(
        PdfFontResolver,
        "CANDIDATES",
        [("regular", ["DejaVuSans.ttf", "/nonexistent/system-font.ttf"])],
    )
    resolver = PdfFontResolver()
    block = TextBlock(
        text="x", x=0.0, y=0.0, width=10.0, height=10.0,
        font_name="Helvetica", font_size=12.0, page=0,
    )

    path = resolver.find_for_block(block)

    assert path is not None
    assert path.parent == DEFAULT_FONT_DIR
    assert resolver._source_of(path) == "bundled"


def test_resolver_works_on_windows_like_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """На «Windows» без единого системного TTF работает bundled-шрифт."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        PdfFontResolver,
        "CANDIDATES",
        [
            ("regular", ["DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"]),
            ("bold", ["DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]),
        ],
    )
    resolver = PdfFontResolver()
    block = TextBlock(
        text="x", x=0.0, y=0.0, width=10.0, height=10.0,
        font_name="Helvetica-Bold", font_size=12.0, page=0,
    )

    path = resolver.find_for_block(block)

    assert path is not None, "bundled-шрифт не найден в Windows-окружении"
    assert path.parent == DEFAULT_FONT_DIR
    assert path.name == "DejaVuSans-Bold.ttf"


def test_deb_size_increase_under_5_percent() -> None:
    """Собранный .deb вырос не более чем на 5 % относительно baseline."""
    debs = sorted((PROJECT_ROOT / "dist").glob("termru_*.deb"))
    if not debs:
        pytest.skip("нет dist/termru_*.deb — соберите packaging/build-deb.sh")

    size = debs[0].stat().st_size
    growth = (size - DEB_BASELINE_BYTES) / DEB_BASELINE_BYTES

    assert growth < 0.05, (
        f"{debs[0].name}: {size} байт (+{growth:.2%}) — больше лимита 5 %"
    )


#: Пять строк с англицизмами — каждая даёт замену с подменой base-14 шрифта.
MULTI_LINES = [
    "The feedback from the meeting was clear.",
    "Our roadmap and deadline are fixed.",
    "The stakeholder meeting was short.",
    "Onboarding and milestone are planned.",
    "The deliverable and feedback are ready.",
]


@pytest.fixture()
def multi_block_pdf(tmp_path: Path) -> Path:
    """PDF с пятью блоками Helvetica (пять подмен шрифта)."""
    path = tmp_path / "five.pdf"
    pdf = canvas.Canvas(str(path))
    pdf.setFont("Helvetica", 12)
    for index, line in enumerate(MULTI_LINES):
        pdf.drawString(72, 720 - index * 20, line)
    pdf.save()
    return path


def _run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Вызвать ``main`` и вернуть (код, stdout, stderr)."""
    from src.cli import main

    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_stderr_quiet_by_default(
    multi_block_pdf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Без --verbose внутренние трейсбеки pdf_edit_engine не попадают в stderr."""
    code, _, err = _run_cli(
        ["--input", str(multi_block_pdf), "--output", str(tmp_path / "out.pdf")],
        capsys,
    )

    assert code == 0
    assert "Font extension failed" not in err
    assert "подавлено" in err


def test_warning_emitted_once(
    multi_block_pdf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Предупреждение о подавлении — ровно одно на документ."""
    _, _, err = _run_cli(
        ["--input", str(multi_block_pdf), "--output", str(tmp_path / "out.pdf")],
        capsys,
    )

    lines = [line for line in err.splitlines() if "подавлено" in line]
    assert len(lines) == 1, lines
    assert "5 внутренних предупреждений" in lines[0]


def test_stderr_shows_traceback_with_verbose(
    multi_block_pdf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """С --verbose трейсбеки движка остаются в stderr."""
    code, _, err = _run_cli(
        [
            "--input", str(multi_block_pdf),
            "--output", str(tmp_path / "out.pdf"),
            "--verbose",
        ],
        capsys,
    )

    assert code == 0
    assert "Font extension failed" in err


def test_verbose_mode_does_not_suppress(
    multi_block_pdf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """В verbose видны все пять трейсбеков (по одному на блок)."""
    _, _, err = _run_cli(
        [
            "--input", str(multi_block_pdf),
            "--output", str(tmp_path / "out.pdf"),
            "--verbose",
        ],
        capsys,
    )

    assert err.count("Font extension failed") == len(MULTI_LINES)
    assert "подавлено" not in err


def test_engine_logging_restored_after_run(
    multi_block_pdf: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """После прогона логгер pdf_edit_engine снова всплывает в root."""
    import logging

    _run_cli(
        ["--input", str(multi_block_pdf), "--output", str(tmp_path / "out.pdf")],
        capsys,
    )

    assert logging.getLogger("pdf_edit_engine").propagate is True
    assert logging.getLogger("pdf_edit_engine").handlers == []
