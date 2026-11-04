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
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel, QStackedWidget, QToolBar

# Изоляция QSettings: тесты не должны менять профиль пользователя.
QSettings.setPath(
    QSettings.Format.NativeFormat, QSettings.Scope.UserScope, tempfile.mkdtemp()
)

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
report["central_is_stack"] = isinstance(window.centralWidget(), QStackedWidget)
report["stack_count"] = window.stack.count()
report["start_index"] = window.stack.currentIndex()
report["start_screen_is_page0"] = window.stack.widget(0) is window.start_screen
report["editor_is_page1"] = window.stack.widget(1) is window.editor_screen
report["ribbon_in_editor_screen"] = (
    window.editor_screen.layout().itemAt(0).widget() is window.ribbon
)
report["tabs_in_editor_screen"] = (
    window.editor_screen.layout().itemAt(1).widget() is window.tabs
)
report["toolbar_count"] = len(window.findChildren(QToolBar))
report["ribbon_tabs"] = window.ribbon.tab_names()
report["ribbon_current"] = window.ribbon.current_tab()
report["ribbon_home"] = window.ribbon.actions_on("Главная")
report["ribbon_insert"] = window.ribbon.actions_on("Вставка")
report["ribbon_termru"] = window.ribbon.actions_on("TermRu")
report["ribbon_view_groups"] = sorted(
    title for (tab, title) in window.ribbon._groups if tab == "Вид"
)
report["ribbon_quick"] = [
    window.ribbon.quick_layout.itemAt(i).widget().defaultAction().objectName()
    for i in range(window.ribbon.quick_layout.count())
]
report["ribbon_icons_ok"] = all(
    not action.icon().isNull()
    for action in (
        window.action_bold,
        window.action_italic,
        window.action_table,
        window.action_russify,
    )
)
report["ribbon_use_llm"] = window.ribbon.use_llm_check.text()
report["ribbon_zoom"] = window.ribbon.zoom_slider.value()
report["start_heading"] = window.start_screen.findChild(QLabel, "startTitle").text()
report["start_subtitle"] = window.start_screen.findChild(QLabel, "startSubtitle").text()
report["start_cards"] = sorted(window.start_screen._cards)
report["start_icons_ok"] = all(
    label.pixmap() is not None and not label.pixmap().isNull()
    for label in window.start_screen.card_icons.values()
)
report["nav_buttons"] = sorted(window.start_screen.nav_buttons)
report["recent_files"] = window.start_screen.recent_files()
report["tab_titles"] = [
    window.tabs.tabText(index) for index in range(window.tabs.count())
]
report["word_editor_in_tab0"] = window.tabs.widget(0) is window.editor
report["pptx_editor_in_tab1"] = window.tabs.widget(1) is window.pptx_editor
report["pdf_editor_in_tab2"] = window.tabs.widget(2) is window.pdf_editor
report["menus"] = [action.text() for action in window.menuBar().actions()]
report["file_menu"] = [
    action.text()
    for action in window.menuBar().actions()[0].menu().actions()
    if not action.isSeparator()
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

# Форматирование доступно только в редакторе: на стартовом экране действия выключены.
window.show_editor()

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
        return text.replace("audit is ready", "аудит готов")


rusify.DeepSeekClient = FakeClient

events = {"result": None, "error": None}
loop = QEventLoop()
worker = RusifyWorker("The feedback and audit is ready", use_llm=True, api_key="test-key")
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
report["format_disabled"] = not window.action_bold.isEnabled()

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


def _write_glossary_files(
    directory: Path, glossary: dict[str, str], exceptions: list[str]
) -> tuple[Path, Path]:
    """Создать временные ``glossary.json`` и ``exceptions.json``.

    Args:
        directory: Каталог для файлов.
        glossary: Пары «термин → замена».
        exceptions: Токены исключений.

    Returns:
        tuple[Path, Path]: пути к глоссарию и исключениям.

    Raises:
        Ничего не бросает.
    """
    glossary_path = directory / "glossary.json"
    exceptions_path = directory / "exceptions.json"
    glossary_path.write_text(
        json.dumps(glossary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    exceptions_path.write_text(
        json.dumps(dict.fromkeys(exceptions), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return glossary_path, exceptions_path


def _write_sample_pdf(path: Path) -> None:
    """Создать двухстраничный PDF для тестов редактора.

    Args:
        path: Куда записать файл.

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(path), pagesize=A4)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(60, 780, "Quarterly review")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(60, 740, "Our product roadmap is ready")
    pdf.drawString(60, 720, "The feedback from the meeting is below")
    pdf.showPage()
    pdf.setFont("Helvetica", 12)
    pdf.drawString(60, 780, "Second page with deadline")
    pdf.showPage()
    pdf.save()


#: Проверка загрузки PDF: страницы, рендер, блоки, битый файл.
_SCRIPT_PDF_LOAD = (
    _SCRIPT_PREAMBLE
    + """
from pathlib import Path

import src.gui.main_window as main_window

path = sys.argv[1]

report["opened"] = window.open_path(path)
report["active_tab"] = window.tabs.currentIndex()
report["tab_title"] = window.tabs.tabText(window.tabs.currentIndex())

editor = window.pdf_editor
report["pages"] = editor.page_count()
report["page_names"] = [
    editor.page_list.item(index).text()
    for index in range(editor.page_list.count())
]
report["current_page"] = editor.page_list.currentRow()
report["page1_ids"] = editor.block_ids()
report["page1_texts"] = editor.block_texts()
report["total_blocks"] = editor.total_block_count()
pixmap = editor.image_label.pixmap()
report["render_null"] = pixmap.isNull()
report["render_size"] = [pixmap.width(), pixmap.height()]
report["raw_size"] = [
    editor._page_pixmap.width(),
    editor._page_pixmap.height(),
] if editor._page_pixmap is not None else None
report["modified_after_load"] = editor.is_modified()
report["status_path"] = window.status_path.text()
report["status_counts"] = window.status_counts.text()
report["title"] = window.windowTitle()
report["russify_label"] = window.action_russify.text()
report["format_disabled"] = not window.action_bold.isEnabled()

# Вторая страница: свой набор блоков.
editor.select_page(1)
report["page2_ids"] = editor.block_ids()
report["page2_texts"] = editor.block_texts()
report["status_after_page2"] = window.status_counts.text()

# Правка не теряется при возврате на первую страницу.
editor.select_page(0)
editor.block_editors()[0].setPlainText("Updated title")
report["modified_after_edit"] = editor.is_modified()
editor.select_page(1)
editor.select_page(0)
report["text_after_switch"] = editor.block_texts()[0]

# Пустой PDF (только графика) -- блоков нет, но страница рисуется.
report["rendered_after_switch"] = not editor.image_label.pixmap().isNull()

# Битый файл: ошибка в диалоге, открытый документ остаётся.
import src.gui.pdf_editor as pdf_module

errors = []
pdf_module.QMessageBox.critical = lambda *args, **kwargs: errors.append(list(args[1:3]))
broken = Path(path).with_name("broken.pdf")
broken.write_bytes(b"%PDF-1.4 not really a pdf")
report["broken_result"] = editor.load_file(str(broken))
report["broken_error"] = bool(errors)
report["pages_after_error"] = editor.page_count()
report["text_kept_after_error"] = editor.block_texts()[0]

editor.mark_saved()
window.pdf_editor.close_document()
window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Проверка сохранения PDF: правка блоков, кириллица, перезапись «на место».
_SCRIPT_PDF_SAVE = (
    _SCRIPT_PREAMBLE
    + """
import shutil
from pathlib import Path

import pymupdf

src, dst, inplace = sys.argv[1], sys.argv[2], sys.argv[3]

editor = window.pdf_editor
report["opened"] = editor.load_file(src)
editors = editor.block_editors()
editors[0].setPlainText("Ежеквартальный обзор")
editors[1].setPlainText("Наша дорожная карта готова")
report["modified_before_save"] = editor.is_modified()

report["saved"] = editor.save_file(dst)
report["modified_after_save"] = editor.is_modified()
report["current_path"] = editor.current_path.name
report["texts_after_reload"] = editor.block_texts()
report["total_after_reload"] = editor.total_block_count()

doc = pymupdf.open(dst)
report["out_pages"] = doc.page_count
report["out_text"] = doc.load_page(0).get_text()
report["out_fonts"] = sorted({font[3] for font in doc.load_page(0).get_fonts()})
pix = doc.load_page(0).get_pixmap(matrix=pymupdf.Matrix(1, 1))
report["out_render"] = [pix.width, pix.height]
doc.close()

# Перезапись «на место» исходного файла: результат идёт через временный файл.
shutil.copyfile(src, inplace)
report["inplace_opened"] = editor.load_file(inplace)
editor.select_page(1)
editor.block_editors()[0].setPlainText("Вторая страница обновлена")
report["inplace_saved"] = editor.save_file(inplace)
report["tmp_left"] = sorted(
    path.name for path in Path(inplace).parent.glob(".*termru-tmp*")
)
doc = pymupdf.open(inplace)
report["inplace_pages"] = doc.page_count
report["inplace_page1"] = doc.load_page(0).get_text()
report["inplace_page2"] = doc.load_page(1).get_text()
report["inplace_size"] = Path(inplace).stat().st_size
doc.close()

editor.mark_saved()
window.pdf_editor.close_document()
window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка русификации на вкладке PDF: маршрутизация замен и запуск воркера.
_SCRIPT_PDF_RUSIFY = (
    _SCRIPT_PREAMBLE
    + """
import pymupdf

import src.gui.main_window as main_window

src, dst = sys.argv[1], sys.argv[2]

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

editor = window.pdf_editor
report["opened"] = window.open_path(src)
report["texts_before"] = editor.block_texts()
report["current_text"] = editor.current_text()

# Ветка применения: _on_rusify_ready направляет замены в PDF-редактор.
window._rusify_target = "pdf"
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
doc = pymupdf.open(dst)
report["saved_text"] = doc.load_page(0).get_text()
doc.close()

# Ветка запуска: кнопка берёт текст страницы и блокирует правку.
main_window.ReplacementsDialog.exec = (
    lambda self: main_window.ReplacementsDialog.DialogCode.Rejected
)
window.russify_document()
report["worker_started"] = window._worker is not None and window._worker.isRunning()
report["worker_text"] = window._worker._text
report["editors_locked"] = all(
    block.isReadOnly() for block in editor.block_editors()
)
report["page_list_locked"] = not editor.page_list.isEnabled()
window._worker.wait(20000)
report["worker_finished"] = window._worker.isFinished()

editor.mark_saved()
window.pdf_editor.close_document()
window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Проверка предупреждения о повторной обработке уже сконвертированного PDF.
_SCRIPT_PDF_REPROCESS = (
    _SCRIPT_PREAMBLE
    + """
from pathlib import Path

import src.gui.pdf_editor as pdf_module

src, dst = sys.argv[1], sys.argv[2]

editor = window.pdf_editor
report["opened"] = editor.load_file(src)
report["already_processed"] = editor._already_processed()
editor.block_editors()[0].setPlainText("Повторная правка")

answers = []


def _deny(*args, **kwargs):
    answers.append(list(args[1:3]))
    return pdf_module.QMessageBox.StandardButton.No


pdf_module.QMessageBox.warning = _deny
report["saved_no"] = editor.save_file(dst)
report["file_created_no"] = Path(dst).exists()
report["warning_shown"] = bool(answers)
report["warning_title"] = answers[0][0] if answers else None
report["still_modified"] = editor.is_modified()

pdf_module.QMessageBox.warning = (
    lambda *args, **kwargs: pdf_module.QMessageBox.StandardButton.Yes
)
report["saved_yes"] = editor.save_file(dst)
report["file_created_yes"] = Path(dst).exists()

editor.mark_saved()
window.pdf_editor.close_document()
window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Проверка редактора словаря: загрузка, добавление слов, запись в JSON.
_SCRIPT_GLOSSARY_EDITOR = (
    _SCRIPT_PREAMBLE
    + """
import json
from pathlib import Path

from src.glossary_manager import GlossaryManager
from src.gui.glossary_editor import GlossaryEditorDialog
import src.gui.glossary_editor as editor_module
from src.replacement_engine import ReplacementEngine

glossary_path, exceptions_path = sys.argv[1], sys.argv[2]

manager = GlossaryManager(glossary_path, exceptions_path)
dialog = GlossaryEditorDialog(manager)

report["tabs"] = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
report["glossary_headers"] = [
    dialog.glossary_table.horizontalHeaderItem(i).text()
    for i in range(dialog.glossary_table.columnCount())
]
report["exception_headers"] = [
    dialog.exceptions_table.horizontalHeaderItem(i).text()
    for i in range(dialog.exceptions_table.columnCount())
]
report["glossary_rows"] = dialog.glossary_table.rowCount()
report["glossary_cells"] = [
    dialog.glossary_table.item(0, 0).text(),
    dialog.glossary_table.item(0, 1).text(),
]
report["exception_rows"] = dialog.exceptions_table.rowCount()
report["counts_label"] = dialog.counts.text()

# Добавляем термин и исключение, затем сохраняем.
row = dialog.add_row(dialog.glossary_table)
dialog.glossary_table.item(row, 0).setText("invoice")
dialog.glossary_table.item(row, 1).setText("счёт")
ex_row = dialog.add_row(dialog.exceptions_table)
dialog.exceptions_table.item(ex_row, 0).setText("SKU")

warnings = []
editor_module.QMessageBox.warning = lambda *args, **kwargs: warnings.append(
    list(args[1:3])
)
report["saved"] = dialog.save()
report["dialog_result"] = dialog.result()
report["file_glossary"] = json.loads(Path(glossary_path).read_text(encoding="utf-8"))
report["file_exceptions"] = json.loads(Path(exceptions_path).read_text(encoding="utf-8"))
report["tmp_left"] = sorted(
    path.name for path in Path(glossary_path).parent.glob("*termru-tmp*")
)

# Новые слова сразу видны движку замены (reload внутри save).
report["hit"] = dict(manager.get_replacements("the invoice is ready"))
report["engine_text"] = ReplacementEngine(manager).replace_dict_only(
    "Our invoice is ready"
)[0]
report["exception_hit"] = manager.is_exception("sku")

# Дубликат (регистр не важен) сохранять нельзя: файл остаётся прежним.
row = dialog.add_row(dialog.glossary_table)
dialog.glossary_table.item(row, 0).setText("Invoice")
dialog.glossary_table.item(row, 1).setText("счёт-фактура")
report["duplicate_saved"] = dialog.save()
report["duplicate_warning"] = warnings[0][1] if warnings else None
report["file_after_duplicate"] = json.loads(
    Path(glossary_path).read_text(encoding="utf-8")
)

# Удаление строки работает на выбранной таблице.
before = dialog.glossary_table.rowCount()
dialog.glossary_table.setCurrentCell(before - 1, 0)
report["removed"] = dialog.remove_selected_rows(dialog.glossary_table)
report["rows_after_remove"] = dialog.glossary_table.rowCount()

dialog.close()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Проверка настроек: вкладки, переключение темы и режим PDF.
_SCRIPT_SETTINGS_THEME = (
    _SCRIPT_PREAMBLE
    + """
from pathlib import Path

from PySide6.QtCore import QSettings

settings_dir = sys.argv[1]
# Изолируем QSettings от реального профиля пользователя. Важно: экземпляры
# QSettings(org, app) используют NativeFormat, поэтому подменять нужно его.
QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, settings_dir)

from src.gui.settings import SettingsDialog, load_settings, stylesheet_for

report["defaults"] = load_settings()

dialog = SettingsDialog(
    initial={"api_key": "", "use_llm": False, "theme": "dark", "pdf_fonts": "always"}
)
report["tabs"] = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
report["theme_items"] = [
    dialog.theme_combo.itemData(i) for i in range(dialog.theme_combo.count())
]
report["pdf_items"] = [
    dialog.pdf_combo.itemData(i) for i in range(dialog.pdf_combo.count())
]

# Переключение темы применяется к QApplication сразу.
dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("light"))
report["theme_after_switch"] = dialog.theme()
report["style_matches_light"] = app.styleSheet() == stylesheet_for("light")

dialog.pdf_combo.setCurrentIndex(dialog.pdf_combo.findData("strict"))
report["pdf_mode"] = dialog.pdf_font_mode()

dialog.save_and_close()
report["dialog_result"] = dialog.result()
report["saved"] = load_settings()
settings_file = Path(settings_dir) / "TermRu" / "Editor.conf"
report["settings_file"] = str(settings_file)
report["settings_written"] = settings_file.exists()
report["settings_text"] = (
    settings_file.read_text(encoding="utf-8") if settings_file.exists() else ""
)

# Отмена возвращает прежнюю тему.
dialog2 = SettingsDialog(
    initial={"api_key": "", "use_llm": False, "theme": "light", "pdf_fonts": "strict"}
)
dialog2.theme_combo.setCurrentIndex(dialog2.theme_combo.findData("dark"))
report["theme_switched_again"] = app.styleSheet() == stylesheet_for("dark")
dialog2.reject()
report["theme_after_reject"] = app.styleSheet() == stylesheet_for("light")

window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка, что отредактированный словарь сразу работает в русификации.
_SCRIPT_GLOSSARY_RUSSIFY = (
    _SCRIPT_PREAMBLE
    + """
import src.gui.main_window as main_window
from src.glossary_manager import GlossaryManager

main_window.QMessageBox.information = lambda *args, **kwargs: None
main_window.ReplacementsDialog.exec = (
    lambda self: main_window.ReplacementsDialog.DialogCode.Rejected
)

window.glossary = GlossaryManager(sys.argv[1], sys.argv[2])
window.editor.setPlainText("Our gadget is ready")
report["text_before"] = window.editor.toPlainText()

window.russify_document()
report["worker_started"] = window._worker is not None
report["worker_uses_manager"] = window._worker._glossary is window.glossary
report["worker_text"] = window._worker._text
window._worker.result_ready.connect(
    lambda text, changes: report.setdefault("result", [text, len(changes)])
)
window._worker.wait(20000)
for _ in range(20):
    app.processEvents()

report["text_after"] = report.get("result", [None])[0]

window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.pdf_editor.close_document()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Регрессия: результат русификации не должен отбрасываться как «отмена».
_SCRIPT_RUSIFY_PREVIEW = (
    _SCRIPT_PREAMBLE
    + """
import src.gui.main_window as main_window

messages = []
main_window.QMessageBox.information = lambda *a, **k: messages.append(list(a[1:3]))
main_window.QMessageBox.critical = lambda *a, **k: messages.append(list(a[1:3]))
main_window.load_settings = lambda: {
    "api_key": "", "use_llm": False, "theme": "dark", "pdf_fonts": "always"
}

dialogs = []


class SpyDialog(main_window.ReplacementsDialog):
    def __init__(self, items, parent=None):
        dialogs.append(len(items))
        super().__init__(items, parent)

    def exec(self):
        return main_window.ReplacementsDialog.DialogCode.Rejected


main_window.ReplacementsDialog = SpyDialog

window.editor.setPlainText("The feedback from the meeting is below")
window.russify_document()
report["progress_created"] = window.progress_dialog is not None
report["auto_close"] = window.progress_dialog.autoClose() if window.progress_dialog else None
report["auto_reset"] = window.progress_dialog.autoReset() if window.progress_dialog else None

for _ in range(400):
    app.processEvents()
window._worker.wait(20000)
for _ in range(400):
    app.processEvents()

report["cancelled"] = window._rusify_cancelled
report["progress_closed"] = window.progress_dialog is None
report["dialogs"] = dialogs
report["messages"] = messages
report["editor_unlocked"] = not window.editor.isReadOnly()

window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Ошибка DeepSeek: окно с ошибкой + трейсбек в stderr, прогресс закрыт.
_SCRIPT_RUSIFY_API_ERROR = (
    _SCRIPT_PREAMBLE
    + """
import src.gui.main_window as main_window
import src.gui.rusify as rusify_module
from src.deepseek_client import DeepSeekAPIError

messages = []
main_window.QMessageBox.information = lambda *a, **k: messages.append(list(a[1:3]))
main_window.QMessageBox.critical = lambda *a, **k: messages.append(list(a[1:3]))
main_window.load_settings = lambda: {
    "api_key": "sk-test", "use_llm": True, "theme": "dark", "pdf_fonts": "always"
}


class FailingClient:
    def __init__(self, *args, **kwargs):
        pass

    def replace_text(self, *args, **kwargs):
        raise DeepSeekAPIError("DeepSeek API недоступен после 3 попыток: boom")


rusify_module.DeepSeekClient = FailingClient

window.editor.setPlainText("The feedback from the meeting is below")
window.russify_document()
for _ in range(400):
    app.processEvents()
window._worker.wait(20000)
for _ in range(400):
    app.processEvents()

report["cancelled"] = window._rusify_cancelled
report["progress_closed"] = window.progress_dialog is None
report["messages"] = messages
report["editor_unlocked"] = not window.editor.isReadOnly()

window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Неожиданное падение внутри потока: «Критическая ошибка» + трейсбек.
_SCRIPT_RUSIFY_CRASH = (
    _SCRIPT_PREAMBLE
    + """
import src.gui.main_window as main_window
import src.gui.rusify as rusify_module

messages = []
main_window.QMessageBox.information = lambda *a, **k: messages.append(list(a[1:3]))
main_window.QMessageBox.critical = lambda *a, **k: messages.append(list(a[1:3]))
main_window.load_settings = lambda: {
    "api_key": "", "use_llm": False, "theme": "dark", "pdf_fonts": "always"
}


class BrokenEngine:
    def replace_dict_only(self, text):
        raise RuntimeError("движок сломался")


rusify_module.ReplacementEngine = lambda glossary: BrokenEngine()

window.editor.setPlainText("The feedback from the meeting is below")
window.russify_document()
for _ in range(400):
    app.processEvents()
window._worker.wait(20000)
for _ in range(400):
    app.processEvents()

report["cancelled"] = window._rusify_cancelled
report["progress_closed"] = window.progress_dialog is None
report["messages"] = messages
report["editor_unlocked"] = not window.editor.isReadOnly()

window.editor.mark_saved()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


#: Стартовый экран: карточки создают документы и открывают редактор.
_SCRIPT_START_CREATE = (
    _SCRIPT_PREAMBLE
    + """
from pathlib import Path

import src.gui.main_window as main_window

main_window.QMessageBox.information = lambda *a, **k: None
main_window.QMessageBox.critical = lambda *a, **k: None

pptx_target, pdf_target = sys.argv[1], sys.argv[2]

report["start_index_before"] = window.stack.currentIndex()

# DOCX
window.start_screen._cards["docx"].click()
report["docx_after"] = [window.stack.currentIndex(), window.tabs.currentIndex()]
report["docx_path"] = window.editor.current_path
report["docx_text"] = window.editor.toPlainText()
report["docx_modified"] = window.editor.is_modified()
report["docx_title"] = window.windowTitle()

# PPTX
window.show_start_screen()
report["after_home"] = window.stack.currentIndex()
window.start_screen._cards["pptx"].click()
report["pptx_after"] = [window.stack.currentIndex(), window.tabs.currentIndex()]
report["pptx_slides"] = window.pptx_editor.slide_count()
report["pptx_path"] = window.pptx_editor.current_path
report["pptx_saved"] = window.pptx_editor.save_file(pptx_target)
report["pptx_file_exists"] = Path(pptx_target).exists()

# PDF
window.show_start_screen()
window.start_screen._cards["pdf"].click()
report["pdf_after"] = [window.stack.currentIndex(), window.tabs.currentIndex()]
report["pdf_pages"] = window.pdf_editor.page_count()
report["pdf_path"] = window.pdf_editor.current_path
report["pdf_rendered"] = not window.pdf_editor.image_label.pixmap().isNull()
report["pdf_saved"] = window.pdf_editor.save_file(pdf_target)
report["pdf_file_exists"] = Path(pdf_target).exists()
report["pdf_saved_path"] = (
    window.pdf_editor.current_path.name if window.pdf_editor.current_path else None
)
report["pdf_pages_after_save"] = window.pdf_editor.page_count()

window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.pdf_editor.close_document()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Стартовый экран: открытие файла, список недавних, возврат и ошибка.
_SCRIPT_START_OPEN = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtWidgets import QPushButton

import src.gui.main_window as main_window

errors = []
main_window.QMessageBox.critical = lambda *a, **k: errors.append(list(a[1:3]))

sample = sys.argv[1]

report["open"] = window.open_path(sample)
report["index_after_open"] = window.stack.currentIndex()
report["tab_after_open"] = window.tabs.currentIndex()
report["recent_after_open"] = window.start_screen.recent_files()

window.show_start_screen()
report["index_after_home"] = window.stack.currentIndex()
buttons = window.start_screen.recent_container.findChildren(QPushButton)
report["recent_buttons"] = [button.text() for button in buttons]
buttons[0].click()
report["index_after_recent_click"] = window.stack.currentIndex()

# Повторное открытие не дублирует запись в списке.
window.show_start_screen()
window.open_path(sample)
report["recent_dedup"] = window.start_screen.recent_files()

# Неудачное открытие возвращает на стартовый экран.
window.show_start_screen()
report["bad_open"] = window.open_path("/nonexistent/broken.docx")
report["index_after_bad"] = window.stack.currentIndex()
report["error_shown"] = bool(errors)

window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.pdf_editor.close_document()
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


def _run_gui_script_capture(script: str, *args: str) -> tuple[dict, str]:
    """Выполнить пробу в подпроцессе и вернуть JSON-отчёт вместе со stderr.

    Args:
        script: Текст скрипта (``python -c``).
        *args: Аргументы скрипта.

    Returns:
        tuple[dict, str]: разобранный JSON и stderr подпроцесса.

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
    return json.loads(lines[-1]), completed.stderr


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
    report, _ = _run_gui_script_capture(script, *args)
    return report


def test_main_window_builds_ui() -> None:
    """Окно содержит стартовый экран, вкладки редактора, меню и статус-бар."""
    report = _run_gui_script(_SCRIPT_UI)

    assert report["central_is_stack"] is True
    assert report["stack_count"] == 2, report["stack_count"]
    assert report["start_index"] == 0, "при запуске должен открываться стартовый экран"
    assert report["start_screen_is_page0"] is True
    assert report["editor_is_page1"] is True

    assert report["start_heading"] == "Добро пожаловать в TermRu"
    assert report["start_subtitle"] == "Умный редактор документов с ИИ-русификацией"
    assert report["start_cards"] == ["docx", "pdf", "pptx"], report["start_cards"]
    assert report["start_icons_ok"] is True, "иконки qtawesome не загрузились"
    assert report["nav_buttons"] == ["about", "home", "open", "settings"]
    assert report["recent_files"] == [], report["recent_files"]

    assert report["file_menu"] == [
        "Открыть",
        "Сохранить",
        "Сохранить как…",
        "На главный экран",
        "Выход",
    ], report["file_menu"]

    assert report["tab_titles"] == [
        "Word (DOCX)",
        "PowerPoint (PPTX)",
        "PDF",
    ], report["tab_titles"]
    assert report["word_editor_in_tab0"] is True
    assert report["pptx_editor_in_tab1"] is True
    assert report["pdf_editor_in_tab2"] is True
    assert len(report["menus"]) == 4, report["menus"]
    assert report["toolbar_count"] == 0, "старый QToolBar не убран из редактора"
    assert report["ribbon_in_editor_screen"] is True, "лента не попала в экран редактора"
    assert report["tabs_in_editor_screen"] is True
    assert report["ribbon_tabs"] == ["Главная", "Вставка", "TermRu", "Вид"], report[
        "ribbon_tabs"
    ]
    assert report["ribbon_current"] == "Главная"
    assert report["ribbon_quick"] == [
        "actionOpen",
        "actionSave",
        "actionSaveAs",
        "actionHome",
    ], report["ribbon_quick"]
    assert report["ribbon_icons_ok"] is True, "у кнопок ленты нет иконок qtawesome"
    assert report["ribbon_view_groups"] == ["Масштаб", "Тема"], report["ribbon_view_groups"]
    assert report["ribbon_use_llm"] == "Использовать ИИ (DeepSeek)"
    assert report["ribbon_zoom"] == 100
    assert report["tools_menu"] == [
        "Редактор словаря…",
        "Настройки…",
        "Русифицировать документ",
    ], report["tools_menu"]
    assert report["status_path"] == "Новый документ"
    assert report["status_counts"] == "Слов: 0 · Символов: 0"
    assert report["title"].startswith("TermRu — Редактор документов")


def test_format_actions_in_ribbon() -> None:
    """Действия форматирования лежат на вкладке «Главная» в своих группах."""
    report = _run_gui_script(_SCRIPT_UI)

    assert report["ribbon_home"] == [
        "action_bold",
        "action_italic",
        "action_underline",
        "action_align_left",
        "action_align_center",
        "action_align_right",
        "action_bullet_list",
        "action_numbered_list",
        "action_undo",
        "action_redo",
        "action_find",
    ], report["ribbon_home"]
    assert report["ribbon_insert"] == [
        "action_table",
        "action_image",
        "action_text_box",
    ], report["ribbon_insert"]
    assert report["ribbon_termru"] == ["action_russify", "actionGlossary"], report[
        "ribbon_termru"
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
    assert "аудит готов" in new_text

    sources = {item["source"] for item in changes}
    assert "dict" in sources and "llm" in sources, sources

    llm_pairs = [
        (item["original"], item["replacement"]) for item in changes if item["source"] == "llm"
    ]
    assert ("audit is ready", "аудит готов") in llm_pairs, llm_pairs

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
    assert report["format_disabled"] is True, "форматирование DOCX осталось доступным"

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


def test_pdf_editor_load(tmp_path: Path) -> None:
    """PDF открывается: страницы, рендер, блоки по страницам, битый файл."""
    path = tmp_path / "report.pdf"
    _write_sample_pdf(path)

    report = _run_gui_script(_SCRIPT_PDF_LOAD, str(path))

    assert report["opened"] is True
    assert report["active_tab"] == 2, "вкладка не переключилась на PDF"
    assert report["tab_title"] == "PDF"

    assert report["pages"] == 2
    assert report["page_names"] == ["Страница 1", "Страница 2"], report["page_names"]
    assert report["current_page"] == 0

    assert report["page1_ids"] == [0, 1, 2], report["page1_ids"]
    assert report["page1_texts"] == [
        "Quarterly review",
        "Our product roadmap is ready",
        "The feedback from the meeting is below",
    ], report["page1_texts"]
    assert report["total_blocks"] == 4

    assert report["render_null"] is False, "страница не отрисовалась"
    # A4 при zoom=1.5 — это ~893x1263 точек, то есть рендер именно 1.5x.
    assert report["raw_size"] is not None
    assert report["raw_size"][0] > 800 and report["raw_size"][1] > 1100, report["raw_size"]

    assert report["modified_after_load"] is False
    assert report["status_path"] == str(path)
    assert report["status_counts"] == "Страниц: 2 · Блоков: 3", report["status_counts"]
    assert report["title"] == "TermRu — Редактор документов — report.pdf[*]"
    assert report["russify_label"] == "Русифицировать страницу"
    assert report["format_disabled"] is True, "форматирование DOCX осталось доступным"

    assert report["page2_ids"] == [3], report["page2_ids"]
    assert report["page2_texts"] == ["Second page with deadline"]
    assert report["status_after_page2"] == "Страниц: 2 · Блоков: 1"

    assert report["modified_after_edit"] is True
    assert report["text_after_switch"] == "Updated title", "правка потерялась"
    assert report["rendered_after_switch"] is True

    assert report["broken_result"] is False
    assert report["broken_error"] is True, "ошибка открытия не показана"
    assert report["pages_after_error"] == 2, "битый файл сбросил открытый документ"
    assert report["text_kept_after_error"] == "Updated title"


def test_pdf_editor_save(tmp_path: Path) -> None:
    """Правка блоков записывается в PDF, кириллица кодируется, «на место» тоже."""
    source = tmp_path / "in.pdf"
    target = tmp_path / "out.pdf"
    inplace = tmp_path / "inplace.pdf"
    _write_sample_pdf(source)

    report = _run_gui_script(
        _SCRIPT_PDF_SAVE, str(source), str(target), str(inplace)
    )

    assert report["opened"] is True
    assert report["modified_before_save"] is True
    assert report["saved"] is True, "PDF не записался"
    assert target.is_file()
    assert report["modified_after_save"] is False
    assert report["current_path"] == "out.pdf"

    # Файл перечитан после записи: блоки соответствуют новой вёрстке.
    assert report["total_after_reload"] >= 4

    assert report["out_pages"] == 2, "потерялись страницы"
    assert "Ежеквартальный" in report["out_text"], report["out_text"]
    assert "обзор" in report["out_text"]
    assert "Наша дорожная карта готова" in report["out_text"].replace("\n", " ")
    assert report["out_render"][0] > 500, "страница результата не рендерится"
    # base-14 Helvetica не умеет кириллицу — движок подставил свой CID-шрифт.
    assert any(
        name not in {"Helvetica", "Helvetica-Bold"} for name in report["out_fonts"]
    ), report["out_fonts"]

    # Перезапись «на место» идёт через временный файл и не оставляет мусора.
    assert report["inplace_opened"] is True
    assert report["inplace_saved"] is True
    assert report["tmp_left"] == [], report["tmp_left"]
    assert report["inplace_pages"] == 2
    assert "Quarterly review" in report["inplace_page1"], report["inplace_page1"]
    assert "Вторая страница обновлена" in report["inplace_page2"].replace("\n", " ")
    assert report["inplace_size"] > 1000


def test_pdf_rusify_flow(tmp_path: Path) -> None:
    """Русификация на вкладке PDF правит блоки страницы и запускает воркер."""
    source = tmp_path / "in.pdf"
    target = tmp_path / "out.pdf"
    _write_sample_pdf(source)

    report = _run_gui_script(_SCRIPT_PDF_RUSIFY, str(source), str(target))

    assert report["opened"] is True
    assert report["current_text"] == (
        "Quarterly review\n\nOur product roadmap is ready\n\n"
        "The feedback from the meeting is below"
    ), report["current_text"]

    assert report["texts_after_ready"][2] == "The обратная связь from the встреча is below"
    assert report["status_after_ready"] == "Применено замен: 2"
    assert report["modified_after_ready"] is True

    assert report["saved"] is True
    assert "обратная связь" in report["saved_text"], report["saved_text"]
    assert "встреча" in report["saved_text"]

    assert report["worker_started"] is True, "воркер не запустился"
    assert "обратная связь" in report["worker_text"]
    assert report["editors_locked"] is True, "правка не заблокирована на время работы"
    assert report["page_list_locked"] is True, "список страниц не заблокирован"
    assert report["worker_finished"] is True


def test_pdf_editor_warns_before_reprocessing(tmp_path: Path) -> None:
    """Повторная запись уже обработанного PDF требует подтверждения."""
    from src.document_processor import DocumentProcessor

    original = tmp_path / "in.pdf"
    processed = tmp_path / "processed.pdf"
    target = tmp_path / "again.pdf"
    _write_sample_pdf(original)

    processor = DocumentProcessor(pdf_substitute_fonts="always")
    blocks = processor.extract_text(str(original))
    result = processor.apply_replacements(
        str(original), {blocks[0]["block_id"]: "Ежеквартальный обзор"}, str(processed)
    )
    assert result["success"] is True

    report = _run_gui_script(_SCRIPT_PDF_REPROCESS, str(processed), str(target))

    assert report["opened"] is True
    assert report["already_processed"] is True, "файл не распознан как обработанный"
    assert report["saved_no"] is False, "запись прошла без подтверждения"
    assert report["file_created_no"] is False
    assert report["still_modified"] is True, "правки потерялись при отказе"
    assert report["warning_shown"] is True
    assert report["warning_title"] == "PDF уже обработан"

    assert report["saved_yes"] is True
    assert report["file_created_yes"] is True


def test_glossary_editor_load_and_save(tmp_path: Path) -> None:
    """Диалог словаря читает JSON, добавляет слова, пишет файлы и делает reload."""
    glossary_path, exceptions_path = _write_glossary_files(
        tmp_path, {"feedback": "обратная связь"}, ["API"]
    )
    report = _run_gui_script(
        _SCRIPT_GLOSSARY_EDITOR, str(glossary_path), str(exceptions_path)
    )

    assert report["tabs"] == ["Словарь", "Исключения"]
    assert report["glossary_headers"] == ["Английский", "Русский"]
    assert report["exception_headers"] == ["Слово / Аббревиатура"]
    assert report["glossary_rows"] == 1
    assert report["glossary_cells"] == ["feedback", "обратная связь"]
    assert report["exception_rows"] == 1
    assert report["counts_label"] == "Терминов: 1 · исключений: 1"

    assert report["saved"] is True
    assert report["dialog_result"] == 1, "диалог не закрылся с Accepted"
    assert report["file_glossary"] == {
        "feedback": "обратная связь",
        "invoice": "счёт",
    }, report["file_glossary"]
    assert report["file_exceptions"] == {"API": None, "SKU": None}
    assert report["tmp_left"] == [], report["tmp_left"]

    # Новое слово сразу работает: менеджер перечитан внутри save().
    assert report["hit"] == {"invoice": "счёт"}
    assert report["engine_text"] == "Our счёт is ready"
    assert report["exception_hit"] is True

    # Дубликат (регистр не важен) не пишется в файл.
    assert report["duplicate_saved"] is False
    assert "повторяется" in (report["duplicate_warning"] or "")
    assert "Invoice" not in report["file_after_duplicate"]

    assert report["removed"] == 1
    assert report["rows_after_remove"] == 2


def test_settings_theme_change(tmp_path: Path) -> None:
    """Тема переключается и сохраняется в QSettings, режим PDF тоже."""
    report = _run_gui_script(_SCRIPT_SETTINGS_THEME, str(tmp_path))

    assert report["tabs"] == ["DeepSeek", "Внешний вид", "PDF"]
    assert report["theme_items"] == ["dark", "light"]
    assert report["pdf_items"] == ["strict", "base14", "always"]
    assert report["defaults"]["theme"] == "dark"
    assert report["defaults"]["pdf_fonts"] == "always"

    assert report["theme_after_switch"] == "light"
    assert report["style_matches_light"] is True, "светлая тема не применилась сразу"

    assert report["pdf_mode"] == "strict"
    assert report["dialog_result"] == 1

    assert report["saved"]["theme"] == "light"
    assert report["saved"]["pdf_fonts"] == "strict"
    assert report["settings_written"] is True
    assert str(tmp_path) in report["settings_file"]
    settings_text = report["settings_text"].replace(" ", "")
    assert "theme=light" in settings_text, settings_text
    assert "substitute_fonts=strict" in settings_text, settings_text

    # Отмена возвращает тему, которая была до открытия диалога.
    assert report["theme_switched_again"] is True
    assert report["theme_after_reject"] is True


def test_russify_uses_edited_glossary(tmp_path: Path) -> None:
    """Термин из отредактированного словаря срабатывает в русификации сразу."""
    glossary_path, exceptions_path = _write_glossary_files(
        tmp_path, {"gadget": "устройство"}, ["API"]
    )
    report = _run_gui_script(
        _SCRIPT_GLOSSARY_RUSSIFY, str(glossary_path), str(exceptions_path)
    )

    assert report["text_before"] == "Our gadget is ready"
    assert report["worker_started"] is True
    assert report["worker_uses_manager"] is True, "воркер не получил словарь окна"
    assert report["worker_text"] == "Our gadget is ready"
    assert report["text_after"] == "Our устройство is ready", report["text_after"]


def _project_root() -> Path:
    """Корень проекта.

    Returns:
        Path: каталог, где лежат ``unifypy.json`` и точки входа.

    Raises:
        Ничего не бросает.
    """
    return Path(__file__).resolve().parents[1]


def test_gui_entry_module_imports() -> None:
    """``termru_gui_entry.py`` импортируется и выбирает режим по аргументам."""
    root = _project_root()
    assert (root / "termru_gui_entry.py").is_file(), "нет termru_gui_entry.py"
    assert (root / "termru_cli_entry.py").is_file(), "нет termru_cli_entry.py (CLI)"
    assert not (root / "termru_entry.py").exists(), "осталась старая точка входа"

    script = """
import json

import termru_gui_entry as entry

print(json.dumps({
    "no_args": entry.use_cli_mode([]),
    "with_input": entry.use_cli_mode(["--input", "x.docx"]),
    "version": entry.use_cli_mode(["--version"]),
    "gui_flag": entry.use_cli_mode([entry.GUI_FLAG]),
    "smoke_flag": entry.use_cli_mode([entry.GUI_SMOKE_FLAG]),
    "main_callable": callable(entry.main),
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(root),
    )

    assert completed.returncode == 0, completed.stderr[-3000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])

    assert report["no_args"] is False, "без аргументов должен стартовать GUI"
    assert report["with_input"] is True, "аргументы должны включать CLI"
    assert report["version"] is True
    assert report["gui_flag"] is False
    assert report["smoke_flag"] is False
    assert report["main_callable"] is True


def test_unifypy_config_builds_gui() -> None:
    """``unifypy.json`` собирает GUI-версию и не тянет тяжёлые модули Qt."""
    config = json.loads(
        (_project_root() / "unifypy.json").read_text(encoding="utf-8")
    )

    assert config["entry"] == "termru_gui_entry.py"
    assert config["name"] == "TermRu", "имя приложения влияет на имя бинарника"
    assert config.get("package") == "termru", "имя deb-пакета должно быть в нижнем регистре"

    pyinstaller = config["pyinstaller"]
    hidden = set(pyinstaller["hidden_import"])
    assert {"PySide6.QtSvg", "PySide6.QtPrintSupport"} <= hidden, hidden
    assert "qdarkstyle" in hidden, "стили темы иначе не попадут в бандл"

    excludes = set(pyinstaller["exclude_module"])
    for module in (
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia",
        "PySide6.Qt3DCore",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtQuick",
        "PySide6.QtQml",
    ):
        assert module in excludes, f"{module} не исключён — бандл раздуется"
    # PDF-редактор рендерит страницы через pypdfium2 (~8 МБ), а тяжёлый
    # PyMuPDF (~50 МБ) в бандл не попадает.
    assert "pymupdf" in excludes
    assert "fitz" in excludes
    assert "pypdfium2" not in excludes
    assert "pypdfium2_raw" not in excludes


def test_rusify_preview_is_shown() -> None:
    """Успешный прогон доходит до диалога замен, а не «отменяется» прогрессом.

    Регрессия: ``QProgressDialog`` с включённым ``autoClose`` вызывает
    ``cancel()`` на ``setValue(100)``, сигнал ``canceled`` помечал прогон
    отменённым, и результат воркера молча отбрасывался.
    """
    report = _run_gui_script(_SCRIPT_RUSIFY_PREVIEW)

    assert report["progress_created"] is True, "прогресс-диалог не создан"
    assert report["auto_close"] is False, "autoClose снова включён"
    assert report["auto_reset"] is False, "autoReset снова включён"

    assert report["cancelled"] is False, "результат помечен как отменённый"
    assert report["progress_closed"] is True, "прогресс-диалог не закрыт"
    assert report["dialogs"] == [2], report["dialogs"]  # feedback + meeting
    assert report["messages"] == [], report["messages"]
    assert report["editor_unlocked"] is True


def test_rusify_api_error_shows_dialog_and_traceback() -> None:
    """Ошибка DeepSeek: окно с ошибкой, трейсбек в stderr, прогресс закрыт."""
    report, stderr = _run_gui_script_capture(_SCRIPT_RUSIFY_API_ERROR)

    assert report["cancelled"] is False
    assert report["progress_closed"] is True
    assert report["editor_unlocked"] is True

    assert report["messages"], "окно с ошибкой не показано"
    kind, text = report["messages"][0]
    assert text.startswith("Ошибка API:"), text
    assert "boom" in text

    assert "Traceback (most recent call last)" in stderr, stderr[-2000:]
    assert "DeepSeekAPIError" in stderr, stderr[-2000:]


def test_rusify_crash_reports_critical_error() -> None:
    """Неожиданное падение движка: «Критическая ошибка» и трейсбек, не тишина."""
    report, stderr = _run_gui_script_capture(_SCRIPT_RUSIFY_CRASH)

    assert report["cancelled"] is False
    assert report["progress_closed"] is True
    assert report["editor_unlocked"] is True

    assert report["messages"], "окно с ошибкой не показано"
    _, text = report["messages"][0]
    assert text == "Критическая ошибка: движок сломался", text

    assert "Traceback (most recent call last)" in stderr, stderr[-2000:]
    assert "RuntimeError: движок сломался" in stderr, stderr[-2000:]


#: Проверка стартового состояния: центральный стек открыт на приветствии.
_SCRIPT_START_DEFAULT = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtWidgets import QLabel, QPushButton

start = window.start_screen
report["central_is_stack"] = isinstance(window.centralWidget(), QStackedWidget)
report["stack_count"] = window.stack.count()
report["index"] = window.stack.currentIndex()
report["page0_is_start"] = window.stack.widget(0) is start
report["current_is_start"] = window.stack.currentWidget() is start
report["editor_is_page1"] = window.stack.widget(1) is window.editor_screen
report["editor_not_current"] = window.stack.currentWidget() is not window.editor_screen

report["title"] = start.findChild(QLabel, "startTitle").text()
report["subtitle"] = start.findChild(QLabel, "startSubtitle").text()
report["cards"] = [start._cards[kind].property("kind") for kind in ("docx", "pptx", "pdf")]
report["card_titles"] = [
    start._cards[kind].findChild(QLabel, "cardTitle").text()
    for kind in ("docx", "pptx", "pdf")
]
report["card_captions"] = [
    start._cards[kind].findChild(QLabel, "cardCaption").text()
    for kind in ("docx", "pptx", "pdf")
]
report["card_icon_sizes"] = [
    [start.card_icons[kind].pixmap().width(), start.card_icons[kind].pixmap().height()]
    for kind in ("docx", "pptx", "pdf")
]
report["open_button"] = start.open_button.text()
report["nav"] = [start.nav_buttons[key].text() for key in ("home", "open", "settings", "about")]
report["nav_active"] = [
    key for key in ("home", "open", "settings", "about")
    if start.nav_buttons[key].property("active") == "true"
]
report["recent"] = start.recent_files()
report["recent_placeholder"] = [
    label.text() for label in start.recent_container.findChildren(QLabel)
]
report["editor_hidden"] = window.editor_screen.isHidden()

window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)

#: Проверка перехода со стартового экрана в редактор через open_path().
_SCRIPT_START_OPEN_EDITOR = (
    _SCRIPT_PREAMBLE
    + """
sample = sys.argv[1]
report["index_before"] = window.stack.currentIndex()
report["page_before_is_start"] = window.stack.currentWidget() is window.start_screen

report["opened"] = window.open_path(sample)
report["index_after"] = window.stack.currentIndex()
report["page_after_is_editor"] = window.stack.currentWidget() is window.editor_screen
report["tab_after"] = window.tabs.currentIndex()
report["text_loaded"] = "roadmap" in window.editor.toPlainText().lower()
report["recent"] = window.start_screen.recent_files()

window.show_start_screen()
report["index_after_home"] = window.stack.currentIndex()
report["page_after_home_is_start"] = (
    window.stack.currentWidget() is window.start_screen
)

window.editor.mark_saved()
window.pptx_editor.mark_saved()
window.pdf_editor.close_document()
window.close()
print(json.dumps(report, ensure_ascii=False))
"""
)


def test_start_screen_is_default() -> None:
    """При запуске центральный стек показывает стартовый экран, а не редактор."""
    report = _run_gui_script(_SCRIPT_START_DEFAULT)

    assert report["central_is_stack"] is True
    assert report["stack_count"] == 2, report["stack_count"]
    assert report["index"] == 0, "при запуске должен открываться стартовый экран"
    assert report["page0_is_start"] is True
    assert report["current_is_start"] is True, "стартовый экран не активен"
    assert report["editor_is_page1"] is True
    assert report["editor_not_current"] is True, "редактор показан вместо приветствия"
    assert report["editor_hidden"] is True

    assert report["title"] == "Добро пожаловать в TermRu"
    assert report["subtitle"] == "Умный редактор документов с ИИ-русификацией"
    assert report["cards"] == ["docx", "pptx", "pdf"], report["cards"]
    assert report["card_titles"] == ["Word", "PowerPoint", "PDF"], report["card_titles"]
    assert report["card_captions"] == [
        "Документ DOCX",
        "Презентация PPTX",
        "Пустая страница PDF",
    ], report["card_captions"]
    assert report["card_icon_sizes"] == [[48, 48], [48, 48], [48, 48]], report[
        "card_icon_sizes"
    ]
    assert report["open_button"] == "Открыть файл с компьютера…"
    assert report["nav"] == ["Главная", "Открыть файл", "Настройки", "О программе"]
    assert report["nav_active"] == ["home"], "активным должен быть только «Главная»"
    assert report["recent"] == []
    assert report["recent_placeholder"] == [
        "Пока пусто — откройте первый документ."
    ], report["recent_placeholder"]


def test_start_screen_open_file() -> None:
    """``open_path`` уводит со стартового экрана в редактор, «Главная» — обратно."""
    sample = PROJECT_ROOT / "tests" / "fixtures" / "sample.docx"
    assert sample.is_file()

    report = _run_gui_script(_SCRIPT_START_OPEN_EDITOR, str(sample))

    assert report["index_before"] == 0
    assert report["page_before_is_start"] is True

    assert report["opened"] is True
    assert report["index_after"] == 1, "стек не переключился на редактор"
    assert report["page_after_is_editor"] is True
    assert report["tab_after"] == 0
    assert report["text_loaded"] is True
    assert report["recent"] == [str(sample)], report["recent"]

    assert report["index_after_home"] == 0
    assert report["page_after_home_is_start"] is True



def test_start_screen_create_documents(tmp_path: Path) -> None:
    """Карточки стартового экрана создают пустые документы и открывают редактор."""
    pptx_target = tmp_path / "new.pptx"
    pdf_target = tmp_path / "new.pdf"

    report = _run_gui_script(
        _SCRIPT_START_CREATE, str(pptx_target), str(pdf_target)
    )

    assert report["start_index_before"] == 0
    assert report["after_home"] == 0, "«На главный экран» не вернул страницу 0"

    # DOCX: пустой документ без пути, вкладка Word.
    assert report["docx_after"] == [1, 0], report["docx_after"]
    assert report["docx_path"] is None
    assert report["docx_text"] == ""
    assert report["docx_modified"] is False
    assert "Новый документ" in report["docx_title"]

    # PPTX: один пустой слайд, сохраняется как новый файл.
    assert report["pptx_after"] == [1, 1], report["pptx_after"]
    assert report["pptx_slides"] == 1
    assert report["pptx_path"] is None
    assert report["pptx_saved"] is True
    assert report["pptx_file_exists"] is True

    # PDF: одна страница, рендерится и сохраняется из буфера.
    assert report["pdf_after"] == [1, 2], report["pdf_after"]
    assert report["pdf_pages"] == 1
    assert report["pdf_path"] is None
    assert report["pdf_rendered"] is True
    assert report["pdf_saved"] is True
    assert report["pdf_file_exists"] is True
    assert report["pdf_saved_path"] == "new.pdf"
    assert report["pdf_pages_after_save"] == 1


def test_start_screen_open_and_recent(tmp_path: Path) -> None:
    """Открытие файла уводит в редактор, помнит файл и возвращается на главную."""
    sample = PROJECT_ROOT / "tests" / "fixtures" / "sample.docx"
    assert sample.is_file()

    report = _run_gui_script(_SCRIPT_START_OPEN, str(sample))

    assert report["open"] is True
    assert report["index_after_open"] == 1, "стек не переключился на редактор"
    assert report["tab_after_open"] == 0
    assert report["recent_after_open"] == [str(sample)], report["recent_after_open"]

    assert report["index_after_home"] == 0
    assert report["recent_buttons"] == ["sample.docx"], report["recent_buttons"]
    assert report["index_after_recent_click"] == 1, "клик по недавнему не открыл файл"

    assert report["recent_dedup"] == [str(sample)], report["recent_dedup"]

    assert report["bad_open"] is False
    assert report["error_shown"] is True
    assert report["index_after_bad"] == 0, "после ошибки окно не вернулось на главную"


#: Проверка вкладок ленты: состав, переключение и группы.
_SCRIPT_RIBBON_TABS = (
    _SCRIPT_PREAMBLE
    + """
from PySide6.QtCore import Qt

ribbon = window.ribbon
report["tab_count"] = ribbon.tabs.count()
report["tab_names"] = ribbon.tab_names()
report["stack_count"] = ribbon.stack.count()
report["current"] = ribbon.current_tab()
report["initial_index"] = ribbon.tabs.currentIndex()

report["home_groups"] = sorted(
    title for (tab, title) in ribbon._groups if tab == "Главная"
)
report["insert_groups"] = sorted(
    title for (tab, title) in ribbon._groups if tab == "Вставка"
)
report["termru_groups"] = sorted(
    title for (tab, title) in ribbon._groups if tab == "TermRu"
)
report["view_groups"] = sorted(
    title for (tab, title) in ribbon._groups if tab == "Вид"
)

ribbon.set_current_tab("Вид")
report["after_switch"] = ribbon.current_tab()
report["stack_follows_tabs"] = ribbon.stack.currentIndex() == ribbon.tabs.currentIndex()

# Лента адаптируется под ширину: узкое окно прячет ленту, оставляя вкладки.
report["full_height"] = ribbon.height()
ribbon.apply_width(600)
report["collapsed_height"] = ribbon.height()
report["collapsed_stack_hidden"] = ribbon.stack.isHidden()
ribbon.apply_width(1200)
report["expanded_stack_visible"] = not ribbon.stack.isHidden()
report["expanded_height"] = ribbon.height()
ribbon.apply_width(900)
report["compact"] = ribbon._compact
big_button = ribbon.button_for("action_russify")
report["compact_icon_only"] = (
    big_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
)
ribbon.apply_width(1200)
report["expanded_text_under_icon"] = (
    big_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
)

print(json.dumps(report))
"""
)

#: Проверка размещения кнопки русификации на вкладке TermRu.
_SCRIPT_RIBBON_RUSIFY = (
    _SCRIPT_PREAMBLE
    + """
ribbon = window.ribbon
report["tab_of_action"] = ribbon.tab_of_action("action_russify")
report["termru_actions"] = ribbon.actions_on("TermRu")

button = ribbon.button_for("action_russify")
report["button_exists"] = button is not None
report["bound_action"] = (
    button.defaultAction().objectName() if button is not None else None
)
report["button_text"] = button.text() if button is not None else ""
report["button_big"] = (
    button.property("big") if button is not None else None
)
report["button_page"] = (
    button.parent().parent().parent().objectName() if button is not None else None
)

group_title = None
group_tab = None
if button is not None:
    for (tab, title), group in ribbon._groups.items():
        if button in group.buttons:
            group_tab = tab
            group_title = title
report["group_of_button"] = group_title
report["tab_of_group"] = group_tab

report["termru_page_exports"] = getattr(window, "action_russify", None) is not None

# Чекбокс ИИ лежит на той же вкладке TermRu.
llm_page = ribbon.use_llm_check.parent().parent().parent()
report["llm_page"] = llm_page.objectName()
report["llm_text"] = ribbon.use_llm_check.text()

print(json.dumps(report))
"""
)


def test_ribbon_widget_has_tabs() -> None:
    """Лента собрана из четырёх вкладок, у каждой есть свои группы."""
    report = _run_gui_script(_SCRIPT_RIBBON_TABS)

    assert report["tab_count"] == 4, report["tab_names"]
    assert report["tab_names"] == ["Главная", "Вставка", "TermRu", "Вид"], report[
        "tab_names"
    ]
    assert report["stack_count"] == 4, "у каждой вкладки должна быть своя лента"
    assert report["initial_index"] == 0
    assert report["current"] == "Главная"

    assert report["home_groups"] == [
        "Абзац",
        "Редактирование",
        "Шрифт",
    ], report["home_groups"]
    assert report["insert_groups"] == [
        "Иллюстрации",
        "Таблицы",
        "Текст",
    ], report["insert_groups"]
    assert report["termru_groups"] == ["Искусственный интеллект", "Русификация", "Словарь"], (
        report["termru_groups"]
    )
    assert report["view_groups"] == ["Масштаб", "Тема"], report["view_groups"]

    assert report["after_switch"] == "Вид"
    assert report["stack_follows_tabs"] is True, "лента не переключилась на вкладку"

    assert report["full_height"] == 128, report["full_height"]
    assert report["collapsed_height"] < report["full_height"], report["collapsed_height"]
    assert report["collapsed_stack_hidden"] is True, "узкая лента не свернулась"
    assert report["expanded_stack_visible"] is True, "лента не развернулась обратно"
    assert report["expanded_height"] == report["full_height"], report["expanded_height"]

    assert report["compact"] is True, "промежуточная ширина не включила иконочный режим"
    assert report["compact_icon_only"] is True, "в узком режиме осталась подпись"
    assert report["expanded_text_under_icon"] is True, (
        "в широком режиме подпись под иконкой не вернулась"
    )


def test_rusify_action_in_ribbon() -> None:
    """Кнопка русификации живёт на вкладке TermRu и связана со своим действием."""
    report = _run_gui_script(_SCRIPT_RIBBON_RUSIFY)

    assert report["tab_of_action"] == "TermRu", report["tab_of_action"]
    assert "action_russify" in report["termru_actions"], report["termru_actions"]

    assert report["button_exists"] is True, "кнопка русификации не создана"
    assert report["bound_action"] == "action_russify"
    assert report["button_text"] == "Русифицировать документ", report["button_text"]
    assert report["button_big"] == "true", "кнопка русификации должна быть крупной"

    assert report["group_of_button"] == "Русификация", report["group_of_button"]
    assert report["tab_of_group"] == "TermRu", report["tab_of_group"]
    assert report["button_page"] == "ribbonPageTermRu", report["button_page"]

    assert report["llm_page"] == "ribbonPageTermRu", report["llm_page"]
    assert report["llm_text"] == "Использовать ИИ (DeepSeek)", report["llm_text"]


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

