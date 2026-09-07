# Changelog

## v3.1.5 - 2026-09-07

### Honest oracle benchmark

- The perfect-foresight oracle was held to the fixed `BATTERY_DAY_TARGET_SOC_PCT`
  while the live plan aims at the night's real need. That flattered the optimizer:
  the benchmark was forbidden from spending battery the optimizer was free to spend,
  so days looked closer to optimal than they were.
- The oracle now uses the same end-of-day requirement the plan computes, rebuilt
  from the stored forecasts for that date and the next, and reports which target it
  used and why.
- Known limitation: "actual" is derived from the inverter's cumulative meters while
  the oracle replays reconstructed intervals, so on days with poor telemetry
  coverage the two disagree and the oracle is not a strict upper bound.

## v3.1.4 - 2026-09-07

### The battery refills from energy the cap cannot carry

- The daytime plan treated refilling the battery as something export had to be
  sacrificed for, so at low SOC it closed the export cap until the battery was full.
  On a hard-capped site that is backwards: export is limited to the cap while
  charging is not, so energy above `load + cap` reaches the battery whatever the
  setpoint is. Only the shortfall beyond that competes with export.
- `surplus_above_cap_kwh()` computes that energy from the discounted forecast,
  limited by the battery's charge power, and credits it against the charge deficit.
- The effect is confined to the middle of the range: below roughly `load + cap` there
  is no above-cap energy and charging still wins, and on very strong days the budget
  already saturated the cap. In between - where a capped site spends most of the year
  - the cap now stays open instead of closing to charge.
- Opt out with `DAY_ABOVE_CAP_REFILL_ENABLED=false`.

### Setpoints that survive being frozen

- Cloud telemetry goes stale for hours at a time and writes are frozen while it is,
  so a setpoint is not a decision that can be revised next minute: it may stand until
  sunset. That makes 0 W the worst available choice - harmless if revisable, a lost
  afternoon if not.
- During daylight the recommendation is now floored at `DAY_STALENESS_FLOOR_W`
  (default 300 W), but only when the discounted forecast still meets the end-of-day
  requirement after paying for the floor. A day that cannot afford it still charges,
  and the floor is dropped near sunset.
- Both terms are reported by `deyeopt-day-plan` and on the dashboard.

## v3.1.3 - 2026-09-07

### Load-scaled strategy reserve

- The strategy reserve is now held as a number of hours of measured house draw
  instead of a fixed slab of kWh. A flat 2.5 kWh is about seven hours of cover in
  summer but barely three once heating raises the base load, so the same setting was
  over-cautious in July and thin in January.
- Hours per strategy live in `strategy_presets.py`, calibrated to reproduce each
  preset's previous kWh at a typical total draw, so upgrading changes nothing on day
  one and the reserve re-tunes itself as the learned load history moves.
- `max-export` keeps a zero reserve; `save` still reserves more than `conservative`
  at every load.
- Clamped by `DAY_RESERVE_MIN_KWH`/`DAY_RESERVE_MAX_KWH`; set
  `DAY_LOAD_SCALED_RESERVE_ENABLED=false` to restore the fixed per-strategy values.
- Applied consistently to both the daytime export budget and the overnight
  morning-SOC requirement, which previously read the fixed value directly.
- The reserve and its source are shown by `deyeopt-day-plan` and on the dashboard.

## v3.1.2 - 2026-09-05

### Intraday forecast-bias corrector

- The daytime plan now corrects the remaining forecast by how today is actually
  tracking against its own curve. Previously both learning inputs were
  completed-day statistics: the safe factor came from the last 3-14 finished days
  and nothing looked at the day in progress.
- The correction is multiplicative on the forecast; the safe factor stays a
  separate uncertainty discount applied on top, so correcting for today's
  conditions never spends the safety margin.
- The ratio is taken over a trailing window (default 3 h) using the inverter's own
  daily production counter, falling back to the whole day so far when the window
  has no opening sample. The trailing window matters: a strong dawn can mask an
  afternoon running well behind forecast, which a whole-day ratio averages away.
- The forecast is integrated to the timestamp of the actual sample, never to the
  wall clock, so stale telemetry cannot invent a shortfall. Staleness is reported
  in the source tag.
- The correction is shrunk toward 1.0 until enough forecast energy has elapsed to
  make the ratio meaningful, and clamped to `DAY_INTRADAY_BIAS_MIN`/`MAX`.
