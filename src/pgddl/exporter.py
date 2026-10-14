"""PostgreSQL catalog inspection and DDL generation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Any

import psycopg
from psycopg.rows import dict_row


def quote_ident(identifier: str) -> str:
    """Quote a PostgreSQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """Quote a PostgreSQL string literal."""
    escapes = {
        "\\": "\\\\",
        "'": "''",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
        "\v": "\\v",
    }
    if "\0" in value:
        raise ValueError("PostgreSQL string literals cannot contain NUL characters")
    return "E'" + "".join(escapes.get(char, char) for char in value) + "'"


def _qualified(schema: str, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}"


def _rows(cursor: Any, query: str, schemas: list[str]) -> list[Mapping[str, Any]]:
    cursor.execute(query, (schemas,))
    return cursor.fetchall()


_SCHEMAS_QUERY = """
SELECT nspname AS schema_name
FROM pg_catalog.pg_namespace
WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
  AND (%s::text[] IS NULL OR nspname = ANY(%s::text[]))
ORDER BY nspname
"""

# Other catalog queries take a single text[] argument containing selected schemas.
_ENUMS_QUERY = """
SELECT n.nspname AS schema_name, t.typname AS type_name, e.enumlabel AS label
FROM pg_catalog.pg_type AS t
JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
JOIN pg_catalog.pg_enum AS e ON e.enumtypid = t.oid
WHERE n.nspname = ANY(%s)
ORDER BY n.nspname, t.typname, e.enumsortorder
"""

_DOMAINS_QUERY = """
SELECT n.nspname AS schema_name, t.typname AS type_name,
       pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base_type,
       t.typnotnull AS not_null, t.typdefault AS default_expr,
       CASE WHEN t.typcollation <> base.typcollation THEN cn.nspname END AS collation_schema,
       col.collname AS collation_name
FROM pg_catalog.pg_type AS t
JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
JOIN pg_catalog.pg_type AS base ON base.oid = t.typbasetype
LEFT JOIN pg_catalog.pg_collation AS col ON col.oid = t.typcollation
LEFT JOIN pg_catalog.pg_namespace AS cn ON cn.oid = col.collnamespace
WHERE t.typtype = 'd' AND n.nspname = ANY(%s)
ORDER BY n.nspname, t.typname
"""

_DOMAIN_CONSTRAINTS_QUERY = """
SELECT n.nspname AS schema_name, t.typname AS type_name,
       con.conname AS constraint_name,
       pg_catalog.pg_get_constraintdef(con.oid, false) AS definition
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_type AS t ON t.oid = con.contypid
JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
WHERE n.nspname = ANY(%s)
  AND con.contype <> 'n'
ORDER BY n.nspname, t.typname, con.conname
"""

_COMPOSITES_QUERY = """
SELECT n.nspname AS schema_name, t.typname AS type_name,
       a.attname AS attribute_name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
       CASE WHEN a.attcollation <> at.typcollation THEN cn.nspname END AS collation_schema,
       col.collname AS collation_name
FROM pg_catalog.pg_type AS t
JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
JOIN pg_catalog.pg_class AS c ON c.oid = t.typrelid
JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
JOIN pg_catalog.pg_type AS at ON at.oid = a.atttypid
LEFT JOIN pg_catalog.pg_collation AS col ON col.oid = a.attcollation
LEFT JOIN pg_catalog.pg_namespace AS cn ON cn.oid = col.collnamespace
WHERE t.typtype = 'c' AND c.relkind = 'c'
  AND n.nspname = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY n.nspname, t.typname, a.attnum
"""

