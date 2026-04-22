#!/usr/bin/env python3
"""
ACC shared memory telemetry relay.

Runs on the game machine (Windows or Linux with Proton). Reads ACC's
shared memory pages and forwards key telemetry fields over UDP to the
openpilot bridge machine.

ACC exposes three shared memory pages:
  - Physics  (acpmf_physics):  speed, acceleration, inputs, etc.
  - Graphics (acpmf_graphics): session status, position, lap info
  - Static   (acpmf_static):   car model, track name, max RPM

This relay reads the Physics page at ~100Hz and packs key fields into
a compact UDP packet for the bridge.

Platform notes:
  - Windows: uses mmap with named shared memory (tagname parameter)
  - Linux/Proton: ACC shared memory appears in /dev/shm/ as files
    named "acpmf_physics", "acpmf_graphics", "acpmf_static"

Usage:
  python acc_telemetry_relay.py --host 192.168.1.50 --port 9000
  python acc_telemetry_relay.py --host 192.168.1.50 --port 9000 --rate 100
"""
import argparse
import ctypes
import mmap
import os
import platform
import socket
import struct
import sys
import time

# --- ACC Physics shared memory layout (key fields) ---
#
# The full Physics page is approximately 716 bytes. We only read the
# fields we need. Offsets are from the start of the shared memory page.
#
# Offset  Type       Field
# 0       int        packetId
# 4       float      gas           [0, 1]
# 8       float      brake         [0, 1]
# 12      float      fuel
# 16      float      gear          (0=reverse, 1=neutral, 2+=forward)
# 20      float      rpm
# 24      float      steerAngle    (degrees)
# 28      float      speedKmh
# ...
# 56      float[3]   accG          (x=lateral, y=vertical, z=longitudinal)
# ...

class ACCPhysics(ctypes.Structure):
    """Partial layout of ACC's Physics shared memory page.

    Only fields up to and including accG are defined. The full structure
    has many more fields but we only need a subset for telemetry relay.
    """
    _fields_ = [
        ("packetId", ctypes.c_int),       # 0
        ("gas", ctypes.c_float),           # 4
        ("brake", ctypes.c_float),         # 8
        ("fuel", ctypes.c_float),          # 12
        ("gear", ctypes.c_int),            # 16
        ("rpm", ctypes.c_float),           # 20  (note: gear is int in memory)
        ("steerAngle", ctypes.c_float),    # 24
        ("speedKmh", ctypes.c_float),      # 28
        ("_pad0", ctypes.c_float * 6),     # 32-55 (velocity, pad fields)
        ("accG", ctypes.c_float * 3),      # 56 (x=lateral, y=vertical, z=longitudinal)
    ]


# UDP packet format (must match acc_telemetry.py)
ACC_TELEMETRY_STRUCT = struct.Struct("<"
    "f"     # speed_kmh
    "f"     # speed_ms
    "fff"   # acc_g_x, acc_g_y, acc_g_z
    "f"     # gas
    "f"     # brake
    "f"     # steer_angle
    "i"     # gear
    "f"     # rpm
    "f"     # turbo_boost (0 if not available)
)

# Shared memory page name / file
PHYSICS_SHM_NAME = "Local\\acpmf_physics"
PHYSICS_SHM_FILE = "acpmf_physics"  # /dev/shm/acpmf_physics on Linux
PHYSICS_SHM_SIZE = 1024  # read more than needed to be safe


