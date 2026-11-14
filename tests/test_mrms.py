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
    MRMS_TILES,
    build_mrms_url,
    fetch_mrms_images,
    fetch_single_mrms,
    format_mrms_filename,
    is_valid_mrms_file,
    parse_mrms_filename,
    parse_mrms_filename_details,
)
from automats.graphics import resolve_obs_cell_time, resolve_obs_tile


class TestMRMS(unittest.TestCase):
    def test_format_mrms_filename(self):
        dt = datetime(2024, 5, 7, 6, 0)
        self.assertEqual(format_mrms_filename("cref", dt), "mrms_cref_full_202405070600.png")
        self.assertEqual(format_mrms_filename("CREF", dt), "mrms_cref_full_202405070600.png")
        self.assertEqual(format_mrms_filename("cref", dt, tile="full"), "mrms_cref_full_202405070600.png")
        self.assertEqual(format_mrms_filename("cref", dt, tile="conus"), "mrms_cref_conus_202405070600.png")
        self.assertEqual(format_mrms_filename("cref", dt, tile="NC"), "mrms_cref_NC_202405070600.png")
        self.assertEqual(format_mrms_filename("cref", dt, tile="EastCO"), "mrms_cref_EastCO_202405070600.png")
        self.assertEqual(format_mrms_filename("echo_top_18", dt, tile="SW"), "mrms_echo_top_18_SW_202405070600.png")

    def test_parse_mrms_filename(self):
        parsed = parse_mrms_filename("mrms_cref_full_202405070600.png")
        self.assertIsNotNone(parsed)
        var, tile, dt = parsed
        self.assertEqual(var, "cref")
        self.assertEqual(tile, "full")
        self.assertEqual(dt, datetime(2024, 5, 7, 6, 0))

        # Test regional tile
        parsed_nc = parse_mrms_filename("mrms_cref_NC_202405070600.png")
        self.assertIsNotNone(parsed_nc)
        self.assertEqual(parsed_nc, ("cref", "NC", datetime(2024, 5, 7, 6, 0)))

        # Test EastCO tile
        parsed_eastco = parse_mrms_filename("mrms_cref_EastCO_202405070600.png")
        self.assertEqual(parsed_eastco, ("cref", "EastCO", datetime(2024, 5, 7, 6, 0)))

        # Test 10-digit format
        parsed10 = parse_mrms_filename("mrms_cref_full_2024050706.png")
        self.assertIsNotNone(parsed10)
        self.assertEqual(parsed10, ("cref", "full", datetime(2024, 5, 7, 6, 0)))

        # Invalid format (without tile is now invalid)
        self.assertIsNone(parse_mrms_filename("random_image.png"))
        self.assertIsNone(parse_mrms_filename("mrms_cref_202405070600.png"))

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
        self.assertIn("zoom=4", url)
        self.assertIn("clon=-98", url)
        self.assertIn("clat=38", url)

        # Test regional tile URLs
        url_nc = build_mrms_url(dt, var="cref", tile="NC")
        self.assertIn("zoom=6", url_nc)
        self.assertIn("clon=-97.0", url_nc)
        self.assertIn("clat=43.5", url_nc)

        url_eastco = build_mrms_url(dt, var="cref", tile="EastCO")
        self.assertIn("zoom=8", url_eastco)
        self.assertIn("clon=-104.9", url_eastco)
        self.assertIn("clat=39.0", url_eastco)

    def test_resolve_obs_tile(self):
        # 1. Inherited from companion graphic in same row
        tile1 = resolve_obs_tile(
            spec={"type": "same cycle", "graphics name": "cref_full_sfc_f006.png"},
            row_items=["cref_NC_sfc_f001.pg", "obs"],
            item_str="obs",
        )
        self.assertEqual(tile1, "NC")

        # 2. Inherited from spec graphics name in same valid time
        tile2 = resolve_obs_tile(
            spec={"type": "same valid time", "graphics name": "cref_EastCO_sfc_f006.png"},
            row_items=["first", "second", "obs"],
            item_str="obs",
        )
        self.assertEqual(tile2, "EastCO")

        # 3. Explicit spec tile override
        tile3 = resolve_obs_tile(
            spec={"type": "same valid time", "tile": "SW", "graphics name": "cref_full_sfc_f006.png"},
            row_items=["first", "obs"],
            item_str="obs",
        )
        self.assertEqual(tile3, "SW")

        # 4. Explicit obs item tag
        tile4 = resolve_obs_tile(
            spec={"type": "same cycle"},
            row_items=["cref_full_sfc_f001.png", "obs:SC"],
            item_str="obs:SC",
        )
        self.assertEqual(tile4, "SC")

        # 5. Default fallback
        tile5 = resolve_obs_tile(spec={}, row_items=["obs"], item_str="obs")
        self.assertEqual(tile5, "full")

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
            target_path = Path(td) / "mrms_cref_full_202405070600.png"
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
        recipe_path = Path(__file__).resolve().parent.parent / "recipes" / "pygraf_slides_2exps_May2024.yaml"
        tasks = mod.extract_obs_from_recipe(recipe_path)
        self.assertEqual(len(tasks), 10)
        self.assertEqual(tasks[0]["valid_dt"], datetime(2024, 5, 8, 0, 0))
        self.assertEqual(tasks[0]["tile"], "NE")
        self.assertEqual(tasks[2]["valid_dt"], datetime(2024, 5, 8, 22, 0))
        self.assertEqual(tasks[2]["tile"], "SE")
        self.assertEqual(tasks[6]["valid_dt"], datetime(2024, 5, 7, 1, 0))
        self.assertEqual(tasks[6]["tile"], "full")
        self.assertEqual(tasks[8]["valid_dt"], datetime(2024, 5, 10, 11, 0))
        self.assertEqual(tasks[8]["tile"], "full")
        self.assertEqual(tasks[9]["valid_dt"], datetime(2024, 5, 10, 11, 0))
        self.assertEqual(tasks[9]["tile"], "full")


if __name__ == "__main__":
    unittest.main()
