#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# 上游 Release -> UEFI 三产物输入（需求 3 的正解：主干用上游源码/构建成品，跟着他更新）。
#
# 与旧 port-full-build.sh 的本质区别：
#   * 旧路径试图在公共 runner 上「从内核源码重新编译」，需要 QEMU chroot /
#     Ubuntu ISO / 原厂 DTB / 签名密钥 / 手工外部输入，跑不通，只能产占位骨架；
#   * 本路径消费上游 yzddmr6 **最新 Release 里由他源码编译好的成品**（A 槽三元组
#     + root.sparse.img + 切换模块），只做 UEFI 化后处理。上游发新版自动跟随。
#
# 流水线：
#   1. resolve  : GitHub API 查 latest Release（不写死 tag），出 downloads.tsv
#   2. download : 按 role 下载 *-boot.zip / *-tools.zip / *-rootfs.img.zst.part*
#                 + SHA256SUMS.assets / assets-manifest.json，逐个 sha256 校验
#   3. rootfs   : cat part* | zstd -d > root.sparse.img（已是 android sparse，原样透传）
#   4. adapt    : 解 boot 三元组 -> Image/initramfs/DTB -> 本机固件四件套 ESP
#                 （Image / board.dtb / initramfs / boot.img，boot.img = Android boot v2）
#                 -> 生成含 piano/piano 与 sunuefi_root 契约的真实 Module ZIP
#   5. esp.raw  : mkfs.vfat -S 4096（对齐原厂 4 KiB 扇区）+ mcopy 打成 FAT32 镜像
#   6. 输出 port-inputs.env（与 workflow 契约一致）+ upstream-release.json / upstream-adapt.json
#
# 用法：
#   ./port-from-upstream-release.sh --fork <本仓库根> --inputs <产物目录> \
#       [--repo yzddmr6/xiaomipad-8pro-mainline] [--tag <留空=latest>] \
#       [--skip-root] [--fixture <本地资产目录>] [--force]
#
# 依赖（GitHub Actions ubuntu-24.04-arm 上可 apt 安装）：
#   curl, unzip, zstd, mkfs.vfat, mcopy(mtools), sha256sum, python3
# 独立 port 仓库：只写 --inputs 目录；不碰设备、不执行 fastboot。
set -euo pipefail

info() { printf '\033[36m[upstream]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

FORK=""; INPUTS=""; REPO="yzddmr6/xiaomipad-8pro-mainline"; TAG=""
SKIP_ROOT=0; FIXTURE=""; FORCE=0; ESP_SIZE_MIB=512
while [ $# -gt 0 ]; do
  case "$1" in
    --fork) FORK="$2"; shift 2;;
    --inputs) INPUTS="$2"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    --tag) TAG="$2"; shift 2;;
    --esp-size-mib) ESP_SIZE_MIB="$2"; shift 2;;
    --fixture) FIXTURE="$2"; shift 2;;
    --skip-root) SKIP_ROOT=1; shift;;
    --force) FORCE=1; shift;;
    *) die "未知参数: $1";;
  esac
done

[ -n "$FORK" ] && [ -d "$FORK/port" ] || die "需要 --fork <本仓库根（含 port/）>"
[ -n "$INPUTS" ] || die "需要 --inputs <产物目录>"
FORK="$(cd "$FORK" && pwd)"
mkdir -p "$INPUTS"
INPUTS="$(cd "$INPUTS" && pwd)"
[ "$FORCE" = 1 ] || { [ -z "$(ls -A "$INPUTS" 2>/dev/null)" ] || die "产物目录非空，加 --force 覆盖: $INPUTS"; }
TOOLS="$FORK/port/tools"
ADAPTER="$TOOLS/port-upstream-release.py"
[ -f "$ADAPTER" ] || die "缺 $ADAPTER"

CACHE="$INPUTS/_assets"
mkdir -p "$CACHE"

# ---------------------------------------------------------------- 1) resolve
info "[1/6] 解析上游最新 Release（repo=$REPO, tag=${TAG:-latest}）"
if [ -n "$FIXTURE" ]; then
  [ -d "$FIXTURE" ] || die "--fixture 目录不存在: $FIXTURE"
  cp -f "$FIXTURE"/upstream-release.json "$INPUTS/" 2>/dev/null || \
    python3 - "$FIXTURE" "$INPUTS" "$REPO" <<'PY'
import json, sys
from pathlib import Path
f, out, repo = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
tag = (f / "TAG").read_text().strip() if (f / "TAG").is_file() else "fixture-local"
assets = []
for p in sorted(f.iterdir()):
    if p.is_file() and p.name not in ("TAG", "upstream-release.json", "SHA256SUMS.assets"):
        assets.append({"name": p.name, "asset_id": 0, "size": p.stat().st_size,
                       "role": "fixture", "api_url": ""})
