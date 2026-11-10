"""Command-line planning and sequential plot generation."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import shutil
import sys
from threading import Event, Lock, Thread
import time

from automats.config import load_plan
from automats.runner import figure_exists, run_plan
from automats.slides import build_slides


class ProgressDisplay:
    def __init__(self, total: int, stream=None, clock=time.monotonic):
        self.stream = sys.stdout if stream is None else stream
        self.clock = clock
        self.total = total
        self.completed = 0
        self.message = "Starting AutoMATS"
        self.wait_until = None
        self.started = clock()
        self.frame = 0
        self.width = 0
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.lock = Lock()
        self.stop = Event()
        self.thread = None

    def _clear(self):
        if self.interactive and self.width:
            self.stream.write("\r" + " " * self.width + "\r")
            self.width = 0

    def _draw(self):
        elapsed = max(0, int(self.clock() - self.started))
        message = self.message
        if self.wait_until is not None:
            remaining = math.ceil(max(0, self.wait_until - self.clock()))
            message = f"Waiting {remaining}s: {message}"
        if self.interactive:
            filled = 16 * self.completed // max(1, self.total)
            bar = "=" * filled + "." * (16 - filled)
            spinner = "|/-\\"[self.frame % 4]
            text = f"{spinner} [{bar}] {self.completed}/{self.total} {elapsed}s elapsed | {message}"
            columns = max(20, shutil.get_terminal_size(fallback=(100, 24)).columns - 1)
            text = text[:columns]
            self._clear()
            self.stream.write("\r" + text)
            self.width = len(text)
            self.frame += 1
        else:
            self.stream.write(f"Working [{self.completed}/{self.total}, {elapsed}s elapsed]: {message}\n")
        self.stream.flush()

    def update(self, message: str, completed: int | None = None, total: int | None = None,
               wait_seconds: float | None = None):
        with self.lock:
            self.message = str(message).replace("\n", " ")
            self.wait_until = None if wait_seconds is None else self.clock() + max(0, wait_seconds)
            if total is not None:
                self.total = total
            if completed is not None:
                self.completed = min(max(0, completed), self.total)
            self._draw()

    def refresh(self):
        with self.lock:
            self._draw()

    def log(self, message: str):
        with self.lock:
            self._clear()
            self.stream.write(str(message) + "\n")
            self.stream.flush()

    def _animate(self):
        interval = 0.2 if self.interactive else 10
        while not self.stop.wait(interval):
            self.refresh()

    def __enter__(self):
        self.refresh()
        self.thread = Thread(target=self._animate, name="automats-progress", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, error_type, error, traceback):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        self.update("Stopped" if error_type else "Finished")
        if self.interactive:
            self.stream.write("\n")
            self.stream.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="YAML-driven MATS verification plots")
    parser.add_argument("recipe", type=Path, help="YAML plotting recipe")
    parser.add_argument("--output-dir", type=Path, default=Path("figures"), help="Output directory (default: ./figures)")
    parser.add_argument("--plan", action="store_true", help="List filenames without opening a browser or contacting MATS")
    local_mode = parser.add_mutually_exclusive_group()
    local_mode.add_argument("--metadata-only", action="store_true", help="Add metadata to older cached figures locally; never contact MATS")
    local_mode.add_argument("--slides-only", "--slide-only", dest="slides_only", action="store_true",
                            help="Build slides from existing PNG figures; never contact MATS")
    parser.add_argument("--update", action="store_true",
                        help="Regenerate all figures even if they already exist in the output directory")
    parser.add_argument("--limit", type=int, help="Generate at most this many figures")
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument("--headless", action="store_true", help="Run Chrome in headless background mode (default)")
    browser_mode.add_argument("--show-browser", action="store_true", help="Keep Chrome visible instead of running in the background")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.slides_only and args.limit is not None:
        parser.error("--limit cannot be used with --slides-only")
    if args.slides_only and args.update:
        parser.error("--update cannot be used with --slides-only")
    if args.metadata_only and args.update:
        parser.error("--update cannot be used with --metadata-only")
    try:
        plan = load_plan(args.recipe)
        for warning in plan.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        print(f"{len(plan.jobs)} figures; endpoint: {plan.settings.base_url}", flush=True)
        if args.plan:
            for job in plan.jobs:
                if not args.update and figure_exists(args.output_dir, job.relative_path):
                    continue
                print(args.output_dir / job.relative_path)
            if plan.slides:
                print(f"{len(plan.slides)} slides: {args.output_dir / 'slides.pptx'}")
            if plan.pygraf_graphics and plan.pygraf_graphics.enable:
                print(f"pygraf graphics slides enabled ({plan.pygraf_graphics.recipe}): {args.output_dir / 'slides.pptx'}")
            if plan.slide_error is not None:
                print(f"Warning: Slide generation disabled: {plan.slide_error}", file=sys.stderr)
            return 0
        if args.slides_only:
            missing_figures: list[tuple[str, str]] = []
            graphics_stats: dict[str, int] = {"saved": 0}
            deck = build_slides(
                plan,
                args.output_dir,
                missing=missing_figures,
                graphics_stats=graphics_stats,
            )
            if plan.pygraf_graphics and plan.pygraf_graphics.enable:
                print(
                    f"Saved {graphics_stats['saved']} graphics under {(args.output_dir / 'graphics').resolve()}",
                    flush=True,
                )
            print(f"Saved slides to {deck.resolve()}", flush=True)
            if missing_figures:
                unique_missing = len({path for path, _ in missing_figures})
                print(
                    f"Warning: Used placeholders for {unique_missing} missing/unreadable MATS figure(s).",
                    file=sys.stderr,
                )
            return 0
        deck = None
        failed_plots: list[tuple[object, str]] = []
        missing_figures = []
        graphics_stats = {"saved": 0}
        with ProgressDisplay(len(plan.jobs)) as progress:
            count = run_plan(plan, args.output_dir, args.limit, not args.show_browser,
                     metadata_only=args.metadata_only, show_browser=args.show_browser,
                     update=args.update,
                     log=progress.log, status=progress.update, failures=failed_plots)
            if plan.slides or (plan.pygraf_graphics and plan.pygraf_graphics.enable) or plan.slide_error is not None:
                if not args.metadata_only and args.limit is not None and args.limit < len(plan.jobs):
                    progress.log("Slides skipped for a partial --limit run; use --slides-only to build from existing PNGs")
                else:
                    progress.update("Building PowerPoint slides")
                    try:
                        deck = build_slides(
                            plan,
                            args.output_dir,
                            log=progress.log,
                            missing=missing_figures,
                            graphics_stats=graphics_stats,
                        )
                    except Exception as error:
                        progress.log(f"Warning: Slide generation failed: {error}. Saved figures are retained.")
        print(f"Saved {count} MATS figures under {args.output_dir.resolve()}", flush=True)
        if deck is not None and plan.pygraf_graphics and plan.pygraf_graphics.enable:
            print(
                f"Saved {graphics_stats['saved']} graphics under {(args.output_dir / 'graphics').resolve()}",
                flush=True,
            )
        if failed_plots:
            print(f"Warning: Skipped {len(failed_plots)} failed MATS plot(s):", file=sys.stderr)
            for failed_job, reason in failed_plots:
                print(f"  - {failed_job.relative_path}: {reason}", file=sys.stderr)
        if deck is not None:
            print(f"Saved slides to {deck.resolve()}", flush=True)
        if missing_figures:
            unique_missing = len({path for path, _ in missing_figures})
            print(
                f"Warning: Used placeholders for {unique_missing} missing/unreadable MATS figure(s).",
                file=sys.stderr,
            )
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed figures are retained for resume.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"AutoMATS: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())