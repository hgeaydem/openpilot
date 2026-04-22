#!/usr/bin/env python3
"""
ACC (Assetto Corsa Competizione) <-> openpilot bridge.

Runs on the Linux machine with a camera pointed at the game screen.
Supports two camera backends:
  - OpenCV (--cam-backend opencv): portable, works everywhere
  - GStreamer/NVIDIA (--cam-backend gstreamer): hardware-accelerated on Jetson
  - Screen capture (--cam-backend screen): captures game window directly (local testing)

Data flow:
  Camera -> openpilot (modeld -> plannerd -> controlsd)
  ACC telemetry relay (UDP from game machine) -> vehicle state -> fake CAN
  openpilot control output -> UDP to companion on game machine
  companion -> virtual Xbox 360 controller -> ACC

Usage (x86/generic):
  python bridge.py --game-host 192.168.1.100 --camera 0

Usage (Jetson with GStreamer + DeepStream):
  python bridge.py --game-host 192.168.1.100 --cam-backend gstreamer \\
                   --cam-source v4l2-mjpeg --cam-device /dev/video0
"""
import time
import math
import atexit
import numpy as np
import threading
import argparse
import struct
import socket
import sys
import os
import signal

# Add openpilot root to path
OPENPILOT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
sys.path.insert(0, OPENPILOT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cereal.messaging as messaging
from common.params import Params
from common.realtime import Ratekeeper, DT_DMON
from lib.can import can_function
from selfdrive.car.honda.values import CruiseButtons
from selfdrive.test.helpers import set_params_enabled

from acc_telemetry import ACCTelemetry
from configparser import ConfigParser

# --- Configuration ---

W, H = 1164, 874
REPEAT_COUNTER = 5
PRINT_DECIMATION = 100

cfg = ConfigParser()
cfg.read(os.path.join(os.path.dirname(__file__), "tweak.cfg"))
STEER_RATIO = cfg.getfloat("steer", "STEER_RATIO", fallback=15)
BASE_MAX_STEER_ANGLE = cfg.getfloat("steer", "BASE_MAX_STEER_ANGLE", fallback=40)
STEER_SPEED_OFFSET = cfg.getfloat("steer", "STEER_SPEED_OFFSET", fallback=10)
STEER_SPEED_DENOM = cfg.getfloat("steer", "STEER_SPEED_DENOM", fallback=1.4)

print(f"Steer config: ratio={STEER_RATIO} max_angle={BASE_MAX_STEER_ANGLE} "
      f"speed_offset={STEER_SPEED_OFFSET} speed_denom={STEER_SPEED_DENOM}")

# Control packet sent to companion: steering[-1,1], throttle[0,1], brake[0,1], engaged(bool)
CONTROL_STRUCT = struct.Struct("<fff?")

# --- Messaging ---

pm = messaging.PubMaster(['roadCameraState', 'sensorEvents', 'can'])
sm = messaging.SubMaster(['carControl', 'controlsState'])


class VehicleState:
    def __init__(self):
        self.speed = 0.0
        self.angle = 0.0
        self.cruise_button = 0
        self.is_engaged = False
        self.left_blinker = False
        self.right_blinker = False


def steer_rate_limit(old, new):
    limit = 1.0
    if new > old + limit:
        return old + limit
    elif new < old - limit:
        return old - limit
    return new


# --- Camera capture ---

frame_id = 0

def cam_callback(image):
    global frame_id
    dat = messaging.new_message('roadCameraState')
    dat.roadCameraState = {
        "frameId": frame_id,
        "image": image.tobytes(),
        "transform": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    }
    pm.send('roadCameraState', dat)
    frame_id += 1


def start_camera_opencv(camera_id, target_fps=20):
    import cv2
    cap = cv2.VideoCapture(camera_id)
    if not cap.isOpened():
        print(f"ERROR: Cannot open camera {camera_id}")
        return

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"OpenCV camera opened: {actual_w}x{actual_h}, target FPS: {target_fps}")

    dt = 1.0 / target_fps

    while True:
        t0 = time.time()
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        src_h, src_w = frame.shape[:2]
        target_aspect = W / H
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

        cam_callback(frame)

        elapsed = time.time() - t0
        sleep_time = dt - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)