(out / "upstream-release.json").write_text(json.dumps({
    "schema": 1, "source": "upstream-release", "repo": repo, "tag": tag,
    "published_at": None, "target_commitish": "fixture", "html_url": "",
    "name": tag, "assets": assets, "note": "离线 fixture，用于自检转换链路",
}, indent=2, ensure_ascii=False) + "\n")
PY
  info "  fixture 模式：跳过 GitHub API（tag=$(python3 -c "import json;print(json.load(open('$INPUTS/upstream-release.json'))['tag'])")）"
else
  python3 "$ADAPTER" resolve --repo "$REPO" --tag "$TAG" --inputs "$INPUTS"
fi
UP_TAG=$(python3 -c "import json;print(json.load(open('$INPUTS/upstream-release.json'))['tag'])")
info "  上游版本 = $UP_TAG"

# ------------------------------------------------------------- 2) 下载 + 校验
info "[2/6] 获取上游资产并校验 SHA256SUMS.assets"
if [ -n "$FIXTURE" ]; then
  cp -f "$FIXTURE"/* "$CACHE"/ 2>/dev/null || true
  cp -f "$CACHE/SHA256SUMS.assets" "$INPUTS/" 2>/dev/null || true
  info "  fixture 模式：从 $FIXTURE 复制资产"
else
  [ -f "$INPUTS/downloads.tsv" ] || die "缺 downloads.tsv（resolve 未产出）"
  TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
  dl() {  # <name> <asset_id> <role>
    local name="$1" id="$2" role="$3" dest="$CACHE/$1"
    [ -f "$dest" ] || {
      info "  下载 $name ($role)"
      curl -sSL --fail --retry 3 --retry-delay 2 --connect-timeout 30 --max-time 1800 \
        -H "Authorization: Bearer $TOKEN" -H "Accept: application/octet-stream" \
        "https://api.github.com/repos/$REPO/releases/assets/$id" -o "$dest" \
        || die "下载失败: $name（asset id=$id）"
    }
  }
  while IFS=$'\t' read -r name id size role url; do
    case "$role" in
      checksums|manifest|boot|tools) dl "$name" "$id" "$role";;
      root-part) [ "$SKIP_ROOT" = 1 ] || dl "$name" "$id" "$role";;
      *) : ;;  # packages.zip 等不需要
    esac
  done < "$INPUTS/downloads.tsv"
  cp -f "$CACHE/SHA256SUMS.assets" "$INPUTS/" || die "缺 SHA256SUMS.assets"
fi

# 逐个校验已下载资产（SHA256SUMS.assets 覆盖全部上游资产，含未下载的 packages.zip）
info "  校验 sha256 ..."
CHECKED=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  expected="${line%% *}"; fname="${line#*  }"; fname="${fname#\*}"
  [ -f "$CACHE/$fname" ] || continue
  actual=$(sha256sum "$CACHE/$fname" | cut -d' ' -f1)
  [ "$actual" = "$expected" ] || die "SHA256 不匹配: $fname（期望 $expected，实际 $actual）"
  info "    OK $fname"
  CHECKED=$((CHECKED + 1))
done < "$CACHE/SHA256SUMS.assets"
[ "$CHECKED" -gt 0 ] || warn "SHA256SUMS.assets 中没有命中已下载资产（fixture 可能未提供校验文件）"

# ------------------------------------------------------------ 3) rootfs 分卷
info "[3/6] 拼接 rootfs 分卷并解压为 root.sparse.img"
if [ "$SKIP_ROOT" = 1 ]; then
  warn "  --skip-root：跳过 3.5GB 分卷下载/解压（仅用于快速验证链路）"
  ROOT_STATE="SKIPPED"
else
  mapfile -t PARTS < <(find "$CACHE" -maxdepth 1 -name '*-rootfs.img.zst.part*' | sort -V)
  if [ "${#PARTS[@]}" -eq 0 ]; then
    warn "  未发现 rootfs 分卷，root 产物将缺失（发布门禁会拦截）"
    ROOT_STATE="MISSING"
  else
    command -v zstd >/dev/null 2>&1 || die "缺 zstd（CI 请 apt-get install -y zstd）"
    info "  ${#PARTS[@]} 个分卷 -> cat | zstd -d -> root.sparse.img"
    cat "${PARTS[@]}" | zstd -d -T0 --stdout > "$INPUTS/root.sparse.img"
    SZ=$(stat -c %s "$INPUTS/root.sparse.img")
    [ "$SZ" -gt 65536 ] || die "解压出的 root.sparse.img 过小: ${SZ}B"
    # 分卷累计 3.5GB，解压后立刻删掉，避免 runner 磁盘翻倍占用
    rm -f "${PARTS[@]}"
    info "  已释放分卷占用；root.sparse.img ${SZ}B"
    python3 - "$INPUTS/root.sparse.img" <<'PY'
import struct, sys
from pathlib import Path
p = Path(sys.argv[1])
head = p.open("rb").read(28)
if len(head) == 28 and struct.unpack_from("<I", head, 0)[0] == 0xed26ff3a:
    magic, maj, minr, fhs, chs, blk, blks, chunks, crc = struct.unpack("<IHHHHIIII", head)
    print(f"  android sparse: block={blk} total_blks={blks} chunks={chunks} "
          f"逻辑大小={blk*blks/1024**3:.1f}GiB 实际={p.stat().st_size/1024**3:.2f}GiB")
else:
    print("  [warn] 不是 android sparse 头，按 raw ext4 处理（打包器会现场编码）")
PY
    ROOT_STATE="READY"
  fi
fi

# ------------------------------------------------------------ 4) tools/boot 解包
info "[4/6] 解上游 boot.zip / tools.zip"
cp -f "$CACHE"/*-boot.zip "$INPUTS/" 2>/dev/null || die "缺 *-boot.zip"
command -v unzip >/dev/null 2>&1 || die "缺 unzip"
mkdir -p "$INPUTS/boot-unpacked" "$INPUTS/tools-unpacked"
unzip -oq "$INPUTS"/*-boot.zip -d "$INPUTS/boot-unpacked"
for t in "$CACHE"/*-tools.zip; do
  [ -f "$t" ] && unzip -oq "$t" -d "$INPUTS/tools-unpacked" || true
done
info "  boot 成员: $(find "$INPUTS/boot-unpacked" -type f | wc -l) 个"

# ---------------------------------------------------------------- 5) adapt
info "[5/6] UEFI 化适配（内核/DTB/initramfs -> ESP + Module）"
python3 "$ADAPTER" adapt --fork "$FORK" --inputs "$INPUTS" --repo "$REPO" --tag "$UP_TAG"

# ------------------------------------------------------------- 6) ESP FAT32
info "[6/6] 把 ESP 目录打成 FAT32 raw 镜像"
for t in truncate mkfs.vfat mcopy; do
  command -v "$t" >/dev/null 2>&1 || die "缺 $t（CI 请 apt-get install -y mtools）"
done
ESPIMG="$INPUTS/esp.raw"
truncate -s "${ESP_SIZE_MIB}M" "$ESPIMG"
# 本机原厂 ESP 的几何是 4096 B/扇区、1 扇区/簇、保留 32 扇区、2 份 FAT
# （实测 BPS=4096 SPC=1 RsvdSec=32 NumFAT=2）。mkfs.vfat 默认 512 B 扇区，
# 而分区的逻辑扇区是 4 KiB，固件按设备扇区读 FAT 会整体错位——必须显式对齐。
mkfs.vfat -F 32 -S 4096 -s 1 -R 32 -f 2 -n SUNUEFI_ESP "$ESPIMG" >/dev/null
MTOOLS_SKIP_CHECK=1 mcopy -i "$ESPIMG" -s "$INPUTS/esp-tree/EFI" ::
SZ=$(stat -c %s "$ESPIMG")
[ $((SZ % 4096)) -eq 0 ] || die "ESP 镜像未 4K 对齐: $SZ"
grep -q '^ESP_IMAGE=' "$INPUTS/port-inputs.env" || die "缺 port-inputs.env"
# 值一律加引号：KERNEL_SOURCE 等含空格，裸写会让 workflow 的 source 把 "+" 当命令
sed -i "s|^ESP_IMAGE=.*|ESP_IMAGE=\"$ESPIMG\"|" "$INPUTS/port-inputs.env"
grep -q '^ROOT_STATE=' "$INPUTS/port-inputs.env" || echo "ROOT_STATE=\"$ROOT_STATE\"" >> "$INPUTS/port-inputs.env"
info "  esp.raw = ${SZ}B (${ESP_SIZE_MIB}MiB)"

python3 - "$INPUTS" "$UP_TAG" "$REPO" "$ROOT_STATE" <<'PY'
import hashlib, json, sys
from pathlib import Path
d, tag, repo, root_state = Path(sys.argv[1]), *sys.argv[2:5]
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
files = {}
for n in ("Image", "piano-boot.dtb", "initramfs.cpio.gz", "esp.raw",
          "root.sparse.img", "SunUEFI-Piano-%s.zip" % tag):
    p = d / n
    if p.is_file() and p.stat().st_size:
        files[n] = {"bytes": p.stat().st_size, "sha256": sha(p)}
mp = d / "upstream-adapt.json"
r = json.loads(mp.read_text()) if mp.is_file() else {}
r.update({"upstream_tag": tag, "upstream_repo": repo,
          "root_state": root_state, "real_images": files,
          "status": "READY" if "esp.raw" in files and "root.sparse.img" in files
                    else "PARTIAL_NO_ROOT"})
mp.write_text(json.dumps(r, indent=2, ensure_ascii=False) + "\n")
print("  真实镜像:", ", ".join(f"{k}({v['bytes']}B)" for k, v in files.items()))
PY

info "完成：$INPUTS/port-inputs.env"
info "  source=upstream-release  tag=$UP_TAG  上游=$REPO"
info "  提示：本脚本不碰设备；真机引导需验证 G1 挂载 / G2 EFI Stub / G3 OS4.0.5 BOOT 头兼容"
