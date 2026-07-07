"""
Haptic Drive - steer your 8BitDo Ultimate 2C's rumble motors with WASD
so the controller "walks" across the floor like a vibrobot.

How it works
------------
The 8BitDo Ultimate 2C, connected via its 2.4GHz dongle, shows up to Windows
as an XInput (Xbox-compatible) gamepad. XInput lets any program set the
speed of the controller's two rumble motors independently:

    - Left motor  = low-frequency / strong "heavy" motor
    - Right motor = high-frequency / weak "buzzy" motor

Spinning the motors makes the controller vibrate. Because the two motors
sit on opposite sides of an asymmetric plastic shell, running just one of
them makes the whole controller pivot, and running both makes it creep
forward (the same trick "bristlebot" toys use). This app turns WASD into
motor commands so you can crudely "drive" it:

    W - both motors, full steady    -> creeps forward
    A - right motor only, full      -> turns left
    D - left motor only, full       -> turns right
    S - "smart straight": left motor full, right motor held at whatever
                                        the manual strength slider is set
                                        to (the right motor runs slightly
                                        stronger than the left on this
                                        hardware, so throttling it back
                                        balances the two sides out)

There's also a manual-control panel with independent strength/frequency
sliders per motor for experimenting directly, bypassing WASD entirely.

No physical movement is guaranteed - it depends on your floor surface,
the controller's weight balance, and motor intensity. Use the sliders to
tune it experimentally for your floor.


Requirements
------------
Windows only. No pip installs needed - uses ctypes to call the XInput
DLL that ships with Windows, and tkinter for the GUI (both stdlib).

Run
---
    py haptic_drive.py
"""

import ctypes
import os
import sys
import time
import tkinter as tk
from tkinter import ttk

SCRIPT_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ORIENTATION_IMAGE_PATH = os.path.join(SCRIPT_DIR, "controller_orientation.png")

# ---------------------------------------------------------------------------
# XInput bindings
# ---------------------------------------------------------------------------

_XINPUT_DLL_NAMES = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")


def _load_xinput():
    last_err = None
    for name in _XINPUT_DLL_NAMES:
        try:
            return ctypes.WinDLL(name)
        except OSError as e:
            last_err = e
    raise OSError(f"Could not load any XInput DLL: {last_err}")


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [
        ("wLeftMotorSpeed", ctypes.c_ushort),
        ("wRightMotorSpeed", ctypes.c_ushort),
    ]


ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167


class XInputController:
    """Thin wrapper around one XInput controller slot (0-3)."""

    def __init__(self):
        self.xinput = _load_xinput()
        self.xinput.XInputGetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XINPUT_STATE)]
        self.xinput.XInputGetState.restype = ctypes.c_uint
        self.xinput.XInputSetState.argtypes = [ctypes.c_uint, ctypes.POINTER(XINPUT_VIBRATION)]
        self.xinput.XInputSetState.restype = ctypes.c_uint
        self.index = None
        self._last_left = -1
        self._last_right = -1

    def find_connected(self):
        """Scan slots 0-3, return the first connected controller index, or None."""
        state = XINPUT_STATE()
        for i in range(4):
            result = self.xinput.XInputGetState(i, ctypes.byref(state))
            if result == ERROR_SUCCESS:
                self.index = i
                return i
        self.index = None
        return None

    def is_connected(self):
        if self.index is None:
            return False
        state = XINPUT_STATE()
        result = self.xinput.XInputGetState(self.index, ctypes.byref(state))
        return result == ERROR_SUCCESS

    def set_vibration(self, left, right):
        """left/right are 0-65535. Skips redundant calls to avoid USB spam."""
        left = max(0, min(65535, int(left)))
        right = max(0, min(65535, int(right)))
        if self.index is None:
            return False
        if left == self._last_left and right == self._last_right:
            return True
        vib = XINPUT_VIBRATION(left, right)
        result = self.xinput.XInputSetState(self.index, ctypes.byref(vib))
        if result == ERROR_SUCCESS:
            self._last_left = left
            self._last_right = right
            return True
        return False

    def stop(self):
        self.set_vibration(0, 0)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

KEYSYMS_WASD = {"w", "a", "s", "d"}
TICK_MS = 30  # motor update / key-poll rate