def start_camera_gstreamer(source, device, width, height, fps):
    from camera_gst import GstCamera, probe_v4l2_formats

    if source == 'auto':
        source = probe_v4l2_formats(device)
        print(f"Auto-detected V4L2 source type: {source}")

    cam = GstCamera(
        source=source,
        device=device,
        width=width,
        height=height,
        fps=fps,
        callback=cam_callback,
    )
    cam.start()

    while cam.is_running:
        time.sleep(1)


# --- Fake peripheral systems ---

def panda_state_thread():
    pm_local = messaging.PubMaster(['pandaState'])
    while True:
        dat = messaging.new_message('pandaState')
        dat.valid = True
        dat.pandaState = {
            'ignitionLine': True,
            'pandaType': "blackPanda",
            'controlsAllowed': True,
            'safetyModel': 'hondaNidec',
        }
        pm_local.send('pandaState', dat)
        time.sleep(0.5)


def fake_driver_monitoring_thread():
    pm_local = messaging.PubMaster(['driverState', 'driverMonitoringState'])
    while True:
        dat = messaging.new_message('driverState')
        dat.driverState.faceProb = 1.0
        pm_local.send('driverState', dat)

        dat = messaging.new_message('driverMonitoringState')
        dat.driverMonitoringState = {
            "faceDetected": True,
            "isDistracted": False,
            "awarenessStatus": 1.,
        }
        pm_local.send('driverMonitoringState', dat)
        time.sleep(DT_DMON)


def fake_gps_thread():
    pm_local = messaging.PubMaster(['gpsLocationExternal'])
    while True:
        dat = messaging.new_message('gpsLocationExternal')
        pm_local.send('gpsLocationExternal', dat)
        time.sleep(0.1)


def imu_from_telemetry(acc: ACCTelemetry):
    dat = messaging.new_message('sensorEvents', 1)
    dat.sensorEvents[0].sensor = 4
    dat.sensorEvents[0].type = 0x10
    dat.sensorEvents[0].init('acceleration')
    state = acc.get_state()
    dat.sensorEvents[0].acceleration.v = state['acc_g']
    pm.send('sensorEvents', dat)


def can_function_runner(vs):
    i = 0
    while True:
        can_function(pm, vs.speed, vs.angle, i, vs.cruise_button,
                     vs.is_engaged, vs.left_blinker, vs.right_blinker)
        time.sleep(0.01)
        i += 1


# --- Config file watcher ---

def config_change_callback():
    global STEER_RATIO, STEER_SPEED_OFFSET, BASE_MAX_STEER_ANGLE, STEER_SPEED_DENOM
    cfg.read(os.path.join(os.path.dirname(__file__), "tweak.cfg"))
    STEER_RATIO = cfg.getfloat("steer", "STEER_RATIO", fallback=15)
    STEER_SPEED_OFFSET = cfg.getfloat("steer", "STEER_SPEED_OFFSET", fallback=10)
    BASE_MAX_STEER_ANGLE = cfg.getfloat("steer", "BASE_MAX_STEER_ANGLE", fallback=40)
    STEER_SPEED_DENOM = cfg.getfloat("steer", "STEER_SPEED_DENOM", fallback=1.4)
    print(f"Steer config updated: ratio={STEER_RATIO} max_angle={BASE_MAX_STEER_ANGLE} "
          f"speed_offset={STEER_SPEED_OFFSET} speed_denom={STEER_SPEED_DENOM}")


def config_watcher_thread():
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class Handler(FileSystemEventHandler):
            def on_modified(self, event):
                if event.src_path.endswith("tweak.cfg") and not event.is_directory:
                    config_change_callback()

        observer = Observer()
        observer.schedule(Handler(), os.path.dirname(__file__) or ".", recursive=False)
        observer.start()
        observer.join()
    except ImportError:
        print("watchdog not installed, config hot-reload disabled")


# --- Main bridge loop ---

