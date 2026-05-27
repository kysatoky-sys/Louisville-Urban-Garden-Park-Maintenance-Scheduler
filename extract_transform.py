"""
Extract & Transform — Louisville Urban Garden & Park Maintenance Scheduler
--------------------------------------------------------------------------
Reads park coordinates from seed_parks.csv, fetches hourly weather data
from the Open-Meteo API for each park using the openmeteo-requests client,
aggregates the hourly values to daily means, validates and cleans the data,
and writes a single output file:

    data/weather_data.csv

One row per park per forecast day. That file is the only handoff between
this script and load.py. No database connection is required here.

Inputs (data/):
    seed_parks.csv

Outputs (data/):
    weather_data.csv

Hourly fields fetched from Open-Meteo (aggregated to daily means):
    soil_temperature_0cm
    soil_moisture_0_to_1cm
    evapotranspiration
    precipitation

Required packages:
    pip install pandas openmeteo-requests requests-cache retry-requests
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import openmeteo_requests
import pandas as pd
import requests_cache
from retry_requests import retry


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

SEED_PARKS_CSV   = DATA_DIR / "seed_parks.csv"
WEATHER_DATA_CSV = DATA_DIR / "weather_data.csv"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS  = 16

# Hourly fields requested from the API.
# Order matters — Variables(index) reads them in the order listed here.
HOURLY_FIELDS = [
    "soil_temperature_0cm",
    "soil_moisture_0_to_1cm",
    "evapotranspiration",
    "precipitation",
]

# Plausible value ranges for Louisville, KY — used for range validation.
# These bounds apply to the DAILY aggregated values produced by
# aggregate_hourly_to_daily(), not to the raw hourly readings.
# Soil temperature and soil moisture are daily means; evapotranspiration
# and precipitation are daily totals (sum of 24 hourly values).
VALIDATION_BOUNDS: dict[str, tuple[float, float]] = {
    "soil_temperature_0cm":   (-10.0, 130.0),  # °F — daily mean
    "soil_moisture_0_to_1cm": (0.0,   1.0),    # m³/m³ — daily mean (physically bounded)
    "evapotranspiration":     (0.0,   1.5),    # inches — daily total (extreme KY summer ~0.5)
    "precipitation":          (0.0,   15.0),   # inches — daily total (record Louisville day ~10)
}


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
logger = logging.getLogger("extract_transform")


# ---------------------------------------------------------------------------
# Open-Meteo client setup
# ---------------------------------------------------------------------------

# Sets up the Open-Meteo API client with a local cache and automatic retry.
# The cache stores responses for 1 hour so re-running the script during
# development does not consume extra API calls.
# The retry wrapper retries up to 5 times with exponential backoff (0.2 s base).
def build_openmeteo_client() -> openmeteo_requests.Client:
    cache_session = requests_cache.CachedSession(
        str(BASE_DIR / ".cache"), expire_after=3600
    )
    retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
    return openmeteo_requests.Client(session=retry_session)


# ---------------------------------------------------------------------------
# Extract — seed_parks.csv
# ---------------------------------------------------------------------------

# Reads park coordinates from seed_parks.csv and assigns each park a stable
# integer park_id based on its row position (1-based).
#
# This is the single place park IDs are assigned for the entire pipeline.
# The IDs are written into weather_data.csv and load.py reads them back from
# that file — it does not re-derive them independently. This means the IDs
# are consistent even if seed_parks.csv is reordered, because load.py always
# follows what this script produced rather than recalculating from scratch.
#
# Drops any parks where latitude or longitude could not be parsed, since
# those parks cannot be passed to the API.
def extract_parks() -> pd.DataFrame:
    df = pd.read_csv(SEED_PARKS_CSV)
    df = df.reset_index(drop=True)
    df.insert(0, "park_id", df.index + 1)

    df["name"]      = df["name"].astype(str).str.strip()
    df["location"]  = df["location"].astype(str).str.strip()
    df["park_type"] = df["park_type"].astype(str).str.strip()
    df["area_size"] = pd.to_numeric(df["area_size"], errors="coerce")
    df["latitude"]  = pd.to_numeric(df["latitude"],  errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")

    missing_coords = df["latitude"].isna() | df["longitude"].isna()
    if missing_coords.any():
        logger.warning(
            "Dropping %d park(s) with unparseable coordinates: %s",
            missing_coords.sum(),
            df.loc[missing_coords, "name"].tolist(),
        )
        df = df[~missing_coords]

    logger.info("Loaded %d parks from %s.", len(df), SEED_PARKS_CSV.name)
    return df


# ---------------------------------------------------------------------------
# Extract — Open-Meteo API (hourly)
# ---------------------------------------------------------------------------

# Fetches hourly weather data for a single park, converts it to a DataFrame,
# and returns it. The openmeteo-requests client handles caching and retries.
# Returns None if the API call or response parsing fails.
def fetch_hourly_for_park(
    park: pd.Series,
    client: openmeteo_requests.Client,
) -> pd.DataFrame | None:
    params = {
        "latitude":         park["latitude"],
        "longitude":        park["longitude"],
        "hourly":           HOURLY_FIELDS,
        "timezone":         "America/New_York",
        "forecast_days":    FORECAST_DAYS,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit":  "mph",
        "precipitation_unit": "inch",
    }

    try:
        responses = client.weather_api(OPEN_METEO_URL, params=params)
        response  = responses[0]

        # Validation check 1 — API response validation
        # Confirms the response object has a populated hourly block and that
        # the number of variable arrays matches the number of fields requested.
        # Why it matters: a count mismatch means Variables(index) would read
        # the wrong field at every index, silently poisoning all constraint
        # evaluations for this park.
        # What happens if this fails: ValueError raised, park is skipped,
        # error logged, remaining parks continue.
        hourly = response.Hourly()
        if hourly is None:
            raise ValueError("Open-Meteo response returned no hourly block.")
        n_vars = hourly.VariablesLength()
        if n_vars != len(HOURLY_FIELDS):
            raise ValueError(
                f"Expected {len(HOURLY_FIELDS)} hourly variables, "
                f"got {n_vars}. Check HOURLY_FIELDS matches the API request."
            )

    except Exception as exc:
        logger.error("Skipping park '%s': %s", park["name"], exc)
        return None

    # Build a tidy hourly DataFrame using the same pattern as the sample code
    date_range = pd.date_range(
        start=pd.to_datetime(hourly.Time(),    unit="s", utc=True),
        end=  pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
        freq= pd.Timedelta(seconds=hourly.Interval()),
        inclusive="left",
    )

    hourly_data: dict = {"datetime": date_range}
    for i, field in enumerate(HOURLY_FIELDS):
        hourly_data[field] = hourly.Variables(i).ValuesAsNumpy()

    df = pd.DataFrame(hourly_data)
    df["park_id"]   = park["park_id"]
    df["park_name"] = park["name"]
    return df


# Iterates over all parks, fetches hourly data, and concatenates into one DataFrame.
def fetch_all_parks(
    parks_df: pd.DataFrame,
    client: openmeteo_requests.Client,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    logger.info("Fetching hourly forecast for %d parks...", len(parks_df))

    for _, park in parks_df.iterrows():
        logger.info("  Fetching: %s", park["name"])
        df = fetch_hourly_for_park(park, client)
        if df is not None:
            frames.append(df)

    if not frames:
        raise RuntimeError(
            "No weather data was retrieved for any park. "
            "Check network connectivity and the Open-Meteo API."
        )

    combined = pd.concat(frames, ignore_index=True)
    logger.info("Raw hourly rows fetched: %d", len(combined))
    return combined


# ---------------------------------------------------------------------------
# Transform — aggregate hourly to daily and validate
# ---------------------------------------------------------------------------

# Converts timezone-aware UTC datetime to a local date, then aggregates
# all hourly observations within each calendar day for each park.
# Soil temperature and soil moisture are averaged. Evapotranspiration and
# precipitation are summed (they are rates / accumulations, not averages).
def aggregate_hourly_to_daily(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["forecast_date"] = (
        df["datetime"]
        .dt.tz_convert("America/New_York")
        .dt.date
    )

    daily = (
        df.groupby(["park_id", "park_name", "forecast_date"])
        .agg(
            soil_temperature_0cm   =("soil_temperature_0cm",   "mean"),
            soil_moisture_0_to_1cm =("soil_moisture_0_to_1cm", "mean"),
            evapotranspiration     =("evapotranspiration",      "sum"),
            precipitation          =("precipitation",           "sum"),
        )
        .reset_index()
    )

    logger.info(
        "Aggregated %d hourly rows to %d daily rows (%d parks).",
        len(df), len(daily), daily["park_id"].nunique(),
    )
    return daily


# Applies three data quality checks to the aggregated daily DataFrame
# and returns a clean version ready to write to weather_data.csv.
def validate_weather(df: pd.DataFrame) -> pd.DataFrame:
    initial_rows = len(df)

    # Validation check 2 — Null value check
    # Each weather column must contain no null values after aggregation.
    # Why it matters: null values in the fields used by constraint rules
    # silently fail comparisons — a null soil_moisture value would bypass
    # a waterlogging check and recommend mowing on a saturated field.
    # What happens if this fails: nulls are filled with the column median
    # across all parks; a warning is logged with the count and fill value.
    for col in HOURLY_FIELDS:
        null_count = df[col].isna().sum()
        if null_count > 0:
            median_val = df[col].median()
            logger.warning(
                "NULL CHECK — '%s': %d null value(s). Filling with median (%.4f).",
                col, null_count, median_val,
            )
            df[col] = df[col].fillna(median_val)

    # Validation check 3 — Range validation
    # Each aggregated column must fall within plausible Louisville bounds.
    # Why it matters: an out-of-range daily value (e.g. a unit mismatch)
    # would cause constraint thresholds to fire incorrectly for every park.
    # What happens if this fails: values are clipped to valid bounds;
    # a warning is logged with the column name and the count of affected rows.
    for col, (lo, hi) in VALIDATION_BOUNDS.items():
        out_of_range = df[(df[col] < lo) | (df[col] > hi)]
        if len(out_of_range) > 0:
            logger.warning(
                "RANGE CHECK — '%s': %d row(s) outside [%.4f, %.4f]. Clipping.",
                col, len(out_of_range), lo, hi,
            )
            df[col] = df[col].clip(lower=lo, upper=hi)

    # Validation check 4 — Duplicate detection
    # Each (park_id, forecast_date) pair must appear exactly once.
    # Why it matters: duplicates produce duplicate schedule rows and violate
    # the unique constraint on the weather_forecast database table.
    # What happens if this fails: duplicates are dropped keeping the last
    # occurrence; a warning is logged with the count.
    dupes = df.duplicated(subset=["park_id", "forecast_date"], keep=False)
    if dupes.sum() > 0:
        logger.warning(
            "DUPLICATE CHECK — %d duplicate (park_id, forecast_date) row(s). "
            "Keeping last occurrence.",
            dupes.sum(),
        )
        df = df.drop_duplicates(subset=["park_id", "forecast_date"], keep="last")

    logger.info(
        "Validation complete. Rows in: %d | Rows out: %d.", initial_rows, len(df)
    )
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Validation — output row count check
# ---------------------------------------------------------------------------

# Validation check 5 — Output row count check
# Confirms the final DataFrame is non-empty before writing to disk.
# Why it matters: an empty DataFrame means all park fetches failed or
# the transform phase dropped every row. Writing an empty CSV would allow
# load.py to run but produce an empty database with no error surfaced.
# What happens if this fails: raises RuntimeError; no CSV is written.
def verify_output_row_count(df: pd.DataFrame) -> None:
    if df.empty:
        raise RuntimeError(
            "ROW COUNT CHECK — weather_data DataFrame is empty. "
            "No output file will be written."
        )
    logger.info(
        "Output row count check passed — %d rows (%d parks × ~%d days).",
        len(df), df["park_id"].nunique(), FORECAST_DAYS,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=== Extract & Transform pipeline starting ===")

    # Build the Open-Meteo client (cache + retry)
    client = build_openmeteo_client()

    # Extract — read park coordinates from seed CSV
    parks_df = extract_parks()

    # Extract — fetch hourly weather data from Open-Meteo for each park
    raw_hourly_df = fetch_all_parks(parks_df, client)

    # Transform — aggregate hourly to daily totals and means
    daily_df = aggregate_hourly_to_daily(raw_hourly_df)

    # Transform — validate and clean the aggregated daily data
    weather_df = validate_weather(daily_df)

    # Verify the output is non-empty before writing
    verify_output_row_count(weather_df)

    # Write weather_data.csv to data/ for load.py
    weather_df.to_csv(WEATHER_DATA_CSV, index=False)
    logger.info(
        "Written: %s  (%d rows, %d parks)",
        WEATHER_DATA_CSV.name, len(weather_df), weather_df["park_id"].nunique(),
    )
    logger.info("=== Extract & Transform complete ===")


if __name__ == "__main__":
    main()
