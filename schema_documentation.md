# Database Schema Documentation

## Louisville Urban Garden & Park Maintenance Scheduler

This database stores Louisville park information, daily weather values derived from hourly Open-Meteo API forecasts, maintenance task definitions with structured JSON constraint rules, and a derived maintenance schedule that recommends, defers, or skips each task per park per day based on current weather conditions.

The schema is normalized to approximately Third Normal Form (3NF) by:

- separating parks, tasks, forecasts, and schedule decisions into distinct tables
- avoiding repeated data across rows
- storing weather constraint rules as structured JSONB in the task table rather than hardcoding them in Python

---

### ER Diagram

![Park Maintenance ERD](park_maintenance_ERD.png)

---

## Database Overview

The database contains four tables:

- `park`
- `maintenance_task`
- `weather_forecast`
- `maintenance_schedule`

### Entity Relationship Summary

| Table | Purpose |
| --- | --- |
| `park` | Stores Louisville green space metadata and coordinates |
| `maintenance_task` | Stores maintenance tasks and their structured JSON weather constraint rules |
| `weather_forecast` | Stores daily aggregated weather values derived from hourly Open-Meteo data |
| `maintenance_schedule` | Derived table — daily recommendation for every park and task combination |

---

## Table Documentation

### 1. `park`

**Purpose**

Stores static metadata for each Louisville park. The latitude and longitude columns are used by `extract_transform.py` to call the Open-Meteo API with park-specific coordinates so that micro-climate differences across Louisville are captured.

**Seeded from**

`data/seed_parks.csv`

**Examples**

- Cherokee Park — East Louisville, 1,214,000 m², Regional Park
- Waterfront Park — Downtown, 121,400 m², Waterfront Park
- Tyler Park — Highlands, 48,600 m², Neighborhood Park

**Primary Key**

- `park_id`

**Relationships**

- One `park` can relate to many `weather_forecast` records.
- One `park` can relate to many `maintenance_schedule` records.
- Referenced by `weather_forecast.park_id` and `maintenance_schedule.park_id`.

**Table Structure**

| Column Name | Data Type | Key | Description |
| --- | --- | --- | --- |
| `park_id` | `SERIAL` | Primary Key | Auto-incremented integer assigned by PostgreSQL |
| `name` | `TEXT` | | Common park name (e.g. Cherokee Park) |
| `location` | `TEXT` | | Louisville neighbourhood or district (e.g. East Louisville) |
| `area_size` | `INTEGER` | | Park area in square metres |
| `park_type` | `TEXT` | | Classification (e.g. Regional Park, Neighborhood Park, Community Park) |
| `latitude` | `DOUBLE PRECISION` | | WGS-84 latitude used for Open-Meteo API calls |
| `longitude` | `DOUBLE PRECISION` | | WGS-84 longitude used for Open-Meteo API calls |

**Note on park IDs**

`extract_transform.py` assigns each park a temporary integer ID (1-based row position) and writes it into `weather_data.csv`. `load.py` reads that file first and derives park IDs from it by joining on park name — not by re-reading the CSV row order. This guarantees that the `park_id` values in `weather_forecast` and `maintenance_schedule` always match the `park_id` values PostgreSQL assigns via `SERIAL`, regardless of any future changes to row order in `seed_parks.csv`.

---

### 2. `maintenance_task`

**Purpose**

Stores each maintenance task along with its weather decision rules as structured JSONB. The constraint rules are read by `load.py` at runtime when computing the maintenance schedule — no thresholds or conditions are hardcoded in Python. Updating the schedule logic only requires editing `seed_maintenance_tasks.csv` and re-running the pipeline.

**Seeded from**

`data/seed_maintenance_tasks.csv`

**Tasks loaded**

- Mowing — skip when soil is waterlogged, soil is too cold, ET0 is near zero, or rain fell recently
- Irrigation — skip when soil moisture is adequate, rain fell recently, ET0 is too low, or rain is forecast tomorrow
- Fertilizing — skip when soil is saturated, soil is too cold, or rain is forecast within 48 hours
- Planting — skip when soil is waterlogged, soil is too cold, or transplant stress risk is high
- Pruning — skip when soil is too soft, rain fell recently, frost is forecast, or high ET0 stress is forecast

**Primary Key**

- `task_id`

**Relationships**

