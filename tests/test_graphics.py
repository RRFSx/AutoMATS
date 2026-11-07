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
        # <PDY><cyc><fhr:02d>
        pdy, cyc, valid_dir = compute_valid_time("2024051300", 6)
        self.assertEqual(pdy, "20240513")
        self.assertEqual(cyc, "00")
        self.assertEqual(valid_dir, "202405130006")

        pdy, cyc, valid_dir = compute_valid_time("2024051300", 0)
        self.assertEqual(valid_dir, "202405130000")

        # Example from user: 2024050612 and fhr 2 -> 202405061202
        pdy, cyc, valid_dir = compute_valid_time("2024050612", 2)
        self.assertEqual(pdy, "20240506")
        self.assertEqual(cyc, "12")
        self.assertEqual(valid_dir, "202405061202")

        pdy, cyc, valid_dir = compute_valid_time("2024050712", 24)
        self.assertEqual(pdy, "20240507")
        self.assertEqual(cyc, "12")
        self.assertEqual(valid_dir, "202405071224")

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

    def test_detect_prefix_from_path(self):
        from automats.graphics import detect_prefix_from_path
        self.assertEqual(detect_prefix_from_path("/gpfs/f6/com/rrfs/v2.1.4"), "rrfs")
        self.assertEqual(detect_prefix_from_path("/gpfs/f6/world-shared/com/hrrr/v4.1/"), "hrrr")
        self.assertEqual(detect_prefix_from_path("/scratch1/com/rap/v5.0"), "rap")
        self.assertEqual(detect_prefix_from_path("/other/path/without_com"), "")

    def test_build_remote_path_auto_prefix(self):
        remote = build_remote_path(
            comroot="/gpfs/f6/world-shared/com/hrrr/v4.1",
            prefix="",  # empty -> auto-detects 'hrrr'
            cycle="2024051300",
            wgf="det",
            tile="full",
            fhr=6,
            graphics_name="cref_full_sfc_f006.png",
        )
        self.assertIn("/com/hrrr/v4.1/hrrr.20240513/00/graphics/det/full/202405130006/cref_full_sfc_f006.png", remote)

    def test_batch_pull_graphics_files_tar(self):
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

            def fake_stream_tar(username, port, tasks, cluster="HPC", debug=False):
                for rpath, lpath in tasks:
                    lpath.write_bytes(b"downloaded")
                return len(tasks), 0, 0

            with patch("automats.graphics.stream_tar_pull", side_effect=fake_stream_tar) as mock_tar:
                results = batch_pull_graphics_files(tasks)
                # cached_file was already present, so stream_tar_pull should only be called once for the 2 pending files
                self.assertEqual(mock_tar.call_count, 1)
                self.assertTrue(results[str(cached_file)])
                self.assertTrue(results[str(new_file1)])
                self.assertTrue(results[str(new_file2)])

    def test_stream_tar_pull_extracts_files(self):
        import io
        import tarfile
        from unittest.mock import MagicMock, patch
        from automats.graphics import stream_tar_pull

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_file1 = tmp / "out1.png"
            out_file2 = tmp / "out2.png"

            # Create an in-memory tar archive with two files
            tar_bytes = io.BytesIO()
            with tarfile.open(fileobj=tar_bytes, mode="w") as tf:
                data1 = b"PNG_IMAGE_1"
                ti1 = tarfile.TarInfo(name="gpfs/path/fig1.png")
                ti1.size = len(data1)
                tf.addfile(ti1, io.BytesIO(data1))

                data2 = b"PNG_IMAGE_2"
                ti2 = tarfile.TarInfo(name="gpfs/path/fig2.png")
                ti2.size = len(data2)
                tf.addfile(ti2, io.BytesIO(data2))

            tar_bytes.seek(0)

            mock_proc = MagicMock()
            mock_proc.stdin = io.BytesIO()
            mock_proc.stdout = tar_bytes
            mock_proc.stderr = io.BytesIO()
            mock_proc.wait.return_value = 0

            tasks = [
                ("/gpfs/path/fig1.png", out_file1),
                ("/gpfs/path/fig2.png", out_file2),
                ("/gpfs/path/missing.png", tmp / "missing.png"),
            ]

            with patch("subprocess.Popen", return_value=mock_proc):
                completed, missing, errors = stream_tar_pull(
                    username="user",
                    port=40896,
                    tasks=tasks,
                    cluster="gaeac6",
                    debug=True,
                )

            self.assertEqual(completed, 2)
            self.assertEqual(missing, 1)
            self.assertEqual(errors, 0)
            self.assertTrue(out_file1.exists())
            self.assertEqual(out_file1.read_bytes(), b"PNG_IMAGE_1")
            self.assertTrue(out_file2.exists())
            self.assertEqual(out_file2.read_bytes(), b"PNG_IMAGE_2")
            self.assertFalse((tmp / "missing.png").exists())


if __name__ == "__main__":
    unittest.main()