def open_shared_memory():
    """Open ACC Physics shared memory, auto-detecting platform."""
    system = platform.system()

    if system == "Windows":
        # Windows: named shared memory via mmap tagname
        try:
            mm = mmap.mmap(-1, PHYSICS_SHM_SIZE, tagname=PHYSICS_SHM_NAME,
                           access=mmap.ACCESS_READ)
            print(f"Opened ACC shared memory (Windows named: {PHYSICS_SHM_NAME})")
            return mm
        except Exception as e:
            # Try without "Local\" prefix
            alt_name = "acpmf_physics"
            try:
                mm = mmap.mmap(-1, PHYSICS_SHM_SIZE, tagname=alt_name,
                               access=mmap.ACCESS_READ)
                print(f"Opened ACC shared memory (Windows named: {alt_name})")
                return mm
            except Exception:
                raise RuntimeError(
                    f"Cannot open ACC shared memory on Windows.\n"
                    f"  Tried: {PHYSICS_SHM_NAME}, {alt_name}\n"
                    f"  Make sure ACC is running.\n"
                    f"  Original error: {e}"
                )

    else:
        # Linux / Proton: shared memory files in /dev/shm/
        shm_path = os.path.join("/dev/shm", PHYSICS_SHM_FILE)
        if not os.path.exists(shm_path):
            raise RuntimeError(
                f"ACC shared memory file not found: {shm_path}\n"
                f"  Make sure ACC is running under Proton/Wine.\n"
                f"  Proton usually maps shared memory to /dev/shm/."
            )

        fd = os.open(shm_path, os.O_RDONLY)
        mm = mmap.mmap(fd, PHYSICS_SHM_SIZE, access=mmap.ACCESS_READ)
        os.close(fd)
        print(f"Opened ACC shared memory (Linux: {shm_path})")
        return mm


def read_physics(mm):
    """Read physics data from the memory-mapped shared memory page."""
    mm.seek(0)
    raw = mm.read(ctypes.sizeof(ACCPhysics))
    physics = ACCPhysics.from_buffer_copy(raw)
    return physics


def main():
    parser = argparse.ArgumentParser(
        description='ACC shared memory telemetry relay',
    )
    parser.add_argument('--host', required=True,
                        help='Bridge machine IP address')
    parser.add_argument('--port', type=int, default=9000,
                        help='UDP port on bridge machine (default: 9000)')
    parser.add_argument('--rate', type=int, default=100,
                        help='Send rate in Hz (default: 100)')
    args = parser.parse_args()

    print(f"ACC Telemetry Relay")
    print(f"  Target: {args.host}:{args.port}")
    print(f"  Rate: {args.rate} Hz")
    print()

    # Open shared memory
    print("Waiting for ACC shared memory...")
    mm = None
    while mm is None:
        try:
            mm = open_shared_memory()
        except RuntimeError as e:
            print(f"  {e}")
            print("  Retrying in 3 seconds...")
            time.sleep(3)

    # UDP socket for sending
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    target = (args.host, args.port)

    dt = 1.0 / args.rate
    last_packet_id = -1
    send_count = 0

    print(f"Relaying ACC telemetry to {args.host}:{args.port}...")

    try:
        while True:
            t0 = time.time()

            physics = read_physics(mm)

            # Only send if data has changed (new packetId)
            if physics.packetId != last_packet_id:
                last_packet_id = physics.packetId

                speed_kmh = physics.speedKmh
                speed_ms = speed_kmh / 3.6

                packet = ACC_TELEMETRY_STRUCT.pack(
                    speed_kmh,
                    speed_ms,
                    physics.accG[0],   # lateral
                    physics.accG[1],   # vertical
                    physics.accG[2],   # longitudinal
                    physics.gas,
                    physics.brake,
                    physics.steerAngle,
                    physics.gear,
                    physics.rpm,
                    0.0,  # turbo_boost (not directly exposed in basic physics)
                )

                try:
                    sock.sendto(packet, target)
                    send_count += 1
                except OSError:
                    pass

                if send_count % (args.rate * 5) == 0 and send_count > 0:
                    print(f"  Sent {send_count} packets | "
                          f"speed={speed_kmh:.1f}km/h rpm={physics.rpm:.0f} "
                          f"gear={physics.gear} gas={physics.gas:.2f} "
                          f"brake={physics.brake:.2f}")

            elapsed = time.time() - t0
            if elapsed < dt:
                time.sleep(dt - elapsed)

    except KeyboardInterrupt:
        print(f"\nStopped after sending {send_count} packets.")
    finally:
        mm.close()
        sock.close()


if __name__ == "__main__":
    main()
