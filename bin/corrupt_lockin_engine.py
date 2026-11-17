#!/usr/bin/env python3
"""OpenGL 3.3 + libmpv Veil-Rift, Bad-Sector & Ghostly GPU Game-of-Life Engine.

Implements:
1. Clean outer emerald hacker video (`corrupt-lockin-8VsvdCaSyaQ-permanent.mp4`) with EWMH
   window-aware ICE (`_NET_CLIENT_LIST_STACKING`) and spreading Gen 1B Bad Sectors.
2. Decoupled 10.0-second `RiftField` (`R`=linear distance profile, `G`=10s age, `B`=stroke seed,
   `A`=2.4s arc) with scalpel-vs-cleaver width, suture-zipper smart merge, cursor dwell
   6.0s `[tDd//WATCHDOG]` bite micro-rifts, burst `+++ATH0 // NO CARRIER` flashes, and
   `+3.0s` scar-remap sector events.
3. 100% GPU-native ping-pong Conway's Game of Life shader (`corrupt-life-step.frag`, 640x360)
   clinging ghostly along rift lips, bad sectors, and window borders.
4. Optional Light GPU Raytracing mode (`~/.config/wallpaper-mode/raytrace` -> `[RT:3D]` / `[2D]`)
   and crisp top-left & top-right `tDd` BBS telemetry HUD boxes.
"""
from __future__ import annotations

import ctypes
import math
import os
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# Automatically enable NVIDIA PRIME render offload when an NVIDIA discrete GPU is present
if os.environ.get("WALLPAPER_DISABLE_NVIDIA_OFFLOAD") != "1" and Path("/proc/driver/nvidia/version").is_file():
    os.environ.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
    os.environ.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")

import cairo  # noqa: E402
import gi  # noqa: E402
import numpy as np  # noqa: E402

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk  # noqa: E402


STATE_DIR = Path.home() / ".config/wallpaper-mode"
RAYTRACE_FILE = STATE_DIR / "raytrace"
SHADER_DIR = Path.home() / ".local/share/wallpaper-mode"
GL_C_SOURCE = SHADER_DIR / "corrupt_gl_renderer.c"
VK_RT_C_SOURCE = SHADER_DIR / "vk_rt5070.c"
VK_RT_H_SOURCE = SHADER_DIR / "vk_rt5070.h"
VK_RT_COMP_SOURCE = SHADER_DIR / "corrupt-rt5070.comp"
VK_RT_SPV_PATH = SHADER_DIR / "corrupt-rt5070.spv"
GL_SO_PATH = SHADER_DIR / "corrupt_gl_renderer.so"
VEIL_FRAG_PATH = SHADER_DIR / "corrupt-veil.frag"
LIFE_FRAG_PATH = SHADER_DIR / "corrupt-life-step.frag"

LOCKIN_MANTRAS: tuple[str, ...] = (
    "CARRIER_LOCKED // NO ESCAPE UNTIL SHIP",
    "LATRANDO, VULNERA INVENIO // 0xDEAD:0xBEEF",
    "SIGNAL_SYNC: LOCKED // DEEP WORK ACTIVE",
    "BAD_SECTOR_SCAN // WATCHDOG_ICE ONLINE",
    "ATDT 2400 // UNDERGROUND BBS HANDSHAKE OK",
    "SECTOR 0xDEADFA17 // MEMORY MAP GUARDED",
    "BITE THE HAND THAT PATCHES SLOW // LOCKED",
)

GLITCH_CHARS = "#$%&*+?!~^=:;/\\|<>[]{}0123456789ABCDEF"
TRUTHY_STRINGS = frozenset({"1", "on", "true", "yes"})


def read_raytrace_config() -> bool:
    """Reads `~/.config/wallpaper-mode/raytrace` (`on` if 1/on/true/yes, else `off`)."""
    try:
        raw = RAYTRACE_FILE.read_text(encoding="ascii", errors="ignore").strip().lower()
    except OSError:
        return False
    return raw in TRUTHY_STRINGS


