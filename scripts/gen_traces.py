"""Generate the five measurement traces described in M1 §8 when Brightspace
files are not present. Trace 3 is a sequential scan of an 80-page file
repeated ten times."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "traces"
TRACES.mkdir(exist_ok=True)


def write(name: str, ids: list[int]) -> None:
    path = TRACES / name
    path.write_text("\n".join(str(i) for i in ids) + "\n", encoding="utf-8")
    print(name, len(ids), "refs")


def main():
    # Trace 0: §5.3 worked example
    write("trace0_example.txt", [1, 2, 3, 1, 4, 2])

    # Trace 1: random-ish within 32 pages
    write("trace1_random.txt", [(i * 7 + 3) % 32 for i in range(500)])

    # Trace 2: repeated hot set of 8 pages
    hot = list(range(8))
    write("trace2_hot.txt", (hot * 40))

    # Trace 3: sequential scan of 80 pages, repeated 10 times (LRU thrash case)
    seq = list(range(80))
    write("trace3_seq80x10.txt", seq * 10)

    # Trace 4: looping over 16 pages
    write("trace4_loop16.txt", list(range(16)) * 30)

    # Trace 5: mixed stride
    write("trace5_stride.txt", [(i * 3) % 64 for i in range(400)])


if __name__ == "__main__":
    main()
