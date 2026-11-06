#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# 把 UEFI 移植层应用到你 fork 的 xiaomipad-8pro-mainline 工作树。
#
# 设计原则（需求 3，patchless 只增不改）：UEFI 全部落在两个上游不占用的命名空间
#   port/  与  .github/workflows/sunuefi-*.yml
# 绝不修改上游受管文件（kernel/ device/ ubuntu/ tools/ ...），因此随时可
# git merge upstream/main 而不冲突。
#
# 用法： ./apply-port.sh <你的 mainline fork 路径> [SunUEFI 源码路径]
#  - 第 1 参数必填：本地 mainline fork 的 git 工作树根目录（含 .git）
#  - 第 2 参数可选：本地 Project-SunUEFI 源码路径（只登记引用，不复制二进制）
#
# 本脚本只写文件，不编译内核、不构建 UEFI 固件、不碰任何设备。
set -euo pipefail

KIT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-}"
SUNUEFI="${2:-}"

if [ -z "$TARGET" ] || [ ! -d "$TARGET/.git" ]; then
  echo "用法: $0 <mainline fork 的 git 工作树路径> [SunUEFI 源码路径]" >&2
  echo "第 1 参数必须是已 clone 的 mainline 工作树（含 .git）。" >&2
  exit 2
fi
TARGET="$(cd "$TARGET" && pwd)"

echo "== 目标工作树: $TARGET"
echo "== 移植层来源: $KIT/overlay"

# 1. 复制自包含 port/ 子树（上游无此目录；若已存在先备份）
if [ -e "$TARGET/port" ]; then
  echo "!! $TARGET/port 已存在，先备份为 port.bak.$(date +%s)" >&2
  cp -a "$TARGET/port" "$TARGET/port.bak.$(date +%s)"
  rm -rf "$TARGET/port"
fi
cp -a "$KIT/overlay/port" "$TARGET/port"

# 2. 复制 GitHub Actions（只放带 sunuefi- 前缀的 workflow，避免与上游/未来 workflow 撞名）
mkdir -p "$TARGET/.github/workflows"
cp -a "$KIT"/overlay/.github/workflows/sunuefi-*.yml "$TARGET/.github/workflows/"

# 3. 同步 verify-port.sh 到 port/ 内（供 merge 后自检调用）
cp -a "$KIT/verify-port.sh" "$TARGET/port/verify-port.sh"
# selftest 需要 tests/ 目录，复制进 port/tests（保持 port/ 自包含）
mkdir -p "$TARGET/port/tests"
cp -a "$KIT/tests/selftest.py" "$TARGET/port/tests/selftest.py"

