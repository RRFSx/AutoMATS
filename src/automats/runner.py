"""Sequential MATS execution and local result tracking."""

from dataclasses import asdict
from difflib import get_close_matches
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import time

from automats.config import Plan, PlotJob, Settings, STATISTICS


FIGURE_STYLE_VERSION = 8
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]
CHROMIUM_IGNORE_DEFAULT_ARGS = ["--enable-automation"]


def is_png(image: bytes) -> bool:
    return image.startswith(PNG_SIGNATURE) and image.endswith(PNG_END)


class RunError(RuntimeError):
    pass


def lock_error(lock: Path) -> RunError:
    header = f"AutoMATS cannot start because the run lock exists: {lock}"
    caution = "Do not remove a lock while a run is active. Do not delete pacing.json or reset cooldown state."
    posix = os.name == "posix"
    quoted = shlex.quote(str(lock)) if posix else f'"{lock}"'
    inspect = f"cat {quoted}" if posix else f"type {quoted}"
    remove = f"rm -- {quoted}" if posix else f"del {quoted}"
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
        if not 0 < pid <= 2147483647:
            raise ValueError("invalid PID")
    except (OSError, ValueError):
        return RunError(
            f"{header}\nThe recorded PID is unreadable; a run may still be starting.\n"
            f"Inspect the lock: {inspect}\nCheck for running AutoMATS processes before doing anything else.\n"
            f"Only after confirming no run or previous server query remains, remove the stale lock: {remove}\n"
            f"Then rerun your original command. {caution}"
        )
    check = f"ps -p {pid} -o pid=,comm=,etime=" if posix else f'tasklist /FI "PID eq {pid}"'
    alive = None
    if posix:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except (PermissionError, OSError):
            alive = True
    if alive is not False:
        return RunError(
            f"{header}\nRecorded PID: {pid}; the process is active or its status cannot be safely confirmed.\n"
            f"Inspect it: {check}\nWait for the original run to finish, or stop it with Ctrl+C in its terminal.\n"
            f"If the PID no longer exists, confirm any previous server query has finished before removing the stale lock.\n{caution}"
        )
    return RunError(
        f"{header}\nRecorded PID {pid} no longer exists; the lock appears stale.\n"
        f"Verify: {check}\nConfirm no other AutoMATS run is active and any previous server query has finished "
        f"(allow at least five minutes after an uncertain submission).\n"
        f"Only then remove this lock: {remove}\nThen rerun your original command; saved figures are retained.\n{caution}"
    )


class ResolutionError(RunError):
    def __init__(self, parameter: str, requested: str, options: list[str], matches: list[str]):
        self.missing = not matches
        if parameter in {"model", "data-source"}:
            subject = f"Experiment/model {requested!r}"
        elif parameter == "parameter":
            subject = f"MATS setting name {requested!r}"
        elif parameter == "plot_type":
            subject = f"Plot type {requested!r}"
        elif parameter == "application":
            subject = f"MATS application {requested!r}"
        else:
            subject = f"Setting {parameter!r}, value {requested!r}"
        if matches:
            detail = f"is ambiguous; matching choices: {matches[:8]}"
        else:
            detail = "does not exist in the available choices or configured aliases"
            if parameter in {"model", "data-source"}:
                detail = "is unavailable; an exact model identifier is required"
            if len(options) <= 8:
                detail += f"; available choices: {options}"
            else:
                suggestions = get_close_matches(requested, options, n=8, cutoff=0)
                detail += f"; closest available choices (suggestions only, {len(options)} total): {suggestions}"
        super().__init__(f"{subject} {detail}. No automatic substitution was made.")


def resolve_option(requested: str, options: list[str], parameter: str,
                   aliases: dict[str, str] | None = None, *, partial: bool = True) -> str:
    matches = [option for option in options if option.casefold() == requested.casefold()]
    strict = not partial or parameter in {"model", "data-source"}
    if not matches and not strict:
        normalize = lambda value: re.sub(r"[\s_-]+", "", value.casefold())
        target = (aliases or {}).get(requested.casefold(), requested)
        matches = [option for option in options if normalize(option) == normalize(target)]
        if not matches:
            matches = [option for option in options if normalize(target) in normalize(option)]
    if len(matches) == 1:
        return matches[0]
    raise ResolutionError(parameter, requested, options, matches)


def validate_effective_curves(curves: list[dict], effective: list[dict]) -> None:
    if len(curves) != len(effective):
        raise RunError("MATS did not add every requested curve")
    for curve, expected in zip(curves, effective):
        for parameter, value in expected.items():
            actual = curve.get(parameter)
            if actual != value and actual != [value] and value != [actual]:
                raise RunError(f"Curve {expected.get('label')}: {parameter} differs from accepted value {value!r}")