_SEQUENCES_QUERY = """
SELECT n.nspname AS schema_name, c.relname AS sequence_name,
       s.seqstart AS start_value, s.seqincrement AS increment_by,
       pg_catalog.format_type(s.seqtypid, NULL) AS data_type,
       s.seqmax AS max_value, s.seqmin AS min_value,
       s.seqcache AS cache_size, s.seqcycle AS cycle,
       tn.nspname AS owner_schema, tc.relname AS owner_table, a.attname AS owner_column,
       d.deptype AS dependency_type
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_sequence AS s ON s.seqrelid = c.oid
LEFT JOIN pg_catalog.pg_depend AS d
  ON d.classid = 'pg_catalog.pg_class'::regclass
 AND d.objid = c.oid AND d.refclassid = 'pg_catalog.pg_class'::regclass
 AND d.deptype IN ('a', 'i')
LEFT JOIN pg_catalog.pg_class AS tc ON tc.oid = d.refobjid
LEFT JOIN pg_catalog.pg_namespace AS tn ON tn.oid = tc.relnamespace
LEFT JOIN pg_catalog.pg_attribute AS a
  ON a.attrelid = tc.oid AND a.attnum = d.refobjsubid
WHERE c.relkind = 'S' AND n.nspname = ANY(%s)
ORDER BY n.nspname, c.relname
"""

_TABLES_QUERY = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       a.attname AS column_name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null,
       pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_expr,
       a.attidentity AS identity_kind, a.attgenerated AS generated_kind,
       pg_catalog.col_description(c.oid, a.attnum) AS comment,
       CASE WHEN a.attcollation <> t.typcollation THEN cn.nspname END AS collation_schema,
       col.collname AS collation_name,
       pg_catalog.pg_get_partkeydef(c.oid) AS partition_by,
       pg_catalog.pg_get_expr(c.relpartbound, c.oid, true) AS partition_bound,
       pn.nspname AS parent_schema, pc.relname AS parent_table
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
LEFT JOIN pg_catalog.pg_attrdef AS d
  ON d.adrelid = a.attrelid AND d.adnum = a.attnum
LEFT JOIN pg_catalog.pg_collation AS col ON col.oid = a.attcollation
LEFT JOIN pg_catalog.pg_namespace AS cn ON cn.oid = col.collnamespace
LEFT JOIN pg_catalog.pg_inherits AS inh
  ON inh.inhrelid = c.oid AND c.relispartition
LEFT JOIN pg_catalog.pg_class AS pc ON pc.oid = inh.inhparent
LEFT JOIN pg_catalog.pg_namespace AS pn ON pn.oid = pc.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s)
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum
"""

_CONSTRAINTS_QUERY = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       con.conname AS constraint_name, con.contype AS constraint_type,
       pg_catalog.pg_get_constraintdef(con.oid, false) AS definition,
       refn.nspname AS ref_schema, refc.relname AS ref_table
FROM pg_catalog.pg_constraint AS con
JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_class AS refc ON refc.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace AS refn ON refn.oid = refc.relnamespace
WHERE n.nspname = ANY(%s)
  AND con.conparentid = 0
  AND con.contype <> 'n'
ORDER BY n.nspname, c.relname, con.conname
"""

_INDEXES_QUERY = """
SELECT i.schemaname AS schema_name, i.tablename AS table_name,
       i.indexdef AS definition, parent_ns.nspname AS parent_schema,
       parent_index.relname AS parent_index_name, index_class.relname AS index_name
FROM pg_catalog.pg_indexes AS i
JOIN pg_catalog.pg_namespace AS index_ns ON index_ns.nspname = i.schemaname
JOIN pg_catalog.pg_class AS index_class
  ON index_class.relnamespace = index_ns.oid AND index_class.relname = i.indexname
LEFT JOIN pg_catalog.pg_inherits AS inh ON inh.inhrelid = index_class.oid
LEFT JOIN pg_catalog.pg_class AS parent_index ON parent_index.oid = inh.inhparent
LEFT JOIN pg_catalog.pg_namespace AS parent_ns ON parent_ns.oid = parent_index.relnamespace
WHERE i.schemaname = ANY(%s)
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_constraint AS con
    WHERE con.conindid = index_class.oid AND con.contype IN ('p', 'u', 'x')
  )
ORDER BY i.schemaname, i.tablename, i.indexname
"""