# 4. 清理缓存/字节码污染
find "$TARGET/port" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$TARGET/port" -type f -name '*.pyc' -delete 2>/dev/null || true
chmod +x "$TARGET"/port/tools/*.py "$TARGET"/port/tools/*.sh \
         "$TARGET"/port/build/*.sh "$TARGET"/port/verify-port.sh \
         "$TARGET"/port/tests/*.py 2>/dev/null || true

# 5. 登记 SunUEFI 引导层引用（固定 commit 清单，不复制二进制）
SUN_COMMIT="(未提供 SunUEFI 路径)"
if [ -n "$SUNUEFI" ] && [ -d "$SUNUEFI/.git" ]; then
  SUN_COMMIT="$(git -C "$SUNUEFI" rev-parse HEAD)"
  {
    echo "# SunUEFI 引导层引用清单（apply-port.sh 生成，勿手改）"
    echo "# 来源 commit: $SUN_COMMIT"
    echo "# 建议 git subtree 固定引入："
    echo "#   git subtree add --prefix=sunuefi <你的 SunUEFI fork url> main --squash"
    echo "uefi/core uefi/components uefi/handoff uefi/platforms"
    echo "android/module android/native"
    echo "tools/build_android_product_module.py tools/build_boot_request.py"
    echo "tools/build_boot_repack.py tools/package_sparse_images.py"
    echo "tools/package_kernel_payload.py tools/install_piano.py"
    echo "vendor/piano vendor/piano-linux"
  } > "$TARGET/port/docs/SUNUEFI-SUBTREE.txt"
else
  echo "# 未提供 SunUEFI 路径；按 PORTING-MAP.md 第 1 节用 git subtree 引入引导层。" \
    > "$TARGET/port/docs/SUNUEFI-SUBTREE.txt"
fi

# 6. 接线清单：全部是「只读/后处理/校验」，明确零上游修改
cat > "$TARGET/port/docs/WIRING-POINTS.txt" <<'EOF'
UEFI 移植接线点 —— 全部「不修改上游源码」（需求 3：同步不冲突）
================================================================
上一版曾要求人工改 5 处上游文件，本版已全部撤销，改为对上游「构建产物」做后处理：

  1) 内核 EFI Stub：不改 kernel/source.json。CONFIG_EFI_STUB 作为构建期「只读校验项」
     （见 config/port-profile.json 的 kernel_config_requirements），由 CI 检查产物；
     确需开启时在你自己的内核 fork 分支里加，绝不写回上游目录。
  2) 内核产物对接：port/tools/port-stage-esp.py 读取 build-kernel.sh/
     prepare-boot-dtb.py/build-initramfs.sh 的产物，输出 ESP 布局（不写上游）。
  3) rootfs 后处理：port/tools/port-inject-login.py（piano/piano + root piano）
     + port-adapt-rootfs.py（fstab/root-policy）作用于「解出的 rootfs 目录」，
     不改 ubuntu/、不改 build-rootfs-image.py 的校验逻辑。
  4) 切换/分区：源仓库三元组轮换脚本保留不动（只「不调用」）；改由
     port/tools/port-switch.sh + SunUEFI Android 助手模块负责；分区计划见
     port-partition-plan.py（新建 sunuefi_esp/sunuefi_root，不动 userdata）。
  5) 文档：不改上游 docs/；全部说明收进 port/docs/。

唯一需要「真机构建时」人工闭环的仍是 PORTING-MAP.md 的三个契约缺口
（G1 挂载契约 / G2 EFI Stub / G3 合体 BOOT 头），它们是验证动作，不是文件修改。
EOF

# 7. 上游 remote 提示（不自动 fetch，避免联网）
cat > "$TARGET/port/docs/UPSTREAM-SETUP.txt" <<'EOF'
一次性配置（详见 port/docs/SYNC-UPSTREAM.md）：
  git remote add upstream https://github.com/yzddmr6/xiaomipad-8pro-mainline.git
  bash port/tools/port-sync-upstream.sh . --check   # 查更新
  bash port/tools/port-sync-upstream.sh . --merge   # 合并（port/ 原样保留）
  git push origin main                              # 触发 sunuefi-build.yml
GitHub Secrets 可选配 SYNC_TOKEN（repo PAT），让同步后主动派发重建事件。

发布 Release（需求 7，见 port/docs/RELEASE.md）：
  git tag -a v0.10.0-port1 -m "SunUEFI port1" && git push origin v0.10.0-port1
  # tag push -> sunuefi-build.yml 构建 + publish job 自动发布
  # 或 Actions -> SunUEFI build products -> 勾 publish_release
  # 或 Actions -> SunUEFI publish release -> 填 tag 从已有 run 补发（不重编译）
EOF

# 8. MANIFEST
cat > "$TARGET/port/MANIFEST.json" <<EOF
{
  "applied_by": "apply-port.sh",
  "schema": 3,
  "target_repo": "xiaomipad-8pro-mainline (fork)",
  "upstream_kernel": "7.2.14-piano56 (linux-sm8750-piano)",
  "sunuefi_source_commit": "$SUN_COMMIT",
  "mode": "patchless-self-contained-subtree",
  "port_owned_namespaces": ["port/", ".github/workflows/sunuefi-*"],
  "touched_upstream_files": "none（零上游文件修改，见 docs/WIRING-POINTS.txt）",
  "requirements": {
    "1_uefi_dual_boot": "port/tools/port-stage-esp.py + port-switch.sh",
    "2_partitions": "sunuefi_esp/sunuefi_root (config/port-profile.json)",
    "3_upstream_sync": "port/tools/port-sync-upstream.sh + port-check-isolation.py + workflows/sunuefi-sync-upstream.yml",
    "4_three_artifacts": "port/tools/port-package-artifacts.py (SunUEFI-Module/-ESP/-Root-<target>)",
    "5_login": "piano/piano + root piano (port/tools/port-inject-login.py)",
    "6_github_actions": ".github/workflows/sunuefi-build.yml + sunuefi-sync-upstream.yml",
    "7_release": "sunuefi-build.yml#publish + sunuefi-release.yml + port/tools/port-release-assets.py + config/release-policy.json + docs/RELEASE.md"
  },
  "status": "PORT_APPLIED_NOT_BUILT_NOT_FLASHED"
}
EOF

echo "== 完成。UEFI 移植层已落到：$TARGET/port 与 $TARGET/.github/workflows/sunuefi-*.yml"
echo "== 下一步： $KIT/verify-port.sh $TARGET   （离线自检）"
echo "== 同步上游：见 $TARGET/port/docs/SYNC-UPSTREAM.md"
