"""Micro-benchmarks for Gitak's pure-Python numeric paths.

Run: python bench/benchmark.py

Reports:
  1. the peer-tutoring matcher's per-candidate cost (old O(load) sum vs the
     current O(1) lookup) on a realistic number of pairings;
  2. the mean/standard-deviation kernel (gitak.fastmath), pure Python vs the
     compiled C extension when it is available.

These are the parts Gitak actually runs in Python. The dominant costs of the
full pipeline live in scikit-learn and SQLite, which are already native C.
"""

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gitak import fastmath


def _time(fn, repeat=5):
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def bench_pairing(n=1400):
    """Emulate suggest()'s inner matching loop with the old and new load
    accounting. n ~= a large school's number of flagged subject-slots."""
    rng = random.Random(1)
    # candidate tutor pools keyed by (class, subject); a handful of tutors each
    pools = [[(rng.random(), 1000 + rng.randrange(400)) for _ in range(6)]
             for _ in range(n)]
    MAX = 2

    def old():
        load = {}
        for i, pool in enumerate(pools):
            pick = None
            for _, sid in pool:
                key = sum(v for (t, _s), v in load.items() if t == sid)
                if key < MAX:
                    pick = sid
                    break
            if pick is not None:
                load[(pick, i)] = load.get((pick, i), 0) + 1

    def new():
        load_by_tutor = {}
        for i, pool in enumerate(pools):
            pick = None
            for _, sid in pool:
                if load_by_tutor.get(sid, 0) < MAX:
                    pick = sid
                    break
            if pick is not None:
                load_by_tutor[pick] = load_by_tutor.get(pick, 0) + 1

    t_old = _time(old)
    t_new = _time(new)
    print(f"peer-tutoring match ({n} pairings)")
    print(f"  old (sum over load dict): {t_old*1000:7.2f} ms")
    print(f"  new (O(1) per candidate): {t_new*1000:7.2f} ms")
    print(f"  speedup: {t_old / t_new:.1f}x\n")


def bench_mean_std(n_students=12000, subjects=14):
    rng = random.Random(2)
    groups = [[rng.uniform(1, 10) for _ in range(subjects)] for _ in range(n_students)]
    t_py = _time(lambda: fastmath._py_mean_std(groups))
    print(f"mean/std kernel ({n_students} students x {subjects} subjects)")
    print(f"  pure Python: {t_py*1000:7.2f} ms")
    if fastmath.USING_C:
        t_c = _time(lambda: fastmath.mean_std(groups))
        print(f"  C extension: {t_c*1000:7.2f} ms")
        print(f"  speedup: {t_py / t_c:.1f}x")
    else:
        print("  C extension: not built (using pure Python) -- see docs/PERFORMANCE.md")
    print()


if __name__ == "__main__":
    print(f"fastmath backend: {fastmath.BACKEND}\n")
    bench_pairing()
    bench_mean_std()