class _NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("c_class", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


_XErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class X11WindowScanner:
    """Lightweight pure-ctypes EWMH scanner for visible normal X11 windows on the active desktop."""

    IS_VIEWABLE = 2
    INPUT_OUTPUT = 1
    ANY_PROPERTY_TYPE = 0

    def __init__(self, ignore_xids: set[int] | None = None) -> None:
        self.ignore_xids: set[int] = set(ignore_xids or ())
        self._x11: ctypes.CDLL | None = None
        self._dpy: int | None = None
        self._root: int = 0
        self._err_handler = _XErrorHandler(self._ignore_x_error)
        self._atoms: dict[str, int] = {}
        self._init_x11()

    @staticmethod
    def _ignore_x_error(_dpy: int, _err: int) -> int:
        return 0

    def _init_x11(self) -> None:
        try:
            x11 = ctypes.CDLL("libX11.so.6")
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.restype = ctypes.c_int
            x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
            x11.XDefaultScreen.restype = ctypes.c_int
            x11.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            x11.XRootWindow.restype = ctypes.c_ulong
            x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XGetWindowProperty.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_long,
                ctypes.c_long,
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            x11.XGetWindowProperty.restype = ctypes.c_int
            x11.XGetWindowAttributes.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(_XWindowAttributes),
            ]
            x11.XGetWindowAttributes.restype = ctypes.c_int
            x11.XTranslateCoordinates.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong),
            ]
            x11.XTranslateCoordinates.restype = ctypes.c_int
            x11.XFree.argtypes = [ctypes.c_void_p]
            x11.XFree.restype = ctypes.c_int
            x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
            x11.XSync.restype = ctypes.c_int
            x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
            x11.XSetErrorHandler.restype = ctypes.c_void_p

            dpy = x11.XOpenDisplay(None)
            if not dpy:
                return
            scr = x11.XDefaultScreen(dpy)
            root = x11.XRootWindow(dpy, scr)

            atom_names = (
                "_NET_CLIENT_LIST_STACKING",
                "_NET_CLIENT_LIST",
                "_NET_CURRENT_DESKTOP",
                "_NET_WM_DESKTOP",
                "_NET_WM_WINDOW_TYPE",
                "_NET_WM_WINDOW_TYPE_DESKTOP",
                "_NET_WM_WINDOW_TYPE_DOCK",
                "_NET_WM_WINDOW_TYPE_TOOLTIP",
                "_NET_WM_WINDOW_TYPE_NOTIFICATION",
                "_NET_WM_STATE",
                "_NET_WM_STATE_HIDDEN",
            )
            for name in atom_names:
                self._atoms[name] = int(x11.XInternAtom(dpy, name.encode("ascii"), 0))

            self._x11 = x11
            self._dpy = dpy
            self._root = int(root)
        except Exception:
            self._x11 = None
            self._dpy = None

    def close(self) -> None:
        if self._x11 is not None and self._dpy:
            try:
                self._x11.XCloseDisplay(self._dpy)
            except Exception:
                pass
        self._dpy = None
        self._x11 = None

    def _get_ulong_prop(self, win: int, atom: int, max_items: int = 256) -> list[int]:
        if self._x11 is None or not self._dpy or not atom:
            return []
        actual_type = ctypes.c_ulong(0)
        actual_format = ctypes.c_int(0)
        nitems = ctypes.c_ulong(0)
        bytes_after = ctypes.c_ulong(0)
        prop_ptr = ctypes.c_void_p(0)

        rc = self._x11.XGetWindowProperty(
            self._dpy,
            ctypes.c_ulong(win),
            ctypes.c_ulong(atom),
            0,
            max_items,
            0,
            self.ANY_PROPERTY_TYPE,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(nitems),
            ctypes.byref(bytes_after),
            ctypes.byref(prop_ptr),
        )
        if rc != 0 or not prop_ptr.value:
            return []
        try:
            count = int(nitems.value)
            if count <= 0 or actual_format.value != 32:
                return []
            arr_type = ctypes.c_ulong * count
            arr = ctypes.cast(prop_ptr.value, ctypes.POINTER(arr_type)).contents
            return [int(arr[i]) for i in range(count)]
        finally:
            self._x11.XFree(prop_ptr)

    def query_rects(
        self,
        win_x: int,
        win_y: int,
        width: int,
        height: int,
        max_rects: int = 8,
    ) -> list[tuple[float, float, float, float]]:
        """Returns up to `max_rects` normalized `(x0_uv, y0_uv, x1_uv, y1_uv)` window rects."""
        if self._x11 is None or not self._dpy or width <= 0 or height <= 0:
            return []

        prev_handler = None
        try:
            prev_handler = self._x11.XSetErrorHandler(
                ctypes.cast(self._err_handler, ctypes.c_void_p)
            )
            cur_desk_vals = self._get_ulong_prop(
                self._root, self._atoms["_NET_CURRENT_DESKTOP"], 4
            )
            cur_desktop = cur_desk_vals[0] if cur_desk_vals else None

            clients = self._get_ulong_prop(
                self._root, self._atoms["_NET_CLIENT_LIST_STACKING"], 256
            )
            if not clients:
                clients = self._get_ulong_prop(self._root, self._atoms["_NET_CLIENT_LIST"], 256)
            if not clients:
                return []

            skip_types = {
                self._atoms["_NET_WM_WINDOW_TYPE_DESKTOP"],
                self._atoms["_NET_WM_WINDOW_TYPE_DOCK"],
                self._atoms["_NET_WM_WINDOW_TYPE_TOOLTIP"],
                self._atoms["_NET_WM_WINDOW_TYPE_NOTIFICATION"],
            }
            hidden_atom = self._atoms["_NET_WM_STATE_HIDDEN"]
            wm_desktop_atom = self._atoms["_NET_WM_DESKTOP"]
            wm_type_atom = self._atoms["_NET_WM_WINDOW_TYPE"]
            wm_state_atom = self._atoms["_NET_WM_STATE"]

            rects: list[tuple[float, float, float, float]] = []
            attrs = _XWindowAttributes()
            rx = ctypes.c_int(0)
            ry = ctypes.c_int(0)
            child_ret = ctypes.c_ulong(0)

            # Iterate topmost-first from _NET_CLIENT_LIST_STACKING
            for xid in reversed(clients):
                if not xid or xid in self.ignore_xids:
                    continue

                if cur_desktop is not None:
                    w_desk = self._get_ulong_prop(xid, wm_desktop_atom, 2)
                    if w_desk and w_desk[0] not in (cur_desktop, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                        continue

                w_types = self._get_ulong_prop(xid, wm_type_atom, 8)
                if any(t in skip_types for t in w_types):
                    continue

                w_states = self._get_ulong_prop(xid, wm_state_atom, 12)
                if hidden_atom and hidden_atom in w_states:
                    continue

                if self._x11.XGetWindowAttributes(self._dpy, ctypes.c_ulong(xid), ctypes.byref(attrs)) == 0:
                    continue
                if (
                    attrs.override_redirect != 0
                    or attrs.map_state != self.IS_VIEWABLE
                    or attrs.c_class != self.INPUT_OUTPUT
                    or attrs.width < 48
                    or attrs.height < 36
                ):
                    continue

                if (
                    self._x11.XTranslateCoordinates(
                        self._dpy,
                        ctypes.c_ulong(xid),
                        ctypes.c_ulong(self._root),
                        0,
                        0,
                        ctypes.byref(rx),
                        ctypes.byref(ry),
                        ctypes.byref(child_ret),
                    )
                    == 0
                ):
                    continue

                x0 = (float(rx.value) - float(win_x)) / float(width)
                y0 = (float(ry.value) - float(win_y)) / float(height)
                x1 = (float(rx.value + attrs.width) - float(win_x)) / float(width)
                y1 = (float(ry.value + attrs.height) - float(win_y)) / float(height)

                x0_c = max(0.0, min(1.0, x0))
                y0_c = max(0.0, min(1.0, y0))
                x1_c = max(0.0, min(1.0, x1))
                y1_c = max(0.0, min(1.0, y1))
                if (x1_c - x0_c) < 0.025 or (y1_c - y0_c) < 0.025:
                    continue

                rects.append((round(x0_c, 5), round(y0_c, 5), round(x1_c, 5), round(y1_c, 5)))
                if len(rects) >= max_rects:
                    break

            self._x11.XSync(self._dpy, 0)
            return rects
        except Exception:
            return []
        finally:
            if self._x11 is not None and self._dpy and prev_handler is not None:
                try:
                    self._x11.XSync(self._dpy, 0)
                    self._x11.XSetErrorHandler(prev_handler)
                except Exception:
                    pass


@dataclass
class TelemetrySnapshot:
    cpu: float = 0.10
    cpu_peak_core: float = 0.15
    gpu: float = 0.05
    vram: float = 0.10
    gpu_temp_c: int = 40
    ram: float = 0.30
    burst: float = 0.08


class SystemTelemetry:
    """Zero-subprocess hardware load reader (/proc + AMD DRM sysfs + NVIDIA NVML ctypes)."""

    def __init__(self) -> None:
        self._prev_cpu: dict[str, tuple[int, int]] = {}
        self._nvml: ctypes.CDLL | None = None
        self._nvml_dev = ctypes.c_void_p()
        self._ema_cpu = 0.10
        self._ema_gpu = 0.06
        self._ema_ram = 0.30
        self._ema_vram = 0.10
        self._burst = 0.08
        self._init_nvml()
        self._read_cpu_raw()

    def _init_nvml(self) -> None:
        try:
            nvml = ctypes.CDLL("libnvidia-ml.so.1")
            if nvml.nvmlInit_v2() == 0:
                dev = ctypes.c_void_p()
                if nvml.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev)) == 0:
                    self._nvml = nvml
                    self._nvml_dev = dev
        except OSError:
            self._nvml = None

    def close(self) -> None:
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass
            self._nvml = None

    def _read_cpu_raw(self) -> tuple[float, float]:
        try:
            lines = Path("/proc/stat").read_text(encoding="ascii", errors="ignore").splitlines()
        except OSError:
            return self._ema_cpu, self._ema_cpu

        avg_load = 0.0
        core_loads: list[float] = []
        for line in lines:
            if not line.startswith("cpu"):
                break
            parts = line.split()
            name = parts[0]
            try:
                nums = [int(x) for x in parts[1:9]]
            except ValueError:
                continue
            if len(nums) < 4:
                continue
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            total = sum(nums)
            prev = self._prev_cpu.get(name)
            self._prev_cpu[name] = (idle, total)
            if prev is None:
                continue
            d_total = total - prev[1]
            d_idle = idle - prev[0]
            if d_total <= 0:
                continue
            busy = max(0.0, min(1.0, (d_total - d_idle) / d_total))
            if name == "cpu":
                avg_load = busy
            else:
                core_loads.append(busy)

        peak_core = max(core_loads) if core_loads else avg_load
        effective_cpu = max(avg_load, min(1.0, avg_load * 0.65 + peak_core * 0.35))
        return effective_cpu, peak_core

    def _read_ram_raw(self) -> float:
        total_kb = 0
        avail_kb = 0
        try:
            with open("/proc/meminfo", "r", encoding="ascii", errors="ignore") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    if total_kb and avail_kb:
                        break
        except (OSError, ValueError, IndexError):
            return self._ema_ram
        if total_kb <= 0:
            return self._ema_ram
        return max(0.0, min(1.0, (total_kb - avail_kb) / total_kb))

    def _read_gpu_raw(self) -> tuple[float, float, int]:
        nv_gpu = 0.0
        nv_vram = 0.0
        gpu_temp = 0
        if self._nvml is not None:
            try:
                util = _NvmlUtilization()
                mem = _NvmlMemory()
                temp = ctypes.c_uint()
                self._nvml.nvmlDeviceGetUtilizationRates(self._nvml_dev, ctypes.byref(util))
                self._nvml.nvmlDeviceGetMemoryInfo(self._nvml_dev, ctypes.byref(mem))
                self._nvml.nvmlDeviceGetTemperature(self._nvml_dev, 0, ctypes.byref(temp))
                nv_gpu = max(float(util.gpu), float(util.memory) * 0.6) / 100.0
                nv_vram = (float(mem.used) / float(mem.total)) if mem.total > 0 else 0.0
                gpu_temp = int(temp.value)
            except Exception:
                pass

        drm_gpu = 0.0
        for candidate in Path("/sys/class/drm").glob("card*/device/gpu_busy_percent"):
            try:
                busy = max(0.0, float(candidate.read_text().strip()) - 8.0) / 92.0
                drm_gpu = max(drm_gpu, min(1.0, busy))
            except (OSError, ValueError):
                continue

        return max(0.0, min(1.0, max(nv_gpu, drm_gpu))), max(0.0, min(1.0, nv_vram)), gpu_temp

    def sample(self) -> TelemetrySnapshot:
        raw_cpu, peak_core = self._read_cpu_raw()
        raw_ram = self._read_ram_raw()
        raw_gpu, raw_vram, gpu_temp = self._read_gpu_raw()

        jump = max(0.0, raw_cpu - self._ema_cpu) * 1.5 + max(0.0, raw_gpu - self._ema_gpu) * 1.5
        self._ema_cpu = self._ema_cpu * 0.65 + raw_cpu * 0.35
        self._ema_gpu = self._ema_gpu * 0.65 + raw_gpu * 0.35
        self._ema_ram = self._ema_ram * 0.80 + raw_ram * 0.20
        self._ema_vram = self._ema_vram * 0.75 + raw_vram * 0.25

        load_mix = 0.42 * self._ema_cpu + 0.36 * self._ema_gpu + 0.22 * self._ema_ram
        self._burst = max(0.0, min(1.0, self._burst * 0.84 + jump))
        if load_mix > 0.45 and random.random() < 0.22 * load_mix:
            self._burst = min(1.0, max(self._burst, 0.35 + 0.55 * load_mix))

        return TelemetrySnapshot(
            cpu=round(self._ema_cpu, 4),
            cpu_peak_core=round(peak_core, 4),
            gpu=round(self._ema_gpu, 4),
            vram=round(self._ema_vram, 4),
            gpu_temp_c=gpu_temp,
            ram=round(self._ema_ram, 4),
            burst=round(self._burst, 4),
        )


def _smoothstep_np(edge0: float, edge1: float, x: np.ndarray | float) -> np.ndarray | float:
    u = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def _tau_from_age(age: np.ndarray | float) -> np.ndarray | float:
    t = (1.0 - age) * 10.0
    h = _smoothstep_np(2.6, 9.6, t)
    return 0.10 + 0.90 * h * h * (3.0 - 2.0 * h)


