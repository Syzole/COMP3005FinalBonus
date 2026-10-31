import argparse
from pathlib import Path

from engine.tokeniser import tokenise, LexError
from engine.parser import Parser, ParseError
from engine.tree import format_tree
from engine.eval import format_result, evaluate, EvalError
from engine.table import load_relation, Catalog


def main():
    arg_parse = argparse.ArgumentParser(description="Query language for relational databases")
    arg_parse.add_argument("--tree", action="store_true", help="print the AST instead of executing")
    arg_parse.add_argument("query", help="query string")
    arg_parse.add_argument(
        "--data",
        help="path to a data.txt / relation file",
        default="tests/bonus1/data/Employees.txt",
    )
    args = arg_parse.parse_args()

    try:
        tokens = tokenise(args.query)
        ast = Parser(tokens).parse()
    except LexError as e:
        print(f"error: lexical, {e}")
        return
    except ParseError as e:
        print(f"error: syntax, {e}")
        return

    if args.tree:
        print(format_tree(ast))
        return

    data_path = Path(args.data)
    if not data_path.is_file():
        root = Path(__file__).resolve().parent.parent
        data_path = root / "tests" / "bonus1" / "data" / "Employees.txt"

    catalog = Catalog(load_relation(data_path.read_text(encoding="utf-8")))
    try:
        result = evaluate(ast, catalog)
        print(format_result(result))
    except EvalError as e:
        print(f"error: {e}")


if __name__ == "__main__":
    main()
