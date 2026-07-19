"""Numeric kernels: pure-Python correctness, and C/Python equivalence when
the compiled extension is present."""

import random
import statistics

import pytest

from gitak import fastmath


def _reference(groups):
    out = []
    for vals in groups:
        if not vals:
            out.append((0.0, 0.0))
        else:
            out.append((statistics.fmean(vals), statistics.pstdev(vals)))
    return out


def test_mean_std_matches_statistics():
    groups = [[8, 9, 10], [5, 5, 5], [1, 10], [7.5, 6.5, 8.0, 9.0]]
    got = fastmath._py_mean_std(groups)
    ref = _reference(groups)
    for (m, s), (rm, rs) in zip(got, ref):
        assert m == pytest.approx(rm)
        assert s == pytest.approx(rs)


def test_empty_group():
    assert fastmath._py_mean_std([[]]) == [(0.0, 0.0)]
    assert fastmath._py_mean_std([]) == []


def test_single_value_has_zero_spread():
    assert fastmath._py_mean_std([[7]]) == [(7.0, 0.0)]


def test_active_backend_is_correct():
    # whichever backend is active must agree with the reference
    groups = [[random.uniform(1, 10) for _ in range(random.randint(1, 16))]
              for _ in range(200)]
    got = fastmath.mean_std(groups)
    ref = _reference(groups)
    for (m, s), (rm, rs) in zip(got, ref):
        assert m == pytest.approx(rm)
        assert s == pytest.approx(rs)


@pytest.mark.skipif(not fastmath.USING_C,
                    reason="C extension not built; pure-Python fallback in use")
def test_c_matches_python_exactly():
    rng = random.Random(0)
    groups = [[rng.uniform(1, 10) for _ in range(rng.randint(0, 20))]
              for _ in range(500)]
    py = fastmath._py_mean_std(groups)
    c = fastmath.mean_std(groups)
    # identical arithmetic, so results must be bit-for-bit equal
    assert c == py
