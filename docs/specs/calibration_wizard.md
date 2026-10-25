# Calibration wizard: spec (v1, 2026-10-25)

Goal: anyone with an EDU09 and a multimeter can calibrate their own unit. The user describes the reference
voltages they can produce. The wizard plans the steps, tells the user what to connect and asks for the multimeter
reading, then measures. At the end it fits and saves a per-unit calibration (with a backup) and verifies it.
Front ends: CLI `edu09-calibrate`, a GUI `QWizard` and MCP tools (only with `--allow-calibration`). Everything
can be tested end to end on the simulator.

Base: `main` @ ec50d15. Related docs: protocol.md 5.3 and 11.2-11.3, `docs/data/edu09_gain_sweep_unit1.json`,
and architecture.md 3.3, 4.3, 5 and 6.

---

## 0. Physics the design relies on (unit 1, protocol.md 11.3 + sweep data)

| Effect | Value | Consequence |
|---|---|---|
| Offset/ground | open-input procedure (`measure_calibration`): base_ofs, 3-point GndFit (k~1.03), range_corr | Stays as is. It is the wizard's **zero step** |
| Ground model accuracy | within ~1 count at positions -4..+4 | 1 count at 5 div is 0.6 %, the same size as the gain error. So the zero step also **measures the ground at every (range, DAC byte) the plan uses** (the "ground map") |
| Positive gain | reads 0.4-2.6 % high (32.1-32.9 counts/div), no dependence on deflection up to 6 div | One point per range is enough. Fit through ground |
| Negative gain | 0.5-1 % higher than the positive gain (counts/div) | Separate `range_gain_neg` |
| Negative expansion | 0.2 and 0.5 V/div: >4 div below ground it expands (0.5 V/div secant 33.0 to 4 div, 33.55 at 5, 34.73 at 6). 0.1 and 1 V/div: none seen | Per-range piecewise correction, a function of **deflection** (not ADC code or position) |
| Usable positions | gain points with ground and level within +-3.5 div | Planner constraint |
| Settling / mains | 20 ms DAC settle (worker does it); average whole mains periods: 5 ms/div, first 625 samples per frame | Same capture recipe as `gain_from_reference` |
| Input limit | max ~30 V | References capped at +-15 V by default (`max_abs_v`, hard cap 30) |

---

## 1. Calibration data model v2 (`units.py`, `storage.py`, `protocol/tables.py`)

### 1.1 Fields (`Calibration`, frozen)
Existing fields are unchanged. New or changed:

| Field | Type (normalised) | Default | Meaning |
|---|---|---|---|
| `range_gain` | `dict[float, float]` | `{}` | **Positive-side** gain `g+ = V_true/V_read` (same semantics as v1; v1 applied it to both sides) |
| `range_gain_neg` | `dict[float, float]` | `{}` | Negative-side linear gain `g-`. Missing range: `range_gain_for(vdiv)` (so v1 behaviour is preserved) |
| `neg_nonlinearity` | `dict[float, tuple[tuple[float, float], ...]]` | `{}` (`None` accepted on input, becomes `{}`) | Per-range knots `(r, e)` of the negative excess, see 1.2. **Now applied** |
| `meta` | `dict[str, Any] \| None` | `None` | Provenance (created, tool, mode, sources, residuals). `compare=False`, `hash=False`, **not part of `calibration_id`**, at most 8 KiB as JSON |

`source` stays `"measured"` for wizard results (no new literal). Provenance goes in `meta` (`{"tool":"edu09-calibrate","created":ISO8601,"mode":...,"sources":[...],"points":n,"ranges":{vdiv:{"pos":..,"neg":..,"nl":..}}}`).

### 1.2 Negative nonlinearity parametrisation (decision)

Definitions, for one range on the negative side:
- `c >= 0`: counts below ground.
- `r = c * g- / counts_per_div`: the **linear-corrected read deflection** in divisions.
- `d`: the true deflection in divisions.
- Model: `d = r - E(r)`. `E` is piecewise linear through the knots `(r_k, e_k)`. `E(r) = 0` for `r <= r_0`. Beyond the last knot, `E` is extrapolated with the slope of the last segment.

The inverse is also piecewise linear, with knots `(r_k - e_k, r_k)`. Both directions are a single `np.interp` plus a linear tail.

Unit 1 at 0.5 V/div (`g- = 32/33.0`) gives knots `[(4.0, 0.0), (5.083, 0.083), (6.315, 0.315)]`.

Options considered:

| Option | Pros | Cons |
|---|---|---|
| (a) Table of secant counts/div vs deflection (the form we measured) | Matches the measurements | Not a direct mapping. The inverse needs root finding. Hard to validate for monotonicity |
| (b) Quadratic beyond a knee: `C = cpd*d + a*(d-knee)^2` | 2 parameters. Fits unit 1 well (a~2.65 counts/div^2). Analytic inverse | Assumes a shape. The knee has to be fitted. A cubic or saturating unit would be mis-modelled |
| **(c) PWL excess knots in the read domain (chosen)** | Any number of points (1 measured point gives 2 knots). Exact, cheap inverse in both directions. Monotonicity is a simple slope check. Holds measured data verbatim. A later parametric fit can be *sampled into* knots without a format change | Kinks at the knots (well below 1 count) |

Validation (`CalibrationError`, constants in `tables.py`):
- Keys are valid `VoltsPerDiv`.
- 2 <= knots <= `NL_MAX_KNOTS` (8).
- `e_0 == 0`.
- `1.0 <= r_0`, and `r` strictly increasing with `r <= NL_MAX_READ_DIV` (10).
- `|e| <= NL_MAX_EXCESS_DIV` (2.0).
- Every segment slope `de/dr` (including the extrapolated tail) is within `[-NL_MAX_SLOPE, NL_MAX_SLOPE]` (0.5). Then `dd/dr >= 0.5`, so the mapping is strictly monotonic and well conditioned.

