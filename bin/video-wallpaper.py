#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Automatically prefer NVIDIA discrete GPU offload when present on hybrid Linux desktops
if os.environ.get("WALLPAPER_DISABLE_NVIDIA_OFFLOAD") != "1" and Path("/proc/driver/nvidia/version").is_file():
    os.environ.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")
    os.environ.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402,F401


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _discover_default_browser_cookies() -> str:
    candidates = (
        ("chromium+gnomekeyring", Path.home() / ".config/microsoft-edge-dev/Default"),
        ("chromium+gnomekeyring", Path.home() / ".config/microsoft-edge/Default"),
        ("chromium+gnomekeyring", Path.home() / ".config/chromium/Default"),
        ("chrome+gnomekeyring", Path.home() / ".config/google-chrome/Default"),
    )
    for prefix, profile_dir in candidates:
        if profile_dir.is_dir():
            return f"{prefix}:{profile_dir}"
    return ""


DEFAULT_YTDL_COOKIES_FROM_BROWSER = _discover_default_browser_cookies()
YTDL_COOKIES_FROM_BROWSER = os.environ.get(
    "VIDEO_WALLPAPER_YTDL_COOKIES_FROM_BROWSER",
    DEFAULT_YTDL_COOKIES_FROM_BROWSER,
)
MPV_VIDEO_OUTPUT = os.environ.get("VIDEO_WALLPAPER_MPV_VO", "gpu")
MPV_CORRUPT_VIDEO_OUTPUT = os.environ.get("VIDEO_WALLPAPER_CORRUPT_MPV_VO", "gpu-next")
MPV_GPU_API = os.environ.get("VIDEO_WALLPAPER_MPV_GPU_API", "opengl")
MPV_GPU_CONTEXT = os.environ.get("VIDEO_WALLPAPER_MPV_GPU_CONTEXT", "x11")
MPV_HWDEC = os.environ.get("VIDEO_WALLPAPER_MPV_HWDEC", "nvdec")
MPV_YTDL_FORMAT = os.environ.get(
    "VIDEO_WALLPAPER_YTDL_FORMAT",
    "bestvideo[height<=720]+bestaudio/best[height<=720]/best[height<=720]",
)
MPV_YTDL_FORMAT_NO_AUDIO = os.environ.get(
    "VIDEO_WALLPAPER_YTDL_FORMAT_NO_AUDIO",
    "bestvideo[height<=720]/best[height<=720]",
)
MPV_DEMUXER_MAX_BYTES = os.environ.get("VIDEO_WALLPAPER_DEMUXER_MAX_BYTES", "64MiB")
MPV_DEMUXER_MAX_BACK_BYTES = os.environ.get("VIDEO_WALLPAPER_DEMUXER_MAX_BACK_BYTES", "16MiB")
MPV_CACHE_SECS = os.environ.get("VIDEO_WALLPAPER_CACHE_SECS", "20")


def default_mpv_ipc_file() -> Path:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if not runtime_dir.is_dir():
        runtime_dir = Path("/tmp")
    return runtime_dir / "wallpaper-mode-mpv.sock"


def default_corrupt_ready_file() -> Path:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    if not runtime_dir.is_dir():
        runtime_dir = Path("/tmp")
    return runtime_dir / "wallpaper-mode-corrupt.ready"


