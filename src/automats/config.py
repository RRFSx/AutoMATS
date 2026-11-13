"""Validate YAML recipes and expand their Cartesian products without accessing MATS."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from itertools import product
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


class ConfigError(ValueError):
    pass


VARIABLES = {"temperature": ("Temperature", "T"), "t": ("Temperature", "T"),
             "rhobt": ("RHobT", "RHobT"), "rh": ("RH", "RH"),
             "wind": ("Wind", "Wind")}
STATISTICS = {"rmse": "RMSE", "bias": "Bias (Model - Obs)", "csi": "CSI"}
STATISTIC_GROUPS = {
    "rmse": ("rmse",),
    "bias": ("bias",),
    "rmse+bias": ("rmse", "bias"),
    "csi": ("csi",),
    "csi+bias": ("csi", "bias"),
}
LEAD_COLORS = ("red", "blue", "green", "orange", "magenta", "teal", "black")
DEFAULT_DESCRIPTION_FONT_SIZE = 24
DEFAULT_LEGEND_FONT_SIZE = 28
APPLICATION_ALIASES = {"upper air": "upperair", "upperair": "upperair", "surface": "surface"}
VARIABLE_ALIASES = {
    "t2m": "Temperature at 2m (\u00b0C)",
    "td2m": "Dewpoint at 2m (\u00b0C)",
    "rh2m": "Relative Humidity at 2m",
    "wind10m": "Wind Speed at 10m (m/s)",
    "wspeed": "Wind Speed at 10m (m/s)",
    "uspeed": "U Wind",
    "vspeed": "V Wind",
}


def _lead_tag(lead: int | tuple[int, ...] | None) -> str:
    if lead is None:
        return ""
    leads = (lead,) if isinstance(lead, int) else lead
    return "f" + "+".join(f"{value:03d}" for value in leads)


def _lead_choice(value: Any) -> int | tuple[int, ...]:
    if type(value) is int and 0 <= value <= 999:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\s*\+\s*[0-9]+)+", value.strip()):
        leads = tuple(int(part.strip()) for part in value.strip().split("+"))
        if all(0 <= lead <= 999 for lead in leads) and len(set(leads)) == len(leads):
            return leads
    raise ConfigError("Forecast leads must be integers 0-999 or unique plus-separated integers, such as 0+1+3")


def _split_comroot(comroot: str | None) -> tuple[str | None, str | None]:
    if not comroot:
        return None, None
    raw = str(comroot).strip()
    if not raw:
        return None, None
    match = re.match(r"^([A-Za-z0-9_-]+):(.*)$", raw)
    if match:
        cluster = match.group(1).strip().lower() or None
        path = match.group(2).strip()
        clean_path = (path.rstrip("/") or "/") if path else None
        return cluster, clean_path
    clean_path = raw.rstrip("/") or "/"
    if clean_path == "/gpfs/f6" or clean_path.startswith("/gpfs/f6/"):
        return "gaeac6", clean_path
    if clean_path == "/gpfs/f7" or clean_path.startswith("/gpfs/f7/"):
        return "gaeac7", clean_path
    return None, clean_path


@dataclass(frozen=True)
class Experiment:
    name: str
    color: str
    overlay: bool = True
    COMROOT: str | None = None
    short_name: str | None = None

    @property
    def label(self) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", self.name)

    @property
    def display_name(self) -> str:
        return self.short_name or self.name

    @property
    def hpc_cluster(self) -> str | None:
        return _split_comroot(self.COMROOT)[0]

    @property
    def remote_comroot(self) -> str | None:
        return _split_comroot(self.COMROOT)[1]


@dataclass(frozen=True)
class CurveSpec:
    name: str
    label: str
    color: str
    statistic: str | None
    dash: str
    lead: int | None


@dataclass(frozen=True)
class PlotJob:
    name: str
    plot_type: str
    truth: str | None
    region: str | None
    matched_samples: bool | None
    variable: str | None
    variable_tag: str
    statistic: str | None
    lead: int | tuple[int, ...] | None
    start: str | None
    end: str | None
    experiments: tuple[Experiment, ...]
    relative_path: str
    width: int = 1200
    height: int = 1200
    forecast_lead_colors: tuple[tuple[int, str], ...] = ()
    application: str = "upperair"
    parameters: tuple[tuple[str, str | int | float | bool], ...] = ()
    line_weight: float = 2
    marker_weight: float = 7
    default_date_experiment: str | None = None
    description_font_size: float = DEFAULT_DESCRIPTION_FONT_SIZE
    legend_font_size: float = DEFAULT_LEGEND_FONT_SIZE

    @property
    def statistics(self) -> tuple[str | None, ...]:
        return STATISTIC_GROUPS.get(self.statistic, (self.statistic,))

    @property
    def leads(self) -> tuple[int | None, ...]:
        return (self.lead,) if self.lead is None or isinstance(self.lead, int) else self.lead

    @property
    def lead_tag(self) -> str:
        return _lead_tag(self.lead)

    def lead_color(self, index: int) -> str:
        return dict(self.forecast_lead_colors).get(self.leads[index], LEAD_COLORS[index % len(LEAD_COLORS)])

    def curve_label(self, experiment: Experiment, statistic: str | None, lead: int | None = None) -> str:
        label = experiment.label
        if len(self.leads) > 1:
            label += f"F{self.leads[0] if lead is None else lead:03d}"
        if len(self.statistics) > 1:
            label += statistic.upper()
        return label

    @property
    def curves(self) -> tuple[CurveSpec, ...]:
        return tuple(CurveSpec(
            experiment.name, self.curve_label(experiment, statistic, lead),
            self.lead_color(index) if len(self.leads) > 1 else experiment.color,
            statistic, "dash" if len(self.statistics) > 1 and statistic == "bias" else "solid", lead)
            for experiment in self.experiments for index, lead in enumerate(self.leads)
            for statistic in self.statistics)

    @property
    def legend_entries(self) -> tuple[Experiment, ...]:
        if len(self.leads) == 1:
            return tuple(
                Experiment(experiment.display_name, experiment.color, experiment.overlay,
                           experiment.COMROOT, experiment.short_name)
                for experiment in self.experiments
            )
        return tuple(Experiment(f"{experiment.display_name} f{lead:03d}h", self.lead_color(index))
                     for experiment in self.experiments for index, lead in enumerate(self.leads))

    @property
    def fingerprint(self) -> str:
        configuration = asdict(self)
        configuration.pop("description_font_size", None)
        configuration.pop("legend_font_size", None)
        for experiment in configuration.get("experiments", ()):
            if isinstance(experiment, dict):
                experiment.pop("short_name", None)
        if not self.forecast_lead_colors:
            configuration.pop("forecast_lead_colors")
        if self.application == "upperair":
            configuration.pop("application")
        if not self.parameters:
            configuration.pop("parameters")
        if self.default_date_experiment is None:
            configuration.pop("default_date_experiment")
        payload = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode()).hexdigest()

    @property
    def mats_dates(self) -> str | None:
        if self.start is None or self.end is None:
            return None
        start = datetime.fromisoformat(self.start).strftime("%m/%d/%Y %H:%M")
        end = datetime.fromisoformat(self.end).strftime("%m/%d/%Y %H:%M")
        return f"{start} - {end}"


@dataclass(frozen=True)
class Settings:
    inter_plot_waiting_seconds: float = 1
    cooldown_seconds: float = 180
    max_attempts: int = 3
    base_url: str = "https://gsl.noaa.gov/mats"
    control_waiting_seconds: float = 0.2


@dataclass(frozen=True)
class SlideSpec:
    plot_group: str
    title: str
    rows: tuple[tuple[str, ...], ...]
    captions: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class PygrafGraphicsConfig:
    enable: bool = False
    recipe: str | None = None


@dataclass(frozen=True)
class Plan:
    jobs: tuple[PlotJob, ...]
    warnings: tuple[str, ...]
    settings: Settings
    slides: tuple[SlideSpec, ...] = ()
    slide_error: str | None = None
    pygraf_graphics: PygrafGraphicsConfig | None = None
    experiments: tuple[Experiment, ...] = ()
    recipe_path: Path | None = None
    title: str | None = None


def _mapping(value: Any, context: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a mapping")
    return value


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must be a nonempty string")
    return value.strip()


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{context} must be true or false")
    return value


def _choices(value: Any, context: str) -> list:
    values = value if isinstance(value, list) else [value]
    if not values or any(item is None for item in values):
        raise ConfigError(f"{context} must have at least one value")
    return values


def _forecast_colors(raw: Any) -> dict[int, str]:
    raw = _mapping(raw, "forecast lead colors")
    colors = {}
    for lead, value in raw.items():
        if type(lead) is not int or not 0 <= lead <= 999:
            raise ConfigError("forecast lead colors keys must be integer forecast hours from 0 to 999")
        color = _text(value, f"forecast lead colors[{lead}]")
        if not re.fullmatch(r"(?:#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?|[A-Za-z]+)", color):
            raise ConfigError(f"forecast lead colors[{lead}] must be a CSS color name or #RGB/#RRGGBB value")
        colors[lead] = color
    return colors


def _date(value: Any, context: str) -> str:
    try:
        parsed = datetime.fromisoformat(_text(value, context).replace("Z", "+00:00"))
    except ValueError as error:
        raise ConfigError(f"{context} must be an ISO timestamp with a timezone") from error
    if parsed.utcoffset() is None:
        raise ConfigError(f"{context} must include a timezone")
    if parsed.second or parsed.microsecond:
        raise ConfigError(f"{context} must have minute precision for MATS")
    return parsed.astimezone(timezone.utc).isoformat()


def _is_diff_curve_param(param_name: str) -> bool:
    norm = re.sub(r"[\s_-]+", "", param_name.casefold())
    return norm in {"differencecurves", "diffcurves", "diff", "plotformat"}


def _is_diff_curve_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if not value:
        return False
    norm = str(value).strip().casefold()
    return norm not in {"none", "false", "0", "no"}


def _is_valid_utc_hour_param(param_name: str) -> bool:
    """Returns True when the parameter selects the Valid UTC Hour (valid-time) filter.

    Recognised spellings: 'Valid UTC Hour', 'valid_utc_hour', 'valid hour', 'utc hour'.
    When True, only the *value* (e.g. 'all', '0z', '12z') is used as the filename token
    instead of the usual 'key+value' concatenation.
    """
    norm = re.sub(r"[\s_-]+", "", param_name.casefold())
    return norm in {"validutchour", "validhour", "utchour"}


def _safe_name(value: Any, context: str) -> str:
    value = _text(value, context)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ConfigError(f"{context} may contain only letters, digits, underscores and hyphens")
    return value


def _settings(raw: Any) -> Settings:
    raw = _mapping(raw, "automation")
    allowed = {"inter-plot waiting seconds", "cooldown_seconds", "max_attempts", "base_url", "control waiting seconds"}
    if set(raw) - allowed:
        raise ConfigError(f"Unknown automation options: {sorted(set(raw) - allowed)}")
    base_url = _text(raw.get("base_url", "https://gsl.noaa.gov/mats"), "base_url").rstrip("/")
    if base_url not in {"https://gsl.noaa.gov/mats", "https://gsl.noaa.gov/int-mats"}:
        raise ConfigError("base_url must be https://gsl.noaa.gov/mats or https://gsl.noaa.gov/int-mats")
    waiting_seconds = raw.get("inter-plot waiting seconds", 1)
    control_waiting_seconds = raw.get("control waiting seconds", 0.2)
    cooldown = raw.get("cooldown_seconds", 180)
    attempts = raw.get("max_attempts", 3)
    for value, minimum, name in ((waiting_seconds, 1, "inter-plot waiting seconds"),
                                 (cooldown, 180, "cooldown_seconds")):
        if type(value) not in (int, float) or not minimum <= value <= 86400:
            raise ConfigError(f"{name} must be between {minimum} and 86400")
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise ConfigError("max_attempts must be between 1 and 3")
    if type(control_waiting_seconds) not in (int, float) or not 0.2 <= control_waiting_seconds <= 10:
        raise ConfigError("control waiting seconds must be between 0.2 and 10")
    return Settings(waiting_seconds, cooldown, attempts, base_url, control_waiting_seconds)


def load_plan(path: Path) -> Plan:
    if yaml is None:
        raise ConfigError("PyYAML is required to read YAML recipes; activate the virtual environment or install PyYAML")
    try:
        with path.open(encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Cannot read recipe: {error}") from error
    return build_plan(raw, recipe_path=path)


def _format_slide_caption(caption: str, job: PlotJob) -> str:
    m = re.match(r"^threshold_*(.+)$", caption, re.IGNORECASE)
    if not m:
        return caption
    val = m.group(1).lstrip("_")
    var = (job.variable or "").lower()
    var_tag = (job.variable_tag or "").lower()
    app = (job.application or "").lower()

    if "reflectivity" in var or "reflectivity" in var_tag or "radar" in app:
        return f">{val} dBZ"
    if "ceiling" in var or "ceiling" in var_tag:
        return f"<{val} ft"
    if "visibility" in var or "visibility" in var_tag or "vis" in var_tag:
        unit = "mile" if val in ("1", "1/2", "0.5") else "miles"
        return f"<{val} {unit}"
    return caption


def _slides(raw: Any, jobs: tuple[PlotJob, ...]) -> tuple[SlideSpec, ...]:
    if not isinstance(raw, list):
        raise ConfigError("slides must be a list")
    slides = []
    for index, item in enumerate(raw, 1):
        context = f"slide {index}"
        item = _mapping(item, context)
        group = _text(item.get("plot group"), f"{context}.plot group")
        norm_group = lambda g: g.casefold().replace("roab", "raob")
        candidates = [job for job in jobs if norm_group(job.name) == norm_group(group)]
        if not candidates:
            raise ConfigError(f"{context}: unknown plot group {group!r}")
        count = item.get("number of rows")
        if type(count) is not int or count < 1:
            raise ConfigError(f"{context}.number of rows must be a positive integer")
        row_keys = {f"row{number}" for number in range(1, count + 1)}
        unknown = set(item) - {"plot group", "number of rows", "title"} - row_keys
        if unknown:
            raise ConfigError(f"{context}: unknown options {sorted(unknown)}; use number of rows")
        title = _text(item.get("title", candidates[0].name), f"{context}.title")
        rows = []
        slide_parts = []
        slide_jobs = []
        for number in range(1, count + 1):
            references = item.get(f"row{number}")
            if not isinstance(references, list) or not references:
                raise ConfigError(f"{context}.row{number} must be a nonempty list")
            resolved = []
            row_parts = []
            row_jobs = []
            for reference in references:
                original = _text(reference, f"{context}.row{number} figure")
                reference = original.casefold()
                exact = []
                exact_diff_stripped = []
                partial = []
                for job in candidates:
                    path = Path(job.relative_path)
                    short = path.stem[len(job.name) + 1:]
                    statistic = re.sub(r"[^A-Za-z0-9_+-]+", "_", job.statistic or "default").strip("_")
                    var_prefix = job.variable_tag + "_"
                    stem_without_var = short[len(var_prefix):] if short.casefold().startswith(var_prefix.casefold()) else short
                    names = {path.name.casefold(), path.stem.casefold(), short.casefold(),
                             stem_without_var.casefold(),
                             path.relative_to(job.name).as_posix().casefold(),
                             job.variable_tag.casefold(), f"{job.variable_tag}_{statistic}".casefold()}
                    if "+" in statistic:
                        for single_stat in statistic.split("+"):
                            single_stat = single_stat.strip()
                            if single_stat:
                                names.add(short.casefold().replace(statistic.casefold(), single_stat))
                                names.add(stem_without_var.casefold().replace(statistic.casefold(), single_stat))
                    diff_free_names = {n.replace("+diff", "") for n in names}
                    if reference in names:
                        exact.append(job)
                    elif reference in diff_free_names:
                        exact_diff_stripped.append(job)
                    elif any(reference in name for name in names | diff_free_names):
                        partial.append(job)
                matches = exact or exact_diff_stripped or partial
                if len(matches) != 1:
                    detail = "ambiguous" if matches else "unknown"
                    choices = ", ".join(job.relative_path for job in matches or candidates)
                    raise ConfigError(f"{context}.row{number}: {detail} figure {reference!r} "
                                      f"in {group!r}; use a unique reference. Figures: {choices}")
                resolved.append(matches[0].relative_path)
                caption = Path(original).stem
                prefix = matches[0].name + "_"
                if caption.casefold().startswith(prefix.casefold()):
                    caption = caption[len(prefix):]
                row_parts.append(caption.split("_"))
                row_jobs.append(matches[0])
            rows.append(tuple(resolved))
            slide_parts.append(row_parts)
            slide_jobs.append(row_jobs)

        all_parts = [p for row in slide_parts for p in row]
        use_second_section = (
            len(all_parts) > 1
            and all(len(p) >= 2 for p in all_parts)
            and len({p[0].casefold() for p in all_parts}) == 1
        )
        captions = tuple(
            tuple(
                _format_slide_caption(p[1] if use_second_section else p[0], job)
                for p, job in zip(parts_row, jobs_row)
            )
            for parts_row, jobs_row in zip(slide_parts, slide_jobs)
        )
        slides.append(SlideSpec(candidates[0].name, title, tuple(rows), captions))
    return tuple(slides)


def build_plan(raw: Any, recipe_path: Path | None = None) -> Plan:
    raw = _mapping(raw, "recipe")
    common = _mapping(raw.get("common"), "common")
    unknown = set(common) - {
        "start", "end",
        "line weight", "marker weight",
        "description font size", "description_font_size",
        "legend font size", "legend_font_size",
    }
    if unknown:
        raise ConfigError(f"Unknown common options: {sorted(unknown)}")
    if "description font size" in common and "description_font_size" in common:
        raise ConfigError("Use only one description font size spelling in common")
    if "legend font size" in common and "legend_font_size" in common:
        raise ConfigError("Use only one legend font size spelling in common")
    start = _date(common["start"], "common.start") if common.get("start") is not None else None
    end = _date(common["end"], "common.end") if common.get("end") is not None else None
    if start is not None and end is not None and start >= end:
        raise ConfigError("common.start must precede common.end")
    line_weight = common.get("line weight", 2)
    marker_weight = common.get("marker weight", 7)
    for name, value in (("line weight", line_weight), ("marker weight", marker_weight)):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ConfigError(f"common.{name} must be a positive finite number in pixels")
    description_font_size = common.get(
        "description font size",
        common.get("description_font_size", DEFAULT_DESCRIPTION_FONT_SIZE),
    )
    legend_font_size = common.get(
        "legend font size",
        common.get("legend_font_size", DEFAULT_LEGEND_FONT_SIZE),
    )
    for name, value in (
        ("description font size", description_font_size),
        ("legend font size", legend_font_size),
    ):
        if type(value) not in (int, float) or not math.isfinite(value) or not 4 <= value <= 72:
            raise ConfigError(f"common.{name} must be a finite number between 4 and 72 pixels")
    notices = []
    experiments = []
    for index, item in enumerate(_choices(raw.get("experiments"), "experiments")):
        item = _mapping(item, f"experiment {index + 1}")
        allowed_exp = {"MATS name", "MATS_name", "name", "color", "overlay", "COMROOT", "short name", "short_name"}
        if set(item) - allowed_exp:
            raise ConfigError(
                f"Unknown experiment options: {sorted(set(item) - allowed_exp)}; "
                "use 'MATS name' for the curve label and optional 'short name' for display"
            )
        name_keys = [k for k in ("MATS name", "MATS_name", "name") if k in item]
        if len(name_keys) > 1:
            raise ConfigError("Use only one MATS name spelling in experiment")
        if "short name" in item and "short_name" in item:
            raise ConfigError("Use only one short name spelling in experiment")
        raw_name = item[name_keys[0]] if name_keys else None
        name = _text(raw_name, "experiment.MATS name")
        if not re.search(r"[A-Za-z0-9]", name):
            raise ConfigError("experiment.MATS name must contain an alphanumeric character for its MATS curve label")
        raw_short = item["short name"] if "short name" in item else item.get("short_name")
        short_name = None
        if raw_short is not None:
            short_name = _text(raw_short, "experiment.short name")
            if not re.search(r"[A-Za-z0-9]", short_name):
                raise ConfigError("experiment.short name must contain an alphanumeric character")
        color = _text(item.get("color", LEAD_COLORS[index % len(LEAD_COLORS)]), "experiment.color")
        overlay = _boolean(item.get("overlay", True), "experiment.overlay")
        comroot = _text(item["COMROOT"], "experiment.COMROOT") if "COMROOT" in item and item["COMROOT"] is not None else None
        experiments.append(Experiment(name, color, overlay, comroot, short_name))
    if len({item.name for item in experiments}) != len(experiments):
        notices.append("Repeated experiment names: these entries query the same MATS model, not different experiments.")
    jobs = []
    paths = set()
    for item in _choices(raw.get("plots"), "plots"):
        item = _mapping(item, "plot")
        name = _safe_name(item.get("name"), "plot.name")
        reserved = {
            "name", "application", "plot_type", "truth", "region", "matched_samples",
            "variable", "statistic", "forecast lead time", "forecast_lead_time",
            "forecast lead colors", "exclude experiment", "exclude experiments",
            "exclude_experiment", "exclude_experiments",
            "include experiment", "include experiments",
            "include_experiment", "include_experiments",
        }
        exclude_keys = [k for k in ("exclude experiment", "exclude experiments",
                                    "exclude_experiment", "exclude_experiments") if k in item]
        include_keys = [k for k in ("include experiment", "include experiments",
                                    "include_experiment", "include_experiments") if k in item]
        if len(exclude_keys) > 1:
            raise ConfigError(f"Use only one exclude experiment spelling in plot {name!r}")
        if len(include_keys) > 1:
            raise ConfigError(f"Use only one include experiment spelling in plot {name!r}")
        if exclude_keys and include_keys:
            raise ConfigError(f"Use either include experiment or exclude experiment in plot {name!r}, not both")

        plot_experiments = list(experiments)
        normalize = lambda s: re.sub(r"[\s_-]+", "", s.casefold())

        def _match_experiment(norm_val: str) -> list[Experiment]:
            matched = []
            for exp in experiments:
                candidates = {normalize(exp.name), normalize(exp.label)}
                if exp.short_name:
                    candidates.add(normalize(exp.short_name))
                if norm_val in candidates:
                    matched.append(exp)
            return matched

        if exclude_keys:
            raw_excluded = item[exclude_keys[0]]
            excluded_names = [_text(val, f"plot {name!r} {exclude_keys[0]}")
                              for val in _choices(raw_excluded, exclude_keys[0])]
            to_exclude = set()
            for val in excluded_names:
                matches = _match_experiment(normalize(val))
                if not matches:
                    continue
                if len(matches) > 1:
                    matched_names = [exp.name for exp in matches]
                    raise ConfigError(f"Plot {name!r}: exclude experiment {val!r} is ambiguous: {matched_names}")
                to_exclude.add(matches[0])
            plot_experiments = [exp for exp in experiments if exp not in to_exclude]
            if not plot_experiments:
                raise ConfigError(f"Plot {name!r}: all experiments were excluded; at least one experiment must remain")
        elif include_keys:
            raw_included = item[include_keys[0]]
            included_names = [_text(val, f"plot {name!r} {include_keys[0]}")
                              for val in _choices(raw_included, include_keys[0])]
            to_include = set()
            for val in included_names:
                matches = _match_experiment(normalize(val))
                if not matches:
                    available = [exp.name for exp in experiments]
                    raise ConfigError(f"Plot {name!r}: include experiment {val!r} does not match any configured experiment: {available}")
                if len(matches) > 1:
                    matched_names = [exp.name for exp in matches]
                    raise ConfigError(f"Plot {name!r}: include experiment {val!r} is ambiguous: {matched_names}")
                to_include.add(matches[0])
            plot_experiments = [exp for exp in experiments if exp in to_include]
            if not plot_experiments:
                raise ConfigError(f"Plot {name!r}: no configured experiments match include experiment")

        plot_groups = []
        plot_overlaid = tuple(exp for exp in plot_experiments if exp.overlay)
        if plot_overlaid:
            plot_groups.append(("", plot_overlaid))
        for index, experiment in enumerate(plot_experiments, 1):
            if not experiment.overlay:
                slug = re.sub(r"[^A-Za-z0-9_-]+", "_", experiment.label).strip("_") or "experiment"
                plot_groups.append((f"{slug}-{index}", (experiment,)))

        extra_keys = sorted(set(item) - reserved)
        extra_values = []
        for key in extra_keys:
            _text(key, "parameter name")
            values = _choices(item[key], key)
            if any(type(value) not in (str, int, float, bool) or
                   isinstance(value, str) and not value.strip() for value in values):
                raise ConfigError(f"{key} must contain scalar parameter values")
            extra_values.append(values)
        application_name = _text(item.get("application"), "application")
        application = APPLICATION_ALIASES.get(application_name.casefold(), application_name)
        plot_type = _text(item.get("plot_type"), "plot_type").casefold()
        if application == "upperair" and name == "ua_profile" and plot_type == "dieoff":
            notices.append("ua_profile with plot_type: dieoff is interpreted as Profile. Use plot_type: profile explicitly.")
            plot_type = "profile"
        truth = _text(item["truth"], "truth") if "truth" in item else None
        region = _text(item["region"], "region") if "region" in item else None
        matched = _boolean(item["matched_samples"], "matched_samples") if "matched_samples" in item else None
        variables = []
        for variable in _choices(item["variable"], "variable") if "variable" in item else [None]:
            if variable is None:
                variables.append((None, "default"))
                continue
            key = _text(variable, "variable").casefold()
            if application == "upperair" and key in VARIABLES:
                variables.append(VARIABLES[key])
            else:
                tag = re.sub(r"[^A-Za-z0-9_-]+", "_", key).strip("_")
                if not tag:
                    raise ConfigError("variable must contain an alphanumeric character")
                variables.append((_text(variable, "variable"), tag))
        stats = []
        for statistic in _choices(item["statistic"], "statistic") if "statistic" in item else [None]:
            if statistic is None:
                stats.append(None)
                continue
            key = re.sub(r"\s+", "", _text(statistic, "statistic").casefold())
            stats.append(key)
        if "forecast lead time" in item and "forecast_lead_time" in item:
            raise ConfigError("Use only one forecast lead time spelling")
        raw_leads = item.get("forecast_lead_time", item.get("forecast lead time"))
        leads = [_lead_choice(value) for value in _choices(raw_leads, "forecast lead time")] if raw_leads is not None else [None]
        colors = _forecast_colors(item["forecast lead colors"]) if "forecast lead colors" in item else {}
        width, height = (1200, 1200) if plot_type == "profile" else (1600, 900)
        grouped_leads = {value for group in leads if isinstance(group, tuple) for value in group}
        if "forecast lead colors" in item and grouped_leads - colors.keys():
            raise ConfigError(f"forecast lead colors missing grouped leads: {sorted(grouped_leads - colors.keys())}")
        for (variable, tag), statistic, lead, extras in product(variables, stats, leads, product(*extra_values)):
            selected_groups = [("", (plot_experiments[0],))] if isinstance(lead, tuple) else plot_groups
            for group, curves in selected_groups:
                statistic_tag = re.sub(r"[^A-Za-z0-9_+-]+", "_", statistic or "default").strip("_")
                parameter_tags = []
                for parameter, value in zip(extra_keys, extras):
                    if _is_diff_curve_param(parameter):
                        if _is_diff_curve_enabled(value) and "+diff" not in statistic_tag:
                            statistic_tag = f"{statistic_tag}+diff"
                        continue
                    if _is_valid_utc_hour_param(parameter):
                        # Use only the value (e.g. 'all', '0z', '12z') as the filename token,
                        # omitting the parameter key so slide references stay short and readable.
                        value_tag = re.sub(r"[^a-z0-9.+-]+", "_", str(value).strip().casefold()).strip("_")
                        if not value_tag:
                            raise ConfigError(f"Cannot create a readable filename for {parameter!r}={value!r}")
                        parameter_tags.append(value_tag)
                        continue
                    key_tag = re.sub(r"[^a-z0-9]+", "", parameter.casefold())
                    value_tag = re.sub(r"[^a-z0-9.+-]+", "_", str(value).strip().casefold()).strip("_")
                    if not key_tag or not value_tag:
                        raise ConfigError(f"Cannot create a readable filename for parameter {parameter!r}={value!r}")
                    parameter_tags.append(key_tag + value_tag)
                filename = "_".join(part for part in (name, tag, statistic_tag, _lead_tag(lead), *parameter_tags) if part)
                relative = Path(name) / group / f"{filename}.png"
                if relative.as_posix() in paths:
                    raise ConfigError(f"Duplicate output path: {relative}")
                paths.add(relative.as_posix())
                jobs.append(PlotJob(name, plot_type, truth, region, matched, variable, tag,
                                    statistic, lead, start, end, curves, relative.as_posix(),
                                    width=width, height=height,
                                    forecast_lead_colors=tuple(sorted((value, colors[value]) for value in lead))
                                    if isinstance(lead, tuple) and colors else (), application=application,
                                    parameters=tuple(zip(extra_keys, extras)),
                                    line_weight=line_weight, marker_weight=marker_weight,
                                    default_date_experiment=plot_experiments[0].name
                                    if start is None or end is None else None,
                                    description_font_size=description_font_size,
                                    legend_font_size=legend_font_size))
    planned_jobs = tuple(jobs)
    slide_error = None
    try:
        slides = _slides(raw.get("slides", []), planned_jobs)
    except ConfigError as error:
        slides = ()
        slide_error = str(error)

    pygraf_raw = raw.get("pygraf graphics slides")
    pygraf_cfg = None
    if pygraf_raw is not None:
        pygraf_raw = _mapping(pygraf_raw, "pygraf graphics slides")
        unknown = set(pygraf_raw) - {"enable", "recipe"}
        if unknown:
            raise ConfigError(f"Unknown pygraf graphics slides options: {sorted(unknown)}")
        enable = _boolean(pygraf_raw.get("enable", False), "pygraf graphics slides.enable")
        recipe_val = pygraf_raw.get("recipe")
        recipe_str = _text(recipe_val, "pygraf graphics slides.recipe") if recipe_val is not None else None
        pygraf_cfg = PygrafGraphicsConfig(enable=enable, recipe=recipe_str)

    recipe_title = _text(raw["title"], "title") if "title" in raw and raw["title"] is not None else None

    return Plan(
        planned_jobs,
        tuple(notices),
        _settings(raw.get("automation", {})),
        slides,
        slide_error,
        pygraf_graphics=pygraf_cfg,
        experiments=tuple(experiments),
        recipe_path=recipe_path,
        title=recipe_title,
    )


def _atomic_copy_file(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    if source.resolve() == destination.resolve():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def copy_used_recipes(plan: Plan, output_dir: Path, recipe_path: Path | None = None) -> tuple[Path, ...]:
    """Copy the active recipe (and enabled pygraf graphics recipe) into output_dir for reference."""
    main_recipe = recipe_path if recipe_path is not None else plan.recipe_path
    if main_recipe is None or not main_recipe.is_file():
        return ()

    copied: list[Path] = []
    main_dst = output_dir / main_recipe.name
    _atomic_copy_file(main_recipe, main_dst)
    copied.append(main_dst)

    if plan.pygraf_graphics and plan.pygraf_graphics.enable and plan.pygraf_graphics.recipe:
        from automats.graphics import resolve_graphics_recipe_path

        gfx_recipe = resolve_graphics_recipe_path(plan.pygraf_graphics.recipe, main_recipe)
        if gfx_recipe is not None and gfx_recipe.is_file():
            gfx_dst = output_dir / gfx_recipe.name
            if gfx_dst.resolve() != main_dst.resolve():
                _atomic_copy_file(gfx_recipe, gfx_dst)
                copied.append(gfx_dst)

    return tuple(copied)