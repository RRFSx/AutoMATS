import unittest
from pathlib import Path
import tempfile
import sys

# Ensure ush is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ush"))

from download_pgraf_graphics import (
    build_download_list,
    cleanup_empty_directories,
    extract_expname,
    parse_cycle_list,
    parse_fhr_config,
    parse_fhr_list,
    resolve_fhrs_for_cycle,
    stream_tar_pull,
)


class DownloadGraphicsTests(unittest.TestCase):
    def test_extract_expname(self):
        p1 = "/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4"
        self.assertEqual(extract_expname(p1), "RRFSv2X.retv4")

        p2 = "/scratch1/NCEPDEV/stmp2/First.Last/my_test_exp/com/model/v1.0"
        self.assertEqual(extract_expname(p2), "my_test_exp")

        p3 = "/some/path/without/exp_final"
        self.assertEqual(extract_expname(p3), "exp_final")

    def test_parse_fhr_list(self):
        # Range string 0-6
        self.assertEqual(parse_fhr_list("0-6"), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(parse_fhr_list(["0-6"]), [0, 1, 2, 3, 4, 5, 6])

        # Range with step 0-6-2
        self.assertEqual(parse_fhr_list("0-6-2"), [0, 2, 4, 6])

        # Explicit list
        self.assertEqual(parse_fhr_list([0, 3, 6]), [0, 3, 6])
        self.assertEqual(parse_fhr_list(["0", "3", "6"]), [0, 3, 6])

    def test_parse_cycle_list(self):
        # Range with step 24
        cycles_24 = parse_cycle_list("2024050600-2024050900-24")
        self.assertEqual(
            cycles_24,
            ["2024050600", "2024050700", "2024050800", "2024050900"],
        )

        # Range default 1h step
        cycles_1h = parse_cycle_list("2024050600-2024050603")
        self.assertEqual(
            cycles_1h,
            ["2024050600", "2024050601", "2024050602", "2024050603"],
        )

        # Explicit list
        cycles_exp = parse_cycle_list(["2024050600", "2024050712"])
        self.assertEqual(cycles_exp, ["2024050600", "2024050712"])

    def test_build_download_list(self):
        recipe = {
            "COMROOT": "gaeac6:/gpfs/f6/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4",
            "prefix": "rrfs",
            "WGF": ["det"],
            "cycle": ["2024050600-2024050601-1"],
            "fhr": ["0-1"],
            "tile": ["full"],
            "graphics name template": ["cref_{tile}_sfc_f{fhr}.png"],
        }
        out_base = Path("/local/figures")
        cluster, user, port, expname, tasks = build_download_list(recipe, out_base)

        self.assertEqual(cluster, "gaeac6")
        self.assertEqual(expname, "RRFSv2X.retv4")
        # 2 cycles (00, 01) * 1 WGF * 1 tile * 2 fhrs (0, 1) * 1 template = 4 tasks
        self.assertEqual(len(tasks), 4)

        # Check first task: cycle 2024050600, fhr 0 -> valid_time = 202405060000
        r0, l0 = tasks[0]
        expected_r0 = "/gpfs/f6/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4/rrfs.20240506/00/graphics/det/full/202405060000/cref_full_sfc_f000.png"
        expected_l0 = out_base / "RRFSv2X.retv4" / "det" / "full" / "2024050600" / "cref_full_sfc_f000.png"
        self.assertEqual(r0, expected_r0)
        self.assertEqual(l0, expected_l0)

    def test_parse_fhr_config_and_resolve(self):
        # 1. New dictionary mapping structure (list of dicts)
        raw_cfg = [
            {"default": [0, 1]},
            {"00,12": ["0-72"]},
        ]
        cfg = parse_fhr_config(raw_cfg)

        # Cycle at 10z -> uses default [0, 1]
        fhrs_10z = resolve_fhrs_for_cycle(cfg, "2024050610")
        self.assertEqual(fhrs_10z, [0, 1])

        # Cycle at 00z -> uses 0-72 (73 lead hours)
        fhrs_00z = resolve_fhrs_for_cycle(cfg, "2024050700")
        self.assertEqual(fhrs_00z, list(range(0, 73)))

        # Cycle at 12z -> uses 0-72
        fhrs_12z = resolve_fhrs_for_cycle(cfg, "2024050712")
        self.assertEqual(fhrs_12z, list(range(0, 73)))

        # 2. String fallback structure (from line-by-line fallback parser)
        fallback_cfg = [
            "default: [0, 1]",
            "'00,12': [0-72]",
        ]
        cfg2 = parse_fhr_config(fallback_cfg)
        self.assertEqual(resolve_fhrs_for_cycle(cfg2, "2024050610"), [0, 1])
        self.assertEqual(resolve_fhrs_for_cycle(cfg2, "2024050700"), list(range(0, 73)))

        # 3. Legacy flat format
        legacy_cfg = parse_fhr_config(["0-6"])
        self.assertEqual(resolve_fhrs_for_cycle(legacy_cfg, "2024050610"), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(resolve_fhrs_for_cycle(legacy_cfg, "2024050700"), [0, 1, 2, 3, 4, 5, 6])

    def test_build_download_list_with_cycle_dependent_fhr(self):
        recipe = {
            "COMROOT": "gaeac6:/gpfs/f6/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4",
            "prefix": "rrfs",
            "WGF": ["det"],
            "cycle": ["2024050610", "2024050700"],
            "fhr": [
                {"default": [0, 1]},
                {"00,12": ["0-2"]},
            ],
            "tile": ["full"],
            "graphics name template": ["cref_{tile}_sfc_f{fhr}.png"],
        }
        out_base = Path("/local/figures")
        cluster, user, port, expname, tasks = build_download_list(recipe, out_base)

        # 2024050610 has 2 fhrs (0, 1) -> 2 tasks
        # 2024050700 has 3 fhrs (0, 1, 2) -> 3 tasks
        # Total = 5 tasks
        self.assertEqual(len(tasks), 5)

        t_10z = [t for t in tasks if "2024050610" in str(t[1])]
        t_00z = [t for t in tasks if "2024050700" in str(t[1])]
        self.assertEqual(len(t_10z), 2)
        self.assertEqual(len(t_00z), 3)

        # Check valid_time in remote path (<PDY><cyc><fhr:02d>)
        # 2024050610 fhr 1 -> 202405061001
        self.assertIn("202405061001/cref_full_sfc_f001.png", t_10z[1][0])
        # 2024050700 fhr 2 -> 202405070002
        self.assertIn("202405070002/cref_full_sfc_f002.png", t_00z[2][0])

    def test_cleanup_empty_directories_all_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exp_dir = Path(tmpdir) / "my_exp"
            nested = exp_dir / "enkf" / "full" / "2024050600"
            nested.mkdir(parents=True, exist_ok=True)
            self.assertTrue(nested.exists())

            removed = cleanup_empty_directories(exp_dir)
            self.assertGreater(removed, 0)
            self.assertFalse(exp_dir.exists())

    def test_cleanup_empty_directories_partial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exp_dir = Path(tmpdir) / "my_exp"
            has_file = exp_dir / "det" / "full" / "2024050600"
            has_file.mkdir(parents=True, exist_ok=True)
            (has_file / "figure.png").write_text("dummy")

            empty_branch = exp_dir / "enkf" / "full" / "2024050600"
            empty_branch.mkdir(parents=True, exist_ok=True)

            removed = cleanup_empty_directories(exp_dir)
            self.assertGreater(removed, 0)
            # Empty branch removed
            self.assertFalse((exp_dir / "enkf").exists())
            # Branch with file preserved
            self.assertTrue((has_file / "figure.png").exists())

    def test_cleanup_empty_directories_ignores_ds_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            exp_dir = Path(tmpdir) / "my_exp"
            sub = exp_dir / "empty_dir"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / ".DS_Store").write_text("dummy")

            removed = cleanup_empty_directories(exp_dir)
            self.assertGreater(removed, 0)
            self.assertFalse(exp_dir.exists())

    def test_stream_tar_pull_success_and_missing(self):
        from unittest.mock import patch, MagicMock
        import io
        import tarfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_file1 = tmp / "output" / "f1.png"
            out_file2 = tmp / "output" / "f2.png"
            out_missing = tmp / "output" / "missing.png"

            tasks = [
                ("/gpfs/path/f1.png", out_file1),
                ("/gpfs/path/f2.png", out_file2),
                ("/gpfs/path/missing.png", out_missing),
            ]

            # Generate a mock tar stream containing only f1.png and f2.png
            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as tw:
                data1 = b"image_data_1"
                t1 = tarfile.TarInfo(name="/gpfs/path/f1.png")
                t1.size = len(data1)
                tw.addfile(t1, io.BytesIO(data1))

                data2 = b"image_data_2"
                t2 = tarfile.TarInfo(name="/gpfs/path/f2.png")
                t2.size = len(data2)
                tw.addfile(t2, io.BytesIO(data2))

            tar_buf.seek(0)

            mock_proc = MagicMock()
            mock_proc.stdin = io.BytesIO()
            mock_proc.stdout = tar_buf
            mock_proc.stderr = io.BytesIO(b"tar: /gpfs/path/missing.png: Cannot stat: No such file or directory\n")
            mock_proc.returncode = 1
            mock_proc.wait = MagicMock(return_value=1)

            with patch("subprocess.Popen", return_value=mock_proc):
                completed, missing, errors = stream_tar_pull("testuser", 40896, tasks, cluster="gaeac6", debug=True)

            self.assertEqual(completed, 2)
            self.assertEqual(missing, 1)
            self.assertEqual(errors, 0)
            self.assertTrue(out_file1.exists())
            self.assertEqual(out_file1.read_bytes(), b"image_data_1")
            self.assertTrue(out_file2.exists())
            self.assertEqual(out_file2.read_bytes(), b"image_data_2")
            self.assertFalse(out_missing.exists())

    def test_stream_tar_pull_empty_tasks(self):
        completed, missing, errors = stream_tar_pull("user", 1234, [])
        self.assertEqual((completed, missing, errors), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
