"""SQL front-end errors"""

from __future__ import annotations


class SqlError(Exception):
    """error: <category>: <message> at line L, column C"""

    #  throw an error with enough information to locate the error

    def __init__(self, category: str, message: str, line: int = 1, column: int = 1):
        self.category = category
        self.message = message
        self.line = line
        self.column = column
        super().__init__(self.format())

    def format(self) -> str:
        return f"error: {self.category}: {self.message} at line {self.line}, column {self.column}"
