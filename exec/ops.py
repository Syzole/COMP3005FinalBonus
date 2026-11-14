"""Iterator operators. Every live page is checked out of the plan budget."""

from __future__ import annotations

from collections import defaultdict

from storage.disk import DiskManager
from storage.page import (
    NO_NEXT, NO_SPACE, SlottedPage, decode_tuple, encode_tuple,
    dir_entry_count, dir_get_entry, dir_get_next,
)
from exec.budget import ExecError, PageSlot
from exec.compare import (
    check_condition_types, cmp_values, equality_keys, fnv, make_key_cmp,
    matches, parse_cond, project_key, resolve,
)
from exec.runs import pack_tuples

END = object()


def qualify(table: str, schema: list) -> list:
    out = []
    for col in schema:
        name = f"{table}.{col[0]}"
        if col[1] == "INT":
            out.append([name, "INT"])
        else:
            out.append([name, "VARCHAR", col[2]])
    return out


def same_types(left: list, right: list) -> bool:
    if len(left) != len(right):
        return False
    for a, b in zip(left, right):
        if a[1] != b[1]:
            return False
        if a[1] == "VARCHAR" and a[2] != b[2]:
            return False
    return True


def collect_data_pages(disk: DiskManager, buf: bytearray) -> list[int]:
    """Walk the heap directory using one buffer. Directory reads are not charged."""
    ids = []
    dir_id = 0
    seen = set()
    while dir_id not in seen:
        seen.add(dir_id)
        buf[:] = disk.read_page(dir_id)
        count = dir_entry_count(buf)
        for i in range(count):
            pid, _free = dir_get_entry(buf, i)
            ids.append(pid)
        nxt = dir_get_next(buf)
        if nxt == NO_NEXT:
            break
        dir_id = nxt
    return ids


def _fresh(buf) -> SlottedPage:
    return SlottedPage.fresh(buf, 0)


class Operator:
    def __init__(self, ctx, name: str):
        self.ctx = ctx
        self.budget = ctx.budget
        self.op_name = name
        self.schema: list = []
        self.children: list[Operator] = []
        self.tuples_out = 0
        self.comparisons = 0
        self.page_reads = 0
        self.page_writes = 0
        self._extras: list[Operator] = []
        self._prepared = False

    def prepare(self) -> None:
        raise NotImplementedError

    def open(self) -> None:
        pass

    def next(self):
        return END

    def close(self) -> None:
        pass

    def counters(self) -> dict:
        c = {
            "tuples_out": self.tuples_out,
            "comparisons": self.comparisons,
            "page_reads": self.page_reads,
            "page_writes": self.page_writes,
        }
        for extra in self._extras:
            e = extra.counters()
            c["comparisons"] += e["comparisons"]
            c["page_reads"] += e["page_reads"]
            c["page_writes"] += e["page_writes"]
        return c

    def trace_label(self) -> str:
        return self.op_name


class SeqScan(Operator):
    raw_ok = True

    def __init__(self, ctx, table: str, alias: str | None = None):
        super().__init__(ctx, "seqscan")
        self.table = table
        self.alias = alias  # optional; qualifies columns as alias.col
        self.disk = None
        self.ids: list[int] = []
        self.page_i = 0
        self.slot: PageSlot | None = None
        self.slots: list[int] = []
        self.slot_i = 0
        self._raw = False

    def prepare(self) -> None:
        info = self.ctx.meta.get(self.table)
        if info is None:
            raise ExecError(f"Node 'seqscan': unknown table '{self.table}'.")
        # Prefer plan alias (e) over physical table name (Emp) for column quals.
        qual = self.alias if self.alias else self.table
        self.schema = qualify(qual, info["schema"])
        self._path = info["path"]
        self._prepared = True

    def open(self) -> None:
        self.disk = DiskManager(self._path)
        self.slot = PageSlot(self.budget)
        self.ids = collect_data_pages(self.disk, self.slot.buf)
        self.page_i = 0
        self.slots = []
        self.slot_i = 0
        self._raw = False

    def open_raw(self) -> None:
        self.disk = DiskManager(self._path)
        slot = PageSlot(self.budget)
        try:
            self.ids = collect_data_pages(self.disk, slot.buf)
        finally:
            slot.release()
        self.page_i = 0
        self._raw = True

    def has_more(self) -> bool:
        return self.page_i < len(self.ids)

    def read_data_page(self, buf: bytearray) -> bool:
        if self.page_i >= len(self.ids):
            return False
        buf[:] = self.disk.read_page(self.ids[self.page_i])
        self.page_i += 1
        self.page_reads += 1
        sp = SlottedPage(buf)
        self.tuples_out += len(sp.live_slots())
        return True

    def _load(self) -> bool:
        if self.page_i >= len(self.ids):
            self.slots = []
            return False
        self.slot.buf[:] = self.disk.read_page(self.ids[self.page_i])
        self.page_i += 1
        self.page_reads += 1
        sp = SlottedPage(self.slot.buf)
        self.slots = list(sp.live_slots())
        self.slot_i = 0
        if not self.slots:
            return self._load()
        return True

    def next(self):
        if self.slot is None:
            return END
        if self.slot_i >= len(self.slots):
            if not self._load():
                return END
        rec = SlottedPage(self.slot.buf).get(self.slots[self.slot_i])
        self.slot_i += 1
        self.tuples_out += 1
        return decode_tuple(self.schema, rec)

    def close(self) -> None:
        if self.slot is not None:
            self.slot.release()
            self.slot = None

    def trace_label(self) -> str:
        return f"seqscan {self.table}"


class RunSource(Operator):
    """One-frame-or-raw scan of a run file. Not a plan node."""

    raw_ok = True

    def __init__(self, ctx, run, schema, counter: Operator):
        super().__init__(ctx, "run")
        self.run = run
        self.schema = schema
        self.io = counter
        self.page_i = 0
        self.slot = None
        self.slots: list[int] = []
        self.slot_i = 0

    def prepare(self) -> None:
        self._prepared = True

    def open_raw(self) -> None:
        self.page_i = 0

    def has_more(self) -> bool:
        return self.page_i < self.run.num_pages()

    def read_data_page(self, buf: bytearray) -> bool:
        if self.page_i >= self.run.num_pages():
            return False
        self.run.read_page(self.page_i, buf, self.io)
        self.page_i += 1
        return True

    def open(self) -> None:
        self.slot = PageSlot(self.budget)
        self.page_i = 0
        self.slots = []
        self.slot_i = 0

    def next(self):
        if self.slot is None:
            return END
        if self.slot_i >= len(self.slots):
            if self.page_i >= self.run.num_pages():
                return END
            self.run.read_page(self.page_i, self.slot.buf, self.io)
            self.page_i += 1
            sp = SlottedPage(self.slot.buf)
            self.slots = list(sp.live_slots())
            self.slot_i = 0
            if not self.slots:
                return self.next()
        rec = SlottedPage(self.slot.buf).get(self.slots[self.slot_i])
        self.slot_i += 1
        return decode_tuple(self.schema, rec)

    def close(self) -> None:
        if self.slot is not None:
            self.slot.release()
            self.slot = None


