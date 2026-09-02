#!/usr/bin/env python3

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests
from notifications import send_signal

# Must be set before importing meteodatalab to suppress GRIB version mismatch warnings.
os.environ.setdefault("ECCODES_VERSION_CHECK_OFF", "1")

from earthkit.data import config as ek_config
from meteodatalab import ogd_api
from meteodatalab.operators import wind

log = logging.getLogger(__name__)

# Persistent cache so repeated runs within the same 6 h model run skip re-downloads.
ek_config.set("cache-policy", "user")

# ============================================================
# Configuration
# ============================================================

TZ = ZoneInfo("Europe/Zurich")

# ogd_api uses the short name (no "ch.meteoschweiz." prefix).
#ICON_COLLECTION = "ogd-forecasting-icon-ch2"  # 2.1 km grid, 120 h lookahead
ICON_COLLECTION = "ogd-forecasting-icon-ch1"  # 1 km grid, 33 h lookahead

LOOKAHEAD_HOURS = 30

# Set to 3 for ~3× fewer downloads if cold-start time is a concern.
STEP_HOURS = 1

STATE_FILE = Path("windfoil_grid_state.json")
PLOT_DIR = Path("plots")
CACHE_DIR = Path(".cache")
DRY_RUN = os.getenv("DRY_RUN") == "1"

# IMPORTANT:
# Adjust lat/lon, min_kn and min_hours to match each spot and your conditions.

SPOTS = {
    "Préverenges": {
        "lat": 46.482,
        "lon": 6.463,
        "min_kn": 12,
        "min_hours": 3,
    },
    "Saint-Prex": {
        "lat": 46.512,
        "lon": 6.520,
        "min_kn": 12,
        "min_hours": 3,
    },
}


# ============================================================
# ICON-CH2-EPS data access
# ============================================================


def _nearest_cell(da, lat, lon):
    """Return the flat cell index closest to (lat, lon) in the unstructured ICON grid."""
    lats = da.coords["lat"].values  # shape (cell,)
    lons = da.coords["lon"].values  # shape (cell,)
    return int(np.argmin((lats - lat) ** 2 + (lons - lon) ** 2))


def _ref_time_from_url(url):
    """Extract model reference time from a GRIB2 asset URL without downloading it."""
    match = re.search(r"-(\d{12})-", url)
    return pd.Timestamp(match.group(1), tz="UTC")


def _extract_spot(da_speed, da_dir, da_ref, ref_ts, lat, lon):
    """Extract wind time-series at one GPS point from already-loaded grid DataArrays."""
    cell_idx = _nearest_cell(da_ref, lat, lon)
    log.debug("GPS (%.4f, %.4f) → ICON cell %d", lat, lon, cell_idx)

    def _get(da):
        return da.isel(cell=cell_idx).mean(dim="eps").squeeze(("ref_time", "z")).values

    times = [
        (ref_ts + pd.Timedelta(lt)).tz_convert(TZ)
        for lt in da_ref.coords["lead_time"].values
    ]
    return pd.DataFrame({
        "time": times,
        "wind_kn": _get(da_speed) * 1.944,
        "direction_deg": _get(da_dir),
    })


def build_all_forecasts():
    """Download the ICON-CH2 grid once and return a DataFrame per spot.

    Steps:
      1. One STAC search to resolve the current model ref_time (no GRIB download).
      2. Load any spot whose cache file already exists for that ref_time.
      3. If any spot is uncached, download the full grid exactly once and extract all of them.
    """
    lead_times = [
        timedelta(hours=h)
        for h in range(STEP_HOURS, LOOKAHEAD_HOURS + 1, STEP_HOURS)
    ]

    # Resolve ref_time via a lightweight STAC search (no GRIB data downloaded).
    urls = ogd_api.get_asset_urls(
        ogd_api.Request(
            collection=ICON_COLLECTION,
            variable="U_10M",
            ref_time="latest",
            perturbed=True,
            lead_time=timedelta(hours=STEP_HOURS),
        )
    )
    ref_ts = _ref_time_from_url(urls[0])
    log.info("ICON-CH2 ref time: %s", ref_ts)

    # Remove stale cache files from previous model runs.
    current_prefix = ref_ts.strftime("%Y%m%d%H%M")
    if CACHE_DIR.exists():
        for old in CACHE_DIR.glob("*.pkl"):
            if not old.name.startswith(current_prefix):
                old.unlink()
                log.debug("Removed stale cache: %s", old.name)

    cache_files = {
        name: CACHE_DIR / f"{ref_ts:%Y%m%d%H%M}_{cfg['lat']:.4f}_{cfg['lon']:.4f}.pkl"
        for name, cfg in SPOTS.items()
    }
    uncached = [name for name, path in cache_files.items() if not path.exists()]

    result = {
        name: pd.read_pickle(path)
        for name, path in cache_files.items()
        if name not in uncached
    }
    if result:
        log.info("Cache hit for: %s", ", ".join(result))

    if uncached:
        log.info("Downloading grid for: %s", ", ".join(uncached))
        da_u = ogd_api.get_from_ogd(
            ogd_api.Request(
                collection=ICON_COLLECTION,
                variable="U_10M",
                ref_time=ref_ts.to_pydatetime(),
                perturbed=True,
                lead_time=lead_times,
            )
        )
        da_v = ogd_api.get_from_ogd(
            ogd_api.Request(
                collection=ICON_COLLECTION,
                variable="V_10M",
                ref_time=ref_ts.to_pydatetime(),
                perturbed=True,
                lead_time=lead_times,
            )
        )
        da_speed = wind.speed(da_u, da_v)
        da_dir = wind.direction(da_u, da_v)

        CACHE_DIR.mkdir(exist_ok=True)
        for name in uncached:
            cfg = SPOTS[name]
            df = _extract_spot(da_speed, da_dir, da_u, ref_ts, cfg["lat"], cfg["lon"])
            df.to_pickle(cache_files[name])
            log.info("Cached: %s", cache_files[name].name)
            result[name] = df

    return result


