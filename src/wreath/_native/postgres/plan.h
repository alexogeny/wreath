#ifndef WREATH_POSTGRES_PLAN_H
#define WREATH_POSTGRES_PLAN_H

#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *statement_name;
    PyObject *parameter_oids;
    PyObject *result_oids;
    PyObject *result_names;
    PyObject *decoder_plan;
} WreathPgPlan;

extern PyTypeObject *WreathPgPlanType;
int wreath_pg_plan_init(PyObject *module);

#endif
