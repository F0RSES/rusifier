"""Smoke-тесты GUI: окно поднимается, DOCX открывается и сохраняется.

Qt запускается в offscreen-режиме, поэтому дисплей в CI не нужен.

Проверки выполняются в отдельном процессе: в некоторых окружениях
``LD_LIBRARY_PATH`` содержит чужую сборку Qt, и тогда PySide6 падает на
``undefined symbol``. Загрузчик читает эту переменную один раз при старте
процесса, поэтому изолировать окружение можно только подпроцессом — там мы
подставляем ``Qt/lib`` из самого пакета PySide6.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DOCX = PROJECT_ROOT / "tests" / "fixtures" / "sample.docx"

#: Общая часть пробного скрипта: приложение и главное окно.
_SCRIPT_PREAMBLE = """
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QToolBar

from src.gui.docx_editor import DocxEditor
from src.gui.main_window import MainWindow

app = QApplication([])
window = MainWindow()
report = {}
"""

#: Проверка сборки интерфейса.
_SCRIPT_UI = (
    _SCRIPT_PREAMBLE
    + """
toolbars = window.findChildren(QToolBar)
report["central_is_editor"] = isinstance(window.centralWidget(), DocxEditor)
report["menus"] = [action.text() for action in window.menuBar().actions()]
report["toolbar_actions"] = len(toolbars[0].actions()) if toolbars else 0
report["status_path"] = window.status_path.text()
report["status_counts"] = window.status_counts.text()
report["title"] = window.windowTitle()
window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка открытия и сохранения реального DOCX.
_SCRIPT_ROUNDTRIP = (
    _SCRIPT_PREAMBLE
    + """
from docx import Document

sample = sys.argv[1]
target = sys.argv[2]

opened = window.editor.load_file(sample)
html = window.editor.toHtml()
text = window.editor.toPlainText()
report["opened"] = bool(opened)
report["has_table_html"] = "<table" in html
report["has_bold_html"] = "font-weight:700" in html or "<strong" in html
report["text_has_roadmap"] = "product roadmap" in text
report["text_has_cells"] = "TLS handshake" in text and "milestone owner" in text
report["status_path"] = window.status_path.text()
report["modified_after_load"] = window.editor.is_modified()

saved = window.editor.save_file(target)
report["saved"] = bool(saved)
report["modified_after_save"] = window.editor.is_modified()

saved_doc = Document(target)
report["saved_tables"] = len(saved_doc.tables)
report["saved_columns"] = len(saved_doc.tables[0].columns) if saved_doc.tables else 0
report["saved_row"] = (
    [cell.text for cell in saved_doc.tables[0].rows[0].cells]
    if saved_doc.tables
    else []
)
report["saved_bold"] = [
    run.text for para in saved_doc.paragraphs for run in para.runs if run.bold
]
report["saved_headings"] = [
    para.style.name
    for para in saved_doc.paragraphs
    if para.style.name.startswith("Heading")
]
report["saved_first_paragraph"] = [
    para.text for para in saved_doc.paragraphs if para.text.strip()
][:1]
report["saved_css_leak"] = any(
    "white-space" in para.text or "border-width" in para.text
    for para in saved_doc.paragraphs
)

window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка флага изменений и ошибки открытия.
_SCRIPT_MODIFIED = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtGui import QTextCursor

sample = sys.argv[1]
window.editor.load_file(sample)
report["after_load"] = window.editor.is_modified()

window.editor.moveCursor(QTextCursor.MoveOperation.End)
window.editor.insertPlainText(" Дополнительный текст.")
report["after_edit"] = window.editor.is_modified()

import src.gui.docx_editor as gui_editor
shown = []
gui_editor.QMessageBox.critical = lambda *a, **k: shown.append(a[1:3])
report["missing_file_result"] = window.editor.load_file("/nonexistent/нет.docx")
report["error_shown"] = bool(shown)

window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


def _gui_environment() -> dict[str, str]:
    """Окружение подпроцесса: offscreen и Qt-библиотеки из PySide6.

    Returns:
        dict[str, str]: переменные окружения для запуска пробы.
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    qt_lib = (
        Path(sys.prefix)
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "PySide6"
        / "Qt"
        / "lib"
    )
    if qt_lib.is_dir():
        # Приоритет над чужой Qt из родительского LD_LIBRARY_PATH.
        env["LD_LIBRARY_PATH"] = str(qt_lib)
    return env


def _run_gui_script(script: str, *args: str) -> dict:
    """Выполнить пробу в подпроцессе и вернуть её JSON-отчёт.

    Args:
        script: Текст скрипта (``python -c``).
        *args: Аргументы скрипта.

    Returns:
        dict: Разобранный JSON.

    Raises:
        AssertionError: если подпроцесс упал или не вернул JSON.
    """
    completed = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(PROJECT_ROOT),
        env=_gui_environment(),
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, completed.stdout
    return json.loads(lines[-1])


def test_main_window_builds_ui() -> None:
    """Окно содержит меню, панель инструментов, редактор и статус-бар."""
    report = _run_gui_script(_SCRIPT_UI)

    assert report["central_is_editor"] is True
    assert len(report["menus"]) == 4, report["menus"]
    assert report["toolbar_actions"] >= 3
    assert report["status_path"] == "Новый документ"
    assert report["status_counts"] == "Слов: 0 · Символов: 0"
    assert report["title"].startswith("TermRu — Редактор документов")


def test_open_and_save_keep_table_and_bold(tmp_path: Path) -> None:
    """DOCX открывается и сохраняется без потери таблицы, жирного и заголовка."""
    target = tmp_path / "roundtrip.docx"

    report = _run_gui_script(_SCRIPT_ROUNDTRIP, str(SAMPLE_DOCX), str(target))

    assert report["opened"] is True
    assert report["has_table_html"] is True
    assert report["has_bold_html"] is True
    assert report["text_has_roadmap"] is True
    assert report["text_has_cells"] is True
    assert report["status_path"] == str(SAMPLE_DOCX)
    assert report["modified_after_load"] is False

    assert report["saved"] is True
    assert target.is_file()
    assert report["modified_after_save"] is False

    assert report["saved_tables"] == 1, "таблица потерялась"
    assert report["saved_columns"] == 2, "потерялась колонка таблицы"
    assert report["saved_row"] == ["TLS handshake", "milestone owner"]
    assert "product roadmap" in report["saved_bold"], report["saved_bold"]
    assert report["saved_headings"][:1] == ["Heading 1"]
    assert report["saved_css_leak"] is False, "CSS из <style> попал в текст документа"
    assert report["saved_first_paragraph"] == ["Quarterly review"]


def test_modified_flag_and_error_reporting() -> None:
    """Флаг изменений реагирует на правку; ошибка открытия показывается."""
    report = _run_gui_script(_SCRIPT_MODIFIED, str(SAMPLE_DOCX))

    assert report["after_load"] is False
    assert report["after_edit"] is True
    assert report["missing_file_result"] is False
    assert report["error_shown"] is True


def test_gui_entrypoint_imports() -> None:
    """``python -m src.gui.main`` импортируется без запуска цикла событий."""
    script = """
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from src.gui.main import main
print("main callable:", callable(main))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(PROJECT_ROOT),
        env=_gui_environment(),
    )

    assert completed.returncode == 0, completed.stderr[-3000:]
    assert "main callable: True" in completed.stdout

