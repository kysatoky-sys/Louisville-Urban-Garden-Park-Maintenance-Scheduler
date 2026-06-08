"""
app.py  —  Dash Dashboard
Louisville Urban Garden & Park Maintenance Scheduler
----------------------------------------------------
Reads live data from the same Supabase PostgreSQL database populated
by pipeline.py (or extract_transform.py + load.py) and renders a
four-page interactive operations dashboard.

Four pages
----------
  Schedule   —  Today's recommended / deferred / skipped cards per park × task
  Forecast   —  16-day weather charts per park with constraint threshold lines
  Explorer   —  Full filterable schedule table across any date range
  Parks      —  Overview stats, recommendation mix, and per-park comparison

Run
---
    pip install -r requirements.txt
    python app.py        →  http://localhost:8050

.env  (same file used by pipeline.py)
-----
    DB_PASSWORD=your_supabase_database_password
    DB_REF=your_supabase_project_ref

    # OR override with a full URL:
    SUPABASE_DB_URL=postgresql+psycopg2://postgres:<pw>@db.<ref>.supabase.co:5432/postgres
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

import dash
from dash import Input, Output, callback, ctx, dash_table, dcc, html
import plotly.graph_objects as go


# ── Environment & engine ───────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")


def _build_db_url() -> str:
    url = os.getenv("SUPABASE_DB_URL")
    if url:
        return url
    pw  = os.getenv("DB_PASSWORD")
    ref = os.getenv("DB_REF")
    if not pw or not ref:
        raise RuntimeError(
            "Set SUPABASE_DB_URL, or set both DB_PASSWORD and DB_REF in .env"
        )
    return f"postgresql+psycopg2://postgres:{pw}@db.{ref}.supabase.co:5432/postgres"


ENGINE = create_engine(_build_db_url(), pool_pre_ping=True, pool_size=3, max_overflow=2)


# ── Database query helpers ─────────────────────────────────────────────────────

def _q(sql: str, params: dict | None = None) -> pd.DataFrame:
    with ENGINE.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def get_parks() -> pd.DataFrame:
    return _q(
        "SELECT park_id, name, location, area_size, park_type, latitude, longitude "
        "FROM public.park ORDER BY name"
    )


def get_tasks() -> pd.DataFrame:
    return _q(
        "SELECT task_id, task_name, category, description, frequency_days "
        "FROM public.maintenance_task ORDER BY task_name"
    )


def get_schedule(
    start: str,
    end: str,
    park_ids: list | None = None,
    task_ids: list | None = None,
    recs: list | None = None,
) -> pd.DataFrame:
    clauses = ["ms.schedule_date BETWEEN :start AND :end"]
    params: dict = {"start": start, "end": end}
    if park_ids:
        clauses.append("ms.park_id = ANY(:pids)")
        params["pids"] = park_ids
    if task_ids:
        clauses.append("ms.task_id = ANY(:tids)")
        params["tids"] = task_ids
    if recs:
        clauses.append("ms.recommendation = ANY(:recs)")
        params["recs"] = recs
    where = " AND ".join(clauses)
    return _q(f"""
        SELECT ms.schedule_date,
               p.name       AS park_name,
               p.location   AS district,
               p.park_type,
               mt.task_name,
               mt.category,
               ms.recommendation,
               ms.reason,
               ms.park_id,
               ms.task_id
        FROM   public.maintenance_schedule ms
        JOIN   public.park             p  ON p.park_id  = ms.park_id
        JOIN   public.maintenance_task mt ON mt.task_id = ms.task_id
        WHERE  {where}
        ORDER  BY ms.schedule_date, p.name, mt.task_name
    """, params)


def get_forecast(park_id: int | None = None) -> pd.DataFrame:
    if park_id:
        return _q("""
            SELECT wf.forecast_id,
                   wf.park_id,
                   wf.forecast_date,
                   wf.soil_temperature_0cm,
                   wf.soil_moisture_0_to_1cm,
                   wf.evapotranspiration,
                   wf.precipitation,
                   p.name AS park_name
            FROM   public.weather_forecast wf
            JOIN   public.park p ON p.park_id = wf.park_id
            WHERE  wf.park_id = :pid
            ORDER  BY wf.forecast_date
        """, {"pid": park_id})
    return _q("""
        SELECT wf.forecast_id,
               wf.park_id,
               wf.forecast_date,
               wf.soil_temperature_0cm,
               wf.soil_moisture_0_to_1cm,
               wf.evapotranspiration,
               wf.precipitation,
               p.name AS park_name
        FROM   public.weather_forecast wf
        JOIN   public.park p ON p.park_id = wf.park_id
        ORDER  BY p.name, wf.forecast_date
    """)


# ── Design tokens ──────────────────────────────────────────────────────────────
# Aesthetic: botanical field journal — warm cream paper, deep forest ink,
# copper-amber accents. Feels like a professional groundskeeper's logbook.

C = dict(
    bg        = "#f5f0e8",       # warm parchment
    surface   = "#faf7f2",       # lighter parchment
    surface2  = "#ede8de",       # mid parchment
    border    = "#c8bfaa",       # warm tan border
    border2   = "#a09080",       # deeper tan
    ink       = "#1a2410",       # deep forest ink
    ink2      = "#3d4a30",       # mid forest
    ink3      = "#6b7a58",       # muted forest
    green     = "#2d6a2d",       # forest green
    green2    = "#4a9e3f",       # lighter green
    green_bg  = "#e8f2e8",       # green tint
    amber     = "#b06000",       # deep amber
    amber2    = "#d4820a",       # lighter amber
    amber_bg  = "#fdf3e0",       # amber tint
    red       = "#8b1a1a",       # deep red
    red2      = "#c0392b",       # lighter red
    red_bg    = "#fde8e8",       # red tint
    blue      = "#1a4a6b",       # deep blue
    blue2     = "#2980b9",       # lighter blue
    white     = "#ffffff",
)

# Recommendation visual config
REC = {
    "recommended": dict(color=C["green"],  bg=C["green_bg"],  border=C["green2"],  icon="✦"),
    "deferred":    dict(color=C["amber"],  bg=C["amber_bg"],  border=C["amber2"],  icon="◐"),
    "skipped":     dict(color=C["red"],    bg=C["red_bg"],    border=C["red2"],    icon="✕"),
}

# Plotly base layout — warm paper look
PLOT_BASE = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="#fdf9f4",
    font=dict(family="'Lora', Georgia, serif", color=C["ink2"], size=11),
    xaxis=dict(
        gridcolor=C["border"], linecolor=C["border2"],
        tickfont=dict(size=10, family="'JetBrains Mono', monospace"),
    ),
    yaxis=dict(
        gridcolor=C["border"], linecolor=C["border2"],
        tickfont=dict(size=10, family="'JetBrains Mono', monospace"),
    ),
    legend=dict(
        bgcolor="rgba(250,247,242,0.9)",
        bordercolor=C["border"], borderwidth=1,
        font=dict(size=11),
    ),
    margin=dict(l=52, r=16, t=36, b=40),
)


# ── Shared table style ─────────────────────────────────────────────────────────
TABLE_KWARGS = dict(
    style_table={"overflowX": "auto", "borderRadius": "6px", "overflow": "hidden"},
    style_header={
        "backgroundColor": C["surface2"],
        "color": C["ink"],
        "fontFamily": "'JetBrains Mono', monospace",
        "fontSize": "11px",
        "fontWeight": "600",
        "textTransform": "uppercase",
        "letterSpacing": "0.08em",
        "border": f"1px solid {C['border']}",
        "padding": "10px 14px",
    },
    style_cell={
        "backgroundColor": C["surface"],
        "color": C["ink2"],
        "fontFamily": "'JetBrains Mono', monospace",
        "fontSize": "12px",
        "border": f"1px solid {C['border']}",
        "padding": "9px 14px",
        "textAlign": "left",
        "maxWidth": "340px",
        "overflow": "hidden",
        "textOverflow": "ellipsis",
    },
    style_data_conditional=[
        {"if": {"row_index": "odd"}, "backgroundColor": C["bg"]},
        {"if": {"filter_query": '{Recommendation} = "recommended"',
                "column_id": "Recommendation"},
         "color": C["green"], "fontWeight": "600"},
        {"if": {"filter_query": '{Recommendation} = "deferred"',
                "column_id": "Recommendation"},
         "color": C["amber"], "fontWeight": "600"},
        {"if": {"filter_query": '{Recommendation} = "skipped"',
                "column_id": "Recommendation"},
         "color": C["red"], "fontWeight": "600"},
    ],
    sort_action="native",
    filter_action="native",
    page_size=15,
)


# ── Reusable UI components ─────────────────────────────────────────────────────

def page_header(title: str, sub: str = "") -> html.Div:
    return html.Div([
        html.H1(title, className="page-title"),
        html.P(sub, className="page-sub") if sub else None,
        html.Div(className="page-rule"),
    ], className="page-header")


def kpi(label: str, value: str, variant: str = "neutral") -> html.Div:
    """Single KPI stat card. variant: neutral | green | amber | red | blue"""
    return html.Div([
        html.Div(className=f"kpi-accent {variant}"),
        html.Div([
            html.P(label, className="kpi-label"),
            html.P(value, className=f"kpi-value {variant}"),
        ], className="kpi-body"),
    ], className="kpi-card")


def rec_pill(rec: str) -> html.Span:
    cfg = REC.get(rec, dict(color=C["ink3"], bg=C["surface2"],
                            border=C["border"], icon="·"))
    return html.Span(
        [cfg["icon"], " ", rec],
        style={
            "display": "inline-flex", "alignItems": "center", "gap": "4px",
            "padding": "2px 9px", "borderRadius": "4px",
            "fontSize": "10px", "fontWeight": "600",
            "textTransform": "uppercase", "letterSpacing": "0.1em",
            "fontFamily": "'JetBrains Mono', monospace",
            "color": cfg["color"],
            "background": cfg["bg"],
            "border": f"1px solid {cfg['border']}",
        }
    )


def schedule_card(park: str, task: str, category: str,
                  rec: str, reason: str) -> html.Div:
    cfg = REC.get(rec, dict(color=C["ink3"], bg=C["surface"],
                            border=C["border2"], icon="·"))
    return html.Div([
        html.Div([
            html.P(park, className="sc-park"),
            html.P(f"{task}  ·  {category}", className="sc-meta"),
            rec_pill(rec),
            html.P(
                reason or "All weather conditions are favorable.",
                className="sc-reason"
            ),
        ]),
    ], className=f"sched-card rec-{rec}",
       style={"borderLeftColor": cfg["color"]})


def chart_card(title: str, fig: go.Figure,
               legend_items: list | None = None) -> html.Div:
    children = [
        html.P(title, className="chart-label"),
        dcc.Graph(
            figure=fig,
            config={"displayModeBar": False},
            style={"height": "210px"},
        ),
    ]
    if legend_items:
        children.append(html.Div(legend_items, className="chart-legend"))
    return html.Div(children, className="card")


def threshold_item(label: str, colour: str) -> html.Div:
    return html.Div([
        html.Span(style={
            "display": "inline-block", "width": "20px", "height": "0",
            "borderTop": f"2px dashed {colour}", "verticalAlign": "middle",
            "marginRight": "6px",
        }),
        html.Span(label, style={"fontSize": "10px", "color": C["ink3"],
                                 "fontFamily": "'JetBrains Mono', monospace"}),
    ], style={"display": "flex", "alignItems": "center"})


def empty_state(msg: str = "No data found for the selected filters.") -> html.P:
    return html.P(msg, className="empty-state")


def new_fig(**kwargs) -> go.Figure:
    # Merge PLOT_BASE with caller kwargs, letting kwargs override PLOT_BASE.
    # A plain dict-unpack (**PLOT_BASE, **kwargs) raises TypeError when both
    # dicts share a key (e.g. 'margin'), so we merge explicitly instead.
    layout = {**PLOT_BASE, **kwargs}
    fig = go.Figure()
    fig.update_layout(**layout)
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# PAGE BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

# ── Page 1: Schedule ──────────────────────────────────────────────────────────

def build_schedule_page() -> html.Div:
    today = str(date.today())
    df    = get_schedule(today, today)

    n_tot  = len(df)
    n_rec  = int((df["recommendation"] == "recommended").sum()) if n_tot else 0
    n_def  = int((df["recommendation"] == "deferred").sum())    if n_tot else 0
    n_skip = int((df["recommendation"] == "skipped").sum())     if n_tot else 0

    tasks    = get_tasks()
    t_opts   = [{"label": r["task_name"], "value": r["task_name"]}
                for _, r in tasks.iterrows()]
    rec_opts = [{"label": r.title(), "value": r}
                for r in ["recommended", "deferred", "skipped"]]

    return html.Div([
        page_header(
            "Today's Schedule",
            f"{today}  ·  {n_tot} evaluations across all parks"
        ),

        # KPI strip
        html.Div([
            kpi("Total Tasks",   str(n_tot),  "neutral"),
            kpi("Recommended",   str(n_rec),  "green"),
            kpi("Deferred",      str(n_def),  "amber"),
            kpi("Skipped",       str(n_skip), "red"),
        ], className="kpi-strip"),

        # Filters
        html.Div([
            html.Div([
                html.P("Filter by task", className="ctl-label"),
                dcc.Dropdown(
                    id="sched-filter-task",
                    options=t_opts, multi=True,
                    placeholder="All tasks",
                    className="dd",
                ),
            ], className="ctl-group"),
            html.Div([
                html.P("Filter by status", className="ctl-label"),
                dcc.Dropdown(
                    id="sched-filter-rec",
                    options=rec_opts, multi=True,
                    placeholder="All statuses",
                    className="dd",
                ),
            ], className="ctl-group"),
        ], className="controls-bar"),

        # Card grid (populated by callback)
        html.Div(id="sched-card-grid", className="sched-grid"),
    ], className="page-body")


# ── Page 2: Forecast ──────────────────────────────────────────────────────────

def build_forecast_page() -> html.Div:
    parks   = get_parks()
    p_opts  = [{"label": r["name"], "value": int(r["park_id"])}
               for _, r in parks.iterrows()]
    default = int(parks.iloc[0]["park_id"]) if len(parks) > 0 else None

    return html.Div([
        page_header(
            "Weather Forecast",
            "16-day Open-Meteo hourly data aggregated to daily values  ·  soil temp °F  ·  precip & ET inches"
        ),

        html.Div([
            html.Div([
                html.P("Select park", className="ctl-label"),
                dcc.Dropdown(
                    id="wx-park-select",
                    options=p_opts, value=default,
                    clearable=False, className="dd",
                    style={"minWidth": "240px"},
                ),
            ], className="ctl-group"),
        ], className="controls-bar"),

        html.Div(id="wx-chart-area"),
    ], className="page-body")


# ── Page 3: Explorer ──────────────────────────────────────────────────────────

def build_explorer_page() -> html.Div:
    parks    = get_parks()
    tasks    = get_tasks()
    today    = date.today()

    p_opts   = [{"label": r["name"],      "value": int(r["park_id"])}
                for _, r in parks.iterrows()]
    t_opts   = [{"label": r["task_name"], "value": int(r["task_id"])}
                for _, r in tasks.iterrows()]
    rec_opts = [{"label": r.title(), "value": r}
                for r in ["recommended", "deferred", "skipped"]]

    return html.Div([
        page_header(
            "Task Explorer",
            "Filter the complete maintenance schedule by any combination of park, task, status, and date"
        ),

        # Filters
        html.Div([
            html.Div([
                html.P("Parks", className="ctl-label"),
                dcc.Dropdown(id="exp-parks", options=p_opts, multi=True,
                             placeholder="All", className="dd",
                             style={"minWidth": "200px"}),
            ], className="ctl-group"),
            html.Div([
                html.P("Tasks", className="ctl-label"),
                dcc.Dropdown(id="exp-tasks", options=t_opts, multi=True,
                             placeholder="All", className="dd",
                             style={"minWidth": "180px"}),
            ], className="ctl-group"),
            html.Div([
                html.P("Status", className="ctl-label"),
                dcc.Dropdown(id="exp-recs", options=rec_opts, multi=True,
                             placeholder="All", className="dd",
                             style={"minWidth": "160px"}),
            ], className="ctl-group"),
            html.Div([
                html.P("Date range", className="ctl-label"),
                dcc.DatePickerRange(
                    id="exp-dates",
                    start_date=str(today),
                    end_date=str(today + timedelta(days=15)),
                    display_format="YYYY-MM-DD",
                    className="date-picker",
                ),
            ], className="ctl-group"),
        ], className="controls-bar"),

        # Dynamic outputs
        html.Div(id="exp-kpis",  className="kpi-strip"),
        html.Div(id="exp-chart", style={"marginBottom": "16px"}),
        html.Div(id="exp-table"),
    ], className="page-body")


# ── Page 4: Parks Overview ────────────────────────────────────────────────────

def build_parks_page() -> html.Div:
    parks  = get_parks()
    w0     = str(date.today() - timedelta(days=7))
    w1     = str(date.today() + timedelta(days=7))
    sched  = get_schedule(w0, w1)

    n_tot  = len(sched)
    n_rec  = int((sched["recommendation"] == "recommended").sum())
    n_def  = int((sched["recommendation"] == "deferred").sum())
    n_skip = int((sched["recommendation"] == "skipped").sum())

    # ── Donut — recommendation mix ──────────────────────────────────────
    donut = new_fig(height=240, showlegend=True,
                    margin=dict(l=10, r=100, t=24, b=10),
                    legend=dict(orientation="v", x=1.02, y=0.5,
                                font=dict(size=11)))
    donut.add_trace(go.Pie(
        labels=["Recommended", "Deferred", "Skipped"],
        values=[n_rec, n_def, n_skip],
        hole=0.58,
        marker=dict(
            colors=[C["green2"], C["amber2"], C["red2"]],
            line=dict(color=C["bg"], width=3),
        ),
        textfont=dict(family="'JetBrains Mono', monospace", size=10),
        hovertemplate="<b>%{label}</b><br>%{value} decisions (%{percent})<extra></extra>",
    ))
    donut.add_annotation(
        text=f"<b>{n_tot}</b>",
        x=0.5, y=0.5, showarrow=False,
        font=dict(family="'Lora', serif", size=22, color=C["ink"]),
    )

    # ── Stacked bar — decisions by task ─────────────────────────────────
    task_bar = new_fig(barmode="stack", height=240,
                       margin=dict(l=40, r=10, t=24, b=64))
    if not sched.empty:
        tc = (sched.groupby(["task_name", "recommendation"])
              .size().unstack(fill_value=0))
        for rec, colour in [("recommended", C["green2"]),
                             ("deferred",    C["amber2"]),
                             ("skipped",     C["red2"])]:
            if rec in tc.columns:
                task_bar.add_trace(go.Bar(
                    name=rec.title(), x=tc.index, y=tc[rec],
                    marker_color=colour,
                    hovertemplate="%{x}<br>" + rec.title() + ": %{y}<extra></extra>",
                ))

    # ── Park summary table ───────────────────────────────────────────────
    rows = []
    for _, pk in parks.iterrows():
        sub = sched[sched["park_id"] == pk["park_id"]]
        rows.append({
            "Park":        pk["name"],
            "District":    pk["location"],
            "Type":        pk["park_type"],
            "Area (m²)":   f"{int(pk['area_size']):,}" if pd.notna(pk.get("area_size")) else "—",
            "Recommended": int((sub["recommendation"] == "recommended").sum()),
            "Deferred":    int((sub["recommendation"] == "deferred").sum()),
            "Skipped":     int((sub["recommendation"] == "skipped").sum()),
        })
    summary_df = pd.DataFrame(rows)

    return html.Div([
        page_header(
            "Park Overview",
            f"±7-day window  ·  {len(parks)} parks  ·  {n_tot} total decisions"
        ),

        # KPI strip
        html.Div([
            kpi("Parks",       str(len(parks)), "neutral"),
            kpi("Decisions",   str(n_tot),      "neutral"),
            kpi("Recommended", str(n_rec),       "green"),
            kpi("Deferred",    str(n_def),       "amber"),
            kpi("Skipped",     str(n_skip),      "red"),
        ], className="kpi-strip"),

        # Charts row
        html.Div([
            chart_card("RECOMMENDATION MIX  (±7 DAYS)", donut),
            chart_card("DECISIONS BY TASK  (±7 DAYS)", task_bar),
        ], className="grid-2"),

        # Summary table
        html.Div([
            html.P("ALL PARKS — ±7-DAY SUMMARY", className="chart-label"),
            dash_table.DataTable(
                data=summary_df.to_dict("records"),
                columns=[{"name": c, "id": c} for c in summary_df.columns],
                **{k: v for k, v in TABLE_KWARGS.items()
                   if k not in ("filter_action", "sort_action", "page_size")},
                sort_action="native",
                page_size=12,
            ),
        ], className="card"),
    ], className="page-body")


# ══════════════════════════════════════════════════════════════════════════════
# APP LAYOUT
# ══════════════════════════════════════════════════════════════════════════════

NAV_ITEMS = [
    ("nav-schedule", "◈", "Schedule"),
    ("nav-forecast", "⌀", "Forecast"),
    ("nav-explorer", "⊞", "Explorer"),
    ("nav-parks",    "◉", "Parks"),
]

_HERE = Path(__file__).parent

app = dash.Dash(
    __name__,
    title="Louisville Park Maintenance",
    assets_folder=str(_HERE / "assets"),
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
    suppress_callback_exceptions=True,
)

app.index_string = """
<!DOCTYPE html>
<html>
  <head>
    {%metas%}
    <title>{%title%}</title>
    {%favicon%}
    {%css%}
    <style>
