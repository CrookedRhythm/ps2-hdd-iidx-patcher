"""Build a synthetic PS2 APA drive image (no real game data) with several beatmania IIDX games
whose executables pass patch480p.verify(). usage: make_test_drive.py <patch480p dir> <out dir>"""
import io, os, random, struct, sys, zlib
import pycdlib

pdir, out = sys.argv[1], sys.argv[2]  # <patch480p dir> <output dir>
sys.path.insert(0, pdir)
import patch480p as P
os.makedirs(out, exist_ok=True)
rnd = random.Random(7)


def forge_crc(block, pos, target):
    """Set block[pos:pos+4] so zlib.crc32(block) == target (CRC32 is affine over GF(2))."""
    def crc_with(x):
        b = bytearray(block); b[pos:pos + 4] = x.to_bytes(4, "little"); return zlib.crc32(b)
    c0 = crc_with(0)
    cols = [crc_with(1 << i) ^ c0 for i in range(32)]
    rows = []  # solve sum(x_i * cols[i]) = target ^ c0
    want = target ^ c0
    # Gaussian elimination on 32x32 bit matrix
    m = [(sum(((cols[i] >> r) & 1) << i for i in range(32)), (want >> r) & 1) for r in range(32)]
    piv = []
    for col in range(32):
        r = next((k for k in range(len(m)) if k not in [p[0] for p in piv] and (m[k][0] >> col) & 1), None)
        if r is None:
            continue
        piv.append((r, col))
        for k in range(len(m)):
            if k != r and (m[k][0] >> col) & 1:
                m[k] = (m[k][0] ^ m[r][0], m[k][1] ^ m[r][1])
    x = 0
    for r, col in piv:
        if m[r][1]:
            x |= 1 << col
    block[pos:pos + 4] = x.to_bytes(4, "little")
    assert zlib.crc32(block) == target


def fake_elf(name, break_word=False):
    size, msg_va = P.ELF_INFO[name]
    v = P.VERSIONS[name]
    d = bytearray(rnd.getrandbits(8) for _ in range(size))
    d[:0x80] = bytes(0x80)
    d[0:16] = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    struct.pack_into("<HHIIIIIHHHHHH", d, 16, 2, 8, 1, 0x100008, 0x34, 0, 0, 0x34, 0x20, 1, 0, 0, 0)
    struct.pack_into("<8I", d, 0x34, 1, 0x80, 0x100000, 0x100000, size - 0x80, size + 0x100000, 7, 0x80)
    for key in P.PLAN_KEYS:
        for va, old, new, note in v[key]:
            if old is not None:
                struct.pack_into("<I", d, va - 0x100000 + 0x80, old)
    if any(old is None for va, old, _, _ in v["patches24"]):
        o = msg_va - 0x100000 + 0x80
        blk = d[o:o + 0x200]
        forge_crc(blk, 0x1FC, P.MSG_BLOCK_CRC32)
        d[o:o + 0x200] = blk
    if break_word:
        va = v["patches24"][0][0]
        struct.pack_into("<I", d, va - 0x100000 + 0x80, 0xDEADBEEF)
    return bytes(d)


def make_iso(boot, elf, pad_len):
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=1, vol_ident="GAME")
    cnf = ("BOOT2 = cdrom0:\\%s;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n" % boot).encode()
    pad = bytes(rnd.getrandbits(8) for _ in range(pad_len))
    iso.add_fp(io.BytesIO(cnf), len(cnf), "/SYSTEM.CNF;1")
    iso.add_fp(io.BytesIO(pad), len(pad), "/AAPAD.BIN;1")
    iso.add_fp(io.BytesIO(elf), len(elf), "/%s;1" % boot)
    buf = io.BytesIO(); iso.write_fp(buf); iso.close()
    return buf.getvalue()


