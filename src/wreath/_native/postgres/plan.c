#include "plan.h"

#include "decode.h"

#include <stddef.h>
#include <structmember.h>

PyTypeObject *WreathPgPlanType = NULL;

static int
plan_traverse(WreathPgPlan *self, visitproc visit, void *arg)
{
    Py_VISIT(self->statement_name);
    Py_VISIT(self->parameter_oids);
    Py_VISIT(self->result_oids);
    Py_VISIT(self->result_names);
    Py_VISIT(self->decoder_plan);
    return 0;
}

static int
plan_clear(WreathPgPlan *self)
{
    Py_CLEAR(self->statement_name);
    Py_CLEAR(self->parameter_oids);
    Py_CLEAR(self->result_oids);
    Py_CLEAR(self->result_names);
    Py_CLEAR(self->decoder_plan);
    return 0;
}

static void
plan_dealloc(WreathPgPlan *self)
{
    PyObject_GC_UnTrack(self);
    plan_clear(self);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyObject *
plan_new(PyTypeObject *type, PyObject *args, PyObject *kwargs)
{
    WreathPgPlan *self;
    PyObject *statement_name;
    PyObject *parameter_oids;
    PyObject *result_oids;
    PyObject *result_names;
    static char *keywords[] = {
        "statement_name", "parameter_oids", "result_oids", "result_names", NULL
    };

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "OOOO:Plan", keywords,
                                     &statement_name, &parameter_oids,
                                     &result_oids, &result_names)) return NULL;
    if (!PyBytes_Check(statement_name) || !PyTuple_Check(parameter_oids) ||
        !PyTuple_Check(result_oids) || !PyTuple_Check(result_names)) {
        PyErr_SetString(PyExc_TypeError, "invalid Plan fields");
        return NULL;
    }
    self = (WreathPgPlan *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->statement_name = Py_NewRef(statement_name);
    self->parameter_oids = Py_NewRef(parameter_oids);
    self->result_oids = Py_NewRef(result_oids);
    self->result_names = Py_NewRef(result_names);
    {
        Py_ssize_t count = PyTuple_GET_SIZE(result_oids);
        PyObject *formats = PyTuple_New(count);
        if (formats == NULL) {
            Py_DECREF(self);
            return NULL;
        }
        for (Py_ssize_t i = 0; i < count; i++) {
            PyTuple_SET_ITEM(formats, i, PyLong_FromLong(1));
        }
        self->decoder_plan = wreath_pg_decoder_plan_new(
            result_oids, formats, result_names
        );
        Py_DECREF(formats);
        if (self->decoder_plan == NULL) {
            Py_DECREF(self);
            return NULL;
        }
    }
    return (PyObject *)self;
}

static PyMemberDef plan_members[] = {
    {"statement_name", Py_T_OBJECT_EX, offsetof(WreathPgPlan, statement_name), READONLY, NULL},
    {"parameter_oids", Py_T_OBJECT_EX, offsetof(WreathPgPlan, parameter_oids), READONLY, NULL},
    {"result_oids", Py_T_OBJECT_EX, offsetof(WreathPgPlan, result_oids), READONLY, NULL},
    {"result_names", Py_T_OBJECT_EX, offsetof(WreathPgPlan, result_names), READONLY, NULL},
    {"decoder_plan", Py_T_OBJECT_EX, offsetof(WreathPgPlan, decoder_plan), READONLY, NULL},
    {NULL, 0, 0, 0, NULL},
};

static PyType_Slot plan_slots[] = {
    {Py_tp_new, plan_new},
    {Py_tp_dealloc, plan_dealloc},
    {Py_tp_traverse, plan_traverse},
    {Py_tp_clear, plan_clear},
    {Py_tp_members, plan_members},
    {0, NULL},
};

static PyType_Spec plan_spec = {
    .name = "wreath._native._postgres.Plan",
    .basicsize = sizeof(WreathPgPlan),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    .slots = plan_slots,
};

int
wreath_pg_plan_init(PyObject *module)
{
    int result;
    WreathPgPlanType = (PyTypeObject *)PyType_FromSpec(&plan_spec);
    if (WreathPgPlanType == NULL) return -1;
    result = PyModule_AddObjectRef(module, "Plan", (PyObject *)WreathPgPlanType);
    if (result == 0) Py_DECREF(WreathPgPlanType);
    return result;
}