def validate_curves(job: PlotJob, curves: list[dict], models: list[str]) -> None:
    if len(curves) != len(job.curves) or len(models) != len(job.curves):
        raise RunError("MATS did not add every requested experiment/statistic curve")
    for specification, curve, model in zip(job.curves, curves, models):
        expected = {"label": specification.label, "data-source": model,
                    "database": job.truth, "region-type": "Predefined region",
                    "region": job.region, "variable": job.variable,
                    "statistic": STATISTICS[specification.statistic], "curve-dates": job.mats_dates,
                    "top": "1", "bottom": "1000", "valid-time": "unused"}
        for parameter, value in expected.items():
            if curve.get(parameter) != value:
                raise RunError(f"Curve {specification.label}: {parameter} differs from requested value {value!r}")
        leads = curve.get("forecast-length")
        if leads != str(specification.lead) and leads != [str(specification.lead)]:
            raise RunError(f"Curve {specification.label}: wrong forecast lead {leads!r}")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


class ResultStore:
    def __init__(self, root: Path, base_url: str):
        self.root = root
        self.base_url = base_url
        self.path = root / ".automats-state.json"
        try:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {"results": {}, "next_request_at": 0}
        except (ValueError, OSError) as error:
            raise RunError(f"Cannot read resume state: {error}") from error
        if not isinstance(self.state, dict) or not isinstance(self.state.get("results"), dict):
            raise RunError("Invalid resume state")

    def flush(self) -> None:
        _atomic_write(self.path, json.dumps(self.state, indent=2).encode())

    def completed(self, job: PlotJob, *, current_style: bool = True, effective: dict | None = None) -> bool:
        record = self.state["results"].get(job.relative_path, {})
        if not isinstance(record, dict):
            return False
        if current_style and record.get("figure_style_version") != FIGURE_STYLE_VERSION:
            return False
        if record.get("fingerprint") != job.fingerprint or record.get("base_url") != self.base_url:
            return False
        if effective is not None and record.get("mats", {}).get("effective_settings") != effective:
            return False
        try:
            image = (self.root / job.relative_path).read_bytes()
        except OSError:
            return False
        return is_png(image) and sha256(image).hexdigest() == record.get("sha256")

    def save(self, job: PlotJob, image: bytes, metadata: dict) -> None:
        if not is_png(image):
            raise RunError("Plotly did not return a PNG image")
        _atomic_write(self.root / job.relative_path, image)
        self.state["results"][job.relative_path] = {
            "fingerprint": job.fingerprint, "base_url": self.base_url,
            "figure_style_version": FIGURE_STYLE_VERSION,
            "sha256": sha256(image).hexdigest(), "configuration": asdict(job),
            "mats": metadata,
        }
        self.flush()


class QueryPacer:
    def __init__(self, settings: Settings, clock=time.monotonic, wait=time.sleep,
                 state_path: Path | None = None, wall_clock=time.time):
        self.settings = settings
        self.clock = clock
        self.wait = wait
        self.wall_clock = wall_clock
        self.state_path = state_path
        self.next_start = 0.0
        self.wait_reason = "Minimum wait between plots"
        if state_path and state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                deadline = state["next_request_at"]
                self.next_start = self.clock() + max(0, deadline - self.wall_clock())
                reason = state.get("reason")
                self.wait_reason = reason if isinstance(reason, str) and reason else "Previous run pacing deadline (reason not recorded)"
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise RunError(f"Cannot read request pacing state: {error}") from error

    def _persist(self) -> None:
        if self.state_path:
            deadline = self.wall_clock() + max(0, self.next_start - self.clock())
            _atomic_write(self.state_path, json.dumps({"next_request_at": deadline, "reason": self.wait_reason}).encode())

    def remaining(self) -> float:
        return max(0, self.next_start - self.clock())

    def wait_ready(self) -> None:
        while self.clock() < self.next_start:
            self.wait(self.next_start - self.clock())

    def before_query(self) -> None:
        self.wait_ready()
        self.next_start = self.clock() + self.settings.minimum_waiting_seconds
        self.wait_reason = "Minimum wait between plots"
        self._persist()

    def after_plot(self) -> None:
        deadline = self.clock() + self.settings.minimum_waiting_seconds
        if deadline >= self.next_start:
            self.next_start = deadline
            self.wait_reason = "Minimum wait between plots"
        self._persist()

    def cooldown(self, retry_after: float = 0, *, reason: str = "MATS server throttling cooldown") -> None:
        delay = max(self.settings.cooldown_seconds, retry_after)
        deadline = self.clock() + delay
        if deadline >= self.next_start:
            self.next_start = deadline
            self.wait_reason = reason
        self._persist()


