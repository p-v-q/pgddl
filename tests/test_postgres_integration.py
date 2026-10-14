from __future__ import annotations

import os
import re
import subprocess
import unittest
import uuid
from difflib import unified_diff

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from pgddl import export_ddl


POSTGRES_TYPES = (
    "smallint",
    "integer",
    "bigint",
    "numeric(12, 3)",
    "real",
    "double precision",
    "boolean",
    "character(5)",
    "character varying(40)",
    "text",
    "date",
    "time(3)",
    "time with time zone",
    "timestamp(3)",
    "timestamp with time zone",
    "interval day to second",
    "uuid",
    "json",
    "jsonb",
    "bytea",
    "smallint[]",
    "text[][]",
    "int4range",
    "int8range",
    "numrange",
    "tsrange",
    "tstzrange",
    "daterange",
    "bit(7)",
    "bit varying(11)",
    "inet",
    "cidr",
    "macaddr",
    "oid",
    "xml",
    "point",
    "line",
    "lseg",
    "box",
    "path",
    "polygon",
    "circle",
    "tsvector",
    "tsquery",
    "money",
)

TEST_DSN = os.environ.get("PGDDL_TEST_DSN")


@unittest.skipUnless(TEST_DSN, "set PGDDL_TEST_DSN to run PostgreSQL integration tests")
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.admin_dsn = TEST_DSN
        cls.database_name = f"pgddl_test_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(cls.admin_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database_name))
            )
        cls.test_dsn = make_conninfo(cls.admin_dsn, dbname=cls.database_name)

    @classmethod
    def tearDownClass(cls) -> None:
        with psycopg.connect(cls.admin_dsn, autocommit=True) as connection:
            connection.execute(
                """
                SELECT pg_catalog.pg_terminate_backend(pid)
                FROM pg_catalog.pg_stat_activity
                WHERE datname = %s AND pid <> pg_catalog.pg_backend_pid()
                """,
                (cls.database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(cls.database_name))
            )

    def setUp(self) -> None:
        suffix = re.sub(r"[^a-z0-9_]", "_", self.id().lower())[-35:]
        self.schema_name = f"pgddl_{suffix}"
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema_name))
            )

    def tearDown(self) -> None:
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema_name)
                )
            )

    def test_full_schema_round_trip_matches_pg_dump(self) -> None:
        schema = sql.Identifier(self.schema_name)
        schema_text = self.schema_name
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                sql.SQL(
                    """
                    CREATE TYPE {}.mood AS ENUM ('new', 'ready', 'it''s done');
                    CREATE DOMAIN {}.positive_id AS integer NOT NULL
                      CONSTRAINT positive_id_check CHECK (VALUE > 0);
                    CREATE DOMAIN {}.normalized_label AS text COLLATE "C"
                      DEFAULT 'not set';
                    CREATE TYPE {}.postal_address AS (
                      street text COLLATE "C",
                      zip_code integer
                    );
                    CREATE SEQUENCE {}.manual_seq
                      AS bigint INCREMENT BY 3 MINVALUE 5 MAXVALUE 500
                      START WITH 11 CACHE 2 CYCLE;

                    CREATE TABLE {}.companies (
                      id {}.positive_id PRIMARY KEY,
                      name {}.normalized_label NOT NULL UNIQUE
                    );
                    CREATE TABLE {}.customers (
                      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      company_id {}.positive_id NOT NULL,
                      mood {}.mood NOT NULL DEFAULT 'new',
                      address {}.postal_address,
                      labels text[] NOT NULL DEFAULT ARRAY[]::text[],
                      metadata jsonb,
                      updated_at timestamp with time zone NOT NULL DEFAULT now(),
                      generated_label integer GENERATED ALWAYS AS (cardinality(labels)) STORED,
                      manual_id bigint NOT NULL DEFAULT nextval({}::regclass),
                      CONSTRAINT customer_company_fk FOREIGN KEY (company_id)
                        REFERENCES {}.companies(id) ON DELETE CASCADE,
                      CONSTRAINT customer_manual_positive CHECK (manual_id > 0)
                    );
                    ALTER SEQUENCE {}.manual_seq OWNED BY {}.customers.manual_id;
                    CREATE INDEX customers_mood_idx
                      ON {}.customers USING btree (mood)
                      WHERE mood <> 'new';
                    CREATE INDEX customers_labels_gin
                      ON {}.customers USING gin (labels);

                    CREATE TABLE {}.events (
                      event_id integer NOT NULL,
                      occurred_on date NOT NULL,
                      payload jsonb,
                      PRIMARY KEY (event_id, occurred_on)
                    ) PARTITION BY RANGE (occurred_on);
                    CREATE TABLE {}.events_2026 PARTITION OF {}.events
                      FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
                    CREATE INDEX events_payload_idx ON {}.events USING gin (payload);

                    CREATE FUNCTION {}.touch_updated_at()
                      RETURNS trigger LANGUAGE plpgsql AS $function$
                    BEGIN
                      NEW.updated_at = CURRENT_TIMESTAMP;
                      RETURN NEW;
                    END
                    $function$;
                    CREATE TRIGGER customers_touch_updated_at
                      BEFORE UPDATE ON {}.customers
                      FOR EACH ROW EXECUTE FUNCTION {}.touch_updated_at();

                    CREATE VIEW {}.customer_names AS
                      SELECT id, generated_label FROM {}.customers;
                    CREATE VIEW {}.customer_summary AS
                      SELECT id FROM {}.customer_names;
                    CREATE MATERIALIZED VIEW {}.customer_totals AS
                      SELECT count(*) AS total FROM {}.customers;
                    COMMENT ON SCHEMA {} IS 'owner''s schema';
                    COMMENT ON TABLE {}.customers IS E'customer\\nrecords';
                    COMMENT ON COLUMN {}.customers.generated_label IS 'derived';
                    """
                ).format(
                    schema,  # enum
                    schema,  # domain
                    schema,  # collated domain
                    schema,  # composite type
                    schema,  # manual sequence
                    schema, schema, schema,  # companies table and domain types
                    schema,  # customers table
                    schema,  # company_id domain
                    schema,  # mood enum
                    schema,  # address composite
                    sql.Literal(f"{schema_text}.manual_seq"),
                    schema,  # FK target schema
                    schema,  # owned sequence
                    schema,  # sequence owner table
                    schema,  # mood index
                    schema,  # labels index
                    schema,  # partitioned parent table
                    schema, schema,  # partition child and parent
                    schema,  # partitioned index
                    schema,  # trigger function
                    schema, schema,  # trigger table and function
                    schema, schema,  # first view and its table
                    schema, schema,  # dependent view and its view
                    schema, schema,  # materialized view and its table
                    schema,  # schema comment
                    schema,  # table comment
                    schema,  # column comment
                )
            )

        ddl = export_ddl(self.test_dsn, self.schema_name)
        self.assertIn("CREATE TYPE", ddl)
        self.assertIn("CREATE DOMAIN", ddl)
        self.assertIn("CREATE SEQUENCE", ddl)
        self.assertIn("PARTITION OF", ddl)
        self.assertIn("CREATE TRIGGER", ddl)
        self.assertIn("CREATE MATERIALIZED VIEW", ddl)

        source_dump = self._pg_dump_schema()
        self.assertIn("CREATE TYPE", source_dump)
        self.assertIn("CREATE FUNCTION", source_dump)
        self.assertIn("CREATE MATERIALIZED VIEW", source_dump)

        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(schema)
            )
            connection.execute(ddl)

        restored_dump = self._pg_dump_schema()
        source_canonical = self._canonical_dump(source_dump)
        restored_canonical = self._canonical_dump(restored_dump)
        self.assertEqual(
            source_canonical,
            restored_canonical,
            "\n".join(
                unified_diff(
                    source_canonical.splitlines(),
                    restored_canonical.splitlines(),
                    fromfile="pg_dump source",
                    tofile="pg_dump after pgddl restore",
                    lineterm="",
                )
            ),
        )

    def test_export_of_empty_schema_is_valid_and_executable(self) -> None:
        ddl = export_ddl(self.test_dsn, self.schema_name)
        self.assertIn(f'CREATE SCHEMA IF NOT EXISTS "{self.schema_name}";', ddl)
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {}").format(sql.Identifier(self.schema_name))
            )
            connection.execute(ddl)
            found = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s)",
                (self.schema_name,),
            ).fetchone()[0]
        self.assertTrue(found)

    def test_sequence_types_and_fk_to_unique_index(self) -> None:
        s = self.schema_name
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                f"""
                CREATE SEQUENCE "{s}".small_seq AS smallint;
                CREATE SEQUENCE "{s}".int_seq AS integer;
                CREATE TABLE "{s}".parent (a int, b int);
                CREATE UNIQUE INDEX parent_ab ON "{s}".parent (a, b);
                CREATE TABLE "{s}".child (
                  a int, b int, FOREIGN KEY (a, b) REFERENCES "{s}".parent (a, b)
                );
                """
            )
        ddl = export_ddl(self.test_dsn, s)
        self.assertIn("AS smallint", ddl)
        self.assertIn("AS integer", ddl)
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(f'DROP SCHEMA "{s}" CASCADE')
            connection.execute(ddl)

    def test_table_selection(self) -> None:
        s = self.schema_name
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                f"""
                CREATE TABLE "{s}".users (id serial PRIMARY KEY, name text);
                CREATE TABLE "{s}".orders (
                  id integer PRIMARY KEY,
                  user_id integer REFERENCES "{s}".users (id)
                );
                CREATE INDEX orders_user_idx ON "{s}".orders (user_id);
                COMMENT ON TABLE "{s}".orders IS 'the orders';
                CREATE TABLE "{s}".audit_a (id integer);
                CREATE TABLE "{s}".audit_b (id integer);
                """
            )
        only_orders = export_ddl(self.test_dsn, s, tables="orders")
        self.assertIn('orders_user_idx', only_orders)
        self.assertIn("the orders", only_orders)
        self.assertNotIn("users", only_orders)
        self.assertNotIn("FOREIGN KEY", only_orders)

        both = export_ddl(self.test_dsn, s, tables=["users", f"{s}.orders"])
        self.assertIn("FOREIGN KEY", both)
        self.assertIn("users_id_seq", both)
        self.assertNotIn("audit_a", both)

        wild = export_ddl(self.test_dsn, s, tables="audit_*")
        self.assertIn("audit_a", wild)
        self.assertIn("audit_b", wild)
        self.assertNotIn("orders", wild)

        excluded = export_ddl(self.test_dsn, s, exclude_tables="audit_*")
        self.assertNotIn("audit_a", excluded)
        self.assertIn("orders", excluded)

        with self.assertRaises(ValueError):
            export_ddl(self.test_dsn, s, tables="nosuch")
        with self.assertRaises(ValueError):
            export_ddl(self.test_dsn, s, tables="*", exclude_tables="*")

        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(f'DROP SCHEMA "{s}" CASCADE')
            connection.execute(both)
            n = connection.execute(
                "SELECT count(*) FROM pg_tables WHERE schemaname = %s", (s,)
            ).fetchone()[0]
        self.assertEqual(n, 2)

    def _pg_dump_schema(self) -> str:
        result = subprocess.run(
            [
                "pg_dump",
                self.test_dsn,
                "--schema-only",
                f"--schema={self.schema_name}",
                "--no-owner",
                "--no-privileges",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    @staticmethod
    def _canonical_dump(dump: str) -> str:
        ignored_prefixes = (
            "-- Dumped from database version ",
            "-- Dumped by pg_dump version ",
            "SET ",
            "SELECT pg_catalog.set_config(",
            "\\connect ",
            "\\restrict ",
            "\\unrestrict ",
        )
        return "\n".join(
            line.rstrip()
            for line in dump.splitlines()
            if line.strip() and not line.startswith(ignored_prefixes)
        )


def _type_round_trip_test(sql_type: str):
    def test(self: PostgreSQLIntegrationTests) -> None:
        qualified_table = sql.SQL("{}.typed_value").format(
            sql.Identifier(self.schema_name)
        )
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                sql.SQL("CREATE TABLE {} (value {})").format(
                    qualified_table, sql.SQL(sql_type)
                )
            )
            original_type = connection.execute(
                """
                SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = 'typed_value'
                  AND a.attname = 'value'
                """,
                (self.schema_name,),
            ).fetchone()[0]

        ddl = export_ddl(self.test_dsn, self.schema_name)
        with psycopg.connect(self.test_dsn) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(self.schema_name)
                )
            )
            connection.execute(ddl)
            restored_type = connection.execute(
                """
                SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = 'typed_value'
                  AND a.attname = 'value'
                """,
                (self.schema_name,),
            ).fetchone()[0]
        self.assertEqual(restored_type, original_type, sql_type)

    return test


for _index, _type in enumerate(POSTGRES_TYPES, start=1):
    _name = re.sub(r"[^a-z0-9]+", "_", _type.lower()).strip("_")
    setattr(
        PostgreSQLIntegrationTests,
        f"test_catalog_type_round_trip_{_index:02}_{_name}",
        _type_round_trip_test(_type),
    )
