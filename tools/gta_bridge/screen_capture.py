"""
Screen capture camera backend.

Captures frames directly from the screen or a specific game window,
replacing the physical camera for local testing (game and openpilot
on the same machine).

Uses mss for fast screen capture. Optionally targets a specific window
by title using xdotool (X11).
"""
import time
import subprocess
import numpy as np
import cv2

try:
    import mss
except ImportError:
    mss = None

W, H = 1164, 874

WINDOW_RELOCATE_INTERVAL = 5.0


def find_window_geometry(title_pattern):
    try:
        result = subprocess.run(
            ['xdotool', 'search', '--name', title_pattern],
            capture_output=True, text=True, timeout=5,
        )
        wids = result.stdout.strip().split('\n')
        if not wids or not wids[0]:
            return None

        wid = wids[0]

        geo = subprocess.run(
            ['xdotool', 'getwindowgeometry', '--shell', wid],
            capture_output=True, text=True, timeout=5,
        )

        vals = {}
        for line in geo.stdout.strip().split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                vals[k] = int(v)

        size = subprocess.run(
            ['xdotool', 'getwindowfocus', 'getwindowgeometry', '--shell', wid],
            capture_output=True, text=True, timeout=5,
        )
        for line in size.stdout.strip().split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                if k in ('WIDTH', 'HEIGHT'):
                    vals[k] = int(v)

        return {
            "left": vals.get("X", 0),
            "top": vals.get("Y", 0),
            "width": vals.get("WIDTH", 1920),
            "height": vals.get("HEIGHT", 1080),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None


def parse_region_string(region_str):
    parts = [int(x.strip()) for x in region_str.split(',')]
    if len(parts) != 4:
        raise ValueError(f"Expected x,y,w,h but got: {region_str}")
    return {"left": parts[0], "top": parts[1], "width": parts[2], "height": parts[3]}


def start_camera_screen(callback, window_title=None, region=None, target_fps=20):
    if mss is None:
        raise ImportError("mss is required for screen capture: pip install mss")

    sct = mss.mss()

    monitor = None
    if window_title:
        monitor = find_window_geometry(window_title)
        if monitor:
            print(f"Found window '{window_title}': {monitor['width']}x{monitor['height']} "
                  f"at ({monitor['left']}, {monitor['top']})")
        else:
            print(f"WARNING: Window '{window_title}' not found, capturing primary monitor")

    if monitor is None and region:
        if isinstance(region, str):
            monitor = parse_region_string(region)
        else:
            monitor = region
        print(f"Capturing region: {monitor['width']}x{monitor['height']} "
              f"at ({monitor['left']}, {monitor['top']})")

    if monitor is None:
        monitor = sct.monitors[1]
        print(f"Capturing primary monitor: {monitor['width']}x{monitor['height']}")

    dt = 1.0 / target_fps
    target_aspect = W / H
    last_relocate = time.time()

    print(f"Screen capture started, target: {W}x{H} @ {target_fps} FPS")

    while True:
        t0 = time.time()

        if window_title and (t0 - last_relocate > WINDOW_RELOCATE_INTERVAL):
            updated = find_window_geometry(window_title)
            if updated:
                monitor = updated
            last_relocate = t0

        img = sct.grab(monitor)
        frame = np.array(img)[:, :, :3]  # BGRA -> BGR

        src_h, src_w = frame.shape[:2]
        src_aspect = src_w / src_h

        if src_aspect > target_aspect:
            crop_w = int(src_h * target_aspect)
            x_off = (src_w - crop_w) // 2
            frame = frame[:, x_off:x_off + crop_w]
        elif src_aspect < target_aspect:
            crop_h = int(src_w / target_aspect)
            y_off = (src_h - crop_h) // 2
            frame = frame[y_off:y_off + crop_h, :]

        frame = cv2.resize(frame, (W, H))
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        callback(frame)

        elapsed = time.time() - t0
        sleep_time = dt - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)
