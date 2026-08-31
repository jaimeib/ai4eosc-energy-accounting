"""Unit tests for data_quality._clean_values / data_quality.clean_result."""

import math

from ai4eosc_energy_accounting import data_quality
from ai4eosc_energy_accounting.config import DataQualityConfig

STEP = 30
START = 1_000_000


def _values(nums):
    return [[START + i * STEP, str(n)] for i, n in enumerate(nums)]


def _clean(nums, **cfg_kwargs):
    """Return ``(cleaned_floats, corrections)`` for a bare list of numbers."""
    cfg = DataQualityConfig(**cfg_kwargs)
    new_values, corrections = data_quality._clean_values(_values(nums), cfg)
    return [float(v) for _, v in new_values], corrections


def test_clean_series_is_returned_unchanged():
    values = _values([5, 5, 6, 5, 5])
    new_values, corrections = data_quality._clean_values(values, DataQualityConfig())
    assert new_values is values
    assert corrections == []


def test_spike_in_the_middle_replaced_by_mean_of_adjacent_good_samples():
    out, corrections = _clean([5, 5, 5000, 5, 5])
    assert len(corrections) == 1
    assert corrections[0][0] == 2
    assert math.isclose(out[2], 5.0)


def test_spike_as_first_sample_uses_linear_trend_of_next_two():
    # good tail 10, 20, 30, 40 (slope +10/step) extrapolated back to index 0 -> 0
    out, corrections = _clean([9000, 10, 20, 30, 40])
    assert corrections[0][0] == 0
    assert math.isclose(out[0], 0.0, abs_tol=1e-9)


def test_spike_as_last_sample_uses_linear_trend_of_previous_two():
    # good head 40, 30, 20, 10 (slope -10/step) extrapolated forward to last -> 0
    out, corrections = _clean([40, 30, 20, 10, 9000])
    assert corrections[0][0] == 4
    assert math.isclose(out[4], 0.0, abs_tol=1e-9)


def test_two_consecutive_spikes_both_corrected_from_good_neighbours():
    out, corrections = _clean([10, 10, 5000, 6000, 10, 10])
    assert [c[0] for c in corrections] == [2, 3]
    assert math.isclose(out[2], 10.0)
    assert math.isclose(out[3], 10.0)


def test_non_finite_sample_is_treated_as_bad_and_corrected():
    out, corrections = _clean([5, 5, float("nan"), 5, 5])
    assert len(corrections) == 1
    assert math.isclose(out[2], 5.0)


def test_series_shorter_than_min_samples_is_untouched():
    values = _values([5, 9000, 5])
    new_values, corrections = data_quality._clean_values(values, DataQualityConfig())
    assert new_values is values
    assert corrections == []


def test_series_with_no_finite_samples_is_left_as_is():
    values = _values([float("inf")] * 5)
    new_values, corrections = data_quality._clean_values(values, DataQualityConfig())
    assert new_values is values
    assert corrections == []


def test_sustained_step_up_across_orders_of_magnitude_is_not_corrected():
    # microwatts -> milliwatts -> watts, each level held: a real ramp, not errors
    nums = [1, 1, 1, 1, 1000, 1000, 1000, 1_000_000, 1_000_000, 1_000_000]
    _out, corrections = _clean(nums)
    assert corrections == []


def test_transient_jump_to_a_far_higher_level_is_corrected():
    # low, one sample orders of magnitude up, straight back to low
    _out, corrections = _clean([5, 5, 5, 5, 5000, 5, 5, 5, 5])
    assert [c[0] for c in corrections] == [4]


def test_series_varying_within_a_band_is_not_reshaped():
    # every sample has neighbours of a similar magnitude, so none of them
    # dominates its local level: a noisy signal is left as it is.
    _out, corrections = _clean([100, 500, 200, 600, 150, 550, 250, 500, 300])
    assert corrections == []


def test_isolated_spike_stands_out_even_when_the_local_level_is_high():
    _out, corrections = _clean([400, 500, 450, 90000, 480, 520, 460])
    assert [c[0] for c in corrections] == [3]


def test_low_dip_toward_zero_is_not_corrected():
    out, corrections = _clean([500, 500, 1, 500, 500])
    assert corrections == []
    assert out[2] == 1.0


def test_outlier_factor_controls_sensitivity():
    # 50 is 5x the median of 10: past the default factor of 10 it stays,
    # with a factor of 3 it is flagged.
    assert _clean([10, 10, 50, 10, 10])[1] == []
    _out, corrections = _clean([10, 10, 50, 10, 10], outlier_factor=3.0)
    assert len(corrections) == 1


def test_clean_result_preserves_structure_and_keeps_timestamps():
    series = {"metric": {"x": "y"}, "values": _values([5, 5, 9000, 5, 5])}
    out = data_quality.clean_result([series], DataQualityConfig())
    assert out[0]["metric"] == {"x": "y"}
    assert out[0]["values"][2][0] == series["values"][2][0]
    assert math.isclose(float(out[0]["values"][2][1]), 5.0)


def test_clean_result_returns_an_all_clean_series_object_untouched():
    series = {"metric": {}, "values": _values([5, 5, 5, 5, 5])}
    out = data_quality.clean_result([series], DataQualityConfig())
    assert out[0] is series
