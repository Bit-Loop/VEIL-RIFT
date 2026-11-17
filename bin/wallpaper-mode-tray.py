#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import subprocess
import time
import warnings
from typing import Any

import gi

warnings.filterwarnings("ignore", category=DeprecationWarning)

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


CMD = str(pathlib.Path.home() / ".local" / "bin" / "wallpaper-mode")
ICON_DIR = pathlib.Path.home() / ".local" / "share" / "wallpaper-mode"
CORRUPT_ICON = ICON_DIR / "wallpaper-corrupt.svg"
STATIC_ICON = ICON_DIR / "wallpaper-static.svg"
VIDEO_ICON = ICON_DIR / "wallpaper-video.svg"
WARNING_ICON = ICON_DIR / "wallpaper-warning.svg"
SCROLL_DEBOUNCE_SECONDS = 0.35


def run(args: list[str], timeout: float = 3.0) -> str | None:
    try:
        proc = subprocess.run(
            [CMD, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def spawn(args: list[str]) -> None:
    subprocess.Popen([CMD, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class WallpaperModeTray:
    def __init__(self) -> None:
        ensure_icons()
        self.icon = Gtk.StatusIcon()
        self.icon.set_visible(True)
        self.icon.connect("activate", self.toggle)
        self.icon.connect("popup-menu", self.popup_menu)
        self.icon.connect("scroll-event", self.on_scroll)
        self.state: dict[str, Any] = {}
        self.last_scroll_at = 0.0
        self.update()
        GLib.timeout_add_seconds(3, self.update)

    def read_state(self) -> dict[str, Any]:
        raw = run(["status-json"])
        if not raw:
            return {"mode": "unknown", "video_running": False}
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return {"mode": "unknown", "video_running": False}
        return state if isinstance(state, dict) else {"mode": "unknown"}

    def update(self) -> bool:
        self.state = self.read_state()
        mode = str(self.state.get("mode") or "unknown")
        running = bool(self.state.get("video_running"))
        corrupt_running = bool(self.state.get("corrupt_running"))
        raytrace = bool(self.state.get("raytrace_enabled"))
        audio = bool(self.state.get("audio_enabled"))
        picom_running = bool(self.state.get("picom_running"))
        volume = volume_status(self.state)
        title = truncate(str(self.state.get("title") or ""), 72)
        history = self.state.get("history")

        if mode == "corrupt":
            icon_file = CORRUPT_ICON
            icon_name = "utilities-terminal"
            tip_lines = [
                "Mode: Locked-In Corruption (Glitch-BBS + Game of Life)"
                + (" [active]" if corrupt_running or running else " [starting]"),
                f"GPU Raytracing: {'on' if raytrace else 'off'}",
                "Video: Permanent 8VsvdCaSyaQ Hacker Matrix",
            ]
        elif mode == "video":
            icon_file = VIDEO_ICON
            icon_name = "media-playback-start"
            tip_lines = ["Mode: Video Wallpaper" + (" [running]" if running else " [selected]")]
        elif mode == "static":
            icon_file = STATIC_ICON
            icon_name = "applications-games"
            tip_lines = ["Mode: Static + Gaming (Live wallpaper & Picom OFF)"]
        else:
            icon_file = WARNING_ICON
            icon_name = "dialog-question"
            tip_lines = ["Wallpaper mode unknown"]

        tip_lines.append("Compositor (picom): " + ("running" if picom_running else "stopped (gaming)"))
        if mode == "video":
            tip_lines.append("Audio: " + ("enabled" if audio else "disabled"))
            if volume:
                tip_lines.append(f"Video volume: {volume}")
            if title:
                tip_lines.append(f"Current: {title}")

        if isinstance(history, list) and history and mode == "video":
            tip_lines.append("Recent videos:")
            for index, entry in enumerate(history[:6], 1):
                if not isinstance(entry, dict):
                    continue
                entry_title = truncate(str(entry.get("title") or entry.get("source") or ""), 64)
                if entry_title:
                    tip_lines.append(f"{index}. {entry_title}")

        tip_lines.append("Left-click: cycle modes (Corrupt -> Static/Gaming -> Video)")
        tip = "\n".join(tip_lines)

        if icon_file.exists():
            self.icon.set_from_file(str(icon_file))
        else:
            self.icon.set_from_icon_name(icon_name)
        self.icon.set_tooltip_text(tip)
        return True

    def schedule_updates(self) -> None:
        for delay_ms in (450, 1250, 2600):
            GLib.timeout_add(delay_ms, self.update_once)

    def on_scroll(self, _icon: Gtk.StatusIcon, event: Gdk.EventScroll) -> bool:
        now = time.monotonic()
        if now - self.last_scroll_at < SCROLL_DEBOUNCE_SECONDS:
            return True
        self.last_scroll_at = now

        direction = getattr(event, "direction", None)
        if direction == Gdk.ScrollDirection.UP:
            adjust_volume("up")
        elif direction == Gdk.ScrollDirection.DOWN:
            adjust_volume("down")
        elif direction == Gdk.ScrollDirection.SMOOTH:
            delta_y = getattr(event, "delta_y", 0.0)
            if delta_y < 0:
                adjust_volume("up")
            elif delta_y > 0:
                adjust_volume("down")
            else:
                return False
        else:
            return False
        GLib.timeout_add(250, self.update_once)
        return True

    def toggle(self, *_args: object) -> None:
        spawn(["toggle"])
        self.schedule_updates()

    def update_once(self) -> bool:
        self.update()
        return False

    def popup_menu(self, _icon: Gtk.StatusIcon, button: int, activate_time: int) -> None:
        mode = str(self.state.get("mode") or "static")
        picom_running = bool(self.state.get("picom_running"))
        raytrace = bool(self.state.get("raytrace_enabled"))
        menu = Gtk.Menu()

        mark = lambda m: "● " if mode == m else "○ "
        self.add_item(
            menu,
            f"{mark('corrupt')}Locked-In Corruption (Glitch-BBS)",
            lambda *_: self.run_and_update(["corrupt"]),
        )
        self.add_item(
            menu,
            "✦ Light GPU Raytracing: ON" if raytrace else "✧ Light GPU Raytracing: OFF",
            lambda *_: self.run_and_update(["raytrace-toggle"]),
        )
        self.add_item(
            menu,
            "Switch to Corruption + GPU Raytracing",
            lambda *_: self.run_and_update(["corrupt-rt"]),
        )
        self.add_item(
            menu,
            f"{mark('video')}Video Wallpaper",
            lambda *_: self.run_and_update(["video"]),
        )
        self.add_item(
            menu,
            f"{mark('static')}Static + Gaming Mode (Picom Off)",
            lambda *_: self.run_and_update(["static"]),
        )

        self.add_separator(menu)
        if mode == "static":
            self.add_item(
                menu,
                "Exit Gaming Mode (Restore Live Wallpaper)",
                lambda *_: self.run_and_update(["game-mode-off"]),
            )
        else:
            self.add_item(
                menu,
                "Quick Enter Static / Gaming Mode",
                lambda *_: self.run_and_update(["static"]),
            )

        self.add_separator(menu)
        if bool(self.state.get("audio_enabled")):
            self.add_item(menu, "Disable Video Audio", lambda *_: self.run_and_update(["audio-toggle"]))
        else:
            self.add_item(menu, "Enable Video Audio (Video Mode)", lambda *_: self.run_and_update(["audio-toggle"]))
        self.add_volume_menu(menu)

        self.add_separator(menu)
        self.add_item(menu, "Default Hacker Video (Video Mode)", lambda *_: self.run_and_update(["default-video"]))
        self.add_recent_menu(menu)
        self.add_item(menu, "Set Video URL...", lambda *_: self.prompt_source())

        self.add_separator(menu)
        self.add_item(menu, "Refresh Static APOD/Bing Wallpaper", lambda *_: self.run_and_update(["refresh-static"]))
        self.add_item(
            menu,
            f"State: {mode} | rt: {'on' if raytrace else 'off'} | picom: {'on' if picom_running else 'off'}",
            None,
        )
        self.add_item(menu, "Quit Wallpaper Tray", lambda *_: Gtk.main_quit())
        menu.show_all()
        menu.popup(None, None, None, None, button, activate_time)

    def run_and_update(self, args: list[str]) -> None:
        spawn(args)
        self.schedule_updates()

    def add_item(self, menu: Gtk.Menu, label: str, callback: object | None) -> None:
        item = Gtk.MenuItem(label=label)
        if callback is None:
            item.set_sensitive(False)
        else:
            item.connect("activate", callback)
        menu.append(item)

    def add_separator(self, menu: Gtk.Menu) -> None:
        menu.append(Gtk.SeparatorMenuItem())

    def add_volume_menu(self, menu: Gtk.Menu) -> None:
        submenu = Gtk.Menu()
        self.add_item(submenu, "Volume Up", lambda *_: self.run_volume_action("up"))
        self.add_item(submenu, "Volume Down", lambda *_: self.run_volume_action("down"))
        self.add_item(submenu, "Toggle Mute", lambda *_: self.run_mute_toggle())
        volume = volume_status(self.state)
        if volume:
            self.add_separator(submenu)
            self.add_item(submenu, f"Current: {volume}", None)
        item = Gtk.MenuItem(label="Video Volume")
        item.set_submenu(submenu)
        menu.append(item)

    def add_recent_menu(self, menu: Gtk.Menu) -> None:
        history = self.state.get("history")
        submenu = Gtk.Menu()
        count = 0
        if isinstance(history, list):
            for entry in history[:10]:
                if not isinstance(entry, dict):
                    continue
                source = str(entry.get("source") or "").strip()
                if not source:
                    continue
                title = truncate(str(entry.get("title") or source), 72)
                self.add_item(
                    submenu,
                    title,
                    lambda _item, selected=source: self.run_and_update(["set-source-start", selected]),
                )
                count += 1
        if count == 0:
            self.add_item(submenu, "No recent videos", None)
        item = Gtk.MenuItem(label="Recent Videos (Video Mode)")
        item.set_submenu(submenu)
        menu.append(item)

    def run_volume_action(self, delta: str) -> None:
        adjust_volume(delta)
        GLib.timeout_add(250, self.update_once)

    def run_mute_toggle(self) -> None:
        spawn(["mpv-mute-toggle"])
        GLib.timeout_add(250, self.update_once)

    def prompt_source(self) -> None:
        dialog = Gtk.Dialog(title="Video Wallpaper URL (Video Mode)")
        dialog.add_buttons(
            "Cancel",
            Gtk.ResponseType.CANCEL,
            "Save",
            Gtk.ResponseType.ACCEPT,
            "Save + Start Video Mode",
            Gtk.ResponseType.OK,
        )
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_resizable(False)

        entry = Gtk.Entry()
        entry.set_width_chars(72)
        entry.set_activates_default(True)
        entry.set_text(run(["source"]) or "")
        entry.set_placeholder_text("https://youtu.be/...")
        dialog.get_content_area().set_spacing(8)
        dialog.get_content_area().set_margin_top(10)
        dialog.get_content_area().set_margin_bottom(10)
        dialog.get_content_area().set_margin_start(10)
        dialog.get_content_area().set_margin_end(10)
        dialog.get_content_area().add(entry)
        dialog.show_all()

        response = dialog.run()
        source = entry.get_text().strip()
        dialog.destroy()
        if response not in (Gtk.ResponseType.ACCEPT, Gtk.ResponseType.OK) or not source:
            return
        if response == Gtk.ResponseType.OK:
            spawn(["set-source-start", source])
        else:
            spawn(["set-source", source])
        self.schedule_updates()


def ensure_icons() -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    icons = {
        CORRUPT_ICON: """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect width="22" height="22" rx="2" fill="#000000"/>
  <rect x="1.5" y="1.5" width="19" height="19" rx="1" fill="#070002" stroke="#c40814" stroke-width="1.3" stroke-dasharray="3 1.5"/>
  <path d="M5 7l2.5 2.5M7.5 7L5 9.5M14.5 7L17 9.5M17 7l-2.5 2.5" stroke="#c40814" stroke-width="1.7" stroke-linecap="square"/>
  <rect x="4" y="13" width="2.6" height="2.6" fill="#00e03d"/>
  <rect x="7.5" y="13" width="2.6" height="2.6" fill="#cc3b00"/>
  <rect x="11" y="15.5" width="2.6" height="2.6" fill="#00cc70"/>
  <rect x="14.5" y="13" width="2.6" height="2.6" fill="#6e020a"/>
  <path d="M3 11.5h16" stroke="#00e03d" stroke-width="1" stroke-opacity="0.72"/>
</svg>
""",
        STATIC_ICON: """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect width="22" height="22" rx="3" fill="#09111f"/>
  <rect x="2.5" y="3.5" width="17" height="11.5" rx="1.5" fill="#133a4a" stroke="#49d8ff" stroke-width="1.2"/>
  <path d="M3.8 14l4-3.8 2.6 2.3 2-1.9 5.2 3.4z" fill="#30f2a3"/>
  <circle cx="15.8" cy="6.8" r="1.6" fill="#ffd166"/>
  <path d="M6.5 18.2h9" stroke="#ff4f88" stroke-width="2" stroke-linecap="round"/>
  <circle cx="5" cy="18.2" r="1.2" fill="#49d8ff"/>
  <circle cx="17" cy="18.2" r="1.2" fill="#30f2a3"/>
</svg>
""",
        VIDEO_ICON: """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect width="22" height="22" rx="3" fill="#090d13"/>
  <path d="M4 4h14v14H4z" fill="#151827" stroke="#ff4f88" stroke-width="1.2"/>
  <path d="M9 7.2v7.6l6-3.8z" fill="#49d8ff"/>
  <path d="M5 6h2M5 10h2M5 14h2M15 6h2M15 10h2M15 14h2" stroke="#30f2a3" stroke-width="1.1" stroke-linecap="round"/>
</svg>
""",
        WARNING_ICON: """<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">
  <rect width="22" height="22" rx="3" fill="#160d11"/>
  <path d="M11 3.5l8 15H3z" fill="#ffb703" stroke="#ff4f88" stroke-width="1.2"/>
  <path d="M11 8v4.5" stroke="#111" stroke-width="1.8" stroke-linecap="round"/>
  <circle cx="11" cy="15.5" r="1" fill="#111"/>
</svg>
""",
    }
    for path, content in icons.items():
        if not path.exists() or path.read_text() != content:
            path.write_text(content)


def truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "..."


def volume_status(state: dict[str, Any] | None = None) -> str:
    state = state or {}
    volume = state.get("mpv_volume")
    muted = bool(state.get("mpv_effective_muted"))
    if isinstance(volume, (int, float)):
        return f"{int(volume)}% ({'muted' if muted else 'on'})"
    return run(["mpv-volume-status"]) or ""


def adjust_volume(direction: str) -> None:
    if direction == "up":
        spawn(["mpv-volume-up"])
    elif direction == "down":
        spawn(["mpv-volume-down"])


if __name__ == "__main__":
    WallpaperModeTray()
    Gtk.main()