Gains keep the existing `(RANGE_GAIN_MIN, RANGE_GAIN_MAX) = (0.8, 1.25)`. The `range_gain_neg/range_gain` ratio is not limited at load; the fitter warns about it (section 3.4).

### 1.3 Where it is applied (single place)
New pure functions in `units.py`. Both are vectorised and float64.
```python
def deflection_to_volts(dev_counts: ArrayLike, vdiv: VoltsPerDiv | float, cal: Calibration) -> FloatArray
    # dev_counts = counts *above* ground in the voltage sense (+ = more positive volts)
    # >= 0: v = dev * vdiv * g+ / cpd ;  < 0: r = -dev * g- / cpd ; v = -(r - E(r)) * vdiv
def volts_to_deflection(volts: ArrayLike, vdiv: VoltsPerDiv | float, cal: Calibration) -> FloatArray  # exact inverse
```
- `counts_to_volts = deflection_to_volts((raw - gnd) * s, ...)`, where `s = -1 if adc_inverted else 1`. `volts_to_counts = gnd + s * volts_to_deflection(...)`. `volts_to_raw` and `trigger_byte` inherit this through `volts_to_counts` (trigger levels below ground are therefore NL-corrected).
- `_ypos_steps` (position to DAC) keeps the **positive** effective counts/div (unchanged). `level_range_v` is unchanged. GND coupling still gives zeros.
- Invariants (tests):
  - strictly increasing in raw, continuous at ground;
  - `volts_to_counts(counts_to_volts(x)) == x` to 1e-9 over raw 0..255 at every range and position;
  - with empty v2 fields the output is **bitwise identical** to v1.

New helpers on `Calibration`:
- `range_gain_neg_for(vdiv)`
- `nonlinearity_for(vdiv) -> tuple[Knot, ...]`
- `with_gains(*, pos=None, neg=None, nonlinearity=None, meta=...) -> Calibration`: per-range merge; a value of `None` removes the key.
- `without_gain_correction() -> Calibration`: all three gain maps empty and `meta=None`. Every measurement uses it.