- Opt-out with `DAY_INTRADAY_BIAS_ENABLED=false`.
- Added `StateDB.day_production_at()`.
- Fixed the forecast point integral to be half-open. Each point carries the energy
  of the interval it starts, so an inclusive upper bound over-counted one interval
  and biased every ratio low.
- `deyeopt-day-plan` and the dashboard show the bias, its source, and the
  forecast -> bias-corrected -> safe chain.

## v3.1.1 - 2026-09-05

### Daytime export budget

- The daytime plan can now export stored battery surplus, not only PV surplus.
  `night_export_for_target_w` was wired into the morning plan alone, so a battery
  that was already full at dawn had no path to be sold down ahead of a strong
  next-day forecast, so a full battery could sit with the export cap shut while
  surplus PV was curtailed.
- The strategy reserve is applied as an end-of-day stored-energy floor instead of
  being subtracted from the export budget. At high SOC the reserve is already
  physically in the battery, so subtracting it again double-counted it.
- The end-of-day SOC target is derived from the real night need to tomorrow's PV
  handoff. Stored surplus is released only when tomorrow's *discounted* forecast
  can refill to the configured day target; otherwise the day target still holds.
- Export is allocated at the hard cap when the budget can sustain it for the rest
  of the window. Flat averaging never reached the cap even when the budget allowed,
  and cap headroom unused in an interval is lost permanently.
- `deyeopt-day-plan` reports the end-of-day target, the PV/stored split of the
  budget, cap-sustain hours and the allocation mode.

### Closed-loop safety

- Added a curtailment override: when telemetry is fresh and the battery is at its
  ceiling, no longer absorbing, and PV is still producing, the sell cap is reopened
  to the legal limit regardless of the forecast-derived budget. Stale telemetry
  never opens the valve; `save` is exempt; the override only ever raises the cap.
  Configured by `DAY_CURTAILMENT_OVERRIDE_*`.
- Deye `error 540` device rejections get their own longer cooldown
  (`CONTROL_DEVICE_REJECT_RETRY_MINUTES`, default 45) so repeated rejections stop
  burning the daily order-submission ceiling.

### Dashboard

- Added a control panel: confirmed export setpoint vs recommendation and headroom,
  decision reason and last action, write and submission budgets, telemetry health,
  and the end-of-day target / budget split / allocation mode. All of this was
  already in the database and none of it was rendered.
- Added a curtailment banner that fires on the live battery-full-and-throttled state.
- Added an intraday power timeline (PV, load, grid export, confirmed export cap)
  and a battery SOC panel with the curtailment threshold marked.
- Fixed the forecast-vs-achieved chart: every series is now on one shared
  minute-of-day domain. Each was previously normalised to its own first and last
  timestamp across the same pixel width, so a half-day actual curve was drawn as
  if it covered the whole day.
- Counter-reconstructed intervals are drawn faded; their instantaneous power is an
  artifact of sample spacing rather than a measurement.
- Date defaults to site-local rather than UTC; the cap slider takes its bounds from
  the configured hard limit; fetch failures are shown instead of swallowed;
  export-cap steps carried over from a previous day anchor at midnight.
- Validated categorical palette with legends and non-colliding direct labels,
  hover crosshairs and tooltips, dark mode, and horizontally scrollable tables.

### Writes and windows tab

- Added `/api/control` and a second dashboard tab answering "did the write go through,
  and if not, what was in the way".
- Write attempts for the day are listed with the change, delta, reason, status, the
  Deye error code parsed out of the order details, confirmation time and order id.
- Write-window availability is reconstructed from the action recorded on every
  control tick and drawn as a banded 24-hour timeline: open, write in flight,
  blocked by write cooldown or budget, blocked by failed-order backoff, blocked by a
  stale or refusing DeyeCloud, and no controller data. Confirmed and rejected writes
  are marked on the band.
- Added a "right now" gate panel naming the current blocker and, where the gate is a
  timed one, when the next window can open; plus time-by-gate-state and the longest
  blocked intervals.
- Gate state uses the reserved status palette in severity order and is always named
  in the legend and tables, so it never rides on colour alone.

### Notes

- The write-count safeguards, hard export limit, cloud-stall freeze, pending-order
  lock and `conservative`/`risky`/`max-export`/`save`/`economic` semantics are
  otherwise unchanged.

## v3.1.0 - 2026-09-04

### Read-only analytics layer

