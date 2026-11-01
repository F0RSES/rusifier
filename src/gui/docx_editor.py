"""Виджет редактора DOCX: документ показывается как HTML в ``QTextEdit``.

Конвертация DOCX → HTML выполняется mammoth (заголовки, абзацы, списки, таблицы,
жирный/курсив), обратно — html2docx. Обе библиотеки выбраны вместо ``docwow``:
docwow жёстко ограничен Python < 3.11, а проект работает на 3.13.
"""

from __future__ import annotations

from pathlib import Path

import mammoth
from html2docx import html2docx
from lxml import etree
from lxml import html as lxml_html
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMessageBox, QTextEdit, QWidget

#: Обёртка вокруг HTML mammoth: базовые стили, чтобы документ читался в тёмной теме.
_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: "Segoe UI", "DejaVu Sans", sans-serif; font-size: 11pt; }}
table {{ border-collapse: collapse; }}
td, th {{ border: 1px solid #808080; padding: 4px 8px; }}
</style></head><body>{body}</body></html>"""


class DocxEditor(QTextEdit):
    """Редактор DOCX-документа с загрузкой и сохранением через HTML.

    Signals:
        file_loaded: путь открытого файла (после успешной загрузки).
        file_saved: путь записанного файла (после успешного сохранения).

    Args:
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    file_loaded = Signal(str)
    file_saved = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Создать пустой редактор.

        Args:
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setTabChangesFocus(False)
        #: Путь открытого файла (``None`` — документ ещё не сохранялся).
        self.current_path: Path | None = None
        #: Предупреждения конвертера для строки статуса.
        self.last_warnings: list[str] = []
        self.document().setModified(False)

    # -- модификация ---------------------------------------------------------
    def is_modified(self) -> bool:
        """Есть ли несохранённые изменения.

        Returns:
            bool: ``True``, если документ изменён после загрузки/сохранения.

        Raises:
            Ничего не бросает.
        """
        return bool(self.document().isModified())

    def mark_saved(self) -> None:
        """Сбросить флаг изменений (после сохранения).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.document().setModified(False)

    # -- файлы ---------------------------------------------------------------
    def load_file(self, filepath: str) -> bool:
        """Загрузить DOCX и показать его как HTML.

        Args:
            filepath: Путь к ``.docx``.

        Returns:
            bool: ``True`` при успехе; при ошибке показывается ``QMessageBox``
            и возвращается ``False``.

        Raises:
            Ничего не бросает: ошибки файла и конвертера показываются диалогом.
        """
        path = Path(filepath)
        try:
            if not path.is_file():
                raise FileNotFoundError(f"Файл не найден: {path}")
            with path.open("rb") as handle:
                result = mammoth.convert_to_html(handle)
        except FileNotFoundError as exc:
            self._show_error("Не удалось открыть документ", exc)
            return False
        except Exception as exc:  # noqa: BLE001 - mammoth бросает разные типы
            self._show_error("Не удалось прочитать DOCX", exc)
            return False

        self.last_warnings = [f"{m.type}: {m.message}" for m in result.messages]
        self.setHtml(_HTML_TEMPLATE.format(body=result.value))
        self.current_path = path
        self.mark_saved()
        self.file_loaded.emit(str(path))
        return True

    def save_file(self, filepath: str) -> bool:
        """Сохранить текущее содержимое редактора в DOCX.

        Args:
            filepath: Путь результата (расширение ``.docx`` добавляет вызывающий).

        Returns:
            bool: ``True`` при успехе; при ошибке показывается ``QMessageBox``
            и возвращается ``False``.

        Raises:
            Ничего не бросает: ошибки записи показываются диалогом.
        """
        path = Path(filepath)
        try:
            html_text = self._normalize_html(self.toHtml())
            document_bytes = html2docx(html_text, title=path.stem)
            path.write_bytes(document_bytes.getvalue())
        except (PermissionError, OSError) as exc:
            self._show_error("Не удалось сохранить документ", exc)
            return False
        except Exception as exc:  # noqa: BLE001 - конвертер может упасть на HTML
            self._show_error("Не удалось собрать DOCX", exc)
            return False

        self.current_path = path
        self.mark_saved()
        self.file_saved.emit(str(path))
        return True

    # -- служебное -----------------------------------------------------------
    @staticmethod
    def _normalize_html(html_text: str) -> str:
        """Привести HTML Qt к тегам, которые понимает html2docx.

        Две несовместимости, найденные на реальном ``sample.docx``:

        1. ``<td><p>…</p></td>`` — html2docx 1.6 теряет ячейку и оставляет одну
           колонку вместо двух (так размечают и mammoth, и ``QTextEdit``);
        2. ``<span style="font-weight:700">`` — Qt так рисует жирный, а html2docx
           понимает только ``<b>``/``<strong>``; без правки жирный терялся.

        Дополнительно поддерживаются курсив и подчёркивание — курсив mammoth тоже
        приходит от Qt как ``font-style:italic``.

        Args:
            html_text: HTML из ``QTextEdit.toHtml()``.

        Returns:
            str: нормализованный HTML (или исходный, если разбор не удался).

        Raises:
            Ничего не бросает.
        """
        try:
            tree = lxml_html.fromstring(html_text)
        except (ValueError, TypeError):
            return html_text

        # 0. Убрать head/style: html2docx иначе вставляет CSS текстом в документ
        #    (первый параграф сохранённого файла начинался с "p, li { white-space: … }").
        for node in tree.xpath("//head | //style | //script"):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)

        # 1. Плоское содержимое ячеек таблицы.
        for cell in tree.xpath("//td|//th"):
            children = list(cell)
            if len(children) == 1 and children[0].tag in ("p", "div"):
                children[0].drop_tag()

        # 2. Инлайн-стили Qt -> семантические теги.
        for span in list(tree.xpath("//span[@style]")):
            css = span.get("style", "").replace(" ", "").lower()
            wrappers: list[str] = []
            if "font-weight:700" in css or "font-weight:bold" in css:
                wrappers.append("b")
            if "font-style:italic" in css:
                wrappers.append("i")
            if "text-decoration:underline" in css:
                wrappers.append("u")
            if not wrappers:
                continue

            outer = etree.Element(wrappers[0])
            current = outer
            for tag in wrappers[1:]:
                current = etree.SubElement(current, tag)
            current.text = span.text
            for child in list(span):
                current.append(child)
            outer.tail = span.tail
            parent = span.getparent()
            if parent is None:
                continue
            parent.replace(span, outer)

        return lxml_html.tostring(tree, encoding="unicode")

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
