"""
main.py

Configuration inclinometre de Baumer
-------------------------------------
Desktop GUI (Tkinter) that connects to a Baumer inclinometer over a
PCAN-USB adapter and runs the configuration sequence described in the
vendor notes:

    1. Read HeartBeat producer time (0x1017:00). If it isn't already at
       the target value, write it, then confirm the 0x700+NodeID
       heartbeat frame is actually cycling at that period.
    2. Read the Filter value (0x2603:00). If it isn't already at the
       target value, write it and verify by reading it back.
    3. Store parameters ("save" -> 0x1010:01).
    4. Reload / verify parameters ("load" -> 0x1011:01) and confirm the
       filter value is still correct.

Run with:
    python main.py

Requires a PCAN-USB adapter + the PEAK PCAN-Basic driver installed, and
`pip install python-can`. If no adapter is found, the app offers to run
in Simulation mode against a built-in fake sensor so the UI can still be
exercised end to end.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox
from typing import Callable, Optional

from canopen_client import CanopenClient, CanopenConfig, SdoError

try:
    from PIL import Image, ImageTk
except ImportError:  # Pillow not installed -> fall back to the drawn icon
    Image = None
    ImageTk = None

ASSET_DIR = Path(__file__).resolve().parent / "assets"
SENSOR_PHOTO_PATH = ASSET_DIR / "baumer.webp"

# --------------------------------------------------------------------------- #
# Target configuration values (per the vendor configuration notes)
# --------------------------------------------------------------------------- #
DEFAULT_HEARTBEAT_MS = 1000
DEFAULT_FILTER = 5
DEFAULT_NODE_ID = 1

# --------------------------------------------------------------------------- #
# Modern dark palette
# --------------------------------------------------------------------------- #
APP_BG = "#0e1015"
CARD_BG = "#1a1d24"
CHIP_BG = "#20242d"
BORDER = "#2a2e38"
TITLEBAR_BG = "#1c1c1c"
BLACK_BOX = "#0d0d0d"
PHOTO_CARD_BG = "#3f3f3f"

ACCENT = "#5b8cff"
ACCENT_HOVER = "#6f99ff"
ACCENT_SOFT = "#1e2740"

GREEN = "#34d399"
GREEN_SOFT = "#123227"
RED = "#ff6b74"
RED_SOFT = "#3a1d22"

TEXT_PRIMARY = "#f2f4f8"
TEXT_SECONDARY = "#9aa1b1"
TEXT_MUTED = "#6b7280"
DISABLED_BG = "#2a2e38"
DISABLED_FG = "#6b7280"

FONT_FAMILY = "Segoe UI"


def rounded_rect_points(x1, y1, x2, y2, r):
    """Point list for a smooth rounded-rectangle polygon."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]


@dataclass
class UiState:
    connected: bool = False
    progress: int = 0
    status_text: str = "Prêt à lancer la configuration"
    status_color: str = TEXT_SECONDARY
    running: bool = False


# --------------------------------------------------------------------------- #
# Small reusable "modern" widgets built on Canvas (Tk has no native
# rounded corners / hover states)
# --------------------------------------------------------------------------- #
class RoundedButton(tk.Canvas):
    def __init__(
        self, master, text, command: Optional[Callable] = None, *,
        bg_color=ACCENT, hover_color=ACCENT_HOVER, fg_color=TEXT_PRIMARY,
        font_size=10, bold=True, radius=10, padx=20, pady=11,
        min_width=0, icon="", parent_bg=CARD_BG,
    ):
        self.command = command
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.fg_color = fg_color
        self.disabled = False
        self.text_value = text
        self.icon = icon
        self.radius = radius
        self.padx = padx
        self.pady = pady

        self._font = tkfont.Font(
            family=FONT_FAMILY, size=font_size, weight="bold" if bold else "normal"
        )
        width, height = self._measure(min_width)
        super().__init__(master, width=width, height=height, bg=parent_bg, highlightthickness=0)
        self._render(self.bg_color)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _label(self):
        return f"{self.icon}  {self.text_value}" if self.icon else self.text_value

    def _measure(self, min_width):
        tw = self._font.measure(self._label())
        th = self._font.metrics("linespace")
        return max(min_width, tw + 2 * self.padx), th + 2 * self.pady

    def _render(self, fill):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        pts = rounded_rect_points(1, 1, w - 1, h - 1, self.radius)
        self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        color = DISABLED_FG if self.disabled else self.fg_color
        self.create_text(w / 2, h / 2, text=self._label(), fill=color, font=self._font)

    def _on_enter(self, _e):
        if not self.disabled:
            self.config(cursor="hand2")
            self._render(self.hover_color)

    def _on_leave(self, _e):
        if not self.disabled:
            self._render(self.bg_color)

    def _on_click(self, _e):
        if not self.disabled and self.command:
            self.command()

    def set_text(self, text: str):
        self.text_value = text
        w, h = self._measure(0)
        self.config(width=max(w, int(self["width"])))
        self._render(self.hover_color if str(self["cursor"]) == "hand2" else self.bg_color)

    def set_enabled(self, enabled: bool):
        self.disabled = not enabled
        self._render(DISABLED_BG if not enabled else self.bg_color)


