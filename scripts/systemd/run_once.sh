#!/usr/bin/env bash
# Run pipeline for a specific file (oneshot, not via systemd instance)
#
# Usage:
#   ./run_once.sh <csv_filename>              # file already in incoming/ folder
#   ./run_once.sh <csv_filename> --ftp        # fetch from FTP first, then process
#
# Examples:
#   ./run_once.sh sample_data_1-21_181_firstcontentfulpaint_iter5_win1_inter5.csv
#   ./run_once.sh sample_data_1-21_181_firstcontentfulpaint_iter5_win1_inter5.csv --ftp
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <csv_filename_or_path> [--ftp]" >&2
    echo "" >&2
    echo "  (no flag)  File must already exist in incoming/ folder (local mode)" >&2
    echo "  --ftp      Download file from FTP server first, then process" >&2
    exit 1
fi

INPUT_FILE="$1"
SOURCE="local"
[[ "${2:-}" == "--ftp" ]] && SOURCE="ftp"

VENV_PYTHON="/opt/perf-analytics/venv/bin/python"
RUNNER="/opt/perf-analytics/scripts/run_timerweight_report.py"
CONFIG="/opt/perf-analytics/config/ops_pipeline.conf"
export PYTHONPATH="/opt/perf-analytics/scripts"

echo "[INFO] Source : ${SOURCE}  (ftp = download from FTP then process, local = file already in incoming/)"
echo "[INFO] File   : ${INPUT_FILE}"
echo "[INFO] Running pipeline..."
"${VENV_PYTHON}" "${RUNNER}" \
    --input-file "${INPUT_FILE}" \
    --source "${SOURCE}" \
    --config "${CONFIG}"
