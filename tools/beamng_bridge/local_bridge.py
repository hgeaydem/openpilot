#!/usr/bin/env python3
"""
Local BeamNG.drive bridge - runs bridge + companion on the same machine.

Uses screen capture instead of a physical camera to grab frames directly
from the game window. Launches the companion automatically with a
virtual Xbox 360 controller.

Usage:
  python local_bridge.py
  python local_bridge.py --window-title "BeamNG.drive"
  python local_bridge.py --screen-region 0,0,1920,1080
  python local_bridge.py --steering-sensitivity 0.7 --throttle-scale 0.8
"""
import argparse
import subprocess
import sys
import os
import signal
import atexit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(
        description='BeamNG.drive local bridge (single machine, screen capture)',
    )
    parser.add_argument('--companion-port', type=int, default=5555,
                        help='Internal UDP port for bridge<->companion (default: 5555)')
    parser.add_argument('--telemetry-port', type=int, default=4444,
                        help='UDP port for BeamNG OutGauge telemetry (default: 4444)')
    parser.add_argument('--cam-fps', type=int, default=20,
                        help='Screen capture FPS (default: 20)')
    parser.add_argument('--window-title', default=None,
                        help='Game window title for screen capture')
    parser.add_argument('--screen-region', default=None,
                        help='Screen region as x,y,w,h')
    # Companion options
    parser.add_argument('--steering-sensitivity', type=float, default=1.0,
                        help='Steering multiplier [0.0-1.0] (default: 1.0)')
    parser.add_argument('--throttle-scale', type=float, default=1.0,
                        help='Throttle multiplier [0.0-1.0] (default: 1.0)')
    args = parser.parse_args()

    companion_proc = None

    def cleanup(sig=None, frame=None):
        if companion_proc and companion_proc.poll() is None:
            companion_proc.terminate()
            try:
                companion_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                companion_proc.kill()

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda s, f: (cleanup(s, f), sys.exit(0)))

    # Start companion
    companion_cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, 'companion.py'),
        '--listen-port', str(args.companion_port),
        '--steering-sensitivity', str(args.steering_sensitivity),
        '--throttle-scale', str(args.throttle_scale),
    ]

    print("Starting companion (virtual Xbox 360 controller)...")
    companion_proc = subprocess.Popen(companion_cmd)

    # Build bridge command
    bridge_cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, 'bridge.py'),
        '--game-host', '127.0.0.1',
        '--companion-port', str(args.companion_port),
        '--telemetry-port', str(args.telemetry_port),
        '--cam-backend', 'screen',
        '--cam-fps', str(args.cam_fps),
    ]
    if args.window_title:
        bridge_cmd.extend(['--window-title', args.window_title])
    if args.screen_region:
        bridge_cmd.extend(['--screen-region', args.screen_region])

    print("Starting bridge with screen capture...")
    try:
        bridge_proc = subprocess.Popen(bridge_cmd)
        bridge_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
