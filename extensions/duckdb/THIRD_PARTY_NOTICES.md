# Attribution

The standard matching algorithm and SQL pipeline are adapted from Whereabouts,
copyright (c) 2023 Alex Lee, under the repository's MIT license. The initial port
was based on commit `27e8f61c11901b6aa3ac5d168a54ec7635409300`.
See `licenses/whereabouts-MIT.txt`.

The DuckDB C API headers in `vendor/duckdb` are distributed under DuckDB's MIT
license, reproduced in that directory. These headers expose the versioned
`C_STRUCT` extension API with a minimum API version of 1.2.0.

The checksum-pinned dependency fetcher, checksum manifest, and native extension
metadata writer are adapted from duckdb-geosupport, copyright (c) 2026
Geosupport for DuckDB contributors, under the MIT license reproduced in
`licenses/geosupport-MIT.txt`. Geosupport is not a build or runtime dependency.

No reference address datasets are distributed with this extension. The example
data is illustrative. Users supply and license their own reference datasets.
