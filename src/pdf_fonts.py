"""Подбор и внедрение кириллических TTF в PDF.

Основание — :file:`docs/pdf-font-research.md`: ``pdf_edit_engine`` 0.2.0
расширяет **только** Type0/Identity-H шрифты, а base-14 (Helvetica, Times,
Courier, Symbol, ZapfDingbats) — это Type1 без ``/FontDescriptor`` и
``/FontFile*``. Их нельзя ни расширить через ``extend_subset``, ни
перекодировать, поэтому единственный рабочий путь — собрать собственный
Type0/Identity-H subset из TTF и передать имя ресурса в
``replace_block(font_name=...)``.

Модуль даёт три вещи:

* :class:`PdfFontResolver` — какой TTF взять под стиль блока (regular/bold/
  italic/bold-italic) и есть ли в нём кириллица;
* :func:`add_cyrillic_font` — внедрение subset'а в страницу как Type0/Identity-H;
* :func:`repair_inherited_font` — восстановление ``Tf`` в ``BT``-блоках, которые
  наследовали текстовое состояние (иначе следующий текст рисуется нашим
  CID-шрифтом и превращается в мусор).
"""

from __future__ import annotations

import io
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pikepdf

__all__ = [
    "CYRILLIC_RE",
    "DEFAULT_FONT_DIR",
    "FONT_SUBSTITUTION_MODES",
    "MISSING_FONT_MESSAGE",
    "PdfFontResolver",
    "PdfFontStrategy",
    "add_cyrillic_font",
    "build_type0_font",
    "is_base14_font",
    "is_encode_error",
    "repair_inherited_font",
    "text_has_cyrillic",
]

LOGGER = logging.getLogger(__name__)

#: Шрифты, поставляемые вместе с приложением (попадают в бандл через add_data).
#: В собранном приложении ``__file__`` указывает внутрь ``sys._MEIPASS``, поэтому
#: путь автоматически ведёт на распакованные ``data/fonts``.
DEFAULT_FONT_DIR = Path(__file__).resolve().parent.parent / "data" / "fonts"

#: Кириллица в тексте (включая Ё/ё).
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

#: Сообщение для ``errors``, когда шрифт с кириллицей не нашёлся.
MISSING_FONT_MESSAGE = (
    "Кириллический шрифт не найден в системе; установите fonts-dejavu "
    "или используйте --pdf-fallback=polydoc"
)

#: Буквы, которые обязаны быть в шрифте, чтобы мы им доверили замену.
_CYRILLIC_ALPHABET = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
)

_BASE14_PREFIXES = ("Helvetica", "Times", "Courier", "Symbol", "ZapfDingbats")

#: Производители PDF часто кладут эти семейства как алиасы base-14.
_BASE14_ALIASES = ("Arial", "TimesNewRoman", "CourierNew", "HelveticaNeue")

#: Признаки начертания в имени файла/шрифта.
_BOLD_TOKEN_RE = re.compile(r"bold|black|heavy", re.IGNORECASE)
_ITALIC_TOKEN_RE = re.compile(r"italic|oblique", re.IGNORECASE)


def text_has_cyrillic(text: str) -> bool:
    """Проверить, есть ли в тексте кириллица.

    Args:
        text: Проверяемый текст.

    Returns:
        bool: ``True``, если найден хотя бы один символ ``[А-Яа-яЁё]``.

    Raises:
        Ничего не бросает: не-строка считается текстом без кириллицы.
    """
    if not isinstance(text, str):
        return False
    return bool(CYRILLIC_RE.search(text))


