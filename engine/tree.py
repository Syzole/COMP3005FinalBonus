from dataclasses import dataclass

"""
AST nodes for the query tree. The parser builds these; eval and plan_json consume them.
"""


@dataclass
class Relation:
    """Base relation / scan."""
    name: str


@dataclass
class Project:
    """project[attrs](input). attrs are strings, optionally 'table.column'."""
    attrs: list[str]
    input: object


@dataclass
class Select:
    """select[condition](input)."""
    cond: object
    input: object


@dataclass
class Rename:
    """rename[name](input)."""
    name: str
    input: object


@dataclass
class Distinct:
    """distinct(input) — bag duplicate removal."""
    input: object


@dataclass
class Binary:
    """union | intersect | minus | times | join."""
    op: str
    left: object
    right: object
    cond: object | None = None


@dataclass
class Compare:
    op: str
    left: object
    right: object


@dataclass
class Attr:
    """Attribute: Name, or Emp.DID when relation is set."""
    name: str
    relation: str | None = None


@dataclass
class Num:
    value: int


@dataclass
class Str:
    value: str


@dataclass
class Not:
    cond: object


@dataclass
class And:
    left: object
    right: object


@dataclass
class Or:
    left: object
    right: object


def format_operand(node) -> str:
    if isinstance(node, Attr):
        return f"{node.relation}.{node.name}" if node.relation else node.name
    elif isinstance(node, Num):
        return str(node.value)
    elif isinstance(node, Str):
        return node.value
    return str(node)


def format_condition(node) -> str:
    if isinstance(node, Compare):
        return f"{format_operand(node.left)} {node.op} {format_operand(node.right)}"
    elif isinstance(node, And):
        return f"{format_condition(node.left)} and {format_condition(node.right)}"
    elif isinstance(node, Or):
        return f"{format_condition(node.left)} or {format_condition(node.right)}"
    elif isinstance(node, Not):
        return f"not {format_condition(node.cond)}"
    return str(node)


def format_tree(node) -> str:
    return "\n".join(walk_tree(node, prefix="", is_last=True, is_root=True))


def label_and_children(node) -> tuple[str, list]:
    if isinstance(node, Relation):
        return node.name, []

    elif isinstance(node, Project):
        return "Project", [
            (f"attrs: {', '.join(node.attrs)}", None),
            ("input", node.input),
        ]

    elif isinstance(node, Select):
        return "Select", [
            ("cond", node.cond),
            ("input", node.input),
        ]

    elif isinstance(node, Distinct):
        return "Distinct", [("input", node.input)]

    elif isinstance(node, Compare):
        return f"{format_operand(node.left)} {node.op} {format_operand(node.right)}", []
    elif isinstance(node, And):
        return "And", [("left", node.left), ("right", node.right)]
    elif isinstance(node, Or):
        return "Or", [("left", node.left), ("right", node.right)]
    elif isinstance(node, Not):
        return "Not", [("cond", node.cond)]

    elif isinstance(node, Rename):
        return "Rename", [
            (f"name: {node.name}", None),
            ("input", node.input),
        ]

    elif isinstance(node, Binary):
        children = []
        if node.cond is not None:
            children.append((f"cond: {format_condition(node.cond)}", None))
        children.append(("left", node.left))
        children.append(("right", node.right))
        return node.op.capitalize(), children

    return repr(node), []


def walk_tree(node, prefix: str, is_last: bool, is_root: bool) -> list[str]:
    label, children = label_and_children(node)
    if is_root:
        lines = [label]
    else:
        branch = "└── " if is_last else "├── "
        lines = [prefix + branch + label]
        prefix = prefix + ("    " if is_last else "│   ")
    for i, (child_label, child_node) in enumerate(children):
        last = i == len(children) - 1
        branch = "└── " if last else "├── "
        lines.append(prefix + branch + child_label)
        if child_node is None:
            continue
        next_prefix = prefix + ("    " if last else "│   ")
        sub = walk_tree(child_node, next_prefix, True, False)
        lines.extend(sub)
    return lines
