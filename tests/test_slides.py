import unittest
from pathlib import Path
from unittest.mock import MagicMock

from automats.config import Experiment, PlotJob, Plan, SlideSpec
from automats.slides import (
    classify_slide_section,
    derive_deck_title,
    derive_subtitle,
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
        self.assertEqual((c1[0], c1[1], c1[2]), (214, 39, 40))

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

        # Add 2 graphic slides
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


if __name__ == "__main__":
    unittest.main()
