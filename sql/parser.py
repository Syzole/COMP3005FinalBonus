"""Recursive-descent SQL parser."""

from __future__ import annotations

from sql.lexer import Tok, Token, lex
from sql.errors import SqlError
from sql import ast as A


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0 #index of the token we are looking at

    def peek(self) -> Token:
        return self.tokens[self.i] #look ahead without advancing the index

    def at(self, *kinds) -> bool:
        #true if tyhe curr token is one of kinds
        # keyword is a special case any keyword as a kind
        t = self.peek()
        if Tok.KEYWORD in kinds and t.type is Tok.KEYWORD:
            return True
        return t.type in kinds

    def kw(self, *words) -> bool:
        #true if the current token is a keyword and its value is in words
        t = self.peek()
        return t.type is Tok.KEYWORD and t.value in words

    def eat_kw(self, word: str) -> Token:
        #we need the exact word to be matched then we move
        t = self.peek()
        if t.type is not Tok.KEYWORD or t.value != word:
            raise SqlError("syntax", f"expected {word}", t.line, t.column)
        self.i += 1
        return t

    def eat(self, kind: Tok, msg: str | None = None) -> Token:
        #requires the exact token type to be matched then we move
        t = self.peek()
        if t.type is not kind:
            raise SqlError("syntax", msg or f"expected {kind.name}", t.line, t.column)
        self.i += 1
        return t

    def advance(self) -> Token:
        #moves to the next token without checking anything
        t = self.peek()
        self.i += 1
        return t

    def parse_statements(self) -> list:
        # script: statement * ;
        # Extra semi-colons are skipped
        stmts = []
        while self.peek().type is not Tok.EOF:
            if self.peek().type is Tok.SEMI:
                self.advance()
                continue
            stmts.append(self.parse_statement())
            if self.peek().type is Tok.SEMI:
                self.advance()
        return stmts

    def parse_statement(self):
        # CREATE TABLE | INSERT | query
        if self.kw("CREATE"):
            return self.parse_create()
        if self.kw("INSERT"):
            return self.parse_insert()
        return self.parse_query()

    def parse_create(self) -> A.CreateTable:
        # CREATE TABLE ident '(' coldef (',' coldef)* ')'
        t0 = self.eat_kw("CREATE")
        self.eat_kw("TABLE")
        name = self.eat(Tok.IDENT, "expected table name").value
        self.eat(Tok.LPAREN)
        cols = [self.parse_coldef()]
        while self.peek().type is Tok.COMMA:
            self.advance()
            cols.append(self.parse_coldef())
        self.eat(Tok.RPAREN)
        return A.CreateTable(name, cols, t0.line, t0.column)

    def parse_coldef(self) -> list:
        # ident INT | ident VARCHAR '(' number ')'
        # Returned as a list: [name, "INT"] or [name, "VARCHAR", n]
        name = self.eat(Tok.IDENT, "expected column name").value
        if self.kw("INT"):
            self.advance()
            return [name, "INT"]
        if self.kw("VARCHAR"):
            self.advance()
            self.eat(Tok.LPAREN)
            n = int(self.eat(Tok.NUMBER).value)
            self.eat(Tok.RPAREN)
            return [name, "VARCHAR", n]
        t = self.peek()
        raise SqlError("syntax", "expected INT or VARCHAR", t.line, t.column)

    def parse_insert(self) -> A.Insert:
        # INSERT INTO ident VALUES '(' literal (',' literal)* ')'
        t0 = self.eat_kw("INSERT")
        self.eat_kw("INTO")
        table = self.eat(Tok.IDENT).value
        self.eat_kw("VALUES")
        self.eat(Tok.LPAREN)
        vals = [self.parse_literal()]
        while self.peek().type is Tok.COMMA:
            self.advance()
            vals.append(self.parse_literal())
        self.eat(Tok.RPAREN)
        return A.Insert(table, vals, t0.line, t0.column)

    def parse_literal(self):   #literal: number | string
        t = self.peek()
        if t.type is Tok.NUMBER:
            self.advance()
            return A.LitInt(int(t.value), t.line, t.column)
        if t.type is Tok.STRING:
            self.advance()
            return A.LitStr(t.value, t.line, t.column)
        raise SqlError("syntax", "expected literal", t.line, t.column)

    def parse_query(self):
        # select_block ( (UNION [ALL] | INTERSECT | EXCEPT) select_block )*
        # Left-associative: each new operator wraps the query built so far.
        left = self.parse_select_block()
        while self.kw("UNION", "INTERSECT", "EXCEPT"):
            op_tok = self.advance()
            if op_tok.value == "UNION" and self.kw("ALL"):
                self.advance()
                op = "union_all"
            else:
                op = op_tok.value.lower()
            right = self.parse_select_block()
            left = A.SetOp(op, left, right)
        return left

    def parse_select_block(self):
        # '(' query ')' | SELECT [DISTINCT] select_list FROM from
        #   [WHERE cond] [GROUP BY cols [HAVING cond]] [ORDER BY sorts] [LIMIT n]
        # A parenthesized query can itself contain UNION/INTERSECT/EXCEPT.
        if self.peek().type is Tok.LPAREN:
            self.advance()
            q = self.parse_query()
            self.eat(Tok.RPAREN)
            return q
        t0 = self.eat_kw("SELECT")
        distinct = False
        if self.kw("DISTINCT"):
            self.advance()
            distinct = True
        items = self.parse_select_list()
        self.eat_kw("FROM")
        from_clause = self.parse_from()
        where = None
        if self.kw("WHERE"):
            self.advance()
            where = self.parse_condition()
        group_by = []
        having = None
        if self.kw("GROUP"):
            self.advance()
            self.eat_kw("BY")
            group_by = self.parse_col_list()
            if self.kw("HAVING"):
                self.advance()
                having = self.parse_condition()
        order_by = []
        if self.kw("ORDER"):
            self.advance()
            self.eat_kw("BY")
            order_by = self.parse_sort_list()
        limit = None
        if self.kw("LIMIT"):
            self.advance()
            limit = int(self.eat(Tok.NUMBER).value)
        return A.SelectBlock(
            distinct, items, from_clause, where, group_by, having, order_by, limit,
            t0.line, t0.column,
        )

    def parse_select_list(self) -> list:
        # '*' | select_item (',' select_item)*
        if self.peek().type is Tok.STAR:
            self.advance()
            return [A.SelectItem("*", None)]
        items = [self.parse_select_item()]
        while self.peek().type is Tok.COMMA:
            self.advance()
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> A.SelectItem:
        # aggregate [AS ident] | column [AS ident]
        # A bare alias (no AS) is not accepted here; AS is required.
        if self.kw("COUNT", "SUM", "AVG", "MIN", "MAX"):
            agg = self.parse_aggregate()
            alias = None
            if self.kw("AS"):
                self.advance()
                alias = self.eat(Tok.IDENT).value
                agg.alias = alias
            elif self.peek().type is Tok.IDENT and not self._looks_like_clause():
                # bare alias without AS — not in grammar; require AS
                pass
            return A.SelectItem(agg, alias)
        col = self.parse_column()
        alias = None
        if self.kw("AS"):
            self.advance()
            alias = self.eat(Tok.IDENT).value
        return A.SelectItem(col, alias)

    def _looks_like_clause(self) -> bool:
        # keywords that star tthe next c lause, so a following identifier is not an alias
        return self.kw(
            "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT",
            "UNION", "INTERSECT", "EXCEPT", "JOIN", "ON",
        )

    def parse_aggregate(self) -> A.Aggregate:
        # fn '(' '*' ')' | fn '(' column ')'
        t = self.advance()
        fn = t.value # COUNT, SUM, AVG, MIN, MAX
        self.eat(Tok.LPAREN)
        if self.peek().type is Tok.STAR:
            self.advance()
            arg = "*"
        else:
            arg = self.parse_column()
        self.eat(Tok.RPAREN)
        return A.Aggregate(fn, arg, line=t.line, column=t.column)

    def parse_from(self):
        # table_ref (JOIN ...)* | table_ref (',' table_ref)*
        # A JOIN chain and a comma list are alternatives; they are not mixed.
        first = self.parse_table_ref()
        if self.kw("JOIN"):
            return self.parse_join_chain(first)
        tables = [first]
        while self.peek().type is Tok.COMMA:
            self.advance()
            tables.append(self.parse_table_ref())
        return A.FromComma(tables)

    def parse_join_chain(self, left: A.TableRef) -> A.JoinOn:
        # JOIN table ON cond (JOIN table ON cond)*
        # First join is stored as left/right/cond; further joins go in more.
        self.eat_kw("JOIN")
        right = self.parse_table_ref()
        self.eat_kw("ON")
        cond = self.parse_condition()
        more = []
        while self.kw("JOIN"):
            self.advance()
            r2 = self.parse_table_ref()
            self.eat_kw("ON")
            c2 = self.parse_condition()
            more.append((r2, c2))
        return A.JoinOn(left, right, cond, more)

    def parse_table_ref(self) -> A.TableRef:
        # '(' query ')' AS ident | ident [AS ident | ident]
        # Subqueries require AS. Base tables may omit AS (bare alias).
        t = self.peek()
        if t.type is Tok.LPAREN:
            self.advance()
            sub = self.parse_query()
            self.eat(Tok.RPAREN)
            self.eat_kw("AS")
            alias = self.eat(Tok.IDENT).value
            return A.TableRef("subquery", None, alias, sub, t.line, t.column)
        name = self.eat(Tok.IDENT, "expected table name").value
        alias = None
        if self.kw("AS"):
            self.advance()
            alias = self.eat(Tok.IDENT).value
        elif self.peek().type is Tok.IDENT and not self._looks_like_clause() and not self.kw("JOIN"):
            # optional AS omitted — grammar says [ AS ident ]; allow bare alias
            alias = self.advance().value
        return A.TableRef("base", name, alias, None, t.line, t.column)

    def parse_col_list(self) -> list:
        # column (',' column)*   used by GROUP BY
        cols = [self.parse_column()]
        while self.peek().type is Tok.COMMA:
            self.advance()
            cols.append(self.parse_column())
        return cols

    def parse_sort_list(self) -> list:
        # sort_key (',' sort_key)*   used by ORDER BY
        keys = [self.parse_sort_key()]
        while self.peek().type is Tok.COMMA:
            self.advance()
            keys.append(self.parse_sort_key())
        return keys

    def parse_sort_key(self) -> A.SortKey:
        # column [ASC | DESC]   default direction is ASC
        col = self.parse_column()
        direction = "asc"
        if self.kw("ASC"):
            self.advance()
            direction = "asc"
        elif self.kw("DESC"):
            self.advance()
            direction = "desc"
        return A.SortKey(col, direction)

    def parse_column(self) -> A.ColRef:
        # ident | ident '.' ident
        # qualifier.column when a dot follows; otherwise a bare column name.
        t = self.eat(Tok.IDENT, "expected column")
        if self.peek().type is Tok.DOT:
            self.advance()
            c = self.eat(Tok.IDENT).value
            return A.ColRef(c, t.value, t.line, t.column)
        return A.ColRef(t.value, None, t.line, t.column)

    # conditions: OR of AND of NOT of comparison
    def parse_condition(self):
        return self.parse_or()

    def parse_or(self):
        # and (OR and)* left-associative
        left = self.parse_and()
        while self.kw("OR"):
            self.advance()
            right = self.parse_and()
            left = A.Or(left, right)
        return left

    def parse_and(self):
        # not (AND not)* left-associative
        left = self.parse_not()
        while self.kw("AND"):
            self.advance()
            right = self.parse_not()
            left = A.And(left, right)
        return left

    def parse_not(self):
        # NOT not | comparison, right-recursive so NOT NOT x works
        if self.kw("NOT"):
            self.advance()
            return A.Not(self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        #'(' condition ')' | operand op operand
        if self.peek().type is Tok.LPAREN:
            self.advance()
            c = self.parse_condition()
            self.eat(Tok.RPAREN)
            return c
        left = self.parse_operand()
        t = self.peek()
        if t.type is not Tok.OP:
            raise SqlError("syntax", "expected comparison operator", t.line, t.column)
        op = self.advance().value
        if op == "<>":
            op = "!="  # one spelling for the rest of the engine
        right = self.parse_operand()
        return A.Compare(op, left, right, t.line, t.column)

    def parse_operand(self):
        # number | string | aggregate | column
        # Aggregates are parsed here so HAVING can use them; WHERE rejects them later.
        t = self.peek()
        if t.type is Tok.NUMBER:
            self.advance()
            return A.LitInt(int(t.value), t.line, t.column)
        if t.type is Tok.STRING:
            self.advance()
            return A.LitStr(t.value, t.line, t.column)
        # Aggregates allowed in HAVING (and rejected later in WHERE by resolve).
        if self.kw("COUNT", "SUM", "AVG", "MIN", "MAX"):
            return self.parse_aggregate()
        if t.type is Tok.IDENT:
            return self.parse_column()
        raise SqlError("syntax", "expected operand", t.line, t.column)


def parse(source: str):
    #Lex the whole script and return a list of statements
    tokens = lex(source)
    return Parser(tokens).parse_statements()


def parse_query(source: str):
    # Parse one query, a trailing semi-colon is optional; anything else is an error 
    tokens = lex(source)
    p = Parser(tokens)
    q = p.parse_query()
    if p.peek().type not in (Tok.EOF, Tok.SEMI):
        t = p.peek()
        raise SqlError("syntax", f"unexpected token {t.value!r}", t.line, t.column)
    return q
