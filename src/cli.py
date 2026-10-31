"""Командный интерфейс TermRu на argparse.

Пайплайн::

    extract_text → replace_dict_only → (опционально) DeepSeek → apply_replacements

* Словарный проход детерминирован и бесплатен.
* LLM подключается только для блоков, где после словаря остались латинские слова
  длиной ≥ 4 символов (исключения из ``exceptions.json`` не считаются).
* Блоки отправляются в LLM батчами с маркерами ``<<<TERMRU:N>>>``; если модель
  нарушила разметку, батч дообрабатывается по одному блоку.

Потоки вывода
-------------
* ``stdout`` — только отчёт ``--dry-run`` в виде JSON (машиночитаемо).
* ``stderr`` — все сообщения через ``logging`` (``--verbose`` → DEBUG, иначе
  WARNING и выше).

Коды возврата
-------------
* ``0`` — успех (в том числе ``--dry-run``).
* ``1`` — ошибка обработки: нет входного файла, неподдерживаемый формат, битые
  словари, ошибка записи, LLM недоступна при ``--use-llm``.
* ``2`` — невалидные аргументы: неизвестный флаг, нечисловой/неположительный
  ``--batch-size``, ошибка разбора аргументов.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .deepseek_client import DeepSeekClient, DeepSeekError, estimate_input_tokens
from .document_processor import (
    DocumentProcessingError,
    DocumentProcessor,
    UnsupportedFormatError,
)
from .glossary_manager import GlossaryError, GlossaryManager
from .replacement_engine import ReplacementEngine

__all__ = [
    "EXIT_INVALID_ARGS",
    "EXIT_OK",
    "EXIT_PROCESSING_ERROR",
    "build_parser",
    "default_output_path",
    "main",
]

LOGGER = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_PROCESSING_ERROR = 1
EXIT_INVALID_ARGS = 2


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GLOSSARY_PATH = _PROJECT_ROOT / "data" / "glossary.json"
DEFAULT_EXCEPTIONS_PATH = _PROJECT_ROOT / "data" / "exceptions.json"

#: Латинское слово длиной ≥ 4 символов — кандидат на LLM-обработку.
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{4,}")

#: Маркер блока внутри батча для LLM.
_MARKER_RE = re.compile(r"<<<TERMRU:(\d+)>>>")
_MARKER_HINT = "Разделители <<<TERMRU:N>>> не переводи, не удаляй и не меняй."


def auto_batch_size(total_blocks: int) -> int:
    """Подобрать размер батча под объём документа.

    Крупные документы выигрывают от крупных батчей (меньше запросов → меньше
    повторно отправляемого system-префикса), мелкие — от батчей по одному.

    Args:
        total_blocks: Количество текстовых блоков в документе.

    Returns:
        int: ``max(1, min(20, total_blocks // 20))``.

    Raises:
        Ничего не бросает.
    """
    return max(1, min(20, total_blocks // 20))


def default_output_path(input_path: str | Path) -> Path:
    """Построить путь результата по умолчанию: ``<stem>_ru<suffix>``.

    Args:
        input_path: Путь к исходному документу.

    Returns:
        Path: ``report.docx`` → ``report_ru.docx`` (расширение сохраняется).

    Raises:
        Ничего не бросает.
    """
    path = Path(input_path)
    return path.with_name(f"{path.stem}_ru{path.suffix}")


def build_parser() -> argparse.ArgumentParser:
    """Собрать парсер аргументов CLI.

    Args:
        Нет аргументов.

    Returns:
        argparse.ArgumentParser: парсер с опциями ``--input`` … ``--verbose``.

    Raises:
        Ничего не бросает.
    """
    parser = argparse.ArgumentParser(
        prog="termru",
        description=(
            "Замена англицизмов в DOCX/PPTX/PDF на русские аналоги "
            "с сохранением форматирования."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        metavar="PATH",
        help="входной файл (.docx, .pptx или .pdf)",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="файл результата; по умолчанию <input>_ru.<ext>",
    )
    parser.add_argument(
        "--glossary",
        default=str(DEFAULT_GLOSSARY_PATH),
        metavar="PATH",
        help="JSON-глоссарий замен (по умолчанию data/glossary.json)",
    )
    parser.add_argument(
        "--exceptions",
        default=str(DEFAULT_EXCEPTIONS_PATH),
        metavar="PATH",
        help="JSON-список исключений (по умолчанию data/exceptions.json)",
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="подключить DeepSeek для блоков, где остались англицизмы",
    )
    parser.add_argument(
        "--no-sanitize",
        action="store_true",
        help=(
            "не обезвреживать prompt-injection в тексте документа "
            "(только для отладки)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только отчёт в stdout (JSON), файл не пишется",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        metavar="INT",
        help=(
            "сколько блоков отправлять в LLM за один запрос; по умолчанию "
            "max(1, min(20, блоков // 20))"
        ),
    )
    parser.add_argument(
        "--estimate-cost",
        action="store_true",
        help=(
            "оценить входные токены LLM и выйти: без запросов и без записи файла"
        ),
    )
    parser.add_argument(
        "--pdf-font-dir",
        metavar="PATH",
        help="дополнительная папка с TTF для PDF-замен (портативная сборка)",
    )
    parser.add_argument(
        "--pdf-fallback",
        choices=("fail", "polydoc"),
        default="fail",
        help=(
            "если кириллический шрифт не найден: fail — ошибка по блоку "
            "(по умолчанию), polydoc — пересобрать PDF (вёрстка теряется)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="машиночитаемая квитанция в stdout (одна строка JSON)",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--verbose",
        action="store_true",
        help="подробное логирование в stderr (DEBUG)",
    )
    verbosity.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="только WARN/ERROR в stderr (несовместимо с --verbose)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI: прочитать документ, заменить, записать или отчитаться.

    Args:
        argv: Аргументы командной строки без имени программы; ``None`` —
            взять ``sys.argv[1:]``.

    Returns:
        int: код возврата: ``0`` — успех, ``1`` — ошибка обработки,
        ``2`` — невалидные аргументы.

    Raises:
        Ничего не бросает: ошибки логируются в stderr и превращаются в код
        возврата.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse уже напечатал usage/ошибку
        return exc.code if isinstance(exc.code, int) else EXIT_OK

    _force_utf8_streams()
    _configure_logging(args.verbose, args.quiet)
    started = time.perf_counter()
    json_mode = bool(args.json)

    if args.batch_size is not None and args.batch_size < 1:
        LOGGER.error("--batch-size должен быть >= 1, получено %s", args.batch_size)
        return EXIT_INVALID_ARGS

    try:
        glossary = GlossaryManager(args.glossary, args.exceptions)
    except GlossaryError as exc:
        return _emit_error(f"Не удалось загрузить словари: {exc}", None, json_mode)

    llm_client = None
    if args.use_llm and not args.estimate_cost:
        try:
            llm_client = DeepSeekClient(sanitize=not args.no_sanitize)
        except DeepSeekError as exc:
            return _emit_error(f"LLM недоступна: {exc}", None, json_mode)

    engine = ReplacementEngine(glossary, deepseek_client=llm_client)
    processor = DocumentProcessor(
        pdf_font_dir=args.pdf_font_dir,
        pdf_fallback=args.pdf_fallback,
    )

    input_path = Path(args.input).expanduser()
    try:
        blocks = processor.extract_text(str(input_path))
    except (UnsupportedFormatError, DocumentProcessingError) as exc:
        return _emit_error(str(exc), None, json_mode)

    LOGGER.info("Блоков к обработке: %s", len(blocks))

    batch_size = args.batch_size
    if batch_size is None:
        batch_size = auto_batch_size(len(blocks))
        LOGGER.info("batch_size автовыбран: %s (блоков: %s)", batch_size, len(blocks))
    else:
        LOGGER.debug("batch_size задан явно: %s", batch_size)

    originals: dict[int, str] = {int(block["block_id"]): block["text"] for block in blocks}
    texts: dict[int, str] = {}
    changes: list[dict] = []
    warnings: list[str] = []
    llm_queue: list[int] = []
    # Очередь LLM нужна и для оценки стоимости, где клиент не создаётся.
    wants_llm = args.use_llm or args.estimate_cost

    for block in blocks:
        block_id = int(block["block_id"])
        new_text, block_changes = engine.replace_dict_only(block["text"])
        texts[block_id] = new_text
        for change in block_changes:
            changes.append(
                {
                    "block_id": block_id,
                    "original": change["original"],
                    "replacement": change["replacement"],
                    "source": change["source"],
                    "position": change["position"],
                }
            )
        if wants_llm and _has_latin_words(new_text, glossary):
            llm_queue.append(block_id)

    estimated: dict[str, int] | None = None
    if args.estimate_cost:
        estimated = estimate_input_tokens(
            dict(glossary.glossary),
            dict.fromkeys(glossary.exceptions, None),
            [texts[block_id] for block_id in llm_queue],
            batch_size,
        )
        LOGGER.info(
            "оценка LLM: ~%s входных токенов, батчей — %s (запросы не выполнялись)",
            estimated["estimated_input_tokens"],
            estimated["estimated_batches"],
        )
    elif llm_client is not None and llm_queue:
        LOGGER.info(
            "Блоков на LLM-доработку: %s (батч по %s)",
            len(llm_queue),
            batch_size,
        )
        llm_changes, llm_warnings = _run_llm_batches(
            client=llm_client,
            glossary=glossary,
            block_ids=llm_queue,
            texts=texts,
            batch_size=batch_size,
        )
        changes.extend(llm_changes)
        warnings.extend(llm_warnings)
    elif llm_client is not None:
        LOGGER.info("Англицизмов не осталось: LLM не вызывается")

    changed_blocks = sum(
        1 for block_id, text in texts.items() if originals.get(block_id) != text
    )
    report_changes = [
        {key: change[key] for key in ("block_id", "original", "replacement", "source")}
        for change in changes
    ]
    report = {
        "total_blocks": len(blocks),
        "dict_replacements": sum(1 for change in changes if change["source"] == "dict"),
        "llm_replacements": sum(1 for change in changes if change["source"] == "llm"),
        "changes": report_changes,
        "warnings": warnings,
    }

    if args.dry_run:
        payload = dict(report)
        if json_mode:
            payload["changes"] = changes
        if estimated is not None:
            payload["estimated_input_tokens"] = estimated["estimated_input_tokens"]
        _write_stdout(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        LOGGER.info("dry-run: отчёт в stdout, файл не записан")
        return EXIT_OK

    if estimated is not None:
        _write_stdout(json.dumps(estimated, ensure_ascii=False) + "\n")
        LOGGER.info("--estimate-cost: файл не записан")
        return EXIT_OK

    output_path = (
        Path(args.output).expanduser() if args.output else default_output_path(input_path)
    )
    try:
        result = processor.apply_replacements(str(input_path), texts, str(output_path))
    except Exception as exc:  # noqa: BLE001 - граница CLI: наружу только код возврата
        return _emit_error(f"Не удалось применить замены: {exc}", None, json_mode)

    if not result["success"]:
        return _emit_error(
            f"Результат не записан: {output_path}",
            result["errors"],
            json_mode,
        )

    for warning in warnings:
        LOGGER.warning("%s", warning)
    LOGGER.info(
        "Готово: %s | блоков: %s | замен словарём: %s | замен LLM: %s | изменено блоков: %s",
        output_path,
        report["total_blocks"],
        report["dict_replacements"],
        report["llm_replacements"],
        changed_blocks,
    )

    if json_mode:
        _write_stdout(
            json.dumps(
                {
                    "status": "ok",
                    "output": str(output_path.resolve()),
                    "input": str(input_path.resolve()),
                    "summary": {
                        "total_blocks": report["total_blocks"],
                        "changed_blocks": changed_blocks,
                        "dict_replacements": report["dict_replacements"],
                        "llm_replacements": report["llm_replacements"],
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                    },
                    "changes": changes,
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return EXIT_OK


def _run_llm_batches(
    client: DeepSeekClient,
    glossary: GlossaryManager,
    block_ids: Sequence[int],
    texts: dict[int, str],
    batch_size: int,
) -> tuple[list[dict], list[str]]:
    """Обработать очередь блоков через LLM батчами, сохраняя порядок.

    Блоки склеиваются маркерами ``<<<TERMRU:N>>>``. Если модель нарушила
    разметку (маркеров не столько, сколько блоков), батч дообрабатывается по
    одному блоку — это дороже, но не теряет результат.

    Args:
        client: Клиент DeepSeek.
        glossary: Менеджер словарей (глоссарий и исключения для промпта).
        block_ids: Идентификаторы блоков в порядке документа.
        texts: Текущие тексты блоков; изменённые значения пишутся сюда же.
        batch_size: Сколько блоков отправлять за один запрос.

    Returns:
        tuple[list[dict], list[str]]: (changes с ``source="llm"``, предупреждения).

    Raises:
        Ничего не бросает: сбой LLM превращается в предупреждение, блок
        остаётся со словарной заменой.
    """
    changes: list[dict] = []
    warnings: list[str] = []
    glossary_mapping = dict(glossary.glossary)
    exceptions_mapping = dict.fromkeys(glossary.exceptions, None)

    for batch_index, start in enumerate(range(0, len(block_ids), batch_size), start=1):
        batch = list(block_ids[start : start + batch_size])
        batch_texts = [texts[block_id] for block_id in batch]
        LOGGER.debug("LLM-батч %s: блоки %s", batch_index, batch)

        raw = _request_llm(
            client,
            _join_blocks(batch_texts),
            glossary_mapping,
            exceptions_mapping,
            block_id=",".join(str(block_id) for block_id in batch),
        )
        _collect_sanitization(client, warnings)
        if raw is None:
            warnings.append(
                f"батч {batch_index}: LLM недоступна, оставлены словарные замены"
            )
            continue

        parts = _split_blocks(raw, len(batch))
        if parts is None:
            warnings.append(
                f"батч {batch_index}: нарушена разметка ответа, "
                "обрабатываю по одному блоку"
            )
            LOGGER.warning(
                "Батч %s: не удалось разбить ответ по маркерам, обрабатываю поштучно",
                batch_index,
            )
            for block_id, text in zip(batch, batch_texts):
                single = _request_llm(
                    client, text, glossary_mapping, exceptions_mapping, block_id=block_id
                )
                _collect_sanitization(client, warnings)
                if single is None:
                    warnings.append(f"блок {block_id}: LLM недоступна")
                    continue
                _record_llm_change(changes, warnings, block_id, text, single, texts)
            continue

        for block_id, before, after in zip(batch, batch_texts, parts):
            _record_llm_change(changes, warnings, block_id, before, after, texts)

    return changes, warnings


def _request_llm(
    client: DeepSeekClient,
    text: str,
    glossary_mapping: dict[str, str],
    exceptions_mapping: dict[str, None],
    block_id: int | str | None = None,
) -> str | None:
    """Выполнить один запрос к LLM и вернуть текст ответа.

    Args:
        client: Клиент DeepSeek.
        text: Текст для обработки (один блок или склеенный батч).
        glossary_mapping: Глоссарий для промпта.
        exceptions_mapping: Исключения для промпта.
        block_id: Идентификатор блока или список id батча — попадает в лог
            санитизации.

    Returns:
        str | None: Ответ модели либо ``None``, если запрос не удался.

    Raises:
        Ничего не бросает: ``DeepSeekError`` логируется и превращается в ``None``.
    """
    try:
        return client.replace_text(
            text, glossary_mapping, exceptions_mapping, block_id=block_id
        )
    except DeepSeekError as exc:
        LOGGER.warning("Запрос к LLM не удался: %s", exc)
        return None


def _collect_sanitization(client: DeepSeekClient, warnings: list[str]) -> None:
    """Перенести сведения о санитизации из клиента в отчёт CLI.

    Клиент пишет предупреждение в лог, но машиночитаемый отчёт должен знать о
    факте инъекции — иначе CI не отличит «чистый» документ от подозрительного.

    Args:
        client: Клиент, у которого мог сработать ``last_sanitization``.
        warnings: Накопитель предупреждений отчёта.

    Returns:
        None

    Raises:
        Ничего не бросает: у сторонних клиентов атрибута может не быть.
    """
    info = getattr(client, "last_sanitization", None)
    if not info:
        return
    warnings.append(
        f"блок {info.get('block_id')}: текст санитизирован "
        f"(маркеров: {info.get('escaped_markers', 0)}, "
        f"инструкций удалено: {info.get('removed_instructions', 0)})"
    )


def _record_llm_change(
    changes: list[dict],
    warnings: list[str],
    block_id: int,
    before: str,
    after: str,
    texts: dict[int, str],
) -> None:
    """Зафиксировать LLM-правку блока, если текст действительно изменился.

    Args:
        changes: Накопитель изменений (пополняется при успехе).
        warnings: Накопитель предупреждений.
        block_id: Идентификатор блока.
        before: Текст до LLM (после словарного прохода).
        after: Ответ LLM для этого блока.
        texts: Текущие тексты блоков (обновляется при изменении).

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    cleaned = after.strip() if isinstance(after, str) else ""
    if not cleaned:
        warnings.append(f"блок {block_id}: LLM вернула пустой текст")
        return
    if cleaned == before:
        LOGGER.debug("Блок %s: LLM не изменила текст", block_id)
        return
    texts[block_id] = cleaned
    changes.append(
        {
            "block_id": block_id,
            "original": before,
            "replacement": cleaned,
            "source": "llm",
            "position": _first_difference(before, cleaned),
        }
    )


def _first_difference(before: str, after: str) -> int:
    """Индекс первого различающегося символа двух строк.

    Для LLM-правок меняется весь блок, поэтому «позиция» — это первое место,
    где текст разошёлся (для словарных замен позиция приходит из движка).

    Args:
        before: Текст до правки.
        after: Текст после правки.

    Returns:
        int: индекс первого различия; ``min(len(before), len(after))``, если
        одна строка — префикс другой.

    Raises:
        Ничего не бросает.
    """
    limit = min(len(before), len(after))
    for index in range(limit):
        if before[index] != after[index]:
            return index
    return limit


def _join_blocks(texts: Sequence[str]) -> str:
    """Склеить тексты блоков в один запрос с маркерами-разделителями.

    Args:
        texts: Тексты блоков в порядке документа.

    Returns:
        str: Подсказка про маркеры плюс блоки вида
        ``<<<TERMRU:0>>>\\n<текст>``, разделённые переводами строк.

    Raises:
        Ничего не бросает.
    """
    parts: list[str] = [_MARKER_HINT, ""]
    for index, text in enumerate(texts):
        parts.append(f"<<<TERMRU:{index}>>>")
        parts.append(text)
    return "\n".join(parts)


def _split_blocks(response: str, count: int) -> list[str] | None:
    """Разобрать ответ LLM обратно на блоки по маркерам.

    Args:
        response: Ответ модели.
        count: Ожидаемое число блоков.

    Returns:
        list[str] | None: Тексты блоков в исходном порядке либо ``None``, если
        маркеров не столько, сколько ожидалось, или они идут не по порядку.

    Raises:
        Ничего не бросает.
    """
    matches = list(_MARKER_RE.finditer(response))
    if len(matches) != count:
        return None

    parts: list[str] = []
    for index, match in enumerate(matches):
        if match.group(1) != str(index):
            return None
        end = matches[index + 1].start() if index + 1 < count else len(response)
        parts.append(response[match.end() : end].strip())
    return parts


def _has_latin_words(text: str, glossary: GlossaryManager) -> bool:
    """Проверить, остались ли в тексте англицизмы для LLM-доработки.

    Слова короче 4 символов и токены из исключений (``TLS``, ``KPI``, ``HTTPS``)
    кандидатами не считаются.

    Args:
        text: Текст после словарного прохода.
        glossary: Менеджер словарей (для проверки исключений).

    Returns:
        bool: ``True``, если найден хотя бы один латинский фрагмент ≥ 4 букв.

    Raises:
        Ничего не бросает.
    """
    for match in _LATIN_WORD_RE.finditer(text):
        if glossary.is_exception(match.group(0)):
            continue
        return True
    return False


def _force_utf8_streams() -> None:
    """Переключить stdout/stderr на UTF-8.

    На Windows кодировка консоли по умолчанию — cp1252/cp866, и кириллица в
    JSON или логах падает с ``UnicodeEncodeError``. Переключение безопасно:
    консоль Windows работает через Unicode API, а для пайпов UTF-8 — стандарт.

    Returns:
        None

    Raises:
        Ничего не бросает: некоторые подменённые потоки (pytest) не поддерживают
        ``reconfigure``.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue


def _configure_logging(verbose: bool, quiet: bool = False) -> None:
    """Настроить логирование в stderr.

    Args:
        verbose: ``True`` — уровень DEBUG.
        quiet: ``True`` — только WARNING и ERROR (INFO подавляется).

    Returns:
        None

    Raises:
        Ничего не бросает: взаимоисключение проверяет argparse.
    """
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(message)s",
        force=True,
    )
    LOGGER.setLevel(level)
    # Внутренние DEBUG-простыни сторонних библиотек (fontTools, PIL, ...) не
    # помогают пользователю, поэтому глушим их даже в --verbose.
    for noisy in ("fontTools", "pikepdf", "PIL", "pdfminer", "reportlab"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _emit_error(
    message: str,
    errors: Sequence[dict] | None,
    json_mode: bool,
) -> int:
    """Залогировать ошибку и, при ``--json``, отдать квитанцию об ошибке.

    Args:
        message: Короткое описание проблемы.
        errors: Ошибки по блокам (``{"block_id": ..., "message": ...}``).
        json_mode: Печатать ли JSON в stdout.

    Returns:
        int: всегда :data:`EXIT_PROCESSING_ERROR`.

    Raises:
        Ничего не бросает.
    """
    LOGGER.error("%s", message)
    for error in errors or []:
        LOGGER.error("Блок %s: %s", error.get("block_id"), error.get("message"))

    if json_mode:
        payload = {
            "status": "error",
            "error": message,
            "errors": list(errors) if errors else [{"block_id": None, "message": message}],
        }
        _write_stdout(json.dumps(payload, ensure_ascii=False) + "\n")
    return EXIT_PROCESSING_ERROR


def _write_stdout(payload: str) -> None:
    """Записать данные в stdout (отчёт, а не сообщение).

    Args:
        payload: Готовый текст отчёта (JSON с завершающим переводом строки).

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    sys.stdout.write(payload)
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover - точка входа модуля
    raise SystemExit(main())




