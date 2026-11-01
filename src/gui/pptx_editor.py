"""Виджет редактора PPTX: слайды слева, текстовые блоки справа.

Слайды не рендерятся: это намеренно текстовый редактор. Слева — список
слайдов, справа — по одному ``QTextEdit`` на каждый текстовый блок выбранного
слайда (место-заполнитель, надпись, текст в группе или ячейка таблицы).

Текст пишется обратно не «как получится»: сохраняется структура абзацев и
формат первого run в каждом абзаце (жирный, курсив, размер, цвет). Поэтому
замена англицизма не сбрасывает оформление заголовка слайда.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from pptx import Presentation
from pptx.oxml.ns import qn
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

#: Подписи типов мест-заполнителей (``placeholder_format.type``).
_PLACEHOLDER_LABELS = {
    "TITLE": "Заголовок",
    "CENTER_TITLE": "Заголовок",
    "SUBTITLE": "Подзаголовок",
    "BODY": "Текст",
    "OBJECT": "Содержимое",
}

#: Текст-заглушка, когда на слайде нет ни одного непустого текстового блока.
EMPTY_SLIDE_TEXT = "Нет текстовых блоков"

#: Подпись вкладки в главном окне (используется и в тестах).
TAB_TITLE = "PowerPoint (PPTX)"

#: Префикс элементов списка слайдов.
SLIDE_ITEM_PREFIX = "Слайд"

#: Пространство имён ``xml:space`` — нужно, чтобы PowerPoint не срезал пробелы.
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

#: Сколько символов заголовка слайда показывать в списке.
_TITLE_PREVIEW = 40

#: Мягкий перевод строки (Shift+Enter) в OOXML: ``a:br``, в тексте — ``\x0b``.
SOFT_BREAK = "\x0b"


def display_text(text_frame: Any) -> str:
    """Текст фигуры, пригодный для поля ввода.

    Мягкий перевод строки приходит из python-pptx символом ``\\x0b``, который
    ``QTextEdit`` показывает как управляющий символ. Заменяем его обычным
    переводом строки: при записи он станет отдельным абзацем — визуально то
    же самое, а текст не портится.

    Args:
        text_frame: ``text_frame`` фигуры или ячейки таблицы.

    Returns:
        str: Текст с переводами строк ``\\n``.

    Raises:
        Ничего не бросает.
    """
    return text_frame.text.replace(SOFT_BREAK, "\n")


# -- запись текста в фигуру ---------------------------------------------------
def _set_run_text(run: Any, text: str) -> None:
    """Записать текст в run, сохранив пробелы по краям.

    PowerPoint срезает ведущие и хвостовые пробелы, если у ``a:t`` нет
    ``xml:space="preserve"``; python-pptx этот атрибут не ставит.

    Args:
        run: Объект ``_Run`` из python-pptx.
        text: Новый текст фрагмента.

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    run.text = text
    if text == text.strip():
        return
    element = run._r.find(qn("a:t"))
    if element is not None:
        element.set(_XML_SPACE, "preserve")


def _write_paragraph(paragraph: Any, text: str) -> None:
    """Заменить содержимое абзаца, сохранив формат первой части.

    Args:
        paragraph: Объект ``_Paragraph`` из python-pptx.
        text: Новый текст абзаца (без переводов строки).

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    runs = list(paragraph.runs)
    if runs:
        _set_run_text(runs[0], text)
        for extra in runs[1:]:
            extra._r.getparent().remove(extra._r)
        return
    if text:
        _set_run_text(paragraph.add_run(), text)


def _append_paragraph(text_frame: Any, template: Any, text: str) -> None:
    """Добавить абзац, скопировав оформление у соседнего.

    Args:
        text_frame: ``text_frame`` фигуры.
        template: Абзац-образец (обычно последний существующий).
        text: Текст нового абзаца.

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    paragraph = text_frame.add_paragraph()
    source_properties = template._p.find(qn("a:pPr"))
    if source_properties is not None:
        paragraph._p.insert(0, deepcopy(source_properties))

    run = paragraph.add_run()
    _set_run_text(run, text)
    if template.runs:
        run_properties = template.runs[0]._r.find(qn("a:rPr"))
        if run_properties is not None:
            # ``a:rPr`` обязан идти перед ``a:t`` — вставляем в начало.
            run._r.insert(0, deepcopy(run_properties))


