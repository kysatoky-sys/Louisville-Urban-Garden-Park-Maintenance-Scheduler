# Validation Framework Documentation

## Louisville Urban Garden & Park Maintenance Scheduler

This document describes all eight data quality and validation checks implemented in the ETL pipeline. Checks 1–5 run in `extract_transform.py` before `weather_data.csv` is written to disk. Checks 6–8 run in `load.py` before and after data is inserted into the database.

---

## Overview

| # | Check name | Type | Script | Stage |
| --- | --- | --- | --- | --- |
| 1 | API response validation | API response validation | `extract_transform.py` | Extract |
| 2 | Null value check | Null value check | `extract_transform.py` | Transform |
| 3 | Range validation | Range validation | `extract_transform.py` | Transform |
| 4 | Duplicate detection | Duplicate detection | `extract_transform.py` | Transform |
| 5 | Output row count check | Row count verification | `extract_transform.py` | Transform |
| 6 | JSON schema validation | Schema/type validation | `load.py` | Load (pre-insert) |
| 7 | Referential integrity check | Referential integrity | `load.py` | Load (pre-insert) |
| 8 | Post-load row count check | Row count verification | `load.py` | Load (post-insert) |

---

## Validation Check 1 — API Response Validation

**Location:** `extract_transform.py` → `fetch_hourly_for_park()`

**What it checks**

After each Open-Meteo API call, two structural conditions are verified:

- The response must contain a populated hourly block (`response.Hourly()` must not be `None`).
- The number of variable arrays returned (`hourly.VariablesLength()`) must equal the number of fields in `HOURLY_FIELDS` (currently 4: `soil_temperature_0cm`, `soil_moisture_0_to_1cm`, `evapotranspiration`, `precipitation`).

**Why it matters**

Hourly data is read by positional index using `hourly.Variables(i).ValuesAsNumpy()`. The order of variables in the response matches the order they were requested. If the API returns fewer variables than expected — because a field name was misspelled, the API contract changed, or the request was malformed — every index beyond the last variable returned reads from the wrong array. For example, if only 3 variables come back, reading index 3 for `precipitation` would silently return `evapotranspiration` values instead, poisoning every constraint evaluation for that park with no error.

**What happens if it fails**

A `ValueError` is raised inside a `try/except` block. The exception is caught, the error is logged with the park name and reason, and the function returns `None`. The calling loop in `fetch_all_parks()` skips that park and continues fetching the remaining parks. The pipeline does not halt.

```
ERROR — Skipping park 'Cherokee Park': Expected 4 hourly variables, got 3.
```

---

## Validation Check 2 — Null Value Check

**Location:** `extract_transform.py` → `validate_weather()`

**What it checks**

After hourly data has been aggregated to daily means and totals, each of the four weather columns — `soil_temperature_0cm`, `soil_moisture_0_to_1cm`, `evapotranspiration`, `precipitation` — is checked for null values using `pandas.Series.isna().sum()`.

**Why it matters**

The constraint evaluation engine in `load.py` calls `_compare(actual, operator, value)` for every rule. When `actual` is null, `_compare()` returns `False` as a safe default — but this means the check silently does not fire. A null `soil_moisture_0_to_1cm` bypasses the waterlogging check and recommends mowing on a saturated field. A null `precipitation` value prevents a deferral from triggering before a rainy day. Nulls reaching the constraint engine produce incorrect recommendations with no indication anything was wrong.

**What happens if it fails**

Each null cell is filled with the median of that column across all parks and days in the current forecast window. The median is used rather than a fixed fallback because it is representative of current Louisville conditions. A warning is logged for each affected column, including the null count and the median value applied.

```
WARNING — NULL CHECK — 'soil_moisture_0_to_1cm': 2 null value(s). Filling with median (0.2841).
```

---

## Validation Check 3 — Range Validation

**Location:** `extract_transform.py` → `validate_weather()`

**What it checks**

Each of the four daily aggregated weather columns is checked against plausible bounds for Louisville, KY. These bounds apply to the daily values produced by `aggregate_hourly_to_daily()`, not to raw hourly readings.