class Filter(Operator):
    def __init__(self, ctx, condition: str, child: Operator):
        super().__init__(ctx, "filter")
        self.condition = condition
        self.child = child
        self.children = [child]
        self.cond = None

    def prepare(self) -> None:
        self.child.prepare()
        self.schema = self.child.schema
        self.cond = parse_cond(self.condition)
        try:
            check_condition_types(self.cond, self.schema, "filter")
        except ExecError as e:
            _raise_column(e, "filter")
        self._prepared = True

    def open(self) -> None:
        self.child.open()

    def next(self):
        while True:
            tup = self.child.next()
            if tup is END:
                return END
            self.comparisons += 1
            if matches(self.cond, self.schema, tup):
                self.tuples_out += 1
                return tup

    def close(self) -> None:
        self.child.close()


class Project(Operator):
    def __init__(self, ctx, columns: list, child: Operator):
        super().__init__(ctx, "project")
        self.columns = columns
        self.child = child
        self.children = [child]
        self.indexes: list[int] = []

    def prepare(self) -> None:
        self.child.prepare()
        seen = set()
        indexes = []
        schema = []
        for name in self.columns:
            if name in seen:
                raise ExecError(f"Node 'project': duplicate column '{name}'.")
            seen.add(name)
            try:
                idx = resolve(name, self.child.schema)
            except ExecError:
                raise ExecError(f"Node 'project': unknown column '{name}'.")
            indexes.append(idx)
            schema.append(list(self.child.schema[idx]))
        self.indexes = indexes
        self.schema = schema
        self._prepared = True

    def open(self) -> None:
        self.child.open()

    def next(self):
        tup = self.child.next()
        if tup is END:
            return END
        self.tuples_out += 1
        return [tup[i] for i in self.indexes]

    def close(self) -> None:
        self.child.close()


class Limit(Operator):
    def __init__(self, ctx, n: int, child: Operator):
        super().__init__(ctx, "limit")
        self.n = n
        self.child = child
        self.children = [child]
        self.seen = 0

    def prepare(self) -> None:
        self.child.prepare()
        self.schema = self.child.schema
        self._prepared = True

    def open(self) -> None:
        self.seen = 0
        if self.n > 0:
            self.child.open()
            self._opened = True
        else:
            self._opened = False

    def next(self):
        if self.seen >= self.n:
            return END
        tup = self.child.next()
        if tup is END:
            return END
        self.seen += 1
        self.tuples_out += 1
        return tup

    def close(self) -> None:
        if getattr(self, "_opened", False):
            self.child.close()
            self._opened = False


class UnionAll(Operator):
    def __init__(self, ctx, left: Operator, right: Operator):
        super().__init__(ctx, "union_all")
        self.left = left
        self.right = right
        self.children = [left, right]
        self.phase = "L"
        self._right_open = False
        self._left_open = False

    def prepare(self) -> None:
        self.left.prepare()
        self.right.prepare()
        if not same_types(self.left.schema, self.right.schema):
            raise ExecError("Node 'union_all': inputs are not union-compatible.")
        self.schema = [list(c) for c in self.left.schema]
        self._prepared = True

    def open(self) -> None:
        self.left.open()
        self._left_open = True
        self.phase = "L"
        self._right_open = False

    def next(self):
        if self.phase == "L":
            tup = self.left.next()
            if tup is not END:
                self.tuples_out += 1
                return tup
            self.left.close()
            self._left_open = False
            self.right.open()
            self._right_open = True
            self.phase = "R"
        tup = self.right.next()
        if tup is END:
            return END
        self.tuples_out += 1
        return tup

    def close(self) -> None:
        if self._left_open:
            self.left.close()
            self._left_open = False
        if self._right_open:
            self.right.close()
            self._right_open = False


def _raise_column(err: ExecError, node: str) -> None:
    msg = str(err)
    if msg.startswith("unknown column "):
        col = msg[len("unknown column "):]
        raise ExecError(f"Node '{node}': unknown column '{col}'.")
    if "type error" in msg or msg.startswith("Node "):
        if msg.startswith("Node "):
            raise err
        raise ExecError(f"Node '{node}': {msg}.")
    raise err


def _check_names(schema: list, node: str) -> None:
    names = [c[0] for c in schema]
    if len(names) != len(set(names)):
        raise ExecError(f"Node '{node}': duplicate column name in output schema.")


class RunCursor:
    def __init__(self, run, schema, budget, counter: Operator):
        self.run = run
        self.schema = schema
        self.counter = counter
        self.slot = PageSlot(budget)
        self.page_i = 0
        self.slots: list[int] = []
        self.slot_i = 0
        self.done = False

    def _fill(self) -> bool:
        if self.page_i >= self.run.num_pages():
            self.done = True
            self.slots = []
            return False
        self.run.read_page(self.page_i, self.slot.buf, self.counter)
        self.page_i += 1
        sp = SlottedPage(self.slot.buf)
        self.slots = list(sp.live_slots())
        self.slot_i = 0
        if not self.slots:
            return self._fill()
        return True

    def peek(self):
        if self.done:
            return None
        if self.slot_i >= len(self.slots):
            if not self._fill():
                return None
        rec = SlottedPage(self.slot.buf).get(self.slots[self.slot_i])
        return decode_tuple(self.schema, rec)

    def pop(self):
        tup = self.peek()
        if tup is None:
            return None
        self.slot_i += 1
        return tup

    def close(self) -> None:
        if self.slot is not None:
            self.slot.release()
            self.slot = None


