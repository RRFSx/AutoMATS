import base64
import json
import struct
from copy import deepcopy
from dataclasses import asdict, replace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import time
import unittest
import zlib
from unittest.mock import Mock, patch

from automats.config import Settings, STATISTICS, build_plan
from automats.browser import EXPORT_IMAGE, Throttled, UpperAirBrowser, annotate_image, discover_applications, figure_header, minimize_window, resolve_application, retry_after_seconds
from automats.cli import ProgressDisplay, main
from automats.runner import CHROMIUM_ARGS, CHROMIUM_IGNORE_DEFAULT_ARGS, DEFAULT_USER_AGENT, PNG_SIGNATURE, QueryPacer, ResolutionError, ResultStore, RunError, figure_exists, is_png, lock_error, resolve_option, run_plan, validate_curves, validate_effective_curves
from automats.slides import SlideError, build_slides
from test_config import example


def png_image(color=(0, 0, 0)):
    def chunk(kind, data):
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00" + bytes(color))
    return PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def clock(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class ApplicationDiscoveryTests(unittest.TestCase):
    def test_homepage_titles_and_slugs_resolve_without_a_whitelist(self):
        apps = discover_applications([
            {"title": "Radar (MRMS)", "href": "/mats/radar"},
            {"title": "Upper Air (RAOBs/AMDAR)", "href": "upperair"},
            {"title": "Surface (METAR)", "href": "surface"},
            {"title": "New Application", "href": "new-app"},
            {"title": "Radar (MRMS)", "href": "/mats/radar/"},
        ], "https://gsl.noaa.gov/mats")
        self.assertEqual(len(apps), 4)
        self.assertEqual(resolve_application("Radar (MRMS)", apps)["slug"], "radar")
        self.assertEqual(resolve_application("upper air", apps)["slug"], "upperair")
        self.assertEqual(resolve_application("surface", apps)["slug"], "surface")
        self.assertEqual(resolve_application("New Application", apps)["slug"], "new-app")

    def test_only_same_origin_application_routes_are_discovered(self):
        apps = discover_applications([
            {"title": "Radar", "href": "/mats/radar"},
            {"title": "External", "href": "https://example.com/mats/radar"},
            {"title": "Other endpoint", "href": "/int-mats/radar"},
            {"title": "Documentation", "href": "/mats/docs/index.html"},
            {"title": "Section", "href": "#radar"},
            {"title": "Query", "href": "/mats/radar?token=example"},
        ], "https://gsl.noaa.gov/mats")
        self.assertEqual([app["slug"] for app in apps], ["radar"])

    def test_unknown_and_ambiguous_applications_fail_explicitly(self):
        apps = discover_applications([
            {"title": "Upper Air", "href": "upperair"},
            {"title": "MET Upper Air", "href": "met-upperair"},
        ], "https://gsl.noaa.gov/mats")
        for requested in ("air", "unknown"):
            with self.subTest(requested=requested), self.assertRaises(ResolutionError):
                resolve_application(requested, apps)


class ProgressTests(unittest.TestCase):
    def test_heartbeat_worker_refreshes_while_waiting(self):
        progress = ProgressDisplay(12, StringIO())
        progress.stop = Mock()
        progress.stop.wait.side_effect = [False, True]
        with patch.object(progress, "refresh") as refresh:
            progress._animate()
        refresh.assert_called_once()
        self.assertEqual(progress.stop.wait.call_args_list[0].args, (10,))

    def test_non_terminal_reports_stages_and_elapsed_heartbeat(self):
        output = StringIO()
        clock = FakeClock()
        progress = ProgressDisplay(12, output, clock.clock)
        progress.update("Starting Chrome")
        clock.now = 10
        progress.refresh()
        progress.update("Preparing temperature", completed=1)
        text = output.getvalue()
        self.assertIn("Working [0/12, 0s elapsed]: Starting Chrome", text)
        self.assertIn("Working [0/12, 10s elapsed]: Starting Chrome", text)
        self.assertIn("Working [1/12, 10s elapsed]: Preparing temperature", text)
        self.assertNotIn("\r", text)

    def test_terminal_animates_without_inventing_completed_figures(self):
        class Terminal(StringIO):
            def isatty(self):
                return True

        output = Terminal()
        progress = ProgressDisplay(12, output)
        progress.update("Waiting for MATS")
        first = output.getvalue()
        progress.refresh()
        second = output.getvalue()[len(first):]
        self.assertIn("0/12", first)
        self.assertIn("0/12", second)
        self.assertIn("[................]", second)
        self.assertIn("\r", second)
        self.assertNotEqual(first, second)

    def test_wait_countdown_decreases_and_clears_when_stage_changes(self):
        output = StringIO()
        clock = FakeClock()
        progress = ProgressDisplay(12, output, clock.clock)
        progress.update("MATS HTTP 429 rate-limit protection", wait_seconds=180)
        clock.now = 10
        progress.refresh()
        text = output.getvalue()
        self.assertIn("Waiting 180s", text)
        self.assertIn("Waiting 170s", text)
        self.assertIn("10s elapsed", text)
        previous = len(text)
        progress.update("Preparing MATS")
        self.assertNotIn("Waiting", output.getvalue()[previous:])

    def test_display_stops_its_thread_on_failure(self):
        output = StringIO()
        progress = ProgressDisplay(12, output)
        with patch("automats.cli.Thread") as thread:
            with self.assertRaises(RunError):
                with progress:
                    raise RunError("MATS denied access")
            thread.return_value.start.assert_called_once()
            thread.return_value.join.assert_called_once()
        self.assertTrue(progress.stop.is_set())
        self.assertIn("Stopped", output.getvalue())

    def test_runner_reports_startup_waiting_query_and_saved_stages(self):
        plan = build_plan(example())
        plan = replace(plan, jobs=plan.jobs[:1])
        clock = FakeClock()
        pacer = QueryPacer(plan.settings, clock.clock, clock.wait)
        pacer.next_start = 10
        status = Mock()
        with tempfile.TemporaryDirectory() as directory, patch("playwright.sync_api.sync_playwright"):
            with patch("automats.runner.Path.home", return_value=Path(directory)), \
                    patch("automats.runner.QueryPacer", return_value=pacer), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.effective_settings = {}
                client.return_value.plot.return_value = (png_image(), {})
                self.assertEqual(run_plan(plan, Path(directory) / "figures", log=lambda message: None,
                                          status=status), 1)
        messages = [call.args[0] for call in status.call_args_list]
        for stage in ("Initializing", "Checking the run lock", "Launching Chrome", "Minimum wait",
                      "Preparing MATS", "Querying MATS", "Saving PNG", "Saved", "Closing Chrome"):
            self.assertTrue(any(stage in message for message in messages), stage)
        self.assertTrue(any(call.kwargs.get("completed") == 1 for call in status.call_args_list))
        self.assertTrue(any(call.kwargs.get("wait_seconds") == 10 for call in status.call_args_list))


class LockTests(unittest.TestCase):
    def test_ctrl_c_releases_lock_during_wait_preparation_and_query(self):
        plan = build_plan(example())
        plan = replace(plan, jobs=plan.jobs[:1])
        for stage in ("wait", "prepare", "plot"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                clock = FakeClock()
                pacer = QueryPacer(plan.settings, clock.clock, clock.wait)
                if stage == "wait":
                    pacer.next_start = 10
                    pacer.wait = Mock(side_effect=KeyboardInterrupt)
                with patch("automats.runner.Path.home", return_value=root), \
                        patch("automats.runner.QueryPacer", return_value=pacer), \
                        patch("playwright.sync_api.sync_playwright") as manager, \
                        patch("automats.browser.UpperAirBrowser") as client:
                    if stage != "wait":
                        getattr(client.return_value, stage).side_effect = KeyboardInterrupt
                    with self.assertRaises(KeyboardInterrupt):
                        run_plan(plan, root / "figures", log=lambda message: None)
                    manager.return_value.__enter__.return_value.chromium.launch.return_value.close.assert_called_once()
                    self.assertFalse((root / ".cache" / "automats" / "run.lock").exists())
                    if stage == "plot":
                        self.assertEqual(pacer.next_start, 180)
                    else:
                        self.assertLess(pacer.next_start, 180)

    def test_live_lock_explains_pid_and_wait_without_removal_command(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run.lock"
            lock.write_text("12345")
            with patch("automats.runner.os.name", "posix"), patch("automats.runner.os.kill") as probe:
                message = str(lock_error(lock))
            probe.assert_called_once_with(12345, 0)
            self.assertIn("12345", message)
            self.assertIn("ps -p 12345", message)
            self.assertIn("Ctrl+C", message)
            self.assertNotIn("rm --", message)
            self.assertTrue(lock.exists())

    def test_stale_lock_gives_conditional_recovery_and_preserves_pacing(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run lock"
            lock.write_text("12345")
            with patch("automats.runner.os.name", "posix"), \
                    patch("automats.runner.os.kill", side_effect=ProcessLookupError):
                message = str(lock_error(lock))
            self.assertIn("no longer exists", message)
            self.assertIn("Only then remove", message)
            self.assertIn("rm -- '", message)
            self.assertIn("five minutes", message)
            self.assertIn("Do not delete pacing.json", message)
            self.assertTrue(lock.exists())

    def test_invalid_pid_does_not_probe_or_automatically_remove_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run.lock"
            for value in ("", "abc", "0", "-1"):
                lock.write_text(value)
                with self.subTest(value=value), patch("automats.runner.os.kill") as probe:
                    message = str(lock_error(lock))
                probe.assert_not_called()
                self.assertIn("PID is unreadable", message)
                self.assertTrue(lock.exists())

    def test_lock_failure_prints_guidance_before_browser_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / ".cache" / "automats"
            cache.mkdir(parents=True)
            lock = cache / "run.lock"
            lock.write_text("12345")
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("automats.runner.os.kill", side_effect=ProcessLookupError), \
                    patch("playwright.sync_api.sync_playwright") as browser:
                with self.assertRaisesRegex(RunError, "rerun your original command"):
                    run_plan(build_plan(example()), root / "figures")
                browser.assert_not_called()
            self.assertTrue(lock.exists())


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pacer = QueryPacer(Settings(), self.clock.clock, self.clock.wait)

    def test_queries_and_retries_are_sequentially_paced(self):
        self.pacer.before_query()
        self.assertEqual(self.clock.now, 0)
        self.clock.now += 5
        self.pacer.after_plot()
        self.pacer.before_query()
        self.assertEqual(self.clock.now, 6)
        self.clock.now += 5
        self.pacer.after_plot()
        self.pacer.before_query()
        self.assertEqual(self.clock.now, 12)

    def test_long_plots_still_have_the_full_wait_after_completion(self):
        self.pacer.before_query()
        self.clock.now += 45
        self.pacer.after_plot()
        self.pacer.before_query()
        self.assertEqual(self.clock.now, 46)
        self.assertEqual(self.clock.waits, [1])

    def test_ten_second_gap_follows_short_and_long_plots_and_keeps_cooldown(self):
        pacer = QueryPacer(Settings(inter_plot_waiting_seconds=10), self.clock.clock, self.clock.wait)
        pacer.before_query()
        self.clock.now += 4
        pacer.after_plot()
        pacer.before_query()
        self.assertEqual(self.clock.now, 14)
        self.assertEqual(self.clock.waits, [10])
        self.clock.now += 12
        pacer.after_plot()
        pacer.before_query()
        self.assertEqual(self.clock.now, 36)
        self.assertEqual(self.clock.waits, [10, 10])
        pacer.cooldown()
        pacer.before_query()
        self.assertEqual(self.clock.now, 216)

    def test_completed_plot_gap_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pacing.json"
            wall = lambda: 10000 + self.clock.now
            pacer = QueryPacer(Settings(), self.clock.clock, self.clock.wait, path, wall)
            pacer.before_query()
            self.clock.now += 45
            pacer.after_plot()
            restarted = QueryPacer(Settings(), self.clock.clock, self.clock.wait, path, wall)
            self.assertEqual(restarted.remaining(), 1)
            self.assertEqual(restarted.wait_reason, "Minimum wait between plots")

    def test_throttling_and_retry_after_extend_the_gate(self):
        self.pacer.before_query()
        self.pacer.cooldown()
        self.pacer.before_query()
        self.assertEqual(self.clock.now, 180)
        self.pacer.cooldown(retry_after=600)
        self.pacer.before_query()
        self.assertEqual(self.clock.now, 780)

    def test_deadlines_survive_process_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pacing.json"
            wall = lambda: self.clock.now + 10000
            first = QueryPacer(Settings(), self.clock.clock, self.clock.wait, path, wall)
            first.before_query()
            first.cooldown(600, reason="MATS HTTP 429 rate-limit protection")
            restarted = QueryPacer(Settings(), self.clock.clock, self.clock.wait, path, wall)
            self.assertEqual(restarted.wait_reason, "MATS HTTP 429 rate-limit protection")
            restarted.before_query()
            self.assertEqual(self.clock.now, 600)

    def test_legacy_deadlines_are_honored_without_guessing_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pacing.json"
            path.write_text(json.dumps({"next_request_at": 10200}))
            original = path.read_bytes()
            pacer = QueryPacer(Settings(), self.clock.clock, self.clock.wait, path, lambda: 10000)
            self.assertEqual(pacer.remaining(), 200)
            self.assertIn("reason not recorded", pacer.wait_reason)
            self.assertEqual(path.read_bytes(), original)

    def test_retry_after_supports_seconds_dates_and_invalid_values(self):
        self.assertEqual(retry_after_seconds("600"), 600)
        self.assertEqual(retry_after_seconds("Thu, 01 Jan 1970 00:10:00 GMT", now=100), 500)
        for value in ("nan", "inf", "garbage", "-5"):
            self.assertEqual(retry_after_seconds(value), 0)


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.plan = build_plan(example())
        self.job = self.plan.jobs[0]

    def test_empty_plan_does_not_start_browser(self):
        plan = replace(self.plan, jobs=())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("playwright.sync_api.sync_playwright") as browser:
                self.assertEqual(run_plan(plan, root / "figures", log=lambda message: None), 0)
                browser.assert_not_called()

    def test_default_skip_existing_all_figures_does_not_start_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "figures"
            for job in self.plan.jobs:
                target = output / job.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png_image())
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("playwright.sync_api.sync_playwright") as browser:
                saved = run_plan(self.plan, output, log=lambda message: None)
                self.assertEqual(saved, 0)
                browser.assert_not_called()

    def test_default_skip_existing_partial_figures_only_plots_missing(self):
        plan = replace(self.plan, jobs=self.plan.jobs[:2])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "figures"
            first_target = output / plan.jobs[0].relative_path
            first_target.parent.mkdir(parents=True, exist_ok=True)
            first_target.write_bytes(png_image())
            plotted_jobs = []

            def plot(job):
                plotted_jobs.append(job)
                return png_image(), {}

            with patch("automats.runner.Path.home", return_value=root), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.plot.side_effect = plot
                saved = run_plan(plan, output, log=lambda message: None)
                self.assertEqual(saved, 1)
                self.assertEqual(plotted_jobs, [plan.jobs[1]])

    def test_update_regenerates_existing_figures(self):
        plan = replace(self.plan, jobs=self.plan.jobs[:2])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "figures"
            for job in plan.jobs:
                target = output / job.relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(png_image())
            plotted_jobs = []

            def plot(job):
                plotted_jobs.append(job)
                return png_image(), {}

            with patch("automats.runner.Path.home", return_value=root), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.plot.side_effect = plot
                saved = run_plan(plan, output, update=True, log=lambda message: None)
                self.assertEqual(saved, 2)
                self.assertEqual(plotted_jobs, list(plan.jobs))

    def test_store_accepts_only_png_and_rejects_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory), self.plan.settings.base_url)
            image = png_image()
            store.save(self.job, image, {})
            self.assertTrue(store.completed(self.job))
            with self.assertRaisesRegex(RunError, "PNG image"):
                store.save(self.job, b"\xff\xd8old jpeg\xff\xd9", {})
            self.assertEqual((Path(directory) / self.job.relative_path).read_bytes(), image)

    def test_exact_case_insensitive_model_matching(self):
        self.assertEqual(resolve_option("RRFSv2x", ["RRFSv2X", "RRFSv2X_OTHER"], "model"), "RRFSv2X")
        for options in (["RRFSv2X_OTHER"], ["RRFSv2X", "rrfsv2x"]):
            with self.assertRaises(RunError):
                resolve_option("RRFSv2x", options, "model")

    def test_runner_waits_ten_seconds_after_a_long_plot_not_after_final_plot(self):
        plan = replace(self.plan, jobs=self.plan.jobs[:2],
                       settings=replace(self.plan.settings, inter_plot_waiting_seconds=10))
        clock = FakeClock()
        pacer = QueryPacer(plan.settings, clock.clock, clock.wait)
        starts = []

        def plot(job):
            starts.append(clock.now)
            clock.now += 45
            return png_image(), {}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("automats.runner.QueryPacer", return_value=pacer), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.plot.side_effect = plot
                self.assertEqual(run_plan(plan, root / "figures", log=lambda message: None), 2)
        self.assertEqual(starts, [0, 55])
        self.assertEqual(clock.waits, [10])
        self.assertEqual(clock.now, 100)

    def test_pre_query_failure_does_not_create_or_extend_cooldown(self):
        plan = replace(self.plan, jobs=(self.job,))
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / ".cache" / "automats" / "pacing.json"
            pacer = QueryPacer(plan.settings, clock.clock, clock.wait, state_path,
                               wall_clock=lambda: 10000 + clock.now)
            pacer.before_query()
            original = state_path.read_bytes()
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("automats.runner.QueryPacer", return_value=pacer), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.prepare.side_effect = RunError("Unknown experiment")
                with self.assertRaisesRegex(RunError, "Unknown experiment"):
                    run_plan(plan, root / "figures", log=lambda message: None)
                client.return_value.plot.assert_not_called()
            self.assertEqual(clock.now, 1)
            self.assertEqual(pacer.next_start, 1)
            self.assertEqual(state_path.read_bytes(), original)

    def test_submitted_query_failure_keeps_cooldown_and_does_not_retry(self):
        plan = replace(self.plan, jobs=(self.job,))
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / ".cache" / "automats" / "pacing.json"
            pacer = QueryPacer(plan.settings, clock.clock, clock.wait, state_path,
                               wall_clock=lambda: 10000 + clock.now)
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("automats.runner.QueryPacer", return_value=pacer), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.plot.side_effect = RunError("Query completion is unknown")
                with self.assertRaisesRegex(RunError, "Query completion is unknown"):
                    run_plan(plan, root / "figures", log=lambda message: None)
                client.return_value.plot.assert_called_once()
            self.assertEqual(pacer.next_start, 180)
            self.assertEqual(json.loads(state_path.read_text())["next_request_at"], 10180)
            self.assertIn("submitted query", json.loads(state_path.read_text())["reason"])

    def test_save_failure_after_completed_query_does_not_add_cooldown(self):
        plan = replace(self.plan, jobs=(self.job,))
        clock = FakeClock()
        pacer = QueryPacer(plan.settings, clock.clock, clock.wait)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("automats.runner.QueryPacer", return_value=pacer), \
                    patch("automats.runner.ResultStore") as store, \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.plot.return_value = (png_image(), {})
                store.return_value.save.side_effect = OSError("Disk full")
                with self.assertRaisesRegex(OSError, "Disk full"):
                    run_plan(plan, root / "figures", log=lambda message: None)
                client.return_value.plot.assert_called_once()
            self.assertEqual(pacer.next_start, 1)

    def test_throttled_preparation_keeps_retry_after_protection(self):
        plan = replace(self.plan, jobs=(self.job,))
        clock = FakeClock()
        pacer = QueryPacer(plan.settings, clock.clock, clock.wait)
        status = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("automats.runner.QueryPacer", return_value=pacer), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.prepare.side_effect = [Throttled("HTTP 429", 600), None]
                client.return_value.plot.return_value = (png_image(), {})
                self.assertEqual(run_plan(plan, root / "figures", log=lambda message: None, status=status), 1)
                client.return_value.plot.assert_called_once()
            self.assertEqual(clock.now, 600)
            self.assertTrue(any("HTTP 429" in call.args[0] and call.kwargs.get("wait_seconds") == 600
                                for call in status.call_args_list))

    def test_unique_parameter_matching_and_aliases(self):
        self.assertEqual(resolve_option("dieoff type", ["dieoff-type", "region-type"], "parameter"), "dieoff-type")
        self.assertEqual(resolve_option("Temperature at 2m", ["Temperature at 2m (C)", "Dewpoint"], "variable"),
                         "Temperature at 2m (C)")
        self.assertEqual(resolve_option("t2m", ["Temperature at 2m (C)"], "variable",
                                        {"t2m": "Temperature at 2m"}), "Temperature at 2m (C)")
        self.assertEqual(resolve_option("Wind", ["Wind", "Wind Speed"], "variable"), "Wind")

        # Threshold matching with aliases: "1" matches "1 (visibility < 1 mi)" instead of "1/2" or "10"
        vis_options = [
            "1/2 (visibility < 1/2 mi)",
            "1 (visibility < 1 mi)",
            "10 (visibility < 10 mi)",
            "2 (visibility < 2 mi)",
            "3 (visibility < 3 mi)",
            "5 (visibility < 5 mi)",
        ]
        vis_aliases = {"1": "1 (visibility", "1/2": "1/2 (visibility", "10": "10 (visibility"}
        self.assertEqual(resolve_option("1", vis_options, "threshold", vis_aliases), "1 (visibility < 1 mi)")
        self.assertEqual(resolve_option("1/2", vis_options, "threshold", vis_aliases), "1/2 (visibility < 1/2 mi)")
        self.assertEqual(resolve_option("10", vis_options, "threshold", vis_aliases), "10 (visibility < 10 mi)")
        self.assertEqual(resolve_option("3", vis_options, "threshold", vis_aliases), "3 (visibility < 3 mi)")
        self.assertEqual(resolve_option("5", vis_options, "threshold", vis_aliases), "5 (visibility < 5 mi)")

        for value, options in (("speed", ["U Speed", "V Speed"]), ("xxxxx", ["Dieoff"])):
            with self.assertRaises(RunError):
                resolve_option(value, options, "variable")

    def test_missing_setting_and_experiment_errors_are_explicit_and_bounded(self):
        for parameter, requested, options, subject in (
                ("parameter", "typo key", ["region", "variable"], "MATS setting name 'typo key'"),
                ("truth", "QC-METAR", ["METAR"], "Setting 'truth', value 'QC-METAR'"),
                ("data-source", "UNKNOWN", [f"MODEL_{index}" for index in range(2000)], "Experiment/model 'UNKNOWN'")):
            with self.subTest(parameter=parameter), self.assertRaises(ResolutionError) as error:
                resolve_option(requested, options, parameter)
            self.assertIn(subject, str(error.exception))
            self.assertIn("No automatic substitution", str(error.exception))
            self.assertTrue(error.exception.missing)
            self.assertLess(len(str(error.exception)), 1000)

    def test_runtime_error_identifies_plot_and_pre_query_stage(self):
        with tempfile.TemporaryDirectory() as directory, patch("playwright.sync_api.sync_playwright"):
            with patch("automats.runner.Path.home", return_value=Path(directory)), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.prepare.side_effect = RunError("Experiment/model 'UNKNOWN' is unavailable")
                with self.assertRaises(RunError) as error:
                    run_plan(self.plan, Path(directory) / "figures")
                message = str(error.exception)
                self.assertIn("Plot 'ua_profile' (upperair / profile)", message)
                self.assertIn("Experiment/model 'UNKNOWN'", message)
                self.assertIn("before submitting this figure's plot query", message)
                client.return_value.plot.assert_not_called()

    def test_effective_curve_validation_covers_defaults(self):
        expected = [{"label": "model", "data-source": "MODEL", "statistic": "RMSE",
                     "forecast-length": ["0", "1"], "dieoff-type": "Dieoff"}]
        validate_effective_curves(deepcopy(expected), expected)
        changed = deepcopy(expected)
        changed[0]["statistic"] = "Bias"
        with self.assertRaises(RunError):
            validate_effective_curves(changed, expected)

    def test_cache_requires_matching_effective_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory), self.plan.settings.base_url)
            effective = {"statistic": "RMSE", "dieoff-type": "Dieoff"}
            store.save(self.job, png_image(), {"effective_settings": effective})
            self.assertTrue(store.completed(self.job, effective=effective))
            self.assertFalse(store.completed(self.job, effective={"statistic": "Bias"}))

    def test_update_regenerates_even_a_matching_existing_figure(self):
        plan = replace(self.plan, jobs=(self.job,))
        clock = FakeClock()
        pacer = QueryPacer(plan.settings, clock.clock, clock.wait)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "figures"
            store = ResultStore(output, plan.settings.base_url)
            store.save(self.job, png_image((1, 1, 1)), {"effective_settings": {}})
            self.assertTrue(store.completed(self.job, effective={}))
            images = [png_image((2, 2, 2)), png_image((3, 3, 3))]
            with patch("automats.runner.Path.home", return_value=Path(directory)), \
                    patch("automats.runner.QueryPacer", return_value=pacer), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.effective_settings = {}
                client.return_value.plot.side_effect = [(image, {"effective_settings": {}}) for image in images]
                for image in images:
                    self.assertEqual(run_plan(plan, output, update=True, log=lambda message: None), 1)
                    self.assertEqual((output / self.job.relative_path).read_bytes(), image)
                self.assertEqual(client.return_value.plot.call_count, 2)

    def test_failed_regeneration_preserves_the_previous_figure(self):
        plan = replace(self.plan, jobs=(self.job,))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "figures"
            old_image = png_image((1, 1, 1))
            ResultStore(output, plan.settings.base_url).save(self.job, old_image, {"effective_settings": {}})
            with patch("automats.runner.Path.home", return_value=Path(directory)), \
                    patch("playwright.sync_api.sync_playwright"), \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.effective_settings = {}
                client.return_value.plot.side_effect = RunError("MATS query failed")
                with self.assertRaisesRegex(RunError, "MATS query failed"):
                    run_plan(plan, output, update=True, log=lambda message: None)
            self.assertEqual((output / self.job.relative_path).read_bytes(), old_image)

    def test_curve_validation_rejects_wrong_dates_leads_and_models(self):
        models = ["RRFSv2X", "RRFSv2B"]
        curves = [{"label": experiment.label, "data-source": model,
                   "database": self.job.truth, "region-type": "Predefined region",
                   "region": self.job.region, "variable": self.job.variable,
                   "statistic": "RMSE", "curve-dates": self.job.mats_dates,
                   "forecast-length": ["0"], "top": "1", "bottom": "1000",
                   "valid-time": "unused"}
                  for experiment, model in zip(self.job.experiments, models)]
        validate_curves(self.job, curves, models)
        for parameter, value in (("data-source", "HRRR_OPS"), ("forecast-length", ["0", "1"]),
                                 ("curve-dates", "wrong dates"), ("region", "wrong region")):
            changed = deepcopy(curves)
            changed[0][parameter] = value
            with self.assertRaises(RunError):
                validate_curves(self.job, changed, models)
        with self.assertRaises(RunError):
            validate_curves(self.job, curves[:1], models)

    def test_resume_requires_matching_endpoint_config_and_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ResultStore(root, self.plan.settings.base_url)
            self.assertFalse(store.completed(self.job))
            store.save(self.job, png_image(), {"result_key": "test"})
            self.assertTrue(ResultStore(root, self.plan.settings.base_url).completed(self.job))
            self.assertFalse(ResultStore(root, "https://gsl.noaa.gov/mats").completed(self.job))
            raw = example()
            raw["common"]["end"] = "2026-10-16T00:00:00Z"
            self.assertFalse(store.completed(build_plan(raw).jobs[0]))
            (root / self.job.relative_path).write_bytes(png_image((4, 4, 4)))
            self.assertFalse(store.completed(self.job))

    def test_rejects_non_png_without_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ResultStore(root, self.plan.settings.base_url)
            with self.assertRaises(RunError):
                store.save(self.job, b"not a PNG", {})
            self.assertFalse((root / self.job.relative_path).exists())

    def test_combined_curves_validate_every_metric_and_require_all_curves(self):
        job = replace(self.job, statistic="rmse+bias")
        models = ["RRFSv2X", "RRFSv2X", "RRFSv2B", "RRFSv2B"]
        curves = [{"label": specification.label, "data-source": model,
                   "database": job.truth, "region-type": "Predefined region",
                   "region": job.region, "variable": job.variable,
                   "statistic": STATISTICS[specification.statistic], "curve-dates": job.mats_dates,
                   "forecast-length": "0", "top": "1", "bottom": "1000", "valid-time": "unused"}
                  for specification, model in zip(job.curves, models)]
        validate_curves(job, curves, models)
        changed = deepcopy(curves)
        changed[1]["statistic"] = "RMSE"
        with self.assertRaises(RunError):
            validate_curves(job, changed, models)
        with self.assertRaises(RunError):
            validate_curves(job, curves[:-1], models[:-1])
        self.assertEqual([curve.color for curve in job.curves], ["red", "red", "blue", "blue"])
        self.assertEqual([curve.dash for curve in job.curves], ["solid", "dash", "solid", "dash"])

    def test_grouped_leads_validate_each_curves_own_lead(self):
        job = replace(self.job, statistic="rmse+bias", lead=(0, 1, 3, 6, 9),
                      experiments=self.job.experiments[:1])
        models = ["RRFSv2X"] * len(job.curves)
        curves = [{"label": specification.label, "data-source": model,
                   "database": job.truth, "region-type": "Predefined region",
                   "region": job.region, "variable": job.variable,
                   "statistic": STATISTICS[specification.statistic], "curve-dates": job.mats_dates,
                   "forecast-length": str(specification.lead), "top": "1", "bottom": "1000",
                   "valid-time": "unused"} for specification, model in zip(job.curves, models)]
        validate_curves(job, curves, models)
        curves[3]["forecast-length"] = "0"
        with self.assertRaisesRegex(RunError, "wrong forecast lead"):
            validate_curves(job, curves, models)


