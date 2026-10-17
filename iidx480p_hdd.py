#!/usr/bin/env python3
"""
iidx480p_hdd.py - apply the beatmania IIDX native 480p patch (iidx-ps2-480p by
blurtic2) to games already installed on a PS2 hard drive, with the drive
connected to a PC.  Also works on a plain .iso file.

The patch itself comes from the project's own patch480p.py: this tool imports
it, uses its list of supported games, its checks that the executable is
original, and its exact patch words. It only adds the part that reads and
writes the game on the PS2 drive (from ps2_hdd_patcher.py).

Commands
  list    DEVICE                              show installed games and their 480p status
  patch   DEVICE (--game ID | --all) [--480] [--write]
  restore DEVICE BACKUP [--write]             undo using the backup file a patch created

  default:  480p, 448 lines (as the original game)
  --480:    480p showing all 480 rendered lines
  Running `patch` again on a patched game switches it between the two variants.

Files needed:
  ps2_hdd_patcher.py (same folder)
  patch480p.py and mipsasm.py from https://github.com/blurtic2/iidx-ps2-480p
  (in vendor/iidx-ps2-480p/, or next to this script)

The 480p patch is the work of the iidx-ps2-480p contributors (MIT license).
"""

import argparse
import contextlib
import io
import os
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ps2_hdd_patcher as hdd                     # noqa: E402
from ps2_hdd_patcher import PatchError            # noqa: E402

VARIANTS = {"patches24": "480p (448 lines)", "patches480": "480p (480 lines)"}


VENDOR_DIR = os.path.join(HERE, "vendor", "iidx-ps2-480p")


def load_patch480p(folder):
    """Load patch480p.py from --patcher-dir, else vendor/iidx-ps2-480p/, else this folder."""
    candidates = [folder] if folder else [VENDOR_DIR, HERE]
    for c in candidates:
        c = os.path.abspath(c)
        if all(os.path.exists(os.path.join(c, f)) for f in ("patch480p.py", "mipsasm.py")):
            sys.path.insert(0, c)
            import patch480p
            return patch480p
    raise PatchError("patch480p.py and mipsasm.py not found in %s. Download them from "
                     "https://github.com/blurtic2/iidx-ps2-480p and put them in vendor/iidx-ps2-480p/ "
                     "(or pass --patcher-dir)." % " or ".join(os.path.abspath(c) for c in candidates))


def read_boot_elf(dev, game):
    parts = hdd.boot_path(dev, game)
    name = parts[-1].split(";")[0].upper()
    off, size = hdd.iso_find(dev, game, parts)
    return name, off, game.read(dev, off, size)


def elf_status(P, name, elf):
    """Which variant of the patch (if any) this executable currently has."""
    v = P.VERSIONS[name]
    for key, label in VARIANTS.items():
        t = bytearray(elf)
        P.apply(t, P.plan_words(v, key))
        if bytes(t) == elf:
            return label
    size, msg_va = P.ELF_INFO[name]
    if len(elf) != size:
        return "unrecognised (executable size differs)"
    words = [(va, old) for key in VARIANTS for va, old, _, _ in v.get(key, []) if old is not None]
    if all(P._word(elf, va) == old for va, old in words):
        cave = any(old is None for key in VARIANTS for _, old, _, _ in v.get(key, []))
        o = msg_va - P.ELF_BASE_VA + P.ELF_BASE_OFF
        if not cave or zlib.crc32(elf[o:o + P.MSG_BLOCK_SIZE]) == P.MSG_BLOCK_CRC32:
            return "original (480i)"
    return "modified by something else"


def verify_quiet(P, elf, name):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = P.verify(bytearray(elf), name, P.VERSIONS[name])
    return ok, [line for line in buf.getvalue().splitlines() if line.strip()]


def supported_games(P, games):
    keys = {hdd.norm_id(k): k for k in P.VERSIONS}
    return [(g, keys[hdd.norm_id(g.startup)]) for g in games if hdd.norm_id(g.startup) in keys]


def cmd_list(args):
    P = load_patch480p(args.patcher_dir)
    dev, games, kind = hdd.open_target(args.device)
    try:
        sup = dict((id(g), k) for g, k in supported_games(P, games))
        print("%-12s %-46s %s" % ("ID", "TITLE", "480p STATUS"))
        for g in games:
            if id(g) not in sup:
                if args.all_games:
                    print("%-12s %-46s %s" % (g.startup or "?", g.name[:46], "not supported"))
                continue
            try:
                name, _, elf = read_boot_elf(dev, g)
                status = elf_status(P, name, elf) if name in P.VERSIONS else "boot file %s not supported" % name
            except PatchError as e:
                status = "error: %s" % e
            print("%-12s %-46s %s" % (g.startup, P.VERSIONS[sup[id(g)]]["title"][:46], status))
        if not sup:
            print("(no supported beatmania IIDX games found%s)"
                  % ("" if args.all_games else "; use --all-games to list everything"))
    finally:
        dev.close()