| Column | Valid range | Units | Basis |
| --- | --- | --- | --- |
| `soil_temperature_0cm` | −10 to 130 | °F, daily mean | Louisville record low −22°F, record high 107°F with margin |
| `soil_moisture_0_to_1cm` | 0.0 to 1.0 | m³/m³, daily mean | Physically bounded — cannot exceed 1.0 |
| `evapotranspiration` | 0.0 to 1.5 | inches, daily total | Extreme Kentucky summer peak ~0.5 in/day |
| `precipitation` | 0.0 to 15.0 | inches, daily total | Louisville daily record ~10 inches |

**Why it matters**

An out-of-range value typically indicates a unit mismatch — for example, the API returning Celsius when Fahrenheit was requested. A soil temperature of 5 when 41°F was expected would trigger a Planting frost skip on a perfectly safe warm day. A soil moisture above 1.0 (physically impossible) would fire every waterlogging rule across all five tasks for every park on that date.

**What happens if it fails**

Values outside the valid range are clipped to the nearest bound using `pandas.Series.clip()`. A warning is logged with the column name and the count of affected rows. Clipping is used rather than dropping the row because a single anomalous value in one park does not invalidate the entire forecast.

```
WARNING — RANGE CHECK — 'soil_temperature_0cm': 1 row(s) outside [-10.0000, 130.0000]. Clipping.
```

---

## Validation Check 4 — Duplicate Detection

**Location:** `extract_transform.py` → `validate_weather()`

**What it checks**

The daily aggregated DataFrame is checked for rows where the combination of `park_id` and `forecast_date` appears more than once, using `pandas.DataFrame.duplicated(subset=["park_id", "forecast_date"])`.

**Why it matters**

Duplicates can arise if the API is called twice for the same park — for instance, if a retry partially succeeded and both attempts appended results. A duplicate `(park_id, forecast_date)` row causes two `maintenance_schedule` rows to be computed for the same park/task/date combination. When `load.py` tries to insert those rows, the second insert hits the `UNIQUE (park_id, task_id, schedule_date)` constraint on the `maintenance_schedule` table, which either raises an error or silently updates with stale data depending on conflict handling.

**What happens if it fails**

Duplicate rows are removed using `pandas.DataFrame.drop_duplicates(keep="last")`, retaining the most recently appended result. A warning is logged with the total count of rows removed.

```
WARNING — DUPLICATE CHECK — 4 duplicate (park_id, forecast_date) row(s). Keeping last occurrence.
```

---

## Validation Check 5 — Output Row Count Check

**Location:** `extract_transform.py` → `verify_output_row_count()`

**What it checks**

After all transformation and validation steps, the final daily DataFrame is checked to confirm it contains at least one row before being written to `weather_data.csv`.

**Why it matters**

An empty DataFrame at this point means either every single park fetch failed or the validation phase dropped every row. Writing an empty CSV does not raise an error — pandas produces a valid but header-only file. `load.py` would then read zero weather rows, `build_schedule()` would produce an empty schedule, and the pipeline would either fail the referential integrity check or complete silently with nothing inserted into the database.

**What happens if it fails**

A `RuntimeError` is raised immediately. `weather_data.csv` is not written. The pipeline halts with a clear message so the operator knows to investigate the fetch or transform phase.

```
RuntimeError: ROW COUNT CHECK — weather_data DataFrame is empty. No output file will be written.
```

---

## Validation Check 6 — JSON Schema Validation

**Location:** `load.py` → `read_tasks()`

**What it checks**

Each row in `seed_maintenance_tasks.csv` must have a `weather_constraints` value that parses successfully with `json.loads()`. The parsed object is immediately re-serialised with `json.dumps()` so only well-formed, normalised JSON reaches the database.

**Why it matters**

`weather_constraints` is stored as a `JSONB` column in PostgreSQL. A malformed string causes a database error at insert time. More critically, if the column is replaced with an empty dict during this check, `build_schedule()` evaluates zero rules for that task — every park on every day receives `recommended` regardless of weather conditions. Catching this at read time rather than at insert time produces a more actionable error message.

