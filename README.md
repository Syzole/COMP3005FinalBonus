# COMP3005 Final Bonus

A small database system written for COMP3005. SQL goes in at the top; 4 KB pages sit on disk at the bottom. and alot of pain in the middle.

This was a great learning experience and I'm proud of the work I did and what I learned.

If you have any questions, feel free to contact me at arjunbedi3@cmail.carleton.ca

**Demo:** https://youtu.be/l1SVC2oZDsw

## What it is

Four modules, each with its own CLI, wired together by a shell:

| Module | Package | Role |
|--------|---------|------|
| M1 | `storage/` | Buffer pool, heap files, slotted 4 KB pages |
| M2 | `exec/` | Iterator execution under a frame budget |
| M3 | `sql/` | Lexer, parser, name resolution, logical plans |
| M4 | `opt/` | Statistics, rewrites, and join-order search |

`engine/` is the shared library (expressions, relational algebra, plan trees). `python -m shell` runs the full path: SQL, then optimization, then execution, then storage.

Design notes and measurements are in `reports/`.

## Try it

Start the shell. The database directory is created if it does not exist. Statements end with `;`.

```
python -m shell --db data/demo
```

```
sql> CREATE TABLE Emp (name VARCHAR(20), age INT);
created table Emp
sql> INSERT INTO Emp VALUES ('alice', 35);
inserted 1 row into Emp
sql> INSERT INTO Emp VALUES ('bob', 25);
inserted 1 row into Emp
sql> SELECT name, age FROM Emp WHERE age > 30;
Emp.name | Emp.age
----------------------------------------
alice | 35
(1 rows, reads=1, writes=0)
```

`.quit` leaves the shell. The same statements can be passed one at a time with `-c`.
