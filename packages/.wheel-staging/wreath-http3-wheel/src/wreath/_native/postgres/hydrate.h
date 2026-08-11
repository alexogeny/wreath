#ifndef WREATH_POSTGRES_HYDRATE_H
#define WREATH_POSTGRES_HYDRATE_H

#include <Python.h>

extern PyTypeObject *WreathPgHydratePlanType;

int wreath_pg_hydrate_init(PyObject *module);
void wreath_pg_hydrate_fini(void);

/* Decode up to `limit` rows of `tape` straight into model instances, appending
   them to `dest` and reusing objects already present in `identity_map`.
   Returns 0, or -1 with an exception set. */
int wreath_pg_hydrate_models(PyObject *decoder_plan, PyObject *tape_object,
                          PyObject *hydrate_plan, Py_ssize_t limit, PyObject *dest,
                          PyObject *identity_map, PyObject *owner);

#endif
