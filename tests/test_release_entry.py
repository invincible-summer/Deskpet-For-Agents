from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import main


class ReleaseEntryTests(unittest.TestCase):
    def test_version_dispatch_does_not_touch_gui(self):
        out = io.StringIO()
        with patch("sys.stdout", out), patch.object(main, "_dpi_aware") as dpi:
            self.assertEqual(main.main(["--version"]), 0)
        self.assertTrue(out.getvalue().strip())
        dpi.assert_not_called()

    def test_converter_dispatch_happens_before_platform_guard(self):
        with patch("tools.convert.main", return_value=7) as worker, \
             patch.object(main, "_dpi_aware") as dpi:
            rc = main.main(["--deskpet-internal-converter", "x", "y"])
        self.assertEqual(rc, 7)
        worker.assert_called_once_with(["x", "y"])
        dpi.assert_not_called()
