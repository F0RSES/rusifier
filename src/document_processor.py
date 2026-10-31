"""Чтение и сохранение документов DOCX, PPTX и PDF.

Модуль извлекает текстовые блоки с привязкой к их месту в документе и записывает
изменённые блоки обратно, не нарушая исходное форматирование.

Модель блоков
-------------
* DOCX/PPTX — блок = inline-контейнер модели :mod:`polydoc` (параграф, заголовок,
  элемент списка, ячейка таблицы, содержимое которых хранится как список ``Inline``),
  либо «плоское» текстовое поле (заголовок/заметки слайда, код, подпись).
* PDF — блок = ``pdf_edit_engine.models.TextBlock`` (строка текста с bbox и шрифтом).

``block_id`` — это индекс блока в порядке обхода документа **после** отбрасывания
пустых и whitespace-only блоков. Обход детерминирован, поэтому ``extract_text`` и
``apply_replacements`` дают одинаковые идентификаторы для одного и того же файла.

Сохранение форматирования
-------------------------
Наивная замена по тексту ломает стили: в DOCX/PPTX слово часто разбито на несколько
runs (часть bold/italic), поэтому ``run.text.replace(...)`` либо не находит фразу,
либо схлопывает абзац в один run.

Здесь применяется посимвольный diff (``difflib.SequenceMatcher``) между старым и
новым текстом блока, и правки вносятся только в изменённые диапазоны. Неизменённые
участки сохраняют свои runs и стили; вставленный текст наследует стиль run, с
которого началось изменение (поведение, аналогичное ``polydoc.edit.replace_text``).

PDF
---
Полностью сохраняющий вёрстку путь — ``pdf_edit_engine`` (правка content stream с
тем же шрифтом и кеглем по bbox строки). ``polydoc`` для PDF — writer на ReportLab
(«PDF writer, built on ReportLab's Platypus layout engine»), то есть документ
**перерисовывается** и исходная вёрстка теряется. Поэтому порядок движков задан
константой :data:`PDF_ENGINE_ORDER`, где первым идёт сохраняющий вёрстку движок;
``polydoc`` остаётся fallback-ом и явно логирует потерю вёрстки.

Ограничение PDF: замена англицизма на кириллицу возможна только если шрифт
документа это позволяет (Type0/Identity-H с расширяемым subset). Для base-14
шрифтов (Helvetica и т.п.) ``pdf_edit_engine`` вернёт ``success=False`` с
``glyphs_missing``; такая ошибка попадает в ``errors`` результата, файл не портится.
"""

from __future__ import annotations

import dataclasses
import difflib
import logging
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pikepdf
import polydoc
from polydoc.model import CodeBlock, Image, Inline, Slide, Table, Text
from polydoc.model.inline import iter_text_nodes, merge_runs

from .pdf_fonts import (
    MISSING_FONT_MESSAGE,
    PdfFontResolver,
    add_cyrillic_font,
    is_base14_font,
    repair_inherited_font,
    text_has_cyrillic,
)

__all__ = [
    "DocumentProcessor",
    "DocumentProcessingError",
    "SUPPORTED_FORMATS",
    "UnsupportedFormatError",
]

LOGGER = logging.getLogger(__name__)

#: Форматы, с которыми работает модуль.
SUPPORTED_FORMATS: tuple[str, ...] = ("docx", "pptx", "pdf")

#: Расширение файла (в нижнем регистре) -> формат.
_FORMAT_BY_SUFFIX: dict[str, str] = {".docx": "docx", ".pptx": "pptx", ".pdf": "pdf"}

#: Порядок движков для PDF.
#:
#: Изначальное ТЗ предполагало «сначала polydoc, при ошибке pdf-edit-engine».
#: Порядок сознательно обратный: polydoc перерисовывает PDF через ReportLab и теряет
#: исходную вёрстку, а pdf-edit-engine правит content stream на месте. Если нужен
#: прежний порядок — поменяйте кортеж на ("polydoc", "pdf_edit_engine").
PDF_ENGINE_ORDER: tuple[str, ...] = ("pdf_edit_engine", "polydoc")

#: Максимальная длина текста блока, для которого считается посимвольный diff.
#: Для более длинных блоков применяется замена «целиком в первый run».
_MAX_DIFF_CHARS = 20_000

#: Плоские строковые поля, несущие видимый пользователю текст.
#: Аналог ``polydoc.edit.text._PLAIN_FIELDS``, но только для извлечения блоков.
_PLAIN_TEXT_FIELDS: frozenset[tuple[type, str]] = frozenset(
    {
        (Slide, "title"),
        (Slide, "notes"),
        (CodeBlock, "code"),
        (Image, "alt"),
        (Image, "caption"),
        (Table, "caption"),
    }
)


