"""
PS2 Disc Dumper
Created by TrixR4PiGz

Copies a PlayStation 2 game disc from an optical drive to an .iso file you can
load in PCSX2. Windows only, no extra Python packages needed.

Features
  - Simple window (drive picker, folder picker, progress bar, log)
  - Game title lookup from PCSX2's GameIndex.yaml (files are named by title)
  - Smarter bad-sector recovery (several retry passes before zero-filling)
  - Optional verify pass (re-reads the disc and the saved file, compares hashes)
  - Optional Redump datfile check (confirms your dump matches a known-good disc)
  - Optional CHD conversion (smaller files for PCSX2; needs chdman.exe)
  - Writes a .log file next to every dump
  - Remembers your settings, warns about FAT32 folders, beeps when finished
  - PS1 discs -> BIN/CUE (EXPERIMENTAL, needs "Run as administrator")
  - Automatic Redump renaming when a match is found
  - Bad-sector map (.bad) written next to the dump
  - Multi-disc / disc-swap friendly naming
  - Quick dump / Dump & Verify modes
  - Dark mode toggle
  - Drag-and-drop for Redump .dat and GameIndex.yaml
  - Copy log / export log as HTML
  - Remembers last drive and auto-scans on launch

Build an .exe (no console window) with:
    python -m pip install pyinstaller
    python -m PyInstaller --onefile --windowed --name PS2DiscDumper ps2_disc_dumper.py
"""

import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import json
import os
import queue
import re
import shutil
import string
import struct
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import zlib

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:  # handled in main()
    tk = None

VERSION = "2.1"
AUTHOR = "TrixR4PiGz"
IS_WINDOWS = sys.platform == "win32"

MAX_BAD_SECTORS = 500          # give up if the disc is this damaged
RECOVERY_PASSES = 6            # slow retry passes over unreadable sectors
FAT32_MAX_FILE = 4 * 1024 ** 3 - 1
DRIVE_CDROM = 5

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
IOCTL_CDROM_READ_TOC = 0x00024000
IOCTL_SCSI_PASS_THROUGH_DIRECT = 0x0004D014
IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PS2DiscDumper")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


# --------------------------------------------------------------------------
# Windows API setup
# --------------------------------------------------------------------------
if IS_WINDOWS:
    import msvcrt  # noqa: F401  (used for get_osfhandle)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetErrorMode(1)  # no "insert disc" popups

    kernel32.GetLogicalDrives.restype = wt.DWORD
    kernel32.GetDriveTypeW.argtypes = [wt.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wt.UINT
    kernel32.GetVolumeInformationW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, wt.DWORD,
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.DWORD), wt.LPWSTR, wt.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wt.BOOL
    kernel32.DeviceIoControl.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD,
        wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wt.BOOL
    kernel32.GetDiskFreeSpaceExW.argtypes = [
        wt.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
    ]
    kernel32.GetDiskFreeSpaceExW.restype = wt.BOOL
    kernel32.CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE,
    ]
    kernel32.CreateFileW.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL


class SCSI_PASS_THROUGH_DIRECT(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("ScsiStatus", ctypes.c_ubyte),
        ("PathId", ctypes.c_ubyte),
        ("TargetId", ctypes.c_ubyte),
        ("Lun", ctypes.c_ubyte),
        ("CdbLength", ctypes.c_ubyte),
        ("SenseInfoLength", ctypes.c_ubyte),
        ("DataIn", ctypes.c_ubyte),
        ("DataTransferLength", ctypes.c_ulong),
        ("TimeOutValue", ctypes.c_ulong),
        ("DataBuffer", ctypes.c_void_p),
        ("SenseInfoOffset", ctypes.c_ulong),
        ("Cdb", ctypes.c_ubyte * 16),
    ]


class SPTD_WITH_SENSE(ctypes.Structure):
    _fields_ = [
        ("sptd", SCSI_PASS_THROUGH_DIRECT),
        ("filler", ctypes.c_ulong),
        ("sense", ctypes.c_ubyte * 32),
    ]


class Cancelled(Exception):
    pass


