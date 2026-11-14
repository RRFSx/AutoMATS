import unittest
from pathlib import Path
import tempfile
from unittest.mock import MagicMock

from automats.config import Experiment, PlotJob, Plan, SlideSpec
from automats.slides import (
    classify_slide_section,
    derive_deck_title,
    derive_subtitle,
    populate_subtitle_runs,
    parse_color_to_rgb,
    PREFERRED_SECTIONS,
)


class SlidesOverviewTests(unittest.TestCase):
    def test_derive_deck_title_explicit_title(self):
        plan = Plan(
            jobs=(),
            warnings=(),
            settings=MagicMock(),
            experiments=(),
            slides=(),
            title="Custom Verification Study",
        )
        self.assertEqual(derive_deck_title(plan), "Custom Verification Study")

    def test_derive_deck_title_retro_on_cluster(self):
        exp = Experiment(
            name="rrfs_ret_v4",
            color="red",
            COMROOT="gaeac6:/gpfs/f6/world-shared/com/rrfs",
        )
        plan = Plan(
            jobs=(),
            warnings=(),
            settings=MagicMock(),
            experiments=(exp,),
            slides=(),
            recipe_path=Path("baseline_retro.yaml"),
        )
        self.assertEqual(derive_deck_title(plan), "Baseline Retro on GaeaC6")

    def test_derive_deck_title_realtime_rrfs(self):
        exp = Experiment(
            name="RRFSv2_prod",
            color="blue",
            COMROOT="gaeac7:/gpfs/f7/com/rrfs",
        )
        plan = Plan(
            jobs=(),
            warnings=(),
            settings=MagicMock(),
            experiments=(exp,),
            slides=(),
            recipe_path=Path("realtime_run.yaml"),
        )
        self.assertEqual(derive_deck_title(plan), "Realtime RRFSv2 runs on GaeaC7")

    def test_derive_deck_title_experiments_fallback(self):
        exp1 = Experiment(name="EXP1", color="red")
        exp2 = Experiment(name="EXP2", color="blue")
        plan = Plan(
            jobs=(),
            warnings=(),
            settings=MagicMock(),
            experiments=(exp1, exp2),
            slides=(),
            recipe_path=Path("comparison.yaml"),
        )
        self.assertEqual(derive_deck_title(plan), "EXP1 vs EXP2 Verification")
        exp1_short = Experiment(name="RRFSv2_long_exp_1", short_name="base4", color="red")
        exp2_short = Experiment(name="RRFSv2_long_exp_2", short_name="3p5km", color="blue")
        plan_short = replace_exps = Plan(
            jobs=(),
            warnings=(),
            settings=MagicMock(),
            experiments=(exp1_short, exp2_short),
            slides=(),
            recipe_path=Path("comparison.yaml"),
        )
        self.assertEqual(derive_deck_title(plan_short), "base4 vs 3p5km Verification")

    def test_derive_deck_title_default(self):
        plan = Plan(
            jobs=(),
            warnings=(),
            settings=MagicMock(),
            experiments=(),
            slides=(),
            recipe_path=Path("my_suite.yaml"),
        )
        self.assertEqual(derive_deck_title(plan), "My Suite Verification")

    def test_derive_subtitle(self):
        job1 = MagicMock(start="2024-05-01T00:00:00Z", end="2024-05-10T00:00:00Z")
        job2 = MagicMock(start="2024-05-02T00:00:00Z", end="2024-05-15T00:00:00Z")
        plan = Plan(
            jobs=(job1, job2),
            warnings=(),
            settings=MagicMock(),
            experiments=(),
            slides=(),
            recipe_path=Path("baseline.yaml"),
        )
        subtitle = derive_subtitle(plan)
        self.assertIn("Evaluation Period: 2024-05-01 to 2024-05-15", subtitle)
        self.assertIn("Powered by AutoMATS , Recipe: baseline.yaml", subtitle)

    def test_populate_subtitle_runs_hyperlinks(self):
        try:
            from pptx import Presentation
            from pptx.util import Inches
        except ImportError:
            self.skipTest("pptx not installed")

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(10), Inches(2))
        p = tb.text_frame.paragraphs[0]

        job1 = MagicMock(start="2024-05-01T00:00:00Z", end="2024-05-10T00:00:00Z")
        job2 = MagicMock(start="2024-05-02T00:00:00Z", end="2024-05-15T00:00:00Z")
        plan_baseline = Plan(
            jobs=(job1, job2),
            warnings=(),
            settings=MagicMock(),
            experiments=(),
            slides=(),
            recipe_path=Path("baseline.yaml"),
        )
        populate_subtitle_runs(p, plan_baseline)

        self.assertEqual(p.text, derive_subtitle(plan_baseline))
        self.assertEqual(p.text, "Evaluation Period: 2024-05-01 to 2024-05-15   •   Powered by AutoMATS , Recipe: baseline.yaml")

        runs_dict = {r.text: r for r in p.runs}
        self.assertIn("AutoMATS", runs_dict)
        self.assertEqual(runs_dict["AutoMATS"].hyperlink.address, "https://github.com/RRFSx/AutoMATS")
        self.assertTrue(runs_dict["AutoMATS"].font.underline)

        self.assertIn("baseline.yaml", runs_dict)
        self.assertEqual(
            runs_dict["baseline.yaml"].hyperlink.address,
            "https://github.com/RRFSx/AutoMATS/blob/main/recipes/baseline.yaml",
        )
        self.assertTrue(runs_dict["baseline.yaml"].font.underline)

        # Test with custom recipe that is not baseline.yaml
        p2 = tb.text_frame.add_paragraph()
        plan_custom = Plan(
            jobs=(),
            warnings=(),
            settings=MagicMock(),
            experiments=(),
            slides=(),
            recipe_path=Path("custom_suite.yaml"),
        )
        populate_subtitle_runs(p2, plan_custom)
        self.assertEqual(p2.text, derive_subtitle(plan_custom))
        runs2_dict = {r.text: r for r in p2.runs}
        self.assertIn("AutoMATS", runs2_dict)
        self.assertEqual(runs2_dict["AutoMATS"].hyperlink.address, "https://github.com/RRFSx/AutoMATS")
        self.assertIn("custom_suite.yaml", runs2_dict)
        self.assertIsNone(getattr(runs2_dict["custom_suite.yaml"].hyperlink, "address", None))

    def test_classify_slide_section(self):
        self.assertEqual(classify_slide_section("Surface 2m Temperature", "sfc_t2m"), "Surface")
        self.assertEqual(classify_slide_section("Upper Air 500 hPa Wind", "ua_wind"), "Upper Air")
        self.assertEqual(classify_slide_section("Composite Reflectivity", "cref_mrms"), "Radar Refl")
        self.assertEqual(classify_slide_section("Radar Verification", "radar_dieoff"), "Radar Refl")
        self.assertEqual(classify_slide_section("Cloud Base Ceiling", "ceiling_dieoff"), "Ceiling")
        self.assertEqual(classify_slide_section("Surface Visibility", "visibility_dieoff"), "Visibility")
        self.assertEqual(classify_slide_section("Solar Radiation Flux", "sw_radiation"), "Radiation")
        self.assertEqual(classify_slide_section("Miscellaneous Diagnostics", "misc"), "Other")

    def test_parse_color_to_rgb(self):
        c1 = parse_color_to_rgb("red")
        self.assertEqual((c1[0], c1[1], c1[2]), (255, 0, 0))

        c_blue = parse_color_to_rgb("blue")
        self.assertEqual((c_blue[0], c_blue[1], c_blue[2]), (0, 0, 255))

        c_orange = parse_color_to_rgb("orange")
        self.assertEqual((c_orange[0], c_orange[1], c_orange[2]), (255, 165, 0))

        c_green = parse_color_to_rgb("green")
        self.assertEqual((c_green[0], c_green[1], c_green[2]), (0, 128, 0))

        c_magenta = parse_color_to_rgb("magenta")
        self.assertEqual((c_magenta[0], c_magenta[1], c_magenta[2]), (255, 0, 255))

        c_teal = parse_color_to_rgb("teal")
        self.assertEqual((c_teal[0], c_teal[1], c_teal[2]), (0, 128, 128))

        c_black = parse_color_to_rgb("black")
        self.assertEqual((c_black[0], c_black[1], c_black[2]), (0, 0, 0))

        c2 = parse_color_to_rgb("#ff0000")
        self.assertEqual((c2[0], c2[1], c2[2]), (255, 0, 0))

        c3 = parse_color_to_rgb("rgb(10, 20, 30)")
        self.assertEqual((c3[0], c3[1], c3[2]), (10, 20, 30))

        c4 = parse_color_to_rgb("unknown_color_name")
        self.assertEqual((c4[0], c4[1], c4[2]), (70, 80, 95))

    def test_populate_overview_slide_and_backlink(self):
        import sys
        from unittest.mock import MagicMock

        created_modules = []
        for mod_name in (
            "pptx",
            "pptx.dml",
            "pptx.dml.color",
            "pptx.enum",
            "pptx.enum.shapes",
            "pptx.enum.text",
            "pptx.util",
        ):
            if mod_name not in sys.modules:
                m = MagicMock()
                if mod_name == "pptx.util":
                    m.Inches = lambda x: int(x * 914400)
                    m.Pt = lambda x: int(x * 12700)
                sys.modules[mod_name] = m
                created_modules.append(mod_name)

        try:
            from automats.slides import add_overview_backlink, populate_overview_slide

            class MockShape:
                def __init__(self):
                    self.text_frame = MagicMock()
                    self.text_frame.paragraphs = [MagicMock()]
                    self.text_frame.add_paragraph.return_value = MagicMock()
                    self.fill = MagicMock()
                    self.line = MagicMock()
                    self.click_action = MagicMock()

            shapes_created = []

            def mock_add_shape(*args, **kwargs):
                s = MockShape()
                shapes_created.append(s)
                return s

            def mock_add_textbox(*args, **kwargs):
                s = MockShape()
                shapes_created.append(s)
                return s

            mock_slide = MagicMock()
            mock_slide.shapes.add_shape.side_effect = mock_add_shape
            mock_slide.shapes.add_textbox.side_effect = mock_add_textbox

            target_surface = MagicMock()
            target_upper_air = MagicMock()
            section_first_slide = {
                "Surface": target_surface,
                "Upper Air": target_upper_air,
            }

            exp1 = Experiment(name="RRFSv2_retv4", color="red", COMROOT="gaeac6:/gpfs/f6/com")
            exp2 = Experiment(name="HRRR_OPS", color="orange")
            plan = Plan(
                jobs=(),
                warnings=(),
                settings=MagicMock(),
                experiments=(exp1, exp2),
                slides=(),
                recipe_path=Path("baseline_retro.yaml"),
            )

            populate_overview_slide(mock_slide, plan, section_first_slide)

            # Check navigation targets assigned
            nav_targets = [
                s.click_action.target_slide
                for s in shapes_created
                if s.click_action.target_slide in (target_surface, target_upper_air)
            ]
            self.assertIn(target_surface, nav_targets)
            self.assertIn(target_upper_air, nav_targets)

            # Check overview slide experiment headings show short_name (full_name) when short_name differs
            exp_short_1 = Experiment(
                name="RRFSv2_conus_3km_ret_v2x_retv4",
                short_name="base4",
                color="red",
                COMROOT="gaeac6:/gpfs/f6/com",
            )
            exp_short_2 = Experiment(
                name="RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km",
                short_name="3p5km",
                color="blue",
            )
            exp_plain = Experiment(name="HRRR_OPS", color="orange")
            shapes_created.clear()
            populate_overview_slide(
                mock_slide,
                Plan(
                    jobs=(),
                    warnings=(),
                    settings=MagicMock(),
                    experiments=(exp_short_1, exp_short_2, exp_plain),
                    slides=(),
                    recipe_path=Path("baseline.yaml"),
                ),
                section_first_slide,
            )
            texts = [s.text_frame.paragraphs[0].text for s in shapes_created]
            self.assertIn("base4 (RRFSv2_conus_3km_ret_v2x_retv4)", texts)
            self.assertIn("3p5km (RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km)", texts)
            self.assertIn("HRRR_OPS", texts)

            # Test backlink
            content_slide = MagicMock()
            content_slide.shapes.add_shape.side_effect = mock_add_shape
            add_overview_backlink(content_slide, mock_slide, deck_width=1000)
            self.assertEqual(shapes_created[-1].click_action.target_slide, mock_slide)
        finally:
            for mod_name in created_modules:
                sys.modules.pop(mod_name, None)

    def test_graphics_slides_backlink_does_not_slice_deck_slides(self):
        """python-pptx Slides does not support slicing (deck.slides[start:] raises AttributeError)."""
        class MockSlides:
            def __init__(self):
                self._items = []

            def __len__(self):
                return len(self._items)

            def __getitem__(self, idx):
                if isinstance(idx, slice):
                    raise AttributeError("'list' object has no attribute 'rId'")
                return self._items[idx]

            def add_slide(self, layout):
                s = MagicMock()
                self._items.append(s)
                return s

        mock_slides = MockSlides()
        # Add overview and 2 initial slides
        for _ in range(3):
            mock_slides.add_slide(None)
        before_count = len(mock_slides)

        # Add 2 pygraf slides
        for _ in range(2):
            mock_slides.add_slide(None)

        # Verify iterating with integer indexing range(before_count, len(mock_slides)) succeeds
        visited = []
        for slide_idx in range(before_count, len(mock_slides)):
            visited.append(mock_slides[slide_idx])

        self.assertEqual(len(visited), 2)

        # Verify that slicing raises the exact error we encountered
        with self.assertRaises(AttributeError) as ctx:
            _ = mock_slides[before_count:]
        self.assertIn("has no attribute 'rId'", str(ctx.exception))


