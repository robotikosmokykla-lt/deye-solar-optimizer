# Architecture

How the optimizer decides what to do, and why each guard exists. Read this before
changing anything in `energy_strategy.py` or `controller.py`.

## The problem this solves

A grid-tied hybrid inverter with a **hard export limit** and a battery. The export
cap is the binding constraint: if the limit is 1 kW, no more than 1 kW can ever
leave the house, so surplus PV above that has exactly two destinations — the
battery, or curtailment.

That shapes every decision:

* **Cap headroom is perishable.** Export capacity not used in an interval is gone.
  It cannot be saved for later.
* **Storing is lossy but usually worth it.** A round trip costs roughly 14%, but
  a stored kWh can still be exported overnight, whereas a curtailed kWh is zero.
* **Writes are scarce.** Changing an inverter setting writes to its flash. The
  optimizer budgets a handful of confirmed changes per day, so each one must count.

## The loop

`controller.py` runs one tick every `CONTROL_LOOP_SECONDS` (default 60):

```
fetch telemetry  ->  derive control SOC  ->  classify phase  ->  build plan
      -> recommend an export cap  ->  check the write gates  ->  maybe submit
```

Nothing is queued between ticks. Every tick re-derives the recommendation from
scratch, so a decision is never stale: whatever the controller believes at the
moment a write window opens is what gets written.

### Phases

| Phase | From | To | Plan |
|---|---|---|---|
| `NIGHT` | sunset − `CONTROL_NIGHT_START_MINUTES_BEFORE_SUNSET` | morning PV handoff | `build_morning_soc_plan` |
| `DAY` | morning PV handoff | sunset − night-start offset | `build_day_energy_plan` |

The **morning PV handoff** is not sunrise. It is the first time the forecast
sustains `NIGHT_MORNING_SURPLUS_THRESHOLD_W` for `NIGHT_SUSTAINED_MINUTES`, minus
a lead. Below that threshold PV cannot carry the house, so the battery is still
the one working.

## The daytime export budget

`build_day_energy_plan` answers: *how much energy may leave the house before
sunset without leaving the battery short overnight?*

```
1. end-of-day SOC target
       floor + (night draw to tomorrow's handoff + reserve) / capacity
   Released to that floor only when tomorrow's DISCOUNTED forecast can refill it;
   otherwise the configured day target holds. No tomorrow forecast => never release.

2. remaining PV
       forecast  x  intraday bias  x  safe factor

3. energy balance
       budget = PV surplus + stored surplus above the end-of-day target
       (or minus the charge deficit, when SOC is below it)

4. allocation
       budget sustains the cap for the rest of the window ? cap : flat average
```

Four ideas here are load-bearing:

**The reserve is an end-of-day floor, not an export deduction.** Subtracting it
from the export budget *as well as* holding it in the battery counts it twice, and
at high SOC that suppresses export while the battery is full and PV is curtailed.

**The reserve is hours of cover, not a fixed slab.** A fixed kWh reserve is many
hours of autonomy at summer base load and few once heating raises it. It scales
with the learned house load so autonomy stays constant across the season.

**Bias and safety are different things.** The intraday bias corrects the forecast's
*error* — how today is actually tracking against its own curve. The safe factor is
an *uncertainty* discount. They are applied separately so correcting for today's
weather never spends the safety margin.

**Flat averaging cannot reach the cap.** Spreading a budget evenly over the day
guarantees the cap is never saturated even when the budget could sustain it, which
silently wastes perishable headroom. Hence `cap_sustained`.

## Feedback, because forecasts lie

The plan is feed-forward from a forecast, so two closed-loop terms correct it:

* **Intraday bias** (`intraday_forecast_bias`) compares the inverter's own daily
  production counter against the forecast integrated *to the timestamp of that
  sample*, never to the wall clock. With stale telemetry the two differ by hours
  and would invent a shortfall. The ratio is shrunk toward 1.0 until enough
  forecast energy has elapsed to be meaningful, then clamped.

* **Curtailment override** (`Controller.curtailment_override`) is the safety net
  for when the plan is simply wrong. If telemetry is fresh, the battery is at its
  ceiling, it is no longer absorbing, and PV is still producing, then surplus is
  being thrown away right now and the cap reopens to the legal limit regardless of
  the budget. It only ever *raises* the cap, never lowers it.

## Write gates

`Controller.can_write` re-checks all of these every tick. Any one blocks a write:

| Gate | Purpose |
|---|---|
| target within `0..GRID_EXPORT_HARD_LIMIT_W` | never exceed the legal limit |
| no order already pending | one in flight at a time |
| daily submission ceiling | bounds command storms, counts failures too |
| daily successful-write budget | flash wear; only confirmed changes count |
| per-reason day budget | stops one subsystem eating the whole allowance |
| failed-order backoff | device rejections get a longer cooldown than transients |
| uncertain-submit guard | an ambiguous response may have landed; never duplicate |
| successful-write cooldown | minimum interval between confirmed changes |
| minimum write delta | do not spend a write on a trivial change |

Two budgets are tracked deliberately: **confirmed successful changes** (wear) and
**accepted submissions** (storms). A rejected order costs a submission but not a
wear slot, so a couple of device rejections cannot block a legitimate correction.

Writes are frozen entirely while telemetry is stale. In practice the ability to
write and the availability of fresh telemetry are the same condition — the cloud
reports the device offline for both — so there is no "act on stale data" path.

## Analytics is not in the control path

`analytics_engine.py`, `dashboard_server.py` and `analytics_cli.py` open the state
database **read-only** and contain no inverter write calls. They can be run, killed
or crashed without affecting control. The dashboard binds to localhost by default.

Counterfactual replays, the oracle and the curtailment estimate are **benchmarks,
not policies**: the oracle has perfect hindsight, and curtailment is a model
estimate rather than an irradiance measurement.

## Module map

| File | Role |
|---|---|
| `controller.py` | the loop, phases, write gates, order lifecycle |
| `energy_strategy.py` | budgets, targets, bias, reserve — **no write calls** |
| `strategy_presets.py` | named strategies and their risk parameters |
| `solar_forecast.py` | Open-Meteo → per-array 15-minute PV forecast |
| `deye_api.py` | Deye Cloud client: auth, telemetry, orders |
| `state_db.py` | SQLite state, telemetry, decisions, writes |
| `config_loader.py` | `.env` parsing; environment overrides file |
| `analytics_engine.py` | read-only replays, scoring, timelines |
| `dashboard_server.py` | read-only HTTP API + dashboard |
| `day_planner.py` | one-shot read-only explanation of the current plan |
| `profile_manager.py` | infrequent work-mode/profile commissioning |

## Conventions worth knowing

* **Battery power is negative when charging**, positive when discharging.
* **Grid power is negative when exporting.**
* Forecast points carry the energy of the interval they *start*, so integrals are
  half-open: summing to 12:00 excludes the 12:00–12:15 interval.
* Times are stored as local ISO strings with offset; days are grouped by local date.