class RiftField:
    """Decoupled 10.0-second healing digital tear field (`480x270`).

    Packed RGBA channels:
      - `R` (`profile`): linear spatial profile `clip(1.0 - dist / radius, 0.0, 1.0)` (zeroed at `age <= 0`).
      - `G` (`age`): `1.0` fresh -> decays linearly `age - dt / 10.0` to `0.0` over `10.0s`.
      - `B` (`seed`): per-stroke random seed (`0.05..0.95`, constant per continuous drag).
      - `A` (`arc`): `1.0` fresh -> cools linearly over `2.4s`.
    """

    HEAL_SECONDS = 10.0
    ARC_COOL_SECONDS = 2.4

    def __init__(self, grid_w: int = 480, grid_h: int = 270) -> None:
        self.gw = grid_w
        self.gh = grid_h
        self.profile = np.zeros((self.gh, self.gw), dtype=np.float32)
        self.age = np.zeros((self.gh, self.gw), dtype=np.float32)
        self.seed = np.zeros((self.gh, self.gw), dtype=np.float32)
        self.arc = np.zeros((self.gh, self.gw), dtype=np.float32)
        self.packed = np.zeros((self.gh, self.gw, 4), dtype=np.uint8)

        yy, xx = np.mgrid[0:self.gh, 0:self.gw]
        self._xx = xx.astype(np.float32)
        self._yy = yy.astype(np.float32)

        self.prev_uv: tuple[float, float] | None = None
        self.mouse_uv: tuple[float, float] = (0.5, 0.5)
        self.mouse_speed_norm: float = 0.0
        self.stroke_seed: float = round(random.uniform(0.05, 0.95), 4)
        self._idle_sec: float = 1.0
        self._stroke_arc_len: float = 0.0

    @property
    def scar(self) -> np.ndarray:
        """Backwards-compatible read view of the spatial profile channel."""
        return self.profile

    def update_decay(self, dt: float) -> bool:
        """Advances 10s age & 2.4s arc decay; returns True when an active scar seals (9.5s..10.0s)."""
        if dt <= 0.0:
            return False

        # Detect active scars entering/finishing the 9.5s..10.0s seal window (age crossing <= 0.04)
        was_above_seal = self.age > 0.04
        np.maximum(self.age - (dt / self.HEAL_SECONDS), 0.0, out=self.age)
        np.maximum(self.arc - (dt / self.ARC_COOL_SECONDS), 0.0, out=self.arc)

        sealed_pixels = int(np.count_nonzero(was_above_seal & (self.age <= 0.04)))

        dead = self.age <= 0.0
        if np.any(dead):
            self.profile[dead] = 0.0
            self.arc[dead] = 0.0

        self.mouse_speed_norm *= 0.80
        return sealed_pixels >= 10

    def stamp_segment(
        self,
        uv0: tuple[float, float],
        uv1: tuple[float, float],
        speed_norm: float,
        load_mix: float,
        *,
        stroke_seed: float | None = None,
        age_val: float = 1.0,
        radius_override: float | None = None,
        arc_start: float | None = None,
        allow_fork: bool = True,
    ) -> None:
        x0, y0 = uv0[0] * self.gw, uv0[1] * self.gh
        x1, y1 = uv1[0] * self.gw, uv1[1] * self.gh
        dx = x1 - x0
        dy = y1 - y0
        seg_len_sq = dx * dx + dy * dy
        seg_len = math.sqrt(seg_len_sq)

        # Scalpel-vs-cleaver base radius on 480x270 grid (slow = surgical incision, fast = wide chasm)
        if radius_override is not None:
            base_radius = float(radius_override)
        else:
            base_radius = min(46.0, 16.0 + 21.0 * float(speed_norm) + 12.0 * float(load_mix))

        seed_val = float(self.stroke_seed if stroke_seed is None else stroke_seed)
        s_arc0 = float(self._stroke_arc_len if arc_start is None else arc_start)

        tip_reach = base_radius * 1.75
        pad = int(math.ceil(tip_reach + 3.0))
        ix0 = max(0, int(min(x0, x1)) - pad)
        ix1 = min(self.gw, int(max(x0, x1)) + pad + 1)
        iy0 = max(0, int(min(y0, y1)) - pad)
        iy1 = min(self.gh, int(max(y0, y1)) + pad + 1)
        if ix0 >= ix1 or iy0 >= iy1:
            return

        sub_x = self._xx[iy0:iy1, ix0:ix1]
        sub_y = self._yy[iy0:iy1, ix0:ix1]

        if seg_len < 0.05:
            dist = np.hypot(sub_x - x1, sub_y - y1)
            ang = np.arctan2(sub_y - y1, sub_x - x1)
            star_mod = 0.72 + 0.28 * np.abs(np.cos(ang * 3.0 + seed_val * 19.0))
            new_d = np.clip(1.0 - dist / (base_radius * star_mod), 0.0, 1.0).astype(np.float32)
        else:
            inv_len = 1.0 / seg_len
            tx = dx * inv_len
            ty = dy * inv_len
            rx = sub_x - x0
            ry = sub_y - y0

            # Decompose into tangent coordinate s_along and perpendicular distance w_perp
            s_along = rx * tx + ry * ty
            w_perp = np.abs(-rx * ty + ry * tx)

            s_clamped = np.clip(s_along, 0.0, seg_len)
            arc_u = s_arc0 + s_clamped / float(max(self.gw, 1))

            # Irregular tectonic fracture swell along stroke (seamless C0 across segments!)
            swell = (
                0.80
                + 0.21 * np.sin(arc_u * 27.0 + seed_val * 41.0)
                + 0.13 * np.sin(arc_u * 63.0 - seed_val * 59.0)
            )
            if radius_override is None and arc_start is None:
                # Blade-sharp incision entry ramp so stroke start is pinched rather than blunt
                entry_pinch = 0.36 + 0.64 * np.clip(arc_u / 0.035, 0.0, 1.0)
                swell = swell * entry_pinch

            local_r = np.maximum(base_radius * swell, 2.5)

            # Dagger-cusp lance tips beyond segment ends instead of rounded sausage pill-caps!
            ds_out = np.maximum(0.0, -s_along) + np.maximum(0.0, s_along - seg_len)
            tip_len = local_r * 1.68
            tip_taper = np.power(np.clip(1.0 - ds_out / tip_len, 0.0, 1.0), 1.65)
            new_d = np.clip(tip_taper - (w_perp / local_r), 0.0, 1.0).astype(np.float32)

        old_d = self.profile[iy0:iy1, ix0:ix1]
        old_age = self.age[iy0:iy1, ix0:ix1]
        old_tau = _tau_from_age(old_age)
        new_tau = float(_tau_from_age(age_val))

        upd = (new_d > 0.0) & (((new_d - new_tau) > (old_d - old_tau)) | (old_age <= 0.004))
        if np.any(upd):
            old_d[upd] = new_d[upd]
            old_age[upd] = float(age_val)
            self.seed[iy0:iy1, ix0:ix1][upd] = seed_val
            cur_arc = self.arc[iy0:iy1, ix0:ix1]
            cur_arc[upd] = 1.0

        # Acute cyber-lightning fork splinter on fast slashes
        if (
            allow_fork
            and radius_override is None
            and seg_len >= 4.0
            and speed_norm > 0.38
            and random.random() < (0.18 + 0.24 * float(speed_norm))
        ):
            base_ang = math.atan2(uv1[1] - uv0[1], uv1[0] - uv0[0])
            fork_sign = 1.0 if random.random() < 0.5 else -1.0
            fork_ang = base_ang + fork_sign * random.uniform(0.42, 0.92)
            fork_len = (0.026 + 0.042 * float(speed_norm)) * random.uniform(0.75, 1.25)
            mid_u = uv0[0] + 0.65 * (uv1[0] - uv0[0])
            mid_v = uv0[1] + 0.65 * (uv1[1] - uv0[1])
            end_u = max(0.01, min(0.99, mid_u + math.cos(fork_ang) * fork_len * 0.62))
            end_v = max(0.01, min(0.99, mid_v + math.sin(fork_ang) * fork_len))
            self.stamp_segment(
                (mid_u, mid_v),
                (end_u, end_v),
                speed_norm=speed_norm * 0.6,
                load_mix=load_mix,
                stroke_seed=seed_val,
                age_val=min(float(age_val), 0.72),
                radius_override=max(5.5, base_radius * 0.36),
                arc_start=0.05,
                allow_fork=False,
            )

    def stamp_bite(self, uv: tuple[float, float]) -> None:
        """Stamps a sharp ~1.5s `[tDd//WATCHDOG]` fang micro-rift around a dwelling cursor."""
        u, v = uv
        bite_seed = round(random.uniform(0.05, 0.95), 4)
        # At age=0.34 (t=6.6s), tau=0.697 -> core rips open cleanly with arc=1.0 and zips shut over ~1.5s
        du = 0.018
        dv = 0.026
        self.stamp_segment(
            (max(0.0, u - du), max(0.0, v - dv)),
            (min(1.0, u + du), min(1.0, v + dv)),
            speed_norm=0.35,
            load_mix=0.25,
            stroke_seed=bite_seed,
            age_val=0.34,
            radius_override=12.5,
            arc_start=0.08,
            allow_fork=False,
        )
        self.stamp_segment(
            (max(0.0, u - du * 0.75), min(1.0, v + dv * 0.85)),
            (min(1.0, u + du * 0.75), max(0.0, v - dv * 0.85)),
            speed_norm=0.30,
            load_mix=0.25,
            stroke_seed=bite_seed,
            age_val=0.34,
            radius_override=10.5,
            arc_start=0.08,
            allow_fork=False,
        )

    def step_pointer(
        self,
        uv: tuple[float, float],
        dt: float,
        load_mix: float,
    ) -> tuple[bool, float, bool]:
        scar_sealed = self.update_decay(dt)
        self.mouse_uv = uv
        if self.prev_uv is None:
            self.prev_uv = uv
            return False, 0.0, scar_sealed

        du = uv[0] - self.prev_uv[0]
        dv = uv[1] - self.prev_uv[1]
        uv_dist = math.hypot(du, dv)
        if uv_dist < 0.0009:
            self._idle_sec += max(0.0, dt)
            return False, self.mouse_speed_norm, scar_sealed

        if self._idle_sec > 0.12:
            self.stroke_seed = round(random.uniform(0.05, 0.95), 4)
            self._stroke_arc_len = 0.0
        self._idle_sec = 0.0

        speed_uv_per_sec = uv_dist / max(dt, 0.008)
        speed_norm = min(1.0, speed_uv_per_sec / 1.15)
        self.mouse_speed_norm = max(self.mouse_speed_norm, speed_norm)
        self.stamp_segment(
            self.prev_uv,
            uv,
            self.mouse_speed_norm,
            load_mix,
            stroke_seed=self.stroke_seed,
            arc_start=self._stroke_arc_len,
        )
        self._stroke_arc_len += math.hypot(du * self.gw, dv * self.gh) / float(max(self.gw, 1))
        self.prev_uv = uv
        return True, self.mouse_speed_norm, scar_sealed

    def extract_rt_emitters(
        self,
        cur_uv: tuple[float, float],
        speed_norm: float,
        dwell_sec: float,
        heartbeat: float,
        snap: TelemetrySnapshot,
        elapsed: float,
        window_rects: list[tuple[float, float, float, float]],
    ) -> tuple[int, ctypes.Array[ctypes.c_float], int, ctypes.Array[ctypes.c_float]]:
        """Extracts up to 22 multi-color area-light emitters & 16 3D occluder boxes for RTX 5070 RT Cores."""
        emit_list: list[tuple[float, float, float, float, float, float, float, float]] = []

        # 1. Emitter 0: Cursor Plasma / Phosphor Torch (z=0.040 < bezel z=0.046 -> casts sharp slotted shadows!)
        hot_cur = min(1.0, speed_norm * 1.35 + (0.65 if dwell_sec >= 1.5 else 0.0))
        cr = 0.03 * (1.0 - hot_cur) + 0.92 * hot_cur
        cg = 0.84 * (1.0 - hot_cur) + 0.22 * hot_cur
        cb = 0.44 * (1.0 - hot_cur) + 0.04 * hot_cur
        c_int = 0.26 + 0.65 * speed_norm + (0.34 if dwell_sec >= 1.5 else 0.0)
        emit_list.append((cur_uv[0], cur_uv[1], 0.040, 0.018, cr, cg, cb, c_int))

        # 2. Emitters 1..15: Vectorized 16x9 macro-tile peak extraction along active rift spines & Gen 1B shelves
        if self.gw == 480 and self.gh == 270:
            tau = _tau_from_age(self.age)
            spine_score = (
                (self.profile > np.maximum(tau, 0.52))
                * (self.age > 0.12)
                * self.profile
                * (0.48 + 0.52 * self.arc)
            )
            tiles = spine_score.reshape(9, 30, 16, 30).transpose(0, 2, 1, 3).reshape(144, 900)
            tile_max = tiles.max(axis=1)
            active_tiles = np.flatnonzero(tile_max > 0.28)
            if active_tiles.size > 0:
                if active_tiles.size > 14:
                    top_k = np.argsort(tile_max[active_tiles])[-14:][::-1]
                    active_tiles = active_tiles[top_k]
                else:
                    active_tiles = active_tiles[np.argsort(tile_max[active_tiles])[::-1]]

                # Energy-conserving line-radiance normalization so 14 tiles never over-expose!
                norm_scale = 1.0 / math.sqrt(max(1.0, float(active_tiles.size) * 0.65))
                local_argmax = tiles[active_tiles].argmax(axis=1)
                for idx_i, tile_id in enumerate(active_tiles):
                    ty = int(tile_id // 16)
                    tx = int(tile_id % 16)
                    loc = int(local_argmax[idx_i])
                    py = ty * 30 + (loc // 30)
                    px = tx * 30 + (loc % 30)
                    uv_x = (px + 0.5) / 480.0
                    uv_y = (py + 0.5) / 270.0
                    arc_v = float(self.arc[py, px])
                    age_v = float(self.age[py, px])
                    seed_v = float(self.seed[py, px])
                    # Mix deep arterial blood-crimson (#b80612) spine emitters with Gen 1B royal-UV & emerald!
                    hue_pick = (tx * 5 + ty * 3 + int(seed_v * 11.0)) % 5
                    if hue_pick in (0, 1, 2):
                        # Dark arterial blood-red (strictly low G so green circuits don't wash yellow!)
                        er = 0.82 + 0.14 * arc_v
                        eg = 0.025 + 0.08 * (arc_v * arc_v)
                        eb = 0.075 * (1.0 - 0.5 * arc_v)
                    elif hue_pick == 3:
                        # Gen 1B Royal Ultraviolet rift-shelf bounce
                        er, eg, eb = 0.40, 0.055, 0.90
                    else:
                        # Gen 1B Phosphor-Emerald rift-shelf bounce
                        er, eg, eb = 0.00, 0.84, 0.46
                    e_int = (
                        (0.48 + 0.58 * arc_v + 0.18 * heartbeat)
                        * min(1.0, age_v * 2.2)
                        * norm_scale
                    )
                    emit_list.append((uv_x, uv_y, 0.036, 0.022, er, eg, eb, e_int))

        # 3. Active 32x18 Bad Sector 3D Monoliths + Gen 1B Ultraviolet/Emerald/Amber GI Emitters
        load_mix = 0.42 * snap.cpu + 0.36 * snap.gpu + 0.22 * snap.ram
        box_list: list[tuple[float, float, float, float, float]] = []
        for wx0, wy0, wx1, wy1 in window_rects[:8]:
            box_list.append((wx0, wy0, wx1, wy1, 0.078))

        # Deterministic fast Bad Sector candidate ring around screen perimeter & high-load clusters
        sec_Phase = elapsed * 0.021
        cand_sectors: list[tuple[float, int, int]] = []
        for by in range(1, 17, 2):
            for bx in range(1, 31, 3):
                u_c = (bx + 0.5) / 32.0
                v_c = (by + 0.5) / 18.0
                if v_c < 0.14 and (u_c < 0.32 or u_c > 0.68) and load_mix < 0.65:
                    continue
                edge_p = max(abs(u_c - 0.5) * 2.0, abs(v_c - 0.5) * 2.0)
                score = (
                    0.5
                    + 0.5 * math.sin(bx * 0.77 + sec_Phase * 9.0 - by * 0.51)
                    + 0.22 * max(0.0, edge_p - 0.55)
                    + 0.45 * load_mix
                )
                if score > 0.78:
                    cand_sectors.append((score, bx, by))

        cand_sectors.sort(reverse=True)
        n_sec_boxes = min(8, max(2, int(2 + 8 * load_mix)))
        for s_idx, (_sc, bx, by) in enumerate(cand_sectors[:n_sec_boxes]):
            x0 = bx / 32.0
            y0 = by / 18.0
            x1 = (bx + 1.0) / 32.0
            y1 = (by + 1.0) / 18.0
            if len(box_list) < 16:
                box_list.append((x0, y0, x1, y1, 0.054))
            if len(emit_list) < 22 and s_idx < 5:
                cx = 0.5 * (x0 + x1)
                cy = 0.5 * (y0 + y1)
                palette_idx = (bx * 7 + by * 3) % 3
                if palette_idx == 0:
                    # Vivid Gen 1B Royal Ultraviolet GI
                    sr, sg, sb = 0.42, 0.06, 0.92
                elif palette_idx == 1:
                    # Phosphor Emerald GI
                    sr, sg, sb = 0.00, 0.86, 0.44
                else:
                    # Burnt Sulfur-Amber GI
                    sr, sg, sb = 0.88, 0.32, 0.02
                s_int = 0.16 + 0.32 * load_mix + 0.16 * snap.burst
                emit_list.append((cx, cy, 0.038, 0.025, sr, sg, sb, s_int))

        n_emit = min(24, len(emit_list))
        emit_arr = (ctypes.c_float * max(8, n_emit * 8))()
        for i, vals in enumerate(emit_list[:n_emit]):
            base = i * 8
            for k in range(8):
                emit_arr[base + k] = float(vals[k])

        n_box = min(16, len(box_list))
        box_arr = (ctypes.c_float * max(5, n_box * 5))()
        for i, bvals in enumerate(box_list[:n_box]):
            base = i * 5
            for k in range(5):
                box_arr[base + k] = float(bvals[k])

        return n_emit, emit_arr, n_box, box_arr

    def active_scar_pct(self) -> int:
        live = self.age > 0.004
        if not np.any(live):
            return 0
        tau = _tau_from_age(self.age)
        open_mask = live & (self.profile > tau)
        return int(min(100, round(float(open_mask.mean()) * 100.0)))

    def pack_rgba(self) -> np.ndarray:
        dead = self.age <= 0.0
        if np.any(dead):
            self.profile[dead] = 0.0
            self.arc[dead] = 0.0
        np.multiply(np.clip(self.profile, 0.0, 1.0), 255.0, out=self.packed[..., 0], casting="unsafe")
        np.multiply(np.clip(self.age, 0.0, 1.0), 255.0, out=self.packed[..., 1], casting="unsafe")
        np.multiply(np.clip(self.seed, 0.0, 1.0), 255.0, out=self.packed[..., 2], casting="unsafe")
        np.multiply(np.clip(self.arc, 0.0, 1.0), 255.0, out=self.packed[..., 3], casting="unsafe")
        return self.packed


class VeilAsciiDimension:
    """Renders living `tDd` glitch ASCII behind the veil (oxblood/crimson/amber/emerald; zero cyan/pink)."""

    COL_OXBLOOD: tuple[float, float, float] = (0.52, 0.02, 0.06)
    COL_CRIMSON: tuple[float, float, float] = (0.80, 0.04, 0.08)
    COL_AMBER: tuple[float, float, float] = (0.86, 0.34, 0.02)
    COL_EMERALD: tuple[float, float, float] = (0.00, 0.80, 0.44)
    COL_PHOSPHOR: tuple[float, float, float] = (0.00, 0.88, 0.24)

    def __init__(self, width: int = 1280, height: int = 720) -> None:
        self.w = width
        self.h = height
        self.buf = np.zeros((self.h, self.w, 4), dtype=np.uint8)
        self.step_no = 0
        self.gaze = 0
        self.blink = 0
        self.reel = 0
        self.hex_seed = 0xDEAD

    def _corrupt(self, text: str, rate: float) -> str:
        if rate <= 0.005:
            return text
        return "".join(
            (random.choice(GLITCH_CHARS) if (ch != " " and random.random() < rate) else ch)
            for ch in text
        )

    def _hound_block(
        self,
        snap: TelemetrySnapshot,
        phase: int,
        *,
        stalk_dwell: float = 0.0,
    ) -> list[tuple[str, tuple[float, float, float]]]:
        stress = 0.45 * snap.cpu + 0.35 * snap.gpu + 0.20 * snap.ram
        if stalk_dwell >= 3.5 or snap.burst > 0.55 or stress > 0.62:
            eyes = "x    x" if ((self.step_no + phase) % 2 == 0) else "#    !"
            eye_col = self.COL_CRIMSON
        elif stalk_dwell >= 1.5:
            eyes = "<o> <o>"[:6]
            eye_col = self.COL_AMBER
        elif (self.blink + phase) % 11 == 0:
            eyes = "-    -"
            eye_col = self.COL_EMERALD
        else:
            g = (self.gaze + phase) % 3 - 1
            eyes = "o.  o." if g < 0 else (".o  .o" if g > 0 else "o    o")
            eye_col = self.COL_AMBER if stress > 0.38 else self.COL_PHOSPHOR

        ant = "/" if ((self.step_no + phase) % 2 == 0) else "\\"
        tag = f"0x{(self.hex_seed ^ (phase * 0x1111)) & 0xFFFF:04X}"
        crown_col = self.COL_CRIMSON if stalk_dwell >= 1.5 else self.COL_OXBLOOD
        return [
            (f"   /{ant}____/{ant}   ", crown_col),
            ("  /  .::.  \\  ", self.COL_PHOSPHOR),
            (f" /[ {eyes} ]\\ ", eye_col),
            ("  \\  \\__/  /  ", self.COL_OXBLOOD if stress > 0.55 else self.COL_PHOSPHOR),
            (f"  `-[{tag}]-'  ", self.COL_AMBER),
            (" [tDd//WATCHDOG]", self.COL_EMERALD),
        ]

    def _optic_block(
        self, snap: TelemetrySnapshot, phase: int
    ) -> list[tuple[str, tuple[float, float, float]]]:
        reels = ("|", "/", "-", "\\")
        r1 = reels[(self.reel + phase) % 4]
        r2 = reels[(self.reel + phase + 2) % 4]
        stress = 0.40 * snap.cpu + 0.40 * snap.gpu + 0.20 * snap.ram
        pos = (self.step_no + phase) % 5
        iris = (" " * pos) + ("(<*>)" if stress > 0.5 else "((0))") + (" " * (4 - pos))
        wave_chars = "_.-~^"
        wave = "".join(
            wave_chars[
                min(
                    4,
                    int((math.sin(self.step_no * 0.9 + i + phase) * 0.5 + 0.5) * (2.0 + 2.8 * stress)),
                )
            ]
            for i in range(9)
        )
        return [
            (f" .-=[{r1}]===[{r2}]=-.", self.COL_OXBLOOD),
            (" | .---------. |", self.COL_PHOSPHOR),
            (f" | |{iris}| |", self.COL_CRIMSON if stress > 0.5 else self.COL_EMERALD),
            (" | `---------' |", self.COL_PHOSPHOR),
            (f" |  [{wave}]  |", self.COL_AMBER),
            (" `-[ICE//2400]-'", self.COL_EMERALD),
        ]

    def render(
        self,
        snap: TelemetrySnapshot,
        *,
        lag_uv: tuple[float, float] = (0.5, 0.5),
        dwell_sec: float = 0.0,
        no_carrier_y: float = -1.0,
    ) -> np.ndarray:
        self.step_no += 1
        self.reel = (self.reel + 1) % 4
        self.blink += 1
        if random.random() < 0.35:
            self.gaze = random.choice([0, 1, 2])
        if random.random() < 0.30:
            self.hex_seed = random.choice([0xDEAD, 0xBEEF, 0x1337, 0xFA17, 0xC0DE, 0x1F3A])

        self.buf.fill(0)
        surf = cairo.ImageSurface.create_for_data(
            memoryview(self.buf), cairo.FORMAT_ARGB32, self.w, self.h, self.w * 4
        )
        ctx = cairo.Context(surf)
        ctx.select_font_face("Liberation Mono", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

        mut_rate = min(0.22, 0.02 + 0.15 * (snap.cpu + snap.gpu) * 0.5 + 0.12 * snap.burst)
        ctx.set_font_size(12.5)
        line_h = 15.5

        # Primary [tDd//WATCHDOG] construct stalks ~300ms lagged cursor EMA
        dog_x = max(18.0, min(float(self.w - 156), lag_uv[0] * self.w - 64.0))
        dog_y = max(24.0, min(float(self.h - 104), lag_uv[1] * self.h - 38.0))

        cols_x = [85, 420, 760, 1040]
        rows_y = [115, 315, 515]

        for r_i, base_y in enumerate(rows_y):
            for c_i, base_x in enumerate(cols_x):
                # Skip background grid cell if primary watchdog overlaps it so glyphs stay legible
                if abs(base_x - dog_x) < 138.0 and abs(base_y - dog_y) < 92.0:
                    continue
                phase = r_i * 4 + c_i + 1
                block = (
                    self._hound_block(snap, phase)
                    if (r_i + c_i) % 2 == 0
                    else self._optic_block(snap, phase)
                )
                bob = math.sin(self.step_no * 0.45 + phase) * 4.0
                for l_i, (txt, rgb) in enumerate(block):
                    g_txt = self._corrupt(txt, mut_rate)
                    tx = base_x
                    ty = base_y + bob + l_i * line_h
                    ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.68)
                    ctx.move_to(tx, ty)
                    ctx.show_text(g_txt)

        # Draw primary lagged cursor-stalking [tDd//WATCHDOG]
        primary_dog = self._hound_block(snap, 0, stalk_dwell=dwell_sec)
        dog_alpha = min(0.96, 0.78 + 0.06 * min(3.0, dwell_sec))
        dog_mut = mut_rate * (1.35 if dwell_sec >= 3.5 else 0.75)
        for l_i, (txt, rgb) in enumerate(primary_dog):
            g_txt = self._corrupt(txt, dog_mut)
            ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], dog_alpha)
            ctx.move_to(dog_x, dog_y + l_i * line_h)
            ctx.show_text(g_txt)

        # Crisp 120ms burst `+++ATH0 // NO CARRIER` phosphor scan-band
        if 0.0 <= no_carrier_y <= 1.0:
            py = max(20.0, min(float(self.h - 14), no_carrier_y * self.h))
            ctx.set_source_rgba(0.03, 0.0, 0.01, 0.82)
            ctx.rectangle(0.0, py - 13.0, float(self.w), 18.0)
            ctx.fill()

            ctx.set_font_size(13.0)
            banner = "+++ATH0 // NO CARRIER :: 0xDEAD LINK_DROP // +++ATH0 // NO CARRIER :: 0xDEAD LINK_DROP"
            ctx.set_source_rgba(
                self.COL_PHOSPHOR[0], self.COL_PHOSPHOR[1], self.COL_PHOSPHOR[2], 0.96
            )
            ctx.move_to(24.0, py)
            ctx.show_text(banner)
            ctx.set_source_rgba(self.COL_CRIMSON[0], self.COL_CRIMSON[1], self.COL_CRIMSON[2], 0.85)
            ctx.move_to(710.0, py)
            ctx.show_text("+++ATH0 // NO CARRIER [CARRIER_LOST]")

        surf.flush()
        return self.buf


