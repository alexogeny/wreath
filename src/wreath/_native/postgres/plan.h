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
    /* Bind/Execute/Sync for this plan with no arguments, by result format:
     * [0] text (`execute`), [1] binary (everything else). Built on first use.
     * Held here rather than beside the plan so it needs no invalidation --
     * evicting the plan frees the packets, and a re-prepared statement is a new
     * plan with a new name. See `build_cached` in protocol.c. */
    PyObject *packets[2];
} WreathPgPlan;

extern PyTypeObject *WreathPgPlanType;
int wreath_pg_plan_init(PyObject *module);

#endif
