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

from PySide6.QtWidgets import QApplication, QTabWidget, QToolBar

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
report["central_is_tabs"] = isinstance(window.centralWidget(), QTabWidget)
report["tab_titles"] = [
    window.tabs.tabText(index) for index in range(window.tabs.count())
]
report["word_editor_in_tab0"] = window.tabs.widget(0) is window.editor
report["pptx_editor_in_tab1"] = window.tabs.widget(1) is window.pptx_editor
report["menus"] = [action.text() for action in window.menuBar().actions()]
report["toolbar_actions"] = len(toolbars[0].actions()) if toolbars else 0
report["toolbar_count"] = len(toolbars)
report["format_actions"] = [
    action.objectName() for action in window.format_toolbar.actions() if action.objectName()
]
report["tools_menu"] = [
    action.text()
    for action in window.menuBar().actions()[2].menu().actions()
    if not action.isSeparator()
]
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


#: Проверка панели форматирования и вставки таблицы/списка.
_SCRIPT_FORMAT = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCursor, QTextListFormat

editor = window.editor
editor.setPlainText("hello world")
cursor = editor.textCursor()
cursor.select(QTextCursor.SelectionType.Document)
editor.setTextCursor(cursor)

window.action_bold.setChecked(True)
window.action_italic.setChecked(True)
window.action_underline.setChecked(True)
report["bold"] = editor.fontWeight() >= QFont.Weight.Bold
report["italic"] = bool(editor.fontItalic())
report["underline"] = bool(editor.fontUnderline())

window.action_align_center.trigger()
report["align_center"] = bool(editor.alignment() & Qt.AlignmentFlag.AlignHCenter)

editor.setPlainText("item one")
window.insert_list(QTextListFormat.Style.ListDisc)
report["has_list"] = "<ul" in editor.toHtml() or "list-style" in editor.toHtml()

window.insert_table(2, 3)
report["has_table"] = "<table" in editor.toHtml()

editor.moveCursor(QTextCursor.MoveOperation.Start)
report["bold_action_unchecked"] = not window.action_bold.isChecked()

window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка QThread-воркера с подменённым движком.
_SCRIPT_WORKER = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtCore import QEventLoop, QTimer

from src.gui.rusify import RusifyWorker


class FakeEngine:
    def replace_dict_only(self, text):
        return (
            text.replace("feedback", "обратная связь"),
            [
                {
                    "original": "feedback",
                    "replacement": "обратная связь",
                    "source": "dict",
                    "position": 4,
                }
            ],
        )


events = {"progress": [], "result": None, "error": None}
loop = QEventLoop()
worker = RusifyWorker("the feedback is clear", engine=FakeEngine())
worker.progress.connect(events["progress"].append)


def _done(text, changes):
    events["result"] = [text, changes]
    loop.quit()


def _failed(message):
    events["error"] = message
    loop.quit()


worker.result_ready.connect(_done)
worker.failed.connect(_failed)
worker.start()
QTimer.singleShot(10000, loop.quit)
loop.exec()
worker.wait(3000)

report["progress"] = events["progress"]
report["result"] = events["result"]
report["error"] = events["error"]
report["thread_finished"] = worker.isFinished()
window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка LLM-режима воркера с подменённым клиентом DeepSeek.
_SCRIPT_WORKER_LLM = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtCore import QEventLoop, QTimer

import src.gui.rusify as rusify
from src.gui.rusify import RusifyWorker


class FakeClient:
    calls = []

    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key

    def replace_text(self, text, glossary, exceptions, log_callback=None, block_id=None):
        FakeClient.calls.append(
            {"key": self.api_key, "block_id": block_id, "text": text}
        )
        return text.replace("report is ready", "отчёт готов")


rusify.DeepSeekClient = FakeClient

events = {"result": None, "error": None}
loop = QEventLoop()
worker = RusifyWorker("The feedback and report is ready", use_llm=True, api_key="test-key")
worker.result_ready.connect(
    lambda text, changes: (events.update(result=[text, list(changes)]), loop.quit())
)
worker.failed.connect(lambda message: (events.update(error=message), loop.quit()))
worker.start()
QTimer.singleShot(10000, loop.quit)
loop.exec()
worker.wait(3000)

report["result"] = events["result"]
report["error"] = events["error"]
report["calls"] = FakeClient.calls
window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка обработки ошибки API в LLM-режиме.
_SCRIPT_WORKER_LLM_ERROR = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtCore import QEventLoop, QTimer

import src.gui.rusify as rusify
from src.gui.rusify import RusifyWorker
from src.deepseek_client import DeepSeekAPIError