def is_base14_font(name: str | None) -> bool:
    """Определить, является ли шрифт base-14 (нерасширяемым Type1).

    Args:
        name: ``/BaseFont`` из ресурсов страницы либо имя ресурса.

    Returns:
        bool: ``True`` для Helvetica/Times/Courier/Symbol/ZapfDingbats и их
        начертаний (``Helvetica-Bold``, ``Times,BoldItalic``) и для алиасов
        вроде ``Arial``/``TimesNewRoman``. Префикс subset'а (``ABCDEF+``)
        отбрасывается, поэтому встроенный Helvetica тоже распознаётся.

    Raises:
        Ничего не бросает.
    """
    if not name:
        return False
    clean = str(name).lstrip("/").replace(" ", "")
    if "+" in clean:  # "AAAAAA+DejaVuSans" -> "DejaVuSans"
        clean = clean.split("+", 1)[1]
    base = re.split(r"[,\-]", clean)[0]
    lowered = base.lower()
    for prefix in _BASE14_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return True
    for alias in _BASE14_ALIASES:
        if lowered.startswith(alias.lower()):
            return True
    return False


#: Допустимые режимы подмены шрифта в PDF (``--pdf-substitute-fonts``).
#:
#: * ``strict`` — подменяем только base-14 (поведение по умолчанию);
#: * ``base14`` — strict + подмена, если движок не смог закодировать текст
#:   не-base-14 шрифтом;
#: * ``always`` — подмена при любой ошибке кодирования.
FONT_SUBSTITUTION_MODES = ("strict", "base14", "always")


def is_encode_error(message: str | None) -> bool:
    """Похоже ли сообщение движка на «шрифт не может закодировать текст».

    Движок сообщает об этом по-разному: ``"Font cannot encode text and no
    extensible (Type0/Identity-H) font available in bbox"`` для не-CID шрифтов,
    ``"Font extension failed for body font 'X' (missing: [...])"`` для сабсетов
    Type0, а в наш ``errors`` попадает ещё и русская строка «в шрифте нет глифов».

    Args:
        message: Текст ошибки/предупреждения движка либо ``None``.

    Returns:
        bool: ``True``, если это ошибка кодирования.

    Raises:
        Ничего не бросает.
    """
    if not message:
        return False
    lowered = str(message).lower()
    markers = (
        "cannot encode",
        "extension failed",
        "no extensible",
        "glyphs_missing",
        "нет глифов",
    )
    return any(marker in lowered for marker in markers)