S = 512
games = [  # (boot name, title, break?, pad bytes)
    ("SLPM_551.17", "beatmania IIDX 15 DJ TROOPERS", False, 11 * 2**20),   # ELF straddles main/sub
    ("SLPM_552.21", "beatmania IIDX 16 EMPRESS", False, 3 * 2**20),
    ("SLPM_668.28", "beatmania IIDX 13 DistorteD", False, 12 * 2**20 - 300000),
    ("SLPM_669.95", "beatmania IIDX 14 GOLD (modified)", True, 2**20),
    ("SLUS_209.46", "Some Other Game", False, 2**20),
]
PART = 0x8000                     # 16 MB partitions
disk = bytearray(0x2000 * S)       # __mbr area
headers = [("__mbr", 0, 0x2000, 0x0001, 0, 0, 0, [])]
lba = 0x2000
layout = []
for boot, title, brk, pad in games:
    elf = fake_elf(boot, brk) if boot in P.VERSIONS else bytes(1000)
    open(os.path.join(out, boot + ".elf"), "wb").write(elf)
    iso = make_iso(boot, elf, pad)
    open(os.path.join(out, boot + ".iso"), "wb").write(iso)
    main = lba
    usable = [(main, 0x2000)]
    need = len(iso) - (PART - 0x2000) * S
    nsub = 0
    while need > 0:
        nsub += 1
        usable.append((main + nsub * PART, 0x800))
        need -= (PART - 0x800) * S
    pid = ("PP." + boot.replace("_", "-").replace(".", "")[:10] + ".." + title.upper().replace(" ", "_"))[:32]
    layout.append((boot, title, main, nsub, pid, iso, usable))
    lba += (nsub + 1) * PART
disk.extend(bytes(lba * S - len(disk)))

def hdr(start, length, nxt, prev, pid, ptype, flags=0, nsub=0, main=0, number=0, subs=()):
    h = bytearray(1024)
    struct.pack_into("<IIII", h, 0, 0, 0x00415041, nxt, prev)
    h[0x10:0x10 + len(pid)] = pid.encode()
    struct.pack_into("<IIHHI", h, 0x40, start, length, ptype, flags, nsub)
    struct.pack_into("<II", h, 0x58, main, number)
    for i, (s, l) in enumerate(subs):
        struct.pack_into("<II", h, 0x200 + 8 * i, s, l)
    if pid == "__mbr":
        h[0x100:0x120] = b"Sony Computer Entertainment Inc."
    struct.pack_into("<I", h, 0, sum(struct.unpack_from("<255I", h, 4)) & 0xFFFFFFFF)
    disk[start * S:start * S + 1024] = h

chain = [0] + [s for (_, _, main, nsub, _, _, _) in layout for s in [main + i * PART for i in range(nsub + 1)]]
nxt = {chain[i]: (chain[i + 1] if i + 1 < len(chain) else 0) for i in range(len(chain))}
prv = {chain[i]: chain[i - 1] for i in range(1, len(chain))}
hdr(0, 0x2000, nxt[0], chain[-1], "__mbr", 0x0001)
for boot, title, main, nsub, pid, iso, usable in layout:
    subs = [(main + (i + 1) * PART, PART) for i in range(nsub)]
    hdr(main, PART, nxt[main], prv[main], pid, 0x1337, nsub=nsub, subs=subs)
    for i, (s, _) in enumerate(subs):
        hdr(s, PART, nxt[s], prv[s], pid, 0x1337, flags=1, main=main, number=i + 1)
    # data + HDL header
    specs, pos = [], 0
    for start, reserved in usable:
        n = min(len(iso) - pos, (PART - reserved) * S)
        if n <= 0:
            break
        disk[(start + reserved) * S:(start + reserved) * S + n] = iso[pos:pos + n]
        specs.append((pos // 2048, start + reserved, n))
        pos += n
    h = bytearray(1024)
    struct.pack_into("<I", h, 0, 0xDEADFEED)
    h[8:8 + len(title)] = title.encode()
    h[0xAC:0xAC + len(boot)] = boot.encode()
    struct.pack_into("<IIi", h, 0xE8, 0, 0x14, len(specs))
    for i, s in enumerate(specs):
        struct.pack_into("<III", h, 0xF4 + 12 * i, *s)
    disk[(main + 0x808) * S:(main + 0x808) * S + 1024] = h

open(os.path.join(out, "ps2disk.img"), "wb").write(disk)
print("disk %d MB; games:" % (len(disk) >> 20), [(b, len(u)) for b, _, _, _, _, _, u in layout])
