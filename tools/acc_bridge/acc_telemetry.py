#!/usr/bin/env python3
"""
ACC (Assetto Corsa Competizione) UDP telemetry receiver.

Receives vehicle telemetry from the acc_telemetry_relay.py script,
which reads ACC's shared memory and forwards it over UDP. ACC uses
a shared memory interface (not native UDP), so the relay bridges
the gap.

Packet format (little-endian, 44 bytes):
  float speed_kmh          # vehicle speed in km/h
  float speed_ms           # vehicle speed in m/s (speed_kmh / 3.6)
  float acc_g_x            # lateral acceleration (g)
  float acc_g_y            # vertical acceleration (g)
  float acc_g_z            # longitudinal acceleration (g)
  float gas                # throttle [0, 1]
  float brake              # brake [0, 1]
  float steer_angle        # steering angle (degrees)
  int32 gear               # gear (0=reverse, 1=neutral, 2+=forward)
  float rpm                # engine RPM
  float turbo_boost        # turbo boost pressure

The relay must be running on the game machine and configured to send
to this bridge machine's IP. See acc_telemetry_relay.py for setup.
"""
import socket
import struct
import threading
import time

# Matches the struct layout in acc_telemetry_relay.py
ACC_TELEMETRY_STRUCT = struct.Struct("<"
    "f"     # speed_kmh
    "f"     # speed_ms (speed_kmh / 3.6)
    "fff"   # acc_g_x, acc_g_y, acc_g_z (lateral, vertical, longitudinal)
    "f"     # gas (0-1)
    "f"     # brake (0-1)
    "f"     # steer_angle (degrees)
    "i"     # gear (0=reverse, 1=neutral, 2+=forward)
    "f"     # rpm
    "f"     # turbo_boost
)


class ACCTelemetry:
    def __init__(self, listen_port=9000):
        self.port = listen_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', listen_port))
        self.sock.settimeout(2.0)

        self.speed_ms = 0.0
        self.speed_kmh = 0.0
        self.gas = 0.0
        self.brake = 0.0
        self.steer = 0.0
        self.gear = 0
        self.rpm = 0.0
        self.turbo_boost = 0.0
        self.acc_g = [0.0, 0.0, 0.0]

        self.connected = False
        self._running = False
        self._lock = threading.Lock()
        self._last_recv = 0

    def update(self):
        try:
            data, addr = self.sock.recvfrom(256)
            if len(data) >= ACC_TELEMETRY_STRUCT.size:
                self._parse(data)
                if not self.connected:
                    print(f"ACC telemetry connected (from {addr[0]})")
                self.connected = True
                self._last_recv = time.time()
                return True
        except socket.timeout:
            if self.connected and time.time() - self._last_recv > 5.0:
                print("ACC telemetry connection lost")
                self.connected = False
        return False

    def _parse(self, data):
        with self._lock:
            values = ACC_TELEMETRY_STRUCT.unpack_from(data)
            self.speed_kmh = values[0]
            self.speed_ms = values[1]
            self.acc_g = [values[2], values[3], values[4]]
            self.gas = values[5]
            self.brake = values[6]
            self.steer = values[7]
            self.gear = values[8]
            self.rpm = values[9]
            self.turbo_boost = values[10]

    def get_state(self):
        with self._lock:
            return {
                'speed_ms': self.speed_ms,
                'speed_kmh': self.speed_kmh,
                'gas': self.gas,
                'brake': self.brake,
                'steer': self.steer,
                'gear': self.gear,
                'rpm': self.rpm,
                'acc_g': list(self.acc_g),
            }

    def start_async(self):
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        return t

    def _poll_loop(self):
        while self._running:
            self.update()

    def stop(self):
        self._running = False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=9000)
    args = parser.parse_args()

    acc = ACCTelemetry(args.port)
    print(f"Listening for ACC telemetry on UDP port {args.port}...")

    while True:
        if acc.update():
            s = acc.get_state()
            print(f"speed={s['speed_kmh']:.1f}km/h steer={s['steer']:.3f} "
                  f"gas={s['gas']:.2f} brake={s['brake']:.2f} "
                  f"rpm={s['rpm']:.0f} gear={s['gear']}")
