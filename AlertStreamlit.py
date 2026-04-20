"""
AlertStreamlit.py – Online Structural Monitoring Dashboard
-----------------------------------------------------------
Reads XML alert records via Google Drive API.

Dependencies (requirements.txt):
    streamlit
    pandas
    plotly
    google-auth
    google-auth-httplib2
    google-api-python-client
"""

from __future__ import annotations

import io
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    GDRIVE_OK = True
except ImportError:
    GDRIVE_OK = False

try:
    from influxdb_client import InfluxDBClient
    INFLUX_OK = True
except ImportError:
    INFLUX_OK = False

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Structural Alert Monitor",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)

ALERT_DIR = Path("alert_records")

LEVEL_COLORS = {
    "Warning":  "#ffaa44",
    "Critical": "#ff6b7a",
}

THEME_BG    = "#1e2130"
THEME_MID   = "#252a3d"
THEME_ACC   = "#4a9eff"
THEME_GREEN = "#4ddd88"
THEME_TEXT  = "#e8eaf0"

st.markdown("""
<style>
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background-color: #1e2130 !important;
    color: #e8eaf0 !important;
}
[data-testid="stSidebar"] { background-color: #16192b !important; }
[data-testid="metric-container"] {
    background-color: #252a3d;
    border: 1px solid #3a4060;
    border-radius: 10px;
    padding: 14px 18px;
}
[data-testid="metric-container"] label { color: #9aa0b8 !important; font-size: 0.82rem; }
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: #4a9eff !important; font-size: 1.8rem; font-weight: 700;
}
div.stButton > button {
    background-color: #4a9eff; color: white; border: none;
    border-radius: 6px; padding: 0.4rem 1.2rem; font-weight: 600;
}
div.stButton > button:hover { background-color: #2d7de8; }
h1, h2, h3, h4 { color: #e8eaf0 !important; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def parse_xml(source) -> List[dict]:
    records: List[dict] = []
    try:
        if isinstance(source, (bytes, bytearray)):
            root = ET.fromstring(source.decode("utf-8", errors="replace"))
        else:
            tree = ET.parse(str(source))
            root = tree.getroot()

        project = root.attrib.get("project", "Unknown")
        for ev in root.findall("event"):
            ts    = ev.attrib.get("timestamp", "")
            cycle = ev.attrib.get("cycle", "0")
            for v in ev.findall("violation"):
                records.append({
                    "project"    : project,
                    "timestamp"  : ts,
                    "cycle"      : int(cycle) if cycle.isdigit() else 0,
                    "rule"       : v.attrib.get("rule", ""),
                    "sensor"     : v.attrib.get("sensor", ""),
                    "dimension"  : v.attrib.get("dimension", ""),
                    "max_value"  : float(v.attrib.get("max_value", 0)),
                    "threshold"  : float(v.attrib.get("threshold", 0)),
                    "operator"   : v.attrib.get("operator", ">="),
                    "alert_level": v.attrib.get("alert_level", "Warning"),
                })
    except Exception as exc:
        st.error(f"Failed to parse XML: {exc}")
    return records


def load_df(records: List[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["date"]      = df["timestamp"].dt.date
    df["hour"]      = df["timestamp"].dt.hour
    return df.sort_values("timestamp").reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# Google Drive
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_resource(show_spinner=False)
def _get_drive_service():
    """Build Google Drive service with cached credentials."""
    sa_info = dict(st.secrets["GOOGLE_SERVICE_ACCOUNT"])
    # Fix newlines in private_key (Streamlit escapes them)
    if "private_key" in sa_info:
        sa_info["private_key"] = sa_info["private_key"].replace("\\n", "\n")
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


@st.cache_data(ttl=60, show_spinner="Listing XML files on Google Drive…")
def list_gdrive_xml_files(folder_id: str) -> List[dict]:
    """List all .xml files in the Drive folder."""
    service = _get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and name contains '.xml' and trashed=false",
        fields="files(id, name, modifiedTime, size)",
        orderBy="modifiedTime desc",
        pageSize=50,
    ).execute()
    return results.get("files", [])


@st.cache_data(ttl=120, show_spinner="Downloading XML from Google Drive…")
def fetch_gdrive_file(file_id: str) -> bytes:
    """Download a Drive file by ID."""
    service = _get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## 🏗️ Alert Monitor")
    st.markdown("---")

    source = st.radio(
        "Data Source",
        ["☁️  Google Drive", "📁  Local alert_records/", "📤  Upload XML"],
        index=0,
    )

    records: List[dict] = []

    if source == "☁️  Google Drive":
        if not GDRIVE_OK:
            st.error("google-api-python-client not installed")
        elif "GOOGLE_SERVICE_ACCOUNT" not in st.secrets or "GDRIVE_FOLDER_ID" not in st.secrets:
            st.error("Missing secrets: GOOGLE_SERVICE_ACCOUNT or GDRIVE_FOLDER_ID")
        else:
            status_ph = st.empty()

            col_ref, col_clr = st.columns(2)
            with col_ref:
                do_refresh = st.button("🔄 Refresh", use_container_width=True)
            with col_clr:
                if st.button("🗑️ Clear cache", use_container_width=True):
                    st.cache_data.clear()
                    st.rerun()

            if do_refresh:
                list_gdrive_xml_files.clear()
                fetch_gdrive_file.clear()

            folder_id = st.secrets["GDRIVE_FOLDER_ID"]["folder_id"]

            try:
                files = list_gdrive_xml_files(folder_id)

                if not files:
                    status_ph.warning("⚠️ No XML files found in the Drive folder.")
                else:
                    status_ph.success(f"✅ Connected — {len(files)} file(s) found")

                    file_labels = [
                        f"{f['name']}  ({_fmt_size(int(f.get('size', 0)))} · "
                        f"{_fmt_ts(f.get('modifiedTime', ''))})"
                        for f in files
                    ]
                    sel_idx = st.selectbox(
                        "Select XML file",
                        range(len(files)),
                        format_func=lambda i: file_labels[i],
                        index=0,
                    )
                    sel_file = files[sel_idx]
                    st.caption(
                        f"📄 **{sel_file['name']}**  \n"
                        f"Modified: {_fmt_ts(sel_file.get('modifiedTime', ''))}"
                    )
                    try:
                        xml_bytes = fetch_gdrive_file(sel_file["id"])
                        records   = parse_xml(xml_bytes)
                    except Exception as exc:
                        st.error(f"❌ Download failed: {exc}")

            except Exception as exc:
                status_ph.error(f"❌ Google Drive error: {exc}")

    elif source == "📁  Local alert_records/":
        if ALERT_DIR.exists():
            xml_files = sorted(ALERT_DIR.glob("*.xml"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
            if xml_files:
                selected = st.selectbox("Choose file", options=xml_files,
                                        format_func=lambda p: p.name)
                if selected:
                    records = parse_xml(selected)
            else:
                st.warning("No XML files found in alert_records/")
        else:
            st.warning("alert_records/ folder not found.")

    else:
        uploaded = st.file_uploader("Upload an alert XML file", type=["xml"])
        if uploaded is not None:
            records = parse_xml(uploaded.read())

    st.markdown("---")
    df_full = load_df(records)

    if records:
        st.info(f"**{len(records)}** violation records loaded")

    if not df_full.empty:
        st.markdown("### Filters")
        all_sensors = sorted(df_full["sensor"].unique())
        sel_sensors = st.multiselect("Sensors",     all_sensors, default=all_sensors)
        all_dims    = sorted(df_full["dimension"].unique())
        sel_dims    = st.multiselect("Dimensions",  all_dims,    default=all_dims)
        all_levels  = sorted(df_full["alert_level"].unique())
        sel_levels  = st.multiselect("Alert Level", all_levels,  default=all_levels)

        date_min = df_full["date"].min()
        date_max = df_full["date"].max()
        if date_min != date_max:
            dr = st.date_input("Date range", value=(date_min, date_max),
                               min_value=date_min, max_value=date_max)
            d0, d1 = (dr if isinstance(dr, (list, tuple)) and len(dr) == 2
                      else (date_min, date_max))
        else:
            d0, d1 = date_min, date_max

        mask = (
            df_full["sensor"].isin(sel_sensors) &
            df_full["dimension"].isin(sel_dims) &
            df_full["alert_level"].isin(sel_levels) &
            (df_full["date"] >= d0) &
            (df_full["date"] <= d1)
        )
        df = df_full[mask].copy()
    else:
        df = df_full


# ══════════════════════════════════════════════════════════════════════════════
# Main content
# ══════════════════════════════════════════════════════════════════════════════

st.title("🏗️ Structural Monitoring – Alert Dashboard")

if df_full.empty:
    st.info("👈 Load an XML file from the sidebar to get started.")
    st.stop()

# ── KPI metrics ───────────────────────────────────────────────────────────────
n_events     = df["cycle"].nunique()
n_violations = len(df)
n_critical   = int((df["alert_level"] == "Critical").sum())
pct_crit     = f"{100*n_critical/n_violations:.1f}%" if n_violations else "0%"
n_sensors    = df["sensor"].nunique()
project_name = df["project"].iloc[0] if "project" in df.columns else "–"

if not df["timestamp"].isna().all():
    ts_min = df["timestamp"].dropna().min().strftime("%Y-%m-%d %H:%M")
    ts_max = df["timestamp"].dropna().max().strftime("%Y-%m-%d %H:%M")
    date_range_str = f"{ts_min}  →  {ts_max}"
else:
    date_range_str = "–"

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Project",          project_name)
c2.metric("Alert Events",     f"{n_events}")
c3.metric("Total Violations", f"{n_violations}")
c4.metric("Critical",         f"{n_critical}  ({pct_crit})")
c5.metric("Sensors",          f"{n_sensors}")
st.caption(f"🕒 Date range: {date_range_str}")
st.markdown("---")

# ── Stem chart ────────────────────────────────────────────────────────────────
st.subheader("📊 Violations Over Time (Stem Chart)")

df_stem          = df.copy()
df_stem["key"]   = df_stem["sensor"] + " / " + df_stem["dimension"]
df_stem          = df_stem.sort_values("timestamp").reset_index(drop=True)
df_stem["index"] = range(len(df_stem))
stem_colors      = px.colors.qualitative.D3
stem_fig         = go.Figure()

for i, key in enumerate(df_stem["key"].unique()):
    sub   = df_stem[df_stem["key"] == key]
    color = stem_colors[i % len(stem_colors)]
    for _, row in sub.iterrows():
        stem_fig.add_trace(go.Scatter(
            x=[row["index"], row["index"]], y=[0, row["max_value"]],
            mode="lines", line=dict(color=color, width=1.5),
            showlegend=False, hoverinfo="skip",
        ))
    stem_fig.add_trace(go.Scatter(
        x=sub["index"], y=sub["max_value"],
        mode="markers", name=key,
        marker=dict(size=8, color=color),
        hovertemplate=(
            f"<b>{key}</b><br>Value: %{{y:.5f}}<br>"
            "Cycle: %{customdata[0]}<br>Level: %{customdata[1]}<br>"
            "Time: %{customdata[2]}<extra></extra>"
        ),
        customdata=sub[["cycle", "alert_level", "timestamp"]].values,
    ))

for rule_name, rule_df in df_stem.groupby("rule"):
    thr = rule_df["threshold"].iloc[0]
    lc  = LEVEL_COLORS.get(rule_df["alert_level"].iloc[0], "#ffaa44")
    stem_fig.add_hline(y=thr, line_dash="dash", line_color=lc, line_width=1.5,
                       annotation_text=f"{rule_name} ({thr:.4g})",
                       annotation_font_color=lc, annotation_font_size=10)

stem_fig.update_layout(
    paper_bgcolor=THEME_BG, plot_bgcolor=THEME_MID,
    font=dict(color=THEME_TEXT, size=11),
    legend=dict(bgcolor=THEME_MID, bordercolor="#3a4060", borderwidth=1),
    xaxis=dict(title="Alert Index",        gridcolor="#3a4060", zerolinecolor="#3a4060"),
    yaxis=dict(title="Max Absolute Value", gridcolor="#3a4060", zerolinecolor="#3a4060"),
    margin=dict(l=60, r=20, t=30, b=50), height=420,
)
st.plotly_chart(stem_fig, use_container_width=True)

# ── Max value trend ───────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📉 Max Value Trend per Sensor / Dimension")

trend_df        = (df.groupby(["timestamp","sensor","dimension"])["max_value"]
                     .max().reset_index())
trend_df["key"] = trend_df["sensor"] + " / " + trend_df["dimension"]
trend_fig = px.line(
    trend_df, x="timestamp", y="max_value", color="key",
    labels={"max_value":"Max Value","timestamp":"Time","key":"Sensor / Dim"},
    template="plotly_dark",
    color_discrete_sequence=px.colors.qualitative.D3,
)
trend_fig.update_layout(
    paper_bgcolor=THEME_BG, plot_bgcolor=THEME_MID,
    font=dict(color=THEME_TEXT, size=11),
    legend=dict(bgcolor=THEME_MID, bordercolor="#3a4060", borderwidth=1),
    xaxis=dict(gridcolor="#3a4060", zerolinecolor="#3a4060"),
    yaxis=dict(gridcolor="#3a4060", zerolinecolor="#3a4060"),
    margin=dict(l=60, r=20, t=30, b=50), height=360,
)
st.plotly_chart(trend_fig, use_container_width=True)

# ── Pie + bar ─────────────────────────────────────────────────────────────────
st.markdown("---")
left_col, right_col = st.columns([1, 2])

with left_col:
    st.subheader("🥧 Alert Level Breakdown")
    pc = df["alert_level"].value_counts()
    pie_fig = go.Figure(go.Pie(
        labels=pc.index.tolist(), values=pc.values.tolist(),
        marker=dict(colors=[LEVEL_COLORS.get(l, THEME_ACC) for l in pc.index],
                    line=dict(color=THEME_BG, width=2)),
        textinfo="label+percent", textfont=dict(color=THEME_TEXT, size=12),
        hovertemplate="%{label}: %{value} (%{percent})<extra></extra>",
    ))
    pie_fig.update_layout(paper_bgcolor=THEME_BG, font=dict(color=THEME_TEXT),
                          showlegend=False, margin=dict(l=10,r=10,t=10,b=10), height=280)
    st.plotly_chart(pie_fig, use_container_width=True)

with right_col:
    st.subheader("📈 Violations by Sensor")
    bar_df = df.groupby(["sensor","alert_level"]).size().reset_index(name="count")
    bar_fig = px.bar(bar_df, x="sensor", y="count", color="alert_level",
                     color_discrete_map=LEVEL_COLORS, template="plotly_dark",
                     labels={"count":"Violations","sensor":"Sensor","alert_level":"Level"})
    bar_fig.update_layout(
        paper_bgcolor=THEME_BG, plot_bgcolor=THEME_MID,
        font=dict(color=THEME_TEXT, size=11),
        legend=dict(bgcolor=THEME_MID, bordercolor="#3a4060", borderwidth=1, title_text="Level"),
        xaxis=dict(gridcolor="#3a4060"), yaxis=dict(gridcolor="#3a4060"),
        margin=dict(l=40,r=10,t=20,b=50), height=280,
    )
    st.plotly_chart(bar_fig, use_container_width=True)

# ── Daily heatmap ─────────────────────────────────────────────────────────────
if df["date"].nunique() > 1:
    st.markdown("---")
    st.subheader("📅 Daily Violations Heatmap")
    heat_df  = df.groupby(["date","alert_level"]).size().reset_index(name="count")
    heat_fig = px.bar(heat_df, x="date", y="count", color="alert_level",
                      color_discrete_map=LEVEL_COLORS, template="plotly_dark",
                      labels={"count":"Violations","date":"Date","alert_level":"Level"})
    heat_fig.update_layout(
        paper_bgcolor=THEME_BG, plot_bgcolor=THEME_MID,
        font=dict(color=THEME_TEXT, size=11),
        legend=dict(bgcolor=THEME_MID, bordercolor="#3a4060", title_text="Level"),
        xaxis=dict(gridcolor="#3a4060"), yaxis=dict(gridcolor="#3a4060"),
        margin=dict(l=40,r=10,t=20,b=50), height=280,
    )
    st.plotly_chart(heat_fig, use_container_width=True)

# ── Data table ────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("📋 Violation Records")

show_cols  = ["timestamp","cycle","sensor","dimension",
              "rule","max_value","threshold","operator","alert_level"]
disp_df    = df[[c for c in show_cols if c in df.columns]].copy()
disp_df["timestamp"] = disp_df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S %Z")
disp_df["max_value"] = disp_df["max_value"].map("{:.6f}".format)
disp_df["threshold"] = disp_df["threshold"].map("{:.6f}".format)


def _level_color(val: str) -> str:
    if val == "Critical": return "color: #ff6b7a; font-weight: bold"
    if val == "Warning":  return "color: #ffaa44; font-weight: bold"
    return ""


st.dataframe(disp_df.style.map(_level_color, subset=["alert_level"]),
             use_container_width=True, height=400)

# ── Downloads ─────────────────────────────────────────────────────────────────
dl1, dl2 = st.columns(2)

csv_buf = io.StringIO()
disp_df.to_csv(csv_buf, index=False)
dl1.download_button(
    "⬇️  Download CSV",
    data=csv_buf.getvalue().encode("utf-8"),
    file_name=f"alert_violations_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    mime="text/csv",
)

json_df = df[[c for c in show_cols if c in df.columns]].copy()
json_df["timestamp"] = json_df["timestamp"].astype(str)
dl2.download_button(
    "⬇️  Download JSON",
    data=json.dumps(json_df.to_dict(orient="records"), indent=2).encode("utf-8"),
    file_name=f"alert_violations_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
    mime="application/json",
)

st.markdown("---")
st.caption("Structural Monitoring Alert Dashboard  |  Powered by Streamlit + Plotly")


# ══════════════════════════════════════════════════════════════════════════════
# LIVE INFLUXDB SECTION
# Replicates the exact query + max-abs logic from Alert.py AlertEngine._do_cycle
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.title("⚡ Live InfluxDB Monitor")
st.caption("Queries InfluxDB directly and plots the max absolute value per sensor / dimension with threshold lines.")

if not INFLUX_OK:
    st.error("influxdb-client is not installed. Add `influxdb-client` to requirements.txt and redeploy.")
    st.stop()


# ── Helper functions (exact copies of Alert.py logic) ─────────────────────────

def _build_flux_query(bucket, project, fields, range_window,
                      aggregate_every, use_aggregation, max_raw_points) -> str:
    f_filter  = " or ".join(f'r["_field"] == "{f}"' for f in fields)
    keep_cols = ", ".join(f'"{f}"' for f in fields)
    agg = (f'  |> aggregateWindow(every: {aggregate_every}, fn: max, createEmpty: false)\n'
           if use_aggregation else "")
    raw = (f'  |> sort(columns: ["_time"], desc: false)\n'
           f'  |> limit(n: {max_raw_points})\n'
           if not use_aggregation else "")
    return (
        f'from(bucket: "{bucket}")\n'
        f'  |> range(start: -{range_window})\n'
        f'  |> filter(fn: (r) => r["_measurement"] == "{project}")\n'
        f'  |> filter(fn: (r) => {f_filter})\n'
        f'{agg}'
        f'  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        f'  |> keep(columns: ["_time", "device_name", {keep_cols}])\n'
        f'{raw}'
    )


def _fetch_frame(query_api, query: str) -> pd.DataFrame:
    """Exact replica of Alert.py fetch_frame (timezone-safe for Streamlit Cloud)."""
    try:
        frame = query_api.query_data_frame(query)
    except Exception:
        frame = query_api.query_data_frame(query)
    if isinstance(frame, list):
        frame = pd.concat(frame, ignore_index=True) if frame else pd.DataFrame()
    if frame.empty:
        return frame
    frame = frame.drop(
        columns=[c for c in frame.columns if c.startswith("result") or c.startswith("table")],
        errors="ignore",
    )
    frame["_time"] = pd.to_datetime(frame["_time"], errors="coerce", utc=True)
    return frame.dropna(subset=["_time"]).sort_values(["device_name", "_time"]).reset_index(drop=True)


def _compute_max_vals(frame: pd.DataFrame) -> dict:
    """
    For every sensor × dimension: find the value whose absolute value is largest.
    Returns  { sensor: { dim: float } }  — same as AlertEngine._do_cycle max_vals.
    """
    dims    = [c for c in frame.columns if c not in {"_time", "device_name"}]
    result  = {}
    for sensor in frame["device_name"].unique():
        sub = frame[frame["device_name"] == sensor]
        result[sensor] = {}
        for dim in dims:
            if dim in sub.columns:
                s = sub[dim].dropna()
                if not s.empty:
                    idx = s.abs().idxmax()
                    result[sensor][dim] = float(s.loc[idx])
    return result


def _check_thresholds(max_vals: dict, rules: list) -> list:
    """
    Apply threshold rules to max_vals dict.
    Returns list of violation dicts (same schema as Alert.py violations).
    """
    violations = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        sensors    = list(max_vals.keys())
        if rule["sensor_filter"] != "All":
            sensors = [s for s in sensors if s == rule["sensor_filter"]]
        all_dims   = {dim for sv in max_vals.values() for dim in sv}
        check_dims = all_dims if rule["dimension"] == "All" else {rule["dimension"]}
        for sensor in sensors:
            for dim in check_dims:
                mv = max_vals.get(sensor, {}).get(dim)
                if mv is None:
                    continue
                mv_abs = abs(mv)
                op     = rule["operator"]
                thr    = rule["value"]
                hit    = ((op == ">=" and mv_abs >= thr) or
                          (op == ">"  and mv_abs >  thr) or
                          (op == "==" and abs(mv_abs - thr) < 1e-9))
                if hit:
                    violations.append({
                        "rule"       : rule["name"],
                        "sensor"     : sensor,
                        "dimension"  : dim,
                        "max_value"  : mv,
                        "threshold"  : thr,
                        "operator"   : op,
                        "alert_level": rule["alert_level"],
                    })
    return violations


# ── Connection settings form ──────────────────────────────────────────────────

with st.expander("🔌 InfluxDB Connection Settings", expanded=True):
    lc1, lc2 = st.columns(2)
    with lc1:
        live_url    = st.text_input("InfluxDB URL",    value=st.session_state.get("live_url",   "http://localhost:8086"), key="live_url")
        live_org    = st.text_input("Organisation",    value=st.session_state.get("live_org",   ""),                     key="live_org")
        live_bucket = st.text_input("Bucket",          value=st.session_state.get("live_bucket",""),                     key="live_bucket")
        live_proj   = st.text_input("Project (measurement)", value=st.session_state.get("live_proj",""),                 key="live_proj")
    with lc2:
        live_token  = st.text_input("Auth Token",      value=st.session_state.get("live_token", ""), type="password",   key="live_token")
        live_fields = st.text_input("Fields (comma-separated)", value=st.session_state.get("live_fields","velx,vely,velz"), key="live_fields")
        live_range  = st.text_input("Range Window",    value=st.session_state.get("live_range", "20m"),                 key="live_range")
        live_agg_ev = st.text_input("Aggregate Every", value=st.session_state.get("live_agg_ev","1s"),                  key="live_agg_ev")
    use_agg   = st.checkbox("Use aggregateWindow (recommended)", value=True,  key="live_use_agg")
    auto_live = st.checkbox("Auto-refresh every 60 s",           value=False, key="live_auto")


# ── Threshold rules editor ────────────────────────────────────────────────────

st.markdown("### 🎯 Threshold Rules")
st.caption("Define rules to overlay on the chart. Violated bars are highlighted.")

if "live_rules" not in st.session_state:
    st.session_state.live_rules = []

# Add rule form
with st.expander("➕ Add / Edit Rule"):
    rc1, rc2, rc3, rc4, rc5, rc6 = st.columns([2, 2, 2, 1.5, 2, 1.5])
    with rc1: r_name   = st.text_input("Rule Name",    value="Rule 1",  key="_rn")
    with rc2: r_sensor = st.text_input("Sensor Filter (or 'All')", value="All", key="_rs")
    with rc3:
        all_live_dims = [f.strip() for f in live_fields.split(",") if f.strip()]
        r_dim  = st.selectbox("Dimension", ["All"] + all_live_dims, key="_rd")
    with rc4: r_op    = st.selectbox("Operator", [">=", ">", "=="], key="_ro")
    with rc5: r_val   = st.number_input("Threshold Value", value=0.001, format="%.6f", key="_rv")
    with rc6: r_level = st.selectbox("Level", ["Warning", "Critical"], key="_rl")

    if st.button("✅ Add Rule"):
        st.session_state.live_rules.append({
            "name"         : r_name,
            "sensor_filter": r_sensor,
            "dimension"    : r_dim,
            "operator"     : r_op,
            "value"        : float(r_val),
            "alert_level"  : r_level,
            "enabled"      : True,
        })
        st.rerun()

# Show and manage existing rules
if st.session_state.live_rules:
    rules_df = pd.DataFrame(st.session_state.live_rules)[
        ["name","sensor_filter","dimension","operator","value","alert_level","enabled"]
    ]
    st.dataframe(rules_df, use_container_width=True, height=150)
    del_idx = st.number_input("Delete rule # (1-based)", min_value=1,
                               max_value=len(st.session_state.live_rules),
                               step=1, key="_del_idx")
    if st.button("🗑️ Delete selected rule"):
        st.session_state.live_rules.pop(int(del_idx) - 1)
        st.rerun()
    if st.button("🗑️ Clear all rules"):
        st.session_state.live_rules = []
        st.rerun()
else:
    st.info("No rules defined yet. Add rules above to see threshold lines on the chart.")


# ── Fetch & Plot ──────────────────────────────────────────────────────────────

fetch_col, _ = st.columns([1, 4])
do_fetch = fetch_col.button("🔄 Fetch Live Data", type="primary", use_container_width=True)

if do_fetch or (auto_live and "live_last_fetch" in st.session_state
                and time.time() - st.session_state.live_last_fetch > 60):

    fields_list = [f.strip() for f in live_fields.split(",") if f.strip()]

    if not all([live_url, live_token, live_org, live_bucket, live_proj, fields_list]):
        st.error("Please fill in all connection fields before fetching.")
    else:
        with st.spinner("Connecting to InfluxDB and fetching data…"):
            try:
                client    = InfluxDBClient(
                    url     = live_url,
                    token   = live_token,
                    org     = live_org,
                    timeout = 30_000,
                )
                query_api = client.query_api()
                query     = _build_flux_query(
                    bucket         = live_bucket,
                    project        = live_proj,
                    fields         = fields_list,
                    range_window   = live_range,
                    aggregate_every= live_agg_ev,
                    use_aggregation= use_agg,
                    max_raw_points = 50_000,
                )
                frame = _fetch_frame(query_api, query)
                client.close()

                if frame.empty:
                    st.warning("Query returned no data. Check your bucket, project, and range window.")
                else:
                    max_vals = _compute_max_vals(frame)
                    violations = _check_thresholds(max_vals, st.session_state.live_rules)
                    violated_keys = {(v["sensor"], v["dimension"]) for v in violations}

                    st.session_state.live_frame     = frame
                    st.session_state.live_max_vals  = max_vals
                    st.session_state.live_violations= violations
                    st.session_state.live_last_fetch= time.time()
                    st.session_state.live_query     = query

            except Exception as exc:
                st.error(f"❌ InfluxDB error: {exc}")
                client.close() if "client" in dir() else None


# ── Render results if data is cached ─────────────────────────────────────────

if "live_max_vals" in st.session_state and st.session_state.live_max_vals:
    max_vals   = st.session_state.live_max_vals
    violations = st.session_state.get("live_violations", [])
    frame      = st.session_state.live_frame
    fetch_time = st.session_state.get("live_last_fetch")

    violated_keys = {(v["sensor"], v["dimension"]) for v in violations}

    # ── KPI row ───────────────────────────────────────────────────────────────
    n_sensors = len(max_vals)
    n_dims    = len({d for sv in max_vals.values() for d in sv})
    n_rows    = len(frame)
    n_viols   = len(violations)
    n_crit    = sum(1 for v in violations if v["alert_level"] == "Critical")
    fetch_str = datetime.utcfromtimestamp(fetch_time).strftime("%H:%M:%S UTC") if fetch_time else "–"

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Sensors",     n_sensors)
    k2.metric("Dimensions",  n_dims)
    k3.metric("Data Rows",   f"{n_rows:,}")
    k4.metric("Violations",  n_viols)
    k5.metric("Critical",    n_crit)
    st.caption(f"🕒 Last fetch: {fetch_str}  |  Range: `{live_range}`  |  Project: `{live_proj}`")

    # ── Bar chart: max abs value per sensor × dim ─────────────────────────────
    st.markdown("---")
    st.subheader("📊 Max Absolute Value per Sensor / Dimension")

    bar_rows = []
    for sensor, dims in max_vals.items():
        for dim, val in dims.items():
            is_violated = (sensor, dim) in violated_keys
            viol_info   = next(
                (v for v in violations if v["sensor"] == sensor and v["dimension"] == dim),
                None,
            )
            bar_rows.append({
                "key"       : f"{sensor} / {dim}",
                "sensor"    : sensor,
                "dimension" : dim,
                "abs_value" : abs(val),
                "raw_value" : val,
                "violated"  : is_violated,
                "level"     : viol_info["alert_level"] if viol_info else "OK",
                "threshold" : viol_info["threshold"]   if viol_info else None,
                "rule"      : viol_info["rule"]        if viol_info else "–",
            })

    bar_df = pd.DataFrame(bar_rows).sort_values(["sensor","dimension"])

    # Assign bar colours: Critical=red, Warning=orange, OK=blue
    COLOR_MAP  = {"Critical": "#ff6b7a", "Warning": "#ffaa44", "OK": "#4a9eff"}
    bar_colors = [COLOR_MAP.get(r, "#4a9eff") for r in bar_df["level"]]

    fig = go.Figure()

    # One bar per sensor/dim
    fig.add_trace(go.Bar(
        x            = bar_df["key"],
        y            = bar_df["abs_value"],
        marker_color = bar_colors,
        text         = bar_df["abs_value"].map("{:.5f}".format),
        textposition = "outside",
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Max |value|: %{y:.6f}<br>"
            "Raw value: %{customdata[0]:.6f}<br>"
            "Status: %{customdata[1]}<br>"
            "Rule: %{customdata[2]}<extra></extra>"
        ),
        customdata=bar_df[["raw_value","level","rule"]].values,
        name="Max |value|",
    ))

    # Threshold lines — one per unique (rule, dimension, threshold) combination
    added_thresholds = set()
    for rule in st.session_state.live_rules:
        thr_key = (rule["name"], rule["value"])
        if thr_key in added_thresholds:
            continue
        added_thresholds.add(thr_key)
        lc = LEVEL_COLORS.get(rule["alert_level"], "#ffaa44")
        fig.add_hline(
            y                   = rule["value"],
            line_dash           = "dash",
            line_color          = lc,
            line_width          = 2,
            annotation_text     = f"{rule['name']} ({rule['value']:.4g})",
            annotation_font_color=lc,
            annotation_font_size = 11,
            annotation_position  = "top right",
        )

    fig.update_layout(
        paper_bgcolor = THEME_BG,
        plot_bgcolor  = THEME_MID,
        font          = dict(color=THEME_TEXT, size=11),
        xaxis         = dict(
            title        = "Sensor / Dimension",
            gridcolor    = "#3a4060",
            tickangle    = -30,
        ),
        yaxis         = dict(
            title     = "Max Absolute Value",
            gridcolor = "#3a4060",
            zeroline  = True,
            zerolinecolor="#3a4060",
        ),
        margin        = dict(l=60, r=40, t=60, b=80),
        height        = 480,
        showlegend    = False,
        bargap        = 0.35,
    )

    # Legend annotation (manual, since colours encode status)
    for label, color in [("● Critical", "#ff6b7a"), ("● Warning", "#ffaa44"), ("● OK", "#4a9eff")]:
        fig.add_annotation(
            x=1, y=1.06, xref="paper", yref="paper",
            text=f'<span style="color:{color}">{label}</span>',
            showarrow=False, font=dict(size=11),
            xanchor="right" if label.startswith("● C") else (
                "center" if label.startswith("● W") else "left"
            ),
        )

    st.plotly_chart(fig, use_container_width=True)

    # ── Time-series trend per sensor/dim ──────────────────────────────────────
    st.markdown("---")
    st.subheader("📉 Raw Time-Series (selected sensors)")

    all_sensor_keys = [
        f"{sensor} / {dim}"
        for sensor in max_vals
        for dim in max_vals[sensor]
    ]
    sel_keys = st.multiselect(
        "Choose sensor / dimension to plot",
        options=all_sensor_keys,
        default=all_sensor_keys[:min(3, len(all_sensor_keys))],
        key="live_ts_sel",
    )

    if sel_keys:
        ts_traces = go.Figure()
        stem_colors = px.colors.qualitative.D3

        for i, key in enumerate(sel_keys):
            parts  = key.split(" / ", 1)
            sensor, dim = parts[0], parts[1]
            sub    = frame[frame["device_name"] == sensor][["_time", dim]].dropna()
            if sub.empty:
                continue
            color  = stem_colors[i % len(stem_colors)]
            ts_traces.add_trace(go.Scatter(
                x            = sub["_time"],
                y            = sub[dim].abs(),
                mode         = "lines+markers",
                name         = key,
                line         = dict(color=color, width=1.5),
                marker       = dict(size=4),
                hovertemplate= f"<b>{key}</b><br>|value|: %{{y:.6f}}<br>Time: %{{x}}<extra></extra>",
            ))

        # Threshold lines on time-series too
        for rule in st.session_state.live_rules:
            lc = LEVEL_COLORS.get(rule["alert_level"], "#ffaa44")
            ts_traces.add_hline(
                y                   = rule["value"],
                line_dash           = "dash",
                line_color          = lc,
                line_width          = 1.5,
                annotation_text     = f"{rule['name']} ({rule['value']:.4g})",
                annotation_font_color=lc,
                annotation_font_size = 10,
            )

        ts_traces.update_layout(
            paper_bgcolor = THEME_BG,
            plot_bgcolor  = THEME_MID,
            font          = dict(color=THEME_TEXT, size=11),
            xaxis         = dict(title="Time (UTC)", gridcolor="#3a4060"),
            yaxis         = dict(title="|Value|",     gridcolor="#3a4060"),
            legend        = dict(bgcolor=THEME_MID, bordercolor="#3a4060", borderwidth=1),
            margin        = dict(l=60, r=20, t=30, b=50),
            height        = 380,
        )
        st.plotly_chart(ts_traces, use_container_width=True)

    # ── Violations table ──────────────────────────────────────────────────────
    if violations:
        st.markdown("---")
        st.subheader(f"🚨 Active Violations ({len(violations)})")
        viol_df = pd.DataFrame(violations)[
            ["rule","sensor","dimension","max_value","threshold","operator","alert_level"]
        ].copy()
        viol_df["max_value"] = viol_df["max_value"].map("{:.6f}".format)
        viol_df["threshold"] = viol_df["threshold"].map("{:.6f}".format)
        st.dataframe(
            viol_df.style.map(_level_color, subset=["alert_level"]),
            use_container_width=True, height=200,
        )
    else:
        if st.session_state.live_rules:
            st.success("✅ All sensors are within defined thresholds.")

    # ── Flux query expander ───────────────────────────────────────────────────
    with st.expander("🔍 View Flux Query"):
        st.code(st.session_state.get("live_query", ""), language="sql")

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    if auto_live:
        time.sleep(60)
        st.rerun()