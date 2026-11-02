"""Клиент DeepSeek API (OpenAI-совместимый) для LLM-режима замены.

Один запрос = один батч текста. Клиент подставляет в шаблон
``prompts/system_prompt.txt`` список исключений, глоссарий и сам текст, просит
модель вернуть только исправленный текст и проверяет, что ответ действительно
пригоден (не пустой, без эха промпта и markdown-обёрток).

Надёжность
----------
* ``temperature=0.1`` — предсказуемость важнее разнообразия.
* Таймаут 60 с на запрос.
* Транспортные и HTTP-ошибки (``openai.APIError``) повторяются до 3 раз с
  экспоненциальной паузой (1, 2, 4 с).
* Непригодный ответ (пустой, с эхом ``EXCEPTIONS``/``GLOSSARY``, в
  markdown-обёртке) повторяется до 2 попыток, затем
  :class:`DeepSeekValidationError`.
* Использование токенов (``prompt_tokens`` / ``completion_tokens``) уходит в
  ``log_callback``.

Отклонение от ТЗ
----------------
Признак ``\\`\\`\\``` считается артефактом только если его **не было** в исходном
тексте: иначе абзац с легальным блоком кода невозможно было бы обработать
(модель обязана сохранить backticks по правилу 4 промпта). Так же трактуются
маркеры ``EXCEPTIONS``/``GLOSSARY`` — эхо промпта отличается от упоминания
этих слов в самом документе.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import openai
from dotenv import load_dotenv
from openai import OpenAI

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DeepSeekAPIError",
    "DeepSeekClient",
    "DeepSeekConfigError",
    "DeepSeekError",
    "DeepSeekValidationError",
]

LOGGER = logging.getLogger(__name__)

#: Корень проекта: отсюда берутся ``.env`` и ``prompts/system_prompt.txt``.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = _PROJECT_ROOT / "prompts" / "system_prompt.txt"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 1.0
DEFAULT_MAX_VALIDATION_ATTEMPTS = 2
TEMPERATURE = 0.1

_FENCE = "```"

#: Маркеры, появление которых в ответе означает эхо промпта.
_ECHO_MARKERS = ("EXCEPTIONS", "GLOSSARY")

#: Преамбулы вида «Вот результат:» / «Here is the result:».
_PREAMBLE_RE = re.compile(
    r"^\s*(?:(?:вот|here\s+is)\s+)?(?:(?:исправленный|готовый|corrected|the)\s+)*"
    r"(?:результат|текст|result|text)\s*[:\-—]\s*",
    re.IGNORECASE,
)

#: Zero-width space: невидим для пользователя, но ломает совпадение с маркером.
_ZWSP = "\u200b"

#: Метки, которые модель не должна принимать за разделы промпта.
_PROMPT_MARKER_RE = re.compile(r"(EXCEPTIONS:|GLOSSARY:|Текст:)", re.IGNORECASE)

#: Классическая prompt-injection конструкция.
_IGNORE_INSTRUCTION_RE = re.compile(
    r"ignore\s+(?:all\s+)?(?:previous|above)\s+instructions", re.IGNORECASE
)

#: Код в ```...``` и `...` не санитизируется.
_CODE_SPAN_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)

#: Обратная операция: снять ZWSP, если модель переписала экранированный маркер.
_ESCAPED_MARKER_RE = re.compile(
    r"([EGТ])\u200b(?=(?:XCEPTIONS|LOSSARY|екст):)", re.IGNORECASE
)

#: Маркер промпта в начале строки (сильнее, чем просто упоминание в тексте).
_MARKER_LINE_RE = re.compile(r"(?m)^[ \t]*(?:EXCEPTIONS:|GLOSSARY:|Текст:)", re.IGNORECASE)

#: Упоминания системного промпта.
_SYSTEM_PROMPT_RE = re.compile(
    r"system\s+(?:prompt|message)|instructions\s+above", re.IGNORECASE
)

#: Ролевые конструкции в начале предложения (``You are a …``).
_ROLEPLAY_RE = re.compile(
    r"(?:^|[.!?]\s+)(?:you\s+are\s+a|act\s+as|pretend\s+to\s+be)\b", re.IGNORECASE
)

#: Длинная base64-подобная последовательность без пробелов.
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")

#: Упоминания учётных данных.
_CREDENTIAL_RE = re.compile(
    r"\b(?:api[ _-]?key|deepseek_api_key|password|secret)\b", re.IGNORECASE
)


def _looks_suspicious(text: str) -> tuple[bool, list[str]]:
    """Есть ли в тексте признаки попытки управлять моделью.

    Используется опциональным режимом ``--skip-suspicious``: такие блоки не
    отправляются в LLM вовсе (только словарная обработка). Это **не** замена
    санитизации — независимый, более грубый механизм снижения риска.

    Args:
        text: Исходный (не санитизированный) текст блока.

    Returns:
        tuple[bool, list[str]]: (подозрительный ли блок, список причин
        человеческим языком). Пустой список причин означает «чисто».

    Raises:
        Ничего не бросает.
    """
    if not text:
        return False, []

    reasons: list[str] = []
    if _MARKER_LINE_RE.search(text):
        reasons.append("EXCEPTIONS:/GLOSSARY:/Текст: marker")
    if _IGNORE_INSTRUCTION_RE.search(text):
        reasons.append("ignore previous instructions")
    if _SYSTEM_PROMPT_RE.search(text):
        reasons.append("system prompt reference")
    if _ROLEPLAY_RE.search(text):
        reasons.append("role-play instruction")
    if _BASE64_BLOB_RE.search(text):
        reasons.append("base64 > 100 chars")
    if _CREDENTIAL_RE.search(text):
        reasons.append("credential reference")
    return bool(reasons), reasons


def _map_outside_code(text: str, transform: Callable[[str], str]) -> str:
    """Применить преобразование ко всем участкам вне ```...``` и ``...``.

    Args:
        text: Исходный текст.
        transform: Функция, применяемая к фрагментам без кода.

    Returns:
        str: Текст, где код оставлен дословно, а остальное преобразовано.

    Raises:
        Ничего не бросает.
    """
    parts: list[str] = []
    position = 0
    for match in _CODE_SPAN_RE.finditer(text):
        parts.append(transform(text[position : match.start()]))
        parts.append(match.group(0))  # код неприкосновенен
        position = match.end()
    parts.append(transform(text[position:]))
    return "".join(parts)


class DeepSeekError(Exception):
    """Базовое исключение клиента DeepSeek.

    Позволяет CLI ловить одним ``except`` и транспортные сбои, и непригодные
    ответы модели.
    """


class DeepSeekAPIError(DeepSeekError):
    """Транспортная или HTTP-ошибка при обращении к DeepSeek API.

    Поднимается после исчерпания повторов (``openai.APIError``, таймауты,
    недоступность сети); исходное исключение доступно через ``__cause__``.

    Attributes:
        attempts: Сколько запросов было выполнено, если известно.
    """

    def __init__(self, message: str, attempts: int | None = None) -> None:
        """Создать исключение об ошибке API.

        Args:
            message: Описание проблемы.
            attempts: Количество выполненных запросов, если известно.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.attempts = attempts
        super().__init__(message)