STATE_DIR = Path.home() / ".config/wallpaper-mode"
CACHE_DIR = Path.home() / ".cache/wallpaper-mode"
SHADER_DIR = Path.home() / ".local/share/wallpaper-mode"
CORRUPT_SHADER_FILE = SHADER_DIR / "corrupt-lockin.glsl"
PHOTO_WALLPAPER_DIR = Path(
    os.environ.get("WALLPAPER_MODE_PHOTOS_DIR") or Path.home() / "Pictures" / "wallpaper-mode"
)
AUDIO_FILE = STATE_DIR / "audio"
MPV_VOLUME_FILE = STATE_DIR / "mpv-volume"
MPV_MUTE_FILE = STATE_DIR / "mpv-mute"
MPV_IPC_FILE = Path(os.environ.get("VIDEO_WALLPAPER_MPV_IPC") or default_mpv_ipc_file())
CORRUPT_READY_FILE = default_corrupt_ready_file()
CURRENT_META_FILE = STATE_DIR / "current-video.json"
HISTORY_FILE = STATE_DIR / "history.json"
CACHE_FILE = CACHE_DIR / "current-first-15min.mp4"
CACHE_META_FILE = CACHE_DIR / "current-first-15min.json"
LEGACY_CACHE_FILE = CACHE_DIR / "current.mkv"
LEGACY_CACHE_META_FILE = CACHE_DIR / "current.json"
DEFAULT_VIDEO_SOURCE = "https://www.youtube.com/watch?v=8VsvdCaSyaQ"
DEFAULT_VIDEO_ID = "8VsvdCaSyaQ"
DEFAULT_VIDEO_TITLE = "Fake Cam Hacker Screen 4K"
CORRUPT_VIDEO_TITLE = "tDd // Locked-In Glitch-BBS [8VsvdCaSyaQ]"
DEFAULT_CLIP_SECONDS = max(1, int(os.environ.get("VIDEO_WALLPAPER_DEFAULT_CLIP_SECONDS", "900")))
DEFAULT_CACHE_FILE = PHOTO_WALLPAPER_DIR / f"default-{DEFAULT_VIDEO_ID}-first-15min.mp4"
DEFAULT_CACHE_META_FILE = PHOTO_WALLPAPER_DIR / f"default-{DEFAULT_VIDEO_ID}-first-15min.json"
PERMANENT_CORRUPT_FILE = PHOTO_WALLPAPER_DIR / f"corrupt-lockin-{DEFAULT_VIDEO_ID}-permanent.mp4"
LEGACY_DEFAULT_CACHE_FILE = CACHE_DIR / f"default-{DEFAULT_VIDEO_ID}.mkv"
LEGACY_DEFAULT_CACHE_META_FILE = CACHE_DIR / f"default-{DEFAULT_VIDEO_ID}.json"
CACHE_SWITCH_SECONDS = max(1, int(os.environ.get("VIDEO_WALLPAPER_CACHE_SWITCH_SECONDS", "900")))


def virtual_screen_geometry(screen: Gdk.Screen) -> tuple[int, int, int, int]:
    display = screen.get_display() if screen is not None else Gdk.Display.get_default()
    if display is not None and hasattr(display, "get_n_monitors"):
        monitor_count = display.get_n_monitors()
        geometries = []
        for index in range(monitor_count):
            monitor = display.get_monitor(index)
            if monitor is not None:
                geometries.append(monitor.get_geometry())
        if geometries:
            left = min(g.x for g in geometries)
            top = min(g.y for g in geometries)
            right = max(g.x + g.width for g in geometries)
            bottom = max(g.y + g.height for g in geometries)
            return left, top, max(1, right - left), max(1, bottom - top)

    if screen is not None:
        return 0, 0, max(1, screen.get_width()), max(1, screen.get_height())
    return 0, 0, 1920, 1080


def process_table() -> list[tuple[int, str]]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    rows: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows.append((pid, parts[1] if len(parts) > 1 else ""))
    return rows


def is_video_wallpaper_runner(args: str) -> bool:
    parts = args.split()
    if not parts:
        return False
    command = Path(parts[0]).name
    if not command.startswith("python"):
        return False
    return any(Path(part).name == "video-wallpaper.py" for part in parts[1:4])


def is_wallpaper_mpv(args: str) -> bool:
    parts = args.split()
    if not parts or Path(parts[0]).name != "mpv":
        return False
    if (
        str(MPV_IPC_FILE) in args
        or str(CACHE_DIR) in args
        or str(PERMANENT_CORRUPT_FILE) in args
        or DEFAULT_VIDEO_SOURCE in args
    ):
        return True
    return "--wid=" in args and "--no-input-default-bindings" in args and "--no-input-terminal" in args


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_existing_wallpaper_processes() -> None:
    current_pid = os.getpid()
    targets = [
        pid
        for pid, args in process_table()
        if pid != current_pid and (is_video_wallpaper_runner(args) or is_wallpaper_mpv(args))
    ]
    if not targets:
        unlink_stale_mpv_ipc()
        return

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.monotonic() + 3.0
    remaining = targets
    while time.monotonic() < deadline:
        remaining = [pid for pid in remaining if process_alive(pid)]
        if not remaining:
            break
        time.sleep(0.08)

    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    unlink_stale_mpv_ipc()


