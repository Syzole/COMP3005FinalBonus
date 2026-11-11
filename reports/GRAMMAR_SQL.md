# GRAMMAR for M3

recursive-descent grammar

Keywords are case-insensitive 

identifiers are case-sensitive.

## EBNF

```
statement     ::= create | insert | query

create        ::= CREATE TABLE ident "(" coldef { "," coldef } ")"
coldef        ::= ident ( INT | VARCHAR "(" int ")" )

insert        ::= INSERT INTO ident VALUES "(" literal { "," literal } ")"

query         ::= select_block { setop select_block }
setop         ::= UNION [ ALL ] | INTERSECT | EXCEPT
                  (* left-associative, same precedence *)

select_block  ::= SELECT [ DISTINCT ] select_list
                  FROM from_clause
                  [ WHERE condition ]
                  [ GROUP BY col_list [ HAVING condition ] ]
                  [ ORDER BY sort_list ]
                  [ LIMIT int ]
                | "(" query ")"

select_list   ::= "*" | select_item { "," select_item }
select_item   ::= ( column | aggregate ) [ AS ident ]
aggregate     ::= ( COUNT | SUM | AVG | MIN | MAX ) "(" ( column | "*" ) ")"

from_clause   ::= table_ref { "," table_ref }
                | table_ref JOIN table_ref ON condition
                  { JOIN table_ref ON condition }

table_ref     ::= ident [ AS ident | ident ]
                | "(" query ")" AS ident

col_list      ::= column { "," column }
sort_list     ::= sort_key { "," sort_key }
sort_key      ::= column [ ASC | DESC ]

condition     ::= or_cond
or_cond       ::= and_cond { OR and_cond }
and_cond      ::= not_cond { AND not_cond }
not_cond      ::= NOT not_cond | comparison
comparison    ::= "(" condition ")" | operand comp_op operand
comp_op       ::= "=" | "<>" | "<" | "<=" | ">" | ">="
operand       ::= column | int | string | aggregate
                  (* aggregate operands are for HAVING; WHERE rejects them in resolve *)

column        ::= ident | ident "." ident
literal       ::= int | string
```

## Precedence and associativity

| Construct | Precedence (tightest first) | Associativity |
|-----------|----------------------------|---------------|
| `NOT` | 1 | unary |
| `AND` | 2 | left |
| `OR` | 3 | left |
| `UNION` / `UNION ALL` / `INTERSECT` / `EXCEPT` | same level | left |

So `A UNION B EXCEPT C` parses as `(A UNION B) EXCEPT C`.

## Differences from the M3 spec 2 sketch

1. **Bare table aliases** (`FROM Emp e`) are accepted in addition to `AS` — common SQL and needed by the §5 examples.
2. **Aggregates as condition operands** — required for `HAVING COUNT(*) > 5`; rejected in WHERE by the semantic pass (`aggregate` category).
3. Plan condition strings use `!=` (engine lexer) where SQL wrote `<>`.

## Lexical notes

- `--` line comments
- Maximal munch for `<=`, `>=`, `<>`
- Strings: `''` → one quote
- Integers only (optional leading `+`/`-`)