class DeepSeekConfigError(DeepSeekAPIError):
    """Ошибка конфигурации: нет ключа, шаблон промпта не найден или битый.

    Наследуется от :class:`DeepSeekAPIError`, чтобы вызывающий код мог ловить
    все проблемы интеграции одним типом.
    """


class DeepSeekValidationError(DeepSeekError):
    """Ответ модели непригоден: пустой, эхо промпта или markdown-обёртка.

    Поднимается после ``max_validation_attempts`` неудачных попыток.
    """


def _load_env() -> None:
    """Загрузить переменные из ``.env`` проекта, не перетирая уже заданные.

    Args:
        Нет аргументов.

    Returns:
        None

    Raises:
        Ничего не бросает: ошибка чтения ``.env`` попадает в лог и не мешает
        работе (ключ мог быть передан явно или задан в окружении).
    """
    env_path = _PROJECT_ROOT / ".env"
    try:
        load_dotenv(env_path if env_path.exists() else None, override=False)
    except Exception as exc:  # noqa: BLE001 - .env не должен ломать запуск
        LOGGER.warning("Не удалось загрузить .env: %s", exc)


def _dump_glossary(glossary: Mapping[str, str]) -> str:
    """Стабильная JSON-сериализация глоссария.

    Ключи сортируются, чтобы system-префикс был побайтово одинаковым между
    запросами — иначе кэш провайдера не сработает.

    Args:
        glossary: Словарь замен.

    Returns:
        str: JSON без ASCII-экранирования.

    Raises:
        Ничего не бросает.
    """
    ordered = {
        str(key): str(value)
        for key, value in sorted(glossary.items(), key=lambda kv: str(kv[0]))
    }
    return json.dumps(ordered, ensure_ascii=False)


