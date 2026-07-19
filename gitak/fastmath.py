"""Numeric kernels with an optional C acceleration.

Gitak's runtime is dominated by native code already: scikit-learn's tree
fitting and SQLite. The pure-Python arithmetic that remains lives here, in
small deterministic kernels, so it can be swapped for a compiled version
without changing results.

If the C extension ``gitak._speedups`` has been built (see docs/PERFORMANCE.md)
it is used automatically; otherwise these pure-Python implementations run.
The two paths are byte-for-byte equivalent and tests assert it, so behaviour
never depends on whether the extension is present.
"""


def _py_mean_std(groups):
    """For each inner sequence, return (mean, population standard deviation)."""
    out = []
    for vals in groups:
        n = len(vals)
        if n == 0:
            out.append((0.0, 0.0))
            continue
        s = 0.0
        for v in vals:
            s += v
        mean = s / n
        var = 0.0
        for v in vals:
            d = v - mean
            var += d * d
        out.append((mean, (var / n) ** 0.5))
    return out


try:  # pragma: no cover - depends on whether the extension was compiled
    from . import _speedups as _c
    mean_std = _c.mean_std
    USING_C = True
    BACKEND = "c"
except ImportError:  # pragma: no cover
    mean_std = _py_mean_std
    USING_C = False
    BACKEND = "python"