class PdfFontStrategy:
    """Решает, можно ли подменить шрифт блока нашим CID-шрифтом.

    Режимы (``mode``):

    * ``strict`` — подмена только для base-14 (как было до появления режимов:
      такой шрифт — Type1 без ``/FontDescriptor``, расширить его нельзя);
    * ``base14`` — strict + подмена не-base-14 шрифта, если движок ответил
      ошибкой кодирования (LibreOffice/Liberation и подобное);
    * ``always`` — подмена при любой ошибке кодирования, независимо от типа
      шрифта (LaTeX-сабсеты Type0 без кириллических глифов).

    Args:
        mode: Один из :data:`FONT_SUBSTITUTION_MODES`.
        resolver: Резолвер кириллических TTF (для проверки, что шрифт есть).

    Raises:
        ValueError: неизвестный режим.
    """

    def __init__(self, mode: str, resolver: PdfFontResolver) -> None:
        """Создать стратегию подмены.

        Args:
            mode: ``"strict"``, ``"base14"`` или ``"always"``.
            resolver: Резолвер шрифтов с кириллицей.

        Returns:
            None

        Raises:
            ValueError: если ``mode`` неизвестен.
        """
        if mode not in FONT_SUBSTITUTION_MODES:
            raise ValueError(
                f"mode должен быть одним из {FONT_SUBSTITUTION_MODES}, получено {mode!r}"
            )
        self.mode = mode
        self.resolver = resolver

    def is_base14(self, block: Any, base_font: str | None = None) -> bool:
        """Является ли шрифт блока base-14-совместимым.

        Args:
            block: ``TextBlock`` (используется ``font_name`` как фолбэк).
            base_font: ``/BaseFont`` из ресурсов страницы, если уже известен.

        Returns:
            bool: ``True`` для Helvetica/Times/Courier/их алиасов.

        Raises:
            Ничего не бросает.
        """
        candidate = base_font if base_font is not None else getattr(block, "font_name", None)
        return is_base14_font(candidate)

    def should_substitute(
        self,
        block: Any,
        new_text: str,
        edit_result_or_none: Any | None,
        *,
        base_font: str | None = None,
        font_subtype: str | None = None,
    ) -> tuple[bool, str]:
        """Нужно ли подменить шрифт блока и почему.

        Вызывается дважды за блок: до правки (``edit_result_or_none is None`` —
        preflight: подмена нужна, если шрифт base-14) и после неудачной правки
        (по сообщению движка).

        ``base14`` подменяет только «простые» шрифты (``/Type1``, ``/TrueType``):
        их движок не умеет расширять вообще. CID-сабсет (``/Type0``) движок
        хотя бы пытается расширить — для него нужен ``always``.

        Args:
            block: ``TextBlock`` блока.
            new_text: Новый текст (для сообщения о причине).
            edit_result_or_none: ``EditResult`` неудачной правки либо ``None``
                для preflight.
            base_font: ``/BaseFont`` шрифта блока (если известен точнее имени
                ресурса).
            font_subtype: ``/Subtype`` шрифта (``/Type0``, ``/Type1``,
                ``/TrueType``), если известен.

        Returns:
            tuple[bool, str]: (нужно ли подменять, причина человеческим языком).

        Raises:
            Ничего не бросает.
        """
        base14 = self.is_base14(block, base_font)
        if edit_result_or_none is None:
            if base14:
                return True, f"шрифт {base_font or block.font_name} base-14 — нужен CID-шрифт"
            return False, f"шрифт {base_font or block.font_name} не base-14 — подмена не требуется"

        message = getattr(edit_result_or_none, "warnings", None) or []
        detail = " ".join(str(item) for item in message)
        missing = getattr(
            getattr(edit_result_or_none, "fidelity_report", None), "glyphs_missing", None
        )
        if missing:
            detail += " " + " ".join(str(char) for char in missing)
        if not is_encode_error(detail):
            return False, "движок не сообщил об ошибке кодирования"

        font_label = base_font or block.font_name
        if self.mode == "strict":
            return False, f"strict: подмена {font_label} запрещена"
        if self.mode == "base14":
            if base14:
                return False, f"base14: шрифт {font_label} уже base-14, подмена не помогла"
            if font_subtype == "/Type0":
                return False, (
                    f"base14: {font_label} — CID-сабсет, расширяется движком; "
                    "для подмены нужен режим always"
                )
            return True, f"base14: шрифт {font_label} не может закодировать текст"
        return True, f"always: подмена {font_label} при ошибке кодирования"



def _style_from_name(name: str | None) -> str:
    """Вывести начертание из имени шрифта.

    Args:
        name: ``/BaseFont`` или имя ресурса (``Helvetica-BoldOblique``).

    Returns:
        str: ``"regular"``, ``"bold"``, ``"italic"`` или ``"bold-italic"``.

    Raises:
        Ничего не бросает.
    """
    lowered = name or ""
    bold = bool(_BOLD_TOKEN_RE.search(lowered))
    italic = bool(_ITALIC_TOKEN_RE.search(lowered))
    if bold and italic:
        return "bold-italic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "regular"


def _matches_style(filename: str, style: str) -> bool:
    """Проверить, соответствует ли имя файла начертанию.

    Нужен для запасного поиска по ``*.ttf`` в bundled-папке: иначе bold-блоку
    достался бы любой лежащий рядом шрифт (например, regular).

    Args:
        filename: Имя файла, например ``DejaVuSans-BoldOblique.ttf``.
        style: ``"regular"``, ``"bold"``, ``"italic"`` или ``"bold-italic"``.

    Returns:
        bool: ``True``, если начертание в имени совпадает с запрошенным.

    Raises:
        Ничего не бросает.
    """
    bold = bool(_BOLD_TOKEN_RE.search(filename))
    italic = bool(_ITALIC_TOKEN_RE.search(filename))
    if style == "bold":
        return bold and not italic
    if style == "italic":
        return italic and not bold
    if style == "bold-italic":
        return bold and italic
    return not bold and not italic


