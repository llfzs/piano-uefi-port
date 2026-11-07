#!/usr/bin/env python3
"""把四件套打包成固件能读的 FAT32 ESP 镜像。

固件通过 UEFI Simple File System 读 `\\EFI\\Piano\\stable\\` 下的四个文件，
所以 ESP 必须是 FAT32。本机既没有 mkfs.vfat 也没有 mtools，而且原厂 ESP
用的是 4 KiB 扇区这种非常规几何（`mkfs.fat -S 4096`），手搓引导扇区风险
不小。因此这里不自己排版式，而是拿一份原厂 ESP 镜像当模板：

* 引导扇区、保留扇区、FSInfo 全部逐字节沿用模板；
* FAT 表清空后重新分配；
* 目录项直接沿用模板里的那一条（连同它前面的长文件名项），只改大小和
  起始簇。这样长文件名、大小写、时间戳都和固件已经认得住的那份一致。

用法：
    make-esp-image.py --template stock-esp.img \\
        --source ../tmp/native-esp --output sunuefi_esp.img
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from pathlib import Path

FILES = ("Image", "board.dtb", "initramfs", "boot.img")
SUBDIR = Path("EFI/Piano/stable")

ATTR_VOLUME = 0x08
ATTR_DIR = 0x10
ATTR_LFN = 0x0F


class Geometry:
    def __init__(self, boot: bytes) -> None:
        (self.bytes_per_sector,) = struct.unpack_from("<H", boot, 11)
        self.sectors_per_cluster = boot[13]
        (self.reserved_sectors,) = struct.unpack_from("<H", boot, 14)
        self.fat_count = boot[16]
        (self.total_sectors,) = struct.unpack_from("<I", boot, 32)
        (self.fat_sectors,) = struct.unpack_from("<I", boot, 36)
        (self.root_cluster,) = struct.unpack_from("<I", boot, 44)
        (self.fsinfo_sector,) = struct.unpack_from("<H", boot, 48)
        self.label = boot[71:82].decode("latin-1").rstrip()

        self.cluster_bytes = self.bytes_per_sector * self.sectors_per_cluster
        self.fat_offset = self.reserved_sectors * self.bytes_per_sector
        self.data_offset = (
            self.reserved_sectors + self.fat_count * self.fat_sectors
        ) * self.bytes_per_sector
        self.cluster_count = (self.total_sectors * self.bytes_per_sector - self.data_offset) // self.cluster_bytes
        if self.cluster_count < 65525:
            sys.exit(f"模板不是 FAT32：只有 {self.cluster_count} 个簇，少于 65525")

    def cluster_offset(self, cluster: int) -> int:
        return self.data_offset + (cluster - 2) * self.cluster_bytes


def read_fat(image: Path, geom: Geometry) -> bytes:
    with image.open("rb") as f:
        f.seek(geom.fat_offset)
        return f.read(geom.fat_sectors * geom.bytes_per_sector)


def chain_of(fat: bytes, start: int) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    cluster = start
    while 2 <= cluster < 0x0FFFFFF8 and cluster not in seen:
        seen.add(cluster)
        out.append(cluster)
        (nxt,) = struct.unpack_from("<I", fat, cluster * 4)
        nxt &= 0x0FFFFFFF
        if nxt < 2 or nxt >= 0x0FFFFFF8:
            break
        cluster = nxt
    return out


def read_cluster_chain(fh, geom: Geometry, fat: bytes, start: int) -> bytes:
    parts = []
    for cluster in chain_of(fat, start):
        fh.seek(geom.cluster_offset(cluster))
        parts.append(fh.read(geom.cluster_bytes))
    return b"".join(parts)


def short_name_of(entry: bytes) -> str:
    base = entry[0:8].decode("latin-1").rstrip()
    ext = entry[8:11].decode("latin-1").rstrip()
    return f"{base}.{ext}" if ext else base


def assemble_lfn(entries: list[bytes]) -> str | None:
    """把同一个长文件名的一组 0x0F 项拼回字符串。"""
    if not entries:
        return None
    ordered = sorted(entries, key=lambda e: e[0] & 0x1F)
    chars: list[str] = []
    for entry in ordered:
        chunk = entry[1:11] + entry[14:26] + entry[28:32]
        for i in range(0, len(chunk), 2):
            unit = int.from_bytes(chunk[i : i + 2], "little")
            if unit in (0x0000, 0xFFFF):
                continue
            chars.append(chr(unit))
    return "".join(chars)


def parse_dir(raw: bytes) -> list[tuple[int, bytes, list[bytes]]]:
    """返回 (偏移, 8.3 目录项, 修饰它的长文件名项)。"""
    out: list[tuple[int, bytes, list[bytes]]] = []
    pending: list[bytes] = []
    for offset in range(0, len(raw) - 31, 32):
        entry = raw[offset : offset + 32]
        if entry[0] == 0x00:
            break
        if entry[0] == 0xE5:
            pending = []
            continue
        attr = entry[11]
        if attr == ATTR_LFN:
            pending.append(entry)
            continue
        out.append((offset, entry, pending))
        pending = []
    return out


def find_entry(raw: bytes, name: str) -> tuple[int, bytes, str]:
    """在目录里按名字找条目：先比长文件名，再比 8.3 名。"""
    wanted_long = name.lower()
    wanted_short = name.upper()
    for offset, entry, lfn_entries in parse_dir(raw):
        if entry[11] & ATTR_VOLUME:
            continue
        long_name = assemble_lfn(lfn_entries)
        if long_name and long_name.rstrip("\x00").lower() == wanted_long:
            return offset, entry, short_name_of(entry)
        if not long_name and short_name_of(entry) == wanted_short:
            return offset, entry, short_name_of(entry)
    raise SystemExit(f"模板 ESP 里找不到目录条目：{name}")


def set_dir_entry(entry: bytearray, first_cluster: int, size: int) -> None:
    struct.pack_into("<H", entry, 20, first_cluster >> 16)
    struct.pack_into("<I", entry, 28, size)
    struct.pack_into("<H", entry, 26, first_cluster & 0xFFFF)


def first_cluster(entry: bytes) -> int:
    return (struct.unpack_from("<H", entry, 20)[0] << 16) | struct.unpack_from("<H", entry, 26)[0]


def set_fat(fat: bytearray, cluster: int, value: int) -> None:
    struct.pack_into("<I", fat, cluster * 4, value & 0x0FFFFFFF)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build(template: Path, source: Path, output: Path) -> None:
    with template.open("rb") as fh:
        boot = fh.read(512)
    geom = Geometry(boot)
    fat = read_fat(template, geom)

    total_size = geom.total_sectors * geom.bytes_per_sector
    if template.stat().st_size != total_size:
        sys.exit(f"模板大小 {template.stat().st_size} 与引导扇区声明的 {total_size} 不符")

    dirs: list[tuple[list[int], bytes]] = []
    with template.open("rb") as fh:

        def load_dir(cluster: int) -> bytes:
            chain = chain_of(fat, cluster)
            if not chain:
                sys.exit(f"模板目录簇 {cluster} 的 FAT 链为空")
            raw = read_cluster_chain(fh, geom, fat, cluster)
            if len(raw) < len(chain) * geom.cluster_bytes:
                sys.exit(f"模板目录簇 {cluster} 的链不完整")
            dirs.append((chain, raw))
            return raw

        root_raw = load_dir(geom.root_cluster)
        _, efi_entry, _ = find_entry(root_raw, "EFI")
        efi_cluster = first_cluster(efi_entry)
        efi_raw = load_dir(efi_cluster)
        _, piano_entry, _ = find_entry(efi_raw, "Piano")
        piano_cluster = first_cluster(piano_entry)
        piano_raw = load_dir(piano_cluster)
        _, stable_entry, _ = find_entry(piano_raw, "stable")
        stable_cluster = first_cluster(stable_entry)
        stable_raw = bytearray(load_dir(stable_cluster))

    # 目录簇沿用模板里的编号，目录项本身也照抄，只改四个文件的大小和起始簇。
    # FAT 只准备一份，写盘时复制到每个副本（模板是 2 份，第二份漏写会让 fsck /
    # 读第二份的固件看到全零表）。
    fat_bytes = geom.fat_sectors * geom.bytes_per_sector
    new_fat = bytearray(fat_bytes)
    set_fat(new_fat, 0, 0x0FFFFFF8)
    set_fat(new_fat, 1, 0x0FFFFFFF)
    for chain, _ in dirs:
        for index, cluster in enumerate(chain):
            last = index + 1 == len(chain)
            set_fat(new_fat, cluster, 0x0FFFFFFF if last else chain[index + 1])

    # 新文件的起始簇必须落在模板实际用过的簇之后，不能写死常数：模板里的目录簇
    # 一旦不是 2..5，写死就会让文件数据覆盖目录，回读时报「找不到目录条目」。
    used_in_template = [
        c for c in range(2, geom.cluster_count + 2)
        if struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF
    ]
    next_free = (max(used_in_template) + 1) if used_in_template else 2

    payload: list[tuple[int, bytes]] = []
    for name in FILES:
        path = source / SUBDIR / name
        if not path.is_file():
            sys.exit(f"缺少输入文件：{path}")
        data = path.read_bytes()
        offset, entry, short = find_entry(bytes(stable_raw), name)
        first = next_free
        count = max(1, (len(data) + geom.cluster_bytes - 1) // geom.cluster_bytes)
        for index in range(count):
            cluster = next_free + index
            last = index == count - 1
            set_fat(new_fat, cluster, 0x0FFFFFFF if last else cluster + 1)
        next_free += count
        patched = bytearray(entry)
        set_dir_entry(patched, first, len(data))
        stable_raw[offset : offset + 32] = patched
        payload.append((first, data))
        print(f"  {name:<12} {len(data):>10d} B  {count:>6d} 簇  短名 {short}  起始簇 {first}")

    if next_free > geom.cluster_count + 2:
        sys.exit(f"内容超出 ESP 容量：需要到簇 {next_free}，只有 {geom.cluster_count + 2}")

    # stable 目录项被就地改过，写盘时要写改后的版本（dirs 里存的是读取时的字节）。
    for index, (chain, _) in enumerate(dirs):
        if chain and chain[0] == stable_cluster:
            dirs[index] = (chain, bytes(stable_raw))
            break
    else:
        sys.exit("内部错误：dirs 里没有 stable 目录")

    # 引导扇区 + 保留扇区（含 FSInfo）逐字节照抄模板。
    reserved = geom.reserved_sectors * geom.bytes_per_sector
    with template.open("rb") as fh:
        fh.seek(0)
        head = fh.read(reserved)

    used = sum(
        1 for c in range(2, geom.cluster_count + 2)
        if struct.unpack_from("<I", new_fat, c * 4)[0] & 0x0FFFFFFF
    )
    free_first = next(
        (c for c in range(2, geom.cluster_count + 2) if not struct.unpack_from("<I", new_fat, c * 4)[0] & 0x0FFFFFFF),
        0xFFFFFFFF,
    )
    fsinfo_offset = geom.fsinfo_sector * geom.bytes_per_sector
    fsinfo = bytearray(head[fsinfo_offset : fsinfo_offset + geom.bytes_per_sector])
    if struct.unpack_from("<I", fsinfo, 0)[0] == 0x41615252:
        # FSInfo 布局：lead@0，struc_sig@484，Free_Count@488，Nxt_Free@492，trail@508。
        struct.pack_into("<I", fsinfo, 488, geom.cluster_count - used)
        struct.pack_into("<I", fsinfo, 492, free_first)
    head = head[:fsinfo_offset] + bytes(fsinfo) + head[fsinfo_offset + geom.bytes_per_sector :]

    # 只写用到的簇，其余留空洞（NTFS 上是稀疏文件，不占实际磁盘）。
    with output.open("wb") as out:
        out.truncate(total_size)
        out.seek(0)
        out.write(head)
        for copy in range(geom.fat_count):
            out.seek(geom.fat_offset + copy * fat_bytes)
            out.write(bytes(new_fat))
        for chain, raw in dirs:
            for index, cluster in enumerate(chain):
                out.seek(geom.cluster_offset(cluster))
                start = index * geom.cluster_bytes
                out.write(raw[start : start + geom.cluster_bytes])
        for first, data in payload:
            out.seek(geom.cluster_offset(first))
            out.write(data)
        out.flush()


def verify(image: Path, source: Path) -> int:
    """不依赖构建时的中间状态，重新把镜像当外部输入解析一遍。"""
    with image.open("rb") as fh:
        boot = fh.read(512)
        geom = Geometry(boot)
        fat = read_fat(image, geom)
        root_raw = read_cluster_chain(fh, geom, fat, geom.root_cluster)
        _, efi_entry, _ = find_entry(root_raw, "EFI")
        efi_cluster = first_cluster(efi_entry)
        efi_raw = read_cluster_chain(fh, geom, fat, efi_cluster)
        _, piano_entry, _ = find_entry(efi_raw, "Piano")
        piano_cluster = first_cluster(piano_entry)
        piano_raw = read_cluster_chain(fh, geom, fat, piano_cluster)
        _, stable_entry, _ = find_entry(piano_raw, "stable")
        stable_cluster = first_cluster(stable_entry)
        stable_raw = read_cluster_chain(fh, geom, fat, stable_cluster)

        failures = 0
        for name in FILES:
            offset, entry, _ = find_entry(stable_raw, name)
            size = struct.unpack_from("<I", entry, 28)[0]
            first = struct.unpack_from("<H", entry, 20)[0] << 16 | struct.unpack_from("<H", entry, 26)[0]
            data = read_cluster_chain(fh, geom, fat, first)[:size]
            want = (source / SUBDIR / name).read_bytes()
            ok = data == want
            got_hash = hashlib.sha256(data).hexdigest()[:16]
            want_hash = hashlib.sha256(want).hexdigest()[:16]
            print(f"  {'OK  ' if ok else 'FAIL'} {name:<12} {size:>10d} B  {got_hash} vs {want_hash}")
            failures += 0 if ok else 1

        used = sum(1 for c in range(2, geom.cluster_count + 2) if struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF)
        print(f"  已用簇 {used} / {geom.cluster_count}（{used * geom.cluster_bytes / 2**20:.1f} MiB）")

        # 多副本 FAT 必须逐字节一致；只读第一份的自校验发现不了第二份是空的。
        copies = []
        for index in range(geom.fat_count):
            fh.seek(geom.fat_offset + index * geom.fat_sectors * geom.bytes_per_sector)
            copies.append(fh.read(geom.fat_sectors * geom.bytes_per_sector))
        if any(copy != copies[0] for copy in copies[1:]):
            print(f"  FAIL {geom.fat_count} 份 FAT 不一致")
            failures += 1
        else:
            print(f"  OK   {geom.fat_count} 份 FAT 一致")

        # FSInfo 的 Free_Count / Nxt_Free 与实际 FAT 对账。
        fh.seek(geom.fsinfo_sector * geom.bytes_per_sector)
        fsinfo = fh.read(geom.bytes_per_sector)
        free_count, nxt_free = struct.unpack_from("<2I", fsinfo, 488)
        want_free = geom.cluster_count - used
        first_free = next(
            (c for c in range(2, geom.cluster_count + 2) if not struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF),
            0xFFFFFFFF,
        )
        ok = free_count == want_free and nxt_free == first_free
        print(f"  {'OK  ' if ok else 'FAIL'} FSInfo Free_Count={free_count}（应 {want_free}）Nxt_Free={nxt_free}（应 {first_free}）")
        failures += 0 if ok else 1
        return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="生成固件可读的 FAT32 ESP 镜像")
    parser.add_argument("--template", required=True, type=Path, help="原厂 ESP 整块镜像（提供几何与目录项）")
    parser.add_argument("--source", required=True, type=Path, help="含 EFI/Piano/stable 四件套的目录")
    parser.add_argument("--output", required=True, type=Path, help="输出镜像路径")
    args = parser.parse_args()

    print(f"模板 {args.template}  输出 {args.output}")
    build(args.template, args.source, args.output)
    print("回读校验：")
    failures = verify(args.output, args.source)
    size = args.output.stat().st_size
    print(f"镜像 {size} 字节，sha256 {sha256_of(args.output)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
