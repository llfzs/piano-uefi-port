#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# 上游全量构建接线器（FULL_BUILD=true 时由 sunuefi-build.yml 调用）。
#
# 与旧版 workflow 里那段 "echo 示例 + || true" 的本质区别：
#   * 本脚本 set -euo pipefail，任何前置条件不满足都 exit 1，绝不静默产出占位；
#   * 真实 clone/校验内核源码、真实跑 tools/build-kernel.sh、真实把 ESP 目录
#     打成 4K 对齐的 FAT32 raw 镜像；
#   * 无法在公共 runner 上自动获得的设备/发行版输入（root 镜像、Android 助手模块、
#     原厂 DTB overlay、initramfs）作为显式外部输入，缺失时给出精确指引。
#
# 设计约束（独立 port 仓库）：
#   * 本仓库不含上游源码；--fork 指向本仓库根（含 port/），上游源码由
#     --upstream-src 指向一份自备的 yzddmr6/xiaomipad-8pro-mainline 工作树；
#     不传 --upstream-src 时退回 --fork（便于离线自检/兼容本地同时放两份的布局）。
#   * 只读上游文件（kernel/source.json、tools/build-kernel.sh 等），绝不写回；
#   * 所有产物落在 --inputs 指向的构建临时目录（CI 传 $RUNNER_TEMP）；
#   * 不联网以外的副作用、不碰设备、不执行 fastboot。
#
# 用法：
#   ./port-full-build.sh --fork <仓库根> --inputs <产物目录> \
#       [--upstream-src <自备的上游源码工作树>] \
#       [--kernel-src <已有内核 git 仓库>] \
#       [--esp-size-mib 512] [--root-image <ext4|sparse>] \
#       [--module-zip <SunUEFI-Piano-*.zip>] [--initramfs <cpio.gz>] \
#       [--stock-dtb-base <dtb>] [--stock-dtb-overlay <dtbo>] [--ufdt <bin>] \
#       [--dry-run] [--force]
#
# 输出（全部写入 --inputs 目录）：
#   port-inputs.env             给 workflow source 的变量（ESP_IMAGE/ROOT_IMAGE/...）
#   port-inputs-manifest.json   每项输入的来源、状态、sha256
#   Image / piano-boot.dtb / initramfs.cpio.gz / esp.raw / root.* / *.zip（按可用性）
set -euo pipefail

info() { printf '\033[36m[full-build]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

FORK=""; UPSTREAM_SRC=""; INPUTS=""; KERNEL_SRC=""; ESP_SIZE_MIB=512
ROOT_IMAGE=""; MODULE_ZIP=""; INITRAMFS=""
STOCK_DTB_BASE=""; STOCK_DTB_OVERLAY=""; UFDT=""
EXTERNAL_INPUTS=""
DRY_RUN=0; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --fork) FORK="$2"; shift 2;;
    --upstream-src) UPSTREAM_SRC="$2"; shift 2;;
    --inputs) INPUTS="$2"; shift 2;;
    --kernel-src) KERNEL_SRC="$2"; shift 2;;
    --external-inputs) EXTERNAL_INPUTS="$2"; shift 2;;
    --esp-size-mib) ESP_SIZE_MIB="$2"; shift 2;;
    --root-image) ROOT_IMAGE="$2"; shift 2;;
    --module-zip) MODULE_ZIP="$2"; shift 2;;
    --initramfs) INITRAMFS="$2"; shift 2;;
    --stock-dtb-base) STOCK_DTB_BASE="$2"; shift 2;;
    --stock-dtb-overlay) STOCK_DTB_OVERLAY="$2"; shift 2;;
    --ufdt) UFDT="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --force) FORCE=1; shift;;
    *) die "未知参数: $1";;
  esac
done

[ -n "$FORK" ] && [ -d "$FORK/port" ] || die "需要 --fork <本仓库根（含 port/）>"
[ -n "$INPUTS" ] || die "需要 --inputs <产物目录>"
FORK="$(cd "$FORK" && pwd)"
# 独立仓库不含上游源码；缺省退回 FORK（离线自检布局可把两者放一起）
UPSTREAM_SRC="${UPSTREAM_SRC:-$FORK}"
[ -d "$UPSTREAM_SRC" ] || die "--upstream-src 不存在: $UPSTREAM_SRC"
UPSTREAM_SRC="$(cd "$UPSTREAM_SRC" && pwd)"
mkdir -p "$INPUTS"
INPUTS="$(cd "$INPUTS" && pwd)"
[ "$FORCE" = 1 ] || { [ -z "$(ls -A "$INPUTS" 2>/dev/null)" ] || die "产物目录非空，加 --force 覆盖: $INPUTS"; }

