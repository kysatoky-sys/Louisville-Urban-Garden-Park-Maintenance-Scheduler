"""
app.py — Dash Dashboard Layer
Louisville Urban Garden & Park Maintenance Scheduler

Connects to the same Supabase PostgreSQL database populated by load.py
and renders an interactive operations dashboard for park maintenance staff.

Pages / views:
  - Today's Schedule   — recommendation cards per park × task for today
  - Weather Forecast   — 16-day forecast charts per park
  - Task Explorer      — filter any park/task/date range; see all decisions
  - Park Overview      — map + summary stats per park

Run:
    python app.py

Environment variables (.env):
    SUPABASE_DB_URL   or   DB_PASSWORD + DB_REF
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

import dash
from dash import dcc, html, Input, Output, callback, dash_table
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# ── Environment ────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")


def get_database_url() -> str:
    url = os.getenv("SUPABASE_DB_URL")
    if url:
        return url
    pw  = os.getenv("DB_PASSWORD")
    ref = os.getenv("DB_REF")
    if not pw or not ref:
        raise RuntimeError("Set SUPABASE_DB_URL or both DB_PASSWORD and DB_REF in .env")
    return f"postgresql+psycopg2://postgres:{pw}@db.{ref}.supabase.co:5432/postgres"


ENGINE = create_engine(get_database_url(), pool_pre_ping=True, pool_size=3)


# ── Data helpers ───────────────────────────────────────────────────────────────

def query(sql: str, params: dict | None = None) -> pd.DataFrame:
    with ENGINE.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def load_parks() -> pd.DataFrame:
    return query("SELECT * FROM public.park ORDER BY name")


def load_tasks() -> pd.DataFrame:
    return query(
        "SELECT task_id, task_name, category, description, frequency_days "
        "FROM public.maintenance_task ORDER BY task_name"
    )


def load_schedule(start: str, end: str,
                  park_ids: list | None = None,
                  task_ids: list | None = None) -> pd.DataFrame:
    where_parts = ["ms.schedule_date BETWEEN :start AND :end"]
    params: dict = {"start": start, "end": end}

    if park_ids:
        where_parts.append("ms.park_id = ANY(:park_ids)")
        params["park_ids"] = park_ids
    if task_ids:
        where_parts.append("ms.task_id = ANY(:task_ids)")
        params["task_ids"] = task_ids

    where = " AND ".join(where_parts)
    sql = f"""
        SELECT
            ms.schedule_id,
            ms.park_id,
            p.name        AS park_name,
            p.location    AS district,
            p.park_type,
            ms.task_id,
            mt.task_name,
            mt.category,
            ms.schedule_date,
            ms.recommendation,
            ms.reason
        FROM public.maintenance_schedule ms
        JOIN public.park             p  ON p.park_id  = ms.park_id
        JOIN public.maintenance_task mt ON mt.task_id = ms.task_id
        WHERE {where}
        ORDER BY ms.schedule_date, p.name, mt.task_name
    """
    return query(sql, params)


def load_forecast(park_id: int | None = None) -> pd.DataFrame:
    if park_id:
        sql = """
            SELECT wf.*, p.name AS park_name
            FROM public.weather_forecast wf
            JOIN public.park p ON p.park_id = wf.park_id
            WHERE wf.park_id = :pid
            ORDER BY wf.forecast_date
        """
        return query(sql, {"pid": park_id})
    sql = """
        SELECT wf.*, p.name AS park_name
        FROM public.weather_forecast wf
        JOIN public.park p ON p.park_id = wf.park_id
        ORDER BY p.name, wf.forecast_date
    """
    return query(sql)


# ── Colour palette & design tokens ────────────────────────────────────────────
PALETTE = {
    "bg":           "#0f1a14",
    "surface":      "#172219",
    "surface2":     "#1e2d22",
    "border":       "#2d4a35",
    "green_bright": "#4ade80",
    "green_mid":    "#22c55e",
    "green_dim":    "#16a34a",
    "amber":        "#fbbf24",
    "red":          "#f87171",
    "blue":         "#60a5fa",
    "text":         "#e2f0e8",
    "text_dim":     "#8aab93",
    "text_muted":   "#4d7a5a",
}

REC_COLOURS = {
    "recommended": PALETTE["green_bright"],
    "deferred":    PALETTE["amber"],
    "skipped":     PALETTE["red"],
}

REC_BG = {
    "recommended": "#052e16",
    "deferred":    "#1c1100",
    "skipped":     "#1c0707",
}

PLOTLY_TEMPLATE = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="DM Mono, monospace", color=PALETTE["text_dim"], size=11),
        colorway=[PALETTE["green_bright"], PALETTE["amber"], PALETTE["blue"],
                  PALETTE["red"], "#a78bfa", "#fb923c"],
        xaxis=dict(gridcolor=PALETTE["border"], linecolor=PALETTE["border"],
                   tickfont=dict(size=10)),
        yaxis=dict(gridcolor=PALETTE["border"], linecolor=PALETTE["border"],
                   tickfont=dict(size=10)),
        legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor=PALETTE["border"],
                    borderwidth=1),
        margin=dict(l=50, r=20, t=40, b=40),
    )
)


# ── Reusable UI components ─────────────────────────────────────────────────────

def stat_card(label: str, value: str, colour: str = PALETTE["green_bright"],
              sub: str = "") -> html.Div:
    return html.Div([
        html.P(label, className="stat-label"),
        html.P(value, className="stat-value", style={"color": colour}),
        html.P(sub, className="stat-sub") if sub else None,
    ], className="stat-card")


def rec_badge(rec: str) -> html.Span:
    colour = REC_COLOURS.get(rec, PALETTE["text_dim"])
    icons  = {"recommended": "✦", "deferred": "◈", "skipped": "✕"}
    return html.Span(
        [icons.get(rec, "·"), " ", rec],
        style={
            "color": colour,
            "background": REC_BG.get(rec, PALETTE["surface2"]),
            "border": f"1px solid {colour}40",
            "borderRadius": "4px",
            "padding": "2px 8px",
            "fontSize": "11px",
            "fontFamily": "DM Mono, monospace",
            "letterSpacing": "0.05em",
            "textTransform": "uppercase",
            "whiteSpace": "nowrap",
        }
    )


def section_header(title: str, subtitle: str = "") -> html.Div:
    return html.Div([
        html.H2(title, className="section-title"),
        html.P(subtitle, className="section-sub") if subtitle else None,
    ], className="section-header")


# ── App initialisation ─────────────────────────────────────────────────────────
app = dash.Dash(
    __name__,
    title="Louisville Park Maintenance",
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
    suppress_callback_exceptions=True,
)


# ── Layout ─────────────────────────────────────────────────────────────────────
app.layout = html.Div([

    # ── Global style injection ─────────────────────────────────────────────
    html.Style("""
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,400&family=Spectral:ital,wght@0,300;0,600;0,700;1,300;1,600&display=swap');

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background: #0f1a14;
            color: #e2f0e8;
            font-family: 'DM Mono', monospace;
            font-size: 13px;
            line-height: 1.6;
            min-height: 100vh;
        }

        /* ── Sidebar ─────────────────────────────── */
        .sidebar {
            position: fixed; top: 0; left: 0; bottom: 0; width: 220px;
            background: #0c1410;
            border-right: 1px solid #2d4a35;
            display: flex; flex-direction: column;
            z-index: 100; padding: 0;
        }
        .sidebar-logo {
            padding: 28px 24px 20px;
            border-bottom: 1px solid #1e2d22;
        }
        .sidebar-logo h1 {
            font-family: 'Spectral', serif;
            font-size: 16px; font-weight: 600; font-style: italic;
            color: #4ade80; line-height: 1.3;
            letter-spacing: -0.01em;
        }
        .sidebar-logo p {
            font-size: 10px; color: #4d7a5a;
            text-transform: uppercase; letter-spacing: 0.12em;
            margin-top: 4px;
        }
        .sidebar-nav { padding: 16px 12px; flex: 1; }
        .nav-section-label {
            font-size: 9px; color: #4d7a5a;
            text-transform: uppercase; letter-spacing: 0.15em;
            padding: 8px 12px 6px; margin-top: 8px;
        }
        .nav-item {
            display: flex; align-items: center; gap: 10px;
            padding: 9px 12px; border-radius: 6px;
            color: #8aab93; text-decoration: none;
            font-size: 12px; cursor: pointer;
            transition: all 0.15s; border: none; background: none;
            width: 100%; text-align: left; margin-bottom: 2px;
        }
        .nav-item:hover { background: #1e2d22; color: #e2f0e8; }
        .nav-item.active { background: #172219; color: #4ade80; border-left: 2px solid #4ade80; }
        .nav-icon { font-size: 14px; width: 18px; text-align: center; }
        .sidebar-footer {
            padding: 16px 24px;
            border-top: 1px solid #1e2d22;
            font-size: 10px; color: #4d7a5a;
            line-height: 1.8;
        }

        /* ── Main content ─────────────────────────── */
        .main-content {
            margin-left: 220px;
            min-height: 100vh;
            padding: 32px 36px;
        }

        /* ── Section headers ──────────────────────── */
        .section-header { margin-bottom: 24px; }
        .section-title {
            font-family: 'Spectral', serif;
            font-size: 26px; font-weight: 600; font-style: italic;
            color: #e2f0e8; line-height: 1.2;
        }
        .section-sub { font-size: 12px; color: #8aab93; margin-top: 4px; }

        /* ── Stat cards ──────────────────────────────── */
        .stat-row { display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }
        .stat-card {
            background: #172219; border: 1px solid #2d4a35;
            border-radius: 8px; padding: 18px 22px;
            flex: 1; min-width: 140px;
        }
        .stat-label { font-size: 10px; color: #4d7a5a; text-transform: uppercase; letter-spacing: 0.12em; margin-bottom: 6px; }
        .stat-value { font-size: 28px; font-family: 'Spectral', serif; font-weight: 600; line-height: 1; }
        .stat-sub { font-size: 11px; color: #8aab93; margin-top: 4px; }

        /* ── Cards ───────────────────────────────────── */
        .card {
            background: #172219; border: 1px solid #2d4a35;
            border-radius: 8px; padding: 20px 24px; margin-bottom: 20px;
        }
        .card-title {
            font-size: 11px; color: #4d7a5a;
            text-transform: uppercase; letter-spacing: 0.12em;
            margin-bottom: 14px;
        }

        /* ── Grid layouts ────────────────────────────── */
        .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .three-col { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }

        /* ── Schedule cards ──────────────────────────── */
        .schedule-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 14px; margin-top: 16px;
        }
        .schedule-card {
            background: #172219; border: 1px solid #2d4a35;
            border-radius: 8px; padding: 16px 18px;
            position: relative; overflow: hidden;
        }
        .schedule-card::before {
            content: ''; position: absolute;
            left: 0; top: 0; bottom: 0; width: 3px;
        }
        .schedule-card.recommended::before { background: #4ade80; }
        .schedule-card.deferred::before    { background: #fbbf24; }
        .schedule-card.skipped::before     { background: #f87171; }
        .schedule-card-park { font-size: 13px; font-weight: 500; color: #e2f0e8; margin-bottom: 3px; }
        .schedule-card-task { font-size: 11px; color: #8aab93; margin-bottom: 10px; }
        .schedule-card-reason { font-size: 11px; color: #8aab93; margin-top: 8px; font-style: italic; line-height: 1.5; }

        /* ── Controls ────────────────────────────────── */
        .controls-bar {
            display: flex; gap: 14px; align-items: flex-end;
            margin-bottom: 24px; flex-wrap: wrap;
        }
        .control-group { display: flex; flex-direction: column; gap: 6px; min-width: 180px; }
        .control-label { font-size: 10px; color: #4d7a5a; text-transform: uppercase; letter-spacing: 0.1em; }

        /* Dropdowns */
        .Select-control, .Select-menu-outer {
            background: #172219 !important; border-color: #2d4a35 !important;
            color: #e2f0e8 !important;
        }
        .Select-value-label, .Select-placeholder { color: #8aab93 !important; }
        .Select-option { background: #172219 !important; color: #e2f0e8 !important; }
        .Select-option:hover, .Select-option.is-focused { background: #1e2d22 !important; }
        .VirtualizedSelectOption { background: #172219 !important; }
        .DateInput_input { background: #172219 !important; color: #e2f0e8 !important; border-bottom-color: #2d4a35 !important; font-family: 'DM Mono', monospace !important; font-size: 12px !important; }
        .DateRangePickerInput { background: #172219 !important; border-color: #2d4a35 !important; }
        .CalendarMonth, .DayPicker { background: #172219 !important; }

        /* ── Data table ──────────────────────────────── */
        .dash-table-container .dash-spreadsheet-container .dash-spreadsheet-inner table {
            font-family: 'DM Mono', monospace !important; font-size: 12px !important;
        }

        /* ── Weather chart tabs ───────────────────────── */
        .tab-bar {
            display: flex; gap: 6px; margin-bottom: 20px; flex-wrap: wrap;
        }
        .tab-btn {
            padding: 6px 14px; border-radius: 4px;
            font-family: 'DM Mono', monospace; font-size: 11px;
            border: 1px solid #2d4a35; background: #172219; color: #8aab93;
            cursor: pointer; transition: all 0.15s;
            text-transform: uppercase; letter-spacing: 0.08em;
        }
        .tab-btn:hover { background: #1e2d22; color: #e2f0e8; }
        .tab-btn.active { background: #1e2d22; color: #4ade80; border-color: #4ade80; }

        /* ── Page transition ─────────────────────────── */
        .page-content { animation: fadeIn 0.2s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

        /* ── Scrollbar ───────────────────────────────── */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #0c1410; }
        ::-webkit-scrollbar-thumb { background: #2d4a35; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #4d7a5a; }

        /* ── Divider ─────────────────────────────────── */
        .divider { height: 1px; background: #2d4a35; margin: 20px 0; }

        /* ── Tag pills ───────────────────────────────── */
        .tag {
            display: inline-block; padding: 2px 8px; border-radius: 3px;
            font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em;
            border: 1px solid; margin-right: 4px;
        }
        .tag-vegetation { color: #4ade80; border-color: #4ade8050; background: #4ade8010; }
        .tag-water      { color: #60a5fa; border-color: #60a5fa50; background: #60a5fa10; }
        .tag-soil       { color: #fb923c; border-color: #fb923c50; background: #fb923c10; }
    """),

    # ── Sidebar ────────────────────────────────────────────────────────────
    html.Div([
        html.Div([
            html.H1("Louisville Park\nMaintenance"),
            html.P("Operations Dashboard"),
        ], className="sidebar-logo"),

        html.Nav([
            html.P("Views", className="nav-section-label"),
            html.Button([html.Span("⬡", className="nav-icon"), "Today's Schedule"],
                id="nav-schedule", className="nav-item active",
                n_clicks=0),
            html.Button([html.Span("⌀", className="nav-icon"), "Weather Forecast"],
                id="nav-weather", className="nav-item",
                n_clicks=0),
            html.Button([html.Span("⊞", className="nav-icon"), "Task Explorer"],
                id="nav-explorer", className="nav-item",
                n_clicks=0),
            html.Button([html.Span("◉", className="nav-icon"), "Park Overview"],
                id="nav-parks", className="nav-item",
                n_clicks=0),
        ], className="sidebar-nav"),

        html.Div([
            html.P("Louisville, KY"),
            html.P("Open-Meteo API"),
            html.P("Supabase PostgreSQL"),
        ], className="sidebar-footer"),
    ], className="sidebar"),

    # ── Main content ───────────────────────────────────────────────────────
    html.Div([
        dcc.Store(id="active-page", data="schedule"),
        html.Div(id="page-content", className="page-content"),
    ], className="main-content"),

], style={"minHeight": "100vh"})


# ══════════════════════════════════════════════════════════════════════════════
# PAGE RENDERERS
# ══════════════════════════════════════════════════════════════════════════════

def render_schedule_page() -> html.Div:
    today_str = str(date.today())
    df = load_schedule(today_str, today_str)

    total    = len(df)
    rec_cnt  = (df["recommendation"] == "recommended").sum()
    def_cnt  = (df["recommendation"] == "deferred").sum()
    skip_cnt = (df["recommendation"] == "skipped").sum()

    # Cards
    cards = []
    for _, row in df.iterrows():
        rec = row["recommendation"]
        cards.append(
            html.Div([
                html.Div([
                    html.Div(row["park_name"], className="schedule-card-park"),
                    html.Div(row["task_name"], className="schedule-card-task"),
                    rec_badge(rec),
                    html.Div(row["reason"] or "All conditions favorable.",
                             className="schedule-card-reason"),
                ])
            ], className=f"schedule-card {rec}")
        )

    if not cards:
        cards = [html.P("No schedule data found for today. Run load.py first.",
                        style={"color": PALETTE["text_muted"], "padding": "40px 0"})]

    return html.Div([
        section_header("Today's Schedule",
                       f"{today_str}  ·  {total} task evaluations across all parks"),
        html.Div([
            stat_card("Total Tasks",    str(total),    PALETTE["text"]),
            stat_card("Recommended",   str(rec_cnt),  PALETTE["green_bright"]),
            stat_card("Deferred",      str(def_cnt),  PALETTE["amber"]),
            stat_card("Skipped",       str(skip_cnt), PALETTE["red"]),
        ], className="stat-row"),

        html.Div([
            html.Div([
                html.P("FILTER BY RECOMMENDATION", className="card-title"),
                dcc.Dropdown(
                    id="sched-filter-rec",
                    options=[{"label": r.title(), "value": r}
                             for r in ["recommended", "deferred", "skipped"]],
                    multi=True, placeholder="All",
                    style={"fontFamily": "DM Mono, monospace", "fontSize": "12px"},
                ),
            ], style={"maxWidth": "360px"}),
        ], style={"marginBottom": "20px"}),

        html.Div(cards, className="schedule-grid", id="schedule-cards"),
    ])


def render_weather_page() -> html.Div:
    parks_df = load_parks()
    park_options = [{"label": r["name"], "value": r["park_id"]}
                    for _, r in parks_df.iterrows()]
    default_park = int(parks_df.iloc[0]["park_id"]) if len(parks_df) > 0 else None

    return html.Div([
        section_header("Weather Forecast",
                       "16-day hourly-aggregated Open-Meteo data per park"),
        html.Div([
            html.Div([
                html.P("PARK", className="control-label"),
                dcc.Dropdown(
                    id="wx-park-select",
                    options=park_options,
                    value=default_park,
                    clearable=False,
                    style={"fontFamily": "DM Mono, monospace", "fontSize": "12px"},
                ),
            ], className="control-group"),
        ], className="controls-bar"),

        html.Div(id="wx-charts"),
    ])


def render_explorer_page() -> html.Div:
    parks_df = load_parks()
    tasks_df  = load_tasks()
    today     = date.today()
    start_def = str(today)
    end_def   = str(today + timedelta(days=6))

    park_opts = [{"label": r["name"], "value": r["park_id"]} for _, r in parks_df.iterrows()]
    task_opts = [{"label": r["task_name"], "value": r["task_id"]} for _, r in tasks_df.iterrows()]

    return html.Div([
        section_header("Task Explorer", "Filter the full maintenance schedule by park, task, or date range"),

        html.Div([
            html.Div([
                html.P("PARKS", className="control-label"),
                dcc.Dropdown(id="exp-parks", options=park_opts, multi=True,
                             placeholder="All parks",
                             style={"fontFamily": "DM Mono, monospace", "fontSize": "12px"}),
            ], className="control-group"),
            html.Div([
                html.P("TASKS", className="control-label"),
                dcc.Dropdown(id="exp-tasks", options=task_opts, multi=True,
                             placeholder="All tasks",
                             style={"fontFamily": "DM Mono, monospace", "fontSize": "12px"}),
            ], className="control-group"),
            html.Div([
                html.P("DATE RANGE", className="control-label"),
                dcc.DatePickerRange(
                    id="exp-dates",
                    start_date=start_def,
                    end_date=end_def,
                    display_format="YYYY-MM-DD",
                    style={"fontFamily": "DM Mono, monospace"},
                ),
            ], className="control-group"),
        ], className="controls-bar"),

        html.Div(id="exp-summary-row", className="stat-row"),
        html.Div(id="exp-chart"),
        html.Div(id="exp-table"),
    ])


def render_parks_page() -> html.Div:
    parks_df   = load_parks()
    sched_all  = load_schedule(
        str(date.today() - timedelta(days=7)),
        str(date.today() + timedelta(days=7))
    )

    rows = []
    for _, p in parks_df.iterrows():
        pid  = p["park_id"]
        psub = sched_all[sched_all["park_id"] == pid]
        rec  = (psub["recommendation"] == "recommended").sum()
        def_ = (psub["recommendation"] == "deferred").sum()
        skip = (psub["recommendation"] == "skipped").sum()
        rows.append({
            "Park":         p["name"],
            "District":     p["location"],
            "Type":         p["park_type"],
            "Area (m²)":    f"{int(p['area_size']):,}" if pd.notna(p.get("area_size")) else "—",
            "✦ Recommended": rec,
            "◈ Deferred":   def_,
            "✕ Skipped":    skip,
        })

    summary_df = pd.DataFrame(rows)

    # Donut chart — overall recommendation split across all parks
    total_rec  = (sched_all["recommendation"] == "recommended").sum()
    total_def  = (sched_all["recommendation"] == "deferred").sum()
    total_skip = (sched_all["recommendation"] == "skipped").sum()

    donut = go.Figure(go.Pie(
        labels=["Recommended", "Deferred", "Skipped"],
        values=[total_rec, total_def, total_skip],
        hole=0.6,
        marker=dict(colors=[PALETTE["green_bright"], PALETTE["amber"], PALETTE["red"]],
                    line=dict(color=PALETTE["bg"], width=3)),
        textfont=dict(family="DM Mono, monospace", size=11),
        hovertemplate="<b>%{label}</b><br>%{value} decisions<br>%{percent}<extra></extra>",
    ))
    donut.update_layout(
        template=PLOTLY_TEMPLATE, height=260,
        showlegend=True,
        legend=dict(orientation="v", x=1.05, y=0.5),
        annotations=[dict(text=f"{total_rec+total_def+total_skip}<br><span style='font-size:10px'>decisions</span>",
                          x=0.5, y=0.5, font=dict(size=18, family="Spectral, serif",
                                                    color=PALETTE["text"]),
                          showarrow=False)],
        margin=dict(l=10, r=80, t=20, b=10),
    )

    # Bar chart — by task
    task_counts = sched_all.groupby(["task_name", "recommendation"]).size().unstack(fill_value=0)
    bar_fig = go.Figure()
    for rec, colour in REC_COLOURS.items():
        if rec in task_counts.columns:
            bar_fig.add_trace(go.Bar(
                name=rec.title(), x=task_counts.index,
                y=task_counts[rec], marker_color=colour,
                hovertemplate="%{x}<br>" + rec.title() + ": %{y}<extra></extra>",
            ))
    bar_fig.update_layout(
        template=PLOTLY_TEMPLATE, barmode="stack", height=260,
        xaxis_title=None, yaxis_title="Decisions",
        showlegend=True, margin=dict(l=40, r=10, t=20, b=60),
    )

    return html.Div([
        section_header("Park Overview",
                       "Summary of all parks with ±7-day recommendation breakdown"),

        html.Div([
            html.Div([
                stat_card("Parks", str(len(parks_df)), PALETTE["text"]),
            ], className="stat-card", style={"flex": "0 0 auto", "minWidth": "100px",
                                              "background": PALETTE["surface"],
                                              "border": f"1px solid {PALETTE['border']}",
                                              "borderRadius": "8px", "padding": "18px 22px"}),
            stat_card("Total Decisions", str(len(sched_all)),     PALETTE["text"]),
            stat_card("Recommended",     str(total_rec),          PALETTE["green_bright"]),
            stat_card("Deferred",        str(total_def),          PALETTE["amber"]),
            stat_card("Skipped",         str(total_skip),         PALETTE["red"]),
        ], className="stat-row"),

        html.Div([
            html.Div([
                html.P("RECOMMENDATION MIX  (±7 days)", className="card-title"),
                dcc.Graph(figure=donut, config={"displayModeBar": False}),
            ], className="card"),
            html.Div([
                html.P("DECISIONS BY TASK  (±7 days)", className="card-title"),
                dcc.Graph(figure=bar_fig, config={"displayModeBar": False}),
            ], className="card"),
        ], className="two-col"),

        html.Div([
            html.P("ALL PARKS", className="card-title"),
            dash_table.DataTable(
                data=summary_df.to_dict("records"),
                columns=[{"name": c, "id": c} for c in summary_df.columns],
                style_table={"overflowX": "auto"},
                style_header={
                    "backgroundColor": PALETTE["surface2"],
                    "color": PALETTE["text_dim"],
                    "fontFamily": "DM Mono, monospace",
                    "fontSize": "10px",
                    "fontWeight": "500",
                    "textTransform": "uppercase",
                    "letterSpacing": "0.1em",
                    "border": f"1px solid {PALETTE['border']}",
                    "padding": "10px 14px",
                },
                style_cell={
                    "backgroundColor": PALETTE["surface"],
                    "color": PALETTE["text"],
                    "fontFamily": "DM Mono, monospace",
                    "fontSize": "12px",
                    "border": f"1px solid {PALETTE['border']}",
                    "padding": "10px 14px",
                    "textAlign": "left",
                },
                style_data_conditional=[
                    {"if": {"row_index": "odd"},
                     "backgroundColor": PALETTE["bg"]},
                ],
                sort_action="native",
                page_size=12,
            ),
        ], className="card"),
    ])


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════

# ── Navigation ─────────────────────────────────────────────────────────────────
@callback(
    Output("active-page", "data"),
    Output("nav-schedule", "className"),
    Output("nav-weather",  "className"),
    Output("nav-explorer", "className"),
    Output("nav-parks",    "className"),
    Input("nav-schedule", "n_clicks"),
    Input("nav-weather",  "n_clicks"),
    Input("nav-explorer", "n_clicks"),
    Input("nav-parks",    "n_clicks"),
    prevent_initial_call=False,
)
def update_nav(n1, n2, n3, n4):
    ctx   = dash.callback_context
    page  = "schedule"
    if ctx.triggered:
        btn_id = ctx.triggered[0]["prop_id"].split(".")[0]
        mapping = {
            "nav-schedule": "schedule",
            "nav-weather":  "weather",
            "nav-explorer": "explorer",
            "nav-parks":    "parks",
        }
        page = mapping.get(btn_id, "schedule")

    def cls(name):
        return "nav-item active" if name == page else "nav-item"

    return page, cls("schedule"), cls("weather"), cls("explorer"), cls("parks")


@callback(Output("page-content", "children"), Input("active-page", "data"))
def render_page(page):
    if page == "weather":
        return render_weather_page()
    if page == "explorer":
        return render_explorer_page()
    if page == "parks":
        return render_parks_page()
    return render_schedule_page()


# ── Schedule filter ─────────────────────────────────────────────────────────────
@callback(
    Output("schedule-cards", "children"),
    Input("sched-filter-rec", "value"),
    prevent_initial_call=True,
)
def filter_schedule_cards(rec_filter):
    today_str = str(date.today())
    df = load_schedule(today_str, today_str)
    if rec_filter:
        df = df[df["recommendation"].isin(rec_filter)]

    cards = []
    for _, row in df.iterrows():
        rec = row["recommendation"]
        cards.append(
            html.Div([
                html.Div(row["park_name"], className="schedule-card-park"),
                html.Div(row["task_name"], className="schedule-card-task"),
                rec_badge(rec),
                html.Div(row["reason"] or "All conditions favorable.",
                         className="schedule-card-reason"),
            ], className=f"schedule-card {rec}")
        )

    if not cards:
        return [html.P("No tasks match the selected filter.",
                       style={"color": PALETTE["text_muted"], "padding": "20px 0"})]
    return cards


# ── Weather charts ─────────────────────────────────────────────────────────────
@callback(Output("wx-charts", "children"), Input("wx-park-select", "value"))
def update_weather_charts(park_id):
    if not park_id:
        return html.P("Select a park above.", style={"color": PALETTE["text_muted"]})

    df = load_forecast(int(park_id))
    if df.empty:
        return html.P("No forecast data for this park. Run extract_transform.py first.",
                      style={"color": PALETTE["text_muted"]})

    df["forecast_date"] = pd.to_datetime(df["forecast_date"])
    park_name = df["park_name"].iloc[0]

    # ── Chart 1: Soil Temperature ──────────────────────────────────────────
    fig_temp = go.Figure()
    fig_temp.add_trace(go.Scatter(
        x=df["forecast_date"], y=df["soil_temperature_0cm"],
        mode="lines+markers",
        line=dict(color=PALETTE["amber"], width=2),
        marker=dict(size=5),
        fill="tozeroy", fillcolor=f"{PALETTE['amber']}15",
        name="Soil Temp (°F)",
        hovertemplate="<b>%{x|%b %d}</b><br>Soil Temp: %{y:.1f}°F<extra></extra>",
    ))
    fig_temp.update_layout(template=PLOTLY_TEMPLATE, height=200,
                           yaxis_title="°F", showlegend=False,
                           title=dict(text="Soil Temperature (°F)", font=dict(size=12,
                                      family="DM Mono, monospace", color=PALETTE["text_dim"])))

    # ── Chart 2: Soil Moisture ─────────────────────────────────────────────
    fig_moist = go.Figure()
    fig_moist.add_trace(go.Scatter(
        x=df["forecast_date"], y=df["soil_moisture_0_to_1cm"],
        mode="lines+markers",
        line=dict(color=PALETTE["blue"], width=2),
        marker=dict(size=5),
        fill="tozeroy", fillcolor=f"{PALETTE['blue']}15",
        name="Soil Moisture",
        hovertemplate="<b>%{x|%b %d}</b><br>Moisture: %{y:.3f} m³/m³<extra></extra>",
    ))
    # Waterlogging threshold line at 0.4
    fig_moist.add_hline(y=0.4, line_dash="dot",
                         line_color=PALETTE["red"], opacity=0.6,
                         annotation_text="waterlog threshold (0.40)",
                         annotation_font=dict(size=10, color=PALETTE["red"]))
    fig_moist.update_layout(template=PLOTLY_TEMPLATE, height=200,
                             yaxis_title="m³/m³", showlegend=False,
                             title=dict(text="Soil Moisture (0–1 cm)", font=dict(size=12,
                                        family="DM Mono, monospace", color=PALETTE["text_dim"])))

    # ── Chart 3: Precipitation ─────────────────────────────────────────────
    fig_precip = go.Figure()
    fig_precip.add_trace(go.Bar(
        x=df["forecast_date"], y=df["precipitation"],
        marker_color=PALETTE["blue"],
        opacity=0.75,
        name="Precipitation",
        hovertemplate="<b>%{x|%b %d}</b><br>Precip: %{y:.2f} in<extra></extra>",
    ))
    fig_precip.update_layout(template=PLOTLY_TEMPLATE, height=200,
                              yaxis_title="inches", showlegend=False,
                              title=dict(text="Daily Precipitation (inches)", font=dict(size=12,
                                         family="DM Mono, monospace", color=PALETTE["text_dim"])))

    # ── Chart 4: Evapotranspiration ────────────────────────────────────────
    fig_et = go.Figure()
    fig_et.add_trace(go.Scatter(
        x=df["forecast_date"], y=df["evapotranspiration"],
        mode="lines+markers",
        line=dict(color=PALETTE["green_bright"], width=2),
        marker=dict(size=5),
        fill="tozeroy", fillcolor=f"{PALETTE['green_bright']}15",
        name="ET (inches)",
        hovertemplate="<b>%{x|%b %d}</b><br>ET: %{y:.3f} in<extra></extra>",
    ))
    fig_et.update_layout(template=PLOTLY_TEMPLATE, height=200,
                          yaxis_title="inches", showlegend=False,
                          title=dict(text="Evapotranspiration (inches)", font=dict(size=12,
                                     family="DM Mono, monospace", color=PALETTE["text_dim"])))

    return html.Div([
        html.P(f"FORECAST FOR {park_name.upper()}", className="card-title"),
        html.Div([
            html.Div([dcc.Graph(figure=fig_temp,  config={"displayModeBar": False})], className="card"),
            html.Div([dcc.Graph(figure=fig_moist, config={"displayModeBar": False})], className="card"),
        ], className="two-col"),
        html.Div([
            html.Div([dcc.Graph(figure=fig_precip, config={"displayModeBar": False})], className="card"),
            html.Div([dcc.Graph(figure=fig_et,     config={"displayModeBar": False})], className="card"),
        ], className="two-col"),
    ])


# ── Task Explorer callbacks ────────────────────────────────────────────────────
@callback(
    Output("exp-summary-row", "children"),
    Output("exp-chart", "children"),
    Output("exp-table", "children"),
    Input("exp-parks", "value"),
    Input("exp-tasks",  "value"),
    Input("exp-dates",  "start_date"),
    Input("exp-dates",  "end_date"),
)
def update_explorer(parks, tasks, start, end):
    if not start or not end:
        return [], html.Div(), html.Div()

    df = load_schedule(start[:10], end[:10],
                       park_ids=parks or None,
                       task_ids=tasks or None)

    if df.empty:
        return (
            [stat_card("Results", "0", PALETTE["text_muted"])],
            html.Div(),
            html.P("No records match the selected filters.",
                   style={"color": PALETTE["text_muted"], "padding": "20px 0"}),
        )

    total    = len(df)
    rec_cnt  = (df["recommendation"] == "recommended").sum()
    def_cnt  = (df["recommendation"] == "deferred").sum()
    skip_cnt = (df["recommendation"] == "skipped").sum()

    summary = [
        stat_card("Total",       str(total),    PALETTE["text"]),
        stat_card("Recommended", str(rec_cnt),  PALETTE["green_bright"]),
        stat_card("Deferred",    str(def_cnt),  PALETTE["amber"]),
        stat_card("Skipped",     str(skip_cnt), PALETTE["red"]),
    ]

    # Stacked bar by date
    daily = (df.groupby(["schedule_date", "recommendation"])
               .size().unstack(fill_value=0).reset_index())
    daily["schedule_date"] = pd.to_datetime(daily["schedule_date"])

    fig = go.Figure()
    for rec, colour in REC_COLOURS.items():
        if rec in daily.columns:
            fig.add_trace(go.Bar(
                x=daily["schedule_date"], y=daily[rec],
                name=rec.title(), marker_color=colour,
                hovertemplate="%{x|%b %d}<br>" + rec.title() + ": %{y}<extra></extra>",
            ))
    fig.update_layout(template=PLOTLY_TEMPLATE, barmode="stack", height=260,
                       xaxis_title=None, yaxis_title="Tasks",
                       showlegend=True, margin=dict(l=40, r=10, t=20, b=40))

    chart = html.Div([
        html.P("DAILY RECOMMENDATION BREAKDOWN", className="card-title"),
        dcc.Graph(figure=fig, config={"displayModeBar": False}),
    ], className="card")

    # Table
    table_df = df[["schedule_date", "park_name", "task_name",
                    "category", "recommendation", "reason"]].copy()
    table_df.columns = ["Date", "Park", "Task", "Category", "Recommendation", "Reason"]
    table_df["Date"] = table_df["Date"].astype(str)

    tbl = html.Div([
        html.P("SCHEDULE RECORDS", className="card-title"),
        dash_table.DataTable(
            data=table_df.to_dict("records"),
            columns=[{"name": c, "id": c} for c in table_df.columns],
            style_table={"overflowX": "auto"},
            style_header={
                "backgroundColor": PALETTE["surface2"],
                "color": PALETTE["text_dim"],
                "fontFamily": "DM Mono, monospace",
                "fontSize": "10px",
                "fontWeight": "500",
                "textTransform": "uppercase",
                "letterSpacing": "0.1em",
                "border": f"1px solid {PALETTE['border']}",
                "padding": "10px 14px",
            },
            style_cell={
                "backgroundColor": PALETTE["surface"],
                "color": PALETTE["text"],
                "fontFamily": "DM Mono, monospace",
                "fontSize": "12px",
                "border": f"1px solid {PALETTE['border']}",
                "padding": "10px 14px",
                "textAlign": "left",
                "maxWidth": "320px",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
            },
            style_data_conditional=[
                {"if": {"filter_query": '{Recommendation} = "recommended"',
                         "column_id": "Recommendation"},
                 "color": PALETTE["green_bright"]},
                {"if": {"filter_query": '{Recommendation} = "deferred"',
                         "column_id": "Recommendation"},
                 "color": PALETTE["amber"]},
                {"if": {"filter_query": '{Recommendation} = "skipped"',
                         "column_id": "Recommendation"},
                 "color": PALETTE["red"]},
                {"if": {"row_index": "odd"}, "backgroundColor": PALETTE["bg"]},
            ],
            sort_action="native",
            filter_action="native",
            page_size=15,
            tooltip_data=[
                {col: {"value": str(row[col]), "type": "markdown"}
                 for col in table_df.columns}
                for row in table_df.to_dict("records")
            ],
            tooltip_duration=None,
        ),
    ], className="card")

    return summary, chart, tbl


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8050)
