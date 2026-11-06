"""Hand-written SQL lexer (no regex)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from sql.errors import SqlError


KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "HAVING", "ORDER", "LIMIT",
    "AS", "DISTINCT", "JOIN", "ON", "UNION", "ALL", "INTERSECT", "EXCEPT",
    "CREATE", "TABLE", "INSERT", "INTO", "VALUES",
    "INT", "VARCHAR",
    "COUNT", "SUM", "AVG", "MIN", "MAX",
    "AND", "OR", "NOT",
    "ASC", "DESC",
}


class Tok(Enum):
    KEYWORD = auto() # example: SELECT, FROM, WHERE, etc.
    IDENT = auto() # example: name, age, city, etc.
    NUMBER = auto()
    STRING = auto()
    OP = auto()       # = <> < <= > >=
    COMMA = auto() #,
    DOT = auto() #.
    STAR = auto() #*
    LPAREN = auto() #(
    RPAREN = auto() #)
    SEMI = auto() #;
    EOF = auto() #end of file


@dataclass
class Token:
    type: Tok
    value: str
    line: int
    column: int


def lex(source: str) -> list[Token]: # similar to bonus 1, no longer case sensitive
    tokens: list[Token] = []
    i = 0
    n = len(source)
    line = 1
    col = 1

    def peek(k=0): # peek ahead k chars 
        return source[i + k] if i + k < n else ""

    def advance(): #move to next char 1 position and go to next line if necessary
        nonlocal i, line, col
        ch = source[i]
        i += 1  
        if ch == "\n":
            line += 1
            col = 1
        else:
            col += 1
        return ch
 
    while i < n: #
        ch = peek()
        if ch in " \t\r\n": # skip whitespace
            advance()
            continue
        # comment
        if ch == "-" and peek(1) == "-": #added comments to this
            while i < n and peek() != "\n":
                advance()
            continue

        start_line, start_col = line, col # track the start of the token

        # begin handling special characters and maximal munch
        if ch == ",":
            advance()
            tokens.append(Token(Tok.COMMA, ",", start_line, start_col))
            continue
        if ch == ".":
            advance()
            tokens.append(Token(Tok.DOT, ".", start_line, start_col))
            continue
        if ch == "*":
            advance()
            tokens.append(Token(Tok.STAR, "*", start_line, start_col))
            continue
        if ch == "(":
            advance()
            tokens.append(Token(Tok.LPAREN, "(", start_line, start_col))
            continue
        if ch == ")":
            advance()
            tokens.append(Token(Tok.RPAREN, ")", start_line, start_col))
            continue
        if ch == ";":
            advance()
            tokens.append(Token(Tok.SEMI, ";", start_line, start_col))
            continue

        # operators — maximal munch
        if ch in "=<>":
            op = advance()
            if op == "<" and peek() in "=>":
                op += advance()
            elif op == ">" and peek() == "=":
                op += advance()
            tokens.append(Token(Tok.OP, op, start_line, start_col))
            continue

        # string
        if ch == "'":
            advance()
            chars = []
            while True:
                if i >= n:
                    raise SqlError("lexical", "unterminated string", start_line, start_col)
                if peek() == "'":
                    advance()
                    if peek() == "'":
                        advance()
                        chars.append("'")
                        continue
                    break
                chars.append(advance())
            tokens.append(Token(Tok.STRING, "".join(chars), start_line, start_col))
            continue

        # number (optional leading sign only if preceded appropriately — accept +-)
        if ch.isdigit() or (ch in "+-" and peek(1).isdigit()):
            num = advance()
            while peek().isdigit():
                num += advance()
            tokens.append(Token(Tok.NUMBER, num, start_line, start_col))
            continue

        # ident / keyword
        if ch.isalpha() or ch == "_":
            ident = advance()
            while peek().isalnum() or peek() == "_":
                ident += advance()
            upper = ident.upper()
            if upper in KEYWORDS:
                tokens.append(Token(Tok.KEYWORD, upper, start_line, start_col))
            else:
                tokens.append(Token(Tok.IDENT, ident, start_line, start_col))
            continue

        raise SqlError("lexical", f"unexpected character {ch!r}", start_line, start_col)

    tokens.append(Token(Tok.EOF, "", line, col))
    return tokens
