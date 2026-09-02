# Wing Alert

Wing Alert checks MeteoSwiss ICON forecasts for windfoil conditions at the configured spots and sends each newly detected wind window to a Signal group. The grid-based checker also saves forecast plots and caches downloaded model data.

The container runs [`wing-alert-grid.py`](wing-alert-grid.py) once. Schedule the container with cron, systemd, or another scheduler if it should check regularly.

## Configuration

Create a local environment file, for example `.env`, with:

```dotenv
SIGNAL_CLI=signal-cli
SIGNAL_CONFIG_DIR=/var/lib/signal-cli
SIGNAL_ACCOUNT=+your-signal-number
SIGNAL_GROUP_ID=your-base64-group-id

# Set to 1 to print alerts without sending them.
DRY_RUN=0
```

`SIGNAL_ACCOUNT` must be a registered `signal-cli` account, and `SIGNAL_GROUP_ID` must be the Base64 group ID known to that account. The `signal-data` directory contains Signal account state and must not be committed or baked into the image.

The local environment file and Signal data are deliberately excluded from Git. Keep the real phone number, group ID, and account database out of public repositories.

## Build the image

Using Docker:

```bash
docker build -t wing-alert .
```

Using Podman:

```bash
podman build -t wing-alert .
```

## Run once

Create a directory for persistent checker data:

```bash
mkdir -p runtime
```

Using Docker:

```bash
docker run --rm \
  --env-file .env \
  -v "$PWD/signal-data:/var/lib/signal-cli" \
  -v "$PWD/runtime:/data" \
  wing-alert
```

Using Podman on an SELinux host, add `:Z` to the bind mounts:

```bash
podman run --rm \
  --env-file .env \
  -v "$PWD/signal-data:/var/lib/signal-cli:Z" \
  -v "$PWD/runtime:/data:Z" \
  wing-alert
```

The `/data` volume persists:

- `windfoil_grid_state.json`, which prevents duplicate alerts;
- `.cache/`, which avoids re-downloading the same model run; and
- `plots/`, which contains generated forecast images.

## Test without sending

Set `DRY_RUN=1` in `.env`, then run the same container command. The forecast is still downloaded and evaluated, but the Signal command is not called.

## Signal-cli account setup

The image installs `signal-cli`, but registration or linking is a one-time setup step. Populate the mounted `signal-data` directory with a registered account before running the checker. The account must belong to the target Signal group.

## Local development

Run the notification unit tests with:

```bash
python3 -m unittest -v
```

The repository also contains [`wing-alert.py`](wing-alert.py), a non-grid MeteoSwiss checker for point forecasts. The Docker image is intentionally configured to run the grid checker.
