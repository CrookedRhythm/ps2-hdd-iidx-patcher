#!/usr/bin/env python3
"""
ps2_hdd_patcher.py - apply PCSX2 .pnach patches or xdelta patches to PS2 games
that are already installed on a PS2 hard drive (HDLoader / OPL format), with the
drive connected to a PC.  Also works on a plain .iso file.

Nothing is written unless you pass --write.  Every write saves a backup file
first, and `restore` puts the original bytes back.

Commands
  drives                                  find connected drives that look like PS2 HDDs
  list    DEVICE                          list installed games
  pnach   DEVICE PNACH --game ID [--group NAME] [--write]
  xdelta  DEVICE PATCH --game ID [--write] [--work-dir DIR] [--xdelta-exe PATH]
  restore DEVICE BACKUP [--write]

DEVICE is a whole drive (Windows: \\\\.\\PhysicalDrive2, Linux: /dev/sdb,
macOS: /dev/rdisk3), a raw image of a PS2 drive, or a plain .iso file.

How it works
  OPL stores a game as an APA partition (type 0x1337) plus sub-partitions.  The
  HDLoader header at partition start + 0x808 sectors holds a slice table
  (ISO sector offset, disk LBA, size in bytes) that OPL itself uses to map disc
  sectors to the drive.  This tool reads the same table, finds the boot ELF
  through SYSTEM.CNF, converts each pnach memory address to a position inside
  the ELF through its program headers, and writes the new bytes to the matching
  disk sector.  Partition headers are never modified.

Requires Python 3.8+ and admin/root rights for raw drive access.  xdelta mode
also needs the xdelta3 program.
"""

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

VERSION = "1.0"

HDD_SECTOR = 512
CD_SECTOR = 2048
ALIGN = 4096                      # raw device I/O granularity (multiple of 512 and 4Kn)

APA_MAGIC = 0x00415041            # 'APA\0'
APA_MBR_MAGIC = b"Sony Computer Entertainment Inc."
APA_TYPE_HDL = 0x1337
APA_FLAG_SUB = 0x0001
HDL_HEADER_SECTOR = 0x808         # (0x100000 + 4096) / 512, as in OPL src/hdd.c
HDL_MAGIC = 0xDEADFEED
HDL_NUM_PART_SPECS = 65


class PatchError(Exception):
    pass


# --------------------------------------------------------------------------
# Raw block access (aligned, so it works on Windows / macOS raw devices)
# --------------------------------------------------------------------------