def run_plan(plan: Plan, output: Path, limit: int | None = None, headless: bool = True,
             log=print, *, metadata_only: bool = False, show_browser: bool = False, status=None) -> int:
    def report(message, completed=None, total=None, wait_seconds=None):
        if status is not None:
            try:
                details = {"completed": completed, "total": total}
                if wait_seconds is not None:
                    details["wait_seconds"] = wait_seconds
                status(message, **details)
            except (OSError, ValueError):
                pass

    report("Initializing browser automation")
    from automats.browser import Throttled, UpperAirBrowser, annotate_image

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RunError("Playwright is missing; install AutoMATS's declared dependencies first") from error
    cache = Path.home() / ".cache" / "automats"
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / "run.lock"
    report("Checking the run lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise lock_error(lock) from error
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(str(os.getpid()))
        report("Reading previous figure metadata")
        store = ResultStore(output, plan.settings.base_url)
        pacer = QueryPacer(plan.settings, state_path=cache / "pacing.json")
        jobs = [job for job in plan.jobs if not metadata_only or
                (not store.completed(job) and store.completed(job, current_style=False))]
        if not jobs:
            report("No figures need processing", completed=0, total=0)
            log("No cached figures need a metadata refresh" if metadata_only else
                "No figures requested")
            return 0
        report("Starting browser automation", completed=0, total=len(jobs))
        with sync_playwright() as manager:
            local_only = metadata_only
            run_headless = (headless or local_only) and not show_browser
            report("Launching Chrome")
            browser = manager.chromium.launch(
                channel="chrome",
                headless=run_headless,
                args=CHROMIUM_ARGS,
                ignore_default_args=CHROMIUM_IGNORE_DEFAULT_ARGS,
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1600, "height": 1400},
                    user_agent=DEFAULT_USER_AGENT,
                )
                client = UpperAirBrowser(context, plan.settings.base_url,
                                         minimize=not (run_headless or show_browser),
                                         control_waiting_seconds=plan.settings.control_waiting_seconds)
                saved = 0
                for index, job in enumerate(jobs, 1):
                    if limit is not None and saved >= limit:
                        break
                    label = Path(job.relative_path).name
                    report(f"Preparing {label}", completed=index - 1, total=len(jobs))
                    if metadata_only:
                        report(f"Updating local image metadata: {label}")
                        log(f"[{index}/{len(jobs)}] Add metadata locally: {job.relative_path}")
                        page = context.new_page()
                        try:
                            image = (output / job.relative_path).read_bytes()
                            metadata = dict(store.state["results"][job.relative_path]["mats"])
                            metadata.pop("disclaimer", None)
                            image, metadata["figure"] = annotate_image(
                                page, job, image, metadata.get("figure"),
                                plotted_curves=metadata.get("curves"))
                            store.save(job, image, metadata)
                            saved += 1
                            report(f"Updated {label}", completed=index)
                        finally:
                            page.close()
                        continue
                    submitted = False
                    query_pending = False
                    try:
                        for attempt in range(1, plan.settings.max_attempts + 1):
                            remaining = pacer.remaining()
                            if remaining > 0:
                                report(pacer.wait_reason, wait_seconds=remaining)
                            pacer.wait_ready()
                            report(f"Preparing MATS controls and validating curves: {label}")
                            try:
                                client.prepare(job)
                                break
                            except Throttled as error:
                                pacer.cooldown(error.retry_after, reason="MATS HTTP 429 rate-limit protection")
                                if attempt == plan.settings.max_attempts:
                                    raise
                                log(f"{error}; cooling down before retrying page preparation")
                        remaining = pacer.remaining()
                        if remaining > 0:
                            report(pacer.wait_reason, wait_seconds=remaining)
                        pacer.before_query()
                        log(f"[{index}/{len(jobs)}] {job.relative_path}")
                        report(f"Querying MATS and exporting image: {label}")
                        submitted = True
                        query_pending = True
                        image, metadata = client.plot(job)
                        query_pending = False
                        report(f"Saving PNG and metadata: {label}")
                        store.save(job, image, metadata)
                        saved += 1
                        pacer.after_plot()
                        report(f"Saved {label}", completed=index)
                    except BaseException as error:
                        if isinstance(error, Throttled):
                            pacer.cooldown(error.retry_after, reason="MATS HTTP 429 rate-limit protection")
                        elif query_pending:
                            pacer.cooldown(reason="Previous submitted query may still be running")
                        if submitted:
                            log("Stopped after a submitted query; no query retries or further plots will be sent")
                        if isinstance(error, RunError):
                            stage = "after submitting a plot query" if submitted else "before submitting this figure's plot query"
                            raise RunError(
                                f"Plot {job.name!r} ({job.application} / {job.plot_type}), figure {job.relative_path!r}: "
                                f"{error} Stopped {stage}; {saved} figure(s) saved during this run are retained."
                            ) from error
                        raise
                report(f"Run complete: {saved} figure(s) saved")
                return saved
            finally:
                report("Closing Chrome")
                browser.close()
    finally:
        lock.unlink(missing_ok=True)