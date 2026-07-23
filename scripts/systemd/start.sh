#!/usr/bin/env bash
# Perf Report Monitor - Start script
set -euo pipefail

SERVICE="perf-report-monitor.service"

echo "[INFO] Starting ${SERVICE}..."
sudo systemctl start "${SERVICE}"
echo "[INFO] Done. Checking status..."
systemctl status --no-pager "${SERVICE}"
