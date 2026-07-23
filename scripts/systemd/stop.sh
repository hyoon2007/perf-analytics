#!/usr/bin/env bash
# Perf Report Monitor - Stop script
set -euo pipefail

SERVICE="perf-report-monitor.service"

echo "[INFO] Stopping ${SERVICE}..."
sudo systemctl stop "${SERVICE}"
echo "[INFO] Done. Final status:"
systemctl status --no-pager "${SERVICE}" || true
