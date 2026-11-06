<p align="center">
  <img src="assets/logo.png" alt="AutoMATS" width="260">
</p>

# AutoMATS
Automatic MATS plot generation    
_(Only tested on MacOS)_

## Run

Python 3.10+ and installed Google Chrome are required.
Chrome runs in the background by default (no visible window).
Use `--show-browser` to keep it visible for debugging

### One-Time Setup

```sh
cd AutoMATS
./setup.sh
```
The setup script creates a private `.venv` and installs AutoMATS, PyYAML,
Playwright, and PowerPoint dependencies automatically. 
It does not change your system or Conda packages.
**Rerun setup if you update the source code

### Run

```sh
./run.sh recipes/baseline.yaml
```

## Recipes

Every recipe requires `common` for styling and optional dates:

```yaml
common:
  start: "2024-05-06T10:00:00Z"
  end: "2024-05-13T00:00:00Z"
  line weight: 2
  marker weight: 7
```
Omit `start` and `end` to use the first experiment's default MATS date range.


```yaml
statistic: [rmse, bias]
```
This makes separate RMSE and bias figures

```yaml
statistic: [rmse+bias]
```

This makes one combined RMSE+bias figure.

Forecast leads also support separate or grouped figures:

```yaml
forecast lead time: [0, 1, 2, 3, 6, 9, 12]
```
```yaml
forecast lead time: [0+1+3+6+9]
```
Separate and grouped entries can coexist, for example
`forecast lead time: [0, 1+3+6]`. 

### Forecast Lead Colors

Optionally define `forecast lead colors:` inside a plot entry. Keys are integer
forecast hours, not positions in the lead list. CSS color names or quoted hex
values (`#RGB` or `#RRGGBB`) are accepted:

```yaml
forecast lead time: [0+1+3+6+9]
forecast lead colors:
  0: red
  1: royalblue
  3: seagreen
  6: darkorange
  9: magenta
```

The same color applies to RMSE, bias, error bars, and the solid legend swatch
for that lead. 

## Slides

Add `slides` to a recipe to build a 16:9 PowerPoint deck at
`<output-dir>/slides.pptx` after plotting. Each entry creates one slide:

```yaml
slides:
- plot group: ua_dieoff
  title: Upper Air Verification
  number of rows: 2
  row1: [T, RHobT]
  row2: [Wind]
```

Rows share the available height equally. Figures share each row's width,
retain their proportions, and are centered within their space. A single
figure is centered in its row. `title` defaults to the plot group name.

Figure references match within the named plot group: exact names first,
then unique partial matches. Missing or ambiguous references stop slide
generation only; plotting continues. 

AutoMATS skips figures that already exist in the output directory by default (only querying MATS for missing figures). To force regeneration of all figures, pass `--update`:

```sh
./run.sh recipes/baseline.yaml --update
```

Rebuild slides from existing PNGs without opening Chrome or contacting MATS:

```sh
./run.sh recipes/baseline.yaml --slides-only
```

This still requires a valid recipe and every referenced PNG in the output
directory. 

## Server Protection

- One Chrome session and one plot query at a time; no parallel plotting.
- A 3-second wait after a completed plot by default;
  `automation.inter-plot waiting seconds` accepts customizations. 
- Parameter changes and Add Curve wait for controls to remain stable for
  0.3 seconds by default, including pending metadata updates.
- A user-wide process lock prevents concurrent AutoMATS runs on the same machine.
- Ctrl+C normally closes Chrome and releases the lock. Forced termination or
  interruption during cleanup can leave a stale lock.

Configure the wait in your recipe's existing `automation` section:

```yaml
automation:
  inter-plot waiting seconds: 3
  control waiting seconds: 0.3
```

## Test

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```
