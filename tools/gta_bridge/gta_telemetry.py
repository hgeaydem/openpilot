#!/usr/bin/env python3
"""
GTA5 UDP telemetry receiver.

Receives vehicle telemetry from the GTAVTelemetry ScriptHookVDotNet
mod running inside GTA5. The mod sends UDP packets containing speed,
position, heading, steering angle, and other vehicle state.

Packet format (little-endian):
  float speed_ms           # vehicle speed in m/s
  float heading            # heading in degrees [0, 360)
  float pos_x, pos_y, pos_z  # world position
  float steering           # current steering angle [-1, 1]
  float throttle           # current throttle [0, 1]
  float brake              # current brake [0, 1]
  int32 gear               # current gear (0=reverse, 1=neutral, 2+=forward)
  float rpm                # engine RPM normalized [0, 1]
  float vel_x, vel_y, vel_z  # velocity vector m/s
  float acc_x, acc_y, acc_z  # acceleration vector (g)

The mod must be configured to send to the bridge machine's IP.
See gta_telemetry_mod/ for installation instructions.
"""
import socket
import struct
import threading
import time

# Matches the struct layout in GTAVTelemetry.cs
TELEMETRY_STRUCT = struct.Struct("<"
    "f"    # speed_ms
    "f"    # heading
    "fff"  # pos_x, pos_y, pos_z
    "f"    # steering
    "f"    # throttle
    "f"    # brake
    "i"    # gear
    "f"    # rpm (normalized 0-1)
    "fff"  # vel_x, vel_y, vel_z
    "fff"  # acc_x, acc_y, acc_z
)


class GTATelemetry:
    def __init__(self, listen_port=5557):
        self.port = listen_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', listen_port))
        self.sock.settimeout(2.0)

        self.speed_ms = 0.0
        self.speed_kmh = 0.0
        self.heading = 0.0
        self.pos = [0.0, 0.0, 0.0]
        self.steering = 0.0
        self.throttle = 0.0
        self.brake = 0.0
        self.gear = 0
        self.rpm = 0.0
        self.velocity = [0.0, 0.0, 0.0]
        self.acc_g = [0.0, 0.0, 0.0]

        self.connected = False
        self._running = False
        self._lock = threading.Lock()
        self._last_recv = 0

    def update(self):
        try:
            data, addr = self.sock.recvfrom(256)
            if len(data) >= TELEMETRY_STRUCT.size:
                self._parse(data)
                if not self.connected:
                    print(f"GTA5 telemetry connected (from {addr[0]})")
                self.connected = True
                self._last_recv = time.time()
                return True
        except socket.timeout:
            if self.connected and time.time() - self._last_recv > 5.0:
                print("GTA5 telemetry connection lost")
                self.connected = False
        return False

    def _parse(self, data):
        with self._lock:
            values = TELEMETRY_STRUCT.unpack_from(data)
            self.speed_ms = values[0]
            self.speed_kmh = values[0] * 3.6
            self.heading = values[1]
            self.pos = [values[2], values[3], values[4]]
            self.steering = values[5]
            self.throttle = values[6]
            self.brake = values[7]
            self.gear = values[8]
            self.rpm = values[9]
            self.velocity = [values[10], values[11], values[12]]
            self.acc_g = [values[13], values[14], values[15]]

    def get_state(self):
        with self._lock:
            return {
                'speed_ms': self.speed_ms,
                'speed_kmh': self.speed_kmh,
                'heading': self.heading,
                'steering': self.steering,
                'throttle': self.throttle,
                'brake': self.brake,
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
    parser.add_argument('--port', type=int, default=5557)
    args = parser.parse_args()

    gta = GTATelemetry(args.port)
    print(f"Listening for GTA5 telemetry on UDP port {args.port}...")

    while True:
        if gta.update():
            s = gta.get_state()
            print(f"speed={s['speed_kmh']:.1f}km/h steer={s['steering']:.3f} "
                  f"gas={s['throttle']:.2f} brake={s['brake']:.2f} "
                  f"rpm={s['rpm']:.2f} gear={s['gear']}")