class Sort(Operator):
    """External merge sort.

    A bare page source (seqscan or run) is consumed B pages at a time, so pass 0
    fills B pages. Any other child keeps its own scan frame, and runs use the
    frames that remain. A run that does not fill a merge group is carried
    forward and not rewritten. The last pass streams and does not write,
    unless spill_final is set (sort-merge and set operators need a file).
    """

    def __init__(self, ctx, child: Operator, keys: list, spill_final: bool = False):
        super().__init__(ctx, "sort")
        self.child = child
        self.children = [child]
        self.keys = keys
        self.spill_final = spill_final
        self.passes = 0
        self.input_pages = 0
        self.cmp = None
        self._mem = None
        self._mem_n = 0
        self._mem_i = 0
        self._cursors: list[RunCursor] = []
        self._reserve: PageSlot | None = None
        self._stream_runs = []
        self._out_run = None
        self._child_open = False

    def prepare(self) -> None:
        self.child.prepare()
        self.schema = [list(c) for c in self.child.schema]
        for name, _direction in self.keys:
            try:
                resolve(name, self.schema)
            except ExecError:
                raise ExecError(f"Node 'sort': unknown column '{name}'.")
        self.cmp = make_key_cmp(self.schema, self.keys, self)
        self._prepared = True

    def open(self) -> None:
        if self.budget.B < 3:
            raise ExecError("sort needs at least 3 frames")
        if getattr(self.child, "raw_ok", False):
            self._consume_raw(self.child)
        else:
            self._consume_tuples()

    def _sort_rows(self, rows: list) -> None:
        from functools import cmp_to_key
        rows.sort(key=cmp_to_key(self.cmp))

    def _read_charged(self, src, k: int):
        rows = []
        n = 0
        try:
            for _ in range(k):
                slot = PageSlot(self.budget)
                try:
                    ok = src.read_data_page(slot.buf)
                    page_rows = []
                    if ok:
                        sp = SlottedPage(slot.buf)
                        page_rows = [
                            decode_tuple(self.schema, sp.get(s)) for s in sp.live_slots()
                        ]
                finally:
                    slot.release()
                if not ok:
                    break
                self.budget.acquire(1)
                n += 1
                rows.extend(page_rows)
        except Exception:
            if n:
                self.budget.release(n)
            raise
        return n, rows

    def _keep_or_spill(self, rows, n, more, runs) -> bool:
        """Return True if the input is finished and this batch was the last."""
        self._sort_rows(rows)
        last = not more
        if not runs and last:
            self.passes = 1
            if self.spill_final:
                self.budget.release(n)
                self._out_run = pack_tuples(self.schema, rows, self.budget, self, self.ctx)
                self._mem_n = 0
            else:
                self._mem = rows
                self._mem_n = n
                self._mem_i = 0
            return True
        self.budget.release(n)
        if rows:
            runs.append(pack_tuples(self.schema, rows, self.budget, self, self.ctx))
        return last

    def _consume_raw(self, src) -> None:
        src.open_raw()
        self._child_open = True
        runs = []
        total = 0
        while True:
            n, rows = self._read_charged(src, self.budget.B)
            if n == 0:
                break
            total += n
            more = src.has_more()
            if self._keep_or_spill(rows, n, more, runs):
                break
        self.input_pages = total
        if self._mem is not None or (self.spill_final and self._out_run is not None and not runs):
            self._close_child()
            if total == 0:
                self.passes = 0
            return
        self._close_child()
        if not runs:
            self.passes = 0
            self._mem = []
            if self.spill_final:
                self._out_run = self.ctx.new_run()
            return
        self.passes = 1
        self._finish_runs(runs)

    def _fill_tuple_run(self, avail: int, pending):
        rows = []
        pages = 0
        ended = False
        slot = None
        sp = None
        try:
            while pages < avail:
                if slot is None:
                    slot = PageSlot(self.budget)
                    sp = _fresh(slot.buf)
                if pending is not None:
                    tup = pending
                    pending = None
                else:
                    tup = self.child.next()
                    if tup is END:
                        ended = True
                        break
                rec = encode_tuple(self.schema, tup)
                if sp.insert(rec) == NO_SPACE:
                    if sp.slot_count == 0:
                        raise ExecError("tuple does not fit in a page")
                    slot.release()
                    slot = None
                    self.budget.acquire(1)
                    pages += 1
                    pending = tup
                    if pages >= avail:
                        break
                    continue
                rows.append(list(tup))
            if slot is not None:
                if sp is not None and sp.slot_count > 0:
                    slot.release()
                    slot = None
                    self.budget.acquire(1)
                    pages += 1
                else:
                    slot.release()
                    slot = None
        finally:
            if slot is not None and slot.live:
                slot.release()
        return pages, rows, pending, ended

    def _consume_tuples(self) -> None:
        self.child.open()
        self._child_open = True
        runs = []
        pending = None
        total = 0
        while True:
            avail = self.budget.B - self.budget.held
            if avail < 1:
                raise ExecError("memory budget exceeded during sort")
            pages, rows, pending, ended = self._fill_tuple_run(avail, pending)
            if pages == 0 and pending is None:
                break
            total += pages
            more = not ended or pending is not None
            if self._keep_or_spill(rows, pages, more, runs):
                break
        self.input_pages = total
        self._close_child()
        if self._mem is not None or (self.spill_final and self._out_run is not None and not runs):
            if total == 0:
                self.passes = 0
            return
        if not runs:
            self.passes = 0
            self._mem = []
            if self.spill_final:
                self._out_run = self.ctx.new_run()
            return
        self.passes = 1
        self._finish_runs(runs)

    def _close_child(self) -> None:
        if self._child_open:
            self.child.close()
            self._child_open = False

    def _finish_runs(self, runs) -> None:
        fanin = self.budget.B - 1
        while len(runs) > 1:
            if len(runs) <= fanin:
                self.passes += 1
                if self.spill_final:
                    self._out_run = self._merge_to_file(runs)
                else:
                    self._open_stream(runs)
                return
            self.passes += 1
            runs = self._merge_pass(runs)
        if len(runs) == 1:
            if self.spill_final:
                self._out_run = runs[0]
            else:
                self.passes += 1
                self._open_stream(runs)

    def _pick(self, cursors: list[RunCursor]):
        best_i = None
        best = None
        for i, cur in enumerate(cursors):
            tup = cur.peek()
            if tup is None:
                continue
            if best is None:
                best_i, best = i, tup
                continue
            c = self.cmp(tup, best)
            if c < 0 or (c == 0 and i < best_i):
                best_i, best = i, tup
        return best_i

    def _merge_to_file(self, group):
        cursors = [RunCursor(run, self.schema, self.budget, self) for run in group]
        out_slot = PageSlot(self.budget)
        out = self.ctx.new_run()
        try:
            sp = _fresh(out_slot.buf)
            while True:
                best_i = self._pick(cursors)
                if best_i is None:
                    break
                tup = cursors[best_i].pop()
                rec = encode_tuple(self.schema, tup)
                if sp.insert(rec) == NO_SPACE:
                    out.write_page(out_slot.buf, self)
                    sp = _fresh(out_slot.buf)
                    if sp.insert(rec) == NO_SPACE:
                        raise ExecError("tuple does not fit in a page")
            if sp.slot_count:
                out.write_page(out_slot.buf, self)
        finally:
            for cur in cursors:
                cur.close()
            out_slot.release()
            for run in group:
                run.delete()
        return out

    def _merge_pass(self, runs):
        fanin = self.budget.B - 1
        out = []
        i = 0
        while i < len(runs):
            group = runs[i:i + fanin]
            i += len(group)
            if len(group) == 1:
                out.append(group[0])
            else:
                out.append(self._merge_to_file(group))
        return out

    def _open_stream(self, runs) -> None:
        self._cursors = [RunCursor(run, self.schema, self.budget, self) for run in runs]
        self._reserve = PageSlot(self.budget)
        self._stream_runs = runs

    def next(self):
        if self._cursors:
            best_i = self._pick(self._cursors)
            if best_i is None:
                return END
            tup = self._cursors[best_i].pop()
            self.tuples_out += 1
            return list(tup)
        if self._mem is None:
            return END
        if self._mem_i >= len(self._mem):
            return END
        tup = self._mem[self._mem_i]
        self._mem_i += 1
        self.tuples_out += 1
        return list(tup)

    def close(self) -> None:
        if self._mem_n:
            self.budget.release(self._mem_n)
            self._mem_n = 0
        self._mem = None
        for cur in self._cursors:
            cur.close()
        self._cursors = []
        if self._reserve is not None:
            self._reserve.release()
            self._reserve = None
        for run in self._stream_runs:
            run.delete()
        self._stream_runs = []
        self._close_child()

    def materialize(self):
        self.spill_final = True
        self.open()
        if self._out_run is None:
            self._out_run = self.ctx.new_run()
        return self._out_run


