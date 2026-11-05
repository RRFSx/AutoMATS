from copy import deepcopy
from pathlib import Path
import unittest

from automats.config import ConfigError, build_plan


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

    def test_minimum_waiting_seconds_and_one_second_default(self):
        raw = example()
        raw["automation"] = {"minimum waiting seconds": 10}
        preferred = build_plan(raw).settings
        self.assertEqual(preferred.minimum_waiting_seconds, 10)
        raw["automation"] = {}
        self.assertEqual(build_plan(raw).settings.minimum_waiting_seconds, 1)
        raw["automation"] = {"minimum waiting seconds": 5}
        self.assertEqual(build_plan(raw).settings.minimum_waiting_seconds, 5)
        for value in (1, 1.5, 4, 86400):
            raw["automation"] = {"minimum waiting seconds": value}
            self.assertEqual(build_plan(raw).settings.minimum_waiting_seconds, value)

    def test_control_waiting_seconds_default_and_overrides_are_independent(self):
        raw = example()
        self.assertEqual(build_plan(raw).settings.control_waiting_seconds, 0.2)
        for value in (0.2, 0.3, 0.5, 1, 10):
            raw["automation"] = {"control waiting seconds": value}
            with self.subTest(value=value):
                settings = build_plan(raw).settings
                self.assertEqual(settings.control_waiting_seconds, value)
                self.assertEqual(settings.minimum_waiting_seconds, 1)
                self.assertEqual(settings.cooldown_seconds, 180)

    def test_control_waiting_seconds_rejects_invalid_values(self):
        for value in (0, 0.09, 0.1, 0.19, -1, 10.1, True, "0.5", float("nan"), float("inf")):
            raw = example()
            raw["automation"] = {"control waiting seconds": value}
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "control waiting seconds"):
                build_plan(raw)

    def test_minimum_waiting_seconds_rejects_invalid_values_and_old_keys(self):
        for value in (0, 0.9, -1, 86401, True, "10", float("nan"), float("inf")):
            raw = example()
            raw["automation"] = {"minimum waiting seconds": value}
            with self.subTest(value=value), self.assertRaises(ConfigError):
                build_plan(raw)
        for key in ("min_waiting_time", "min_interval_seconds"):
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
        for settings in ({"minimum waiting seconds": 0}, {"minimum waiting seconds": 0.9},
                 {"cooldown_seconds": 10},
                         {"max_attempts": 10}, {"minimum waiting seconds": float("nan")}):
            raw = example()
            raw["automation"] = settings
            with self.assertRaises(ConfigError):
                build_plan(raw)

    def test_explicit_wait_preserves_one_second_default(self):
        raw = example()
        raw["automation"] = {"minimum waiting seconds": 10}
        settings = build_plan(raw).settings
        self.assertEqual(settings.minimum_waiting_seconds, 10)
        self.assertEqual(settings.cooldown_seconds, 180)
        raw.pop("automation")
        self.assertEqual(build_plan(raw).settings.minimum_waiting_seconds, 1)


if __name__ == "__main__":
    unittest.main()