def ensure_gl_library() -> ctypes.CDLL:
    SHADER_DIR.mkdir(parents=True, exist_ok=True)

    # Compile Vulkan 1.3 rayQuery compute shader if .comp is newer than .spv
    if VK_RT_COMP_SOURCE.exists() and (
        not VK_RT_SPV_PATH.exists()
        or VK_RT_COMP_SOURCE.stat().st_mtime > VK_RT_SPV_PATH.stat().st_mtime
    ):
        subprocess.run(
            [
                "glslangValidator",
                "--target-env",
                "vulkan1.3",
                "-V",
                str(VK_RT_COMP_SOURCE),
                "-o",
                str(VK_RT_SPV_PATH),
            ],
            check=True,
        )

    need_build = not GL_SO_PATH.exists()
    if not need_build:
        so_mtime = GL_SO_PATH.stat().st_mtime
        for src in (GL_C_SOURCE, VK_RT_C_SOURCE, VK_RT_H_SOURCE):
            if src.exists() and src.stat().st_mtime > so_mtime:
                need_build = True
                break

    if need_build:
        tmp_so = SHADER_DIR / f".corrupt_gl_renderer.so.{os.getpid()}"
        c_sources = [str(GL_C_SOURCE)]
        link_libs = ["-lX11", "-lepoxy", "-lmpv", "-lm"]
        if VK_RT_C_SOURCE.exists():
            c_sources.append(str(VK_RT_C_SOURCE))
            link_libs.append("-lvulkan")
        subprocess.run(
            [
                "gcc",
                "-O3",
                "-shared",
                "-fPIC",
                *c_sources,
                "-o",
                str(tmp_so),
                *link_libs,
            ],
            check=True,
        )
        tmp_so.replace(GL_SO_PATH)

    lib = ctypes.CDLL(str(GL_SO_PATH))
    lib.corrupt_gl_init.argtypes = [
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    lib.corrupt_gl_init.restype = ctypes.c_void_p

    if hasattr(lib, "corrupt_gl_rt_device"):
        lib.corrupt_gl_rt_device.argtypes = [ctypes.c_void_p]
        lib.corrupt_gl_rt_device.restype = ctypes.c_char_p

    lib.corrupt_gl_resize.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.corrupt_gl_resize.restype = None

    for fn_name in (
        "corrupt_gl_upload_rift",
        "corrupt_gl_upload_veil",
        "corrupt_gl_upload_hud",
    ):
        fn = getattr(lib, fn_name)
        fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        fn.restype = None

    for step_fn in ("corrupt_gl_step_life", "corrupt_gl_render"):
        fn = getattr(lib, step_fn)
        fn.argtypes = [
            ctypes.c_void_p,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
        ]
        fn.restype = None

    lib.corrupt_gl_read_pixels.argtypes = [
        ctypes.c_void_p,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_void_p,
    ]
    lib.corrupt_gl_read_pixels.restype = ctypes.c_int

    if hasattr(lib, "corrupt_gl_set_windows"):
        lib.corrupt_gl_set_windows.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_float),
        ]
        lib.corrupt_gl_set_windows.restype = None

    if hasattr(lib, "corrupt_gl_set_extra"):
        lib.corrupt_gl_set_extra.argtypes = [
            ctypes.c_void_p,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
        ]
        lib.corrupt_gl_set_extra.restype = None

    if hasattr(lib, "corrupt_gl_trace_lights"):
        lib.corrupt_gl_trace_lights.argtypes = [
            ctypes.c_void_p,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_float),
        ]
        lib.corrupt_gl_trace_lights.restype = None

    lib.corrupt_gl_destroy.argtypes = [ctypes.c_void_p]
    lib.corrupt_gl_destroy.restype = None
    return lib


