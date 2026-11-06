"""MATS parameter discovery, curve validation, and Plotly export."""

import base64
from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from urllib.parse import urljoin, urlsplit

from automats.config import PlotJob, STATISTICS, VARIABLES, VARIABLE_ALIASES
from automats.runner import ResolutionError, RunError, is_png, resolve_option, validate_effective_curves


def discover_applications(links: list[dict], base_url: str) -> list[dict]:
    base_url = base_url.rstrip("/") + "/"
    origin = urlsplit(base_url)
    applications = {}
    for link in links:
        title = " ".join(str(link.get("title", "")).split())
        url = urljoin(base_url, str(link.get("href", "")))
        parsed = urlsplit(url)
        if (not title or parsed.scheme != origin.scheme or parsed.netloc.lower() != origin.netloc.lower()
                or not parsed.path.startswith(origin.path) or parsed.query or parsed.fragment):
            continue
        slug = parsed.path[len(origin.path):].strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
            continue
        url = url.rstrip("/")
        applications.setdefault(url, {"title": title, "slug": slug, "url": url})
    return list(applications.values())


def resolve_application(requested: str, applications: list[dict]) -> dict:
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.casefold())
    target = normalize(requested)
    matches = [app for app in applications if target in {normalize(app["title"]), normalize(app["slug"])}]
    if not matches and target:
        matches = [app for app in applications if target in normalize(app["title"]) or target in normalize(app["slug"])]
    if len(matches) != 1:
        raise ResolutionError("application", requested, [app["title"] for app in applications],
                              [app["title"] for app in matches])
    return matches[0]


def resolve_job_dates(job: PlotJob, default_dates: str) -> PlotJob:
    if job.start is not None and job.end is not None:
        return job
    try:
        default_start, default_end = default_dates.split(" - ")
        start = job.start or datetime.strptime(default_start, "%m/%d/%Y %H:%M").replace(tzinfo=timezone.utc).isoformat()
        end = job.end or datetime.strptime(default_end, "%m/%d/%Y %H:%M").replace(tzinfo=timezone.utc).isoformat()
    except (AttributeError, TypeError, ValueError) as error:
        raise RunError(f"Cannot read the first experiment's default date range: {default_dates!r}") from error
    if start >= end:
        raise RunError(f"Resolved date range is invalid: {start} - {end}; start must precede end. "
                       "Set common.start and common.end explicitly.")
    return replace(job, start=start, end=end)


def figure_header(job: PlotJob, effective: dict | None = None) -> list[str]:
    if effective and effective.get("date_range"):
        job = replace(job, start=effective["date_range"]["start"], end=effective["date_range"]["end"])
    elif effective and job.mats_dates is None:
        dates = effective.get("plot_parameters", {}).get("dates") if effective["plot_type"] == "TimeSeries" else \
            effective["curves"][0].get("curve-dates")
        job = resolve_job_dates(job, dates)
    start = datetime.fromisoformat(job.start).strftime("%Y-%m-%d %H:%M") if job.start else "MATS default start"
    end = datetime.fromisoformat(job.end).strftime("%Y-%m-%d %H:%M") if job.end else "MATS default end"
    if effective:
        curve = effective["curves"][0]
        fields = [f"{start} - {end}", effective["plot_type"],
                  curve.get("truth", curve.get("database", "")), curve.get("region", ""),
                  curve.get("variable", ""), curve.get("statistic", "")]
        if len(job.statistics) > 1:
            fields[-1] = "RMSE (solid) + BIAS (dashed)"
        if job.lead is not None:
            fields.insert(2, job.lead_tag + "h")
        for parameter in ("dieoff-type", "top", "bottom", "averaging"):
            if parameter in curve:
                fields.append(f"{parameter}={curve[parameter]}")
        return [", ".join(str(field) for field in fields if field != "")]
    statistic = "RMSE (solid) + BIAS (dashed)" if len(job.statistics) > 1 else (job.statistic or "default").upper()
    lead = f", {job.lead_tag}h" if job.lead is not None else ""
    return [
        f"{start} - {end}{lead}, {job.truth}, {job.region}, "
        f"1-1000 hPa, {job.variable_tag}, {statistic}",
    ]


