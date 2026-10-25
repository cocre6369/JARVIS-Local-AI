"""Memory persistence and screen-understanding primitives."""

from __future__ import annotations

import pytest

from jarvis.memory.store import MemoryStore
from jarvis.platform.base import Rect, UIElement
from jarvis.vision.elements import match_elements


@pytest.fixture()
def memory(tmp_path):
    store = MemoryStore(db_path=tmp_path / "memory.db")
    yield store
    store.close()


def test_facts_round_trip_and_disable(memory):
    memory.add_fact("preference", "coffee", "flat white")
    facts = memory.list_facts()
    assert any(f.key == "coffee" and f.value == "flat white" for f in facts)

    fact = next(f for f in facts if f.key == "coffee")
    memory.set_fact_enabled(fact.id, False)
    assert memory.list_facts() == [] or all(f.key != "coffee"
                                            for f in memory.list_facts())
    enabled_only = [f.key for f in memory.list_facts()]
    assert "coffee" not in enabled_only
    with_disabled = [f.key for f in memory.list_facts(include_disabled=True)]
    assert "coffee" in with_disabled

    stats = memory.stats()
    assert stats["facts_disabled"] >= 1


def test_credentials_are_refused_not_stored(memory):
    memory.add_fact("other", "api_token", "password=Sup3rS3cret!x")
    facts = memory.list_facts(include_disabled=True)
    assert not any("Sup3rS3cret" in (f.value or "") for f in facts), \
        "a credential was stored in memory"


def test_conversation_history_persists(memory):
    session_id = "test-session-1"
    memory.add_message(session_id, "user", "hello")
    memory.add_message(session_id, "assistant", "hi there")
    rows = memory.history(session_id)
    assert [r["role"] for r in rows] == ["user", "assistant"]


def _elements():
    return [
        UIElement(name="Play", control_type="Button", rect=Rect(100, 100, 80, 40),
                  patterns=["click"]),
        UIElement(name="Pause", control_type="Button", rect=Rect(200, 100, 80, 40),
                  patterns=["click"]),
        UIElement(name="Search", control_type="Edit", rect=Rect(300, 20, 200, 30),
                  patterns=["set_value"]),
        UIElement(name="Volume", control_type="Slider", rect=Rect(500, 100, 120, 20)),
        UIElement(name="", control_type="Text", rect=Rect(10, 400, 300, 20),
                  value="Now playing"),
    ]


def test_element_matching_scores_exact_and_fuzzy():
    screen = Rect(0, 0, 1920, 1080)
    exact = match_elements("Play", _elements(), screen=screen)
    assert exact and exact[0].element.name == "Play"
    assert exact[0].score >= 0.95

    fuzzy = match_elements("serch", _elements(), screen=screen)
    assert fuzzy and fuzzy[0].element.name == "Search"

    nothing = match_elements("zzz_qqq_xyz", _elements(), screen=screen,
                             min_score=0.6)
    assert not nothing


def test_editable_controls_preferred_for_value_requests():
    screen = Rect(0, 0, 1920, 1080)
    matches = match_elements("search box", _elements(), screen=screen,
                             require_pattern="set_value")
    assert matches and matches[0].element.control_type == "Edit"


def test_password_elements_are_scored_down_hard():
    """Credential inputs must lose to any other candidate for the same query."""
    from jarvis.vision.elements import score_element

    screen = Rect(0, 0, 1920, 1080)
    plain = UIElement(name="Password", control_type="Edit",
                      rect=Rect(700, 300, 200, 30), patterns=["set_value"])
    secret = UIElement(name="Password", control_type="Edit",
                       rect=Rect(700, 300, 200, 30), is_password=True,
                       patterns=["set_value"])
    plain_score = (score_element("password", plain, screen) or
                   type("M", (), {"score": -1.0})()).score
    secret_score = (score_element("password", secret, screen) or
                    type("M", (), {"score": -1.0})()).score
    assert secret_score < plain_score
    # and the masking layer covers it in every screenshot
    from jarvis.vision import masking

    regions = masking.password_regions([secret])
    assert regions