def ensure_permanent_corrupt_video() -> Path:
    """Guarantees the permanent local green cyberpunk video (8VsvdCaSyaQ) exists offline."""
    PHOTO_WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    if valid_default_cache_file(PERMANENT_CORRUPT_FILE):
        return PERMANENT_CORRUPT_FILE

    migrate_default_cache_if_available()
    ensure_default_clip_from_legacy(DEFAULT_VIDEO_SOURCE, DEFAULT_VIDEO_TITLE)

    if valid_default_cache_file(DEFAULT_CACHE_FILE):
        tmp = PERMANENT_CORRUPT_FILE.with_name(f".{PERMANENT_CORRUPT_FILE.stem}.tmp.{os.getpid()}.mp4")
        try:
            if tmp.exists():
                tmp.unlink()
            try:
                os.link(DEFAULT_CACHE_FILE, tmp)
            except OSError:
                shutil.copy2(DEFAULT_CACHE_FILE, tmp)
            tmp.replace(PERMANENT_CORRUPT_FILE)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    if valid_default_cache_file(PERMANENT_CORRUPT_FILE):
        return PERMANENT_CORRUPT_FILE
    if valid_cache_file(DEFAULT_CACHE_FILE):
        return DEFAULT_CACHE_FILE
    raise RuntimeError(f"Permanent corrupt wallpaper video is unavailable: {PERMANENT_CORRUPT_FILE}")


