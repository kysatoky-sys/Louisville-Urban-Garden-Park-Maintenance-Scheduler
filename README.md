# Louisville Urban Garden & Park Maintenance Scheduler

An ETL pipeline that pulls daily weather data from Open-Meteo for 8 Louisville,
KY parks, applies data quality checks, and generates a daily maintenance schedule
by evaluating task-specific weather constraints stored declaratively in the database.

---

## Repository structure

```
louisville-garden-scheduler/
├── schema.sql                  ← Run this first in Supabase SQL editor
├── load.py                     ← Seed loader: parks + maintenance_tasks CSVs → DB
├── etl.py                      ← Main ETL: Open-Meteo → weather_observations → maintenance_schedule
├── seed_parks.csv              ← 8 Louisville parks with coordinates
├── seed_maintenance_tasks.csv  ← 5 tasks with declarative weather constraints
├── requirements.txt
├── .env.example                ← Copy to .env and fill in credentials
├── .gitignore
└── README.md
```

---

## Quick-start

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd louisville-garden-scheduler

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up credentials
cp .env.example .env
# Edit .env and fill in SUPABASE_URL, SUPABASE_KEY, DATABASE_URL
# (see "Credentials" section below)

# 5. Apply the schema to your Supabase project
#    Open the Supabase SQL editor and paste + run schema.sql

# 6. Seed reference data (parks + tasks)
python load.py

# 7. Run the ETL (defaults to yesterday → today)
python etl.py