class HapticDriveApp:
    def __init__(self, root):
        self.root = root
        root.title("Haptic Drive - 8BitDo Ultimate 2C")
        root.geometry("460x750")
        root.resizable(False, False)

        self.controller = XInputController()
        self.pressed = set()
        self._pending_release = {}  # keysym -> after() id, for autorepeat debounce

        self.intensity = tk.IntVar(value=65000)

        self.manual_mode = tk.BooleanVar(value=False)
        self.left_strength = tk.IntVar(value=20000)
        self.left_freq_hz = tk.DoubleVar(value=0.0)
        self.right_strength = tk.IntVar(value=62000)
        self.right_freq_hz = tk.DoubleVar(value=0.0)

        self._build_ui()

        root.bind("<KeyPress>", self._on_key_press)
        root.bind("<KeyRelease>", self._on_key_release)
        root.focus_force()

        self._phase_t0 = time.monotonic()
        self._connect(initial=True)
        self._tick()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI ----------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        orient_label = ttk.Label(
            self.root, text="Orient your controller like this before you start:",
            justify="center", foreground="#c07a00", font=("Segoe UI", 9, "italic"))
        orient_label.pack(fill="x", padx=12, pady=(8, 0))

        if os.path.exists(ORIENTATION_IMAGE_PATH):
            self.orientation_photo = tk.PhotoImage(file=ORIENTATION_IMAGE_PATH).subsample(2, 2)
            ttk.Label(self.root, image=self.orientation_photo).pack(pady=(2, 4))
        else:
            orient_canvas = tk.Canvas(self.root, width=420, height=110, bg="#f4f0e8", highlightthickness=0)
            orient_canvas.pack(padx=12, pady=(2, 4))
            self._draw_orientation_diagram(orient_canvas)

        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", **pad)
        self.status_label = ttk.Label(status_frame, text="Searching for controller...",
                                       font=("Segoe UI", 11, "bold"))
        self.status_label.pack(side="left")
        ttk.Button(status_frame, text="Reconnect", command=self._connect).pack(side="right")

        keys_frame = ttk.LabelFrame(self.root, text="Drive (click here, then hold WASD)")
        keys_frame.pack(fill="x", **pad)
        self.key_canvas = tk.Canvas(keys_frame, width=420, height=140, bg="#1e1e1e", highlightthickness=0)
        self.key_canvas.pack(padx=8, pady=8)
        self._draw_keys()

        motor_frame = ttk.LabelFrame(self.root, text="Live motor speed")
        motor_frame.pack(fill="x", **pad)
        self.left_bar = ttk.Progressbar(motor_frame, maximum=65535, length=380)
        self.left_bar.pack(pady=(8, 2), padx=8)
        ttk.Label(motor_frame, text="Left motor (low-freq / strong)").pack()
        self.right_bar = ttk.Progressbar(motor_frame, maximum=65535, length=380)
        self.right_bar.pack(pady=(8, 2), padx=8)
        ttk.Label(motor_frame, text="Right motor (high-freq / weak)").pack(pady=(0, 8))

        manual_frame = ttk.LabelFrame(self.root, text="Manual motor control (overrides WASD)")
        manual_frame.pack(fill="x", **pad)
        ttk.Checkbutton(manual_frame, text="Enable manual control", variable=self.manual_mode).pack(
            anchor="w", padx=8, pady=(4, 0))
        self._slider(manual_frame, "Left motor strength", self.left_strength, 0, 65535)
        self._slider(manual_frame, "Left motor frequency (Hz, 0 = steady)", self.left_freq_hz, 0.0, 40.0, resolution=0.5)
        self._slider(manual_frame, "Right motor strength", self.right_strength, 0, 65535)
        self._slider(manual_frame, "Right motor frequency (Hz, 0 = steady)", self.right_freq_hz, 0.0, 40.0, resolution=0.5)

        tune_frame = ttk.LabelFrame(self.root, text="WASD tuning (adjust to match your floor)")
        tune_frame.pack(fill="x", **pad)

        self._slider(tune_frame, "Intensity", self.intensity, 10000, 65535)

        ttk.Button(self.root, text="STOP (space)", command=self._emergency_stop).pack(fill="x", **pad)
        self.root.bind("<space>", lambda e: self._emergency_stop())

        note = ("W both motors full steady  |  A right motor full -> turns left\n"
                "D left motor full -> turns right\n"
                "S smart straight: uses the manual strength sliders above, steady")
        ttk.Label(self.root, text=note, justify="center", foreground="#888").pack(pady=(0, 6))

    def _draw_orientation_diagram(self, canvas):
        floor_y = 90
        canvas.create_line(20, floor_y, 400, floor_y, fill="#999", width=2)
        canvas.create_text(30, floor_y + 12, text="floor", fill="#999", font=("Segoe UI", 8), anchor="w")

        # controller silhouette lying face-down: two raised grip "feet" with
        # the body dipping between them, like a shallow bridge
        points = [
            (90, floor_y), (95, 55), (135, 32), (175, 45),
            (210, 52), (245, 45), (285, 32), (325, 55), (330, floor_y),
        ]
        flat_points = [c for xy in points for c in xy]
        canvas.create_line(*flat_points, fill="#b0785a", width=4, smooth=True, joinstyle="round")

        canvas.create_text(115, 25, text="grip (foot)", font=("Segoe UI", 8, "bold"), fill="#7a4a30")
        canvas.create_text(305, 25, text="grip (foot)", font=("Segoe UI", 8, "bold"), fill="#7a4a30")
        canvas.create_text(210, 40, text="back panel up", font=("Segoe UI", 8, "italic"), fill="#7a4a30")

        canvas.create_oval(88, floor_y - 4, 98, floor_y + 4, fill="#7a4a30", outline="")
        canvas.create_oval(325, floor_y - 4, 335, floor_y + 4, fill="#7a4a30", outline="")

    def _slider(self, parent, label, var, lo, hi, resolution=1):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text=label, width=34).pack(side="left")
        ttk.Scale(row, from_=lo, to=hi, variable=var, orient="horizontal").pack(
            side="left", fill="x", expand=True)

    def _draw_keys(self):
        self.key_rects = {}
        specs = {"w": (150, 10, 210, 60), "a": (85, 75, 145, 125),
                 "s": (150, 75, 210, 125), "d": (215, 75, 275, 125)}
        for k, (x0, y0, x1, y1) in specs.items():
            rect = self.key_canvas.create_rectangle(x0, y0, x1, y1, fill="#3a3a3a", outline="#888", width=2)
            self.key_canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=k.upper(),
                                         fill="white", font=("Segoe UI", 16, "bold"))
            self.key_rects[k] = rect

    def _refresh_key_colors(self):
        for k, rect in self.key_rects.items():
            self.key_canvas.itemconfig(rect, fill="#2a8f2a" if k in self.pressed else "#3a3a3a")

    # -- controller connection ----------------------------------------------

    def _connect(self, initial=False):
        idx = self.controller.find_connected()
        if idx is not None:
            self.status_label.config(text=f"Connected: controller slot {idx}", foreground="#2a8f2a")
        else:
            self.status_label.config(text="No controller found - check the 2.4GHz dongle", foreground="#c0392b")

    # -- key handling (with autorepeat debounce) -----------------------------

    def _on_key_press(self, event):
        k = event.keysym.lower()
        if k not in KEYSYMS_WASD:
            return
        if k in self._pending_release:
            self.root.after_cancel(self._pending_release.pop(k))
        self.pressed.add(k)
        self._refresh_key_colors()

    def _on_key_release(self, event):
        k = event.keysym.lower()
        if k not in KEYSYMS_WASD:
            return

        def do_release():
            self._pending_release.pop(k, None)
            self.pressed.discard(k)
            self._refresh_key_colors()

        # tkinter fires KeyRelease/KeyPress repeatedly during OS autorepeat;
        # delay the release briefly so a same-key repress cancels it.
        self._pending_release[k] = self.root.after(40, do_release)

    def _emergency_stop(self):
        self.pressed.clear()
        self._refresh_key_colors()
        self.manual_mode.set(False)
        self.left_strength.set(0)
        self.right_strength.set(0)
        self.controller.stop()

    # -- motor mixing loop ----------------------------------------------------

    def _tick(self):
        if not self.controller.is_connected():
            self.controller.index = None
            self._connect()

        left, right = self._compute_motor_speeds()
        self.controller.set_vibration(left, right)
        self.left_bar["value"] = left
        self.right_bar["value"] = right

        self.root.after(TICK_MS, self._tick)

    def _compute_motor_speeds(self):
        if self.controller.index is None:
            return 0, 0

        t = time.monotonic() - self._phase_t0

        if self.manual_mode.get():
            left = self._manual_motor_value(t, self.left_strength.get(), self.left_freq_hz.get())
            right = self._manual_motor_value(t, self.right_strength.get(), self.right_freq_hz.get())
            return left, right

        p = self.pressed
        if not p:
            return 0, 0

        base = self.intensity.get()

        forward = "w" in p
        straight = "s" in p
        turn_left = "a" in p
        turn_right = "d" in p

        if turn_left and turn_right:
            return 0, 0  # opposing turns cancel out

        if turn_left:
            return 0, base  # right motor only, full steady -> turns left

        if turn_right:
            return base, 0  # left motor only, full steady -> turns right

        if straight:
            # reuses whatever is currently dialed into the manual strength
            # sliders above, steady (no pulsing), instead of a fixed ratio.
            return self.left_strength.get(), self.right_strength.get()

        if forward:
            return base, base  # both motors full steady

        return 0, 0

    @staticmethod
    def _manual_motor_value(t, strength, freq_hz):
        if strength <= 0:
            return 0
        if freq_hz <= 0:
            return strength
        on = (t * freq_hz) % 1.0 < 0.5
        return strength if on else 0

    def _on_close(self):
        try:
            self.controller.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    if not sys.platform.startswith("win"):
        print("This tool uses XInput and only works on Windows.")
        sys.exit(1)
    root = tk.Tk()
    HapticDriveApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
