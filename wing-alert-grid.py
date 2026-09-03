#!/usr/bin/env python3

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests
import earthkit.data as ekd
from notifications import send_signal

# Must be set before importing meteodatalab to suppress GRIB version mismatch warnings.
os.environ.setdefault("ECCODES_VERSION_CHECK_OFF", "1")

from earthkit.data import config as ek_config
from meteodatalab import ogd_api

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


def _ref_time_from_url(url):
    """Extract model reference time from a GRIB2 asset URL without downloading it."""
    match = re.search(r"-(\d{12})-", url)
    return pd.Timestamp(match.group(1), tz="UTC")


def _as_lead_time(step):
    """Convert an ecCodes step value to a pandas duration."""
    if isinstance(step, (int, np.integer)):
        return pd.Timedelta(hours=int(step))
    return pd.Timedelta(step)


def _get_ensemble_urls(variable, lead_times, ref_ts=None):
    """Resolve a complete ensemble asset set for one model run."""
    request_args = {
        "collection": ICON_COLLECTION,
        "ref_time": "latest" if ref_ts is None else ref_ts.to_pydatetime(),
        "perturbed": True,
        "lead_time": lead_times,
    }
    urls = ogd_api.get_asset_urls(
        ogd_api.Request(variable=variable, **request_args)
    )
    if not urls:
        run = "latest available" if ref_ts is None else str(ref_ts)
        raise RuntimeError(
            f"No complete {variable} ensemble asset set found for model run {run}"
        )
    return urls


def _load_grid_coords(grid_uuid):
    """Load static ICON coordinates while tolerating ecCodes field naming."""
    url = ogd_api._get_geo_coord_url(
        grid_uuid,
        collection=ogd_api.Collection(ICON_COLLECTION),
    )
    coords = {}
    fields = ekd.from_source("url", [url], stream=True).to_fieldlist()

    for field in fields:
        labels = []
        for key in ("shortName", "name", "parameterName"):
            try:
                labels.append(str(field.metadata(key)).upper())
            except KeyError:
                pass
        label = " ".join(labels)

        if "CLON" in labels or "LONGITUDE" in label:
            coords["lon"] = field.to_numpy(
                flatten=True,
                dtype=np.float32,
                copy=False,
            )
        elif "CLAT" in labels or "LATITUDE" in label:
            coords["lat"] = field.to_numpy(
                flatten=True,
                dtype=np.float32,
                copy=False,
            )

    if set(coords) != {"lat", "lon"}:
        raise RuntimeError(
            "Static ICON grid is missing longitude/latitude fields; "
            f"found {sorted(coords)}"
        )

    return coords


def _read_ensemble_points(urls, cell_indices=None):
    """Read ensemble GRIB fields one at a time and retain only spot values.

    The standard meteodata-lab decoder assembles the complete ensemble into an
    xarray array. A single ICON-CH1 ensemble field is large enough that this can
    exceed the container memory limit. Streaming keeps at most one full GRIB
    field in memory; the accumulated result contains only the configured spots.
    """
    samples = {}

    fields = ekd.from_source("url", urls, stream=True).to_fieldlist()
    for field in fields:
        if cell_indices is None:
            grid_uuid = UUID(str(field.metadata("uuidOfHGrid")))
            coords = _load_grid_coords(grid_uuid)
            lat = np.asarray(coords["lat"])
            lon = np.asarray(coords["lon"])
            cell_indices = {
                name: _nearest_cell_from_coords(lat, lon, cfg["lat"], cfg["lon"])
                for name, cfg in SPOTS.items()
            }

        step = _as_lead_time(field.metadata("step"))
        member = int(field.metadata("perturbationNumber"))
        values = field.to_numpy(flatten=True, dtype=np.float32, copy=False)
        samples[(step, member)] = np.asarray(
            [values[index] for index in cell_indices.values()],
            dtype=np.float32,
        )
        del values

    if not samples:
        raise RuntimeError("No ensemble fields were returned by MeteoSwiss")

    return samples, cell_indices


def _nearest_cell_from_coords(lat, lon, target_lat, target_lon):
    """Return the closest unstructured-grid cell index."""
    return int(np.argmin((lat - target_lat) ** 2 + (lon - target_lon) ** 2))


def build_all_forecasts():
    """Stream the ICON ensemble and return a DataFrame per spot.

    Steps:
      1. One STAC search to resolve the current model ref_time (no GRIB download).
      2. Load any spot whose cache file already exists for that ref_time.
      3. If any spot is uncached, stream U/V ensemble fields and extract all spots.
    """
    lead_times = [
        timedelta(hours=h)
        for h in range(STEP_HOURS, LOOKAHEAD_HOURS + 1, STEP_HOURS)
    ]

    # Select the newest run that has the complete requested horizon. This avoids
    # selecting a freshly published run whose later assets are not available yet.
    u_urls = _get_ensemble_urls("U_10M", lead_times)
    ref_ts = _ref_time_from_url(u_urls[0])
    log.info("ICON-CH2 ref time: %s", ref_ts)

    # Remove stale cache files from previous model runs.
    current_prefix = ref_ts.strftime("%Y%m%d%H%M")
    if CACHE_DIR.exists():
        for old in CACHE_DIR.glob("*.pkl"):
            if not old.name.startswith(current_prefix):
                old.unlink()
                log.debug("Removed stale cache: %s", old.name)

    cache_files = {
        name: CACHE_DIR / f"{ref_ts:%Y%m%d%H%M}_{cfg['lat']:.4f}_{cfg['lon']:.4f}_ensemble.pkl"
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
        v_urls = _get_ensemble_urls("V_10M", lead_times, ref_ts)

        u_samples, cell_indices = _read_ensemble_points(u_urls)
        v_samples, _ = _read_ensemble_points(v_urls, cell_indices)
        common_keys = sorted(set(u_samples) & set(v_samples))
        if not common_keys:
            raise RuntimeError("U and V ensemble fields have no matching steps")

        samples_by_step = {}
        for step, member in common_keys:
            samples_by_step.setdefault(step, []).append(member)

        spot_rows = {name: [] for name in SPOTS}
        for step in sorted(samples_by_step):
            members = samples_by_step[step]
            u = np.stack([u_samples[(step, member)] for member in members])
            v = np.stack([v_samples[(step, member)] for member in members])

            speed = np.hypot(u, v) * 1.944
            direction = (np.degrees(np.arctan2(u, v)) + 180) % 360
            direction_rad = np.radians(direction)
            mean_direction = (
                np.degrees(
                    np.arctan2(
                        np.nanmean(np.sin(direction_rad), axis=0),
                        np.nanmean(np.cos(direction_rad), axis=0),
                    )
                )
                % 360
            )

            forecast_time = (ref_ts + step).tz_convert(TZ)
            for index, name in enumerate(SPOTS):
                spot_rows[name].append(
                    {
                        "time": forecast_time,
                        "wind_kn": np.nanmean(speed[:, index]),
                        "direction_deg": mean_direction[index],
                    }
                )

        CACHE_DIR.mkdir(exist_ok=True)
        for name in uncached:
            df = pd.DataFrame(spot_rows[name])
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
    return path


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

        plot_path = plot_forecast(spot_name, df, config, windows)

        for rows in windows:

            alert = describe_window(spot_name, rows)

            # One notification per window start; re-sends suppressed until next start.
            key = f"{spot_name}|{alert['start']:%Y%m%d%H}"

            if key in state:
                continue

            found = True

            send_signal(
                alert,
                "MeteoSwiss ICON-CH2-EPS",
                dry_run=DRY_RUN,
                attachment=plot_path,
            )

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