PORT="$FORK/port"
PROFILE="$PORT/config/port-profile.json"
[ -f "$PROFILE" ] || die "缺 port/config/port-profile.json"

if [ ! -f "$UPSTREAM_SRC/kernel/source.json" ]; then
  die "缺上游源码元数据：$UPSTREAM_SRC/kernel/source.json
  本仓库是独立 port 层，不含 kernel/。选 source=kernel-source 时请先自备一份
  yzddmr6/xiaomipad-8pro-mainline 源码工作树并传 --upstream-src <该目录>；
  只想构建 UEFI 可用镜像包请用默认 source=upstream-release（无需源码）。"
fi

# ---------------------------------------------------------------- 0) 元数据
# 不硬编码内核仓库/commit：全部从上游 kernel/source.json 读取，
# 上游换基线时本脚本自动跟随。
info "[1/8] 读取上游内核元数据 kernel/source.json"
python3 - "$UPSTREAM_SRC/kernel/source.json" "$INPUTS/kernel-plan.json" <<'PY'
import json, sys
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
if not src.is_file():
    raise SystemExit(f"缺上游元数据: {src}")
d = json.loads(src.read_text())
need = ("url", "commit", "branch", "release_config_fragments")
missing = [k for k in need if not d.get(k)]
if missing:
    raise SystemExit(f"kernel/source.json 缺字段: {missing}")
frags = d["release_config_fragments"]
if not isinstance(frags, list) or not frags or not all(isinstance(x, str) and x for x in frags):
    raise SystemExit("release_config_fragments 必须是非空字符串数组")
plan = {
    "url": d["url"], "commit": d["commit"], "branch": d.get("branch", ""),
    "config": d.get("config", ""), "kernel_release": d.get("kernel_release", ""),
    "config_fragments": frags,
}
dst.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
print(f"  url    = {plan['url']}")
print(f"  commit = {plan['commit']}")
print(f"  branch = {plan['branch']}  release = {plan['kernel_release']}")
print(f"  config fragments = {len(frags)} 个")
PY

KURL=$(python3 -c "import json;print(json.load(open('$INPUTS/kernel-plan.json'))['url'])")
KCOMMIT=$(python3 -c "import json;print(json.load(open('$INPUTS/kernel-plan.json'))['commit'])")
KBRANCH=$(python3 -c "import json;print(json.load(open('$INPUTS/kernel-plan.json'))['branch'])")

# ---------------------------------------------------------------- 1) 源码
info "[2/8] 内核源码：clone / 校验固定 commit 与 config fragments"
if [ -z "$KERNEL_SRC" ]; then
  KERNEL_SRC="$INPUTS/kernel-src"
  if [ "$DRY_RUN" = 1 ]; then
    warn "dry-run：跳过 clone，仅记录将使用的源码地址"
  elif [ ! -d "$KERNEL_SRC/.git" ]; then
    git clone --no-checkout --filter=blob:none "$KURL" "$KERNEL_SRC"
    git -C "$KERNEL_SRC" fetch --depth 1 origin "$KCOMMIT"
    git -C "$KERNEL_SRC" checkout --detach "$KCOMMIT"
  fi
fi

SKIP_SRC_CHECK=0
if [ "$DRY_RUN" = 1 ] && [ ! -d "$KERNEL_SRC/.git" ]; then
  SKIP_SRC_CHECK=1
  warn "dry-run 且未提供 --kernel-src，跳过源码校验"
fi
if [ "$SKIP_SRC_CHECK" = 0 ]; then
  [ -d "$KERNEL_SRC/.git" ] || die "内核源码不是独立 git 仓库: $KERNEL_SRC（build-kernel.sh 硬性要求）"
  git -C "$KERNEL_SRC" cat-file -e "${KCOMMIT}^{commit}" \
    || die "内核源码缺固定 commit $KCOMMIT（请 fetch 该 commit 或删除 $KERNEL_SRC 重来）"
  python3 - "$KERNEL_SRC" "$INPUTS/kernel-plan.json" <<'PY'
