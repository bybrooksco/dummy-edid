#!/usr/bin/env python3
"""Add a custom resolution to an HDMI/DP dummy plug by rewriting its EDID EEPROM over I2C.

Made for game-streaming hosts (Sunshine/Moonlight, Steam Remote Play) where the client
screen has an aspect ratio no commodity dummy plug advertises (3:2, 16:10, 21:9, ...).
Standard library only. Needs read/write access to /dev/i2c-* (root, or the i2c group)
and the i2c-dev module loaded for writes.
"""
import argparse
import fcntl
import glob
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path

I2C_SLAVE = 0x0703
EDID_ADDR = 0x50


# ---------- EDID parsing ----------

def blocks(e):
    return [e[i:i + 128] for i in range(0, len(e), 128)]


def check_edid(e):
    if len(e) < 128 or e[:8] != bytes.fromhex('00ffffffffffff00'):
        sys.exit('error: not an EDID (bad header)')
    if len(e) % 128:
        sys.exit(f'error: EDID length {len(e)} is not a multiple of 128')
    for n, b in enumerate(blocks(e)):
        if sum(b) % 256:
            sys.exit(f'error: block {n} checksum is invalid')


def dtd_offsets(e):
    """Offsets of every detailed timing descriptor (DTD) that holds a timing."""
    offs = [o for o in (54, 72, 90, 108) if e[o] | e[o + 1]]
    for base in range(128, len(e), 128):
        if e[base] != 0x02:  # CTA-861 extension only
            continue
        d = e[base + 2]
        if d < 4:
            continue
        o = base + d
        while o + 18 <= base + 127 and (e[o] | e[o + 1]):
            offs.append(o)
            o += 18
    return offs


def decode_dtd(d):
    clk = (d[0] | d[1] << 8) * 10_000
    ha, hb = d[2] | (d[4] >> 4) << 8, d[3] | (d[4] & 15) << 8
    va, vb = d[5] | (d[7] >> 4) << 8, d[6] | (d[7] & 15) << 8
    hz = clk / ((ha + hb) * (va + vb))
    mm = (d[12] | (d[14] >> 4) << 8, d[13] | (d[14] & 15) << 8)
    return ha, va, hz, clk, mm


def range_limit_clock(e):
    """Max pixel clock (Hz) from the base block's Display Range Limits descriptor, if any."""
    for o in (54, 72, 90, 108):
        if e[o:o + 3] == b'\0\0\0' and e[o + 3] == 0xFD:
            return e[o + 9] * 10_000_000
    return None


# ---------- timing ----------

def cvt_rb2(w, h, hz):
    """VESA CVT reduced blanking v2 timing: (pclk Hz, hblank, hfp, hsync, vblank, vfp, vsync)."""
    hblank, hfp, hsync = 80, 8, 32
    vsync, vbp_min, vfp_min, min_vbi = 8, 6, 1, 460e-6
    h_period = (1 / hz - min_vbi) / h
    vblank = max(int(min_vbi / h_period) + 1, vfp_min + vsync + vbp_min)
    vfp = vblank - vsync - vbp_min
    pclk = int((w + hblank) * (h + vblank) * hz) // 10_000 * 10_000  # CVT rounds down; EDID stores 10 kHz units
    return pclk, hblank, hfp, hsync, vblank, vfp, vsync


def encode_dtd(w, h, hz, mm):
    pclk, hb, hfp, hs, vb, vfp, vs = cvt_rb2(w, h, hz)
    c = pclk // 10_000
    if w > 4095 or h > 4095 or c > 0xFFFF:
        sys.exit(f'error: {w}x{h}@{hz} does not fit in an EDID detailed timing descriptor')
    hmm, vmm = mm
    return bytes([
        c & 255, c >> 8, w & 255, hb & 255, (w >> 8) << 4 | hb >> 8,
        h & 255, vb & 255, (h >> 8) << 4 | vb >> 8, hfp & 255, hs & 255,
        (vfp & 15) << 4 | (vs & 15),
        (hfp >> 8) << 6 | (hs >> 8) << 4 | (vfp >> 4) << 2 | (vs >> 4),
        hmm & 255, vmm & 255, (hmm >> 8) << 4 | vmm >> 8, 0, 0,
        0x1A,  # digital separate sync, hsync +, vsync - (CVT-RB)
    ]), pclk


