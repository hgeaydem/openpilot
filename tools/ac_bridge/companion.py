#!/usr/bin/env python3
"""
Companion script - runs on the GAME MACHINE (where AC and Fanatec DD+ are).

Receives steering/throttle/brake commands from the openpilot bridge over UDP,
then:
  1. Sends FFB to the real Fanatec DD+ to physically turn the wheel
     (AC reads the wheel position as steering input)
  2. Emits throttle/brake on a virtual pedal device via uinput

The bridge machine sends control packets at 100Hz as:
  struct { float steering; float throttle; float brake; uint8_t engaged; }

Usage:
  On the game machine (with Fanatec DD+ connected):
    python companion.py --listen-port 5555

  Configure AC to use:
    - Fanatec wheel for steering axis
    - "AC Bridge Virtual Pedals" for throttle/brake axes
"""
import argparse
import socket
import struct
import time
import signal
import sys
import numpy as np

from fanatec_ffb import FanatecFFBController, VirtualPedals, find_fanatec_wheel

CONTROL_STRUCT = struct.Struct("<fff?")  # steering[-1,1], throttle[0,1], brake[0,1], engaged

HEARTBEAT_TIMEOUT = 5.0


def main():
    parser = argparse.ArgumentParser(description='AC Bridge companion (game machine side)')
    parser.add_argument('--listen-port', type=int, default=5555,
                        help='UDP port to receive control commands from bridge')
    parser.add_argument('--no-ffb', action='store_true',
                        help='Disable FFB wheel control (pedals only)')
    parser.add_argument('--ffb-strength', type=float, default=1.0,
                        help='FFB strength multiplier [0.0-1.0]')
    parser.add_argument('--p-gain', type=float, default=5.0,
                        help='Position tracking P gain')
    parser.add_argument('--d-gain', type=float, default=0.3,
                        help='Position tracking D gain')
    args = parser.parse_args()

    ffb_ctrl = None
    pedals = None

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
                    steer_cmd = np.clip(steering * args.ffb_strength, -1.0, 1.0)
                    if ffb_ctrl:
                        ffb_ctrl.set_target(steer_cmd)
                    pedals.emit(throttle, brake)
                else:
                    if ffb_ctrl:
                        ffb_ctrl.set_target(0.0)
                    pedals.emit(0, 0)

        except socket.timeout:
            if time.time() - last_recv_time > HEARTBEAT_TIMEOUT:
                if engaged:
                    print("Bridge connection lost, disengaging")
                    engaged = False
                    if ffb_ctrl:
                        ffb_ctrl.set_target(0.0)
                    pedals.emit(0, 0)


if __name__ == "__main__":
    main()