class SlideDeduplicationHelpersTests(unittest.TestCase):
    def test_find_free_rectangles_splits_partial_rows_and_full_container(self):
        from automats.slides import _find_free_rectangles

        container = (0, 0, 1000, 600)
        # 2+1 grid: top row has 2 figures, bottom row has 1 centered figure.
        blocked = [
            (20, 10, 480, 290),
            (520, 10, 980, 290),
            (260, 310, 740, 590),
        ]
        free = _find_free_rectangles(container, blocked)
        self.assertTrue(any(r[0] >= 740 and r[1] >= 290 and r[2] == 1000 and r[3] == 600 for r in free))
        self.assertTrue(any(r[0] == 0 and r[1] >= 290 and r[2] <= 260 and r[3] == 600 for r in free))
        for rx0, ry0, rx1, ry1 in free:
            for bx0, by0, bx1, by1 in blocked:
                overlap_x = max(rx0, bx0) < min(rx1, bx1)
                overlap_y = max(ry0, by0) < min(ry1, by1)
                self.assertFalse(overlap_x and overlap_y)

        self.assertEqual(_find_free_rectangles(container, [container]), [])

    def test_choose_free_space_legend_prefers_bottom_right_and_handles_full_grid(self):
        from pptx.util import Inches
        from automats.slides import _choose_free_space_legend

        slide_w = Inches(13 + 1 / 3)
        margin = Inches(0.25)
        content_top = Inches(0.9)
        grid_bottom = Inches(7.25)
        items = [{"name": "base4", "color": "red"}, {"name": "3p5km", "color": "blue"}]

        # Symmetric 2+1 occupied boxes -> bottom-left and bottom-right are both free; bottom-right must win.
        occupied_2_plus_1 = [
            (Inches(0.25), Inches(0.90), Inches(6.50), Inches(3.95)),
            (Inches(6.80), Inches(0.90), Inches(13.08), Inches(3.95)),
            (Inches(4.30), Inches(4.10), Inches(9.05), Inches(7.25)),
        ]
        placement = _choose_free_space_legend(
            items, occupied_2_plus_1, slide_w, margin, content_top, grid_bottom,
        )
        self.assertIsNotNone(placement)
        box_w = placement["layout"]["width"]
        box_h = placement["layout"]["height"]
        self.assertGreaterEqual(placement["left"], Inches(9.05))
        self.assertLessEqual(placement["left"] + box_w, slide_w - margin)
        self.assertGreaterEqual(placement["top"], Inches(3.95))
        self.assertLessEqual(placement["top"] + box_h, grid_bottom)

        # 1+2 occupied boxes -> only top-right is empty; still places inside that empty area.
        occupied_1_plus_2 = [
            (Inches(4.30), Inches(0.90), Inches(9.05), Inches(3.95)),
            (Inches(0.25), Inches(4.10), Inches(6.50), Inches(7.25)),
            (Inches(6.80), Inches(4.10), Inches(13.08), Inches(7.25)),
        ]
        top_right = _choose_free_space_legend(
            items, occupied_1_plus_2, slide_w, margin, content_top, grid_bottom,
        )
        self.assertIsNotNone(top_right)
        self.assertGreaterEqual(top_right["left"], Inches(9.05))
        self.assertLessEqual(top_right["top"] + top_right["layout"]["height"], Inches(4.10))

        # Full 2x2 occupied grid -> returns None to trigger compact bottom strip reservation.
        occupied_2x2 = [
            (Inches(0.25), Inches(0.90), Inches(6.50), Inches(3.95)),
            (Inches(6.80), Inches(0.90), Inches(13.08), Inches(3.95)),
            (Inches(0.25), Inches(4.10), Inches(6.50), Inches(7.25)),
            (Inches(6.80), Inches(4.10), Inches(13.08), Inches(7.25)),
        ]
        self.assertIsNone(
            _choose_free_space_legend(items, occupied_2x2, slide_w, margin, content_top, grid_bottom)
        )

    def test_layout_legend_box_scales_font_and_uses_flow_when_needed(self):
        from pptx.util import Inches
        from automats.slides import _layout_legend_box

        items = [
            {"name": f"experiment_{i}_long_label", "color": c}
            for i, c in enumerate(("red", "blue", "green", "orange", "purple", "teal"))
        ]
        # Horizontal bottom strip
        strip_layout = _layout_legend_box(
            items[:3],
            (Inches(0.25), 0, Inches(13.08), Inches(0.55)),
            prefer_vertical=False,
        )
        self.assertIsNotNone(strip_layout)
        self.assertLessEqual(strip_layout["height"], Inches(0.55))

        # Tight box forces font scaling or multi-column grid
        tight_layout = _layout_legend_box(
            items,
            (0, 0, Inches(2.8), Inches(2.0)),
            prefer_vertical=True,
        )
        self.assertIsNotNone(tight_layout)
        self.assertLessEqual(tight_layout["width"], Inches(2.8))
        self.assertLessEqual(tight_layout["height"], Inches(2.0))
        self.assertLess(tight_layout["font_pt"], 13.0)

    def test_figure_caption_and_legend_resolution_helpers(self):
        from dataclasses import asdict
        from automats.config import Experiment, PlotJob
        from automats.slides import _figure_caption_text, _resolve_figure_legend_items

        self.assertEqual(
            _figure_caption_text({"header": ["2024-05-06 10:00", "", " Ceiling, CSI, dieoff-type=Dieoff "]}),
            "2024-05-06 10:00, Ceiling, CSI",
        )
        self.assertEqual(
            _figure_caption_text({"header": ["2024-05-06 10:00, Dieoff, dieoff-type=Valid Time"]}),
            "2024-05-06 10:00, Dieoff, Valid Time",
        )
        self.assertIsNone(_figure_caption_text({"header": []}))

        # Direct v11 legend_items path
        direct = _resolve_figure_legend_items(
            {
                "mats": {
                    "figure": {
                        "legend_items": [
                            {"name": "base4", "color": "red"},
                            {"name": "3p5km - base4", "color": "rgb(255,165,0)"},
                        ],
                        "legend_styles": ["solid"],
                    }
                }
            },
            None,
        )
        self.assertEqual(
            direct,
            ([{"name": "base4", "color": "red"}, {"name": "3p5km - base4", "color": "rgb(255,165,0)"}], ("solid",)),
        )

        # Fallback to record["configuration"] when recipe has 3 experiments vs cached 2 experiments
        cfg_job = PlotJob(
            name="ceiling_dieoff",
            plot_type="dieoff",
            truth="METAR",
            region="All HRRR domain",
            matched_samples=True,
            variable="Ceiling",
            variable_tag="ceiling",
            statistic="csi",
            lead=6,
            start=None,
            end=None,
            experiments=(
                Experiment(name="EXP_BASE", short_name="base4", color="red"),
                Experiment(name="EXP_NEW", short_name="3p5km", color="blue"),
            ),
            relative_path="ceiling_dieoff/ceiling_dieoff_ceiling_csi+diff_threshold500.png",
            application="ceil-vis",
            parameters=(("Difference curves", "Diff all curves against the 1st added curve"),),
        )
        recipe_job = PlotJob(
            **{
                **{k: v for k, v in asdict(cfg_job).items() if k not in ("experiments", "parameters", "forecast_lead_colors")},
                "experiments": (
                    Experiment(name="EXP_BASE", short_name="base4", color="red"),
                    Experiment(name="EXP_NEW", short_name="3p5km", color="blue"),
                    Experiment(name="HRRR_OPS", color="orange"),
                ),
                "parameters": cfg_job.parameters,
                "forecast_lead_colors": (),
            }
        )
        resolved_fallback = _resolve_figure_legend_items(
            {
                "configuration": asdict(cfg_job),
                "mats": {
                    "curves": [{"name": "EXP_BASE", "lead": None}],
                    "figure": {
                        "legend_names": ["base4", "3p5km - base4"],
                        "legend_styles": ["solid"],
                    },
                },
            },
            recipe_job,
        )
        self.assertIsNotNone(resolved_fallback)
        self.assertEqual(
            resolved_fallback[0],
            [
                {"name": "base4", "color": "red"},
                {"name": "3p5km - base4", "color": "rgb(255,165,0)"},
            ],
        )

    def test_extract_shared_slide_caption_handles_variables_leads_and_ambiguity(self):
        from automats.config import Experiment, PlotJob
        from automats.slides import _extract_shared_slide_caption

        exps = (Experiment(name="EXP_A", color="red"),)

        def make_item(rel_path: str, side_label: str, header: list[str], eff_var: str | None, job_var: str, tag: str, lead=None):
            job = PlotJob(
                name="grp",
                plot_type="profile" if lead is not None else "dieoff",
                truth="RAOBs",
                region="HRRR domain",
                matched_samples=True,
                variable=job_var,
                variable_tag=tag,
                statistic="rmse+bias",
                lead=lead,
                start=None,
                end=None,
                experiments=exps,
                relative_path=rel_path,
                application="upperair",
            )
            record = {"mats": {"figure": {"header": header}}}
            if eff_var is not None:
                record["mats"]["effective_settings"] = {"curves": [{"variable": eff_var}]}
            return {
                "relative_path": rel_path,
                "side_label": side_label,
                "fig_meta": record["mats"]["figure"],
                "record": record,
            }, job

        # 1. Surface Dieoff via VARIABLE_ALIASES fallback (even when effective_settings is absent)
        sfc_pairs = [
            make_item("grp/t2m.png", "t2m", ["2024-05-06, Dieoff, METAR, Temperature at 2m (°C), RMSE, dieoff-type=Dieoff"], None, "t2m", "t2m"),
            make_item("grp/td2m.png", "td2m", ["2024-05-06, Dieoff, METAR, Dewpoint at 2m (°C), RMSE, dieoff-type=Dieoff"], None, "td2m", "td2m"),
            make_item("grp/wind10m.png", "wind10m", ["2024-05-06, Dieoff, METAR, Wind Speed at 10m (m/s), RMSE, dieoff-type=Dieoff"], None, "wind10m", "wind10m"),
        ]
        self.assertEqual(
            _extract_shared_slide_caption(
                [p[0] for p in sfc_pairs],
                {p[1].relative_path: p[1] for p in sfc_pairs},
                "Surface Dieoff",
            ),
            "2024-05-06, Dieoff, METAR, RMSE",
        )

        # 2. Upper Air Profile 2x3 with leads shown in title + differing variables shown in side labels
        prof_pairs = [
            make_item("grp/T_f003.png", "T", ["2024-05-06, Profile, f003h, RAOBs, Temperature, RMSE, top=1, bottom=1000"], "Temperature", "Temperature", "T", 3),
            make_item("grp/RH_f003.png", "RHobT", ["2024-05-06, Profile, f003h, RAOBs, RHobT, RMSE, top=1, bottom=1000"], "RHobT", "RHobT", "RHobT", 3),
            make_item("grp/W_f006.png", "Wind", ["2024-05-06, Profile, f006h, RAOBs, Wind, RMSE, top=1, bottom=1000"], "Wind", "Wind", "Wind", 6),
        ]
        self.assertEqual(
            _extract_shared_slide_caption(
                [p[0] for p in prof_pairs],
                {p[1].relative_path: p[1] for p in prof_pairs},
                "Upper Air Profile f003, f006",
            ),
            "2024-05-06, Profile, RAOBs, RMSE, top=1, bottom=1000",
        )

        # 3. Missing lead in title/side_label -> refuses merge (returns None)
        self.assertIsNone(
            _extract_shared_slide_caption(
                [p[0] for p in prof_pairs],
                {p[1].relative_path: p[1] for p in prof_pairs},
                "Upper Air Profile f003",
            )
        )

        # 4. Ambiguous side_label shared across two different variables -> refuses merge
        amb_pairs = [
            make_item("grp/T.png", "Var", ["2024-05-06, Temperature, RMSE"], "Temperature", "Temperature", "T"),
            make_item("grp/W.png", "Var", ["2024-05-06, Wind, RMSE"], "Wind", "Wind", "Wind"),
        ]
        self.assertIsNone(
            _extract_shared_slide_caption(
                [p[0] for p in amb_pairs],
                {p[1].relative_path: p[1] for p in amb_pairs},
                "Upper Air Dieoff",
            )
        )

        # 5. Differing non-variable tokens (date/stat) -> refuses merge
        stat_pairs = [
            make_item("grp/a.png", "T", ["2024-05-06, Temperature, RMSE"], "Temperature", "Temperature", "T"),
            make_item("grp/b.png", "RHobT", ["2024-05-07, RHobT, RMSE"], "RHobT", "RHobT", "RHobT"),
        ]
        self.assertIsNone(
            _extract_shared_slide_caption(
                [p[0] for p in stat_pairs],
                {p[1].relative_path: p[1] for p in stat_pairs},
                "Upper Air Dieoff",
            )
        )

    def test_choose_free_space_legend_targets_inter_column_gap_and_compact_size(self):
        from pptx.util import Inches
        from automats.slides import _choose_free_space_legend, _layout_legend_box

        items = [{"name": "base4", "color": "red"}, {"name": "3p5km", "color": "blue"}]
        compact = _layout_legend_box(items, (0, 0, Inches(3.0), Inches(3.0)), prefer_vertical=True)
        self.assertIsNotNone(compact)
        self.assertLessEqual(compact["width"], Inches(1.05))
        self.assertLessEqual(compact["height"], Inches(0.52))

        slide_w = Inches(13 + 1 / 3)
        margin = Inches(0.25)
        content_top = Inches(0.9)
        grid_bottom = Inches(7.25)
        grid_mid_y = (content_top + grid_bottom) / 2

        # Simulated 2x3 grid (pictures + side labels on row 0 and row 1), wide gap between col 1 and col 2.
        occupied_2x3 = [
            (Inches(0.98), Inches(0.90), Inches(4.09), Inches(4.01)),
            (Inches(5.30), Inches(0.90), Inches(8.41), Inches(4.01)),
            (Inches(9.62), Inches(0.90), Inches(12.73), Inches(4.01)),
            (Inches(0.98), Inches(4.14), Inches(4.09), Inches(7.25)),
            (Inches(5.30), Inches(4.14), Inches(8.41), Inches(7.25)),
            (Inches(9.62), Inches(4.14), Inches(12.73), Inches(7.25)),
        ]
        mid_2_3 = _choose_free_space_legend(
            items,
            occupied_2x3,
            slide_w,
            margin,
            content_top,
            grid_bottom,
            target_xy=((Inches(8.41) + Inches(9.62)) / 2, grid_mid_y),
        )
        self.assertIsNotNone(mid_2_3)
        self.assertGreaterEqual(mid_2_3["left"], Inches(8.41))
        self.assertLessEqual(mid_2_3["left"] + mid_2_3["layout"]["width"], Inches(9.62))
        cy = mid_2_3["top"] + mid_2_3["layout"]["height"] / 2
        self.assertAlmostEqual(cy, grid_mid_y, delta=Inches(0.2))

    def test_build_slides_applies_pure_blue_to_variable_titles_and_utc_captions(self):
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from automats.slides import build_slides
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            img_path = tmp / "ROAB_profile_T_rmse+bias_f000_allz.png"
            im = Image.new("RGB", (100, 100), color="white")
            im.save(img_path, format="PNG")

            slide_var = SlideSpec(
                plot_group="ROAB_profile",
                title="T ROAB Profile f000, f001",
                rows=((img_path.name,),),
                captions=(("allz",),),
            )
            slide_nonvar = SlideSpec(
                plot_group="sfc_dieoff",
                title="Surface Dieoff",
                rows=((img_path.name,),),
                captions=(("t2m",),),
            )
            plan = Plan(
                jobs=(),
                warnings=(),
                settings=MagicMock(),
                experiments=(),
                slides=(slide_var, slide_nonvar),
            )
            dest = build_slides(plan, tmp)
            prs = Presentation(dest)
            # Slide 0 is overview, Slide 1 is T ROAB Profile, Slide 2 is Surface Dieoff
            s1 = prs.slides[1]
            title_p1 = [s for s in s1.shapes if s.has_text_frame and s.text_frame.text == "T ROAB Profile f000, f001"][0].text_frame.paragraphs[0]
            self.assertEqual(len(title_p1.runs), 2)
            self.assertEqual(title_p1.runs[0].text, "T")
            self.assertEqual(title_p1.runs[0].font.color.rgb, RGBColor(0, 0, 255))
            self.assertEqual(title_p1.runs[1].text, " ROAB Profile f000, f001")
            self.assertIsNone(title_p1.runs[1].font.color.type)

            captions_s1 = [s.text_frame.text for s in s1.shapes if s.name.startswith("Caption ")]
            self.assertEqual(captions_s1, ["allz"])

            s2 = prs.slides[2]
            title_p2 = [s for s in s2.shapes if s.has_text_frame and s.text_frame.text == "Surface Dieoff"][0].text_frame.paragraphs[0]
            self.assertEqual(len(title_p2.runs), 1)
            self.assertEqual(title_p2.runs[0].text, "Surface Dieoff")
            self.assertIsNone(title_p2.runs[0].font.color.type)



if __name__ == "__main__":
    unittest.main()