ANNOTATE_IMAGE = """async ({image, header, experiments, sourceTop, removeOldLegend, clearDisclaimer,
    lineWeight=2, markerWeight=7}) => {
    for (const experiment of experiments)
        if (!CSS.supports('color', experiment.color)) throw Error('Invalid plot color: ' + experiment.color);
    const source = new Image();
    source.src = image;
    await source.decode();
    const canvas = document.createElement('canvas');
    canvas.width = source.naturalWidth;
    const context = canvas.getContext('2d');
    if (removeOldLegend) {
        canvas.height = source.naturalHeight;
        context.drawImage(source, 0, 0);
        const scanHeight = Math.min(source.naturalHeight - sourceTop, 400);
        const pixels = context.getImageData(0, sourceTop, canvas.width, scanHeight).data;
        let border = -1;
        for (let row = 0; row < scanHeight; row++) {
            let dark = 0;
            for (let column = 0; column < canvas.width; column++) {
                const offset = (row * canvas.width + column) * 4;
                if (pixels[offset] < 100 && pixels[offset + 1] < 100 && pixels[offset + 2] < 100) dark++;
            }
            if (dark > canvas.width * 0.65) { border = row; break; }
        }
        if (border < 0) throw Error('Cannot safely locate the plot border for local legend refresh');
        sourceTop += Math.max(0, border - 4);
    }
    const chartHeight = source.naturalHeight - sourceTop;
    if (chartHeight <= 0) throw Error('Invalid previous metadata header height');
    const padding = 12;
    const availableWidth = canvas.width - 2 * padding;
    const font = '16px sans-serif';
    context.font = font;
    const wrap = (text, width) => {
        const lines = [];
        let line = '';
        for (const character of text) {
            if (line && context.measureText(line + character).width > width) {
                lines.push(line); line = '';
            }
            line += character;
        }
        lines.push(line);
        return lines;
    };
    const rows = [];
    for (const text of header) {
        rows.push(...wrap(text, availableWidth));
    }
    const symbolWidth = 38;
    const widths = experiments.map(experiment => context.measureText(experiment.name).width + symbolWidth + 18);
    const stacked = widths.reduce((total, width) => total + width, 0) > availableWidth;
    const legend = [];
    let legendHeight = 0;
    let horizontalOffset = 0;
    for (const [index, experiment] of experiments.entries()) {
        const lines = wrap(experiment.name, availableWidth - symbolWidth);
        legend.push({experiment, lines, x:stacked ? padding : padding + horizontalOffset,
                     y:stacked ? legendHeight : 0});
        if (stacked) legendHeight += lines.length * 24;
        else { legendHeight = 24; horizontalOffset += widths[index]; }
    }
    const metadataHeight = 4 + rows.length * 22;
    const legendTop = metadataHeight + 4;
    const headerHeight = legendTop + legendHeight + 6;
    canvas.height = chartHeight + headerHeight;
    context.fillStyle = 'white';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = '#202020';
    context.textBaseline = 'top';
    context.font = font;
    let position = 4;
    for (const row of rows) {
        context.fillText(row, padding, position);
        position += 22;
    }
    for (const entry of legend) {
        const position = legendTop + entry.y;
        context.strokeStyle = entry.experiment.color;
        context.fillStyle = entry.experiment.color;
        context.lineWidth = lineWeight;
        context.beginPath(); context.moveTo(entry.x, position + 12);
        context.lineTo(entry.x + 30, position + 12); context.stroke();
        context.beginPath(); context.arc(entry.x + 15, position + 12, markerWeight / 2, 0, 2 * Math.PI); context.fill();
        context.fillStyle = '#202020';
        for (const [index, line] of entry.lines.entries())
            context.fillText(line, entry.x + symbolWidth, position + 4 + index * 24);
    }
    context.drawImage(source, 0, sourceTop, source.naturalWidth, chartHeight,
        0, headerHeight, canvas.width, chartHeight);
    if (clearDisclaimer && !removeOldLegend) {
        context.fillStyle = 'white';
        context.fillRect(0, headerHeight, canvas.width, 18);
    }
    return {image:canvas.toDataURL('image/png'), width:canvas.width,
            height:canvas.height, header_height:headerHeight,
            legend_rows:stacked ? legendHeight / 24 : 1, legend_in_header:true,
            legend_names:experiments.map(experiment => experiment.name)};
}"""