class StatusPill(tk.Canvas):
    """A small rounded badge with a coloured dot + label, e.g. connection status."""

    def __init__(self, master, text, dot_color, *, parent_bg=CARD_BG, min_width=0):
        self.dot_color = dot_color
        self.text_value = text
        self._font = tkfont.Font(family=FONT_FAMILY, size=9, weight="bold")
        tw = self._font.measure(text)
        width = max(min_width, tw + 40)
        height = 26
        super().__init__(master, width=width, height=height, bg=parent_bg, highlightthickness=0)
        self._render()

    def _render(self):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        pts = rounded_rect_points(0, 0, w, h, h / 2)
        self.create_polygon(pts, smooth=True, fill=CHIP_BG, outline=BORDER)
        self.create_oval(10, h / 2 - 4, 18, h / 2 + 4, fill=self.dot_color, outline=self.dot_color)
        self.create_text(
            26, h / 2, text=self.text_value, fill=TEXT_PRIMARY, font=self._font, anchor="w"
        )

    def update_status(self, text: str, dot_color: str):
        self.text_value = text
        self.dot_color = dot_color
        tw = self._font.measure(text)
        self.config(width=tw + 40)
        self._render()


class RoundedProgressBar(tk.Canvas):
    def __init__(self, master, height=16, track_color=CHIP_BG, fill_color=ACCENT, parent_bg=CARD_BG):
        super().__init__(master, height=height, bg=parent_bg, highlightthickness=0)
        self.track_color = track_color
        self.fill_color = fill_color
        self.progress = 0
        self.bind("<Configure>", lambda _e: self._render())

    def set_progress(self, pct: int, color: Optional[str] = None):
        self.progress = max(0, min(100, pct))
        if color:
            self.fill_color = color
        self._render()

    def _render(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            return
        r = h / 2
        pts = rounded_rect_points(0, 0, w, h, r)
        self.create_polygon(pts, smooth=True, fill=self.track_color, outline=self.track_color)
        fill_w = w * (self.progress / 100.0)
        if fill_w > 1:
            fr = min(r, fill_w / 2)
            pts2 = rounded_rect_points(0, 0, fill_w, h, fr)
            self.create_polygon(pts2, smooth=True, fill=self.fill_color, outline=self.fill_color)


class SensorPhoto(tk.Frame):
    """Displays the real Baumer sensor photo (assets/baumer.webp) inside a
    small card that matches the photo's own background so it blends
    cleanly into the dark theme.

    The photo is scaled to a target display width to make it appear larger
    in the UI (upscaling if necessary).
    """

    def __init__(self, master, image_path: Path, display_width: int = 440):
        super().__init__(master, bg=PHOTO_CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        img = Image.open(image_path).convert("RGBA")
        w, h = img.size
        if w != display_width:
            scale = display_width / w
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)  # keep a reference alive
        tk.Label(self, image=self._photo, bg=PHOTO_CARD_BG).pack(padx=16, pady=16)


class SensorIcon(tk.Canvas):
    """Show the sensor photo if available; fall back to a stylised drawing.

    Looks for `baumer.webp` first in the `assets/` directory, then in the
    same folder as `main.py`. Uses Pillow (Image, ImageTk) when available.
    """

    def __init__(self, master, **kwargs):
        super().__init__(master, width=440, height=340, bg=CARD_BG, highlightthickness=0, **kwargs)
        # Prefer the packaged asset, otherwise the local file next to main.py
        photo_path = SENSOR_PHOTO_PATH
        if not photo_path.exists():
            photo_path = Path(__file__).resolve().parent / "baumer.webp"

        if Image is not None and ImageTk is not None and photo_path.exists():
            try:
                img = Image.open(photo_path)
                img = img.resize((440, 340), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(img)
                self.create_image(0, 0, anchor="nw", image=self._photo)
                return
            except Exception:
                # If loading fails, fall back to the drawn icon below
                pass

        self._draw()

    def _draw(self):
        # soft drop shadow
        self.create_oval(55, 150, 195, 168, fill="#0a0b0e", outline="")
        # cable
        self.create_line(35, 140, 72, 112, width=8, fill="#3a3f4a", capstyle=tk.ROUND)
        self.create_line(72, 112, 96, 100, width=8, fill="#3a3f4a", capstyle=tk.ROUND)
        # body (isometric-ish block) in accent-tinted neutrals
        self.create_polygon(
            96, 88, 176, 52, 210, 70, 130, 108,
            fill="#e7eaf2", outline="#c3c8d6", width=1.5,
        )
        self.create_polygon(
            130, 108, 210, 70, 210, 100, 130, 138,
            fill="#c9cee0", outline="#c3c8d6", width=1.5,
        )
        self.create_polygon(
            96, 88, 130, 108, 130, 138, 96, 118,
            fill="#d9dded", outline="#c3c8d6", width=1.5,
        )
        # mounting hole
        try:
            self.create_oval(166, 78, 186, 98, fill=ACCENT_SOFT, outline=ACCENT)
        except Exception:
            self.create_oval(166, 78, 186, 98, fill="#b0c4de", outline="#4a7fd6")
        # brand text
        self.create_text(
            156, 82, text="Baumer", angle=22, fill="#8a90a6",
            font=(FONT_FAMILY, 9, "italic"),
        )


class StatChip(tk.Frame):
    """A small card showing a label + a large read-only value."""

    def __init__(self, master, label: str, initial_value: str, icon: str = "", var: Optional[tk.StringVar] = None):
        super().__init__(master, bg=CHIP_BG, highlightbackground=BORDER, highlightthickness=1)
        self.var = var or tk.StringVar(value=initial_value)

        inner = tk.Frame(self, bg=CHIP_BG)
        inner.pack(fill="both", expand=True, padx=12, pady=8)

        top = tk.Frame(inner, bg=CHIP_BG)
        top.pack(anchor="w")
        if icon:
            tk.Label(top, text=icon, bg=CHIP_BG, fg=ACCENT, font=(FONT_FAMILY, 9)).pack(side="left", padx=(0, 4))
        tk.Label(
            top, text=label.upper(), bg=CHIP_BG, fg=TEXT_MUTED,
            font=(FONT_FAMILY, 8, "bold"),
        ).pack(side="left")

        tk.Label(
            inner, textvariable=self.var, bg=CHIP_BG, fg=TEXT_PRIMARY,
            font=(FONT_FAMILY, 14, "bold"),
        ).pack(anchor="w", pady=(2, 0))


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #
class BaumerConfigApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Configurateur Baumer")
        self.configure(bg=APP_BG)
        self.geometry("700x840")
        self.minsize(620, 760)

        self.ui = UiState()
        self.client: Optional[CanopenClient] = None
        self._events: "queue.Queue[tuple]" = queue.Queue()

        self._build_layout()
        self.after(100, self._poll_events)

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_layout(self):
        self._build_titlebar()

        outer = tk.Frame(self, bg=APP_BG)
        outer.pack(fill="both", expand=True, padx=24, pady=24)

        card = tk.Frame(outer, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="both", expand=True)

        content = tk.Frame(card, bg=CARD_BG)
        content.pack(fill="both", expand=True, padx=28, pady=26)

        self._build_launch_section(content)
        self._build_progress(content)
        self._build_sensor_image(content)
        self._build_stats(content)

    def _build_divider(self, parent, pady=(12, 12)):
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=pady)

    def _build_titlebar(self):
        bar = tk.Frame(self, bg=TITLEBAR_BG, height=48)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        accent = tk.Frame(bar, bg=ACCENT, width=6)
        accent.pack(side="left", fill="y")

        title_box = tk.Frame(bar, bg=BLACK_BOX)
        title_box.pack(side="left", fill="y", padx=(8, 0), pady=8)
        tk.Label(
            title_box, text="Configuration inclinomètre de Baumer",
            bg=BLACK_BOX, fg=TEXT_PRIMARY, font=(FONT_FAMILY, 12, "bold"), padx=14, pady=6,
        ).pack()

        right = tk.Frame(bar, bg=TITLEBAR_BG)
        right.pack(side="right", padx=14)

        self.connect_btn = tk.Button(
            right, text="Connecter", bg=ACCENT, fg=TEXT_PRIMARY,
            activebackground=ACCENT_HOVER, activeforeground=TEXT_PRIMARY,
            font=(FONT_FAMILY, 10, "bold"), relief="flat", padx=16, pady=6,
            command=self.on_connect_clicked,
        )
        self.connect_btn.pack(side="left", padx=(0, 16))

        status_frame = tk.Frame(right, bg=TITLEBAR_BG)
        status_frame.pack(side="left")
        self.status_label = tk.Label(
            status_frame, text="Deconnecte", bg=TITLEBAR_BG, fg=RED,
            font=(FONT_FAMILY, 10, "bold"),
        )
        self.status_label.pack(side="left")
        self.status_dot = tk.Canvas(
            status_frame, width=14, height=14, bg=TITLEBAR_BG, highlightthickness=0
        )
        self.status_dot.pack(side="left", padx=(4, 0))
        self._draw_dot(RED)

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(3, 3, 12, 12, fill=color, outline=color)

    def _build_launch_section(self, parent):
        eyebrow = tk.Frame(parent, bg=CARD_BG)
        eyebrow.pack(fill="x", pady=(0, 10))
        tk.Frame(eyebrow, bg=ACCENT, width=3, height=14).pack(side="left", padx=(0, 8))
        tk.Label(
            eyebrow, text="CONFIGURATION", bg=CARD_BG, fg=TEXT_MUTED,
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(side="left")

        self.launch_btn = RoundedButton(
            parent, "Lancer la configuration", command=self.on_launch_config,
            icon="\u25B6", bg_color=ACCENT, hover_color=ACCENT_HOVER,
            parent_bg=CARD_BG, min_width=560, font_size=11, pady=13,
        )
        self.launch_btn.pack(fill="x")
        self.launch_btn.set_enabled(False)

    def _build_progress(self, parent):
        frame = tk.Frame(parent, bg=CARD_BG)
        frame.pack(fill="x", pady=(20, 0))

        header = tk.Frame(frame, bg=CARD_BG)
        header.pack(fill="x")
        tk.Label(
            header, text="Progression", bg=CARD_BG, fg=TEXT_SECONDARY, font=(FONT_FAMILY, 10),
        ).pack(side="left")
        self.progress_pct_label = tk.Label(
            header, text="0%", bg=CARD_BG, fg=TEXT_PRIMARY, font=(FONT_FAMILY, 10, "bold"),
        )
        self.progress_pct_label.pack(side="right")

        self.progress_bar = RoundedProgressBar(frame, parent_bg=CARD_BG)
        self.progress_bar.pack(fill="x", pady=(8, 10))
        self.progress_bar.set_progress(self.ui.progress)

        self.status_text_label = tk.Label(
            frame, text=self.ui.status_text, bg=CARD_BG, fg=self.ui.status_color,
            font=(FONT_FAMILY, 10), wraplength=520, justify="left", anchor="w",
        )
        self.status_text_label.pack(fill="x")

    def _build_sensor_image(self, parent):
        self._build_divider(parent, pady=(22, 4))
        frame = tk.Frame(parent, bg=CARD_BG)
        frame.pack(pady=(4, 4))
        if Image is not None and SENSOR_PHOTO_PATH.exists():
            self.sensor_icon = SensorPhoto(frame, SENSOR_PHOTO_PATH)
        else:
            self.sensor_icon = SensorIcon(frame)
        self.sensor_icon.pack()

    def _build_stats(self, parent):
        self._build_divider(parent, pady=(4, 18))
        row = tk.Frame(parent, bg=CARD_BG)
        row.pack(fill="x")

        self.heartbeat_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value="")

        hb_chip = StatChip(row, "HeartBeat", "", icon="\u23F1", var=self.heartbeat_var)
        hb_chip.pack(side="left")

        filt_chip = StatChip(row, "Filtre", "", icon="\u2261", var=self.filter_var)
        filt_chip.pack(side="left", padx=(10, 0))

    # ------------------------------------------------------------------ #
    # Connect / disconnect
    # ------------------------------------------------------------------ #
    def on_connect_clicked(self):
        if self.ui.connected:
            self._disconnect()
            return

        cfg = CanopenConfig(node_id=DEFAULT_NODE_ID)
        client = CanopenClient(cfg)
        self.client = client
        self.ui.connected = True
        self.status_label.config(text="Connecté", fg=GREEN)
        self._draw_dot(GREEN)
        self.connect_btn.config(text="Déconnecter", state="normal")
        self.launch_btn.set_enabled(True)
        self._set_status(0, "Prêt à lancer la configuration", TEXT_SECONDARY)

    def _disconnect(self):
        if self.client is not None:
            self.client.disconnect()
            self.client = None
        self.ui.connected = False
        self.status_label.config(text="Déconnecté", fg=RED)
        self._draw_dot(RED)
        self.connect_btn.config(text="Connecter", state="normal")
        self.launch_btn.set_enabled(False)
        self._set_status(0, "Prêt à lancer la configuration", TEXT_SECONDARY)

    # ------------------------------------------------------------------ #
    # Configuration sequence
    # ------------------------------------------------------------------ #
    def on_launch_config(self):
        if not self.ui.connected or self.client is None:
            messagebox.showwarning(
                "Non connecte", "Connectez-vous d'abord au capteur."
            )
            return
        if self.ui.running:
            return

        if not self.client.is_connected:
            try:
                self.client.connect()
            except Exception as exc:
                messagebox.showerror("Erreur de connexion", str(exc))
                return

        try:
            self.client.start_remote_node()
        except Exception as exc:
            messagebox.showerror("Erreur de démarrage du nœud", str(exc))
            return

        # HeartBeat / Filtre are read-only fields (they just display the
        # value read from the sensor), so the configuration always targets
        # the fixed defaults defined at the top of this file.
        target_hb = DEFAULT_HEARTBEAT_MS
        target_filter = DEFAULT_FILTER

        self.ui.running = True
        self.launch_btn.set_enabled(False)
        self._set_status(5, "Configuration en cours", ACCENT)
        threading.Thread(
            target=self._run_config_sequence, args=(target_hb, target_filter), daemon=True
        ).start()

    def _run_config_sequence(self, target_hb: int, target_filter: int):
        client = self.client
        assert client is not None
        try:
            # Step 1: HeartBeat
            self._events.put(("progress", (15, "Configuration en cours", ACCENT)))
            current_hb = client.read_heartbeat_ms()
            hb_already_ok = current_hb == target_hb

            if not hb_already_ok:
                client.write_heartbeat_ms(target_hb)
                self._events.put(("progress", (35, "Configuration en cours", ACCENT)))
                readback_hb = client.read_heartbeat_ms()
                if readback_hb != target_hb:
                    raise RuntimeError(
                        f"La relecture du HeartBeat ({readback_hb} ms) ne correspond pas "
                        f"a la valeur ecrite ({target_hb} ms)."
                    )
                # confirm frames are actually cycling on 0x700+NodeID
                self._wait_for_heartbeat_cycling(client, target_hb)

            self._events.put(("heartbeat_value", target_hb))
            self._events.put(("progress", (50, "Configuration en cours", ACCENT)))

            # Step 2: Filter
            current_filter = client.read_filter()
            filter_already_ok = current_filter == target_filter
            if not filter_already_ok:
                client.write_filter(target_filter)
                self._events.put(("progress", (65, "Configuration en cours", ACCENT)))
                readback_filter = client.read_filter()
                if readback_filter != target_filter:
                    raise RuntimeError(
                        f"La relecture du filtre ({readback_filter}) ne correspond pas "
                        f"a la valeur ecrite ({target_filter})."
                    )
            self._events.put(("filter_value", target_filter))

            if hb_already_ok and filter_already_ok:
                self._events.put(("progress", (100, "Capteur dèja configuré", GREEN)))
                self._events.put(("done", None))
                return

            # Step 3: Save
            self._events.put(("progress", (80, "Configuration en cours", ACCENT)))
            client.save_parameters()

            # Step 4: Reload / final verify
            self._events.put(("progress", (90, "Configuration en cours", ACCENT)))
            client.restore_defaults()
            final_filter = client.read_filter()
            if final_filter != target_filter:
                raise RuntimeError(
                    "Apres sauvegarde/rechargement, le filtre releve "
                    f"({final_filter}) ne correspond plus a la cible ({target_filter})."
                )

            self._events.put(("progress", (100, "Configuration terminée", GREEN)))
            self._events.put(("done", None))

        except (SdoError, TimeoutError, RuntimeError) as exc:
            self._events.put(("progress", (self.ui.progress, "Configuration echouée", RED)))
            detail = str(exc)
            if isinstance(exc, TimeoutError):
                detail = (
                    "Aucune réponse SDO reçue du capteur \n"
                    "Vérifiez le câblage CAN, l'alim du capteur \n"
                    "Déconnecter puis reconnecter."
                )
            self._events.put(("error", detail))
            self._events.put(("done", None))

    def _wait_for_heartbeat_cycling(self, client: CanopenClient, expected_ms: int, timeout: float = 3.0):
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            period = client.last_heartbeat_period_ms
            if period is not None and abs(period - expected_ms) < expected_ms * 0.25 + 50:
                return
            _time.sleep(0.1)
        # Not fatal -- some setups only emit heartbeats when the NMT state
        # machine is operational. We warn but don't hard-fail the sequence.

    # ------------------------------------------------------------------ #
    # Event pump (worker threads -> UI thread)
    # ------------------------------------------------------------------ #
    def _set_status(self, progress: int, text: str, color: str):
        self.ui.progress = progress
        self.ui.status_text = text
        self.ui.status_color = color
        self.status_text_label.config(text=text, fg=color)
        self.progress_pct_label.config(text=f"{progress}%")
        self.progress_bar.set_progress(progress, color)

    def _poll_events(self):
        try:
            while True:
                kind, payload = self._events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, kind: str, payload):
        if kind == "connected":
            client = payload
            self.client = client
            self.ui.connected = True
            label = "Connecté"
            self.status_label.config(text=label, fg=GREEN)
            self._draw_dot(GREEN)
            self.connect_btn.config(text="Déconnecter", state="normal")
            self.launch_btn.set_enabled(True)
            self._set_status(0, "Prêt à lancer la configuration", TEXT_SECONDARY)

        elif kind == "connect_failed":
            self.connect_btn.config(text="Connecter", state="normal")
            if payload:
                messagebox.showerror("Erreur de connexion", payload)

        elif kind == "progress":
            progress, text, color = payload
            self._set_status(progress, text, color)

        elif kind == "heartbeat_value":
            self.heartbeat_var.set(f"{payload} ms")

        elif kind == "filter_value":
            self.filter_var.set(str(payload))

        elif kind == "error":
            messagebox.showerror("Erreur de configuration", payload)

        elif kind == "done":
            self.ui.running = False
            self.launch_btn.set_enabled(self.ui.connected)


if __name__ == "__main__":
    app = BaumerConfigApp()
    app.mainloop()