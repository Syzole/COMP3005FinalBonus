"""Test page.py"""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from storage.page import SlottedPage, NO_SPACE


class TestPage(unittest.TestCase):
    def test_section_3_2(self):
        page = SlottedPage(page_id=0)
        self.assertEqual(page.slot_count, 0)
        self.assertEqual(page.free_end, 4096)
        self.assertEqual(page.free_space(), 4088)

        s0 = page.insert(bytes(13))
        self.assertEqual(s0, 0)
        self.assertEqual(page.slot_count, 1)
        self.assertEqual(page.free_end, 4083)
        self.assertEqual(page.free_space(), 4071)
        self.assertEqual(page.get_slot(0), (4083, 13))

        s1 = page.insert(bytes(20))
        self.assertEqual(s1, 1)
        self.assertEqual(page.slot_count, 2)
        self.assertEqual(page.free_end, 4063)
        self.assertEqual(page.free_space(), 4047)
        self.assertEqual(page.get_slot(1), (4063, 20))

        s2 = page.insert(bytes(13))
        self.assertEqual(s2, 2)
        self.assertEqual(page.slot_count, 3)
        self.assertEqual(page.free_end, 4050)
        self.assertEqual(page.free_space(), 4030)
        self.assertEqual(page.get_slot(2), (4050, 13))

        page.delete(1)
        self.assertEqual(page.slot_count, 3)
        self.assertEqual(page.free_end, 4050)
        self.assertEqual(page.free_space(), 4030)
        self.assertEqual(page.get_slot(1), (0, 0))

        # capacity for L=13
        self.assertEqual(4088 // (13 + 4), 240)

    def test_dead_slot_read_fails(self):
        page = SlottedPage(page_id=1)
        page.insert(b"abc")
        page.delete(0)
        with self.assertRaises(Exception) as cm:
            page.get(0)
        msg = str(cm.exception).lower()
        self.assertTrue("dead" in msg or "slot" in msg)

    def test_no_space(self):
        page = SlottedPage(page_id=2)
        n = 0
        while True:
            r = page.insert(bytes(100))
            if r == NO_SPACE:
                break
            n += 1
        self.assertGreater(n, 0)
        self.assertEqual(page.insert(bytes(100)), NO_SPACE)


if __name__ == "__main__":
    unittest.main()
