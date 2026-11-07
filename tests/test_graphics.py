import unittest
from pathlib import Path
import tempfile

from automats.config import Experiment, build_plan
from automats.graphics import (
    ORDINALS,
    build_remote_path,
    compute_valid_time,
    parse_graphics_name,
    resolve_experiment,
)


class GraphicsTests(unittest.TestCase):
    def test_experiment_comroot_parsing(self):
        exp1 = Experiment(
            name="RRFSv2_retv4",
            color="red",
            COMROOT="gaeac6:/gpfs/f6/world-shared/com/rrfs/v2.1.4",
        )
        self.assertEqual(exp1.hpc_cluster, "gaeac6")
        self.assertEqual(exp1.remote_comroot, "/gpfs/f6/world-shared/com/rrfs/v2.1.4")

        exp2 = Experiment(name="HRRR_OPS", color="orange")
        self.assertIsNone(exp2.hpc_cluster)
        self.assertIsNone(exp2.remote_comroot)

    def test_recipe_with_comroot_and_graphics_slides(self):
        raw = {
            "common": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-15T00:00:00Z"},
            "experiments": [
                {
                    "name": "RRFSv2X",
                    "color": "red",
                    "COMROOT": "gaeac6:/gpfs/f6/com/rrfs/v2.1.4",
                },
                {"name": "HRRR_OPS", "color": "orange"},
            ],
            "plots": [
                {
                    "name": "sfc_tseries",
                    "application": "surface",
                    "plot_type": "time series",
                    "variable": ["t2m"],
                    "statistic": ["rmse"],
                }
            ],
            "pygraf graphics slides": {
                "enable": True,
                "recipe": "graphics_May2024_layout2.yaml",
            },
        }
        plan = build_plan(raw)
        self.assertEqual(len(plan.experiments), 2)
        self.assertEqual(plan.experiments[0].hpc_cluster, "gaeac6")
        self.assertIsNotNone(plan.pygraf_graphics)
        self.assertTrue(plan.pygraf_graphics.enable)
        self.assertEqual(plan.pygraf_graphics.recipe, "graphics_May2024_layout2.yaml")

    def test_ordinals_and_experiment_resolution(self):
        exps = (
            Experiment(name="exp_one", color="red"),
            Experiment(name="exp_two", color="blue"),
            Experiment(name="exp_three", color="green"),
        )
        self.assertEqual(resolve_experiment("first", exps), exps[0])
        self.assertEqual(resolve_experiment("second", exps), exps[1])
        self.assertEqual(resolve_experiment("third", exps), exps[2])
        self.assertIsNone(resolve_experiment("fourth", exps))
        self.assertEqual(resolve_experiment("exp_one", exps), exps[0])

    def test_parse_graphics_name(self):
        # Standard .png
        name, tile, fhr = parse_graphics_name("cref_full_sfc_f006.png")
        self.assertEqual(name, "cref_full_sfc_f006.png")
        self.assertEqual(tile, "full")
        self.assertEqual(fhr, 6)

        # Typo .pg normalized to .png
        name, tile, fhr = parse_graphics_name("cref_full_sfc_f001.pg")
        self.assertEqual(name, "cref_full_sfc_f001.png")
        self.assertEqual(tile, "full")
        self.assertEqual(fhr, 1)

        # Different tile and lead
        name, tile, fhr = parse_graphics_name("cloudcover_conus_total_f012.png")
        self.assertEqual(tile, "conus")
        self.assertEqual(fhr, 12)

    def test_valid_time_dir_formatting(self):
        # YYYYMMDDxxxx where xxxx is 4-digit forecast lead hour
        pdy, cyc, valid_dir = compute_valid_time("2024051300", 6)
        self.assertEqual(pdy, "20240513")
        self.assertEqual(cyc, "00")
        self.assertEqual(valid_dir, "202405130006")

        pdy, cyc, valid_dir = compute_valid_time("2024051300", 0)
        self.assertEqual(valid_dir, "202405130000")

        pdy, cyc, valid_dir = compute_valid_time("2024050712", 24)
        self.assertEqual(pdy, "20240507")
        self.assertEqual(cyc, "12")
        self.assertEqual(valid_dir, "202405070024")

    def test_build_remote_path(self):
        remote = build_remote_path(
            comroot="/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4",
            prefix="rrfs",
            cycle="2024051300",
            wgf="det",
            tile="full",
            fhr=6,
            graphics_name="cref_full_sfc_f006.png",
        )
        expected = (
            "/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4"
            "/rrfs.20240513/00/graphics/det/full/202405130006/cref_full_sfc_f006.png"
        )
        self.assertEqual(remote, expected)

    def test_batch_pull_graphics_files_parallel(self):
        from unittest.mock import patch
        from automats.graphics import batch_pull_graphics_files

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cached_file = tmp / "cached.png"
            cached_file.write_bytes(b"dummy")

            new_file1 = tmp / "new1.png"
            new_file2 = tmp / "new2.png"

            tasks = [
                {"username": "user", "port": 40896, "remote_path": "/r/1", "local_path": cached_file, "cluster": "gaeac6"},
                {"username": "user", "port": 40896, "remote_path": "/r/2", "local_path": new_file1, "cluster": "gaeac6"},
                {"username": "user", "port": 40896, "remote_path": "/r/3", "local_path": new_file2, "cluster": "gaeac6"},
            ]

            def fake_scp(user, port, rpath, lpath):
                lpath.write_bytes(b"downloaded")
                return True

            with patch("automats.graphics.scp_pull_file", side_effect=fake_scp) as mock_scp:
                results = batch_pull_graphics_files(tasks, max_workers=2)
                # cached_file was already present, so scp should only be called for 2 files
                self.assertEqual(mock_scp.call_count, 2)
                self.assertTrue(results[str(cached_file)])
                self.assertTrue(results[str(new_file1)])
                self.assertTrue(results[str(new_file2)])


if __name__ == "__main__":
    unittest.main()
