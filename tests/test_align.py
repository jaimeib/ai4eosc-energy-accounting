"""Unit tests for main._align_down / main._resolve_extract_to (pointer.align_seconds)."""

import datetime

from ai4eosc_energy_accounting import main as main_mod


def test_align_down_disabled_returns_input_unchanged():
    when = datetime.datetime(2026, 8, 24, 6, 0, 1, tzinfo=datetime.timezone.utc)
    assert main_mod._align_down(when, 0) == when


def test_align_down_floors_to_interval_boundary_past_it():
    when = datetime.datetime(2026, 8, 24, 6, 0, 1, tzinfo=datetime.timezone.utc)
    expected = datetime.datetime(2026, 8, 24, 6, 0, 0, tzinfo=datetime.timezone.utc)
    assert main_mod._align_down(when, 21600) == expected  # 6h


def test_align_down_floors_to_previous_boundary_before_it():
    when = datetime.datetime(2026, 8, 24, 5, 59, 59, tzinfo=datetime.timezone.utc)
    expected = datetime.datetime(2026, 8, 24, 0, 0, 0, tzinfo=datetime.timezone.utc)
    assert main_mod._align_down(when, 21600) == expected  # 6h


def test_align_down_exact_boundary_is_unchanged():
    when = datetime.datetime(2026, 8, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
    assert main_mod._align_down(when, 21600) == when


def test_resolve_extract_to_disabled_subtracts_lag_from_now():
    now = datetime.datetime(2026, 8, 24, 6, 0, 1, tzinfo=datetime.timezone.utc)
    expected = now - datetime.timedelta(seconds=60)
    assert main_mod._resolve_extract_to(now, 0, 60) == expected


def test_resolve_extract_to_aligned_and_boundary_old_enough_returns_boundary():
    now = datetime.datetime(2026, 8, 24, 6, 5, 0, tzinfo=datetime.timezone.utc)
    boundary = datetime.datetime(2026, 8, 24, 6, 0, 0, tzinfo=datetime.timezone.utc)
    assert main_mod._resolve_extract_to(now, 21600, 60) == boundary


def test_resolve_extract_to_aligned_but_boundary_too_recent_returns_none():
    now = datetime.datetime(2026, 8, 24, 6, 0, 30, tzinfo=datetime.timezone.utc)
    assert main_mod._resolve_extract_to(now, 21600, 60) is None