class BrokenClient:
    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key

    def replace_text(self, *args, **kwargs):
        raise DeepSeekAPIError("Неверный ключ")


rusify.DeepSeekClient = BrokenClient

events = {"error": None}
loop = QEventLoop()
worker = RusifyWorker("The feedback is ready", use_llm=True, api_key="bad-key")
worker.result_ready.connect(lambda text, changes: loop.quit())
worker.failed.connect(lambda message: (events.update(error=message), loop.quit()))
worker.start()
QTimer.singleShot(10000, loop.quit)
loop.exec()
worker.wait(3000)

report["error"] = events["error"]
window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка диалога предпросмотра и применения замен в редакторе.
_SCRIPT_PREVIEW = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtCore import Qt

from src.gui.rusify import ReplacementsDialog

changes = [
    {"original": "feedback", "replacement": "обратная связь", "source": "dict"},
    {"original": "meeting", "replacement": "встреча", "source": "llm"},
]
dialog = ReplacementsDialog(changes, window)
report["rows"] = dialog.table.rowCount()
report["headers"] = [
    dialog.table.horizontalHeaderItem(index).text() for index in range(4)
]
report["sources"] = [dialog.table.item(row, 2).text() for row in range(dialog.table.rowCount())]
report["selected_before"] = [pair[0] for pair in dialog.selected_replacements()]
dialog.table.item(1, 3).setCheckState(Qt.CheckState.Unchecked)
report["selected_after"] = [pair[0] for pair in dialog.selected_replacements()]
dialog.deleteLater()

window.editor.setPlainText("the feedback and meeting are here")
report["applied"] = window.apply_replacements(
    [("feedback", "обратная связь"), ("meeting", "встреча")]
)
report["text_after"] = window.editor.toPlainText()

# Замена внутри жирного фрагмента не должна терять формат.
window.editor.setHtml("<p>Our <b>feedback</b> is ready</p>")
window.apply_replacements([("feedback", "обратная связь")])
html_after = window.editor.toHtml()
report["bold_kept"] = "font-weight:700" in html_after and "обратная связь" in html_after