/* ============================================================
   Louisville Park Maintenance — Dashboard Styles
   Aesthetic: Botanical field journal — warm parchment paper,
   deep forest ink, copper-amber accents, JetBrains Mono data.
   ============================================================ */

@import url('https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,600;0,700;1,400;1,600&family=JetBrains+Mono:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:         #f5f0e8;
  --surface:    #faf7f2;
  --surface2:   #ede8de;
  --border:     #c8bfaa;
  --border2:    #a09080;
  --ink:        #1a2410;
  --ink2:       #3d4a30;
  --ink3:       #6b7a58;
  --green:      #2d6a2d;
  --green2:     #4a9e3f;
  --green-bg:   #e8f2e8;
  --amber:      #b06000;
  --amber2:     #d4820a;
  --amber-bg:   #fdf3e0;
  --red:        #8b1a1a;
  --red2:       #c0392b;
  --red-bg:     #fde8e8;
  --blue:       #1a4a6b;
  --blue2:      #2980b9;
  --white:      #ffffff;
}

html, body {
  background: var(--bg);
  color: var(--ink);
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  line-height: 1.65;
  min-height: 100vh;
}

/* ── Scrollbar ─────────────────────────────────────── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--surface2); }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--ink3); }

/* ══════════════════════════════════════════════════════
   SHELL — sidebar + main
   ══════════════════════════════════════════════════════ */