class PdfFontResolver:
    """Подбирает TTF с кириллицей под начертание исходного шрифта блока.

    Порядок поиска: ``bundled_dir`` (портативная сборка / ``--pdf-font-dir``),
    затем системные пути из :attr:`CANDIDATES`. Отсутствующие файлы
    пропускаются, поэтому один и тот же список работает в Linux, Windows и
    macOS.

    Args:
        bundled_dir: Дополнительная папка с TTF; проверяется первой.
        log_callback: Необязательный ``callable(level: str, msg: str)``.
    """

    #: Кандидаты по начертаниям: ``(стиль, [bundled-имя, *системные пути])``.
    #: Первый элемент — имя файла в :data:`DEFAULT_FONT_DIR` (или в каталоге,
    #: переданном в ``bundled_dir``); остальные — абсолютные системные пути.
    CANDIDATES: list[tuple[str, list[str]]] = [
        (
            "regular",
            [
                "DejaVuSans.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/termru/fonts/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
            ],
        ),
        (
            "bold",
            [
                "DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/termru/fonts/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            ],
        ),
        (
            "italic",
            [
                "DejaVuSans-Oblique.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
                "/usr/share/termru/fonts/DejaVuSans-Oblique.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansOblique.ttf",
                "C:/Windows/Fonts/ariali.ttf",
                "C:/Windows/Fonts/segoeuii.ttf",
                "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
            ],
        ),
        (
            "bold-italic",
            [
                "DejaVuSans-BoldOblique.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
                "/usr/share/termru/fonts/DejaVuSans-BoldOblique.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
                "/usr/share/fonts/truetype/noto/NotoSans-BoldItalic.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBoldOblique.ttf",
                "C:/Windows/Fonts/arialbi.ttf",
                "C:/Windows/Fonts/segoeuiz.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
            ],
        ),
    ]

    def __init__(
        self,
        bundled_dir: Path | None = None,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """Создать резолвер шрифтов.

        Args:
            bundled_dir: Папка со шрифтами, которая проверяется первой. По
                умолчанию — :data:`DEFAULT_FONT_DIR` (``data/fonts`` проекта,
                в собранном приложении — внутри бандла). Чтобы отключить
                бандл, передайте несуществующий путь.
            log_callback: Необязательный обработчик сообщений.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._bundled_dir = (
            Path(bundled_dir) if bundled_dir is not None else DEFAULT_FONT_DIR
        )
        self._log_callback = log_callback
        self._support_cache: dict[str, bool] = {}

    def find_for_block(self, block: Any, base_font: str | None = None) -> Path | None:
        """Найти TTF с кириллицей под начертание блока.

        Args:
            block: ``TextBlock`` из ``pdf_edit_engine`` (берётся ``font_name``,
                если ``base_font`` не задан).
            base_font: Реальный ``/BaseFont`` из ресурсов страницы. Нужен
                потому, что ``TextBlock.font_name`` — это имя ресурса
                (``F1``), а не гарнитура.

        Returns:
            Path | None: первый существующий TTF с полным набором кириллицы
            для нужного начертания либо ``None``, если такого нет.

        Raises:
            Ничего не бросает.
        """
        name = base_font or getattr(block, "font_name", None)
        style = _style_from_name(name)
        path = self._first_supported(style)
        if path is None:
            self._log(
                "warning",
                f"Нет TTF с кириллицей для начертания {style!r} "
                f"(шрифт блока: {name!r})",
            )
        else:
            self._log(
                "debug",
                f"{style}: выбран шрифт {path.name} ({self._source_of(path)})",
            )
        return path


    def supports_cyrillic(self, ttf_path: Path) -> bool:
        """Проверить, что в TTF есть все буквы русского алфавита.

        Args:
            ttf_path: Путь к TTF.

        Returns:
            bool: ``True``, если ``fontTools`` подтвердил покрытие. Если
            ``fontTools`` не установлен — предупреждение в лог и ``True``
            (доверяем имени файла, как требует контракт модуля).

        Raises:
            Ничего не бросает.
        """
        key = str(ttf_path)
        if key not in self._support_cache:
            self._support_cache[key] = self._probe_cyrillic(Path(ttf_path))
        return self._support_cache[key]

    def _probe_cyrillic(self, path: Path) -> bool:
        """Прочитать cmap шрифта и сверить русский алфавит.

        Args:
            path: Путь к TTF.

        Returns:
            bool: ``True`` при полном покрытии кириллицы.

        Raises:
            Ничего не бросает: ошибки чтения превращаются в ``False``.
        """
        try:
            from fontTools.ttLib import TTFont  # noqa: PLC0415 - ленивый импорт
        except ImportError:  # pragma: no cover - зависит от окружения
            self._log(
                "warning",
                "fontTools недоступен: покрытие кириллицей не проверяется, "
                "доверяем имени файла",
            )
            return True

        try:
            font = TTFont(str(path), lazy=True)
        except Exception as exc:  # noqa: BLE001 - битый файл не ломает прогон
            self._log("warning", f"Не удалось прочитать шрифт {path}: {exc}")
            return False
        try:
            cmap = font.getBestCmap() or {}
        finally:
            font.close()

        missing = [char for char in _CYRILLIC_ALPHABET if ord(char) not in cmap]
        if missing:
            self._log("debug", f"{path.name}: нет глифов {''.join(missing)}")
            return False
        return True

    def find_any_cyrillic(self) -> Path | None:
        """Первый доступный TTF с кириллицей независимо от начертания.

        Нужен для fallback-пути через polydoc: стиль там всё равно теряется,
        важно лишь, чтобы шрифт умел кириллицу.

        Returns:
            Path | None: путь к шрифту либо ``None``, если ничего не найдено.

        Raises:
            Ничего не бросает.
        """
        for style, _paths in self.CANDIDATES:
            path = self._first_supported(style)
            if path is not None:
                return path
        self._log("warning", "Ни одного TTF с кириллицей не найдено")
        return None

    def _first_supported(self, style: str) -> Path | None:
        """Первый существующий и покрывающий кириллицу TTF для начертания.

        Args:
            style: ``"regular"``, ``"bold"``, ``"italic"`` или ``"bold-italic"``.

        Returns:
            Path | None: путь к шрифту либо ``None``.

        Raises:
            Ничего не бросает.
        """
        for candidate in self._search_order(style):
            if not candidate.is_file():
                continue
            if self.supports_cyrillic(candidate):
                self._log("debug", f"{candidate.name} ({self._source_of(candidate)})")
                return candidate
            self._log("debug", f"{candidate}: кириллица неполная — пропущен")
        return None

    def _source_of(self, path: Path) -> str:
        """Определить, откуда взят шрифт: из бандла или из системы.

        Args:
            path: Найденный путь к TTF.

        Returns:
            str: ``"bundled"`` или ``"system"``.

        Raises:
            Ничего не бросает.
        """
        try:
            path.resolve().relative_to(self._bundled_dir.resolve())
        except (ValueError, OSError):
            return "system"
        return "bundled"

    def _search_order(self, style: str) -> list[Path]:
        """Построить порядок поиска: bundled-папка, затем системные пути.

        Первый элемент списка кандидатов — имя файла в ``bundled_dir``; дальше
        идут абсолютные системные пути. Это даёт одинаковый результат на всех
        платформах: если шрифт есть в дистрибутиве, он используется всегда.

        Args:
            style: Начертание.

        Returns:
            list[Path]: кандидаты в порядке проверки.

        Raises:
            Ничего не бросает.
        """
        raw = self._candidates_for(style)
        if raw is None:
            # Начертание не заявлено в CANDIDATES — намеренно ничего не ищем
            # (иначе bold-блоку достался бы regular из bundled-папки).
            return []

        ordered: list[Path] = []

        bundled_name = raw[0] if raw else ""
        if bundled_name:
            ordered.append(self._bundled_dir / bundled_name)
        if self._bundled_dir.is_dir():
            # Запасной вариант: TTF того же начертания в папке
            # (--pdf-font-dir с произвольными именами файлов).
            ordered.extend(
                sorted(
                    path
                    for path in self._bundled_dir.glob("*.ttf")
                    if _matches_style(path.name, style)
                )
            )

        ordered.extend(Path(entry) for entry in raw[1:])
        return ordered

    def _candidates_for(self, style: str) -> list[str] | None:
        """Пути-кандидаты для начертания (читается из :attr:`CANDIDATES`).

        Args:
            style: Начертание.

        Returns:
            list[str] | None: список путей или ``None``, если начертание вообще
            не описано в :attr:`CANDIDATES`.

        Raises:
            Ничего не бросает.
        """
        for candidate_style, paths in self.CANDIDATES:
            if candidate_style == style:
                return list(paths)
        return None

    def _log(self, level: str, message: str) -> None:
        """Записать сообщение в ``logging`` и в ``log_callback``.

        Args:
            level: ``"debug"``, ``"info"``, ``"warning"`` или ``"error"``.
            message: Текст сообщения.

        Returns:
            None

        Raises:
            Ничего не бросает: сбой колбэка логируется и гасится.
        """
        getattr(LOGGER, level, LOGGER.info)("%s", message)
        if self._log_callback is None:
            return
        try:
            self._log_callback(level, message)
        except Exception as exc:  # noqa: BLE001 - колбэк внешний
            LOGGER.warning("log_callback завершился ошибкой: %s", exc)


def build_type0_font(
    pdf: pikepdf.Pdf,
    ttf_path: Path,
    chars: str,
    base_font_name: str = "TermRuCyr",
) -> pikepdf.Object:
    """Собрать Type0/Identity-H шрифт с subset'ом TTF под нужные символы.

    Результат — готовый объект для ``/Resources/Font``. Именно такой шрифт
    ``pdf_edit_engine`` соглашается использовать: ``can_encode`` строится по
    ``/ToUnicode``, а ширина берётся из ``/W``.

    Args:
        pdf: Открытый документ (объекты создаются в нём).
        ttf_path: Путь к TTF с TrueType-контурами (``/FontFile2``).
        chars: Символы, которые должны быть в subset'е.
        base_font_name: Имя шрифта в PDF (без слэша).

    Returns:
        pikepdf.Object: словарь Type0 с потомком CIDFontType2.

    Raises:
        ImportError: если не установлен ``fontTools``.
        OSError: если шрифт не читается.
    """
    from fontTools import subset  # noqa: PLC0415 - тяжёлый импорт, нужен лениво

    options = subset.Options()
    options.glyph_names = False
    options.notdef_outline = True
    options.recalc_bounds = True
    options.recalc_timestamp = False
    options.drop_tables = sorted(set(options.drop_tables) | {"FFTM", "DSIG"})

    font = subset.load_font(str(ttf_path), options)
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(text="".join(sorted(set(chars))))
    subsetter.subset(font)

    buffer = io.BytesIO()
    font.save(buffer)
    font_data = buffer.getvalue()

    units_per_em = font["head"].unitsPerEm
    scale = 1000.0 / units_per_em
    cmap = font.getBestCmap() or {}

    widths: dict[int, int] = {}
    tounicode: dict[int, str] = {}
    missing: list[str] = []
    for char in sorted(set(chars)):
        glyph = cmap.get(ord(char))
        if glyph is None:
            missing.append(char)
            continue
        gid = font.getGlyphID(glyph)
        widths[gid] = round(font["hmtx"].metrics[glyph][0] * scale)
        tounicode[gid] = char

    if missing:
        LOGGER.warning("В шрифте %s нет глифов: %s", ttf_path.name, "".join(missing))

    head, hhea = font["head"], font["hhea"]
    os2 = font.get("OS/2")
    descriptor = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/FontDescriptor"),
                "/FontName": pikepdf.Name(f"/{base_font_name}"),
                "/Flags": 32,
                "/FontBBox": [
                    round(head.xMin * scale),
                    round(head.yMin * scale),
                    round(head.xMax * scale),
                    round(head.yMax * scale),
                ],
                "/ItalicAngle": 0,
                "/Ascent": round(hhea.ascent * scale),
                "/Descent": round(hhea.descent * scale),
                "/CapHeight": round(getattr(os2, "sCapHeight", hhea.ascent) * scale),
                "/StemV": 80,
                "/FontFile2": pdf.make_stream(font_data),
            }
        )
    )
    descriptor["/FontFile2"]["/Length1"] = len(font_data)

    # /W строго в виде [c [w] c [w] ...]: плоские пары (c w) спецификации
    # не соответствуют и дают неверные ширины (проверено в research).
    widths_array = pikepdf.Array()
    for gid in sorted(widths):
        widths_array.append(gid)
        widths_array.append(pikepdf.Array([widths[gid]]))

    cid_font = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/CIDFontType2"),
                "/BaseFont": pikepdf.Name(f"/{base_font_name}"),
                "/CIDSystemInfo": pikepdf.Dictionary(
                    {
                        "/Registry": pikepdf.String("Adobe"),
                        "/Ordering": pikepdf.String("Identity"),
                        "/Supplement": 0,
                    }
                ),
                "/FontDescriptor": descriptor,
                "/DW": 1000,
                "/W": widths_array,
                "/CIDToGIDMap": pikepdf.Name("/Identity"),
            }
        )
    )

    entries = "\n".join(
        f'<{gid:04X}> <{"".join(f"{ord(ch):04X}" for ch in uni)}>'
        for gid, uni in sorted(tounicode.items())
    )
    cmap_text = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(tounicode)} beginbfchar\n{entries}\nendbfchar\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    )

    return pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type0"),
                "/BaseFont": pikepdf.Name(f"/{base_font_name}"),
                "/Encoding": pikepdf.Name("/Identity-H"),
                "/DescendantFonts": pikepdf.Array([cid_font]),
                "/ToUnicode": pdf.make_stream(cmap_text.encode("latin-1")),
            }
        )
    )


def add_cyrillic_font(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    ttf_path: Path,
    chars: str,
    resource_name: str,
) -> str:
    """Добавить Type0-шрифт в ресурсы страницы под заданным именем.

    Args:
        pdf: Открытый документ.
        page: Страница, в ресурсы которой добавляется шрифт.
        ttf_path: Путь к TTF с кириллицей.
        chars: Символы, которые должны быть доступны.
        resource_name: Имя ресурса без слэша (``TFCyr0``).

    Returns:
        str: имя ресурса для ``replace_block(font_name=...)``.

    Raises:
        ImportError: ``fontTools`` не установлен.
        OSError: шрифт не читается.
    """
    font = build_type0_font(pdf, ttf_path, chars, base_font_name=resource_name)

    resources = page.get("/Resources")
    if resources is None:
        resources = pikepdf.Dictionary()
        page["/Resources"] = resources
    fonts = resources.get("/Font")
    if fonts is None:
        fonts = pikepdf.Dictionary()
        resources["/Font"] = fonts
    fonts[pikepdf.Name(f"/{resource_name}")] = font
    return resource_name


_BT_BLOCK_RE = re.compile(r"BT(.*?)ET", re.S)
_HAS_TF_RE = re.compile(r"/\S+\s+[\d.]+\s+Tf")
_HAS_TEXT_OP_RE = re.compile(r"\bTj\b|\bTJ\b")

#: ``a b c d e f Tm`` — позиция текста блока (e = x, f = y).
_BT_TM_RE = re.compile(
    r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+"
    r"([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+([-+]?\d*\.?\d+)\s+Tm"
)


def _nearest_tf(
    position: tuple[float, float] | None,
    fallback_map: Mapping[tuple[float, float], str],
    default_tf: str | None,
) -> str | None:
    """Выбрать ``Tf`` для блока без шрифта по его позиции.

    Близость важнее направления: ``block.y`` из ``get_text_layout`` — это низ
    bbox, а ``Tm`` задаёт базовую линию, поэтому координата самого блока
    систематически на ~0.25 кегля **меньше** ``Tm`` своей же строки. Если
    предпочитать только «блоки выше», собственный блок всегда проигрывает
    соседу на строку выше (реальный случай: Times-строка получала Helvetica).
    Поэтому ранг — сначала ``|dy|``, затем ``|dx|``; «выше» остаётся лишь
    тай-брейком при равном расстоянии (порядок чтения).

    Args:
        position: ``(y, x)`` блока из content stream или ``None``.
        fallback_map: ``{(y, x): "Tf-оператор"}`` — шрифты блоков страницы.
        default_tf: Шрифт, если позиция неизвестна.

    Returns:
        str | None: Оператор ``Tf`` либо ``None``, если выбрать не из чего.

    Raises:
        Ничего не бросает.
    """
    if not fallback_map:
        return default_tf
    if position is None:
        return default_tf

    y_pos, x_pos = position
    best: tuple[tuple[float, float, int], str] | None = None
    for (block_y, block_x), tf in fallback_map.items():
        rank = (
            abs(block_y - y_pos),
            abs(block_x - x_pos),
            0 if block_y >= y_pos else 1,
        )
        if best is None or rank < best[0]:
            best = (rank, tf)
    return best[1] if best is not None else default_tf


def repair_inherited_font(
    stream: str,
    fallback_map: Mapping[tuple[float, float], str],
    default_tf: str | None = None,
) -> tuple[str, int, list[str]]:
    """Вернуть ``Tf`` в ``BT``-блоки, которые его потеряли.

    ``replace_block`` переписывает «палитру» и ставит ``Tf`` на свой блок.
    Следующий ``BT`` без ``Tf`` (так делает, например, reportlab) наследует
    текстовое состояние — то есть наш CID-шрифт — и 1-байтовые коды начинают
    читаться как половины 2-байтовых.

    Шрифт подбирается по позиции: для каждого блока без ``Tf`` берётся
    ближайший предшествующий блок страницы с известным ``Tf`` (карта строится
    по всем блокам, а не по одному «самому частому» на страницу).

    Args:
        stream: Декодированный content stream страницы.
        fallback_map: ``{(y, x): "Tf-оператор"}`` — шрифты блоков страницы.
        default_tf: Оператор для блоков, чью позицию определить не удалось.

    Returns:
        tuple[str, int, list[str]]: (исправленный поток, сколько блоков
        починено, предупреждения о блоках без определимого шрифта).

    Raises:
        Ничего не бросает.
    """
    if not fallback_map:
        # Ни одного известного блока на странице — выбирать не из чего.
        # Предупреждать не о чем: ремонт этой страницы не применялся вовсе
        # (в отличие от блока, чью позицию не удалось разобрать).
        return stream, 0, []

    fixed = 0
    problems: list[str] = []

    def _fix(match: re.Match[str]) -> str:
        """Вставить ``Tf`` в один ``BT``-блок, если его там нет.

        Args:
            match: Совпадение регулярного выражения ``BT(.*?)ET``.

        Returns:
            str: Исходный блок или блок с добавленным ``Tf``.

        Raises:
            Ничего не бросает.
        """
        nonlocal fixed
        body = match.group(1)
        if _HAS_TF_RE.search(body) or not _HAS_TEXT_OP_RE.search(body):
            return match.group(0)

        position: tuple[float, float] | None = None
        tm_match = _BT_TM_RE.search(body)
        if tm_match:
            position = (float(tm_match.group(6)), float(tm_match.group(5)))

        tf = _nearest_tf(position, fallback_map, default_tf)
        if tf is None:
            problems.append(
                "BT без Tf: не удалось определить шрифт по позиции "
                f"({position}) — блок оставлен как есть"
            )
            return match.group(0)

        fixed += 1
        return "BT\n" + tf + body + "ET"

    return _BT_BLOCK_RE.sub(_fix, stream), fixed, problems