- Added local HTML dashboard on `127.0.0.1:8787` via separate `deye-solar-analytics` systemd service.
- Added `deyeopt-analytics` CLI.
- Added stateful counterfactual replay for static export caps (default 0/300/500/800/1000 W) and arbitrary cap slider.
- Added perfect-foresight oracle benchmark with end-of-day SOC target.
- Added economic perfect-foresight oracle using configured import/export/wear costs.
- Added optional live `economic` strategy for static tariffs: battery-backed export is enabled only when configured export revenue beats replacement-import energy plus modeled battery wear; otherwise battery is preserved and only forecast-safe surplus is exported.
- Added actual-vs-forecast cumulative PV display, forecast revision history, rolling 30-day actual-vs-static score.
- Added battery aging/stress metrics: throughput, EFC, high/low-SOC hours and C-rate.
- Added rough forecast-based curtailment estimator.
- Added per-MPPT observed-energy integration and optional MPPT-to-array mapping.

### Learning

- Forecast records now optionally store their full 15-minute point series for later analytics.
- Added delayed lead-time-bucketed probabilistic forecast calibration: P10/P20/P50/P80/P90 activate only after the configured minimum history (default 10 comparable completed days).
- Added adaptive morning timing learner. It remains inactive until default 7 suitable post-v3.1 days and then applies a clamped median actual-vs-forecast sustained-PV timing bias.

### Safety/packaging

- Analytics uses SQLite read-only mode and contains no Deye control call path.
- Dashboard binds to localhost by default.
- v3.1 upgrade appends newly introduced `.env` keys without overwriting existing values or credentials.
- Existing `conservative`/`risky`/`max-export`/`save` behavior, hard export limit, write-count safeguards and cloud-stall protections are unchanged; `economic` is opt-in.
- Boiler/cooker timing optimization and generic appliance scheduling are intentionally not included; those remain planning inputs only.

## v3.0.0 - 2026-09-04

### Configuration / GitHub readiness

- Replaced runtime `config.toml` + separate credential files with one secure `.env` source of truth.
- Added dependency-free `.env` parser (`config_loader.py`); process environment can override file values.
- Added `.env.example` covering Deye credentials, site, PV arrays, battery, grid limits, scheduled loads, forecast uncertainty and write safety.
- Added GitHub-safe `.gitignore` excluding real `.env`, DBs, logs, JSONL and diagnostic bundles.
- Upgrade automatically migrates v2.x `config.toml` and credential files into `/etc/deye-solar-optimizer/deye.env` while preserving SQLite state and live/dry-run mode.
- Legacy heater schedules are preserved during migration; optional `--water-heater-time HH:MM` can override the planning time explicitly.

### Scheduled loads

- Water heater is configurable by enable/time/power/duration/measured energy from `.env`.
- Added optional cooker schedule/power/duration/measured-energy inputs.
- Both scheduled loads are deducted from the daytime energy budget and from historical house-load learning.
- Scheduled-load settings are planning inputs only; v3 does not switch appliances.

### Write accounting / safety

- Confirmed Deye `status=500` failed orders no longer consume the normal successful-setting-change budget.
- Normal budget defaults to 4 confirmed `status=666` changes/day.
- One bonus fifth successful write can be allowed for a material delta, default >=500 W.
- Added separate anti-storm ceiling of 8 positive-orderId submissions/day, so repeated failures remain bounded.
- Day-energy-budget per-day quota now counts confirmed successful budget changes, not failed accepted orders.
- Existing failed-order retry backoff, pending-order lock, cloud-staleness freeze and uncertain-submit guard remain intact.

### Diagnostics

- Added installed `deye-day-export` command.
- Default exports current local day.
- Added `--hours N`, positional `N`, and `--since` time-window exports.
- Window exports prune the SQLite snapshot and filter journal/JSONL to reduce upload size while retaining KV/control state.
- Added `--full-db` when long-term history is explicitly needed.
- Diagnostic bundles include redacted `.env`, current status/day-plan/strategy and one current `/device/latest` read.

### Documentation / calibration

- Rewrote English and Lithuanian README files for public GitHub use.
- Added step-by-step calibration for telemetry signs, inverter overhead, effective battery capacity, charge efficiency, ordinary house load, scheduled loads, PV geometry, forecast uncertainty and morning handoff.
- Documented seasonal re-learning and DeyeCloud cached-telemetry behavior.

## v2.3.0

- Added named strategy tags: conservative, risky, max-export, save.
- Added forecast-protected morning SOC for conservative operation.
- Fixed pre-dawn daytime planning horizon to start at morning handoff.