.shell { display: flex; min-height: 100vh; }

/* ── Sidebar ───────────────────────────────────────── */
.sidebar {
  width: 210px;
  flex-shrink: 0;
  background: var(--ink);
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 0; left: 0; bottom: 0;
  z-index: 100;
  border-right: 2px solid #2d4020;
}

/* Brand */
.brand {
  padding: 28px 20px 22px;
  border-bottom: 1px solid #2d4020;
}
.brand-icon {
  font-size: 28px;
  color: var(--green2);
  margin-bottom: 10px;
  display: block;
}
.brand-title {
  font-family: 'Lora', serif;
  font-size: 16px;
  font-weight: 700;
  font-style: italic;
  color: var(--bg);
  line-height: 1.4;
  white-space: pre-line;
  letter-spacing: -0.01em;
}
.brand-sub {
  font-size: 9px;
  color: #6b7a58;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  margin-top: 6px;
}

/* Nav */
.sidebar-nav { padding: 14px 10px; flex: 1; }

.nav-section-label {
  font-size: 9px;
  color: #4a5a38;
  text-transform: uppercase;
  letter-spacing: 0.2em;
  padding: 8px 10px 6px;
}

.nav-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 5px;
  width: 100%;
  background: none;
  border: none;
  color: #8aaa7a;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  cursor: pointer;
  text-align: left;
  margin-bottom: 2px;
  transition: background 0.13s, color 0.13s, border-color 0.13s;
  border-left: 2px solid transparent;
}
.nav-btn:hover { background: #243318; color: var(--bg); }
.nav-btn.active {
  background: #1e2d14;
  color: var(--green2);
  border-left-color: var(--green2);
}
.nav-icon { font-size: 14px; width: 18px; text-align: center; opacity: 0.85; }

/* Sidebar footer */
.sidebar-foot {
  padding: 14px 20px;
  border-top: 1px solid #2d4020;
  font-size: 10px;
  color: #4a5a38;
  line-height: 2;
}

/* ── Main content ──────────────────────────────────── */
.main { margin-left: 210px; min-height: 100vh; }

.page-content { animation: pageIn 0.2s ease both; }
@keyframes pageIn {
  from { opacity: 0; transform: translateY(10px); }
  to   { opacity: 1; transform: translateY(0); }
}

.page-body { padding: 32px 36px; }

/* ══════════════════════════════════════════════════════
   PAGE HEADER
   ══════════════════════════════════════════════════════ */

.page-header { margin-bottom: 28px; }

.page-title {
  font-family: 'Lora', serif;
  font-size: 30px;
  font-weight: 700;
  font-style: italic;
  color: var(--ink);
  line-height: 1.2;
  letter-spacing: -0.02em;
}

.page-sub {
  font-size: 11px;
  color: var(--ink3);
  margin-top: 5px;
  font-family: 'JetBrains Mono', monospace;
}

.page-rule {
  height: 2px;
  background: linear-gradient(to right, var(--green), var(--border) 60%, transparent);
  margin-top: 18px;
}

/* ══════════════════════════════════════════════════════
   KPI CARDS
   ══════════════════════════════════════════════════════ */

.kpi-strip {
  display: flex;
  gap: 12px;
  margin-bottom: 26px;
  flex-wrap: wrap;
}

.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 7px;
  flex: 1;
  min-width: 120px;
  display: flex;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(26,36,16,0.06);
}