def annotate_image(page, job: PlotJob, image: bytes, previous_figure: dict | None = None,
                   *, effective_settings: dict | None = None,
                   plotted_curves: list[dict] | None = None) -> tuple[bytes, dict]:
    if not is_png(image):
        raise RunError("Metadata rendering requires a PNG image")
    effective_settings = effective_settings or (previous_figure or {}).get("effective_settings")
    header = figure_header(job, effective_settings)
    source_top = (previous_figure or {}).get("header_height", 0)
    if type(source_top) is not int or source_top < 0:
        raise RunError("Invalid previous metadata header height")
    legend_experiments = [asdict(experiment) for experiment in job.legend_entries]
    if plotted_curves is not None and len(plotted_curves) < len(job.curves):
        plotted_names = {c.get("name") for c in plotted_curves if c.get("name")}
        if len(job.leads) == 1:
            filtered = [asdict(exp) for exp in job.experiments if exp.name in plotted_names]
        else:
            filtered = [
                asdict(entry) for entry, (exp, lead) in zip(
                    job.legend_entries,
                    [(e, l) for e in job.experiments for l in job.leads]
                )
                if any(c.get("name") == exp.name and c.get("lead") == lead for c in plotted_curves)
            ]
        if filtered:
            legend_experiments = filtered
    rendered = page.evaluate(ANNOTATE_IMAGE, {
        "image": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
        "header": header,
        "experiments": legend_experiments,
        "sourceTop": source_top,
        "removeOldLegend": bool(previous_figure) and not previous_figure.get("legend_in_header", False),
        "clearDisclaimer": bool((previous_figure or {}).get("disclaimer")),
        "lineWeight": job.line_weight,
        "markerWeight": job.marker_weight,
    })
    prefix = "data:image/png;base64,"
    if not rendered["image"].startswith(prefix):
        raise RunError("Metadata rendering did not return a PNG")
    metadata = {key: rendered[key] for key in (
        "width", "height", "header_height", "legend_rows", "legend_in_header", "legend_names")}
    metadata["header"] = header
    metadata["legend_styles"] = ["solid"]
    metadata["line_weight"] = job.line_weight
    metadata["marker_weight"] = job.marker_weight
    if effective_settings:
        metadata["effective_settings"] = effective_settings
    image = base64.b64decode(rendered["image"][len(prefix):], validate=True)
    if not is_png(image):
        raise RunError("Metadata rendering returned an invalid PNG")
    return image, metadata


class Throttled(RunError):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value: str, now: float | None = None) -> float:
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


SET_PARAMETER = """async ({name, value}) => {
    matsParamUtils.setInputValueForParamAndTriggerChange(name, value);
    const tracker = globalThis.Tracker || globalThis.Package?.tracker?.Tracker;
    await new Promise(resolve => tracker.afterFlush(resolve));
    return matsParamUtils.getValueForParamName(name);
}"""

PARAMETERS = """() => {
    const values = matsParamUtils.getElementValues();
    return Object.entries({...values.curveParams, ...values.plotParams}).map(([name, value]) => {
        const parameter = matsParamUtils.getParameterForName(name);
        let element;
        try { element = matsParamUtils.getInputElementForParamName(name); } catch (_) {}
        const options = element?.options ? Array.from(element.options).filter(option => !option.disabled)
            .map(option => ({value:option.value, label:option.textContent.trim()})) : [];
        if (!options.length && parameter?.type === 'radioGroup') {
            for (const input of document.querySelectorAll('input[type="radio"]')) {
                if (input.name === name && !input.disabled) options.push({value:input.value, label:input.value});
            }
        }
        let current = value;
        if (element?.options) current = element.multiple ?
            Array.from(element.selectedOptions).map(option => option.value) : element.value;
        else if (parameter?.type === 'dateRange') current = matsParamUtils.getValueForParamName(name);
        else if (element && 'value' in element) current = element.value;
        let label = parameter?.label || parameter?.text;
        if (!label) {
            try {
                const itemBtn = document.querySelector('#' + name + '-item .control-button, #' + name + '-item button, #' + name + '-item label');
                if (itemBtn) label = itemBtn.textContent.trim();
            } catch (_) {}
        }
        if (!label && name === 'plotFormat') label = 'Difference Curves';
        return {name, label:label || name, type:parameter?.type,
            value:current, options, disabled:!!element?.disabled,
            scope:name in values.curveParams ? 'curve' : 'plot'};
    });
}"""

FLUSH = """async () => {
    const tracker = globalThis.Tracker || globalThis.Package?.tracker?.Tracker;
    await new Promise(resolve => tracker.afterFlush(resolve));
}"""

