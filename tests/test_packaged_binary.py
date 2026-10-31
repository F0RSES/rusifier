"""Проверки собранного бинарника: DOCX, PPTX и PDF открываются и обрабатываются.

Тесты работают с артефактом сборки (``dist/termru/termru`` из onedir или
``dist/termru`` из onefile), а не с виртуальным окружением: именно здесь
проявляются ошибки ``--exclude-module`` в ``unifypy.json`` (например,
исключённый модуль polydoc или нехватка бинарника numpy).

Если сборки нет — тесты пропускаются: ``pytest`` в чистом клоне не требует
тяжёлых артефактов.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from pptx import Presentation
from pptx.util import Inches
from reportlab.pdfgen import canvas

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Где может лежать собранный бинарник (onedir, onefile, установленный deb).
_BINARY_CANDIDATES = (
    PROJECT_ROOT / "dist" / "termru" / "termru",
    PROJECT_ROOT / "dist" / "termru",
    Path("/opt/termru/termru"),
)

#: Сколько ждать один прогон собранного приложения.
TIMEOUT_SECONDS = 180


def _find_binary() -> Path | None:
    """Найти собранный бинарник TermRu.

    Returns:
        Path | None: путь к исполняемому файлу либо ``None``, если сборки нет.
    """
    for candidate in _BINARY_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


@pytest.fixture(scope="module")
def packaged_binary() -> Path:
    """Путь к собранному бинарнику (иначе — skip всего модуля)."""
    binary = _find_binary()
    if binary is None:
        pytest.skip("нет собранного бинарника (dist/termru или /opt/termru/termru)")
    return binary


def _run(binary: Path, source: Path, output: Path) -> tuple[int, dict, str]:
    """Запустить бинарник на документе и разобрать JSON-ответ.

    Args:
        binary: Собранный исполняемый файл.
        source: Входной документ.
        output: Куда записать результат.

    Returns:
        tuple[int, dict, str]: (код возврата, разобранный JSON, stderr).
    """
    completed = subprocess.run(
        [
            str(binary),
            "--input",
            str(source),
            "--output",
            str(output),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=str(PROJECT_ROOT),
    )
    payload = completed.stdout.strip()
    data = json.loads(payload.splitlines()[-1]) if payload else {}
    return completed.returncode, data, completed.stderr


def _make_docx(path: Path) -> Path:
    """DOCX с англицизмами (замена словарём, без сети)."""
    document = DocxDocument()
    document.add_heading("Q3 report", level=1)
    document.add_paragraph("The feedback from the meeting was clear.")
    document.add_paragraph("Deadline and roadmap stay as they are.")
    document.save(path)
    return path


def _make_pptx(path: Path) -> Path:
    """PPTX с англицизмами (замена словарём, без сети)."""
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "Roadmap"
    slide.placeholders[1].text = "The feedback from the meeting was clear."
    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "Deadline"
    textbox = second.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    textbox.text_frame.text = "Onboarding and milestone feedback"
    presentation.save(path)
    return path


def _make_pdf(path: Path) -> Path:
    """PDF с base-14 Helvetica — путь через внедрение CID-шрифта."""
    document = canvas.Canvas(str(path))
    document.setFont("Helvetica", 12)
    document.drawString(72, 700, "The feedback from the meeting was clear.")
    document.drawString(72, 680, "Deadline and roadmap stay as they are.")
    document.save()
    return path


def test_packaged_binary_starts(packaged_binary: Path) -> None:
    """Собранный бинарник отвечает на ``--help`` (импорты polydoc/pdf_edit_engine целы)."""
    completed = subprocess.run(
        [str(packaged_binary), "--help"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "--input" in completed.stdout


def test_deb_opens_docx(packaged_binary: Path, tmp_path: Path) -> None:
    """DOCX: exit=0, ``status == "ok"`` и замены в результате."""
    source = _make_docx(tmp_path / "sample.docx")
    output = tmp_path / "sample_ru.docx"

    code, data, stderr = _run(packaged_binary, source, output)

    assert code == 0, stderr[-2000:]
    assert data.get("status") == "ok", data
    assert output.is_file()
    assert data["summary"]["changed_blocks"] > 0


def test_deb_opens_pptx(packaged_binary: Path, tmp_path: Path) -> None:
    """PPTX: exit=0, ``status == "ok"`` и замены в результате."""
    source = _make_pptx(tmp_path / "sample.pptx")
    output = tmp_path / "sample_ru.pptx"

    code, data, stderr = _run(packaged_binary, source, output)

    assert code == 0, stderr[-2000:]
    assert data.get("status") == "ok", data
    assert output.is_file()
    assert data["summary"]["changed_blocks"] > 0


def test_deb_opens_pdf(packaged_binary: Path, tmp_path: Path) -> None:
    """PDF: exit=0, ``status == "ok"``, кириллица записана в файл."""
    source = _make_pdf(tmp_path / "sample.pdf")
    output = tmp_path / "sample_ru.pdf"

    code, data, stderr = _run(packaged_binary, source, output)

    assert code == 0, stderr[-2000:]
    assert data.get("status") == "ok", data
    assert output.is_file()
    assert data["summary"]["changed_blocks"] > 0

    pymupdf = pytest.importorskip("pymupdf", reason="нужен для чтения текста PDF")
    with pymupdf.open(str(output)) as document:
        text = document[0].get_text()
    assert "обратная связь" in text, text


def test_deb_formats_registered(packaged_binary: Path) -> None:
    """Исключённые форматы polydoc не утащили за собой docx/pptx/pdf.

    Регистрация форматов в polydoc «мягкая»: если модуль не загрузился, он
    попадает в список ``unavailable``. Проверяем, что нужных форматов там нет.
    """
    completed = subprocess.run(
        [str(packaged_binary), "--help"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )
    combined = completed.stdout + completed.stderr
    for fmt in ("'docx'", "'pptx'", "'pdf'"):
        assert f"{fmt} reader is unavailable" not in combined, combined[-2000:]
        assert f"{fmt} writer is unavailable" not in combined, combined[-2000:]
    # Исключённые форматы не должны шуметь в stderr (фильтр в termru_entry.py).
    assert "is unavailable" not in combined, combined[-2000:]


def test_binary_size_budget(packaged_binary: Path) -> None:
    """Бандл не раздулся обратно: страховка от регрессии ``exclude_module``."""
    binary_bytes = packaged_binary.stat().st_size
    internal = packaged_binary.parent / "_internal"
    total = binary_bytes
    if internal.is_dir():
        total += sum(item.stat().st_size for item in internal.rglob("*") if item.is_file())
    assert total < 160 * 1024 * 1024, (
        f"бандл {total / 1024 / 1024:.0f} МБ — excludes не применились"
    )