class RawDevice:
    def __init__(self, path, writable=False):
        self.path = path
        self.writable = writable
        mode = "r+b" if writable else "rb"
        try:
            self.f = open(path, mode, buffering=0)
        except PermissionError:
            raise PatchError(
                "Permission denied opening %s. Run as Administrator (Windows) "
                "or with sudo (Linux/macOS)." % path)
        except OSError as e:
            raise PatchError("Cannot open %s: %s" % (path, e))

    def close(self):
        self.f.close()

    def _read_aligned(self, start, length):
        self.f.seek(start)
        data = b""
        while len(data) < length:
            chunk = self.f.read(length - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def read(self, offset, length):
        start = offset - (offset % ALIGN)
        end = offset + length
        end_al = end + (-end % ALIGN)
        data = self._read_aligned(start, end_al - start)
        part = data[offset - start: offset - start + length]
        if len(part) != length:
            raise PatchError("Short read at byte offset %d (wanted %d bytes, got %d)"
                             % (offset, length, len(part)))
        return part

    def write(self, offset, payload):
        if not self.writable:
            raise PatchError("Device not opened for writing")
        start = offset - (offset % ALIGN)
        end = offset + len(payload)
        end_al = end + (-end % ALIGN)
        block = bytearray(self._read_aligned(start, end_al - start))
        if len(block) < end - start:
            raise PatchError("Write past end of device at offset %d" % offset)
        block[offset - start: offset - start + len(payload)] = payload
        self.f.seek(start)
        written = self.f.write(bytes(block))
        if written != len(block):
            raise PatchError("Short write at offset %d" % start)
        self.f.flush()
        try:
            os.fsync(self.f.fileno())
        except OSError:
            pass


# --------------------------------------------------------------------------
# APA partition table
# --------------------------------------------------------------------------

def apa_checksum(hdr):
    return sum(struct.unpack_from("<255I", hdr, 4)) & 0xFFFFFFFF


def parse_apa_header(hdr):
    (checksum, magic, nxt, prev) = struct.unpack_from("<4I", hdr, 0)
    pid = hdr[0x10:0x30].split(b"\0")[0].decode("ascii", "replace")
    start, length = struct.unpack_from("<2I", hdr, 0x40)
    ptype, flags = struct.unpack_from("<2H", hdr, 0x48)
    nsub, = struct.unpack_from("<I", hdr, 0x4C)
    main, number = struct.unpack_from("<2I", hdr, 0x58)
    subs = [struct.unpack_from("<2I", hdr, 0x200 + 8 * i) for i in range(min(nsub, 64))]
    return {
        "checksum": checksum, "checksum_ok": checksum == apa_checksum(hdr),
        "magic": magic, "next": nxt, "prev": prev, "id": pid,
        "start": start, "length": length, "type": ptype, "flags": flags,
        "nsub": nsub, "main": main, "number": number, "subs": subs,
    }


def is_apa_disk(dev):
    try:
        hdr = dev.read(0, 1024)
    except PatchError:
        return False
    magic, = struct.unpack_from("<I", hdr, 4)
    return magic == APA_MAGIC and hdr[0x100:0x120] == APA_MBR_MAGIC


def walk_apa(dev):
    parts = []
    seen = set()
    lba = 0
    while True:
        if lba in seen:
            break
        seen.add(lba)
        hdr = dev.read(lba * HDD_SECTOR, 1024)
        p = parse_apa_header(hdr)
        if p["magic"] != APA_MAGIC:
            raise PatchError("APA chain broken at LBA %d (no APA magic)" % lba)
        if p["start"] != lba:
            raise PatchError("APA header at LBA %d says it starts at %d" % (lba, p["start"]))
        parts.append(p)
        if p["next"] == 0 or len(parts) > 100000:
            break
        lba = p["next"]
    return parts


# --------------------------------------------------------------------------
# Games and sector mapping
# --------------------------------------------------------------------------

class Game:
    """A disc image: maps ISO byte offsets to device byte offsets."""

    def __init__(self, name, startup, part_id, specs, disc_type=None):
        self.name = name
        self.startup = startup
        self.part_id = part_id
        self.specs = specs          # list of (iso_lsn_start, device_byte_start, size_bytes)
        self.disc_type = disc_type
        self.size = max((s[0] * CD_SECTOR + s[2] for s in specs), default=0)

    def map(self, iso_off):
        for lsn0, dev0, size in self.specs:
            base = lsn0 * CD_SECTOR
            if base <= iso_off < base + size:
                return dev0 + (iso_off - base), base + size - iso_off
        raise PatchError("ISO offset 0x%X is outside the installed image" % iso_off)

    def runs(self, iso_off, length):
        """Split an ISO byte range into (device_offset, iso_offset, length) runs."""
        out = []
        while length > 0:
            dev, avail = self.map(iso_off)
            n = min(length, avail)
            out.append((dev, iso_off, n))
            iso_off += n
            length -= n
        return out

    def read(self, dev, iso_off, length):
        return b"".join(dev.read(d, n) for d, _, n in self.runs(iso_off, length))


def hdl_game_from_partition(dev, part):
    hdr = dev.read((part["start"] + HDL_HEADER_SECTOR) * HDD_SECTOR, 1024)
    magic, = struct.unpack_from("<I", hdr, 0)
    if magic != HDL_MAGIC:
        return None
    name = hdr[0x08:0xA8].split(b"\0")[0].decode("ascii", "replace")
    startup = hdr[0xAC:0xE8].split(b"\0")[0].decode("ascii", "replace")
    layer1, disc_type, nparts = struct.unpack_from("<IIi", hdr, 0xE8)
    if not 0 < nparts <= HDL_NUM_PART_SPECS:
        raise PatchError("Game '%s': implausible slice count %d" % (name, nparts))
    specs = []
    expect = 0
    for i in range(nparts):
        off, start, size = struct.unpack_from("<3I", hdr, 0xF4 + 12 * i)
        if off != expect or size % CD_SECTOR:
            raise PatchError("Game '%s': slice table is not in the expected format "
                             "(slice %d: offset %d, expected %d, size %d)"
                             % (name, i, off, expect, size))
        specs.append((off, start * HDD_SECTOR, size))
        expect = off + size // CD_SECTOR
    return Game(name, startup, part["id"], specs, disc_type)


def list_hdl_games(dev):
    games = []
    for p in walk_apa(dev):
        if p["type"] == APA_TYPE_HDL and not (p["flags"] & APA_FLAG_SUB):
            g = hdl_game_from_partition(dev, p)
            if g:
                g.apa_checksum_ok = p["checksum_ok"]
                games.append(g)
    return games


def norm_id(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def open_target(path, writable=False):
    """Returns (device, games, kind) where kind is 'apa' or 'iso'."""
    dev = RawDevice(path, writable)
    if is_apa_disk(dev):
        return dev, list_hdl_games(dev), "apa"
    try:
        pvd = dev.read(16 * CD_SECTOR, CD_SECTOR)
    except PatchError:
        pvd = b""
    if pvd[1:6] == b"CD001":
        size = os.path.getsize(path)
        g = Game(os.path.basename(path), "", None, [(0, 0, size - size % CD_SECTOR)])
        try:
            g.startup = boot_path(dev, g)[-1].split(";")[0]
        except PatchError:
            pass
        return dev, [g], "iso"
    dev.close()
    raise PatchError("%s is neither an APA-formatted PS2 drive nor an ISO image" % path)


def pick_game(games, kind, query):
    if kind == "iso":
        return games[0]
    if not query:
        raise PatchError("Use --game to choose a game. Run `list` to see them.")
    q = norm_id(query)
    exact = [g for g in games if q and (norm_id(g.startup) == q or norm_id(g.part_id) == q)]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [g for g in games if q and (q in norm_id(g.name) or q in norm_id(g.part_id))]
    if len(fuzzy) == 1:
        return fuzzy[0]
    cands = exact or fuzzy
    if not cands:
        raise PatchError("No installed game matches '%s'. Run `list`." % query)
    raise PatchError("'%s' matches several games: %s"
                     % (query, ", ".join("%s (%s)" % (g.name, g.startup) for g in cands)))


# --------------------------------------------------------------------------
# ISO9660 and ELF
# --------------------------------------------------------------------------

def iso_find(dev, game, path_parts):
    pvd = game.read(dev, 16 * CD_SECTOR, CD_SECTOR)
    if pvd[1:6] != b"CD001":
        raise PatchError("No ISO9660 volume found in '%s' (sector mapping may be wrong)" % game.name)
    rec = pvd[156:190]
    extent, size = struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0]
    is_dir = True
    for i, want in enumerate(path_parts):
        if not is_dir:
            raise PatchError("Path component is not a directory")
        want = want.upper().split(";")[0]
        data = game.read(dev, extent * CD_SECTOR, size)
        found = None
        pos = 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = (pos // CD_SECTOR + 1) * CD_SECTOR
                continue
            r = data[pos:pos + ln]
            nl = r[32]
            name = r[33:33 + nl].decode("ascii", "replace").upper().split(";")[0]
            if name.endswith(".") and "." not in name[:-1]:
                name = name[:-1]
            if name == want:
                found = (struct.unpack_from("<I", r, 2)[0], struct.unpack_from("<I", r, 10)[0],
                         bool(r[25] & 2))
                break
            pos += ln
        if not found:
            raise PatchError("'%s' not found on the disc" % "\\".join(path_parts[:i + 1]))
        extent, size, is_dir = found
    return extent * CD_SECTOR, size


def boot_path(dev, game):
    off, size = iso_find(dev, game, ["SYSTEM.CNF"])
    text = game.read(dev, off, min(size, 4096)).decode("ascii", "replace")
    m = re.search(r"BOOT2\s*=\s*cdrom0?:\\?([^\r\n]+)", text, re.I)
    if not m:
        raise PatchError("SYSTEM.CNF has no BOOT2 line:\n" + text)
    return [p for p in m.group(1).strip().split("\\") if p]


def elf_segments(elf):
    if elf[:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 1:
        raise PatchError("Boot file is not a 32-bit little-endian ELF")
    phoff, = struct.unpack_from("<I", elf, 0x1C)
    phentsize, phnum = struct.unpack_from("<HH", elf, 0x2A)
    segs = []
    for i in range(phnum):
        ptype, poff, vaddr, paddr, filesz, memsz = struct.unpack_from("<6I", elf, phoff + i * phentsize)
        if ptype == 1:
            segs.append((vaddr, poff, filesz, memsz))
    return segs


def vaddr_to_file(segs, addr, length):
    a = addr & 0x1FFFFFFF
    for vaddr, poff, filesz, memsz in segs:
        v = vaddr & 0x1FFFFFFF
        if v <= a and a + length <= v + filesz:
            return poff + (a - v)
        if v <= a < v + memsz:
            raise PatchError("Address %08X is in zero-filled memory (bss), not stored in the ELF"
                             % addr)
    raise PatchError("Address %08X is not part of the boot ELF. The patch may target code "
                     "loaded later from another file, which this tool cannot patch." % addr)


def pcsx2_crc(elf):
    n = len(elf) // 4
    crc = 0
    for w in struct.unpack_from("<%dI" % n, elf):
        crc ^= w
    return crc


# --------------------------------------------------------------------------
# pnach
# --------------------------------------------------------------------------

PNACH_SIZES = {"byte": 1, "short": 2, "word": 4, "double": 8}


def parse_pnach(text):
    groups = {"": []}
    order = [""]
    current = ""
    skipped = []
    for raw in text.splitlines():
        line = raw.split("//")[0].strip()
        if not line:
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            current = m.group(1).strip()
            if current not in groups:
                groups[current] = []
                order.append(current)
            continue
        m = re.match(r"^patch\s*=\s*(.+)$", line, re.I)
        if not m:
            continue
        f = [x.strip() for x in m.group(1).split(",")]
        if len(f) < 5:
            skipped.append((raw.strip(), "not enough fields"))
            continue
        cpu, typ = f[1].upper(), f[3].lower()
        if cpu != "EE":
            skipped.append((raw.strip(), "%s patches are not supported" % cpu))
            continue
        if typ not in PNACH_SIZES:
            skipped.append((raw.strip(), "patch type '%s' is not supported" % typ))
            continue
        addr = int(f[2], 16)
        size = PNACH_SIZES[typ]
        value = int(f[4], 16) & ((1 << (8 * size)) - 1)
        groups[current].append((addr, size, value, raw.strip()))
    return groups, order, skipped


def select_patches(groups, order, wanted):
    named = [g for g in order if g]
    patches = list(groups.get("", []))
    if wanted:
        for w in wanted:
            match = [g for g in named if g.lower() == w.lower()]
            if not match:
                raise PatchError("No group named [%s]. Groups in this file: %s"
                                 % (w, ", ".join("[%s]" % g for g in named)))
            patches += groups[match[0]]
    elif named:
        if len(named) == 1:
            patches += groups[named[0]]
        else:
            raise PatchError("This pnach has several groups; choose one with --group:\n  "
                             + "\n  ".join("[%s] (%d patches)" % (g, len(groups[g])) for g in named))
    if not patches:
        raise PatchError("No usable patches selected")
    return patches


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------

def save_backup(path, meta, regions, dev):
    """regions: list of (device_offset, original_bytes, new_bytes)."""
    blob = io.BytesIO()
    entries = []
    for off, old, new in regions:
        entries.append({"offset": off, "length": len(old), "data_at": blob.tell(),
                        "patched_sha256": hashlib.sha256(new).hexdigest()})
        blob.write(old)
    meta = dict(meta)
    meta["entries"] = entries
    meta["tool_version"] = VERSION
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(meta, indent=1))
        z.writestr("original.bin", blob.getvalue())


def default_backup_name(tag):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return "backup_%s_%s.ps2bak" % (re.sub(r"[^A-Za-z0-9._-]", "_", tag or "game"), stamp)


def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    try:
        return input(prompt + " Type YES to continue: ").strip() == "YES"
    except EOFError:
        return False


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_drives(args):
    if os.name == "nt":
        cands = [r"\\.\PhysicalDrive%d" % i for i in range(32)]
    elif sys.platform == "darwin":
        cands = ["/dev/rdisk%d" % i for i in range(32)]
    else:
        cands = sorted(set(["/dev/sd" + c for c in "abcdefghijklmnop"] +
                           ["/dev/nvme%dn1" % i for i in range(4)] +
                           ["/dev/nbd%d" % i for i in range(4)]))
    found = 0
    denied = 0
    for c in cands:
        if os.name != "nt" and not os.path.exists(c):
            continue
        try:
            dev = RawDevice(c)
        except PatchError as e:
            if "Permission" in str(e):
                denied += 1
            continue
        try:
            if is_apa_disk(dev):
                games = list_hdl_games(dev)
                print("%-22s PS2 APA drive, %d installed game(s)" % (c, len(games)))
                found += 1
        except PatchError as e:
            print("%-22s APA drive, but: %s" % (c, e))
        finally:
            dev.close()
    if not found:
        print("No PS2-formatted drive found.")
        if denied:
            print("Some drives could not be opened: run as Administrator / with sudo.")


def cmd_list(args):
    dev, games, kind = open_target(args.device)
    try:
        if kind == "iso":
            print("Plain ISO image. Boot file: %s" % (games[0].startup or "?"))
            return
        print("%-12s %-8s %-32s %s" % ("ID", "SIZE", "PARTITION", "TITLE"))
        for g in games:
            print("%-12s %5dMB  %-32s %s%s" % (g.startup, g.size // (1024 * 1024), g.part_id, g.name,
                                              "" if g.apa_checksum_ok else "  [header checksum mismatch]"))
        print("%d game(s)." % len(games))
    finally:
        dev.close()


def file_changes_to_regions(dev, game, file_iso_off, old_data, new_data):
    """Turn the differences between two versions of a file on the disc into device regions
    (offset, original sector bytes, new sector bytes), one per changed 512-byte sector."""
    if len(old_data) != len(new_data):
        raise PatchError("Patched file has a different size; cannot write in place")
    sectors = {}
    for i in range(len(old_data)):
        if old_data[i] != new_data[i]:
            dev_off, _ = game.map(file_iso_off + i)
            sectors.setdefault(dev_off - dev_off % HDD_SECTOR, []).append((dev_off, new_data[i]))
    regions = []
    for sec in sorted(sectors):
        old = dev.read(sec, HDD_SECTOR)
        new = bytearray(old)
        for dev_off, b in sectors[sec]:
            new[dev_off - sec] = b
        regions.append((sec, old, bytes(new)))
    return regions


def plan_pnach(dev, game, pnach_path, groups_wanted, quiet=False):
    with open(pnach_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    groups, order, skipped = parse_pnach(text)
    patches = select_patches(groups, order, groups_wanted)

    parts = boot_path(dev, game)
    elf_off, elf_size = iso_find(dev, game, parts)
    elf = game.read(dev, elf_off, elf_size)
    segs = elf_segments(elf)

    crc = pcsx2_crc(elf)
    m = re.search(r"_([0-9A-Fa-f]{8})\.pnach$", os.path.basename(pnach_path))
    crc_note = None
    if m and int(m.group(1), 16) != crc:
        crc_note = ("CRC mismatch: the pnach is for ELF CRC %s, the installed boot file has "
                    "CRC %08X. It may be a different version or region, or already modified."
                    % (m.group(1).upper(), crc))

    new_elf = bytearray(elf)
    rows = []
    for addr, size, value, line in patches:
        foff = vaddr_to_file(segs, addr, size)
        new_bytes = value.to_bytes(size, "little")
        rows.append((addr, size, foff, bytes(elf[foff:foff + size]), new_bytes))
        new_elf[foff:foff + size] = new_bytes

    regions = file_changes_to_regions(dev, game, elf_off, elf, new_elf)

    info = {"boot": "\\".join(parts), "elf_iso_offset": elf_off, "elf_size": elf_size,
            "crc": crc, "crc_note": crc_note, "rows": rows, "skipped": skipped,
            "patch_count": len(patches)}
    return info, regions


def cmd_pnach(args):
    dev, games, kind = open_target(args.device)
    try:
        game = pick_game(games, kind, args.game)
        info, regions = plan_pnach(dev, game, args.pnach, args.group)
    finally:
        dev.close()

    print("Game:       %s (%s)" % (game.name, game.startup or "?"))
    if game.part_id:
        print("Partition:  %s" % game.part_id)
    print("Boot file:  %s  (%d bytes, ISO offset 0x%X, PCSX2 CRC %08X)"
          % (info["boot"], info["elf_size"], info["elf_iso_offset"], info["crc"]))
    for line, why in info["skipped"]:
        print("  skipped:  %s  (%s)" % (line, why))
    print()
    print("  %-8s %-10s %-18s %-18s %s" % ("ADDRESS", "ELF OFF", "CURRENT", "NEW", ""))
    already = 0
    for addr, size, foff, old, new in info["rows"]:
        state = "already applied" if old == new else ""
        already += old == new
        print("  %08X 0x%08X %-18s %-18s %s" % (addr, foff, old[::-1].hex().upper(),
                                                new[::-1].hex().upper(), state))
    print()
    if not regions:
        print("Nothing to do: every patch is already applied.")
        return 0
    if info["crc_note"]:
        print("WARNING: " + info["crc_note"])
        if already:
            print("Some patches are already applied, so this game may have been patched before.")
        if not args.force:
            print("Refusing to continue. Use --force if you are sure this patch fits this game.")
            return 2
    print("%d patch line(s), %d disk sector(s) to change." % (info["patch_count"], len(regions)))
    if not args.write:
        print("Dry run only. Add --write to apply.")
        return 0
    return apply_regions(args, game, regions, {"kind": "pnach", "pnach": os.path.basename(args.pnach),
                                               "groups": args.group or []})


def apply_regions(args, game, regions, extra_meta):
    if not confirm("About to write %d region(s) to %s." % (len(regions), args.device), args.yes):
        print("Cancelled.")
        return 1
    backup = args.backup or default_backup_name(game.startup or game.name)
    meta = {"device": args.device, "game": game.name, "startup": game.startup,
            "partition": game.part_id, "created": datetime.datetime.now().isoformat()}
    meta.update(extra_meta)

    dev = RawDevice(args.device, writable=True)
    try:
        # Re-check that nothing changed since planning.
        for off, old, new in regions:
            if dev.read(off, len(old)) != old:
                raise PatchError("Data at offset %d changed since it was read. Aborting." % off)
        save_backup(backup, meta, regions, dev)
        print("Backup saved: %s" % os.path.abspath(backup))
        for off, old, new in regions:
            dev.write(off, new)
        bad = [off for off, old, new in regions if dev.read(off, len(new)) != new]
    finally:
        dev.close()
    if bad:
        raise PatchError("Verification failed at %d region(s). Restore with: restore %s %s"
                         % (len(bad), args.device, backup))
    print("Done. All changes written and verified.")
    print("To undo:  python %s restore \"%s\" \"%s\" --write"
          % (os.path.basename(sys.argv[0]), args.device, backup))
    return 0


def find_xdelta(explicit):
    if explicit:
        return explicit
    for name in ("xdelta3", "xdelta3.exe", "xdelta", "xdelta.exe"):
        p = shutil.which(name)
        if p:
            return p
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    for name in ("xdelta3.exe", "xdelta.exe", "xdelta3"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    raise PatchError("xdelta3 not found. Install it, put xdelta3.exe next to this script, "
                     "or pass --xdelta-exe PATH.")


def cmd_xdelta(args):
    exe = find_xdelta(args.xdelta_exe)
    dev, games, kind = open_target(args.device)
    work = args.work_dir or tempfile.mkdtemp(prefix="ps2patch_")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "original.iso")
    out = os.path.join(work, "patched.iso")
    try:
        game = pick_game(games, kind, args.game)
        need = game.size * 2 + (1 << 26)
        free = shutil.disk_usage(work).free
        if free < need:
            raise PatchError("Need about %d MB free in %s, only %d MB available. Use --work-dir."
                             % (need >> 20, work, free >> 20))
        print("Game: %s (%s), %d MB" % (game.name, game.startup or "?", game.size >> 20))
        print("Reading the installed image to %s ..." % src)
        with open(src, "wb") as fo:
            pos = 0
            step = 8 << 20
            while pos < game.size:
                n = min(step, game.size - pos)
                fo.write(game.read(dev, pos, n))
                pos += n
                print("\r  %3d%%" % (pos * 100 // game.size), end="", flush=True)
        print()
    finally:
        dev.close()

    print("Applying %s with %s ..." % (os.path.basename(args.patch), os.path.basename(exe)))
    r = subprocess.run([exe, "-d", "-f", "-s", src, args.patch, out],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise PatchError("xdelta3 failed (this patch was probably made for a different ISO):\n"
                         + r.stdout.decode("utf-8", "replace"))
    new_size = os.path.getsize(out)
    if new_size > game.size:
        raise PatchError("The patched image is %d bytes larger than the installed one. It cannot "
                         "be written in place; reinstall the patched ISO (kept at %s) instead."
                         % (new_size - game.size, out))

    # Compare in 2048-byte sectors and collect changed runs.
    regions = []
    with open(src, "rb") as fa, open(out, "rb") as fb:
        pos = 0
        run_start = None
        run_old = bytearray()
        run_new = bytearray()
        while pos < new_size:
            a = fa.read(CD_SECTOR)
            b = fb.read(CD_SECTOR)
            if len(b) < len(a):
                a = a[:len(b)]
            if a != b:
                if run_start is None:
                    run_start = pos
                run_old += a
                run_new += b
            elif run_start is not None:
                regions.append((run_start, bytes(run_old), bytes(run_new)))
                run_start, run_old, run_new = None, bytearray(), bytearray()
            pos += len(b)
        if run_start is not None:
            regions.append((run_start, bytes(run_old), bytes(run_new)))

    # Translate ISO ranges to device ranges.
    dev_regions = []
    for iso_off, old, new in regions:
        for d, io_, n in game.runs(iso_off, len(old)):
            k = io_ - iso_off
            dev_regions.append((d, old[k:k + n], new[k:k + n]))
    changed = sum(len(o) for _, o, _ in dev_regions)
    print("%d changed region(s), %d KB in total." % (len(dev_regions), changed >> 10))
    if new_size < game.size:
        print("Note: the patched image is smaller than the installed one; the extra tail is left as is.")
    if not dev_regions:
        print("Nothing to do: the installed game already matches the patched image.")
        return 0
    if not args.write:
        print("Dry run only. Add --write to apply. Work files are in %s" % work)
        return 0
    rc = apply_regions(args, game, dev_regions, {"kind": "xdelta", "patch": os.path.basename(args.patch)})
    if rc == 0 and not args.keep_work:
        for p in (src, out):
            try:
                os.remove(p)
            except OSError:
                pass
    return rc


def cmd_restore(args):
    with zipfile.ZipFile(args.backup) as z:
        meta = json.loads(z.read("manifest.json"))
        blob = z.read("original.bin")
    entries = meta["entries"]
    print("Backup of: %s (%s), made %s, %d region(s)"
          % (meta.get("game"), meta.get("startup"), meta.get("created"), len(entries)))
    dev = RawDevice(args.device, writable=args.write)
    try:
        if not is_apa_disk(dev) and meta.get("partition"):
            raise PatchError("%s is not an APA drive, but this backup came from one." % args.device)
        mismatch = 0
        for e in entries:
            cur = dev.read(e["offset"], e["length"])
            if hashlib.sha256(cur).hexdigest() != e["patched_sha256"]:
                mismatch += 1
        if mismatch:
            print("WARNING: %d region(s) no longer contain the patched data (changed since, "
                  "or wrong drive)." % mismatch)
            if not args.force:
                print("Refusing to restore. Use --force to restore anyway.")
                return 2
        if not args.write:
            print("Dry run only. Add --write to restore.")
            return 0
        if not confirm("About to restore %d region(s) on %s." % (len(entries), args.device), args.yes):
            print("Cancelled.")
            return 1
        for e in entries:
            data = blob[e["data_at"]:e["data_at"] + e["length"]]
            dev.write(e["offset"], data)
        bad = sum(1 for e in entries
                  if dev.read(e["offset"], e["length"]) != blob[e["data_at"]:e["data_at"] + e["length"]])
    finally:
        dev.close()
    if bad:
        raise PatchError("Verification failed for %d region(s)." % bad)
    print("Restored and verified.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Patch games installed on a PS2 HDD (OPL/HDLoader) from a PC.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("drives", help="find connected PS2-formatted drives")

    p = sub.add_parser("list", help="list installed games")
    p.add_argument("device")

    def common_write(p):
        p.add_argument("--write", action="store_true", help="actually write (default is a dry run)")
        p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
        p.add_argument("--force", action="store_true", help="continue despite CRC/verification warnings")

    p = sub.add_parser("pnach", help="apply a PCSX2 .pnach patch")
    p.add_argument("device")
    p.add_argument("pnach")
    p.add_argument("--game", help="game ID (e.g. SLPM_552.21), partition name or part of the title")
    p.add_argument("--group", action="append", help="[group] from the pnach to apply (repeatable)")
    p.add_argument("--backup", help="backup file to create (default: backup_<ID>_<time>.ps2bak)")
    common_write(p)

    p = sub.add_parser("xdelta", help="apply an xdelta patch made for the game's ISO")
    p.add_argument("device")
    p.add_argument("patch")
    p.add_argument("--game")
    p.add_argument("--backup")
    p.add_argument("--work-dir", help="folder for temporary ISO copies (needs ~2x game size)")
    p.add_argument("--keep-work", action="store_true", help="keep the temporary ISO copies")
    p.add_argument("--xdelta-exe", help="path to xdelta3")
    common_write(p)

    p = sub.add_parser("restore", help="undo a patch using its backup file")
    p.add_argument("device")
    p.add_argument("backup")
    common_write(p)

    args = ap.parse_args(argv)
    try:
        return {"drives": cmd_drives, "list": cmd_list, "pnach": cmd_pnach,
                "xdelta": cmd_xdelta, "restore": cmd_restore}[args.cmd](args) or 0
    except PatchError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
