"""Виджет редактора PDF: страницы слева, рендер в центре, блоки справа.

PDF не поддерживает потоковое редактирование, поэтому интерфейс разбит на три
панели: список страниц, картинка текущей страницы (PDFium через pypdfium2) и
текстовые блоки этой страницы. Правки копятся в памяти и записываются только при
сохранении — через :meth:`DocumentProcessor.apply_replacements`, который
переносит текст по bbox строк и сам подбирает шрифт с кириллицей (DejaVu).

Рендер вынесен на ``pypdfium2``: PDFium занимает ~8 МБ против ~50 МБ у PyMuPDF,
а в собранном GUI-дистрибутиве каждый мегабайт на счету.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pypdfium2 as pdfium
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
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

from ..document_processor import PDF_OWN_FONT_PREFIXES, DocumentProcessor
from ..pdf_fonts import FONT_SUBSTITUTION_MODES

#: Подпись вкладки в главном окне (используется и в тестах).
TAB_TITLE = "PDF"

#: Масштаб рендера страницы: 1.5 даёт читаемую картинку без лишней памяти.
ZOOM = 1.5

#: Префикс элементов списка страниц.
PAGE_PREFIX = "Страница"

#: Заглушка, когда на странице нет текста.
EMPTY_PAGE_TEXT = "На странице нет текстовых блоков"

#: Заглушка, когда документ не открыт.
NO_DOCUMENT_TEXT = "Откройте PDF, чтобы увидеть страницы и текстовые блоки."

#: Заглушка, когда движок не сообщил разбивку блоков по страницам.
NO_PAGING_TEXT = "Разбивка по страницам недоступна: показаны все блоки документа."

#: Заглушка, когда запрошена страница вне диапазона.
NO_PAGE_TEXT = "Страница не выбрана"

#: Режим подмены шрифта: в GUI всегда разрешаем CID-шрифт (DejaVu), иначе
#: кириллица в PDF с base-14/встроенными шрифтами не закодируется.
FONT_SUBSTITUTION_MODE = "always"

#: Сколько ошибок максимум показывать в одном диалоге.
_MAX_ERRORS_IN_DIALOG = 5


def blank_pdf_bytes() -> bytes:
    """Собрать пустой одностраничный PDF (A4, без текста).

    Минимальный документ пишется вручную: тянуть reportlab ради пустой страницы
    в GUI-бандл незачем.

    Returns:
        bytes: валидный PDF 1.4 с одной пустой страницей A4.

    Raises:
        Ничего не бросает.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << >> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)



