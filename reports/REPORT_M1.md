As a note all the traces in the report are in the traces folder, and were genearted using a script. I could not find them on the brightspace and will ask the prof about it.
# Capcity math
I used the following formula:
For fixed length records of L bytes, the number per page is floor(4088 / (L + 4)). 

So for a schme with 3 ints, that is 4*3 => L = 12

So 4088 / 16 => 255.5 => 255 records per page

For a scehma of a INT, b VARCHAR(5)='bob', c INT, L = (4 + (2+3) + 4) => 13

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


# Policy testing (MISING TRACCES MUST ASK PROF)
I tested the following policies:
- LRU
- Clock


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