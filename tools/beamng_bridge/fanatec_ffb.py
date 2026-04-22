#!/usr/bin/env python3
"""
Fanatec Clubsport DD+ force feedback controller.

Finds the real Fanatec wheel via evdev and uses FF_CONSTANT effects
to drive the wheel to a target steering position. This creates a
position-tracking servo where openpilot's steering commands physically
turn the wheel.

Also creates a virtual uinput device for throttle/brake pedals so
Assetto Corsa can read pedal inputs separately from the wheel.

Requirements:
  - Linux with evdev and uinput support
  - hid-fanatecff kernel module (https://github.com/gotzl/hid-fanatecff)
  - pip install evdev
"""
import os
import sys
import time
import struct
import threading
import numpy as np
from pathlib import Path

try:
    import evdev
    from evdev import ecodes, ff, InputDevice, UInput
except ImportError:
    print("evdev not found. Install with: pip install evdev")
    sys.exit(1)

FANATEC_VENDOR_ID = 0x0EB7
FANATEC_DD_PLUS_PRODUCT_IDS = [
    0x0020,  # DD+/DD Pro/CSL DD
    0x0001,  # ClubSport Wheel Base
    0x0006,  # CSL Elite
    0x0E03,  # CSL Elite PS4
]

# FF_CONSTANT update rate (Hz)
FFB_UPDATE_RATE = 200
# Maximum force level (0x7FFF = max for FF_CONSTANT)
MAX_FORCE = 0x7FFF
# Proportional gain for position tracking
P_GAIN = 5.0
# Derivative gain for damping
D_GAIN = 0.3
# Maximum steering angle the wheel reports (in device units, typically [-32767, 32767])
WHEEL_RANGE = 32767


def find_fanatec_wheel():
    devices = [InputDevice(path) for path in evdev.list_devices()]
    for dev in devices:
        info = dev.info
        if info.vendor == FANATEC_VENDOR_ID and info.product in FANATEC_DD_PLUS_PRODUCT_IDS:
            caps = dev.capabilities(verbose=True)
            has_ff = any('EV_FF' in str(c) for c in caps)
            has_abs_x = False
            for cap_type, cap_list in caps.items():
                if 'EV_ABS' in str(cap_type):
                    for item in cap_list:
                        if 'ABS_X' in str(item):
                            has_abs_x = True
            if has_ff and has_abs_x:
                print(f"Found Fanatec wheel: {dev.name} at {dev.path}")
                print(f"  Vendor: 0x{info.vendor:04X} Product: 0x{info.product:04X}")
                return dev
            dev.close()
        else:
            dev.close()
    return None


