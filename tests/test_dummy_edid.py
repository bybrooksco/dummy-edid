"""Offline tests against a real plug's EDID. Run: python3 -m unittest discover tests"""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dummy_edid as d  # noqa: E402

FIX = Path(__file__).parent / 'fixtures'
FACTORY = (FIX / 'xieoery-x15-factory.bin').read_bytes()
# Written to a real Xieoery X15 and confirmed working at 3600x2400 on an RTX 2060 SUPER.
KNOWN_GOOD = (FIX / 'xieoery-x15-3600x2400.bin').read_bytes()


class Tests(unittest.TestCase):
    def test_reproduces_known_good_plug(self):
        new, pclk = d.add_mode(FACTORY, 3600, 2400, 60, 4, 600e6)
        self.assertEqual(new, KNOWN_GOOD)
        self.assertEqual(pclk, 545_150_000)

    def test_rejects_mode_over_hdmi20_clock(self):
        with self.assertRaises(SystemExit):
            d.add_mode(FACTORY, 3840, 2560, 60, 4, 600e6)

    def test_refuses_preferred_slot(self):
        with self.assertRaises(SystemExit):
            d.add_mode(FACTORY, 3000, 2000, 60, 1, 600e6)

    def test_checksums_valid_and_only_one_dtd_changes(self):
        new, _ = d.add_mode(FACTORY, 2560, 1600, 60, 2, 600e6)
        d.check_edid(new)
        diff = [i for i in range(256) if new[i] != FACTORY[i]]
        self.assertTrue(all(72 <= i < 90 or i == 127 for i in diff), diff)
        self.assertEqual(d.decode_dtd(new[72:90])[:2], (2560, 1600))

    def test_cli_file_mode(self):
        out = Path(self._testMethodName + '.bin')
        try:
            subprocess.run([sys.executable, str(Path(d.__file__)), 'add', str(FIX / 'xieoery-x15-factory.bin'),
                            '3600x2400@60', '--slot', '4', '--file', '-o', str(out)], check=True, capture_output=True)
            self.assertEqual(out.read_bytes(), KNOWN_GOOD)
        finally:
            out.unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
