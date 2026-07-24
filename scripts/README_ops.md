# Operations Guide

The monitor runs **two pipelines in parallel** on every new CSV during the
comparison period:

- **v6** (`run_v6_report.py`, package `perf_analytics_v6`) — the pipeline we are
  moving to, converted from `anomaly_report_pipeline_v6_9.ipynb`. Emails are
  tagged `[v6]`.
- **v1** (`run_timerweight_report.py`, timer-weight + SHAP) — the current
  pipeline, kept for side-by-side comparison. Emails are tagged `[v1]`.

Both pipelines use the **same LLM** — the OAuth2 inference gateway
(`qwen3-14b-awq`) via `perf_analytics/llm_gateway.py`. There is no local Ollama
dependency anymore.

The monitor downloads each file once, runs **v6 first, then v1** (separate
processes, sequential), and moves the file to `processed/` if v6 succeeded, else
`failed/`. v1 is best-effort: if it crashes (e.g. SHAP OOM on large data) the
monitor and the finished v6 run are unaffected.

## 1) Install dependencies

```bash
cd /opt/perf-analytics
source venv/bin/activate
pip install -r scripts/requirements_ops.txt
```

## 2) Configure runtime values

- `/opt/perf-analytics/config/ops_pipeline.conf` — paths, FTP, retries, SES conf pointer
- `/opt/perf-analytics/config/ses_email.conf` — SES region, from/to addresses
- `/opt/perf-analytics/.sec/aws-ses` — AWS SES access key / secret
- `/opt/perf-analytics/.sec/inference-gateway` — LLM gateway OAuth2 credentials + `LLM_MODEL`
- FTP password: `/opt/perf-analytics/.sec/ftp_password` (preferred) or `FTP_PASSWORD` env

Secrets and `config/*.conf` are git-ignored — apply changes to them directly on
the server; they do not travel through git.

## 3) Run once — v6

```bash
cd /opt/perf-analytics
source venv/bin/activate
PYTHONPATH=scripts python scripts/run_v6_report.py --input-file <file>.csv --source local
```

## 4) Run once — v1

```bash
PYTHONPATH=scripts python scripts/run_timerweight_report.py --input-file <file>.csv --source local
```

Common flags for both runners: `--source {local,ftp}`, `--skip-email`,
`--no-move` (leave the CSV in place; the monitor uses this so both pipelines
read the same file), `--subject-prefix`. If `--input-file` is only a filename it
is resolved under `incoming_dir`.

## 5) Start the parallel monitor (5-minute polling)

```bash
cd /opt/perf-analytics
source venv/bin/activate
PYTHONPATH=scripts python scripts/watch_incoming.py --interval 300
```

Monitor flags: `--interval`, `--skip-llm`, `--skip-email` (passed through to both
runners).

## 6) systemd service

See: `/opt/perf-analytics/scripts/systemd/INSTALL.md`

## 7) Cutover (later — stop v1)

When the v6 output is trusted, drop the v1 run: in `watch_incoming.py` remove the
`_run_runner(V1_RUNNER, ...)` call (and the `[v1]` tag becomes unnecessary).
No other change is required.
