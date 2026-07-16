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
if [[ "${DCMGET_SKIP_WEASIS:-0}" == "1" ]]; then
  echo "已按 DCMGET_SKIP_WEASIS=1 跳过 Weasis 资源缓存。"
elif .venv/bin/python scripts/prepare_weasis.py --platform windows-x86_64 --download-only; then
  echo "已缓存并校验 Windows Weasis 4.7.1 安装资源。"
else
  echo "警告：Weasis 资源缓存失败；DICOMDIR 和网页预览仍可用，PDI 将不包含 Windows 便携查看器。" >&2
fi
test -f config.json || cp config.example.json config.json
echo "部署完成。运行 ./scripts/run_ui.sh 启动 DcmGet。"
