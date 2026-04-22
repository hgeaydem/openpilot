#!/usr/bin/env python3
"""
GTA5 companion script - runs on the GAME MACHINE.

Receives steering/throttle/brake commands from the openpilot bridge over UDP,
and emits them on a virtual Xbox 360 controller via uinput. GTA5 (native or
via Proton) reads this controller as standard gamepad input.

Usage:
  python companion.py --listen-port 5555

  In GTA5 settings, set input method to "Gamepad" and the game will
  pick up the virtual Xbox 360 controller automatically.
"""
import argparse
import socket
import struct
import time
import signal
import sys
import numpy as np

from xbox_controller import VirtualXboxController

CONTROL_STRUCT = struct.Struct("<fff?")  # steering[-1,1], throttle[0,1], brake[0,1], engaged

HEARTBEAT_TIMEOUT = 5.0


def main():
    parser = argparse.ArgumentParser(description='GTA5 Bridge companion (game machine side)')
    parser.add_argument('--listen-port', type=int, default=5555,
                        help='UDP port to receive control commands from bridge')
    parser.add_argument('--steering-sensitivity', type=float, default=1.0,
                        help='Steering multiplier [0.0-1.0] (default: 1.0)')
    parser.add_argument('--throttle-scale', type=float, default=1.0,
                        help='Throttle multiplier [0.0-1.0] (default: 1.0)')
    args = parser.parse_args()

    controller = VirtualXboxController()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', args.listen_port))
    sock.settimeout(1.0)
    print(f"Listening for bridge commands on UDP port {args.listen_port}")

    last_recv_time = time.time()
    engaged = False

    def cleanup(sig=None, frame=None):
        print("\nShutting down companion...")
        controller.reset()
        time.sleep(0.1)
        controller.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("Waiting for bridge connection...")
    print("  Make sure GTA5 input is set to 'Gamepad'")

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
                    steer_cmd = np.clip(steering * args.steering_sensitivity, -1.0, 1.0)
                    throttle_cmd = np.clip(throttle * args.throttle_scale, 0.0, 1.0)
                    controller.emit(
                        steering=steer_cmd,
                        throttle=throttle_cmd,
                        brake=brake,
                    )
                else:
                    controller.reset()

        except socket.timeout:
            if time.time() - last_recv_time > HEARTBEAT_TIMEOUT:
                if engaged:
                    print("Bridge connection lost, disengaging")
                    engaged = False
                    controller.reset()


if __name__ == "__main__":
    main()