class VideoWallpaper:
    def __init__(self, video_path: str, *, corrupt_mode: bool = False) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        PHOTO_WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
        terminate_existing_wallpaper_processes()

        self.corrupt_mode = corrupt_mode
        self.corrupt_controller = None
        self.resize_source_id: int | None = None
        self.switching = False
        self.quitting = False
        self.processes: list[subprocess.Popen[bytes]] = []
        self.windows: list[Gtk.Window] = []
        self.geom = (0, 0, 1920, 1080)

        if self.corrupt_mode:
            permanent_file = ensure_permanent_corrupt_video()
            self.video_path = str(permanent_file)
            self.title = CORRUPT_VIDEO_TITLE
            self.cache_enabled = True
            self.cache_file = permanent_file
            self.cache_meta_file = DEFAULT_CACHE_META_FILE
            self.playback_source = str(permanent_file)
            self.record_cache = False
        else:
            self.video_path = normalized_source(video_path)
            self.title = title_guess(self.video_path)
            # The built-in default must remain an offline wallpaper.
            self.cache_enabled = is_default_video(self.video_path) or not audio_enabled()
            if self.cache_enabled:
                migrate_default_cache_if_available()
                ensure_default_clip_from_legacy(self.video_path, self.title)
            self.cache_file, self.cache_meta_file = cache_paths_for(self.video_path)
            cached = cached_file_for(self.video_path) if self.cache_enabled else None
            if is_default_video(self.video_path) and cached is None:
                raise RuntimeError(f"default wallpaper cache is unavailable: {DEFAULT_CACHE_FILE}")
            self.playback_source = str(cached) if cached else self.video_path
            self.record_cache = (
                self.cache_enabled
                and is_remote_url(self.video_path)
                and self.playback_source == self.video_path
            )

        signal.signal(signal.SIGINT, self.quit)
        signal.signal(signal.SIGTERM, self.quit)

        if not self.corrupt_mode:
            if self.record_cache:
                reset_cache(self.video_path, self.title)
                threading.Thread(
                    target=build_cache_clip, args=(self.video_path, self.title), daemon=True
                ).start()
            write_current_meta(
                self.video_path,
                self.title,
                self.playback_source,
                cache_ready=self.is_cache_playback(),
            )
            update_history(self.video_path, self.title)
            if is_remote_url(self.video_path) and not self.is_cache_playback():
                threading.Thread(target=self.refresh_metadata, daemon=True).start()

        self.create_windows()
        GLib.timeout_add_seconds(2, self.check_players)

        if self.corrupt_mode:
            from corrupt_lockin_engine import CorruptLockInController

            x, y, w, h = self.geom
            gdk_win = self.windows[0].get_window()
            if gdk_win is None:
                raise RuntimeError("failed to acquire XID for corrupt GL renderer")
            self.corrupt_controller = CorruptLockInController(
                gdk_win.get_xid(),
                x,
                y,
                w,
                h,
                Path(self.playback_source),
                ready_file=CORRUPT_READY_FILE,
            )
            GLib.timeout_add(CorruptLockInController.TICK_MS, self.tick_corrupt)
        elif self.record_cache:
            GLib.timeout_add_seconds(CACHE_SWITCH_SECONDS, self.enable_cache_replay)

    def create_windows(self) -> None:
        screen = Gdk.Screen.get_default()
        if screen is None:
            raise RuntimeError("no GDK screen available")

        screen.connect("size-changed", self.on_screen_changed)
        screen.connect("monitors-changed", self.on_screen_changed)

        x, y, width, height = virtual_screen_geometry(screen)
        self.geom = (x, y, width, height)
        window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        window.set_title("video-wallpaper")
        window.set_name("video-wallpaper")
        window.set_role("video-wallpaper")
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_accept_focus(False)
        window.set_focus_on_map(False)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.set_type_hint(Gdk.WindowTypeHint.DESKTOP)
        window.stick()
        window.set_keep_below(True)
        window.move(x, y)
        window.set_default_size(width, height)
        window.resize(width, height)
        window.realize()
        gdk_window = window.get_window()
        if gdk_window is None:
            raise RuntimeError("failed to create wallpaper window")
        gdk_window.set_override_redirect(True)
        gdk_window.move_resize(x, y, width, height)
        window.show_all()

        empty_region = cairo.Region()
        gdk_window.input_shape_combine_region(empty_region, 0, 0)
        gdk_window.lower()
        self.windows.append(window)
        if not self.corrupt_mode:
            self.start_mpv(gdk_window.get_xid(), 0, self.playback_source)

    def on_screen_changed(self, *_args: object) -> None:
        if self.resize_source_id is not None:
            GLib.source_remove(self.resize_source_id)
        self.resize_source_id = GLib.timeout_add(250, self._apply_screen_geometry)

    def _apply_screen_geometry(self) -> bool:
        self.resize_source_id = None
        screen = Gdk.Screen.get_default()
        if screen is None:
            return False
        x, y, width, height = virtual_screen_geometry(screen)
        if (x, y, width, height) == self.geom:
            return False
        self.geom = (x, y, width, height)
        for window in self.windows:
            window.move(x, y)
            window.resize(width, height)
            gdk_window = window.get_window()
            if gdk_window is not None:
                gdk_window.move_resize(x, y, width, height)
                empty_region = cairo.Region()
                gdk_window.input_shape_combine_region(empty_region, 0, 0)
                gdk_window.lower()
        if self.corrupt_controller is not None:
            self.corrupt_controller.resize(x, y, width, height)
        return False

    def tick_corrupt(self) -> bool:
        if self.quitting or self.corrupt_controller is None:
            return False
        try:
            return self.corrupt_controller.tick()
        except Exception as exc:
            log_message(f"WARN: corrupt engine tick error: {exc}")
            return True

    def start_mpv(self, xid: int, index: int, source: str) -> None:
        if index == 0:
            unlink_stale_mpv_ipc()

        vo_driver = MPV_CORRUPT_VIDEO_OUTPUT if self.corrupt_mode else MPV_VIDEO_OUTPUT
        shader_args: list[str] = []
        if self.corrupt_mode and CORRUPT_SHADER_FILE.is_file():
            shader_args.append(f"--glsl-shader={CORRUPT_SHADER_FILE}")

        cmd = [
            "mpv",
            "--no-config",
            f"--wid={xid}",
            f"--vo={vo_driver}",
            f"--gpu-api={MPV_GPU_API}",
            f"--gpu-context={MPV_GPU_CONTEXT}",
            f"--hwdec={MPV_HWDEC}",
            "--vd-lavc-dr=yes",
            "--vd-lavc-threads=2",
            "--cache=yes",
            f"--demuxer-max-bytes={MPV_DEMUXER_MAX_BYTES}",
            f"--demuxer-max-back-bytes={MPV_DEMUXER_MAX_BACK_BYTES}",
            f"--cache-secs={MPV_CACHE_SECS}",
            "--loop-file=inf",
            "--no-osc",
            "--no-input-default-bindings",
            "--no-input-terminal",
            "--really-quiet",
            *shader_args,
            f"--ytdl-format={ytdl_format()}",
            *audio_args(index, force_silent=self.corrupt_mode),
            *ytdl_cookie_args(source),
            "--panscan=1",
            "--keepaspect=no",
            source,
        ]
        log_path = Path.home() / ".local/state/video-wallpaper.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as log_handle:
            proc = subprocess.Popen(
                cmd, stdout=log_handle, stderr=log_handle, start_new_session=True
            )
        self.processes.append(proc)

    def check_players(self) -> bool:
        if self.switching or self.quitting:
            return True
        if self.corrupt_mode:
            if self.corrupt_controller is None:
                self.quit()
                return False
        else:
            if not self.processes or all(proc.poll() is not None for proc in self.processes):
                self.quit()
                return False
        for window in self.windows:
            window.set_keep_below(True)
            gdk_window = window.get_window()
            if gdk_window is not None:
                gdk_window.lower()
        return True

    def refresh_metadata(self) -> None:
        metadata = fetch_metadata(self.video_path)
        title = str(metadata.get("title") or "").strip()
        if not title:
            return
        self.title = title
        update_history(self.video_path, title)
        write_current_meta(
            self.video_path,
            title,
            self.playback_source,
            cache_ready=self.is_cache_playback(),
        )

    def enable_cache_replay(self) -> bool:
        GLib.timeout_add_seconds(5, self.switch_to_cache_if_ready)
        return False

    def is_cache_playback(self) -> bool:
        return self.playback_source == str(self.cache_file)

    def switch_to_cache_if_ready(self) -> bool:
        if self.corrupt_mode or not self.record_cache or not self.cache_enabled or audio_enabled():
            return False
        if not valid_cache_file(self.cache_file):
            return True

        self.switching = True
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.processes:
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
        if not valid_cache_file(self.cache_file):
            self.switching = False
            return True

        self.processes = []
        self.record_cache = False
        self.playback_source = str(self.cache_file)
        write_cache_meta(self.video_path, self.title, ready=True)
        write_current_meta(self.video_path, self.title, self.playback_source, cache_ready=True)
        for index, window in enumerate(self.windows):
            gdk_window = window.get_window()
            if gdk_window is not None:
                self.start_mpv(gdk_window.get_xid(), index, self.playback_source)
        self.switching = False
        return False

    def quit(self, *_args: object) -> None:
        if self.quitting:
            return
        self.quitting = True
        if self.corrupt_controller is not None:
            try:
                self.corrupt_controller.close()
            except Exception:
                pass
            self.corrupt_controller = None
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.processes:
            try:
                proc.wait(timeout=0.8)
            except subprocess.TimeoutExpired:
                proc.kill()
        unlink_stale_mpv_ipc()
        Gtk.main_quit()


