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

        exp_f6 = Experiment(
            name="RRFSv2_retv5",
            color="blue",
            COMROOT="/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2.retv5/com/rrfs/v2.1.4/",
        )
        self.assertEqual(exp_f6.hpc_cluster, "gaeac6")
        self.assertEqual(
            exp_f6.remote_comroot,
            "/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2.retv5/com/rrfs/v2.1.4",
        )

        exp_f7 = Experiment(name="RRFS_f7", color="green", COMROOT="/gpfs/f7/some/com/rrfs/v2")
        self.assertEqual(exp_f7.hpc_cluster, "gaeac7")
        self.assertEqual(exp_f7.remote_comroot, "/gpfs/f7/some/com/rrfs/v2")

        exp_other = Experiment(name="RRFS_other", color="purple", COMROOT="/gpfs/f60/not_f6")
        self.assertIsNone(exp_other.hpc_cluster)
        self.assertEqual(exp_other.remote_comroot, "/gpfs/f60/not_f6")

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
            "pygraf slides": {
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
            Experiment(name="exp_one", color="red", short_name="base4"),
            Experiment(name="exp_two", color="blue", short_name="3p5km"),
            Experiment(name="exp_three", color="green"),
        )
        self.assertEqual(resolve_experiment("first", exps), exps[0])
        self.assertEqual(resolve_experiment("second", exps), exps[1])
        self.assertEqual(resolve_experiment("third", exps), exps[2])
        self.assertIsNone(resolve_experiment("fourth", exps))
        self.assertEqual(resolve_experiment("exp_one", exps), exps[0])
        self.assertEqual(resolve_experiment("base4", exps), exps[0])
        self.assertEqual(resolve_experiment("3P5KM", exps), exps[1])

    def test_process_graphics_slides_uses_short_name_captions_and_preserves_cache_dir(self):
        from io import BytesIO
        from unittest.mock import patch
        from PIL import Image
        from pptx import Presentation
        from automats.graphics import process_graphics_slides

        png_buf = BytesIO()
        Image.new("RGB", (60, 40), color="navy").save(png_buf, format="PNG")
        valid_png = png_buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            gfx_recipe = tmp / "graphics_layout.yaml"
            gfx_recipe.write_text(
                "pygraf slides:\n"
                "- type: same valid time\n"
                "  title: Reflectivity\n"
                "  cycle: 2024050612\n"
                "  graphics name: cref_full_sfc_f006.png\n"
                "  number of rows: 1\n"
                "  row1: [first, second]\n"
                "- type: same cycle\n"
                "  title: Reflectivity\n"
                "  experiment: second\n"
                "  cycle: 2024050612\n"
                "  number of rows: 1\n"
                "  row1: [cref_full_sfc_f006.png]\n",
                encoding="utf-8",
            )
            plan = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {
                        "name": "RRFSv2_conus_3km_ret_v2x_retv4",
                        "short name": "base4",
                        "color": "red",
                        "COMROOT": "gaeac6:/gpfs/exp1/com/rrfs/v2",
                    },
                    {
                        "name": "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km",
                        "short name": "3p5km",
                        "color": "blue",
                        "COMROOT": "gaeac6:/gpfs/exp2/com/rrfs/v2",
                    },
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })

            # Cache paths must continue using full canonical exp.name directories
            for exp_name in (
                "RRFSv2_conus_3km_ret_v2x_retv4",
                "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km",
            ):
                p = tmp / "graphics" / exp_name / "2024050612_cref_full_sfc_f006.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(valid_png)

            deck = Presentation()
            with patch("automats.graphics.test_hpc_session_ready", return_value=True):
                added = process_graphics_slides(plan, tmp, deck)

            self.assertEqual(added, 2)
            slide1_texts = [s.text for s in deck.slides[0].shapes if s.has_text_frame]
            slide2_texts = [s.text for s in deck.slides[1].shapes if s.has_text_frame]
            self.assertIn("base4", slide1_texts)
            self.assertIn("3p5km", slide1_texts)
            self.assertTrue(any("3p5km" in t and "Cycle: 2024050612" in t for t in slide2_texts))
            self.assertFalse(any("RRFSv2_conus_3km" in t for t in slide1_texts + slide2_texts))

            # In 'type: same cycle', short name run is blue while cycle suffix stays slate gray
            from pptx.dml.color import RGBColor
            slide2_heading = next(
                s for s in deck.slides[1].shapes
                if s.has_text_frame and "3p5km" in s.text and "Cycle: 2024050612" in s.text
            )
            sub_p = slide2_heading.text_frame.paragraphs[1]
            self.assertEqual(len(sub_p.runs), 2)
            self.assertEqual(sub_p.runs[0].text, "3p5km")
            self.assertEqual(sub_p.runs[0].font.color.rgb, RGBColor(0, 0, 255))
            self.assertEqual(sub_p.runs[1].text, "   •   Cycle: 2024050612")
            self.assertEqual(sub_p.runs[1].font.color.rgb, RGBColor(70, 85, 105))

            # In 'type: same valid time', subtitle stays slate gray
            slide1_heading = next(
                s for s in deck.slides[0].shapes
                if s.has_text_frame and "Cycle: 2024050612 F006" in s.text
            )
            self.assertEqual(slide1_heading.text_frame.paragraphs[1].font.color.rgb, RGBColor(70, 85, 105))

    def test_process_graphics_slides_removes_per_plot_captions_for_composite_reflectivity_with_cycle_fhr(self):
        from io import BytesIO
        from unittest.mock import patch
        from PIL import Image
        from pptx import Presentation
        from automats.graphics import process_graphics_slides

        png_buf = BytesIO()
        Image.new("RGB", (60, 40), color="navy").save(png_buf, format="PNG")
        valid_png = png_buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            gfx_recipe = tmp / "graphics_layout.yaml"
            gfx_recipe.write_text(
                "pygraf slides:\n"
                "- type: same valid time\n"
                "  title: Composite Reflectivity\n"
                "  cycle: 2024050612\n"
                "  graphics name: cref_full_sfc_f006.png\n"
                "  number of rows: 1\n"
                "  row1: [first, second]\n"
                "- type: same cycle\n"
                "  title: Composite Reflectivity\n"
                "  experiment: second\n"
                "  cycle: 2024050612\n"
                "  number of rows: 1\n"
                "  row1: [cref_full_sfc_f006.png]\n",
                encoding="utf-8",
            )
            plan = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {
                        "name": "RRFSv2_conus_3km_ret_v2x_retv4",
                        "short name": "base4",
                        "color": "red",
                        "COMROOT": "gaeac6:/gpfs/exp1/com/rrfs/v2",
                    },
                    {
                        "name": "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km",
                        "short name": "3p5km",
                        "color": "blue",
                        "COMROOT": "gaeac6:/gpfs/exp2/com/rrfs/v2",
                    },
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })

            for exp_name in (
                "RRFSv2_conus_3km_ret_v2x_retv4",
                "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km",
            ):
                p = tmp / "graphics" / exp_name / "2024050612_cref_full_sfc_f006.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(valid_png)

            deck = Presentation()
            with patch("automats.graphics.test_hpc_session_ready", return_value=True):
                added = process_graphics_slides(plan, tmp, deck)

            self.assertEqual(added, 2)
            slide1_texts = [s.text for s in deck.slides[0].shapes if s.has_text_frame]
            slide2_texts = [s.text for s in deck.slides[1].shapes if s.has_text_frame]

            # In Slide 1 (same valid time, minor title has cycle and fhr), labels are clean and header is cropped
            self.assertIn("base4", slide1_texts)
            self.assertIn("3p5km", slide1_texts)
            self.assertTrue(any("Cycle: 2024050612 F006" in t for t in slide1_texts))

            # In Slide 2 (same cycle, minor title has cycle but no fhr), per-plot caption F006 is retained
            self.assertIn("F006", slide2_texts)
            self.assertTrue(any("3p5km" in t and "Cycle: 2024050612" in t for t in slide2_texts))

    def test_crop_pygraf_header(self):
        from PIL import Image, ImageDraw
        from automats.graphics import crop_pygraf_header, detect_pygraf_borders, extract_pygraf_colorbar

        # Synthesize a pygraf-like image (712 x 549) with top border at y=67
        im = Image.new("RGB", (712, 549), color="white")
        draw = ImageDraw.Draw(im)
        draw.text((100, 20), "Composite Reflectivity (dBZ, shaded)", fill=(0, 0, 0))
        draw.line([(11, 67), (706, 67)], fill=(0, 0, 0), width=1)

        cropped = crop_pygraf_header(im)
        self.assertEqual(cropped.size, (712, 482))

        # Regional pygraf image (712 x 676) with #333333 (51, 51, 51) border lines and bottom colorbar
        im_reg = Image.new("RGB", (712, 676), color="white")
        draw_reg = ImageDraw.Draw(im_reg)
        draw_reg.text((100, 20), "Composite Reflectivity (dBZ, shaded)", fill=(51, 51, 51))
        # Top border at y=79
        draw_reg.line([(11, 79), (706, 79)], fill=(51, 51, 51), width=1)
        # Bottom border at y=604
        draw_reg.line([(11, 604), (706, 604)], fill=(51, 51, 51), width=1)
        # Colorbar box below y=604
        draw_reg.rectangle([(20, 620), (690, 650)], fill=(0, 128, 255))

        top_y, bottom_y = detect_pygraf_borders(im_reg)
        self.assertEqual(top_y, 79)
        self.assertEqual(bottom_y, 604)

        # Header-only crop
        cropped_reg_hdr = crop_pygraf_header(im_reg, crop_colorbar=False)
        self.assertEqual(cropped_reg_hdr.size, (712, 597))

        # Header + colorbar crop
        cropped_reg_both = crop_pygraf_header(im_reg, crop_colorbar=True)
        self.assertEqual(cropped_reg_both.size, (712, 526))

        # Extract rotated colorbar
        cb_rot = extract_pygraf_colorbar(im_reg)
        self.assertIsNotNone(cb_rot)
        self.assertEqual(cb_rot.size[1], 679)  # height in vertical orientation corresponds to width

        # Image without pygraf border is returned unchanged
        small_im = Image.new("RGB", (100, 100), color="white")
        self.assertEqual(crop_pygraf_header(small_im).size, (100, 100))
        self.assertIsNone(extract_pygraf_colorbar(small_im))

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
                {"username": "user", "remote_path": "/r/1", "local_path": cached_file, "cluster": "gaeac6"},
                {"username": "user", "remote_path": "/r/2", "local_path": new_file1, "cluster": "gaeac6"},
                {"username": "user", "remote_path": "/r/3", "local_path": new_file2, "cluster": "gaeac6"},
            ]

            def fake_stream_tar(username, tasks, cluster="HPC", debug=False):
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

    def test_stream_tar_pull_shared_remote_path_to_multiple_destinations(self):
        import io
        import tarfile
        from unittest.mock import MagicMock, patch
        from automats.graphics import batch_pull_graphics_files, stream_tar_pull

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
            out_exp1 = tmp / "exp1" / "2024050700_cref_full_sfc_f006.png"
            out_exp2 = tmp / "exp2" / "2024050700_cref_full_sfc_f006.png"

            def make_tar_bytes():
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tf:
                    data = b"SHARED_CREF_PNG"
                    ti = tarfile.TarInfo(name="/gpfs/shared/cref_full_sfc_f006.png")
                    ti.size = len(data)
                    tf.addfile(ti, io.BytesIO(data))
                buf.seek(0)
                return buf

            stdin_cap = CapturingBytesIO()
            mock_proc = MagicMock()
            mock_proc.stdin = stdin_cap
            mock_proc.stdout = make_tar_bytes()
            mock_proc.stderr = io.BytesIO()
            mock_proc.wait.return_value = 0

            tasks = [
                ("/gpfs/shared/cref_full_sfc_f006.png", out_exp1),
                ("/gpfs/shared/cref_full_sfc_f006.png", out_exp2),
            ]

            with patch("subprocess.Popen", return_value=mock_proc):
                completed, missing, errors = stream_tar_pull(
                    username="user",
                    tasks=tasks,
                    cluster="gaeac6",
                    debug=True,
                )

            self.assertEqual((completed, missing, errors), (2, 0, 0))
            self.assertEqual(stdin_cap.captured, b"/gpfs/shared/cref_full_sfc_f006.png\x00")
            self.assertEqual(out_exp1.read_bytes(), b"SHARED_CREF_PNG")
            self.assertEqual(out_exp2.read_bytes(), b"SHARED_CREF_PNG")

            # Also verify batch_pull_graphics_files delivers both local files end-to-end
            out_b1 = tmp / "batch_exp1" / "cref.png"
            out_b2 = tmp / "batch_exp2" / "cref.png"
            mock_proc2 = MagicMock()
            mock_proc2.stdin = CapturingBytesIO()
            mock_proc2.stdout = make_tar_bytes()
            mock_proc2.stderr = io.BytesIO()
            mock_proc2.wait.return_value = 0

            with patch("subprocess.Popen", return_value=mock_proc2):
                results = batch_pull_graphics_files([
                    {
                        "username": "user",
                        "remote_path": "/gpfs/shared/cref_full_sfc_f006.png",
                        "local_path": out_b1,
                        "cluster": "gaeac6",
                    },
                    {
                        "username": "user",
                        "remote_path": "/gpfs/shared/cref_full_sfc_f006.png",
                        "local_path": out_b2,
                        "cluster": "gaeac6",
                    },
                ])

            self.assertTrue(results[str(out_b1)])
            self.assertTrue(results[str(out_b2)])
            self.assertEqual(out_b1.read_bytes(), b"SHARED_CREF_PNG")
            self.assertEqual(out_b2.read_bytes(), b"SHARED_CREF_PNG")

    def test_load_hpc_users_simple_format(self):
        from automats.graphics import load_hpc_users

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / ".hpc_config"
            cfg_path.write_text(
                "# RDHPCS usernames by cluster\n"
                "gaeac6: Guoqing.Ge\n"
                "hera: 'Alt.User' # inline comment\n"
                "ursa: <your_username>\n"
                "gaeac7:\n"
                "  username: Nested.User\n",
                encoding="utf-8",
            )
            users = load_hpc_users(cfg_path)
            self.assertEqual(
                users,
                {
                    "gaeac6": "Guoqing.Ge",
                    "hera": "Alt.User",
                    "gaeac7": "Nested.User",
                },
            )

    def test_process_graphics_slides_tracks_newly_saved_graphics_count(self):
        from io import BytesIO
        from unittest.mock import patch
        from PIL import Image
        from pptx import Presentation
        from automats.graphics import process_graphics_slides

        png_buf = BytesIO()
        Image.new("RGB", (60, 40), color="navy").save(png_buf, format="PNG")
        valid_png = png_buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            gfx_recipe = tmp / "graphics_layout.yaml"
            gfx_recipe.write_text(
                "pygraf slides:\n"
                "- type: same valid time\n"
                "  title: Reflectivity\n"
                "  cycle: 2024050612\n"
                "  graphics name: cref_full_sfc_f006.png\n"
                "  number of rows: 1\n"
                "  row1: [exp1, exp2, obs]\n",
                encoding="utf-8",
            )
            plan = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {"name": "exp1", "color": "red", "COMROOT": "gaeac6:/gpfs/exp1/com/rrfs/v2"},
                    {"name": "exp2", "color": "blue", "COMROOT": "gaeac6:/gpfs/exp2/com/rrfs/v2"},
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })

            # Pre-cache exp1 so only exp2 (1 pygraf) + obs (1 MRMS) are newly downloaded
            cached_exp1 = tmp / "graphics" / "exp1" / "2024050612_cref_full_sfc_f006.png"
            cached_exp1.parent.mkdir(parents=True, exist_ok=True)
            cached_exp1.write_bytes(valid_png)

            def fake_batch_pull(tasks, max_workers=1, debug=False):
                results = {}
                for t in tasks:
                    loc = Path(t["local_path"])
                    loc.parent.mkdir(parents=True, exist_ok=True)
                    if not loc.exists():
                        loc.write_bytes(valid_png)
                    results[str(loc)] = True
                return results

            def fake_fetch_mrms(uncached, output_dir=None):
                for item in uncached:
                    dest = Path(item["dest_path"])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(valid_png)

            deck = Presentation()
            stats = {}
            with (
                patch("automats.graphics.test_hpc_session_ready", return_value=True),
                patch("automats.graphics.batch_pull_graphics_files", side_effect=fake_batch_pull),
                patch("automats.mrms.fetch_mrms_images", side_effect=fake_fetch_mrms),
                patch("automats.graphics.is_valid_mrms_file", side_effect=lambda p: Path(p).exists() and Path(p).stat().st_size > 0),
            ):
                added = process_graphics_slides(plan, tmp, deck, stats=stats)

            self.assertEqual(added, 1)
            self.assertEqual(stats["saved"], 2)

    def test_hpc_session_ready_requires_ping_response(self):
        import json
        import os
        import select
        import socket
        import threading
        import uuid
        from automats.graphics import test_hpc_session_ready

        unique_user = f"gfxping_{uuid.uuid4().hex[:8]}"
        sock_path = f"/tmp/hpc_term_{unique_user}_gaeac7.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(4)
        stop_evt = threading.Event()
        state = {"mode": "hang"}
        conns = []

        def _serve():
            while not stop_evt.is_set():
                r, _, _ = select.select([srv], [], [], 0.05)
                if srv in r:
                    conn, _ = srv.accept()
                    if state["mode"] == "ok":
                        data = conn.recv(4096)
                        req = json.loads(data.decode("utf-8").strip())
                        self.assertEqual(req.get("cmd"), "ping")
                        conn.sendall(b'{"code": 0, "stdout_b64": ""}\n')
                        conn.close()
                    elif state["mode"] == "fail":
                        conn.recv(4096)
                        conn.sendall(b'{"code": 1, "error": "dead"}\n')
                        conn.close()
                    else:
                        conns.append(conn)

        from unittest.mock import patch
        th = threading.Thread(target=_serve, daemon=True)
        th.start()
        try:
            with patch("glob.glob", return_value=[sock_path]):
                self.assertFalse(test_hpc_session_ready("gaeac7", username=unique_user))
                state["mode"] = "fail"
                self.assertFalse(test_hpc_session_ready("gaeac7", username=unique_user))
                state["mode"] = "ok"
                self.assertTrue(test_hpc_session_ready("gaeac7", username=unique_user))
        finally:
            stop_evt.set()
            th.join(timeout=1.0)
            for c in conns:
                try:
                    c.close()
                except OSError:
                    pass
            srv.close()
            if os.path.exists(sock_path):
                os.unlink(sock_path)

    def test_ensure_hpc_worker_if_master_alive_reports_stale_and_handles_timeout(self):
        from contextlib import redirect_stderr
        import io
        import subprocess
        from unittest.mock import MagicMock, patch
        import automats.graphics as gfx

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
            patch.object(gfx, "test_hpc_session_ready", return_value=False),
            patch("subprocess.Popen", return_value=mock_proc_fail),
            redirect_stderr(err_buf),
        ):
            ok = gfx._ensure_hpc_worker_if_master_alive("gaeac7", "user")

        self.assertFalse(ok)
        self.assertIn("stalled for 12s", err_buf.getvalue())
        self.assertIn("Closed stale multiplex SSH session", err_buf.getvalue())

        mock_proc_timeout = MagicMock()
        mock_proc_timeout.communicate.side_effect = [subprocess.TimeoutExpired(["ssh_helper.py"], 48.0), ("", "")]
        err_buf2 = io.StringIO()
        with (
            patch.object(gfx, "test_hpc_session_ready", return_value=False),
            patch("subprocess.Popen", return_value=mock_proc_timeout),
            redirect_stderr(err_buf2),
        ):
            ok2 = gfx._ensure_hpc_worker_if_master_alive("gaeac7", "user")

        self.assertFalse(ok2)
        self.assertTrue(mock_proc_timeout.terminate.called)
        self.assertIn("Timed out verifying/recovering", err_buf2.getvalue())

    def test_process_graphics_slides_attempts_worker_recovery_and_prints_status(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import BytesIO, StringIO
        from unittest.mock import patch
        from PIL import Image
        from pptx import Presentation
        import automats.graphics as gfx

        png_buf = BytesIO()
        Image.new("RGB", (60, 40), color="navy").save(png_buf, format="PNG")
        valid_png = png_buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            gfx_recipe = tmp / "graphics_layout.yaml"
            gfx_recipe.write_text(
                "pygraf slides:\n"
                "- type: same valid time\n"
                "  title: Reflectivity\n"
                "  cycle: 2024050612\n"
                "  graphics name: cref_full_sfc_f006.png\n"
                "  number of rows: 1\n"
                "  row1: [exp1]\n",
                encoding="utf-8",
            )
            plan = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {"name": "exp1", "color": "red", "COMROOT": "gaeac7:/gpfs/f7/exp1/com/rrfs/v2"},
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })

            def fake_batch_pull(tasks, max_workers=1, debug=False):
                results = {}
                for t in tasks:
                    loc = Path(t["local_path"])
                    loc.parent.mkdir(parents=True, exist_ok=True)
                    loc.write_bytes(valid_png)
                    results[str(loc)] = True
                return results

            deck = Presentation()
            out_fail = StringIO()
            err_fail = StringIO()
            with (
                patch.object(gfx, "test_hpc_session_ready", return_value=False),
                patch.object(gfx, "_ensure_hpc_worker_if_master_alive", return_value=False) as mock_recover_fail,
                redirect_stdout(out_fail),
                redirect_stderr(err_fail),
            ):
                added_fail = gfx.process_graphics_slides(plan, tmp, deck)

            self.assertEqual(added_fail, 0)
            self.assertEqual(mock_recover_fail.call_count, 1)
            self.assertIn("checking SSH connection", out_fail.getvalue())
            self.assertIn("Skipping pygraf slides", err_fail.getvalue())

            out_ok = StringIO()
            stats = {}
            with (
                patch.object(gfx, "test_hpc_session_ready", return_value=False),
                patch.object(gfx, "_ensure_hpc_worker_if_master_alive", return_value=True) as mock_recover_ok,
                patch.object(gfx, "batch_pull_graphics_files", side_effect=fake_batch_pull),
                redirect_stdout(out_ok),
            ):
                added_ok = gfx.process_graphics_slides(plan, tmp, deck, stats=stats)

            self.assertEqual(added_ok, 1)
            self.assertEqual(stats["saved"], 1)
            self.assertEqual(mock_recover_ok.call_count, 1)
            self.assertIn("checking SSH connection", out_ok.getvalue())

    def test_ssh_check_status_bar_and_progress_display_coordination(self):
        from contextlib import redirect_stderr
        import io
        import os
        import time
        from unittest.mock import patch
        from pptx import Presentation
        from automats.cli import ProgressDisplay
        import automats.graphics as gfx

        class FakeTty(io.StringIO):
            def isatty(self):
                return True

        fake_tty = FakeTty()
        with patch.object(gfx.shutil, "get_terminal_size", return_value=os.terminal_size((100, 24))):
            with gfx._SshCheckStatusBar(
                "Checking 'gaeac7' SSH connection...",
                stream=fake_tty,
                interval=0.02,
            ):
                time.sleep(0.14)

        raw = fake_tty.getvalue()
        self.assertIn("\r", raw)
        self.assertIn("[====", raw)
        self.assertTrue(raw.endswith("\r"))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            gfx_recipe = tmp / "graphics_layout.yaml"
            gfx_recipe.write_text(
                "pygraf slides:\n"
                "- type: same valid time\n"
                "  title: Reflectivity\n"
                "  cycle: 2024050612\n"
                "  graphics name: cref_full_sfc_f006.png\n"
                "  number of rows: 1\n"
                "  row1: [exp1]\n",
                encoding="utf-8",
            )
            plan = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {"name": "exp1", "color": "red", "COMROOT": "gaeac7:/gpfs/f7/exp1/com/rrfs/v2"},
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })

            progress_stream = io.StringIO()
            progress = ProgressDisplay(1, stream=progress_stream)
            deck = Presentation()
            with (
                patch.object(gfx, "test_hpc_session_ready", return_value=False),
                patch.object(gfx, "_ensure_hpc_worker_if_master_alive", return_value=False) as mock_rec,
                redirect_stderr(io.StringIO()),
            ):
                gfx.process_graphics_slides(plan, tmp, deck, log=progress.log)

            self.assertEqual(mock_rec.call_count, 1)
            self.assertEqual(mock_rec.call_args.kwargs.get("show_status"), False)
            self.assertIn("checking SSH connection", progress_stream.getvalue())

    def test_process_graphics_slides_deduplicates_missing_comroot_warning_and_logs_cleanly(self):
        from contextlib import redirect_stderr
        import io
        import os
        from unittest.mock import patch
        from pptx import Presentation
        from automats.cli import ProgressDisplay
        import automats.graphics as gfx

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            gfx_recipe = tmp / "graphics_layout.yaml"
            gfx_recipe.write_text(
                "pygraf slides:\n"
                "- type: same valid time\n"
                "  title: Reflectivity\n"
                "  cycle: 2024050700\n"
                "  graphics name: cref_full_sfc_f006.png\n"
                "  number of rows: 2\n"
                "  row1: [first, second]\n"
                "  row2: [obs]\n"
                "- type: same cycle\n"
                "  title: Reflectivity\n"
                "  experiment: second\n"
                "  cycle: 2024050700\n"
                "  number of rows: 2\n"
                "  row1: [cref_full_sfc_f001.pg, obs]\n"
                "  row2: [cref_full_sfc_f002.pg, obs]\n",
                encoding="utf-8",
            )
            plan = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {"name": "exp_with_comroot", "color": "red", "COMROOT": "gaeac6:/gpfs/exp1/com/rrfs/v2"},
                    {"name": "exp_no_comroot", "color": "blue"},
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })

            expected_warning = "[!] Warning: Experiment 'exp_no_comroot' has no COMROOT configured."

            # 1. Without outer log callback, warning prints once to stderr
            deck1 = Presentation()
            err1 = io.StringIO()
            with (
                patch.object(gfx, "test_hpc_session_ready", return_value=True),
                patch.object(gfx, "batch_pull_graphics_files", return_value={}),
                patch("automats.mrms.fetch_mrms_images", return_value=None),
                redirect_stderr(err1),
            ):
                added1 = gfx.process_graphics_slides(plan, tmp, deck1)

            self.assertEqual(added1, 2)
            self.assertEqual(err1.getvalue().count(expected_warning), 1)

            # 2. With TTY ProgressDisplay, warning logs once cleanly without colliding on spinner text
            class FakeTty(io.StringIO):
                def isatty(self):
                    return True

            tty_stream = FakeTty()
            err2 = io.StringIO()
            deck2 = Presentation()
            with (
                patch.object(gfx.shutil, "get_terminal_size", return_value=os.terminal_size((120, 24))),
                patch.object(gfx, "test_hpc_session_ready", return_value=True),
                patch.object(gfx, "batch_pull_graphics_files", return_value={}),
                patch("automats.mrms.fetch_mrms_images", return_value=None),
                redirect_stderr(err2),
            ):
                with ProgressDisplay(0, stream=tty_stream) as progress:
                    added2 = gfx.process_graphics_slides(plan, tmp, deck2, log=progress.log)

            self.assertEqual(added2, 2)
            self.assertEqual(err2.getvalue(), "")
            output = tty_stream.getvalue()
            self.assertEqual(output.count(expected_warning), 1)
            self.assertIn(f"\r{expected_warning}\n", output)
            self.assertNotIn(f"session{expected_warning}", output)
            self.assertIn("Building pygraf slides", output)

            # 3. Bare /gpfs/f6/... infers gaeac6 for download, while unknown bare paths warn cleanly once
            plan_inferred = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {"name": "exp_f6", "color": "red", "COMROOT": "/gpfs/f6/exp1/com/rrfs/v2"},
                    {"name": "exp_unknown", "color": "blue", "COMROOT": "/scratch3/exp2/com/rrfs/v2"},
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })
            captured_tasks = []
            err3 = io.StringIO()
            deck3 = Presentation()
            with (
                patch.object(gfx, "test_hpc_session_ready", return_value=True),
                patch.object(gfx, "batch_pull_graphics_files", side_effect=lambda tasks, **_: captured_tasks.extend(tasks) or {}),
                patch("automats.mrms.fetch_mrms_images", return_value=None),
                redirect_stderr(err3),
            ):
                added3 = gfx.process_graphics_slides(plan_inferred, tmp, deck3)

            self.assertEqual(added3, 2)
            self.assertEqual(len(captured_tasks), 1)
            self.assertEqual(captured_tasks[0]["cluster"], "gaeac6")
            self.assertIn("/gpfs/f6/exp1/com/rrfs/v2/rrfs.20240507/00/graphics/det/full/202405070006/cref_full_sfc_f006.png",
                          captured_tasks[0]["remote_path"])
            unknown_warning = (
                "[!] Warning: Experiment 'exp_unknown' COMROOT '/scratch3/exp2/com/rrfs/v2' "
                "cannot infer an HPC cluster; prefix with <cluster>: (e.g. ursa:/path)."
            )
            self.assertEqual(err3.getvalue().count(unknown_warning), 1)


