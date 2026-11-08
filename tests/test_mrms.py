"""Unit tests for MRMS observation retrieval and integration."""

from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from automats.mrms import (
    PNG_END,
    PNG_SIGNATURE,
    build_mrms_url,
    fetch_mrms_images,
    fetch_single_mrms,
    format_mrms_filename,
    is_valid_mrms_file,
    parse_mrms_filename,
)
from automats.graphics import resolve_obs_cell_time


class TestMRMS(unittest.TestCase):
    def test_format_mrms_filename(self):
        dt = datetime(2024, 5, 7, 6, 0)
        self.assertEqual(format_mrms_filename("cref", dt), "mrms_cref_202405070600.png")
        self.assertEqual(format_mrms_filename("CREF", dt), "mrms_cref_202405070600.png")
        self.assertEqual(format_mrms_filename("echo_top_18", dt), "mrms_echo_top_18_202405070600.png")

    def test_parse_mrms_filename(self):
        parsed = parse_mrms_filename("mrms_cref_202405070600.png")
        self.assertIsNotNone(parsed)
        var, dt = parsed
        self.assertEqual(var, "cref")
        self.assertEqual(dt, datetime(2024, 5, 7, 6, 0))

        # Test 10-digit format
        parsed10 = parse_mrms_filename("mrms_cref_2024050706.png")
        self.assertIsNotNone(parsed10)
        self.assertEqual(parsed10[1], datetime(2024, 5, 7, 6, 0))

        # Invalid format
        self.assertIsNone(parse_mrms_filename("random_image.png"))

    def test_build_mrms_url(self):
        dt = datetime(2024, 5, 7, 6, 0)
        url = build_mrms_url(dt, var="cref")
        self.assertIn("year=2024", url)
        self.assertIn("month=5", url)
        self.assertIn("day=7", url)
        self.assertIn("hour=6", url)
        self.assertIn("minute=0", url)
        self.assertIn("product_type=crefls", url)
        self.assertIn("product=CREF", url)

    def test_is_valid_mrms_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test.png"

            # Empty file
            p.write_bytes(b"")
            self.assertFalse(is_valid_mrms_file(p))

            # Small dummy file
            p.write_bytes(PNG_SIGNATURE + b"dummy" + PNG_END)
            self.assertFalse(is_valid_mrms_file(p))

            # Valid large PNG
            content = PNG_SIGNATURE + b"x" * 2000 + PNG_END
            p.write_bytes(content)
            self.assertTrue(is_valid_mrms_file(p))

    def test_resolve_obs_cell_time_same_valid_time(self):
        spec = {
            "type": "same valid time",
            "cycle": "2024050700",
            "graphics name": "cref_full_sfc_f006.png",
        }
        var, valid_dt = resolve_obs_cell_time(spec, ["obs"], "obs")
        self.assertEqual(var, "cref")
        self.assertEqual(valid_dt, datetime(2024, 5, 7, 6, 0))

    def test_resolve_obs_cell_time_same_cycle(self):
        spec = {
            "type": "same cycle",
            "cycle": "2024050700",
        }
        row1 = ["obs", "cref_full_sfc_f001.pg"]
        var1, valid_dt1 = resolve_obs_cell_time(spec, row1, "obs")
        self.assertEqual(var1, "cref")
        self.assertEqual(valid_dt1, datetime(2024, 5, 7, 1, 0))

        row2 = ["obs", "cref_full_sfc_f002.png"]
        var2, valid_dt2 = resolve_obs_cell_time(spec, row2, "obs")
        self.assertEqual(var2, "cref")
        self.assertEqual(valid_dt2, datetime(2024, 5, 7, 2, 0))

    def test_resolve_obs_cell_time_explicit(self):
        spec = {"type": "same cycle", "cycle": "2024050700"}

        # Explicit timestamp
        var1, valid_dt1 = resolve_obs_cell_time(spec, ["obs_202405070800"], "obs_202405070800")
        self.assertEqual(var1, "cref")
        self.assertEqual(valid_dt1, datetime(2024, 5, 7, 8, 0))

        # Explicit variable and timestamp
        var2, valid_dt2 = resolve_obs_cell_time(spec, ["obs_cref_202405070900"], "obs_cref_202405070900")
        self.assertEqual(var2, "cref")
        self.assertEqual(valid_dt2, datetime(2024, 5, 7, 9, 0))

    def test_fetch_mrms_images_cache_hit(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            obs_dir = output_dir / "graphics" / "obs"
            obs_dir.mkdir(parents=True, exist_ok=True)

            dt = datetime(2024, 5, 7, 6, 0)
            cached_file = obs_dir / format_mrms_filename("cref", dt)
            cached_file.write_bytes(PNG_SIGNATURE + b"x" * 2000 + PNG_END)

            tasks = [{"var": "cref", "valid_dt": dt, "dest_path": cached_file}]

            logs = []
            res = fetch_mrms_images(tasks, output_dir=output_dir, log=logs.append)
            self.assertIn(str(cached_file), res)
            self.assertTrue(any("CACHE HIT" in l for l in logs))

    def test_fetch_single_mrms_mock(self):
        with tempfile.TemporaryDirectory() as td:
            target_path = Path(td) / "mrms_cref_202405070600.png"
            mock_page = MagicMock()
            mock_page.evaluate.return_value = json.dumps({"x": 10, "y": 20, "width": 800, "height": 600})
            valid_png = PNG_SIGNATURE + b"x" * 1500 + PNG_END
            mock_page.screenshot.return_value = valid_png

            ok = fetch_single_mrms(
                page=mock_page,
                url="https://example.com",
                target_path=target_path,
                max_attempts=1,
            )
            self.assertTrue(ok)
            self.assertTrue(target_path.exists())
            self.assertTrue(is_valid_mrms_file(target_path))

    def test_download_mrms_script_helpers(self):
        import importlib.util
        ush_script = Path(__file__).resolve().parent.parent / "ush" / "download_mrms.py"
        spec = importlib.util.spec_from_file_location("download_mrms", ush_script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # parse_time_spec with range and step
        res_range = mod.parse_time_spec("2024050700-2024050703-1h")
        self.assertEqual(len(res_range), 4)
        self.assertEqual(res_range[0], datetime(2024, 5, 7, 0, 0))
        self.assertEqual(res_range[-1], datetime(2024, 5, 7, 3, 0))

        # parse_time_spec with list of timestamps
        res_list = mod.parse_time_spec("2024050700,2024050706")
        self.assertEqual(res_list, [datetime(2024, 5, 7, 0, 0), datetime(2024, 5, 7, 6, 0)])

        # parse_time_spec single 12-digit timestamp
        res_single = mod.parse_time_spec("202405070600")
        self.assertEqual(res_single, [datetime(2024, 5, 7, 6, 0)])

        # extract_obs_from_recipe
        recipe_path = Path(__file__).resolve().parent.parent / "recipes" / "graphics_slides_layout2_May2024.yaml"
        tasks = mod.extract_obs_from_recipe(recipe_path)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0]["valid_dt"], datetime(2024, 5, 7, 6, 0))
        self.assertEqual(tasks[1]["valid_dt"], datetime(2024, 5, 7, 1, 0))
        self.assertEqual(tasks[2]["valid_dt"], datetime(2024, 5, 7, 2, 0))


if __name__ == "__main__":
    unittest.main()
