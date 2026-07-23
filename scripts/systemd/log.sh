#!/usr/bin/env bash
# Perf Report Monitor - Log viewer
#
# Usage:
#   ./log.sh          # tail -f (live)
#   ./log.sh -n 50    # show last 50 lines
#   ./log.sh --since "10 min ago"

SERVICE="perf-report-monitor.service"

LINES=""
SINCE=""
FOLLOW=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n) LINES="$2"; FOLLOW=false; shift 2 ;;
        --since) SINCE="$2"; FOLLOW=false; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

ARGS=(-u "${SERVICE}" --no-pager)
[[ -n "${LINES}" ]] && ARGS+=(-n "${LINES}")
[[ -n "${SINCE}" ]] && ARGS+=(--since "${SINCE}")
[[ "${FOLLOW}" == true ]] && ARGS+=(-f)

journalctl "${ARGS[@]}"
