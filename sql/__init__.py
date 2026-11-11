"""Module 3: SQL front end — lexer, parser, name resolution, logical plans."""

# Public re-exports for `from sql import compile_sql, SqlError`
from sql.errors import SqlError
from sql.pipeline import compile_sql

__all__ = ["SqlError", "compile_sql"]
