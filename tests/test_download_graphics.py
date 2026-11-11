import unittest
from pathlib import Path
import tempfile
import sys

# Ensure ush is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ush"))

from download_pygraf_graphics import (
    build_download_list,
    cleanup_empty_directories,
    detect_prefix,
    detect_prefix_from_path,
    extract_expname,
    parse_cycle_list,
    parse_fhr_config,
    parse_fhr_list,
    parse_simple_yaml,
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
        cluster, user, expname, prefix, tasks = build_download_list(recipe, out_base)

        self.assertEqual(cluster, "gaeac6")
        self.assertEqual(expname, "RRFSv2X.retv4")
        self.assertEqual(prefix, "rrfs")
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
        cluster, user, expname, prefix, tasks = build_download_list(recipe, out_base)

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
                completed, missing, errors = stream_tar_pull("testuser", tasks, cluster="gaeac6", debug=True)

            self.assertEqual(completed, 2)
            self.assertEqual(missing, 1)
            self.assertEqual(errors, 0)
            self.assertTrue(out_file1.exists())
            self.assertEqual(out_file1.read_bytes(), b"image_data_1")
            self.assertTrue(out_file2.exists())
            self.assertEqual(out_file2.read_bytes(), b"image_data_2")
            self.assertFalse(out_missing.exists())

    def test_stream_tar_pull_empty_tasks(self):
        completed, missing, errors = stream_tar_pull("user", [])
        self.assertEqual((completed, missing, errors), (0, 0, 0))

    def test_stream_tar_pull_shared_remote_path_multiple_destinations(self):
        from unittest.mock import patch, MagicMock
        import io
        import tarfile

        class CapturingBytesIO(io.BytesIO):
            def __init__(self):
                super().__init__()
                self.captured = b""

            def close(self):
                if not self.closed:
                    self.captured = self.getvalue()
                super().close()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out1 = tmp / "exp1" / "shared.png"
            out2 = tmp / "exp2" / "shared.png"
            miss1 = tmp / "exp1" / "missing.png"
            miss2 = tmp / "exp2" / "missing.png"

            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as tw:
                data = b"SHARED_DATA"
                ti = tarfile.TarInfo(name="/gpfs/path/shared.png")
                ti.size = len(data)
                tw.addfile(ti, io.BytesIO(data))
            tar_buf.seek(0)

            stdin_cap = CapturingBytesIO()
            mock_proc = MagicMock()
            mock_proc.stdin = stdin_cap
            mock_proc.stdout = tar_buf
            mock_proc.stderr = io.BytesIO()
            mock_proc.wait.return_value = 0

            tasks = [
                ("/gpfs/path/shared.png", out1),
                ("/gpfs/path/shared.png", out2),
                ("/gpfs/path/missing.png", miss1),
                ("/gpfs/path/missing.png", miss2),
            ]

            with patch("subprocess.Popen", return_value=mock_proc):
                completed, missing, errors = stream_tar_pull("testuser", tasks, cluster="gaeac6", debug=True)

            self.assertEqual((completed, missing, errors), (2, 2, 0))
            self.assertEqual(stdin_cap.captured, b"/gpfs/path/shared.png\x00/gpfs/path/missing.png\x00")
            self.assertEqual(out1.read_bytes(), b"SHARED_DATA")
            self.assertEqual(out2.read_bytes(), b"SHARED_DATA")
            self.assertFalse(miss1.exists())
            self.assertFalse(miss2.exists())

    def test_parse_simple_yaml_nested(self):
        raw = """
COMROOT: gaeac6:/gpfs/f6/model/com/rrfs/v1
prefix: rrfs
WGF: [det]
cycle: [2024050600-2024050602]
fhr:
- default: [0, 1]
- '00,12': [0-6]
tile: [full]
graphics list:
- cref_{tile}_sfc_f{fhr}.png
- temp_{tile}_2m_f{fhr}.png:
    cycle: [2024050612]
    fhr: [0-3]
"""
        d = parse_simple_yaml(raw)
        self.assertEqual(d["COMROOT"], "gaeac6:/gpfs/f6/model/com/rrfs/v1")
        self.assertEqual(d["prefix"], "rrfs")
        self.assertEqual(d["WGF"], ["det"])
        self.assertEqual(d["tile"], ["full"])
        self.assertEqual(d["cycle"], ["2024050600-2024050602"])
        self.assertEqual(len(d["graphics list"]), 2)
        self.assertEqual(d["graphics list"][0], "cref_{tile}_sfc_f{fhr}.png")
        self.assertIn("temp_{tile}_2m_f{fhr}.png", d["graphics list"][1])
        self.assertEqual(d["graphics list"][1]["temp_{tile}_2m_f{fhr}.png"]["cycle"], ["2024050612"])

    def test_build_download_list_with_per_graphic_overrides(self):
        recipe = {
            "COMROOT": "gaeac6:/gpfs/f6/OPSROOT/exp/com/rrfs/v1",
            "prefix": "rrfs",
            "WGF": ["det"],
            "tile": ["full"],
            "cycle": ["2024050600"],
            "fhr": [0, 1],
            "graphics list": [
                "cref_{tile}_sfc_f{fhr}.png",
                {
                    "temp_{tile}_2m_f{fhr}.png": {
                        "cycle": ["2024050612"],
                        "fhr": [0, 1, 2],
                    },
                },
            ],
        }
        out_base = Path("/local/figures")
        cluster, user, expname, prefix, tasks = build_download_list(recipe, out_base)

        # First graphic uses default cycle 2024050600 with fhr 0, 1 -> 2 tasks
        cref_tasks = [t for t in tasks if "cref_" in str(t[1])]
        self.assertEqual(len(cref_tasks), 2)
        self.assertIn("2024050600", str(cref_tasks[0][1]))
        self.assertIn("2024050600", str(cref_tasks[1][1]))

        # Second graphic uses custom cycle 2024050612 with fhr 0, 1, 2 -> 3 tasks
        temp_tasks = [t for t in tasks if "temp_" in str(t[1])]
        self.assertEqual(len(temp_tasks), 3)
        self.assertIn("2024050612", str(temp_tasks[0][1]))
        self.assertIn("2024050612", str(temp_tasks[1][1]))
        self.assertIn("2024050612", str(temp_tasks[2][1]))

    def test_detect_prefix_from_path(self):
        self.assertEqual(
            detect_prefix_from_path("gaeac6:/gpfs/f6/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4"),
            "rrfs",
        )
        self.assertEqual(
            detect_prefix_from_path("/gpfs/hps/nco/ops/com/hrrr/v4.1"),
            "hrrr",
        )
        self.assertEqual(
            detect_prefix_from_path("/scratch1/NCEPDEV/stmp2/user/com/rap/v5.0"),
            "rap",
        )
        self.assertEqual(
            detect_prefix_from_path("/path/to/custom/archive"),
            "",
        )

    def test_build_download_list_auto_detects_prefix(self):
        recipe_no_prefix = {
            "COMROOT": "gaeac6:/gpfs/f6/OPSROOT/RRFSv2X.retv4/com/hrrr/v4.1",
            # 'prefix' is omitted intentionally
            "WGF": ["det"],
            "tile": ["full"],
            "cycle": ["2024050600"],
            "fhr": [0],
            "graphics list": ["cref_{tile}_sfc_f{fhr}.png"],
        }
        out_base = Path("/local/figures")
        cluster, user, expname, prefix, tasks = build_download_list(recipe_no_prefix, out_base)
        self.assertEqual(prefix, "hrrr")
        # Check task remote path contains auto-detected prefix 'hrrr.20240506'
        self.assertIn("hrrr.20240506", tasks[0][0])

    def test_build_download_list_with_comroot_list(self):
        raw_yaml = """
COMROOT:
- gaeac6:/gpfs/f6/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4
- ursa:/scratch3/BMC/OPSROOT/RRFSv2X.retv4p1/com/rrfs/v2.1.4
WGF: [det]
tile: [full]
cycle: [2024050600]
fhr: [0, 1]
graphics list:
- cref_{tile}_sfc_f{fhr}.png
"""
        recipe = parse_simple_yaml(raw_yaml)
        out_base = Path("/local/figures")
        cluster, user, expname, prefix, tasks = build_download_list(recipe, out_base)
        self.assertEqual(cluster, "gaeac6, ursa")
        self.assertEqual(expname, "RRFSv2X.retv4, RRFSv2X.retv4p1")
        self.assertEqual(prefix, "rrfs")
        self.assertEqual(len(tasks), 4)
        self.assertEqual(
            tasks[0][0],
            "/gpfs/f6/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4/rrfs.20240506/00/graphics/det/full/202405060000/cref_full_sfc_f000.png",
        )
        self.assertEqual(
            tasks[0][1],
            out_base / "RRFSv2X.retv4" / "det" / "full" / "2024050600" / "cref_full_sfc_f000.png",
        )
        self.assertEqual(
            tasks[2][0],
            "/scratch3/BMC/OPSROOT/RRFSv2X.retv4p1/com/rrfs/v2.1.4/rrfs.20240506/00/graphics/det/full/202405060000/cref_full_sfc_f000.png",
        )
        self.assertEqual(
            tasks[2][1],
            out_base / "RRFSv2X.retv4p1" / "det" / "full" / "2024050600" / "cref_full_sfc_f000.png",
        )

    def test_main_standalone_ignores_sibling_baseline_and_downloads_comroot_list(self):
        from unittest.mock import patch
        import download_pygraf_graphics as dl

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            dl_yaml = tmp / "download_pygraf_graphics.yaml"
            dl_yaml.write_text(
                "COMROOT:\n"
                "- gaeac6:/gpfs/f6/OPSROOT/EXP_ONE/com/rrfs/v2.1.4\n"
                "- gaeac6:/gpfs/f6/OPSROOT/EXP_TWO/com/rrfs/v2.1.4\n"
                "WGF: [det]\n"
                "cycle: [2024050600]\n"
                "fhr: [0]\n"
                "tile: [full]\n"
                "graphics list:\n"
                "- cref_{tile}_sfc_f{fhr}.png\n",
                encoding="utf-8",
            )
            # Sibling baseline.yaml should have no effect on standalone execution
            base_yaml = tmp / "baseline.yaml"
            base_yaml.write_text(
                "experiments:\n"
                "- name: other\n"
                "  color: red\n"
                "  COMROOT: gaeac6:/gpfs/f6/OPSROOT/OTHER_EXP/com/rrfs/v2.1.4\n",
                encoding="utf-8",
            )

            out_dir = tmp / "graphics"
            pulled_tasks = []

            def fake_pull(username, tasks, cluster="HPC", debug=False):
                pulled_tasks.extend(tasks)
                for _, lpath in tasks:
                    lpath.parent.mkdir(parents=True, exist_ok=True)
                    lpath.write_bytes(b"PNGDATA")
                return len(tasks), 0, 0

            with (
                patch.object(dl, "test_hpc_session_ready", return_value=True),
                patch.object(dl, "stream_tar_pull", side_effect=fake_pull),
            ):
                rc = dl.main([str(dl_yaml), "-o", str(out_dir)])

            self.assertEqual(rc, 0)
            self.assertEqual(len(pulled_tasks), 2)
            self.assertIn("/EXP_ONE/", pulled_tasks[0][0])
            self.assertIn("/EXP_TWO/", pulled_tasks[1][0])
            self.assertFalse((out_dir / "OTHER_EXP").exists())
            self.assertTrue((out_dir / "EXP_ONE" / "det" / "full" / "2024050600" / "cref_full_sfc_f000.png").exists())
            self.assertTrue((out_dir / "EXP_TWO" / "det" / "full" / "2024050600" / "cref_full_sfc_f000.png").exists())

    def test_load_hpc_users_populates_download_targets(self):
        from unittest.mock import patch
        import download_pygraf_graphics as dl

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cfg = tmp / ".hpc_config"
            cfg.write_text(
                "# RDHPCS usernames by cluster\n"
                "gaeac6: User.Gaea\n"
                "hera: User.Hera\n",
                encoding="utf-8",
            )
            recipe = {
                "COMROOT": [
                    "gaeac6:/gpfs/f6/OPSROOT/EXP1/com/rrfs/v2.1.4",
                    "hera:/scratch1/OPSROOT/EXP2/com/rrfs/v2.1.4",
                ],
                "WGF": ["det"],
                "tile": ["full"],
                "cycle": ["2024050600"],
                "fhr": [0],
                "graphics list": ["cref_{tile}_sfc_f{fhr}.png"],
            }
            with patch.object(dl, "find_hpc_config_file", return_value=cfg):
                targets = dl.build_download_targets(recipe, tmp / "graphics")
                _, users_str, _, _, _ = dl.build_download_list(recipe, tmp / "graphics")

            self.assertEqual(targets[0]["username"], "User.Gaea")
            self.assertEqual(targets[1]["username"], "User.Hera")
            self.assertEqual(users_str, "User.Gaea, User.Hera")

    def test_hpc_session_ready_requires_ping_response(self):
        import json
        import select
        import socket
        import threading
        import uuid
        import download_pygraf_graphics as dl

        unique_user = f"testping_{uuid.uuid4().hex[:8]}"
        sock_path = f"/tmp/hpc_term_{unique_user}_gaeac7.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(4)
        stop_evt = threading.Event()
        reply_mode = {"respond": False}
        conns = []

        def _serve():
            while not stop_evt.is_set():
                r, _, _ = select.select([srv], [], [], 0.05)
                if srv in r:
                    conn, _ = srv.accept()
                    if reply_mode["respond"]:
                        data = conn.recv(4096)
                        req = json.loads(data.decode("utf-8").strip())
                        self.assertEqual(req.get("cmd"), "ping")
                        self.assertTrue(req.get("probe_remote"))
                        conn.sendall(b'{"code": 0, "stdout_b64": ""}\n')
                        conn.close()
                    else:
                        conns.append(conn)

        from unittest.mock import patch
        th = threading.Thread(target=_serve, daemon=True)
        th.start()
        try:
            with patch("glob.glob", return_value=[sock_path]):
                # Accepting socket that hangs without replying must be rejected
                self.assertFalse(dl.test_hpc_session_ready("gaeac7", username=unique_user))
                # Switch to healthy ping responder -> accepted
                reply_mode["respond"] = True
                self.assertTrue(dl.test_hpc_session_ready("gaeac7", username=unique_user))
        finally:
            stop_evt.set()
            th.join(timeout=1.0)
            for c in conns:
                try:
                    c.close()
                except OSError:
                    pass
            srv.close()
            if Path(sock_path).exists():
                Path(sock_path).unlink()

    def test_stream_tar_pull_counts_helper_failure_and_terminates_on_interrupt(self):
        from contextlib import redirect_stderr
        import io
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            tasks = [("/gpfs/path/f1.png", tmp / "f1.png")]

            mock_proc = MagicMock()
            mock_proc.stdin = io.BytesIO()
            mock_proc.stdout = io.BytesIO(b"")
            mock_proc.stderr = io.BytesIO(b"[!] No active HPC session for 'gaeac7'.\n")
            mock_proc.wait.return_value = 1

            err_buf = io.StringIO()
            with patch("subprocess.Popen", return_value=mock_proc), redirect_stderr(err_buf):
                completed, missing, errors = stream_tar_pull("user", tasks, cluster="gaeac7", debug=False)

            self.assertEqual((completed, missing, errors), (0, 1, 1))
            self.assertIn("No active HPC session", err_buf.getvalue())

            mock_proc2 = MagicMock()
            mock_proc2.stdin = io.BytesIO()
            mock_proc2.stdout = io.BytesIO(b"")
            mock_proc2.stderr = io.BytesIO()
            with patch("subprocess.Popen", return_value=mock_proc2), patch("tarfile.open", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    stream_tar_pull("user", tasks, cluster="gaeac7")
            self.assertTrue(mock_proc2.terminate.called)

    def test_main_auto_recovers_wedged_worker_when_master_alive(self):
        from unittest.mock import patch
        import download_pygraf_graphics as dl

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            dl_yaml = tmp / "download_pygraf_graphics.yaml"
            dl_yaml.write_text(
                "COMROOT: gaeac7:/gpfs/f7/OPSROOT/EXP_RECOV/com/rrfs/v2.1.4\n"
                "WGF: [det]\n"
                "cycle: [2024050600]\n"
                "fhr: [0]\n"
                "graphics list:\n"
                "- cref_{tile}_sfc_f{fhr}.png\n",
                encoding="utf-8",
            )
            out_dir = tmp / "graphics"

            def fake_pull(username, tasks, cluster="HPC", debug=False):
                for _, lpath in tasks:
                    lpath.parent.mkdir(parents=True, exist_ok=True)
                    lpath.write_bytes(b"PNG_RECOVERED")
                return len(tasks), 0, 0

            with (
                patch.object(dl, "test_hpc_session_ready", return_value=False),
                patch.object(dl, "_ensure_hpc_worker_if_master_alive", return_value=True) as mock_recover,
                patch.object(dl, "stream_tar_pull", side_effect=fake_pull) as mock_pull,
            ):
                rc = dl.main([str(dl_yaml), "-o", str(out_dir)])

            self.assertEqual(rc, 0)
            self.assertEqual(mock_recover.call_count, 1)
            self.assertEqual(mock_pull.call_count, 1)
            self.assertTrue(
                (out_dir / "EXP_RECOV" / "det" / "full" / "2024050600" / "cref_full_sfc_f000.png").exists()
            )

    def test_ensure_hpc_worker_if_master_alive_reports_stale_and_handles_timeout(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        import subprocess
        from unittest.mock import MagicMock, patch
        import download_pygraf_graphics as dl

        mock_proc_ok = MagicMock()
        mock_proc_ok.communicate.return_value = ("", "")
        mock_proc_ok.returncode = 0
        out_ok = io.StringIO()
        with (
            patch.object(dl, "test_hpc_session_ready", return_value=False),
            patch("subprocess.Popen", return_value=mock_proc_ok),
            redirect_stdout(out_ok),
        ):
            self.assertTrue(dl._ensure_hpc_worker_if_master_alive("gaeac7", "user"))
        self.assertIn("Background HPC terminal session for 'gaeac7' is ready", out_ok.getvalue())

        mock_proc_fail = MagicMock()
        mock_proc_fail.communicate.return_value = (
            "",
            "[!] Error: Background terminal session for 'gaeac7' failed to start: Remote SSH session stalled for 12s.\n"
            "[!] Closed stale multiplex SSH session for user@gaea-c7.fairmont.rdhpcs.noaa.gov.\n"
            "[!] Error: No active HPC session for 'gaeac7'.\n",
        )
        mock_proc_fail.returncode = 1

        err_buf = io.StringIO()
        with (
            patch.object(dl, "test_hpc_session_ready", return_value=False),
            patch("subprocess.Popen", return_value=mock_proc_fail),
            redirect_stderr(err_buf),
        ):
            ok = dl._ensure_hpc_worker_if_master_alive("gaeac7", "user")

        self.assertFalse(ok)
        self.assertIn("stalled for 12s", err_buf.getvalue())
        self.assertIn("Closed stale multiplex SSH session", err_buf.getvalue())

        mock_proc_timeout = MagicMock()
        mock_proc_timeout.communicate.side_effect = [subprocess.TimeoutExpired(["ssh_helper.py"], 48.0), ("", "")]
        err_buf2 = io.StringIO()
        with (
            patch.object(dl, "test_hpc_session_ready", return_value=False),
            patch("subprocess.Popen", return_value=mock_proc_timeout),
            redirect_stderr(err_buf2),
        ):
            ok2 = dl._ensure_hpc_worker_if_master_alive("gaeac7", "user")

        self.assertFalse(ok2)
        self.assertTrue(mock_proc_timeout.terminate.called)
        self.assertIn("Timed out verifying/recovering", err_buf2.getvalue())

    def test_ssh_check_status_bar_tty_and_non_tty_heartbeat(self):
        import io
        import os
        import time
        from unittest.mock import patch
        import download_pygraf_graphics as dl

        class FakeTty(io.StringIO):
            def isatty(self):
                return True

        fake_tty = FakeTty()
        with patch.object(dl.shutil, "get_terminal_size", return_value=os.terminal_size((100, 24))):
            with dl._SshCheckStatusBar(
                "Checking 'gaeac7' SSH connection...",
                stream=fake_tty,
                interval=0.02,
            ):
                time.sleep(0.14)

        raw = fake_tty.getvalue()
        self.assertIn("\r", raw)
        self.assertIn("[====", raw)
        self.assertIn("Checking 'gaeac7' SSH connection...", raw)
        # Final clear writes spaces + '\r' back to column 0
        self.assertTrue(raw.endswith("\r"))

        fake_log = io.StringIO()
        clock_vals = iter([0.0, 5.0, 10.0, 10.0])
        bar = dl._SshCheckStatusBar(
            "Checking 'gaeac7' SSH connection...",
            stream=fake_log,
            heartbeat_interval=5.0,
            clock=lambda: next(clock_vals, 10.0),
        )
        with bar._lock:
            bar._draw_locked()
        log_out = fake_log.getvalue()
        self.assertNotIn("\r", log_out)
        self.assertIn("... Checking 'gaeac7' SSH connection... (5s elapsed)", log_out)

    def test_main_does_not_auto_launch_ssh_helper_on_tty_or_non_tty(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch
        import download_pygraf_graphics as dl

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            dl_yaml = tmp / "download_pygraf_graphics.yaml"
            dl_yaml.write_text(
                "COMROOT: gaeac7:/gpfs/f7/OPSROOT/EXP_AUTH/com/rrfs/v2.1.4\n"
                "WGF: [det]\n"
                "cycle: [2024050600]\n"
                "fhr: [0]\n"
                "graphics list:\n"
                "- cref_{tile}_sfc_f{fhr}.png\n",
                encoding="utf-8",
            )
            out_dir = tmp / "graphics"

            for is_tty in (False, True):
                out_buf = io.StringIO()
                err_buf = io.StringIO()
                with (
                    patch.object(dl, "test_hpc_session_ready", return_value=False),
                    patch.object(dl, "_ensure_hpc_worker_if_master_alive", return_value=False) as mock_recover,
                    patch("subprocess.run", side_effect=AssertionError("ssh_helper.py should not be auto-launched")),
                    patch.object(sys.stdin, "isatty", return_value=is_tty),
                    patch.object(sys.stdout, "isatty", return_value=is_tty),
                    redirect_stdout(out_buf),
                    redirect_stderr(err_buf),
                ):
                    rc = dl.main([str(dl_yaml), "-o", str(out_dir)])

                self.assertEqual(rc, 1)
                self.assertEqual(mock_recover.call_count, 1)
                self.assertEqual(mock_recover.call_args.kwargs.get("check_session_first"), False)
                self.assertIn("checking SSH connection", out_buf.getvalue())
                self.assertIn("ush/ssh_helper.py gaeac7", err_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
