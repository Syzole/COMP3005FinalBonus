"""Standalone condition parse + evaluate (used by M2, M3, M4)."""

from engine.tokeniser import tokenise, TokenType
from engine.parser import Parser, ParseError
from engine.eval import matches, value_of, compare, EvalError
from engine.tree import format_condition

__all__ = [
    "parse_condition",
    "matches",
    "value_of",
    "compare",
    "format_condition",
    "ParseError",
    "EvalError",
]


def parse_condition(source: str):
    """Parse a condition string such as 'R.b = S.b' into an AST."""
    tokens = tokenise(source)
    parser = Parser(tokens)
    node = parser.condition()
    if parser.peek().type is not TokenType.EOF:
        raise ParseError(f"unexpected token after condition: {parser.peek().type}")
    return node
