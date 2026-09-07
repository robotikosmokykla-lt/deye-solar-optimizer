# Initial calibration checklist - v3.1.0

1. Keep `CONTROL_DRY_RUN=true` for first observation.
2. Verify grid/battery power signs against the inverter display.
3. Verify `GRID_EXPORT_HARD_LIMIT_W` before any live write.
4. Collect at least one clean night and estimate `LOAD_SYSTEM_OVERHEAD_W` from battery - export - house load.
5. Estimate `BATTERY_EFFECTIVE_KWH` over a broad SOC discharge range.
6. Measure heater/cooker energy and enter schedules before trusting load learning.
7. Verify PV kWp, tilt and azimuth for every array in `PV_ARRAYS_JSON`.
8. Accumulate >=5 complete forecast/actual days before relying on learned forecast uncertainty.
9. Compare the sustained-PV morning handoff against actual PV for several mornings.
10. Start live control conservatively and inspect `deye-day-export --hours 6` after each material change.


## v3.1 learning gates

Do not force learned models early. Recommended defaults:

```env
FORECAST_PROBABILISTIC_MIN_DAYS=10
MORNING_LEARNING_MIN_DAYS=7
```

Until those gates are met, the explicit safe forecast factor and manual morning bias remain authoritative. Review the dashboard after the first 1-2 weeks and confirm the learned distributions are plausible before relying on them in less conservative strategies.

For MPPT learning, configure `ANALYTICS_MPPT_MAP_JSON` only after physically verifying which Deye PV channel corresponds to each modeled array.

For economic comparison, enter your actual import/export tariffs and optionally an estimated battery wear cost. These values are analytics-only in v3.1.