def _write_text_frame(text_frame: Any, text: str) -> None:
    """Записать многострочный текст в ``text_frame``.

    Строки раскладываются по существующим абзацам; лишние абзацы удаляются,
    недостающие добавляются с оформлением последнего. Так сохраняется
    форматирование (жирный заголовок, размер, цвет) при правке текста.

    Args:
        text_frame: ``text_frame`` фигуры или ячейки таблицы.
        text: Текст из ``QTextEdit`` (строки разделены переводом строки).

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraphs = list(text_frame.paragraphs)
    if not paragraphs:
        text_frame.text = text
        return

    for index, line in enumerate(lines):
        if index < len(paragraphs):
            _write_paragraph(paragraphs[index], line)
        else:
            _append_paragraph(text_frame, paragraphs[-1], line)

    for extra in paragraphs[len(lines):]:
        extra._p.getparent().remove(extra._p)


# -- обход фигур --------------------------------------------------------------
def _placeholder_label(shape: Any) -> str:
    """Название типа места-заполнителя по-русски.

    Args:
        shape: Фигура python-pptx.

    Returns:
        str: Подпись («Заголовок», «Текст», …) или пустая строка.

    Raises:
        Ничего не бросает.
    """
    if not getattr(shape, "is_placeholder", False):
        return ""
    try:
        raw = str(shape.placeholder_format.type).split(" ", 1)[0]
    except (AttributeError, ValueError):
        return ""
    return _PLACEHOLDER_LABELS.get(raw, "")


def block_caption(shape: Any) -> str:
    """Подпись блока над полем ввода.

    Args:
        shape: Фигура, ячейка таблицы или надпись.

    Returns:
        str: Например, ``"Заголовок · Title 1"`` или ``"TextBox 3"``.

    Raises:
        Ничего не бросает.
    """
    name = str(getattr(shape, "name", "") or "Текст")
    label = _placeholder_label(shape)
    return f"{label} · {name}" if label else name


def _iter_text_blocks(shapes: Iterable[Any]) -> Iterator[tuple[str, Any]]:
    """Перебрать блоки с непустым текстом: фигуры, группы и ячейки таблиц.

    Пустые места-заполнители пропускаются: править в них нечего, а в списке
    они дали бы пустые поля ввода на каждом слайде.

    Args:
        shapes: Коллекция фигур слайда (``slide.shapes`` или ``group.shapes``).

    Returns:
        Iterator[tuple[str, Any]]: пары «подпись блока, объект с text_frame».

    Raises:
        Ничего не бросает.
    """
    for shape in shapes:
        if getattr(shape, "has_text_frame", False):
            if shape.text_frame.text.strip():
                yield block_caption(shape), shape
            continue
        has_table = getattr(shape, "has_table", False)
        if has_table and shape.has_table:
            table_name = str(getattr(shape, "name", "") or "Таблица")
            for row_index, row in enumerate(shape.table.rows, start=1):
                for column_index, cell in enumerate(row.cells, start=1):
                    if cell.text_frame.text.strip():
                        yield f"{table_name} · R{row_index}C{column_index}", cell
            continue
        nested = getattr(shape, "shapes", None)
        if nested is not None:
            yield from _iter_text_blocks(nested)


def _slide_title(slide: Any) -> str:
    """Заголовок слайда для списка слева.

    Args:
        slide: Слайд python-pptx.

    Returns:
        str: Первая строка заголовка (обрезанная) или пустая строка.

    Raises:
        Ничего не бросает.
    """
    try:
        title_shape = slide.shapes.title
    except (AttributeError, ValueError):
        return ""
    if title_shape is None:
        return ""
    return " ".join(title_shape.text_frame.text.split())[:_TITLE_PREVIEW]


def _block_height(text: str) -> int:
    """Высота поля ввода под количество строк текста.

    Args:
        text: Текущий текст блока.

    Returns:
        int: Высота в пикселях (64–220).

    Raises:
        Ничего не бросает.
    """
    lines = text.count("\n") + 1
    return max(64, min(220, 22 * lines + 24))


@dataclass
class _TextBlock:
    """Связка «фигура ↔ поле ввода» для одного текстового блока слайда.

    Args:
        shape: Фигура, ячейка таблицы или надпись с ``text_frame``.
        editor: Поле ввода, показанное в правой панели.
        caption: Подпись блока (для тестов и отладки).
        original_text: Текст, который сейчас записан в файл.

    Raises:
        Ничего не бросает.
    """

    shape: Any
    editor: QTextEdit
    caption: str
    original_text: str


class PptxEditor(QWidget):
    """Текстовый редактор PPTX: список слайдов и поля ввода их текста.

    Signals:
        file_loaded: путь открытой презентации.
        file_saved: путь записанной презентации.
        modified_changed: флаг «есть несохранённые правки».
        slide_changed: индекс выбранного слайда.

    Args:
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    file_loaded = Signal(str)
    file_saved = Signal(str)
    modified_changed = Signal(bool)
    slide_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Собрать пустой редактор с пустым списком слайдов.

        Args:
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        #: Открытая презентация (``None`` — файл ещё не загружен).
        self.presentation: Presentation | None = None
        #: Путь открытого файла (``None`` — презентация не сохранялась).
        self.current_path: Path | None = None
        self._blocks: list[_TextBlock] = []
        self._current_index = -1
        self._modified = False
        self._loading = False
        self._build_ui()

    # -- сборка интерфейса ---------------------------------------------------
    def _build_ui(self) -> None:
        """Создать сплиттер: список слайдов слева, блоки справа.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)

        self.slide_list = QListWidget(splitter)
        self.slide_list.setObjectName("slideList")
        self.slide_list.setMinimumWidth(170)
        self.slide_list.currentRowChanged.connect(self.on_slide_selected)
        splitter.addWidget(self.slide_list)

        self.scroll = QScrollArea(splitter)
        self.scroll.setObjectName("blockScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.panel = QWidget(self.scroll)
        self.panel_layout = QVBoxLayout(self.panel)
        self.panel_layout.setContentsMargins(10, 10, 10, 10)
        self.panel_layout.setSpacing(6)
        self.scroll.setWidget(self.panel)
        splitter.addWidget(self.scroll)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 790])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.splitter = splitter

        self._show_empty("Откройте файл PowerPoint (PPTX), чтобы увидеть слайды.")

    # -- файлы ---------------------------------------------------------------
    def load_file(self, filepath: str) -> bool:
        """Открыть PPTX и заполнить список слайдов.

        Args:
            filepath: Путь к файлу ``.pptx``.

        Returns:
            bool: ``True`` при успехе; при ошибке показывается ``QMessageBox``
            и возвращается ``False``.

        Raises:
            Ничего не бросает: ошибки файла показываются диалогом.
        """
        path = Path(filepath)
        self._flush_current_slide()
        try:
            if not path.is_file():
                raise FileNotFoundError(f"Файл не найден: {path}")
            presentation = Presentation(str(path))
        except Exception as exc:  # noqa: BLE001 - битый архив/не PPTX
            self._show_error("Не удалось открыть презентацию", exc)
            return False

        # Список чистим без сигналов: иначе промежуточный выбор слайда
        # перестроил бы панель для файла, который уже заменён.
        self.slide_list.blockSignals(True)
        self.slide_list.clear()
        self.slide_list.blockSignals(False)
        self._clear_panel()

        self.presentation = presentation
        self.current_path = path
        self._current_index = -1
        self._fill_slide_list()

        if self.slide_list.count():
            self.slide_list.setCurrentRow(0)
        else:
            self._show_empty("В презентации нет слайдов.")

        self._set_modified(False)
        self.file_loaded.emit(str(path))
        return True

    def save_file(self, filepath: str) -> bool:
        """Записать правки в PPTX.

        Args:
            filepath: Путь назначения (``.pptx``).

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает: ошибки записи показываются диалогом.
        """
        if self.presentation is None:
            self._show_error(
                "Не удалось сохранить презентацию",
                RuntimeError("Презентация не открыта"),
            )
            return False

        self._flush_current_slide()
        path = Path(filepath)
        try:
            self.presentation.save(str(path))
        except Exception as exc:  # noqa: BLE001 - права, занятый файл, битый XML
            self._show_error("Не удалось сохранить презентацию", exc)
            return False

        self.current_path = path
        self._set_modified(False)
        self.file_saved.emit(str(path))
        return True

    def _fill_slide_list(self) -> None:
        """Заполнить список слайдов названиями (``Слайд 1: Заголовок``).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.slide_list.clear()
        if self.presentation is None:
            return
        for index, slide in enumerate(self.presentation.slides, start=1):
            title = _slide_title(slide)
            label = f"{SLIDE_ITEM_PREFIX} {index}"
            item = QListWidgetItem(f"{label}: {title}" if title else label)
            item.setToolTip(title or label)
            self.slide_list.addItem(item)

    # -- выбор слайда --------------------------------------------------------
    def on_slide_selected(self, index: int) -> None:
        """Показать текстовые блоки выбранного слайда.

        Args:
            index: Индекс слайда в списке (``-1`` — выбор снят).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._flush_current_slide()
        self._current_index = index
        self._rebuild_panel(index)
        if index >= 0:
            self.slide_changed.emit(index)

    def _rebuild_panel(self, index: int) -> None:
        """Пересобрать правую панель под слайд ``index``.

        Args:
            index: Индекс слайда.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._clear_panel()
        if self.presentation is None or not 0 <= index < self.slide_count():
            self._show_empty("Слайд не выбран.")
            return

        shapes = list(_iter_text_blocks(self.presentation.slides[index].shapes))
        if not shapes:
            self._show_empty(EMPTY_SLIDE_TEXT)
            return

        self._loading = True
        try:
            for position, (caption_text, shape) in enumerate(shapes):
                caption = QLabel(caption_text, self.panel)
                caption.setObjectName(f"blockCaption{position}")
                caption_font = caption.font()
                caption_font.setBold(True)
                caption.setFont(caption_font)
                self.panel_layout.addWidget(caption)

                text = display_text(shape.text_frame)
                editor = QTextEdit(self.panel)
                editor.setObjectName(f"textBlock{position}")
                editor.setAcceptRichText(False)
                editor.setTabChangesFocus(True)
                editor.setPlainText(text)
                editor.setFixedHeight(_block_height(text))
                editor.textChanged.connect(self._on_block_changed)
                self.panel_layout.addWidget(editor)

                self._blocks.append(
                    _TextBlock(
                        shape=shape,
                        editor=editor,
                        caption=caption.text(),
                        original_text=text,
                    )
                )
        finally:
            self._loading = False

        self.panel_layout.addStretch(1)

    def _on_block_changed(self) -> None:
        """Пометить презентацию изменённой после правки текста.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._loading:
            return
        self._set_modified(True)

    def _flush_current_slide(self) -> None:
        """Перенести правки из полей ввода в фигуры текущего слайда.

        Вызывается перед сменой слайда, сохранением и русификацией — так
        текст в файле всегда соответствует тому, что видит пользователь.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self.presentation is None or self._current_index < 0:
            return

        changed = False
        for block in self._blocks:
            text = block.editor.toPlainText()
            if text == block.original_text:
                continue
            try:
                _write_text_frame(block.shape.text_frame, text)
            except Exception:  # noqa: BLE001 - битая фигура не блокирует сохранение
                continue
            block.original_text = text
            changed = True
        if changed:
            self._set_modified(True)

    # -- доступ к содержимому ------------------------------------------------
    def slide_count(self) -> int:
        """Количество слайдов в открытой презентации.

        Returns:
            int: Число слайдов (0, если файл не открыт).

        Raises:
            Ничего не бросает.
        """
        if self.presentation is None:
            return 0
        return len(self.presentation.slides)

    def block_count(self) -> int:
        """Количество текстовых блоков на текущем слайде.

        Returns:
            int: Число полей ввода.

        Raises:
            Ничего не бросает.
        """
        return len(self._blocks)

    def block_captions(self) -> list[str]:
        """Подписи текстовых блоков текущего слайда.

        Returns:
            list[str]: Подписи в порядке отображения.

        Raises:
            Ничего не бросает.
        """
        return [block.caption for block in self._blocks]

    def block_editors(self) -> list[QTextEdit]:
        """Поля ввода текущего слайда.

        Returns:
            list[QTextEdit]: Поля в порядке отображения.

        Raises:
            Ничего не бросает.
        """
        return [block.editor for block in self._blocks]

    def block_texts(self) -> list[str]:
        """Текущий текст полей ввода (без обращения к файлу).

        Returns:
            list[str]: Тексты блоков в порядке отображения.

        Raises:
            Ничего не бросает.
        """
        return [block.editor.toPlainText() for block in self._blocks]

    def current_text(self) -> str:
        """Текст текущего слайда для русификации.

        Returns:
            str: Блоки, разделённые пустой строкой.

        Raises:
            Ничего не бросает.
        """
        return "\n\n".join(self.block_texts())

    def select_slide(self, index: int) -> None:
        """Выбрать слайд программно (как клик в списке).

        Args:
            index: Индекс слайда.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.slide_list.setCurrentRow(index)

    def set_read_only(self, read_only: bool) -> None:
        """Заблокировать правку на время фоновой работы.

        Args:
            read_only: ``True`` — запретить изменения и смену слайда.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.slide_list.setEnabled(not read_only)
        for block in self._blocks:
            block.editor.setReadOnly(read_only)

    def apply_replacements(self, pairs: Sequence[tuple[str, str]]) -> int:
        """Заменить фрагменты в текстовых блоках текущего слайда.

        Поиск идёт по документу поля ввода, вставка — через ``QTextCursor``,
        поэтому новый текст наследует начертание найденного фрагмента. За
        один вызов заменяется первое вхождение каждого фрагмента — так же,
        как в DOCX-редакторе.

        Args:
            pairs: Пары ``(оригинал, замена)`` в порядке применения.

        Returns:
            int: Сколько замен реально применено.

        Raises:
            Ничего не бросает.
        """
        applied = 0
        for original, replacement in pairs:
            if not original or original == replacement:
                continue
            for block in self._blocks:
                found = block.editor.document().find(original)
                if found.isNull():
                    continue
                found.beginEditBlock()
                found.insertText(replacement)
                found.endEditBlock()
                applied += 1
                break
        if applied:
            self._flush_current_slide()
            self._set_modified(True)
        return applied

    # -- состояние -----------------------------------------------------------
    def is_modified(self) -> bool:
        """Есть ли несохранённые изменения.

        Returns:
            bool: ``True``, если текст полей отличается от текста в файле.

        Raises:
            Ничего не бросает.
        """
        if self._modified:
            return True
        return any(
            block.editor.toPlainText() != block.original_text for block in self._blocks
        )

    def mark_saved(self) -> None:
        """Сбросить флаг изменений.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._set_modified(False)

    def _set_modified(self, value: bool) -> None:
        """Обновить флаг изменений и сообщить об этом окну.

        Args:
            value: Новое значение флага.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._modified == value:
            return
        self._modified = value
        self.modified_changed.emit(value)

    # -- служебное -----------------------------------------------------------
    def _clear_panel(self) -> None:
        """Удалить все виджеты правой панели.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        while self.panel_layout.count():
            item = self.panel_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._blocks.clear()

    def _show_empty(self, message: str) -> None:
        """Показать заглушку вместо полей ввода.

        Args:
            message: Текст заглушки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._clear_panel()
        hint = QLabel(message, self.panel)
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid); padding: 24px;")
        self.panel_layout.addWidget(hint)
        self.panel_layout.addStretch(1)

    def _show_error(self, title: str, exc: BaseException) -> None:
        """Показать ошибку пользователю.

        Args:
            title: Заголовок диалога.
            exc: Исключение с описанием.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        QMessageBox.critical(self, title, str(exc))
