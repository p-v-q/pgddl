"""Command-line interface for pgddl."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

import psycopg

from pgddl import __version__
from pgddl.exporter import export_ddl


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgddl",
        description="Export PostgreSQL schema definitions as SQL.",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("PGDDL_DSN"),
        help="PostgreSQL connection string (or set PGDDL_DSN)",
    )
    parser.add_argument(
        "--schema",
        action="append",
        dest="schemas",
        help="schema to export; repeat to export multiple schemas (default: all non-system schemas)",
    )
    parser.add_argument(
        "-t",
        "--table",
        action="append",
        dest="tables",
        help="table to export (name, schema.name or wildcard); repeatable",
    )
    parser.add_argument(
        "-T",
        "--exclude-table",
        action="append",
        dest="exclude_tables",
        help="table to skip (name, schema.name or wildcard); repeatable",
    )
    parser.add_argument("-o", "--output", type=Path, help="write SQL to this file (default: stdout)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("provide --dsn or set PGDDL_DSN")
    try:
        ddl = export_ddl(args.dsn, args.schemas, args.tables, args.exclude_tables)
        if args.output:
            args.output.write_text(ddl, encoding="utf-8")
        else:
            sys.stdout.write(ddl)
    except (OSError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: error: {error}\n")
    except psycopg.Error as error:
        parser.exit(1, f"{parser.prog}: database error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
