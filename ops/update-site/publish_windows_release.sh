#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'

readonly REMOTE_HOST='bwg-snell'
readonly REMOTE_SITE_ROOT='/opt/1panel/www/sites/dcmget.v2ex.com.cn/index'
readonly REMOTE_UPDATES_ROOT="${REMOTE_SITE_ROOT}/updates"
readonly REMOTE_RELEASES_ROOT="${REMOTE_UPDATES_ROOT}/releases"
readonly REMOTE_STABLE_ROOT="${REMOTE_UPDATES_ROOT}/stable"
readonly VERSION_PATTERN='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
readonly SAFE_FILE_PATTERN='^[A-Za-z0-9][A-Za-z0-9._+-]*$'

usage() {
    echo "用法: $0 <本地 release/windows 目录> <MAJOR.MINOR.PATCH>" >&2
}

die() {
    echo "错误: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"
}

local_sha256() {
    local file=$1
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -- "$file" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -- "$file" | awk '{print $1}'
    else
        die '本机缺少 sha256sum 或 shasum'
    fi
}

[[ $# -eq 2 ]] || {
    usage
    exit 2
}

SOURCE_INPUT=$1
VERSION=$2

[[ $VERSION =~ $VERSION_PATTERN ]] || die '版本号必须是安全的 MAJOR.MINOR.PATCH 格式，例如 3.2.0'
[[ -d $SOURCE_INPUT ]] || die "发布目录不存在: $SOURCE_INPUT"
[[ ! -L $SOURCE_INPUT ]] || die '发布目录不能是符号链接'

require_command ssh
require_command scp
require_command rsync
require_command awk
require_command openssl
require_command python3
require_command sort

SOURCE_DIR=$(cd -- "$SOURCE_INPUT" && pwd -P)
MANIFEST_PATH="${SOURCE_DIR}/UPDATE-MANIFEST.json.p7"
[[ -f $MANIFEST_PATH && ! -L $MANIFEST_PATH ]] || die '发布目录必须包含普通文件 UPDATE-MANIFEST.json.p7'

shopt -s nullglob dotglob
source_entries=("${SOURCE_DIR}"/*)
(( ${#source_entries[@]} > 0 )) || die '发布目录为空'

source_files=()
for path in "${source_entries[@]}"; do
    [[ ! -L $path ]] || die "拒绝符号链接: ${path##*/}"
    [[ -f $path ]] || die "发布目录只允许包含顶层普通文件: ${path##*/}"
    name=${path##*/}
    [[ $name =~ $SAFE_FILE_PATTERN ]] || die "拒绝不安全的文件名: $name"
    source_files+=("$path")
done

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/dcmget-update-publish.XXXXXX")
CHECKSUM_FILE="${WORK_DIR}/expected-sha256"
REMOTE_NONCE="$(date -u +%Y%m%d%H%M%S)-$$"
REMOTE_RELEASE_TEMP="${REMOTE_RELEASES_ROOT}/.publish-${VERSION}-${REMOTE_NONCE}"
REMOTE_RELEASE_TARGET="${REMOTE_RELEASES_ROOT}/${VERSION}"
REMOTE_STABLE_TEMP="${REMOTE_STABLE_ROOT}/.UPDATE-MANIFEST.json.p7.publish-${REMOTE_NONCE}"
REMOTE_TEMP_CREATED=0
REMOTE_STABLE_TEMP_CREATED=0

cleanup_remote_release_temp() {
    ssh "$REMOTE_HOST" bash -s -- "$REMOTE_RELEASES_ROOT" "$VERSION" "$REMOTE_RELEASE_TEMP" <<'REMOTE_CLEANUP' >/dev/null 2>&1 || true
set -euo pipefail
releases_root=$1
version=$2
temp_path=$3
case "$temp_path" in
    "${releases_root}/.publish-${version}-"*) rm -rf -- "$temp_path" ;;
    *) exit 2 ;;
esac
REMOTE_CLEANUP
}

cleanup_remote_stable_temp() {
    ssh "$REMOTE_HOST" bash -s -- "$REMOTE_STABLE_ROOT" "$REMOTE_STABLE_TEMP" <<'REMOTE_CLEANUP' >/dev/null 2>&1 || true
set -euo pipefail
stable_root=$1
temp_path=$2
case "$temp_path" in
    "${stable_root}/.UPDATE-MANIFEST.json.p7.publish-"*) rm -f -- "$temp_path" ;;
    *) exit 2 ;;
esac
REMOTE_CLEANUP
}