.kpi-accent {
  width: 4px;
  flex-shrink: 0;
  background: var(--border2);
}
.kpi-accent.green  { background: var(--green2); }
.kpi-accent.amber  { background: var(--amber2); }
.kpi-accent.red    { background: var(--red2); }
.kpi-accent.blue   { background: var(--blue2); }
.kpi-accent.neutral{ background: var(--border2); }

.kpi-body { padding: 14px 16px; }

.kpi-label {
  font-size: 9px;
  color: var(--ink3);
  text-transform: uppercase;
  letter-spacing: 0.14em;
  margin-bottom: 5px;
}

.kpi-value {
  font-family: 'Lora', serif;
  font-size: 32px;
  font-weight: 700;
  line-height: 1;
  color: var(--ink);
}
.kpi-value.green   { color: var(--green); }
.kpi-value.amber   { color: var(--amber); }
.kpi-value.red     { color: var(--red); }
.kpi-value.blue    { color: var(--blue); }
.kpi-value.neutral { color: var(--ink); }

/* ══════════════════════════════════════════════════════
   CONTROLS
   ══════════════════════════════════════════════════════ */

.controls-bar {
  display: flex;
  gap: 16px;
  align-items: flex-end;
  margin-bottom: 22px;
  flex-wrap: wrap;
}
.ctl-group { display: flex; flex-direction: column; gap: 5px; }
.ctl-label {
  font-size: 9px;
  color: var(--ink3);
  text-transform: uppercase;
  letter-spacing: 0.14em;
}

