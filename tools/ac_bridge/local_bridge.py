#!/usr/bin/env python3
"""
Local Assetto Corsa bridge - runs bridge + companion on the same machine.

Uses screen capture instead of a physical camera to grab frames directly
from the game window. Launches the companion automatically.

Usage:
  python local_bridge.py --ac-host 127.0.0.1
  python local_bridge.py --ac-host 127.0.0.1 --window-title "Assetto Corsa"
  python local_bridge.py --ac-host 127.0.0.1 --screen-region 0,0,1920,1080
  python local_bridge.py --ac-host 127.0.0.1 --no-ffb
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
        description='AC local bridge (single machine, screen capture)',
    )
    parser.add_argument('--ac-host', default='127.0.0.1',
                        help='AC telemetry host (default: 127.0.0.1)')
    parser.add_argument('--ac-port', type=int, default=9996,
                        help='AC telemetry UDP port (default: 9996)')
    parser.add_argument('--companion-port', type=int, default=5555,
                        help='Internal UDP port for bridge<->companion (default: 5555)')
    parser.add_argument('--cam-fps', type=int, default=20,
                        help='Screen capture FPS (default: 20)')
    parser.add_argument('--window-title', default=None,
                        help='Game window title for screen capture')
    parser.add_argument('--screen-region', default=None,
                        help='Screen region as x,y,w,h')
    # Companion options
    parser.add_argument('--no-ffb', action='store_true',
                        help='Disable FFB wheel control (pedals only)')
    parser.add_argument('--ffb-strength', type=float, default=1.0,
                        help='FFB strength multiplier [0.0-1.0]')
    parser.add_argument('--p-gain', type=float, default=5.0,
                        help='FFB position tracking P gain')
    parser.add_argument('--d-gain', type=float, default=0.3,
                        help='FFB position tracking D gain')
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
        '--ffb-strength', str(args.ffb_strength),
        '--p-gain', str(args.p_gain),
        '--d-gain', str(args.d_gain),
    ]
    if args.no_ffb:
        companion_cmd.append('--no-ffb')

    print("Starting companion...")
    companion_proc = subprocess.Popen(companion_cmd)

    # Build bridge command
    bridge_cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, 'bridge.py'),
        '--ac-host', args.ac_host,
        '--ac-port', str(args.ac_port),
        '--companion-port', str(args.companion_port),
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