cleanup() {
    status=$?
    trap - EXIT INT TERM
    if (( REMOTE_STABLE_TEMP_CREATED )); then
        cleanup_remote_stable_temp
    fi
    if (( REMOTE_TEMP_CREATED )); then
        cleanup_remote_release_temp
    fi
    rm -rf -- "$WORK_DIR"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

SIGNED_MANIFEST_CONTENT="${WORK_DIR}/signed-update-manifest.json"
if ! openssl smime -verify -binary -inform DER -noverify \
    -in "$MANIFEST_PATH" -out "$SIGNED_MANIFEST_CONTENT" \
    >/dev/null 2>&1; then
    die 'UPDATE-MANIFEST.json.p7 签名无效或不是内嵌 DER PKCS#7'
fi

python3 - "$SIGNED_MANIFEST_CONTENT" "$SOURCE_DIR" "$VERSION" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
source_dir = Path(sys.argv[2])
expected_version = sys.argv[3]
safe_name = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
sha256 = re.compile(r"^[0-9a-f]{64}$")

try:
    raw_manifest = manifest_path.read_bytes()
    manifest = json.loads(raw_manifest.decode("utf-8"))
except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"已签名更新清单不是有效 UTF-8 JSON: {exc}") from exc

expected = {
    "version": expected_version,
    "product": "DcmGet",
    "platform": "windows-x64",
    "channel": "stable",
    "schema_version": 1,
    "layout_version": 1,
}
for field, value in expected.items():
    if manifest.get(field) != value:
        raise SystemExit(
            f"已签名更新清单 {field} 不匹配: "
            f"expected={value!r} actual={manifest.get(field)!r}"
        )

unsigned_manifest = source_dir / "UPDATE-MANIFEST.json"
if unsigned_manifest.exists():
    if unsigned_manifest.is_symlink() or not unsigned_manifest.is_file():
        raise SystemExit("UPDATE-MANIFEST.json 必须是普通文件")
    if unsigned_manifest.read_bytes() != raw_manifest:
        raise SystemExit("UPDATE-MANIFEST.json 与 PKCS#7 内嵌已签内容不一致")

artifacts = manifest.get("artifacts")
if not isinstance(artifacts, list) or not artifacts:
    raise SystemExit("已签名更新清单没有发布资源")

seen = set()
full_installers = 0
component_patches = 0
install_tree = str(manifest.get("install_tree_sha256", "")).lower()
if sha256.fullmatch(install_tree) is None:
    raise SystemExit("已签名更新清单缺少有效安装树指纹")

def validate_component_patch(record, name):
    global component_patches
    base_version = str(record.get("base_version", ""))
    if re.fullmatch(r"\d+\.\d+\.\d+", base_version) is None:
        raise SystemExit(f"组件增量包基础版本无效: {name}")
    if tuple(map(int, base_version.split("."))) >= tuple(
        map(int, expected_version.split("."))
    ):
        raise SystemExit(f"组件增量包基础版本不能高于或等于目标版本: {name}")
    if (
        record.get("signature_status") != "NOT_APPLICABLE"
        or record.get("preserves_user_data") is not True
        or record.get("content_scope") != "application"
        or record.get("layout_version") != manifest.get("layout_version")
        or record.get("install_path_allowlist") != ["DcmGet.exe", "_internal/**"]
        or record.get("removed_paths") != []
    ):
        raise SystemExit(f"组件增量包安全范围声明无效: {name}")
    base_tree = str(record.get("base_tree_sha256", "")).lower()
    target_tree = str(record.get("target_tree_sha256", "")).lower()
    if sha256.fullmatch(base_tree) is None or target_tree != install_tree:
        raise SystemExit(f"组件增量包树指纹无效: {name}")
    files = record.get("files")
    if not isinstance(files, list) or not files:
        raise SystemExit(f"组件增量包缺少文件清单: {name}")
    paths = set()
    for item in files:
        if not isinstance(item, dict):
            raise SystemExit(f"组件增量包文件记录无效: {name}")
        relative = str(item.get("path", "")).replace("\\", "/")
        parts = relative.split("/")
        allowed = relative == "DcmGet.exe" or (
            len(parts) >= 2 and parts[0] == "_internal"
        )
        canonical = relative.casefold()
        if (
            not allowed
            or any(part in {"", ".", ".."} for part in parts)
            or ":" in relative
            or canonical in paths
        ):
            raise SystemExit(f"组件增量包文件路径无效或重复: {relative}")
        paths.add(canonical)
        try:
            file_size = int(item.get("size", -1))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"组件增量包文件大小无效: {relative}") from exc
        file_sha256 = str(item.get("sha256", "")).lower()
        if file_size < 0 or sha256.fullmatch(file_sha256) is None:
            raise SystemExit(f"组件增量包文件指纹无效: {relative}")
        if item.get("base_missing") is True:
            if item.get("base_size") is not None or item.get("base_sha256"):
                raise SystemExit(f"组件增量包新增文件基础状态冲突: {relative}")
        else:
            try:
                base_size = int(item.get("base_size", -1))
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"组件增量包基础文件大小无效: {relative}") from exc
            base_sha256 = str(item.get("base_sha256", "")).lower()
            if base_size < 0 or sha256.fullmatch(base_sha256) is None:
                raise SystemExit(f"组件增量包基础文件指纹无效: {relative}")
    component_patches += 1

