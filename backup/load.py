"""
Load — Louisville Urban Garden & Park Maintenance Scheduler
------------------------------------------------------------
Reads weather_data.csv (produced by extract_transform.py), seed_parks.csv,
and seed_maintenance_tasks.csv from the data/ directory, evaluates each
task's weather constraint rules against the daily weather data to compute a
maintenance schedule, then creates the database schema and loads all tables.

Reads from data/:
    weather_data.csv              (produced by extract_transform.py)
    seed_parks.csv                (park names, coordinates, location, area, type)
    seed_maintenance_tasks.csv    (task definitions and JSON constraint rules)

Loads into Supabase PostgreSQL:
    public.park
    public.maintenance_task
    public.weather_forecast
    public.maintenance_schedule

Required packages:
    pip install pandas sqlalchemy psycopg2-binary python-dotenv

.env values expected:
    DB_PASSWORD=your_supabase_database_password
    DB_REF=your_supabase_project_ref

Optional:
    SUPABASE_DB_URL=postgresql+psycopg2://...
    RESET_TABLES=true
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.types import Float, Integer, Text


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

SEED_PARKS_CSV   = DATA_DIR / "seed_parks.csv"
SEED_TASKS_CSV   = DATA_DIR / "seed_maintenance_tasks.csv"
WEATHER_DATA_CSV = DATA_DIR / "weather_data.csv"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "etl.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("load")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

# This helper builds the database connection URL from environment variables.
# It reads credentials from a .env file and supports a direct SUPABASE_DB_URL override.
def get_database_url() -> str:
    load_dotenv()

    database_url = os.getenv("SUPABASE_DB_URL")
    if database_url:
        return database_url

    password = os.getenv("DB_PASSWORD")
    db_ref   = os.getenv("DB_REF")

    if not password or not db_ref:
        raise RuntimeError(
            "Set SUPABASE_DB_URL, or set both DB_PASSWORD and DB_REF in your .env file."
        )

    return (
        "postgresql+psycopg2://"
        f"postgres:{password}"
        f"@db.{db_ref}.supabase.co:5432/postgres"
    )


def table_reset_enabled() -> bool:
    # Allow users to choose whether to drop existing tables before loading data.
    # Useful when a clean import is needed instead of appending to old data.
    return os.getenv("RESET_TABLES", "true").strip().lower() in {"1", "true", "yes", "y"}


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

# This function defines the four tables and their relationships.
# It optionally drops existing tables and recreates the schema from scratch.
def create_schema(engine) -> None:
    drop_sql = """
    DROP TABLE IF EXISTS public.maintenance_schedule CASCADE;
    DROP TABLE IF EXISTS public.weather_forecast CASCADE;
    DROP TABLE IF EXISTS public.maintenance_task CASCADE;
    DROP TABLE IF EXISTS public.park CASCADE;
    """

    create_sql = """
    CREATE TABLE IF NOT EXISTS public.park (
        park_id   SERIAL PRIMARY KEY,
        name      TEXT NOT NULL,
        location  TEXT NOT NULL,
        area_size INTEGER,
        park_type TEXT NOT NULL,
        latitude  DOUBLE PRECISION NOT NULL,
        longitude DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS public.maintenance_task (
        task_id             SERIAL PRIMARY KEY,
        task_name           TEXT NOT NULL UNIQUE,
        category            TEXT NOT NULL,
        description         TEXT,
        frequency_days      INTEGER,
        weather_constraints JSONB NOT NULL
    );

    CREATE TABLE IF NOT EXISTS public.weather_forecast (
        forecast_id            BIGSERIAL PRIMARY KEY,
        park_id                INTEGER NOT NULL REFERENCES public.park(park_id),
        forecast_date          DATE NOT NULL,
        soil_temperature_0cm   DOUBLE PRECISION,
        soil_moisture_0_to_1cm DOUBLE PRECISION,
        evapotranspiration     DOUBLE PRECISION,
        precipitation          DOUBLE PRECISION,
        created_at             TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (park_id, forecast_date)
    );

    CREATE TABLE IF NOT EXISTS public.maintenance_schedule (
        schedule_id    BIGSERIAL PRIMARY KEY,
        park_id        INTEGER NOT NULL REFERENCES public.park(park_id),
        task_id        INTEGER NOT NULL REFERENCES public.maintenance_task(task_id),
        schedule_date  DATE NOT NULL,
        recommendation TEXT NOT NULL CHECK (recommendation IN ('recommended', 'deferred', 'skipped')),
        reason         TEXT,
        created_at     TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (park_id, task_id, schedule_date)
    );
    """

    with engine.begin() as conn:
        if table_reset_enabled():
            logger.info("RESET_TABLES=true — dropping existing tables...")
            conn.execute(text(drop_sql))
        conn.execute(text(create_sql))

    logger.info("Schema created successfully.")


# ---------------------------------------------------------------------------
# CSV readers
# ---------------------------------------------------------------------------

# Reads and cleans seed_parks.csv, then assigns each park the same park_id
# that extract_transform.py stamped onto that park's rows in weather_data.csv.
#
# The join is on park name — not on row position — so the park_id values in
# parks_df are guaranteed to match the park_id values already in weather_df,
# regardless of row order in the seed file.
#
# Why this matters: both scripts independently read seed_parks.csv. If either
# script derives park_id by row position (index + 1) without coordinating with
# the other, a reordered or edited CSV would cause weather data for one park to
# be stored and queried under a different park's ID — silently, with no error.
#
# park_id is used here during schedule computation and as the FK when inserting
# weather_forecast and maintenance_schedule rows. It is dropped before the park
# table insert so PostgreSQL's SERIAL column auto-assigns the same values.
def read_parks(weather_df: pd.DataFrame) -> pd.DataFrame:
    df = pd.read_csv(SEED_PARKS_CSV)
    df["name"]      = df["name"].astype(str).str.strip()
    df["location"]  = df["location"].astype(str).str.strip()
    df["park_type"] = df["park_type"].astype(str).str.strip()
    df["area_size"] = pd.to_numeric(df["area_size"], errors="coerce")
    df["latitude"]  = pd.to_numeric(df["latitude"],  errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    # Build a name → park_id lookup from weather_data.csv.
    # extract_transform.py wrote the park_id into every weather row, so this
    # lookup is the authoritative source of which ID belongs to which park.
    id_lookup = (
        weather_df[["park_id", "park_name"]]
        .drop_duplicates()
        .rename(columns={"park_name": "name"})
    )
    df = df.merge(id_lookup, on="name", how="left")

    # Warn about any parks in the seed file that have no weather data.
    # This can happen legitimately if a park's API fetch failed entirely.
    missing_id = df["park_id"].isna()
    if missing_id.any():
        logger.warning(
            "No weather data found for %d park(s): %s. "
            "These parks will be inserted into the park table but will have "
            "no weather_forecast or maintenance_schedule rows.",
            missing_id.sum(),
            df.loc[missing_id, "name"].tolist(),
        )
        # Assign placeholder IDs for parks with no weather so the table insert
        # still works. The max existing ID is used as the base to avoid clashes.
        max_id = int(df["park_id"].max(skipna=True) or 0)
        placeholder_ids = range(max_id + 1, max_id + 1 + missing_id.sum())
        df.loc[missing_id, "park_id"] = list(placeholder_ids)

    df["park_id"] = df["park_id"].astype(int)
    logger.info("Read %d parks from %s.", len(df), SEED_PARKS_CSV.name)
    return df


# Reads and cleans seed_maintenance_tasks.csv.
# task_id is assigned here (1-based index) for use in schedule computation.
# It is dropped before the DB insert because the SERIAL column auto-populates.
# frequency_days is 'null' as a string for Planting — to_numeric coerces to NaN.
def read_tasks() -> pd.DataFrame:
    df = pd.read_csv(SEED_TASKS_CSV)
    df = df.reset_index(drop=True)
    df.insert(0, "task_id", df.index + 1)
    df["task_name"]      = df["task_name"].astype(str).str.strip()
    df["category"]       = df["category"].astype(str).str.strip()
    df["description"]    = df["description"].astype(str).str.strip()
    df["frequency_days"] = pd.to_numeric(df["frequency_days"], errors="coerce")

    # Validation check 1 — Schema/type validation on weather_constraints
    # Each task row must have a weather_constraints value that parses as JSON.
    # Why it matters: malformed JSON cannot be stored as JSONB in PostgreSQL and
    # would cause the constraint evaluator to skip all rules for that task,
    # producing incorrect recommendations for every park on every day.
    # What happens if this fails: the row is kept with an empty rule set so it
    # can still be inserted; the error is logged with the row index.
    validated: list[str] = []
    for idx, raw in df["weather_constraints"].items():
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            validated.append(json.dumps(parsed))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error(
                "SCHEMA/TYPE VALIDATION — weather_constraints for task row %d "
                "is invalid JSON: %s. Replacing with empty rule set.",
                idx, exc,
            )
            validated.append(json.dumps(
                {"skip_if": [], "skip_if_recent": [], "defer_if_upcoming": [], "require": []}
            ))
    df["weather_constraints"] = validated
    logger.info("Read %d tasks from %s.", len(df), SEED_TASKS_CSV.name)
    return df


# Reads weather_data.csv produced by extract_transform.py.
# park_name is kept here because read_parks() uses it to join park IDs.
# It is dropped later, just before the weather_forecast insert, because
# the weather_forecast table has no park_name column.
# forecast_date is cast to a Python date object (pandas reads CSV dates as strings).
def read_weather_data() -> pd.DataFrame:
    df = pd.read_csv(WEATHER_DATA_CSV)
    df["park_id"]       = pd.to_numeric(df["park_id"], errors="coerce").astype(int)
    df["forecast_date"] = pd.to_datetime(df["forecast_date"]).dt.date
    logger.info(
        "Read %d weather rows from %s (%d parks).",
        len(df), WEATHER_DATA_CSV.name, df["park_id"].nunique(),
    )
    return df


# ---------------------------------------------------------------------------
# Constraint evaluation engine
# ---------------------------------------------------------------------------

# Evaluates a single comparison between a daily weather value and a threshold.
# Returns False when the actual value is null so missing data never triggers a skip.
def _compare(actual: float | None, operator: str, threshold: float) -> bool:
    if actual is None or pd.isna(actual):
        return False
    ops = {
        "gt":  lambda a, b: a > b,
        "gte": lambda a, b: a >= b,
        "lt":  lambda a, b: a < b,
        "lte": lambda a, b: a <= b,
        "eq":  lambda a, b: a == b,
    }
    fn = ops.get(operator)
    if fn is None:
        logger.warning(
            "Unknown operator '%s' in weather_constraints — skipping rule.", operator
        )
        return False
    return fn(actual, threshold)


# Evaluates all four constraint rule types for one park/task/date combination.
# Rules are checked in priority order: skipped > deferred > recommended.
# skip_if_recent and defer_if_upcoming use lookback_hours / lookahead_hours
# which are converted to whole days for lookup against the daily weather table.
# Returns a (recommendation, reason) tuple.
def evaluate_constraints(
    constraints: dict[str, Any],
    today_row: pd.Series,
    weather_df: pd.DataFrame,
    park_id: int,
    target_date: date,
) -> tuple[str, str]:
    park_weather = weather_df[weather_df["park_id"] == park_id].copy()
    park_weather["forecast_date"] = pd.to_datetime(park_weather["forecast_date"]).dt.date

    # skip_if — skip when today's aggregated daily value meets the condition
    for rule in constraints.get("skip_if", []):
        if _compare(today_row.get(rule["field"]), rule["operator"], rule["value"]):
            return "skipped", rule.get("reason", "Condition met: skip_if")

    # skip_if_recent — skip when a condition was true within the lookback window.
    # lookback_hours is converted to whole days (ceiling) for daily-row lookup.
    for rule in constraints.get("skip_if_recent", []):
        lookback_hours = rule.get("lookback_hours", 24)
        lookback_days  = max(1, -(-lookback_hours // 24))   # ceiling division
        for offset in range(1, lookback_days + 1):
            past_rows = park_weather[
                park_weather["forecast_date"] == (target_date - timedelta(days=offset))
            ]
            if not past_rows.empty and _compare(
                past_rows.iloc[0].get(rule["field"]), rule["operator"], rule["value"]
            ):
                return (
                    "skipped",
                    f"(Past {offset}d) {rule.get('reason', 'Condition met: skip_if_recent')}",
                )

    # require — skip when a required positive condition is NOT met today
    for rule in constraints.get("require", []):
        if not _compare(today_row.get(rule["field"]), rule["operator"], rule["value"]):
            return (
                "skipped",
                f"Requirement not met: {rule.get('reason', 'require condition failed')}",
            )

    # defer_if_upcoming — defer when adverse weather is forecast in the lookahead window.
    # lookahead_hours converted to whole days (ceiling) for daily-row lookup.
    for rule in constraints.get("defer_if_upcoming", []):
        lookahead_hours = rule.get("lookahead_hours", 24)
        lookahead_days  = max(1, -(-lookahead_hours // 24))
        for offset in range(1, lookahead_days + 1):
            future_rows = park_weather[
                park_weather["forecast_date"] == (target_date + timedelta(days=offset))
            ]
            if not future_rows.empty and _compare(
                future_rows.iloc[0].get(rule["field"]), rule["operator"], rule["value"]
            ):
                return (
                    "deferred",
                    f"(In {offset}d) {rule.get('reason', 'Condition met: defer_if_upcoming')}",
                )

    return "recommended", "All weather conditions are favorable."


# ---------------------------------------------------------------------------
# Schedule computation
# ---------------------------------------------------------------------------

# Crosses all parks × tasks × forecast dates.
# For each combination, reads the constraint rules from the task row,
# evaluates them against the daily weather data, and records the
# recommendation with its reason.
def build_schedule(
    weather_df: pd.DataFrame,
    tasks_df: pd.DataFrame,
    parks_df: pd.DataFrame,
) -> pd.DataFrame:
    all_park_ids = set(parks_df["park_id"].astype(int))
    weather_df = weather_df.copy()
    weather_df["forecast_date"] = pd.to_datetime(weather_df["forecast_date"]).dt.date

    records: list[dict] = []
    counts = {"recommended": 0, "deferred": 0, "skipped": 0}

    for park_id in all_park_ids:
        park_wx = weather_df[weather_df["park_id"] == park_id]
        if park_wx.empty:
            logger.warning(
                "No weather rows for park_id=%s — skipping schedule.", park_id
            )
            continue

        for _, task in tasks_df.iterrows():
            constraints = task["weather_constraints"]
            if isinstance(constraints, str):
                try:
                    constraints = json.loads(constraints)
                except json.JSONDecodeError:
                    logger.error(
                        "Failed to parse weather_constraints for task '%s'. "
                        "Defaulting all days to recommended.",
                        task["task_name"],
                    )
                    constraints = {}

            for _, today_row in park_wx.iterrows():
                target_date = today_row["forecast_date"]
                recommendation, reason = evaluate_constraints(
                    constraints, today_row, weather_df, park_id, target_date
                )
                records.append({
                    "park_id":        park_id,
                    "task_id":        task["task_id"],
                    "schedule_date":  target_date,
                    "recommendation": recommendation,
                    "reason":         reason,
                })
                counts[recommendation] += 1

    logger.info(
        "Schedule computed: %d recommended | %d deferred | %d skipped",
        counts["recommended"], counts["deferred"], counts["skipped"],
    )
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

# Writes a DataFrame to the target PostgreSQL table using SQLAlchemy.
def write_table(df: pd.DataFrame, table_name: str, engine, dtype: dict) -> None:
    logger.info("Loading %s (%d rows)...", table_name, len(df))
    df.to_sql(
        table_name,
        engine,
        schema="public",
        if_exists="append",
        index=False,
        method="multi",
        chunksize=500,
        dtype=dtype,
    )
    logger.info("  %s loaded.", table_name)


# Upserts weather_forecast rows.
# On conflict on (park_id, forecast_date) all weather columns are updated
# so re-running the pipeline always reflects the latest API data.
# park_name is dropped here — it was kept through the pipeline so read_parks()
# could use it for the park_id join, but the weather_forecast table has no
# park_name column.
def upsert_weather_forecast(df: pd.DataFrame, engine) -> None:
    df = df.drop(columns=["park_name"], errors="ignore")
    logger.info("Loading weather_forecast (%d rows)...", len(df))
    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(
                text("""
                    INSERT INTO public.weather_forecast
                        (park_id, forecast_date, soil_temperature_0cm,
                         soil_moisture_0_to_1cm, evapotranspiration, precipitation)
                    VALUES
                        (:park_id, :forecast_date, :soil_temp,
                         :soil_moist, :et0, :precip)
                    ON CONFLICT (park_id, forecast_date) DO UPDATE SET
                        soil_temperature_0cm   = EXCLUDED.soil_temperature_0cm,
                        soil_moisture_0_to_1cm = EXCLUDED.soil_moisture_0_to_1cm,
                        evapotranspiration     = EXCLUDED.evapotranspiration,
                        precipitation          = EXCLUDED.precipitation,
                        created_at             = NOW()
                """),
                {
                    "park_id":       int(row["park_id"]),
                    "forecast_date": row["forecast_date"],
                    "soil_temp":     row["soil_temperature_0cm"],
                    "soil_moist":    row["soil_moisture_0_to_1cm"],
                    "et0":           row["evapotranspiration"],
                    "precip":        row["precipitation"],
                },
            )
    logger.info("  weather_forecast loaded.")


# Upserts maintenance_schedule rows.
# On conflict on (park_id, task_id, schedule_date) the recommendation and
# reason are updated so each run reflects the latest weather data.
def upsert_maintenance_schedule(df: pd.DataFrame, engine) -> None:
    logger.info("Loading maintenance_schedule (%d rows)...", len(df))
    with engine.begin() as conn:
        for _, row in df.iterrows():
            conn.execute(
                text("""
                    INSERT INTO public.maintenance_schedule
                        (park_id, task_id, schedule_date, recommendation, reason)
                    VALUES
                        (:park_id, :task_id, :schedule_date, :recommendation, :reason)
                    ON CONFLICT (park_id, task_id, schedule_date) DO UPDATE SET
                        recommendation = EXCLUDED.recommendation,
                        reason         = EXCLUDED.reason,
                        created_at     = NOW()
                """),
                {
                    "park_id":        int(row["park_id"]),
                    "task_id":        int(row["task_id"]),
                    "schedule_date":  row["schedule_date"],
                    "recommendation": row["recommendation"],
                    "reason":         row.get("reason", ""),
                },
            )
    logger.info("  maintenance_schedule loaded.")


# ---------------------------------------------------------------------------
# Validation — referential integrity check
# ---------------------------------------------------------------------------

# Validation check 2 — Referential integrity check
# Confirms every park_id in weather_data and every park_id and task_id
# in the computed schedule exists in the parent tables already loaded.
# Why it matters: a missing parent row violates a foreign key constraint
# and crashes the insert with a cryptic database error.
# What happens if this fails: raises RuntimeError with the specific missing
# IDs before any child rows are inserted.
def check_referential_integrity(
    engine,
    weather_df: pd.DataFrame,
    schedule_df: pd.DataFrame,
) -> None:
    with engine.connect() as conn:
        db_park_ids = {
            row[0] for row in
            conn.execute(text("SELECT park_id FROM public.park")).fetchall()
        }
        db_task_ids = {
            row[0] for row in
            conn.execute(text("SELECT task_id FROM public.maintenance_task")).fetchall()
        }

    missing_wx_parks = set(weather_df["park_id"].astype(int)) - db_park_ids
    if missing_wx_parks:
        raise RuntimeError(
            f"REFERENTIAL INTEGRITY — {len(missing_wx_parks)} park_id(s) in "
            f"weather_data not found in park table: {missing_wx_parks}"
        )

    missing_sched_parks = set(schedule_df["park_id"].astype(int)) - db_park_ids
    if missing_sched_parks:
        raise RuntimeError(
            f"REFERENTIAL INTEGRITY — {len(missing_sched_parks)} park_id(s) in "
            f"maintenance_schedule not found in park table: {missing_sched_parks}"
        )

    missing_tasks = set(schedule_df["task_id"].astype(int)) - db_task_ids
    if missing_tasks:
        raise RuntimeError(
            f"REFERENTIAL INTEGRITY — {len(missing_tasks)} task_id(s) in "
            f"maintenance_schedule not found in maintenance_task table: {missing_tasks}"
        )

    logger.info("Referential integrity check passed — all foreign keys resolve.")


# ---------------------------------------------------------------------------
# Validation — post-load row count check
# ---------------------------------------------------------------------------

# Validation check 3 — Post-load row count check
# Queries each table after loading and compares the count to the source.
# Why it matters: SQLAlchemy does not raise an error when individual rows
# are silently skipped; a count mismatch is the only reliable signal of a
# partial load.
# What happens if this fails: raises RuntimeError with expected vs actual
# counts for each affected table.
def verify_db_row_counts(engine, expected: dict[str, int]) -> None:
    with engine.connect() as conn:
        for table, expected_count in expected.items():
            actual_count = conn.execute(
                text(f"SELECT COUNT(*) FROM public.{table}")
            ).scalar()
            if actual_count != expected_count:
                raise RuntimeError(
                    f"ROW COUNT CHECK — table '{table}': expected {expected_count} rows, "
                    f"found {actual_count}. Possible partial load."
                )
            logger.info("Row count check — %s: %d rows ✓", table, actual_count)


# ---------------------------------------------------------------------------
# Main workflow orchestration
# ---------------------------------------------------------------------------

# Ties the full load workflow together:
# read CSVs → compute schedule → create schema → insert tables → verify counts.
def main() -> None:
    logger.info("=== Load pipeline starting ===")

    # Guard: confirm weather_data.csv exists before doing anything else
    if not WEATHER_DATA_CSV.exists():
        raise FileNotFoundError(
            f"{WEATHER_DATA_CSV} not found. "
            "Run extract_transform.py first to generate weather data."
        )

    # Read weather_data.csv first — park_id values here are authoritative.
    # read_parks() uses them to assign the correct IDs to each park row.
    weather_df = read_weather_data()

    # Read seed CSVs. park_id is derived from weather_df via a name join,
    # not from row position, so CSV reordering cannot cause a mismatch.
    parks_df = read_parks(weather_df)
    tasks_df = read_tasks()

    # Compute the maintenance schedule from weather data and task constraints
    schedule_df = build_schedule(weather_df, tasks_df, parks_df)

    # Connect and create the database schema
    engine = create_engine(get_database_url())
    print("Creating Supabase PostgreSQL schema...")
    create_schema(engine)

    # Load parent tables first (no foreign key dependencies).
    # park_id is dropped so the SERIAL column auto-assigns the same values.
    write_table(
        parks_df.drop(columns=["park_id"]), "park", engine,
        dtype={
            "name":      Text(),
            "location":  Text(),
            "area_size": Integer(),
            "park_type": Text(),
            "latitude":  Float(),
            "longitude": Float(),
        },
    )

    # task_id is dropped so the SERIAL column auto-assigns the same values.
    write_table(
        tasks_df.drop(columns=["task_id"]), "maintenance_task", engine,
        dtype={
            "task_name":           Text(),
            "category":            Text(),
            "description":         Text(),
            "frequency_days":      Integer(),
            "weather_constraints": Text(),
        },
    )

    # Referential integrity check before inserting child tables
    check_referential_integrity(engine, weather_df, schedule_df)

    # Load child tables (reference park and maintenance_task)
    upsert_weather_forecast(weather_df, engine)
    upsert_maintenance_schedule(schedule_df, engine)

    # Post-load row count verification
    verify_db_row_counts(
        engine,
        {
            "park":                 len(parks_df),
            "maintenance_task":     len(tasks_df),
            "weather_forecast":     len(weather_df),
            "maintenance_schedule": len(schedule_df),
        },
    )

    print("===================================")
    print("ETL LOAD COMPLETE")
    print("===================================")


if __name__ == "__main__":
    main()