def add_mode(e, w, h, hz, slot, max_clock):
    """Return a new EDID with DTD number `slot` (1-based, see `show`) replaced by WxH@hz."""
    e = bytearray(e)
    offs = dtd_offsets(e)
    if not 1 <= slot <= len(offs):
        sys.exit(f'error: slot must be 1..{len(offs)}')
    if slot == 1:
        sys.exit('error: slot 1 is the preferred (native) timing; pick another slot')
    o = offs[slot - 1]
    # image size = the base block's display size (cm), so edid-decode sees no size mismatch
    mm = (e[21] * 10, e[22] * 10) if e[21] and e[22] else decode_dtd(e[o:o + 18])[4]
    dtd, pclk = encode_dtd(w, h, hz, mm)
    limits = [c for c in (max_clock, range_limit_clock(e)) if c]
    if pclk > min(limits):
        sys.exit(f'error: {w}x{h}@{hz} needs {pclk / 1e6:.2f} MHz, over the {min(limits) / 1e6:.0f} MHz '
                 f'limit (HDMI 2.0 = 600, HDMI 1.4 = 340; set --max-clock if your link allows more). '
                 f'Try a lower refresh rate or a smaller size with the same aspect ratio.')
    e[o:o + 18] = dtd
    base = o // 128 * 128
    e[base + 127] = (-sum(e[base:base + 127])) & 255
    check_edid(bytes(e))
    return bytes(e), pclk


# ---------- I2C ----------

def connectors():
    for p in sorted(glob.glob('/sys/class/drm/card*-*')):
        edid = Path(p, 'edid')
        if Path(p, 'status').read_text().strip() == 'connected' and edid.exists():
            data = edid.read_bytes()
            if data:
                yield Path(p).name.split('-', 1)[1], data


def i2c_read(bus, n):
    fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
    try:
        fcntl.ioctl(fd, I2C_SLAVE, EDID_ADDR)
        out = b''
        for off in range(0, n, 128):
            os.write(fd, bytes([off]))
            out += os.read(fd, 128)
        return out
    finally:
        os.close(fd)


def i2c_write(bus, changes):
    fd = os.open(f'/dev/i2c-{bus}', os.O_RDWR)
    try:
        fcntl.ioctl(fd, I2C_SLAVE, EDID_ADDR)
        for off, val in changes:
            os.write(fd, bytes([off, val]))
            time.sleep(0.02)  # EEPROM write cycle (24C02 needs <=5 ms)
    finally:
        os.close(fd)


def find_bus(edid):
    """The I2C bus whose EEPROM at 0x50 returns exactly this EDID. Read-only probing."""
    if not glob.glob('/dev/i2c-*'):
        sys.exit('error: no /dev/i2c-* devices; run: sudo modprobe i2c-dev')
    hits = []
    for dev in sorted(glob.glob('/dev/i2c-*'), key=lambda p: int(p.rsplit('-', 1)[1])):
        bus = int(dev.rsplit('-', 1)[1])
        try:
            if i2c_read(bus, len(edid)) == edid:
                hits.append(bus)
        except OSError:
            pass
    if len(hits) != 1:
        sys.exit(f'error: expected exactly one I2C bus holding this EDID, found {hits or "none"}')
    return hits[0]


def connector_edid(name):
    found = dict(connectors())
    if name not in found:
        sys.exit(f'error: connector {name} not connected; connected: {", ".join(found) or "none"}')
    return found[name]


def write_and_verify(bus, old, new):
    changes = [(i, b) for i, (a, b) in enumerate(zip(old, new)) if a != b]
    i2c_write(bus, changes)
    back = i2c_read(bus, len(new))
    if back != new:
        sys.exit(f'error: readback mismatch after writing {len(changes)} bytes; '
                 'the plug may be write-protected or partially written. Restore from the backup.')
    return len(changes)


# ---------- commands ----------