def normalized_source(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "ytdl"}:
        return source
    return str(Path(source).expanduser())


def is_remote_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https", "ytdl"}


def video_id(source: str) -> str:
    parsed = urlparse(source)
    host = parsed.netloc.lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if host.endswith("youtube.com"):
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [""])[0]
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"embed", "live", "shorts", "v"}:
            return parts[1]
    return ""


def is_default_video(source: str) -> bool:
    return video_id(source) == DEFAULT_VIDEO_ID or source == DEFAULT_VIDEO_SOURCE


def cache_paths_for(source: str) -> tuple[Path, Path]:
    if is_default_video(source):
        return DEFAULT_CACHE_FILE, DEFAULT_CACHE_META_FILE
    return CACHE_FILE, CACHE_META_FILE


def valid_cache_file(path: Path) -> bool:
    duration = media_duration_seconds(path)
    return duration is not None and duration >= 1


def valid_default_cache_file(path: Path) -> bool:
    duration = media_duration_seconds(path)
    minimum_duration = max(1, DEFAULT_CLIP_SECONDS - 5)
    return duration is not None and duration >= minimum_duration and media_has_video_stream(path)


def title_guess(source: str) -> str:
    if video_id(source) == DEFAULT_VIDEO_ID:
        return DEFAULT_VIDEO_TITLE
    if not is_remote_url(source):
        return Path(source).name
    return source


def audio_enabled() -> bool:
    return AUDIO_FILE.exists() and AUDIO_FILE.read_text().strip().lower() in {"1", "on", "true", "yes"}


