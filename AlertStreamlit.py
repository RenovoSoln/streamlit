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