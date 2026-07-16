#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ "$(uname -s)" == "Darwin" ]]; then
  PYQT_DIR="$(find .venv -type d -path '*/site-packages/PyQt5' -print -quit)"
  if [[ -n "$PYQT_DIR" ]]; then
    while IFS= read -r -d '' duplicate; do
      canonical="$(printf '%s' "$duplicate" | sed -E 's/ [0-9]+(\.[^./]+)$/\1/')"
      if [[ "$canonical" != "$duplicate" && -f "$canonical" ]]; then
        rm -f "$duplicate"
      fi
    done < <(find "$PYQT_DIR" -type f -name '* [0-9]*.*' -print0)
    chflags -R nohidden "$PYQT_DIR" 2>/dev/null || true
  fi
fi
exec .venv/bin/python DICOM_download_ui.py --config config.json