import json, subprocess, sys
from pathlib import Path
src, plan = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())
miss = []
for f in plan["config_fragments"]:
    if not (src / f).is_file():
        miss.append(f + " (文件不存在)")
        continue
    r = subprocess.run(["git", "-C", str(src), "ls-files", "--error-unmatch", "--", f],
                       capture_output=True, text=True)
    if r.returncode != 0:
        miss.append(f + " (未被 git 跟踪)")
if miss:
    raise SystemExit("config fragments 校验失败: " + "; ".join(miss))
print(f"  commit 与 {len(plan['config_fragments'])} 个 config fragments 校验通过")
PY
fi

# ---------------------------------------------------------------- dry-run 出口
if [ "$DRY_RUN" = 1 ]; then
  info "dry-run：跳过工具链检查 / 内核编译 / ESP 镜像化"
  cat > "$INPUTS/port-inputs.env" <<EOF
DRY_RUN=1
INPUTS_DIR="$INPUTS"
FULL_BUILD_STATUS=DRY_RUN
ESP_IMAGE=""
ROOT_IMAGE=""
MODULE_ZIP=""
EOF
  python3 - "$INPUTS" <<'PY'
import json, sys
from pathlib import Path
d = Path(sys.argv[1])
(d / "port-inputs-manifest.json").write_text(json.dumps({
    "status": "DRY_RUN", "note": "仅校验元数据与源码契约，未编译、未产出镜像",
    "kernel_plan": json.loads((d / "kernel-plan.json").read_text()),
}, indent=2, ensure_ascii=False) + "\n")
PY
  info "dry-run 完成：$INPUTS/port-inputs.env"
  exit 0
fi

# ------------------------------------------------- 外部输入自动发现（本机产物）
# 公共 runner 无法自动获得的设备/发行版产物，由用户放到 --external-inputs 目录
# （默认 $FORK/port/inputs，属 port/ 自有命名空间）或由自建 runner 预置。
# 显式命令行参数优先于自动发现。
info "[3/8] 发现外部输入（initramfs / root 镜像 / Module ZIP / 原厂 DTB）"
EXT="$EXTERNAL_INPUTS"
[ -n "$EXT" ] || EXT="$FORK/port/inputs"
if [ -d "$EXT" ]; then
  info "  扫描 $EXT"
  [ -n "$INITRAMFS" ] || { for c in "$EXT"/initramfs.cpio.gz "$EXT"/initramfs.img; do [ -f "$c" ] && { INITRAMFS="$c"; break; }; done; }
  [ -n "$ROOT_IMAGE" ] || { for c in "$EXT"/root.ext4 "$EXT"/root.sparse.img "$EXT"/root.image "$EXT"/root-*.sparse.img; do [ -f "$c" ] && { ROOT_IMAGE="$c"; break; }; done; }
  [ -n "$MODULE_ZIP" ] || { for c in "$EXT"/SunUEFI-Piano*.zip; do [ -f "$c" ] && { MODULE_ZIP="$c"; break; }; done; }
  [ -n "$STOCK_DTB_BASE" ] || { for c in "$EXT"/stock-base.dtb "$EXT"/stock.dtb; do [ -f "$c" ] && { STOCK_DTB_BASE="$c"; break; }; done; }
  [ -n "$STOCK_DTB_OVERLAY" ] || { for c in "$EXT"/stock-overlay.dtbo "$EXT"/stock.dtbo; do [ -f "$c" ] && { STOCK_DTB_OVERLAY="$c"; break; }; done; }
  [ -n "$UFDT" ] || { for c in "$EXT"/ufdt "$EXT"/ufdt.py; do [ -f "$c" ] && { UFDT="$c"; break; }; done; }
  for pair in "initramfs:$INITRAMFS" "root:$ROOT_IMAGE" "module:$MODULE_ZIP" \
              "dtb-base:$STOCK_DTB_BASE" "dtb-overlay:$STOCK_DTB_OVERLAY" "ufdt:$UFDT"; do
    info "    ${pair%%:*} = ${pair#*:}" | sed 's/= $/= (未提供)/'
  done
else
  warn "  外部输入目录不存在: $EXT"
  warn "  请把本机产出的 initramfs.cpio.gz / root.ext4 / SunUEFI-Piano-*.zip 等放进去，"
  warn "  或改用 FULL_BUILD=false 只跑 CI 骨架（发布门禁会强制降级为草稿+预发布）。"
fi

