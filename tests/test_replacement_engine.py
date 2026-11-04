"""Тесты :class:`src.replacement_engine.ReplacementEngine`.

Фикстуры глоссария создаются в ``tmp_path``, чтобы тесты не зависели от
содержимого ``data/``. Отдельный тест проверяет, что проектными файлами
(плоская схема) менеджер тоже пользуется корректно.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.glossary_manager import GlossaryError, GlossaryManager
from src.replacement_engine import ReplacementEngine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict) -> None:
    """Записать JSON в UTF-8 без ASCII-экранирования."""
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def manager(tmp_path: Path) -> GlossaryManager:
    """Глоссарий с терминами разной длины и исключениями KPI/TLS."""
    glossary = tmp_path / "glossary.json"
    exceptions = tmp_path / "exceptions.json"
    _write_json(
        glossary,
        {
            "feedback": "обратная связь",
            "roadmap": "план развития",
            "product roadmap": "план развития продукта",
            "kpi": "ключевой показатель",
            "meeting": "встреча",
        },
    )
    _write_json(exceptions, {"KPI": None, "TLS": None})
    return GlossaryManager(str(glossary), str(exceptions))


@pytest.fixture()
def engine(manager: GlossaryManager) -> ReplacementEngine:
    """Движок в словарном режиме (без LLM)."""
    return ReplacementEngine(manager)


def test_case_preservation(engine: ReplacementEngine) -> None:
    """Регистр первой буквы и ALL CAPS переносятся на замену."""
    text = "Feedback, FEEDBACK and feedback."

    new_text, changes = engine.replace_dict_only(text)

    assert new_text == "Обратная связь, ОБРАТНАЯ СВЯЗЬ and обратная связь."
    assert [change["replacement"] for change in changes] == [
        "Обратная связь",
        "ОБРАТНАЯ СВЯЗЬ",
        "обратная связь",
    ]


def test_word_boundary(engine: ReplacementEngine) -> None:
    """Подстроки не заменяются: feedbackloop остаётся как есть."""
    text = "feedbackloop and feedback."

    new_text, changes = engine.replace_dict_only(text)

    assert new_text == "feedbackloop and обратная связь."
    assert "обратная связьloop" not in new_text
    assert len(changes) == 1
    assert changes[0]["position"] == len("feedbackloop and ")


def test_exception_priority(engine: ReplacementEngine) -> None:
    """KPI есть и в глоссарии, и в исключениях — замены нет ни в каком регистре."""
    text = "KPI, kpi and Kpi stay; meeting changes."

    new_text, changes = engine.replace_dict_only(text)

    assert "ключевой показатель" not in new_text
    assert new_text == "KPI, kpi and Kpi stay; встреча changes."
    assert [change["original"] for change in changes] == ["meeting"]


def test_longer_term_first(engine: ReplacementEngine) -> None:
    """product roadmap заменяется целиком, а не по частям."""
    text = "Our product roadmap is ready."

    new_text, changes = engine.replace_dict_only(text)

    assert new_text == "Our план развития продукта is ready."
    assert "product план развития" not in new_text
    assert len(changes) == 1
    assert changes[0]["original"] == "product roadmap"
    assert changes[0]["position"] == 4


def test_change_fields_and_positions(engine: ReplacementEngine) -> None:
    """Каждый change содержит нужные поля, а position указывает в исходный текст."""
    text = "Feedback at the meeting, then feedback again."

    new_text, changes = engine.replace_dict_only(text)

    assert len(changes) == 3
    for change in changes:
        assert set(change) == {"original", "replacement", "source", "position"}
        assert change["source"] == "dict"
        start = change["position"]
        assert text[start : start + len(change["original"])] == change["original"]

    assert [change["position"] for change in changes] == sorted(
        change["position"] for change in changes
    )
    assert new_text == "Обратная связь at the встреча, then обратная связь again."


def test_replacement_containing_term_does_not_loop(tmp_path: Path) -> None:
    """Замена, содержащая сам термин, не приводит к повторной обработке."""
    glossary = tmp_path / "g.json"
    exceptions = tmp_path / "e.json"
    _write_json(glossary, {"meeting": "встреча (meeting)"})
    _write_json(exceptions, {})
    engine = ReplacementEngine(GlossaryManager(str(glossary), str(exceptions)))

    new_text, changes = engine.replace_dict_only("meeting")

    assert new_text == "встреча (meeting)"
    assert len(changes) == 1


def test_no_cascade_into_inserted_text(tmp_path: Path) -> None:
    """Вставленная замена не сканируется повторно другим термином."""
    glossary = tmp_path / "g.json"
    exceptions = tmp_path / "e.json"
    _write_json(glossary, {"roadmap": "план", "план": "схема"})
    _write_json(exceptions, {})
    engine = ReplacementEngine(GlossaryManager(str(glossary), str(exceptions)))

    new_text, changes = engine.replace_dict_only("roadmap")

    assert new_text == "план"
    assert len(changes) == 1


def test_get_replacements_filters_exceptions(manager: GlossaryManager) -> None:
    """get_replacements возвращает только присутствующие не-исключения."""
    found = manager.get_replacements("KPI and a meeting")

    assert "kpi" not in found
    assert found == {"meeting": "встреча"}
    assert list(found) == ["meeting"]


def test_reload_picks_up_changes(tmp_path: Path) -> None:
    """reload() подхватывает новый термин, движок видит его без пересоздания."""
    glossary = tmp_path / "g.json"
    exceptions = tmp_path / "e.json"
    _write_json(glossary, {"feedback": "обратная связь"})
    _write_json(exceptions, {})
    manager = GlossaryManager(str(glossary), str(exceptions))
    engine = ReplacementEngine(manager)
    assert engine.replace_dict_only("deadline") == ("deadline", [])

    _write_json(glossary, {"feedback": "обратная связь", "deadline": "крайний срок"})
    manager.reload()

    new_text, changes = engine.replace_dict_only("deadline")
    assert new_text == "крайний срок"
    assert len(changes) == 1


def test_invalid_json_raises(tmp_path: Path) -> None:
    """Битый JSON даёт GlossaryError с указанием файла."""
    glossary = tmp_path / "g.json"
    exceptions = tmp_path / "e.json"
    glossary.write_text("{not json", encoding="utf-8")
    _write_json(exceptions, {})

    with pytest.raises(GlossaryError):
        GlossaryManager(str(glossary), str(exceptions))


def test_invalid_glossary_value_raises(tmp_path: Path) -> None:
    """Нестроковая замена нарушает схему."""
    glossary = tmp_path / "g.json"
    exceptions = tmp_path / "e.json"
    _write_json(glossary, {"feedback": 42})
    _write_json(exceptions, {})

    with pytest.raises(GlossaryError):
        GlossaryManager(str(glossary), str(exceptions))


def test_project_data_files_work() -> None:
    """Проектные data/*.json читаются и дают ожидаемые замены."""
    manager = GlossaryManager(
        str(PROJECT_ROOT / "data" / "glossary.json"),
        str(PROJECT_ROOT / "data" / "exceptions.json"),
    )
    engine = ReplacementEngine(manager)

    assert "feedback" in manager.glossary
    assert manager.is_exception("kpi")

    new_text, changes = engine.replace_dict_only("Feedback on the product roadmap")

    assert new_text == "Обратная связь on the дорожная карта продукта"
    assert len(changes) == 2


def test_composite_term_milestone_owner_replaced_whole() -> None:
    """Составной термин «milestone owner» заменяется целиком, а не по частям."""
    manager = GlossaryManager(
        str(PROJECT_ROOT / "data" / "glossary.json"),
        str(PROJECT_ROOT / "data" / "exceptions.json"),
    )
    engine = ReplacementEngine(manager)

    new_text, changes = engine.replace_dict_only("milestone owner")

    assert new_text == "владелец контрольной точки"
    assert len(changes) == 1, changes
    assert changes[0]["original"] == "milestone owner"
    assert changes[0]["replacement"] == "владелец контрольной точки"

    # Отдельные термины не потеряны — для других контекстов.
    assert engine.replace_dict_only("milestone")[0] == "контрольная точка"
    assert engine.replace_dict_only("owner")[0] == "владелец"


def test_project_glossary_covers_it_and_business() -> None:
    """Проектный словарь большой, офлайн-переводы официальные, исключения не задеты."""
    manager = GlossaryManager(
        str(PROJECT_ROOT / "data" / "glossary.json"),
        str(PROJECT_ROOT / "data" / "exceptions.json"),
    )
    engine = ReplacementEngine(manager)

    assert len(manager.glossary) >= 150, len(manager.glossary)

    expected = {
        "feature": "функциональность",
        "bug": "ошибка",
        "deploy": "развертывание",
        "pull request": "пулл-реквест",
        "sprint": "спринт",
        "backlog": "бэклог",
        "revenue": "выручка",
        "churn": "отток",
        "conversion": "конверсия",
        "target": "цель",
    }
    for term, target in expected.items():
        assert manager.glossary.get(term) == target, (term, manager.glossary.get(term))

    # В словаре не должно быть терминов, которые объявлены исключениями.
    clashes = sorted(term for term in manager.glossary if manager.is_exception(term))
    assert not clashes, clashes

    # Аббревиатуры остаются как есть, а соседние термины переводятся.
    text, changes = engine.replace_dict_only("Sprint by API: KPI and TLS unchanged, bug fixed")
    assert text == "Спринт by API: KPI and TLS unchanged, ошибка fixed", text
    assert [change["original"] for change in changes] == ["Sprint", "bug"], changes


def test_empty_text_and_type_errors(engine: ReplacementEngine) -> None:
    """Пустой текст — без изменений; не-строка — TypeError."""
    assert engine.replace_dict_only("") == ("", [])
    with pytest.raises(TypeError):
        engine.replace_dict_only(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        engine._glossary.get_replacements(None)  # type: ignore[arg-type]


def test_replace_with_llm_not_implemented(engine: ReplacementEngine) -> None:
    """LLM-режим пока заглушка этапа 3."""
    assert engine.has_llm_client is False
    with pytest.raises(NotImplementedError):
        engine.replace_with_llm("feedback")