class BaselineC6Tests(unittest.TestCase):
    @staticmethod
    def _import_baselinec6():
        import sys
        ush_dir = str(Path(__file__).resolve().parents[1] / "ush")
        if ush_dir not in sys.path:
            sys.path.insert(0, ush_dir)
        import baselinec6
        return baselinec6

    def _make_fake_root(self, tmp: Path) -> Path:
        recipes_dir = tmp / "recipes"
        recipes_dir.mkdir(parents=True, exist_ok=True)
        (recipes_dir / "baseline.yaml").write_text(
            "experiments:\n"
            "- name: RRFSv2_conus_3km_ret_v2x_retv4\n"
            "  color: red\n"
            "  COMROOT: gaeac6:/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4\n"
            "- name: existing_other\n"
            "  color: blue\n"
            "  COMROOT: gaeac6:/gpfs/f6/existing/com/rrfs/v2.1.4\n"
            "pygraf slides:\n"
            "  enable: true\n"
            "  recipe: pygraf_slides_layout2_May2024.yaml\n"
            "common:\n"
            "  line weight: 3\n"
            "plots:\n"
            "- name: sfc_dieoff\n"
            "  application: surface\n"
            "  plot_type: dieoff\n"
            "  variable: [t2m]\n"
            "  statistic: [rmse]\n",
            encoding="utf-8",
        )
        return tmp

    def test_resolve_comroot_cluster_inference(self):
        baselinec6 = self._import_baselinec6()
        self.assertEqual(
            baselinec6.resolve_comroot("/gpfs/f6/bil-fire10-oar/user/com/rrfs/v2.1.4/"),
            "gaeac6:/gpfs/f6/bil-fire10-oar/user/com/rrfs/v2.1.4",
        )
        self.assertEqual(
            baselinec6.resolve_comroot("/gpfs/f7/bil-fire10-oar/user/com/rrfs/v2.1.4"),
            "gaeac7:/gpfs/f7/bil-fire10-oar/user/com/rrfs/v2.1.4",
        )
        self.assertEqual(baselinec6.resolve_comroot("/gpfs/f6"), "gaeac6:/gpfs/f6")
        self.assertEqual(baselinec6.resolve_comroot("/gpfs/f7"), "gaeac7:/gpfs/f7")
        self.assertEqual(
            baselinec6.resolve_comroot("GAEAC7:/gpfs/f7/custom/path/"),
            "gaeac7:/gpfs/f7/custom/path",
        )
        self.assertEqual(
            baselinec6.resolve_comroot("ursa:/scratch3/user/com/rrfs/v2"),
            "ursa:/scratch3/user/com/rrfs/v2",
        )
        with self.assertRaises(ValueError):
            baselinec6.resolve_comroot("/gpfs/f60/not_f6")
        with self.assertRaises(ValueError):
            baselinec6.resolve_comroot("/scratch3/no_cluster_prefix")

    def test_main_positional_expname_and_hpc_dir(self):
        from contextlib import redirect_stderr
        import io
        from unittest.mock import patch
        import yaml

        baselinec6 = self._import_baselinec6()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_fake_root(Path(tmpdir))
            captured = {}

            def fake_automats_main(cli_args):
                captured["args"] = cli_args
                return 0

            stderr_buf = io.StringIO()
            with (
                patch.object(baselinec6, "ROOT", root),
                patch.object(baselinec6, "automats_main", side_effect=fake_automats_main),
                redirect_stderr(stderr_buf),
            ):
                rc = baselinec6.main([
                    "my_mats_exp",
                    "my_short",
                    "/gpfs/f6/bil-fire10-oar/scratch/me/com/rrfs/v2.1.4/",
                    "--slides-only",
                ])

            self.assertEqual(rc, 0)
            self.assertEqual(stderr_buf.getvalue(), "")
            tmp_recipe = Path(captured["args"][0])
            self.assertEqual(tmp_recipe, root / "tmp" / "baseline_c6_my_mats_exp.yaml")
            self.assertEqual(
                captured["args"][1:],
                ["--slides-only", "--output-dir", str(root / "results" / "baseline_c6_my_mats_exp")],
            )

            data = yaml.safe_load(tmp_recipe.read_text(encoding="utf-8"))
            self.assertEqual(len(data["experiments"]), 2)
            self.assertEqual(data["experiments"][0]["MATS name"], "RRFSv2_conus_3km_ret_v2x_retv4")
            self.assertEqual(data["experiments"][0]["short name"], baselinec6.BASELINE_SHORT_NAME)
            self.assertEqual(
                data["experiments"][0]["COMROOT"],
                "gaeac6:/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4",
            )
            self.assertEqual(data["experiments"][1]["MATS name"], "my_mats_exp")
            self.assertEqual(data["experiments"][1]["short name"], "my_short")
            self.assertEqual(data["experiments"][1]["color"], "blue")
            self.assertEqual(
                data["experiments"][1]["COMROOT"],
                "gaeac6:/gpfs/f6/bil-fire10-oar/scratch/me/com/rrfs/v2.1.4",
            )
            self.assertTrue(data["pygraf slides"]["enable"])

    def test_main_f7_with_leading_options_and_flags(self):
        from contextlib import redirect_stderr
        import io
        from unittest.mock import patch
        import yaml

        baselinec6 = self._import_baselinec6()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_fake_root(Path(tmpdir))
            captured = {}

            def fake_automats_main(cli_args):
                captured["args"] = cli_args
                return 0

            stderr_buf = io.StringIO()
            with (
                patch.object(baselinec6, "ROOT", root),
                patch.object(baselinec6, "automats_main", side_effect=fake_automats_main),
                redirect_stderr(stderr_buf),
            ):
                rc = baselinec6.main([
                    "--output-dir", str(root / "custom_out"),
                    "--limit", "2",
                    "my_f7_exp",
                    "my_f7_short",
                    "/gpfs/f7/bil-fire10-oar/scratch/me/com/rrfs/v2.1.4",
                ])

            self.assertEqual(rc, 0)
            self.assertEqual(stderr_buf.getvalue(), "")
            tmp_recipe = Path(captured["args"][0])
            data = yaml.safe_load(tmp_recipe.read_text(encoding="utf-8"))
            self.assertEqual(data["experiments"][1]["MATS name"], "my_f7_exp")
            self.assertEqual(data["experiments"][1]["short name"], "my_f7_short")
            self.assertEqual(
                data["experiments"][1]["COMROOT"],
                "gaeac7:/gpfs/f7/bil-fire10-oar/scratch/me/com/rrfs/v2.1.4",
            )
            self.assertEqual(
                captured["args"][1:],
                ["--output-dir", str(root / "custom_out"), "--limit", "2"],
            )

            # Flag form --expname / --short-name / --hpc-dir also resolves /gpfs/f6 and /gpfs/f7
            stderr_buf2 = io.StringIO()
            with (
                patch.object(baselinec6, "ROOT", root),
                patch.object(baselinec6, "automats_main", side_effect=fake_automats_main),
                redirect_stderr(stderr_buf2),
            ):
                rc2 = baselinec6.main([
                    "--expname", "existing_other",
                    "--short-name", "other_s",
                    "--hpc-dir", "/gpfs/f7/new_dir/com/rrfs/v2.1.4",
                ])

            self.assertEqual(rc2, 0)
            self.assertEqual(stderr_buf2.getvalue(), "")
            data2 = yaml.safe_load(Path(captured["args"][0]).read_text(encoding="utf-8"))
            self.assertEqual(data2["experiments"][1]["short name"], "other_s")
            self.assertEqual(
                data2["experiments"][1]["COMROOT"],
                "gaeac7:/gpfs/f7/new_dir/com/rrfs/v2.1.4",
            )

    def test_main_rejects_swapped_or_ambiguous_args(self):
        from contextlib import redirect_stderr
        import io
        from unittest.mock import patch
        baselinec6 = self._import_baselinec6()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_fake_root(Path(tmpdir))
            with patch.object(baselinec6, "ROOT", root):
                # Directory path given first instead of expname
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(baselinec6.main(["/gpfs/f6/some/path", "my_short"]), 1)
                self.assertIn("Invalid <expname>", err.getvalue())

                # Missing required <short_name>
                for bad_args in (
                    ["my_exp"],
                    ["my_exp", "--slides-only"],
                    ["--expname", "my_exp"],
                    ["my_exp", "--short-name", "   "],
                    ["my_exp", "--short-name="],
                ):
                    err = io.StringIO()
                    with redirect_stderr(err), self.subTest(bad_args=bad_args):
                        self.assertEqual(baselinec6.main(bad_args), 1)
                        self.assertIn("short_name", err.getvalue())

                # Missing value for --short-name
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(baselinec6.main(["my_exp", "--short-name"]), 1)
                self.assertIn("Missing value for --short-name", err.getvalue())

                # Forgot <short_name> and put HPC path in 2nd position
                for path_arg in ("/gpfs/f6/some/path", "gaeac6:/gpfs/f6/some/path", "/scratch3/some/path"):
                    err = io.StringIO()
                    with redirect_stderr(err), self.subTest(path_arg=path_arg):
                        self.assertEqual(baselinec6.main(["my_exp", path_arg]), 1)
                        self.assertIn("Invalid <short_name>", err.getvalue())

                # Bare non-/gpfs/f6,/gpfs/f7 path without <cluster>:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(baselinec6.main(["my_exp", "my_s", "/scratch3/some/path"]), 1)
                    # Extra unexpected positional argument
                    self.assertEqual(baselinec6.main(["my_exp", "my_s", "/gpfs/f6/some/path", "extra"]), 1)

    def test_main_omitted_hpc_dir_skips_target_comroot_keeps_baseline_graphics(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch
        import yaml

        baselinec6 = self._import_baselinec6()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_fake_root(Path(tmpdir))
            captured = {}

            def fake_automats_main(cli_args, *, pre_slides_warnings=None):
                captured["args"] = cli_args
                captured["warnings"] = list(pre_slides_warnings or ())
                return 0

            with (
                patch.object(baselinec6, "ROOT", root),
                patch.object(baselinec6, "automats_main", new=fake_automats_main),
            ):
                rc1 = baselinec6.main(["existing_other", "other_s", "--plan"])
                data1 = yaml.safe_load(Path(captured["args"][0]).read_text(encoding="utf-8"))

            self.assertEqual(rc1, 0)
            self.assertEqual(data1["experiments"][0]["short name"], "base4")
            self.assertEqual(data1["experiments"][1]["short name"], "other_s")
            self.assertEqual(
                data1["experiments"][0]["COMROOT"],
                "gaeac6:/gpfs/f6/bil-fire10-oar/world-shared/jjh/rrfsv2/RRFSv2X/L60/OPSROOT/RRFSv2X.retv4/com/rrfs/v2.1.4",
            )
            self.assertNotIn("COMROOT", data1["experiments"][1])
            self.assertTrue(data1["pygraf slides"]["enable"])
            self.assertEqual(
                captured["warnings"],
                ["Warning: no pygraf graphics downloaded for 'existing_other' as hpc_dir is missing."],
            )

            # End-to-end check against real automats.cli.main: warning prints before "Saved slides to"
            out_dir = root / "results" / "baseline_c6_brand_new_exp"

            def fake_build_slides(plan, out_path, *, log=None, missing=None, graphics_stats=None):
                if graphics_stats is not None:
                    graphics_stats["saved"] = 0
                return out_path / "slides.pptx"

            combined_meta = io.StringIO()
            combined_slides_only = io.StringIO()
            err_only = io.StringIO()
            out_only = io.StringIO()
            with (
                patch.object(baselinec6, "ROOT", root),
                patch("automats.cli.run_plan", return_value=0),
                patch("automats.cli.build_slides", side_effect=fake_build_slides),
            ):
                with redirect_stdout(combined_meta), redirect_stderr(combined_meta):
                    rc2 = baselinec6.main(["brand_new_exp", "brand_s", "--metadata-only"])
                with redirect_stdout(combined_slides_only), redirect_stderr(combined_slides_only):
                    rc3 = baselinec6.main(["brand_new_exp", "brand_s", "--slides-only"])
                with redirect_stdout(out_only), redirect_stderr(err_only):
                    rc4 = baselinec6.main(["brand_new_exp", "brand_s", "--slides-only"])

            expected_warning = "Warning: no pygraf graphics downloaded for 'brand_new_exp' as hpc_dir is missing."
            expected_saved_slides = f"Saved slides to {out_dir.resolve() / 'slides.pptx'}"

            self.assertEqual(rc2, 0)
            self.assertTrue((out_dir / "baseline_c6_brand_new_exp.yaml").is_file())
            meta_lines = [line for line in combined_meta.getvalue().splitlines() if line.strip()]
            self.assertEqual(meta_lines[-2], expected_warning)
            self.assertEqual(meta_lines[-1], expected_saved_slides)
            self.assertIn(f"Saved 0 graphics under {out_dir.resolve() / 'graphics'}", meta_lines)

            self.assertEqual(rc3, 0)
            slides_only_lines = [line for line in combined_slides_only.getvalue().splitlines() if line.strip()]
            self.assertEqual(slides_only_lines[-2], expected_warning)
            self.assertEqual(slides_only_lines[-1], expected_saved_slides)

            self.assertEqual(rc4, 0)
            self.assertEqual(err_only.getvalue().splitlines()[-1], expected_warning)
            self.assertEqual(out_only.getvalue().splitlines()[-1], expected_saved_slides)

            # Failure exit code must not emit the omitted-hpc_dir warning
            err_fail = io.StringIO()
            with (
                patch.object(baselinec6, "ROOT", root),
                patch("automats.cli.load_plan", side_effect=RuntimeError("boom")),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err_fail),
            ):
                rc_fail = baselinec6.main(["brand_new_exp", "brand_s", "--slides-only"])
            self.assertEqual(rc_fail, 1)
            self.assertNotIn("no pygraf graphics downloaded", err_fail.getvalue())

            # Legacy 1-arg mock for automats_main still works cleanly
            legacy_err = io.StringIO()
            with (
                patch.object(baselinec6, "ROOT", root),
                patch.object(baselinec6, "automats_main", side_effect=lambda cli_args: 0),
                redirect_stderr(legacy_err),
            ):
                rc_legacy = baselinec6.main(["brand_new_exp", "brand_s", "--plan"])
            self.assertEqual(rc_legacy, 0)
            self.assertEqual(legacy_err.getvalue().strip(), expected_warning)

    def test_main_uses_baseline_hpc_dir_even_if_baseline_yaml_lacks_it(self):
        from unittest.mock import patch
        import yaml

        baselinec6 = self._import_baselinec6()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "recipes").mkdir(parents=True, exist_ok=True)
            (root / "recipes" / "baseline.yaml").write_text(
                "experiments:\n"
                "- name: unrelated\n"
                "  color: orange\n"
                "pygraf slides:\n"
                "  enable: true\n"
                "  recipe: pygraf_slides_layout2_May2024.yaml\n",
                encoding="utf-8",
            )
            captured = {}

            def fake_automats_main(cli_args):
                captured["args"] = cli_args
                return 0

            with (
                patch.object(baselinec6, "ROOT", root),
                patch.object(baselinec6, "automats_main", side_effect=fake_automats_main),
            ):
                rc = baselinec6.main(["my_exp", "my_s", "/gpfs/f7/some/com/rrfs/v2"])

            self.assertEqual(rc, 0)
            data = yaml.safe_load(Path(captured["args"][0]).read_text(encoding="utf-8"))
            self.assertEqual(data["experiments"][0]["MATS name"], baselinec6.BASELINE_EXPERIMENT)
            self.assertEqual(data["experiments"][0]["short name"], baselinec6.BASELINE_SHORT_NAME)
            self.assertEqual(data["experiments"][1]["short name"], "my_s")
            self.assertEqual(
                data["experiments"][0]["COMROOT"],
                baselinec6.resolve_comroot(baselinec6.BASELINE_HPC_DIR),
            )
            self.assertEqual(data["experiments"][1]["COMROOT"], "gaeac7:/gpfs/f7/some/com/rrfs/v2")

    def test_three_pygraf_slide_types_and_simplified_naming(self):
        from io import BytesIO
        from unittest.mock import patch
        from PIL import Image, ImageDraw
        from pptx import Presentation
        from automats.graphics import process_graphics_slides

        im = Image.new("RGB", (712, 549), color="white")
        draw = ImageDraw.Draw(im)
        draw.text((100, 20), "Composite Reflectivity (dBZ, shaded)", fill=(0, 0, 0))
        draw.line([(11, 67), (706, 67)], fill=(0, 0, 0), width=1)
        buf = BytesIO()
        im.save(buf, format="PNG")
        pygraf_png = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            gfx_recipe = tmp / "pygraf_slides_layout2.yaml"
            gfx_recipe.write_text(
                "pygraf slides:\n"
                "- type: same cycle same fhr\n"
                "  title: Composite Reflectivity\n"
                "  WGF: det\n"
                "  graphics name: cref_full_sfc_f006.png\n"
                "  cycle: 2024050700\n"
                "  number of rows: 1\n"
                "  row1: [first, second]\n"
                "- type: same cycle different fhr\n"
                "  title: Composite Reflectivity\n"
                "  WGF: det\n"
                "  experiment: second\n"
                "  cycle: 2024050700\n"
                "  number of rows: 1\n"
                "  row1: [cref_full_sfc_f001.pg]\n"
                "- type: different cycle same valid time\n"
                "  title: Composite Reflectivity\n"
                "  WGF: det\n"
                "  experiment: second\n"
                "  number of rows: 1\n"
                "  row1: [2024051009_cref_full_sfc_f002.pg]\n",
                encoding="utf-8",
            )
            plan = build_plan({
                "common": {"start": "2024-05-06T00:00:00Z", "end": "2024-05-07T00:00:00Z"},
                "experiments": [
                    {"name": "exp1", "color": "red", "COMROOT": "gaeac6:/gpfs/exp1/com/rrfs/v2"},
                    {"name": "exp2", "color": "blue", "COMROOT": "gaeac6:/gpfs/exp2/com/rrfs/v2"},
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
                "pygraf slides": {"enable": True, "recipe": str(gfx_recipe)},
            })

            deck = Presentation()
            with (
                patch("automats.graphics.test_hpc_session_ready", return_value=True),
                patch("automats.graphics.batch_pull_graphics_files", return_value={}),
            ):
                (tmp / "graphics" / "exp1" / "2024050700_cref_full_sfc_f006.png").parent.mkdir(parents=True, exist_ok=True)
                (tmp / "graphics" / "exp1" / "2024050700_cref_full_sfc_f006.png").write_bytes(pygraf_png)
                (tmp / "graphics" / "exp2" / "2024050700_cref_full_sfc_f006.png").parent.mkdir(parents=True, exist_ok=True)
                (tmp / "graphics" / "exp2" / "2024050700_cref_full_sfc_f006.png").write_bytes(pygraf_png)
                (tmp / "graphics" / "exp2" / "2024050700_cref_full_sfc_f001.png").parent.mkdir(parents=True, exist_ok=True)
                (tmp / "graphics" / "exp2" / "2024050700_cref_full_sfc_f001.png").write_bytes(pygraf_png)
                (tmp / "graphics" / "exp2" / "2024051009_cref_full_sfc_f002.png").parent.mkdir(parents=True, exist_ok=True)
                (tmp / "graphics" / "exp2" / "2024051009_cref_full_sfc_f002.png").write_bytes(pygraf_png)

                added = process_graphics_slides(plan, tmp, deck)

            self.assertEqual(added, 3)

            # Slide 1: same cycle same fhr
            s1_texts = [s.text for s in deck.slides[0].shapes if s.has_text_frame]
            self.assertTrue(any("Cycle: 2024050700 F006" in t for t in s1_texts))

            # Slide 2: same cycle different fhr
            s2_texts = [s.text for s in deck.slides[1].shapes if s.has_text_frame]
            self.assertTrue(any("Cycle: 2024050700" in t for t in s2_texts))
            self.assertIn("F001", s2_texts)

            # Slide 3: different cycle same valid time
            s3_texts = [s.text for s in deck.slides[2].shapes if s.has_text_frame]
            self.assertTrue(any("Valid Time: 20240510 11 UTC" in t for t in s3_texts))
            self.assertIn("Cycle: 2024051009 F002", s3_texts)


if __name__ == "__main__":
    unittest.main()
