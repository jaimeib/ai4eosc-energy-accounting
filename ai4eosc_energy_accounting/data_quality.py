"""Detect and correct out-of-magnitude samples in a Mimir range-query result.

``scaph_process_power_consumption_microwatts`` (and the optional GPU power
query) are instantaneous samples that get folded straight into a per-allocation
Wh sum. A single bad measurement -- a series that normally sits at a few watts
suddenly reporting several kW, or a ``NaN``/``Inf`` -- would inflate the
reported energy with no sanity check. This module rewrites such samples, per
series, from the nearest trustworthy neighbours, before the sum is computed.

A sample is only treated as bad when it sits an order of magnitude above the
local level on *both* sides, so a genuine sustained ramp (microwatts to
milliwatts to watts, each level held for a while) is left alone while a lone
spike that the series immediately drops back down from is corrected. Only high
spikes are touched: a sample dropping toward zero is a legitimate idle period.
"""

import logging
import math
import statistics

LOG = logging.getLogger(__name__)


def clean_result(result, cfg):
    """Return a Prometheus "matrix" ``data.result`` list with out-of-magnitude
    samples in each series' ``values`` corrected.

    A new list is built; series and samples that need no change are kept as-is
    (same objects), so an all-clean result comes back untouched.
    """
    cleaned = []
    total = 0
    for series in result:
        values = series.get("values", [])
        new_values, corrections = _clean_values(values, cfg)
        if corrections:
            total += len(corrections)
            labels = series.get("metric", {})
            for idx, old, new in corrections:
                LOG.warning(
                    "data quality: %s sample at %s corrected %.4g -> %.4g",
                    labels,
                    values[idx][0],
                    old,
                    new,
                )
        cleaned.append(series if new_values is values else {**series, "values": new_values})

    if total:
        LOG.info("data quality: corrected %d out-of-magnitude sample(s)", total)
    return cleaned


def _clean_values(values, cfg):
    """Correct out-of-magnitude entries in a single series' ``values`` list.

    :returns: ``(new_values, corrections)`` where ``corrections`` is a list of
        ``(index, old_value, new_value)`` tuples. When nothing changed,
        ``new_values`` is the original list object and ``corrections`` is empty.
    """
    n = len(values)
    if n < cfg.min_samples:
        return values, []

    try:
        nums = [float(v) for _, v in values]
        ts = [float(t) for t, _ in values]
    except (TypeError, ValueError):
        return values, []

    if not any(math.isfinite(x) for x in nums):
        return values, []

    window = max(cfg.window, 1)

    def side_level(lo, hi):
        """Median of the finite samples in ``nums[lo:hi]`` (the local level on
        one side of a sample), or ``None`` if there are none."""
        vals = [nums[j] for j in range(lo, hi) if math.isfinite(nums[j])]
        return statistics.median(vals) if vals else None

    def is_bad(i):
        """A sample is a bad measurement when it is not finite, or when it sits
        an order of magnitude (``outlier_factor``) above the local level on
        *both* sides. Requiring both sides means a genuine sustained step up
        (microwatts -> milliwatts -> watts, each level held) is left alone,
        while a lone spike that the series drops back down from is corrected.
        At the very start/end of the series only the one available side is
        checked (matching the "trend of the next/previous two" rule)."""
        x = nums[i]
        if not math.isfinite(x):
            return True
        left = side_level(max(0, i - window), i)
        right = side_level(i + 1, min(n, i + window + 1))
        refs = [r for r in (left, right) if r is not None and r > 0]
        if not refs:
            return False
        return x > cfg.outlier_factor * max(refs)

    bad = [i for i in range(n) if is_bad(i)]
    if not bad:
        return values, []

    bad_set = set(bad)
    good_idx = [
        i for i in range(n) if i not in bad_set and math.isfinite(nums[i])
    ]
    good_median = (
        statistics.median([nums[i] for i in good_idx]) if good_idx else None
    )

    corrections = []
    for i in bad:
        new = _reconstruct(i, ts, nums, good_idx, good_median)
        if new is None:
            LOG.warning(
                "data quality: sample #%d could not be corrected (no "
                "trustworthy samples in the series); leaving it as-is",
                i,
            )
            continue
        if new >= nums[i]:
            # The value the neighbours imply is not below the flagged sample,
            # so it is consistent with the local trend (e.g. the last sample
            # of a still-climbing ramp), not a spike to bring down.
            continue
        corrections.append((i, nums[i], new))

    if not corrections:
        return values, []

    new_values = list(values)
    for i, _old, new in corrections:
        new_values[i] = [values[i][0], repr(new)]
    return new_values, corrections


def _reconstruct(i, ts, nums, good_idx, good_median):
    """Infer a replacement for the sample at index ``i`` from the nearest good
    (finite, not flagged) samples.

    - a good sample on each side: linear interpolation between them at ``ts[i]``
      (for evenly spaced adjacent neighbours this is their mean).
    - only good samples after ``i`` (spike at/near the start): linear trend
      through the two nearest ones, extrapolated back to ``ts[i]``.
    - only good samples before ``i`` (spike at/near the end): linear trend
      through the two nearest ones, extrapolated forward to ``ts[i]``.
    - none of the above, or a negative result: the median of the good samples.

    Returns ``None`` only when the series has no good samples at all.
    """
    if not good_idx:
        return None

    left = [j for j in good_idx if j < i]
    right = [j for j in good_idx if j > i]

    value = None
    if left and right:
        value = _line_at(ts[i], ts[left[-1]], nums[left[-1]], ts[right[0]], nums[right[0]])
    elif len(right) >= 2:
        value = _line_at(ts[i], ts[right[0]], nums[right[0]], ts[right[1]], nums[right[1]])
    elif len(left) >= 2:
        value = _line_at(ts[i], ts[left[-1]], nums[left[-1]], ts[left[-2]], nums[left[-2]])

    if value is None or value < 0:
        value = good_median
    return value


def _line_at(x, x0, y0, x1, y1):
    """Value at ``x`` of the line through ``(x0, y0)`` and ``(x1, y1)``."""
    if x1 == x0:
        return (y0 + y1) / 2
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
