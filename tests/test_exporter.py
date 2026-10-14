from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, patch

from pgddl.cli import main
from pgddl.exporter import (
    _normalize_schemas,
    _render,
    export_ddl,
    quote_ident,
    quote_literal,
)


class IdentifierQuotingTests(unittest.TestCase):
    def test_quote_ident_escapes_double_quotes(self):
        self.assertEqual(quote_ident('odd"name'), '"odd""name"')

    def test_quote_literal_escapes_single_quotes(self):
        self.assertEqual(quote_literal("it's"), "E'it''s'")

    def test_quote_literal_escapes_backslashes_and_newlines(self):
        self.assertEqual(quote_literal("a\\b\n"), "E'a\\\\b\\n'")

    def test_quote_literal_rejects_nul(self):
        with self.assertRaises(ValueError):
            quote_literal("invalid\0string")


class SchemaArgumentTests(unittest.TestCase):
    def test_string_is_one_schema(self):
        self.assertEqual(_normalize_schemas("public"), ["public"])

    def test_iterable_is_materialized(self):
        self.assertEqual(_normalize_schemas(iter(["public", "sales"])), ["public", "sales"])

    def test_invalid_schema_lists_are_rejected(self):
        for value in ([], ["public", ""], ["public", "public"], [None]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _normalize_schemas(value)

    def test_invalid_dsn_is_rejected_without_connecting(self):
        with patch("pgddl.exporter.psycopg.connect") as connect:
            for dsn in ("", "  ", None):
                with self.subTest(dsn=dsn), self.assertRaises(ValueError):
                    export_ddl(dsn)  # type: ignore[arg-type]
            connect.assert_not_called()


class DDLRenderingTests(unittest.TestCase):
    def test_exports_common_objects_and_quotes_values(self):
        catalog = {
            "enums": [
                {"schema_name": "public", "type_name": "mood", "label": "it's fine"},
                {"schema_name": "public", "type_name": "mood", "label": "ok"},
            ],
            "sequences": [
                {
                    "schema_name": "public",
                    "sequence_name": "users_id_seq",
                    "start_value": 1,
                    "increment_by": 1,
                    "min_value": 1,
                    "max_value": 999, "data_type": "bigint",
                    "cache_size": 1,
                    "cycle": False,
                    "owner_schema": "public",
                    "owner_table": "users",
                    "owner_column": "id",
                    "dependency_type": "a",
                },
                {
                    "schema_name": "public",
                    "sequence_name": "users_identity_seq",
                    "start_value": 1,
                    "increment_by": 1,
                    "min_value": 1,
                    "max_value": 999, "data_type": "bigint",
                    "cache_size": 1,
                    "cycle": False,
                    "owner_schema": "public",
                    "owner_table": "users",
                    "owner_column": "generated_id",
                    "dependency_type": "i",
                },
            ],
            "tables": [
                {
                    "schema_name": "public",
                    "table_name": "users",
                    "column_name": "id",
                    "data_type": "integer",
                    "not_null": True,
                    "default_expr": "nextval('users_id_seq'::regclass)",
                    "identity_kind": "",
                    "generated_kind": "",
                    "comment": "user's identifier",
                    "collation_schema": None,
                    "collation_name": None,
                    "partition_by": None,
                    "partition_bound": None,
                    "parent_schema": None,
                    "parent_table": None,
                },
                {
                    "schema_name": "public",
                    "table_name": "users",
                    "column_name": "generated_id",
                    "data_type": "integer",
                    "not_null": True,
                    "default_expr": None,
                    "identity_kind": "a",
                    "generated_kind": "",
                    "comment": None,
                    "collation_schema": None,
                    "collation_name": None,
                    "partition_by": None,
                    "partition_bound": None,
                    "parent_schema": None,
                    "parent_table": None,
                },
                {
                    "schema_name": "public",
                    "table_name": "users",
                    "column_name": "status",
                    "data_type": '"public"."mood"',
                    "not_null": False,
                    "default_expr": "'ok'::mood",
                    "identity_kind": "",
                    "generated_kind": "",
                    "comment": None,
                    "collation_schema": "public",
                    "collation_name": "case sensitive",
                    "partition_by": None,
                    "partition_bound": None,
                    "parent_schema": None,
                    "parent_table": None,
                },
            ],
            "domains": [
                {
                    "schema_name": "public",
                    "type_name": "positive_id",
                    "base_type": "integer",
                    "not_null": True,
                    "default_expr": None,
                    "collation_schema": None,
                    "collation_name": None,
                }
            ],
            "domain_constraints": [
                {
                    "schema_name": "public",
                    "type_name": "positive_id",
                    "constraint_name": "positive_id_check",
                    "definition": "CHECK (VALUE > 0)",
                }
            ],
            "composites": [
                {
                    "schema_name": "public",
                    "type_name": "address",
                    "attribute_name": "street name",
                    "data_type": "text",
                    "collation_schema": None,
                    "collation_name": None,
                }
            ],
            "constraints": [
                {
                    "schema_name": "public",
                    "table_name": "users",
                    "constraint_name": "users_pkey",
                    "constraint_type": "p",
                    "definition": "PRIMARY KEY (id)",
                },
                {
                    "schema_name": "public",
                    "table_name": "users",
                    "constraint_name": "users_mood_check",
                    "constraint_type": "c",
                    "definition": "CHECK (status IS NOT NULL)",
                },
                {
                    "schema_name": "public",
                    "table_name": "users",
                    "constraint_name": "users_org_fkey",
                    "constraint_type": "f",
                    "definition": "FOREIGN KEY (org_id) REFERENCES public.orgs(id)",
                },
            ],
            "indexes": [
                {"definition": 'CREATE INDEX "users_status_idx" ON public.users USING btree (status)'},
                {
                    "definition": 'CREATE INDEX "report_total_idx" ON public.report USING btree (total)'
                },
            ],
            "views": [
                {
                    "schema_name": "public",
                    "view_name": "active_users",
                    "view_kind": "v",
                    "definition": "SELECT id FROM public.users WHERE status = 'ok'::mood",
                    "populated": True,
                },
                {
                    "schema_name": "public",
                    "view_name": "report",
                    "view_kind": "m",
                    "definition": "SELECT count(*) AS total FROM public.users",
                    "populated": False,
                },
            ],
            "view_dependencies": [],
            "routines": [
                {
                    "definition": "CREATE FUNCTION public.normalize_name() RETURNS text LANGUAGE sql AS $$ SELECT 'name' $$"
                }
            ],
            "triggers": [
                {"definition": 'CREATE TRIGGER "users_touch" BEFORE UPDATE ON public.users FOR EACH ROW EXECUTE FUNCTION public.touch()'}
            ],
            "schema_comments": [{"schema_name": "public", "comment": "it's public"}],
            "table_comments": [{"schema_name": "public", "table_name": "users", "comment": "user table"}],
        }

        ddl = _render(catalog, ["public"])

        self.assertIn("CREATE SCHEMA IF NOT EXISTS", ddl)
        self.assertIn("CREATE TYPE", ddl)
        self.assertIn(
            "CREATE DOMAIN \"public\".\"positive_id\" AS integer NOT NULL "
            "CONSTRAINT \"positive_id_check\" CHECK (VALUE > 0)",
            ddl,
        )
        self.assertIn('CREATE TYPE "public"."address"', ddl)
        self.assertIn('"street name" text', ddl)
        self.assertIn("'it''s fine'", ddl)
        self.assertIn("CREATE SEQUENCE", ddl)
        self.assertNotIn("users_identity_seq", ddl)
        self.assertIn("GENERATED ALWAYS AS IDENTITY", ddl)
        self.assertIn("CONSTRAINT \"users_pkey\" PRIMARY KEY (id)", ddl)
        self.assertIn("ADD CONSTRAINT \"users_org_fkey\" FOREIGN KEY", ddl)
        self.assertIn("CREATE INDEX", ddl)
        self.assertIn("CREATE VIEW", ddl)
        self.assertIn("CREATE MATERIALIZED VIEW", ddl)
        self.assertLess(ddl.index("CREATE MATERIALIZED VIEW"), ddl.index("report_total_idx"))
        self.assertIn("CREATE FUNCTION public.normalize_name()", ddl)
        self.assertIn("CREATE TRIGGER \"users_touch\"", ddl)
        self.assertIn("WITH NO DATA", ddl)
        self.assertIn("OWNED BY \"public\".\"users\".\"id\"", ddl)
        self.assertIn("COMMENT ON COLUMN", ddl)
        self.assertIn("user''s identifier", ddl)
        self.assertIn('COLLATE "public"."case sensitive"', ddl)

    def test_empty_catalog_returns_schema_creation(self):
        catalog = {
            "enums": [],
            "domains": [],
            "domain_constraints": [],
            "composites": [],
            "sequences": [],
            "tables": [],
            "constraints": [],
            "indexes": [],
            "views": [],
            "view_dependencies": [],
            "routines": [],
            "triggers": [],
            "schema_comments": [],
            "table_comments": [],
        }
        self.assertEqual(_render(catalog, ["sales"]), 'CREATE SCHEMA IF NOT EXISTS "sales";\n')

    def test_partition_parent_is_created_before_child(self):
        catalog = {
            "enums": [],
            "domains": [],
            "domain_constraints": [],
            "composites": [],
            "sequences": [],
            "tables": [
                {
                    "schema_name": "public",
                    "table_name": "events_2025",
                    "column_name": "id",
                    "data_type": "integer",
                    "not_null": False,
                    "default_expr": None,
                    "identity_kind": "",
                    "generated_kind": "",
                    "comment": None,
                    "collation_schema": None,
                    "collation_name": None,
                    "partition_by": None,
                    "partition_bound": "FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')",
                    "parent_schema": "public",
                    "parent_table": "events",
                },
                {
                    "schema_name": "public",
                    "table_name": "events",
                    "column_name": "id",
                    "data_type": "integer",
                    "not_null": False,
                    "default_expr": None,
                    "identity_kind": "",
                    "generated_kind": "",
                    "comment": None,
                    "collation_schema": None,
                    "collation_name": None,
                    "partition_by": "RANGE (created_at)",
                    "partition_bound": None,
                    "parent_schema": None,
                    "parent_table": None,
                },
            ],
            "constraints": [],
            "indexes": [],
            "views": [],
            "view_dependencies": [],
            "routines": [],
            "triggers": [],
            "schema_comments": [],
            "table_comments": [],
        }

        ddl = _render(catalog, ["public"])

        self.assertLess(
            ddl.index('CREATE TABLE "public"."events"'),
            ddl.index('"public"."events_2025"'),
        )
        self.assertIn("PARTITION BY RANGE (created_at)", ddl)
        self.assertIn("PARTITION OF \"public\".\"events\" FOR VALUES", ddl)

    def test_views_are_created_after_their_dependencies(self):
        catalog = {
            "enums": [],
            "domains": [],
            "domain_constraints": [],
            "composites": [],
            "sequences": [],
            "tables": [],
            "constraints": [],
            "indexes": [],
            "views": [
                {
                    "schema_name": "public",
                    "view_name": "a_view",
                    "view_kind": "v",
                    "definition": "SELECT * FROM public.z_view",
                    "populated": True,
                },
                {
                    "schema_name": "public",
                    "view_name": "z_view",
                    "view_kind": "v",
                    "definition": "SELECT 1 AS value",
                    "populated": True,
                },
            ],
            "view_dependencies": [
                {
                    "view_schema": "public",
                    "view_name": "a_view",
                    "dependency_schema": "public",
                    "dependency_name": "z_view",
                }
            ],
            "routines": [],
            "triggers": [],
            "schema_comments": [],
            "table_comments": [],
        }

        ddl = _render(catalog, ["public"])

        self.assertLess(ddl.index('"z_view"'), ddl.index('"a_view"'))

    def test_renders_virtual_generated_columns(self):
        table = {
            "schema_name": "public",
            "table_name": "computed",
            "column_name": "doubled",
            "data_type": "integer",
            "not_null": False,
            "default_expr": "(base * 2)",
            "identity_kind": "",
            "generated_kind": "v",
            "comment": None,
            "collation_schema": None,
            "collation_name": None,
            "partition_by": None,
            "partition_bound": None,
            "parent_schema": None,
            "parent_table": None,
        }
        catalog = {
            "enums": [],
            "domains": [],
            "domain_constraints": [],
            "composites": [],
            "sequences": [],
            "tables": [table],
            "constraints": [],
            "indexes": [],
            "views": [],
            "view_dependencies": [],
            "routines": [],
            "triggers": [],
            "schema_comments": [],
            "table_comments": [],
        }

        self.assertIn(
            '"doubled" integer GENERATED ALWAYS AS ((base * 2)) VIRTUAL',
            _render(catalog, ["public"]),
        )


class ExportConnectionTests(unittest.TestCase):
    def test_exports_requested_schema_using_catalog_queries(self):
        class Cursor:
            def __init__(self):
                self.query = ""
                self.params = ()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, query, params=None):
                self.query, self.params = query, params

            def fetchall(self):
                if "WHERE nspname !~" in self.query:
                    return [{"schema_name": "public"}]
                return []

        class Connection:
            def __init__(self):
                self.db_cursor = Cursor()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return self.db_cursor

        connection = Connection()
        with patch("pgddl.exporter.psycopg.connect", return_value=connection) as connect:
            ddl = export_ddl("postgresql://localhost/db", "public")

        connect.assert_called_once_with("postgresql://localhost/db", row_factory=ANY)
        self.assertEqual(ddl, 'CREATE SCHEMA IF NOT EXISTS "public";\n')

    def test_requested_missing_schema_is_reported(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, query, _params=None):
                self.query = query

            def fetchall(self):
                return [{"schema_name": "public"}] if "WHERE nspname !~" in self.query else []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return Cursor()

        with patch("pgddl.exporter.psycopg.connect", return_value=Connection()):
            with self.assertRaisesRegex(ValueError, "nosuch"):
                export_ddl("postgresql://localhost/db", ["public", "nosuch"])