CONTROLS_READY = """() => {
    const meteor = globalThis.Meteor || globalThis.Package?.meteor?.Meteor;
    const status = meteor?.status?.();
    const blocks = meteor?.connection?._outstandingMethodBlocks || [];
    return status?.connected !== false && !blocks.some(block => block.methods?.length);
}"""

RESET_CONTROLS = """async () => {
    if (typeof matsCurveUtils.clearAllUsed !== 'function' ||
        typeof matsParamUtils.setAllParamsToDefault !== 'function')
        throw Error('MATS does not expose the controls reset functions required for sequential plots');
    matsCurveUtils.clearAllUsed();
    matsParamUtils.setAllParamsToDefault();
    const tracker = globalThis.Tracker || globalThis.Package?.tracker?.Tracker;
    await new Promise(resolve => tracker.afterFlush(resolve));
}"""

PLOT_STATUS = """() => {
    const error = Session.get('errorMessage');
    if (error) return {error};
    const graph = document.getElementById('placeholder');
    const key = Session.get('plotResultKey');
    const result = matsCurveUtils.getGraphResult();
    if (!key || !result.data || !graph?.data || !graph._fullLayout ||
        graph.getBoundingClientRect().width < 100 ||
        document.getElementById('spinner')?.style.display !== 'none') return false;
    return {key};
}"""

EXPORT_IMAGE = """async ({curves, width, height, timeSeries=false, lineWeight=2, markerWeight=7, allowPartial=false}) => {
    const graph = document.getElementById('placeholder');
    const result = matsCurveUtils.getGraphResult();
    const summary = [];
    const missing = [];
    for (const curve of curves) {
        const matches = graph.data.map((trace, index) => ({trace, index}))
            .filter(({trace}) => trace.label === curve.label);
        if (matches.length > 1) throw Error('Missing or ambiguous curve: ' + curve.label);
        if (matches.length === 0) {
            if (!allowPartial) throw Error('Missing or ambiguous curve: ' + curve.label);
            missing.push(curve.label);
            continue;
        }
        const {trace, index} = matches[0];
        const points = Array.from(trace.x || []).filter((value, point) =>
            value !== null && value !== undefined && value !== '' &&
            (Number.isFinite(Number(value)) || timeSeries && typeof value === 'string' &&
                Number.isFinite(Date.parse(value))) &&
            trace.y?.[point] !== null && trace.y?.[point] !== undefined &&
            Number.isFinite(Number(trace.y[point]))).length;
        if (!points) {
            if (!allowPartial) throw Error('No data for ' + curve.label);
            missing.push(curve.label);
            await Plotly.restyle(graph, {visible: false, showlegend: false}, [index]);
            continue;
        }
        summary.push({...curve, points});
        await Plotly.restyle(graph, {
            name:curve.label, 'line.color':curve.color, 'line.dash':curve.dash,
            'line.width':lineWeight, 'marker.size':markerWeight,
            'marker.color':curve.color, 'error_x.color':curve.color,
            'error_y.color':curve.color, showlegend:true
        }, [index]);
    }
    if (summary.length === 0) {
        throw Error(missing.length ? 'No data for curves: ' + missing.join(', ') : 'No data for requested curves');
    }
    const originalAnnotations = graph.layout.annotations || [];
    const annotations = originalAnnotations.filter(annotation =>
        !/research purposes/i.test(annotation.text || ''));
    await Plotly.relayout(graph, {
        showlegend:false, 'title.text':'', 'margin.t':6,
        annotations
    });
    return {
        image:await Plotly.toImage(graph, {format:'png', width, height}),
        metadata:{result_key:Session.get('plotResultKey'), curves:summary,
                  plot_params:result.basis?.plotParams,
                  layout:{margin:graph.layout.margin, legend:graph.layout.legend}}
    };
}"""


def minimize_window(page) -> None:
    session = page.context.new_cdp_session(page)
    try:
        window = session.send("Browser.getWindowForTarget")
        session.send("Browser.setWindowBounds", {
            "windowId": window["windowId"], "bounds": {"windowState": "minimized"},
        })
    finally:
        session.detach()