### 1.4 Serialisation, file version, `calibration_id`
- `to_dict()` emits `range_gain_neg`, `neg_nonlinearity` (as `{"0.5": [[4.0, 0.0], ...]}`) and `meta` **only when non-empty**. `calibration_id` hashes `to_dict()` **without `meta`**. Result: every v1 calibration keeps the same dict and the same id (pinned test using the user's current file content).
- `storage.save_calibration` writes `"version": 2` if any v2-only key is present, otherwise `1`. This keeps offset-only files readable by older builds. `load_calibration` accepts versions 1 and 2. The parser ignores the version number (it is the union schema).
- `from_dict` still rejects unknown keys. A legacy opaque `neg_nonlinearity` that does not match 1.2 is a `CalibrationError`. No producer ever wrote one. On hardware, `Scope.open` already falls back to `typical()` with a warning.
- `measure_calibration` (offset-only) carries over `range_gain`, `range_gain_neg` and `neg_nonlinearity`. It sets `meta=None`, implemented as `dataclasses.replace(cur, base_ofs=..., range_corr=..., gnd_fit=..., source="measured", meta=None)`. `gain_from_reference` measures with `without_gain_correction()`.

### 1.5 Migration of the user's `~/.config/edu09/calibration.json`
It is v1 with no gains, so it loads unchanged (all gains 1.0, same `calibration_id`). The wizard's zero step re-measures the offsets (or skipping it keeps them). The fit then adds the gains and NL. On save, `Scope.set_calibration(save=True)` backs the file up as `calibration.<stamp>.bak.json` and writes the v2 file. Nothing is migrated automatically on load.

---

## 2. Planner (pure; `src/edu09/calwizard/planner.py` + `model.py`)

### 2.1 Inputs
```python
@dataclass(frozen=True)
class FixedSource:      label: str; volts: float; tolerance_pct: float = 5.0; reversible: bool = False
@dataclass(frozen=True)
class AdjustableSource: label: str; min_v: float; max_v: float; reversible: bool = False
Source = FixedSource | AdjustableSource          # volts / min_v / max_v are magnitudes >= 0

@dataclass(frozen=True)
class TrimmerConfig:      # purpose of the board trimmer not yet known -> everything configurable
    enabled: bool = False
    placement: Literal["first", "after_zero"] = "first"   # "first": zero+gains re-measure whatever it changed
    reference: Literal["source", "open"] = "source"      # open: target 0 V (input disconnected)
    source_label: str | None = None                     # default: source giving deflection nearest 4 div
    vdiv: float = 1.0
    position_div: float | None = None                   # None: centred (-d/2)
    instruction: str = DEFAULT_TRIMMER_TEXT

@dataclass(frozen=True)
class PlanOptions:
    mode: Literal["quick", "standard", "full"] = "standard"
    max_abs_v: float = 15.0                 # 0 < x <= 30 (INPUT_ABS_MAX_V)
    ranges: tuple[float, ...] = all six V/div
    zero: bool = True
    verify: bool = True
    trimmer: TrimmerConfig = TrimmerConfig()
    uncalibrated: Literal["keep", "typical", "unity"] = "keep"   # passed through to the fit

def plan_calibration(sources: Sequence[Source], options: PlanOptions = PlanOptions()) -> Plan
def parse_source(text: str) -> Source        # CLI grammar, 4.1
```
Source validation (`ValueError`):
- at most 8 sources;
- labels are 1-40 printable characters and unique;
- values are finite;
- `0 <= min_v < max_v`;
- `volts > 0`;
- `0 <= tolerance_pct <= 25`.

Any value above `max_abs_v` is clipped for planning and reported.

### 2.2 Targets (deflection windows in true divisions; constants in `model.py`)

| Purpose | Target | Good window | Accepted (fallback) | Modes |
|---|---|---|---|---|
| `gain_pos` | 5.0 | 4.0-6.0 | 2.0-7.0 | all |
| `gain_pos2` (linearity check) | 2.5 | 2.0-3.0 | - | full |
| `gain_neg` | 3.0 | 2.0-4.0 | 1.5-4.0 | all, needs reversible |
| `nonlin5` | 5.0 | 4.6-5.4 | 4.3-5.6 | standard, full |
| `nonlin6` | 6.0 | 5.6-6.4 | 5.6-6.6 | standard, full |
| `nonlin65` | 6.5 | 6.3-6.6 | - | full |

Positions are quantised to 0.25 div:
- Positive: `p = -d/2`, clamped so that `p >= -3.5` and `p + d <= 3.5`.
- Negative: `p = d/2`, clamped so that `p <= 3.5` and `p - 1.06*d >= -3.75` (6 % expansion headroom). So `d <= 6.6`.

### 2.3 Algorithm (deterministic)
1. **Candidates** are signed nominal voltages per source:
   - Fixed: `+V`, plus `-V` if reversible.
   - Adjustable: for every target `t` and range `v`, `V = t.target * v`, rounded "nicely" (0.01 V below 1 V, 0.05 V up to 10 V, 0.1 V above), kept if `min_v <= |V| <= min(max_v, max_abs_v)`; negated as well if reversible.
2. **Coverage of a candidate**: the set of (range, purpose) whose window contains `|V|/vdiv`. Fixed sources also need the whole tolerance band inside the *accepted* window; a band that crosses the good window counts as fallback. Quality is `good` or `fallback`.
3. **Greedy cover**: repeatedly pick the candidate with the largest quality gain. Missing to fallback counts +1, fallback to good counts +1, and any good counts +2 for a missing target. Ties go to: a fixed source first (no knob to set), then a source/polarity already in use, then smaller `|V|`, then source order. Stop when no gain remains.
4. **Assignment**: each (range, purpose) goes to its best candidate (quality, then `|d - target|`). Candidates left without points are dropped. A setup's `points` are ordered by vdiv descending.
5. **Setups and order**:
   - `trimmer` (if enabled, placement `first`), then `zero`, then (`after_zero` trimmer).
   - Then `reference` setups grouped by source in the user's order. Within a source: positive ascending `|V|`, then one lead swap, then negative descending `|V|`.
   - Then `verify`: same source/polarity as the last reference setup, so no reconnection. Up to 3 ranges with nominal `|d|` in 1.5-6.5, at positions shifted by 1 div from the fit positions, so the position-to-DAC path is exercised.
   - The zero step's `points` = every distinct (vdiv, position) used by reference and verify setups, plus the trimmer's point when the trimmer is placed `after_zero` (the ground map).
6. **Each reference setup gets `accept_v`**: the signed reading interval for which all its points stay inside their *accepted* windows and position limits. Adjustable setups must allow at least +-8 % around the target, otherwise the planner drops the colliding point to its own setup.
7. **Coverage report**, per range:
   - `pos`, `neg`: `good|fallback|missing|not_reversible`.
   - `nonlin`: `good|partial|missing|not_in_mode`.
   - `needs`: human hints, e.g. `"positive gain: apply +0.40..+0.60 V"`, `"5 V/div nonlinearity needs 23-28 V (above the 15 V limit)"`, `"no reversible source: negative side uses the positive gain"`, and the divider hint `"or divide a larger source with two resistors; only the multimeter reading at the probe tip matters"`.
   - Plan-level: `reconnections` (number of disconnects, source changes and lead swaps), `user_actions` (number of readings) and `estimated_s` (30 s per user action + 0.8 s per point + 15 s zero + 0.5 s per ground-map point).
   - `warnings`, always including when any source is reversible: **"Only reverse floating sources (batteries, isolated bench supplies). Never reverse a source that shares ground with this PC (e.g. USB 5 V from the same computer): the probe ground clip is the PC ground."**

```python
@dataclass(frozen=True)
class MeasurePoint: vdiv: float; position_div: float; expected_div: float; purpose: str
@dataclass(frozen=True)
class Setup:
    id: str                       # "S01".. stable
    kind: Literal["trimmer", "zero", "reference", "verify"]
    source_label: str | None; volts: float | None   # signed nominal (adjustable: value to set)
    adjustable: bool; instruction: str               # one paragraph, plain text
    points: tuple[MeasurePoint, ...]; accept_v: tuple[float, float] | None
@dataclass(frozen=True)
class RangeCoverage: vdiv: float; pos: str; neg: str; nonlin: str; needs: tuple[str, ...]
@dataclass(frozen=True)
class Plan:
    setups: tuple[Setup, ...]; coverage: tuple[RangeCoverage, ...]; sources: tuple[Source, ...]
    options: PlanOptions; reconnections: int; user_actions: int; estimated_s: float; warnings: tuple[str, ...]
    def to_dict(self) -> dict[str, Any]; @classmethod def from_dict(cls, d) -> Plan
```
Example: an adjustable 0-15 V reversible supply in `standard` mode gives about 5 positive and 11 negative setups (0.3/0.5/0.6/1/1.2/2.5/3/5/6/10/12 V), and 5 V/div nonlinearity is reported missing (it needs over 15 V). Sources "USB 5 V (not reversible)" + "AA 1.5 V reversible" cover 1 V/div positive (good), 2 V/div positive (fallback) and 0.5 V/div positive/negative. 0.1, 0.2 and 5 V/div are reported missing, with the voltages they need.

Source profile persistence:
- `model.load_sources()` / `model.save_sources(sources, options)` use `storage.default_config_dir()/calibration_sources.json` (`{"version":1,"sources":[...],"options":{...}}`).
- The CLI and GUI share it, so users don't retype their sources. The file contains no secrets.

---

## 3. Engine / session (`src/edu09/calwizard/session.py`, pure state machine over `Scope`)

### 3.1 States
```
PLANNED --start()--> IN_SETUP(i) --(setup ok|warning|skipped)--> IN_SETUP(i+1) ... --> READY_TO_FIT
READY_TO_FIT --fit()--> FITTED (preview; fit() idempotent) --save(confirm=True)--> SAVED
SAVED --(verify setup in IN_SETUP)--> DONE        ; SAVED/DONE --revert()--> DONE (previous restored)
any non-terminal --abort()--> ABORTED             ; unrecoverable error --> FAILED
```
- Per-setup phases: `AWAIT_INPUT`, `LIVE` (trimmer only), `DONE`, `FAILED` (retryable), `SKIPPED`.
- No Back button: measurements can't be undone. `retry()` re-runs the current setup and discards its points.

### 3.2 API (frozen; Task B writes this file's signatures first)
```python
class CalibrationSession:
    def __init__(self, scope: Scope, plan: Plan, *, path: str | os.PathLike[str] | None = None,
                 sim: SimReference | None = None, clock: Callable[[], float] = time.monotonic) -> None
    state: SessionState; plan: Plan; current: SetupView | None; results: tuple[SetupResult, ...]
    def start(self) -> SetupView                         # registers as the scope's active session
    def view(self) -> SetupView                          # cheap snapshot, any thread
    def confirm_zero(self, *, input_disconnected: bool) -> SetupResult    # zero setup
    def submit_reading(self, volts: float) -> SetupResult # reference / verify / trimmer(reference=source)
    def trimmer_sample(self) -> TrimmerReading           # trimmer LIVE phase, ~0.25 s each
    def trimmer_done(self) -> SetupView
    def retry(self) -> SetupView; def skip(self) -> SetupView
    def fit(self) -> FitResult                           # READY_TO_FIT/FITTED
    def save(self, *, confirm: bool, apply_only: bool = False) -> SaveResult  # backup path, calibration_id
    def revert(self) -> SaveResult                       # re-save previous calibration (new file backed up)
    def abort(self) -> bool                              # thread-safe, non-blocking
    def summary(self) -> dict[str, Any]                  # small JSON (MCP/log)
def active_session(scope: Scope) -> CalibrationSession | None   # one session per Scope (WeakKeyDictionary)
```
- Blocking methods (anything that measures) hold a non-reentrant op lock. A concurrent call raises `CalibrationError("calibration session busy")`.
- `abort()` sets the session's `threading.Event` and, only if an op is running, calls `scope.abort_exclusive()`. The running op ends with `JobAborted`, which moves the session to ABORTED.
- Starting a second session on the same Scope raises `CalibrationError`.
- A session in a terminal state unregisters itself.

### 3.3 Measurement recipe
Every measurement is one `scope.run_exclusive(fn, abort_event=ev)` per setup: one exclusive job covers all of a setup's points, so the GUI's live view pauses once per setup.
- `cal_meas = base.without_gain_correction()`. `base` is the zero-step result, or the scope's calibration if zero was skipped.
- For each point: `ch = ChannelSettings(vdiv, DC, position)` and `dac = units.offset_dac(ch, cal_meas)`. Capture with `io.live_samples(frames*1024, timebase=5 ms/div, settings=..., calibration=cal_meas)`, using `CAL_WINDOW` = 625 samples per frame. Reference points use `frames = 4`; ground-map points use 2.
- The result is `LevelReading` (reused from `device/calibration.py`, extended with `frame_spread`). Ground is `ground_map[(vdiv, dac)]` if present (`ground_source="map"`), else `units.gnd_counts(ch, cal_meas)` (`"model"`).
- `v_read = (mean - ground) * s * vdiv / counts_per_div` (gain 1, no NL).
- **Zero setup**:
  - `measure_calibration(scope, abort_event=ev)`. Task B adds the `abort_event` pass-through. It does **not** install the result.
  - Then the ground-map job: quiet checks as in `_check_reading(quiet=True)`.
  - Reports the ground model error `max |map - model|` in counts.
  - `input_disconnected=True` is required (`UnsafeOperation` otherwise).
  - **Nothing is installed on the scope before `save()`**, so abort never needs to restore anything.
- **Trimmer**:
  - Measures with the *installed* calibration and the full path (`counts_to_volts` mean), 1 frame per sample.
  - `TrimmerReading{v_meas, v_ref, dev_v, dev_pct, raw_mean, noise_counts, best_abs_dev_pct, trend: better|worse|same, n, ground_source}`. `dev_pct` is relative to `v_ref`, or to `vdiv` for `reference="open"`.
  - The readout is `(mean - ground) * s * vdiv / counts_per_div` (gain 1, no NL) with the ground the zero step measured at the trimmer's exact (range, DAC) (`ground_source="map"`, consistent with the gain steps). Trimmer `first` or zero skipped: model ground (`"model"`, ~1 count = ~1 % at 3 div), stated in the instruction, the submit result and `SetupView.trimmer_ground_source`.
  - Session log: `trimmer_samples` = last 50 samples at full rate; `trimmer.history` = downsampled whole trimming (compact rows `t,n,dev_pct,v_meas,v_ref,raw_mean,noise_counts,best_abs_dev_pct`: each run's first sample, at most one per 0.5 s, and every best-deviation improvement; capped at 600 by dropping every second row and doubling the interval); `trimmer.runs` = per-run summary (initial/final/best deviation, duration, ground source).
- **Verify**: after `save` (the new calibration is installed). Measures through the installed calibration with *model* ground (no map). This is an independent check of the stored calibration and the position-to-DAC mapping.

### 3.4 Checks and quality codes
Each `PointResult` carries a `QualityCode` (`ok, saturated, noisy, no_signal, polarity, reading_mismatch, out_of_window, drift, device_error`), a message and a hint.

| Check | Rule | Effect |
|---|---|---|
| Reading sign | Readings are taken by magnitude. If the sign disagrees with the setup polarity, the expected polarity is used and a note is added ("assuming -2.51 V") | note |
| Reading window | Points whose true deflection `|v_ref|/vdiv` leaves their accepted window or position limits are dropped (`out_of_window`). If none remain, the setup FAILS with "set the supply to X V (allowed a..b)" | point / setup |
| Saturation | clipped > 1 % or mean within 4 counts of 0/255 | point fails |
| Noise | std > 2.5 counts: warning. std > 6 counts: fail ("loose connection or noisy source"). `frame_spread` > 0.5 counts: warning | |
| No signal | `|v_read| < 0.3 div` | fail ("source not connected?") |
| Polarity | `sign(v_read) != sign(v_ref)` and `|v_read| >= 0.3 div` | fail ("swap the leads at the source") |
| Sanity +-15 % | `|v_read/v_ref - 1| > 0.15` | fail ("check the typed value; is the probe on x1?"). If the setup's **first** point fails this way, the remaining points are not measured |
| Drift | After the last point, re-measure the first. `|dv| > max(0.3 %, 0.5 count)` | setup warning ("source drifting: re-read the meter") |
| Device errors | `AcquisitionTimeout`, `DeviceDisconnected` | setup FAILED (retryable). `JobAborted` goes to ABORTED |

Setup status: `ok` (all points ok), `warning` (some points dropped or warned, at least one usable), `failed`.

### 3.5 Fit (`src/edu09/calwizard/fit.py`, pure)
```python
@dataclass(frozen=True)
class GainPoint: vdiv: float; position_div: float; v_ref: float; v_read: float
                 ground_source: Literal["map", "model"]; noise_counts: float; setup_id: str
@dataclass(frozen=True)
class RangeFit: vdiv: float; gain_pos: float | None; gain_neg: float | None
                neg_knots: tuple[tuple[float, float], ...]; n_pos: int; n_neg: int; n_nl: int
                before_err_pct_max: float | None; after_err_pct_max: float | None; status: str  # fitted|kept|typical|unity
                warnings: tuple[str, ...]
@dataclass(frozen=True)
class FitResult: calibration: Calibration; ranges: dict[float, RangeFit]; warnings: tuple[str, ...]
                 def to_dict(self) -> dict[str, Any]       # floats 4 significant digits
def fit_calibration(base: Calibration, points: Sequence[GainPoint], *, previous: Calibration,
                    uncalibrated: Literal["keep", "typical", "unity"] = "keep",
                    knee_div: float = NL_KNEE_DIV, meta: Mapping[str, Any] | None = None) -> FitResult
NL_KNEE_DIV = 4.0; NL_MIN_EXCESS_DIV = 0.03
```
Points are classified by the true deflection `d = v_ref/vdiv`, not by the planned purpose.
- `g+`: least squares through the origin over `d > 0`, `g = sum(vt*vr) / sum(vr^2)`.
- `g-`: the same over `-knee_div <= d < 0`.
- **NL**, for points with `|d| > knee_div`:
  - `r = |v_read|/vdiv * g-` and `e = r - |d|`.
  - Points within 0.2 div of each other are averaged.
  - If `max e < NL_MIN_EXCESS_DIV`: no knots.
  - Otherwise knots are `[(min(knee_div, r_1 - 0.5), 0)] + [(r_i, e_i)]`.
  - Validated with the 1.2 rules. If invalid, NL is dropped with a warning and the gains are kept.
  - Without `g-`, NL is not fitted (it can't be separated from gain).
- Ranges without fitted values follow `uncalibrated`:
  - `keep`: previous `g+`, `g-` and knots together.
  - `typical`: `tables.TYPICAL_RANGE_GAIN` for `g+`, nothing for `g-` and NL.
  - `unity`: remove.
  - If `g+` is fitted and `g-` isn't: the previous `g-`/knots are dropped (falls back to `g+`), because `g-` relative to an old `g+` is not trustworthy.
- `calibration = base.with_gains(...)`, with `meta`, `source="measured"`. Hard limits are those of 1.2.
- Warnings:
  - `|g-1| > 0.05`;
  - `|g-/g+ - 1| > 0.03`;
  - any point residual after the fit > 0.3 %;
  - NL excess at the last knot > 0.6 div;
  - ground from the model on a gain point.
- Before/after error, per point: `100*(v_cal/v_ref - 1)`. `v_cal` = `deflection_to_volts(v_read/vdiv*cpd, vdiv, cal)`, where `cal` = `previous` gains on `base` offsets (before) or the new calibration (after). The "before" isolates the gain change. The offset change is reported separately as base_ofs/k/b old vs new and the ground-map model error.

### 3.6 Save, verify, revert, persistence
- `save(confirm=True)`:
  - Refuses if `scope.calibration.calibration_id != id at start()` ("calibration changed during the session (another client calibrated); restart").
  - Then `scope.set_calibration(cal, save=True, path=path)`, which backs up and writes atomically. Returns `SaveResult{saved, backup_path, calibration_id, path}`.
  - `apply_only=True` installs without writing. This is the default on sim/replay scopes, which have no calibration file unless `path` is given (the same rule as the MCP `calibrate` tool).
- Verify criterion per point: `|v_scope - v_ref| <= 0.005*|v_ref| + 0.05*vdiv`. Result `pass|fail` with errors in %. On failure, offer `revert()` or re-running the wizard.
- **Resuming an in-progress session: no.** A run takes about 10-20 min. Points depend on the zero step, the ground map and the thermal state taken in the same sitting. A resumed half-session would mix stale data, and resuming would double the state surface. Instead:
  - the source profile is persisted (2.3);
  - a **session log** is written at every terminal state (`DONE`, `ABORTED`, `FAILED`) to `storage.default_data_dir()/calibration-sessions/<stamp>.json`. It holds the plan, every point (raw mean, std, DAC, ground, readings), the fit, verify and trimmer samples. Size is 128 KiB or less (detail is dropped in levels to fit); the newest 20 are kept. Its path is returned in `summary()`. It contains no secrets.

### 3.7 Simulator reference (`src/edu09/calwizard/simref.py`)
```python
class SimReference:            # test/demo stand-in for "the human + bench supply + multimeter"
    def __init__(self, device: SimulatedDevice, *, noise_v: float = 0.002, meter_error_pct: float = 0.0,
                 source_error_pct: float = 0.0, seed: int | None = 0) -> None
    @classmethod
    def for_scope(cls, scope: Scope, **kw) -> SimReference | None   # scope.transport (Task B), unwraps RecordingTransport.inner; None if not sim
    def connect(self, volts: float) -> float   # sets device.signal = dc:(volts*(1+source_err)):noise; returns meter reading (3 decimals)
    def disconnect(self) -> None               # dc:0 V with noise (open input)
```
When the session has `sim`:
- It calls `disconnect()` before zero setups.
- `SetupView.sim_reading_v` carries the value `connect(setup.volts)` returned. Front ends prefill it, and the CLI `--yes` mode submits it.
- It never alters behaviour on hardware.

---

## 4. Front ends

### 4.1 CLI `edu09-calibrate` (`src/edu09/calwizard/cli.py`, entry point in `pyproject.toml`)
```
edu09-calibrate [--fixed SPEC]... [--adjustable SPEC]... [--mode quick|standard|full] [--max-volts 15]
                [--ranges 0.1,0.2,...] [--no-zero] [--no-verify] [--uncalibrated keep|typical|unity]
                [--trimmer [first|after_zero]] [--trimmer-vdiv V] [--trimmer-reference source|open] [--trimmer-source LABEL]
                [--plan-only [--json]] [--calibration PATH] [--save-sources]
                [--simulate [--sim-unit ideal|unit1] [--yes]]
--fixed SPEC:      VOLTS[@TOL%][,rev][,label=TEXT]      e.g. 5,label=USB   1.5@10%,rev,label=AA
--adjustable SPEC: MIN..MAX[,rev][,label=TEXT]          e.g. 0..15,rev,label=bench
```
- If no sources are given, it uses the saved profile, or asks interactively. It prints the plan and coverage, then asks to start.
- Per setup it prints the instruction and asks `Multimeter reading in V (r=retry, s=skip, q=quit):`.
- Zero: `Disconnect the probe (or clip tip to ground). Ready? [y/N]`.
- Trimmer: a live `\r` line about 4 times a second until Enter (a stdin reader thread; portable).
- Then the preview table, `Save? [y/N]`, then verify.
- `q` or Ctrl-C calls `abort()`; nothing is saved.
- `--plan-only` never opens a device: text, or `--json` (`Plan.to_dict()`).
- `--simulate` runs on a real-time `SimTransport` whose device has physical calibration `sim.PHYSICAL_UNITS[--sim-unit]`, and uses `SimReference`. With `--yes` it is fully non-interactive. This is the CI smoke path.
- Real hardware never accepts `--yes` (readings must come from a human): that combination is a usage error.
- Exit codes: 0 done, 1 error/aborted, 2 usage.
- `edu09-gain` stays as a low-level tool; the docs point to `edu09-calibrate`.

### 4.2 GUI (`src/edu09/gui/cal_wizard.py`; `main_window.py`)
`CalibrationWizard(QWizard)` takes `(scope, adapter, parent)`. It is window-modal; the main plot stays visible behind it.

Pages:
1. **Sources**:
   - An editable table: type fixed/adjustable, label, volts or min/max, tolerance %, reversible. The reversible checkbox shows the floating-source warning.
   - Mode, max volts, a "Trimmer step" group (the `TrimmerConfig` fields, collapsed) and the uncalibrated policy.
   - Loads/saves the profile.
2. **Plan**: list of setups, coverage grid (ranges x pos/neg/nonlin, with words "ok/fallback/missing"), hints, estimated time and reconnections.
3. **Step** (one page reused for all setups):
   - Instruction text, target value, a reading `QDoubleSpinBox` (4 decimals, prefilled from `sim_reading_v`).
   - Zero step: the **"The input is disconnected" checkbox** (keeps today's confirmation semantics).
   - Measure / Retry / Skip buttons, a busy indicator, and a per-point result list (status + message).
   - Next is enabled when the setup is done or skipped.
   - Trimmer: a large readout (`v_meas`, deviation in mV and %, best so far, trend) refreshed by chained background calls (never overlapping), plus a "Done adjusting" button.
4. **Preview**: per-range table (g+, g-, NL knots count, before/after max error %, status), warnings, and a "Save calibration" button (backup path shown after). Finish is disabled until save or explicit "Close without saving".
5. **Verify**: reading entry (prefilled with the last reading) and measure; pass/fail per point; a "Revert to previous calibration" button.

Wiring:
- All session calls go through `adapter.call_background` with result and error callbacks on the GUI thread.
- Cancel or close calls `session.abort()`, with a confirm dialog once any point exists.
- `MainWindow`: the "&Calibrate…" action now opens the wizard via `open_calibration_wizard()`. That method sets busy `"calibration"`, sets `_job_abort` to a wrapper calling `session.abort()` (so the existing quit-anyway path works), and resumes acquisition on close if it was running.
- The old `calibrate()` (offset-only) method stays for programmatic use and the existing tests, but is no longer in the menu.

### 4.3 MCP tools (`src/edu09/mcp_server/cal_tools.py`; registered from `tools.register_tools` only with `--allow-calibration`)
At most one session per server. It is in memory and aborted after 30 min idle (checked on every call) or on server shutdown. Floats have 4 significant digits; no raw samples; each response is 8 KB or less. Tool descriptions must say: **"Readings must be typed by the human from their multimeter; never invent or estimate them. Ask the human to perform every physical step."**

| Tool | Params | Returns |
|---|---|---|
| `calibration_plan` | `sources: list[{label, volts? , min_v?, max_v?, tolerance_pct=5, reversible=false}]`, `mode="standard"`, `max_abs_v=15`, `trimmer=false` | `{setups:[{id,kind,instruction,source,volts,points}], coverage, reconnections, user_actions, estimated_s, warnings}`. Pure, no device |
| `calibration_start` | same as plan + `uncalibrated="keep"` | `{session_id, step}`. Error if a session is active |
| `calibration_step` | none | `{step}`: `{state, index, total, setup:{id,kind,instruction,volts,needs:"input_disconnected"\|"reading_v"\|"done"\|"none"}, last_result?, live?, sim_reading_v?, warnings}`. In the trimmer LIVE phase each call takes one `trimmer_sample` and returns it as `live` |
| `calibration_submit` | `action: "measure"\|"retry"\|"skip"\|"done"`, `reading_v: float\|None`, `input_disconnected=false` | `{result:{status, points:[{vdiv,position_div,deflection_div,err_pct,status,message}], messages}, step}` |
| `calibration_finish` | `save=false`, `confirm=false` | `save=false`: `{preview}` (fit). `save=true` needs `confirm=true`: `{preview, saved, backup_path, calibration_id, warnings}`, then the step moves to verify |
| `calibration_abort` | none | `{aborted, log_path}` |

- Validation: `|reading_v| <= 30` and finite; at most 8 sources.
- Errors use the existing `"<ErrorClass>: <hint>"` form (`UnsafeOperation` for a missing `confirm`/`input_disconnected`).
- The existing `calibrate` tool is unchanged.
- Optional: `edu09-mcp --sim-unit` mirroring the CLI.

---

## 5. Simulator support (`transport/sim.py`, `transport/replay.py`)
The sim already converts volts to counts with the device's *physical* `Calibration` through `units.volts_to_counts`. Once 1.3 lands, a physical calibration with v2 fields automatically models positive and negative gain and the expansion, including in the trigger comparator. Add:
- `PHYSICAL_UNITS: dict[str, Calibration] = {"ideal": Calibration(), "unit1": UNIT1}`. `UNIT1` = typical offsets (`tables.TYPICAL_*`) + the gains and knots below. The test `test_sim_physics` reproduces `docs/data` within 0.1 counts/div.

  | V/div | g+ | g- | knots (r, e) |
  |---|---|---|---|
  | 0.1 | 0.9870 | 0.9846 | none |
  | 0.2 | 0.9750 | 0.9668 | (4.0,0) (5.069,0.069) (6.26,0.26)* |
  | 0.5 | 0.9741 | 0.9697 | (4.0,0) (5.083,0.083) (6.315,0.315) |
  | 1 | 0.9960 | 0.9892 | none |
  | 2 | 0.9810 | 0.9786 | none |
  | 5 | 0.9789 | 0.9726* | none |

  (* = extrapolated or assumed.)
- `SimulatedDevice.trim_gain: float = 1.0`: a fault/test knob multiplying the input volts (a stand-in for the unknown trimmer, so trimmer-readout tests can move it).
- `RecordingTransport.inner` read-only property (for `SimReference.for_scope`).
- `--simulate` in gui/mcp keeps `ideal` physics by default.

---

## 6. Test plan
- **units/storage (A)**:
  - round trip and monotonicity on a grid of raw 0..255 x 6 ranges x positions {-3.5, 0, 3.5} x {empty, UNIT1};
  - bitwise v1 equality when the v2 fields are empty;
  - continuity at ground;
  - `trigger_byte` below ground uses NL;
  - every knot validation rule (`CalibrationError`);
  - `to_dict` omits empty v2 keys;
  - pinned `calibration_id` of the user's current file;
  - `meta` excluded from id and equality;
  - save writes v1 or v2 by content; load accepts both; legacy opaque NL rejected.
- **fit (A)**:
  - synthetic points from UNIT1 (plus noise) recover `g+` within 0.001, `g-` within 0.002 and `e(6 div)` within 0.02;
  - single-point ranges; NL dropped when invalid or below the excess threshold;
  - all three `uncalibrated` policies and the dropped-`g-` rule;
  - warnings fire at their thresholds;
  - before/after numbers.
- **sim (A)**: UNIT1 reproduces the sweep-data secants (0.5 V/div at 6 div below ground: 34.7 +- 0.1); `trim_gain`; `SimReference`.
- **planner (B)**:
  - deterministic snapshots for four profiles: (a) USB 5 V + AA 1.5 V reversible, (b) 0-15 V reversible, (c) 0-30 V non-reversible with cap 15, (d) no sources (zero only, all gains "missing");
  - every point within the position/level limits and `|V| <= max_abs_v`;
  - grouping order (one lead swap per source);
  - coverage hints contain the needed voltages; the floating warning is present; `parse_source` grammar and errors; `Plan` dict round trip.
- **session (B)**, on `SimTransport.virtual` with UNIT1 + `SimReference`:
  - happy path: standard mode reaches DONE; saved to `tmp_path` with a backup; verify passes; recovered gains within tolerance;
  - error paths: polarity mistake (wrong lead swap), x10 probe (reading x10, gives `reading_mismatch`), wrong supply value (`out_of_window`), noisy source (`noise_v`), saturation;
  - controls: skip, retry, skip zero (model ground flagged);
  - abort during a measurement from another thread (ABORTED, `JobAborted`, nothing installed or saved, previous calibration still active);
  - concurrency: busy re-entry; second session refused; calibration changed mid-session refuses save;
  - revert; log written and pruned.
- **CLI (B)**: `--plan-only` text and JSON; source grammar; `--simulate --sim-unit unit1 --yes` end to end to a tmp file; `--yes` without `--simulate` is a usage error.
- **GUI (C, pytest-qt)**: drive the wizard on sim with prefilled readings to DONE; Cancel aborts; the zero page needs the checkbox; trimmer live updates stop on Done; the menu action opens the wizard; existing calibrate tests still pass.
- **MCP (C)**: tools absent without the flag; full conversational flow on sim; payload sizes; validation; `finish(save=true)` without `confirm` refused; single session; idle-timeout abort.
- **Manual hardware checklist (user, after merge)**:
  - `--plan-only` with the user's real sources;
  - a full standard run with the bench supply;
  - compare the fitted values with `docs/data` (expect `g+` within 0.003);
  - verify passes;
  - trimmer exploration: run with `--trimmer --trimmer-reference open`, then again with `source`, turn the trimmer, and keep the session logs (they decide the trimmer defaults later).

---

## 7. Task split (one wave, 3 coders in one shared checkout, strict file ownership)

**Interface-first rule (as in wave 2).** Within each task's first commit:
- A publishes the `units` v2 signatures (fields, `deflection_to_volts`/`volts_to_deflection`, `with_gains`, `without_gain_correction`) and the `fit.py` dataclasses and signature.
- B publishes `calwizard/__init__.py`, `model.py` (all dataclasses) and the `session.py` class with signatures.

Placeholder bodies are allowed until implemented. Everything else codes against section 3.2 / 1.3.

| Task | Model | Owns (only these files) | Deliverables |
|---|---|---|---|
| **A: data model, fit, sim** | pro | `src/edu09/units.py`, `src/edu09/protocol/tables.py`, `src/edu09/storage.py`, `src/edu09/transport/sim.py`, `src/edu09/transport/replay.py`, `src/edu09/calwizard/fit.py`, `src/edu09/calwizard/simref.py`, `docs/protocol.md` (5.2, 11.3), tests `tests/units/test_units_v2.py`, `tests/units/test_storage_v2.py`, `tests/calwizard/test_fit.py`, `tests/transport/test_sim_physics.py` | Sections 1, 3.5, 3.7 and 5 |
| **B: planner, session, CLI** | pro | `src/edu09/calwizard/__init__.py`, `model.py`, `planner.py`, `session.py`, `cli.py`, `src/edu09/device/calibration.py` (`abort_event` pass-through, `replace`-based carry-over, `without_gain_correction`, `LevelReading.frame_spread`), `src/edu09/device/scope.py` (read-only `Scope.transport` property only), `pyproject.toml` (entry point), tests `tests/calwizard/test_planner.py`, `test_session.py`, `test_cli.py`, `test_e2e_unit1.py`, `tests/calwizard/conftest.py` | Sections 2, 3.1-3.4, 3.6 and 4.1 |
| **C: GUI + MCP + docs** | inherit (sonnet ok for MCP part) | `src/edu09/gui/cal_wizard.py`, `src/edu09/gui/main_window.py`, `src/edu09/mcp_server/cal_tools.py`, `src/edu09/mcp_server/tools.py` (one register call), `src/edu09/mcp_server/server.py` (shutdown hook), `src/edu09/mcp_server/__main__.py` (optional `--sim-unit`), `docs/usage.md`, `docs/specs/architecture.md` (4.3, 5, 6), `README.md`, tests `tests/gui/test_cal_wizard.py`, `tests/mcp/test_cal_tools.py` (+ updates to existing gui tests touching the Calibrate action) | Sections 4.2 and 4.3, plus docs |

Dependencies:
- B's session e2e tests need A's units/fit/simref.
- C's e2e tests need B's session.
- Until those land, C tests against a small fake session (same API). A and B unit-test their pure parts in isolation.

The integration order on merge is A, then B, then C. Then run the full suite plus ruff and mypy.

**Review (reviewer, pro)**:
- Invertibility and monotonicity proofs vs tests.
- Abort and threading in the session and GUI (no measurement after abort; nothing saved; GUI never blocks).
- MCP safety (flag gating, confirm, no fabricated readings path, payload caps).
- Backward compatibility (pinned id, v1 bitwise equality, user file loads).
- Secret audit of the diff.

**Afterwards (orchestrator)**: the hardware session with the user (section 6 checklist), then update `tables.TYPICAL_*` and protocol 11.3 if the results disagree.

## 8. Open questions / risks
- **Trimmer purpose unknown.** Defaults (`placement=first`, `reference=source`, 1 V/div) are guesses. The logs from the first hardware run decide them. If it turns out to trim offset, prefer `reference=open`.
- **NL on other units** may start below 4 div or appear on other ranges. `knee_div` is a fit parameter, and `full` mode adds points. The 0.1, 1 and 2 V/div expansion was never measured beyond 5/3/1.5 div.
- **Positive-side expansion** above 6 div has not been measured. It is not modelled; the format could later add a `pos_nonlinearity` key (v3).
- **Fixed-source-only users** get fallback or missing ranges. The coverage report and divider hint mitigate this. `uncalibrated=typical` is opt-in because the typical values come from one unit.
- **Ground drift** between the zero step and late reference setups (minutes) is not modelled. Verify uses model ground and catches gross drift.
- Older builds can't read v2 files (strict keys). Offset-only files stay v1.