# Optional: specify a custom date range
python etl.py --start 2025-05-01 --end 2025-05-07
```

---

## Credentials

You need two things from your Supabase project dashboard.

| Variable | Where to find it | Used by |
|---|---|---|
| `SUPABASE_URL` | Project Settings → API → Project URL | `load.py` |
| `SUPABASE_KEY` | Project Settings → API → service_role key | `load.py` |
| `DATABASE_URL` | Project Settings → Database → Connection string → URI | `etl.py` |

Use the **service_role** key (not anon) — the loader needs INSERT/UPSERT.

---

## Pipeline overview

```
Open-Meteo Archive API
        │
        │  (per-park HTTP request, with retry)
        ▼
   EXTRACT stage
   • Fetch 6 daily variables for each active park
   • Validate API response structure (check #1)
        │
        ▼
   TRANSFORM stage
   • Null-value check           (check #2)
   • Range validation + clamp   (check #3)
   • Duplicate detection        (check #4)
   • Row-count verification     (check #5)
   • Schema/type validation     (check #7)
   • Derive irrigation_needed flag
        │
        ▼
  LOAD: weather_observations
   • Referential integrity check (check #6)
   • Upsert via SQLAlchemy (ON CONFLICT → update)
        │
        ▼
  LOAD: maintenance_schedule
   • Read weather_constraints from maintenance_tasks (no hardcoded logic)
   • Evaluate skip_if / require / skip_if_recent / defer_if_upcoming rules
   • Upsert recommendation + audit trail per park × task × date
```

---

## Data quality & validation framework

| # | Check | What it validates | Why it matters | On failure |
|---|---|---|---|---|
| 1 | API response validation | HTTP 2xx, valid JSON, `daily` key present, all 6 variables in payload | A silent API change would produce a corrupt or empty observation row | Raises `ValueError`; park skipped for this run |
| 2 | Null value check | Every Open-Meteo variable has a non-null value for each date | Null weather fields silently skip constraint rules — mowing could be scheduled on waterlogged ground | Warning logged; row inserted with NULL; constraint engine skips rules whose field is missing |
| 3 | Range validation | Each variable stays within Louisville-specific physical bounds | Outliers (e.g. 9999 mm rain) would permanently block tasks | Error logged; value clamped to nearest valid boundary; pipeline continues |
| 4 | Duplicate detection | Each (park_id, observation_date) is unique within the API batch | Duplicates indicate pagination bug or date-range overlap; DB unique constraint would abort the batch | Error logged; last occurrence kept |
| 5 | Row count verification | API returned one row per calendar day in the requested range | Silently dropped dates leave gaps in the schedule | Warning logged with expected vs actual count; pipeline continues with partial data |
| 6 | Referential integrity | Every park_id exists in the `parks` table before inserting into `weather_observations` | FK violation aborts the entire batch | Error logged; affected parks excluded from load |
| 7 | Schema/type validation | Numeric fields are Python `float`/`int`; `observation_date` is valid ISO-8601 | Type mismatch causes cryptic insert errors | Error logged per field; row still passed to DB for final type enforcement |

---

## Open-Meteo variables

| Variable | Unit | Role in pipeline |
|---|---|---|
| `soil_moisture_0_to_1cm` | m³/m³ | Waterlogging detection for mowing, planting |
| `soil_temperature_0cm` | °C | Cold-soil guard for planting |
| `precipitation_sum` | mm | Skip irrigation; defer fertilising |
| `temperature_2m_max` | °C | Heat guard; fertiliser activation |
| `temperature_2m_min` | °C | Frost guard for planting and pruning |
| `et0_fao_evapotranspiration` | mm | Irrigation trigger (ET₀ ≥ 2 mm and precip < 3 mm) |

---

## Weather constraint schema

Constraints live in `maintenance_tasks.weather_constraints` (JSONB).
The ETL reads them at runtime — no thresholds are hardcoded in Python.
Adding or changing a rule is a database update, not a code deployment.

```jsonc
{
  "skip_if":           [{ "field": "...", "operator": "gt|gte|lt|lte|eq", "value": 0.4, "reason": "..." }],
  "require":           [{ "field": "...", "operator": "gte", "value": 10, "reason": "..." }],
  "skip_if_recent":    [{ "field": "...", "operator": "gt", "value": 8, "lookback_days": 1, "reason": "..." }],
  "defer_if_upcoming": [{ "field": "...", "operator": "gt", "value": 3, "lookahead_days": 2, "reason": "..." }]
}
```

---

## Re-running safely

Both scripts are fully idempotent:

- `load.py` — uses `ON CONFLICT (id)` for parks and `ON CONFLICT (task_name)` for tasks.
- `etl.py` — uses `ON CONFLICT (park_id, observation_date)` for weather observations and
  `ON CONFLICT (park_id, task_id, scheduled_date)` for the schedule.

Re-running with the same date range overwrites existing rows with fresh data.

---

## Dash Dashboard

`app.py` is a four-page Dash application that reads live data from the same Supabase PostgreSQL database populated by `load.py`.

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:8050
```

Uses the same `.env` credentials as `load.py` (`SUPABASE_DB_URL` or `DB_PASSWORD` + `DB_REF`).

### Pages

| Page | What it shows |
|---|---|
| **Today's Schedule** | Recommendation cards (recommended / deferred / skipped) for every park × task combination for today. Filterable by recommendation type. |
| **Weather Forecast** | Four charts per park: soil temperature, soil moisture (with waterlogging threshold line at 0.4 m³/m³), daily precipitation, and evapotranspiration. Park selector dropdown. |
| **Task Explorer** | Full schedule table and daily stacked bar chart, filterable by park, task, and date range. Colour-coded by recommendation. |
| **Park Overview** | Summary stats table for all parks, a donut chart of the overall recommendation mix, and a stacked bar breakdown by task. |

### Architecture

```
Supabase PostgreSQL
        │
        │  SQLAlchemy (read-only queries)
        ▼
     app.py
        │
        ├── Page: Today's Schedule   (public.maintenance_schedule JOIN park JOIN maintenance_task)
        ├── Page: Weather Forecast   (public.weather_forecast JOIN park)
        ├── Page: Task Explorer      (same as Schedule, with date/park/task filters)
        └── Page: Park Overview      (public.park + aggregated schedule counts)
```
