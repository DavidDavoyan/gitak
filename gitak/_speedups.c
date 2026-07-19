/* Compiled numeric kernels for Gitak (CPython C API).
 *
 * Optional: the package runs on the pure-Python implementations in
 * gitak/fastmath.py when this extension is not built. When it is built it is
 * imported automatically. The two paths are byte-for-byte equivalent and the
 * test suite asserts it, so behaviour never depends on the extension being
 * present.
 *
 * Build:  pip install -e .        (needs a C compiler; see docs/PERFORMANCE.md)
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>

/* mean_std(groups) -> list of (mean, population_stdev) tuples.
 *
 * groups is any iterable of sequences of numbers. Each inner sequence yields
 * one (mean, stdev) pair; an empty sequence yields (0.0, 0.0). This mirrors
 * gitak.fastmath._py_mean_std exactly. */
static PyObject *
speedups_mean_std(PyObject *self, PyObject *args)
{
    PyObject *groups;
    if (!PyArg_ParseTuple(args, "O", &groups))
        return NULL;

    PyObject *outer = PySequence_Fast(groups, "mean_std() expects an iterable of sequences");
    if (outer == NULL)
        return NULL;

    Py_ssize_t n = PySequence_Fast_GET_SIZE(outer);
    PyObject *result = PyList_New(n);
    if (result == NULL) {
        Py_DECREF(outer);
        return NULL;
    }

    PyObject **outer_items = PySequence_Fast_ITEMS(outer);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *inner = PySequence_Fast(outer_items[i], "each group must be a sequence");
        if (inner == NULL) {
            Py_DECREF(result);
            Py_DECREF(outer);
            return NULL;
        }
        Py_ssize_t m = PySequence_Fast_GET_SIZE(inner);
        double mean = 0.0, stdev = 0.0;

        if (m > 0) {
            PyObject **vals = PySequence_Fast_ITEMS(inner);
            double sum = 0.0;
            for (Py_ssize_t j = 0; j < m; j++) {
                double v = PyFloat_AsDouble(vals[j]);
                if (v == -1.0 && PyErr_Occurred()) {
                    Py_DECREF(inner);
                    Py_DECREF(result);
                    Py_DECREF(outer);
                    return NULL;
                }
                sum += v;
            }
            mean = sum / (double)m;
            double var = 0.0;
            for (Py_ssize_t j = 0; j < m; j++) {
                /* values already validated in the first pass */
                double d = PyFloat_AsDouble(vals[j]) - mean;
                var += d * d;
            }
            stdev = sqrt(var / (double)m);
        }
        Py_DECREF(inner);

        PyObject *tup = Py_BuildValue("(dd)", mean, stdev);
        if (tup == NULL) {
            Py_DECREF(result);
            Py_DECREF(outer);
            return NULL;
        }
        PyList_SET_ITEM(result, i, tup);  /* steals the reference */
    }

    Py_DECREF(outer);
    return result;
}

static PyMethodDef speedups_methods[] = {
    {"mean_std", speedups_mean_std, METH_VARARGS,
     "mean_std(groups) -> list of (mean, population stdev) per inner sequence."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef speedups_module = {
    PyModuleDef_HEAD_INIT,
    "_speedups",
    "Compiled numeric kernels for Gitak (optional acceleration).",
    -1,
    speedups_methods
};

PyMODINIT_FUNC
PyInit__speedups(void)
{
    return PyModule_Create(&speedups_module);
}
