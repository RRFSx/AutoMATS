from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import yaml

from automats.config import (
    ConfigError,
    DEFAULT_DESCRIPTION_FONT_SIZE,
    DEFAULT_LEGEND_FONT_SIZE,
    build_plan,
    copy_used_recipes,
    load_plan,
)


def example():
    return {
        "common": {"start": "2026-10-01T00:00:00Z", "end": "2026-10-15T00:00:00Z"},
        "experiments": [
            {"name": "RRFSv2x", "color": "red", "overlay": True},
            {"name": "RRFSv2b", "color": "blue", "overlay": True},
        ],
        "automation": {"base_url": "https://gsl.noaa.gov/int-mats/"},
        "plots": [{"name": "ua_profile", "application": "Upper Air", "plot_type": "dieoff",
                   "truth": "RAOBs (Traditional)", "region": "HRRR domain", "matched_samples": True,
                   "variable": ["Temperature", "RHobT", "Wind"], "statistic": ["rmse", "bias"],
                   "forecast lead time": [0, 1, 2, 3, 6, 9, 12]}],
    }


class ConfigTests(unittest.TestCase):
    def test_slides_disambiguate_captions_using_second_section(self):
        raw = example()
        raw["plots"] = [
            {
                "name": "radar_dieoff",
                "application": "Radar (MRMS)",
                "plot_type": "die off",
                "region": "Continental US",
                "matched_samples": True,
                "variable": ["Composite Reflectivity"],
                "Threshold": [25, 35, 45],
                "statistic": ["CSI", "BIAS"],
            },
            {
                "name": "ceiling_dieoff",
                "application": "Ceiling and Visibility",
                "plot_type": "die off",
                "region": "All HRRR domain",
                "matched_samples": True,
                "variable": ["ceiling"],
                "Threshold": [500, 1000, 3000, 6000],
                "statistic": ["CSI", "BIAS"],
            },
            {
                "name": "visibility_dieoff",
                "application": "Ceiling and Visibility",
                "plot_type": "die off",
                "region": "All HRRR domain",
                "matched_samples": True,
                "variable": ["visibility"],
                "Threshold": [1, 3, 5, 10],
                "statistic": ["CSI", "BIAS"],
            },
        ]
        raw["slides"] = [
            {
                "plot group": "radar_dieoff",
                "title": "RADAR MRMS Dieoff CSI",
                "number of rows": 2,
                "row1": ["csi_threshold25", "csi_threshold35"],
                "row2": ["csi_threshold45"],
            },
            {
                "plot group": "ceiling_dieoff",
                "title": "Ceiling Dieoff CSI",
                "number of rows": 2,
                "row1": ["csi_threshold500", "csi_threshold1000"],
                "row2": ["csi_threshold3000", "csi_threshold6000"],
            },
            {
                "plot group": "ceiling_dieoff",
                "title": "Ceiling Dieoff BIAS",
                "number of rows": 2,
                "row1": ["bias_threshold500", "bias_threshold1000"],
                "row2": ["bias_threshold3000", "bias_threshold6000"],
            },
            {
                "plot group": "visibility_dieoff",
                "title": "Visibility Dieoff CSI",
                "number of rows": 2,
                "row1": ["csi_threshold1", "csi_threshold3"],
                "row2": ["csi_threshold5", "csi_threshold10"],
            },
            {
                "plot group": "visibility_dieoff",
                "title": "Visibility Dieoff BIAS",
                "number of rows": 2,
                "row1": ["bias_threshold1", "bias_threshold3"],
                "row2": ["bias_threshold5", "bias_threshold10"],
            },
        ]
        plan = build_plan(raw)
        self.assertEqual(len(plan.slides), 5)
        self.assertEqual(
            plan.slides[0].captions,
            ((">25 dBZ", ">35 dBZ"), (">45 dBZ",)),
        )
        self.assertEqual(
            plan.slides[1].captions,
            (("<500 ft", "<1000 ft"), ("<3000 ft", "<6000 ft")),
        )
        self.assertEqual(
            plan.slides[2].captions,
            (("<500 ft", "<1000 ft"), ("<3000 ft", "<6000 ft")),
        )
        self.assertEqual(
            plan.slides[3].captions,
            (("<1 mile", "<3 miles"), ("<5 miles", "<10 miles")),
        )
        self.assertEqual(
            plan.slides[4].captions,
            (("<1 mile", "<3 miles"), ("<5 miles", "<10 miles")),
        )

    def test_slides_resolve_rows_exactly_before_unique_partial_matches(self):
        raw = example()
        raw["plots"][0].update({"name": "ua_dieoff", "plot_type": "dieoff",
                                "statistic": ["rmse+bias"]})
        raw["plots"][0].pop("forecast lead time")
        raw["slides"] = [{"plot group": "UA_DIEOFF", "number of rows": 2,
                          "title": "Upper Air Verification",
                          "row1": ["T_rmse+bias", "rhobt_rmse+bias"], "row2": ["Wind_rmse"]}]
        plan = build_plan(raw)
        slide = plan.slides[0]
        self.assertEqual(slide.plot_group, "ua_dieoff")
        self.assertEqual(slide.title, "Upper Air Verification")
        self.assertEqual(slide.rows, (
            ("ua_dieoff/ua_dieoff_T_rmse+bias.png",
             "ua_dieoff/ua_dieoff_RHobT_rmse+bias.png"),
            ("ua_dieoff/ua_dieoff_Wind_rmse+bias.png",)))
        self.assertEqual(slide.captions, (("T", "rhobt"), ("Wind",)))
        raw["slides"][0].pop("title")
        self.assertEqual(build_plan(raw).slides[0].title, "ua_dieoff")
        raw["slides"][0].update({"row1": ["T", "RHobT"], "row2": ["Wind"]})
        short = build_plan(raw).slides[0]
        self.assertEqual(short.rows, slide.rows)
        self.assertEqual(short.captions, (("T", "RHobT"), ("Wind",)))

    def test_slides_reject_ambiguous_figures_and_accept_specific_leads(self):
        raw = example()
        raw["slides"] = [{"plot group": "ua_profile", "number of rows": 1, "row1": ["T_rmse"]}]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 42)
        self.assertEqual(plan.slides, ())
        self.assertIn("ambiguous figure 't_rmse'", plan.slide_error)
        raw["slides"][0]["row1"] = ["T_rmse_f000"]
        self.assertEqual(build_plan(raw).slides[0].rows,
                         (("ua_profile/ua_profile_T_rmse_f000.png",),))
        raw["slides"][0]["row1"] = ["ua_profile_T_rmse_f001.png"]
        self.assertEqual(build_plan(raw).slides[0].rows,
                         (("ua_profile/ua_profile_T_rmse_f001.png",),))
        self.assertEqual(build_plan(raw).slides[0].captions, (("T",),))

    def test_slides_reject_invalid_layout_and_missing_references(self):
        base = {"plot group": "ua_profile", "number of rows": 1, "row1": ["T_rmse_f000"]}
        for change in ({"number of rows": 0}, {"number of rows": True}, {"number of rows": "2"},
                       {"number of rows": 2}, {"row1": []}, {"row1": "T_rmse_f000"},
                       {"row1": [None]}, {"row1": ["missing"]}, {"row2": ["Wind"]},
                       {"plot group": "missing"}, {"title": ""}, {"number or rows": 1}):
            raw = example()
            raw["slides"] = [{**base, **change}]
            with self.subTest(change=change):
                plan = build_plan(raw)
                self.assertEqual(len(plan.jobs), 42)
                self.assertEqual(plan.slides, ())
                self.assertIsNotNone(plan.slide_error)
        for value in (None, {}, "slides"):
            raw["slides"] = value
            with self.subTest(value=value):
                self.assertEqual(build_plan(raw).slide_error, "slides must be a list")
        self.assertEqual(build_plan(example()).slides, ())

    def test_unknown_root_keys_are_ignored_without_changing_plots(self):
        raw = example()
        original = build_plan(raw)
        raw.update({"notes": {"anything": [1, 2, 3]}, "dates": {"start": "not a timestamp"},
                    "future option": False})
        self.assertEqual(build_plan(raw), original)

    def test_radar_application_is_planned_for_connected_discovery(self):
        raw = example()
        raw["plots"] = [{"name": "cref_dieoff", "application": "Radar (MRMS)",
                         "plot_type": "die off", "region": "Continental US",
                         "variable": ["Composite Reflectivity"], "Threshold": [25, 35, 45],
                         "statistic": ["CSI", "Bias"]}]
        raw["slides"] = [{"plot group": "cref_dieoff", "number of rows": 1,
                  "row1": ["composite_reflectivity_csi_threshold25"]}]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 6)
        self.assertTrue(all(job.application == "Radar (MRMS)" for job in plan.jobs))
        self.assertEqual({dict(job.parameters)["Threshold"] for job in plan.jobs}, {25, 35, 45})
        self.assertEqual({job.statistic for job in plan.jobs}, {"csi", "bias"})
        self.assertEqual({job.relative_path for job in plan.jobs}, {
            f"cref_dieoff/cref_dieoff_composite_reflectivity_{statistic}_threshold{threshold}.png"
            for statistic in ("csi", "bias") for threshold in (25, 35, 45)})
        self.assertEqual(plan.slides[0].rows,
                         (("cref_dieoff/cref_dieoff_composite_reflectivity_csi_threshold25.png",),))

    def test_csi_plus_bias_statistic_group_and_slide_matching(self):
        raw = example()
        raw["plots"] = [{
            "name": "radar_dieoff", "application": "Radar (MRMS)",
            "plot_type": "die off", "region": "Continental US",
            "variable": ["Composite Reflectivity"], "Threshold": [25, 35],
            "statistic": ["CSI+Bias"]
        }]
        raw["slides"] = [
            {"plot group": "radar_dieoff", "title": "CSI", "number of rows": 1, "row1": ["csi_threshold25"]},
            {"plot group": "radar_dieoff", "title": "BIAS", "number of rows": 1, "row1": ["bias_threshold35"]},
        ]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 2)
        job = plan.jobs[0]
        self.assertEqual(job.statistic, "csi+bias")
        self.assertEqual(job.statistics, ("csi", "bias"))
        self.assertEqual(len(job.curves), 4)
        self.assertEqual(job.curves[0].statistic, "csi")
        self.assertEqual(job.curves[0].dash, "solid")
        self.assertEqual(job.curves[1].statistic, "bias")
        self.assertEqual(job.curves[1].dash, "dash")
        self.assertEqual(job.curves[2].statistic, "csi")
        self.assertEqual(job.curves[3].statistic, "bias")
        self.assertEqual(plan.slides[0].rows,
                         (("radar_dieoff/radar_dieoff_composite_reflectivity_csi+bias_threshold25.png",),))
        self.assertEqual(plan.slides[1].rows,
                         (("radar_dieoff/radar_dieoff_composite_reflectivity_csi+bias_threshold35.png",),))

    def test_filename_omits_unspecified_lead_but_keeps_explicit_zero(self):
        raw = example()
        raw["plots"][0].update({"name": "ua_dieoff", "plot_type": "dieoff",
                                "variable": ["T"], "statistic": ["rmse"]})
        raw["plots"][0].pop("forecast lead time")
        job = build_plan(raw).jobs[0]
        self.assertEqual(job.relative_path, "ua_dieoff/ua_dieoff_T_rmse.png")
        self.assertEqual(job.lead_tag, "")
        self.assertIsNone(job.lead)
        raw["plots"][0]["forecast lead time"] = [0]
        job = build_plan(raw).jobs[0]
        self.assertEqual(job.relative_path, "ua_dieoff/ua_dieoff_T_rmse_f000.png")
        self.assertEqual(job.lead_tag, "f000")

    def test_difference_curves_filename_and_slide_matching(self):
        raw = example()
        raw["plots"] = [{
            "name": "sfc_tseries",
            "application": "surface",
            "plot_type": "time series",
            "variable": ["t2m", "wind10m"],
            "statistic": ["rmse"],
            "forecast lead time": [0],
            "Difference curves": "Diff all curves against the 1st added curve",
        }]
        raw["slides"] = [{
            "plot group": "sfc_tseries",
            "number of rows": 1,
            "row1": ["t2m_rmse_f000", "wind10m_rmse+diff_f000"],
        }]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 2)
        paths = [job.relative_path for job in plan.jobs]
        self.assertEqual(paths, [
            "sfc_tseries/sfc_tseries_t2m_rmse+diff_f000.png",
            "sfc_tseries/sfc_tseries_wind10m_rmse+diff_f000.png",
        ])
        self.assertEqual(plan.slides[0].rows, (
            ("sfc_tseries/sfc_tseries_t2m_rmse+diff_f000.png",
             "sfc_tseries/sfc_tseries_wind10m_rmse+diff_f000.png"),
        ))
        # Test disabled difference curves
        raw["plots"][0]["Difference curves"] = "none"
        plan_none = build_plan(raw)
        self.assertEqual([job.relative_path for job in plan_none.jobs], [
            "sfc_tseries/sfc_tseries_t2m_rmse_f000.png",
            "sfc_tseries/sfc_tseries_wind10m_rmse_f000.png",
        ])

    def test_exclude_and_include_experiment_per_plot(self):
        raw = example()
        raw["experiments"] = [
            {"name": "EXP_A", "color": "red", "overlay": True},
            {"name": "EXP_B", "color": "blue", "overlay": True},
            {"name": "EXP_C", "color": "orange", "overlay": True},
        ]
        raw["plots"] = [
            {
                "name": "all_exps",
                "application": "surface",
                "plot_type": "dieoff",
                "variable": ["t2m"],
                "statistic": ["rmse"],
            },
            {
                "name": "exclude_c",
                "exclude experiment": ["EXP_C"],
                "application": "Radar (MRMS)",
                "plot_type": "die off",
                "variable": ["Composite Reflectivity"],
                "Threshold": [25],
                "statistic": ["CSI"],
            },
            {
                "name": "include_b",
                "include experiment": "EXP_B",
                "application": "surface",
                "plot_type": "dieoff",
                "variable": ["t2m"],
                "statistic": ["rmse"],
            },
        ]
        plan = build_plan(raw)
        jobs = {job.name: job for job in plan.jobs}
        self.assertEqual([e.name for e in jobs["all_exps"].experiments], ["EXP_A", "EXP_B", "EXP_C"])
        self.assertEqual([e.name for e in jobs["exclude_c"].experiments], ["EXP_A", "EXP_B"])
        self.assertEqual([e.name for e in jobs["include_b"].experiments], ["EXP_B"])
        # Ensure exclude experiment is not part of filename parameter tags
        self.assertEqual(jobs["exclude_c"].relative_path,
                         "exclude_c/exclude_c_composite_reflectivity_csi_threshold25.png")

        # Unknown experiment in exclude experiment is safely ignored (no-op)
        ign = deepcopy(raw)
        ign["plots"][1]["exclude experiment"] = ["NONEXISTENT"]
        plan_ign = build_plan(ign)
        jobs_ign = {job.name: job for job in plan_ign.jobs}
        self.assertEqual([e.name for e in jobs_ign["exclude_c"].experiments], ["EXP_A", "EXP_B", "EXP_C"])

        # Exclude all experiments
        bad = deepcopy(raw)
        bad["plots"][1]["exclude experiment"] = ["EXP_A", "EXP_B", "EXP_C"]
        with self.assertRaisesRegex(ConfigError, "all experiments were excluded"):
            build_plan(bad)

        # Mutually exclusive spelling
        bad = deepcopy(raw)
        bad["plots"][1]["exclude_experiments"] = ["EXP_C"]
        with self.assertRaisesRegex(ConfigError, "Use only one exclude experiment spelling"):
            build_plan(bad)

    def test_readable_parameter_tags_are_safe_stable_and_keep_numeric_signs(self):
        raw = example()
        raw["plots"][0].update({"variable": ["T"], "statistic": ["rmse"],
                                "forecast lead time": [0], "Threshold": [-25, 25, 25.5],
                                "Area label": "North / West", "Smooth": True})
        plan = build_plan(raw)
        self.assertEqual({Path(job.relative_path).name for job in plan.jobs}, {
            f"ua_profile_T_rmse_f000_arealabelnorth_west_smoothtrue_threshold{value}.png"
            for value in (-25, 25, 25.5)})
        self.assertTrue(all(Path(job.relative_path).parent == Path("ua_profile") for job in plan.jobs))
        raw["plots"][0] = dict(reversed(list(raw["plots"][0].items())))
        self.assertEqual(build_plan(raw).jobs, plan.jobs)

    def test_readable_parameter_names_reject_collisions_and_empty_tags(self):
        raw = example()
        raw["plots"][0]["Area label"] = ["North West", "North/West"]
        with self.assertRaisesRegex(ConfigError, "Duplicate output path"):
            build_plan(raw)
        raw["plots"][0]["Area label"] = "/"
        with self.assertRaisesRegex(ConfigError, "Cannot create a readable filename"):
            build_plan(raw)

    def test_every_listed_plot_is_planned(self):
        raw = example()
        second = deepcopy(raw["plots"][0])
        second["name"] = "second_profile"
        second["plot_type"] = "profile"
        raw["plots"].append(second)
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 84)
        self.assertEqual({job.name for job in plan.jobs}, {"ua_profile", "second_profile"})

    def test_inter_plot_waiting_seconds_and_one_second_default(self):
        raw = example()
        raw["automation"] = {"inter-plot waiting seconds": 10}
        preferred = build_plan(raw).settings
        self.assertEqual(preferred.inter_plot_waiting_seconds, 10)
        raw["automation"] = {}
        self.assertEqual(build_plan(raw).settings.inter_plot_waiting_seconds, 1)
        raw["automation"] = {"inter-plot waiting seconds": 5}
        self.assertEqual(build_plan(raw).settings.inter_plot_waiting_seconds, 5)
        for value in (1, 1.5, 4, 86400):
            raw["automation"] = {"inter-plot waiting seconds": value}
            self.assertEqual(build_plan(raw).settings.inter_plot_waiting_seconds, value)

    def test_control_waiting_seconds_default_and_overrides_are_independent(self):
        raw = example()
        self.assertEqual(build_plan(raw).settings.control_waiting_seconds, 0.2)
        for value in (0.2, 0.3, 0.5, 1, 10):
            raw["automation"] = {"control waiting seconds": value}
            with self.subTest(value=value):
                settings = build_plan(raw).settings
                self.assertEqual(settings.control_waiting_seconds, value)
                self.assertEqual(settings.inter_plot_waiting_seconds, 1)
                self.assertEqual(settings.cooldown_seconds, 180)

    def test_control_waiting_seconds_rejects_invalid_values(self):
        for value in (0, 0.09, 0.1, 0.19, -1, 10.1, True, "0.5", float("nan"), float("inf")):
            raw = example()
            raw["automation"] = {"control waiting seconds": value}
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "control waiting seconds"):
                build_plan(raw)

    def test_inter_plot_waiting_seconds_rejects_invalid_values_and_old_keys(self):
        for value in (0, 0.9, -1, 86401, True, "10", float("nan"), float("inf")):
            raw = example()
            raw["automation"] = {"inter-plot waiting seconds": value}
            with self.subTest(value=value), self.assertRaises(ConfigError):
                build_plan(raw)
        for key in ("minimum waiting seconds", "min_waiting_time", "min_interval_seconds"):
            raw["automation"] = {key: 10}
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, "Unknown automation options"):
                build_plan(raw)

    def test_common_dates_and_style_weights_apply_to_every_job(self):
        raw = example()
        raw["common"].update({"line weight": 2, "marker weight": 7})
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 42)
        self.assertTrue(all((job.line_weight, job.marker_weight) == (2, 7) for job in plan.jobs))
        original = plan.jobs[0].fingerprint
        raw["common"]["line weight"] = 3
        self.assertNotEqual(build_plan(raw).jobs[0].fingerprint, original)
        raw["common"]["marker weight"] = 9
        self.assertTrue(all(job.marker_weight == 9 for job in build_plan(raw).jobs))

    def test_omitted_dates_are_deferred_to_the_first_recipe_experiment(self):
        raw = example()
        raw["experiments"][0]["overlay"] = False
        for common in ({}, {"line weight": 3}, {"start": None, "end": None},
                       {"start": "2026-10-01T00:00:00Z"}, {"end": "2026-10-15T00:00:00Z"}):
            raw["common"] = common
            with self.subTest(common=common):
                plan = build_plan(raw)
                self.assertEqual(plan.jobs[0].experiments[0].name, "RRFSv2b")
                self.assertTrue(all(job.default_date_experiment == "RRFSv2x" for job in plan.jobs))
                self.assertTrue(all(job.mats_dates is None for job in plan.jobs))
                self.assertEqual(plan.jobs[0].start,
                                 "2026-10-01T00:00:00+00:00" if common.get("start") else None)
                self.assertEqual(plan.jobs[0].end,
                                 "2026-10-15T00:00:00+00:00" if common.get("end") else None)

    def test_explicit_dates_remain_strict_and_do_not_need_a_default_model(self):
        self.assertIsNone(build_plan(example()).jobs[0].default_date_experiment)
        for endpoint in ("start", "end"):
            for value in ("bad", "2026-10-01", "2026-10-01T00:00:01Z", ""):
                raw = example()
                raw["common"] = {endpoint: value}
                with self.subTest(endpoint=endpoint, value=value), self.assertRaises(ConfigError):
                    build_plan(raw)

    def test_common_rejects_invalid_weights_unknown_keys_and_conflicting_dates(self):
        for name in ("line weight", "marker weight"):
            for value in (0, -1, True, "2", float("nan"), float("inf")):
                raw = example()
                raw["common"][name] = value
                with self.subTest(name=name, value=value), self.assertRaises(ConfigError):
                    build_plan(raw)
        raw = example()
        raw["dates"] = dict(raw["common"])
        self.assertEqual(build_plan(raw).jobs[0].start, "2026-10-01T00:00:00+00:00")
        raw.pop("dates")
        raw["common"]["line wieght"] = 2
        with self.assertRaisesRegex(ConfigError, "line wieght"):
            build_plan(raw)

    def test_unknown_legacy_dates_are_ignored_and_common_is_required(self):
        raw = example()
        raw["dates"] = raw.pop("common")
        with self.assertRaisesRegex(ConfigError, "common must be a mapping"):
            build_plan(raw)
        raw.pop("dates")
        with self.assertRaisesRegex(ConfigError, "common must be a mapping"):
            build_plan(raw)

    def test_baseline_plans_dieoff_and_timeseries_with_requested_colors(self):
        raw = example()
        raw["experiments"] = [{"name": f"MODEL{index}", "color": color}
                              for index, color in enumerate(("red", "blue", "orange", "black"))]
        surface = {"name": "sfc_dieoff", "application": "surface", "truth": "METAR",
                   "plot_type": "dieoff", "region": "All HRRR domain",
                   "variable": ["t2m", "td2m", "wind10m"], "statistic": ["rmse+bias"]}
        raw["plots"] = [surface, {**surface, "name": "sfc_tseries", "plot_type": "time series",
                                 "forecast lead time": [0, 1, 12]}]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 12)
        dieoff = [job for job in plan.jobs if job.plot_type == "dieoff"]
        series = [job for job in plan.jobs if job.plot_type == "time series"]
        self.assertEqual(len(dieoff), 3)
        self.assertEqual(len(series), 9)
        self.assertEqual({job.lead for job in series}, {0, 1, 12})
        for job in plan.jobs:
            self.assertEqual(len(job.curves), 8)
            self.assertEqual([experiment.color for experiment in job.experiments],
                             ["red", "blue", "orange", "black"])
            self.assertEqual((job.width, job.height), (1600, 900))

    def test_surface_baseline_preserves_omitted_mats_defaults(self):
        raw = example()
        raw["plots"] = [{"name": "sfc_dieoff", "application": "surface", "truth": "QC-METAR",
                         "plot_type": "dieoff", "region": "All HRRR domain",
                         "variable": ["t2m", "td2m", "wind10m"]}]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 3)
        self.assertEqual([job.variable for job in plan.jobs], ["t2m", "td2m", "wind10m"])
        for job in plan.jobs:
            self.assertEqual(job.application, "surface")
            self.assertEqual((job.width, job.height), (1600, 900))
            self.assertIsNone(job.statistic)
            self.assertIsNone(job.lead)
            self.assertIsNone(job.matched_samples)
            self.assertEqual(len(job.curves), 2)

    def test_flat_extra_parameters_expand_and_change_fingerprints(self):
        raw = example()
        raw["plots"][0]["dieoff type"] = ["Dieoff", "Other"]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 84)
        self.assertEqual(len({job.relative_path for job in plan.jobs}), 84)
        self.assertNotEqual(plan.jobs[0].fingerprint, plan.jobs[1].fingerprint)
        self.assertEqual(plan.jobs[0].parameters, (("dieoff type", "Dieoff"),))

    def test_forecast_colors_follow_hours_not_group_positions(self):
        raw = example()
        raw["plots"][0]["forecast lead time"] = ["3+0+1"]
        raw["plots"][0]["statistic"] = ["rmse+bias"]
        raw["plots"][0]["forecast lead colors"] = {0: "red", 1: "royalblue", 3: "#009E73", 9: "magenta"}
        job = build_plan(raw).jobs[0]
        self.assertEqual([curve.color for curve in job.curves],
                         ["#009E73", "#009E73", "red", "red", "royalblue", "royalblue"])
        self.assertEqual([entry.color for entry in job.legend_entries], ["#009E73", "red", "royalblue"])
        raw["plots"][0]["forecast lead time"] = ["0+3"]
        self.assertEqual([entry.color for entry in build_plan(raw).jobs[0].legend_entries], ["red", "#009E73"])

    def test_color_change_invalidates_grouped_cache_but_not_ordinary_plots(self):
        raw = example()
        raw["plots"][0]["forecast lead time"] = [0, "0+1"]
        original = build_plan(raw)
        raw["plots"][0]["forecast lead colors"] = {0: "#0072B2", 1: "#E69F00"}
        custom = build_plan(raw)
        self.assertEqual(original.jobs[0].fingerprint, custom.jobs[0].fingerprint)
        self.assertEqual([curve.color for curve in custom.jobs[0].curves], ["red", "blue"])
        self.assertNotEqual(original.jobs[1].fingerprint, custom.jobs[1].fingerprint)

    def test_forecast_colors_require_complete_groups_and_valid_keys_and_values(self):
        for mapping in ({0: "red"}, {}, {0: "red", 1: "#GGGGGG"},
                        {0: "red", 1: 123}, {"0": "red", 1: "blue"},
                        {True: "red", 0: "blue"}, {-1: "red", 0: "blue", 1: "green"},
                        {0: "red", 1: ""}):
            raw = example()
            raw["plots"][0]["forecast lead time"] = ["0+1"]
            raw["plots"][0]["forecast lead colors"] = mapping
            with self.subTest(mapping=mapping), self.assertRaises(ConfigError):
                build_plan(raw)

    def test_grouped_leads_use_only_first_experiment_with_separate_metrics(self):
        raw = example()
        raw["experiments"][0]["overlay"] = False
        raw["plots"][0]["forecast lead time"] = ["0+1+3+6+9"]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 6)
        job = plan.jobs[0]
        self.assertEqual(job.leads, (0, 1, 3, 6, 9))
        self.assertEqual([experiment.name for experiment in job.experiments], ["RRFSv2x"])
        self.assertEqual(len(job.curves), 5)
        self.assertEqual(job.relative_path, "ua_profile/ua_profile_T_rmse_f000+001+003+006+009.png")
        self.assertEqual([curve.lead for curve in job.curves], [0, 1, 3, 6, 9])
        self.assertEqual(len({curve.color for curve in job.curves}), 5)

    def test_grouped_leads_and_combined_metrics_make_ten_curves(self):
        raw = example()
        raw["plots"][0]["forecast lead time"] = ["0+1+3+6+9"]
        raw["plots"][0]["statistic"] = ["rmse+bias"]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 3)
        job = plan.jobs[0]
        self.assertEqual(len(job.curves), 10)
        self.assertEqual(len({curve.label for curve in job.curves}), 10)
        self.assertTrue(all(curve.label.isalnum() for curve in job.curves))
        self.assertTrue(all(curve.name == "RRFSv2x" for curve in job.curves))
        for index in range(0, 10, 2):
            rmse, bias = job.curves[index:index + 2]
            self.assertEqual(rmse.color, bias.color)
            self.assertEqual(rmse.lead, bias.lead)
            self.assertEqual((rmse.dash, bias.dash), ("solid", "dash"))
        self.assertEqual([entry.name for entry in job.legend_entries],
                         [f"RRFSv2x f{lead:03d}h" for lead in (0, 1, 3, 6, 9)])

    def test_grouped_and_separate_leads_can_coexist(self):
        raw = example()
        raw["plots"][0]["forecast lead time"] = [0, "1 + 3"]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 12)
        self.assertEqual(len(plan.jobs[0].experiments), 2)
        self.assertEqual(len(plan.jobs[1].experiments), 1)
        self.assertNotEqual(plan.jobs[0].fingerprint, plan.jobs[1].fingerprint)

    def test_invalid_grouped_leads_are_rejected(self):
        for value in ("0+0", "0+1000", "0+-1", "0+1.5", "0++1", "__import__('os')", True):
            raw = example()
            raw["plots"][0]["forecast lead time"] = [value]
            with self.subTest(value=value), self.assertRaises(ConfigError):
                build_plan(raw)

    def test_user_example_expands_to_42_profiles(self):
        plan = build_plan(example())
        self.assertEqual(len(plan.jobs), 42)
        paths = {job.relative_path for job in plan.jobs}
        self.assertEqual(len(paths), 42)
        self.assertIn("ua_profile/ua_profile_T_rmse_f000.png", paths)
        self.assertIn("ua_profile/ua_profile_RHobT_rmse_f000.png", paths)
        self.assertIn("ua_profile/ua_profile_Wind_bias_f012.png", paths)
        self.assertTrue(all(job.plot_type == "profile" for job in plan.jobs))
        self.assertTrue(all((job.width, job.height) == (1200, 1200) for job in plan.jobs))
        self.assertTrue(all(len(job.experiments) == 2 for job in plan.jobs))
        self.assertEqual(len(plan.warnings), 1)
        self.assertEqual([curve.name for curve in plan.jobs[0].experiments], ["RRFSv2x", "RRFSv2b"])
        self.assertEqual(plan.settings.base_url, "https://gsl.noaa.gov/int-mats")

    def test_combined_statistics_expand_to_21_distinct_figures(self):
        raw = example()
        raw["plots"][0]["statistic"] = ["rmse+bias"]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 21)
        self.assertEqual(len({job.relative_path for job in plan.jobs}), 21)
        self.assertEqual(plan.jobs[0].relative_path, "ua_profile/ua_profile_T_rmse+bias_f000.png")
        self.assertEqual(plan.jobs[0].statistics, ("rmse", "bias"))
        self.assertEqual([plan.jobs[0].curve_label(experiment, statistic)
                          for experiment in plan.jobs[0].experiments
                          for statistic in plan.jobs[0].statistics],
                         ["RRFSv2xRMSE", "RRFSv2xBIAS", "RRFSv2bRMSE", "RRFSv2bBIAS"])

    def test_mats_labels_are_alphanumeric_but_full_model_names_are_preserved(self):
        raw = example()
        name = "RRFSv2_conus_3km_ret_NoObsGrp_Rec_Soil"
        raw["experiments"][0]["name"] = name
        raw["plots"][0]["statistic"] = ["rmse+bias"]
        job = build_plan(raw).jobs[0]
        self.assertTrue(all(curve.label.isalnum() for curve in job.curves))
        self.assertEqual(job.curves[0].label, "RRFSv2conus3kmretNoObsGrpRecSoilRMSE")
        self.assertEqual(job.curves[0].name, name)

    def test_separate_and_combined_options_can_coexist(self):
        raw = example()
        raw["plots"][0]["statistic"] = ["rmse", "bias", "RMSE+BIAS"]
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 63)
        self.assertEqual({job.statistic for job in plan.jobs}, {"rmse", "bias", "rmse+bias"})
        self.assertNotEqual(plan.jobs[0].fingerprint, plan.jobs[42].fingerprint)

    def test_only_known_mats_endpoints_are_allowed(self):
        raw = example()
        for url in ("http://gsl.noaa.gov/int-mats", "https://example.com/mats",
                    "https://gsl.noaa.gov/int-mats?redirect=elsewhere"):
            raw["automation"]["base_url"] = url
            with self.assertRaises(ConfigError):
                build_plan(raw)
        raw.pop("automation")
        self.assertEqual(build_plan(raw).settings.base_url, "https://gsl.noaa.gov/mats")

    def test_curve_labels_are_experiment_names_not_aliases(self):
        plan = build_plan(example())
        self.assertEqual([curve.label for curve in plan.jobs[0].experiments], ["RRFSv2x", "RRFSv2b"])
        raw = example()
        raw["experiments"][0]["label"] = "v2x"
        with self.assertRaises(ConfigError):
            build_plan(raw)

    def test_fingerprint_changes_with_dates_colors_or_models(self):
        raw = example()
        original = build_plan(raw).jobs[0].fingerprint
        for section, field, value in (("common", "end", "2026-10-16T00:00:00Z"),
                                      ("experiments", "color", "green"),
                                      ("experiments", "name", "another_model")):
            changed = deepcopy(raw)
            target = changed[section][0] if section == "experiments" else changed[section]
            target[field] = value
            self.assertNotEqual(build_plan(changed).jobs[0].fingerprint, original)

    def test_rejects_unsafe_names_duplicate_outputs_and_invalid_leads(self):
        for field, value in (("name", "../escape"), ("variable", ["T", "Temperature"]),
                             ("forecast lead time", [True]), ("statistic", [None]),
                             ("matched_samples", "false")):
            with self.subTest(field=field):
                raw = example()
                raw["plots"][0][field] = value
                with self.assertRaises(ConfigError):
                    build_plan(raw)

    def test_separate_experiments_do_not_collide(self):
        raw = example()
        raw["experiments"][1]["overlay"] = False
        plan = build_plan(raw)
        self.assertEqual(len(plan.jobs), 84)
        self.assertEqual(len({job.relative_path for job in plan.jobs}), 84)

    def test_timezone_conversion_and_date_precision(self):
        raw = example()
        raw["common"]["start"] = "2026-09-30T20:00:00-04:00"
        self.assertEqual(build_plan(raw).jobs[0].mats_dates, "10/01/2026 00:00 - 10/15/2026 00:00")
        for value in ("2026-10-01", "2026-10-01T00:00:01Z", "2026-10-20T00:00:00Z"):
            raw["common"]["start"] = value
            with self.assertRaises(ConfigError):
                build_plan(raw)

    def test_cannot_disable_pacing_or_set_unbounded_retries(self):
        for settings in ({"inter-plot waiting seconds": 0}, {"inter-plot waiting seconds": 0.9},
                 {"cooldown_seconds": 10},
                         {"max_attempts": 10}, {"inter-plot waiting seconds": float("nan")}):
            raw = example()
            raw["automation"] = settings
            with self.assertRaises(ConfigError):
                build_plan(raw)

    def test_explicit_wait_preserves_one_second_default(self):
        raw = example()
        raw["automation"] = {"inter-plot waiting seconds": 10}
        settings = build_plan(raw).settings
        self.assertEqual(settings.inter_plot_waiting_seconds, 10)
        self.assertEqual(settings.cooldown_seconds, 180)
        raw.pop("automation")
        self.assertEqual(build_plan(raw).settings.inter_plot_waiting_seconds, 1)

    def test_recipe_title_parsing(self):
        raw = example()
        self.assertIsNone(build_plan(raw).title)
        raw["title"] = "RRFSv2 Verification Study"
        self.assertEqual(build_plan(raw).title, "RRFSv2 Verification Study")
        raw["title"] = "   "
        with self.assertRaises(ConfigError):
            build_plan(raw)

    def test_common_font_sizes_default_and_preserve_fingerprint(self):
        base = build_plan(example())
        self.assertEqual(DEFAULT_DESCRIPTION_FONT_SIZE, 24)
        self.assertEqual(DEFAULT_LEGEND_FONT_SIZE, 28)
        self.assertTrue(all(
            (job.description_font_size, job.legend_font_size) == (24, 28)
            for job in base.jobs
        ))
        original_fingerprints = [job.fingerprint for job in base.jobs]

        for desc_key, leg_key, desc_val, leg_val in (
            ("description font size", "legend font size", 26, 30),
            ("description_font_size", "legend_font_size", 18.5, 22),
            ("description font size", "legend_font_size", 4, 72),
        ):
            raw = example()
            raw["common"][desc_key] = desc_val
            raw["common"][leg_key] = leg_val
            with self.subTest(desc_key=desc_key, leg_key=leg_key, desc_val=desc_val, leg_val=leg_val):
                plan = build_plan(raw)
                self.assertTrue(all(
                    (job.description_font_size, job.legend_font_size) == (desc_val, leg_val)
                    for job in plan.jobs
                ))
                self.assertEqual([job.fingerprint for job in plan.jobs], original_fingerprints)

    def test_common_rejects_invalid_and_conflicting_font_sizes(self):
        for name in ("description font size", "description_font_size", "legend font size", "legend_font_size"):
            for value in (0, -1, 3.9, 72.1, True, False, "24", None, float("nan"), float("inf")):
                raw = example()
                raw["common"][name] = value
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ConfigError, "font size"):
                    build_plan(raw)
        raw = example()
        raw["common"].update({"description font size": 24, "description_font_size": 24})
        with self.assertRaisesRegex(ConfigError, "description font size"):
            build_plan(raw)
        raw = example()
        raw["common"].update({"legend font size": 28, "legend_font_size": 28})
        with self.assertRaisesRegex(ConfigError, "legend font size"):
            build_plan(raw)

    def test_experiment_short_name_display_and_fingerprint(self):
        raw = example()
        raw["experiments"] = [
            {"name": "RRFSv2_conus_3km_ret_v2x_retv4", "color": "red", "overlay": True},
            {"name": "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km", "color": "blue", "overlay": True},
        ]
        raw["plots"][0]["forecast lead time"] = [0, "0+3"]
        base_plan = build_plan(raw)
        base_fps = [job.fingerprint for job in base_plan.jobs]

        for key0, key1 in (("short name", "short_name"), ("short_name", "short name")):
            with self.subTest(key0=key0, key1=key1):
                with_short = deepcopy(raw)
                with_short["experiments"][0][key0] = "base4"
                with_short["experiments"][1][key1] = "3p5km"
                plan = build_plan(with_short)
                self.assertEqual([e.name for e in plan.experiments], [
                    "RRFSv2_conus_3km_ret_v2x_retv4",
                    "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km",
                ])
                self.assertEqual([e.short_name for e in plan.experiments], ["base4", "3p5km"])
                self.assertEqual([e.display_name for e in plan.experiments], ["base4", "3p5km"])
                # Fingerprints and MATS query fields remain unchanged
                self.assertEqual([job.fingerprint for job in plan.jobs], base_fps)
                single_job, grouped_job = plan.jobs[0], plan.jobs[1]
                self.assertEqual([c.name for c in single_job.curves], [
                    "RRFSv2_conus_3km_ret_v2x_retv4",
                    "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km",
                ])
                self.assertEqual([c.label for c in single_job.curves], [
                    "RRFSv2conus3kmretv2xretv4",
                    "RRFSv2conus3kmretv2xretv4p13p5km",
                ])
                self.assertEqual([e.name for e in single_job.legend_entries], ["base4", "3p5km"])
                self.assertEqual([e.name for e in grouped_job.legend_entries], ["base4 f000h", "base4 f003h"])

        # overlay: false keeps path slug derived from canonical exp.name
        separate = deepcopy(raw)
        separate["experiments"][0]["overlay"] = False
        separate["experiments"][0]["short name"] = "base4"
        sep_plan = build_plan(separate)
        self.assertTrue(any(
            job.relative_path.startswith("ua_profile/RRFSv2conus3kmretv2xretv4-1/")
            for job in sep_plan.jobs
        ))
        self.assertFalse(any("base4" in job.relative_path for job in sep_plan.jobs))

        # exclude / include experiment match short_name
        filtered = deepcopy(raw)
        filtered["experiments"][0]["short name"] = "base4"
        filtered["experiments"][1]["short name"] = "3p5km"
        filtered["plots"][0]["exclude experiment"] = ["base4"]
        self.assertEqual([e.name for e in build_plan(filtered).jobs[0].experiments], [
            "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km"
        ])
        filtered["plots"][0].pop("exclude experiment")
        filtered["plots"][0]["include experiment"] = ["3p5km"]
        self.assertEqual([e.name for e in build_plan(filtered).jobs[0].experiments], [
            "RRFSv2_conus_3km_ret_v2x_retv4p1_3p5km"
        ])

        # Invalid or conflicting short name spellings fail explicitly
        bad_conflicting = deepcopy(raw)
        bad_conflicting["experiments"][0].update({"short name": "b4", "short_name": "base4"})
        with self.assertRaisesRegex(ConfigError, "short name"):
            build_plan(bad_conflicting)
        for bad_val in ("", "   ", "---", 123):
            bad = deepcopy(raw)
            bad["experiments"][0]["short name"] = bad_val
            with self.subTest(bad_val=bad_val), self.assertRaisesRegex(ConfigError, "short name"):
                build_plan(bad)

    def test_experiment_mats_name_spellings_and_conflicts(self):
        raw = example()
        base_fps = [job.fingerprint for job in build_plan(raw).jobs]

        for key0, key1 in (("MATS name", "MATS_name"), ("MATS_name", "name")):
            with self.subTest(key0=key0, key1=key1):
                alt = deepcopy(raw)
                alt["experiments"][0][key0] = alt["experiments"][0].pop("name")
                alt["experiments"][1][key1] = alt["experiments"][1].pop("name")
                plan = build_plan(alt)
                self.assertEqual([e.name for e in plan.experiments], ["RRFSv2x", "RRFSv2b"])
                self.assertEqual([job.fingerprint for job in plan.jobs], base_fps)

        # Conflicting spellings fail cleanly
        for k1, k2 in (("MATS name", "name"), ("MATS name", "MATS_name"), ("MATS_name", "name")):
            bad = deepcopy(raw)
            bad["experiments"][0][k1] = "expA"
            bad["experiments"][0][k2] = "expB"
            with self.subTest(conflict=(k1, k2)), self.assertRaisesRegex(ConfigError, "MATS name"):
                build_plan(bad)

        # Missing any name key fails explicitly
        missing = deepcopy(raw)
        missing["experiments"][0].pop("name")
        with self.assertRaisesRegex(ConfigError, "MATS name"):
            build_plan(missing)

    def test_copy_used_recipes_copies_main_and_enabled_graphics_recipe_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recipes_dir = root / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            gfx_recipe = recipes_dir / "gfx_layout.yaml"
            gfx_text = "graphics:\n  - name: test\n"
            gfx_recipe.write_text(gfx_text, encoding="utf-8")

            raw = example()
            raw["pygraf graphics slides"] = {"enable": True, "recipe": "gfx_layout.yaml"}
            main_recipe = recipes_dir / "study.yaml"
            main_recipe.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

            out_dir = root / "results" / "run1"
            plan = load_plan(main_recipe)
            copied = copy_used_recipes(plan, out_dir)
            self.assertEqual(copied, (out_dir / "study.yaml", out_dir / "gfx_layout.yaml"))
            self.assertEqual((out_dir / "study.yaml").read_text(encoding="utf-8"), main_recipe.read_text(encoding="utf-8"))
            self.assertEqual((out_dir / "gfx_layout.yaml").read_text(encoding="utf-8"), gfx_text)

            # Re-running directly from the copied recipe inside output_dir avoids SameFileError
            rerun_plan = load_plan(out_dir / "study.yaml")
            copied_again = copy_used_recipes(rerun_plan, out_dir)
            self.assertEqual(copied_again, (out_dir / "study.yaml", out_dir / "gfx_layout.yaml"))
            self.assertEqual((out_dir / "study.yaml").read_text(encoding="utf-8"), main_recipe.read_text(encoding="utf-8"))

            # When pygraf graphics slides are disabled, only the main recipe is copied
            disabled = example()
            disabled["pygraf graphics slides"] = {"enable": False, "recipe": "gfx_layout.yaml"}
            disabled_recipe = recipes_dir / "no_gfx.yaml"
            disabled_recipe.write_text(yaml.safe_dump(disabled, sort_keys=False), encoding="utf-8")
            out_dir2 = root / "results" / "run2"
            copied2 = copy_used_recipes(load_plan(disabled_recipe), out_dir2)
            self.assertEqual(copied2, (out_dir2 / "no_gfx.yaml",))
            self.assertFalse((out_dir2 / "gfx_layout.yaml").exists())


if __name__ == "__main__":
    unittest.main()