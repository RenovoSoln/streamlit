"""
AlertStreamlit.py – Structural Monitoring Dashboard
====================================================
Two tabs:
  1. Alert History  – reads XML files from Google Drive (written by Alert.py)
  2. Live Monitor   – reads influx_config.json from the same Drive folder,
                      queries InfluxDB automatically, no manual entry needed.

influx_config.json (upload once to your Google Drive folder):
─────────────────────────────────────────────────────────────
{
  "url":             "http://your-influxdb:8086",
  "token":           "your-token",
  "org":             "your-org",
  "bucket":          "shm",
  "project":         "SHM_MAUD",
  "fields":          ["velx", "vely", "velz"],
  "aggregate_every": "5ms",
  "aggregate_fn":    "mean",
  "thresholds": [
    {
      "name":          "Velocity Warning",
      "sensor_filter": "All",
      "dimension":     "All",
      "operator":      ">=",
      "value":         0.001,
      "alert_level":   "Warning",
      "enabled":       true
    }
  ]
}

Streamlit Secrets (.streamlit/secrets.toml or Cloud → Settings → Secrets):
  GDRIVE_FOLDER_ID = "your-folder-id"
  [GOOGLE_SERVICE_ACCOUNT]
  type = "service_account"
  ... (paste full service account JSON fields here)

requirements.txt:
  streamlit>=1.32.0
  pandas>=2.0.0
  plotly>=5.18.0
  google-auth>=2.28.0
  google-auth-httplib2>=0.2.0
  google-api-python-client>=2.120.0
  influxdb-client>=1.40.0
"""

from __future__ import annotations

import io
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Optional deps ──────────────────────────────────────────────────────────────
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

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Structural Alert Monitor",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)

THEME_BG    = "#1e2130"
THEME_MID   = "#252a3d"
THEME_ACC   = "#4a9eff"
THEME_TEXT  = "#e8eaf0"
ALERT_DIR   = Path("alert_records")
LEVEL_COLORS = {"Warning": "#ffaa44", "Critical": "#ff6b7a"}
DIM_COLORS   = px.colors.qualitative.D3