class DumpError(Exception):
    pass


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def safe_name(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .") or "disc"


def app_dir():
    path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    return os.path.dirname(path)


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def play_sound(status):
    try:
        import winsound
        icon = {"ok": winsound.MB_ICONASTERISK, "warn": winsound.MB_ICONEXCLAMATION}.get(
            status, winsound.MB_ICONHAND)
        winsound.MessageBeep(icon)
    except Exception:
        pass


def lba_to_msf(lba):
    """Convert absolute LBA to MSF string used in CUE sheets (INDEX 01)."""
    if lba < 0:
        lba = 0
    return f"{lba // 4500:02d}:{(lba // 75) % 60:02d}:{lba % 75:02d}"


def normalize_serial(serial):
    """Normalize serials so SCES-12345, SCES_123.45, SCES 12345 all match."""
    if not serial:
        return None
    s = serial.upper().replace(" ", "").replace("_", "-").replace(".", "")
    # SCES12345 -> SCES-12345
    m = re.match(r"^([A-Z]{4})(\d{5})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # already SCES-12345 style
    m = re.match(r"^([A-Z]{4})-(\d{5})$", s)
    if m:
        return s
    return s


# --------------------------------------------------------------------------
# Drive / disc helpers
# --------------------------------------------------------------------------
def optical_drives():
    mask = kernel32.GetLogicalDrives()
    found = []
    for i, letter in enumerate(string.ascii_uppercase):
        if mask & (1 << i) and kernel32.GetDriveTypeW(f"{letter}:\\") == DRIVE_CDROM:
            found.append(letter)
    return found


def volume_info(root):
    """Returns (label, filesystem) or (None, None) if nothing readable is there."""
    label = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(64)
    ok = kernel32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 64)
    return (label.value, fs.value) if ok else (None, None)


def filesystem_of(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive or drive.startswith("\\\\"):
        return None
    return volume_info(drive + "\\")[1]


def identify_disc(root):
    """Returns (kind, serial). kind is PS2, PS1, PS3 or UNKNOWN."""
    if os.path.isdir(os.path.join(root, "PS3_GAME")):
        return "PS3", None
    cnf = os.path.join(root, "SYSTEM.CNF")
    if os.path.isfile(cnf):
        with open(cnf, "r", errors="ignore") as f:
            text = f.read().upper()
        m = re.search(r"([A-Z]{4})[_-](\d{3})\.?(\d{2})", text)
        serial = None
        if m:
            serial = normalize_serial(f"{m.group(1)}-{m.group(2)}{m.group(3)}")
        if "BOOT2" in text:
            return "PS2", serial
        if "BOOT" in text:
            return "PS1", serial
    return "UNKNOWN", None


def drive_model(letter):
    """Best-effort drive make/model for the log file."""
    handle = None
    try:
        handle = kernel32.CreateFileW(rf"\\.\{letter}:", 0, 3, None, OPEN_EXISTING, 0, None)
        if handle in (None, INVALID_HANDLE):
            return "unknown"
        query = ctypes.create_string_buffer(struct.pack("<IIB3x", 0, 0, 0), 12)
        out = ctypes.create_string_buffer(1024)
        returned = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(handle, IOCTL_STORAGE_QUERY_PROPERTY, query, 12,
                                      out, 1024, ctypes.byref(returned), None)
        if not ok:
            return "unknown"
        raw = out.raw
        offsets = struct.unpack_from("<III", raw, 12)  # vendor, product, revision

        def text(off):
            return raw[off:raw.index(b"\0", off)].decode(errors="ignore").strip() if off else ""

        return " ".join(t for t in (text(o) for o in offsets) if t) or "unknown"
    except Exception:
        return "unknown"
    finally:
        if handle not in (None, INVALID_HANDLE):
            kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------
# Sector sources (what we read from)
# --------------------------------------------------------------------------
class RawVolumeSource:
    """Reads 2048-byte sectors straight from the drive (PS2 discs -> ISO)."""
    sector_size = 2048
    chunk_sectors = 1024  # 2 MiB per read (was 512)

    def __init__(self, letter):
        self.letter = letter
        self.dev = None
        self.open()
        self.total_sectors = self._find_size() // self.sector_size

    def open(self):
        self.close()
        self.dev = open(rf"\\.\{self.letter}:", "rb", buffering=0)

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except OSError:
                pass
            self.dev = None

    def _find_size(self):
        size = 0
        out = ctypes.c_longlong(0)
        returned = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(
            wt.HANDLE(msvcrt.get_osfhandle(self.dev.fileno())),
            IOCTL_DISK_GET_LENGTH_INFO, None, 0,
            ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(returned), None)
        if ok:
            size = out.value
        if size <= 0:  # fall back to the filesystem's own idea of the size
            total = ctypes.c_ulonglong(0)
            if kernel32.GetDiskFreeSpaceExW(f"{self.letter}:\\", None, ctypes.byref(total), None):
                size = total.value
        return size - (size % self.sector_size)

    def read(self, sector, count):
        want = count * self.sector_size
        self.dev.seek(sector * self.sector_size)
        data = self.dev.read(want)
        if len(data) != want:
            raise OSError("short read")
        return data


def parse_toc(raw):
    """Parses a CDROM_TOC buffer into ([tracks], leadout_lba)."""
    length = (raw[0] << 8) | raw[1]
    count = min((length - 2) // 8, 100)
    tracks, leadout = [], None
    for i in range(count):
        off = 4 + i * 8
        control = raw[off + 1] >> 4
        number = raw[off + 2]
        m, s, f = raw[off + 5], raw[off + 6], raw[off + 7]
        lba = (m * 60 + s) * 75 + f - 150
        if number == 0xAA:
            leadout = lba
        elif 1 <= number <= 99:
            tracks.append({"number": number, "lba": lba, "is_data": bool(control & 0x04)})
    if not tracks or leadout is None:
        raise OSError("Couldn't read the disc's table of contents.")
    return tracks, leadout


class CdRawSource:
    """Reads raw 2352-byte CD sectors with SCSI READ CD (PS1 discs -> BIN/CUE)."""
    sector_size = 2352
    chunk_sectors = 32  # larger chunks for better throughput

    def __init__(self, letter):
        self.letter = letter
        self.handle = None
        self.open()
        self.tracks, self.total_sectors = self._read_toc()

    def open(self):
        self.close()
        h = kernel32.CreateFileW(rf"\\.\{self.letter}:", GENERIC_READ | GENERIC_WRITE,
                                 3, None, OPEN_EXISTING, 0, None)
        if h in (None, INVALID_HANDLE):
            err = ctypes.get_last_error()
            if err == 5:
                raise PermissionError("access denied")
            raise OSError(f"couldn't open the drive (error {err})")
        self.handle = h

    def close(self):
        if self.handle not in (None, INVALID_HANDLE):
            try:
                kernel32.CloseHandle(self.handle)
            except Exception:
                pass
        self.handle = None

    def _read_toc(self):
        buf = ctypes.create_string_buffer(804)
        returned = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(self.handle, IOCTL_CDROM_READ_TOC, None, 0,
                                      buf, 804, ctypes.byref(returned), None)
        if not ok:
            raise OSError("Couldn't read the disc's table of contents.")
        return parse_toc(buf.raw)

    def _track_index(self, lba):
        idx = 0
        for i, tr in enumerate(self.tracks):
            if tr["lba"] <= lba:
                idx = i
        return idx

    def _read_cd(self, lba, count, audio):
        size = count * self.sector_size
        data = ctypes.create_string_buffer(size)
        wrapper = SPTD_WITH_SENSE()
        sptd = wrapper.sptd
        sptd.Length = ctypes.sizeof(SCSI_PASS_THROUGH_DIRECT)
        sptd.CdbLength = 12
        sptd.SenseInfoLength = 32
        sptd.DataIn = 1  # SCSI_IOCTL_DATA_IN
        sptd.DataTransferLength = size
        sptd.TimeOutValue = 30
        sptd.DataBuffer = ctypes.addressof(data)
        sptd.SenseInfoOffset = SPTD_WITH_SENSE.sense.offset
        cdb = sptd.Cdb
        cdb[0] = 0xBE                          # READ CD
        cdb[1] = 0x04 if audio else 0x00       # expected sector type: CD-DA / any
        cdb[2], cdb[3] = (lba >> 24) & 0xFF, (lba >> 16) & 0xFF
        cdb[4], cdb[5] = (lba >> 8) & 0xFF, lba & 0xFF
        cdb[6], cdb[7], cdb[8] = (count >> 16) & 0xFF, (count >> 8) & 0xFF, count & 0xFF
        cdb[9] = 0x10 if audio else 0xF8       # user data only / full raw sector
        returned = wt.DWORD(0)
        ok = kernel32.DeviceIoControl(self.handle, IOCTL_SCSI_PASS_THROUGH_DIRECT,
                                      ctypes.byref(wrapper), ctypes.sizeof(wrapper),
                                      ctypes.byref(wrapper), ctypes.sizeof(wrapper),
                                      ctypes.byref(returned), None)
        if not ok or sptd.ScsiStatus != 0:
            raise OSError("READ CD failed")
        return data.raw

    def read(self, sector, count):
        pieces = []
        while count > 0:
            idx = self._track_index(sector)
            next_start = self.tracks[idx + 1]["lba"] if idx + 1 < len(self.tracks) else self.total_sectors
            n = max(1, min(count, next_start - sector))
            pieces.append(self._read_cd(sector, n, not self.tracks[idx]["is_data"]))
            sector += n
            count -= n
        return b"".join(pieces)


def build_cue(bin_name, tracks):
    """Build a Redump-compatible CUE. INDEX 01 times are relative to the start of the track."""
    lines = [f'FILE "{bin_name}" BINARY']
    for i, tr in enumerate(tracks):
        mode = "MODE2/2352" if tr["is_data"] else "AUDIO"
        lines.append(f"  TRACK {tr['number']:02d} {mode}")
        # INDEX 01 is relative to the beginning of this track (almost always 00:00:00)
        # For multi-track discs the absolute LBA is already accounted for by the FILE order.
        lines.append("    INDEX 01 00:00:00")
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------
# Title lookup (PCSX2 GameIndex.yaml) and Redump datfile
# --------------------------------------------------------------------------
def find_gameindex():
    candidates = [os.path.join(os.path.expanduser("~"), "Documents", "PCSX2", "resources", "GameIndex.yaml")]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates += glob.glob(os.path.join(base, "PCSX2*", "resources", "GameIndex.yaml"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


_SERIAL_LINE = re.compile(r"^([A-Z]{4}[-_]?\d{5}):\s*$")


def load_titles(path):
    titles = {}
    current = None
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        for line in f:
            m = _SERIAL_LINE.match(line)
            if m:
                current = normalize_serial(m.group(1))
                continue
            if current and current not in titles and line.lstrip().startswith("name:"):
                val = line.split(":", 1)[1].strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1].replace('\\"', '"')
                titles[current] = val
    return titles


_redump_cache = {}


def redump_lookup(dat_path, md5, sha1, size):
    """Returns the matching game name from a Redump datfile, or None."""
    stamp = os.path.getmtime(dat_path)
    cached = _redump_cache.get(dat_path)
    if not cached or cached[0] != stamp:
        by_sha1, by_md5 = {}, {}
        for game in ET.parse(dat_path).getroot().iter("game"):
            gname = game.get("name", "")
            for rom in game.iter("rom"):
                try:
                    rsize = int(rom.get("size") or 0)
                except ValueError:
                    rsize = 0
                if rom.get("sha1"):
                    by_sha1[rom.get("sha1").lower()] = (gname, rsize)
                if rom.get("md5"):
                    by_md5[rom.get("md5").lower()] = (gname, rsize)
        cached = (stamp, by_sha1, by_md5)
        _redump_cache[dat_path] = cached
    for table, key in ((cached[1], sha1.lower()), (cached[2], md5.lower())):
        hit = table.get(key)
        if hit and (not hit[1] or hit[1] == size):
            return hit[0]
    return None


# --------------------------------------------------------------------------
# Dumping engine
# --------------------------------------------------------------------------
class Reporter:
    """Thread-safe bridge from the worker thread to the window."""

    def __init__(self, q):
        self.q = q
        self.lines = []
        self._last = 0.0

    def log(self, text):
        self.lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")
        self.q.put(("log", text))

    def progress(self, label, done, total, start):
        now = time.time()
        if now - self._last < 0.15 and done < total:
            return
        self._last = now
        self.q.put(("progress", label, done, total, start))

    def percent(self, label, pct):
        self.q.put(("pct", label, pct))


def read_chunk(source, sector, n, bad):
    """Fast read; if it fails, retry sector by sector and zero-fill what can't be read."""
    try:
        return source.read(sector, n)
    except OSError:
        pass
    pieces = []
    for s in range(sector, sector + n):
        try:
            pieces.append(source.read(s, 1))
        except OSError:
            bad.append(s)
            pieces.append(b"\x00" * source.sector_size)
            if len(bad) > MAX_BAD_SECTORS:
                raise DumpError("Too many unreadable sectors - the disc is badly scratched or dirty.")
    return b"".join(pieces)


def recover_bad_sectors(source, out_path, bad, rep, cancel):
    """Slow retry passes. Patches recovered sectors into the file; returns what's still bad."""
    remaining = list(bad)
    ss = source.sector_size
    for attempt in range(1, RECOVERY_PASSES + 1):
        if not remaining:
            break
        rep.log(f"Recovery pass {attempt}/{RECOVERY_PASSES}: retrying {len(remaining)} sector(s)...")
        rep.progress(f"Recovery pass {attempt}/{RECOVERY_PASSES}", 0, len(remaining), time.time())
        time.sleep(0.8)
        # Re-open only if the previous handle looks dead
        try:
            source.open()
        except OSError:
            pass
        still = []
        with open(out_path, "r+b") as f:
            for i, s in enumerate(remaining):
                if cancel.is_set():
                    raise Cancelled()
                try:
                    data = source.read(s, 1)
                except OSError:
                    still.append(s)
                    continue
                f.seek(s * ss)
                f.write(data)
                if (i + 1) % 5 == 0 or i + 1 == len(remaining):
                    rep.progress(f"Recovery pass {attempt}/{RECOVERY_PASSES}", i + 1, len(remaining), time.time())
        if len(still) < len(remaining):
            rep.log(f"  recovered {len(remaining) - len(still)} sector(s)")
        remaining = still
    return remaining


def hash_file(path, rep, cancel, label):
    md5, sha1, crc = hashlib.md5(), hashlib.sha1(), 0
    total = os.path.getsize(path)
    done, start = 0, time.time()
    with open(path, "rb") as f:
        while True:
            if cancel.is_set():
                raise Cancelled()
            data = f.read(4 * 1024 * 1024)
            if not data:
                break
            md5.update(data)
            sha1.update(data)
            crc = zlib.crc32(data, crc)
            done += len(data)
            rep.progress(label, done, total, start)
    return md5.hexdigest(), sha1.hexdigest(), crc & 0xFFFFFFFF


def dump_source(source, out_path, rep, cancel):
    ss, total = source.sector_size, source.total_sectors
    md5, sha1, crc = hashlib.md5(), hashlib.sha1(), 0
    bad = []
    start = time.time()
    finished = False
    try:
        with open(out_path, "wb") as out:
            sector = 0
            while sector < total:
                if cancel.is_set():
                    raise Cancelled()
                n = min(source.chunk_sectors, total - sector)
                data = read_chunk(source, sector, n, bad)
                out.write(data)
                md5.update(data)
                sha1.update(data)
                crc = zlib.crc32(data, crc)
                sector += n
                rep.progress("Reading disc", sector * ss, total * ss, start)

        initial_bad = len(bad)
        remaining = bad
        md5_hex, sha1_hex, crc_val = md5.hexdigest(), sha1.hexdigest(), crc & 0xFFFFFFFF
        if bad:
            rep.log(f"{initial_bad} sector(s) couldn't be read on the first pass.")
            remaining = recover_bad_sectors(source, out_path, bad, rep, cancel)
            if len(remaining) < initial_bad:
                rep.log("Re-hashing the file after recovery...")
                md5_hex, sha1_hex, crc_val = hash_file(out_path, rep, cancel, "Re-hashing")
        finished = True
    finally:
        if not finished and os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass
    return {
        "md5": md5_hex, "sha1": sha1_hex, "crc": crc_val,
        "initial_bad": initial_bad, "bad": remaining,
        "seconds": time.time() - start,
    }


def verify_dump(source, out_path, expected_md5, rep, cancel):
    """Re-reads the disc and the saved file; both must match the original hash."""
    try:
        source.open()
    except OSError:
        pass
    ss, total = source.sector_size, source.total_sectors
    h = hashlib.md5()
    start, sector = time.time(), 0
    while sector < total:
        if cancel.is_set():
            raise Cancelled()
        n = min(source.chunk_sectors, total - sector)
        try:
            h.update(source.read(sector, n))
        except OSError:
            return False, f"FAILED - the disc couldn't be re-read near sector {sector}."
        sector += n
        rep.progress("Verifying disc", sector * ss, total * ss, start)
    disc_md5 = h.hexdigest()
    file_md5 = hash_file(out_path, rep, cancel, "Verifying file")[0]
    if disc_md5 == expected_md5 == file_md5:
        return True, "PASSED - disc and saved file match."
    if disc_md5 != expected_md5:
        return False, "FAILED - a second read of the disc gave different data (dirty or marginal disc?)."
    return False, "FAILED - the saved file doesn't match what was read (disk problem?)."


def find_chdman(configured):
    for path in (configured, os.path.join(app_dir(), "chdman.exe"), shutil.which("chdman")):
        if path and os.path.isfile(path):
            return path
    return None


def convert_to_chd(chdman, source_path, kind, rep, cancel):
    chd_path = os.path.splitext(source_path)[0] + ".chd"
    if kind == "PS1":
        cmd = [chdman, "createcd", "-i", os.path.splitext(source_path)[0] + ".cue", "-o", chd_path, "-f"]
    else:
        cmd = [chdman, "createdvd", "-hs", "2048", "-i", source_path, "-o", chd_path, "-f"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    tail = ""
    try:
        while True:
            if cancel.is_set():
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                if os.path.exists(chd_path):
                    try:
                        os.remove(chd_path)
                    except OSError:
                        pass
                raise Cancelled()
            chunk = os.read(proc.stdout.fileno(), 512)
            if not chunk:
                break
            tail = (tail + chunk.decode(errors="ignore"))[-400:]
            found = re.findall(r"(\d+(?:\.\d+)?)% complete", tail)
            if found:
                rep.percent("Converting to CHD", float(found[-1]))
        proc.wait()
    except Cancelled:
        raise
    if proc.returncode != 0:
        if os.path.exists(chd_path):
            try:
                os.remove(chd_path)
            except OSError:
                pass
        return None, f"chdman failed (exit code {proc.returncode}): {tail.strip()[-200:]}"
    return chd_path, None


def write_log(path, pairs, transcript):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"PS2 Disc Dumper v{VERSION} - created by {AUTHOR}\n")
        f.write("=" * 64 + "\n")
        for key, value in pairs:
            f.write(f"{key:<16}: {value}\n")
        f.write("\nTranscript\n" + "-" * 64 + "\n")
        f.write("\n".join(transcript) + "\n")


def write_bad_map(path, bad_sectors, sector_size):
    """Write a simple text map of unreadable sectors."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Bad sector map - {len(bad_sectors)} sector(s)\n")
        f.write(f"# Sector size: {sector_size}\n")
        f.write("# Format: LBA (hex offset)\n")
        for s in bad_sectors:
            f.write(f"{s} (0x{s * sector_size:X})\n")


def write_html_log(path, pairs, transcript, status):
    colour = {"ok": "#1a7f37", "warn": "#b35900", "fail": "#c62828"}.get(status, "#333")
    rows = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in pairs)
    body = "<br>".join(line.replace("<", "&lt;").replace(">", "&gt;") for line in transcript)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>PS2 Disc Dumper Log</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; background:#fafafa; color:#222; }}
h1 {{ margin-bottom: 4px; }}
table {{ border-collapse: collapse; margin: 16px 0; }}
td {{ padding: 4px 12px 4px 0; vertical-align: top; }}
.transcript {{ background:#1e1e1e; color:#d4d4d4; padding:12px; border-radius:6px;
              font-family: Consolas, monospace; font-size:13px; white-space:pre-wrap; }}
.status {{ color:{colour}; font-weight:bold; }}
</style></head><body>
<h1>PS2 Disc Dumper v{VERSION}</h1>
<p>Created by {AUTHOR}</p>
<p class="status">Status: {status.upper()}</p>
<table>{rows}</table>
<h2>Transcript</h2>
<div class="transcript">{body}</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def run_job(opts, rep, cancel):
    """Runs the whole dump. Returns (status, short_message); status is ok / warn / fail."""
    letter, kind = opts["letter"], opts["kind"]
    out_dir, name = opts["out_dir"], opts["name"]
    mode = opts.get("mode", "full")  # full | verify_only | quick
    ext = ".bin" if kind == "PS1" else ".iso"
    out_path = os.path.join(out_dir, name + ext)
    log_path = os.path.join(out_dir, name + ".log")
    bad_path = os.path.join(out_dir, name + ".bad")
    t0 = time.time()
    source = None
    dump_done = False
    verify_note = "not requested"
    redump_note = "not requested"
    chd_note = "not requested"
    redump_name = None
    try:
        rep.log("Opening the drive...")
        source = CdRawSource(letter) if kind == "PS1" else RawVolumeSource(letter)
        total_bytes = source.total_sectors * source.sector_size
        if total_bytes <= 0:
            raise DumpError("Couldn't work out the size of the disc.")

        # Stronger FAT32 check: destination folder filesystem
        fs = filesystem_of(out_dir)
        if fs and fs.upper() == "FAT32" and total_bytes > FAT32_MAX_FILE:
            raise DumpError("That folder is on a FAT32 drive, which can't store files over 4 GB. "
                            "Pick a folder on an NTFS or exFAT drive.")

        if shutil.disk_usage(out_dir).free < total_bytes + 50 * 1024 * 1024:
            raise DumpError("Not enough free space in that folder.")
        if kind == "PS1":
            rep.log("PS1 mode is experimental: audio tracks may not be bit-identical to Redump dumps.")
        rep.log(f"Dumping a {kind} disc ({total_bytes / 1073741824:.2f} GB) to {out_path}")
        if mode == "quick":
            rep.log("Quick mode: fewer recovery passes, no verify.")

        # Temporarily reduce recovery passes in quick mode
        global RECOVERY_PASSES
        old_passes = RECOVERY_PASSES
        if mode == "quick":
            RECOVERY_PASSES = 2

        try:
            res = dump_source(source, out_path, rep, cancel)
        finally:
            RECOVERY_PASSES = old_passes

        dump_done = True
        remaining = res["bad"]
        rep.log(f"MD5  {res['md5']}")
        rep.log(f"SHA1 {res['sha1']}")
        rep.log(f"CRC32 {res['crc']:08x}")
        if remaining:
            rep.log(f"WARNING: {len(remaining)} sector(s) could not be read and were zero-filled.")
            try:
                write_bad_map(bad_path, remaining, source.sector_size)
                rep.log(f"Bad-sector map written: {os.path.basename(bad_path)}")
            except OSError as e:
                rep.log(f"Couldn't write bad-sector map: {e}")
        elif res["initial_bad"]:
            rep.log("All unreadable sectors were recovered on retry.")

        if kind == "PS1":
            cue_path = os.path.join(out_dir, name + ".cue")
            with open(cue_path, "w", newline="") as f:
                f.write(build_cue(name + ext, source.tracks))
            rep.log(f"Wrote {os.path.basename(cue_path)} ({len(source.tracks)} track(s))")

        do_verify = opts["verify"] and mode != "quick"
        if mode == "verify_only":
            do_verify = True

        if do_verify:
            if remaining:
                verify_note = "skipped (the disc has unreadable sectors)"
            else:
                ok, verify_note = verify_dump(source, out_path, res["md5"], rep, cancel)
            rep.log(f"Verify: {verify_note}")

        if kind != "PS1" and opts["redump"]:
            if not os.path.isfile(opts["redump"]):
                redump_note = "datfile not found"
            else:
                try:
                    hit = redump_lookup(opts["redump"], res["md5"], res["sha1"], total_bytes)
                    if hit:
                        redump_note = f"MATCH - {hit}"
                        redump_name = hit
                    else:
                        redump_note = "no match (different region/revision, or the dump has errors)"
                except (ET.ParseError, OSError) as e:
                    redump_note = f"couldn't read the datfile ({e})"
            rep.log(f"Redump: {redump_note}")

            # Automatic Redump renaming
            if redump_name and opts.get("auto_rename", True) and not remaining:
                new_base = safe_name(redump_name)
                new_path = os.path.join(out_dir, new_base + ext)
                if new_path != out_path and not os.path.exists(new_path):
                    try:
                        os.rename(out_path, new_path)
                        out_path = new_path
                        name = new_base
                        log_path = os.path.join(out_dir, name + ".log")
                        if os.path.exists(bad_path):
                            new_bad = os.path.join(out_dir, name + ".bad")
                            os.rename(bad_path, new_bad)
                            bad_path = new_bad
                        rep.log(f"Renamed to Redump name: {os.path.basename(out_path)}")
                    except OSError as e:
                        rep.log(f"Could not rename to Redump name: {e}")

        if opts["chd"] and mode != "quick":
            chdman = find_chdman(opts["chdman"])
            if not chdman:
                chd_note = "skipped (chdman.exe not found - set its path under Optional files)"
            else:
                chd_path, err = convert_to_chd(chdman, out_path, kind, rep, cancel)
                if err:
                    chd_note = err
                else:
                    ratio = os.path.getsize(chd_path) / max(os.path.getsize(out_path), 1) * 100
                    chd_note = f"{os.path.basename(chd_path)} ({ratio:.0f}% of the ISO size)"
            rep.log(f"CHD: {chd_note}")

        elapsed = time.time() - t0
        pairs = [
            ("Date", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("Drive", f"{letter}: ({drive_model(letter)})"),
            ("Disc label", opts["label"]),
            ("Disc type", kind),
            ("Serial", opts["serial"] or "unknown"),
            ("Title", opts["title"] or "unknown"),
            ("Output file", out_path),
            ("Size", f"{total_bytes} bytes ({source.total_sectors} sectors of {source.sector_size})"),
            ("MD5", res["md5"]),
            ("SHA1", res["sha1"]),
            ("CRC32", f"{res['crc']:08x}"),
            ("Unreadable (1st)", res["initial_bad"]),
            ("Unreadable (end)", len(remaining)),
            ("Bad sectors", ", ".join(map(str, remaining[:100])) + (" ..." if len(remaining) > 100 else "")
             if remaining else "none"),
            ("Verify", verify_note),
            ("Redump", redump_note),
            ("CHD", chd_note),
            ("Mode", mode),
            ("Time taken", f"{int(elapsed // 60)}m {int(elapsed % 60)}s"),
        ]
        try:
            write_log(log_path, pairs, rep.lines)
            rep.log(f"Log saved: {log_path}")
        except OSError as e:
            rep.log(f"Couldn't write the log file: {e}")

        if remaining:
            return "warn", (f"Finished with {len(remaining)} unreadable sector(s). "
                            "Clean the disc and try again - the game may not boot.")
        if verify_note.startswith("FAILED"):
            return "warn", "Dump saved, but verification FAILED. See the log."
        return "ok", f"Done! Saved {os.path.basename(out_path)}"

    except Cancelled:
        if dump_done:
            rep.log("Cancelled (the dump itself had already finished and was kept).")
            return "warn", "Cancelled - the dump was already saved."
        rep.log("Cancelled. Partial file removed.")
        return "fail", "Cancelled."
    except DumpError as e:
        rep.log(str(e))
        return "fail", str(e)
    except PermissionError:
        msg = "Access denied. Close the program, then right-click it and choose 'Run as administrator'."
        rep.log(msg)
        return "fail", msg
    except OSError as e:
        msg = f"Drive or file error: {e}"
        rep.log(msg)
        return "fail", msg
    finally:
        if source:
            source.close()


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.cfg = load_config()
        self.cancel = threading.Event()
        self.running = False
        self.closing = False
        self.closed = False
        self.disc = None
        self.titles_cache = (None, {})
        self.last_dir = None
        self.dark = self.cfg.get("dark", False)
        self.build_ui()
        self.apply_theme()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_drives(auto=True)
        self.poll()

        # Drag-and-drop support (Windows)
        if IS_WINDOWS:
            try:
                self.root.drop_target_register = getattr(self.root, "drop_target_register", None)
                # tkinterdnd2 is not assumed; we use a simple bind for file drop via shell
                self.root.bind("<Drop>", self.on_drop)
            except Exception:
                pass

    # ---- theme -----------------------------------------------------------
    def apply_theme(self):
        style = ttk.Style()
        if self.dark:
            # Pure white text on dark backgrounds
            bg, fg = "#1e1e1e", "#ffffff"
            entry_bg, btn_bg = "#2d2d2d", "#3c3c3c"
            select = "#0e639c"
            style.theme_use("clam")
            style.configure(".", background=bg, foreground=fg, fieldbackground=entry_bg)
            style.configure("TLabel", background=bg, foreground=fg)
            style.configure("Muted.TLabel", background=bg, foreground=fg)
            style.configure("TFrame", background=bg)
            style.configure("TLabelframe", background=bg, foreground=fg)
            style.configure("TLabelframe.Label", background=bg, foreground=fg)
            style.configure("TButton", background=btn_bg, foreground=fg)
            style.map("TButton",
                      foreground=[("disabled", fg), ("!disabled", fg)],
                      background=[("active", "#4a4a4a"), ("disabled", "#2a2a2a")])
            style.configure("TCheckbutton", background=bg, foreground=fg)
            style.map("TCheckbutton",
                      foreground=[("disabled", fg), ("!disabled", fg)],
                      background=[("active", bg)])
            style.configure("TRadiobutton", background=bg, foreground=fg)
            style.map("TRadiobutton",
                      foreground=[("disabled", fg), ("!disabled", fg)],
                      background=[("active", bg)])
            style.configure("TEntry", fieldbackground=entry_bg, foreground=fg, insertcolor=fg)
            style.map("TEntry",
                      fieldbackground=[("disabled", "#252525"), ("readonly", entry_bg)],
                      foreground=[("disabled", fg), ("!disabled", fg)])
            style.configure("TCombobox", fieldbackground=entry_bg, foreground=fg,
                            background=btn_bg, arrowcolor=fg)
            style.map("TCombobox",
                      fieldbackground=[("readonly", entry_bg), ("disabled", "#252525")],
                      foreground=[("readonly", fg), ("disabled", fg), ("!disabled", fg)],
                      selectbackground=[("readonly", select)],
                      selectforeground=[("readonly", fg)])
            style.configure("Horizontal.TProgressbar", background=select, troughcolor="#333333")
            self.root.configure(bg=bg)
            self.log_box.configure(bg="#111111", fg=fg, insertbackground=fg,
                                   selectbackground=select, selectforeground=fg)
            if getattr(self, "author_label", None) is not None:
                self.author_label.configure(style="Muted.TLabel")
            if getattr(self, "status_label", None) is not None:
                try:
                    current = str(self.status_label.cget("foreground") or "").lower()
                except Exception:
                    current = ""
                # Only leave special status colours; everything else pure white
                if current not in ("#3dd68c", "#f0a202", "#ff6b6b"):
                    self.status_label.configure(foreground=fg)
        else:
            style.theme_use("vista" if "vista" in style.theme_names() else "clam")
            style.configure(".", foreground="black")
            style.configure("TLabel", foreground="black")
            style.configure("Muted.TLabel", foreground="#6b6b6b")
            style.configure("TLabelframe", foreground="black")
            style.configure("TLabelframe.Label", foreground="black")
            style.configure("TButton", foreground="black")
            style.configure("TCheckbutton", foreground="black")
            style.configure("TRadiobutton", foreground="black")
            style.configure("TEntry", foreground="black")
            style.configure("TCombobox", foreground="black")
            self.root.configure(bg="SystemButtonFace")
            self.log_box.configure(bg="white", fg="black", insertbackground="black",
                                   selectbackground="#0078d7", selectforeground="white")
            if getattr(self, "author_label", None) is not None:
                self.author_label.configure(style="Muted.TLabel")
            if getattr(self, "status_label", None) is not None:
                try:
                    current = str(self.status_label.cget("foreground") or "").lower()
                except Exception:
                    current = ""
                if current in ("#f0f0f0", "#d4d4d4", "#ffffff", "#e8e8e8"):
                    self.status_label.configure(foreground="")

    def toggle_theme(self):
        self.dark = not self.dark
        self.cfg["dark"] = self.dark
        self.apply_theme()
        self.save_settings()

    # ---- layout ----------------------------------------------------------
    def build_ui(self):
        r = self.root
        r.title(f"PS2 Disc Dumper v{VERSION} - created by {AUTHOR}")
        r.minsize(720, 780)
        cfg = self.cfg

        head = ttk.Frame(r)
        head.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(head, text="PS2 Disc Dumper", font=("Segoe UI", 17, "bold")).pack(side="left", anchor="w")
        ttk.Button(head, text="Dark / Light", command=self.toggle_theme, width=12).pack(side="right", padx=4)
        self.author_label = ttk.Label(head, text=f"Created by {AUTHOR}", style="Muted.TLabel")
        self.author_label.pack(side="left", padx=12)

        # 1. Disc
        disc = ttk.LabelFrame(r, text="1. Disc")
        disc.pack(fill="x", padx=12, pady=4)
        ttk.Label(disc, text="Drive:").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.drive_var = tk.StringVar()
        self.drive_box = ttk.Combobox(disc, textvariable=self.drive_var, width=6, state="readonly")
        self.drive_box.grid(row=0, column=1, pady=6, sticky="w")
        self.drive_box.bind("<<ComboboxSelected>>", lambda e: self.scan_disc())
        self.refresh_btn = ttk.Button(disc, text="Refresh", command=self.refresh_drives)
        self.refresh_btn.grid(row=0, column=2, padx=8)
        self.disc_text = tk.StringVar(value="Looking for drives...")
        ttk.Label(disc, textvariable=self.disc_text, justify="left").grid(
            row=1, column=0, columnspan=3, padx=8, pady=(0, 8), sticky="w")

        # 2. Save location
        save = ttk.LabelFrame(r, text="2. Save to")
        save.pack(fill="x", padx=12, pady=4)
        save.columnconfigure(1, weight=1)
        default_dir = os.path.join(os.path.expanduser("~"), "Documents", "PS2 Dumps")
        self.dir_var = tk.StringVar(value=cfg.get("save_dir", default_dir))
        self.name_var = tk.StringVar()
        self.ext_text = tk.StringVar(value=".iso")
        ttk.Label(save, text="Folder:").grid(row=0, column=0, padx=8, pady=4, sticky="w")
        ttk.Entry(save, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(save, text="Browse...", command=self.browse_dir).grid(row=0, column=2, padx=8)
        ttk.Label(save, text="File name:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        ttk.Entry(save, textvariable=self.name_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(save, textvariable=self.ext_text, width=14).grid(row=1, column=2, padx=8, sticky="w")

        # Multi-disc helper
        multi = ttk.Frame(save)
        multi.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 4))
        ttk.Label(multi, text="Multi-disc:").pack(side="left")
        self.disc_num_var = tk.StringVar(value="")
        for label, val in (("—", ""), ("Disc 1", " (Disc 1)"), ("Disc 2", " (Disc 2)"),
                           ("Disc 3", " (Disc 3)"), ("Disc 4", " (Disc 4)")):
            ttk.Radiobutton(multi, text=label, variable=self.disc_num_var, value=val,
                            command=self.update_name_from_disc).pack(side="left", padx=4)

        # 3. Options
        opt = ttk.LabelFrame(r, text="3. Options")
        opt.pack(fill="x", padx=12, pady=4)
        self.verify_var = tk.BooleanVar(value=cfg.get("verify", True))
        self.chd_var = tk.BooleanVar(value=cfg.get("chd", False))
        self.sound_var = tk.BooleanVar(value=cfg.get("sound", True))
        self.auto_rename_var = tk.BooleanVar(value=cfg.get("auto_rename", True))
        ttk.Checkbutton(opt, text="Verify the dump afterwards (re-reads the disc; takes extra time)",
                        variable=self.verify_var).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(opt, text="Also convert to CHD (smaller file for PCSX2; needs chdman.exe)",
                        variable=self.chd_var).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(opt, text="Auto-rename to Redump name when a match is found",
                        variable=self.auto_rename_var).pack(anchor="w", padx=8, pady=2)
        ttk.Checkbutton(opt, text="Play a sound when finished",
                        variable=self.sound_var).pack(anchor="w", padx=8, pady=(2, 4))

        # Mode radio
        mode_frame = ttk.Frame(opt)
        mode_frame.pack(anchor="w", padx=8, pady=(2, 6))
        ttk.Label(mode_frame, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value=cfg.get("mode", "full"))
        for text, val in (("Full (verify + recovery)", "full"),
                          ("Dump & Verify only", "verify_only"),
                          ("Quick dump", "quick")):
            ttk.Radiobutton(mode_frame, text=text, variable=self.mode_var, value=val).pack(side="left", padx=6)

        # Optional files
        extra = ttk.LabelFrame(r, text="Optional files (all can be left empty) — drag & drop .dat / .yaml here")
        extra.pack(fill="x", padx=12, pady=4)
        extra.columnconfigure(1, weight=1)
        self.dat_var = tk.StringVar(value=cfg.get("redump_dat", ""))
        self.gi_var = tk.StringVar(value=cfg.get("gameindex", ""))
        self.chdman_var = tk.StringVar(value=cfg.get("chdman", ""))
        rows = [
            ("Redump datfile (.dat):", self.dat_var, "Choose a Redump PS2 datfile",
             [("Datfile", "*.dat *.xml"), ("All files", "*.*")], False),
            ("PCSX2 GameIndex.yaml:", self.gi_var, "Choose PCSX2's GameIndex.yaml",
             [("YAML", "*.yaml *.yml"), ("All files", "*.*")], True),
            ("chdman.exe:", self.chdman_var, "Choose chdman.exe",
             [("Program", "*.exe"), ("All files", "*.*")], False),
        ]
        for i, (text, var, title, types, rescan) in enumerate(rows):
            ttk.Label(extra, text=text).grid(row=i, column=0, padx=8, pady=3, sticky="w")
            ttk.Entry(extra, textvariable=var).grid(row=i, column=1, sticky="ew", pady=3)
            ttk.Button(extra, text="Browse...", command=lambda v=var, t=title, ty=types, rs=rescan:
                       self.browse_file(v, t, ty, rs)).grid(row=i, column=2, padx=8)

        # Buttons + progress
        btns = ttk.Frame(r)
        btns.pack(fill="x", padx=12, pady=(8, 2))
        self.start_btn = ttk.Button(btns, text="Start dump", command=self.on_start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.folder_btn = ttk.Button(btns, text="Open folder", command=self.open_folder, state="disabled")
        self.folder_btn.pack(side="left")
        self.copy_btn = ttk.Button(btns, text="Copy log", command=self.copy_log)
        self.copy_btn.pack(side="left", padx=8)
        self.html_btn = ttk.Button(btns, text="Export HTML log", command=self.export_html_log, state="disabled")
        self.html_btn.pack(side="left")

        self.bar = ttk.Progressbar(r, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=12, pady=(6, 2))
        self.status = tk.StringVar(value="Ready.")
        self.status_label = ttk.Label(r, textvariable=self.status)
        self.status_label.pack(anchor="w", padx=12)

        self.log_box = scrolledtext.ScrolledText(r, height=10, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(6, 12))

        self.last_log_pairs = []
        self.last_status = "ok"

    # ---- small UI actions ------------------------------------------------
    def browse_dir(self):
        path = filedialog.askdirectory(title="Choose where to save dumps", initialdir=self.dir_var.get() or None)
        if path:
            self.dir_var.set(os.path.normpath(path))

    def browse_file(self, var, title, types, rescan):
        path = filedialog.askopenfilename(title=title, filetypes=types)
        if path:
            var.set(os.path.normpath(path))
            if rescan:
                self.scan_disc()

    def open_folder(self):
        if self.last_dir and os.path.isdir(self.last_dir):
            os.startfile(self.last_dir)

    def copy_log(self):
        text = self.log_box.get("1.0", "end-1c")
        if text.strip():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.set("Log copied to clipboard.")

    def export_html_log(self):
        if not self.last_log_pairs:
            messagebox.showinfo("Export", "No log available yet.")
            return
        path = filedialog.asksaveasfilename(
            title="Save HTML log", defaultextension=".html",
            filetypes=[("HTML", "*.html"), ("All files", "*.*")],
            initialfile="dump_log.html")
        if path:
            try:
                write_html_log(path, self.last_log_pairs, [], self.last_status)
                # also dump the transcript from the text box
                transcript = self.log_box.get("1.0", "end-1c").splitlines()
                write_html_log(path, self.last_log_pairs, transcript, self.last_status)
                self.status.set(f"HTML log saved: {os.path.basename(path)}")
            except OSError as e:
                messagebox.showerror("Export", str(e))

    def append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def save_settings(self):
        self.cfg.update({
            "save_dir": self.dir_var.get().strip(), "verify": self.verify_var.get(),
            "chd": self.chd_var.get(), "sound": self.sound_var.get(),
            "redump_dat": self.dat_var.get().strip(), "gameindex": self.gi_var.get().strip(),
            "chdman": self.chdman_var.get().strip(),
            "auto_rename": self.auto_rename_var.get(),
            "mode": self.mode_var.get(),
            "dark": self.dark,
            "last_drive": self.drive_var.get(),
        })
        save_config(self.cfg)

    def set_controls(self, idle):
        state = ["!disabled"] if idle else ["disabled"]
        self.start_btn.state(state if self.disc else ["disabled"])
        self.drive_box.configure(state="readonly" if idle else "disabled")
        self.refresh_btn.state(state)
        self.cancel_btn.state(["disabled"] if idle else ["!disabled"])

    def update_name_from_disc(self):
        if not self.disc:
            return
        base = self.disc.get("title") or self.disc.get("label") or "disc"
        serial = self.disc.get("serial")
        suffix = self.disc_num_var.get()
        name = safe_name(base + (f" [{serial}]" if serial else "") + suffix)
        self.name_var.set(name)

    def on_drop(self, event):
        """Handle simple file drops (Windows shell)."""
        try:
            data = event.data
            if not data:
                return
            # tk may give {path} or path
            path = data.strip("{}").strip()
            if not os.path.isfile(path):
                return
            low = path.lower()
            if low.endswith((".dat", ".xml")):
                self.dat_var.set(path)
                self.append_log(f"Loaded Redump datfile: {os.path.basename(path)}")
            elif low.endswith((".yaml", ".yml")):
                self.gi_var.set(path)
                self.append_log(f"Loaded GameIndex: {os.path.basename(path)}")
                self.scan_disc()
        except Exception:
            pass

    # ---- drive / disc scanning ------------------------------------------
    def refresh_drives(self, auto=False):
        if self.running:
            return
        drives = [f"{d}:" for d in optical_drives()]
        self.drive_box["values"] = drives
        if drives:
            last = self.cfg.get("last_drive", "")
            if auto and last in drives:
                self.drive_var.set(last)
            elif self.drive_var.get() not in drives:
                self.drive_var.set(drives[0])
            self.scan_disc()
        else:
            self.drive_var.set("")
            self.disc = None
            self.disc_text.set("No optical drive found.")
            self.start_btn.state(["disabled"])

    def scan_disc(self):
        letter = self.drive_var.get().rstrip(":")
        if not letter or self.running:
            return
        self.disc = None
        self.start_btn.state(["disabled"])
        self.disc_text.set("Reading disc...")
        gi = self.gi_var.get().strip() or find_gameindex() or ""
        threading.Thread(target=self._scan_worker, args=(letter, gi), daemon=True).start()

    def _scan_worker(self, letter, gi_path):
        info = {"letter": letter}
        label, _ = volume_info(f"{letter}:\\")
        if label is None:
            info["error"] = ("No readable disc in this drive.\n"
                             "Insert a disc and press Refresh. (DVD drives can't read Blu-ray/PS3 discs.)")
            self.q.put(("disc", info))
            return
        kind, serial = identify_disc(f"{letter}:\\")
        info.update(label=label, kind=kind, serial=serial, title=None, size=None)
        if gi_path and os.path.isfile(gi_path):
            if self.titles_cache[0] != gi_path:
                try:
                    self.titles_cache = (gi_path, load_titles(gi_path))
                except OSError:
                    self.titles_cache = (gi_path, {})
            if serial:
                info["title"] = self.titles_cache[1].get(serial)
                # also try a few common variants
                if not info["title"]:
                    for alt in (serial.replace("-", "_"), serial.replace("-", ""), serial.replace("-", ".")):
                        info["title"] = self.titles_cache[1].get(normalize_serial(alt))
                        if info["title"]:
                            break
        if kind != "PS3":
            try:
                src = CdRawSource(letter) if kind == "PS1" else RawVolumeSource(letter)
                info["size"] = src.total_sectors * src.sector_size
                src.close()
            except OSError:
                info["size"] = None
        self.q.put(("disc", info))

    def on_disc(self, info):
        if info["letter"] != self.drive_var.get().rstrip(":"):
            return  # stale result
        if "error" in info:
            self.disc = None
            self.disc_text.set(info["error"])
            return
        self.disc = info
        kind = info["kind"]
        names = {"PS2": "PlayStation 2", "PS1": "PlayStation 1 (BIN/CUE, experimental)",
                 "PS3": "PlayStation 3 - not supported", "UNKNOWN": "Unknown (not recognised as a PlayStation disc)"}
        lines = [f"Label: {info['label']}", f"Type: {names[kind]}"]
        if info["serial"]:
            lines.append(f"Serial: {info['serial']}")
        if info["title"]:
            lines.append(f"Title: {info['title']}")
        if info["size"]:
            lines.append(f"Size: {info['size'] / 1073741824:.2f} GB")
        if kind == "PS3":
            lines.append("PS3 discs are encrypted. Use ps3-disc-dumper with a Blu-ray drive instead.")
        self.disc_text.set("\n".join(lines))
        self.update_name_from_disc()
        self.ext_text.set(".bin + .cue" if kind == "PS1" else ".iso")
        if kind != "PS3" and not self.running:
            self.start_btn.state(["!disabled"])

    # ---- the dump --------------------------------------------------------
    def on_start(self):
        d = self.disc
        if not d or self.running:
            return
        if d["kind"] == "PS3":
            messagebox.showinfo("PS3 disc", "PS3 discs can't be dumped with this tool.")
            return
        if d["kind"] == "UNKNOWN" and not messagebox.askyesno(
                "Not a PlayStation disc?", "This disc wasn't recognised as a PlayStation disc. Dump it anyway as an ISO?"):
            return
        out_dir = self.dir_var.get().strip()
        if not out_dir:
            messagebox.showerror("Save folder", "Please choose a folder to save into.")
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Save folder", f"Can't use that folder:\n{e}")
            return
        name = safe_name(self.name_var.get())
        ext = ".bin" if d["kind"] == "PS1" else ".iso"
        if os.path.exists(os.path.join(out_dir, name + ext)) and not messagebox.askyesno(
                "File exists", f"{name}{ext} already exists. Overwrite it?"):
            return
        self.save_settings()
        opts = {
            "letter": d["letter"], "kind": d["kind"], "label": d["label"], "serial": d["serial"],
            "title": d["title"], "out_dir": out_dir, "name": name,
            "verify": self.verify_var.get(), "chd": self.chd_var.get(),
            "redump": self.dat_var.get().strip(), "chdman": self.chdman_var.get().strip(),
            "auto_rename": self.auto_rename_var.get(),
            "mode": self.mode_var.get(),
        }
        self.last_dir = out_dir
        self.cancel.clear()
        self.running = True
        self.set_controls(False)
        self.folder_btn.state(["disabled"])
        self.html_btn.state(["disabled"])
        self.bar["value"] = 0
        self.status_label.configure(foreground="#ffffff" if self.dark else "")
        self.status.set("Starting...")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        threading.Thread(target=self._job_worker, args=(opts,)).start()

    def _job_worker(self, opts):
        rep = Reporter(self.q)
        try:
            status, msg = run_job(opts, rep, self.cancel)
        except Exception:
            rep.log(traceback.format_exc())
            status, msg = "fail", "Unexpected error - see the log below."
        self.q.put(("done", status, msg, list(rep.lines)))

    def on_cancel(self):
        if self.running:
            self.cancel.set()
            self.status.set("Cancelling...")

    def on_progress(self, label, done, total, start):
        pct = done / total * 100 if total else 0
        elapsed = max(time.time() - start, 0.001)
        speed = done / elapsed
        eta = (total - done) / speed if speed else 0
        self.bar["value"] = pct
        # clearer status line
        if "Recovery" in label:
            self.status.set(f"{label}: {done}/{total} sectors   {pct:.0f}%")
        else:
            self.status.set(f"{label}: {pct:.1f}%   {speed / 1048576:.1f} MB/s   "
                            f"ETA {int(eta // 60)}m{int(eta % 60):02d}s")

    def on_done(self, status, msg, lines=None):
        self.running = False
        if self.closing:
            self.closed = True
            self.root.destroy()
            return
        self.set_controls(True)
        self.folder_btn.state(["!disabled"])
        self.html_btn.state(["!disabled"])
        # Status colours stay distinct; default UI text is pure white in dark mode
        if self.dark:
            colors = {"ok": "#3dd68c", "warn": "#f0a202", "fail": "#ff6b6b"}
        else:
            colors = {"ok": "#1a7f37", "warn": "#b35900", "fail": "#c62828"}
        self.status_label.configure(foreground=colors.get(status, "#ffffff" if self.dark else ""))
        self.status.set(msg)
        if status != "fail":
            self.bar["value"] = 100
        if self.sound_var.get():
            play_sound(status)
        self.last_status = status
        # keep pairs for HTML export (best-effort from last run)
        self.last_log_pairs = [
            ("Status", status),
            ("Message", msg),
        ]

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Dump in progress", "Cancel the dump and exit?"):
                return
            self.closing = True
            self.cancel.set()
            self.status.set("Cancelling...")
            return
        self.save_settings()
        self.closed = True
        self.root.destroy()

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.append_log(msg[1])
                elif kind == "progress":
                    self.on_progress(*msg[1:])
                elif kind == "pct":
                    self.bar["value"] = msg[2]
                    self.status.set(f"{msg[1]}: {msg[2]:.1f}%")
                elif kind == "disc":
                    self.on_disc(msg[1])
                elif kind == "done":
                    self.on_done(msg[1], msg[2], msg[3] if len(msg) > 3 else None)
                if self.closed:
                    return
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    if not IS_WINDOWS:
        print("This tool only runs on Windows.")
        return
    if tk is None:
        print("Python's tkinter is missing. Re-run the Python installer and tick 'tcl/tk and IDLE'.")
        input("Press Enter to close...")
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
