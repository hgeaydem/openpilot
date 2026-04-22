#!/usr/bin/env python3
"""
BeamNG.drive UDP telemetry receiver.

Receives vehicle telemetry from BeamNG.drive via the OutGauge protocol.
OutGauge is a standard UDP telemetry format compatible with Live for Speed
and SimHub. BeamNG must be configured to send OutGauge data to the bridge
machine's IP and port.

OutGauge packet format (little-endian, 96 bytes):
  uint32  time           # time in milliseconds
  char[4] car            # car name
  uint16  flags          # info flags
  uint8   gear           # 0=reverse, 1=neutral, 2+=forward
  uint8   playerid       # player ID
  float   speed          # vehicle speed in m/s
  float   rpm            # engine RPM
  float   turbo          # turbo pressure (bar)
  float   engTemp        # engine temperature (C)
  float   fuel           # fuel level 0 to 1
  float   oilPressure    # oil pressure (bar)
  float   oilTemp        # oil temperature (C)
  uint32  dashLights     # available dash lights
  uint32  showLights     # currently active lights
  float   throttle       # throttle position 0 to 1
  float   brake          # brake position 0 to 1
  float   clutch         # clutch position 0 to 1
  char[16] display1      # display string 1
  char[16] display2      # display string 2
  int32   id             # optional packet ID

BeamNG OutGauge does NOT include steering angle or acceleration data.
Steering is reported as 0 (openpilot uses its own steering model).
Longitudinal acceleration is derived from speed changes (dv/dt).
Lateral and vertical acceleration are set to 0.

Setup in BeamNG.drive:
  Options -> Other -> OutGauge
  Enable OutGauge, set IP to bridge machine, port to 4444.
"""
import socket
import struct
import threading
import time

# OutGauge struct layout (96 bytes)
OUTGAUGE_STRUCT = struct.Struct("<"
    "I"     # time (ms)
    "4s"    # car name
    "H"     # flags
    "BB"    # gear, playerid
    "f"     # speed (m/s)
    "f"     # rpm
    "f"     # turbo
    "f"     # engTemp
    "f"     # fuel
    "f"     # oilPressure
    "f"     # oilTemp
    "I"     # dashLights
    "I"     # showLights
    "f"     # throttle
    "f"     # brake
    "f"     # clutch
    "16s"   # display1
    "16s"   # display2
    "i"     # id
)


class BeamNGTelemetry:
    def __init__(self, listen_port=4444):
        self.port = listen_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', listen_port))
        self.sock.settimeout(2.0)

        self.speed_ms = 0.0
        self.speed_kmh = 0.0
        self.steering = 0.0  # OutGauge doesn't provide steering angle
        self.throttle = 0.0
        self.brake = 0.0
        self.gear = 0
        self.rpm = 0.0
        self.acc_g = [0.0, 0.0, 0.0]

        # For deriving longitudinal acceleration from speed changes
        self._prev_speed = 0.0
        self._prev_time = 0.0

        self.connected = False
        self._running = False
        self._lock = threading.Lock()
        self._last_recv = 0

    def update(self):
        try:
            data, addr = self.sock.recvfrom(256)
            if len(data) >= OUTGAUGE_STRUCT.size:
                self._parse(data)
                if not self.connected:
                    print(f"BeamNG telemetry connected (from {addr[0]})")
                self.connected = True
                self._last_recv = time.time()
                return True
        except socket.timeout:
            if self.connected and time.time() - self._last_recv > 5.0:
                print("BeamNG telemetry connection lost")
                self.connected = False
        return False

    def _parse(self, data):
        with self._lock:
            values = OUTGAUGE_STRUCT.unpack_from(data)
            # values layout:
            #  0: time (ms)
            #  1: car name (bytes)
            #  2: flags
            #  3: gear
            #  4: playerid
            #  5: speed (m/s)
            #  6: rpm
            #  7: turbo
            #  8: engTemp
            #  9: fuel
            # 10: oilPressure
            # 11: oilTemp
            # 12: dashLights
            # 13: showLights
            # 14: throttle
            # 15: brake
            # 16: clutch
            # 17: display1
            # 18: display2
            # 19: id

            self.speed_ms = values[5]
            self.speed_kmh = values[5] * 3.6
            self.throttle = values[14]
            self.brake = values[15]
            self.gear = values[3]
            self.rpm = values[6]

            # Derive longitudinal acceleration from speed change
            now = time.time()
            dt = now - self._prev_time if self._prev_time > 0 else 0.0
            if dt > 0.001:  # avoid division by near-zero
                acc_lon = (self.speed_ms - self._prev_speed) / dt / 9.81  # convert to g
            else:
                acc_lon = 0.0
            self._prev_speed = self.speed_ms
            self._prev_time = now

            # Lateral and vertical g-force not available from OutGauge
            self.acc_g = [acc_lon, 0.0, 0.0]

    def get_state(self):
        with self._lock:
            return {
                'speed_ms': self.speed_ms,
                'speed_kmh': self.speed_kmh,
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
    parser.add_argument('--port', type=int, default=4444)
    args = parser.parse_args()

    beamng = BeamNGTelemetry(args.port)
    print(f"Listening for BeamNG telemetry on UDP port {args.port}...")

    while True:
        if beamng.update():
            s = beamng.get_state()
            print(f"speed={s['speed_kmh']:.1f}km/h steer={s['steering']:.3f} "
                  f"gas={s['throttle']:.2f} brake={s['brake']:.2f} "
                  f"rpm={s['rpm']:.0f} gear={s['gear']}")
