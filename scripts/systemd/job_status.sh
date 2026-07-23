#!/usr/bin/env bash

set -u

SERVICE_NAME="perf-report-monitor.service"
RUNNER_PATTERN="run_timerweight_report.py"
MONITOR_PATTERN="watch_incoming.py"
LOG_LINES=20
SHOW_LOGS=1
WATCH_INTERVAL=0

usage() {
  cat <<'EOF'
Usage: job_status.sh [options]

Check whether perf-analytics jobs are running.

Options:
  --service NAME     systemd service name (default: perf-report-monitor.service)
  --logs N           show last N service logs (default: 20)
  --no-logs          do not print service logs
  --watch SEC        refresh every SEC seconds
  -h, --help         show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)
      SERVICE_NAME="${2:-}"
      shift 2
      ;;
    --logs)
      LOG_LINES="${2:-20}"
      shift 2
      ;;
    --no-logs)
      SHOW_LOGS=0
      shift
      ;;
    --watch)
      WATCH_INTERVAL="${2:-0}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

print_status() {
  local now active_state sub_state main_pid monitor_lines runner_pids

  now="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  active_state="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"
  sub_state="$(systemctl show -p SubState --value "$SERVICE_NAME" 2>/dev/null || true)"
  main_pid="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || true)"

  echo "============================================================"
  echo "[$now] perf-analytics job status"
  echo "Service      : $SERVICE_NAME"
  echo "ActiveState  : ${active_state:-unknown}"
  echo "SubState     : ${sub_state:-unknown}"
  echo "MainPID      : ${main_pid:-unknown}"

  echo
  echo "[Monitor Process]"
  monitor_lines="$(pgrep -af "$MONITOR_PATTERN" 2>/dev/null || true)"
  if [[ -n "$monitor_lines" ]]; then
    echo "$monitor_lines"
  else
    echo "(none)"
  fi

  echo
  echo "[Runner Jobs]"
  runner_pids="$(pgrep -f "$RUNNER_PATTERN" 2>/dev/null || true)"
  if [[ -n "$runner_pids" ]]; then
    echo "Running jobs detected: yes"
    while IFS= read -r pid; do
      [[ -z "$pid" ]] && continue
      ps -p "$pid" -o pid,ppid,etimes,pcpu,pmem,stat,cmd
    done <<< "$runner_pids"
  else
    echo "Running jobs detected: no"
  fi

  if [[ "$SHOW_LOGS" -eq 1 ]]; then
    echo
    echo "[Recent Service Logs]"
    journalctl -u "$SERVICE_NAME" -n "$LOG_LINES" --no-pager 2>/dev/null || echo "Cannot read service logs."
  fi

  echo "============================================================"
}

if [[ "$WATCH_INTERVAL" -gt 0 ]]; then
  while true; do
    clear
    print_status
    sleep "$WATCH_INTERVAL"
  done
else
  print_status
fi
