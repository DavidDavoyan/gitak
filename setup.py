"""Build the optional C speedups.

The extension is marked optional: if there is no C compiler, the build prints
a warning and installs the pure-Python fallback (gitak/fastmath.py) instead,
so `pip install .` always succeeds. Project metadata lives in pyproject.toml.
"""

from setuptools import Extension, setup

setup(
    ext_modules=[
        Extension("gitak._speedups", ["gitak/_speedups.c"], optional=True),
    ],
)