st.markdown("""
<style>
html,body,[data-testid="stAppViewContainer"],[data-testid="stMain"]{
    background-color:#1e2130!important;color:#e8eaf0!important;}
[data-testid="stSidebar"]{background-color:#16192b!important;}
[data-testid="metric-container"]{background-color:#252a3d;border:1px solid #3a4060;
    border-radius:10px;padding:14px 18px;}
[data-testid="metric-container"] label{color:#9aa0b8!important;font-size:.82rem;}
[data-testid="metric-container"] [data-testid="stMetricValue"]{
    color:#4a9eff!important;font-size:1.8rem;font-weight:700;}
div.stButton>button{background-color:#4a9eff;color:white;border:none;
    border-radius:6px;padding:.4rem 1.2rem;font-weight:600;}
div.stButton>button:hover{background-color:#2d7de8;}
h1,h2,h3,h4{color:#e8eaf0!important;}
.stTabs [data-baseweb="tab-list"]{background-color:#16192b;gap:4px;}
.stTabs [data-baseweb="tab"]{background-color:#252a3d;border-radius:6px 6px 0 0;
    color:#9aa0b8;padding:8px 24px;font-weight:600;}
.stTabs [aria-selected="true"]{background-color:#1e2130!important;color:#4a9eff!important;}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# Google Drive helpers
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_resource(show_spinner=False)
def _drive_service():
    sa = dict(st.secrets["GOOGLE_SERVICE_ACCOUNT"])
    if "private_key" in sa:
        sa["private_key"] = sa["private_key"].replace("\\n", "\n")
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _list_drive_files(folder_id: str, name_filter: str = "") -> List[dict]:
    # Strip whitespace and quotes that might be in the folder_id
    folder_id = folder_id.strip().strip('"').strip("'")
    if not folder_id:
        raise ValueError("folder_id is empty")
    q = f"'{folder_id}' in parents and trashed=false"
    if name_filter:
        q += f" and name contains '{name_filter}'"
    return (_drive_service().files()
            .list(q=q, fields="files(id,name,modifiedTime,size)",
                  orderBy="modifiedTime desc", pageSize=50)
            .execute().get("files", []))


@st.cache_data(ttl=120, show_spinner=False)
def _download_file(file_id: str) -> bytes:
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, _drive_service().files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


@st.cache_data(ttl=300, show_spinner=False)
def _load_influx_config(_folder_id: str) -> Optional[dict]:
    files = _list_drive_files(_folder_id, "influx_config.json")
    if not files:
        return None
    return json.loads(_download_file(files[0]["id"]).decode("utf-8"))


# ══════════════════════════════════════════════════════════════════════════════
# XML helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_xml(source) -> List[dict]:
    records = []
    try:
        root = (ET.fromstring(source.decode("utf-8", errors="replace"))
                if isinstance(source, (bytes, bytearray))
                else ET.parse(str(source)).getroot())
        project = root.attrib.get("project", "Unknown")
        for ev in root.findall("event"):
            ts    = ev.attrib.get("timestamp", "")
            cycle = ev.attrib.get("cycle", "0")
            for v in ev.findall("violation"):
                records.append({
                    "project":     project,
                    "timestamp":   ts,
                    "cycle":       int(cycle) if cycle.isdigit() else 0,
                    "rule":        v.attrib.get("rule", ""),
                    "sensor":      v.attrib.get("sensor", ""),
                    "dimension":   v.attrib.get("dimension", ""),
                    "max_value":   float(v.attrib.get("max_value", 0)),
                    "threshold":   float(v.attrib.get("threshold", 0)),
                    "operator":    v.attrib.get("operator", ">="),
                    "alert_level": v.attrib.get("alert_level", "Warning"),
                })
    except Exception as exc:
        st.error(f"XML parse error: {exc}")
    return records


def _load_df(records):
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["date"] = df["timestamp"].dt.date
    df["hour"] = df["timestamp"].dt.hour
    return df.sort_values("timestamp").reset_index(drop=True)


def _level_css(val):
    if val == "Critical": return "color:#ff6b7a;font-weight:bold"
    if val == "Warning":  return "color:#ffaa44;font-weight:bold"
    return ""


def _fmt_ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def _fmt_size(n):
    for u in ("B","KB","MB"):
        if n < 1024: return f"{n:.0f} {u}"
        n //= 1024
    return f"{n:.1f} GB"


# ══════════════════════════════════════════════════════════════════════════════
# InfluxDB helpers  (exact replica of Alert.py AlertEngine logic)
# ══════════════════════════════════════════════════════════════════════════════

def _build_query(cfg: dict, start: str, stop: Optional[str]) -> str:
    fields  = cfg["fields"]
    f_filt  = " or ".join(f'r["_field"] == "{f}"' for f in fields)
    keep    = ", ".join(f'"{f}"' for f in fields)
    rng     = f'start: {start}' + (f', stop: {stop}' if stop else '')
    agg_ev  = cfg.get("aggregate_every", "5ms")
    agg_fn  = cfg.get("aggregate_fn",    "mean")
    return (
        f'from(bucket: "{cfg["bucket"]}")\n'
        f'  |> range({rng})\n'
        f'  |> filter(fn: (r) => r["_measurement"] == "{cfg["project"]}")\n'
        f'  |> filter(fn: (r) => {f_filt})\n'
        f'  |> aggregateWindow(every: {agg_ev}, fn: {agg_fn}, createEmpty: false)\n'
        f'  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
        f'  |> keep(columns: ["_time", "device_name", {keep}])'
    )


def _fetch_influx(cfg: dict, start: str, stop: Optional[str]):
    client = InfluxDBClient(url=cfg["url"], token=cfg["token"],
                            org=cfg["org"], timeout=30_000)
    query  = _build_query(cfg, start, stop)
    try:
        frame = client.query_api().query_data_frame(query)
    finally:
        client.close()
    if isinstance(frame, list):
        frame = pd.concat(frame, ignore_index=True) if frame else pd.DataFrame()
    if frame.empty:
        return pd.DataFrame(), query
    frame = frame.drop(columns=[c for c in frame.columns
                                 if c.startswith("result") or c.startswith("table")],
                       errors="ignore")
    frame["_time"] = pd.to_datetime(frame["_time"], errors="coerce", utc=True)
    frame = (frame.dropna(subset=["_time"])
                  .sort_values(["device_name","_time"])
                  .reset_index(drop=True))
    return frame, query


def _timeseries_abs(frame: pd.DataFrame, fields: List[str]) -> pd.DataFrame:
    """Long-format |value| time-series for every sensor × dim."""
    rows = []
    dims = [c for c in frame.columns if c in fields]
    for sensor in frame["device_name"].unique():
        sub = frame[frame["device_name"] == sensor]
        for dim in dims:
            if dim not in sub.columns:
                continue
            tmp = sub[["_time", dim]].dropna().copy()
            tmp["abs_value"] = tmp[dim].abs()
            tmp["raw_value"] = tmp[dim]
            tmp["sensor"]    = sensor
            tmp["dimension"] = dim
            rows.append(tmp[["_time","sensor","dimension","abs_value","raw_value"]])
    return pd.concat(rows, ignore_index=True).sort_values("_time") if rows else pd.DataFrame()


def _global_max(mv_df: pd.DataFrame) -> pd.DataFrame:
    """Single max |value| row per sensor × dimension."""
    if mv_df.empty:
        return pd.DataFrame()
    
    # More robust approach: use groupby with idxmax and then lookup
    try:
        idx = mv_df.groupby(["sensor", "dimension"])["abs_value"].idxmax()
        result = mv_df.loc[idx][["sensor", "dimension", "abs_value", "raw_value"]].reset_index(drop=True)
        return result
    except Exception:
        # Fallback: manually find max for each group
        result_rows = []
        for (sensor, dimension), group in mv_df.groupby(["sensor", "dimension"]):
            if not group.empty and not group["abs_value"].isna().all():
                max_idx = group["abs_value"].idxmax()
                result_rows.append(group.loc[max_idx, ["sensor", "dimension", "abs_value", "raw_value"]])
        return pd.DataFrame(result_rows) if result_rows else pd.DataFrame()


def _check_thresholds(max_df: pd.DataFrame, rules: List[dict]) -> pd.DataFrame:
    if max_df.empty or not rules:
        return pd.DataFrame()
    hits = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        sub = max_df.copy()
        if rule["sensor_filter"] != "All":
            sub = sub[sub["sensor"] == rule["sensor_filter"]]
        if rule["dimension"] != "All":
            sub = sub[sub["dimension"] == rule["dimension"]]
        op, thr = rule["operator"], rule["value"]
        mask = ((sub["abs_value"] >= thr) if op == ">=" else
                (sub["abs_value"] >  thr) if op == ">"  else
                ((sub["abs_value"] - thr).abs() < 1e-9))
        hit = sub[mask].copy()
        if hit.empty:
            continue
        hit["rule"]        = rule["name"]
        hit["threshold"]   = thr
        hit["operator"]    = op
        hit["alert_level"] = rule["alert_level"]
        hits.append(hit)
    return pd.concat(hits, ignore_index=True) if hits else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## 🏗️ Alert Monitor")
    st.markdown("---")

    gdrive_ok = (GDRIVE_OK
                 and "GOOGLE_SERVICE_ACCOUNT" in st.secrets
                 and "GDRIVE_FOLDER_ID"       in st.secrets)
    
    # Safely get and sanitize folder_id
    if gdrive_ok:
        raw_folder_id = st.secrets.get("GDRIVE_FOLDER_ID", "")
        # Ensure it's a string and sanitize it
        folder_id = str(raw_folder_id).strip().strip('"').strip("'")
    else:
        folder_id = ""
    
    # Validate folder_id if Google Drive is configured
    if gdrive_ok and not folder_id:
        st.warning("⚠️ GDRIVE_FOLDER_ID is configured but empty. Please check your secrets.")
        gdrive_ok = False
    
    # Debug info
    with st.expander("🔍 Debug Info"):
        st.write("**Google Drive Status:**", "✅ Configured" if gdrive_ok else "❌ Not configured")
        if gdrive_ok:
            st.write("**Folder ID (sanitized):**")
            st.code(folder_id)
            st.caption(f"Length: {len(folder_id)} characters")
        else:
            st.write("Google Drive not configured or missing credentials")

    st.markdown("### 📋 Alert History Source")
    xml_src = st.radio("src", ["☁️ Google Drive","📤 Upload XML"],
                       index=0, label_visibility="collapsed")
    xml_records: List[dict] = []

    if xml_src == "☁️ Google Drive" and gdrive_ok:
        c1, c2 = st.columns(2)
        if c1.button("🔄 Refresh", use_container_width=True):
            _download_file.clear(); st.rerun()
        if c2.button("🗑️ Cache",   use_container_width=True):
            st.cache_data.clear();  st.rerun()
        try:
            xfiles = _list_drive_files(folder_id, ".xml")
            if xfiles:
                labels = [f"{f['name']}  ({_fmt_size(int(f.get('size',0)))} · {_fmt_ts(f.get('modifiedTime',''))})"
                          for f in xfiles]
                idx = st.selectbox("XML file", range(len(xfiles)), format_func=lambda i: labels[i])
                xml_records = _parse_xml(_download_file(xfiles[idx]["id"]))
                st.success(f"✅ {len(xml_records)} violations loaded")
            else:
                st.warning("No XML files in Drive folder.")
        except Exception as exc:
            st.error(f"Drive error: {exc}")
    elif xml_src == "📤 Upload XML":
        up = st.file_uploader("Upload XML", type=["xml"])
        if up:
            xml_records = _parse_xml(up.read())

    st.markdown("---")
    df_full = _load_df(xml_records)
    if not df_full.empty:
        st.markdown("### Filters")
        all_s = sorted(df_full["sensor"].unique())
        sel_s = st.multiselect("Sensors",    all_s, default=all_s)
        all_d = sorted(df_full["dimension"].unique())
        sel_d = st.multiselect("Dimensions", all_d, default=all_d)
        all_l = sorted(df_full["alert_level"].unique())
        sel_l = st.multiselect("Level",      all_l, default=all_l)
        dmin, dmax = df_full["date"].min(), df_full["date"].max()
        if dmin != dmax:
            dr   = st.date_input("Date range", value=(dmin, dmax), min_value=dmin, max_value=dmax)
            d0, d1 = dr if isinstance(dr,(list,tuple)) and len(dr)==2 else (dmin, dmax)
        else:
            d0, d1 = dmin, dmax
        mask = (df_full["sensor"].isin(sel_s) & df_full["dimension"].isin(sel_d) &
                df_full["alert_level"].isin(sel_l) &
                (df_full["date"] >= d0) & (df_full["date"] <= d1))
        df = df_full[mask].copy()
    else:
        df = df_full


# ══════════════════════════════════════════════════════════════════════════════
# Tabs
# ══════════════════════════════════════════════════════════════════════════════

tab_hist, tab_live = st.tabs(["📋  Alert History", "⚡  Live InfluxDB Monitor"])


# ──────────────────────────────────────────────────────────────────────────────
# TAB 1 — Alert History
# ──────────────────────────────────────────────────────────────────────────────
with tab_hist:
    st.title("🏗️ Structural Monitoring – Alert History")

    if df_full.empty:
        st.info("👈 Load an XML file from the sidebar to get started.")
    else:
        n_v  = len(df)
        n_cr = int((df["alert_level"]=="Critical").sum())
        pct  = f"{100*n_cr/n_v:.1f}%" if n_v else "0%"
        ts0  = df["timestamp"].dropna().min().strftime("%Y-%m-%d %H:%M") if not df["timestamp"].isna().all() else "–"
        ts1  = df["timestamp"].dropna().max().strftime("%Y-%m-%d %H:%M") if not df["timestamp"].isna().all() else "–"
        proj = df["project"].iloc[0] if "project" in df.columns else "–"

        c1,c2,c3,c4,c5 = st.columns(5)
        c1.metric("Project",         proj)
        c2.metric("Alert Events",    df["cycle"].nunique())
        c3.metric("Total Violations",n_v)
        c4.metric("Critical",        f"{n_cr} ({pct})")
        c5.metric("Sensors",         df["sensor"].nunique())
        st.caption(f"🕒 {ts0}  →  {ts1}")
        st.markdown("---")

        # Stem chart
        st.subheader("📊 Violations Over Time")
        ds = df.copy()
        ds["key"]   = ds["sensor"] + " / " + ds["dimension"]
        ds["index"] = range(len(ds))
        sf = go.Figure()
        for i,key in enumerate(ds["key"].unique()):
            sub = ds[ds["key"]==key]; col = DIM_COLORS[i%len(DIM_COLORS)]
            for _,row in sub.iterrows():
                sf.add_trace(go.Scatter(x=[row["index"],row["index"]], y=[0,row["max_value"]],
                    mode="lines", line=dict(color=col,width=1.5), showlegend=False, hoverinfo="skip"))
            sf.add_trace(go.Scatter(x=sub["index"], y=sub["max_value"], mode="markers", name=key,
                marker=dict(size=8,color=col),
                hovertemplate=f"<b>{key}</b><br>Value: %{{y:.5f}}<br>Cycle: %{{customdata[0]}}<br>"
                              "Level: %{customdata[1]}<extra></extra>",
                customdata=sub[["cycle","alert_level"]].values))
        for rn,rdf in ds.groupby("rule"):
            thr=rdf["threshold"].iloc[0]; lc=LEVEL_COLORS.get(rdf["alert_level"].iloc[0],"#ffaa44")
            sf.add_hline(y=thr, line_dash="dash", line_color=lc, line_width=1.5,
                annotation_text=f"{rn} ({thr:.4g})", annotation_font_color=lc, annotation_font_size=10)
        sf.update_layout(paper_bgcolor=THEME_BG, plot_bgcolor=THEME_MID,
            font=dict(color=THEME_TEXT,size=11),
            legend=dict(bgcolor=THEME_MID,bordercolor="#3a4060",borderwidth=1),
            xaxis=dict(title="Alert Index",gridcolor="#3a4060"),
            yaxis=dict(title="Max Value",gridcolor="#3a4060"),
            margin=dict(l=60,r=20,t=30,b=50), height=420)
        st.plotly_chart(sf, use_container_width=True)

        # Pie + bar
        st.markdown("---")
        lc_,rc_ = st.columns([1,2])
        with lc_:
            st.subheader("🥧 Alert Level Breakdown")
            pc = df["alert_level"].value_counts()
            pf = go.Figure(go.Pie(labels=pc.index.tolist(), values=pc.values.tolist(),
                marker=dict(colors=[LEVEL_COLORS.get(l,THEME_ACC) for l in pc.index],
                            line=dict(color=THEME_BG,width=2)),
                textinfo="label+percent", textfont=dict(color=THEME_TEXT,size=12),
                hovertemplate="%{label}: %{value} (%{percent})<extra></extra>"))
            pf.update_layout(paper_bgcolor=THEME_BG,font=dict(color=THEME_TEXT),
                showlegend=False,margin=dict(l=10,r=10,t=10,b=10),height=280)
            st.plotly_chart(pf, use_container_width=True)
        with rc_:
            st.subheader("📈 Violations by Sensor")
            bd = df.groupby(["sensor","alert_level"]).size().reset_index(name="count")
            bf = px.bar(bd, x="sensor", y="count", color="alert_level",
                color_discrete_map=LEVEL_COLORS, template="plotly_dark",
                labels={"count":"Violations","sensor":"Sensor","alert_level":"Level"})
            bf.update_layout(paper_bgcolor=THEME_BG,plot_bgcolor=THEME_MID,
                font=dict(color=THEME_TEXT,size=11),
                legend=dict(bgcolor=THEME_MID,bordercolor="#3a4060",borderwidth=1,title_text="Level"),
                xaxis=dict(gridcolor="#3a4060"),yaxis=dict(gridcolor="#3a4060"),
                margin=dict(l=40,r=10,t=20,b=50),height=280)
            st.plotly_chart(bf, use_container_width=True)

        # Table
        st.markdown("---")
        st.subheader("📋 Violation Records")
        show_cols = ["timestamp","cycle","sensor","dimension","rule",
                     "max_value","threshold","operator","alert_level"]
        disp = df[[c for c in show_cols if c in df.columns]].copy()
        disp["timestamp"] = disp["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        disp["max_value"] = disp["max_value"].map("{:.6f}".format)
        disp["threshold"] = disp["threshold"].map("{:.6f}".format)
        st.dataframe(disp.style.map(_level_css, subset=["alert_level"]),
                     use_container_width=True, height=400)

        d1_,d2_ = st.columns(2)
        buf = io.StringIO(); disp.to_csv(buf, index=False)
        d1_.download_button("⬇️ Download CSV", buf.getvalue().encode(),
            f"alerts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv","text/csv")
        jdf = df[[c for c in show_cols if c in df.columns]].copy()
        jdf["timestamp"] = jdf["timestamp"].astype(str)
        d2_.download_button("⬇️ Download JSON",
            json.dumps(jdf.to_dict(orient="records"),indent=2).encode(),
            f"alerts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json","application/json")


# ──────────────────────────────────────────────────────────────────────────────
# TAB 2 — Live InfluxDB Monitor
# ──────────────────────────────────────────────────────────────────────────────
with tab_live:
    st.title("⚡ Live InfluxDB Monitor")

    if not INFLUX_OK:
        st.error("influxdb-client not installed — add it to requirements.txt and redeploy.")
        st.stop()

    # ── Load config from Drive ────────────────────────────────────────────────
    cfg: Optional[dict] = None
    if gdrive_ok:
        try:
            with st.spinner("Loading influx_config.json from Google Drive…"):
                cfg = _load_influx_config(folder_id)
        except Exception as exc:
            st.error(f"Could not load config from Drive: {exc}")

    if cfg is None:
        st.error("❌ `influx_config.json` not found in your Google Drive folder.")
        with st.expander("📖 How to create influx_config.json", expanded=True):
            st.code(json.dumps({
                "url":             "http://your-influxdb:8086",
                "token":           "your-token",
                "org":             "your-org",
                "bucket":          "shm",
                "project":         "SHM_MAUD",
                "fields":          ["velx","vely","velz"],
                "aggregate_every": "5ms",
                "aggregate_fn":    "mean",
                "thresholds": [{
                    "name":          "Velocity Warning",
                    "sensor_filter": "All",
                    "dimension":     "All",
                    "operator":      ">=",
                    "value":         0.001,
                    "alert_level":   "Warning",
                    "enabled":       True
                }]
            }, indent=2), language="json")
            st.markdown("Upload this file to your Google Drive folder, then reload the page.")
        st.stop()

    thresholds: List[dict] = cfg.get("thresholds", [])

    # Config badges
    ca,cb,cc,cd = st.columns(4)
    ca.metric("Bucket",     cfg["bucket"])
    cb.metric("Project",    cfg["project"])
    cc.metric("Fields",     ", ".join(cfg["fields"]))
    cd.metric("Thresholds", len(thresholds))
    st.markdown("---")

    # ── Query mode ────────────────────────────────────────────────────────────
    mc, _, ic = st.columns([2,1,3])
    with mc:
        mode = st.radio("Query Mode",
                        ["🔴  Live  (last 1 min, auto-refresh every 60 s)",
                         "📅  Custom Date / Time Range"],
                        index=0, key="qmode")
    is_live = mode.startswith("🔴")
    with ic:
        if is_live:
            st.info("🔄 Automatically refreshes every **60 seconds**. "
                    "Chart always shows the last 1 minute of live data.")
        else:
            st.info("Choose a start and end datetime, then click **Fetch**.")

    # ── Date/time pickers ─────────────────────────────────────────────────────
    if is_live:
        flux_start, flux_stop = "-1m", None
        should_fetch = True
    else:
        st.markdown("#### 🗓️ Date & Time Range (UTC)")
        now_utc = datetime.now(timezone.utc)
        dc1,dc2,dc3,dc4 = st.columns(4)
        s_date = dc1.date_input("Start date", value=(now_utc-timedelta(hours=1)).date(), key="sd")
        s_time = dc2.time_input("Start time", value=(now_utc-timedelta(hours=1)).time(), key="st")
        e_date = dc3.date_input("End date",   value=now_utc.date(),                      key="ed")
        e_time = dc4.time_input("End time",   value=now_utc.time(),                      key="et")
        s_dt   = datetime.combine(s_date, s_time, tzinfo=timezone.utc)
        e_dt   = datetime.combine(e_date, e_time, tzinfo=timezone.utc)
        flux_start = s_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        flux_stop  = e_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        should_fetch = st.button("🔍 Fetch Data", type="primary", key="fetchbtn")

    st.markdown("---")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    if should_fetch:
        with st.spinner("Querying InfluxDB…"):
            try:
                frame, flux_q = _fetch_influx(cfg, flux_start, flux_stop)
                st.session_state.update({
                    "lv_frame": frame, "lv_query": flux_q,
                    "lv_time":  time.time(),
                    "lv_start": flux_start, "lv_stop": flux_stop,
                })
            except Exception as exc:
                st.error(f"❌ InfluxDB error: {exc}")
                st.session_state["lv_frame"] = pd.DataFrame()

    frame  = st.session_state.get("lv_frame",  pd.DataFrame())
    flux_q = st.session_state.get("lv_query",  "")
    ft     = st.session_state.get("lv_time",   None)

    if frame.empty:
        if should_fetch:
            st.warning("No data returned. Check bucket, project name, and time range.")
        else:
            st.info("Choose a time range and click **Fetch Data**, or switch to Live mode.")
        if is_live:
            time.sleep(60); st.rerun()
        st.stop()

    # ── Compute ───────────────────────────────────────────────────────────────
    mv_df   = _timeseries_abs(frame, cfg["fields"])
    max_df  = _global_max(mv_df)
    viol_df = _check_thresholds(max_df, thresholds)
    violated = (set(zip(viol_df["sensor"], viol_df["dimension"]))
                if not viol_df.empty else set())

    # ── KPIs ──────────────────────────────────────────────────────────────────
    ft_str = datetime.utcfromtimestamp(ft).strftime("%H:%M:%S UTC") if ft else "–"
    k1,k2,k3,k4,k5 = st.columns(5)
    k1.metric("Sensors",     frame["device_name"].nunique())
    k2.metric("Data Points", f"{len(frame):,}")
    k3.metric("Dimensions",  len(cfg["fields"]))
    k4.metric("Violations",  len(viol_df) if not viol_df.empty else 0)
    k5.metric("Critical",    int((viol_df["alert_level"]=="Critical").sum())
                             if not viol_df.empty else 0)
    rng_str = f"{flux_start} → {flux_stop}" if flux_stop else f"{flux_start} (live)"
    st.caption(f"🕒 Last fetch: {ft_str}  |  Range: {rng_str}  |  "
               f"Agg: every {cfg.get('aggregate_every','5ms')} ({cfg.get('aggregate_fn','mean')})")

    # ══════════════════════════════════════════════════════════════════════════
    # CHART 1 — Max |value| bar chart
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.subheader("📊 Max Absolute Value per Sensor / Dimension")

    if not max_df.empty:
        mdf = max_df.copy()
        mdf["key"] = mdf["sensor"] + " / " + mdf["dimension"]

        def _bar_col(row):
            if (row["sensor"], row["dimension"]) not in violated:
                return "#4a9eff"
            lv = viol_df[(viol_df["sensor"]==row["sensor"]) &
                         (viol_df["dimension"]==row["dimension"])]["alert_level"].iloc[0]
            return LEVEL_COLORS.get(lv, "#ffaa44")

        colors = [_bar_col(r) for _, r in mdf.iterrows()]

        bf = go.Figure()
        bf.add_trace(go.Bar(
            x=mdf["key"], y=mdf["abs_value"],
            marker_color=colors,
            text=mdf["abs_value"].map("{:.5f}".format),
            textposition="outside",
            hovertemplate=(
                "<b>%{x}</b><br>"
                "Max |value|: %{y:.6f}<br>"
                "Raw value: %{customdata[0]:.6f}<extra></extra>"),
            customdata=mdf[["raw_value"]].values,
        ))
        drawn = set()
        for rule in thresholds:
            k = (rule["name"], rule["value"])
            if k in drawn: continue
            drawn.add(k)
            lc = LEVEL_COLORS.get(rule["alert_level"],"#ffaa44")
            bf.add_hline(y=rule["value"], line_dash="dash", line_color=lc, line_width=2,
                annotation_text=f"{rule['name']} ({rule['value']:.4g})",
                annotation_font_color=lc, annotation_font_size=11,
                annotation_position="top right")
        bf.update_layout(
            paper_bgcolor=THEME_BG, plot_bgcolor=THEME_MID,
            font=dict(color=THEME_TEXT,size=11),
            xaxis=dict(title="Sensor / Dimension", gridcolor="#3a4060", tickangle=-25),
            yaxis=dict(title="Max |value|", gridcolor="#3a4060",
                       zeroline=True, zerolinecolor="#3a4060"),
            margin=dict(l=60,r=40,t=70,b=100), height=480,
            showlegend=False, bargap=0.35,
        )
        # Inline legend
        for lbl,clr,xp in [("● Critical","#ff6b7a",0.99),
                            ("● Warning", "#ffaa44",0.90),
                            ("● OK",      "#4a9eff",0.81)]:
            bf.add_annotation(xref="paper",yref="paper", x=xp, y=1.07,
                text=f'<span style="color:{clr}">{lbl}</span>',
                showarrow=False, font=dict(size=11))
        st.plotly_chart(bf, use_container_width=True)

    # ══════════════════════════════════════════════════════════════════════════
    # CHART 2 — Time-series with range slider
    # ══════════════════════════════════════════════════════════════════════════
    st.markdown("---")
    st.subheader("📉 Max |Value| Over Time")

    if not mv_df.empty:
        all_keys = sorted((mv_df["sensor"] + " / " + mv_df["dimension"]).unique())
        sel = st.multiselect("Sensor / Dimension to plot", options=all_keys,
                             default=all_keys[:min(4,len(all_keys))], key="tskeys")
        if sel:
            tsf = go.Figure()
            for i,key in enumerate(sel):
                s, d = key.split(" / ",1)
                sub  = mv_df[(mv_df["sensor"]==s)&(mv_df["dimension"]==d)]
                col  = DIM_COLORS[i%len(DIM_COLORS)]
                tsf.add_trace(go.Scatter(
                    x=sub["_time"], y=sub["abs_value"],
                    mode="lines", name=key,
                    line=dict(color=col,width=1.5),
                    hovertemplate=f"<b>{key}</b><br>|value|: %{{y:.6f}}<br>%{{x}}<extra></extra>",
                ))
            for rule in thresholds:
                lc = LEVEL_COLORS.get(rule["alert_level"],"#ffaa44")
                tsf.add_hline(y=rule["value"], line_dash="dash", line_color=lc, line_width=1.5,
                    annotation_text=f"{rule['name']} ({rule['value']:.4g})",
                    annotation_font_color=lc, annotation_font_size=10)
            tsf.update_layout(
                paper_bgcolor=THEME_BG, plot_bgcolor=THEME_MID,
                font=dict(color=THEME_TEXT,size=11),
                xaxis=dict(title="Time (UTC)", gridcolor="#3a4060",
                           rangeslider=dict(visible=True, bgcolor=THEME_MID, thickness=0.06)),
                yaxis=dict(title="|Value|", gridcolor="#3a4060"),
                legend=dict(bgcolor=THEME_MID, bordercolor="#3a4060", borderwidth=1),
                margin=dict(l=60,r=20,t=40,b=80), height=460,
            )
            st.plotly_chart(tsf, use_container_width=True)

    # ── Violations table ──────────────────────────────────────────────────────
    st.markdown("---")
    if not viol_df.empty:
        st.subheader(f"🚨 Active Violations ({len(viol_df)})")
        dv = viol_df[["sensor","dimension","abs_value","raw_value",
                      "rule","threshold","operator","alert_level"]].copy()
        for col in ["abs_value","raw_value","threshold"]:
            dv[col] = dv[col].map("{:.6f}".format)
        st.dataframe(dv.style.map(_level_css, subset=["alert_level"]),
                     use_container_width=True, height=200)
    else:
        if thresholds:
            st.success("✅ All sensors within defined thresholds.")

    # ── Flux query ────────────────────────────────────────────────────────────
    with st.expander("🔍 Flux Query sent to InfluxDB"):
        st.code(flux_q, language="sql")

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    if is_live:
        elapsed   = time.time() - ft if ft else 60
        remaining = max(0, 60 - int(elapsed))
        st.caption(f"🔄 Next auto-refresh in **{remaining}s**…")
        time.sleep(remaining)
        st.rerun()