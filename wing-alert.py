#!/usr/bin/env python3

import json
import logging
import os
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from notifications import send_signal

log = logging.getLogger(__name__)

# ============================================================
# Configuration
# ============================================================

TZ = ZoneInfo("Europe/Zurich")

STAC = "https://data.geo.admin.ch/api/stac/v1"
COLLECTION = "ch.meteoschweiz.ogd-local-forecasting"

# MeteoSwiss parameters:
#   fu3010h0 = hourly mean wind, km/h
#   dkl010h0 = hourly mean direction, degrees
PARAMS = (
    "fu3010h0",
    "dkl010h0",
)

# Run the legacy point-forecast checker once per day.
CHECK_MINUTES = 24 * 60
LOOKAHEAD_HOURS = 72

STATE_FILE = Path("windfoil_state.json")

# Set DRY_RUN=1 while testing.
DRY_RUN = os.getenv("DRY_RUN") == "1"


# IMPORTANT:
# Treat wind thresholds/directions below as starting values.
# Adjust them to what YOU consider foilable/safe at each spot.

SPOTS = {
    "Préverenges": {
        # MeteoSwiss postal-code forecast point
        "point_id": 102800,
        "point_type_id": 2,
        "min_kn": 12,
        "min_hours": 2,
    },
    "Saint-Prex": {
        # MeteoSwiss station PRE forecast point.
        # Alternative postal-code point:
        # point_id=116200, point_type_id=2
        "point_id": 331,
        "point_type_id": 1,
        "min_kn": 12,
        "min_hours": 2,
    },
}


# ============================================================
# MeteoSwiss
# ============================================================


def latest_urls():
    """
    Find the latest MeteoSwiss forecast run and return
    the CSV URL for each wind parameter.
    """

    item_id = datetime.now(TZ).strftime("%Y%m%d") + "-ch"

    url = f"{STAC}/collections/{COLLECTION}" f"/items/{item_id}"

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    assets = response.json()["assets"]

    # Asset names look like:
    #
    # vnut12.lssw.202608201200.fu3010h0.csv

    runs = sorted(
        {
            key.split(".")[2]
            for key in assets
            if len(key.split(".")) >= 5 and key.split(".")[2].isdigit()
        }
    )

    if not runs:
        raise RuntimeError("Could not find a MeteoSwiss forecast run")

    latest_run = runs[-1]

    urls = {}

    for param in PARAMS:

        url = next(
            (
                asset["href"]
                for key, asset in assets.items()
                if key.split(".")[2] == latest_run and key.endswith(f".{param}.csv")
            ),
            None,
        )

        if not url:
            raise RuntimeError(f"No MeteoSwiss asset for {param}")

        urls[param] = url

    return latest_run, urls


def load_forecast(urls):
    """
    Download the 3 MeteoSwiss CSV files and retain only
    Préverenges + Saint-Prex.
    """

    frames = []

    wanted = {
        (
            cfg["point_id"],
            cfg["point_type_id"],
        )
        for cfg in SPOTS.values()
    }

    for param, url in urls.items():

        response = requests.get(
            url,
            timeout=90,
        )

        response.raise_for_status()

        df = pd.read_csv(
            BytesIO(response.content),
            sep=";",
            encoding="latin-1",
        )

        # Only keep our two locations
        df = df[
            df[["point_id", "point_type_id"]].apply(tuple, axis=1).isin(wanted)
        ].copy()

        time_col = next(c for c in df.columns if c.lower() in ("date", "time"))

        # MeteoSwiss timestamps are UTC.
        df["time"] = pd.to_datetime(
            df[time_col].astype(str),
            format="%Y%m%d%H%M",
            utc=True,
        ).dt.tz_convert(TZ)

        df[param] = pd.to_numeric(
            df[param],
            errors="coerce",
        )

        frames.append(
            df[
                [
                    "point_id",
                    "point_type_id",
                    "time",
                    param,
                ]
            ]
        )

    # Join wind + direction
    result = frames[0]

    for frame in frames[1:]:

        result = result.merge(
            frame,
            on=[
                "point_id",
                "point_type_id",
                "time",
            ],
            how="inner",
        )

    return result.sort_values("time")


# ============================================================
# Windfoil logic
# ============================================================


def find_windows(df, config):

    now = pd.Timestamp.now(tz=TZ)

    forecast_end = now + pd.Timedelta(hours=LOOKAHEAD_HOURS)

    spot = df[
        (df.point_id == config["point_id"])
        & (df.point_type_id == config["point_type_id"])
        & (df.time > now)
        & (df.time <= forecast_end)
    ].copy()

    # MeteoSwiss gives km/h.
    # Convert to knots.
    spot["wind_kn"] = spot.fu3010h0 / 1.852

    spot["good"] = spot.wind_kn >= config["min_kn"]

    windows = []
    current = []

    for row in spot.itertuples():

        if row.good:

            # Ensure forecast hours are consecutive
            if current and row.time - current[-1].time != pd.Timedelta(hours=1):

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

    # MeteoSwiss says the timestamp represents
    # the END of the preceding hourly interval.
    start = rows[0].time - pd.Timedelta(hours=1)

    end = rows[-1].time

    winds = [row.wind_kn for row in rows]

    # Direction around middle of window
    direction = rows[len(rows) // 2].dkl010h0

    names = [
        "N",
        "NE",
        "E",
        "SE",
        "S",
        "SW",
        "W",
        "NW",
    ]

    compass = names[int((direction + 22.5) // 45) % 8]

    return {
        "spot": spot_name,
        "start": start,
        "end": end,
        "wind": f"{min(winds):.0f}-{max(winds):.0f}",
        "direction": f"{compass} ({direction:.0f}°)",
    }


# ============================================================
# Don't send the same alert repeatedly
# ============================================================


def load_state():

    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text())

    except Exception:
        return {}


def save_state(state):

    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
        )
    )


# ============================================================
# Main check
# ============================================================


def check_once():

    run, urls = latest_urls()

    log.info("MeteoSwiss run: %s", run)

    forecast = load_forecast(urls)

    state = load_state()

    found = False

    for spot_name, config in SPOTS.items():

        windows = find_windows(
            forecast,
            config,
        )

        for rows in windows:

            alert = describe_window(
                spot_name,
                rows,
            )

            # One notification per window start.
            # If MeteoSwiss extends the same window
            # next hour, you won't get spammed.
            key = f"{spot_name}|" f"{alert['start']:%Y%m%d%H}"

            if key in state:
                continue

            found = True

            send_signal(alert, "MeteoSwiss", dry_run=DRY_RUN)

            log.info("ALERT: %s  %s -> %s", spot_name, alert["start"], alert["end"])

            if not DRY_RUN:

                state[key] = datetime.now(TZ).isoformat()

                save_state(state)

    if not found:
        log.info("No new wind windows.")


# ============================================================
# Run forever, checking once per day
# ============================================================


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("Windfoil watcher started.")

    while True:

        try:

            check_once()

        except KeyboardInterrupt:

            log.info("Stopped.")
            break

        except Exception:

            log.exception("Unhandled error")

        time.sleep(CHECK_MINUTES * 60)


if __name__ == "__main__":
    main()