class FanatecFFBController:
    """Controls the real Fanatec DD+ wheel via force feedback to track a target position."""

    def __init__(self, device=None):
        self.device = device or find_fanatec_wheel()
        if self.device is None:
            raise RuntimeError("No Fanatec wheel found. Check connection and hid-fanatecff driver.")

        self._effect_id = -1
        self._target_angle = 0.0  # normalized [-1, 1]
        self._current_angle = 0.0
        self._last_error = 0.0
        self._running = False
        self._lock = threading.Lock()

        self._read_wheel_range()
        self._upload_effect()

    def _read_wheel_range(self):
        caps = self.device.capabilities(absinfo=True)
        for code, absinfo in caps.get(ecodes.EV_ABS, []):
            if code == ecodes.ABS_X:
                self._abs_min = absinfo.min
                self._abs_max = absinfo.max
                self._abs_range = absinfo.max - absinfo.min
                print(f"  Wheel range: [{absinfo.min}, {absinfo.max}]")
                return
        self._abs_min = -WHEEL_RANGE
        self._abs_max = WHEEL_RANGE
        self._abs_range = WHEEL_RANGE * 2

    def _upload_effect(self):
        effect = ff.Effect(
            ecodes.FF_CONSTANT,
            -1,  # new effect
            0,   # trigger button
            ff.Trigger(0, 0),
            ff.Replay(0, 0),  # infinite duration
            ff.EffectType(ff_constant_effect=ff.Constant(0, ff.Envelope(0, 0, 0, 0)))
        )
        self._effect_id = self.device.upload_effect(effect)
        self.device.write(ecodes.EV_FF, self._effect_id, 1)

    def set_target(self, angle_normalized):
        """Set target steering position, normalized to [-1, 1]."""
        with self._lock:
            self._target_angle = np.clip(angle_normalized, -1.0, 1.0)

    def read_current_position(self):
        """Read current wheel position from evdev. Returns normalized [-1, 1]."""
        try:
            for event in self.device.read():
                if event.type == ecodes.EV_ABS and event.code == ecodes.ABS_X:
                    mid = (self._abs_min + self._abs_max) / 2
                    half_range = self._abs_range / 2
                    self._current_angle = (event.value - mid) / half_range
        except BlockingIOError:
            pass
        return self._current_angle

    def _update_force(self):
        """PD controller: compute force to drive wheel toward target."""
        with self._lock:
            target = self._target_angle

        error = target - self._current_angle
        d_error = error - self._last_error
        self._last_error = error

        force = P_GAIN * error + D_GAIN * d_error
        force = np.clip(force, -1.0, 1.0)
        magnitude = int(force * MAX_FORCE)

        if magnitude >= 0:
            direction = 0x0000  # right
        else:
            direction = 0x8000  # left
            magnitude = -magnitude

        effect = ff.Effect(
            ecodes.FF_CONSTANT,
            self._effect_id,
            direction,
            ff.Trigger(0, 0),
            ff.Replay(0, 0),
            ff.EffectType(ff_constant_effect=ff.Constant(magnitude, ff.Envelope(0, 0, 0, 0)))
        )
        self.device.upload_effect(effect)

    def start(self):
        self._running = True
        t = threading.Thread(target=self._control_loop, daemon=True)
        t.start()
        return t

    def stop(self):
        self._running = False
        if self._effect_id >= 0:
            self.device.write(ecodes.EV_FF, self._effect_id, 0)
            try:
                self.device.erase_effect(self._effect_id)
            except Exception:
                pass

    def _control_loop(self):
        dt = 1.0 / FFB_UPDATE_RATE
        while self._running:
            self.read_current_position()
            self._update_force()
            time.sleep(dt)

    def __del__(self):
        self.stop()


class VirtualPedals:
    """Virtual uinput device for throttle and brake pedals."""

    def __init__(self, name='AC Bridge Virtual Pedals'):
        cap = {
            ecodes.EV_ABS: [
                (ecodes.ABS_Z, evdev.AbsInfo(0, 0, 65535, 0, 0, 0)),      # throttle
                (ecodes.ABS_RZ, evdev.AbsInfo(0, 0, 65535, 0, 0, 0)),     # brake
                (ecodes.ABS_Y, evdev.AbsInfo(0, 0, 65535, 0, 0, 0)),      # clutch
            ],
        }
        self.device = UInput(cap, name=name, vendor=0x0EB7, product=0x7001)
        print(f"Virtual pedals created: {self.device.device.path}")

    def emit(self, throttle, brake, clutch=0):
        """
        Set pedal positions.
        throttle: [0.0, 1.0]
        brake:    [0.0, 1.0]
        clutch:   [0.0, 1.0]
        """
        t = int(np.clip(throttle, 0, 1) * 65535)
        b = int(np.clip(brake, 0, 1) * 65535)
        c = int(np.clip(clutch, 0, 1) * 65535)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_Z, t)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_RZ, b)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_Y, c)
        self.device.syn()

    def __del__(self):
        self.device.close()


if __name__ == "__main__":
    print("Searching for Fanatec wheel...")
    wheel = find_fanatec_wheel()
    if wheel:
        print(f"Found: {wheel.name}")
        ctrl = FanatecFFBController(wheel)
        ctrl.start()

        print("Sweeping wheel left-center-right...")
        for angle in np.linspace(-0.5, 0.5, 100):
            ctrl.set_target(angle)
            time.sleep(0.05)
        for angle in np.linspace(0.5, -0.5, 100):
            ctrl.set_target(angle)
            time.sleep(0.05)
        ctrl.set_target(0.0)
        time.sleep(1)
        ctrl.stop()
        print("Done.")
    else:
        print("No Fanatec wheel found.")