def show(e, label):
    check_edid(e)
    name = next((e[o + 5:o + 18].split(b'\n')[0].decode(errors='replace').strip()
                 for o in (54, 72, 90, 108) if e[o:o + 3] == b'\0\0\0' and e[o + 3] == 0xFC), '?')
    print(f'{label}: "{name}", {len(e) // 128} block(s)')
    rl = range_limit_clock(e)
    if rl:
        print(f'  max pixel clock (range limits): {rl / 1e6:.0f} MHz')
    for n, o in enumerate(dtd_offsets(e), 1):
        w, h, hz, clk, _ = decode_dtd(e[o:o + 18])
        print(f'  slot {n}: {w}x{h} @ {hz:.3f} Hz  {clk / 1e6:.2f} MHz' + ('  (preferred)' if n == 1 else ''))


def parse_mode(s):
    try:
        wh, hz = s.split('@')
        w, h = wh.lower().split('x')
        return int(w), int(h), float(hz)
    except ValueError:
        sys.exit(f'error: mode must look like 3600x2400@60, got {s!r}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='connected outputs and their EDID slots')
    a = sub.add_parser('add', help='replace a slot with a new mode')
    a.add_argument('target', help='connector (e.g. HDMI-A-1) or, with --file, an EDID .bin')
    a.add_argument('mode', help='WIDTHxHEIGHT@HZ, e.g. 3600x2400@60')
    a.add_argument('--slot', type=int, help='slot to replace (default: the last one)')
    a.add_argument('--max-clock', type=float, default=600, help='link pixel-clock limit in MHz (default 600, HDMI 2.0)')
    a.add_argument('--file', action='store_true', help='edit an EDID file offline instead of a plug')
    a.add_argument('-o', '--out', help='with --file: output path (default: <input>-<mode>.bin)')
    a.add_argument('--yes', action='store_true', help='actually write to the plug (default: dry run)')
    r = sub.add_parser('restore', help='write a backup .bin back to the plug')
    r.add_argument('connector')
    r.add_argument('backup')
    r.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    if args.cmd == 'list':
        for name, e in connectors():
            show(e, name)
        return

    if args.cmd == 'restore':
        cur, want = connector_edid(args.connector), Path(args.backup).read_bytes()
        check_edid(want)
        bus = find_bus(cur)
        if len(want) != len(cur):
            sys.exit('error: backup size differs from the plug EDID')
        show(want, f'restore to bus {bus}')
        if not args.yes:
            sys.exit('dry run; add --yes to write')
        print(f'restored: {write_and_verify(bus, cur, want)} bytes written and verified. Replug or reboot to apply.')
        return

    w, h, hz = parse_mode(args.mode)
    old = Path(args.target).read_bytes() if args.file else connector_edid(args.target)
    check_edid(old)
    slot = args.slot or len(dtd_offsets(old))
    o = dtd_offsets(old)[slot - 1] if 1 <= slot <= len(dtd_offsets(old)) else None
    replaced = decode_dtd(old[o:o + 18]) if o is not None else None
    new, pclk = add_mode(old, w, h, hz, slot, args.max_clock * 1e6)
    print(f'slot {slot}: {replaced[0]}x{replaced[1]} @ {replaced[2]:.3f} Hz  ->  '
          f'{w}x{h} @ {pclk / ((w + 80) * (h + cvt_rb2(w, h, hz)[4])):.3f} Hz ({pclk / 1e6:.2f} MHz)')

    if args.file:
        out = args.out or f'{Path(args.target).with_suffix("")}-{w}x{h}.bin'
        Path(out).write_bytes(new)
        print(f'wrote {out}')
        return

    bus = find_bus(old)
    backup = Path('backups') / f'{args.target}-{date.today()}-{old[10:16].hex()}.bin'
    backup.parent.mkdir(exist_ok=True)
    if not backup.exists():
        backup.write_bytes(old)
    print(f'plug EEPROM on /dev/i2c-{bus}; original saved to {backup}')
    if not args.yes:
        sys.exit('dry run; add --yes to write')
    n = write_and_verify(bus, old, new)
    print(f'{n} bytes written and verified. Replug the plug or reboot so the GPU re-reads it.')
    print(f'undo: {sys.argv[0]} restore {args.target} {backup} --yes')


if __name__ == '__main__':
    main()
