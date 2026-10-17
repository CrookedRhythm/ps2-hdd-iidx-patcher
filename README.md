# DISCLAIMER

This tool was made with the help of AI. Use it at your own risk. 
I have tested and verified it works.  However, I am not responsible for any damae or dataloss this tool caused by using this tool.

# PS2 HDD Game Patcher

Patch PS2 games that are **already installed on a PS2 hard drive** (OPL / HDLoader format) straight from a PC. No extracting, re-injecting or reinstalling: the tool finds the game on the drive, changes only the bytes the patch changes, and keeps a backup so every change can be undone.

It includes:

- **`iidx480p_hdd.py`**: applies the [iidx-ps2-480p](https://github.com/blurtic2/iidx-ps2-480p) native 480p patch for beatmania IIDX 9th–16 to installed games.
- **`ps2_hdd_patcher.py`**: a general tool for applying PCSX2 `.pnach` code patches or xdelta patches to installed games.

Both also work on a plain `.iso` file.

## Credits

The beatmania IIDX 480p patch, every patch value in it, the list of supported games, and the checks for the original executables come from **[iidx-ps2-480p](https://github.com/blurtic2/iidx-ps2-480p)** by the iidx-ps2-480p contributors, released under the MIT license. Its `patch480p.py` and `mipsasm.py` are included unchanged in [`vendor/iidx-ps2-480p/`](vendor/iidx-ps2-480p/) with their license. See that project for screenshots, the comparison video and technical details.

This repository only adds the part that reads and writes games on a PS2 drive. Please report problems with the 480p patch itself to the original project, and problems with drive access here.

The PS2 drive layout follows [Open PS2 Loader](https://github.com/ps2homebrew/Open-PS2-Loader) and [hdl-dump](https://github.com/ps2homebrew/hdl-dump), which were used as references. No code from them is included.

## Requirements

- Python 3.8 or newer.
- A way to connect the PS2 drive to your PC (USB-to-IDE/SATA adapter).
- Administrator rights (Windows) or `sudo` (Linux/macOS) for raw drive access.
- For xdelta patches only: `xdelta3`.

## beatmania IIDX 480p (Windows example)

Open **Command Prompt or PowerShell as Administrator** in the repository folder.

```
python ps2_hdd_patcher.py drives
python iidx480p_hdd.py list \\.\PhysicalDrive3
python iidx480p_hdd.py patch \\.\PhysicalDrive3 --all
python iidx480p_hdd.py patch \\.\PhysicalDrive3 --all --write
```

- `drives` finds the PS2-formatted drive. Use the path it prints.
- `list` shows each supported IIDX game and whether it is original, 480p (448 lines) or 480p (480 lines).
- `patch` without `--write` only previews.
- `--480` shows all 480 lines instead of 448. Running `patch` again switches a game between the two variants.
- `--game SLPM_551.17` patches one game instead of all of them.

On Linux use `/dev/sdX`, on macOS `/dev/rdiskN`, with `sudo`.

On the PS2: use a component cable or HDMI adapter and a 480p-capable display, and turn off OPL's GSM video mode forcing for these games.

Supported: 9th style, 10th, 11 IIDX RED, 12 HAPPY SKY, 13 DistorteD, 14 GOLD, 15 DJ TROOPERS, 16 EMPRESS (both discs). A game is skipped, with the reason shown, if its executable is not the original or a 480p-patched version.

## Other games: .pnach and xdelta

```
python ps2_hdd_patcher.py list \\.\PhysicalDrive3
python ps2_hdd_patcher.py pnach \\.\PhysicalDrive3 SLUS-12345_ABCD1234.pnach --game SLUS_123.45
python ps2_hdd_patcher.py xdelta \\.\PhysicalDrive3 patch.xdelta --game SLUS_123.45
```

Add `--write` to apply. The `.pnach` mode supports EE `byte`/`short`/`word`/`double` patches that land in the game's main executable, and refuses to write if the CRC in the pnach file name doesn't match the installed game. The xdelta mode needs about twice the game's size in free disk space and only works when the patched image is not larger than the installed one.

## Undo

Every write saves a `backup_<ID>_<date>.ps2bak` file first. Keep it.

```
python iidx480p_hdd.py restore \\.\PhysicalDrive3 backup_SLPM_551.17_20261017-150000.ps2bak --write
```

If a game was patched more than once, restore the newest backup first.

## Safety

- Dry run by default; nothing is written without `--write` and typing `YES`.
- Refuses to touch a drive without a PS2 (APA) partition table, and never changes partition headers.
- Every changed sector is backed up before writing and read back afterwards.

Still: this writes directly to a disk. Back up anything you can't replace, and double-check the drive path.

## Tests

The tests build a synthetic PS2 drive (no game data) and check that the result on the drive is byte-for-byte what upstream `patch480p.py` produces, that variant switching and restore work, and that modified games are skipped.

```
pip install pycdlib
python tests/test_tools.py
```

## Updating the bundled 480p patch

Replace `vendor/iidx-ps2-480p/patch480p.py` and `mipsasm.py` with the latest from the upstream repository, update the commit in [`vendor/iidx-ps2-480p/UPSTREAM.md`](vendor/iidx-ps2-480p/UPSTREAM.md), and run the tests.

## Disclaimer

Unofficial fan project, not affiliated with or endorsed by KONAMI or Sony Interactive Entertainment. No game data is included. You need your own copies of the games. Use at your own risk.

## License

[MIT](LICENSE). The bundled iidx-ps2-480p files keep their own MIT license ([`vendor/iidx-ps2-480p/LICENSE`](vendor/iidx-ps2-480p/LICENSE)).