_VIEWS_QUERY = """
SELECT n.nspname AS schema_name, c.relname AS view_name,
       c.relkind AS view_kind,
       pg_catalog.pg_get_viewdef(c.oid, true) AS definition,
       c.relispopulated AS populated
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v', 'm') AND n.nspname = ANY(%s)
ORDER BY n.nspname, c.relname
"""

_VIEW_DEPENDENCIES_QUERY = """
SELECT DISTINCT source_ns.nspname AS view_schema, source.relname AS view_name,
       dependency_ns.nspname AS dependency_schema, dependency.relname AS dependency_name
FROM pg_catalog.pg_class AS source
JOIN pg_catalog.pg_namespace AS source_ns ON source_ns.oid = source.relnamespace
JOIN pg_catalog.pg_rewrite AS rewrite ON rewrite.ev_class = source.oid
JOIN pg_catalog.pg_depend AS d
  ON d.classid = 'pg_catalog.pg_rewrite'::regclass
 AND d.objid = rewrite.oid
 AND d.refclassid = 'pg_catalog.pg_class'::regclass
JOIN pg_catalog.pg_class AS dependency ON dependency.oid = d.refobjid
JOIN pg_catalog.pg_namespace AS dependency_ns ON dependency_ns.oid = dependency.relnamespace
WHERE source.relkind IN ('v', 'm') AND source_ns.nspname = ANY(%s)
  AND dependency.relkind IN ('v', 'm') AND dependency.oid <> source.oid
ORDER BY source_ns.nspname, source.relname, dependency_ns.nspname, dependency.relname
"""

_ROUTINES_QUERY = """
SELECT n.nspname AS schema_name, p.proname AS routine_name,
       pg_catalog.pg_get_functiondef(p.oid) AS definition
FROM pg_catalog.pg_proc AS p
JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
WHERE p.prokind IN ('f', 'p') AND n.nspname = ANY(%s)
  AND NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_depend AS d
    WHERE d.classid = 'pg_catalog.pg_proc'::regclass
      AND d.objid = p.oid AND d.deptype = 'e'
  )
ORDER BY n.nspname, p.proname, p.oid
"""

_TRIGGERS_QUERY = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       t.tgname AS trigger_name,
       pg_catalog.pg_get_triggerdef(t.oid, true) AS definition
FROM pg_catalog.pg_trigger AS t
JOIN pg_catalog.pg_class AS c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND n.nspname = ANY(%s)
ORDER BY n.nspname, c.relname, t.tgname
"""

_SCHEMA_COMMENTS_QUERY = """
SELECT n.nspname AS schema_name, pg_catalog.obj_description(n.oid, 'pg_namespace') AS comment
FROM pg_catalog.pg_namespace AS n
WHERE n.nspname = ANY(%s)
ORDER BY n.nspname
"""

_TABLE_COMMENTS_QUERY = """
SELECT n.nspname AS schema_name, c.relname AS table_name,
       pg_catalog.obj_description(c.oid, 'pg_class') AS comment
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s)
  AND pg_catalog.obj_description(c.oid, 'pg_class') IS NOT NULL
