#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 Python 3.10+，请先使用系统包管理器安装 python3、python3-venv。" >&2
  exit 1
fi

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "需要 Python 3.10 或更高版本。" >&2
  exit 1
}
python3 -m venv --clear .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/download_dcmtk.py
if [[ "${DCMGET_SKIP_OHIF:-0}" == "1" ]]; then
  echo "已按 DCMGET_SKIP_OHIF=1 跳过 OHIF Viewer 离线资源。"
elif .venv/bin/python scripts/prepare_ohif.py; then
  echo "已下载、校验并准备 OHIF Viewer 3.12.6 离线资源。"
else
  echo "警告：OHIF Viewer 离线资源准备失败；DICOMDIR 和原始 DICOM 仍可用。" >&2
fi
test -f config.json || cp config.example.json config.json
echo "部署完成。运行 ./scripts/run_ui.sh 启动 DcmGet。"