def read_mpv_volume() -> int:
    try:
        raw = MPV_VOLUME_FILE.read_text().strip().rstrip("%")
        volume = int(float(raw))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        volume = 100
    return max(0, min(100, volume))


def mpv_muted() -> bool:
    try:
        return MPV_MUTE_FILE.read_text().strip().lower() in {"1", "on", "true", "yes", "muted"}
    except (FileNotFoundError, OSError):
        return False


def unlink_stale_mpv_ipc() -> None:
    for candidate in (
        MPV_IPC_FILE,
        CORRUPT_READY_FILE,
        MPV_IPC_FILE.parent / "wallpaper-mode-corrupt-0.bgra",
        MPV_IPC_FILE.parent / "wallpaper-mode-corrupt-1.bgra",
    ):
        try:
            candidate.unlink()
        except OSError:
            pass


def audio_args(index: int, *, force_silent: bool = False) -> list[str]:
    if index != 0:
        return ["--no-audio"]
    args = [
        f"--input-ipc-server={MPV_IPC_FILE}",
        f"--volume={read_mpv_volume()}",
    ]
    if force_silent or not audio_enabled():
        args.extend(["--no-audio", "--mute=yes"])
    elif mpv_muted():
        args.append("--mute=yes")
    else:
        args.append("--mute=no")
    return args


def ytdl_format() -> str:
    return MPV_YTDL_FORMAT if audio_enabled() else MPV_YTDL_FORMAT_NO_AUDIO


def ytdl_cookie_args(source: str) -> list[str]:
    parsed = urlparse(source)
    if parsed.scheme not in {"http", "https", "ytdl"}:
        return []
    if not YTDL_COOKIES_FROM_BROWSER:
        return []
    return [f"--ytdl-raw-options=cookies-from-browser={YTDL_COOKIES_FROM_BROWSER}"]


def yt_dlp_cookie_args() -> list[str]:
    if not YTDL_COOKIES_FROM_BROWSER:
        return []
    return ["--cookies-from-browser", YTDL_COOKIES_FROM_BROWSER]


def log_message(message: str) -> None:
    log_path = Path.home() / ".local" / "state" / "video-wallpaper.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}\n")


def usable_source_file(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 1024 * 1024
    except OSError:
        return False


def media_duration_seconds(path: Path) -> float | None:
    if not usable_source_file(path):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    try:
        duration = float(raw)
    except ValueError:
        return None
    if duration <= 0:
        return None
    return duration


def media_has_video_stream(path: Path) -> bool:
    if not usable_source_file(path):
        return False
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "video" in proc.stdout.splitlines()


def safe_slug(text: str, fallback: str) -> str:
    chars: list[str] = []
    previous_dash = False
    for char in text.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    slug = "".join(chars).strip("-")
    return (slug or fallback)[:96].strip("-") or fallback


def publish_video_to_photos(source: str, title: str, path: Path) -> None:
    if not valid_cache_file(path):
        return
    PHOTO_WALLPAPER_DIR.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix if path.suffix else ".mp4"
    if is_default_video(source):
        saved_path = DEFAULT_CACHE_FILE
    else:
        source_slug = video_id(source) or safe_slug(title or source, "video-wallpaper")
        saved_path = PHOTO_WALLPAPER_DIR / f"{source_slug}-first-15min{suffix}"

    try:
        source_path = path.resolve()
        target_path = saved_path.resolve()
    except OSError:
        source_path = path
        target_path = saved_path

    if source_path != target_path:
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = saved_path.with_name(f".{saved_path.name}.tmp.{os.getpid()}")
        try:
            if tmp.exists():
                tmp.unlink()
            try:
                os.link(path, tmp)
            except OSError:
                shutil.copy2(path, tmp)
            tmp.replace(saved_path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    current_link = PHOTO_WALLPAPER_DIR / "current-video.mp4"
    try:
        current_link.unlink()
    except OSError:
        pass
    try:
        current_link.symlink_to(saved_path.name)
    except OSError:
        shutil.copy2(saved_path, current_link)

    write_json(
        PHOTO_WALLPAPER_DIR / "current-video.json",
        {
            "source": source,
            "title": title,
            "id": video_id(source),
            "path": str(saved_path),
            "duration": media_duration_seconds(saved_path),
            "updated_at": int(time.time()),
        },
    )


def run_ffmpeg_clip(input_path: Path, output_path: Path, seconds: int) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix if output_path.suffix else ".mp4"
    tmp = output_path.with_name(f".{output_path.stem}.tmp.{os.getpid()}{suffix}")
    for candidate in (tmp, output_path):
        try:
            candidate.unlink()
        except OSError:
            pass

    copy_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-t",
        str(seconds),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(tmp),
    ]
    transcode_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-t",
        str(seconds),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "scale='min(1280,iw)':-2,fps=30",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(tmp),
    ]

    for cmd in (transcode_cmd, copy_cmd):
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(1800, seconds * 4),
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            log_message(f"ERROR: ffmpeg clip build failed for {input_path}: {exc}")
            return False
        if proc.returncode == 0 and valid_cache_file(tmp):
            tmp.replace(output_path)
            return True
        try:
            tmp.unlink()
        except OSError:
            pass
        if proc.stderr.strip():
            log_message(f"WARN: ffmpeg clip attempt failed for {input_path}: {proc.stderr.strip()[:500]}")

    return False


