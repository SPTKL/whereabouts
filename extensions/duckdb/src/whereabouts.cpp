#include "duckdb_extension.h"
#include "whereabouts_sql.hpp"

DUCKDB_EXTENSION_EXTERN

DUCKDB_EXTENSION_ENTRYPOINT(duckdb_connection connection, duckdb_extension_info info,
                           struct duckdb_extension_access *access) {
    duckdb_result result;
    const auto status = duckdb_query(connection, WHEREABOUTS_SQL, &result);
    if (status != DuckDBSuccess) {
        access->set_error(info, duckdb_result_error(&result));
    }
    duckdb_destroy_result(&result);
    return status == DuckDBSuccess;
}
