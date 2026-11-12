As a note: Brightspace did not publish the five measurement traces in a form I could download, so I generated the §8-style traces with `scripts/gen_traces.py` (see `traces/`). Raw hit-rate JSON: `data/m1_policy_measurements.json` from `scripts/measure_m1_policies.py`.

# Capacity math
I used the following formula:
For fixed length records of L bytes, the number per page is floor(4088 / (L + 4)). 

So for a schema with 3 ints, that is 4*3 => L = 12

So 4088 / 16 => 255.5 => 255 records per page

For a schema of a INT, b VARCHAR(5)='bob', c INT, L = (4 + (2+3) + 4) => 13

So 4088/17 = 240.47 => 240 records per page

For a schema that has a (a INT, b VARCHAR(200)) => L = (4 + (2+100)) = 106, for the note we picked ours string to be x 100 times

So 4088/110 = 37.16 => 37 records per page

Measuring them I got the following results:
| Schema | Record bytes L | Computed | Measured |
|--------|----------------:|---------:|---------:|
| all INT `(a,b,c)` | 12 | 255 | 255 |
| short VARCHAR `(a INT, b VARCHAR(5)='bob', c INT)` | 13 | 240 | 240 |
| long VARCHAR `(a INT, b VARCHAR(200)` with 100-byte value) | 106 | 37 | 37 |

As it can be seen here the computed and measured values are the same, which is expected.


# Policy testing (M1spec §8)

Five traces × LRU/CLOCK × B ∈ {4, 16, 64, 256}. Hit rate = hits / (hits + misses).

Trace meanings: `trace1` random over 32 pages; `trace2` hot set of 8; `trace3` sequential scan of 80 pages × 10; `trace4` loop over 16; `trace5` stride over 64.

### LRU
| Trace | B=4 | B=16 | B=64 | B=256 |
|-------|----:|-----:|-----:|------:|
| trace1 | 0.000 | 0.000 | 0.936 | 0.936 |
| trace2 | 0.000 | 0.975 | 0.975 | 0.975 |
| trace3 | 0.000 | 0.000 | 0.000 | 0.900 |
| trace4 | 0.000 | 0.967 | 0.967 | 0.967 |
| trace5 | 0.000 | 0.000 | 0.840 | 0.840 |

### CLOCK
| Trace | B=4 | B=16 | B=64 | B=256 |
|-------|----:|-----:|-----:|------:|
| trace1 | 0.000 | 0.000 | 0.936 | 0.936 |
| trace2 | 0.000 | 0.975 | 0.975 | 0.975 |
| trace3 | 0.000 | 0.000 | 0.000 | 0.900 |
| trace4 | 0.000 | 0.967 | 0.967 | 0.967 |
| trace5 | 0.000 | 0.000 | 0.840 | 0.840 |

On these traces LRU and CLOCK tied. **Trace 3 at B = 64:** LRU hit rate is **0.0**. The working set is 80 pages and B = 64 < 80, so each sequential pass evicts the page that will be needed next; repeating the scan ten times never finds a resident page. At B = 256 (≥ 80) the first pass fills the pool and later passes hit (0.900).

I would ship **CLOCK**: same hit rates here, cheaper than maintaining an exact LRU list on every pin, and the §5.3 worked example already shows CLOCK can beat LRU on short reuse patterns.

# Scan cost
Table with about 500 pages (501 if you count the directory page), scanned at B = 2, 8, 64:

| B | tuples | file pages | page reads | page writes |
|--:|-------:|-----------:|-----------:|------------:|
| 2 | 254990 | 501 | 501 | 0 |
| 8 | 254990 | 501 | 501 | 0 |
| 64 | 254990 | 501 | 501 | 0 |

As it can be seen here the page reads and writes are the same for all the policies, which is expected, since no pages are written to the disk, and also no cache is used.

# Test on 5.3
| Policy | hits | misses | evictions |
|--------|-----:|-------:|----------:|
| lru | 1 | 5 | 2 |
| clock | 2 | 4 | 1 |

# Readings

1. **Ramakrishnan and Gehrke, Database Management Systems, ch. 9** — page/record layout and heap organization; used for the capacity formula `floor(4088/(L+4))` and the scan cost expectation (≈ one read per data/dir page).
2. **Silberschatz, Korth, Sudarshan, ch. 13** — buffer management and sequential flooding; explains Trace 3 at B=64 (LRU hit rate 0) and the choice to ship CLOCK.