window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Проверка загрузки PPTX: список слайдов, блоки, пустой слайд, битый файл.
_SCRIPT_PPTX_LOAD = (
    _SCRIPT_PREAMBLE
    + """
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches
from PySide6.QtWidgets import QLabel

import src.gui.main_window as main_window
import src.gui.pptx_editor as pptx_module

path = sys.argv[1]

prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Quarterly feedback"
body = slide.placeholders[1]
body.text = "first line"
body.text_frame.add_paragraph().text = "second line"

table_slide = prs.slides.add_slide(prs.slide_layouts[6])
frame = table_slide.shapes.add_table(1, 2, Inches(1), Inches(1), Inches(5), Inches(1))
frame.table.cell(0, 0).text = "TLS handshake"
frame.table.cell(0, 1).text = "milestone owner"

prs.slides.add_slide(prs.slide_layouts[6])
prs.save(path)

report["opened"] = window.open_path(path)
report["active_tab"] = window.tabs.currentIndex()
report["tab_title"] = window.tabs.tabText(window.tabs.currentIndex())

editor = window.pptx_editor
report["slide_count"] = editor.slide_list.count()
report["slide_names"] = [
    editor.slide_list.item(index).text() for index in range(editor.slide_list.count())
]
report["blocks_slide0"] = editor.block_count()
report["captions_slide0"] = editor.block_captions()
report["texts_slide0"] = editor.block_texts()
report["modified_after_load"] = editor.is_modified()
report["status_path"] = window.status_path.text()
report["status_counts"] = window.status_counts.text()
report["title"] = window.windowTitle()
report["russify_label"] = window.action_russify.text()
report["format_hidden"] = window.format_toolbar.isHidden()

# Таблица на слайде тоже даёт текстовые блоки -- по одному на ячейку.
editor.select_slide(1)
report["table_blocks"] = editor.block_count()
report["table_texts"] = editor.block_texts()
report["table_captions"] = editor.block_captions()

# Пустой слайд: полей нет, показана заглушка.
editor.select_slide(2)
report["blank_blocks"] = editor.block_count()
hint = editor.findChild(QLabel, "emptyHint")
report["blank_hint"] = hint.text() if hint is not None else None

# Русификация пустого слайда: сообщение вместо запуска воркера.
messages = []
main_window.QMessageBox.information = (
    lambda *args, **kwargs: messages.append(list(args[1:3]))
)
window.russify_document()
report["empty_message"] = messages
report["worker_after_empty"] = getattr(window, "_worker", None) is not None

# Правка сохраняется при переходе на другой слайд и обратно.
editor.select_slide(0)
editor.block_editors()[0].setPlainText("Черновик заголовка")
report["modified_after_edit"] = editor.is_modified()
editor.select_slide(2)
editor.select_slide(0)
report["text_after_switch"] = editor.block_texts()[0]

# Битый файл: ошибка в диалоге, открытая презентация остаётся на месте.
errors = []
pptx_module.QMessageBox.critical = lambda *args, **kwargs: errors.append(list(args[1:3]))
broken = Path(path).with_name("broken.pptx")
broken.write_bytes(b"this is not a presentation")
report["broken_result"] = editor.load_file(str(broken))
report["broken_error"] = bool(errors)
report["slides_after_error"] = editor.slide_count()
report["text_kept_after_error"] = editor.block_texts()[0]

window.pptx_editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Проверка сохранения PPTX: правка QTextEdit доезжает до файла.
_SCRIPT_PPTX_SAVE = (
    _SCRIPT_PREAMBLE
    + """
from pptx import Presentation

src, dst = sys.argv[1], sys.argv[2]

prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
title = slide.shapes.title
title.text = "Quarterly feedback"
title.text_frame.paragraphs[0].runs[0].font.bold = True
body = slide.placeholders[1]
body.text = "first line"
body.text_frame.add_paragraph().text = "second line"
prs.save(src)

editor = window.pptx_editor
report["opened"] = editor.load_file(src)
report["blocks_before"] = editor.block_texts()

editors = editor.block_editors()
editors[0].setPlainText("Ежеквартальный отчёт")
editors[1].setPlainText("строка один\\nстрока два")
report["modified_before_save"] = editor.is_modified()

report["saved"] = editor.save_file(dst)
report["modified_after_save"] = editor.is_modified()
report["current_path"] = editor.current_path.name

back = Presentation(dst)
report["saved_slides"] = len(back.slides)
report["saved_texts"] = [
    shape.text_frame.text for shape in back.slides[0].shapes if shape.has_text_frame
]
report["saved_title_runs"] = [
    [run.text, bool(run.font.bold)]
    for paragraph in back.slides[0].shapes.title.text_frame.paragraphs
    for run in paragraph.runs
]

window.pptx_editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка русификации на вкладке PPTX: маршрутизация замен и запуск воркера.
_SCRIPT_PPTX_RUSIFY = (
    _SCRIPT_PREAMBLE
    + """
from pptx import Presentation

import src.gui.main_window as main_window

src, dst = sys.argv[1], sys.argv[2]

prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "Product feedback"
slide.shapes.title.text_frame.paragraphs[0].runs[0].font.bold = True
slide.placeholders[1].text = "meeting notes"
prs.save(src)

# Диалоги не показываем: подменяем на «Применить»/«Отмена» без окна.
main_window.QMessageBox.information = lambda *args, **kwargs: None
main_window.QMessageBox.question = lambda *args, **kwargs: None
main_window.ReplacementsDialog.exec = (
    lambda self: main_window.ReplacementsDialog.DialogCode.Accepted
)
main_window.ReplacementsDialog.selected_replacements = lambda self: [
    ("feedback", "обратная связь"),
    ("meeting", "встреча"),
    ("нет такого", "x"),
]

editor = window.pptx_editor
report["opened"] = window.open_path(src)
report["texts_before"] = editor.block_texts()
report["current_text_before"] = editor.current_text()

# Ветка применения: _on_rusify_ready направляет замены в PPTX-редактор.
window._rusify_target = "pptx"
window._on_rusify_ready(
    "",
    [
        {"original": "feedback", "replacement": "обратная связь", "source": "dict"},
        {"original": "meeting", "replacement": "встреча", "source": "llm"},
    ],
)
report["texts_after_ready"] = editor.block_texts()
report["status_after_ready"] = window.statusBar().currentMessage()
report["modified_after_ready"] = editor.is_modified()

report["saved"] = editor.save_file(dst)
back = Presentation(dst)
report["saved_texts"] = [
    shape.text_frame.text for shape in back.slides[0].shapes if shape.has_text_frame
]
report["bold_after"] = [
    bool(run.font.bold)
    for paragraph in back.slides[0].shapes.title.text_frame.paragraphs
    for run in paragraph.runs
]

# Ветка запуска: кнопка берёт текст слайда и блокирует правку на время работы.
main_window.ReplacementsDialog.exec = (
    lambda self: main_window.ReplacementsDialog.DialogCode.Rejected
)
window.russify_document()
report["worker_started"] = window._worker is not None and window._worker.isRunning()
report["worker_text"] = window._worker._text
report["editors_locked"] = all(
    block.isReadOnly() for block in editor.block_editors()
)
report["slide_list_locked"] = not editor.slide_list.isEnabled()
window._worker.wait(20000)
report["worker_progressed"] = window._worker.isFinished()

window.pptx_editor.mark_saved()
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
    """Окно содержит вкладки, меню, панель инструментов и статус-бар."""
    report = _run_gui_script(_SCRIPT_UI)

    assert report["central_is_tabs"] is True
    assert report["tab_titles"] == ["Word (DOCX)", "PowerPoint (PPTX)"], report["tab_titles"]
    assert report["word_editor_in_tab0"] is True
    assert report["pptx_editor_in_tab1"] is True
    assert len(report["menus"]) == 4, report["menus"]
    assert report["toolbar_actions"] >= 3
    assert report["toolbar_count"] == 2
    assert report["tools_menu"] == ["Настройки…", "Русифицировать документ"], report["tools_menu"]
    assert report["status_path"] == "Новый документ"
    assert report["status_counts"] == "Слов: 0 · Символов: 0"
    assert report["title"].startswith("TermRu — Редактор документов")


def test_format_toolbar_actions_exist() -> None:
    """На панели форматирования есть B/I/U, выравнивание, списки и таблица."""
    report = _run_gui_script(_SCRIPT_UI)

    assert report["format_actions"] == [
        "action_bold",
        "action_italic",
        "action_underline",
        "action_align_left",
        "action_align_center",
        "action_align_right",
        "action_bullet_list",
        "action_numbered_list",
        "action_table",
    ]


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


def test_format_actions_apply() -> None:
    """Жирный/курсив/подчёркивание и выравнивание меняют формат выделения."""
    report = _run_gui_script(_SCRIPT_FORMAT)

    assert report["bold"] is True
    assert report["italic"] is True
    assert report["underline"] is True
    assert report["align_center"] is True
    assert report["has_list"] is True, "маркированный список не создался"
    assert report["has_table"] is True, "таблица не вставлена"
    assert report["bold_action_unchecked"] is True, "кнопка не синхронизировалась"


def test_rusify_worker_emits_signals() -> None:
    """QThread-воркер отдаёт прогресс и результат, не блокируя поток."""
    report = _run_gui_script(_SCRIPT_WORKER)

    assert report["error"] is None, report["error"]
    assert report["progress"] == [10, 40, 100], report["progress"]
    assert report["result"] is not None
    new_text, changes = report["result"]
    assert new_text == "the обратная связь is clear"
    assert changes and changes[0]["original"] == "feedback"
    assert report["thread_finished"] is True


def test_rusify_worker_with_mock_llm() -> None:
    """С use_llm воркер добавляет diff-пары от ИИ к словарным."""
    report = _run_gui_script(_SCRIPT_WORKER_LLM)

    assert report["error"] is None, report["error"]
    assert report["result"] is not None
    new_text, changes = report["result"]
    assert "отчёт готов" in new_text

    sources = {item["source"] for item in changes}
    assert "dict" in sources and "llm" in sources, sources

    llm_pairs = [
        (item["original"], item["replacement"]) for item in changes if item["source"] == "llm"
    ]
    assert ("report is ready", "отчёт готов") in llm_pairs, llm_pairs

    assert report["calls"], "LLM не вызывалась"
    assert report["calls"][0]["key"] == "test-key"


def test_rusify_worker_reports_api_error() -> None:
    """Ошибка API превращается в сигнал failed с понятным текстом."""
    report = _run_gui_script(_SCRIPT_WORKER_LLM_ERROR)

    assert report["error"] is not None, "ошибка не передана"
    assert "Ошибка API" in report["error"], report["error"]


def test_replacements_dialog_and_apply() -> None:
    """Диалог показывает замены, галочки снимаются, текст применяется."""
    report = _run_gui_script(_SCRIPT_PREVIEW)

    assert report["rows"] == 2
    assert report["headers"] == ["Оригинал", "Замена", "Источник", "Применить"]
    assert report["sources"] == ["Словарь", "ИИ"], report["sources"]
    assert report["selected_before"] == ["feedback", "meeting"]
    assert report["selected_after"] == ["feedback"], "снятая галочка не учтена"

    assert report["applied"] == 2
    assert report["text_after"] == "the обратная связь and встреча are here"
    assert report["bold_kept"] is True, "замена потеряла жирное начертание"


def test_pptx_editor_load(tmp_path: Path) -> None:
    """PPTX открывается: список слайдов, блоки, пустой слайд, битый файл."""
    path = tmp_path / "deck.pptx"
    report = _run_gui_script(_SCRIPT_PPTX_LOAD, str(path))

    assert report["opened"] is True
    assert report["active_tab"] == 1, "вкладка не переключилась на PPTX"
    assert report["tab_title"] == "PowerPoint (PPTX)"

    assert report["slide_count"] == 3, report["slide_names"]
    assert report["slide_names"] == [
        "Слайд 1: Quarterly feedback",
        "Слайд 2",
        "Слайд 3",
    ], report["slide_names"]

    assert report["blocks_slide0"] == 2
    assert report["captions_slide0"][0].startswith("Заголовок")
    assert report["captions_slide0"][1].startswith("Содержимое")
    assert report["texts_slide0"] == ["Quarterly feedback", "first line\nsecond line"]
    assert report["modified_after_load"] is False

    assert report["status_path"] == str(path)
    assert report["status_counts"] == "Слайдов: 3 · Блоков: 2", report["status_counts"]
    assert report["title"] == f"TermRu — Редактор документов — deck.pptx[*]"
    assert report["russify_label"] == "Русифицировать слайд"
    assert report["format_hidden"] is True, "панель форматирования DOCX осталась видна"

    assert report["table_blocks"] == 2, "ячейки таблицы не попали в редактор"
    assert report["table_texts"] == ["TLS handshake", "milestone owner"]
    assert "R1C1" in report["table_captions"][0], report["table_captions"]
    assert "R1C2" in report["table_captions"][1], report["table_captions"]

    assert report["blank_blocks"] == 0
    assert report["blank_hint"] == "Нет текстовых блоков", report["blank_hint"]

    assert report["empty_message"], "нет сообщения о пустом слайде"
    assert report["empty_message"][0][1] == "На слайде нет текста."
    assert report["worker_after_empty"] is False, "воркер запустился для пустого слайда"

    assert report["modified_after_edit"] is True
    assert report["text_after_switch"] == "Черновик заголовка", "правка потерялась"

    assert report["broken_result"] is False
    assert report["broken_error"] is True, "ошибка открытия не показана"
    assert report["slides_after_error"] == 3, "битый файл сбросил открытую презентацию"
    assert report["text_kept_after_error"] == "Черновик заголовка"


def test_pptx_editor_save(tmp_path: Path) -> None:
    """Изменённый текст QTextEdit сохраняется в PPTX без потери жирного."""
    source = tmp_path / "in.pptx"
    target = tmp_path / "out.pptx"

    report = _run_gui_script(_SCRIPT_PPTX_SAVE, str(source), str(target))

    assert report["opened"] is True
    assert report["blocks_before"] == ["Quarterly feedback", "first line\nsecond line"]
    assert report["modified_before_save"] is True

    assert report["saved"] is True
    assert target.is_file()
    assert report["modified_after_save"] is False
    assert report["current_path"] == "out.pptx"

    assert report["saved_slides"] == 1
    assert report["saved_texts"] == [
        "Ежеквартальный отчёт",
        "строка один\nстрока два",
    ], report["saved_texts"]
    assert report["saved_title_runs"] == [["Ежеквартальный отчёт", True]]


def test_pptx_rusify_flow(tmp_path: Path) -> None:
    """Русификация на вкладке PPTX правит слайд и запускает воркер."""
    source = tmp_path / "in.pptx"
    target = tmp_path / "out.pptx"

    report = _run_gui_script(_SCRIPT_PPTX_RUSIFY, str(source), str(target))

    assert report["opened"] is True
    assert report["current_text_before"] == "Product feedback\n\nmeeting notes"

    assert report["texts_after_ready"] == ["Product обратная связь", "встреча notes"]
    assert report["status_after_ready"] == "Применено замен: 2"
    assert report["modified_after_ready"] is True

    assert report["saved"] is True
    assert report["saved_texts"] == ["Product обратная связь", "встреча notes"]
    assert report["bold_after"] == [True], "замена потеряла жирное начертание"

    assert report["worker_started"] is True, "воркер не запустился"
    assert report["worker_text"] == "Product обратная связь\n\nвстреча notes"
    assert report["editors_locked"] is True, "правка не заблокирована на время работы"
    assert report["slide_list_locked"] is True, "список слайдов не заблокирован"
    assert report["worker_progressed"] is True


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