ORDER BY n.nspname, c.relname
"""


def _normalize_schemas(schemas: str | Iterable[str] | None) -> list[str] | None:
    if schemas is None:
        return None
    if isinstance(schemas, str):
        normalized = [schemas]
    else:
        normalized = list(schemas)
    if not normalized:
        raise ValueError("schemas must contain at least one schema name")
    if any(not isinstance(name, str) or not name for name in normalized):
        raise ValueError("schema names must be non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError("schema names must not contain duplicates")
    return normalized


def _normalize_patterns(value: str | Iterable[str] | None, label: str) -> list[str]:
    if value is None:
        return []
    patterns = [value] if isinstance(value, str) else list(value)
    if any(not isinstance(p, str) or not p for p in patterns):
        raise ValueError(f"{label} must be non-empty strings")
    return patterns


def _table_matches(patterns: list[str], schema: str, table: str) -> bool:
    # "schema.table" patterns match qualified names; bare patterns match table names.
    return any(
        fnmatchcase(f"{schema}.{table}" if "." in pattern else table, pattern)
        for pattern in patterns
    )


_TABLE_SCOPED_EMPTY = (
    "enums",
    "domains",
    "domain_constraints",
    "composites",
    "views",
    "view_dependencies",
    "routines",
)


def _filter_tables(
    catalog: Mapping[str, list[Mapping[str, Any]]],
    schemas: list[str],
    include: list[str],
    exclude: list[str],
) -> tuple[dict[str, list[Mapping[str, Any]]], list[str]]:
    """Restrict a catalog to selected tables and the objects that belong to them."""
    all_tables = {(r["schema_name"], r["table_name"]) for r in catalog["tables"]}
    selected = {
        key
        for key in all_tables
        if (not include or _table_matches(include, *key))
        and not _table_matches(exclude, *key)
    }
    for pattern in include:
        if not any(_table_matches([pattern], *key) for key in all_tables):
            raise ValueError(f"no table matches {pattern!r}")
    if not selected:
        raise ValueError("table filters did not select any tables")
    for row in catalog["tables"]:
        parent = (row["parent_schema"], row["parent_table"])
        key = (row["schema_name"], row["table_name"])
        if parent[0] and key in selected and parent not in selected:
            raise ValueError(
                f"partition {key[0]}.{key[1]} requires its parent "
                f"{parent[0]}.{parent[1]} to be selected"
            )

    def keep(row: Mapping[str, Any], schema_key: str = "schema_name", table_key: str = "table_name") -> bool:
        return (row[schema_key], row[table_key]) in selected

    filtered: dict[str, list[Mapping[str, Any]]] = {name: [] for name in _TABLE_SCOPED_EMPTY}
    filtered["tables"] = [r for r in catalog["tables"] if keep(r)]
    filtered["constraints"] = [
        r
        for r in catalog["constraints"]
        if keep(r) and (r["ref_table"] is None or (r["ref_schema"], r["ref_table"]) in selected)
    ]
    filtered["indexes"] = [r for r in catalog["indexes"] if keep(r)]
    filtered["triggers"] = [r for r in catalog["triggers"] if keep(r)]
    filtered["table_comments"] = [r for r in catalog["table_comments"] if keep(r)]
    filtered["sequences"] = [
        r
        for r in catalog["sequences"]
        if r["owner_table"] and keep(r, "owner_schema", "owner_table")
    ]
    kept_schemas = [s for s in schemas if any(k[0] == s for k in selected)]
    filtered["schema_comments"] = [
        r for r in catalog["schema_comments"] if r["schema_name"] in kept_schemas
    ]
    return filtered, kept_schemas


def _fetch_catalog(cursor: Any, schemas: list[str]) -> dict[str, list[Mapping[str, Any]]]:
    return {
        "enums": _rows(cursor, _ENUMS_QUERY, schemas),
        "domains": _rows(cursor, _DOMAINS_QUERY, schemas),
        "domain_constraints": _rows(cursor, _DOMAIN_CONSTRAINTS_QUERY, schemas),
        "composites": _rows(cursor, _COMPOSITES_QUERY, schemas),
        "sequences": _rows(cursor, _SEQUENCES_QUERY, schemas),
        "tables": _rows(cursor, _TABLES_QUERY, schemas),
        "constraints": _rows(cursor, _CONSTRAINTS_QUERY, schemas),
        "indexes": _rows(cursor, _INDEXES_QUERY, schemas),
        "views": _rows(cursor, _VIEWS_QUERY, schemas),
        "view_dependencies": _rows(cursor, _VIEW_DEPENDENCIES_QUERY, schemas),
        "routines": _rows(cursor, _ROUTINES_QUERY, schemas),
        "triggers": _rows(cursor, _TRIGGERS_QUERY, schemas),
        "schema_comments": _rows(cursor, _SCHEMA_COMMENTS_QUERY, schemas),
        "table_comments": _rows(cursor, _TABLE_COMMENTS_QUERY, schemas),
    }


def _render(catalog: Mapping[str, list[Mapping[str, Any]]], schemas: list[str]) -> str:
    statements: list[str] = []

    for schema in schemas:
        statements.append(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)};")

    schema_comments = {
        row["schema_name"]: row["comment"] for row in catalog["schema_comments"] if row["comment"]
    }
    for schema in schemas:
        if schema_comments.get(schema):
            statements.append(
                f"COMMENT ON SCHEMA {quote_ident(schema)} IS "
                f"{quote_literal(schema_comments[schema])};"
            )

    enums: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in catalog["enums"]:
        enums[(row["schema_name"], row["type_name"])].append(row["label"])
    for (schema, name), labels in sorted(enums.items()):
        values = ", ".join(quote_literal(label) for label in labels)
        statements.append(f"CREATE TYPE {_qualified(schema, name)} AS ENUM ({values});")

    domain_constraints: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in catalog["domain_constraints"]:
        domain_constraints[(row["schema_name"], row["type_name"])].append(row)
    for row in catalog["domains"]:
        definition = (
            f"CREATE DOMAIN {_qualified(row['schema_name'], row['type_name'])} "
            f"AS {row['base_type']}"
        )
        if row["collation_schema"]:
            definition += (
                f" COLLATE {_qualified(row['collation_schema'], row['collation_name'])}"
            )
        if row["default_expr"] is not None:
            definition += f" DEFAULT {row['default_expr']}"
        if row["not_null"]:
            definition += " NOT NULL"
        for constraint in domain_constraints[(row["schema_name"], row["type_name"])]:
            definition += (
                f" CONSTRAINT {quote_ident(constraint['constraint_name'])} "
                f"{constraint['definition']}"
            )
        statements.append(definition + ";")

    composites: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in catalog["composites"]:
        composites[(row["schema_name"], row["type_name"])].append(row)
    for (schema, name), attributes in sorted(composites.items()):
        fields = ",\n  ".join(
            f"{quote_ident(row['attribute_name'])} {row['data_type']}"
            + (
                f" COLLATE {_qualified(row['collation_schema'], row['collation_name'])}"
                if row["collation_schema"]
                else ""
            )
            for row in attributes
        )
        statements.append(f"CREATE TYPE {_qualified(schema, name)} AS (\n  {fields}\n);")

    sequences = catalog["sequences"]
    for row in sequences:
        # Identity sequences are created as part of the table's IDENTITY column.
        if row["dependency_type"] == "i":
            continue
        parts = [
            f"CREATE SEQUENCE {_qualified(row['schema_name'], row['sequence_name'])}",
            f"AS {row['data_type']}",
            f"INCREMENT BY {row['increment_by']}",
            f"MINVALUE {row['min_value']}",
            f"MAXVALUE {row['max_value']}",
            f"START WITH {row['start_value']}",
            f"CACHE {row['cache_size']}",
            "CYCLE" if row["cycle"] else "NO CYCLE",
        ]
        statements.append(" ".join(parts) + ";")

    for row in catalog["routines"]:
        statements.append(row["definition"].rstrip().rstrip(";") + ";")

    tables: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in catalog["tables"]:
        tables[(row["schema_name"], row["table_name"])].append(row)
    constraints: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in catalog["constraints"]:
        constraints[(row["schema_name"], row["table_name"])].append(row)

    foreign_keys: list[Mapping[str, Any]] = []
    column_comments: list[tuple[str, str, str, str]] = []
    ordered_tables: list[tuple[str, str]] = []
    visited: set[tuple[str, str]] = set()

    def visit_table(key: tuple[str, str]) -> None:
        if key in visited:
            return
        visited.add(key)
        columns = tables[key]
        parent_schema = columns[0]["parent_schema"]
        parent_table = columns[0]["parent_table"]
        if parent_schema and parent_table:
            parent_key = (parent_schema, parent_table)
            if parent_key in tables:
                visit_table(parent_key)
        ordered_tables.append(key)

    for table_key in sorted(tables):
        visit_table(table_key)

    for schema, table in ordered_tables:
        columns = tables[(schema, table)]
        first_column = columns[0]
        for column in columns:
            if column["comment"] is not None:
                column_comments.append((schema, table, column["column_name"], column["comment"]))
        parent_schema = first_column["parent_schema"]
        parent_table = first_column["parent_table"]
        if parent_schema and parent_table:
            bound = first_column["partition_bound"]
            if not bound:
                raise ValueError(f"partition bound missing for {schema}.{table}")
            statements.append(
                f"CREATE TABLE {_qualified(schema, table)} PARTITION OF "
                f"{_qualified(parent_schema, parent_table)} {bound};"
            )
            continue

        definitions: list[str] = []
        for column in columns:
            name = quote_ident(column["column_name"])
            definition = f"{name} {column['data_type']}"
            if column["collation_schema"]:
                definition += (
                    f" COLLATE {_qualified(column['collation_schema'], column['collation_name'])}"
                )
            if column["identity_kind"]:
                identity = "ALWAYS" if column["identity_kind"] == "a" else "BY DEFAULT"
                definition += f" GENERATED {identity} AS IDENTITY"
            elif column["generated_kind"]:
                storage = "VIRTUAL" if column["generated_kind"] == "v" else "STORED"
                definition += f" GENERATED ALWAYS AS ({column['default_expr']}) {storage}"
            elif column["default_expr"] is not None:
                definition += f" DEFAULT {column['default_expr']}"
            if column["not_null"]:
                definition += " NOT NULL"
            definitions.append(definition)

        for constraint in constraints[(schema, table)]:
            if constraint["constraint_type"] == "f":
                foreign_keys.append(constraint)
            else:
                definitions.append(
                    f"CONSTRAINT {quote_ident(constraint['constraint_name'])} "
                    f"{constraint['definition']}"
                )
        body = ",\n  ".join(definitions)
        statement = f"CREATE TABLE {_qualified(schema, table)} (\n  {body}\n)"
        if first_column["partition_by"]:
            statement += f" PARTITION BY {first_column['partition_by']}"
        statements.append(statement + ";")

    table_comments = {
        (row["schema_name"], row["table_name"]): row["comment"]
        for row in catalog["table_comments"]
        if row["comment"]
    }
    for (schema, table), comment in sorted(table_comments.items()):
        statements.append(
            f"COMMENT ON TABLE {_qualified(schema, table)} IS {quote_literal(comment)};"
        )
    for schema, table, column, comment in column_comments:
        statements.append(
            f"COMMENT ON COLUMN {_qualified(schema, table)}.{quote_ident(column)} "
            f"IS {quote_literal(comment)};"
        )

    views = {
        (row["schema_name"], row["view_name"]): row for row in catalog["views"]
    }
    dependencies: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for row in catalog["view_dependencies"]:
        dependencies[(row["view_schema"], row["view_name"])].add(
            (row["dependency_schema"], row["dependency_name"])
        )
    ordered_views: list[tuple[str, str]] = []
    visiting: set[tuple[str, str]] = set()
    visited_views: set[tuple[str, str]] = set()

    def visit_view(key: tuple[str, str]) -> None:
        if key in visited_views:
            return
        if key in visiting:
            raise ValueError(f"cyclic view dependency detected at {key[0]}.{key[1]}")
        visiting.add(key)
        for dependency in sorted(dependencies[key]):
            if dependency in views:
                visit_view(dependency)
        visiting.remove(key)
        visited_views.add(key)
        ordered_views.append(key)

    for view_key in sorted(views):
        visit_view(view_key)

    for key in ordered_views:
        row = views[key]
        kind = "VIEW" if row["view_kind"] == "v" else "MATERIALIZED VIEW"
        statement = (
            f"CREATE {kind} {_qualified(row['schema_name'], row['view_name'])} "
            f"AS\n{row['definition'].rstrip(';')}"
        )
        if kind == "MATERIALIZED VIEW" and not row["populated"]:
            statement += "\nWITH NO DATA"
        statements.append(statement + ";")

    for row in catalog["indexes"]:
        statements.append(row["definition"].rstrip(";") + ";")

    for row in catalog["indexes"]:
        if row.get("parent_index_name"):
            statements.append(
                f"ALTER INDEX {_qualified(row['parent_schema'], row['parent_index_name'])} "
                f"ATTACH PARTITION "
                f"{_qualified(row['schema_name'], row['index_name'])};"
            )

    # Foreign keys go after indexes: they may reference columns covered only by a unique index.
    for row in foreign_keys:
        statements.append(
            f"ALTER TABLE {_qualified(row['schema_name'], row['table_name'])} "
            f"ADD CONSTRAINT {quote_ident(row['constraint_name'])} {row['definition']};"
        )

    for row in catalog["triggers"]:
        statements.append(row["definition"].rstrip(";") + ";")

    for row in sequences:
        if row["dependency_type"] == "a" and row["owner_table"] and row["owner_column"]:
            statements.append(
                f"ALTER SEQUENCE {_qualified(row['schema_name'], row['sequence_name'])} "
                f"OWNED BY {_qualified(row['owner_schema'], row['owner_table'])}."
                f"{quote_ident(row['owner_column'])};"
            )

    return "\n\n".join(statements) + ("\n" if statements else "")


def export_ddl(
    dsn: str,
    schemas: str | Iterable[str] | None = None,
    tables: str | Iterable[str] | None = None,
    exclude_tables: str | Iterable[str] | None = None,
) -> str:
    """Return PostgreSQL DDL for the selected schemas and tables.

    With ``schemas=None``, all non-system schemas are exported. A schema name or
    iterable of names can be provided to limit the export.

    ``tables`` and ``exclude_tables`` select tables by name (``orders``), qualified
    name (``sales.orders``), or ``fnmatch`` wildcard (``audit_*``, ``sales.*``).
    When either is given, only the selected tables are exported together with
    their columns, constraints, indexes, triggers, comments and owned sequences.
    Foreign keys to unselected tables are skipped, partitions require their parent
    to be selected, and shared objects (types, domains, functions, views) are not
    exported, so they must already exist where the SQL is applied.

    The exporter connects with psycopg and reads PostgreSQL's system catalogs;
    it does not invoke the ``pg_dump`` executable.
    """
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("dsn must be a non-empty PostgreSQL connection string")
    selected = _normalize_schemas(schemas)
    include = _normalize_patterns(tables, "tables")
    exclude = _normalize_patterns(exclude_tables, "exclude_tables")
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            # Make deparsed expressions qualify non-system objects consistently.
            cursor.execute("SET search_path TO ''")
            cursor.execute(
                _SCHEMAS_QUERY,
                (selected, selected),
            )
            schema_names = [row["schema_name"] for row in cursor.fetchall()]
            missing = sorted(set(selected or ()) - set(schema_names))
            if missing:
                raise ValueError(f"PostgreSQL schemas not found: {', '.join(missing)}")
            if not schema_names:
                requested = ", ".join(selected or [])
                raise ValueError(f"no matching PostgreSQL schemas found: {requested or '(none)'}")
            catalog = _fetch_catalog(cursor, schema_names)
            if include or exclude:
                catalog, schema_names = _filter_tables(catalog, schema_names, include, exclude)
            return _render(catalog, schema_names)
