# pgddl

[![PyPI](https://img.shields.io/pypi/v/pgddl.svg)](https://pypi.org/project/pgddl/)
[![Python](https://img.shields.io/pypi/pyversions/pgddl.svg)](https://pypi.org/project/pgddl/)

`pgddl` exports a PostgreSQL schema as executable SQL. It inspects PostgreSQL's
system catalogs directly using psycopg 3 (PostgreSQL 12+); it does **not**
invoke `pg_dump`.

## Install

```console
python -m pip install pgddl
```

Package page: <https://pypi.org/project/pgddl/>

## Python API

```python
from pgddl import export_ddl

sql = export_ddl("postgresql://user:password@localhost:5432/mydb", schemas="public")
with open("schema.sql", "w", encoding="utf-8") as output:
    output.write(sql)
```

Pass a list to export multiple schemas, or omit `schemas` to export every
non-system schema:

```python
sql = export_ddl("postgresql://localhost/mydb", schemas=["public", "sales"])
```

### Selecting tables

`tables` and `exclude_tables` accept a name, a list, `schema.table` names, and
`fnmatch` wildcards:

```python
export_ddl(dsn, tables="orders")                          # one table
export_ddl(dsn, tables=["public.users", "public.orders"]) # several tables
export_ddl(dsn, schemas="sales", tables="audit_*")        # a range
export_ddl(dsn, exclude_tables=["*_tmp", "audit_*"])      # everything else
```

Selected tables are exported with their columns, constraints, indexes, triggers,
comments and owned sequences. Foreign keys to unselected tables are skipped, a
partition requires its parent, and shared objects (types, domains, functions,
views) are not exported in this mode. A pattern matching nothing raises `ValueError`.

## Command line

```console
pgddl --dsn "postgresql://localhost/mydb" --schema public --output schema.sql
pgddl --dsn "$DSN" -t public.users -t 'audit_*' -T '*_tmp'
```

Set `PGDDL_DSN` instead of putting a connection string on the command line.
The CLI writes SQL to stdout if `--output` is omitted. PostgreSQL connections
use the normal libpq environment variables and psycopg connection options.

## Exported objects

The exporter includes schemas and comments; enum, domain, and composite types;
sequences and ownership; tables and columns (including defaults, identity and
generated columns, comments, and declarative partitions); constraints and
indexes; functions and procedures; views, materialized views, and triggers.
Foreign keys are emitted after table creation so references to tables exported
later in sort order still work.

The output describes schema DDL only; it does not contain table data, roles,
grants, extensions, aggregates, range types, row-level security policies,
ownership/ACL settings, or non-partition table inheritance. Exporting only a
subset of schemas does not automatically include objects in other schemas that
the selected objects depend on. Test generated SQL on the target PostgreSQL
version before using it for production restoration.

Verified by restoring exports of ten real databases into scratch databases and
comparing `pg_dump --schema-only` output. The only difference is that PostgreSQL
re-renders `CHECK (col IN (...))` array expressions with different (equivalent)
parenthesization after restore; `pg_dump` output shows the same behavior.

## Build and publish

```console
python -m pip install build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

## Development

```console
python -m unittest discover -s tests -v
python -m pip install .
```

The PostgreSQL integration suite creates and drops a uniquely named temporary
database; it does not alter the configured database. Set `PGDDL_TEST_DSN` to a
PostgreSQL DSN for a role with `CREATEDB`, and ensure the `pg_dump` executable
is on `PATH`, to run it. It round-trips 45 built-in PostgreSQL types plus a
multi-object fixture and compares the restored schema with `pg_dump --schema-only`.