def bridge_main(q, args):
    # Set up ACC telemetry receiver
    acc = ACCTelemetry(listen_port=args.telemetry_port)
    acc.start_async()
    print(f"Listening for ACC telemetry on UDP port {args.telemetry_port}")
    print("  Run acc_telemetry_relay.py on the game machine and set host to this machine's IP")

    # UDP socket for sending commands to companion
    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    companion_addr = (args.game_host, args.companion_port)
    print(f"Sending controls to companion at {companion_addr}")

    vehicle_state = VehicleState()

    # Select camera backend
    if args.cam_backend == 'gstreamer':
        cam_thread = threading.Thread(
            target=start_camera_gstreamer,
            args=(args.cam_source, args.cam_device,
                  args.cam_width, args.cam_height, args.cam_fps),
            daemon=True,
        )
    elif args.cam_backend == 'screen':
        from screen_capture import start_camera_screen
        cam_thread = threading.Thread(
            target=start_camera_screen,
            args=(cam_callback, args.window_title, args.screen_region, args.cam_fps),
            daemon=True,
        )
    else:
        cam_thread = threading.Thread(
            target=start_camera_opencv,
            args=(args.camera, args.cam_fps),
            daemon=True,
        )

    # Launch threads
    threads = [
        cam_thread,
        threading.Thread(target=panda_state_thread, daemon=True),
        threading.Thread(target=fake_driver_monitoring_thread, daemon=True),
        threading.Thread(target=fake_gps_thread, daemon=True),
        threading.Thread(target=can_function_runner, args=(vehicle_state,), daemon=True),
        threading.Thread(target=config_watcher_thread, daemon=True),
    ]
    for t in threads:
        t.start()

    rk = Ratekeeper(100, print_delay_threshold=0.05)

    is_openpilot_engaged = False
    throttle_out = steer_out = brake_out = 0.0
    old_steer = old_brake = old_throttle = 0.0

    throttle_ease_out_counter = REPEAT_COUNTER
    brake_ease_out_counter = REPEAT_COUNTER
    steer_ease_out_counter = REPEAT_COUNTER

    print("\nBridge running. Keyboard controls:")
    print("  C = engage/increase cruise speed")
    print("  Z = decrease cruise speed")
    print("  V = disengage openpilot")
    print()

    while True:
        cruise_button = 0
        throttle_out = steer_out = brake_out = 0.0

        # Step 1: Read controls from openpilot or manual input
        if not q.empty():
            message = q.get()
            m = message.split('_')
            if m[0] == "cruise":
                if m[1] == "down":
                    cruise_button = CruiseButtons.DECEL_SET
                    is_openpilot_engaged = True
                elif m[1] == "up":
                    cruise_button = CruiseButtons.RES_ACCEL
                    is_openpilot_engaged = True
                elif m[1] == "cancel":
                    cruise_button = CruiseButtons.CANCEL
                    is_openpilot_engaged = False

        if is_openpilot_engaged:
            sm.update(0)
            throttle_out = sm['carControl'].actuators.gas
            brake_out = sm['carControl'].actuators.brake
            steer_out = sm['controlsState'].steeringAngleDesiredDeg

            steer_out = steer_rate_limit(old_steer, steer_out)
            old_steer = steer_out
        else:
            if throttle_out == 0 and old_throttle > 0:
                if throttle_ease_out_counter > 0:
                    throttle_out = old_throttle
                    throttle_ease_out_counter -= 1
                else:
                    throttle_ease_out_counter = REPEAT_COUNTER
                    old_throttle = 0

            if brake_out == 0 and old_brake > 0:
                if brake_ease_out_counter > 0:
                    brake_out = old_brake
                    brake_ease_out_counter -= 1
                else:
                    brake_ease_out_counter = REPEAT_COUNTER
                    old_brake = 0

            if steer_out == 0 and old_steer != 0:
                if steer_ease_out_counter > 0:
                    steer_out = old_steer
                    steer_ease_out_counter -= 1
                else:
                    steer_ease_out_counter = REPEAT_COUNTER
                    old_steer = 0

        # Step 2: Convert steering to normalized [-1,1] and send to companion
        state = acc.get_state() if acc.connected else {'speed_ms': 0}
        speed_ms = max(state.get('speed_ms', 0), 0)

        imu_from_telemetry(acc)

        max_steer_angle = BASE_MAX_STEER_ANGLE + max(0, speed_ms * 3.6 - STEER_SPEED_OFFSET) / STEER_SPEED_DENOM
        steer_normalized = steer_out / (max_steer_angle * STEER_RATIO)
        steer_normalized = np.clip(steer_normalized, -1, 1)

        steer_out = steer_normalized * max_steer_angle * STEER_RATIO
        old_steer = steer_out

        # Send to companion
        packet = CONTROL_STRUCT.pack(steer_normalized, throttle_out, brake_out, is_openpilot_engaged)
        try:
            cmd_sock.sendto(packet, companion_addr)
        except OSError:
            pass

        # Step 3: Update vehicle state for CAN messages
        vehicle_state.speed = speed_ms
        vehicle_state.angle = steer_out
        vehicle_state.cruise_button = cruise_button
        vehicle_state.is_engaged = is_openpilot_engaged

        if rk.frame % PRINT_DECIMATION == 0:
            tel = "connected" if acc.connected else "waiting"
            print(f"engaged={is_openpilot_engaged} steer={steer_out:+7.2f}deg "
                  f"({steer_normalized:+.3f}) throttle={throttle_out:.3f} "
                  f"brake={brake_out:.3f} speed={speed_ms * 3.6:.1f}km/h "
                  f"telemetry={tel}")

        rk.keep_time()