**What happens if it fails**

The affected row is kept and inserted, but its `weather_constraints` is replaced with a safe empty rule set:
`{"skip_if": [], "skip_if_recent": [], "defer_if_upcoming": [], "require": []}`. This prevents a database crash while preserving the task. An error is logged with the row index so the operator can correct `seed_maintenance_tasks.csv` and re-run.

```
ERROR — SCHEMA/TYPE VALIDATION — weather_constraints for task row 2 is invalid JSON: ...
```

---

## Validation Check 7 — Referential Integrity Check

**Location:** `load.py` → `check_referential_integrity()`

**What it checks**

Before upserting `weather_forecast` or `maintenance_schedule` rows, the function queries the `park` and `maintenance_task` tables that were just inserted and compares their primary key sets against the foreign key values in the two child DataFrames:

- Every `park_id` in `weather_df` must exist in `public.park`.
- Every `park_id` in `schedule_df` must exist in `public.park`.
- Every `task_id` in `schedule_df` must exist in `public.maintenance_task`.

**Why it matters**

PostgreSQL enforces foreign key constraints at insert time. An orphaned `park_id` fails the entire transaction with a generic integrity error that does not identify which ID was missing or how many rows were affected. Running this check in Python first produces a precise, actionable error message and prevents a partial-load scenario where some rows committed and others did not.

**What happens if it fails**

A `RuntimeError` is raised with the specific missing IDs before any child rows are inserted. The pipeline halts. No data is written to `weather_forecast` or `maintenance_schedule`.

```
RuntimeError: REFERENTIAL INTEGRITY — 1 park_id(s) in weather_data not found in park table: {5}
```

---

## Validation Check 8 — Post-Load Row Count Check

**Location:** `load.py` → `verify_db_row_counts()`

**What it checks**

After all four tables have been loaded, the function issues `SELECT COUNT(*)` against each table and compares the result to the number of rows in the source DataFrame:

| Table | Expected count source |
| --- | --- |
| `park` | `len(parks_df)` |
| `maintenance_task` | `len(tasks_df)` |
| `weather_forecast` | `len(weather_df)` after `park_name` drop |
| `maintenance_schedule` | `len(schedule_df)` |

**Why it matters**

SQLAlchemy's `to_sql()` and the manual upsert loops do not raise exceptions when individual rows are silently skipped by the database. A row count mismatch after loading is the only reliable way to detect a partial load without inspecting every individual row. A partial `maintenance_schedule` leaves some park/task combinations with no rows — those combinations produce no recommendation output at all, and no error is surfaced without this check.

**What happens if it fails**

A `RuntimeError` is raised with the table name, the expected count, and the actual count found in the database. The pipeline halts so the operator can investigate before the data is used.

```
RuntimeError: ROW COUNT CHECK — table 'maintenance_schedule': expected 640 rows, found 600.
```

---

## Summary

| Check | Severity | Pipeline behaviour |
| --- | --- | --- |
| 1 — API response validation | Per-park error | Skip that park; continue with others |
| 2 — Null value check | Warning | Fill with median; continue |
| 3 — Range validation | Warning | Clip to bounds; continue |
| 4 — Duplicate detection | Warning | Drop duplicates; continue |
| 5 — Output row count check | Fatal | Halt; do not write CSV |
| 6 — JSON schema validation | Per-row error | Replace with empty rule set; continue |
| 7 — Referential integrity check | Fatal | Halt; do not insert child rows |
| 8 — Post-load row count check | Fatal | Halt; alert operator |

Checks 1–4 and 6 are resilient — they log the problem, repair what can be repaired, and continue so that a single bad park or a single malformed task row does not abort an otherwise healthy run. Checks 5, 7, and 8 are fatal because the problems they detect cannot be automatically recovered from: an empty output file, an orphaned foreign key, or a count mismatch all require human investigation before the data can be trusted.
