# Performance

A short, honest account of where Gitak spends time and what is optimized.

## Where the time actually goes

Profiling the two heavy commands on the demo school (~600 students, 3 years, 220k grades):

| Command | Total | Dominated by |
|---|---|---|
| `predict` | ~20 s | scikit-learn gradient-boosting tree fitting (~15 s) and other numpy/sklearn work — **already native C** |
| `seed` | ~3.7 s | SQLite inserts/commits (~1.9 s, **already native C**), then a little Python |

The important conclusion: **Gitak is already bound by native code** — scikit-learn and SQLite are C libraries. Rewriting Python in C cannot speed up the parts that dominate, because those parts are not Python. So the work here is (1) fix the genuinely Python-bound hot spot, and (2) offer an optional C kernel for the remaining pure-Python arithmetic.

## 1. The real win: the peer-tutoring matcher

`pairing.suggest()` matched each flagged student to a tutor while enforcing a per-tutor load cap. The old code recomputed a tutor's load by summing over the entire growing `load` dictionary for **every** candidate — an O(pairings²) pattern. It now keeps a running per-tutor count and checks it in O(1).

Identical results; the benchmark (`python bench/benchmark.py`) on a large school:

```
peer-tutoring match (1400 pairings)
  old (sum over load dict):  137.22 ms
  new (O(1) per candidate):    0.42 ms
  speedup: 327.6x
```

This is an algorithmic fix in pure Python — the right tool for the job, and it needs no compiler.

## 2. Optional C extension (`gitak/_speedups.c`)

For the small deterministic numeric kernel that remains — the per-student
mean and standard deviation across subjects, used by the scoring engine —
there is an optional C extension built with the CPython C API.

- It is **optional**. With no C compiler, `pip install` skips it and the pure-Python fallback in [`gitak/fastmath.py`](../gitak/fastmath.py) runs. The public demo therefore works everywhere with no toolchain.
- The two paths are **byte-for-byte equivalent**; `tests/test_fastmath.py` asserts it whenever the extension is present.

Build it:

```bash
pip install -e .        # needs a C compiler (gcc/clang, or MSVC on Windows)
python -c "from gitak import fastmath; print(fastmath.BACKEND)"   # -> c
python bench/benchmark.py
```

Honesty note: the machine this was developed on had no C compiler, so the compiled path was not built or measured there; the pure-Python fallback is what runs and is fully tested locally. The compiled path is built and exercised in continuous integration: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) compiles the extension on Linux (gcc), macOS (clang) and Windows (MSVC), fails the build if it did not compile, and runs the full test suite including the C-vs-Python equivalence check (which is skipped locally). Even at its best the extension only accelerates a few milliseconds of arithmetic — the honest headline remains that Gitak's cost is in scikit-learn and SQLite, both already C.

## When performance actually matters here

For a single real school (a few hundred to a couple thousand students) none of this is a bottleneck: a full `predict` is seconds, and everything else is sub-second. These changes matter at the tails — very large districts, or re-running the pipeline many times — and the pairing fix is the one that removes a real quadratic cliff.