class PdfEditor(QWidget):
    """Текстовый редактор PDF с предпросмотром страницы.

    Signals:
        file_loaded: путь открытого PDF.
        file_saved: путь записанного PDF.
        modified_changed: флаг «есть несохранённые правки».
        page_changed: индекс выбранной страницы.
        status_message: короткое сообщение для статус-бара главного окна.

    Args:
        parent: Родительский виджет Qt.
        processor: Готовый процессор документов; ``None`` — создать свой.

    Raises:
        Ничего не бросает.
    """

    file_loaded = Signal(str)
    file_saved = Signal(str)
    modified_changed = Signal(bool)
    page_changed = Signal(int)
    status_message = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        processor: DocumentProcessor | None = None,
        font_mode: str = FONT_SUBSTITUTION_MODE,
    ) -> None:
        """Собрать пустой редактор PDF.

        Args:
            parent: Родительский виджет Qt.
            processor: Процессор документов (в тестах можно передать двойник).
            font_mode: Режим подмены шрифта из настроек (``strict``/``base14``/
                ``always``); неизвестное значение заменяется на ``always``.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self._font_mode = (
            font_mode if font_mode in FONT_SUBSTITUTION_MODES else FONT_SUBSTITUTION_MODE
        )
        self._external_processor = processor is not None
        self.processor = processor or DocumentProcessor(
            pdf_substitute_fonts=self._font_mode
        )
        #: Открытый через PyMuPDF документ (``None`` — файл не загружен).
        self._document: Any = None
        #: Путь открытого файла.
        self.current_path: Path | None = None
        #: Блоки: ``[{"block_id": int, "text": str, "page": int}]``.
        self._blocks: list[dict] = []
        #: Текущие тексты блоков (с правками пользователя).
        self._texts: dict[int, str] = {}
        #: Исходные тексты блоков (для флага изменений).
        self._original: dict[int, str] = {}
        #: Байты только что созданного PDF: файла ещё нет (``current_path`` пуст).
        self._pending_bytes: bytes | None = None
        #: Поля ввода текущей страницы: ``block_id -> QTextEdit``.
        self._editors: dict[int, QTextEdit] = {}
        self._current_page = -1
        self._page_pixmap: QPixmap | None = None
        #: Множитель рендера страниц (масштаб из ленты, 1.0 — 100 %).
        self._zoom = ZOOM
        self._paging_available = True
        self._loading = False
        self._build_ui()

    # -- сборка интерфейса ---------------------------------------------------
    def _build_ui(self) -> None:
        """Создать три панели: страницы, картинка, текстовые блоки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)

        self.page_list = QListWidget(splitter)
        self.page_list.setObjectName("pageList")
        self.page_list.setMinimumWidth(130)
        self.page_list.currentRowChanged.connect(self.render_page)
        splitter.addWidget(self.page_list)

        self.image_scroll = QScrollArea(splitter)
        self.image_scroll.setObjectName("pageScroll")
        self.image_scroll.setWidgetResizable(True)
        self.image_scroll.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop
        )
        self.image_label = QLabel(self.image_scroll)
        self.image_label.setObjectName("pageImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_scroll.setWidget(self.image_label)
        splitter.addWidget(self.image_scroll)

        self.blocks_scroll = QScrollArea(splitter)
        self.blocks_scroll.setObjectName("blocksScroll")
        self.blocks_scroll.setWidgetResizable(True)
        self.blocks_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.blocks_panel = QWidget(self.blocks_scroll)
        self.blocks_layout = QVBoxLayout(self.blocks_panel)
        self.blocks_layout.setContentsMargins(10, 10, 10, 10)
        self.blocks_layout.setSpacing(6)
        self.blocks_scroll.setWidget(self.blocks_panel)
        splitter.addWidget(self.blocks_scroll)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([150, 520, 430])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.splitter = splitter

        self._show_blocks_hint(NO_DOCUMENT_TEXT)
        self._show_page_hint(NO_DOCUMENT_TEXT)

    # -- настройки -----------------------------------------------------------
    def font_substitution_mode(self) -> str:
        """Текущий режим подмены шрифта при записи PDF.

        Returns:
            str: ``strict``, ``base14`` или ``always``.

        Raises:
            Ничего не бросает.
        """
        return self._font_mode

    def set_font_substitution_mode(self, mode: str) -> None:
        """Сменить режим подмены шрифта (значение из настроек).

        Процессор пересоздаётся только если его создал сам редактор: двойник,
        переданный в тестах, не подменяется.

        Args:
            mode: ``strict``, ``base14`` или ``always``.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        normalized = mode if mode in FONT_SUBSTITUTION_MODES else FONT_SUBSTITUTION_MODE
        if normalized == self._font_mode:
            return
        self._font_mode = normalized
        if not self._external_processor:
            self.processor = DocumentProcessor(pdf_substitute_fonts=normalized)

    # -- файлы ---------------------------------------------------------------
    def load_file(self, filepath: str) -> bool:
        """Открыть PDF: страницы, первая страница в предпросмотре, блоки.

        Args:
            filepath: Путь к ``.pdf``.

        Returns:
            bool: ``True`` при успехе; при ошибке показывается ``QMessageBox``
            и возвращается ``False``.

        Raises:
            Ничего не бросает: ошибки файла и движка показываются диалогом.
        """
        path = Path(filepath)
        self._flush_page()

        document = None
        try:
            if not path.is_file():
                raise FileNotFoundError(f"Файл не найден: {path}")
            document = pdfium.PdfDocument(str(path))
            if len(document) <= 0:
                raise ValueError("В документе нет страниц")
        except Exception as exc:  # noqa: BLE001 - битый файл, не PDF, пароль
            if document is not None:
                document.close()
            message = str(exc)
            if "password" in message.lower():
                message = "PDF защищён паролем — снимите защиту и откройте файл снова"
            self._show_error("Не удалось открыть PDF", RuntimeError(message))
            return False

        try:
            blocks = self.processor.extract_text(str(path))
        except Exception as exc:  # noqa: BLE001 - граница виджета
            document.close()
            self._show_error("Не удалось извлечь текст", exc)
            return False

        self.close_document()
        self._document = document
        self.current_path = path
        self._pending_bytes = None
        self._load_blocks(blocks, len(document))

        self.page_list.blockSignals(True)
        self.page_list.clear()
        for index in range(len(document)):
            self.page_list.addItem(QListWidgetItem(f"{PAGE_PREFIX} {index + 1}"))
        self.page_list.blockSignals(False)

        self._current_page = -1
        self.page_list.setCurrentRow(0)
        if self._current_page < 0:
            # setCurrentRow не даёт сигнала, если строка уже была нулевой.
            self.render_page(0)

        self.modified_changed.emit(False)
        self.file_loaded.emit(str(path))
        return True

    def _load_blocks(self, blocks: Sequence[dict], page_count: int) -> None:
        """Сохранить блоки и привязать их к страницам.

        Args:
            blocks: Результат ``DocumentProcessor.extract_text``.
            page_count: Число страниц в документе.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        path = self.current_path
        pages = self._page_numbers(path, len(blocks), page_count) if path else []
        self._paging_available = bool(pages) and len(pages) == len(blocks)
        self._blocks = [
            {
                "block_id": int(block["block_id"]),
                "text": str(block["text"]),
                "page": pages[index] if self._paging_available else 0,
            }
            for index, block in enumerate(blocks)
        ]
        self._original = {item["block_id"]: item["text"] for item in self._blocks}
        self._texts = dict(self._original)

    @staticmethod
    def _page_numbers(path: Path, count: int, page_count: int) -> list[int]:
        """Узнать номер страницы для каждого блока по порядку.

        ``extract_text`` не отдаёт страницы, но ``pdf_edit_engine`` строит те же
        блоки (``block_id`` совпадают), поэтому берём ``page`` из его раскладки.
        Если движок недоступен, длина не совпала или номера выходят за границы —
        возвращается пустой список, и редактор показывает все блоки сразу.

        Args:
            path: Путь к PDF.
            count: Сколько блоков нужно разложить.
            page_count: Число страниц в документе.

        Returns:
            list[int]: Номера страниц (с нуля) либо пустой список.

        Raises:
            Ничего не бросает.
        """
        if count <= 0:
            return []
        try:
            import pdf_edit_engine  # noqa: PLC0415 - тяжёлый импорт, нужен по факту

            layout = pdf_edit_engine.get_text_layout(str(path))
        except Exception:  # noqa: BLE001 - движок может отсутствовать/упасть
            return []

        pages: list[int] = []
        for item in layout:
            text = getattr(item, "text", "") or ""
            if not text.strip():
                continue
            number = int(getattr(item, "page", 0) or 0)
            if not 0 <= number < page_count:
                return []
            pages.append(number)
        return pages if len(pages) >= count else []

    # -- запись --------------------------------------------------------------
    def new_document(self) -> None:
        """Создать пустой одностраничный PDF (файла ещё нет).

        Байты держатся в памяти до первого сохранения: тогда ``save_file``
        просто запишет их по выбранному пути и откроет файл заново.

        Returns:
            None

        Raises:
            Ничего не бросает: ошибки показываются диалогом.
        """
        data = blank_pdf_bytes()
        try:
            document = pdfium.PdfDocument(data)
        except Exception as exc:  # noqa: BLE001 - сбой генератора не должен падать
            self._show_error("Не удалось создать PDF", exc)
            return

        self.close_document()
        self._document = document
        self.current_path = None
        self._pending_bytes = data
        self._blocks = []
        self._original = {}
        self._texts = {}
        self._paging_available = True
        self._current_page = -1

        self.page_list.blockSignals(True)
        self.page_list.clear()
        for index in range(len(document)):
            self.page_list.addItem(QListWidgetItem(f"{PAGE_PREFIX} {index + 1}"))
        self.page_list.blockSignals(False)

        self.page_list.setCurrentRow(0)
        if self._current_page < 0:
            self.render_page(0)
        self.modified_changed.emit(False)

    def save_file(self, filepath: str) -> bool:
        """Записать правки в PDF (или перезаписать исходный файл).

        ``DocumentProcessor`` запрещает писать результат поверх источника,
        поэтому при сохранении «на место» сначала пишем во временный файл рядом
        и только потом атомарно заменяем исходный.

        Args:
            filepath: Путь назначения (``.pdf``).

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает: ошибки и предупреждения показываются диалогом.
        """
        if self.current_path is None:
            if self._pending_bytes is None:
                self._show_error(
                    "Не удалось сохранить PDF", RuntimeError("Документ не открыт")
                )
                return False
            target = Path(filepath)
            try:
                target.write_bytes(self._pending_bytes)
            except OSError as exc:
                self._show_error("Не удалось сохранить PDF", exc)
                return False
            return self.load_file(str(target))

        self._flush_page()
        target = Path(filepath)
        changed = {
            block_id: text
            for block_id, text in self._texts.items()
            if self._original.get(block_id) != text
        }
        if not changed:
            self.mark_saved()
            self.status_message.emit("Нет изменений для записи")
            return True
        if not self._confirm_reprocessing():
            self.status_message.emit("Запись отменена")
            return False

        in_place = target.resolve() == self.current_path.resolve()
        destination = (
            target.with_name(f".{target.stem}.termru-tmp{target.suffix}")
            if in_place
            else target
        )
        destination.unlink(missing_ok=True)

        try:
            report = self.processor.apply_replacements(
                str(self.current_path), changed, str(destination)
            )
        except Exception as exc:  # noqa: BLE001 - граница виджета
            destination.unlink(missing_ok=True)
            self._show_error("Не удалось записать PDF", exc)
            return False

        if not destination.exists() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            self._show_save_errors(report)
            return False

        if in_place:
            try:
                destination.replace(target)
            except OSError as exc:
                destination.unlink(missing_ok=True)
                self._show_error("Не удалось заменить исходный файл", exc)
                return False

        self.current_path = target
        self._original = dict(self._texts)
        self.modified_changed.emit(False)
        self._report_save_result(report)
        self.file_saved.emit(str(target))

        # Движок мог перенести строки, поэтому блоки в записанном файле уже
        # другие. Перечитываем результат: иначе следующее сохранение адресовало
        # бы правки по устаревшим block_id.
        previous_page = self._current_page
        if self.load_file(str(target)) and 0 <= previous_page < self.page_count():
            self.select_page(previous_page)
        return True

    def _already_processed(self) -> bool:
        """Содержит ли открытый PDF шрифты, которые внедряет TermRu.

        Шрифты читаются через ``pdf_edit_engine.get_fonts``: pypdfium2 нужен
        только для рендера и перечислять шрифты не умеет.

        Returns:
            bool: ``True``, если файл уже проходил обработку.

        Raises:
            Ничего не бросает: ошибки чтения шрифтов считаются «неизвестно».
        """
        if self.current_path is None:
            return False
        try:
            import pdf_edit_engine  # noqa: PLC0415 - тяжёлый импорт, нужен по факту

            fonts = pdf_edit_engine.get_fonts(str(self.current_path))
        except Exception:  # noqa: BLE001 - движок может отсутствовать/упасть
            return False
        for font in fonts:
            for attribute in ("name", "postscript_name"):
                value = str(getattr(font, attribute, "") or "")
                if value.startswith(PDF_OWN_FONT_PREFIXES):
                    return True
        return False

    def _confirm_reprocessing(self) -> bool:
        """Спросить, записывать ли файл, который уже обрабатывался TermRu.

        Повторная запись пересобирает страницы и перекодирует подставленные
        шрифты: текст, заменённый в прошлый раз, может исказиться. Поэтому по
        умолчанию ответ — «нет».

        Returns:
            bool: ``True``, если пользователь разрешил запись.

        Raises:
            Ничего не бросает.
        """
        if not self._already_processed():
            return True
        answer = QMessageBox.warning(
            self,
            "PDF уже обработан",
            "В этом файле уже есть шрифты TermRu — он проходил обработку.\n\n"
            "Повторная запись пересобирает страницы и может исказить ранее "
            "заменённый текст. Надёжнее сохранить результат в новый файл.\n\n"
            "Записать всё равно?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _report_save_result(self, report: dict) -> None:
        """Сообщить о результате записи: ошибки — диалогом, прочее — статусом.

        Args:
            report: Результат :meth:`DocumentProcessor.apply_replacements`.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        errors = [item for item in (report.get("errors") or []) if isinstance(item, dict)]
        if errors:
            self._show_save_errors(report)

        counts: dict[str, int] = {}
        for warning in report.get("warnings") or []:
            if not isinstance(warning, dict):
                continue
            kind = str(warning.get("kind") or "")
            if kind:
                counts[kind] = counts.get(kind, 0) + 1

        parts: list[str] = []
        replaced = int(report.get("replaced_count") or 0)
        if replaced:
            parts.append(f"Изменено блоков: {replaced}")
        if counts.get("pdf_overflow"):
            parts.append(f"перенесено строк: {counts['pdf_overflow']}")
        if counts.get("pdf_font_too_small"):
            parts.append(f"мелкий шрифт: {counts['pdf_font_too_small']}")
        if counts.get("pdf_tf_repair"):
            parts.append(f"ремонт Tf: {counts['pdf_tf_repair']}")
        if errors:
            parts.append(f"ошибок: {len(errors)}")
        if parts:
            self.status_message.emit("; ".join(parts))

    def _show_save_errors(self, report: dict) -> None:
        """Показать ошибки записи (например, шрифт не смог закодировать текст).

        Args:
            report: Результат :meth:`DocumentProcessor.apply_replacements`.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        errors = [item for item in (report.get("errors") or []) if isinstance(item, dict)]
        if not errors:
            return
        lines = [
            f"• блок {item.get('block_id')}: {item.get('message')}"
            if item.get("block_id") is not None
            else f"• {item.get('message')}"
            for item in errors[:_MAX_ERRORS_IN_DIALOG]
        ]
        if len(errors) > _MAX_ERRORS_IN_DIALOG:
            lines.append(f"… и ещё {len(errors) - _MAX_ERRORS_IN_DIALOG}")
        QMessageBox.warning(
            self,
            "PDF записан с ошибками",
            "Не все блоки удалось заменить:\n" + "\n".join(lines),
        )

    # -- страницы ------------------------------------------------------------
    def set_zoom(self, factor: float) -> None:
        """Задать масштаб рендера страниц и перерисовать текущую.

        Args:
            factor: Множитель масштаба (1.0 — 100 %).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        value = max(0.5, min(3.0, float(factor)))
        if abs(value - self._zoom) < 1e-6:
            return
        self._zoom = value
        if self._document is not None and self._current_page >= 0:
            self.render_page(self._current_page)

    def render_page(self, index: int) -> None:
        """Показать страницу ``index``: картинку и её текстовые блоки.

        Args:
            index: Индекс страницы (``-1`` — выбор снят).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._flush_page()
        self._current_page = index
        if self._document is None:
            self._page_pixmap = None
            self._apply_pixmap()
            self._show_page_hint(NO_DOCUMENT_TEXT)
            self._show_blocks_hint(NO_DOCUMENT_TEXT)
            return
        if not 0 <= index < self.page_count():
            self._page_pixmap = None
            self._apply_pixmap()
            self._show_page_hint(NO_PAGE_TEXT)
            self._show_blocks_hint(NO_PAGE_TEXT)
            return

        self._render_pixmap(index)
        self._rebuild_blocks(index)
        self.page_changed.emit(index)

    def _render_pixmap(self, index: int) -> None:
        """Отрисовать страницу в ``self._page_pixmap`` (zoom = :data:`ZOOM`).

        Args:
            index: Индекс страницы.

        Returns:
            None

        Raises:
            Ничего не бросает: ошибка рендера показывается диалогом.
        """
        self._page_pixmap = None
        try:
            page = self._document[index]
            bitmap = page.render(scale=self._zoom)
            try:
                image = QImage(
                    bitmap.buffer,
                    bitmap.width,
                    bitmap.height,
                    bitmap.stride,
                    QImage.Format.Format_RGB888,
                )
                # ``copy`` обязателен: буфер bitmap освобождается ниже.
                self._page_pixmap = QPixmap.fromImage(image.copy())
            finally:
                bitmap.close()
                page.close()
        except Exception as exc:  # noqa: BLE001 - битая страница не должна ронять окно
            self._show_error("Не удалось отрисовать страницу", exc)
        if self._page_pixmap is None:
            self._show_page_hint("Не удалось отрисовать страницу")
            return
        self._apply_pixmap()

    def _apply_pixmap(self) -> None:
        """Показать картинку страницы, вписав её по ширине панели.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._page_pixmap is None:
            self.image_label.clear()
            return

        width = max(0, self.image_scroll.viewport().width() - 4)
        pixmap = self._page_pixmap
        if width and pixmap.width() > width:
            pixmap = pixmap.scaledToWidth(
                width, Qt.TransformationMode.SmoothTransformation
            )
        self.image_label.setPixmap(pixmap)
        self.image_label.resize(pixmap.size())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - API Qt
        """Пересчитать масштаб картинки при изменении размера окна.

        Args:
            event: Событие изменения размера.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().resizeEvent(event)
        self._apply_pixmap()

    # -- текстовые блоки -----------------------------------------------------
    def _blocks_for_page(self, index: int) -> list[dict]:
        """Блоки, относящиеся к странице ``index``.

        Если разбивку по страницам получить не удалось, показываются все блоки
        документа — так пользователь всё равно может править текст.

        Args:
            index: Индекс страницы.

        Returns:
            list[dict]: блоки в порядке ``block_id``.

        Raises:
            Ничего не бросает.
        """
        if not self._paging_available:
            return list(self._blocks)
        return [block for block in self._blocks if block["page"] == index]

    def _rebuild_blocks(self, index: int) -> None:
        """Пересобрать правую панель под страницу ``index``.

        Args:
            index: Индекс страницы.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        blocks = self._blocks_for_page(index)
        if not blocks:
            self._show_blocks_hint(
                NO_PAGING_TEXT if not self._paging_available else EMPTY_PAGE_TEXT
            )
            return

        self._clear_blocks()
        self._loading = True
        try:
            for block in blocks:
                block_id = block["block_id"]
                caption = QLabel(f"Блок {block_id}", self.blocks_panel)
                caption.setObjectName(f"blockCaption{block_id}")
                caption_font = caption.font()
                caption_font.setBold(True)
                caption.setFont(caption_font)
                self.blocks_layout.addWidget(caption)

                text = self._texts.get(block_id, block["text"])
                editor = QTextEdit(self.blocks_panel)
                editor.setObjectName(f"textBlock{block_id}")
                editor.setAcceptRichText(False)
                editor.setTabChangesFocus(True)
                editor.setPlainText(text)
                editor.setFixedHeight(max(64, min(300, 22 * (text.count("\n") + 1) + 24)))
                editor.textChanged.connect(
                    lambda block_id=block_id: self._on_block_changed(block_id)
                )
                self.blocks_layout.addWidget(editor)
                self._editors[block_id] = editor
        finally:
            self._loading = False
        self.blocks_layout.addStretch(1)

    def _on_block_changed(self, block_id: int) -> None:
        """Запомнить правку блока и обновить флаг изменений.

        Args:
            block_id: Идентификатор блока.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._loading:
            return
        editor = self._editors.get(block_id)
        if editor is None:
            return
        self._texts[block_id] = editor.toPlainText()
        self.modified_changed.emit(self.is_modified())

    def _flush_page(self) -> None:
        """Перенести текст из полей ввода в ``self._texts``.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        for block_id, editor in self._editors.items():
            self._texts[block_id] = editor.toPlainText()

    # -- доступ к содержимому ------------------------------------------------
    def page_count(self) -> int:
        """Количество страниц в открытом документе.

        Returns:
            int: Число страниц (0, если файл не открыт).

        Raises:
            Ничего не бросает.
        """
        if self._document is None:
            return 0
        return len(self._document)

    def block_count(self) -> int:
        """Количество блоков на текущей странице.

        Returns:
            int: Число полей ввода.

        Raises:
            Ничего не бросает.
        """
        return len(self._editors)

    def total_block_count(self) -> int:
        """Количество блоков во всём документе.

        Returns:
            int: Число блоков ``extract_text``.

        Raises:
            Ничего не бросает.
        """
        return len(self._blocks)

    def block_editors(self) -> list[QTextEdit]:
        """Поля ввода текущей страницы.

        Returns:
            list[QTextEdit]: поля по возрастанию ``block_id``.

        Raises:
            Ничего не бросает.
        """
        return [self._editors[key] for key in sorted(self._editors)]

    def block_ids(self) -> list[int]:
        """Идентификаторы блоков текущей страницы.

        Returns:
            list[int]: ``block_id`` по возрастанию.

        Raises:
            Ничего не бросает.
        """
        return sorted(self._editors)

    def block_texts(self) -> list[str]:
        """Текущий текст полей ввода (без обращения к файлу).

        Returns:
            list[str]: тексты блоков по возрастанию ``block_id``.

        Raises:
            Ничего не бросает.
        """
        return [editor.toPlainText() for editor in self.block_editors()]

    def current_text(self) -> str:
        """Текст текущей страницы для русификации.

        Returns:
            str: блоки, разделённые пустой строкой.

        Raises:
            Ничего не бросает.
        """
        return "\n\n".join(self.block_texts())

    def select_page(self, index: int) -> None:
        """Выбрать страницу программно (как клик в списке).

        Args:
            index: Индекс страницы; вне диапазона игнорируется.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if not 0 <= index < self.page_count():
            return
        self.page_list.setCurrentRow(index)
        if self.page_list.currentRow() != index:  # строка уже была текущей
            self.render_page(index)

    def set_read_only(self, read_only: bool) -> None:
        """Заблокировать правку на время фоновой работы.

        Args:
            read_only: ``True`` — запретить изменения и смену страницы.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.page_list.setEnabled(not read_only)
        for editor in self._editors.values():
            editor.setReadOnly(read_only)

    def apply_replacements(self, pairs: Sequence[tuple[str, str]]) -> int:
        """Заменить фрагменты в блоках текущей страницы.

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
            for block_id in self.block_ids():
                editor = self._editors[block_id]
                found = editor.document().find(original)
                if found.isNull():
                    continue
                found.beginEditBlock()
                found.insertText(replacement)
                found.endEditBlock()
                applied += 1
                break
        if applied:
            self._flush_page()
            self.modified_changed.emit(self.is_modified())
        return applied

    # -- состояние -----------------------------------------------------------
    def is_modified(self) -> bool:
        """Есть ли несохранённые правки.

        Returns:
            bool: ``True``, если текст хотя бы одного блока изменился.

        Raises:
            Ничего не бросает.
        """
        return any(
            self._original.get(block_id) != text
            for block_id, text in self._texts.items()
        )

    def mark_saved(self) -> None:
        """Считать текущие тексты сохранёнными.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._flush_page()
        self._original = dict(self._texts)
        self.modified_changed.emit(False)

    # -- служебное -----------------------------------------------------------
    def close_document(self) -> None:
        """Закрыть документ PyMuPDF и очистить панели.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._document is not None:
            try:
                self._document.close()
            except Exception:  # noqa: BLE001 - закрытие не должно ронять окно
                pass
        self._document = None
        self.current_path = None
        self._blocks = []
        self._texts = {}
        self._original = {}
        self._pending_bytes = None
        self._paging_available = True
        self._current_page = -1
        self._page_pixmap = None
        self._clear_blocks()
        self.page_list.blockSignals(True)
        self.page_list.clear()
        self.page_list.blockSignals(False)
        self.image_label.clear()
        self._show_blocks_hint(NO_DOCUMENT_TEXT)
        self._show_page_hint(NO_DOCUMENT_TEXT)

    def _clear_blocks(self) -> None:
        """Удалить все виджеты правой панели.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        while self.blocks_layout.count():
            item = self.blocks_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._editors.clear()

    def _show_blocks_hint(self, message: str) -> None:
        """Показать заглушку вместо полей ввода.

        Args:
            message: Текст заглушки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._clear_blocks()
        hint = QLabel(message, self.blocks_panel)
        hint.setObjectName("blocksHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid); padding: 24px;")
        self.blocks_layout.addWidget(hint)
        self.blocks_layout.addStretch(1)

    def _show_page_hint(self, message: str) -> None:
        """Показать заглушку вместо картинки страницы.

        Args:
            message: Текст заглушки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.image_label.clear()
        self.image_label.setText(message)
        self.image_label.setWordWrap(True)
        self.image_label.resize(self.image_scroll.viewport().size())

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
