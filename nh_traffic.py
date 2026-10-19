"""
nh_traffic.py - a self-refreshing view of National Highways incidents.

Pulls from the same JSON feed that powers the National Highways
'current incidents, disruptions and delays' page, lets you filter once,
then re-fetches on a timer so you don't have to keep poking F5 like a
lab rat hoping for a pellet.

Run with:
    pip install streamlit pandas requests
    streamlit run nh_traffic.py

Caveat: /trafficsearchapi is an undocumented endpoint used by their own
page. It works today, but they could rename or lock it without warning.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

BASE = "https://nationalhighways.co.uk/trafficsearchapi"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal traffic dashboard)"}

UK = ZoneInfo("Europe/London")  # handles the BST/GMT switch automatically

DIRECTIONS = {
    "N": "Northbound", "S": "Southbound", "E": "Eastbound",
    "W": "Westbound", "CW": "Clockwise", "ACW": "Anticlockwise",
}


# ---------------------------------------------------------------- data ----

def _get(path, params=None):
    r = requests.get(f"{BASE}/{path}", params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def get_roads():
    return _get("roads")


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def get_regions():
    return _get("regions")


@st.cache_data(ttl=60, show_spinner=False)
def get_events():
    """Fetch every current event, page by page (the API caps pages at 100)."""
    rows, page, total_pages = [], 1, 1
    while page <= total_pages and page <= 20:  # 20 = sanity cap, not a target
        payload = _get("events", {"page": page, "limit": 100})
        data = payload.get("data", [])
        # Their own JS guards against 'data' arriving as a JSON string, so we do too
        if isinstance(data, str):
            data = json.loads(data)
        rows.extend(data)
        total_pages = (payload.get("pagination") or {}).get("totalPages", 1)
        page += 1
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["delay_min"] = (pd.to_numeric(df["delay"], errors="coerce").fillna(0) / 60).round().astype(int)
    df["direction_name"] = df["direction"].map(DIRECTIONS).fillna(df["direction"])
    df["updatedDate"] = pd.to_datetime(df["updatedDate"], errors="coerce")
    # "There are 4 of 4 lanes closed" means the carriageway is shut
    lanes = df.get("lanesClosedText", pd.Series("", index=df.index)).fillna("").str.extract(r"(\d+) of (\d+)")
    df["fully_closed"] = lanes[0].notna() & (lanes[0] == lanes[1])
    df["endTime"] = pd.to_datetime(df.get("overallEndTime"), errors="coerce")
    # NH lists some roadworks several times over (separate records, same
    # details). Keep one of each as it would appear on screen.
    show_cols = ["title", "direction", "type", "lanesClosedText", "delay"]
    df = df.sort_values("updatedDate", ascending=False).drop_duplicates(subset=show_cols)
    return df


def apply_filters(df, regions, roads, directions, types, max_age_hours):
    if df.empty:
        return df
    # NH timestamps are in UTC.
    now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
    # Anything with a scheduled end time that has already passed is finished
    df = df[df["endTime"].isna() | (df["endTime"] >= now_utc)]
    # Congestion reports are the ones that get orphaned in the feed, so only
    # those need to have been updated recently. Roadworks can sit untouched
    # for days while still very much closing lanes.
    if max_age_hours:
        cutoff = now_utc - pd.Timedelta(hours=max_age_hours)
        stale = (df["type"] == "AbnormalTraffic") & (df["updatedDate"] < cutoff)
        df = df[~stale]
    if regions:
        df = df[df["region"].isin(regions)]
    if roads:
        df = df[df["road"].isin(roads)]
    if directions:
        df = df[df["direction"].isin(directions)]
    if types:
        df = df[df["type"].isin(types)]
    return df.sort_values(["fully_closed", "delay_min", "updatedDate"], ascending=[False, False, False])


# ------------------------------------------------------------------ UI ----

st.set_page_config(page_title="NH live traffic", page_icon=":material/traffic:", layout="wide")
st.title("National Highways - live incidents")

with st.sidebar:
    st.header("Filters")
    try:
        region_opts = get_regions()
        road_opts = get_roads()
    except Exception as e:
        st.error(f"Couldn't load filter lists: {e}")
        region_opts, road_opts = [], []

    sel_regions = st.multiselect("Region", region_opts)
    sel_roads = st.multiselect("Road", road_opts, placeholder="e.g. M4, A303")
    sel_dirs = st.multiselect(
        "Direction", list(DIRECTIONS), format_func=lambda c: DIRECTIONS[c]
    )
    # Types aren't offered by the API as a list, so we learn them from the data
    try:
        type_opts = sorted(get_events()["type"].dropna().unique())
    except Exception:
        type_opts = []
    sel_types = st.multiselect("Event type", type_opts)

    max_age = st.number_input(
        "Hide congestion reports not updated in the last N hours (0 = show all)",
        min_value=0, max_value=720, value=2, step=1,
    )
    minutes = st.radio("Refresh every", [5, 10], index=0, horizontal=True,
                       format_func=lambda m: f"{m} min")
    st.caption("Leave a filter empty to include everything.")


@st.fragment(run_every=f"{minutes}m")
def live_panel(regions, roads, directions, types, max_age, minutes):
    get_events.clear()  # force a genuinely fresh pull on each tick
    try:
        df = get_events()
    except Exception as e:
        st.error(f"Fetch failed ({e}). Will try again in {minutes} min.")
        return

    view = apply_filters(df, regions, roads, directions, types, max_age)

    # Spot anything that wasn't there last time round
    seen = st.session_state.get("seen_ids")
    current = set(view["id"]) if not view.empty else set()
    new_ids = current - seen if seen is not None else set()
    st.session_state["seen_ids"] = current

    now = datetime.now(UK).strftime("%H:%M:%S")  # server runs on UTC, so be explicit
    c1, c2, c3 = st.columns(3)
    c1.metric("Matching events", len(view))
    c2.metric("New since last check", len(new_ids))
    c3.metric("Last fetched", now)
    st.caption(f"Auto-refreshes every {minutes} minutes. Keep this tab open.")

    if view.empty:
        st.success("Nothing matching right now. Enjoy it while it lasts.")
        return

    for _, ev in view.iterrows():
        tag = " [NEW]" if ev["id"] in new_ids else ""
        if ev["fully_closed"]:
            tag = " [CLOSED]" + tag
        delay = f" - {ev['delay_min']} min delay" if ev["delay_min"] else ""
        with st.expander(f"{ev['title']}{delay}{tag}", expanded=bool(tag)):
            st.write(f"**{ev.get('reason', '')}** | {ev['region']} | {ev['direction_name']} | {ev['type']}")
            for col in ["location", "delayText", "lanesClosedText",
                        "returnToNormalText", "timeToClearText", "periodText"]:
                val = ev.get(col)
                if isinstance(val, str) and val.strip():
                    st.write(val)
            if pd.notna(ev["updatedDate"]):
                uk = ev["updatedDate"].tz_localize("UTC").tz_convert("Europe/London")
                st.caption(f"Updated by NH: {uk:%d %b %H:%M} (UK time)")


live_panel(sel_regions, sel_roads, sel_dirs, sel_types, max_age, minutes)
