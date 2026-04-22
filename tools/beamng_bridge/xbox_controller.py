#!/usr/bin/env python3
"""
Virtual Xbox 360 controller via Linux uinput/evdev.

Creates a device that appears as a standard Microsoft Xbox 360 pad
(vendor=0x045e, product=0x028e). Games running natively or via
Proton/Wine will recognize it as a real controller.

GTA5 mapping:
  Left stick X  (ABS_X)  = steering
  Right trigger (ABS_RZ) = throttle/gas
  Left trigger  (ABS_Z)  = brake
  A button               = handbrake (optional)

Requirements:
  - Linux with uinput support (modprobe uinput)
  - pip install evdev
"""
import sys
import numpy as np

try:
    import evdev
    from evdev import UInput, ecodes, AbsInfo
except ImportError:
    print("evdev not found. Install with: pip install evdev")
    sys.exit(1)


class VirtualXboxController:
    """Virtual Xbox 360 controller using Linux uinput."""

    # Xbox 360 USB IDs
    VENDOR = 0x045E   # Microsoft
    PRODUCT = 0x028E  # Xbox 360 Controller
    VERSION = 0x0110

    def __init__(self, name='Microsoft X-Box 360 pad'):
        cap = {
            ecodes.EV_ABS: [
                # Left stick
                (ecodes.ABS_X, AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (ecodes.ABS_Y, AbsInfo(0, -32768, 32767, 16, 128, 0)),
                # Right stick
                (ecodes.ABS_RX, AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (ecodes.ABS_RY, AbsInfo(0, -32768, 32767, 16, 128, 0)),
                # Triggers
                (ecodes.ABS_Z, AbsInfo(0, 0, 255, 0, 0, 0)),    # left trigger (brake)
                (ecodes.ABS_RZ, AbsInfo(0, 0, 255, 0, 0, 0)),   # right trigger (gas)
                # D-pad
                (ecodes.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (ecodes.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ],
            ecodes.EV_KEY: [
                ecodes.BTN_SOUTH,    # A
                ecodes.BTN_EAST,     # B
                ecodes.BTN_NORTH,    # Y
                ecodes.BTN_WEST,     # X
                ecodes.BTN_TL,       # LB
                ecodes.BTN_TR,       # RB
                ecodes.BTN_SELECT,   # Back
                ecodes.BTN_START,    # Start
                ecodes.BTN_MODE,     # Guide
                ecodes.BTN_THUMBL,   # Left stick click
                ecodes.BTN_THUMBR,   # Right stick click
            ],
            ecodes.EV_FF: [
                ecodes.FF_RUMBLE,
            ],
        }
        self.device = UInput(cap, name=name,
                            vendor=self.VENDOR,
                            product=self.PRODUCT,
                            version=self.VERSION)
        print(f"Virtual Xbox 360 controller created: {self.device.device.path}")

    def emit(self, steering=0.0, throttle=0.0, brake=0.0):
        """
        Set controller state.
        steering: [-1.0, 1.0] left to right
        throttle: [0.0, 1.0]
        brake:    [0.0, 1.0]
        """
        steer_val = int(np.clip(steering, -1.0, 1.0) * 32767)
        gas_val = int(np.clip(throttle, 0.0, 1.0) * 255)
        brake_val = int(np.clip(brake, 0.0, 1.0) * 255)

        self.device.write(ecodes.EV_ABS, ecodes.ABS_X, steer_val)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_RZ, gas_val)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_Z, brake_val)
        self.device.syn()

    def set_left_stick(self, x=0.0, y=0.0):
        """Set left stick. x,y in [-1.0, 1.0]."""
        self.device.write(ecodes.EV_ABS, ecodes.ABS_X, int(np.clip(x, -1, 1) * 32767))
        self.device.write(ecodes.EV_ABS, ecodes.ABS_Y, int(np.clip(y, -1, 1) * 32767))
        self.device.syn()

    def set_right_stick(self, x=0.0, y=0.0):
        """Set right stick. x,y in [-1.0, 1.0]."""
        self.device.write(ecodes.EV_ABS, ecodes.ABS_RX, int(np.clip(x, -1, 1) * 32767))
        self.device.write(ecodes.EV_ABS, ecodes.ABS_RY, int(np.clip(y, -1, 1) * 32767))
        self.device.syn()

    def press_button(self, button):
        """Press a button (ecodes.BTN_SOUTH, etc.)."""
        self.device.write(ecodes.EV_KEY, button, 1)
        self.device.syn()

    def release_button(self, button):
        """Release a button."""
        self.device.write(ecodes.EV_KEY, button, 0)
        self.device.syn()

    def reset(self):
        """Center all axes and release all buttons."""
        self.device.write(ecodes.EV_ABS, ecodes.ABS_X, 0)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_Y, 0)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_RX, 0)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_RY, 0)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_Z, 0)
        self.device.write(ecodes.EV_ABS, ecodes.ABS_RZ, 0)
        for btn in [ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_NORTH,
                     ecodes.BTN_WEST, ecodes.BTN_TL, ecodes.BTN_TR]:
            self.device.write(ecodes.EV_KEY, btn, 0)
        self.device.syn()

    def close(self):
        self.reset()
        self.device.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    import time
    print("Creating virtual Xbox 360 controller...")
    ctrl = VirtualXboxController()

    print("Testing: sweep steering left-center-right...")
    for s in [x / 50.0 for x in range(-50, 51)]:
        ctrl.emit(steering=s, throttle=0.3, brake=0.0)
        time.sleep(0.02)
    for s in [x / 50.0 for x in range(50, -51, -1)]:
        ctrl.emit(steering=s, throttle=0.3, brake=0.0)
        time.sleep(0.02)

    ctrl.reset()
    time.sleep(0.5)

    print("Testing: full throttle then brake...")
    ctrl.emit(steering=0, throttle=1.0, brake=0.0)
    time.sleep(1)
    ctrl.emit(steering=0, throttle=0.0, brake=1.0)
    time.sleep(1)

    ctrl.reset()
    print("Done.")
    ctrl.close()