# ============================================================
# Wind window detection
# ============================================================


def find_windows(df, config):

    now = pd.Timestamp.now(tz=TZ)
    forecast_end = now + pd.Timedelta(hours=LOOKAHEAD_HOURS)

    spot = df[
        (df["time"] > now) & (df["time"] <= forecast_end)
    ].copy()

    spot["good"] = spot["wind_kn"] >= config["min_kn"]

    windows = []
    current = []

    for row in spot.itertuples():

        if row.good:

            if current and row.time - current[-1].time != pd.Timedelta(hours=STEP_HOURS):
                if len(current) >= config["min_hours"]:
                    windows.append(current)
                current = []

            current.append(row)

        else:

            if len(current) >= config["min_hours"]:
                windows.append(current)

            current = []

    if len(current) >= config["min_hours"]:
        windows.append(current)

    return windows


def describe_window(spot_name, rows):

    start = rows[0].time
    end = rows[-1].time

    winds = [row.wind_kn for row in rows]
    direction = rows[len(rows) // 2].direction_deg

    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    compass = names[int((direction + 22.5) // 45) % 8]

    return {
        "spot": spot_name,
        "start": start,
        "end": end,
        "wind": f"{min(winds):.0f}-{max(winds):.0f}",
        "direction": f"{compass} ({direction:.0f}°)",
    }


# ============================================================
# Plot
# ============================================================


def plot_forecast(spot_name, df, config, windows):
    PLOT_DIR.mkdir(exist_ok=True)

    fig, ax = plt.subplots(figsize=(12, 4))

    ax.plot(df["time"], df["wind_kn"], color="steelblue", linewidth=1.5)
    ax.axhline(config["min_kn"], color="tomato", linewidth=1, linestyle="--",
               label=f"threshold {config['min_kn']} kt")

    for rows in windows:
        ax.axvspan(rows[0].time, rows[-1].time, alpha=0.15, color="green")

    # Wind direction as quiver arrows along the top of the plot
    ax2 = ax.twinx()
    u = -np.sin(np.radians(df["direction_deg"]))
    v = -np.cos(np.radians(df["direction_deg"]))
    ax2.quiver(df["time"], np.ones(len(df)), u, v,
               scale=30, width=0.002, color="dimgray", alpha=0.6)
    ax2.set_ylim(0, 3)
    ax2.set_yticks([])

    ax.set_title(f"{spot_name} — ICON-CH2 wind forecast")
    ax.set_ylabel("Wind speed (kt)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b\n%H:%M", tz=TZ))
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 6)))
    ax.legend(loc="upper right")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()

    path = PLOT_DIR / f"{spot_name.replace(' ', '_')}.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    log.info("Plot saved: %s", path)


# ============================================================
# State  (prevents sending the same alert twice)
# ============================================================


def load_state():

    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):

    STATE_FILE.write_text(json.dumps(state, indent=2))


# ============================================================
# Main check
# ============================================================


def check_once():

    forecasts = build_all_forecasts()
    state = load_state()
    found = False

    for spot_name, config in SPOTS.items():

        df = forecasts[spot_name]

        windows = find_windows(df, config)

        plot_forecast(spot_name, df, config, windows)

        for rows in windows:

            alert = describe_window(spot_name, rows)

            # One notification per window start; re-sends suppressed until next start.
            key = f"{spot_name}|{alert['start']:%Y%m%d%H}"

            if key in state:
                continue

            found = True

            send_signal(alert, "MeteoSwiss ICON-CH2-EPS", dry_run=DRY_RUN)

            log.info(
                "ALERT: %s  %s → %s",
                spot_name,
                alert["start"],
                alert["end"],
            )

            if not DRY_RUN:
                state[key] = datetime.now(TZ).isoformat()
                save_state(state)

    if not found:
        log.info("No new wind windows.")


# ============================================================
# Entry point (run once; schedule the container with cron for daily checks)
# ============================================================


def main():

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    check_once()


if __name__ == "__main__":
    main()
