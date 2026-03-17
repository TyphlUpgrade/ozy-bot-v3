"""
tests/test_direction.py
=======================
Tests for ozymandias.core.direction — canonical direction type and mappings.
"""
from __future__ import annotations

import logging

import pytest

from ozymandias.core.direction import (
    ACTION_TO_DIRECTION,
    ENTRY_SIDE,
    EXIT_SIDE,
    direction_from_action,
    is_short,
)


class TestActionToDirection:
    def test_buy_maps_to_long(self):
        assert ACTION_TO_DIRECTION["buy"] == "long"

    def test_sell_short_maps_to_short(self):
        assert ACTION_TO_DIRECTION["sell_short"] == "short"


class TestDirectionFromAction:
    def test_buy_returns_long(self):
        assert direction_from_action("buy") == "long"

    def test_sell_short_returns_short(self):
        assert direction_from_action("sell_short") == "short"

    def test_unknown_action_returns_long(self):
        assert direction_from_action("buy_to_cover") == "long"

    def test_unknown_action_emits_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="ozymandias.core.direction"):
            direction_from_action("buy_to_cover")
        assert "buy_to_cover" in caplog.text

    def test_unknown_action_does_not_raise(self):
        # Must not raise regardless of input
        result = direction_from_action("completely_unknown_action_xyz")
        assert result == "long"


class TestSideInverses:
    def test_entry_and_exit_sides_are_inverses_long(self):
        assert ENTRY_SIDE["long"] != EXIT_SIDE["long"]

    def test_entry_and_exit_sides_are_inverses_short(self):
        assert ENTRY_SIDE["short"] != EXIT_SIDE["short"]


class TestRoundTrips:
    def test_long_round_trip(self):
        direction = direction_from_action("buy")
        assert ENTRY_SIDE[direction] == "buy"
        assert EXIT_SIDE[direction] == "sell"

    def test_short_round_trip(self):
        direction = direction_from_action("sell_short")
        assert ENTRY_SIDE[direction] == "sell"
        assert EXIT_SIDE[direction] == "buy"


class TestIsShort:
    def test_is_short_true_for_short(self):
        assert is_short("short") is True

    def test_is_short_false_for_long(self):
        assert is_short("long") is False
