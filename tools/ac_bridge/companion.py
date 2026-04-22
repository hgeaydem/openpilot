#!/usr/bin/env python3
"""
Companion script - runs on the GAME MACHINE (where AC is running).

Receives steering/throttle/brake commands from the openpilot bridge over UDP,
then applies them via one of two controller backends:

  Fanatec mode (default):
    1. Sends FFB to the real Fanatec DD+ to physically turn the wheel
       (AC reads the wheel position as steering input)
    2. Emits throttle/brake on a virtual pedal device via uinput

  Xbox mode (--xbox):
    1. Emits steering/throttle/brake on a virtual Xbox 360 controller
       (AC reads it as a standard gamepad)

The bridge machine sends control packets at 100Hz as:
  struct { float steering; float throttle; float brake; uint8_t engaged; }

Usage:
  Fanatec DD+ mode:
    python companion.py --listen-port 5555

  Xbox controller mode:
    python companion.py --listen-port 5555 --xbox
    python companion.py --listen-port 5555 --xbox --steering-sensitivity 0.7
"""
import argparse
import socket
import struct
import time
import signal
import sys
import numpy as np

from fanatec_ffb import FanatecFFBController, VirtualPedals, find_fanatec_wheel
from xbox_controller import VirtualXboxController

CONTROL_STRUCT = struct.Struct("<fff?")  # steering[-1,1], throttle[0,1], brake[0,1], engaged

HEARTBEAT_TIMEOUT = 5.0


def main():
    parser = argparse.ArgumentParser(description='AC Bridge companion (game machine side)')
    parser.add_argument('--listen-port', type=int, default=5555,
                        help='UDP port to receive control commands from bridge')
    parser.add_argument('--xbox', action='store_true',
                        help='Use virtual Xbox 360 controller instead of Fanatec wheel')
    parser.add_argument('--steering-sensitivity', type=float, default=1.0,
                        help='Steering multiplier [0.0-1.0] (Xbox mode, default: 1.0)')
    parser.add_argument('--throttle-scale', type=float, default=1.0,
                        help='Throttle multiplier [0.0-1.0] (Xbox mode, default: 1.0)')
    parser.add_argument('--no-ffb', action='store_true',
                        help='Disable FFB wheel control (pedals only, Fanatec mode)')
    parser.add_argument('--ffb-strength', type=float, default=1.0,
                        help='FFB strength multiplier [0.0-1.0] (Fanatec mode)')
    parser.add_argument('--p-gain', type=float, default=5.0,
                        help='Position tracking P gain (Fanatec mode)')
    parser.add_argument('--d-gain', type=float, default=0.3,
                        help='Position tracking D gain (Fanatec mode)')
    args = parser.parse_args()

    use_xbox = args.xbox
    controller = None
    ffb_ctrl = None
    pedals = None

    if use_xbox:
        controller = VirtualXboxController()
        print("Xbox 360 controller mode active")
    else:
        if not args.no_ffb:
            wheel = find_fanatec_wheel()
            if wheel:
                ffb_ctrl = FanatecFFBController(wheel)
                ffb_ctrl.start()
                print("FFB wheel control active")
            else:
                print("WARNING: No Fanatec wheel found, running pedals-only mode")
        pedals = VirtualPedals()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', args.listen_port))
    sock.settimeout(1.0)
    print(f"Listening for bridge commands on UDP port {args.listen_port}")

    last_recv_time = time.time()
    engaged = False

    def cleanup(sig=None, frame=None):
        print("\nShutting down companion...")
        if controller:
            controller.reset()
            time.sleep(0.1)
            controller.close()
        if ffb_ctrl:
            ffb_ctrl.set_target(0.0)
            time.sleep(0.1)
            ffb_ctrl.stop()
        if pedals:
            pedals.emit(0, 0)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("Waiting for bridge connection...")

    while True:
        try:
            data, addr = sock.recvfrom(256)
            last_recv_time = time.time()

            if len(data) >= CONTROL_STRUCT.size:
                steering, throttle, brake, eng = CONTROL_STRUCT.unpack_from(data)

                if eng and not engaged:
                    print(f"openpilot ENGAGED (from {addr[0]})")
                elif not eng and engaged:
                    print("openpilot DISENGAGED")
                engaged = eng

                if engaged:
                    if use_xbox:
                        steer_cmd = np.clip(steering * args.steering_sensitivity, -1.0, 1.0)
                        throttle_cmd = np.clip(throttle * args.throttle_scale, 0.0, 1.0)
                        controller.emit(steering=steer_cmd, throttle=throttle_cmd, brake=brake)
                    else:
                        steer_cmd = np.clip(steering * args.ffb_strength, -1.0, 1.0)
                        if ffb_ctrl:
                            ffb_ctrl.set_target(steer_cmd)
                        pedals.emit(throttle, brake)
                else:
                    if use_xbox:
                        controller.reset()
                    else:
                        if ffb_ctrl:
                            ffb_ctrl.set_target(0.0)
                        pedals.emit(0, 0)

        except socket.timeout:
            if time.time() - last_recv_time > HEARTBEAT_TIMEOUT:
                if engaged:
                    print("Bridge connection lost, disengaging")
                    engaged = False
                    if use_xbox:
                        controller.reset()
                    else:
                        if ffb_ctrl:
                            ffb_ctrl.set_target(0.0)
                        pedals.emit(0, 0)


if __name__ == "__main__":
    main()