def plan_game(P, dev, game, plan_key):
    name, elf_off, elf = read_boot_elf(dev, game)
    if name not in P.VERSIONS:
        raise PatchError("boot file %s is not a supported IIDX version" % name)
    v = P.VERSIONS[name]
    ok, msgs = verify_quiet(P, elf, name)
    if not ok:
        raise PatchError("the executable does not match the expected original or patched %s; "
                         "nothing will be written.\n    " % v["title"] + "\n    ".join(msgs))
    before = elf_status(P, name, elf)
    target = bytearray(elf)
    P.apply(target, P.plan_words(v, plan_key))
    regions = hdd.file_changes_to_regions(dev, game, elf_off, elf, bytes(target))
    words = sum(1 for i in range(0, len(elf), 4) if elf[i:i + 4] != target[i:i + 4])
    return {"name": name, "title": v["title"], "before": before, "regions": regions, "words": words}


def cmd_patch(args):
    P = load_patch480p(args.patcher_dir)
    plan_key = "patches480" if args.lines480 else "patches24"
    dev, games, kind = hdd.open_target(args.device)
    plans = []
    try:
        if kind == "iso":
            chosen = games
        elif args.all:
            chosen = [g for g, _ in supported_games(P, games)]
            if not chosen:
                raise PatchError("No supported beatmania IIDX games on this drive.")
        else:
            chosen = [hdd.pick_game(games, kind, args.game)]
        for g in chosen:
            try:
                plans.append((g, plan_game(P, dev, g, plan_key)))
            except PatchError as e:
                print("%s (%s): skipped - %s" % (g.name, g.startup, e))
    finally:
        dev.close()

    todo = []
    for g, p in plans:
        if not p["regions"]:
            print("%s: already %s, nothing to do." % (p["title"], VARIANTS[plan_key]))
            continue
        print("%s (%s)" % (p["title"], p["name"]))
        print("    now: %s  ->  %s" % (p["before"], VARIANTS[plan_key]))
        print("    %d word(s) in the executable, %d disk sector(s)" % (p["words"], len(p["regions"])))
        todo.append((g, p))
    if not todo:
        return 0 if plans else 1
    if not args.write:
        print("\nDry run only. Add --write to apply.")
        return 0
    if not hdd.confirm("\nAbout to patch %d game(s) on %s." % (len(todo), args.device), args.yes):
        print("Cancelled.")
        return 1
    rc = 0
    for g, p in todo:
        print("\n%s:" % p["title"])
        backup = args.backup if (args.backup and len(todo) == 1) else None
        if args.backup and len(todo) > 1:
            root, ext = os.path.splitext(args.backup)
            backup = "%s_%s%s" % (root, p["name"], ext or ".ps2bak")
        ns = argparse.Namespace(device=args.device, yes=True, backup=backup)
        try:
            rc |= hdd.apply_regions(ns, g, p["regions"],
                                    {"kind": "iidx480p", "variant": plan_key, "previous": p["before"]})
        except PatchError as e:
            print("Error: %s" % e)
            rc = 1
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply the beatmania IIDX native 480p patch to games "
                                             "installed on a PS2 HDD (or to an ISO).")
    ap.add_argument("--patcher-dir", help="folder containing patch480p.py and mipsasm.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show installed IIDX games and their 480p status")
    p.add_argument("device")
    p.add_argument("--all-games", action="store_true", help="also list unsupported games")

    p = sub.add_parser("patch", help="apply or switch the 480p patch")
    p.add_argument("device")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--game", help="game ID (e.g. SLPM_551.17), partition name or part of the title")
    g.add_argument("--all", action="store_true", help="every supported IIDX game on the drive")
    p.add_argument("--480", dest="lines480", action="store_true", help="show all 480 lines")
    p.add_argument("--backup", help="backup file name (default: backup_<ID>_<time>.ps2bak)")
    p.add_argument("--write", action="store_true", help="actually write (default is a dry run)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    p = sub.add_parser("restore", help="undo a patch using its backup file")
    p.add_argument("device")
    p.add_argument("backup")
    p.add_argument("--write", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "list":
            return cmd_list(args) or 0
        if args.cmd == "patch":
            return cmd_patch(args)
        return hdd.cmd_restore(args) or 0
    except PatchError as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