- One `maintenance_task` can relate to many `maintenance_schedule` records.
- Referenced by `maintenance_schedule.task_id`.

**Constraints**

- `task_name` is unique.

**Table Structure**

| Column Name | Data Type | Key | Description |
| --- | --- | --- | --- |
| `task_id` | `SERIAL` | Primary Key | Auto-incremented integer assigned by PostgreSQL |
| `task_name` | `TEXT` | Unique | Display name of the task (e.g. Mowing, Irrigation) |
| `category` | `TEXT` | | Task grouping: vegetation, water, or soil |
| `description` | `TEXT` | | Plain-English description of the task |
| `frequency_days` | `INTEGER` | | Recommended recurrence in days; NULL for event-driven tasks such as Planting |
| `weather_constraints` | `JSONB` | | Structured rule engine — four arrays of condition objects |

**`weather_constraints` JSONB structure**

The column stores four arrays of rule objects evaluated in priority order. `skipped` takes priority over `deferred`, which takes priority over `recommended`.

| Key | Evaluation | Effect when matched |
| --- | --- | --- |
| `skip_if` | A condition is true today | Task is skipped |
| `skip_if_recent` | A condition was true within `lookback_hours` | Task is skipped |
| `require` | A required positive condition is NOT met today | Task is skipped |
| `defer_if_upcoming` | A condition is forecast within `lookahead_hours` | Task is deferred |

Each rule object contains:

- `field` — the weather variable to check: `soil_temperature_0cm`, `soil_moisture_0_to_1cm`, `evapotranspiration`, or `precipitation`
- `operator` — comparison operator: `gt`, `gte`, `lt`, `lte`, or `eq`
- `value` — the numeric threshold to compare against
- `reason` — human-readable explanation written directly into `maintenance_schedule.reason`
- `lookback_hours` — hours of history to check (`skip_if_recent` only; converted to days for daily-row lookup)
- `lookahead_hours` — hours of forecast to check (`defer_if_upcoming` only; converted to days for daily-row lookup)

---

### 3. `weather_forecast`

**Purpose**

Stores daily weather values for each park derived from hourly Open-Meteo API data. `extract_transform.py` fetches 16 days of hourly data per park and aggregates: soil temperature and soil moisture are averaged across the day (they are point measurements); evapotranspiration and precipitation are summed (they are accumulations). One row is created per park per forecast date.

**Populated from**

`data/weather_data.csv` (produced by `extract_transform.py`)

**Weather variables stored**

- Daily mean soil surface temperature in Fahrenheit
- Daily mean volumetric soil moisture at 0–1 cm depth (m³/m³)
- Daily total evapotranspiration in inches
- Daily total precipitation in inches

**Primary Key**

- `forecast_id`

**Foreign Keys**

- `park_id` → `park.park_id`

**Relationships**

- Many `weather_forecast` rows can reference one `park`.
- Each combination of park and forecast date is unique.

**Constraints**

- Unique combination of `park_id` and `forecast_date`.

**Table Structure**

| Column Name | Data Type | Key | Description |
| --- | --- | --- | --- |
| `forecast_id` | `BIGSERIAL` | Primary Key | Auto-generated forecast record ID |
| `park_id` | `INTEGER` | Foreign Key | The park this forecast row applies to |
| `forecast_date` | `DATE` | | Calendar date of the forecast |
| `soil_temperature_0cm` | `DOUBLE PRECISION` | | Daily mean soil surface temperature (°F) |
| `soil_moisture_0_to_1cm` | `DOUBLE PRECISION` | | Daily mean volumetric soil moisture, 0–1 cm depth (m³/m³) |
| `evapotranspiration` | `DOUBLE PRECISION` | | Daily total evapotranspiration (inches) |
| `precipitation` | `DOUBLE PRECISION` | | Daily total precipitation (inches) |
| `created_at` | `TIMESTAMPTZ` | | Row insertion timestamp, defaults to `NOW()` |

---

### 4. `maintenance_schedule`

**Purpose**

Derived table storing the daily maintenance recommendation for every combination of park, task, and forecast date. Computed by `load.py` by evaluating each task's `weather_constraints` rules against the corresponding `weather_forecast` row. Each row records the outcome and the human-readable reason text taken directly from the matching constraint rule in `seed_maintenance_tasks.csv`.

**Populated by**

`load.py` constraint evaluation engine

**Recommendation values**

