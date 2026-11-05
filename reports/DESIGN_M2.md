# Module 2 design

Every operator in a plan shares one budget of `B` page frames. A frame is a 4096-byte buffer. A buffer calls `acquire` before it is created and `release` when it is dropped, so a frame is counted only while it is live. A decoded list of tuples is charged for the pages it was read from. That charge is released before a new frame is taken for output. `peak` is the most frames held at once. After `close`, `held` is 0 and every temporary run file is deleted, as the specification requires.

Pipelined operators hold one frame: the page they are reading now. Those operators are seqscan, filter, project, `union_all`, and limit. A blocking operator, such as sort or hash join, uses whatever frames are still free. If a seqscan has no operator above it, it lends its read buffer to its parent. External sort can then fill all `B` pages on pass 0. If a pipelined operator sits above the scan, that operator keeps the scan's frame, and each sort run is `B − 1` pages.

## Sort

Sort is an external merge sort. One in-memory run is sorted with a counted comparison. A merge reads `B − 1` runs at a time and keeps one frame for the output page. If a run would be the only member of its merge group, it is carried forward and is not rewritten.

Pass 0, and every merge except the last, runs inside `open`. The last pass streams rows through `next` and does not write them out.

The number of passes is `1 + ceil(log(ceil(N / B)) / log(B − 1))`.

For `N = 8` and `B = 3` the input becomes three runs of 3, 3, and 2 pages. Pass 0 writes those 8 pages. The 8 input reads stay on the child scan. Pass 1 merges two runs and carries the third forward: 6 reads and 6 writes. Pass 2 streams the last two runs: 8 reads and 0 writes. The sort itself reads 14 pages and writes 14. With the scan's 8 reads, the plan reads 22 pages and writes 14.

## Hash join

Hash join is a grace hash join. The hash is FNV-1a with a salt. It is not Python's `hash`. If the build input is a scan of at most `B − 1` pages, those pages are loaded and the probe input is streamed past the table. Nothing is partitioned. Otherwise it opens `P = B − 1` partitions and keeps one frame for the input page.

If a build partition is still larger than `B − 1` pages, it is partitioned again with a new salt. If a round does not make the largest piece smaller, that piece is joined with block nested loop and the smaller pieces are probed or partitioned again. If the depth reaches 3, the remaining pair is a block nested loop. The in-memory table is never larger than `B − 1` pages.

## Sort-merge join

Each input is sorted with the sort operator, one side at a time. The finished run is written to disk so both cursors can be open together. The merge then holds three frames: one for each sorted input and one for output.

When keys are equal, the matching inner tuples are kept in the free frames and rescanned for each outer tuple of that key. If the run does not fit in the frames still free, it is written to a temporary file and that file is scanned once per outer tuple. A run larger than `B` still completes.

## Distinct, grouping, and sets

Sort-distinct drops a tuple when it matches the one just emitted.

Hash-distinct and hash grouping use the same partition and overflow rule as hash join. Each partition is finished before the next one starts.

An aggregate with no group keys is one running value. An empty input yields one row for `COUNT`. Any other aggregate on an empty input is an error. With group keys, an empty input yields no rows. `AVG` is returned as a string with four decimal places.

`UNION`, `INTERSECT`, and `EXCEPT` sort both inputs and then merge them. `UNION ALL` reads the left input to completion before it opens the right input, so it holds one frame.
