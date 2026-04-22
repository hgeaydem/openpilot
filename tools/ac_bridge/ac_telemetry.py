#!/usr/bin/env python3
"""
Assetto Corsa UDP telemetry client.

Connects to AC's built-in UDP telemetry server (default port 9996)
and parses RTCarInfo packets for vehicle state feedback.

AC UDP protocol:
  1. Send handshake (operationId=0) to AC server
  2. Receive handshake response (car name, driver name, track info)
  3. Send subscribe (operationId=1) to start receiving updates
  4. Receive RTCarInfo packets continuously
  5. Send dismiss (operationId=3) to disconnect
"""
import socket
import struct
import threading
import time

# AC UDP operation IDs
OP_HANDSHAKE = 0
OP_SUBSCRIBE_UPDATE = 1
OP_SUBSCRIBE_SPOT = 2
OP_DISMISS = 3

HANDSHAKE_STRUCT = struct.Struct("<III")

# RTCarInfo struct layout (Windows MSVC packing, bools as 4-byte ints)
# Total expected size: ~328 bytes (may vary by AC version)
RT_CAR_INFO_FIELDS = {
    'speed_kmh':  (0, '<f'),
    'speed_mph':  (4, '<f'),
    'speed_ms':   (8, '<f'),
    'is_abs_enabled':     (12, '<I'),
    'is_abs_in_action':   (16, '<I'),
    'is_tc_in_action':    (20, '<I'),
    'is_tc_enabled':      (24, '<I'),
    'is_in_pit':          (28, '<I'),
    'is_engine_limiter':  (32, '<I'),
    'acc_g_vertical':     (36, '<f'),
    'acc_g_horizontal':   (40, '<f'),
    'acc_g_frontal':      (44, '<f'),
    'lap_time':           (48, '<i'),
    'last_lap':           (52, '<i'),
    'best_lap':           (56, '<i'),
    'lap_count':          (60, '<i'),
    'gas':                (64, '<f'),
    'brake':              (68, '<f'),
    'clutch':             (72, '<f'),
    'engine_rpm':         (76, '<f'),
    'steer':              (80, '<f'),
    'gear':               (84, '<i'),
    'cg_height':          (88, '<f'),
}

# Offset where the float[4] arrays start (after cgHeight)
ARRAYS_OFFSET = 92


class ACTelemetry:
    def __init__(self, ac_host, ac_port=9996):
        self.host = ac_host
        self.port = ac_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)

        self.speed_kmh = 0.0
        self.speed_ms = 0.0
        self.gas = 0.0
        self.brake = 0.0
        self.clutch = 0.0
        self.steer = 0.0
        self.engine_rpm = 0.0
        self.gear = 0
        self.acc_g = [0.0, 0.0, 0.0]
        self.lap_count = 0

        self.car_name = ""
        self.driver_name = ""
        self.track_name = ""

        self.connected = False
        self._running = False
        self._lock = threading.Lock()

    def connect(self):
        data = HANDSHAKE_STRUCT.pack(1, 1, OP_HANDSHAKE)
        self.sock.sendto(data, (self.host, self.port))

        try:
            response, addr = self.sock.recvfrom(1024)
            if len(response) >= 100:
                self._parse_handshake_response(response)
                data = HANDSHAKE_STRUCT.pack(1, 1, OP_SUBSCRIBE_UPDATE)
                self.sock.sendto(data, (self.host, self.port))
                self.connected = True
                print(f"AC telemetry connected: car={self.car_name} track={self.track_name}")
                return True
        except socket.timeout:
            print(f"AC telemetry handshake timeout (is AC running on {self.host}:{self.port}?)")
        return False

    def disconnect(self):
        if self.connected:
            data = HANDSHAKE_STRUCT.pack(1, 1, OP_DISMISS)
            self.sock.sendto(data, (self.host, self.port))
            self.connected = False
        self._running = False

    def _parse_handshake_response(self, data):
        try:
            # Windows wchar_t is 2 bytes (UTF-16LE)
            self.car_name = data[0:100].decode('utf-16-le', errors='ignore').rstrip('\x00')
            self.driver_name = data[100:200].decode('utf-16-le', errors='ignore').rstrip('\x00')
            if len(data) >= 408:
                self.track_name = data[208:308].decode('utf-16-le', errors='ignore').rstrip('\x00')
        except Exception as e:
            print(f"Handshake parse warning: {e}")

    def update(self):
        try:
            data, addr = self.sock.recvfrom(4096)
            if len(data) > 50:
                self._parse_rt_car_info(data)
                return True
        except socket.timeout:
            pass
        return False

    def _parse_rt_car_info(self, data):
        with self._lock:
            try:
                for field_name, (offset, fmt) in RT_CAR_INFO_FIELDS.items():
                    if offset + struct.calcsize(fmt) <= len(data):
                        value = struct.unpack_from(fmt, data, offset)[0]
                        setattr(self, field_name, value)

                self.acc_g = [
                    self.acc_g_vertical if hasattr(self, 'acc_g_vertical') else 0.0,
                    self.acc_g_horizontal if hasattr(self, 'acc_g_horizontal') else 0.0,
                    self.acc_g_frontal if hasattr(self, 'acc_g_frontal') else 0.0,
                ]
            except struct.error:
                pass

    def get_state(self):
        with self._lock:
            return {
                'speed_kmh': self.speed_kmh,
                'speed_ms': self.speed_ms,
                'gas': self.gas,
                'brake': self.brake,
                'steer': self.steer,
                'engine_rpm': self.engine_rpm,
                'gear': self.gear,
                'acc_g': list(self.acc_g),
            }

    def start_async(self):
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        return t

    def _poll_loop(self):
        while self._running and self.connected:
            self.update()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='192.168.1.100')
    parser.add_argument('--port', type=int, default=9996)
    args = parser.parse_args()

    ac = ACTelemetry(args.host, args.port)
    if ac.connect():
        while True:
            if ac.update():
                s = ac.get_state()
                print(f"speed={s['speed_kmh']:.1f}km/h steer={s['steer']:.3f} "
                      f"gas={s['gas']:.2f} brake={s['brake']:.2f} "
                      f"rpm={s['engine_rpm']:.0f} gear={s['gear']}")
