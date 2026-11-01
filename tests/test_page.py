"""Test page.py"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.page import SlottedPage, NO_SPACE


def test_section_3_2():
    page = SlottedPage(page_id=0)
    assert page.slot_count == 0
    assert page.free_end == 4096
    assert page.free_space() == 4088

    s0 = page.insert(bytes(13))
    assert s0 == 0
    assert page.slot_count == 1
    assert page.free_end == 4083
    assert page.free_space() == 4071
    assert page.get_slot(0) == (4083, 13)

    s1 = page.insert(bytes(20))
    assert s1 == 1
    assert page.slot_count == 2
    assert page.free_end == 4063
    assert page.free_space() == 4047
    assert page.get_slot(1) == (4063, 20)

    s2 = page.insert(bytes(13))
    assert s2 == 2
    assert page.slot_count == 3
    assert page.free_end == 4050
    assert page.free_space() == 4030
    assert page.get_slot(2) == (4050, 13)

    page.delete(1)
    assert page.slot_count == 3
    assert page.free_end == 4050
    assert page.free_space() == 4030
    assert page.get_slot(1) == (0, 0)

    # capacity for L=13
    assert 4088 // (13 + 4) == 240


def test_dead_slot_read_fails():
    page = SlottedPage(page_id=1)
    page.insert(b"abc")
    page.delete(0)
    try:
        page.get(0)
        assert False, "expected error"
    except Exception as e:
        assert "dead" in str(e).lower() or "slot" in str(e).lower()


def test_no_space():
    page = SlottedPage(page_id=2)
    # Fill until no space
    n = 0
    while True:
        r = page.insert(bytes(100))
        if r == NO_SPACE:
            break
        n += 1
    assert n > 0
    assert page.insert(bytes(100)) == NO_SPACE


if __name__ == "__main__":
    test_section_3_2()
    test_dead_slot_read_fails()
    test_no_space()
    print("test_page: all ok")