def _dump_exceptions(exceptions: Mapping[str, Any]) -> str:
    """Стабильная JSON-сериализация исключений (только ключи, отсортированы).

    Args:
        exceptions: Отображение токенов (значения игнорируются).

    Returns:
        str: JSON-массив строк.

    Raises:
        Ничего не бросает.
    """
    return json.dumps(sorted(str(key) for key in exceptions), ensure_ascii=False)


def _split_template(template: str, path: Path) -> str:
    """Отделить неизменяемую часть шаблона (system) от места вставки текста.

    Args:
        template: Содержимое ``system_prompt.txt``.
        path: Путь к шаблону (для сообщения об ошибке).

    Returns:
        str: Всё, что стоит до ``{batch_text}``.

    Raises:
        DeepSeekConfigError: в шаблоне нет плейсхолдера ``{batch_text}``.
    """
    head, separator, _tail = template.partition("{batch_text}")
    if not separator:
        raise DeepSeekConfigError(
            f"Шаблон промпта должен содержать {{batch_text}}: {path}"
        )
    return head


def estimate_input_tokens(
    glossary: Mapping[str, str],
    exceptions: Mapping[str, Any],
    texts: Sequence[str],
    batch_size: int,
    prompt_path: str | Path | None = None,
) -> dict[str, int]:
    """Оценить входные токены LLM без обращения к API.

    Считает по формуле из ТЗ: ``(len(glossary_json) + len(exceptions_json) +
    len(rules)) // 3`` на батч, плюс ``sum(len(text)) // 3`` на весь текст.

    Args:
        glossary: Словарь замен.
        exceptions: Токены-исключения.
        texts: Тексты блоков, которые уйдут в LLM.
        batch_size: Размер батча (сколько блоков на запрос).
        prompt_path: Путь к шаблону промпта; ``None`` — шаблон проекта.

    Returns:
        dict[str, int]: ``{"estimated_input_tokens": N, "estimated_batches": K}``.

    Raises:
        DeepSeekConfigError: шаблон не читается или в нём нет ``{batch_text}``.
        ValueError: ``batch_size`` меньше 1.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size должен быть >= 1, получено {batch_size}")

    path = Path(prompt_path) if prompt_path else DEFAULT_PROMPT_PATH
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeepSeekConfigError(f"Не удалось прочитать шаблон промпта {path}: {exc}") from exc

    head = _split_template(template, path)
    glossary_json = _dump_glossary(glossary)
    exceptions_json = _dump_exceptions(exceptions)
    try:
        rules_text = head.format(exceptions_json="", glossary_json="")
    except (KeyError, IndexError, ValueError) as exc:
        raise DeepSeekConfigError(f"Некорректный шаблон промпта {path}: {exc}") from exc

    per_batch = (len(glossary_json) + len(exceptions_json) + len(rules_text)) // 3
    batches = math.ceil(len(texts) / batch_size) if texts else 0
    text_tokens = sum(len(text) for text in texts) // 3

    return {
        "estimated_input_tokens": per_batch * batches + text_tokens,
        "estimated_batches": batches,
    }


class DeepSeekClient:
    """Клиент DeepSeek API для одного батча текста.

    Пример:
        >>> client = DeepSeekClient()                     # ключ из .env
        >>> client.replace_text("Our feedback", {"feedback": "обратная связь"}, {"KPI": None})
        'Our обратная связь'

    Args:
        api_key: Ключ API. Если пусто/``None`` — берётся ``DEEPSEEK_API_KEY``
            из окружения или ``.env``.
        base_url: Базовый URL OpenAI-совместимого API; пустое значение — из
            ``.env`` или ``https://api.deepseek.com``.
        model: Имя модели.
        timeout: Таймаут одного запроса, секунды.
        max_retries: Число повторов при транспортных/HTTP-ошибках.
        retry_backoff: Базовая пауза экспоненциального backoff, секунды.
        max_validation_attempts: Сколько раз запрашивать ответ, если он
            непригоден (пустой / эхо промпта / markdown).
        prompt_path: Путь к шаблону промпта.
        client: Готовый OpenAI-совместимый клиент (для тестов).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        max_validation_attempts: int = DEFAULT_MAX_VALIDATION_ATTEMPTS,
        prompt_path: str | Path | None = None,
        client: Any | None = None,
        sanitize: bool = True,
    ) -> None:
        """Создать клиент DeepSeek и проверить его конфигурацию.

        Args:
            api_key: Ключ API или ``None`` для чтения из окружения/``.env``.
            base_url: Базовый URL API или ``None`` для значения по умолчанию.
            model: Имя модели.
            timeout: Таймаут запроса, секунды.
            max_retries: Число повторов при ``openai.APIError``.
            retry_backoff: Базовая пауза backoff, секунды.
            max_validation_attempts: Число попыток получить пригодный ответ.
            prompt_path: Путь к шаблону промпта.
            client: Готовый клиент (dependency injection для тестов).
            sanitize: Обезвреживать prompt-injection в тексте блока
                (``--no-sanitize`` в CLI выключает).

        Returns:
            None

        Raises:
            DeepSeekConfigError: не задан ключ API, шаблон промпта отсутствует
                или клиент OpenAI не создаётся.
            ValueError: некорректные ``max_retries`` / ``max_validation_attempts``
                / ``timeout``.
        """
        if max_retries < 0:
            raise ValueError(f"max_retries не может быть отрицательным: {max_retries}")
        if max_validation_attempts < 1:
            raise ValueError(
                f"max_validation_attempts должен быть >= 1: {max_validation_attempts}"
            )
        if timeout <= 0:
            raise ValueError(f"timeout должен быть положительным: {timeout}")

        _load_env()

        resolved_key = (api_key or os.getenv("DEEPSEEK_API_KEY", "")).strip()
        if not resolved_key:
            raise DeepSeekConfigError(
                "DEEPSEEK_API_KEY не задан: передайте api_key или заполните .env"
            )

        self._api_key = resolved_key
        self._base_url = (
            base_url or os.getenv("DEEPSEEK_BASE_URL", "").strip() or DEFAULT_BASE_URL
        )
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._max_validation_attempts = max_validation_attempts
        self._prompt_path = Path(prompt_path) if prompt_path else DEFAULT_PROMPT_PATH
        self._sanitize = bool(sanitize)
        #: Сведения о последней санитизации (для отчёта CLI); ``None`` — правок не было.
        self.last_sanitization: dict[str, Any] | None = None
        #: Накопленный расход токенов за сессию (см. :meth:`usage_summary`).
        self._usage_totals: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "requests": 0,
        }
        #: ``True``, если хотя бы один ответ содержал cache-поля.
        self._cache_reported = False

        if not self._prompt_path.exists():
            raise DeepSeekConfigError(f"Шаблон промпта не найден: {self._prompt_path}")

        if client is not None:
            self._client = client
        else:
            try:
                self._client = OpenAI(
                    api_key=self._api_key,
                    base_url=self._base_url,
                    timeout=self._timeout,
                    max_retries=0,  # повторы делает сам клиент, без двойного счёта
                )
            except openai.OpenAIError as exc:
                raise DeepSeekConfigError(f"Не удалось создать клиент OpenAI: {exc}") from exc

    # -- публичные свойства --------------------------------------------------

    @property
    def model(self) -> str:
        """Имя модели, используемой в запросах.

        Returns:
            str: например, ``"deepseek-chat"``.

        Raises:
            Ничего не бросает.
        """
        return self._model

    @property
    def base_url(self) -> str:
        """Базовый URL API.

        Returns:
            str: например, ``"https://api.deepseek.com"``.

        Raises:
            Ничего не бросает.
        """
        return self._base_url

    @property
    def prompt_path(self) -> Path:
        """Путь к используемому шаблону промпта.

        Returns:
            Path: абсолютный путь к ``system_prompt.txt``.

        Raises:
            Ничего не бросает.
        """
        return self._prompt_path


    # -- основной сценарий ---------------------------------------------------

    def replace_text(
        self,
        text: str,
        glossary: Mapping[str, str],
        exceptions: Mapping[str, Any],
        log_callback: Callable[[str, str], None] | None = None,
        block_id: int | str | None = None,
    ) -> str:
        """Отправить один батч текста в DeepSeek и вернуть очищенный ответ.

        Args:
            text: Исходный текст блока (абзац, заголовок, ячейка).
            glossary: Словарь обязательных замен ``{термин: замена}``.
            exceptions: Токены, которые запрещено менять (значения игнорируются).
            log_callback: Необязательный ``callable(level: str, msg: str)``;
                получает расход токенов и причины повторов.
            block_id: Идентификатор блока для предупреждений о санитизации.

        Returns:
            str: Текст от модели без markdown-обёрток и преамбул. При пустом
            ``text`` возвращается он же без обращения к API.

        Raises:
            TypeError: аргументы не тех типов.
            DeepSeekConfigError: шаблон промпта не читается или битый.
            DeepSeekAPIError: транспортная/HTTP-ошибка после всех повторов.
            DeepSeekValidationError: модель так и не вернула пригодный текст.
        """
        if not isinstance(text, str):
            raise TypeError(f"text должен быть строкой, получено {type(text).__name__}")
        if not isinstance(glossary, Mapping):
            raise TypeError(
                f"glossary должен быть отображением, получено {type(glossary).__name__}"
            )
        if not isinstance(exceptions, Mapping):
            raise TypeError(
                f"exceptions должен быть отображением, получено {type(exceptions).__name__}"
            )

        self.last_sanitization = None
        if not text.strip():
            self._log(log_callback, "debug", "пустой текст: запрос не отправляется")
            return text

        user_text = text
        if self._sanitize:
            user_text = self._sanitize_user_text(
                text, block_id=block_id, log_callback=log_callback
            )
        messages = self._render_messages(glossary, exceptions, user_text)
        self._log(
            log_callback,
            "debug",
            "оценка размера промпта ≈ "
            f"{self._estimate_tokens(messages[0]['content'] + messages[1]['content'])} токенов",
        )

        last_problem = "ответ не получен"
        for attempt in range(1, self._max_validation_attempts + 1):
            raw = self._request_with_retries(messages, log_callback)
            try:
                cleaned = self._validate(raw, text)
            except DeepSeekValidationError as exc:
                last_problem = str(exc)
                self._log(
                    log_callback,
                    "warning",
                    f"попытка {attempt}/{self._max_validation_attempts}: {exc}",
                )
                continue
            self._log(log_callback, "info", f"модель вернула {len(cleaned)} символов")
            return self._restore_markers(cleaned)

        raise DeepSeekValidationError(
            "Не удалось получить пригодный ответ после "
            f"{self._max_validation_attempts} попыток: {last_problem}"
        )

    # -- транспорт -----------------------------------------------------------

    def _request_with_retries(
        self,
        messages: Sequence[Mapping[str, str]],
        log_callback: Callable[[str, str], None] | None,
    ) -> str:
        """Выполнить запрос к API с повторами при ``openai.APIError``.

        Args:
            messages: Готовые сообщения (system + user) — system одинаков
                между запросами, поэтому провайдер кэширует префикс.
            log_callback: Необязательный обработчик сообщений.

        Returns:
            str: Сырой текст ответа модели (может быть пустым — это проверит
            вызывающий код).

        Raises:
            DeepSeekAPIError: все попытки исчерпаны либо ошибка не транспортная.
        """
        attempts = self._max_retries + 1
        delay = self._retry_backoff
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=TEMPERATURE,
                    timeout=self._timeout,
                )
            except openai.APIError as exc:
                last_error = exc
                # Лог в stderr: без него сбой запроса виден только как «ничего
                # не произошло» — GUI не имеет своего консольного вывода.
                LOGGER.error(
                    "DeepSeek API: %s (попытка %d/%d): %s",
                    type(exc).__name__,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt >= attempts:
                    break
                self._log(
                    log_callback,
                    "warning",
                    f"API-ошибка ({type(exc).__name__}): {exc}; "
                    f"повтор {attempt}/{self._max_retries} через {delay:.1f} с",
                )
                if delay > 0:
                    time.sleep(delay)
                delay *= 2
                continue
            except Exception as exc:  # noqa: BLE001 - границы модуля
                LOGGER.exception("DeepSeek: непредвиденная ошибка запроса")
                raise DeepSeekAPIError(
                    f"Неожиданная ошибка при запросе к DeepSeek: {exc}"
                ) from exc

            self._log_usage(response, log_callback)
            return self._extract_content(response)

        LOGGER.error(
            "DeepSeek API недоступен после %d попыток: %s",
            attempts,
            last_error,
            exc_info=last_error,
        )
        raise DeepSeekAPIError(
            f"DeepSeek API недоступен после {attempts} попыток: {last_error}",
            attempts=attempts,
        ) from last_error


    # -- разбор и валидация ответа -------------------------------------------

    @staticmethod
    def _extract_content(response: Any) -> str:
        """Достать текст первого choice из ответа API.

        Args:
            response: Ответ ``openai`` (``ChatCompletion`` или его аналог).

        Returns:
            str: Содержимое сообщения; пустая строка, если его нет.

        Raises:
            DeepSeekValidationError: у ответа нет ``choices``.
        """
        try:
            choices = response.choices
        except AttributeError as exc:
            raise DeepSeekValidationError("Ответ API не содержит choices") from exc
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        return content if isinstance(content, str) else ""

    def _log_usage(
        self,
        response: Any,
        log_callback: Callable[[str, str], None] | None,
    ) -> None:
        """Учесть и залогировать расход токенов из ``response.usage``.

        Args:
            response: Ответ API.
            log_callback: Необязательный обработчик сообщений.

        Returns:
            None

        Raises:
            Ничего не бросает: отсутствие ``usage`` просто не логируется.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        cache_hit = getattr(usage, "prompt_cache_hit_tokens", None)
        cache_miss = getattr(usage, "prompt_cache_miss_tokens", None)
        if cache_hit is None and cache_miss is None:
            # OpenAI-совместимый вариант этого же показателя.
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cache_hit = getattr(details, "cached_tokens", None)
        if prompt_tokens is None and completion_tokens is None and cache_hit is None:
            return

        self._accumulate_usage(prompt_tokens, completion_tokens, cache_hit, cache_miss)

        parts = [
            f"prompt_tokens={prompt_tokens}",
            f"completion_tokens={completion_tokens}",
        ]
        if cache_hit is not None:
            parts.append(f"cache_hit={cache_hit}")
        if cache_miss is not None:
            parts.append(f"cache_miss={cache_miss}")
        self._log(log_callback, "info", "DeepSeek usage: " + ", ".join(parts))

    def _accumulate_usage(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        cache_hit: int | None,
        cache_miss: int | None,
    ) -> None:
        """Добавить показатели одного ответа к сессионным итогам.

        Args:
            prompt_tokens: Токены промпта или ``None``.
            completion_tokens: Токены ответа или ``None``.
            cache_hit: Токены из кэша или ``None`` (поле не вернули).
            cache_miss: Токены вне кэша или ``None``.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._usage_totals["requests"] += 1
        self._usage_totals["prompt_tokens"] += int(prompt_tokens or 0)
        self._usage_totals["completion_tokens"] += int(completion_tokens or 0)
        if cache_hit is not None or cache_miss is not None:
            self._cache_reported = True
            self._usage_totals["cache_hit_tokens"] += int(cache_hit or 0)
            self._usage_totals["cache_miss_tokens"] += int(cache_miss or 0)

    def reset_usage(self) -> None:
        """Сбросить накопленные показатели расхода токенов.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        for key in self._usage_totals:
            self._usage_totals[key] = 0
        self._cache_reported = False

    def usage_summary(self) -> dict[str, Any]:
        """Итог расхода токенов за сессию для ``--json``-квитанции.

        ``cache_hit_ratio`` = ``hit / (hit + miss)`` с точностью до 3 знаков.
        Если API не возвращал cache-поля (старые прокси, ``deepseek-reasoner``)
        или запросов не было — ``None``: ноль здесь означал бы «кэш не работал»,
        хотя на самом деле показатель неизвестен.

        Returns:
            dict[str, Any]: ``prompt_tokens``, ``completion_tokens``,
            ``cache_hit_tokens``, ``cache_miss_tokens``, ``cache_hit_ratio``,
            ``requests``.

        Raises:
            Ничего не бросает.
        """
        totals = dict(self._usage_totals)
        cache_total = totals["cache_hit_tokens"] + totals["cache_miss_tokens"]
        ratio: float | None = None
        if self._cache_reported and cache_total > 0:
            ratio = round(totals["cache_hit_tokens"] / cache_total, 3)
        return {
            "prompt_tokens": totals["prompt_tokens"],
            "completion_tokens": totals["completion_tokens"],
            "cache_hit_tokens": totals["cache_hit_tokens"],
            "cache_miss_tokens": totals["cache_miss_tokens"],
            "cache_hit_ratio": ratio,
            "requests": totals["requests"],
        }

    def _validate(self, raw: str, source_text: str) -> str:
        """Проверить сырой ответ и вернуть очищенный текст.

        Args:
            raw: Сырой ответ модели.
            source_text: Исходный текст блока (для различения эха промпта и
                легального упоминания маркеров).

        Returns:
            str: Очищенный текст ответа.

        Raises:
            DeepSeekValidationError: ответ пуст, содержит эхо промпта или
                markdown-обёртку, либо после очистки не содержит текста.
        """
        if not isinstance(raw, str) or not raw.strip():
            raise DeepSeekValidationError("пустой ответ модели")

        problem = self._find_artifact(raw, source_text)
        if problem is not None:
            raise DeepSeekValidationError(problem)

        cleaned = self._clean_response(raw, strip_fences=_FENCE not in source_text)
        if not cleaned.strip():
            raise DeepSeekValidationError("после очистки ответ пуст")
        return cleaned

    @staticmethod
    def _find_artifact(raw: str, source_text: str) -> str | None:
        """Найти в ответе артефакты, которых не было в исходном тексте.

        Args:
            raw: Сырой ответ модели.
            source_text: Исходный текст блока.

        Returns:
            str | None: Описание проблемы либо ``None``, если ответ чистый.

        Raises:
            Ничего не бросает.
        """
        for marker in _ECHO_MARKERS:
            if marker in raw and marker not in source_text:
                return f"ответ содержит эхо промпта ({marker!r})"
        if _FENCE in raw and _FENCE not in source_text:
            return "ответ содержит markdown-обёртку (```)"
        return None


    # -- санитизация пользовательского текста --------------------------------

    def _sanitize_user_text(
        self,
        text: str,
        block_id: int | str | None = None,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> str:
        """Обезвредить prompt-injection в тексте документа.

        Маркеры ``EXCEPTIONS:`` / ``GLOSSARY:`` / ``Текст:`` разрываются
        zero-width space после первого символа (``E\\u200bXCEPTIONS:``): для
        человека строка выглядит так же, но модель не принимает её за раздел
        промпта. Конструкции ``ignore previous instructions`` заменяются на
        ``[удалено]``. Содержимое ```...``` и ``...`` остаётся нетронутым.

        Args:
            text: Текст блока или собранный батч.
            block_id: Идентификатор блока (попадает в предупреждение).
            log_callback: Необязательный обработчик сообщений.

        Returns:
            str: Санитизированный текст; если правок не было — исходный объект.

        Raises:
            Ничего не бросает.
        """
        if not isinstance(text, str) or not text:
            return text

        escaped = 0
        removed = 0

        def _transform(chunk: str) -> str:
            """Обезвредить один фрагмент текста вне кода.

            Args:
                chunk: Участок текста без ```...``` и ``...``.

            Returns:
                str: Фрагмент с экранированными маркерами и вырезанными
                инструкциями.

            Raises:
                Ничего не бросает.
            """
            nonlocal escaped, removed
            if not chunk:
                return chunk

            result = chunk
            # Справа налево, чтобы вставки не сдвигали ещё не обработанные позиции.
            for match in reversed(list(_PROMPT_MARKER_RE.finditer(result))):
                position = match.start() + 1
                result = result[:position] + _ZWSP + result[position:]
                escaped += 1

            result, count = _IGNORE_INSTRUCTION_RE.subn("[удалено]", result)
            removed += count
            return result

        sanitized = _map_outside_code(text, _transform)
        if escaped or removed:
            self.last_sanitization = {
                "block_id": block_id,
                "escaped_markers": escaped,
                "removed_instructions": removed,
            }
            self._log(
                log_callback,
                "warning",
                f"Санитизация текста (block_id={block_id}): "
                f"маркеров экранировано — {escaped}, инструкций удалено — {removed}",
            )
        return sanitized

    @staticmethod
    def _restore_markers(text: str) -> str:
        """Снять служебные ZWSP, если модель переписала экранированный маркер.

        Без этого «невидимый» разделитель попал бы в документ: пользователь его
        не увидит, но поиск и копирование по «EXCEPTIONS:» перестанут работать.

        Args:
            text: Очищенный ответ модели.

        Returns:
            str: Текст без ZWSP внутри маркеров; остальные ZWSP сохраняются.

        Raises:
            Ничего не бросает.
        """
        if _ZWSP not in text:
            return text
        return _ESCAPED_MARKER_RE.sub(lambda match: match.group(1), text)

    # -- шаблон промпта ------------------------------------------------------

    def _load_template(self) -> str:
        """Прочитать шаблон промпта с диска.

        Returns:
            str: Содержимое ``system_prompt.txt``.

        Raises:
            DeepSeekConfigError: файл не читается или пуст.
        """
        try:
            template = self._prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DeepSeekConfigError(
                f"Не удалось прочитать шаблон промпта {self._prompt_path}: {exc}"
            ) from exc
        if not template.strip():
            raise DeepSeekConfigError(f"Шаблон промпта пуст: {self._prompt_path}")
        return template

    def _render_system(
        self,
        glossary: Mapping[str, str],
        exceptions: Mapping[str, Any],
    ) -> str:
        """Собрать неизменяемый system-блок — кэшируемый префикс промпта.

        Всё, что стоит в шаблоне до ``{batch_text}``: роль, правила, EXCEPTIONS
        и GLOSSARY. Текст одинаков между запросами, поэтому DeepSeek кэширует
        префикс и списывает за него примерно в 10 раз меньше.

        Args:
            glossary: Словарь замен.
            exceptions: Токены-исключения (в промпт идут только ключи).

        Returns:
            str: Готовый system-текст.

        Raises:
            DeepSeekConfigError: шаблон не читается, в нём нет ``{batch_text}``
                или он содержит некорректные фигурные скобки.
        """
        template = self._load_template()
        head = _split_template(template, self._prompt_path)
        try:
            return head.format(
                exceptions_json=_dump_exceptions(exceptions),
                glossary_json=_dump_glossary(glossary),
            )
        except (KeyError, IndexError, ValueError) as exc:
            raise DeepSeekConfigError(
                f"Некорректный шаблон промпта {self._prompt_path}: {exc}"
            ) from exc

    def _render_messages(
        self,
        glossary: Mapping[str, str],
        exceptions: Mapping[str, Any],
        batch_text: str,
    ) -> list[dict]:
        """Собрать сообщения: стабильный system + только текст в user.

        Args:
            glossary: Словарь замен.
            exceptions: Токены-исключения.
            batch_text: Текст блока или батча (уже санитизированный).

        Returns:
            list[dict]: ``[{"role": "system", ...}, {"role": "user", ...}]``.

        Raises:
            DeepSeekConfigError: шаблон промпта некорректен.
        """
        return [
            {"role": "system", "content": self._render_system(glossary, exceptions)},
            {"role": "user", "content": batch_text},
        ]

    # -- очистка ответа ------------------------------------------------------

    def _clean_response(self, raw: str, *, strip_fences: bool = True) -> str:
        """Убрать markdown-обёртки и преамбулы из ответа модели.

        Args:
            raw: Сырой ответ модели.
            strip_fences: Снимать ли внешние ```-обёртки (для документов, где
                backticks легальны, передаётся ``False``).

        Returns:
            str: Очищенный текст без преамбул вида «Вот результат:».

        Raises:
            TypeError: если ``raw`` не строка.
        """
        if not isinstance(raw, str):
            raise TypeError(f"raw должен быть строкой, получено {type(raw).__name__}")

        text = raw.strip()
        for _ in range(3):
            before = text
            if strip_fences:
                text = self._strip_fences(text).strip()
            text = _PREAMBLE_RE.sub("", text, count=1).strip()
            if text == before:
                break
        return text

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Снять ```-обёртку, охватывающую весь ответ.

        Args:
            text: Текст ответа.

        Returns:
            str: Текст без внешних fences; внутренние блоки кода сохраняются.

        Raises:
            Ничего не бросает.
        """
        stripped = text.strip()
        if not stripped.startswith(_FENCE):
            return text

        body = stripped[len(_FENCE) :]
        newline = body.find("\n")
        if newline != -1:
            first_line = body[:newline].strip()
            if not first_line or re.fullmatch(r"[\w.+-]*", first_line):
                body = body[newline + 1 :]
        if body.rstrip().endswith(_FENCE):
            body = body.rstrip()[: -len(_FENCE)]
        return body.strip("\n")

    def _estimate_tokens(self, text: str) -> int:
        """Грубо оценить число токенов для смешанного ru/en текста.

        Args:
            text: Текст для оценки.

        Returns:
            int: ``len(text) // 3``; для пустой строки — 0.

        Raises:
            TypeError: если ``text`` не строка.
        """
        if not isinstance(text, str):
            raise TypeError(f"text должен быть строкой, получено {type(text).__name__}")
        return len(text) // 3

    # -- логирование ---------------------------------------------------------

    def _log(
        self,
        log_callback: Callable[[str, str], None] | None,
        level: str,
        message: str,
    ) -> None:
        """Записать сообщение в ``logging`` и в ``log_callback``.

        Args:
            log_callback: Внешний обработчик или ``None``.
            level: ``"debug"``, ``"info"``, ``"warning"`` или ``"error"``.
            message: Текст сообщения.

        Returns:
            None

        Raises:
            Ничего не бросает: исключение внутри ``log_callback`` логируется и
            гасится, чтобы сбой колбэка не ломал обработку.
        """
        getattr(LOGGER, level, LOGGER.info)("%s", message)
        if log_callback is None:
            return
        try:
            log_callback(level, message)
        except Exception as exc:  # noqa: BLE001 - колбэк внешний, не ломает обработку
            LOGGER.warning("log_callback завершился ошибкой: %s", exc)