def main():
    parser = argparse.ArgumentParser(description='ACC <-> openpilot bridge')
    parser.add_argument('--game-host', required=True,
                        help='IP of the game machine running ACC')
    parser.add_argument('--companion-port', type=int, default=5555,
                        help='UDP port for sending controls to companion')
    parser.add_argument('--telemetry-port', type=int, default=9000,
                        help='UDP port to receive ACC telemetry (default: 9000)')
    # Camera backend selection
    parser.add_argument('--cam-backend', default='opencv',
                        choices=['opencv', 'gstreamer', 'screen'],
                        help='Camera backend: opencv (portable), gstreamer (Jetson HW accel), or screen (local testing)')
    parser.add_argument('--camera', type=int, default=0,
                        help='OpenCV camera index (default: 0)')
    # GStreamer-specific options
    parser.add_argument('--cam-source', default='auto',
                        choices=['auto', 'v4l2', 'v4l2-mjpeg', 'v4l2-raw',
                                'v4l2-h264', 'csi', 'rtsp', 'test'],
                        help='GStreamer source type (default: auto-detect)')
    parser.add_argument('--cam-device', default='/dev/video0',
                        help='GStreamer camera device path or RTSP URL')
    parser.add_argument('--cam-width', type=int, default=1920,
                        help='Camera capture width (default: 1920)')
    parser.add_argument('--cam-height', type=int, default=1080,
                        help='Camera capture height (default: 1080)')
    parser.add_argument('--cam-fps', type=int, default=20,
                        help='Camera target FPS (default: 20)')
    # Screen capture options
    parser.add_argument('--window-title', default=None,
                        help='Game window title for screen capture (default: auto)')
    parser.add_argument('--screen-region', default=None,
                        help='Screen region as x,y,w,h for screen capture')
    args = parser.parse_args()

    # Set up openpilot params
    params = Params()
    params.clear_all()
    set_params_enabled()
    params.delete("Offroad_ConnectivityNeeded")

    # Skip calibration for sim
    msg = messaging.new_message('liveCalibration')
    msg.liveCalibration.validBlocks = 20
    msg.liveCalibration.rpyCalib = [0.0, 0.0, 0.0]
    params.put("CalibrationParams", msg.to_bytes())

    from multiprocessing import Process, Queue

    q = Queue()
    p = Process(target=bridge_main, args=(q, args))
    p.daemon = True
    p.start()

    # Keyboard input
    from pynput import keyboard

    def on_press(key):
        try:
            if key.char == 'c':
                print("OP++")
                q.put("cruise_up")
            elif key.char == 'z':
                print("OP--")
                q.put("cruise_down")
            elif key.char == 'v':
                print("OP Cancel")
                q.put("cruise_cancel")
        except AttributeError:
            pass

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

    p.join()


if __name__ == "__main__":
    main()