class UpperAirBrowser:
    def __init__(self, context, base_url: str, *, minimize: bool = True, control_waiting_seconds: float = 0.2):
        self.context = context
        self.control_waiting_seconds = control_waiting_seconds
        self.base_url = base_url.rstrip("/")
        self.url = self.base_url + "/"
        self.applications = None
        self.minimize = minimize
        self.page = None
        self.network_error = None
        self.curves = []
        self.effective_settings = {}
        self.matched_samples = True
        self.loaded_application = None
        self.plotted = False

    def _response(self, response) -> None:
        if not response.url.startswith("https://gsl.noaa.gov/"):
            return
        parsed = urlsplit(response.url)
        metadata_request = "sockjs" in parsed.path.split("/") or parsed.path.endswith("/websocket")
        if response.status in {401, 403} and parsed.path.startswith(urlsplit(self.base_url).path + "/") and (
                metadata_request or response.request.resource_type == "document"):
            url = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
            headers = response.headers
            diagnostics = []
            for name in ("server", "cf-ray", "cf-mitigated"):
                value = " ".join(headers.get(name, "").split())[:160]
                if value:
                    diagnostics.append(f"{name}={value}")
            details = f" ({'; '.join(diagnostics)})" if diagnostics else ""
            subject = "metadata connection" if metadata_request else "page access"
            self.network_error = RunError(
                f"MATS denied {subject}: HTTP {response.status} at {url}{details}. "
                "The page can load while metadata remains unavailable. "
                "Stopping without access-denial retries; check authorized access with MATS administrators."
            )
        elif response.status == 429:
            self.network_error = Throttled("MATS returned HTTP 429", retry_after_seconds(
                response.headers.get("retry-after", "0")))
        elif response.status in {502, 503, 504}:
            self.network_error = RunError(f"MATS returned HTTP {response.status}; stopping rather than repeating unknown server work")

    def _error(self) -> None:
        if self.network_error:
            raise self.network_error
        error = self.page.evaluate("() => Session.get('errorMessage') || ''")
        if error:
            raise RunError(f"MATS: {error}")

    def _open_page(self, url: str) -> None:
        response = self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        if self.network_error:
            raise self.network_error
        if response and response.status >= 400:
            raise RunError(f"MATS access failed: HTTP {response.status} at {url}; page title: {self.page.title()!r}")
        if "Just a moment" in self.page.title():
            raise RunError("MATS access challenge: stop and use an authorized browser session")

    def _parameters(self) -> list[dict]:
        return self.page.evaluate(PARAMETERS)

    def _settle_controls(self) -> None:
        started = time.monotonic()
        stable_since = started
        previous = None
        while True:
            self.page.evaluate(FLUSH)
            self._error()
            ready = self.page.evaluate(CONTROLS_READY)
            state = json.dumps(self._parameters(), sort_keys=True)
            self._error()
            now = time.monotonic()
            if not ready or state != previous:
                stable_since = now
                previous = state
            if ready and now - stable_since >= self.control_waiting_seconds:
                return
            if now - started >= 30:
                raise RunError("MATS controls did not settle within 30 seconds; stopping before the next control action")
            self.page.wait_for_timeout(min(100, self.control_waiting_seconds * 1000))

    def _snapshot(self) -> dict:
        parameters = self._parameters()
        return {scope + "Params": {parameter["name"]: parameter["value"] for parameter in parameters
                                  if parameter["scope"] == scope} for scope in ("curve", "plot")}

    def _resolve_parameter(self, name: str) -> dict:
        parameters = self._parameters()
        aliases = {
            "forecast lead time": "forecast-length",
            "difference curves": "plotFormat",
            "difference_curves": "plotFormat",
            "difference-curves": "plotFormat",
            "diff curves": "plotFormat",
            "diff_curves": "plotFormat",
            "diff": "plotFormat",
            "difference": "plotFormat",
        }
        if self.application == "upperair":
            aliases["truth"] = "database"
        try:
            selected = resolve_option(name, [parameter["name"] for parameter in parameters], "parameter", aliases)
        except ResolutionError as error:
            if not error.missing:
                raise
            label = resolve_option(name, [parameter["label"] for parameter in parameters], "parameter", aliases)
            selected = next(parameter["name"] for parameter in parameters if parameter["label"] == label)
        return next(parameter for parameter in parameters if parameter["name"] == selected)

    def _set(self, name: str, value, choice: bool = True):
        parameter = self._resolve_parameter(name)
        name = parameter["name"]
        if parameter["disabled"]:
            raise RunError(f"Parameter {name!r} is disabled for this plot")
        requested = str(value).lower() if isinstance(value, bool) else str(value)
        if parameter["options"]:
            options = parameter["options"]
            aliases = None
            if name == "variable" and self.application == "surface":
                aliases = VARIABLE_ALIASES
            elif name == "variable" and self.application == "upperair":
                aliases = {key: label for key, (label, tag) in VARIABLES.items()}
            elif name == "statistic" and self.application in {"surface", "upperair"}:
                aliases = STATISTICS
            elif name == "plotFormat":
                aliases = {
                    "none": "none",
                    "false": "none",
                    "diff": "Diff all curves against the 1st added curve",
                    "diff all": "Diff all curves against the 1st added curve",
                    "diff all curves": "Diff all curves against the 1st added curve",
                    "diff all curves against the 1st added curve": "Diff all curves against the 1st added curve",
                    "true": "Diff all curves against the 1st added curve",
                    "pairwise": "Diff the 1st and 2nd curves, 3rd and 4th, etc",
                    "diff pairwise": "Diff the 1st and 2nd curves, 3rd and 4th, etc",
                    "diff the 1st and 2nd curves, 3rd and 4th, etc": "Diff the 1st and 2nd curves, 3rd and 4th, etc",
                }
            candidates = [option["value"] for option in options]
            try:
                selected = resolve_option(requested, candidates, name, aliases)
            except ResolutionError as error:
                if name == "data-source" or not error.missing:
                    raise
                selected_label = resolve_option(requested, [option["label"] for option in options], name, aliases)
                selected = next(option["value"] for option in options if option["label"] == selected_label)
            requested = selected
        elif parameter["type"] not in {"textInput", "numberSpinner", "dateRange"}:
            raise RunError(f"Parameter {name!r} has no dropdown options or supported scalar input")
        self.page.evaluate(SET_PARAMETER, {"name": name, "value": requested})
        self._settle_controls()
        actual = next(parameter["value"] for parameter in self._parameters() if parameter["name"] == name)
        if actual != requested and actual != [requested]:
            if name in {"dates", "curve-dates"} and isinstance(actual, str) and " - " in actual:
                self.expected[name] = actual
                return actual
            model = self.expected.get("data-source")
            context = f" for experiment {model!r}" if model else ""
            raise RunError(f"MATS did not accept {name}={requested!r}{context}: {actual!r}")
        self.expected[name] = actual
        return actual

    def prepare(self, job: PlotJob) -> None:
        from playwright.sync_api import TimeoutError as BrowserTimeout

        self.network_error = None
        if self.page is None or self.page.is_closed():
            self.page = self.context.new_page()
            self.loaded_application = None
            self.plotted = False
            self.page.on("response", self._response)
            if self.minimize:
                minimize_window(self.page)
        if self.applications is None:
            self._open_page(self.base_url + "/")
            links = self.page.locator("a[href]").evaluate_all("""links => links.map(link => ({
                title:link.textContent.trim(), href:link.href
            }))""")
            self.applications = discover_applications(links, self.base_url)
            if not self.applications:
                raise RunError(f"No application links were found on the MATS homepage: {self.base_url}")
        selected = resolve_application(job.application, self.applications)
        self.application = selected["slug"]
        self.url = selected["url"]
        if self.loaded_application == self.application:
            if self.plotted:
                self.page.locator("#backButton").click(timeout=10000)
                self.plotted = False
                self._settle_controls()
            self.page.evaluate(RESET_CONTROLS)
            self.page.wait_for_function("() => (Session.get('Curves') || []).length === 0", polling=250, timeout=10000)
            self._settle_controls()
        else:
            self._open_page(self.url)
            self.plotted = False
        for attempt in range(90):
            if self.network_error:
                raise self.network_error
            try:
                self.page.wait_for_function("""() => typeof matsParamUtils !== 'undefined' &&
                    typeof matsCurveUtils !== 'undefined' && typeof Session !== 'undefined' &&
                    (globalThis.Tracker || globalThis.Package?.tracker?.Tracker) && document.querySelector('#add') &&
                    document.querySelector('#plotTypes-selector') &&
                    typeof matsParamUtils.getElementValues === 'function' &&
                    (matsParamUtils.getOptionsForParam('data-source') || []).length > 0""", timeout=1000, polling=250)
                break
            except BrowserTimeout as error:
                if self.network_error:
                    raise self.network_error from error
                if attempt == 89:
                    state = self.page.evaluate("""() => {
                        const meteor = globalThis.Meteor || globalThis.Package?.meteor?.Meteor;
                        const status = meteor?.status();
                        let modelCount = null;
                        try { modelCount = typeof matsParamUtils === 'undefined' ? 0 :
                            (matsParamUtils.getOptionsForParam('data-source') || []).length; } catch {}
                        return {loadingMetadata: document.body.innerText.includes('Loading app metadata'),
                            connected: status?.connected ?? null, connectionStatus: status?.status ?? null,
                            addButton: !!document.querySelector('#add'),
                            plotSelector: !!document.querySelector('#plotTypes-selector'), modelCount};
                    }""")
                    raise RunError(f"Application {selected['title']!r} did not expose the required MATS curve controls "
                                   "within 90 seconds; it may require a different workflow or have a connection failure. "
                                   f"Readiness: {json.dumps(state, sort_keys=True)}. No plot query was submitted.") from error
        if self.network_error:
            raise self.network_error
        self.loaded_application = self.application
        types = self.page.locator("#plotTypes-selector").evaluate("""element => Array.from(element.options)
            .filter(option => !option.disabled).map(option => ({value:option.value, label:option.textContent.trim()}))""")
        plot_type = resolve_option(job.plot_type, [option["value"] for option in types], "plot_type",
                                   {"time series": "TimeSeries"})
        self.page.select_option("#plotTypes-selector", value=plot_type)
        self.page.wait_for_function("value => Session.get('plotType') === value", arg=plot_type, polling=250)
        self._settle_controls()
        if len({curve.label for curve in job.curves}) != len(job.curves):
            raise RunError("Overlay labels must be unique; MATS rejects duplicate curve labels")
        self.expected = {}
        if self.application == "upperair" and job.truth is not None:
            self._set("truth", job.truth)
        is_combined = len(job.curves) > 1
        model_options = [option["value"] for option in self._resolve_parameter("data-source")["options"]]
        unavailable = []
        model_names = [experiment.name for experiment in job.experiments]
        if job.default_date_experiment is not None and job.default_date_experiment not in model_names:
            model_names.append(job.default_date_experiment)
        available_models = set()
        for model in model_names:
            try:
                resolved = resolve_option(model, model_options, "data-source")
                available_models.add(model)
                available_models.add(resolved)
            except ResolutionError as error:
                unavailable.append(str(error))
        available_experiments = [exp for exp in job.experiments if exp.name in available_models]
        if not available_experiments or (unavailable and not is_combined):
            raise RunError("Experiment validation failed before adding curves:\n" + "\n".join(unavailable))
        default_curves = []
        requested_curves = []
        self.added_curve_specs = []
        curve_errors = {}
        matched_button = self.page.locator("#plotMatched")
        self.matched_samples = job.matched_samples if job.matched_samples is not None else matched_button.count() > 0
        date_parameter = "dates" if plot_type == "TimeSeries" else "curve-dates"
        date_job = job
        if job.mats_dates is None:
            default_model = job.default_date_experiment if job.default_date_experiment in available_models else \
                available_experiments[0].name
            self._set("data-source", default_model)
            if self.application != "upperair" and job.truth is not None:
                self._set("truth", job.truth)
            date_job = resolve_job_dates(job, self._resolve_parameter(date_parameter)["value"])
        for index, curve in enumerate(job.curves):
            if curve.name not in available_models:
                curve_errors[curve.label] = f"Experiment/model {curve.name!r} is unavailable in MATS"
                continue
            try:
                self.expected = {}
                if self.application == "upperair" and job.truth is not None:
                    self._set("truth", job.truth)
                self._set("data-source", curve.name)
                if self.application != "upperair" and job.truth is not None:
                    self._set("truth", job.truth)
                if job.region is not None:
                    self._set("region", job.region)
                if job.variable is not None:
                    self._set("variable", job.variable)
                if curve.statistic is not None:
                    self._set("statistic", curve.statistic)
                if curve.lead is not None:
                    self._set("forecast-length", str(curve.lead))
                assigned = set(self.expected)
                for name, value in job.parameters:
                    parameter = self._resolve_parameter(name)
                    if parameter["scope"] == "plot":
                        continue
                    if parameter["name"] in assigned or parameter["name"] in {
                            "data-source", "label", "curve-dates", "plot-dates", "color"}:
                        raise RunError(f"Duplicate or reserved MATS parameter: {name}")
                    assigned.add(parameter["name"])
                    self._set(name, value)
                actual_dates = self._set(date_parameter, date_job.mats_dates, choice=False)
                if actual_dates != date_job.mats_dates and isinstance(actual_dates, str) and " - " in actual_dates:
                    try:
                        s_part, e_part = actual_dates.split(" - ")
                        c_start = datetime.strptime(s_part, "%m/%d/%Y %H:%M").replace(tzinfo=timezone.utc).isoformat()
                        c_end = datetime.strptime(e_part, "%m/%d/%Y %H:%M").replace(tzinfo=timezone.utc).isoformat()
                        date_job = replace(date_job, start=c_start, end=c_end)
                    except Exception:
                        pass
                self._set("label", curve.label, choice=False)
                if not self.page.evaluate("color => CSS.supports('color', color)", curve.color):
                    raise RunError(f"Invalid plot color: {curve.color!r}")
                snapshot = self._snapshot()
                validate_effective_curves([{**snapshot["curveParams"], **snapshot["plotParams"]}], [self.expected])
                default_curves.append(snapshot["curveParams"])
                requested_curves.append({name: value for name, value in self.expected.items()
                                         if name in snapshot["curveParams"]})
                self.page.locator("#add").click()
                self.page.wait_for_function("""count => (Session.get('Curves') || []).length === count ||
                    !!Session.get('errorMessage')""", arg=len(requested_curves), polling=250)
                self._settle_controls()
                curves_after = self.page.evaluate("() => Session.get('Curves') || []")
                if len(curves_after) != len(requested_curves):
                    mats_err = self.page.evaluate("() => Session.get('errorMessage') || ''")
                    default_curves.pop()
                    requested_curves.pop()
                    raise RunError(f"MATS failed to add curve {curve.label}: {mats_err}")
                self.added_curve_specs.append(curve)
            except (RunError, ResolutionError) as error:
                if isinstance(error, Throttled) or self.network_error:
                    raise
                if not is_combined:
                    raise
                curve_errors[curve.label] = str(error)
                try:
                    self.page.evaluate("() => Session.set('errorMessage', '')")
                except Exception:
                    pass
        if not self.added_curve_specs:
            if curve_errors:
                error_details = "\n".join(f"- {label}: {err}" for label, err in curve_errors.items())
                raise RunError(f"No curves could be added for plot {job.name!r}:\n{error_details}")
            raise RunError(f"No curves were added for plot {job.name!r}")
        self.curves = self.page.evaluate("() => Session.get('Curves')")
        validate_effective_curves(self.curves, requested_curves)
        effective_curves = [{name: curve.get(name, value) for name, value in defaults.items()}
                    for curve, defaults in zip(self.curves, default_curves)]
        for name, value in job.parameters:
            parameter = self._resolve_parameter(name)
            if parameter["scope"] == "plot":
                self._set(name, value)
        plot_parameters = self._snapshot()["plotParams"]
        self.effective_settings = {"application": self.application, "application_title": selected["title"],
                   "application_url": self.url, "plot_type": plot_type,
                       "matched_samples": self.matched_samples, "curves": effective_curves,
                       "plot_parameters": plot_parameters,
                       "date_range": {"start": date_job.start, "end": date_job.end}}

    def plot(self, job: PlotJob) -> tuple[bytes, dict]:
        from playwright.sync_api import TimeoutError as BrowserTimeout

        self.page.evaluate("() => Session.set('plotResultKey', null)")
        selector = "#plotMatched" if self.matched_samples else "#plotUnmatched"
        self.page.locator(selector).click()
        try:
            status = self.page.wait_for_function(PLOT_STATUS, timeout=300000, polling=250).json_value()
        except BrowserTimeout as error:
            raise RunError("Query completion is unknown after five minutes; stopping to avoid overlapping server work") from error
        self._error()
        if status.get("error"):
            raise RunError(f"MATS: {status['error']}")
        exported = self.page.evaluate(EXPORT_IMAGE, {
            "curves": [asdict(curve) for curve in (getattr(self, "added_curve_specs", None) or job.curves)],
            "width": job.width, "height": job.height,
            "timeSeries": self.effective_settings["plot_type"] == "TimeSeries",
            "lineWeight": job.line_weight,
            "markerWeight": job.marker_weight,
            "allowPartial": len(job.curves) > 1,
        })
        prefix = "data:image/png;base64,"
        if not exported["image"].startswith(prefix):
            raise RunError("Unexpected Plotly image format")
        metadata = exported["metadata"]
        metadata["accepted_curves"] = self.curves
        metadata["effective_settings"] = self.effective_settings
        image = base64.b64decode(exported["image"][len(prefix):], validate=True)
        image, metadata["figure"] = annotate_image(
            self.page, job, image,
            effective_settings=self.effective_settings,
            plotted_curves=metadata["curves"]
        )
        self.plotted = True
        return image, metadata