- `recommended` — no skip or defer conditions triggered; the task is appropriate today
- `deferred` — no skip conditions triggered but adverse weather is forecast within the lookahead window
- `skipped` — current, recent, or forecast conditions make the task inadvisable today

**Primary Key**

- `schedule_id`

**Foreign Keys**

- `park_id` → `park.park_id`
- `task_id` → `maintenance_task.task_id`

**Relationships**

- Many `maintenance_schedule` rows can reference one `park`.
- Many `maintenance_schedule` rows can reference one `maintenance_task`.
- Each combination of park, task, and schedule date is unique.

**Constraints**

- Unique combination of `park_id`, `task_id`, and `schedule_date`.
- `recommendation` is restricted to `recommended`, `deferred`, or `skipped`.

**Table Structure**

| Column Name | Data Type | Key | Description |
| --- | --- | --- | --- |
| `schedule_id` | `BIGSERIAL` | Primary Key | Auto-generated schedule record ID |
| `park_id` | `INTEGER` | Foreign Key | The park this schedule row applies to |
| `task_id` | `INTEGER` | Foreign Key | The task this schedule row applies to |
| `schedule_date` | `DATE` | | The forecast date being evaluated |
| `recommendation` | `TEXT` | | Decision output: `recommended`, `deferred`, or `skipped` |
| `reason` | `TEXT` | | Human-readable explanation from the matched constraint rule |
| `created_at` | `TIMESTAMPTZ` | | Row insertion timestamp, defaults to `NOW()` |

---

## Cardinality Relationships

| Parent Table | Child Table | Relationship Type |
| --- | --- | --- |
| `park` | `weather_forecast` | One-to-Many |
| `park` | `maintenance_schedule` | One-to-Many |
| `maintenance_task` | `maintenance_schedule` | One-to-Many |
| `park` + `maintenance_task` | Through `maintenance_schedule` | Many-to-Many |

---

## Normalization Notes (3NF)

This schema is normalized to approximately Third Normal Form:

- Park coordinates and metadata are separated into `park` rather than repeated on every forecast row.
- Weather constraint rules are separated into `maintenance_task` as JSONB rather than duplicated per schedule row.
- The many-to-many relationship between parks and tasks is resolved through `maintenance_schedule`.
- Non-key columns depend only on each table's primary key.

---

## Data Flow

```
data/seed_parks.csv
        │
        ▼
extract_transform.py
    Assigns park_id = row index + 1 (single authority for all IDs)
    Calls Open-Meteo API — one hourly request per park
    Fetches: soil_temperature_0cm, soil_moisture_0_to_1cm,
             evapotranspiration, precipitation
    Aggregates hourly → daily (mean for soil vars, sum for ET and precip)
    Validates: null check, range check, duplicate check, row count check
    Writes park_id + park_name + forecast_date + 4 weather columns
    → data/weather_data.csv
            │
            ▼
data/weather_data.csv ───────────────────────────────────────┐
data/seed_parks.csv ─────────────────────────────────────────┤
data/seed_maintenance_tasks.csv ─────────────────────────────┤
                                                             │
                                                             ▼
                                                       load.py
                                    Reads weather_data.csv first (IDs are authoritative)
                                    Joins seed_parks.csv on name to get park_id
                                    Validates JSON constraints in tasks CSV
                                    Evaluates constraint rules → compute schedule
                                    Creates schema in Supabase
                                    INSERT → public.park
                                    INSERT → public.maintenance_task
                                    Referential integrity check
                                    INSERT → public.weather_forecast
                                    INSERT → public.maintenance_schedule
                                    Post-load row count check
```

---

## Example Relationship Flow

- `maintenance_schedule.park_id = 1` → `park.name = 'Cherokee Park'`, `park.location = 'East Louisville'`
- `maintenance_schedule.task_id = 1` → `maintenance_task.task_name = 'Mowing'`
- `maintenance_schedule.schedule_date = 2025-06-01`
- Matching `weather_forecast` row: `soil_moisture_0_to_1cm = 0.43`
- Constraint evaluated: `skip_if soil_moisture_0_to_1cm gt 0.4` → true
- Result stored: `recommendation = 'skipped'`
- `reason = 'Soil waterlogged — mowing causes compaction and ruts'`

The reason text comes directly from the constraint rule in `seed_maintenance_tasks.csv`. No hardcoded strings exist anywhere in the Python code.