def download_remote_clip(source: str, output_path: Path, seconds: int) -> bool:
    download_dir = CACHE_DIR / f"yt-dlp-{os.getpid()}-{int(time.time())}"
    download_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(download_dir / "clip.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--download-sections",
        f"*0-{seconds}",
        "--force-keyframes-at-cuts",
        "--merge-output-format",
        "mp4",
        "-f",
        MPV_YTDL_FORMAT_NO_AUDIO,
        *yt_dlp_cookie_args(),
        "-o",
        output_template,
        source,
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(1800, seconds * 4),
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        log_message(f"ERROR: yt-dlp clip download failed for {source}: {exc}")
        shutil.rmtree(download_dir, ignore_errors=True)
        return False
    if proc.returncode != 0:
        log_message(f"ERROR: yt-dlp clip download failed for {source}: {proc.stderr.strip()[:500]}")
        shutil.rmtree(download_dir, ignore_errors=True)
        return False

    candidates = sorted(
        [path for path in download_dir.iterdir() if path.is_file()],
        key=lambda item: item.stat().st_size,
        reverse=True,
    )
    for candidate in candidates:
        if run_ffmpeg_clip(candidate, output_path, seconds):
            shutil.rmtree(download_dir, ignore_errors=True)
            return True

    shutil.rmtree(download_dir, ignore_errors=True)
    return False


def build_cache_clip(source: str, title: str) -> None:
    cache_file, _cache_meta_file = cache_paths_for(source)
    write_cache_meta(source, title, ready=False)
    seconds = DEFAULT_CLIP_SECONDS
    ok = (
        download_remote_clip(source, cache_file, seconds)
        if is_remote_url(source)
        else run_ffmpeg_clip(Path(source), cache_file, seconds)
    )
    if not ok:
        log_message(f"ERROR: could not build finite wallpaper clip for {source}")
        return
    write_cache_meta(source, title, ready=True)
    publish_video_to_photos(source, title, cache_file)


def ensure_default_clip_from_legacy(source: str, title: str) -> None:
    if not is_default_video(source) or valid_default_cache_file(DEFAULT_CACHE_FILE):
        return
    for legacy_path in (LEGACY_DEFAULT_CACHE_FILE, LEGACY_CACHE_FILE):
        if usable_source_file(legacy_path):
            log_message(f"INFO: building finite default wallpaper clip from legacy cache {legacy_path}")
            if run_ffmpeg_clip(legacy_path, DEFAULT_CACHE_FILE, DEFAULT_CLIP_SECONDS):
                write_cache_meta(DEFAULT_VIDEO_SOURCE, title or DEFAULT_VIDEO_TITLE, ready=True)
                publish_video_to_photos(DEFAULT_VIDEO_SOURCE, title or DEFAULT_VIDEO_TITLE, DEFAULT_CACHE_FILE)
                return


def read_json(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def update_history(source: str, title: str) -> None:
    raw = read_json(HISTORY_FILE, [])
    entries = raw if isinstance(raw, list) else []
    existing_title = ""
    deduped: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("source") == source:
            existing_title = str(entry.get("title") or "")
            continue
        deduped.append(entry)
    if title == source and existing_title:
        title = existing_title
    deduped.insert(
        0,
        {
            "source": source,
            "title": title,
            "id": video_id(source),
            "updated_at": int(time.time()),
        },
    )
    write_json(HISTORY_FILE, deduped[:10])


def write_current_meta(source: str, title: str, playback_source: str, cache_ready: bool) -> None:
    cache_file, _cache_meta_file = cache_paths_for(source)
    if not is_remote_url(playback_source):
        publish_video_to_photos(source, title, Path(playback_source))
    write_json(
        CURRENT_META_FILE,
        {
            "source": source,
            "title": title,
            "id": video_id(source),
            "playback_source": playback_source,
            "audio_enabled": audio_enabled(),
            "cache_ready": cache_ready,
            "cache_file": str(cache_file),
            "updated_at": int(time.time()),
        },
    )


def write_cache_meta(source: str, title: str, ready: bool) -> None:
    cache_file, cache_meta_file = cache_paths_for(source)
    duration = media_duration_seconds(cache_file) if ready else None
    write_json(
        cache_meta_file,
        {
            "source": source,
            "title": title,
            "id": video_id(source),
            "path": str(cache_file),
            "ready": ready,
            "duration": duration,
            "updated_at": int(time.time()),
        },
    )


def reset_cache(source: str, title: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file, cache_meta_file = cache_paths_for(source)
    for child in (cache_file, cache_meta_file):
        try:
            child.unlink()
        except FileNotFoundError:
            pass
    write_cache_meta(source, title, ready=False)


def cached_file_for(source: str) -> Path | None:
    _cache_file, cache_meta_file = cache_paths_for(source)
    meta = read_json(cache_meta_file, {})
    if not isinstance(meta, dict):
        return None
    if meta.get("source") != source or meta.get("ready") is not True:
        return None
    path = Path(str(meta.get("path") or _cache_file))
    if valid_default_cache_file(path) if is_default_video(source) else valid_cache_file(path):
        return path
    return None


def migrate_default_cache_if_available() -> None:
    if valid_default_cache_file(DEFAULT_CACHE_FILE):
        return
    candidates = (
        (LEGACY_DEFAULT_CACHE_FILE, LEGACY_DEFAULT_CACHE_META_FILE),
        (LEGACY_CACHE_FILE, LEGACY_CACHE_META_FILE),
    )
    for cache_file, cache_meta_file in candidates:
        meta = read_json(cache_meta_file, {})
        if not isinstance(meta, dict):
            continue
        if meta.get("source") != DEFAULT_VIDEO_SOURCE or meta.get("ready") is not True:
            continue
        if not usable_source_file(cache_file):
            continue
        title = str(meta.get("title") or DEFAULT_VIDEO_TITLE)
        if run_ffmpeg_clip(cache_file, DEFAULT_CACHE_FILE, DEFAULT_CLIP_SECONDS):
            write_cache_meta(DEFAULT_VIDEO_SOURCE, title, ready=True)
            publish_video_to_photos(DEFAULT_VIDEO_SOURCE, title, DEFAULT_CACHE_FILE)
            return


def fetch_metadata(source: str) -> dict[str, object]:
    if not is_remote_url(source):
        return {"title": title_guess(source), "id": ""}
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--skip-download",
        "--no-playlist",
        *yt_dlp_cookie_args(),
        source,
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return {"title": title_guess(source), "id": video_id(source)}
    if proc.returncode != 0:
        return {"title": title_guess(source), "id": video_id(source)}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"title": title_guess(source), "id": video_id(source)}
    if not isinstance(data, dict):
        return {"title": title_guess(source), "id": video_id(source)}
    return data


def main() -> int:
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XAUTHORITY", str(Path.home() / ".Xauthority"))

    if len(sys.argv) == 2 and sys.argv[1] in {"--corrupt", "--lockin", "--locked-in"}:
        VideoWallpaper("", corrupt_mode=True)
        Gtk.main()
        return 0

    if len(sys.argv) != 2:
        print("usage: video-wallpaper.py [--corrupt | /path/or/url/to/video]", file=sys.stderr)
        return 2

    VideoWallpaper(sys.argv[1], corrupt_mode=False)
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
