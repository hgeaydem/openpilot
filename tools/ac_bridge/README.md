# Assetto Corsa &lt;-&gt; openpilot Bridge

A bridge that connects [openpilot](https://github.com/commaai/openpilot) to [Assetto Corsa](https://www.assettocorsa.it/), enabling openpilot's self-driving stack to control a car in-game through a real Fanatec Clubsport DD+ wheelbase.

Supports two modes:
- **Two-machine**: A camera on the openpilot machine captures the game screen from a separate game machine. The companion runs on the game machine.
- **Local (single-machine)**: Screen capture grabs frames directly from the game window — no camera or second machine needed. Uses GPU-accelerated NVIDIA NvFBC capture when available, with GStreamer and CPU fallbacks.

openpilot processes the video feed through its full perception and control pipeline (modeld, plannerd, controlsd), producing steering, throttle, and brake commands. These commands are sent to a companion process, which uses force feedback to physically turn the Fanatec wheel (the game reads the wheel position as steering input) and emits throttle/brake on a virtual pedal device.

Assetto Corsa's built-in UDP telemetry feeds real-time vehicle state (speed, g-forces, RPM) back to openpilot for closed-loop control.

---

## Table of Contents

- [Architecture](#architecture)
- [Hardware Requirements](#hardware-requirements)
- [Network Setup](#network-setup)
- [File Reference](#file-reference)
- [Setup: Game Machine (Companion)](#setup-game-machine-companion)
  - [Prerequisites](#companion-prerequisites)
  - [Assetto Corsa Telemetry Configuration](#assetto-corsa-telemetry-configuration)
  - [Fanatec DD+ Linux Driver](#fanatec-dd-linux-driver)
  - [Running the Companion](#running-the-companion)
  - [Assetto Corsa Controller Mapping](#assetto-corsa-controller-mapping)
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
- [TensorRT Acceleration (modeld)](#tensorrt-acceleration-modeld)
  - [How It Works](#tensorrt-how-it-works)
  - [Local Setup (x86 with NVIDIA GPU)](#local-setup-x86-with-nvidia-gpu)
  - [Jetson Setup (L4T Container)](#jetson-setup-l4t-container)
  - [Verifying TensorRT Is Active](#verifying-tensorrt-is-active)
- [Steering Tuning](#steering-tuning)
  - [tweak.cfg Parameters](#tweakcfg-parameters)
  - [Dynamic Steering Ratio](#dynamic-steering-ratio)
  - [Tuning Procedure](#tuning-procedure)
- [Fanatec FFB Wheel Control](#fanatec-ffb-wheel-control)
  - [How It Works](#how-it-works)
  - [PD Controller Tuning](#pd-controller-tuning)
  - [Supported Wheelbases](#supported-wheelbases)
  - [Safety Features](#safety-features)
- [Assetto Corsa Telemetry Protocol](#assetto-corsa-telemetry-protocol)
  - [UDP Handshake](#udp-handshake)
  - [RTCarInfo Fields](#rtcarinfo-fields)
  - [Testing Telemetry](#testing-telemetry)
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
  (Linux / Jetson)                            (Linux, running AC)
 ┌──────────────────────────┐                ┌────────────────────────────┐
 │                          │                │                            │
 │  Camera                  │                │  Assetto Corsa             │
 │   │ (USB / CSI / HDMI    │                │   │                        │
 │   │  capture card)       │                │   │ reads wheel pos        │
 │   ▼                      │                │   │ reads virtual pedals   │
 │  bridge.py               │                │   │                        │
 │   ├─ frames ──► modeld   │                │   ▲          ▲             │
 │   │             │        │   UDP:9996     │   │          │             │
 │   ├─ telemetry ◄─────────────────────────────── AC telemetry server   │
 │   │             ▼        │                │   │          │             │
 │   ├─ fake CAN  plannerd  │                │  companion.py             │
 │   ├─ fake panda │        │                │   ├─ FFB ──►│Fanatec DD+  │
 │   ├─ fake DM    ▼        │                │   │  (wheel turns to      │
 │   │         controlsd    │                │   │   target angle)        │
 │   │             │        │   UDP:5555     │   │          │             │
 │   └─ steer/gas/brake ────────────────────────►├─ virtual pedals       │
 │      (normalized)        │                │   │  (throttle/brake)      │
 │                          │                │   │                        │
 └──────────────────────────┘                └────────────────────────────┘
```

**Key insight**: The real Fanatec DD+ serves as both the physical feedback device AND the game input. The companion sends force feedback to steer the wheel to openpilot's target angle; Assetto Corsa reads the wheel's physical position directly. No virtual steering device is needed.

---

## Hardware Requirements

### Bridge Machine (openpilot host)
- Linux (x86_64 or aarch64)
- Camera: USB webcam, USB HDMI capture card (e.g., Elgato Cam Link), or Jetson CSI camera
- For Jetson deployment: NVIDIA Jetson Orin / Xavier / Nano with JetPack 5.x or 6.x
- Network connection to the game machine

### Game Machine
- Linux (AC runs via Proton/Steam)
- **Fanatec mode** (default): Fanatec Clubsport DD+ wheelbase (or compatible, see [Supported Wheelbases](#supported-wheelbases)) + [hid-fanatecff](https://github.com/gotzl/hid-fanatecff) kernel module
- **Xbox mode** (`--xbox`): No physical wheel needed — uses a virtual Xbox 360 controller via uinput
- Assetto Corsa with UDP telemetry enabled
- Network connection to the bridge machine (or localhost for local mode)

---

## Network Setup

The bridge and game machines communicate over two UDP channels:

| Channel | Direction | Port | Purpose |
|---|---|---|---|
| AC Telemetry | Game &rarr; Bridge | 9996 | Vehicle state: speed, g-forces, RPM, gear |
| Control Commands | Bridge &rarr; Game | 5555 | Steering, throttle, brake, engaged flag |

Both machines must be on the same network. No port forwarding is required if they are on the same subnet. If a firewall is running, open UDP ports 9996 (on the game machine) and 5555 (on the game machine).

---

## File Reference

```
tools/ac_bridge/
├── bridge.py              # Main bridge (runs on openpilot machine)
├── companion.py           # Companion (runs on game machine)
├── ac_telemetry.py        # AC UDP telemetry client
├── fanatec_ffb.py         # Fanatec FFB controller + virtual pedals
├── camera_gst.py          # GStreamer/NVIDIA HW-accelerated camera
├── lib/
│   ├── __init__.py
│   └── can.py             # Fake Honda CAN messages for openpilot
├── xbox_controller.py     # Virtual Xbox 360 controller via uinput
├── screen_capture.py      # Screen capture backends (NvFBC/GStreamer/mss)
├── local_bridge.py        # Single-machine launcher (screen capture mode)
├── tweak.cfg              # Steering ratio tuning (hot-reloadable)
├── Dockerfile.l4t         # L4T container for Jetson
├── run_jetson.sh          # One-command Jetson container launch
├── requirements.txt       # Python deps (bridge + companion)
└── requirements_jetson.txt # Python deps for L4T container
```

| File | Machine | Description |
|---|---|---|
| `bridge.py` | Bridge | Captures camera frames, publishes to openpilot via cereal, receives AC telemetry, reads openpilot's control output, sends commands to companion. Supports OpenCV and GStreamer camera backends. |
| `companion.py` | Game | Receives control commands over UDP. Supports two modes: Fanatec (FFB wheel + virtual pedals) and Xbox (virtual Xbox 360 controller). Auto-disengages on connection loss. |
| `xbox_controller.py` | Game | Creates a virtual Xbox 360 controller using Linux uinput with Microsoft vendor/product IDs. Alternative to Fanatec wheel for steering via gamepad input. |
| `ac_telemetry.py` | Bridge | Implements the AC UDP telemetry protocol: handshake, subscribe, and continuous RTCarInfo parsing. Thread-safe with async polling mode. |
| `fanatec_ffb.py` | Game | Finds Fanatec wheel via evdev, implements PD position servo using FF_CONSTANT effects at 200Hz. Creates virtual pedal device via uinput. |
| `camera_gst.py` | Bridge (Jetson) | GStreamer camera capture with NVIDIA hardware acceleration. Supports v4l2 (MJPEG/raw/H.264), CSI, RTSP, and test sources. All decode, resize, and color conversion on GPU. |
| `lib/can.py` | Bridge | Generates fake Honda Civic CAN messages for openpilot's car interface. Simulates engine data, wheel speeds, steering sensors, cruise buttons, and radar. |
| `screen_capture.py` | Bridge | Screen capture with three-tier backend hierarchy: NVIDIA NvFBC (GPU-accelerated), GStreamer ximagesrc (GPU resize), mss+OpenCV (CPU fallback). For local single-machine mode. |
| `local_bridge.py` | Bridge | Convenience launcher for single-machine mode. Starts companion as subprocess and bridge with screen capture backend. |
| `tweak.cfg` | Bridge | Steering ratio parameters. Watched by the bridge for live changes (no restart needed). |
| `Dockerfile.l4t` | Build | L4T container image based on `deepstream-l4t:7.1-samples-multiarch`. Installs openpilot deps and builds cereal/opendbc. |
| `run_jetson.sh` | Bridge (Jetson) | Builds the container if needed, passes through camera devices and NVIDIA runtime, mounts tweak.cfg for live tuning. |

---

## Setup: Game Machine (Companion)

### Companion Prerequisites

```bash
# Python 3.8+ required
pip install evdev numpy

# Verify uinput module is loaded (for virtual pedals)
sudo modprobe uinput
ls /dev/uinput  # should exist
```

### Assetto Corsa Telemetry Configuration

AC's UDP telemetry must be enabled for the bridge to receive vehicle state. AC listens on UDP port 9996 by default and responds to telemetry handshakes from any client.

1. Launch Assetto Corsa (via Steam/Proton on Linux)
2. The telemetry server starts automatically when a session is loaded
3. No in-game configuration is needed; the bridge connects to AC by sending a UDP handshake

If AC is running under Proton, ensure the network port is not blocked by any firewall rules.

### Fanatec DD+ Linux Driver

The [hid-fanatecff](https://github.com/gotzl/hid-fanatecff) kernel module is required for force feedback support:

```bash
git clone https://github.com/gotzl/hid-fanatecff.git
cd hid-fanatecff
make
sudo make install

# Load the module
sudo modprobe hid-fanatec

# Verify the wheel is detected
sudo dmesg | grep -i fanatec
ls /dev/input/event*  # should show a new device
```

Set the Fanatec wheelbase to **PC mode** (indicated by the red LED on the base).

Verify the wheel is working:

```bash
# List input devices (look for Fanatec)
evtest

# Or use the built-in detection:
python fanatec_ffb.py
```

### Running the Companion

**Fanatec mode** (default — requires Fanatec wheel):

```bash
# Basic usage
python companion.py --listen-port 5555

# With reduced FFB strength (recommended for initial testing)
python companion.py --listen-port 5555 --ffb-strength 0.5

# Pedals only (no wheel FFB, for testing)
python companion.py --listen-port 5555 --no-ffb
```

**Xbox mode** (no wheel required):

```bash
# Virtual Xbox 360 controller
python companion.py --listen-port 5555 --xbox

# With reduced steering sensitivity
python companion.py --listen-port 5555 --xbox --steering-sensitivity 0.7

# With throttle scaling
python companion.py --listen-port 5555 --xbox --throttle-scale 0.8
```

### Assetto Corsa Controller Mapping

#### Fanatec Mode

1. **Steering**: Assign to the Fanatec wheel (it will be physically moved by FFB)
2. **Throttle**: Assign to "AC Bridge Virtual Pedals" &rarr; ABS_Z axis
3. **Brake**: Assign to "AC Bridge Virtual Pedals" &rarr; ABS_RZ axis

In AC's controller settings:
- Go to Settings &rarr; Controls
- Select the Fanatec device for the steering axis
- Select "AC Bridge Virtual Pedals" for gas and brake axes
- Set steering linearity and deadzone to minimum (the bridge handles all processing)

#### Xbox Mode

1. In AC Settings &rarr; Controls, set input method to **Gamepad**
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
python bridge.py --ac-host <GAME_MACHINE_IP> --camera 0
```

### Option B: NVIDIA Jetson (GStreamer + DeepStream)

For hardware-accelerated performance on Jetson:

```bash
# One-command launch (builds container on first run):
./run_jetson.sh --ac-host <GAME_MACHINE_IP>

# Or build and run manually:
docker build -f Dockerfile.l4t -t ac-bridge-l4t ../..
docker run -it --rm --runtime nvidia --network host --ipc host \
    --device=/dev/video0 \
    ac-bridge-l4t \
    --cam-backend gstreamer --cam-source v4l2-mjpeg \
    --ac-host <GAME_MACHINE_IP>
```

### Option C: Local (Screen Capture)

For running everything on one machine — no camera or second machine needed. The bridge captures frames directly from the game window using screen capture.

```bash
# Install dependencies (adds mss for screen capture)
pip install opencv-python numpy pynput watchdog mss evdev

# One-command launch (starts companion + bridge with screen capture):
python local_bridge.py --ac-host 127.0.0.1

# Capture a specific window by title
python local_bridge.py --ac-host 127.0.0.1 --window-title "Assetto Corsa"

# Capture a specific screen region (x,y,w,h)
python local_bridge.py --ac-host 127.0.0.1 --screen-region 0,0,1920,1080

# Disable FFB for initial testing
python local_bridge.py --ac-host 127.0.0.1 --no-ffb

# Xbox controller mode (no Fanatec wheel needed)
python local_bridge.py --ac-host 127.0.0.1 --xbox
python local_bridge.py --ac-host 127.0.0.1 --xbox --steering-sensitivity 0.7
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
  --ac-host HOST            AC telemetry host (default: 127.0.0.1)
  --ac-port PORT            AC telemetry UDP port (default: 9996)
  --companion-port PORT     Internal UDP port for bridge<->companion (default: 5555)

Screen capture:
  --cam-fps FPS             Screen capture FPS (default: 20)
  --window-title TITLE      Game window title for targeted capture
  --screen-region x,y,w,h   Capture a specific screen region

Controller mode:
  --xbox                    Use virtual Xbox 360 controller instead of Fanatec wheel

Xbox options (only with --xbox):
  --steering-sensitivity F  Steering multiplier 0.0-1.0 (default: 1.0)
  --throttle-scale F        Throttle multiplier 0.0-1.0 (default: 1.0)

Fanatec options (default, without --xbox):
  --no-ffb                  Disable FFB wheel control (pedals only)
  --ffb-strength FLOAT      FFB strength multiplier 0.0-1.0 (default: 1.0)
  --p-gain FLOAT            FFB position tracking P gain (default: 5.0)
  --d-gain FLOAT            FFB position tracking D gain (default: 0.3)
```

---

## Usage

### Quick Start

**1. Start the companion on the game machine:**

```bash
python companion.py --listen-port 5555
```

**2. Start Assetto Corsa, load a session (practice, race, etc.)**

**3. Start the bridge on the openpilot machine:**

```bash
# OpenCV (any Linux)
python bridge.py --ac-host 192.168.1.100 --camera 0

# GStreamer (Jetson)
python bridge.py --ac-host 192.168.1.100 --cam-backend gstreamer --cam-source v4l2-mjpeg
```

**4. Engage openpilot:**

Press `C` on the bridge machine to engage openpilot and set a cruise speed. The Fanatec wheel will begin turning as openpilot steers.

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
  --ac-host HOST          IP of the game machine running Assetto Corsa

Network:
  --ac-port PORT          AC telemetry UDP port (default: 9996)
  --companion-host HOST   IP of the companion (default: same as --ac-host)
  --companion-port PORT   UDP port for control commands (default: 5555)

Camera backend:
  --cam-backend {opencv,gstreamer,screen}
                          opencv: portable (default)
                          gstreamer: NVIDIA HW-accelerated (Jetson)
                          screen: screen capture (local mode)

OpenCV options:
  --camera INDEX          Camera device index (default: 0)

GStreamer options:
  --cam-source TYPE       Source type (default: auto)
                          auto, v4l2, v4l2-mjpeg, v4l2-raw,
                          v4l2-h264, csi, rtsp, test
  --cam-device PATH       Device path or RTSP URL (default: /dev/video0)
  --cam-width WIDTH       Capture width (default: 1920)
  --cam-height HEIGHT     Capture height (default: 1080)

Screen capture options:
  --window-title TITLE    Game window title for targeted capture
  --screen-region x,y,w,h Capture a specific screen region

Common:
  --cam-fps FPS           Target framerate (default: 20)
```

### Companion CLI Reference

```
python companion.py [OPTIONS]

Network:
  --listen-port PORT      UDP port for receiving commands (default: 5555)

Controller mode:
  --xbox                  Use virtual Xbox 360 controller instead of Fanatec wheel

Xbox options (only with --xbox):
  --steering-sensitivity F  Steering multiplier 0.0-1.0 (default: 1.0)
  --throttle-scale F        Throttle multiplier 0.0-1.0 (default: 1.0)

Fanatec options (default mode):
  --no-ffb                Disable wheel FFB (pedals only)
  --ffb-strength FLOAT    FFB strength multiplier 0.0-1.0 (default: 1.0)
  --p-gain FLOAT          Position tracking proportional gain (default: 5.0)
  --d-gain FLOAT          Position tracking derivative gain (default: 0.3)
```

---

## Camera Backends

### OpenCV Backend

The default backend. Uses `cv2.VideoCapture` to read from a camera, performs crop and resize on the CPU, and converts BGR to RGB. Works on any Linux system with a USB camera.

```bash
python bridge.py --ac-host 192.168.1.100 --cam-backend opencv --camera 0
```

Frame processing pipeline (CPU):
```
v4l2 capture → BGR numpy array → aspect-ratio crop → resize to 1164x874 → BGR→RGB → cereal
```

### GStreamer/NVIDIA Backend (Jetson)

Hardware-accelerated backend for NVIDIA Jetson. Uses GStreamer with NVIDIA DeepStream plugins. All decode, resize, and color conversion happens on the GPU with zero CPU involvement.

```bash
python bridge.py --ac-host 192.168.1.100 --cam-backend gstreamer --cam-source v4l2-mjpeg
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

Test the GStreamer camera independently before running the full bridge:

```bash
# Auto-detect format and test capture
python camera_gst.py --source auto --device /dev/video0

# Test with MJPEG USB camera
python camera_gst.py --source v4l2-mjpeg --device /dev/video0 --width 1920 --height 1080 --fps 20

# Test with RTSP stream
python camera_gst.py --source rtsp --device rtsp://192.168.1.100:8554/game

# Test pattern (no camera needed)
python camera_gst.py --source test
```

Output will show frame shape and FPS:
```
Auto-detected source type: v4l2-mjpeg
GStreamer pipeline:
  v4l2src device=/dev/video0 ! image/jpeg,width=1920,height=1080,framerate=20/1 ! nvjpegdec ! nvvideoconvert ! video/x-raw,width=1164,height=874,format=RGB ! appsink name=sink
GStreamer camera started (target: 1164x874)
Frame 20: (874, 1164, 3) @ 19.8 FPS
Frame 40: (874, 1164, 3) @ 20.0 FPS
```

To check what formats your camera supports:

```bash
v4l2-ctl -d /dev/video0 --list-formats-ext
```

---

## Jetson Deployment

### Container Build

The Dockerfile is based on `nvcr.io/nvidia/deepstream-l4t:7.1-samples-multiarch`, which includes all NVIDIA GStreamer plugins, CUDA runtime, and TensorRT.

```bash
# Build from the openpilot root directory
docker build -f tools/ac_bridge/Dockerfile.l4t -t ac-bridge-l4t .

# Or with a different DeepStream base version
docker build -f tools/ac_bridge/Dockerfile.l4t \
    --build-arg DEEPSTREAM_TAG=6.3-samples \
    -t ac-bridge-l4t .
```

### Container Run

The `run_jetson.sh` script handles device passthrough, NVIDIA runtime, and networking:

```bash
# Basic (auto-detect camera format)
./run_jetson.sh --ac-host 192.168.1.100

# Specify camera format
./run_jetson.sh --ac-host 192.168.1.100 --cam-source v4l2-mjpeg --cam-device /dev/video0

# RTSP capture instead of physical camera
./run_jetson.sh --ac-host 192.168.1.100 \
    --cam-source rtsp --cam-device rtsp://192.168.1.100:8554/game

# Custom camera resolution
./run_jetson.sh --ac-host 192.168.1.100 --cam-width 1280 --cam-height 720 --cam-fps 30
```

The script passes these Docker flags automatically:

| Flag | Purpose |
|---|---|
| `--runtime nvidia` | NVIDIA GPU access |
| `--network host` | UDP telemetry + control traffic |
| `--ipc host` | cereal shared-memory IPC |
| `--device=/dev/video*` | All camera devices |
| `-v /tmp/argus_socket` | CSI camera (ARGUS) access |

Environment variables for `run_jetson.sh`:

| Variable | Default | Description |
|---|---|---|
| `CAMERA_DEVICE` | `/dev/video0` | Default camera device |
| `IMAGE_TAG` | `ac-bridge-l4t` | Docker image tag |
| `DEEPSTREAM_TAG` | `7.1-samples-multiarch` | DeepStream base image tag |

### JetPack Version Compatibility

| JetPack | L4T | DeepStream Tag | Notes |
|---|---|---|---|
| 6.x (Orin) | R36.x | `7.1-samples-multiarch` | Default, recommended |
| 5.1.x (Orin/Xavier) | R35.x | `6.3-samples` or `6.4-samples` | Set `DEEPSTREAM_TAG` |

### RTSP Capture (Alternative to Physical Camera)

Instead of pointing a physical camera at the game screen, you can stream the game screen over RTSP. This gives better quality (no moiré, glare, or optical distortion) and lower latency.

On the game machine, use OBS Studio with the RTSP output plugin:

1. Install OBS Studio and the [obs-rtspserver](https://github.com/iamscottxu/obs-rtspserver) plugin
2. Set up a scene capturing the game window
3. Start the RTSP server (e.g., on port 8554)

On the bridge machine:

```bash
python bridge.py --ac-host 192.168.1.100 \
    --cam-backend gstreamer \
    --cam-source rtsp \
    --cam-device rtsp://192.168.1.100:8554/game
```

---

## TensorRT Acceleration (modeld)

openpilot's neural network (`modeld`) runs the `supercombo.onnx` model for lane detection, path planning, and lead vehicle tracking. By default it uses ONNX Runtime with CUDA. Enabling TensorRT provides significant inference speedup by compiling the model into an optimized TensorRT engine.

### How It Works {#tensorrt-how-it-works}

The ONNX Runtime `TensorrtExecutionProvider` automatically:
1. Converts supported ONNX subgraphs into TensorRT engines
2. Enables FP16 precision for faster inference on NVIDIA GPUs
3. Caches compiled engines in `models/trt_cache/` so subsequent launches are instant

The first run takes several minutes while TensorRT compiles and optimizes the model. Subsequent runs load the cached engine in seconds.

### Local Setup (x86 with NVIDIA GPU)

```bash
# 1. Install TensorRT (via NVIDIA's apt repository)
# See: https://docs.nvidia.com/deeplearning/tensorrt/install-guide/

# 2. Install onnxruntime-gpu (includes TensorRT EP)
pip install onnxruntime-gpu

# 3. Verify TensorRT EP is available
python3 -c "import onnxruntime; print(onnxruntime.get_available_providers())"
# Should include 'TensorrtExecutionProvider'

# 4. Fetch the ONNX model (git LFS)
cd models && git lfs pull --include="supercombo.onnx"

# 5. Build modeld with ONNX support
USE_NVIDIA_GPU=1 scons -j$(nproc) selfdrive/modeld

# 6. Run the bridge - modeld will automatically use TensorRT
python bridge.py --ac-host 192.168.1.100 --camera 0
```

If TensorRT is not available, modeld falls back to CUDA, then to CPU — no configuration needed.

### Jetson Setup (L4T Container)

The L4T Dockerfile builds modeld with TensorRT support automatically. The DeepStream base image includes TensorRT libraries, and the container installs `onnxruntime-gpu` with TensorRT EP.

```bash
# Build the container (includes modeld + TensorRT)
docker build -f Dockerfile.l4t -t ac-bridge-l4t ../..

# First run: TensorRT compiles the model (~2-5 min on Orin)
# Subsequent runs: cached engine loads instantly
./run_jetson.sh --ac-host 192.168.1.100
```

The container sets `USE_NVIDIA_GPU=1` to build modeld with ONNX Runtime instead of Qualcomm SNPE.

### Verifying TensorRT Is Active

When modeld starts, the ONNX runner logs which execution provider it selected:

```
# TensorRT active (best performance):
OnnxJit is using TensorRT (cache: /openpilot/models/trt_cache)

# CUDA fallback (still GPU-accelerated):
OnnxJit is using CUDA

# CPU fallback (slowest):
OnnxJit is using CPU
```

Check modeld's stderr output. In the container: `docker logs <container_id> 2>&1 | grep OnnxJit`

---

## Steering Tuning

### tweak.cfg Parameters

The `tweak.cfg` file controls how openpilot's steering angle output maps to the wheel. The file is watched at runtime; edits take effect immediately without restarting the bridge.

```ini
[steer]
; Steering ratio - higher means less sensitive steering
STEER_RATIO = 15

; Speed (km/h) at which dynamic ratio adjustment starts
STEER_SPEED_OFFSET = 11

; Base maximum steering angle in degrees
BASE_MAX_STEER_ANGLE = 40

; Higher value = less ratio change per unit speed
STEER_SPEED_DENOM = 1.46
```

### Dynamic Steering Ratio

The effective maximum steering angle increases with speed to prevent over-steering at high speeds:

```
max_steer_angle = BASE_MAX_STEER_ANGLE + max(0, speed_kmh - STEER_SPEED_OFFSET) / STEER_SPEED_DENOM
```

The normalized steering output sent to the companion is:

```
steer_normalized = steer_angle_deg / (max_steer_angle * STEER_RATIO)
```

This value is clipped to `[-1, 1]` and maps to the full wheel rotation range.

### Tuning Procedure

1. Start with the defaults and engage openpilot on a straight road
2. If the car oscillates (weaving left-right): increase `STEER_RATIO` or increase `BASE_MAX_STEER_ANGLE`
3. If the car doesn't turn enough in corners: decrease `STEER_RATIO` or decrease `BASE_MAX_STEER_ANGLE`
4. If steering is good at low speed but too aggressive at high speed: increase `STEER_SPEED_DENOM`
5. Edit `tweak.cfg` while the bridge is running; changes are applied within 1 second

---

## Fanatec FFB Wheel Control

### How It Works

The companion implements a **position-tracking servo** using the Fanatec DD+'s force feedback:

1. **Read**: Reads the wheel's current position via evdev (`EV_ABS`, `ABS_X`)
2. **Compute**: PD controller calculates force: `F = P_GAIN * error + D_GAIN * d_error`
3. **Apply**: Uploads an `FF_CONSTANT` effect with the computed magnitude and direction
4. **Repeat**: Runs at 200Hz for smooth, responsive tracking

The game reads the wheel's physical position directly (it has no idea FFB is driving it), so steering feels natural to the game.

### PD Controller Tuning

| Parameter | Default | Effect |
|---|---|---|
| `--p-gain` | 5.0 | Higher = stiffer tracking, faster response, risk of oscillation |
| `--d-gain` | 0.3 | Higher = more damping, less overshoot, slower response |
| `--ffb-strength` | 1.0 | Scales the target angle (0.5 = half steering range) |

For initial testing, start with `--ffb-strength 0.3` and increase gradually.

### Supported Wheelbases

The companion auto-detects Fanatec wheels by USB vendor ID (`0x0EB7`) and these product IDs:

| Product ID | Wheelbase |
|---|---|
| `0x0020` | Clubsport DD+ / DD Pro / CSL DD |
| `0x0001` | ClubSport Wheel Base |
| `0x0006` | CSL Elite |
| `0x0E03` | CSL Elite PS4 |

Other Fanatec wheelbases may work if they expose `EV_FF` and `ABS_X` via evdev. Add the product ID to `FANATEC_DD_PLUS_PRODUCT_IDS` in `fanatec_ffb.py` if needed.

### Safety Features

- **Heartbeat timeout**: If no control packet is received for 5 seconds, the companion disengages and centers the wheel
- **Clean shutdown**: SIGINT/SIGTERM handlers stop FFB effects and zero the pedals
- **Force clamping**: All FFB forces are clamped to `[-MAX_FORCE, MAX_FORCE]` to prevent runaway torque
- **Target clamping**: Steering targets are clamped to `[-1, 1]` to prevent out-of-range commands

---

## Assetto Corsa Telemetry Protocol

### UDP Handshake

The bridge implements AC's three-step UDP handshake:

```
Bridge                              AC (port 9996)
  │                                    │
  ├── {id=1, ver=1, op=HANDSHAKE} ────►│
  │                                    │
  │◄── HandshakeResponse ─────────────┤
  │    (car name, driver name,         │
  │     track name, track config)      │
  │                                    │
  ├── {id=1, ver=1, op=SUBSCRIBE} ────►│
  │                                    │
  │◄── RTCarInfo (continuous) ────────┤
  │◄── RTCarInfo ─────────────────────┤
  │◄── RTCarInfo ─────────────────────┤
  │    ...                             │
```

### RTCarInfo Fields

The bridge parses these fields from the RTCarInfo UDP packets (Windows MSVC struct packing, booleans as 4-byte ints):

| Offset | Type | Field | Used By |
|---|---|---|---|
| 0 | float | `speed_kmh` | Speed display |
| 8 | float | `speed_ms` | CAN messages, steering ratio |
| 36 | float | `acc_g_vertical` | IMU simulation |
| 40 | float | `acc_g_horizontal` | IMU simulation |
| 44 | float | `acc_g_frontal` | IMU simulation |
| 64 | float | `gas` | Diagnostics |
| 68 | float | `brake` | Diagnostics |
| 76 | float | `engine_rpm` | Diagnostics |
| 80 | float | `steer` | Diagnostics |
| 84 | int | `gear` | Diagnostics |

### Testing Telemetry

Test the telemetry connection independently:

```bash
python ac_telemetry.py --host <GAME_MACHINE_IP> --port 9996
```

Expected output (with AC running and in a session):
```
AC telemetry connected: car=ks_ferrari_488_gt3 track=imola
speed=142.3km/h steer=-0.023 gas=0.78 brake=0.00 rpm=7234 gear=4
speed=145.1km/h steer=-0.019 gas=0.82 brake=0.00 rpm=7412 gear=4
```

---

## Internal Architecture

### Data Flow Detail

The bridge simulates an openpilot-compatible vehicle by providing all the inputs openpilot expects:

```
┌─ Camera ──────────────────────────────────────────────────────────────────┐
│ OpenCV or GStreamer capture → crop/resize to 1164x874 RGB                │
│ → cereal PubMaster('roadCameraState') → modeld                          │
└───────────────────────────────────────────────────────────────────────────┘

┌─ AC Telemetry ────────────────────────────────────────────────────────────┐
│ UDP from AC (speed, g-forces) → fake CAN messages (Honda Civic format)   │
│ → cereal PubMaster('can') → controlsd                                    │
│                                                                           │
│ G-forces → cereal PubMaster('sensorEvents') → locationd                  │
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
| AC telemetry | ~async | Poll AC UDP for vehicle state updates |
| Config watcher | event | Watch `tweak.cfg` for changes (via watchdog) |

The companion runs:

| Thread | Rate | Purpose |
|---|---|---|
| Main loop | ~async | Receive UDP, update targets |
| FFB control loop | 200 Hz | Read wheel position, compute PD force, upload effect |

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

**"OnnxJit is using CPU" (expected TensorRT or CUDA)**
- Verify `onnxruntime-gpu` is installed (not just `onnxruntime`): `pip install onnxruntime-gpu`
- Check available providers: `python3 -c "import onnxruntime; print(onnxruntime.get_available_providers())"`
- For TensorRT: ensure TensorRT is installed and the version matches onnxruntime-gpu's requirements
- On Jetson: use the NVIDIA-provided onnxruntime-gpu wheel for your JetPack version
- Set `ONNXCPU=1` to force CPU mode (for debugging)

**TensorRT first run is very slow**
- This is expected. TensorRT compiles and optimizes the model on first run (~2-5 min on Orin, longer on older hardware)
- The compiled engine is cached in `models/trt_cache/` — subsequent runs load instantly
- Do not interrupt the first run or the cache will be incomplete

**modeld build fails with "SNPE not found" on Jetson**
- Set `USE_NVIDIA_GPU=1` before building: `USE_NVIDIA_GPU=1 scons -j$(nproc) selfdrive/modeld`
- This skips Qualcomm SNPE/Thneed and uses ONNX Runtime instead
- The L4T Dockerfile sets this automatically

**"AC telemetry handshake timeout"**
- Verify AC is running and a session is loaded (not in menus)
- Check network connectivity: `ping <GAME_MACHINE_IP>`
- Ensure UDP port 9996 is not blocked by a firewall
- Try testing telemetry independently: `python ac_telemetry.py --host <IP>`

**"No Fanatec wheel found"** (Fanatec mode)
- Verify the hid-fanatecff module is loaded: `lsmod | grep fanatec`
- Check the wheel is in PC mode (red LED)
- Verify evdev sees it: `evtest` (look for Fanatec in the list)
- Check USB connection: `lsusb | grep 0eb7`
- If you don't have a Fanatec wheel, use `--xbox` mode instead

**Virtual Xbox controller not detected by AC** (Xbox mode)
- Verify uinput is loaded: `ls /dev/uinput`
- Run companion as root if permission denied: `sudo python companion.py --xbox`
- Check that AC input method is set to "Gamepad" in settings
- Verify the device was created: `ls /dev/input/event*` (should show a new device after starting companion)

**Wheel oscillates / vibrates when engaged**
- Reduce `--ffb-strength` (try 0.3 first)
- Reduce `--p-gain` (try 3.0)
- Increase `--d-gain` (try 0.5)

**Car weaves left-right in game**
- Increase `STEER_RATIO` in `tweak.cfg`
- Increase `BASE_MAX_STEER_ANGLE` in `tweak.cfg`
- These can be edited live while the bridge is running

**Car doesn't turn enough in corners**
- Decrease `STEER_RATIO` in `tweak.cfg`
- Decrease `BASE_MAX_STEER_ANGLE` in `tweak.cfg`

**GStreamer pipeline fails to start**
- Check camera format support: `v4l2-ctl -d /dev/video0 --list-formats-ext`
- Try a different `--cam-source` (e.g., `v4l2` instead of `v4l2-mjpeg`)
- On Jetson, verify NVIDIA plugins are available: `gst-inspect-1.0 nvvideoconvert`
- Check camera device permissions: `ls -la /dev/video0`

**"Bridge connection lost, disengaging" (companion)**
- Network issue between bridge and game machines
- Bridge may have crashed; check bridge terminal for errors
- Increase companion `HEARTBEAT_TIMEOUT` if network is flaky

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
- Use `--window-title "Assetto Corsa"` to target the game window specifically
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
- On Jetson: ensure NVIDIA runtime is active (`--runtime nvidia`), check `--cam-source` matches actual camera format
- Reduce `--cam-fps` if the camera can't sustain the requested rate
- For RTSP: reduce OBS output resolution or increase bitrate
