# Operations Guide

## 1) Install dependencies

```bash
cd /opt/perf-analytics
source venv/bin/activate
pip install -r scripts/requirements_ops.txt
```

## 2) Configure runtime values

Edit:

- `/opt/perf-analytics/config/ops_pipeline.conf`
- `/opt/perf-analytics/config/ses_email.conf`
- `/opt/perf-analytics/.sec/aws-ses`

FTP password is no longer required in plaintext config.
Use one of the following:

- `/opt/perf-analytics/.sec/ftp_password` (preferred)
- `FTP_PASSWORD` environment variable

## 3) Run once (local incoming file)

```bash
cd /opt/perf-analytics
source venv/bin/activate
python scripts/run_timerweight_report.py --input-file sample_data_1-21_181_firstcontentfulpaint_iter5_win1_inter5.csv --source local
```

If `--input-file` is only a filename, the script resolves it under `incoming_dir`.

## 4) Run once (fetch from FTP)

```bash
cd /opt/perf-analytics
source venv/bin/activate
python scripts/run_timerweight_report.py --input-file sample_data_1-21_181_firstcontentfulpaint_iter5_win1_inter5.csv --source ftp
```

## 5) Start watchdog monitor (5-minute polling)

```bash
cd /opt/perf-analytics
source venv/bin/activate
python scripts/watch_incoming.py --interval 300
```

This watcher polls the incoming folder every 5 minutes and calls the main program with the new filename.

Optional monitor flags:

- `--runner-max-retries 3`
- `--runner-backoff-seconds 30`
- `--skip-llm`
- `--skip-email`

## 6) systemd service

See: `/opt/perf-analytics/scripts/systemd/INSTALL.md`