class CliTests(unittest.TestCase):
    def test_cli_writes_export_to_file(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "schema.sql"
            with patch("pgddl.cli.export_ddl", return_value="CREATE SCHEMA public;\n") as export:
                result = main(["--dsn", "postgresql://localhost/db", "--schema", "public", "-o", str(output)])

            self.assertEqual(result, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "CREATE SCHEMA public;\n")
            export.assert_called_once_with("postgresql://localhost/db", ["public"], None, None)

    def test_cli_requires_connection_string(self):
        with (
            patch.dict("os.environ", {"PGDDL_DSN": ""}),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            with self.assertRaises(SystemExit) as error:
                main([])
        self.assertEqual(error.exception.code, 2)


class TableMatchTests(unittest.TestCase):
    def test_patterns(self):
        from pgddl.exporter import _table_matches

        self.assertTrue(_table_matches(["orders"], "s", "orders"))
        self.assertTrue(_table_matches(["s.orders"], "s", "orders"))
        self.assertFalse(_table_matches(["t.orders"], "s", "orders"))
        self.assertTrue(_table_matches(["audit_*"], "s", "audit_x"))
        self.assertTrue(_table_matches(["s.*"], "s", "any"))
        self.assertFalse(_table_matches([], "s", "orders"))


if __name__ == "__main__":
    unittest.main()