class MetadataTests(unittest.TestCase):
    def test_png_metadata_preserves_first_four_mats_colors(self):
        from playwright.sync_api import sync_playwright

        job = build_plan(example()).jobs[0]
        colors = ["rgb(255,0,0)", "rgb(0,0,255)", "rgb(255,165,0)", "rgb(95,95,95)"]
        with sync_playwright() as manager:
            browser = manager.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                source = bytes(page.evaluate("""colors => {
                    const canvas=document.createElement('canvas');canvas.width=1200;canvas.height=180;
                    const context=canvas.getContext('2d');context.fillStyle='white';context.fillRect(0,0,1200,180);
                    colors.forEach((color,index)=>{
                        context.strokeStyle=color;context.lineWidth=2;context.beginPath();
                        context.moveTo(20,20+35*index);context.lineTo(1180,20+35*index);context.stroke();
                    });
                    return Array.from(Uint8Array.from(atob(canvas.toDataURL('image/png').split(',')[1]),
                        character=>character.charCodeAt(0)));
                }""", colors))
                image, metadata = annotate_image(page, job, source)
                self.assertTrue(is_png(image))
                pixels = page.evaluate("""async ({image,top}) => {
                    const source=new Image();source.src=image;await source.decode();
                    const canvas=document.createElement('canvas');canvas.width=source.width;canvas.height=source.height;
                    const context=canvas.getContext('2d');context.drawImage(source,0,0);
                    return [0,1,2,3].map(index=>Array.from(context.getImageData(260,top+20+35*index,1,1).data));
                }""", {"image": "data:image/png;base64," + base64.b64encode(image).decode(),
                        "top": metadata["header_height"]})
                self.assertEqual(pixels, [[255, 0, 0, 255], [0, 0, 255, 255],
                                          [255, 165, 0, 255], [95, 95, 95, 255]])
            finally:
                browser.close()

    def test_export_uses_names_and_compact_layout_without_network(self):
        from playwright.sync_api import sync_playwright

        job = build_plan(example()).jobs[0]
        with sync_playwright() as manager:
            try:
                browser = manager.chromium.launch(channel="chrome", headless=True)
            except Exception as error:
                self.skipTest(f"Installed Chrome unavailable: {error}")
            try:
                page = browser.new_page()
                page.evaluate("""names => {
                    const graph = document.createElement('div');
                    graph.id = 'placeholder'; document.body.append(graph);
                    graph.data = names.map(name => ({label:name, x:[1], y:[1000]}));
                    graph.layout = {margin:{t:200}, legend:{y:1.2}, annotations:[
                        {text:'This plot is for research purposes only.', y:1.1},
                        {text:'x = 0', x:0, y:1}
                    ]};
                    globalThis.Session = {get:() => 'offline-test'};
                    globalThis.matsCurveUtils = {getGraphResult:() => ({basis:{plotParams:{}}})};
                    globalThis.Plotly = {
                        restyle:async (graph, changes, indices) => {
                            graph.data[indices[0]].name = changes.name;
                            graph.data[indices[0]].exportStyle = changes;
                        },
                        relayout:async (graph, changes) => {
                            graph.changes = changes;
                        },
                        toImage:async (graph,options) => {
                            if (options.format !== 'png') throw Error('Expected PNG export');
                            const canvas=document.createElement('canvas');canvas.width=1;canvas.height=1;
                            return canvas.toDataURL('image/png');
                        }
                    };
                }""", [experiment.name for experiment in job.experiments])
                exported = page.evaluate(EXPORT_IMAGE, {
                    "curves": [asdict(curve) for curve in job.curves],
                    "width": job.width, "height": job.height,
                })
                actual = page.evaluate("""() => {
                    const graph = document.getElementById('placeholder');
                    return {names:graph.data.map(trace => trace.name), changes:graph.changes};
                }""")
                self.assertEqual(actual["names"], ["RRFSv2x", "RRFSv2b"])
                changes = actual["changes"]
                self.assertEqual(changes["margin.t"], 6)
                self.assertFalse(changes["showlegend"])
                self.assertNotIn("disclaimer", exported["metadata"])
                self.assertEqual(changes["title.text"], "")
                self.assertEqual(changes["annotations"], [{"text": "x = 0", "x": 0, "y": 1}])
                self.assertEqual([curve["name"] for curve in exported["metadata"]["curves"]], actual["names"])
                combined = replace(job, statistic="rmse+bias")
                page.evaluate("""labels => {
                    document.getElementById('placeholder').data = labels.map(label => ({label, x:[1], y:[1000]}));
                }""", [curve.label for curve in combined.curves])
                exported = page.evaluate(EXPORT_IMAGE, {
                    "curves": [asdict(curve) for curve in combined.curves],
                    "width": combined.width, "height": combined.height,
                })
                styles = page.evaluate("() => document.getElementById('placeholder').data.map(trace => trace.exportStyle)")
                self.assertEqual([style["line.color"] for style in styles], ["red", "red", "blue", "blue"])
                self.assertEqual([style["line.dash"] for style in styles], ["solid", "dash", "solid", "dash"])
                self.assertTrue(all(style["line.width"] == 2 and style["marker.size"] == 7 for style in styles))
                page.evaluate(EXPORT_IMAGE, {
                    "curves": [asdict(curve) for curve in combined.curves],
                    "width": combined.width, "height": combined.height,
                    "lineWeight": 3, "markerWeight": 9,
                })
                styles = page.evaluate("() => document.getElementById('placeholder').data.map(trace => trace.exportStyle)")
                self.assertTrue(all(style["line.width"] == 3 and style["marker.size"] == 9 for style in styles))
                self.assertEqual([curve["statistic"] for curve in exported["metadata"]["curves"]],
                                 ["rmse", "bias", "rmse", "bias"])
                page.evaluate("() => document.getElementById('placeholder').data.pop()")
                with self.assertRaisesRegex(Exception, "Missing or ambiguous curve"):
                    page.evaluate(EXPORT_IMAGE, {
                        "curves": [asdict(curve) for curve in combined.curves],
                        "width": combined.width, "height": combined.height,
                    })
                grouped = replace(combined, lead=(0, 1, 3, 6, 9), experiments=job.experiments[:1])
                page.evaluate("""labels => {
                    document.getElementById('placeholder').data = labels.map(label => ({label, x:[1], y:[1000]}));
                }""", [curve.label for curve in grouped.curves])
                exported = page.evaluate(EXPORT_IMAGE, {
                    "curves": [asdict(curve) for curve in grouped.curves], "width": grouped.width, "height": grouped.height})
                self.assertEqual(len(exported["metadata"]["curves"]), 10)
                self.assertEqual([curve["lead"] for curve in exported["metadata"]["curves"]],
                                 [0, 0, 1, 1, 3, 3, 6, 6, 9, 9])

                # Test allowPartial with missing / 0-data curves
                page.evaluate("""() => {
                    const graph = document.getElementById('placeholder');
                    graph.data = [
                        {label: 'RRFSv2xF000RMSE', x: [1], y: [1000]},
                        {label: 'RRFSv2xF000BIAS', x: [], y: []}
                    ];
                }""")
                partial_exported = page.evaluate(EXPORT_IMAGE, {
                    "curves": [asdict(grouped.curves[0]), asdict(grouped.curves[1])],
                    "width": grouped.width, "height": grouped.height,
                    "allowPartial": True,
                })
                self.assertEqual(len(partial_exported["metadata"]["curves"]), 1)
                self.assertEqual(partial_exported["metadata"]["curves"][0]["label"], "RRFSv2xF000RMSE")
            finally:
                browser.close()

    def test_header_matches_requested_compact_format(self):
        job = build_plan(example()).jobs[0]
        self.assertEqual(figure_header(job), [
            "2026-10-01 00:00 - 2026-10-15 00:00, f000h, RAOBs (Traditional), "
            "HRRR domain, 1-1000 hPa, T, RMSE"])
        changed = replace(job, lead=12, statistic="bias", variable="RHobT", variable_tag="RHobT")
        self.assertTrue(figure_header(changed)[0].endswith(", RHobT, BIAS"))
        self.assertIn("f012h", figure_header(changed)[0])
        self.assertEqual(figure_header(replace(job, lead=None)), [
            "2026-10-01 00:00 - 2026-10-15 00:00, RAOBs (Traditional), "
            "HRRR domain, 1-1000 hPa, T, RMSE"])

    def test_header_is_rendered_above_the_image_without_network(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed")
        job = build_plan(example()).jobs[0]
        with sync_playwright() as manager:
            try:
                browser = manager.chromium.launch(channel="chrome", headless=True)
            except Exception as error:
                self.skipTest(f"Installed Chrome unavailable: {error}")
            try:
                page = browser.new_page()
                image = page.evaluate("""() => {
                    const canvas = document.createElement('canvas');
                    canvas.width = 1200; canvas.height = 1200;
                    const context = canvas.getContext('2d');
                    context.fillStyle = 'red'; context.fillRect(0, 0, 1200, 1200);
                    return Array.from(Uint8Array.from(atob(canvas.toDataURL('image/png').split(',')[1]),
                        character => character.charCodeAt(0)));
                }""")
                annotated, metadata = annotate_image(page, job, bytes(image))
                self.assertTrue(annotated.startswith(PNG_SIGNATURE))
                self.assertEqual(metadata["width"], 1200)
                self.assertEqual(metadata["height"], 1200 + metadata["header_height"])
                self.assertEqual(metadata["header_height"], 60)
                self.assertEqual(metadata["legend_rows"], 1)
                self.assertEqual(metadata["legend_names"], [experiment.name for experiment in job.experiments])
                self.assertEqual(metadata["header"], figure_header(job))
                self.assertNotIn("disclaimer", metadata)
                combined = replace(job, statistic="rmse+bias")
                combined_image, combined_metadata = annotate_image(page, combined, bytes(image))
                self.assertEqual(combined_metadata["legend_names"], ["RRFSv2x", "RRFSv2b"])
                self.assertEqual(combined_metadata["legend_styles"], ["solid"])
                extra_swatch_pixels = page.evaluate("""async image => {
                    const source = new Image(); source.src = image; await source.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = source.naturalWidth; canvas.height = source.naturalHeight;
                    const context = canvas.getContext('2d'); context.drawImage(source, 0, 0);
                    const pixels = context.getImageData(50, 30, 36, 24).data;
                    let red = 0;
                    for (let index = 0; index < pixels.length; index += 4)
                        if (pixels[index] > 180 && pixels[index+1] < 100 && pixels[index+2] < 100) red++;
                    return red;
                }""", "data:image/png;base64," + base64.b64encode(combined_image).decode())
                self.assertLess(extra_swatch_pixels, 5)
                self.assertIn("RMSE (solid) + BIAS (dashed)", combined_metadata["header"][0])
                grouped = replace(combined, lead=(0, 1, 3, 6, 9), experiments=job.experiments[:1])
                _, grouped_metadata = annotate_image(page, grouped, bytes(image))
                self.assertEqual(grouped_metadata["legend_names"],
                                 [f"RRFSv2x f{lead:03d}h" for lead in (0, 1, 3, 6, 9)])
                self.assertIn("f000+001+003+006+009h", grouped_metadata["header"][0])
                invalid_colors = replace(grouped, forecast_lead_colors=tuple((lead, "notacolor") for lead in grouped.leads))
                with self.assertRaisesRegex(Exception, "Invalid plot color"):
                    annotate_image(page, invalid_colors, bytes(image))
                pixels = page.evaluate("""async ({image, headerHeight}) => {
                    const source = new Image(); source.src = image; await source.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = source.naturalWidth; canvas.height = source.naturalHeight;
                    const context = canvas.getContext('2d'); context.drawImage(source, 0, 0);
                    const header = context.getImageData(0, 0, canvas.width, headerHeight).data;
                    let dark = 0;
                    for (let index = 0; index < header.length; index += 4)
                        if (header[index] < 100 && header[index+1] < 100 && header[index+2] < 100) dark++;
                    return {dark, chart:Array.from(context.getImageData(100, headerHeight + 100, 1, 1).data)};
                }""", {"image": "data:image/png;base64," + base64.b64encode(annotated).decode(),
                       "headerHeight": metadata["header_height"]})
                self.assertGreater(pixels["dark"], 100)
                self.assertGreater(pixels["chart"][0], 240)
                self.assertLess(pixels["chart"][1], 20)
                refreshed, refreshed_metadata = annotate_image(page, job, annotated, metadata)
                self.assertTrue(refreshed.startswith(PNG_SIGNATURE))
                self.assertEqual(refreshed_metadata["height"], 1260)
                six_experiments = example()
                six_experiments["experiments"] = [
                    {"name": f"RRFSv2_conus_3km_ret_NoObsGrp_Rec_Soil_{index}", "color": "blue"}
                    for index in range(6)]
                crowded_job = build_plan(six_experiments).jobs[0]
                _, crowded = annotate_image(page, crowded_job, bytes(image))
                self.assertEqual(crowded["legend_rows"], 6)
                self.assertEqual(crowded["height"], 1380)
                self.assertEqual(crowded["legend_names"], [experiment.name for experiment in crowded_job.experiments])
                legacy = dict(metadata, disclaimer="Legacy research disclaimer")
                cleared, _ = annotate_image(page, job, annotated, legacy)
                cleared_pixels = page.evaluate("""async ({image, headerHeight}) => {
                    const source = new Image(); source.src = image; await source.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = source.naturalWidth; canvas.height = source.naturalHeight;
                    const context = canvas.getContext('2d'); context.drawImage(source, 0, 0);
                        return {band:Array.from(context.getImageData(100, headerHeight + 4, 1, 1).data),
                            chart:Array.from(context.getImageData(100, headerHeight + 100, 1, 1).data)};
                    }""", {"image": "data:image/png;base64," + base64.b64encode(cleared).decode(),
                           "headerHeight": metadata["header_height"]})
                self.assertTrue(all(channel > 240 for channel in cleared_pixels["band"][:3]))
                self.assertGreater(cleared_pixels["chart"][0], 240)
                self.assertLess(cleared_pixels["chart"][1], 20)
                old_image = page.evaluate("""() => {
                    const canvas = document.createElement('canvas');
                    canvas.width = 1200; canvas.height = 1200;
                    const context = canvas.getContext('2d');
                    context.fillStyle = 'white'; context.fillRect(0, 0, 1200, 1200);
                    context.fillStyle = 'red'; context.fillRect(75, 111, 1050, 1088);
                    context.fillStyle = 'black'; context.fillRect(75, 110, 1050, 1);
                    return Array.from(Uint8Array.from(atob(canvas.toDataURL('image/png').split(',')[1]),
                        character => character.charCodeAt(0)));
                }""")
                upgraded, upgraded_metadata = annotate_image(page, job, bytes(old_image), {
                    "header_height": 0, "disclaimer": "Old disclaimer in removed margin"})
                border_pixels = page.evaluate("""async ({image, top}) => {
                    const source = new Image(); source.src = image; await source.decode();
                    const canvas = document.createElement('canvas');
                    canvas.width = source.naturalWidth; canvas.height = source.naturalHeight;
                    const context = canvas.getContext('2d'); context.drawImage(source, 0, 0);
                    return Array.from(context.getImageData(400, top + 4, 1, 1).data);
                }""", {"image": "data:image/png;base64," + base64.b64encode(upgraded).decode(),
                       "top": upgraded_metadata["header_height"]})
                self.assertTrue(all(channel < 100 for channel in border_pixels[:3]))

                # Difference curves legend
                diff_job = replace(job, parameters=(("Difference curves", "Diff all curves against the 1st added curve"),))
                plot_params = {
                    "curves": [
                        {"data-source": "RRFSv2x", "label": "RRFSv2x", "color": "rgb(255,0,0)", "diffFrom": None},
                        {"data-source": "RRFSv2b", "label": "RRFSv2b", "color": "rgb(0,0,255)", "diffFrom": None},
                        {"data-source": "RRFSv2b", "label": "RRFSv2b-RRFSv2x", "color": "rgb(255,165,0)", "diffFrom": [1, 0]},
                    ]
                }
                diff_annotated, diff_meta = annotate_image(page, diff_job, bytes(image), plot_params=plot_params)
                self.assertEqual(diff_meta["legend_rows"], 2)
                self.assertEqual(diff_meta["legend_names"], ["RRFSv2x", "RRFSv2b", "RRFSv2b - RRFSv2x"])
                self.assertEqual(diff_meta["diff_legend_names"], ["RRFSv2b - RRFSv2x"])
            finally:
                browser.close()

    def test_old_figures_need_local_metadata_refresh_not_a_new_query(self):
        job = build_plan(example()).jobs[0]
        with tempfile.TemporaryDirectory() as directory:
            store = ResultStore(Path(directory), "https://gsl.noaa.gov/int-mats")
            store.save(job, png_image(), {})
            del store.state["results"][job.relative_path]["figure_style_version"]
            self.assertFalse(store.completed(job))
            self.assertTrue(store.completed(job, current_style=False))

    def test_metadata_only_skips_missing_figures_without_opening_browser(self):
        plan = build_plan(example())
        with tempfile.TemporaryDirectory() as directory, patch("playwright.sync_api.sync_playwright") as browser:
            with patch("automats.runner.Path.home", return_value=Path(directory)), redirect_stdout(StringIO()):
                self.assertEqual(run_plan(plan, Path(directory) / "figures", metadata_only=True), 0)
            browser.assert_not_called()


class ControlPacingTests(unittest.TestCase):
    def setUp(self):
        from automats.browser import CONTROLS_READY

        self.clock = FakeClock()
        self.client = UpperAirBrowser(Mock(), "https://gsl.noaa.gov/mats", minimize=False)
        self.client.page = Mock()
        self.client.page.evaluate.side_effect = lambda script: True if script == CONTROLS_READY else None
        self.client.page.wait_for_timeout.side_effect = lambda milliseconds: self.clock.wait(milliseconds / 1000)
        self.client._parameters = Mock(return_value=[{"name": "variable", "value": "T", "options": ["T"]}])
        clock_patch = patch("automats.browser.time.monotonic", side_effect=self.clock.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def test_control_buffer_is_point_two_seconds_by_default(self):
        self.client._settle_controls()
        self.assertAlmostEqual(self.clock.now, 0.2)
        self.assertEqual(self.client.control_waiting_seconds, 0.2)

    def test_parameter_changes_wait_before_returning_accepted_values(self):
        from automats.browser import CONTROLS_READY

        self.client.application = "surface"
        self.client.expected = {}
        self.client._resolve_parameter = Mock(return_value={
            "name": "label", "disabled": False, "options": [], "type": "textInput"})
        self.client._parameters.return_value = [{"name": "label", "value": "next"}]
        self.client.page.evaluate.side_effect = lambda script, *arguments: True if script == CONTROLS_READY else None
        self.assertEqual(self.client._set("label", "next"), "next")
        self.assertAlmostEqual(self.clock.now, 0.2)
        self.assertEqual(self.client.expected, {"label": "next"})

    def test_difference_curves_parameter_resolution_and_setting(self):
        from automats.browser import CONTROLS_READY

        self.client.application = "surface"
        self.client.expected = {}
        plot_format_param = {
            "name": "plotFormat", "label": "Difference Curves", "disabled": False,
            "type": "select", "scope": "plot", "value": "none",
            "options": [
                {"value": "none", "label": "none"},
                {"value": "Diff all curves against the 1st added curve", "label": "Diff all curves against the 1st added curve"},
                {"value": "Diff the 1st and 2nd curves, 3rd and 4th, etc", "label": "Diff the 1st and 2nd curves, 3rd and 4th, etc"},
            ],
        }
        self.client._parameters = Mock(return_value=[plot_format_param])
        self.client.page.evaluate.side_effect = lambda script, *args: True if script == CONTROLS_READY else None
        resolved = self.client._resolve_parameter("Difference curves")
        self.assertEqual(resolved["name"], "plotFormat")
        self.assertEqual(self.client._resolve_parameter("diff")["name"], "plotFormat")
        self.assertEqual(self.client._resolve_parameter("difference_curves")["name"], "plotFormat")

        # Set difference curves using alias
        self.client._parameters = Mock(return_value=[{
            **plot_format_param, "value": "Diff all curves against the 1st added curve"
        }])
        actual = self.client._set("Difference curves", "Diff all curves against the 1st added curve")
        self.assertEqual(actual, "Diff all curves against the 1st added curve")
        self.assertEqual(self.client.expected["plotFormat"], "Diff all curves against the 1st added curve")

    def test_visibility_threshold_setting(self):
        from automats.browser import CONTROLS_READY

        self.client.application = "ceil-vis"
        self.client.expected = {"variable": "visibility"}
        thresh_param = {
            "name": "threshold", "label": "Threshold", "disabled": False,
            "type": "select", "scope": "curve", "value": "none",
            "options": [
                {"value": "1/2 (visibility < 1/2 mi)", "label": "1/2 (visibility < 1/2 mi)"},
                {"value": "1 (visibility < 1 mi)", "label": "1 (visibility < 1 mi)"},
                {"value": "10 (visibility < 10 mi)", "label": "10 (visibility < 10 mi)"},
                {"value": "3 (visibility < 3 mi)", "label": "3 (visibility < 3 mi)"},
            ],
        }
        self.client._parameters = Mock(return_value=[{
            **thresh_param, "value": "1 (visibility < 1 mi)"
        }])
        self.client.page.evaluate.side_effect = lambda script, *args: True if script == CONTROLS_READY else None
        self.client._resolve_parameter = Mock(return_value=thresh_param)
        actual = self.client._set("threshold", "1")
        self.assertEqual(actual, "1 (visibility < 1 mi)")
        self.assertEqual(self.client.expected["threshold"], "1 (visibility < 1 mi)")

    def test_dependent_value_changes_restart_the_stable_interval(self):
        self.client._parameters.side_effect = lambda: [
            {"name": "model", "options": ["old" if self.clock.now < 0.1 else "updated"]}]
        self.client._settle_controls()
        self.assertGreaterEqual(self.clock.now, 0.3)
        self.assertLess(self.clock.now, 0.5)

    def test_pending_metadata_methods_delay_the_stable_interval(self):
        from automats.browser import CONTROLS_READY

        self.client.page.evaluate.side_effect = lambda script: self.clock.now >= 0.3 if script == CONTROLS_READY else None
        self.client._settle_controls()
        self.assertGreaterEqual(self.clock.now, 0.4)

    def test_control_settling_has_a_bounded_timeout(self):
        self.client._parameters.side_effect = lambda: [{"changing": self.clock.now}]
        with self.assertRaisesRegex(RunError, "did not settle within 30 seconds"):
            self.client._settle_controls()
        self.assertLess(self.clock.now, 30.2)

    def test_access_denial_interrupts_control_pacing(self):
        def wait(milliseconds):
            self.clock.wait(milliseconds / 1000)
            self.client.network_error = RunError("HTTP 403")

        self.client.page.wait_for_timeout.side_effect = wait
        with self.assertRaisesRegex(RunError, "HTTP 403"):
            self.client._settle_controls()
        self.assertEqual(self.client.page.wait_for_timeout.call_count, 1)


class NetworkErrorTests(unittest.TestCase):
    def test_metadata_denials_report_only_safe_url_and_diagnostic_headers(self):
        for status in (401, 403):
            client = UpperAirBrowser(Mock(), "https://gsl.noaa.gov/mats", minimize=False)
            response = Mock(url="https://gsl.noaa.gov/mats/upperair/sockjs/info?token=secret#fragment",
                            status=status, headers={"server": "cloudflare", "cf-ray": "test-ray-id",
                                                    "cf-mitigated": "challenge", "set-cookie": "private-cookie"})
            response.request.resource_type = "fetch"
            client._response(response)
            with self.subTest(status=status), self.assertRaises(RunError) as result:
                client._error()
            message = str(result.exception)
            self.assertIn(f"denied metadata connection: HTTP {status}", message)
            self.assertIn("https://gsl.noaa.gov/mats/upperair/sockjs/info", message)
            self.assertIn("cf-ray=test-ray-id", message)
            self.assertIn("cf-mitigated=challenge", message)
            self.assertNotIn("secret", message)
            self.assertNotIn("fragment", message)
            self.assertNotIn("private-cookie", message)

    def test_irrelevant_denials_do_not_stop_mats_initialization(self):
        for url in ("https://gsl.noaa.gov/packages/font.ttf", "https://example.com/mats/sockjs/info",
                    "https://gsl.noaa.gov/int-mats/upperair/sockjs/info"):
            client = UpperAirBrowser(Mock(), "https://gsl.noaa.gov/mats", minimize=False)
            response = Mock(url=url, status=403, headers={})
            response.request.resource_type = "font"
            client._response(response)
            with self.subTest(url=url):
                self.assertIsNone(client.network_error)

    def test_document_denials_and_rate_limits_keep_their_distinct_errors(self):
        client = UpperAirBrowser(Mock(), "https://gsl.noaa.gov/int-mats", minimize=False)
        response = Mock(url="https://gsl.noaa.gov/int-mats/radar", status=403, headers={})
        response.request.resource_type = "document"
        client._response(response)
        self.assertIn("denied page access: HTTP 403", str(client.network_error))
        response.status = 429
        response.headers = {"retry-after": "240"}
        client._response(response)
        self.assertIsInstance(client.network_error, Throttled)
        self.assertEqual(client.network_error.retry_after, 240)


class GenericBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright

        browser_client = UpperAirBrowser
        def make_client(*arguments, **options):
            options.setdefault("control_waiting_seconds", 0.01)
            return browser_client(*arguments, **options)

        client_patch = patch(__name__ + ".UpperAirBrowser", side_effect=make_client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.manager = sync_playwright().start()
        self.addCleanup(self.manager.stop)
        self.browser = self.manager.chromium.launch(channel="chrome", headless=True)
        self.addCleanup(self.browser.close)
        self.context = self.browser.new_context()
        self.navigations = 0
        self.homepage_navigations = 0

        def serve(route):
            if route.request.url.rstrip("/") == "https://gsl.noaa.gov/mats":
                self.homepage_navigations += 1
                route.fulfill(content_type="text/html", body='''
                    <a href="/mats/surface">Surface (METAR)</a>
                    <a href="/mats/upperair">Upper Air (RAOBs/AMDAR)</a>
                    <a href="/mats/radar">Radar (MRMS)</a>
                    <a href="/mats/new-app">New Application</a>
                ''')
                return
            self.navigations += 1
            route.fulfill(content_type="text/html", body=self.app())

        self.context.route("https://gsl.noaa.gov/**", serve)

    def app(self):
        return """<!doctype html><select id="plotTypes-selector" onchange="Session.set('plotType',this.value)">
            <option value="Dieoff">Dieoff</option><option value="TimeSeries">Time series</option></select>
            <button id="add">Add</button><button id="plotMatched">Matched</button>
            <button id="plotUnmatched">Unmatched</button><button id="backButton">Back</button>
            <div id="spinner" style="display:none"></div>
            <div id="placeholder" style="width:1200px;height:600px"></div><script>
            const radar=location.pathname.endsWith('/radar');
            const upperair=location.pathname.endsWith('/upperair');
            const definitions = {
                'data-source':{options:['MODEL_A','MODEL_B']},
                ...(upperair ? {database:{options:['RAOBs (Traditional)']}} : {truth:{options:['QC-METAR','METAR']}}),
                'region-type':{options:['Predefined region','Station']},
                region:{options:radar ? ['Continental US'] : ['All HRRR domain','Other region']},
                variable:{options:radar ? ['Composite Reflectivity'] : upperair ? ['Temperature','RHobT','Wind'] : ['Temperature at 2m (\\u00b0C)','Dewpoint at 2m (\\u00b0C)',
                    'Wind Speed at 10m (m/s)','U Speed at 10m','V Speed at 10m']},
                statistic:{options:radar ? ['CSI','Bias'] : ['RMSE','Bias (Model - Obs)']},
                ...(radar ? {threshold:{options:['25','35','45']}} : {}),
                'forecast-length':{options:['0','1','12']},
                'dieoff-type':{options:['Dieoff','Valid time']},
                'valid-time':{options:[]},
                label:{type:'textInput',value:'default'},
                'curve-dates':{type:'dateRange',value:'10/01/2026 00:00 - 10/15/2026 00:00'},
                dates:{type:'dateRange',value:'10/01/2026 00:00 - 10/15/2026 00:00'}
            };
            const state = {plotType:'Dieoff', Curves:[], errorMessage:'', plotResultKey:null};
            globalThis.Session = {get:name=>state[name],set:(name,value)=>state[name]=value};
            globalThis.Tracker = {afterFlush:callback=>callback()};
            for (const [name,definition] of Object.entries(definitions)) {
                const input = document.createElement(definition.options ? 'select' : 'input');
                input.id=name;
                for (const value of definition.options || []) {
                    const option = document.createElement('option'); option.value=value;
                    option.textContent=value; input.append(option);
                }
                if (!definition.options) input.value=definition.value;
                document.body.append(input);
            }
            const values = () => Object.fromEntries(Object.keys(definitions).filter(name=>name!=='dates')
                .map(name=>[name,document.getElementById(name).value]));
            globalThis.matsParamUtils = {
                getElementValues:()=>({curveParams:{...values(),'data-source':'',variable:''},
                    plotParams:{dates:document.getElementById('dates').value}}),
                getParameterForName:name=>({name,type:definitions[name]?.type || 'select'}),
                getInputElementForParamName:name=>document.getElementById(name),
                getOptionsForParam:name=>definitions[name]?.options || [],
                getValueForParamName:name=>document.getElementById(name).value,
                setInputValueForParamAndTriggerChange:(name,value)=>{
                    document.getElementById(name).value=value;
                    if (name==='data-source') {
                        const dates=value==='MODEL_A' ? '10/01/2026 00:00 - 10/15/2026 00:00' :
                            '09/01/2026 00:00 - 09/15/2026 00:00';
                        document.getElementById('curve-dates').value=dates;
                        document.getElementById('dates').value=dates;
                    }
                },
                setAllParamsToDefault:()=>{
                    for (const [name,definition] of Object.entries(definitions))
                        document.getElementById(name).value=definition.options?.[0] || definition.value || '';
                }
            };
            globalThis.matsCurveUtils = {
                getGraphResult:()=>({data:graph.data,basis:{plotParams:state.Curves}}),
                clearAllUsed:()=>state.Curves=[]
            };
            document.getElementById('add').onclick=()=>state.Curves.push({...values(),'valid-time':'unused',
                'curve-dates':state.plotType==='TimeSeries' ? 'unused' : values()['curve-dates']});
            const graph=document.getElementById('placeholder');
            document.getElementById('backButton').onclick=()=>{
                state.plotResultKey=null; graph.data=[];
            };
            document.getElementById('plotMatched').onclick=()=>{
                state.plotResultKey='local-test'; graph.data=state.Curves.map(curve=>({label:curve.label,
                    x:state.plotType==='TimeSeries' ?
                        ['2026-10-01T00:00:00Z','2026-10-02T00:00:00Z','2026-10-03T00:00:00Z'] : [0,1,2],y:[1,2,3]}));
                graph.layout={margin:{t:40},annotations:[]}; graph._fullLayout={};
            };
            globalThis.Plotly={restyle:async()=>{},relayout:async()=>{},toImage:async(graph,{width,height})=>{
                const canvas=document.createElement('canvas'); canvas.width=width;canvas.height=height;
                const context=canvas.getContext('2d');context.fillStyle='white';context.fillRect(0,0,width,height);
                return canvas.toDataURL('image/png');
            }};
            </script>"""

    def job(self, **parameters):
        raw = example()
        raw["experiments"] = [{"name": "MODEL_A"}, {"name": "MODEL_B"}]
        raw["plots"] = [{"name": "sfc_dieoff", "application": "surface", "plot_type": "dieoff",
                         "truth": "QC-METAR", "region": "All HRRR domain", "variable": "t2m", **parameters}]
        return build_plan(raw).jobs[0]

    def test_surface_defaults_aliases_and_export(self):
        job = self.job(**{"dieoff type": "Dieoff"})
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        client.prepare(job)
        self.assertEqual(client.page.url, "https://gsl.noaa.gov/mats/surface")
        self.assertEqual(len(client.curves), 2)
        self.assertEqual(client.effective_settings["curves"][0]["valid-time"], "unused")
        for curve in client.curves:
            self.assertEqual(curve["variable"], "Temperature at 2m (\u00b0C)")
            self.assertEqual(curve["statistic"], "RMSE")
            self.assertEqual(curve["forecast-length"], "0")
            self.assertEqual(curve["curve-dates"], job.mats_dates)
            self.assertNotIn("top", curve)
        self.assertIsNone(client.page.evaluate("() => Session.get('plotResultKey')"))
        image, metadata = client.plot(job)
        self.assertTrue(image.startswith(PNG_SIGNATURE))
        self.assertEqual(metadata["effective_settings"], client.effective_settings)
        self.assertEqual(metadata["figure"]["width"], job.width)
        self.assertEqual(metadata["figure"]["height"], job.height + metadata["figure"]["header_height"])
        self.assertGreater(metadata["figure"]["width"], metadata["figure"]["height"])
        self.assertIn("QC-METAR", metadata["figure"]["header"][0])
        self.assertNotIn("1-1000 hPa", metadata["figure"]["header"][0])

    def test_controls_wait_between_actions_and_for_delayed_metadata(self):
        script = """<script>
            globalThis.actions = [];
            globalThis.Meteor = {status:()=>({connected:true}), connection:{_outstandingMethodBlocks:[]}};
            const originalSet = matsParamUtils.setInputValueForParamAndTriggerChange;
            matsParamUtils.setInputValueForParamAndTriggerChange = (name, value) => {
                actions.push({name, at:performance.now()});
                originalSet(name, value);
                if (name === 'data-source') {
                    const input = document.getElementById('variable');
                    input.disabled = true;
                    Meteor.connection._outstandingMethodBlocks = [{methods:['metadata']}];
                    setTimeout(() => {
                        input.disabled = false;
                        Meteor.connection._outstandingMethodBlocks = [];
                    }, 150);
                }
            };
            const originalAdd = document.getElementById('add').onclick;
            document.getElementById('add').onclick = () => {
                actions.push({name:'add', at:performance.now()});
                originalAdd();
            };
        </script>"""
        app = self.app() + script
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False,
                                 control_waiting_seconds=0.1)
        with patch.object(self, "app", return_value=app):
            client.prepare(self.job(statistic="rmse"))
        actions = client.page.evaluate("() => actions")
        self.assertEqual(sum(action["name"] == "add" for action in actions), 2)
        for previous, current in zip(actions, actions[1:]):
            self.assertGreaterEqual(current["at"] - previous["at"], 95)
            if previous["name"] == "data-source":
                self.assertGreaterEqual(current["at"] - previous["at"], 245)
        self.assertIsNone(client.page.evaluate("() => Session.get('plotResultKey')"))

    def test_default_dates_are_shared_across_models_and_recorded_in_headers(self):
        for plot_type in ("dieoff", "time series"):
            with self.subTest(plot_type=plot_type):
                job = replace(self.job(statistic="rmse+bias"), plot_type=plot_type, start=None, end=None)
                client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
                client.prepare(job)
                dates = "10/01/2026 00:00 - 10/15/2026 00:00"
                self.assertEqual(client.effective_settings["date_range"],
                                 {"start": "2026-10-01T00:00:00+00:00", "end": "2026-10-15T00:00:00+00:00"})
                if plot_type == "time series":
                    self.assertEqual(client.effective_settings["plot_parameters"]["dates"], dates)
                else:
                    self.assertEqual({curve["curve-dates"] for curve in client.curves}, {dates})
                image, metadata = client.plot(job)
                self.assertTrue(is_png(image))
                self.assertIn("2026-10-01 00:00 - 2026-10-15 00:00", metadata["figure"]["header"][0])
                self.assertIsNone(job.start)

    def test_default_dates_come_from_recipe_first_model_for_separate_jobs(self):
        job = self.job()
        job = replace(job, start=None, end=None, default_date_experiment="MODEL_A",
                      experiments=(job.experiments[1],))
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        client.prepare(job)
        self.assertEqual(client.curves[0]["data-source"], "MODEL_B")
        self.assertEqual(client.curves[0]["curve-dates"], "10/01/2026 00:00 - 10/15/2026 00:00")

    def test_partial_dates_preserve_the_supplied_endpoint(self):
        for start, end, expected in (("2026-10-04T00:00:00+00:00", None,
                                       "10/04/2026 00:00 - 10/15/2026 00:00"),
                                      (None, "2026-10-12T00:00:00+00:00",
                                       "10/01/2026 00:00 - 10/12/2026 00:00")):
            with self.subTest(start=start, end=end):
                job = replace(self.job(), start=start, end=end)
                client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
                client.prepare(job)
                self.assertEqual({curve["curve-dates"] for curve in client.curves}, {expected})

    def test_invalid_default_or_resolved_dates_stop_before_adding_curves(self):
        job = replace(self.job(), start="2026-10-20T00:00:00+00:00", end=None)
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        with self.assertRaisesRegex(RunError, "Resolved date range is invalid"):
            client.prepare(job)
        self.assertEqual(client.page.evaluate("() => Session.get('Curves')"), [])
        app = self.app().replace("10/01/2026 00:00 - 10/15/2026 00:00", "unreadable dates")
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        with patch.object(self, "app", return_value=app), \
            self.assertRaisesRegex(RunError, "Cannot read the first experiment's default date range"):
            client.prepare(replace(job, start=None))
        self.assertEqual(client.page.evaluate("() => Session.get('Curves')"), [])

    def test_combined_surface_statistics_for_all_baseline_variables(self):
        variables = {"t2m": "Temperature at 2m (\u00b0C)",
                     "td2m": "Dewpoint at 2m (\u00b0C)",
                     "wind10m": "Wind Speed at 10m (m/s)"}
        for alias, label in variables.items():
            with self.subTest(variable=alias):
                job = self.job(variable=alias, statistic="rmse+bias")
                client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
                client.prepare(job)
                self.assertEqual(len(client.curves), 4)
                self.assertEqual({curve["variable"] for curve in client.curves}, {label})
                self.assertEqual([curve["statistic"] for curve in client.curves],
                                 ["RMSE", "Bias (Model - Obs)"] * 2)
                self.assertEqual([curve.dash for curve in job.curves], ["solid", "dash"] * 2)
                image, metadata = client.plot(job)
                self.assertTrue(image.startswith(PNG_SIGNATURE))
                self.assertIn("RMSE (solid) + BIAS (dashed)", metadata["figure"]["header"][0])

    def test_job_weights_reach_plot_export_and_legend_metadata(self):
        job = replace(self.job(statistic="rmse+bias"), line_weight=3, marker_weight=9)
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        client.prepare(job)
        client.page.evaluate("""() => {
            Plotly.restyle = async (graph, changes, indices) => {
                for (const index of indices) graph.data[index].appliedStyle = changes;
            };
        }""")
        image, metadata = client.plot(job)
        styles = client.page.evaluate("() => document.getElementById('placeholder').data.map(trace => trace.appliedStyle)")
        self.assertTrue(all(style["line.width"] == 3 and style["marker.size"] == 9 for style in styles))
        self.assertEqual(metadata["figure"]["line_weight"], 3)
        self.assertEqual(metadata["figure"]["marker_weight"], 9)
        self.assertTrue(image.startswith(PNG_SIGNATURE))

    def test_three_combined_variables_reuse_page_and_reset_curves(self):
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        page = None
        for variable in ("t2m", "td2m", "wind10m"):
            job = self.job(variable=variable, statistic="rmse+bias")
            client.prepare(job)
            if page is None:
                page = client.page
            self.assertIs(client.page, page)
            self.assertEqual(len(client.curves), 4)
            self.assertEqual([curve["statistic"] for curve in client.curves],
                             ["RMSE", "Bias (Model - Obs)"] * 2)
            client.plot(job)
        self.assertEqual(self.navigations, 1)
        self.assertEqual(self.homepage_navigations, 1)

    def test_radar_homepage_routing_thresholds_and_bias_use_radar_options(self):
        raw = example()
        raw["experiments"] = [{"name": "MODEL_A"}, {"name": "MODEL_B"}]
        raw["plots"] = [{"name": "cref_dieoff", "application": "Radar (MRMS)", "plot_type": "die off",
                         "region": "Continental US", "variable": "Composite Reflectivity",
                         "Threshold": [25, 35, 45], "statistic": ["CSI", "Bias"]}]
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        for job in build_plan(raw).jobs:
            client.prepare(job)
            self.assertEqual(client.page.url, "https://gsl.noaa.gov/mats/radar")
            self.assertEqual(client.effective_settings["application"], "radar")
            self.assertEqual(client.curves[0]["statistic"], "CSI" if job.statistic == "csi" else "Bias")
            self.assertEqual(client.curves[0]["threshold"], str(dict(job.parameters)["Threshold"]))
            image, metadata = client.plot(job)
            self.assertTrue(is_png(image))
        self.assertEqual(self.homepage_navigations, 1)
        self.assertEqual(self.navigations, 1)

    def test_unknown_application_fails_before_loading_an_application(self):
        job = replace(self.job(), application="Unknown Application")
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        with self.assertRaisesRegex(RunError, "Unknown Application"):
            client.prepare(job)
        self.assertEqual(self.homepage_navigations, 1)
        self.assertEqual(self.navigations, 0)

    def test_full_upperair_title_resolves_variable_and_statistic_aliases(self):
        job = self.job(application="Upper Air (RAOBs/AMDAR)", truth="RAOBs (Traditional)",
                       variable="T", statistic="bias")
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        client.prepare(job)
        self.assertEqual(client.application, "upperair")
        self.assertEqual(client.curves[0]["variable"], "Temperature")
        self.assertEqual(client.curves[0]["statistic"], "Bias (Model - Obs)")

    def test_incompatible_workflow_fails_before_plot_submission(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout

        job = replace(self.job(), application="New Application")
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        with patch("playwright.sync_api.Page.wait_for_function", side_effect=BrowserTimeout("No controls")):
            with self.assertRaisesRegex(RunError, "different workflow.*No plot query"):
                client.prepare(job)
        self.assertEqual(self.homepage_navigations, 1)
        self.assertEqual(self.navigations, 1)

    def test_metadata_403_stops_preparation_early_with_ray_id(self):
        requests = []
        def deny(route):
            requests.append(route.request.url)
            route.fulfill(status=403, headers={"server": "cloudflare", "cf-ray": "local-test-ray"},
                          content_type="text/plain", body="Forbidden")
        self.context.route("https://gsl.noaa.gov/mats/surface/sockjs/info*", deny)
        app = """<!doctype html><div>Loading app metadata...</div><script>
            fetch('/mats/surface/sockjs/info?token=private-token');
        </script>"""
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        started = time.monotonic()
        with patch.object(self, "app", return_value=app), \
            self.assertRaisesRegex(RunError, "HTTP 403.*sockjs/info.*cf-ray=local-test-ray") as result:
            client.prepare(self.job())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("private-token", str(result.exception))
        self.assertNotIn("within 90 seconds", str(result.exception))
        self.assertEqual(client.page.locator("#add").count(), 0)

    def test_dieoff_to_timeseries_uses_plot_dates_and_requested_leads(self):
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        dieoff = self.job(statistic="rmse+bias")
        client.prepare(dieoff)
        client.plot(dieoff)
        for lead in (0, 1, 12):
            job = self.job(plot_type="time series", statistic="rmse+bias", **{"forecast lead time": lead})
            client.prepare(job)
            self.assertEqual(client.effective_settings["plot_type"], "TimeSeries")
            self.assertEqual(client.effective_settings["plot_parameters"]["dates"], job.mats_dates)
            self.assertEqual({curve["forecast-length"] for curve in client.curves}, {str(lead)})
            self.assertEqual({curve["curve-dates"] for curve in client.curves}, {"unused"})
            image, metadata = client.plot(job)
            self.assertTrue(image.startswith(PNG_SIGNATURE))
            self.assertEqual([curve["points"] for curve in metadata["curves"]], [3] * 4)
        self.assertEqual(self.navigations, 1)

    def test_unknown_or_ambiguous_settings_fail_without_plotting(self):
        for parameters in ({"unknown key": "anything"}, {"dieoff type": "xxxxx"},
                           {"variable": "speed"}, {"type": "Dieoff"}, {"statistic": "bogus"}):
            with self.subTest(parameters=parameters):
                client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
                with self.assertRaises(RunError) as error:
                    client.prepare(self.job(**parameters))
                requested_key, requested_value = next(iter(parameters.items()))
                self.assertIn(requested_key if requested_key in {"unknown key", "type"} else str(requested_value),
                              str(error.exception))
                self.assertIsNone(client.page.evaluate("() => Session.get('plotResultKey')"))

    def test_all_missing_experiments_are_reported_before_adding_curves(self):
        job = self.job()
        job = replace(job, experiments=tuple(replace(experiment, name=f"MISSING_{index}")
                                            for index, experiment in enumerate(job.experiments)))
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        with self.assertRaises(RunError) as error:
            client.prepare(job)
        self.assertIn("MISSING_0", str(error.exception))
        self.assertIn("MISSING_1", str(error.exception))
        self.assertIn("MODEL_A", str(error.exception))
        self.assertEqual(client.page.evaluate("() => Session.get('Curves')"), [])
        self.assertIsNone(client.page.evaluate("() => Session.get('plotResultKey')"))

    def test_all_omitted_parameters_use_mats_defaults(self):
        raw = example()
        raw["experiments"] = [{"name": "MODEL_A"}]
        raw["plots"] = [{"name": "minimal", "application": "surface", "plot_type": "Time series"}]
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        client.prepare(build_plan(raw).jobs[0])
        self.assertEqual(client.effective_settings["plot_type"], "TimeSeries")
        self.assertEqual(client.curves[0]["truth"], "QC-METAR")
        self.assertEqual(client.curves[0]["statistic"], "RMSE")

    def test_application_switches_reuse_one_page_and_minimize_only_once(self):
        pages = []
        self.context.on("page", lambda page: pages.append(page))
        jobs = (
            self.job(statistic="rmse"),
            self.job(application="Upper Air", truth="RAOBs (Traditional)", variable="T", statistic="rmse"),
            replace(self.job(application="Radar (MRMS)", region="Continental US",
                             variable="Composite Reflectivity", statistic="CSI"), truth=None),
            self.job(statistic="rmse"),
        )
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=True)
        with patch("automats.browser.minimize_window") as minimize:
            for job in jobs:
                client.prepare(job)
                self.assertEqual(len(pages), 1)
                self.assertIs(client.page, pages[0])
                self.assertIs(client.page.context, self.context)
                self.assertEqual(len(self.context.pages), 1)
                minimize.assert_called_once_with(pages[0])
                client.plot(job)
        self.assertEqual(self.homepage_navigations, 1)
        self.assertEqual(self.navigations, 4)

    def test_combined_plot_gracefully_plots_when_one_experiment_is_unavailable(self):
        job = self.job()
        job = replace(job, experiments=(job.experiments[0], replace(job.experiments[1], name="UNAVAILABLE_MODEL")))
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        client.prepare(job)
        self.assertEqual(len(client.curves), 1)
        self.assertEqual(client.curves[0]["data-source"], "MODEL_A")
        image, metadata = client.plot(job)
        self.assertTrue(image.startswith(PNG_SIGNATURE))
        self.assertEqual(metadata["figure"]["legend_names"], ["MODEL_A"])
        self.assertEqual(len(metadata["curves"]), 1)

    def test_combined_plot_gracefully_skips_unsupported_curve_selection(self):
        job = self.job()
        client = UpperAirBrowser(self.context, "https://gsl.noaa.gov/mats", minimize=False)
        original_set = client._set
        def selective_set(name, value, choice=True):
            if name == "variable" and client.expected.get("data-source") == "MODEL_B":
                raise RunError("Variable not supported for MODEL_B")
            return original_set(name, value, choice)
        with patch.object(client, "_set", side_effect=selective_set):
            client.prepare(job)
        self.assertEqual(len(client.curves), 1)
        self.assertEqual(client.curves[0]["data-source"], "MODEL_A")
        image, metadata = client.plot(job)
        self.assertTrue(image.startswith(PNG_SIGNATURE))
        self.assertEqual(metadata["figure"]["legend_names"], ["MODEL_A"])


class ChromeWindowTests(unittest.TestCase):
    def test_minimize_targets_only_the_pages_own_window(self):
        page = Mock()
        session = page.context.new_cdp_session.return_value
        session.send.side_effect = [{"windowId": 17}, {}]
        minimize_window(page)
        page.context.new_cdp_session.assert_called_once_with(page)
        self.assertEqual(session.send.call_args_list[1].args, (
            "Browser.setWindowBounds", {"windowId": 17, "bounds": {"windowState": "minimized"}}))
        session.detach.assert_called_once()

    def test_normal_chrome_remains_minimized_and_can_render_without_network(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as manager:
            try:
                browser = manager.chromium.launch(channel="chrome", headless=True)
            except Exception as error:
                self.skipTest(f"Chrome unavailable: {error}")
            try:
                page = browser.new_context().new_page()
                minimize_window(page)
                page.set_content('<canvas id="plot" width="20" height="20"></canvas>'
                                 '<button onclick="globalThis.clicked=true">Local control</button>')
                pixel = page.evaluate("""() => {
                    const context = document.getElementById('plot').getContext('2d');
                    context.fillStyle = 'red'; context.fillRect(0, 0, 20, 20);
                    return Array.from(context.getImageData(5, 5, 1, 1).data);
                }""")
                self.assertEqual(pixel, [255, 0, 0, 255])
                page.evaluate("() => { globalThis.ready=false; setTimeout(() => { globalThis.ready=true; }, 50); }")
                page.wait_for_function("() => globalThis.ready", polling=250, timeout=3000)
                page.get_by_role("button", name="Local control").click(timeout=5000)
                self.assertTrue(page.evaluate("() => globalThis.clicked"))
                session = page.context.new_cdp_session(page)
                try:
                    window = session.send("Browser.getWindowForTarget")
                    self.assertEqual(window["bounds"]["windowState"], "minimized")
                    page.goto('data:text/html,<title>Local navigation test</title>')
                    self.assertEqual(page.evaluate("() => typeof globalThis.clicked"), "undefined")
                    page.evaluate("() => { globalThis.previousPlot='old'; }")
                    page.goto('data:text/html,<title>Local navigation test</title>')
                    self.assertEqual(page.evaluate("() => typeof globalThis.previousPlot"), "undefined")
                    after = session.send("Browser.getWindowForTarget")
                    self.assertEqual(after["windowId"], window["windowId"])
                    self.assertEqual(after["bounds"]["windowState"], "minimized")
                    page.context.route("https://automats.test/**", lambda route: route.fulfill(
                        content_type="text/html", body="<title>Local application switch</title>"))
                    for application in ("surface", "upperair", "radar"):
                        page.goto(f"https://automats.test/{application}")
                        switched = session.send("Browser.getWindowForTarget")
                        self.assertEqual(switched["windowId"], window["windowId"])
                        self.assertEqual(switched["bounds"]["windowState"], "minimized")
                        self.assertEqual(len(page.context.pages), 1)
                finally:
                    session.detach()
            finally:
                browser.close()

    def test_chrome_uses_fixed_user_agent_and_disables_automation_detection(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as manager:
            try:
                browser = manager.chromium.launch(
                    channel="chrome",
                    headless=True,
                    args=CHROMIUM_ARGS,
                    ignore_default_args=CHROMIUM_IGNORE_DEFAULT_ARGS,
                )
            except Exception as error:
                self.skipTest(f"Chrome unavailable: {error}")
            try:
                context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
                page = context.new_page()
                self.assertFalse(page.evaluate("navigator.webdriver"))
                self.assertEqual(page.evaluate("navigator.userAgent"), DEFAULT_USER_AGENT)
            finally:
                browser.close()

    def test_run_plan_configures_fixed_user_agent_and_disabled_automation_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = build_plan(example())
            with patch("automats.runner.Path.home", return_value=root), \
                    patch("playwright.sync_api.sync_playwright") as manager, \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.prepare.side_effect = KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    run_plan(plan, root / "figures", log=lambda message: None)
                launch_call = manager.return_value.__enter__.return_value.chromium.launch.call_args
                self.assertTrue(launch_call.kwargs.get("headless"))
                self.assertEqual(launch_call.kwargs.get("args"), CHROMIUM_ARGS)
                self.assertEqual(launch_call.kwargs.get("ignore_default_args"), CHROMIUM_IGNORE_DEFAULT_ARGS)
                context_call = manager.return_value.__enter__.return_value.chromium.launch.return_value.new_context.call_args
                self.assertEqual(context_call.kwargs.get("user_agent"), DEFAULT_USER_AGENT)

            with patch("automats.runner.Path.home", return_value=root), \
                    patch("playwright.sync_api.sync_playwright") as manager, \
                    patch("automats.browser.UpperAirBrowser") as client:
                client.return_value.prepare.side_effect = KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    run_plan(plan, root / "figures", show_browser=True, log=lambda message: None)
                launch_call = manager.return_value.__enter__.return_value.chromium.launch.call_args
                self.assertFalse(launch_call.kwargs.get("headless"))


class SlideTests(unittest.TestCase):
    def setUp(self):
        raw = example()
        raw["plots"][0].update({"name": "ua_dieoff", "plot_type": "dieoff", "statistic": ["rmse+bias"]})
        raw["plots"][0].pop("forecast lead time")
        raw["slides"] = [{"plot group": "ua_dieoff", "title": "Upper Air Verification",
                          "number of rows": 2, "row1": ["T_rmse+bias", "RHobT_rmse+bias"],
                          "row2": ["Wind_rmse+bias"]}]
        self.plan = build_plan(raw)

    def save_figures(self, output):
        from PIL import Image

        for job, color in zip(self.plan.jobs, ("red", "blue", "green")):
            path = output / job.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1600, 960), color).save(path, format="PNG")

    def test_deck_preserves_pngs_aspect_ratio_row_order_and_centering(self):
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from pptx.util import Inches

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.save_figures(output)
            path = build_slides(self.plan, output)
            self.assertEqual(path, output / "slides.pptx")
            deck = Presentation(path)
            self.assertAlmostEqual(deck.slide_width / deck.slide_height, 16 / 9, places=6)
            self.assertEqual(len(deck.slides), 2)
            slide = deck.slides[1]
            self.assertEqual(slide.shapes[0].text, "Upper Air Verification")
            pictures = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
            self.assertEqual([shape.name for shape in pictures],
                             [path for row in self.plan.slides[0].rows for path in row])
            first, second, third = pictures
            self.assertEqual(first.top, second.top)
            self.assertGreaterEqual(second.left, first.left + first.width)
            self.assertGreaterEqual(third.top, first.top + first.height)
            self.assertAlmostEqual(third.left + third.width / 2, deck.slide_width / 2, delta=2)
            self.assertLessEqual(first.width, third.width)
            for picture in pictures:
                self.assertAlmostEqual(picture.width / picture.height, 1600 / 960, places=5)
                self.assertGreaterEqual(picture.left, Inches(0.25))
                self.assertGreaterEqual(picture.top, Inches(0.9))
                self.assertLessEqual(picture.left + picture.width, deck.slide_width - Inches(0.25))
                self.assertLessEqual(picture.top + picture.height, deck.slide_height - Inches(0.25))
                self.assertEqual(picture.image.blob, (output / picture.name).read_bytes())
            captions = [shape for shape in slide.shapes if shape.name.startswith("Caption ")]
            self.assertEqual([shape.text for shape in captions], ["T", "RHobT", "Wind"])
            for caption, picture in zip(captions, pictures):
                self.assertLess(caption.left + caption.width, picture.left)
                self.assertGreaterEqual(caption.left, Inches(0.25))
                self.assertAlmostEqual(caption.top + caption.height / 2,
                                       picture.top + picture.height / 2, delta=2)
            self.assertLessEqual(captions[1].left, second.left)
            self.assertGreaterEqual(captions[1].left, first.left + first.width)

    def test_multiple_slides_and_different_image_shapes(self):
        from PIL import Image
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        second = replace(self.plan.slides[0], title="Profiles", rows=(self.plan.slides[0].rows[0],), captions=())
        plan = replace(self.plan, slides=(*self.plan.slides, second))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.save_figures(output)
            Image.new("RGB", (1200, 1260), "red").save(output / plan.jobs[0].relative_path)
            deck = Presentation(build_slides(plan, output))
            self.assertEqual(len(deck.slides), 3)
            self.assertEqual(deck.slides[2].shapes[0].text, "Profiles")
            profile, landscape = [shape for shape in deck.slides[2].shapes
                                  if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
            self.assertAlmostEqual(profile.width / profile.height, 1200 / 1260, places=5)
            self.assertAlmostEqual(landscape.width / landscape.height, 1600 / 960, places=5)
            self.assertGreaterEqual(landscape.left, profile.left + profile.width)
            self.assertEqual([shape.text for shape in deck.slides[2].shapes
                              if shape.name.startswith("Caption ")], ["T", "RHobT"])

    def test_missing_corrupt_or_non_png_figures_preserve_previous_deck(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.save_figures(output)
            deck = build_slides(self.plan, output)
            previous = deck.read_bytes()
            figure = output / self.plan.jobs[0].relative_path
            figure.unlink()
            with self.assertRaisesRegex(SlideError, "Slide 1.*row 1.*ua_dieoff_T"):
                build_slides(self.plan, output)
            figure.write_bytes(b"not a PNG")
            with self.assertRaises(SlideError):
                build_slides(self.plan, output)
            Image.new("RGB", (10, 10)).save(figure, format="JPEG")
            with self.assertRaisesRegex(SlideError, "expected a PNG"):
                build_slides(self.plan, output)
            self.assertEqual(deck.read_bytes(), previous)
            self.assertEqual(list(output.glob(".slides-*")), [])

    def test_deck_is_replaced_only_after_a_successful_save(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.save_figures(output)
            deck = build_slides(self.plan, output)
            previous = deck.read_bytes()
            with patch("pptx.presentation.Presentation.save", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    build_slides(self.plan, output)
            self.assertEqual(deck.read_bytes(), previous)
            self.assertEqual(list(output.glob(".slides-*")), [])
            updated = replace(self.plan, slides=(replace(self.plan.slides[0], title="Updated"),))
            build_slides(updated, output)
            self.assertNotEqual(deck.read_bytes(), previous)

    def test_slides_only_never_runs_plotting_and_reports_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            self.save_figures(output)
            messages = StringIO()
            with patch("automats.cli.load_plan", return_value=self.plan), \
                patch("automats.cli.run_plan") as execute, redirect_stdout(messages):
                self.assertEqual(main(["recipe.yaml", "--slides-only", "--output-dir", str(output)]), 0)
            execute.assert_not_called()
            self.assertIn("Saved 1 slides to", messages.getvalue())
            self.assertTrue((output / "slides.pptx").is_file())

    def test_cli_builds_automatically_but_not_after_failed_or_partial_runs(self):
        for options, failed, expected in (([], False, True), (["--limit", "1"], False, False),
                                          ([], True, False), (["--metadata-only"], False, True)):
            with self.subTest(options=options, failed=failed), \
                patch("automats.cli.load_plan", return_value=self.plan), \
                patch("automats.cli.run_plan", side_effect=RunError("failed") if failed else None,
                      return_value=3), \
                patch("automats.cli.build_slides", return_value=Path("figures/slides.pptx")) as build, \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(["recipe.yaml", *options]), 1 if failed else 0)
                self.assertEqual(build.called, expected)

    def test_plan_reports_deck_without_generating_anything(self):
        output = StringIO()
        with patch("automats.cli.load_plan", return_value=self.plan), \
            patch("automats.cli.run_plan") as execute, patch("automats.cli.build_slides") as build, \
            redirect_stdout(output):
            self.assertEqual(main(["recipe.yaml", "--plan", "--output-dir", "custom figures"]), 0)
        execute.assert_not_called()
        build.assert_not_called()
        self.assertIn("1 slides: custom figures/slides.pptx", output.getvalue())

    def test_slides_only_requires_configuration_and_rejects_limit(self):
        for flag in ("--slides-only", "--slide-only"):
            with self.subTest(flag=flag):
                with patch("automats.cli.load_plan", return_value=replace(self.plan, slides=())), \
                    patch("automats.cli.run_plan") as execute, redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(main(["recipe.yaml", flag]), 1)
                execute.assert_not_called()
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as result:
                    main(["recipe.yaml", flag, "--limit", "1"])
                self.assertEqual(result.exception.code, 2)

    def test_invalid_slide_references_only_stop_slide_generation(self):
        plan = replace(self.plan, slides=(), slide_error="slide 1: ambiguous figure 'T'")
        messages = StringIO()
        with patch("automats.cli.load_plan", return_value=plan), \
            patch("automats.cli.run_plan", return_value=3) as execute, \
            redirect_stdout(messages), redirect_stderr(messages):
            self.assertEqual(main(["recipe.yaml"]), 0)
            execute.assert_called_once()
            execute.reset_mock()
            self.assertEqual(main(["recipe.yaml", "--slides-only"]), 1)
            execute.assert_not_called()
            self.assertEqual(main(["recipe.yaml", "--plan"]), 0)
            execute.assert_not_called()
        self.assertIn("Slide generation failed: slide 1: ambiguous figure", messages.getvalue())
        self.assertIn("Saved 3 figures", messages.getvalue())
        self.assertIn("Slide generation disabled", messages.getvalue())

    def test_slide_save_errors_do_not_fail_a_successful_plot_run(self):
        for error in (SlideError("missing PNG"), OSError("disk full")):
            messages = StringIO()
            with self.subTest(error=error), patch("automats.cli.load_plan", return_value=self.plan), \
                patch("automats.cli.run_plan", return_value=3), \
                patch("automats.cli.build_slides", side_effect=error), \
                redirect_stdout(messages), redirect_stderr(messages):
                self.assertEqual(main(["recipe.yaml"]), 0)
            self.assertIn("Slide generation failed", messages.getvalue())
            self.assertIn("Saved 3 figures", messages.getvalue())


class CliTests(unittest.TestCase):
    recipe = Path(__file__).resolve().parent / "upperair.yaml"

    def test_ctrl_c_returns_130_and_stops_progress_display(self):
        output = StringIO()
        with patch("automats.cli.run_plan", side_effect=KeyboardInterrupt), \
            redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(main([str(self.recipe)]), 130)
        self.assertIn("Stopped", output.getvalue())
        self.assertIn("Interrupted. Completed figures are retained", output.getvalue())

    def test_help_names_the_yaml_argument_recipe(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("recipe", output.getvalue())
        self.assertIn("YAML plotting recipe", output.getvalue())
        self.assertIn("--output-dir OUTPUT_DIR", output.getvalue())
        self.assertIn("default: ./figures", output.getvalue())
        self.assertNotIn(" config", output.getvalue())

    def test_browser_runs_in_background_by_default_and_can_be_shown(self):
        for options, shown in (([], False), (["--show-browser"], True)):
            with self.subTest(options=options), patch("automats.cli.run_plan", return_value=0) as execute:
                with redirect_stdout(StringIO()):
                    self.assertEqual(main([str(self.recipe), *options]), 0)
                self.assertEqual(execute.call_args.args[3], not shown)
                self.assertEqual(execute.call_args.kwargs["show_browser"], shown)

    def test_plan_lists_42_paths_without_browser_execution(self):
        output = StringIO()
        with patch("automats.cli.run_plan") as execute, redirect_stdout(output):
            self.assertEqual(main([str(self.recipe), "--plan"]), 0)
        execute.assert_not_called()
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 43)
        self.assertIn("figures/ua_profile/ua_profile_T_rmse_f000.png", lines)
        self.assertTrue(all(line.startswith("figures/") for line in lines[1:]))

    def test_output_directory_defaults_to_figures_and_accepts_override(self):
        for options, directory in (([], "figures"), (["--output-dir", "custom figures"], "custom figures")):
            with self.subTest(options=options), patch("automats.cli.run_plan", return_value=0) as execute:
                with redirect_stdout(StringIO()):
                    self.assertEqual(main([str(self.recipe), *options]), 0)
                self.assertEqual(execute.call_args.args[1], Path(directory))

    def test_plan_uses_explicit_output_directory(self):
        output = StringIO()
        with patch("automats.cli.run_plan") as execute, redirect_stdout(output):
            self.assertEqual(main([str(self.recipe), "--output-dir", "custom figures", "--plan"]), 0)
        execute.assert_not_called()
        self.assertIn("custom figures/ua_profile/ua_profile_T_rmse_f000.png", output.getvalue().splitlines())

    def test_update_flag_forwarded_to_run_plan(self):
        with patch("automats.cli.run_plan", return_value=0) as execute:
            with redirect_stdout(StringIO()):
                self.assertEqual(main([str(self.recipe)]), 0)
            self.assertFalse(execute.call_args.kwargs["update"])
        with patch("automats.cli.run_plan", return_value=0) as execute:
            with redirect_stdout(StringIO()):
                self.assertEqual(main([str(self.recipe), "--update"]), 0)
            self.assertTrue(execute.call_args.kwargs["update"])

    def test_update_rejects_slides_only_and_metadata_only(self):
        for flag in ("--slides-only", "--slide-only", "--metadata-only"):
            with self.subTest(flag=flag), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as result:
                main([str(self.recipe), "--update", flag])
            self.assertEqual(result.exception.code, 2)

    def test_plan_omits_existing_figures_unless_update(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            fake_fig = output_dir / "ua_profile" / "ua_profile_T_rmse_f000.png"
            fake_fig.parent.mkdir(parents=True, exist_ok=True)
            fake_fig.write_bytes(png_image())
            output = StringIO()
            with patch("automats.cli.run_plan") as execute, redirect_stdout(output):
                self.assertEqual(main([str(self.recipe), "--output-dir", str(output_dir), "--plan"]), 0)
            execute.assert_not_called()
            lines = output.getvalue().splitlines()
            self.assertNotIn(str(fake_fig), lines)
            self.assertEqual(len(lines), 42)

            output_update = StringIO()
            with patch("automats.cli.run_plan") as execute, redirect_stdout(output_update):
                self.assertEqual(main([str(self.recipe), "--output-dir", str(output_dir), "--plan", "--update"]), 0)
            execute.assert_not_called()
            lines_update = output_update.getvalue().splitlines()
            self.assertIn(str(fake_fig), lines_update)
            self.assertEqual(len(lines_update), 43)


if __name__ == "__main__":
    unittest.main()