/* Dash dropdown overrides */
.dd .Select-control {
  background: var(--surface) !important;
  border-color: var(--border) !important;
  border-radius: 5px !important;
  min-height: 34px !important;
}
.dd .Select-control:hover { border-color: var(--border2) !important; }
.dd .Select-placeholder,
.dd .Select-value-label,
.dd .Select-input > input {
  color: var(--ink2) !important;
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 12px !important;
}
.dd .Select-menu-outer {
  background: var(--surface) !important;
  border-color: var(--border) !important;
  border-radius: 5px !important;
  box-shadow: 0 4px 12px rgba(26,36,16,0.12) !important;
}
.dd .Select-option {
  background: var(--surface) !important;
  color: var(--ink2) !important;
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 12px !important;
}
.dd .Select-option:hover,
.dd .Select-option.is-focused {
  background: var(--surface2) !important;
  color: var(--ink) !important;
}
.dd .Select-multi-value-wrapper .Select-value {
  background: var(--green-bg) !important;
  border-color: var(--green2) !important;
  color: var(--green) !important;
  border-radius: 3px !important;
  font-size: 11px !important;
}
.dd .Select-value-icon { color: var(--green) !important; }

/* Date picker overrides */
.date-picker .DateInput_input {
  background: var(--surface) !important;
  color: var(--ink2) !important;
  border-bottom-color: var(--border) !important;
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 12px !important;
  padding: 6px 8px !important;
}
.date-picker .DateRangePickerInput {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: 5px !important;
}
.date-picker .DateRangePickerInput__withBorder {
  border-radius: 5px !important;
}