# ---------------------------------------------------------------- 工具链
info "[4/8] 检查构建工具链（缺一即失败，不静默降级）"
MISSING=()
for t in aarch64-linux-gnu-gcc make dtc python3 mkfs.vfat mcopy truncate; do
  command -v "$t" >/dev/null 2>&1 || MISSING+=("$t")
done
if [ ${#MISSING[@]} -gt 0 ]; then
  die "缺工具: ${MISSING[*]}
  Ubuntu/Debian: sudo apt-get install -y gcc-aarch64-linux-gnu make device-tree-compiler mtools coreutils
  建议用 self-hosted ARM64 runner，或把已构建产物通过 --root-image/--module-zip 传入。"
fi
info "  工具链齐全"

# ---------------------------------------------------------------- 3) 编译
info "[5/8] 调用上游 tools/build-kernel.sh 编译内核"
KOUT="$INPUTS/kernel-dev"
mkdir -p "$KOUT"
# 只读调用上游脚本；KERNEL_SOURCE/OUT_DIR 指向构建临时目录，零上游写入。
KERNEL_SOURCE="$KERNEL_SRC" OUT_DIR="$KOUT" \
CROSS_COMPILE=aarch64-linux-gnu- JOBS="$(nproc)" \
  bash "$UPSTREAM_SRC/tools/build-kernel.sh"
info "  内核构建完成：$KOUT"

KIMG=""
for c in "$KOUT/Image" "$KERNEL_SRC/arch/arm64/boot/Image" "$KOUT/arch/arm64/boot/Image"; do
  [ -f "$c" ] && { KIMG="$c"; break; }
done
[ -n "$KIMG" ] || die "未找到编译产出的 arm64 Image（检查 build-kernel.sh 的 OUT_DIR 布局）"

# initramfs：build-initramfs.sh 需要 BUSYBOX + GEN_INIT_CPIO 两个外部输入，
# 公共 runner 无法可靠提供，故要求显式传入。
[ -n "$INITRAMFS" ] && [ -f "$INITRAMFS" ] \
  || die "缺 initramfs：请传 --initramfs <initramfs.cpio.gz>。
  可由本机执行 BUSYBOX=... GEN_INIT_CPIO=... OUT_DIR=... bash tools/build-initramfs.sh 产出，
  或复用上游 release 里的 initramfs；本脚本不会拿占位 cpio 冒充。"
cp -f "$INITRAMFS" "$INPUTS/initramfs.cpio.gz"

# DTB：若提供了原厂 base/overlay + ufdt 则跑 prepare-boot-dtb.py 合并；
# 否则退回内核自带 DTB 并显著标记（真机显示/触摸可能异常）。
DTB_STATUS="KERNEL_DTB_ONLY"
if [ -n "$STOCK_DTB_BASE" ] && [ -n "$STOCK_DTB_OVERLAY" ] && [ -n "$UFDT" ]; then
  info "  合并原厂 DTB overlay（prepare-boot-dtb.py）"
  python3 "$UPSTREAM_SRC/tools/prepare-boot-dtb.py" \
    --mainline-dtb "$KERNEL_SRC/arch/arm64/boot/dts/qcom/sm8750-xiaomi-piano.dtb" \
    --stock-base "$STOCK_DTB_BASE" --stock-overlay "$STOCK_DTB_OVERLAY" \
    --ufdt "$UFDT" --output "$INPUTS/dtb-work"
  cp -f "$INPUTS/dtb-work/piano-boot.dtb" "$INPUTS/piano-boot.dtb"
  DTB_STATUS="STOCK_OVERLAY_MERGED"
else
  KDTB="$KERNEL_SRC/arch/arm64/boot/dts/qcom/sm8750-xiaomi-piano.dtb"
  [ -f "$KDTB" ] || die "缺内核 DTB: $KDTB"
  cp -f "$KDTB" "$INPUTS/piano-boot.dtb"
  warn "未提供 --stock-dtb-base/--stock-dtb-overlay/--ufdt，使用内核原生 DTB。"
  warn "原厂 DTB overlay 只能从你的设备提取；未合并时真机显示/触摸/充电可能异常。"
fi
cp -f "$KIMG" "$INPUTS/Image"

# ---------------------------------------------------------------- 4) ESP 镜像化
info "[6/8] 生成 ESP 目录并打成 FAT32 raw 镜像（4K 对齐）"
ESPDIR="$INPUTS/esp-tree"
python3 "$PORT/tools/port-stage-esp.py" \
  --image "$INPUTS/Image" --dtb "$INPUTS/piano-boot.dtb" \
  --initrd "$INPUTS/initramfs.cpio.gz" \
  --profile "$PROFILE" --output "$ESPDIR" --force
ESPIMG="$INPUTS/esp.raw"
truncate -s "${ESP_SIZE_MIB}M" "$ESPIMG"
mkfs.vfat -F 32 -n SUNUEFI_ESP "$ESPIMG" >/dev/null
MTOOLS_SKIP_CHECK=1 mcopy -i "$ESPIMG" -s "$ESPDIR/EFI" ::
SZ=$(stat -c %s "$ESPIMG")
[ $((SZ % 4096)) -eq 0 ] || die "ESP 镜像未按 4K 对齐: $SZ"
info "  esp.raw = ${SZ}B（${ESP_SIZE_MIB}MiB）"

# ---------------------------------------------------------------- 5) 外部输入
info "[7/8] 归置外部输入（root 镜像 / Android 助手模块）"
copy_input() {  # <源> <目标文件名> <标签>
  local src="$1" name="$2" label="$3"
  if [ -n "$src" ] && [ -f "$src" ]; then
    cp -f "$src" "$INPUTS/$name"
    info "  $label = $INPUTS/$name"
    echo "READY"
  else
    warn "  $label 未提供 → 该项产物将是占位骨架（发布门禁会拦截正式发布）"
    echo "MISSING"
  fi
}
ROOT_STATE=$(copy_input "$ROOT_IMAGE" "root.image" "root 镜像")
if [ "$ROOT_STATE" = "READY" ]; then ROOT_IMAGE_PATH="$INPUTS/root.image"; else ROOT_IMAGE_PATH=""; fi
MODULE_STATE=$(copy_input "$MODULE_ZIP" "SunUEFI-Piano.zip" "Module ZIP")
if [ "$MODULE_STATE" = "READY" ]; then MODULE_ZIP_PATH="$INPUTS/SunUEFI-Piano.zip"; else MODULE_ZIP_PATH=""; fi

# ---------------------------------------------------------------- 6) 输出契约
info "[8/8] 写 port-inputs.env / port-inputs-manifest.json"
if [ "$ROOT_STATE" = "READY" ] && [ "$MODULE_STATE" = "READY" ] && [ "$DTB_STATUS" = "STOCK_OVERLAY_MERGED" ]; then
  STATUS="READY"
else
  STATUS="PARTIAL_EXTERNAL_INPUTS"
fi
cat > "$INPUTS/port-inputs.env" <<EOF
DRY_RUN=0
INPUTS_DIR="$INPUTS"
FULL_BUILD_STATUS="$STATUS"
ESP_IMAGE="$ESPIMG"
ROOT_IMAGE="$ROOT_IMAGE_PATH"
MODULE_ZIP="$MODULE_ZIP_PATH"
DTB_STATUS="$DTB_STATUS"
EOF

python3 - "$INPUTS" "$STATUS" "$DTB_STATUS" "$ROOT_STATE" "$MODULE_STATE" "$KCOMMIT" <<'PY'
import hashlib, json, sys
from pathlib import Path
d, status, dtb, root, module, commit = Path(sys.argv[1]), *sys.argv[2:7]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
files = {}
for name in ("Image", "piano-boot.dtb", "initramfs.cpio.gz", "esp.raw",
             "root.image", "SunUEFI-Piano.zip"):
    p = d / name
    if p.is_file():
        files[name] = {"bytes": p.stat().st_size, "sha256": sha(p)}
(d / "port-inputs-manifest.json").write_text(json.dumps({
    "status": status,
    "kernel_commit": commit,
    "dtb_status": dtb,
    "root_input": root, "module_input": module,
    "files": files,
    "note": "READY 表示三产物均有真实镜像；PARTIAL 表示部分输入仍需从设备/本机提供。"
            "无论哪种，发布门禁以 port-release-assets.py 的 placeholder 检测为准。",
}, indent=2, ensure_ascii=False) + "\n")
print("  status =", status, "| 真实产物:", ", ".join(files))
PY

info "完成。workflow 请 source $INPUTS/port-inputs.env 后把 ESP_IMAGE/ROOT_IMAGE/MODULE_ZIP 传给 port-build.sh"