def _agg_name(fn: str, arg: str) -> str:
    if arg == "*":
        return f"{fn}_star"
    return f"{fn}_{arg.replace('.', '_')}"


class Acc:
    def __init__(self, fn: str, idx: int | None):
        self.fn = fn
        self.idx = idx
        self.n = 0
        self.sum = 0
        self.min = None
        self.max = None

    def add(self, row, counter: Operator) -> None:
        self.n += 1
        if self.fn == "count":
            return
        value = row[self.idx]
        if self.fn in ("sum", "avg"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ExecError("SUM and AVG require INT")
            self.sum += value
            return
        if self.min is None:
            self.min = value
            self.max = value
            return
        counter.comparisons += 1
        c = cmp_values(value, self.min if self.fn == "min" else self.max)
        if self.fn == "min" and c < 0:
            self.min = value
        elif self.fn == "max" and c > 0:
            self.max = value

    def value(self):
        if self.fn == "count":
            return self.n
        if self.n == 0:
            raise ExecError("aggregate undefined on empty input")
        if self.fn == "sum":
            return self.sum
        if self.fn == "avg":
            return f"{(self.sum / self.n):.4f}"
        if self.fn == "min":
            return self.min
        return self.max


def _parse_aggs(aggregates, schema, node: str):
    specs = []
    out_schema = []
    for item in aggregates:
        fn, arg = item[0], item[1]
        alias = item[2] if len(item) >= 3 else None
        if fn in ("sum", "avg", "min", "max"):
            try:
                idx = resolve(arg, schema)
            except ExecError:
                raise ExecError(f"Node '{node}': unknown column '{arg}'.")
            col = schema[idx]
            if fn in ("sum", "avg") and col[1] != "INT":
                raise ExecError(f"Node '{node}': SUM and AVG require INT.")
            specs.append((fn, idx))
            name = alias or _agg_name(fn, arg)
            if fn == "avg":
                out_schema.append([name, "VARCHAR", 32])
            elif fn == "sum":
                out_schema.append([name, "INT"])
            else:
                renamed = list(col)
                renamed[0] = name
                out_schema.append(renamed)
        elif fn == "count":
            specs.append(("count", None))
            out_schema.append([alias or _agg_name("count", arg), "INT"])
        else:
            raise ExecError(f"Node '{node}': unknown aggregate '{fn}'.")
    return specs, out_schema


class GroupBy(Operator):
    def __init__(self, ctx, mode: str, keys: list, aggregates: list, child: Operator):
        super().__init__(ctx, "group_by")
        self.mode = mode
        self.keys = keys
        self.aggregates = aggregates
        self.child = child
        self.children = [child]
        self._gen = None
        self._sort = None

    def prepare(self) -> None:
        self.child.prepare()
        self.key_idx = []
        key_schema = []
        for name in self.keys:
            try:
                idx = resolve(name, self.child.schema)
            except ExecError:
                raise ExecError(f"Node 'group_by': unknown column '{name}'.")
            self.key_idx.append(idx)
            key_schema.append(list(self.child.schema[idx]))
        specs, agg_schema = _parse_aggs(self.aggregates, self.child.schema, "group_by")
        self.specs = specs
        self.schema = key_schema + agg_schema
        self._prepared = True

    def open(self) -> None:
        if not self.key_idx:
            self._gen = self._stream_total()
            return
        if self.mode == "sort":
            keys = [(self.child.schema[i][0], "asc") for i in self.key_idx]
            self._sort = Sort(self.ctx, self.child, keys)
            self._extras = [self._sort]
            self._sort.prepare()
            self._sort.open()
            self._gen = self._from_sorted()
        else:
            self._gen = self._from_hash()

    def _new_accs(self):
        return [Acc(fn, idx) for fn, idx in self.specs]

    def _stream_total(self):
        self.child.open()
        accs = self._new_accs()
        empty = True
        try:
            while True:
                tup = self.child.next()
                if tup is END:
                    break
                empty = False
                for acc in accs:
                    acc.add(tup, self)
            if empty:
                if any(fn != "count" for fn, _idx in self.specs):
                    raise ExecError("aggregate undefined on empty input")
                yield [0 if fn == "count" else None for fn, _idx in self.specs]
            else:
                yield [acc.value() for acc in accs]
        finally:
            self.child.close()

    def _from_sorted(self):
        tup = self._sort.next()
        while tup is not END:
            key = project_key(tup, self.key_idx)
            accs = self._new_accs()
            while tup is not END and project_key(tup, self.key_idx) == key:
                if accs[0].n > 0 or any(a.n > 0 for a in accs):
                    self.comparisons += 1
                for acc in accs:
                    acc.add(tup, self)
                tup = self._sort.next()
            yield list(key) + [acc.value() for acc in accs]

    def _from_hash(self):
        runs = _partition_child(self, self.child, self.key_idx, 1)
        try:
            self.child.close()
            yield from self._agg_parts(runs, 0, 1)
        finally:
            for run in runs:
                run.delete()

    def _agg_parts(self, runs, depth, salt):
        limit = self.budget.B - 1
        for run in runs:
            if run.num_pages() == 0:
                continue
            if run.num_pages() <= limit:
                yield from self._agg_run(run)
                continue
            if depth >= 3:
                yield from self._agg_by_sort(run)
                continue
            sub = _partition_run(self, run, self.child.schema, self.key_idx, salt + 1)
            try:
                biggest = max(part.num_pages() for part in sub)
                if biggest >= run.num_pages():
                    yield from self._agg_by_sort(run)
                else:
                    yield from self._agg_parts(sub, depth + 1, salt + 1)
            finally:
                for part in sub:
                    part.delete()

    def _agg_run(self, run):
        rows, n = _load_charged(run, self.child.schema, self.budget, self)
        try:
            groups = {}
            order = []
            for tup in rows:
                key = project_key(tup, self.key_idx)
                if key not in groups:
                    self.comparisons += 1
                    groups[key] = self._new_accs()
                    order.append(key)
                else:
                    self.comparisons += 1
                for acc in groups[key]:
                    acc.add(tup, self)
            for key in order:
                yield list(key) + [acc.value() for acc in groups[key]]
        finally:
            if n:
                self.budget.release(n)

    def _agg_by_sort(self, run):
        src = RunSource(self.ctx, run, self.child.schema, self)
        keys = [(self.child.schema[i][0], "asc") for i in self.key_idx]
        sorter = Sort(self.ctx, src, keys)
        self._extras.append(sorter)
        sorter.prepare()
        sorter.open()
        try:
            tup = sorter.next()
            while tup is not END:
                key = project_key(tup, self.key_idx)
                accs = self._new_accs()
                while tup is not END and project_key(tup, self.key_idx) == key:
                    for acc in accs:
                        acc.add(tup, self)
                    tup = sorter.next()
                yield list(key) + [acc.value() for acc in accs]
        finally:
            sorter.close()

    def next(self):
        if self._gen is None:
            return END
        try:
            row = next(self._gen)
        except StopIteration:
            self._gen = None
            return END
        self.tuples_out += 1
        return row

    def close(self) -> None:
        if self._gen is not None:
            self._gen.close()
            self._gen = None
        if self._sort is not None:
            self._sort.close()
            self._sort = None
        self.child.close()


class Distinct(Operator):
    def __init__(self, ctx, mode: str, child: Operator):
        super().__init__(ctx, "distinct")
        self.mode = mode
        self.child = child
        self.children = [child]
        self._gen = None
        self._sort = None

    def prepare(self) -> None:
        self.child.prepare()
        self.schema = [list(c) for c in self.child.schema]
        self._prepared = True

    def open(self) -> None:
        if self.mode == "sort":
            keys = [(c[0], "asc") for c in self.schema]
            self._sort = Sort(self.ctx, self.child, keys)
            self._extras = [self._sort]
            self._sort.prepare()
            self._sort.open()
            self._prev = None
        else:
            self._gen = self._from_hash()

    def next(self):
        if self.mode == "sort":
            while True:
                tup = self._sort.next()
                if tup is END:
                    return END
                if self._prev is not None and self._sort.cmp(tup, self._prev) == 0:
                    continue
                self._prev = list(tup)
                self.tuples_out += 1
                return list(tup)
        try:
            row = next(self._gen)
        except StopIteration:
            self._gen = None
            return END
        self.tuples_out += 1
        return row

    def _from_hash(self):
        indexes = list(range(len(self.schema)))
        runs = _partition_child(self, self.child, indexes, 1)
        try:
            self.child.close()
            yield from self._dedupe(runs, 0, 1)
        finally:
            for run in runs:
                run.delete()

    def _dedupe(self, runs, depth, salt):
        limit = self.budget.B - 1
        indexes = list(range(len(self.schema)))
        for run in runs:
            if run.num_pages() == 0:
                continue
            if run.num_pages() <= limit:
                rows, n = _load_charged(run, self.schema, self.budget, self)
                try:
                    seen = set()
                    for tup in rows:
                        key = tuple(tup)
                        self.comparisons += 1
                        if key in seen:
                            continue
                        seen.add(key)
                        yield list(tup)
                finally:
                    if n:
                        self.budget.release(n)
                continue
            if depth >= 3:
                yield from self._dedupe_sort(run)
                continue
            sub = _partition_run(self, run, self.schema, indexes, salt + 1)
            try:
                biggest = max(part.num_pages() for part in sub)
                if biggest >= run.num_pages():
                    yield from self._dedupe_sort(run)
                else:
                    yield from self._dedupe(sub, depth + 1, salt + 1)
            finally:
                for part in sub:
                    part.delete()

    def _dedupe_sort(self, run):
        src = RunSource(self.ctx, run, self.schema, self)
        keys = [(c[0], "asc") for c in self.schema]
        sorter = Sort(self.ctx, src, keys)
        self._extras.append(sorter)
        sorter.prepare()
        sorter.open()
        prev = None
        try:
            while True:
                tup = sorter.next()
                if tup is END:
                    break
                if prev is not None and sorter.cmp(tup, prev) == 0:
                    continue
                prev = list(tup)
                yield list(tup)
        finally:
            sorter.close()

    def close(self) -> None:
        if self._gen is not None:
            self._gen.close()
            self._gen = None
        if self._sort is not None:
            self._sort.close()
        self.child.close()


class _SetOp(Operator):
    def __init__(self, ctx, name: str, left: Operator, right: Operator):
        super().__init__(ctx, name)
        self.left = left
        self.right = right
        self.children = [left, right]
        self._gen = None

    def prepare(self) -> None:
        self.left.prepare()
        self.right.prepare()
        if not same_types(self.left.schema, self.right.schema):
            raise ExecError(f"Node '{self.op_name}': inputs are not union-compatible.")
        self.schema = [list(c) for c in self.left.schema]
        self._prepared = True

    def _sorted_run(self, child: Operator):
        keys = [(c[0], "asc") for c in child.schema]
        sorter = Sort(self.ctx, child, keys, spill_final=True)
        self._extras.append(sorter)
        sorter.prepare()
        return sorter.materialize(), sorter

    def close(self) -> None:
        if self._gen is not None:
            self._gen.close()
            self._gen = None
        self.left.close()
        self.right.close()


class Union( _SetOp):
    def __init__(self, ctx, left, right):
        super().__init__(ctx, "union", left, right)

    def open(self) -> None:
        self._gen = self._generate()

    def _generate(self):
        left_run, _ls = self._sorted_run(self.left)
        right_run, _rs = self._sorted_run(self.right)
        lc = RunCursor(left_run, self.left.schema, self.budget, self)
        rc = RunCursor(right_run, self.right.schema, self.budget, self)
        try:
            left = lc.pop()
            right = rc.pop()
            while left is not None or right is not None:
                if right is None or (left is not None and _row_cmp(self, left, right) < 0):
                    value = list(left)
                    while left is not None and _row_eq(self, left, value):
                        left = lc.pop()
                    yield value
                elif left is None or _row_cmp(self, left, right) > 0:
                    value = list(right)
                    while right is not None and _row_eq(self, right, value):
                        right = rc.pop()
                    yield value
                else:
                    value = list(left)
                    while left is not None and _row_eq(self, left, value):
                        left = lc.pop()
                    while right is not None and _row_eq(self, right, value):
                        right = rc.pop()
                    yield value
        finally:
            lc.close()
            rc.close()
            left_run.delete()
            right_run.delete()

    def next(self):
        return _pull(self)


class Intersect(_SetOp):
    def __init__(self, ctx, left, right):
        super().__init__(ctx, "intersect", left, right)

    def open(self) -> None:
        self._gen = self._generate()

    def _generate(self):
        left_run, _ls = self._sorted_run(self.left)
        right_run, _rs = self._sorted_run(self.right)
        lc = RunCursor(left_run, self.left.schema, self.budget, self)
        rc = RunCursor(right_run, self.right.schema, self.budget, self)
        try:
            left = lc.pop()
            right = rc.pop()
            while left is not None and right is not None:
                c = _row_cmp(self, left, right)
                if c < 0:
                    left = lc.pop()
                elif c > 0:
                    right = rc.pop()
                else:
                    value = list(left)
                    while left is not None and _row_eq(self, left, value):
                        left = lc.pop()
                    while right is not None and _row_eq(self, right, value):
                        right = rc.pop()
                    yield value
        finally:
            lc.close()
            rc.close()
            left_run.delete()
            right_run.delete()

    def next(self):
        return _pull(self)


class Except(_SetOp):
    def __init__(self, ctx, left, right):
        super().__init__(ctx, "except", left, right)

    def open(self) -> None:
        self._gen = self._generate()

    def _generate(self):
        left_run, _ls = self._sorted_run(self.left)
        right_run, _rs = self._sorted_run(self.right)
        lc = RunCursor(left_run, self.left.schema, self.budget, self)
        rc = RunCursor(right_run, self.right.schema, self.budget, self)
        try:
            left = lc.pop()
            right = rc.pop()
            while left is not None:
                if right is None or _row_cmp(self, left, right) < 0:
                    value = list(left)
                    while left is not None and _row_eq(self, left, value):
                        left = lc.pop()
                    yield value
                elif _row_cmp(self, left, right) > 0:
                    right = rc.pop()
                else:
                    value = list(left)
                    while left is not None and _row_eq(self, left, value):
                        left = lc.pop()
                    while right is not None and _row_eq(self, right, value):
                        right = rc.pop()
        finally:
            lc.close()
            rc.close()
            left_run.delete()
            right_run.delete()

    def next(self):
        return _pull(self)


def _row_cmp(counter: Operator, left, right) -> int:
    counter.comparisons += 1
    for a, b in zip(left, right):
        c = cmp_values(a, b)
        if c != 0:
            return c
    return 0


def _row_eq(counter: Operator, left, right) -> bool:
    return _row_cmp(counter, left, right) == 0


def _pull(op: Operator):
    if op._gen is None:
        return END
    try:
        row = next(op._gen)
    except StopIteration:
        op._gen = None
        return END
    op.tuples_out += 1
    return row


class NestedLoopJoin(Operator):
    def __init__(self, ctx, condition: str, left: Operator, right: Operator):
        super().__init__(ctx, "nested_loop_join")
        self.condition = condition
        self.left = left
        self.right = right
        self.children = [left, right]
        self.cond = None
        self._pending = None
        self._left_done = False
        self._block = None
        self._block_n = 0
        self._right_open = False
        self._inner = None
        self._oi = 0
        self._left_open = False

    def prepare(self) -> None:
        self.left.prepare()
        self.right.prepare()
        self.schema = [list(c) for c in self.left.schema] + [list(c) for c in self.right.schema]
        _check_names(self.schema, "nested_loop_join")
        self.cond = parse_cond(self.condition)
        try:
            check_condition_types(self.cond, self.schema, "nested_loop_join")
        except ExecError as e:
            _raise_column(e, "nested_loop_join")
        self._prepared = True

    def open(self) -> None:
        if self.budget.B < 3:
            raise ExecError("nested_loop_join needs at least 3 frames")
        self.left.open()
        self._left_open = True

    def _fill_block(self):
        cap = self.budget.B - 2
        if self.budget.held + cap + 1 > self.budget.B:
            cap = self.budget.B - self.budget.held - 1
        if cap < 1:
            raise ExecError("memory budget exceeded during nested_loop_join")
        rows = []
        pages = 0
        ended = False
        slot = None
        sp = None
        pending = self._pending
        try:
            while pages < cap and not ended:
                if slot is None:
                    slot = PageSlot(self.budget)
                    sp = _fresh(slot.buf)
                if pending is not None:
                    tup = pending
                    pending = None
                else:
                    tup = self.left.next()
                    if tup is END:
                        ended = True
                        break
                rec = encode_tuple(self.left.schema, tup)
                if sp.insert(rec) == NO_SPACE:
                    if sp.slot_count == 0:
                        raise ExecError("tuple does not fit in a page")
                    slot.release()
                    slot = None
                    self.budget.acquire(1)
                    pages += 1
                    pending = tup
                    if pages >= cap:
                        break
                    continue
                rows.append(list(tup))
            if slot is not None:
                if sp is not None and sp.slot_count > 0:
                    slot.release()
                    slot = None
                    self.budget.acquire(1)
                    pages += 1
                else:
                    slot.release()
                    slot = None
        finally:
            if slot is not None and slot.live:
                slot.release()
        self._pending = pending
        if ended and pending is None:
            self._left_done = True
        return pages, rows

    def _release_block(self) -> None:
        if self._block_n:
            self.budget.release(self._block_n)
            self._block_n = 0
        self._block = None

    def next(self):
        while True:
            if self._block is None:
                if self._left_done and self._pending is None:
                    return END
                if self._right_open:
                    self.right.close()
                    self._right_open = False
                pages, rows = self._fill_block()
                if pages == 0:
                    return END
                self._block = rows
                self._block_n = pages
                self.right.open()
                self._right_open = True
                self._inner = None
                self._oi = 0
            if self._inner is None:
                tup = self.right.next()
                if tup is END:
                    self._release_block()
                    continue
                self._inner = tup
                self._oi = 0
            if self._oi >= len(self._block):
                self._inner = None
                continue
            left = self._block[self._oi]
            self._oi += 1
            self.comparisons += 1
            row = list(left) + list(self._inner)
            if matches(self.cond, self.schema, row):
                self.tuples_out += 1
                return row

    def close(self) -> None:
        self._release_block()
        if self._left_open:
            self.left.close()
            self._left_open = False
        if self._right_open:
            self.right.close()
            self._right_open = False


class SortMergeJoin(Operator):
    def __init__(self, ctx, condition: str, left: Operator, right: Operator):
        super().__init__(ctx, "sort_merge_join")
        self.condition = condition
        self.left = left
        self.right = right
        self.children = [left, right]
        self._gen = None

    def prepare(self) -> None:
        self.left.prepare()
        self.right.prepare()
        self.schema = [list(c) for c in self.left.schema] + [list(c) for c in self.right.schema]
        _check_names(self.schema, "sort_merge_join")
        cond = parse_cond(self.condition)
        try:
            self.pairs = equality_keys(cond, self.left.schema, self.right.schema, "sort_merge_join")
        except ExecError as e:
            _raise_column(e, "sort_merge_join")
        self._prepared = True

    def open(self) -> None:
        if self.budget.B < 3:
            raise ExecError("sort_merge_join needs at least 3 frames")
        self._gen = self._generate()

    def _sort_side(self, child: Operator, indexes: list):
        keys = [(child.schema[i][0], "asc") for i in indexes]
        sorter = Sort(self.ctx, child, keys, spill_final=True)
        self._extras.append(sorter)
        sorter.prepare()
        return sorter.materialize()

    def _cmp_keys(self, left, right) -> int:
        self.comparisons += 1
        for li, ri in self.pairs:
            c = cmp_values(left[li], right[ri])
            if c != 0:
                return c
        return 0

    def _generate(self):
        left_run = self._sort_side(self.left, [p[0] for p in self.pairs])
        right_run = self._sort_side(self.right, [p[1] for p in self.pairs])
        lc = RunCursor(left_run, self.left.schema, self.budget, self)
        rc = RunCursor(right_run, self.right.schema, self.budget, self)
        reserve = PageSlot(self.budget)
        try:
            left = lc.pop()
            right = rc.pop()
            while left is not None and right is not None:
                c = self._cmp_keys(left, right)
                if c < 0:
                    left = lc.pop()
                elif c > 0:
                    right = rc.pop()
                else:
                    key = tuple(left[i] for i, _r in self.pairs)
                    extra, spill, right = self._buffer_inner(rc, reserve, left, key, right)
                    try:
                        while left is not None and tuple(left[i] for i, _r in self.pairs) == key:
                            if spill is not None:
                                yield from self._emit_spill(spill, reserve, left)
                            else:
                                yield from self._emit_buffered(extra, reserve, left)
                            left = lc.pop()
                    finally:
                        for slot in extra:
                            slot.release()
                        if spill is not None:
                            spill.delete()
        finally:
            lc.close()
            rc.close()
            reserve.release()
            left_run.delete()
            right_run.delete()

    def _same_key(self, left, right, key) -> bool:
        return self._cmp_keys(left, right) == 0 and tuple(right[j] for _i, j in self.pairs) == key

    def _buffer_inner(self, rc, reserve, left, key, right):
        """Keep one equal-key run in free frames. Spill only when it does not fit."""
        extra: list[PageSlot] = []
        spill = None
        sp = _fresh(reserve.buf)

        def spill_page() -> None:
            nonlocal spill, sp
            if spill is None and self.budget.held < self.budget.B:
                slot = PageSlot(self.budget)
                slot.buf[:] = reserve.buf
                extra.append(slot)
                sp = _fresh(reserve.buf)
                return
            if spill is None:
                spill = self.ctx.new_run()
                while extra:
                    slot = extra[0]
                    spill.write_page(slot.buf, self)
                    extra.pop(0)
                    slot.release()
            spill.write_page(reserve.buf, self)
            sp = _fresh(reserve.buf)

        while right is not None and self._same_key(left, right, key):
            rec = encode_tuple(self.right.schema, right)
            if sp.insert(rec) == NO_SPACE:
                spill_page()
                if sp.insert(rec) == NO_SPACE:
                    raise ExecError("tuple does not fit in a page")
            right = rc.pop()
        if spill is not None and sp.slot_count:
            spill.write_page(reserve.buf, self)
        return extra, spill, right

    def _emit_buffered(self, extra, reserve, left):
        for slot in list(extra) + [reserve]:
            sp = SlottedPage(slot.buf)
            for s in sp.live_slots():
                right = decode_tuple(self.right.schema, sp.get(s))
                yield list(left) + list(right)

    def _emit_spill(self, spill, reserve, left):
        for pid in range(spill.num_pages()):
            spill.read_page(pid, reserve.buf, self)
            sp = SlottedPage(reserve.buf)
            for s in sp.live_slots():
                right = decode_tuple(self.right.schema, sp.get(s))
                yield list(left) + list(right)

    def next(self):
        return _pull(self)

    def close(self) -> None:
        if self._gen is not None:
            self._gen.close()
            self._gen = None
        self.left.close()
        self.right.close()


class HashJoin(Operator):
    def __init__(self, ctx, condition: str, left: Operator, right: Operator):
        super().__init__(ctx, "hash_join")
        self.condition = condition
        self.left = left
        self.right = right
        self.children = [left, right]
        self._gen = None
        self.partitions = None
        self.overflow = []

    def prepare(self) -> None:
        self.left.prepare()
        self.right.prepare()
        self.schema = [list(c) for c in self.left.schema] + [list(c) for c in self.right.schema]
        _check_names(self.schema, "hash_join")
        cond = parse_cond(self.condition)
        try:
            self.pairs = equality_keys(cond, self.left.schema, self.right.schema, "hash_join")
        except ExecError as e:
            _raise_column(e, "hash_join")
        self.left_idx = [p[0] for p in self.pairs]
        self.right_idx = [p[1] for p in self.pairs]
        self._prepared = True

    def open(self) -> None:
        if self.budget.B < 3:
            raise ExecError("hash_join needs at least 3 frames")
        self._gen = self._generate()

    def _generate(self):
        if self._build_fits():
            yield from self._join_in_memory()
            return
        r_runs = _partition_child(self, self.left, self.left_idx, 1)
        s_runs = _partition_child(self, self.right, self.right_idx, 1)
        self.partitions = {
            "build_pages": [run.num_pages() for run in r_runs],
            "probe_pages": [run.num_pages() for run in s_runs],
        }
        try:
            self.left.close()
            self.right.close()
            for i in range(len(r_runs)):
                yield from self._probe(r_runs[i], s_runs[i], 0)
        finally:
            for run in r_runs + s_runs:
                run.delete()

    def _build_fits(self) -> bool:
        child = self.left
        if not getattr(child, "raw_ok", False) or isinstance(child, RunSource):
            return False
        child.open_raw()
        return len(child.ids) <= self.budget.B - 1

    def _emit_match(self, buckets, tup):
        key = project_key(tup, self.right_idx)
        cands = buckets.get(key)
        if not cands:
            self.comparisons += 1
            return
        for build in cands:
            self.comparisons += 1
            yield list(build) + list(tup)

    def _join_in_memory(self):
        """Build side fits in B - 1 pages, so grace partitioning would only add partial pages."""
        rows = []
        charged = 0
        probe = None
        try:
            while True:
                slot = PageSlot(self.budget)
                try:
                    ok = self.left.read_data_page(slot.buf)
                    page_rows = []
                    if ok:
                        sp = SlottedPage(slot.buf)
                        page_rows = [
                            decode_tuple(self.left.schema, sp.get(s)) for s in sp.live_slots()
                        ]
                finally:
                    slot.release()
                if not ok:
                    break
                self.budget.acquire(1)
                charged += 1
                rows.extend(page_rows)
            buckets = defaultdict(list)
            for tup in rows:
                buckets[project_key(tup, self.left_idx)].append(tup)
            probe_pages = [0]
            if getattr(self.right, "raw_ok", False) and not isinstance(self.right, RunSource):
                self.right.open_raw()
                probe_pages = [len(self.right.ids)]
                probe = PageSlot(self.budget)
                while self.right.read_data_page(probe.buf):
                    sp = SlottedPage(probe.buf)
                    for s in sp.live_slots():
                        tup = decode_tuple(self.right.schema, sp.get(s))
                        yield from self._emit_match(buckets, tup)
            else:
                self.right.open()
                while True:
                    tup = self.right.next()
                    if tup is END:
                        break
                    yield from self._emit_match(buckets, tup)
            self.partitions = {
                "build_pages": [charged],
                "probe_pages": probe_pages,
                "mode": "memory",
            }
            self.overflow = []
        finally:
            if probe is not None:
                probe.release()
            if charged:
                self.budget.release(charged)
            self.left.close()
            self.right.close()

    def _probe(self, r_run, s_run, depth):
        if r_run.num_pages() == 0 or s_run.num_pages() == 0:
            return
        limit = self.budget.B - 1
        if r_run.num_pages() <= limit:
            yield from self._probe_mem(r_run, s_run)
            return
        if depth >= 3:
            self.overflow.append({
                "depth": depth,
                "build_pages": r_run.num_pages(),
                "probe_pages": s_run.num_pages(),
                "action": "bnlj",
            })
            yield from self._probe_bnlj(r_run, s_run)
            return
        rs = _partition_run(self, r_run, self.left.schema, self.left_idx, depth + 2)
        ss = _partition_run(self, s_run, self.right.schema, self.right_idx, depth + 2)
        try:
            child_pages = [part.num_pages() for part in rs]
            biggest = max(child_pages) if child_pages else 0
            if biggest >= r_run.num_pages():
                self.overflow.append({
                    "depth": depth,
                    "build_pages": r_run.num_pages(),
                    "probe_pages": s_run.num_pages(),
                    "action": "bnlj",
                    "reason": "repartition did not shrink the build side",
                    "child_build_pages": child_pages,
                })
                for part, probe in zip(rs, ss):
                    if part.num_pages() == 0 or probe.num_pages() == 0:
                        continue
                    if part.num_pages() >= r_run.num_pages():
                        yield from self._probe_bnlj(part, probe)
                    else:
                        yield from self._probe(part, probe, depth + 1)
            else:
                self.overflow.append({
                    "depth": depth,
                    "build_pages": r_run.num_pages(),
                    "probe_pages": s_run.num_pages(),
                    "action": "repartition",
                    "child_build_pages": [part.num_pages() for part in rs],
                })
                for a, b in zip(rs, ss):
                    yield from self._probe(a, b, depth + 1)
        finally:
            for part in rs + ss:
                part.delete()

    def _probe_mem(self, r_run, s_run):
        rows, n = _load_charged(r_run, self.left.schema, self.budget, self)
        slot = None
        try:
            buckets = defaultdict(list)
            for tup in rows:
                buckets[project_key(tup, self.left_idx)].append(tup)
            slot = PageSlot(self.budget)
            for pid in range(s_run.num_pages()):
                s_run.read_page(pid, slot.buf, self)
                sp = SlottedPage(slot.buf)
                for s in sp.live_slots():
                    tup = decode_tuple(self.right.schema, sp.get(s))
                    key = project_key(tup, self.right_idx)
                    cands = buckets.get(key)
                    if not cands:
                        self.comparisons += 1
                        continue
                    for build in cands:
                        self.comparisons += 1
                        yield list(build) + list(tup)
        finally:
            if slot is not None:
                slot.release()
            if n:
                self.budget.release(n)

    def _probe_bnlj(self, r_run, s_run):
        cap = self.budget.B - 2
        page_i = 0
        n_pages = r_run.num_pages()
        while page_i < n_pages:
            rows = []
            got = 0
            while got < cap and page_i < n_pages:
                slot = PageSlot(self.budget)
                try:
                    r_run.read_page(page_i, slot.buf, self)
                    page_i += 1
                    sp = SlottedPage(slot.buf)
                    page_rows = [
                        decode_tuple(self.left.schema, sp.get(s)) for s in sp.live_slots()
                    ]
                finally:
                    slot.release()
                self.budget.acquire(1)
                got += 1
                rows.extend(page_rows)
            try:
                slot = PageSlot(self.budget)
                try:
                    for pid in range(s_run.num_pages()):
                        s_run.read_page(pid, slot.buf, self)
                        sp = SlottedPage(slot.buf)
                        for s in sp.live_slots():
                            right = decode_tuple(self.right.schema, sp.get(s))
                            rkey = project_key(right, self.right_idx)
                            for left in rows:
                                self.comparisons += 1
                                if project_key(left, self.left_idx) == rkey:
                                    yield list(left) + list(right)
                finally:
                    slot.release()
            finally:
                if got:
                    self.budget.release(got)

    def next(self):
        return _pull(self)

    def close(self) -> None:
        if self._gen is not None:
            self._gen.close()
            self._gen = None
        self.left.close()
        self.right.close()


def _partition_child(counter: Operator, child: Operator, indexes: list, salt: int):
    if getattr(child, "raw_ok", False) and not isinstance(child, RunSource):
        child.open_raw()
        return _partition_reader(
            counter, child.schema, indexes, salt,
            child.read_data_page, external_input=True,
        )
    if not getattr(child, "_opened_part", False):
        child.open()
    return _partition_reader(
        counter, child.schema, indexes, salt,
        None, external_input=False, pull=child,
    )


def _partition_run(counter: Operator, run, schema, indexes, salt: int):
    src = RunSource(counter.ctx, run, schema, counter)
    src.open_raw()
    return _partition_reader(
        counter, schema, indexes, salt, src.read_data_page, external_input=True,
    )


def _partition_reader(counter, schema, indexes, salt, read_page, external_input: bool, pull=None):
    # Leave one frame for the input page when we read it ourselves. A parent
    # (ORDER BY's sort, for example) may already be holding a frame, so the
    # partition count is whatever is still free, not always B - 1.
    extra = 1 if external_input else 0
    parts = counter.budget.B - counter.budget.held - extra
    if parts < 1:
        raise ExecError("memory budget exceeded during hash partition")
    out_slots = [PageSlot(counter.budget) for _ in range(parts)]
    in_slot = PageSlot(counter.budget) if external_input else None
    pages = [_fresh(slot.buf) for slot in out_slots]
    runs = [counter.ctx.new_run() for _ in range(parts)]
    try:
        def append(tup):
            key = project_key(tup, indexes)
            part = fnv(key, salt) % parts
            rec = encode_tuple(schema, tup)
            if pages[part].insert(rec) == NO_SPACE:
                runs[part].write_page(out_slots[part].buf, counter)
                pages[part] = _fresh(out_slots[part].buf)
                if pages[part].insert(rec) == NO_SPACE:
                    raise ExecError("tuple does not fit in a page")

        if external_input:
            while read_page(in_slot.buf):
                sp = SlottedPage(in_slot.buf)
                for s in sp.live_slots():
                    append(decode_tuple(schema, sp.get(s)))
        else:
            while True:
                tup = pull.next()
                if tup is END:
                    break
                append(tup)
        for i in range(parts):
            if pages[i].slot_count:
                runs[i].write_page(out_slots[i].buf, counter)
    finally:
        for slot in out_slots:
            slot.release()
        if in_slot is not None:
            in_slot.release()
    return runs


def _load_charged(run, schema, budget, counter):
    rows = []
    n = 0
    try:
        for pid in range(run.num_pages()):
            slot = PageSlot(budget)
            try:
                run.read_page(pid, slot.buf, counter)
                sp = SlottedPage(slot.buf)
                page_rows = [decode_tuple(schema, sp.get(s)) for s in sp.live_slots()]
            finally:
                slot.release()
            budget.acquire(1)
            n += 1
            rows.extend(page_rows)
    except Exception:
        if n:
            budget.release(n)
        raise
    return rows, n
