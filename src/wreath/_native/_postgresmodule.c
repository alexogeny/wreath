/* Optional native PostgreSQL driver backend for wreath.postgres. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include "postgres/buffer.h"
#include "postgres/codec.h"
#include "postgres/connection.h"
#include "postgres/decode.h"
#include "postgres/hydrate.h"
#include "postgres/migration_artifact.h"
#include "postgres/migration_image.h"
#include "postgres/migration_resolver.h"
#include "postgres/migration_runner.h"
#include "postgres/migration_sql.h"
#include "postgres/model.h"
#include "postgres/operation.h"
#include "postgres/plan.h"
#include "postgres/protocol.h"
#include "postgres/record.h"
#include "postgres/slab.h"
#include "postgres/tape.h"

static void
postgres_module_free(void *module)
{
    (void)module;
    wreath_pg_connection_fini();
    wreath_pg_protocol_fini();
    wreath_pg_model_fini();
    wreath_pg_hydrate_fini();
    wreath_pg_codec_fini();
    wreath_pg_record_fini();
}

static struct PyModuleDef postgres_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "wreath._native._postgres",
    .m_doc = "Native PostgreSQL driver backend for Wreath.",
    .m_size = -1,
    .m_free = postgres_module_free,
};

PyMODINIT_FUNC
PyInit__postgres(void)
{
    PyObject *module = PyModule_Create(&postgres_module);
    if (module == NULL) {
        return NULL;
    }

    /* Initialize low-level components before the objects that compose them. */
    if (wreath_pg_buffer_init(module) < 0 ||
        wreath_pg_slab_init(module) < 0 ||
        wreath_pg_codec_init(module) < 0 ||
        wreath_pg_tape_init(module) < 0 ||
        wreath_pg_operation_init(module) < 0 ||
        wreath_pg_record_init(module) < 0 ||
        wreath_pg_decode_init(module) < 0 ||
        wreath_pg_model_init(module) < 0 ||
        wreath_pg_hydrate_init(module) < 0 ||
        wreath_pg_migration_artifact_init(module) < 0 ||
        wreath_pg_migration_image_init(module) < 0 ||
        wreath_pg_migration_resolver_init(module) < 0 ||
        wreath_pg_migration_runner_init(module) < 0 ||
        wreath_pg_migration_sql_init(module) < 0 ||
        wreath_pg_protocol_init(module) < 0 ||
        wreath_pg_plan_init(module) < 0 ||
        wreath_pg_connection_init(module) < 0 ||
        PyModule_AddStringConstant(module, "_implementation", "native") < 0) {
        Py_DECREF(module);
        return NULL;
    }

    return module;
}