/* ══════════════════════════════════════════════════════
   CARDS & CHARTS
   ══════════════════════════════════════════════════════ */

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 18px 20px;
  margin-bottom: 16px;
  box-shadow: 0 1px 4px rgba(26,36,16,0.05);
}

.chart-label {
  font-size: 9px;
  color: var(--ink3);
  text-transform: uppercase;
  letter-spacing: 0.15em;
  margin-bottom: 12px;
  font-weight: 600;
}

.chart-legend {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}

/* Two-column grid */
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 16px;
}

/* ══════════════════════════════════════════════════════
   SCHEDULE CARDS
   ══════════════════════════════════════════════════════ */

.sched-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(265px, 1fr));
  gap: 12px;
}

.sched-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 4px solid var(--border2);
  border-radius: 7px;
  padding: 15px 16px;
  box-shadow: 0 1px 3px rgba(26,36,16,0.06);
  transition: box-shadow 0.15s, transform 0.15s;
}
.sched-card:hover {
  box-shadow: 0 3px 10px rgba(26,36,16,0.1);
  transform: translateY(-1px);
}

.sched-card.rec-recommended { border-left-color: #4a9e3f; }
.sched-card.rec-deferred    { border-left-color: #d4820a; }
.sched-card.rec-skipped     { border-left-color: #c0392b; }

.sc-park {
  font-family: 'Lora', serif;
  font-size: 14px;
  font-weight: 600;
  color: var(--ink);
  margin-bottom: 2px;
}
.sc-meta {
  font-size: 10px;
  color: var(--ink3);
  margin-bottom: 9px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.sc-reason {
  font-size: 11px;
  color: var(--ink3);
  margin-top: 8px;
  font-style: italic;
  line-height: 1.55;
}

/* ══════════════════════════════════════════════════════
   DATA TABLE OVERRIDES
   ══════════════════════════════════════════════════════ */

.dash-spreadsheet-container .dash-spreadsheet-inner td,
.dash-spreadsheet-container .dash-spreadsheet-inner th {
  font-family: 'JetBrains Mono', monospace !important;
  font-size: 12px !important;
}

/* ══════════════════════════════════════════════════════
   EMPTY STATE
   ══════════════════════════════════════════════════════ */

.empty-state {
  padding: 64px 0;
  text-align: center;
  color: var(--ink3);
  font-style: italic;
  font-size: 13px;
}

/* ══════════════════════════════════════════════════════
   RESPONSIVE — narrow screens
   ══════════════════════════════════════════════════════ */

@media (max-width: 900px) {
  .grid-2 { grid-template-columns: 1fr; }
}
@media (max-width: 700px) {
  .sidebar { width: 160px; }
  .main { margin-left: 160px; }
  .page-body { padding: 20px 18px; }
}

    </style>
  </head>
  <body>
    {%app_entry%}
    <footer>
      {%config%}
      {%scripts%}
      {%renderer%}
    </footer>
  </body>
</html>
"""

app.layout = html.Div([

    # ── Sidebar ────────────────────────────────────────────────────────────
    html.Aside([
        # Brand
        html.Div([
            html.Div("⬡", className="brand-icon"),
            html.H1("Louisville\nPark\nMaintenance", className="brand-title"),
            html.P("Operations Dashboard", className="brand-sub"),
        ], className="brand"),

        # Nav
        html.Nav([
            html.P("Navigation", className="nav-section-label"),
            *[
                html.Button(
                    [html.Span(icon, className="nav-icon"), label],
                    id=bid, className="nav-btn", n_clicks=0,
                )
                for bid, icon, label in NAV_ITEMS
            ],
        ], className="sidebar-nav"),

        # Footer
        html.Div([
            html.P("Louisville, KY"),
            html.P("Open-Meteo API"),
            html.P("Supabase PostgreSQL"),
        ], className="sidebar-foot"),
    ], className="sidebar"),

    # ── Main ───────────────────────────────────────────────────────────────
    html.Main([
        dcc.Store(id="active-page", data="schedule"),
        html.Div(id="page-content", className="page-content"),
    ], className="main"),

], className="shell")


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════

# ── Navigation ─────────────────────────────────────────────────────────────────
@callback(
    Output("active-page",   "data"),
    Output("nav-schedule",  "className"),
    Output("nav-forecast",  "className"),
    Output("nav-explorer",  "className"),
    Output("nav-parks",     "className"),
    [Input(bid, "n_clicks") for bid, _, _ in NAV_ITEMS],
)
def navigate(*_):
    triggered = ctx.triggered_id or "nav-schedule"
    page_map  = {
        "nav-schedule": "schedule",
        "nav-forecast": "forecast",
        "nav-explorer": "explorer",
        "nav-parks":    "parks",
    }
    active  = page_map.get(triggered, "schedule")
    classes = [
        "nav-btn active" if bid == triggered else "nav-btn"
        for bid, _, _ in NAV_ITEMS
    ]
    return (active, *classes)


# ── Page renderer ──────────────────────────────────────────────────────────────
@callback(Output("page-content", "children"), Input("active-page", "data"))
def render_page(page: str):
    if page == "forecast": return build_forecast_page()
    if page == "explorer": return build_explorer_page()
    if page == "parks":    return build_parks_page()
    return build_schedule_page()


# ── Schedule: filter cards ─────────────────────────────────────────────────────
@callback(
    Output("sched-card-grid", "children"),
    Input("sched-filter-task", "value"),
    Input("sched-filter-rec",  "value"),
)
def update_schedule_cards(task_filter, rec_filter):
    today = str(date.today())
    df    = get_schedule(today, today)

    if task_filter:
        df = df[df["task_name"].isin(task_filter)]
    if rec_filter:
        df = df[df["recommendation"].isin(rec_filter)]

    if df.empty:
        return [empty_state("No tasks match the selected filters.")]

    return [
        schedule_card(
            r["park_name"], r["task_name"], r["category"],
            r["recommendation"], r["reason"] or "",
        )
        for _, r in df.iterrows()
    ]


# ── Forecast: weather charts ───────────────────────────────────────────────────
@callback(Output("wx-chart-area", "children"), Input("wx-park-select", "value"))
def update_wx_charts(park_id):
    if not park_id:
        return empty_state("Select a park above.")

    df = get_forecast(int(park_id))
    if df.empty:
        return empty_state(
            "No forecast data for this park. Run pipeline.py first."
        )

    df["forecast_date"] = pd.to_datetime(df["forecast_date"])
    park_name = df["park_name"].iloc[0]
    hov = "<b>%{x|%b %d}</b><br>"

    # ── Chart 1: Soil Temperature (°F) ──────────────────────────────────
    fig_temp = new_fig(yaxis_title="°F")
    fig_temp.add_trace(go.Scatter(
        x=df["forecast_date"], y=df["soil_temperature_0cm"],
        mode="lines+markers", name="Soil Temp (°F)",
        line=dict(color=C["amber"], width=2.5),
        marker=dict(size=5, color=C["amber"],
                    line=dict(color=C["white"], width=1)),
        fill="tozeroy", fillcolor=f"{C['amber_bg']}",
        hovertemplate=hov + "Soil Temp: %{y:.1f} °F<extra></extra>",
    ))
    # Fertilizing minimum: 41°F (5°C)
    fig_temp.add_hline(
        y=41, line_dash="dot", line_color=C["red2"], opacity=0.7,
        annotation_text="41°F fertilizing min",
        annotation_font=dict(size=9, color=C["red2"], family="'JetBrains Mono', monospace"),
        annotation_position="bottom right",
    )

    # ── Chart 2: Soil Moisture (m³/m³) ──────────────────────────────────
    fig_moist = new_fig(yaxis_title="m³/m³")
    fig_moist.add_trace(go.Scatter(
        x=df["forecast_date"], y=df["soil_moisture_0_to_1cm"],
        mode="lines+markers", name="Soil Moisture",
        line=dict(color=C["blue2"], width=2.5),
        marker=dict(size=5, color=C["blue2"],
                    line=dict(color=C["white"], width=1)),
        fill="tozeroy", fillcolor="#e8f2f8",
        hovertemplate=hov + "Moisture: %{y:.3f} m³/m³<extra></extra>",
    ))
    # Waterlogging threshold — mowing/planting skip
    fig_moist.add_hline(
        y=0.40, line_dash="dot", line_color=C["red2"], opacity=0.75,
        annotation_text="0.40 waterlog (mowing/planting skip)",
        annotation_font=dict(size=9, color=C["red2"], family="'JetBrains Mono', monospace"),
        annotation_position="top right",
    )
    # Dryness threshold — irrigation trigger
    fig_moist.add_hline(
        y=0.25, line_dash="dot", line_color=C["amber2"], opacity=0.65,
        annotation_text="0.25 dry (irrigation trigger)",
        annotation_font=dict(size=9, color=C["amber2"], family="'JetBrains Mono', monospace"),
        annotation_position="bottom right",
    )

    # ── Chart 3: Precipitation (inches) ─────────────────────────────────
    fig_precip = new_fig(yaxis_title="inches")
    fig_precip.add_trace(go.Bar(
        x=df["forecast_date"], y=df["precipitation"],
        name="Precipitation (in)",
        marker_color=C["blue2"], opacity=0.7,
        hovertemplate=hov + "Precipitation: %{y:.2f} in<extra></extra>",
    ))
    # Irrigation-skip threshold
    fig_precip.add_hline(
        y=0.20, line_dash="dot", line_color=C["amber2"], opacity=0.7,
        annotation_text="0.20 in  irrigation skip",
        annotation_font=dict(size=9, color=C["amber2"], family="'JetBrains Mono', monospace"),
        annotation_position="top right",
    )

    # ── Chart 4: Evapotranspiration (inches) ─────────────────────────────
    fig_et = new_fig(yaxis_title="inches")
    fig_et.add_trace(go.Scatter(
        x=df["forecast_date"], y=df["evapotranspiration"],
        mode="lines+markers", name="ET (in)",
        line=dict(color=C["green2"], width=2.5),
        marker=dict(size=5, color=C["green2"],
                    line=dict(color=C["white"], width=1)),
        fill="tozeroy", fillcolor=C["green_bg"],
        hovertemplate=hov + "ET: %{y:.3f} in<extra></extra>",
    ))
    # Irrigation-need trigger
    fig_et.add_hline(
        y=0.08, line_dash="dot", line_color=C["green"], opacity=0.65,
        annotation_text="0.08 in  irrigation trigger",
        annotation_font=dict(size=9, color=C["green"], family="'JetBrains Mono', monospace"),
        annotation_position="bottom right",
    )

    return html.Div([
        html.P(
            f"16-DAY FORECAST  —  {park_name.upper()}",
            className="chart-label",
            style={"marginBottom": "16px"},
        ),
        html.Div([
            chart_card(
                "SOIL SURFACE TEMPERATURE  (°F  —  DAILY MEAN)",
                fig_temp,
                legend_items=[threshold_item("41°F  minimum for fertilizing", C["red2"])],
            ),
            chart_card(
                "SOIL MOISTURE  0–1 cm  (m³/m³  —  DAILY MEAN)",
                fig_moist,
                legend_items=[
                    threshold_item("0.40  waterlogging — mowing / planting skip", C["red2"]),
                    threshold_item("0.25  dry — irrigation trigger", C["amber2"]),
                ],
            ),
        ], className="grid-2"),
        html.Div([
            chart_card(
                "DAILY PRECIPITATION  (INCHES  —  DAILY TOTAL)",
                fig_precip,
                legend_items=[threshold_item("0.20 in  irrigation skip threshold", C["amber2"])],
            ),
            chart_card(
                "EVAPOTRANSPIRATION  (INCHES  —  DAILY TOTAL)",
                fig_et,
                legend_items=[threshold_item("0.08 in  irrigation need trigger", C["green"])],
            ),
        ], className="grid-2"),
    ])


# ── Explorer: stats, chart, table ─────────────────────────────────────────────
@callback(
    Output("exp-kpis",  "children"),
    Output("exp-chart", "children"),
    Output("exp-table", "children"),
    Input("exp-parks",  "value"),
    Input("exp-tasks",  "value"),
    Input("exp-recs",   "value"),
    Input("exp-dates",  "start_date"),
    Input("exp-dates",  "end_date"),
)
def update_explorer(parks, tasks, recs, start, end):
    if not start or not end:
        no_date = empty_state("Select a date range above.")
        return [], no_date, html.Div()

    df = get_schedule(
        start[:10], end[:10],
        park_ids=parks or None,
        task_ids=tasks or None,
        recs=recs     or None,
    )

    if df.empty:
        no_data = empty_state("No records match these filters.")
        return [kpi("Results", "0", "neutral")], no_data, html.Div()

    n_tot  = len(df)
    n_rec  = int((df["recommendation"] == "recommended").sum())
    n_def  = int((df["recommendation"] == "deferred").sum())
    n_skip = int((df["recommendation"] == "skipped").sum())

    kpis = [
        kpi("Total",       str(n_tot),  "neutral"),
        kpi("Recommended", str(n_rec),  "green"),
        kpi("Deferred",    str(n_def),  "amber"),
        kpi("Skipped",     str(n_skip), "red"),
    ]

    # Stacked bar — daily breakdown
    daily = (
        df.groupby(["schedule_date", "recommendation"])
        .size().unstack(fill_value=0).reset_index()
    )
    daily["schedule_date"] = pd.to_datetime(daily["schedule_date"])

    fig = new_fig(barmode="stack", height=240)
    for rec, colour in [("recommended", C["green2"]),
                         ("deferred",    C["amber2"]),
                         ("skipped",     C["red2"])]:
        if rec in daily.columns:
            fig.add_trace(go.Bar(
                name=rec.title(), x=daily["schedule_date"], y=daily[rec],
                marker_color=colour,
                hovertemplate="%{x|%b %d}<br>" + rec.title() + ": %{y}<extra></extra>",
            ))

    chart = html.Div([
        html.P("DAILY RECOMMENDATION BREAKDOWN", className="chart-label"),
        dcc.Graph(figure=fig, config={"displayModeBar": False},
                  style={"height": "240px"}),
    ], className="card")

    # Detail table
    tdf = df[["schedule_date", "park_name", "task_name",
              "category", "recommendation", "reason"]].copy()
    tdf.columns = ["Date", "Park", "Task", "Category", "Recommendation", "Reason"]
    tdf["Date"] = tdf["Date"].astype(str)

    table = html.Div([
        html.P("SCHEDULE RECORDS", className="chart-label"),
        dash_table.DataTable(
            data=tdf.to_dict("records"),
            columns=[{"name": c, "id": c} for c in tdf.columns],
            **TABLE_KWARGS,
            tooltip_data=[
                {col: {"value": str(row[col]), "type": "markdown"}
                 for col in tdf.columns}
                for row in tdf.to_dict("records")
            ],
            tooltip_duration=None,
        ),
    ], className="card")

    return kpis, chart, table


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=8050)
