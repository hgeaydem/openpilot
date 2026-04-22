# GTA5 <-> openpilot Bridge

A bridge that connects [openpilot](https://github.com/commaai/openpilot) to [GTA5](https://www.rockstargames.com/gta-v), enabling openpilot's self-driving stack to control a car in-game through a virtual Xbox 360 controller.

Supports two modes:
- **Two-machine**: A camera on the openpilot machine captures the game screen from a separate game machine. The companion runs on the game machine.
- **Local (single-machine)**: Screen capture grabs frames directly from the game window — no camera or second machine needed. Uses GPU-accelerated NVIDIA NvFBC capture when available, with GStreamer and CPU fallbacks.

openpilot processes the video feed through its full perception and control pipeline (modeld, plannerd, controlsd), producing steering, throttle, and brake commands. These commands are sent to a companion process, which emits them on a virtual Xbox 360 controller via Linux uinput. GTA5 reads this controller as standard gamepad input.

A ScriptHookVDotNet C# mod installed in GTA5 sends real-time vehicle telemetry (speed, acceleration, position) over UDP to the bridge for closed-loop control.

---

## Table of Contents

- [Architecture](#architecture)
- [Hardware Requirements](#hardware-requirements)
- [Network Setup](#network-setup)
- [File Reference](#file-reference)
- [Setup: Game Machine (Companion)](#setup-game-machine-companion)
  - [Prerequisites](#companion-prerequisites)
  - [GTA5 Telemetry Mod Installation](#gta5-telemetry-mod-installation)
  - [Running the Companion](#running-the-companion)
  - [GTA5 Controller Settings](#gta5-controller-settings)
- [Setup: Bridge Machine (openpilot)](#setup-bridge-machine-openpilot)
  - [Option A: Native Linux (OpenCV)](#option-a-native-linux-opencv)
  - [Option B: NVIDIA Jetson (GStreamer + DeepStream)](#option-b-nvidia-jetson-gstreamer--deepstream)
  - [Option C: Local (Screen Capture)](#option-c-local-screen-capture)
- [Usage](#usage)
  - [Quick Start](#quick-start)
  - [Keyboard Controls](#keyboard-controls)
  - [Bridge CLI Reference](#bridge-cli-reference)
  - [Companion CLI Reference](#companion-cli-reference)
- [Camera Backends](#camera-backends)
  - [OpenCV Backend](#opencv-backend)
  - [GStreamer/NVIDIA Backend (Jetson)](#gstreamernvidia-backend-jetson)
  - [GStreamer Source Types](#gstreamer-source-types)
  - [Screen Capture Backend](#screen-capture-backend)
  - [Testing the Camera](#testing-the-camera)
- [Jetson Deployment](#jetson-deployment)
  - [Container Build](#container-build)
  - [Container Run](#container-run)
  - [JetPack Version Compatibility](#jetpack-version-compatibility)
  - [RTSP Capture (Alternative to Physical Camera)](#rtsp-capture-alternative-to-physical-camera)
- [Steering Tuning](#steering-tuning)
  - [tweak.cfg Parameters](#tweakcfg-parameters)
  - [Dynamic Steering Ratio](#dynamic-steering-ratio)
  - [Tuning Procedure](#tuning-procedure)
- [GTA5 Telemetry Mod](#gta5-telemetry-mod)
  - [How It Works](#how-it-works)
  - [Telemetry Fields](#telemetry-fields)
  - [Configuration](#telemetry-configuration)
  - [Testing Telemetry](#testing-telemetry)
- [Virtual Xbox Controller](#virtual-xbox-controller)
  - [How It Works](#xbox-how-it-works)
  - [Axis Mapping](#axis-mapping)
- [Internal Architecture](#internal-architecture)
  - [Data Flow Detail](#data-flow-detail)
  - [Threading Model](#threading-model)
  - [Network Protocol (Bridge to Companion)](#network-protocol-bridge-to-companion)
  - [Fake Peripheral Systems](#fake-peripheral-systems)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
  BRIDGE MACHINE                              GAME MACHINE
  (Linux / Jetson)                            (Linux, running GTA5)
 ┌──────────────────────────┐                ┌────────────────────────────┐
 │                          │                │                            │
 │  Camera                  │                │  GTA5 (via Proton/Wine)    │
 │   │ (USB / CSI / HDMI    │                │   │                        │
 │   │  capture card)       │                │   │ reads virtual Xbox pad │
 │   ▼                      │                │   │                        │
 │  bridge.py               │                │   ▲                        │
 │   ├─ frames ──► modeld   │                │   │                        │
 │   │             │        │   UDP:5557     │   │                        │
 │   ├─ telemetry ◄─────────────────────────────── GTAVTelemetry.cs mod  │
 │   │             ▼        │                │   │                        │
 │   ├─ fake CAN  plannerd  │                │  companion.py             │
 │   ├─ fake panda │        │                │   ├─ virtual Xbox 360     │
 │   ├─ fake DM    ▼        │                │   │  controller (uinput)  │
 │   │         controlsd    │                │   │                        │
 │   │             │        │   UDP:5555     │   │                        │
 │   └─ steer/gas/brake ────────────────────────►└─ steering + pedals    │
 │      (normalized)        │                │                            │
 │                          │                │                            │
 └──────────────────────────┘                └────────────────────────────┘
```

**Key difference from the AC bridge**: GTA5 doesn't support force-feedback wheels natively, so the companion emulates a standard Xbox 360 controller via Linux uinput. GTA5 (native or via Proton) recognizes this as a real gamepad. Telemetry comes from a ScriptHookVDotNet mod rather than a built-in UDP API.

---

## Hardware Requirements

### Bridge Machine (openpilot host)
- Linux (x86_64 or aarch64)
- Camera: USB webcam, USB HDMI capture card (e.g., Elgato Cam Link), or Jetson CSI camera
- For Jetson deployment: NVIDIA Jetson Orin / Xavier / Nano with JetPack 5.x or 6.x
- Network connection to the game machine

### Game Machine
- Linux (for uinput virtual controller support; GTA5 runs via Proton/Steam or Wine)
- ScriptHookV + ScriptHookVDotNet installed in the GTA5 directory
- Network connection to the bridge machine

---

## Network Setup

The bridge and game machines communicate over two UDP channels:

| Channel | Direction | Port | Purpose |
|---|---|---|---|
| GTA5 Telemetry | Game &rarr; Bridge | 5557 | Vehicle state: speed, acceleration, position |
| Control Commands | Bridge &rarr; Game | 5555 | Steering, throttle, brake, engaged flag |

Both machines must be on the same network. If a firewall is running, open UDP port 5557 outbound on the game machine and UDP port 5555 inbound on the game machine.

---

## File Reference

```
tools/gta_bridge/
├── bridge.py              # Main bridge (runs on openpilot machine)
├── companion.py           # Companion (runs on game machine)
├── gta_telemetry.py       # UDP telemetry receiver
├── xbox_controller.py     # Virtual Xbox 360 controller via uinput
├── camera_gst.py          # GStreamer/NVIDIA HW-accelerated camera
├── lib/
│   ├── __init__.py
│   └── can.py             # Fake Honda CAN messages for openpilot
├── gta_telemetry_mod/
│   ├── GTAVTelemetry.cs   # ScriptHookVDotNet C# mod (install in GTA5)
│   └── GTAVTelemetry.ini  # Mod configuration (bridge IP + port)
├── screen_capture.py      # Screen capture backends (NvFBC/GStreamer/mss)
├── local_bridge.py        # Single-machine launcher (screen capture mode)
├── tweak.cfg              # Steering ratio tuning (hot-reloadable)
├── Dockerfile.l4t         # L4T container for Jetson
├── run_jetson.sh          # One-command Jetson container launch
└── requirements.txt       # Python dependencies
```

| File | Machine | Description |
|---|---|---|
| `bridge.py` | Bridge | Captures camera frames, publishes to openpilot via cereal, receives GTA5 telemetry, reads openpilot's control output, sends commands to companion. Supports OpenCV and GStreamer camera backends. |
| `companion.py` | Game | Receives control commands over UDP. Emits steering, throttle, and brake on a virtual Xbox 360 controller. Auto-disengages on connection loss. |
| `gta_telemetry.py` | Bridge | Listens for UDP telemetry packets from the GTA5 mod. Thread-safe state access. |
| `xbox_controller.py` | Game | Creates a virtual Xbox 360 controller using Linux uinput with Microsoft vendor/product IDs. Games recognize it as a standard Xbox pad. |
| `camera_gst.py` | Bridge (Jetson) | GStreamer camera capture with NVIDIA hardware acceleration. Supports v4l2, CSI, RTSP, and test sources. |
| `lib/can.py` | Bridge | Generates fake Honda Civic CAN messages for openpilot's car interface. |
| `GTAVTelemetry.cs` | Game (GTA5 mod) | ScriptHookVDotNet script that reads vehicle state from GTA5 native functions and sends UDP packets to the bridge. |
| `screen_capture.py` | Bridge | Screen capture with three-tier backend hierarchy: NVIDIA NvFBC (GPU-accelerated), GStreamer ximagesrc (GPU resize), mss+OpenCV (CPU fallback). For local single-machine mode. |
| `local_bridge.py` | Bridge | Convenience launcher for single-machine mode. Starts companion as subprocess and bridge with screen capture backend. |
| `tweak.cfg` | Bridge | Steering ratio parameters. Watched for live changes (no restart needed). |
| `Dockerfile.l4t` | Build | L4T container based on `deepstream-l4t:7.1-samples-multiarch`. |
| `run_jetson.sh` | Bridge (Jetson) | Builds container if needed, passes through camera devices and NVIDIA runtime. |

---

## Setup: Game Machine (Companion)

### Companion Prerequisites

```bash
# Python 3.8+ required
pip install evdev numpy

# Verify uinput module is loaded (for virtual Xbox controller)
sudo modprobe uinput
ls /dev/uinput  # should exist

# Your user needs write access to /dev/uinput
# Either run companion as root, or add a udev rule:
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules
sudo usermod -aG input $USER
# Log out and back in for group change to take effect
```

### GTA5 Telemetry Mod Installation

The telemetry mod requires ScriptHookV and ScriptHookVDotNet:

1. **Install ScriptHookV**: Download from [dev-c.com](http://www.dev-c.com/gtav/scripthookv/), copy `ScriptHookV.dll` and `dinput8.dll` to the GTA5 root directory.

2. **Install ScriptHookVDotNet**: Download from [GitHub](https://github.com/scripthookvdotnet/scripthookvdotnet/releases), copy `ScriptHookVDotNet.asi`, `ScriptHookVDotNet2.dll`, and `ScriptHookVDotNet3.dll` to the GTA5 root directory.

3. **Install the telemetry mod**:
   ```
   GTA5 root/
   ├── scripts/
   │   └── GTAVTelemetry.dll    # Compile GTAVTelemetry.cs or copy pre-built
   ├── GTAVTelemetry.ini        # Copy from gta_telemetry_mod/
   ├── ScriptHookV.dll
   ├── ScriptHookVDotNet.asi
   └── ...
   ```

4. **Configure the mod**: Edit `GTAVTelemetry.ini` in the GTA5 root directory:
   ```ini
   [Settings]
   ; IP address of the bridge machine
   host = 192.168.1.50
   ; UDP port (must match bridge --telemetry-port)
   port = 5557
   ```

5. **Compile the mod** (if not using pre-built):
   ```bash
   # On Windows with .NET SDK:
   csc /target:library /reference:ScriptHookVDotNet3.dll GTAVTelemetry.cs
   # Copy GTAVTelemetry.dll to GTA5/scripts/
   ```

### Running the Companion

```bash
# Basic usage
python companion.py --listen-port 5555

# With reduced steering sensitivity (for testing)
python companion.py --listen-port 5555 --steering-sensitivity 0.7

# With throttle scaling
python companion.py --listen-port 5555 --throttle-scale 0.8
```

### GTA5 Controller Settings

Configure GTA5 to use gamepad input:

1. In GTA5 Settings &rarr; Controls, set **Input Method** to **Gamepad**
2. The virtual Xbox 360 controller will be detected automatically
3. Default gamepad mapping works out of the box:
   - Left stick X axis = steering
   - Right trigger = throttle
   - Left trigger = brake

---

## Setup: Bridge Machine (openpilot)

### Option A: Native Linux (OpenCV)

For any Linux machine with a USB camera:

```bash
# Install dependencies
pip install opencv-python numpy pynput watchdog

# openpilot must be built and its processes running:
# modeld, plannerd, controlsd, radard, calibrationd, etc.

# Run the bridge
python bridge.py --game-host <GAME_MACHINE_IP> --camera 0
```

### Option B: NVIDIA Jetson (GStreamer + DeepStream)

For hardware-accelerated performance on Jetson:

```bash
# One-command launch (builds container on first run):
./run_jetson.sh --game-host <GAME_MACHINE_IP>

# Or build and run manually:
docker build -f Dockerfile.l4t -t gta-bridge-l4t ../..
docker run -it --rm --runtime nvidia --network host --ipc host \
    --device=/dev/video0 \
    gta-bridge-l4t \
    --cam-backend gstreamer --cam-source v4l2-mjpeg \
    --game-host <GAME_MACHINE_IP>
```

### Option C: Local (Screen Capture)

For running everything on one machine — no camera or second machine needed. The bridge captures frames directly from the game window using screen capture.

```bash
# Install dependencies (adds mss for screen capture)
pip install opencv-python numpy pynput watchdog mss evdev

# One-command launch (starts companion + bridge with screen capture):
python local_bridge.py

# Capture a specific window by title
python local_bridge.py --window-title "Grand Theft Auto V"

# Capture a specific screen region (x,y,w,h)
python local_bridge.py --screen-region 0,0,1920,1080

# With reduced steering sensitivity (for testing)
python local_bridge.py --steering-sensitivity 0.7 --throttle-scale 0.8
```

The screen capture backend auto-detects the best available method:
1. **NVIDIA NvFBC** (fastest) — GPU-accelerated framebuffer capture via `libnvidia-fbc.so.1`
2. **GStreamer ximagesrc** — X11 capture with GPU-accelerated resize via `nvvideoconvert`
3. **mss + OpenCV** (most portable) — CPU-based screen capture, works everywhere

See [Screen Capture Backend](#screen-capture-backend) for details on each tier.

#### Local Bridge CLI Reference

```
python local_bridge.py [OPTIONS]

Network:
  --companion-port PORT        Internal UDP port for bridge<->companion (default: 5555)
  --telemetry-port PORT        UDP port for GTA5 telemetry (default: 5557)

Screen capture:
  --cam-fps FPS                Screen capture FPS (default: 20)
  --window-title TITLE         Game window title for targeted capture
  --screen-region x,y,w,h      Capture a specific screen region

Companion (controller):
  --steering-sensitivity FLOAT Steering multiplier 0.0-1.0 (default: 1.0)
  --throttle-scale FLOAT       Throttle multiplier 0.0-1.0 (default: 1.0)
```

---

## Usage

### Quick Start

**1. Start the companion on the game machine:**

```bash
python companion.py --listen-port 5555
```

**2. Launch GTA5 and enter a vehicle**

The GTAVTelemetry mod loads automatically with the game and begins sending telemetry.

**3. Start the bridge on the openpilot machine:**

```bash
# OpenCV (any Linux)
python bridge.py --game-host 192.168.1.100 --camera 0

# GStreamer (Jetson)
python bridge.py --game-host 192.168.1.100 --cam-backend gstreamer --cam-source v4l2-mjpeg
```

**4. Engage openpilot:**

Press `C` on the bridge machine to engage openpilot and set a cruise speed. The virtual Xbox controller will begin steering the car.

### Keyboard Controls

All keyboard controls are on the bridge machine:

| Key | Action |
|-----|--------|
| `C` | Engage openpilot / increase cruise speed |
| `Z` | Decrease cruise speed |
| `V` | Disengage openpilot (cancel) |

### Bridge CLI Reference

```
python bridge.py [OPTIONS]

Required:
  --game-host HOST          IP of the game machine running GTA5

Network:
  --companion-port PORT     UDP port for control commands (default: 5555)
  --telemetry-port PORT     UDP port to receive GTA5 telemetry (default: 5557)

Camera backend:
  --cam-backend {opencv,gstreamer,screen}
                            opencv: portable (default)
                            gstreamer: NVIDIA HW-accelerated (Jetson)
                            screen: screen capture (local mode)

OpenCV options:
  --camera INDEX            Camera device index (default: 0)

GStreamer options:
  --cam-source TYPE         Source type (default: auto)
                            auto, v4l2, v4l2-mjpeg, v4l2-raw,
                            v4l2-h264, csi, rtsp, test
  --cam-device PATH         Device path or RTSP URL (default: /dev/video0)
  --cam-width WIDTH         Capture width (default: 1920)
  --cam-height HEIGHT       Capture height (default: 1080)

Screen capture options:
  --window-title TITLE      Game window title for targeted capture
  --screen-region x,y,w,h   Capture a specific screen region

Common:
  --cam-fps FPS             Target framerate (default: 20)
```

### Companion CLI Reference

```
python companion.py [OPTIONS]

Network:
  --listen-port PORT        UDP port for receiving commands (default: 5555)

Controller tuning:
  --steering-sensitivity F  Steering multiplier 0.0-1.0 (default: 1.0)
  --throttle-scale F        Throttle multiplier 0.0-1.0 (default: 1.0)
```

---

## Camera Backends

### OpenCV Backend

The default backend. Uses `cv2.VideoCapture` to read from a camera, performs crop and resize on the CPU, and converts BGR to RGB. Works on any Linux system with a USB camera.

```bash
python bridge.py --game-host 192.168.1.100 --cam-backend opencv --camera 0
```

Frame processing pipeline (CPU):
```
v4l2 capture → BGR numpy array → aspect-ratio crop → resize to 1164x874 → BGR→RGB → cereal
```

### GStreamer/NVIDIA Backend (Jetson)

Hardware-accelerated backend for NVIDIA Jetson. Uses GStreamer with NVIDIA DeepStream plugins. All decode, resize, and color conversion happens on the GPU.

```bash
python bridge.py --game-host 192.168.1.100 --cam-backend gstreamer --cam-source v4l2-mjpeg
```

Frame processing pipeline (GPU):
```
v4l2 capture → nvjpegdec (GPU decode) → nvvideoconvert (GPU resize+colorspace) → RGB appsink → cereal
```

### GStreamer Source Types

| Source | Use Case | GStreamer Pipeline |
|---|---|---|
| `auto` | Auto-detect V4L2 camera format | Probes with `v4l2-ctl`, picks best |
| `v4l2` | Generic V4L2 fallback | `v4l2src → videoconvert → nvvideoconvert` |
| `v4l2-mjpeg` | USB camera / HDMI capture (MJPEG) | `v4l2src → nvjpegdec → nvvideoconvert` |
| `v4l2-raw` | USB camera (YUYV/UYVY) | `v4l2src → nvvideoconvert` |
| `v4l2-h264` | Capture card (H.264 output) | `v4l2src → h264parse → nvv4l2decoder → nvvideoconvert` |
| `csi` | Jetson CSI camera | `nvarguscamerasrc → nvvideoconvert` |
| `rtsp` | Network RTSP stream | `rtspsrc → rtph264depay → h264parse → nvv4l2decoder → nvvideoconvert` |
| `test` | Development test pattern | `videotestsrc → nvvideoconvert` |

### Screen Capture Backend

For single-machine (local) mode. Captures frames directly from the X11 display or a specific window, with no physical camera needed. The backend auto-detects the best available capture method at startup.

**Tier 1: NVIDIA NvFBC (GPU-accelerated)**

Uses NVIDIA's Frame Buffer Capture API via `libnvidia-fbc.so.1`. The GPU handles capture, crop, scale, and RGB conversion entirely — frames arrive in system memory already at the target resolution (1164x874). This is the fastest option with minimal CPU usage.

Requirements:
- NVIDIA GPU with proprietary drivers (440+)
- `libnvidia-fbc.so.1` present (included with most NVIDIA driver installations)
- X11 display server (Wayland is not supported by NvFBC)
- NvFBC may require a professional-grade GPU (Quadro/Tesla) or [patching](https://github.com/keylase/nvidia-patch) for consumer GPUs

```bash
# Verify NvFBC is available
ldconfig -p | grep libnvidia-fbc
# → libnvidia-fbc.so.1 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libnvidia-fbc.so.1
```

**Tier 2: GStreamer ximagesrc (GPU resize)**

Falls back to GStreamer's `ximagesrc` for X11 capture, with `nvvideoconvert` for GPU-accelerated resize. Capture is CPU-based but the expensive resize/color-conversion step runs on the GPU.

Requirements:
- GStreamer 1.0 with `gst-plugins-good` (provides `ximagesrc`)
- NVIDIA GStreamer plugins (`nvvideoconvert`) for GPU resize
- X11 display server

```bash
# Verify GStreamer and ximagesrc are available
gst-inspect-1.0 ximagesrc
gst-inspect-1.0 nvvideoconvert
```

**Tier 3: mss + OpenCV (CPU fallback)**

Pure CPU fallback using the `mss` library for screen capture and OpenCV for resize/color conversion. Works on any system with a display but uses more CPU than the GPU-accelerated options.

Requirements:
- `mss` Python package (`pip install mss`)
- Any display server (X11 or Wayland)

**Backend selection** is automatic. The bridge logs which backend was selected at startup:

```
NvFBC capture initialized (1164x874)           # Tier 1
# or
GStreamer ximagesrc capture initialized         # Tier 2
# or
mss screen capture initialized (monitor 1)     # Tier 3
```

### Testing the Camera

```bash
# Auto-detect format and test capture
python camera_gst.py --source auto --device /dev/video0

# Test with MJPEG USB camera
python camera_gst.py --source v4l2-mjpeg --device /dev/video0 --width 1920 --height 1080 --fps 20

# Test pattern (no camera needed)
python camera_gst.py --source test
```

---

## Jetson Deployment

### Container Build

```bash
# Build from the openpilot root directory
docker build -f tools/gta_bridge/Dockerfile.l4t -t gta-bridge-l4t .

# Or with a different DeepStream base version
docker build -f tools/gta_bridge/Dockerfile.l4t \
    --build-arg DEEPSTREAM_TAG=6.3-samples \
    -t gta-bridge-l4t .
```

### Container Run

The `run_jetson.sh` script handles device passthrough, NVIDIA runtime, and networking:

```bash
# Basic (auto-detect camera format)
./run_jetson.sh --game-host 192.168.1.100

# Specify camera format
./run_jetson.sh --game-host 192.168.1.100 --cam-source v4l2-mjpeg --cam-device /dev/video0

# RTSP capture instead of physical camera
./run_jetson.sh --game-host 192.168.1.100 \
    --cam-source rtsp --cam-device rtsp://192.168.1.100:8554/game
```

Environment variables for `run_jetson.sh`:

| Variable | Default | Description |
|---|---|---|
| `CAMERA_DEVICE` | `/dev/video0` | Default camera device |
| `IMAGE_TAG` | `gta-bridge-l4t` | Docker image tag |
| `DEEPSTREAM_TAG` | `7.1-samples-multiarch` | DeepStream base image tag |

### JetPack Version Compatibility

| JetPack | L4T | DeepStream Tag | Notes |
|---|---|---|---|
| 6.x (Orin) | R36.x | `7.1-samples-multiarch` | Default, recommended |
| 5.1.x (Orin/Xavier) | R35.x | `6.3-samples` or `6.4-samples` | Set `DEEPSTREAM_TAG` |

### RTSP Capture (Alternative to Physical Camera)

Instead of pointing a physical camera at the game screen, you can stream the game screen over RTSP for better quality and lower latency.

On the game machine, use OBS Studio with the RTSP output plugin:

1. Install OBS Studio and the [obs-rtspserver](https://github.com/iamscottxu/obs-rtspserver) plugin
2. Set up a scene capturing the game window
3. Start the RTSP server (e.g., on port 8554)

On the bridge machine:

```bash
python bridge.py --game-host 192.168.1.100 \
    --cam-backend gstreamer \
    --cam-source rtsp \
    --cam-device rtsp://192.168.1.100:8554/game
```

---

## Steering Tuning

### tweak.cfg Parameters

The `tweak.cfg` file controls how openpilot's steering angle output maps to the controller. The file is watched at runtime; edits take effect immediately without restarting the bridge.

```ini
[steer]
; Steering ratio - higher means less sensitive steering
; GTA5 has arcade-style steering, so a lower ratio works better than sims
STEER_RATIO = 12

; Speed (km/h) at which dynamic ratio adjustment starts
STEER_SPEED_OFFSET = 15

; Base maximum steering angle in degrees
BASE_MAX_STEER_ANGLE = 35

; Higher value = less ratio change per unit speed
STEER_SPEED_DENOM = 1.5
```

### Dynamic Steering Ratio

The effective maximum steering angle increases with speed:

```
max_steer_angle = BASE_MAX_STEER_ANGLE + max(0, speed_kmh - STEER_SPEED_OFFSET) / STEER_SPEED_DENOM
```

The normalized steering output sent to the companion is:

```
steer_normalized = steer_angle_deg / (max_steer_angle * STEER_RATIO)
```

This value is clipped to `[-1, 1]` and maps to the full Xbox left stick range.

### Tuning Procedure

1. Start with the defaults and engage openpilot on a straight road in GTA5
2. If the car oscillates (weaving left-right): increase `STEER_RATIO` or increase `BASE_MAX_STEER_ANGLE`
3. If the car doesn't turn enough in corners: decrease `STEER_RATIO` or decrease `BASE_MAX_STEER_ANGLE`
4. If steering is good at low speed but too aggressive at high speed: increase `STEER_SPEED_DENOM`
5. Edit `tweak.cfg` while the bridge is running; changes are applied within 1 second

GTA5 has more arcade-like steering than a sim like Assetto Corsa, so the default values use a lower `STEER_RATIO` (12 vs 15) and lower `BASE_MAX_STEER_ANGLE` (35 vs 40).

---

## GTA5 Telemetry Mod

### How It Works

The `GTAVTelemetry.cs` ScriptHookVDotNet script runs inside GTA5 and reads vehicle state using GTA5's native scripting API. It packs the data into a binary struct and sends it over UDP to the bridge machine at approximately 100Hz.

Unlike Assetto Corsa (which has a built-in UDP telemetry server), GTA5 requires this mod to export vehicle state. The mod pushes data to the bridge (no handshake needed).

### Telemetry Fields

| Field | Type | Description |
|---|---|---|
| `speed_ms` | float | Vehicle speed in m/s |
| `heading` | float | Vehicle heading in radians |
| `pos_x/y/z` | float | World position |
| `steering` | float | Current steering angle |
| `throttle` | float | Current throttle position |
| `brake` | float | Current brake pressure |
| `gear` | int32 | Current gear |
| `rpm` | float | Engine RPM |
| `vel_x/y/z` | float | Velocity vector |
| `acc_x/y/z` | float | Acceleration vector (used for IMU simulation) |

### Telemetry Configuration

Edit `GTAVTelemetry.ini` in the GTA5 root directory:

```ini
[Settings]
; IP address of the bridge machine running openpilot
host = 192.168.1.50
; UDP port (must match --telemetry-port on the bridge)
port = 5557
```

### Testing Telemetry

Test the telemetry receiver independently:

```bash
python gta_telemetry.py
```

Start GTA5, enter a vehicle, and you should see:

```
Listening for GTA5 telemetry on UDP port 5557
GTA5 telemetry connected (from 192.168.1.100)
speed=65.3km/h heading=1.23 gear=3 rpm=4500
```

---

## Virtual Xbox Controller

### How It Works

The companion creates a virtual Xbox 360 controller using Linux's uinput subsystem. It registers with Microsoft's Xbox 360 vendor (0x045E) and product (0x028E) IDs, so games and system utilities (like Steam Input) recognize it as a real Xbox 360 pad.

The controller supports:
- Left stick X axis (steering)
- Left and right triggers (brake and throttle)
- All standard Xbox buttons and axes (for future use)

### Axis Mapping

| Xbox Axis | evdev Code | Range | Bridge Use |
|---|---|---|---|
| Left Stick X | `ABS_X` | -32768 to 32767 | Steering |
| Left Trigger | `ABS_Z` | 0 to 255 | Brake |
| Right Trigger | `ABS_RZ` | 0 to 255 | Throttle |
| Left Stick Y | `ABS_Y` | -32768 to 32767 | Unused |
| Right Stick X/Y | `ABS_RX`/`ABS_RY` | -32768 to 32767 | Unused |

---

## Internal Architecture

### Data Flow Detail

```
┌─ Camera ──────────────────────────────────────────────────────────────────┐
│ OpenCV or GStreamer capture → crop/resize to 1164x874 RGB                │
│ → cereal PubMaster('roadCameraState') → modeld                          │
└───────────────────────────────────────────────────────────────────────────┘

┌─ GTA5 Telemetry ─────────────────────────────────────────────────────────┐
│ UDP from GTA5 mod (speed, acceleration)                                  │
│ → fake CAN messages (Honda Civic format)                                 │
│ → cereal PubMaster('can') → controlsd                                    │
│                                                                           │
│ Acceleration → cereal PubMaster('sensorEvents') → locationd              │
└───────────────────────────────────────────────────────────────────────────┘

┌─ Fake Peripherals ────────────────────────────────────────────────────────┐
│ pandaState: ignition=on, controlsAllowed=true, safety=hondaNidec         │
│ driverState: faceProb=1.0 (driver always attentive)                      │
│ driverMonitoringState: faceDetected=true, isDistracted=false             │
│ gpsLocationExternal: empty (no GPS data)                                 │
│ liveCalibration: pre-set to [0, 0, 0] (skip calibration)                │
└───────────────────────────────────────────────────────────────────────────┘

┌─ openpilot ───────────────────────────────────────────────────────────────┐
│ modeld (neural net) → modelV2 (lane lines, path plan)                    │
│ plannerd → lateralPlan + longitudinalPlan                                │
│ controlsd → carControl (actuators: gas, brake, steeringAngleDesiredDeg)  │
└───────────────────────────────────────────────────────────────────────────┘

┌─ Output ──────────────────────────────────────────────────────────────────┐
│ cereal SubMaster('carControl', 'controlsState')                          │
│ → normalize steering to [-1, 1]                                          │
│ → pack as struct { float steer, throttle, brake; bool engaged; }         │
│ → UDP to companion on game machine                                       │
│ → companion emits on virtual Xbox 360 controller                         │
└───────────────────────────────────────────────────────────────────────────┘
```

### Threading Model

The bridge runs these threads concurrently:

| Thread | Rate | Purpose |
|---|---|---|
| Camera capture | 20 Hz | Capture, crop, resize, publish to cereal |
| Main control loop | 100 Hz | Read openpilot output, send to companion, update CAN |
| CAN publisher | 100 Hz | Fake Honda CAN messages with current vehicle state |
| pandaState | 2 Hz | Fake panda hardware state |
| Driver monitoring | 10 Hz | Fake driver face detection |
| GPS | 10 Hz | Empty GPS messages |
| Telemetry receiver | async | Listen for UDP telemetry from GTA5 mod |
| Config watcher | event | Watch `tweak.cfg` for changes (via watchdog) |

### Network Protocol (Bridge to Companion)

Control packets are sent over UDP at 100Hz. The packet format is a packed C struct:

```
Offset  Type     Field      Range        Description
0       float    steering   [-1.0, 1.0]  Normalized steering angle
4       float    throttle   [0.0, 1.0]   Throttle position
8       float    brake      [0.0, 1.0]   Brake position
12      uint8    engaged    0 or 1       openpilot engagement state
```

Total size: 13 bytes. Packed as `struct.Struct("<fff?")` (little-endian).

### Fake Peripheral Systems

openpilot expects to be running on real comma.ai hardware. The bridge fakes these systems:

| System | Faked As | Why |
|---|---|---|
| Panda (CAN interface) | Black Panda, Honda Nidec safety model | Required for controlsd to allow control output |
| CAN bus | Honda Civic Touring 2016 messages | openpilot uses car-specific CAN parsing; Honda Civic is the simplest |
| Driver monitoring | Face always detected, not distracted | Prevents driver attention warnings |
| GPS | Empty messages | Prevents GPS-related errors |
| Calibration | Pre-calibrated at [0, 0, 0] | Skips the calibration procedure |

---

## Troubleshooting

**"Listening for GTA5 telemetry..." but no data received**
- Verify GTA5 is running with ScriptHookV and ScriptHookVDotNet loaded
- Check that GTAVTelemetry.ini has the correct bridge machine IP
- Verify the telemetry port matches (default: 5557)
- Check network connectivity: `ping <BRIDGE_MACHINE_IP>` from the game machine
- Ensure UDP port 5557 is not blocked by a firewall on the bridge machine

**Virtual Xbox controller not detected by GTA5**
- Verify uinput is loaded: `ls /dev/uinput`
- Run companion as root if permission denied: `sudo python companion.py`
- Check that GTA5 input method is set to "Gamepad" in settings
- Verify the device was created: `ls /dev/input/event*` (should show a new device after starting companion)

**Car weaves left-right in game**
- Increase `STEER_RATIO` in `tweak.cfg`
- Increase `BASE_MAX_STEER_ANGLE` in `tweak.cfg`
- Reduce `--steering-sensitivity` on the companion
- These can be edited live while the bridge is running

**Car doesn't turn enough in corners**
- Decrease `STEER_RATIO` in `tweak.cfg`
- Decrease `BASE_MAX_STEER_ANGLE` in `tweak.cfg`

**GStreamer pipeline fails to start**
- Check camera format support: `v4l2-ctl -d /dev/video0 --list-formats-ext`
- Try a different `--cam-source` (e.g., `v4l2` instead of `v4l2-mjpeg`)
- On Jetson, verify NVIDIA plugins: `gst-inspect-1.0 nvvideoconvert`

**"Bridge connection lost, disengaging" (companion)**
- Network issue between bridge and game machines
- Bridge may have crashed; check bridge terminal for errors

**openpilot doesn't engage**
- Verify openpilot processes are running (modeld, plannerd, controlsd)
- Check that calibration was set: look for "liveCalibration" in bridge startup
- Verify frames are being published: look for increasing frame count in bridge output
- Try pressing `C` multiple times (each press increases cruise speed)

**Screen capture: "NvFBC not available, trying GStreamer..."**
- NvFBC requires NVIDIA proprietary drivers with `libnvidia-fbc.so.1`
- Consumer GPUs (GeForce) may need the [nvidia-patch](https://github.com/keylase/nvidia-patch) to unlock NvFBC
- Check library presence: `ldconfig -p | grep libnvidia-fbc`
- NvFBC only works under X11, not Wayland

**Screen capture: black frames or wrong region captured**
- Use `--window-title "Grand Theft Auto V"` to target the game window specifically
- Use `--screen-region x,y,w,h` to manually specify the capture region
- Verify the game is not minimized (screen capture cannot capture minimized windows)
- If using NvFBC, ensure the game is visible on the primary display

**Screen capture: low FPS**
- NvFBC is fastest; if it's not available, check the troubleshooting entry above
- Reduce `--cam-fps` if the system can't sustain the requested rate
- Close other GPU-intensive applications competing for resources
- mss (CPU fallback) is the slowest option; ensure GStreamer or NvFBC is available for better performance

**Low FPS / high latency**
- On x86: switch to GStreamer backend if possible, or reduce camera resolution
- On Jetson: ensure NVIDIA runtime is active (`--runtime nvidia`)
- Reduce `--cam-fps` if the camera can't sustain the requested rate