for record in artifacts:
    if not isinstance(record, dict):
        raise SystemExit("已签名更新清单包含无效资源记录")
    name = record.get("name")
    if not isinstance(name, str) or safe_name.fullmatch(name) is None:
        raise SystemExit(f"已签名更新清单包含不安全资源名: {name!r}")
    canonical = name.casefold()
    if canonical in seen:
        raise SystemExit(f"已签名更新清单包含重复资源名: {name}")
    seen.add(canonical)
    path = source_dir / name
    if path.is_symlink() or not path.is_file() or path.parent != source_dir:
        raise SystemExit(f"已签名更新资源不存在或类型无效: {name}")
    try:
        expected_size = int(record.get("size"))
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"已签名更新资源大小无效: {name}") from exc
    expected_sha256 = str(record.get("sha256", "")).lower()
    if expected_size < 0 or sha256.fullmatch(expected_sha256) is None:
        raise SystemExit(f"已签名更新资源指纹无效: {name}")
    if path.stat().st_size != expected_size:
        raise SystemExit(f"已签名更新资源大小不一致: {name}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected_sha256:
        raise SystemExit(f"已签名更新资源 SHA-256 不一致: {name}")
    kind = record.get("kind")
    if kind == "full_installer":
        if (
            record.get("signature_status") != "SIGNED"
            or record.get("preserves_user_data") is not True
            or record.get("content_scope") != "application"
        ):
            raise SystemExit(f"完整安装包安全声明无效: {name}")
        full_installers += 1
    elif kind == "component_patch":
        validate_component_patch(record, name)
    else:
        raise SystemExit(f"已签名更新清单包含不支持的资源类型: {kind!r}")

if full_installers > 1:
    raise SystemExit("已签名更新清单最多只能包含一个完整安装包")
if full_installers == 0 and component_patches == 0:
    raise SystemExit("已签名更新清单没有有效的完整安装包或组件增量包")
if full_installers == 0:
    chain = manifest.get("component_chain")
    roots = chain.get("root_full_releases") if isinstance(chain, dict) else None
    if (
        not isinstance(chain, dict)
        or chain.get("schema_version") != 1
        or not isinstance(roots, list)
        or not roots
    ):
        raise SystemExit("patch-only 更新缺少已签名完整发布链锚点")
    root_versions = set()
    for root in roots:
        if not isinstance(root, dict):
            raise SystemExit("组件更新链锚点格式无效")
        root_version = str(root.get("version", ""))
        root_tree = str(root.get("install_tree_sha256", "")).lower()
        if (
            re.fullmatch(r"\d+\.\d+\.\d+", root_version) is None
            or tuple(map(int, root_version.split(".")))
            >= tuple(map(int, expected_version.split(".")))
            or sha256.fullmatch(root_tree) is None
            or root_version in root_versions
        ):
            raise SystemExit("组件更新链锚点无效或重复")
        root_versions.add(root_version)
artifact_full = [item for item in artifacts if item.get("kind") == "full_installer"]
artifact_patches = [item for item in artifacts if item.get("kind") == "component_patch"]
if manifest.get("full_installer") != (artifact_full[0] if artifact_full else None):
    raise SystemExit("已签名更新清单 full_installer 与 artifacts 不一致")
if manifest.get("component_patches") != artifact_patches:
    raise SystemExit("已签名更新清单 component_patches 与 artifacts 不一致")
PY

for file in "${source_files[@]}"; do
    digest=$(local_sha256 "$file")
    printf '%s  %s\n' "$digest" "${file##*/}"
done | LC_ALL=C sort > "$CHECKSUM_FILE"

EXPECTED_FILE_COUNT=${#source_files[@]}
MANIFEST_SHA256=$(local_sha256 "$MANIFEST_PATH")

echo "准备远端同文件系统临时目录: ${REMOTE_HOST}:${REMOTE_RELEASE_TEMP}"
ssh "$REMOTE_HOST" bash -s -- \
    "$REMOTE_UPDATES_ROOT" \
    "$REMOTE_RELEASES_ROOT" \
    "$REMOTE_STABLE_ROOT" \
    "$REMOTE_RELEASE_TEMP" \
    "$REMOTE_RELEASE_TARGET" <<'REMOTE_PREPARE'
set -euo pipefail
updates_root=$1
releases_root=$2
stable_root=$3
release_temp=$4
release_target=$5

command -v sha256sum >/dev/null 2>&1 || {
    echo '远端缺少 sha256sum' >&2
    exit 1
}

mkdir -p -- "$updates_root"
[[ ! -L $updates_root && -d $updates_root ]] || {
    echo "拒绝符号链接或非目录: $updates_root" >&2
    exit 1
}

for directory in "$releases_root" "$stable_root"; do
    if [[ -e $directory || -L $directory ]]; then
        [[ -d $directory && ! -L $directory ]] || {
            echo "拒绝符号链接或非目录: $directory" >&2
            exit 1
        }
    else
        mkdir -- "$directory"
    fi
done

[[ ! -e $release_temp && ! -L $release_temp ]] || {
    echo "远端临时目录已存在: $release_temp" >&2
    exit 1
}
[[ ! -L $release_target ]] || {
    echo "拒绝符号链接版本目录: $release_target" >&2
    exit 1
}
mkdir -- "$release_temp"
REMOTE_PREPARE
REMOTE_TEMP_CREATED=1

echo "上传 releases/${VERSION} 文件..."
rsync -a --delete -- "$SOURCE_DIR/" "$REMOTE_HOST:$REMOTE_RELEASE_TEMP/"
scp -q -- "$CHECKSUM_FILE" "$REMOTE_HOST:$REMOTE_RELEASE_TEMP/.expected-sha256"

echo '校验上传文件的远端 SHA-256...'
ssh "$REMOTE_HOST" bash -s -- "$REMOTE_RELEASE_TEMP" "$EXPECTED_FILE_COUNT" <<'REMOTE_VERIFY'
set -euo pipefail
directory=$1
expected_count=$2
checksum_file="${directory}/.expected-sha256"

[[ -d $directory && ! -L $directory ]] || exit 1
[[ -f $checksum_file && ! -L $checksum_file ]] || exit 1
if find "$directory" -mindepth 1 -maxdepth 1 ! -type f -print -quit | grep -q .; then
    echo "远端临时目录包含非普通文件: $directory" >&2
    exit 1
fi
actual_count=$(find "$directory" -mindepth 1 -maxdepth 1 -type f ! -name '.expected-sha256' | wc -l | tr -d '[:space:]')
[[ $actual_count == "$expected_count" ]] || {
    echo "远端文件数不一致: expected=$expected_count actual=$actual_count" >&2
    exit 1
}
(cd -- "$directory" && sha256sum -c -- '.expected-sha256')
REMOTE_VERIFY

echo "原子发布 releases/${VERSION}..."
ssh "$REMOTE_HOST" bash -s -- \
    "$REMOTE_RELEASE_TEMP" \
    "$REMOTE_RELEASE_TARGET" \
    "$EXPECTED_FILE_COUNT" <<'REMOTE_COMMIT_RELEASE'
set -euo pipefail
release_temp=$1
release_target=$2
expected_count=$3
checksum_file="${release_temp}/.expected-sha256"

verify_existing_target() {
    [[ -d $release_target && ! -L $release_target ]] || return 1
    if find "$release_target" -mindepth 1 -maxdepth 1 ! -type f -print -quit | grep -q .; then
        return 1
    fi
    actual_count=$(find "$release_target" -mindepth 1 -maxdepth 1 -type f | wc -l | tr -d '[:space:]')
    [[ $actual_count == "$expected_count" ]] || return 1
    (cd -- "$release_target" && sha256sum -c -- "$checksum_file")
}

if [[ -e $release_target || -L $release_target ]]; then
    if ! verify_existing_target; then
        echo "同版本目录已存在且内容不同，拒绝覆盖: $release_target" >&2
        exit 1
    fi
    echo '同版本目录内容完全一致，继续切换 stable 清单。'
    rm -rf -- "$release_temp"
else
    rm -- "$checksum_file"
    mv -- "$release_temp" "$release_target"
fi
REMOTE_COMMIT_RELEASE
REMOTE_TEMP_CREATED=0

echo '清理旧版本（保留最新两个，并保护当前 stable 与正在发布版本）...'
ssh "$REMOTE_HOST" bash -s -- \
    "$REMOTE_RELEASES_ROOT" \
    "$REMOTE_STABLE_ROOT/UPDATE-MANIFEST.json.p7" \
    "$VERSION" <<'REMOTE_RETENTION'
set -euo pipefail
IFS=$'\n\t'
releases_root=$1
stable_manifest=$2
publishing_version=$3
version_pattern='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

shopt -s nullglob
versions=()
for path in "$releases_root"/*; do
    name=${path##*/}
    [[ $name =~ $version_pattern ]] || continue
    [[ -d $path && ! -L $path ]] || {
        echo "拒绝处理符号链接或非目录版本项: $path" >&2
        exit 1
    }
    versions+=("$name")
done

(( ${#versions[@]} > 2 )) || exit 0

current_version=''
if [[ -e $stable_manifest || -L $stable_manifest ]]; then
    [[ -f $stable_manifest && ! -L $stable_manifest ]] || {
        echo "stable 清单不是普通文件，跳过旧版本清理: $stable_manifest" >&2
        exit 0
    }
    stable_hash=$(sha256sum -- "$stable_manifest" | awk '{print $1}')
    matches=()
    for candidate in "${versions[@]}"; do
        candidate_manifest="${releases_root}/${candidate}/UPDATE-MANIFEST.json.p7"
        [[ -f $candidate_manifest && ! -L $candidate_manifest ]] || continue
        candidate_hash=$(sha256sum -- "$candidate_manifest" | awk '{print $1}')
        [[ $candidate_hash == "$stable_hash" ]] && matches+=("$candidate")
    done
    if (( ${#matches[@]} != 1 )); then
        echo '无法唯一确认当前 stable 对应版本，为避免误删，跳过旧版本清理。' >&2
        exit 0
    fi
    current_version=${matches[0]}
fi

sorted_versions=()
while IFS= read -r candidate; do
    [[ -n $candidate ]] && sorted_versions+=("$candidate")
done < <(printf '%s\n' "${versions[@]}" | sort -V)

version_count=${#sorted_versions[@]}
newest_a=${sorted_versions[$((version_count - 1))]}
newest_b=${sorted_versions[$((version_count - 2))]}

for candidate in "${sorted_versions[@]}"; do
    [[ $candidate == "$newest_a" || $candidate == "$newest_b" ]] && continue
    [[ $candidate == "$publishing_version" ]] && continue
    [[ -n $current_version && $candidate == "$current_version" ]] && continue
    [[ $candidate =~ $version_pattern ]] || exit 1
    candidate_path="${releases_root}/${candidate}"
    [[ -d $candidate_path && ! -L $candidate_path ]] || exit 1
    rm -rf -- "$candidate_path"
    echo "已删除旧版本: $candidate"
done
REMOTE_RETENTION

echo '上传并校验 stable 清单临时文件...'
[[ $REMOTE_STABLE_TEMP == "$REMOTE_STABLE_ROOT/"* ]] || die 'stable 临时路径越界'
REMOTE_STABLE_TEMP_CREATED=1
scp -q -- "$MANIFEST_PATH" "$REMOTE_HOST:$REMOTE_STABLE_TEMP"

ssh "$REMOTE_HOST" bash -s -- \
    "$REMOTE_STABLE_ROOT" \
    "$REMOTE_STABLE_TEMP" \
    "$MANIFEST_SHA256" <<'REMOTE_COMMIT_STABLE'
set -euo pipefail
stable_root=$1
stable_temp=$2
expected_sha256=$3
stable_target="${stable_root}/UPDATE-MANIFEST.json.p7"

[[ -d $stable_root && ! -L $stable_root ]] || exit 1
[[ -f $stable_temp && ! -L $stable_temp ]] || exit 1
actual_sha256=$(sha256sum -- "$stable_temp" | awk '{print $1}')
[[ $actual_sha256 == "$expected_sha256" ]] || {
    echo "stable 清单 SHA-256 不一致: expected=$expected_sha256 actual=$actual_sha256" >&2
    exit 1
}
[[ ! -L $stable_target ]] || {
    echo "拒绝覆盖符号链接 stable 清单: $stable_target" >&2
    exit 1
}
mv -f -- "$stable_temp" "$stable_target"
REMOTE_COMMIT_STABLE
REMOTE_STABLE_TEMP_CREATED=0

echo "发布完成: ${VERSION}"
echo '清单: https://dcmget.v2ex.com.cn/updates/stable/UPDATE-MANIFEST.json.p7'
echo "资源: https://dcmget.v2ex.com.cn/updates/releases/${VERSION}/"
