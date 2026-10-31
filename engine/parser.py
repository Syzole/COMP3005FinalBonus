from engine.tokeniser import Token, TokenType, tokenise
from engine.tree import *

"""
Recursive-descent parser: tokens -> AST.
Precedence (conditions): not > and > or.
Binary RA ops are left-associative via expression().
"""

OPS = {
    TokenType.EQUALS: "=",
    TokenType.NOT_EQUALS: "!=",
    TokenType.LESS_THAN: "<",
    TokenType.LESS_THAN_OR_EQUALS: "<=",
    TokenType.GREATER_THAN: ">",
    TokenType.GREATER_THAN_OR_EQUALS: ">=",
}

BINARY_OPS = {"union", "intersect", "minus", "times", "join"}

class ParseError(Exception):
    pass

class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    def expression(self):
        left = self.term()
        while self.check(TokenType.IDENTIFIER) and self.peek().value in BINARY_OPS:
            op = self.next().value
            cond = None
            if op == "join": #the reason we have a condition for join is because it is a binary operation and needs a condition unlike the other binary operations
                self.expect(TokenType.LBRACKET)
                cond = self.condition()
                self.expect(TokenType.RBRACKET)
            right = self.term()
            left = Binary(op=op, left=left, right=right, cond=cond)
        return left

    def peek(self) -> Token:
        return self.tokens[self.i]

    def next(self) -> Token:
        token = self.peek()
        self.i += 1
        return token

    def check(self, token_type: TokenType, value: str | None = None) -> bool:
        token = self.peek()
        if token.type != token_type:
            return False
        if value is not None and token.value != value:
            return False
        return True

    def expect(self, token_type: TokenType, value: str | None = None) -> Token:
        if not self.check(token_type, value):
            if token_type is TokenType.RPARENTHESIS:
                raise ParseError("missing )")
            raise ParseError(f"Expected {token_type} but got {self.peek().type}")
        return self.next()

    def parse(self):
        node = self.expression()
        self.expect(TokenType.EOF)
        return node

    def _attr_name(self) -> str:
        """Parse Name or Relation.Name into a single attribute string."""
        name = self.expect(TokenType.IDENTIFIER).value
        if self.check(TokenType.DOT):
            self.next()
            attr = self.expect(TokenType.IDENTIFIER).value
            return f"{name}.{attr}"
        return name

    def project_expr(self):
        self.next()
        self.expect(TokenType.LBRACKET)
        if self.check(TokenType.RBRACKET):
            raise ParseError("empty attribute list")
        attributes = [self._attr_name()]
        while self.check(TokenType.COMMA):
            self.next()
            attributes.append(self._attr_name())
        self.expect(TokenType.RBRACKET)
        self.expect(TokenType.LPARENTHESIS)
        inside = self.expression()
        self.expect(TokenType.RPARENTHESIS)
        return Project(attrs=attributes, input=inside)

    def select_expr(self):
        self.next()
        self.expect(TokenType.LBRACKET)
        condition = self.condition()
        self.expect(TokenType.RBRACKET)
        self.expect(TokenType.LPARENTHESIS)
        inner = self.expression()
        self.expect(TokenType.RPARENTHESIS)
        return Select(cond=condition, input=inner)

    def rename_expr(self):
        self.next()
        self.expect(TokenType.LBRACKET)
        name = self.expect(TokenType.IDENTIFIER).value
        self.expect(TokenType.RBRACKET)
        self.expect(TokenType.LPARENTHESIS)
        inner = self.expression()
        self.expect(TokenType.RPARENTHESIS)
        return Rename(name=name, input=inner)

    def distinct_expr(self):
        self.next()
        self.expect(TokenType.LPARENTHESIS)
        inner = self.expression()
        self.expect(TokenType.RPARENTHESIS)
        return Distinct(input=inner)

    def term(self):
        if self.check(TokenType.IDENTIFIER, "select"):
            return self.select_expr()
        if self.check(TokenType.IDENTIFIER, "project"):
            return self.project_expr()
        if self.check(TokenType.IDENTIFIER, "rename"):
            return self.rename_expr()
        if self.check(TokenType.IDENTIFIER, "distinct"):
            return self.distinct_expr()
        if self.check(TokenType.LPARENTHESIS):
            self.next()
            node = self.expression()
            self.expect(TokenType.RPARENTHESIS)
            return node
        if self.check(TokenType.IDENTIFIER):
            return Relation(name=self.next().value)
        raise ParseError(f"Unexpected token: {self.peek().type}")

    def comparison(self):
        left = self.operand()
        op_tok = self.next()
        op = OPS.get(op_tok.type)
        if op is None:
            raise ParseError(f"expected comparison op, got {op_tok.type}")
        right = self.operand()
        return Compare(op=op, left=left, right=right)

    def operand(self):
        if self.check(TokenType.IDENTIFIER):
            name = self.next().value
            if self.check(TokenType.DOT):
                self.next()
                attr = self.expect(TokenType.IDENTIFIER).value
                return Attr(name=attr, relation=name)
            return Attr(name=name)
        if self.check(TokenType.NUMBER):
            return Num(value=int(self.next().value))
        if self.check(TokenType.STRING):
            return Str(value=self.next().value)
        raise ParseError(f"expected operand, got {self.peek().type}")

    def condition(self):
        return self.or_condition()

    def or_condition(self):
        left = self.and_condition()
        while self.check(TokenType.IDENTIFIER, "or"):
            self.next()
            right = self.and_condition()
            left = Or(left=left, right=right)
        return left

    def and_condition(self):
        left = self.not_condition()
        while self.check(TokenType.IDENTIFIER, "and"):
            self.next()
            right = self.not_condition()
            left = And(left=left, right=right)
        return left

    def not_condition(self):
        if self.check(TokenType.IDENTIFIER, "not"):
            self.next()
            return Not(cond=self.not_condition())
        if self.check(TokenType.LPARENTHESIS):
            self.next()
            node = self.condition()
            self.expect(TokenType.RPARENTHESIS)
            return node
        return self.comparison()