class UnsupportedFormatError(Exception):
    """Формат файла не поддерживается.

    Attributes:
        path: Путь к файлу (или его расширение), который не удалось распознать.
        supported: Кортеж поддерживаемых форматов.
    """

    def __init__(self, path: str, supported: Sequence[str] = SUPPORTED_FORMATS) -> None:
        """Создать исключение о неподдерживаемом формате.

        Args:
            path: Путь к файлу или его расширение.
            supported: Поддерживаемые форматы для текста сообщения.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.path = path
        self.supported = tuple(supported)
        super().__init__(
            f"Неподдерживаемый формат: {path!r}. "
            f"Поддерживаются: {', '.join(self.supported)}."
        )


class DocumentProcessingError(Exception):
    """Ошибка чтения, разбора или записи документа.

    Поднимается, когда файла нет, он повреждён, недоступен для чтения или запись
    результата невозможна. Ошибки отдельных блоков при замене сюда не входят —
    они возвращаются в ``errors`` результата :meth:`DocumentProcessor.apply_replacements`.
    """


#: Логгер библиотеки pdf_edit_engine (её подмодули — дочерние логгеры).
_PDF_ENGINE_LOGGER = "pdf_edit_engine"


class _PdfEngineNoiseCounter(logging.Handler):
    """Считает и глушит внутренние предупреждения ``pdf_edit_engine``.

    Библиотека пишет ``logger.warning("Font extension failed for %s", ...,
    exc_info=True)`` (``structural.py:1196``), когда пытается расширить
    base-14 шрифт, терпит неудачу и берёт подставленный CID-шрифт. Для нас это
    штатный путь, но в stderr это выглядит как трейсбек на каждый блок.

    Хендлер только считает записи: ``propagate=False`` на логгере не пускает их
    в корневой обработчик, поэтому в stderr они не попадают.
    """

    def __init__(self) -> None:
        """Создать счётчик предупреждений движка.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(level=logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Учесть одно предупреждение и не выводить его.

        Args:
            record: Запись логирования.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.count += 1


def _suppress_pdf_engine_noise() -> _PdfEngineNoiseCounter:
    """Перехватить предупреждения ``pdf_edit_engine``.

    Уровни логгеров не меняются (иначе записи не дошли бы до хендлера);
    достаточно отключить всплытие в корневой логгер и повесить счётчик.

    Returns:
        _PdfEngineNoiseCounter: хендлер со счётчиком (нужен для восстановления).

    Raises:
        Ничего не бросает.
    """
    logger = logging.getLogger(_PDF_ENGINE_LOGGER)
    counter = _PdfEngineNoiseCounter()
    logger.addHandler(counter)
    logger.propagate = False
    return counter


def _restore_pdf_engine_logging(counter: _PdfEngineNoiseCounter) -> None:
    """Вернуть логирование ``pdf_edit_engine`` в исходное состояние.

    Args:
        counter: Хендлер, установленный :func:`_suppress_pdf_engine_noise`.

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    logger = logging.getLogger(_PDF_ENGINE_LOGGER)
    logger.removeHandler(counter)
    logger.propagate = True


@dataclass
class _TextUnit:
    """Внутреннее представление текстового блока документа.

    Attributes:
        block_id: Индекс блока в порядке обхода (после отбрасывания пустых блоков).
        kind: ``"inline"`` (список ``Inline``), ``"plain"`` (строка) или ``"pdf"``.
        node: Узел модели polydoc либо ``TextBlock`` из pdf-edit-engine.
        field: Имя поля узла, в котором лежит текст (пусто для ``"pdf"``).
        text: Плоский текст блока.
    """

    block_id: int
    kind: str
    node: Any
    field: str
    text: str


def _load_pdf_engine() -> Any | None:
    """Лениво импортировать ``pdf_edit_engine``.

    Returns:
        Модуль ``pdf_edit_engine`` либо ``None``, если пакет не установлен.

    Raises:
        Ничего не бросает: ``ImportError`` логируется и превращается в ``None``.
    """
    try:
        import pdf_edit_engine  # noqa: PLC0415 - тяжёлый импорт, нужен лениво
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        LOGGER.warning("pdf_edit_engine недоступен, PDF будет обработан через polydoc: %s", exc)
        return None
    return pdf_edit_engine


def _splice_span(
    spans: Sequence[tuple[Text, int, int]],
    start: int,
    end: int,
    new_text: str,
) -> None:
    """Заменить диапазон ``[start, end)`` плоского текста, разложив его по runs.

    Текст попадает в первый затронутый run (и наследует его стиль), из остальных
    затронутых runs соответствующие символы удаляются. Мутирует ``leaf.text``.

    Args:
        spans: Список ``(run, начало, конец)`` — карта плоского текста на runs.
        start: Начало заменяемого диапазона в плоском тексте.
        end: Конец (не включительно) заменяемого диапазона.
        new_text: Текст замены.

    Returns:
        None

    Raises:
        Ничего не бросает; пустой ``spans`` — no-op.
    """
    if not spans:
        return

    if start == end:  # чистая вставка
        leaf, offset = spans[-1][0], len(spans[-1][0].text)
        for candidate, leaf_start, leaf_end in spans:
            if leaf_start <= start <= leaf_end:
                leaf, offset = candidate, start - leaf_start
            if leaf_start > start:
                break
        leaf.text = leaf.text[:offset] + new_text + leaf.text[offset:]
        return

    first = True
    for leaf, leaf_start, leaf_end in spans:
        if leaf_end <= start or leaf_start >= end:
            continue  # run не пересекается с диапазоном
        local_start = max(0, start - leaf_start)
        local_end = min(len(leaf.text), end - leaf_start)
        if first:
            leaf.text = leaf.text[:local_start] + new_text + leaf.text[local_end:]
            first = False
        else:
            leaf.text = leaf.text[:local_start] + leaf.text[local_end:]


def _rewrite_inlines(
    content: Sequence[Inline],
    new_text: str,
) -> tuple[list[Inline], bool]:
    """Переписать плоский текст inline-контейнера, сохранив стили runs.

    Считает посимвольный diff между текущим и новым текстом и правит только
    изменённые диапазоны, поэтому неизменённые runs (bold/italic/ссылки) остаются
    нетронутыми.

    Args:
        content: Список ``Inline`` (содержимое параграфа, ячейки, заголовка).
        new_text: Новый плоский текст блока.

    Returns:
        tuple[list[Inline], bool]: (новый список ``Inline``, был ли текст изменён).

    Raises:
        ValueError: если ``new_text`` не является строкой.
    """
    if not isinstance(new_text, str):
        raise ValueError(f"new_text должен быть строкой, получено {type(new_text).__name__}")

    leaves = iter_text_nodes(content)
    if not leaves:
        if not new_text:
            return list(content), False
        return [Text(new_text)], True

    spans: list[tuple[Text, int, int]] = []
    cursor = 0
    for leaf in leaves:
        spans.append((leaf, cursor, cursor + len(leaf.text)))
        cursor += len(leaf.text)

    old_text = "".join(leaf.text for leaf in leaves)
    if old_text == new_text:
        return list(content), False

    if len(old_text) > _MAX_DIFF_CHARS or len(new_text) > _MAX_DIFF_CHARS:
        # Защита от квадратичного diff на аномально длинных блоках: весь новый
        # текст кладём в первый run, остальные очищаем.
        leaves[0].text = new_text
        for leaf in leaves[1:]:
            leaf.text = ""
        return merge_runs(list(content)), True

    matcher = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
    # Справа налево: смещения слева остаются валидными после правок справа.
    for tag, i1, i2, j1, j2 in reversed(matcher.get_opcodes()):
        if tag == "equal":
            continue
        _splice_span(spans, i1, i2, new_text[j1:j2])

    return merge_runs(list(content)), True


class DocumentProcessor:
    """Чтение текста из DOCX/PPTX/PDF и запись замен с сохранением форматирования.

    Пример:
        >>> processor = DocumentProcessor(log_callback=print)
        >>> blocks = processor.extract_text("report.docx")
        >>> result = processor.apply_replacements(
        ...     "report.docx",
        ...     {blocks[0]["block_id"]: "Новый текст"},
        ...     "report.ru.docx",
        ... )

    Args:
        log_callback: Необязательный ``callable(level: str, msg: str)`` для интеграции
            с CLI. Уровни: ``"debug"``, ``"info"``, ``"warning"``, ``"error"``.
    """

    def __init__(
        self,
        log_callback: Callable[[str, str], None] | None = None,
        *,
        pdf_font_dir: str | Path | None = None,
        pdf_fallback: str = "fail",
        pdf_engine_noise: bool = False,
    ) -> None:
        """Создать процессор документов.

        Args:
            log_callback: Необязательный ``callable(level: str, msg: str)``; уровни —
                ``"debug"``, ``"info"``, ``"warning"``, ``"error"``.
            pdf_font_dir: Дополнительная папка с TTF для PDF-замен (портативная
                сборка / ``--pdf-font-dir``).
            pdf_fallback: Что делать, если кириллический шрифт не найден:
                ``"fail"`` — ошибка по блоку, ``"polydoc"`` — пересобрать PDF
                через polydoc (вёрстка теряется).
            pdf_engine_noise: ``True`` — не подавлять внутренние предупреждения
                ``pdf_edit_engine`` (их трейсбеки полезны с ``--verbose``).

        Returns:
            None

        Raises:
            ValueError: неизвестное значение ``pdf_fallback``.
        """
        if pdf_fallback not in ("fail", "polydoc"):
            raise ValueError(
                f"pdf_fallback должен быть 'fail' или 'polydoc', получено {pdf_fallback!r}"
            )
        self._log_callback = log_callback
        self._pdf_font_dir = Path(pdf_font_dir) if pdf_font_dir else None
        self._pdf_fallback = pdf_fallback
        self._pdf_engine_noise = bool(pdf_engine_noise)
        self._font_resolver: PdfFontResolver | None = None

    @property
    def font_resolver(self) -> PdfFontResolver:
        """Резолвер кириллических TTF (создаётся при первом обращении).

        Returns:
            PdfFontResolver: резолвер, который ищет шрифты в
            ``pdf_font_dir``, затем в системных путях.

        Raises:
            Ничего не бросает.
        """
        if self._font_resolver is None:
            self._font_resolver = PdfFontResolver(
                bundled_dir=self._pdf_font_dir,
                log_callback=self._log_callback,
            )
        return self._font_resolver

    # -- логирование ---------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        """Записать сообщение в ``logging`` и в ``log_callback``.

        Args:
            level: ``"debug"``, ``"info"``, ``"warning"`` или ``"error"``.
            message: Текст сообщения.

        Returns:
            None

        Raises:
            Ничего не бросает: исключение внутри ``log_callback`` логируется и
            гасится, чтобы сбой колбэка не ломал обработку документа.
        """
        getattr(LOGGER, level, LOGGER.info)("%s", message)
        if self._log_callback is None:
            return
        try:
            self._log_callback(level, message)
        except Exception as exc:  # noqa: BLE001 - колбэк внешний, не ломает обработку
            LOGGER.warning("log_callback завершился ошибкой: %s", exc)

    # -- определение формата -------------------------------------------------

    def detect_format(self, file_path: str) -> str:
        """Определить формат документа по расширению файла.

        Args:
            file_path: Путь к файлу.

        Returns:
            str: ``"docx"``, ``"pptx"`` или ``"pdf"``.

        Raises:
            DocumentProcessingError: путь пустой, не существует или не является файлом.
            UnsupportedFormatError: расширение не входит в :data:`SUPPORTED_FORMATS`
                (например, ``.txt``).
        """
        if not file_path or not str(file_path).strip():
            raise DocumentProcessingError("Пустой путь к файлу")

        path = Path(file_path).expanduser()
        if not path.exists():
            raise DocumentProcessingError(f"Файл не найден: {path}")
        if not path.is_file():
            raise DocumentProcessingError(f"Путь не является файлом: {path}")

        fmt = _FORMAT_BY_SUFFIX.get(path.suffix.lower())
        if fmt is None:
            raise UnsupportedFormatError(path.suffix or str(path))
        return fmt


    # -- чтение --------------------------------------------------------------

    def extract_text(self, file_path: str) -> list[dict]:
        """Извлечь текстовые блоки документа.

        Args:
            file_path: Путь к файлу ``.docx``, ``.pptx`` или ``.pdf``.

        Returns:
            list[dict]: Список ``{"block_id": int, "text": str, "source": str}``,
            где ``source`` — ``"docx"``, ``"pptx"`` или ``"pdf"``. ``block_id`` —
            последовательные индексы с нуля в порядке обхода; пустые и
            whitespace-only блоки в список не попадают. Для документа без текста
            возвращается пустой список.

        Raises:
            UnsupportedFormatError: формат не поддерживается.
            DocumentProcessingError: файл не найден, повреждён, защищён паролем или
                не может быть разобран доступным движком.
        """
        fmt = self.detect_format(file_path)
        path = Path(file_path)

        try:
            if fmt == "pdf":
                units, backend = self._pdf_units(path)
                self._log("debug", f"PDF прочитан движком {backend}")
            else:
                units = list(self._iter_units(polydoc.open(str(path))))
        except (UnsupportedFormatError, DocumentProcessingError):
            raise
        except Exception as exc:  # noqa: BLE001 - библиотеки бросают разные типы ошибок
            self._log("error", f"Не удалось прочитать {path.name}: {exc}")
            raise DocumentProcessingError(f"Не удалось прочитать {path}: {exc}") from exc

        blocks = [
            {"block_id": unit.block_id, "text": unit.text, "source": fmt} for unit in units
        ]
        self._log("info", f"{path.name}: извлечено блоков — {len(blocks)}")
        return blocks

    # -- обход модели polydoc ------------------------------------------------

    def _iter_units(self, document: Any) -> Iterator[_TextUnit]:
        """Итерировать текстовые блоки модели ``polydoc`` в порядке обхода.

        Args:
            document: Документ ``polydoc.model.Document``.

        Returns:
            Iterator[_TextUnit]: Блоки с последовательными ``block_id``; пустые и
            whitespace-only пропускаются.

        Raises:
            Ничего не бросает: недоступные поля узла пропускаются.
        """
        block_id = 0
        for node in document.walk():
            if not dataclasses.is_dataclass(node):
                continue
            for spec in dataclasses.fields(node):
                try:
                    value = getattr(node, spec.name)
                except AttributeError:  # pragma: no cover - нестандартные узлы
                    continue
                kind = self._unit_kind(node, spec.name, value)
                if kind is None:
                    continue
                text = self._unit_text(kind, value)
                if not text.strip():
                    continue
                yield _TextUnit(
                    block_id=block_id,
                    kind=kind,
                    node=node,
                    field=spec.name,
                    text=text,
                )
                block_id += 1

    @staticmethod
    def _unit_kind(node: Any, field_name: str, value: Any) -> str | None:
        """Определить, является ли поле узла текстовым блоком.

        Args:
            node: Узел модели polydoc.
            field_name: Имя поля узла.
            value: Значение поля.

        Returns:
            str | None: ``"inline"`` для списка ``Inline``, ``"plain"`` для известных
            строковых полей (заголовок/заметки слайда, код, подпись), иначе ``None``.

        Raises:
            Ничего не бросает.
        """
        if isinstance(value, list) and value and all(isinstance(item, Inline) for item in value):
            return "inline"
        if isinstance(value, str) and (type(node), field_name) in _PLAIN_TEXT_FIELDS:
            return "plain"
        return None

    @staticmethod
    def _unit_text(kind: str, value: Any) -> str:
        """Получить плоский текст блока.

        Args:
            kind: ``"inline"`` или ``"plain"``.
            value: Список ``Inline`` либо строка.

        Returns:
            str: Плоский текст блока (runs склеиваются без разделителей).

        Raises:
            Ничего не бросает.
        """
        if kind == "inline":
            return "".join(leaf.text for leaf in iter_text_nodes(value))
        return value


    # -- запись --------------------------------------------------------------

    def apply_replacements(
        self,
        file_path: str,
        replacements: dict[int, str],
        output_path: str,
    ) -> dict:
        """Применить замены к блокам и сохранить результат в новый файл.

        Исходный файл не изменяется: результат всегда пишется в ``output_path``.

        Args:
            file_path: Путь к исходному ``.docx``/``.pptx``/``.pdf``.
            replacements: Отображение ``{block_id: new_text}`` — новый полный текст
                блока (не фрагмент). Ключи приводятся к ``int``.
            output_path: Путь для результата; расширение должно совпадать с исходным,
                путь не должен совпадать с ``file_path``.

        Returns:
            dict: ``{"success": bool, "errors": list[dict], "replaced_count": int}``.
            ``errors`` — список ``{"block_id": int | None, "message": str}``;
            ``replaced_count`` — число блоков, текст которых реально изменился.
            ``success`` равно ``True`` только если файл записан и ошибок нет; при
            частичном успехе файл содержит применённые замены, а ``success`` — ``False``.

        Raises:
            Ничего не бросает для ошибок файлов и формата — они попадают в ``errors``.
            Непредвиденные сбои логируются и также возвращаются в ``errors``.
        """
        errors: list[dict] = []
        result: dict = {"success": False, "errors": errors, "replaced_count": 0}

        try:
            fmt = self.detect_format(file_path)
        except (UnsupportedFormatError, DocumentProcessingError) as exc:
            self._log("error", str(exc))
            errors.append({"block_id": None, "message": str(exc)})
            return result

        src = Path(file_path).expanduser()
        dst = Path(output_path).expanduser()

        if src.resolve() == dst.resolve():
            message = "output_path совпадает с исходным файлом: перезапись источника запрещена"
            self._log("error", message)
            errors.append({"block_id": None, "message": message})
            return result

        if dst.suffix.lower() != src.suffix.lower():
            message = (
                f"Расширение результата {dst.suffix!r} не совпадает "
                f"с исходным {src.suffix!r}"
            )
            self._log("error", message)
            errors.append({"block_id": None, "message": message})
            return result

        normalized, normalization_errors = self._normalize_replacements(replacements)
        errors.extend(normalization_errors)

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not normalized:
                shutil.copy2(src, dst)
                self._log("info", f"{src.name}: замен нет, файл скопирован в {dst.name}")
            else:
                applied, apply_errors = self._apply(fmt, src, normalized, dst)
                errors.extend(apply_errors)
                result["replaced_count"] = applied
                self._log("info", f"{src.name}: применено замен — {applied}")
        except Exception as exc:  # noqa: BLE001 - границы модуля: наружу только dict
            self._log("error", f"Не удалось записать {dst.name}: {exc}")
            errors.append({"block_id": None, "message": f"Не удалось записать результат: {exc}"})
            return result

        result["success"] = not errors and dst.exists()
        if errors:
            self._log("warning", f"{dst.name}: завершено с ошибками ({len(errors)})")
        return result

    def _apply(
        self,
        fmt: str,
        src: Path,
        replacements: dict[int, str],
        dst: Path,
    ) -> tuple[int, list[dict]]:
        """Применить замены движком, соответствующим формату.

        Args:
            fmt: ``"docx"``, ``"pptx"`` или ``"pdf"``.
            src: Исходный файл.
            replacements: Нормализованные замены ``{block_id: new_text}``.
            dst: Файл результата.

        Returns:
            tuple[int, list[dict]]: (число изменённых блоков, ошибки по блокам).

        Raises:
            Exception: любые ошибки чтения/записи пробрасываются вызывающему
                :meth:`apply_replacements`, который превращает их в ``errors``.
        """
        if fmt == "pdf":
            return self._apply_pdf(src, replacements, dst)
        return self._apply_polydoc(src, replacements, dst)

    def _apply_polydoc(
        self,
        src: Path,
        replacements: dict[int, str],
        dst: Path,
    ) -> tuple[int, list[dict]]:
        """Применить замены к DOCX/PPTX через модель polydoc.

        Args:
            src: Исходный ``.docx``/``.pptx``.
            replacements: Нормализованные замены.
            dst: Файл результата.

        Returns:
            tuple[int, list[dict]]: (число изменённых блоков, ошибки по блокам).

        Raises:
            Exception: ошибки чтения/записи polydoc пробрасываются наверх.
        """
        document = polydoc.open(str(src))
        applied, errors = self._apply_units(list(self._iter_units(document)), replacements)
        document.save(str(dst))
        return applied, errors


    def _apply_units(
        self,
        units: Sequence[_TextUnit],
        replacements: dict[int, str],
    ) -> tuple[int, list[dict]]:
        """Применить замены к набору блоков polydoc.

        Args:
            units: Блоки в том же порядке, в котором их отдаёт ``extract_text``.
            replacements: Нормализованные замены ``{block_id: new_text}``.

        Returns:
            tuple[int, list[dict]]: (число изменённых блоков, ошибки по блокам).

        Raises:
            Ничего не бросает: сбой отдельного блока логируется и попадает в ошибки.
        """
        by_id = {unit.block_id: unit for unit in units}
        applied = 0
        errors: list[dict] = []

        for block_id, new_text in sorted(replacements.items()):
            unit = by_id.get(block_id)
            if unit is None:
                message = f"Блок {block_id} не найден (доступно блоков: {len(units)})"
                self._log("error", message)
                errors.append({"block_id": block_id, "message": message})
                continue
            try:
                changed = self._set_unit_text(unit, new_text)
            except Exception as exc:  # noqa: BLE001 - один блок не должен ломать остальные
                message = f"Не удалось заменить текст блока {block_id}: {exc}"
                self._log("error", message)
                errors.append({"block_id": block_id, "message": message})
                continue
            if changed:
                applied += 1
            else:
                self._log("debug", f"Блок {block_id}: текст не изменился, пропущено")

        return applied, errors

    def _set_unit_text(self, unit: _TextUnit, new_text: str) -> bool:
        """Заменить текст одного блока, сохранив стили runs.

        Args:
            unit: Блок, полученный при обходе документа.
            new_text: Новый полный текст блока.

        Returns:
            bool: ``True``, если текст изменился; ``False``, если он совпадал.

        Raises:
            ValueError: если ``new_text`` не является строкой.
            AttributeError: если узел модели не позволяет изменить поле.
        """
        if not isinstance(new_text, str):
            raise ValueError(
                f"new_text должен быть строкой, получено {type(new_text).__name__}"
            )

        if unit.kind == "plain":
            if unit.text == new_text:
                return False
            setattr(unit.node, unit.field, new_text)
            return True

        content = getattr(unit.node, unit.field)
        rewritten, changed = _rewrite_inlines(content, new_text)
        if not changed:
            return False
        setattr(unit.node, unit.field, rewritten)
        unit.node.adopt(*rewritten)
        return True

    def _normalize_replacements(
        self,
        replacements: Any,
    ) -> tuple[dict[int, str], list[dict]]:
        """Привести замены к ``{int: str}`` и собрать ошибки валидации.

        Args:
            replacements: Пользовательское отображение ``{block_id: new_text}``.

        Returns:
            tuple[dict[int, str], list[dict]]: (валидные замены, ошибки валидации).

        Raises:
            Ничего не бросает: некорректные записи попадают в ошибки.
        """
        errors: list[dict] = []
        normalized: dict[int, str] = {}

        if not isinstance(replacements, Mapping):
            message = (
                "replacements должен быть отображением {block_id: new_text}, "
                f"получено {type(replacements).__name__}"
            )
            self._log("error", message)
            errors.append({"block_id": None, "message": message})
            return normalized, errors

        for raw_id, raw_text in replacements.items():
            try:
                block_id = int(raw_id)
            except (TypeError, ValueError):
                message = f"block_id должен быть целым числом, получено {raw_id!r}"
                self._log("error", message)
                errors.append({"block_id": None, "message": message})
                continue
            if block_id < 0:
                message = f"block_id не может быть отрицательным: {block_id}"
                self._log("error", message)
                errors.append({"block_id": block_id, "message": message})
                continue
            if not isinstance(raw_text, str):
                message = (
                    f"new_text для блока {block_id} должен быть строкой, "
                    f"получено {type(raw_text).__name__}"
                )
                self._log("error", message)
                errors.append({"block_id": block_id, "message": message})
                continue
            normalized[block_id] = raw_text

        return normalized, errors


    # -- PDF -----------------------------------------------------------------

    def _pdf_units(self, path: Path) -> tuple[list[_TextUnit], str]:
        """Извлечь блоки PDF движком из :data:`PDF_ENGINE_ORDER`.

        Args:
            path: Путь к PDF-файлу.

        Returns:
            tuple[list[_TextUnit], str]: (блоки, имя движка, прочитавшего файл).

        Raises:
            DocumentProcessingError: ни один движок не смог прочитать документ.
        """
        for backend in PDF_ENGINE_ORDER:
            if backend == "pdf_edit_engine":
                engine = _load_pdf_engine()
                if engine is None:
                    continue
                try:
                    units = self._engine_units(engine, path)
                except Exception as exc:  # noqa: BLE001 - пробуем следующий движок
                    self._log("warning", f"pdf_edit_engine не прочитал {path.name}: {exc}")
                    continue
                self._log("debug", f"{path.name}: блоки из pdf_edit_engine ({len(units)})")
                return units, backend

            if backend == "polydoc":
                try:
                    document = polydoc.open(str(path))
                except Exception as exc:  # noqa: BLE001 - пробуем следующий движок
                    self._log("warning", f"polydoc не прочитал {path.name}: {exc}")
                    continue
                units = list(self._iter_units(document))
                self._log("debug", f"{path.name}: блоки из polydoc ({len(units)})")
                return units, backend

        raise DocumentProcessingError(f"Ни один движок PDF не смог прочитать документ: {path}")

    @staticmethod
    def _engine_units(engine: Any, path: Path) -> list[_TextUnit]:
        """Построить блоки по ``pdf_edit_engine.get_text_layout``.

        Args:
            engine: Модуль ``pdf_edit_engine``.
            path: Путь к PDF-файлу.

        Returns:
            list[_TextUnit]: Блоки уровня строки с последовательными ``block_id``;
            блоки без видимого текста пропускаются.

        Raises:
            Exception: ошибки ``pdf_edit_engine`` пробрасываются вызывающему.
        """
        units: list[_TextUnit] = []
        for raw in engine.get_text_layout(str(path)):
            text = getattr(raw, "text", "") or ""
            if not text.strip():
                continue
            units.append(
                _TextUnit(block_id=len(units), kind="pdf", node=raw, field="", text=text)
            )
        return units


    def _apply_pdf(
        self,
        src: Path,
        replacements: dict[int, str],
        dst: Path,
    ) -> tuple[int, list[dict]]:
        """Применить замены к PDF, выбрав движок из :data:`PDF_ENGINE_ORDER`.

        Args:
            src: Исходный PDF.
            replacements: Нормализованные замены.
            dst: Файл результата.

        Returns:
            tuple[int, list[dict]]: (число изменённых блоков, ошибки по блокам).

        Raises:
            Exception: ошибки чтения/записи пробрасываются в :meth:`apply_replacements`.
        """
        units, backend = self._pdf_units(src)

        if backend == "pdf_edit_engine":
            return self._apply_pdf_engine(units, replacements, src, dst)

        # TODO(этап 1, fallback): polydoc перерисовывает PDF (ReportLab) и теряет
        # исходную вёрстку. Здесь остаётся место для более точного fallback-а:
        # сопоставить блоки модели polydoc с bbox-ами pdf_edit_engine и заменить
        # текст по координатам, не пересобирая документ.
        self._log(
            "warning",
            "PDF обрабатывается через polydoc: документ перерисовывается (ReportLab), "
            "исходная вёрстка не сохраняется",
        )
        return self._apply_pdf_polydoc(replacements, src, dst)

    def _apply_pdf_engine(
        self,
        units: Sequence[_TextUnit],
        replacements: dict[int, str],
        src: Path,
        dst: Path,
    ) -> tuple[int, list[dict]]:
        """Заменить текст PDF по bbox строк, сохраняя шрифт и вёрстку.

        Правки применяются снизу вверх: ``replace_block`` может сдвигать контент
        ниже изменённого блока, а координаты ещё не обработанных блоков выше
        остаются валидными.

        Args:
            units: Блоки из ``extract_text`` (``TextBlock`` с bbox и шрифтом).
            replacements: Нормализованные замены.
            src: Исходный PDF.
            dst: Файл результата.

        Returns:
            tuple[int, list[dict]]: (число изменённых блоков, ошибки по блокам).
            Блок, который движок не смог закодировать (например, кириллица в
            base-14 шрифте), попадает в ``errors``, документ при этом не портится.

        Raises:
            DocumentProcessingError: ``pdf_edit_engine`` недоступен.
            Exception: ошибки записи файла пробрасываются наверх.
        """
        engine = _load_pdf_engine()
        if engine is None:
            raise DocumentProcessingError("pdf_edit_engine недоступен для записи PDF")

        counter = None if self._pdf_engine_noise else _suppress_pdf_engine_noise()
        try:
            applied, errors = self._run_pdf_engine(
                engine, units, replacements, src, dst
            )
        finally:
            if counter is not None:
                _restore_pdf_engine_logging(counter)

        if counter is not None and counter.count:
            self._log(
                "warning",
                f"PDF: {counter.count} внутренних предупреждений "
                "pdf_edit_engine подавлено (используйте --verbose для деталей)",
            )
        return applied, errors

    def _run_pdf_engine(
        self,
        engine: Any,
        units: Sequence[_TextUnit],
        replacements: dict[int, str],
        src: Path,
        dst: Path,
    ) -> tuple[int, list[dict]]:
        """Один проход правок PDF через ``pdf_edit_engine``.

        Args:
            engine: Модуль ``pdf_edit_engine``.
            units: Блоки из ``extract_text``.
            replacements: Нормализованные замены.
            src: Исходный PDF.
            dst: Файл результата.

        Returns:
            tuple[int, list[dict]]: (число изменённых блоков, ошибки по блокам).

        Raises:
            Exception: ошибки чтения/записи пробрасываются наверх.
        """
        by_id = {unit.block_id: unit for unit in units}
        applied = 0
        errors: list[dict] = []
        pending = dict(replacements)

        with tempfile.TemporaryDirectory(prefix="termru-pdf-") as tmp_dir:
            prepared, resources, font_errors, missing = self._prepare_pdf_fonts(
                src, by_id, pending, Path(tmp_dir)
            )

            if missing and self._pdf_fallback == "polydoc":
                self._log(
                    "warning",
                    "Кириллический шрифт не найден: pdf_fallback=polydoc, "
                    "документ пересобирается (вёрстка теряется)",
                )
                fallback_font = self.font_resolver.find_any_cyrillic()
                return self._apply_pdf_polydoc(
                    replacements, src, dst, font=fallback_font
                )

            errors.extend(font_errors)
            for block_id in missing:
                pending.pop(block_id, None)

            current = prepared
            for step, block_id in enumerate(sorted(pending, reverse=True)):
                new_text = pending[block_id]
                unit = by_id.get(block_id)
                if unit is None:
                    message = f"Блок {block_id} не найден (доступно блоков: {len(units)})"
                    self._log("error", message)
                    errors.append({"block_id": block_id, "message": message})
                    continue
                if unit.text == new_text:
                    self._log("debug", f"Блок {block_id}: текст не изменился, пропущено")
                    continue

                block = unit.node
                bbox = (
                    float(block.x),
                    float(block.y),
                    float(block.x + block.width),
                    float(block.y + block.height),
                )
                target = Path(tmp_dir) / f"step{step}.pdf"
                edit = self._replace_pdf_block(
                    engine,
                    current,
                    block,
                    bbox,
                    new_text,
                    target,
                    block_id,
                    errors,
                    font_name=resources.get(block_id),
                )
                if edit is None:
                    continue
                current = target
                applied += 1

            if resources:
                self._finish_pdf_with_repair(current, dst, by_id, pending)
            else:
                shutil.copy2(current, dst)

        return applied, errors


    def _prepare_pdf_fonts(
        self,
        src: Path,
        by_id: Mapping[int, _TextUnit],
        replacements: dict[int, str],
        tmp_dir: Path,
    ) -> tuple[Path, dict[int, str], list[dict], list[int]]:
        """Внедрить кириллические шрифты и вернуть план подмены по блокам.

        Подмена нужна только там, где новый текст содержит кириллицу, а блок
        набран base-14 шрифтом (Helvetica/Times/Courier/...): такой шрифт —
        Type1 без ``/FontDescriptor``, его нельзя ни расширить, ни
        перекодировать. Кастомные шрифты не трогаем.

        Args:
            src: Исходный PDF.
            by_id: Блоки по ``block_id``.
            replacements: Нормализованные замены.
            tmp_dir: Каталог для промежуточного PDF.

        Returns:
            tuple: ``(pdf_для_дальнейших_правок, {block_id: имя_ресурса},
            ошибки, block_id_без_шрифта)``. Если подмен нет, первым элементом
            возвращается ``src``.

        Raises:
            DocumentProcessingError: PDF не открывается.
        """
        resources: dict[int, str] = {}
        errors: list[dict] = []
        missing: list[int] = []
        substitutes: dict[int, Path] = {}

        try:
            pdf = pikepdf.open(str(src))
        except Exception as exc:  # noqa: BLE001 - разные типы ошибок pikepdf
            raise DocumentProcessingError(f"Не удалось открыть PDF {src}: {exc}") from exc

        with pdf:
            for block_id, new_text in replacements.items():
                unit = by_id.get(block_id)
                if unit is None or not text_has_cyrillic(new_text):
                    continue
                base_font = self._page_base_font(pdf, unit.node)
                if not is_base14_font(base_font):
                    self._log(
                        "debug",
                        f"Блок {block_id}: шрифт {base_font!r} не base-14 — "
                        "подмена не требуется",
                    )
                    continue
                ttf = self.font_resolver.find_for_block(unit.node, base_font)
                if ttf is None:
                    self._log("error", f"Блок {block_id}: {MISSING_FONT_MESSAGE}")
                    errors.append({"block_id": block_id, "message": MISSING_FONT_MESSAGE})
                    missing.append(block_id)
                    continue
                substitutes[block_id] = ttf
                self._log(
                    "info",
                    f"Блок {block_id}: {base_font} -> {ttf.name} (кириллица)",
                )

            if not substitutes:
                return src, resources, errors, missing

            groups: dict[tuple[int, str], set[str]] = {}
            for block_id, ttf in substitutes.items():
                page_no = int(by_id[block_id].node.page)
                groups.setdefault((page_no, str(ttf)), set()).update(replacements[block_id])

            names: dict[tuple[int, str], str] = {}
            for index, ((page_no, ttf_path), chars) in enumerate(sorted(groups.items())):
                name = f"TFCyr{index}"
                add_cyrillic_font(
                    pdf,
                    pdf.pages[page_no],
                    Path(ttf_path),
                    "".join(sorted(chars)),
                    name,
                )
                names[(page_no, ttf_path)] = name
                self._log(
                    "debug",
                    f"стр. {page_no}: {Path(ttf_path).name} внедрён как /{name} "
                    f"({len(chars)} символов)",
                )

            for block_id, ttf in substitutes.items():
                page_no = int(by_id[block_id].node.page)
                resources[block_id] = names[(page_no, str(ttf))]

            prepared = tmp_dir / "prepared.pdf"
            pdf.save(str(prepared))

        return prepared, resources, errors, missing

    @staticmethod
    def _page_base_font(pdf: pikepdf.Pdf, block: Any) -> str | None:
        """Прочитать ``/BaseFont`` шрифта, которым набран блок.

        Args:
            pdf: Открытый документ.
            block: ``TextBlock`` (``page`` + ``font_name`` — имя ресурса).

        Returns:
            str | None: ``/BaseFont`` (например ``/Helvetica``) либо ``None``,
            если ресурс не найден.

        Raises:
            Ничего не бросает.
        """
        try:
            page = pdf.pages[int(block.page)]
            fonts = page["/Resources"]["/Font"]
            resource = str(block.font_name).lstrip("/")
            font_dict = fonts.get(pikepdf.Name(f"/{resource}"))
            if font_dict is None:
                return None
            base = font_dict.get("/BaseFont")
            return str(base) if base is not None else None
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            return None

    def _replace_pdf_block(
        self,
        engine: Any,
        current: Path,
        block: Any,
        bbox: tuple[float, float, float, float],
        new_text: str,
        target: Path,
        block_id: int,
        errors: list[dict],
        font_name: str | None = None,
    ) -> Any | None:
        """Выполнить одну правку PDF и записать ошибку в общий список.

        Args:
            engine: Модуль ``pdf_edit_engine``.
            current: PDF, к которому применяется правка.
            block: ``TextBlock`` блока (используется ``page``).
            bbox: Координаты ``(x0, y0, x1, y1)``.
            new_text: Новый текст блока.
            target: Путь для результата шага.
            block_id: Идентификатор блока (для сообщения об ошибке).
            errors: Список ошибок, который дополняется при неудаче.
            font_name: Имя ресурса внедрённого Type0-шрифта (``TFCyr0``);
                ``None`` — писать шрифтом самого документа.

        Returns:
            Any | None: ``EditResult`` при успехе, иначе ``None``.

        Raises:
            Ничего не бросает: исключения движка логируются и попадают в ``errors``.
        """
        try:
            edit = engine.replace_block(
                str(current),
                int(block.page),
                bbox,
                new_text,
                str(target),
                font_name=font_name,
            )
        except Exception as exc:  # noqa: BLE001 - один блок не ломает остальные
            message = f"Не удалось заменить текст блока {block_id}: {exc}"
            self._log("error", message)
            errors.append({"block_id": block_id, "message": message})
            return None

        if not getattr(edit, "success", False):
            message = (
                f"Движок не смог заменить текст блока {block_id}: "
                f"{self._describe_pdf_edit(edit)}"
            )
            self._log("error", message)
            errors.append({"block_id": block_id, "message": message})
            return None

        return edit

    @staticmethod
    def _describe_pdf_edit(edit: Any) -> str:
        """Кратко описать причину неуспешной правки PDF.

        Args:
            edit: ``pdf_edit_engine.models.EditResult``.

        Returns:
            str: Причины из ``warnings`` и ``fidelity_report.glyphs_missing``;
            при отсутствии деталей — ``"причина не сообщена"``.

        Raises:
            Ничего не бросает.
        """
        reasons = [str(item) for item in (getattr(edit, "warnings", None) or [])]
        report = getattr(edit, "fidelity_report", None)
        missing = getattr(report, "glyphs_missing", None) if report is not None else None
        if missing:
            unique = dict.fromkeys(str(char) for char in missing)
            reasons.append("в шрифте нет глифов: " + " ".join(unique))
        return "; ".join(reasons) if reasons else "причина не сообщена"

    def _finish_pdf_with_repair(
        self,
        current: Path,
        dst: Path,
        by_id: Mapping[int, _TextUnit],
        replacements: dict[int, str],
    ) -> None:
        """Сохранить результат и вернуть ``Tf`` в ``BT``-блоки без шрифта.

        Появляется из-за подмены шрифта: ``replace_block`` ставит ``Tf`` нашим
        CID-шрифтом, а следующий ``BT`` без своего ``Tf`` наследует текстовое
        состояние и читает 1-байтовые коды как половины 2-байтовых.

        Args:
            current: PDF после всех правок.
            dst: Итоговый файл.
            by_id: Блоки по ``block_id``.
            replacements: Применённые замены (для выбора исходного шрифта).

        Returns:
            None

        Raises:
            DocumentProcessingError: файл не открывается или не сохраняется.
        """
        repaired_total = 0
        try:
            pdf = pikepdf.open(str(current))
        except Exception as exc:  # noqa: BLE001 - разные типы ошибок pikepdf
            raise DocumentProcessingError(f"Не удалось открыть PDF {current}: {exc}") from exc

        with pdf:
            for index, page in enumerate(pdf.pages):
                fallback = self._fallback_tf(by_id, replacements, index)
                if fallback is None:
                    continue
                contents = page.get("/Contents")
                if contents is None:
                    continue
                if isinstance(contents, pikepdf.Array):
                    raw = b"".join(stream.read_bytes() for stream in contents)
                else:
                    raw = contents.read_bytes()
                patched, count = repair_inherited_font(raw.decode("latin-1"), fallback)
                if count:
                    page["/Contents"] = pdf.make_stream(patched.encode("latin-1"))
                    repaired_total += count

            if repaired_total:
                self._log("debug", f"ремонт Tf: исправлено BT-блоков — {repaired_total}")
            pdf.save(str(dst))

    @staticmethod
    def _fallback_tf(
        by_id: Mapping[int, _TextUnit],
        replacements: dict[int, str],
        page_index: int,
    ) -> str | None:
        """Оператор ``Tf`` исходного шрифта страницы (самый частый из заменённых).

        Args:
            by_id: Блоки по ``block_id``.
            replacements: Применённые замены.
            page_index: Номер страницы (0-based).

        Returns:
            str | None: например ``"/F1 12 Tf"`` либо ``None``, если на
            странице не было замен.

        Raises:
            Ничего не бросает.
        """
        counts: Counter[tuple[str, float]] = Counter()
        for block_id, unit in by_id.items():
            if block_id not in replacements:
                continue
            node = unit.node
            if int(node.page) != page_index:
                continue
            counts[(str(node.font_name).lstrip("/"), float(node.font_size))] += 1
        if not counts:
            return None
        (name, size), _ = counts.most_common(1)[0]
        return f"/{name} {size:g} Tf"

    def _apply_pdf_polydoc(
        self,
        replacements: dict[int, str],
        src: Path,
        dst: Path,
        font: Path | None = None,
    ) -> tuple[int, list[dict]]:
        """Fallback: пересобрать PDF из модели polydoc.

        Вёрстка и шрифты исходного PDF не сохраняются — это аварийный путь.
        Чтобы кириллица вообще записалась, TTF регистрируется в ReportLab и
        передаётся в writer как ``font=`` (по умолчанию там Helvetica, которая
        кириллицу не умеет и печатает «IIIII»).

        Args:
            replacements: Нормализованные замены.
            src: Исходный PDF.
            dst: Файл результата.
            font: TTF с кириллицей для ReportLab; ``None`` — встроенный шрифт.

        Returns:
            tuple[int, list[dict]]: (число изменённых блоков, ошибки).

        Raises:
            Exception: ошибки чтения/записи polydoc пробрасываются наверх.
        """
        document = polydoc.open(str(src))
        applied, errors = self._apply_units(list(self._iter_units(document)), replacements)

        options: dict[str, Any] = {}
        if font is not None:
            registered = self._register_polydoc_font(font)
            if registered:
                options["font"] = registered
        document.save(str(dst), format="pdf", **options)

        if any(text_has_cyrillic(text) for text in replacements.values()):
            if not self._output_has_cyrillic(dst):
                message = (
                    "polydoc пересобрал PDF, но кириллица не записалась: нужен TTF "
                    "с поддержкой русского (fonts-dejavu) или --pdf-font-dir"
                )
                self._log("error", message)
                errors.append({"block_id": None, "message": message})
        return applied, errors

    def _register_polydoc_font(self, ttf: Path) -> str | None:
        """Зарегистрировать TTF в ReportLab под общим именем семейства.

        Args:
            ttf: Путь к шрифту с кириллицей.

        Returns:
            str | None: имя семейства для опции ``font=`` либо ``None``, если
            ReportLab недоступен или шрифт не зарегистрировался.

        Raises:
            Ничего не бросает.
        """
        try:
            from reportlab.pdfbase import pdfmetrics  # noqa: PLC0415 - ленивый импорт
            from reportlab.pdfbase.ttfonts import TTFont as ReportLabTTFont
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            self._log("warning", f"reportlab недоступен для PDF-fallback: {exc}")
            return None

        name = "TermRuCyr"
        try:
            pdfmetrics.registerFont(ReportLabTTFont(name, str(ttf)))
            pdfmetrics.registerFontFamily(
                name, normal=name, bold=name, italic=name, boldItalic=name
            )
        except Exception as exc:  # noqa: BLE001 - шрифт может быть битым
            self._log("warning", f"Не удалось зарегистрировать {ttf.name}: {exc}")
            return None
        self._log("debug", f"polydoc-фолбэк: ReportLab использует {ttf.name}")
        return name

    @staticmethod
    def _output_has_cyrillic(path: Path) -> bool:
        """Проверить, есть ли кириллица в тексте готового PDF.

        Args:
            path: Путь к PDF.

        Returns:
            bool: ``True``, если текст содержит кириллицу или проверить нечем.

        Raises:
            Ничего не бросает.
        """
        engine = _load_pdf_engine()
        if engine is None:  # pragma: no cover - движок есть всегда
            return True
        try:
            return text_has_cyrillic(engine.get_text(str(path)))
        except Exception:  # noqa: BLE001 - проверка не должна ломать запись
            return True