class CorruptLockInController:
    """Drives the 30 FPS OpenGL 3.3 + libmpv + RTX 5070 Vulkan 1.3 Hardware Light Raytracer."""

    TICK_MS = 33

    def __init__(
        self,
        parent_xid: int,
        win_x: int,
        win_y: int,
        width: int,
        height: int,
        video_path: Path,
        ready_file: Path | None = None,
    ) -> None:
        self.parent_xid = int(parent_xid)
        self.win_x = win_x
        self.win_y = win_y
        self.width = max(320, width)
        self.height = max(240, height)
        self.ready_file = ready_file

        self._lib = ensure_gl_library()
        self._gl_handle = self._lib.corrupt_gl_init(
            self.parent_xid,
            self.width,
            self.height,
            str(video_path).encode("utf-8"),
            str(VEIL_FRAG_PATH).encode("utf-8"),
            str(LIFE_FRAG_PATH).encode("utf-8"),
        )
        if not self._gl_handle:
            raise RuntimeError("Failed to initialize OpenGL + libmpv corrupt renderer")

        self.rt_device_name: str = ""
        if hasattr(self._lib, "corrupt_gl_rt_device"):
            raw_dev = self._lib.corrupt_gl_rt_device(self._gl_handle)
            if raw_dev:
                self.rt_device_name = raw_dev.decode("utf-8", errors="ignore").strip()

        self.telemetry = SystemTelemetry()
        self.win_scanner = X11WindowScanner(ignore_xids={self.parent_xid})
        self.rift = RiftField(480, 270)
        self.veil = VeilAsciiDimension(1280, 720)
        self.hud_buf = np.zeros((self.height, self.width, 4), dtype=np.uint8)

        self._display = Gdk.Display.get_default()
        self._pointer = (
            self._display.get_default_seat().get_pointer()
            if self._display is not None and self._display.get_default_seat() is not None
            else None
        )

        self.start_mono = time.monotonic()
        self.last_mono = self.start_mono
        self.tick_count = 0
        self.life_gen = 0
        self.snap = self.telemetry.sample()
        self.mantra_index = 0

        # Raytrace, cursor dwell, lagged watchdog EMA, burst NO CARRIER & scar remap state
        self._raytrace_override: bool | None = None
        self.raytrace_enabled: bool = read_raytrace_config()
        self._rt_warmed: bool = False
        self.dwell_sec: float = 0.0
        self._dwell_anchor_uv: tuple[float, float] | None = None
        self.lag_uv: tuple[float, float] = (0.5, 0.5)

        self.no_carrier_y: float = -1.0
        self.no_carrier_until: float = 0.0
        self._no_carrier_cooldown_until: float = 0.0

        self.scar_remap_seed: float = 0.0
        self.scar_remap_until: float = 0.0
        self._scar_remap_debounce_until: float = 0.0
        self._scar_remap_count: int = 0
        self.scar_remap_hex: str = "1F3A"

        self._pinned_windows: list[tuple[float, float, float, float]] | None = None
        self.window_rects: list[tuple[float, float, float, float]] = []

        initial_rects = self.win_scanner.query_rects(self.win_x, self.win_y, self.width, self.height)
        self._push_windows(initial_rects)
        self._push_extra()
        self._upload_veil()
        self._upload_rift()
        if self.raytrace_enabled:
            self._trace_rtx_lights(0.0, self.rift.mouse_uv, 0.0, temporal_blend=1.0)
            self._rt_warmed = True
        self._upload_hud(0.0)

        if self.ready_file is not None:
            try:
                self.ready_file.write_text(f"{os.getpid()}\n", encoding="ascii")
            except OSError:
                pass

    def set_raytrace(self, enabled: bool | None) -> None:
        """Sets or unpins RTX 5070 Hardware Light Raytracing mode and updates GPU uniforms & HUD."""
        self._raytrace_override = enabled
        prev = self.raytrace_enabled
        self.raytrace_enabled = read_raytrace_config() if enabled is None else bool(enabled)
        if self.raytrace_enabled and not prev:
            self._rt_warmed = False
        self._push_extra()
        if self._gl_handle:
            self._upload_rift()
            if self.raytrace_enabled:
                elapsed = time.monotonic() - self.start_mono
                self._trace_rtx_lights(
                    elapsed,
                    self.rift.mouse_uv,
                    self.rift.mouse_speed_norm,
                    temporal_blend=1.0,
                )
                self._rt_warmed = True
            self._upload_hud(time.monotonic() - self.start_mono)

    def _trace_rtx_lights(
        self,
        elapsed: float,
        cur_uv: tuple[float, float],
        speed_norm: float,
        *,
        temporal_blend: float = 0.24,
    ) -> None:
        """Dispatches Vulkan 1.3 hardware RT-Core area-light shadow & GI ray queries on RTX 5070."""
        if (
            not self._gl_handle
            or not self.raytrace_enabled
            or not hasattr(self._lib, "corrupt_gl_trace_lights")
        ):
            return

        bpm = 50.0 + 90.0 * float(self.snap.cpu)
        beat_p = (elapsed * (bpm / 60.0)) % 1.0
        heartbeat = min(
            1.0,
            math.exp(-(((beat_p - 0.08) / 0.045) ** 2))
            + 0.68 * math.exp(-(((beat_p - 0.23) / 0.055) ** 2)),
        )

        n_emit, emit_arr, n_box, box_arr = self.rift.extract_rt_emitters(
            cur_uv=cur_uv,
            speed_norm=speed_norm,
            dwell_sec=self.dwell_sec,
            heartbeat=heartbeat,
            snap=self.snap,
            elapsed=elapsed,
            window_rects=self.window_rects,
        )
        loads4 = (ctypes.c_float * 4)(
            float(self.snap.cpu),
            float(self.snap.gpu),
            float(self.snap.ram),
            float(self.snap.burst),
        )
        mouse4 = (ctypes.c_float * 4)(
            float(cur_uv[0]),
            float(cur_uv[1]),
            float(speed_norm),
            float(self.dwell_sec),
        )
        aspect = float(self.width) / float(max(1, self.height))
        blend = 1.0 if not self._rt_warmed else float(temporal_blend)
        self._lib.corrupt_gl_trace_lights(
            self._gl_handle,
            float(elapsed),
            aspect,
            blend,
            loads4,
            mouse4,
            n_emit,
            emit_arr,
            n_box,
            box_arr,
        )
        self._rt_warmed = True

    def set_windows(self, rects: list[tuple[float, float, float, float]] | None) -> None:
        """Pins simulated UV window rects (or passes `None` to resume live X11 EWMH scanning)."""
        self._pinned_windows = list(rects[:8]) if rects is not None else None
        if self._pinned_windows is not None:
            self._push_windows(self._pinned_windows)
        else:
            live = self.win_scanner.query_rects(self.win_x, self.win_y, self.width, self.height)
            self._push_windows(live)

    def _push_windows(self, rects: list[tuple[float, float, float, float]]) -> None:
        self.window_rects = list(rects[:8])
        if not self._gl_handle or not hasattr(self._lib, "corrupt_gl_set_windows"):
            return
        count = len(self.window_rects)
        flat = (ctypes.c_float * max(4, count * 4))()
        for idx, (x0, y0, x1, y1) in enumerate(self.window_rects):
            base = idx * 4
            flat[base + 0] = float(x0)
            flat[base + 1] = float(y0)
            flat[base + 2] = float(x1)
            flat[base + 3] = float(y1)
        self._lib.corrupt_gl_set_windows(self._gl_handle, count, flat)

    def _push_extra(self) -> None:
        if not self._gl_handle or not hasattr(self._lib, "corrupt_gl_set_extra"):
            return
        self._lib.corrupt_gl_set_extra(
            self._gl_handle,
            1.0 if self.raytrace_enabled else 0.0,
            float(self.dwell_sec),
            float(self.no_carrier_y),
            float(self.scar_remap_seed),
        )

    def resize(self, win_x: int, win_y: int, width: int, height: int) -> None:
        self.win_x = win_x
        self.win_y = win_y
        width = max(320, width)
        height = max(240, height)
        if width == self.width and height == self.height:
            return
        self.width = width
        self.height = height
        self.hud_buf = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        if self._gl_handle:
            self._lib.corrupt_gl_resize(self._gl_handle, width, height)
            if self._pinned_windows is None:
                self._push_windows(
                    self.win_scanner.query_rects(self.win_x, self.win_y, self.width, self.height)
                )
            self._upload_hud(time.monotonic() - self.start_mono)

    def close(self) -> None:
        if self.ready_file is not None:
            try:
                self.ready_file.unlink()
            except OSError:
                pass
        if self._gl_handle:
            self._lib.corrupt_gl_destroy(self._gl_handle)
            self._gl_handle = None
        self.win_scanner.close()
        self.telemetry.close()

    def _read_mouse_uv(self) -> tuple[float, float]:
        if self._pointer is None:
            return self.rift.mouse_uv
        try:
            _scr, mx, my = self._pointer.get_position()
            u = max(0.0, min(1.0, (mx - self.win_x) / max(1, self.width)))
            v = max(0.0, min(1.0, (my - self.win_y) / max(1, self.height)))
            return u, v
        except Exception:
            return self.rift.mouse_uv

    def _upload_rift(self) -> None:
        packed = self.rift.pack_rgba()
        self._lib.corrupt_gl_upload_rift(
            self._gl_handle, self.rift.gw, self.rift.gh, packed.ctypes.data_as(ctypes.c_void_p)
        )

    def _upload_veil(self) -> None:
        veil_arr = self.veil.render(
            self.snap,
            lag_uv=self.lag_uv,
            dwell_sec=self.dwell_sec,
            no_carrier_y=self.no_carrier_y,
        )
        self._lib.corrupt_gl_upload_veil(
            self._gl_handle, self.veil.w, self.veil.h, veil_arr.ctypes.data_as(ctypes.c_void_p)
        )

    @staticmethod
    def _ascii_bar(ratio: float, width: int = 14) -> str:
        filled = max(0, min(width, round(ratio * width)))
        return "█" * filled + "·" * (width - filled)

    def _upload_hud(self, elapsed: float) -> None:
        self.hud_buf.fill(0)
        surf = cairo.ImageSurface.create_for_data(
            memoryview(self.hud_buf), cairo.FORMAT_ARGB32, self.width, self.height, self.width * 4
        )
        ctx = cairo.Context(surf)

        w, h = float(self.width), float(self.height)
        scale = min(w, h) / 1080.0
        pad = 14.0 * scale
        font_sz = max(11.0, 13.0 * scale)
        line_h = font_sz * 1.38

        hrs = int(elapsed) // 3600
        mins = (int(elapsed) % 3600) // 60
        secs = int(elapsed) % 60
        lock_clock = f"{hrs:02d}:{mins:02d}:{secs:02d}"

        col_crimson = (0.84, 0.05, 0.10)
        col_amber = (0.88, 0.36, 0.03)
        col_phosphor = (0.00, 0.90, 0.25)
        col_emerald = (0.00, 0.80, 0.45)

        load_mix = 0.42 * self.snap.cpu + 0.36 * self.snap.gpu + 0.22 * self.snap.ram
        if load_mix >= 0.65 or self.snap.burst >= 0.68:
            state_label = "BAD_SECTOR_SURGE"
            state_rgb = col_crimson
        elif load_mix >= 0.38 or self.snap.burst >= 0.45:
            state_label = "SECTOR_DRIFT"
            state_rgb = col_amber
        else:
            state_label = "CARRIER_LOCKED"
            state_rgb = col_phosphor

        cpu_pct = int(round(self.snap.cpu * 100))
        gpu_pct = int(round(self.snap.gpu * 100))
        ram_pct = int(round(self.snap.ram * 100))

        cpu_col = col_crimson if cpu_pct >= 70 else (col_amber if cpu_pct >= 40 else col_phosphor)
        gpu_col = col_crimson if gpu_pct >= 75 else (col_amber if gpu_pct >= 35 else col_emerald)
        ram_col = col_crimson if ram_pct >= 80 else (col_amber if ram_pct >= 55 else col_emerald)

        ctx.select_font_face("Liberation Mono", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(font_sz)

        gpu_extra = f" {self.snap.gpu_temp_c}C" if self.snap.gpu_temp_c > 0 else ""
        gpu_role = (
            "RTX_5070_RT"
            if (self.raytrace_enabled and "5070" in self.rt_device_name)
            else ("VK_LIGHT_RT" if (self.raytrace_enabled and self.rt_device_name) else "RIFT_SPINE")
        )
        left_rows = [
            (f"[ tDd // GLITCH_BBS :: 0xDEAD:0xBEEF ]  LOCK {lock_clock}", (0.93, 0.89, 0.83)),
            (f"CPU [{self._ascii_bar(self.snap.cpu)}] {cpu_pct:3d}% :: SYNC_TEAR", cpu_col),
            (f"GPU [{self._ascii_bar(self.snap.gpu)}] {gpu_pct:3d}% :: {gpu_role}{gpu_extra}", gpu_col),
            (f"RAM [{self._ascii_bar(self.snap.ram)}] {ram_pct:3d}% :: BAD_SECTOR", ram_col),
        ]

        max_lw = max(ctx.text_extents(text).width for text, _ in left_rows)
        plate_w = max_lw + 22.0 * scale
        plate_h = len(left_rows) * line_h + 14.0 * scale
        lx, ly = pad, pad

        ctx.set_source_rgba(0.0, 0.0, 0.0, 0.88)
        ctx.rectangle(lx, ly, plate_w, plate_h)
        ctx.fill()
        ctx.set_line_width(max(1.1, 1.4 * scale))
        ctx.set_dash([5.0 * scale, 3.0 * scale], 0.0)
        ctx.set_source_rgba(state_rgb[0], state_rgb[1], state_rgb[2], 0.88)
        ctx.rectangle(lx, ly, plate_w, plate_h)
        ctx.stroke()
        ctx.set_dash([], 0.0)

        for idx, (text, rgb) in enumerate(left_rows):
            ty = ly + 10.0 * scale + (idx + 0.82) * line_h
            ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.98)
            ctx.move_to(lx + 11.0 * scale, ty)
            ctx.show_text(text)

        scar_pct = self.rift.active_scar_pct()
        blink = "█" if (int(elapsed * 2) % 2 == 0) else "_"
        if self.raytrace_enabled:
            rt_tag = "[RTX5070:RT]" if "5070" in self.rt_device_name else "[VK:RT]"
        else:
            rt_tag = "[2D]"

        if self.scar_remap_seed > 0.0:
            status_line = (f"> 0x{self.scar_remap_hex} REMAPPED // SCAR SEALED {blink}", col_amber)
        elif self.no_carrier_y >= 0.0:
            status_line = (f"> +++ATH0 // NO CARRIER :: LINK_DROP {blink}", col_crimson)
        else:
            status_line = (f"> {LOCKIN_MANTRAS[self.mantra_index]} {blink}", col_phosphor)

        right_rows = [
            (f"/[ x  x ]\\  NODE_0xDEAD :: {state_label} {rt_tag}", state_rgb),
            (
                f"LIFE_GEN:{self.life_gen:05d}  RIFT_OPEN:{scar_pct:2d}%  HEAL:10.0s",
                col_emerald,
            ),
            status_line,
        ]

        max_rw = max(ctx.text_extents(text).width for text, _ in right_rows)
        r_plate_w = max_rw + 22.0 * scale
        r_plate_h = len(right_rows) * line_h + 14.0 * scale
        rx = max(lx + plate_w + 12.0 * scale, w - r_plate_w - pad)
        ry = pad

        if rx + r_plate_w <= w - 2:
            r_border = col_crimson if (self.raytrace_enabled or scar_pct > 0) else col_emerald
            ctx.set_source_rgba(0.0, 0.0, 0.0, 0.88)
            ctx.rectangle(rx, ry, r_plate_w, r_plate_h)
            ctx.fill()
            ctx.set_line_width(max(1.1, 1.4 * scale))
            ctx.set_dash([5.0 * scale, 3.0 * scale], 0.0)
            ctx.set_source_rgba(r_border[0], r_border[1], r_border[2], 0.88)
            ctx.rectangle(rx, ry, r_plate_w, r_plate_h)
            ctx.stroke()
            ctx.set_dash([], 0.0)

            for idx, (text, rgb) in enumerate(right_rows):
                ty = ry + 10.0 * scale + (idx + 0.82) * line_h
                ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.98)
                ctx.move_to(rx + 11.0 * scale, ty)
                ctx.show_text(text)

        surf.flush()
        self._lib.corrupt_gl_upload_hud(
            self._gl_handle, self.width, self.height, self.hud_buf.ctypes.data_as(ctypes.c_void_p)
        )

    def tick(self, override_uv: tuple[float, float] | None = None) -> bool:
        if not self._gl_handle:
            return False

        now = time.monotonic()
        dt = max(0.005, min(0.25, now - self.last_mono))
        self.last_mono = now
        elapsed = now - self.start_mono
        self.tick_count += 1

        hud_dirty = False
        veil_dirty = False

        if self.tick_count % 15 == 0:
            self.snap = self.telemetry.sample()
            if self._raytrace_override is None:
                rt_now = read_raytrace_config()
                if rt_now != self.raytrace_enabled:
                    self.raytrace_enabled = rt_now
                    hud_dirty = True
        else:
            self.snap.burst = round(max(0.0, self.snap.burst * 0.95), 4)

        if self.tick_count % 360 == 0:
            self.mantra_index = (self.mantra_index + 1) % len(LOCKIN_MANTRAS)

        # Window-Aware ICE: query visible X11 window rects every ~1.0s
        if self.tick_count % 30 == 0 and self._pinned_windows is None:
            rects = self.win_scanner.query_rects(self.win_x, self.win_y, self.width, self.height)
            self._push_windows(rects)

        # Burst `NO CARRIER` 120ms phosphor scan-row flash
        if self.snap.burst > 0.68 and now >= self._no_carrier_cooldown_until:
            self.no_carrier_y = round(random.uniform(0.15, 0.85), 4)
            self.no_carrier_until = now + 0.12
            self._no_carrier_cooldown_until = now + 0.65
            veil_dirty = True
            hud_dirty = True
        elif now >= self.no_carrier_until and self.no_carrier_y >= 0.0:
            self.no_carrier_y = -1.0
            veil_dirty = True
            hud_dirty = True

        load_mix = 0.42 * self.snap.cpu + 0.36 * self.snap.gpu + 0.22 * self.snap.ram

        cur_uv = override_uv if override_uv is not None else self._read_mouse_uv()

        # ~300ms lagged cursor EMA for primary `[tDd//WATCHDOG]` construct
        ema_alpha = 1.0 - math.exp(-dt / 0.30)
        self.lag_uv = (
            self.lag_uv[0] + ema_alpha * (cur_uv[0] - self.lag_uv[0]),
            self.lag_uv[1] + ema_alpha * (cur_uv[1] - self.lag_uv[1]),
        )

        # Cursor dwell (<1.5px movement accumulates dwell_sec; >=6.0s stamps 1.5s bite & resets to 1.6s)
        if self._dwell_anchor_uv is None:
            self._dwell_anchor_uv = cur_uv
            self.dwell_sec += dt
        else:
            move_px = math.hypot(
                (cur_uv[0] - self._dwell_anchor_uv[0]) * self.width,
                (cur_uv[1] - self._dwell_anchor_uv[1]) * self.height,
            )
            if move_px < 1.5:
                self.dwell_sec += dt
            else:
                self.dwell_sec = 0.0
                self._dwell_anchor_uv = cur_uv

        _moved, speed_norm, scar_sealed = self.rift.step_pointer(cur_uv, dt, load_mix)

        if self.dwell_sec >= 6.0:
            self.rift.stamp_bite(cur_uv)
            self.dwell_sec = 1.6
            veil_dirty = True

        # +3.0s Scar Remap sector residue & HUD confirmation when an active scar seals (9.5s..10.0s)
        if scar_sealed and now >= self._scar_remap_debounce_until:
            self.scar_remap_seed = round(random.uniform(0.55, 0.98), 4)
            self.scar_remap_until = now + 3.0
            self._scar_remap_debounce_until = now + 1.5
            self._scar_remap_count += 1
            if self._scar_remap_count == 1:
                self.scar_remap_hex = "1F3A"
            else:
                self.scar_remap_hex = (
                    f"{random.choice((0x1F3A, 0x2C0E, 0x7A19, 0x4D8B, 0x9E04)):04X}"
                )
            hud_dirty = True
        elif now >= self.scar_remap_until and self.scar_remap_seed > 0.0:
            self.scar_remap_seed = 0.0
            hud_dirty = True

        self._upload_rift()
        self._push_extra()

        # Step 100% GPU-native Conway's Game of Life ping-pong shader (~15 Hz -> 30 Hz under load/motion)
        life_stride = 1 if (speed_norm > 0.15 or load_mix > 0.40 or self.dwell_sec >= 1.5) else 2
        if self.tick_count % life_stride == 0:
            self.life_gen += 1
            self._lib.corrupt_gl_step_life(
                self._gl_handle,
                float(elapsed),
                float(self.snap.cpu),
                float(self.snap.gpu),
                float(self.snap.ram),
                float(self.snap.burst),
                float(cur_uv[0]),
                float(cur_uv[1]),
                float(speed_norm),
            )

        # Dispatch RTX 5070 Vulkan 1.3 hardware RT-Core light & shadow tracing when enabled
        if self.raytrace_enabled:
            self._trace_rtx_lights(
                elapsed,
                cur_uv,
                speed_norm,
                temporal_blend=0.34 if speed_norm > 0.15 else 0.22,
            )

        if veil_dirty or self.tick_count % 5 == 0:
            self._upload_veil()

        if hud_dirty or self.tick_count % 15 == 0:
            self._upload_hud(elapsed)

        self._lib.corrupt_gl_render(
            self._gl_handle,
            float(elapsed),
            float(self.snap.cpu),
            float(self.snap.gpu),
            float(self.snap.ram),
            float(self.snap.burst),
            float(cur_uv[0]),
            float(cur_uv[1]),
            float(speed_norm),
        )
        return True

    def capture_rgba(self, elapsed_override: float | None = None) -> np.ndarray:
        raw = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        if self._gl_handle:
            self._upload_rift()
            self._push_extra()
            elapsed = (
                float(elapsed_override)
                if elapsed_override is not None
                else (time.monotonic() - self.start_mono)
            )
            if self.raytrace_enabled:
                # 4 sub-millisecond hardware RT accumulation passes for silky penumbra shadows
                for pass_i in range(4):
                    self._trace_rtx_lights(
                        elapsed + pass_i * 0.016,
                        self.rift.mouse_uv,
                        self.rift.mouse_speed_norm,
                        temporal_blend=1.0 if pass_i == 0 else (1.0 / float(pass_i + 1)),
                    )
            self._lib.corrupt_gl_read_pixels(
                self._gl_handle,
                float(elapsed),
                float(self.snap.cpu),
                float(self.snap.gpu),
                float(self.snap.ram),
                float(self.snap.burst),
                float(self.rift.mouse_uv[0]),
                float(self.rift.mouse_uv[1]),
                float(self.rift.mouse_speed_norm),
                raw.ctypes.data_as(ctypes.c_void_p),
            )
        return np.flipud(raw).copy()
