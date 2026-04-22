# BeamNG.drive <-> openpilot Bridge

A bridge that connects [openpilot](https://github.com/commaai/openpilot) to [BeamNG.drive](https://www.beamng.com/), enabling openpilot's self-driving stack to control a vehicle in-game.

Supports two controller modes:
- **Fanatec DD+ mode** (default): Force feedback physically turns a real Fanatec Clubsport DD+ wheelbase to openpilot's target steering angle. BeamNG reads the wheel position directly. Throttle and brake go through virtual pedals via uinput.
- **Xbox mode** (`--xbox`): Emits steering, throttle, and brake on a virtual Xbox 360 controller via uinput. No physical wheel needed.

Supports three deployment modes:
- **Two-machine**: A camera on the openpilot machine captures the game screen from a separate game machine. The companion runs on the game machine.
- **Local (single-machine)**: Screen capture grabs frames directly from the game window — no camera or second machine needed. Uses GPU-accelerated NVIDIA NvFBC capture when available, with GStreamer and CPU fallbacks.
- **Jetson**: Hardware-accelerated GStreamer pipeline on NVIDIA Jetson with TensorRT inference.

BeamNG.drive provides telemetry via the OutGauge protocol — a standard UDP format compatible with Live for Speed and SimHub. The bridge receives OutGauge packets directly from BeamNG, no relay script needed.

---

## Table of Contents

- [Architecture](#architecture)
- [Hardware Requirements](#hardware-requirements)
- [Network Setup](#network-setup)
- [File Reference](#file-reference)
- [Setup: Game Machine (Companion)](#setup-game-machine-companion)
  - [Prerequisites](#companion-prerequisites)
  - [BeamNG OutGauge Configuration](#beamng-outgauge-configuration)
  - [Fanatec DD+ Linux Driver](#fanatec-dd-linux-driver)
  - [Running the Companion](#running-the-companion)
  - [BeamNG Controller Settings](#beamng-controller-settings)
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
  - [How It Works](#ffb-how-it-works)
  - [PD Controller Tuning](#pd-controller-tuning)
  - [Supported Wheelbases](#supported-wheelbases)
  - [Safety Features](#safety-features)
- [BeamNG OutGauge Telemetry](#beamng-outgauge-telemetry)
  - [How It Works](#outgauge-how-it-works)
  - [OutGauge Fields](#outgauge-fields)
  - [Limitations](#outgauge-limitations)
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
  (Linux / Jetson)                            (Linux, running BeamNG via Proton)
 ┌──────────────────────────┐                ┌────────────────────────────┐
 │                          │                │                            │
 │  Camera                  │                │  BeamNG.drive              │
 │   │ (USB / CSI / HDMI    │                │   │                        │
 │   │  capture card)       │                │   │ reads wheel pos /      │
 │   ▼                      │                │   │ reads virtual Xbox pad │
 │  bridge.py               │                │   │                        │
 │   ├─ frames ──► modeld   │                │   ▲                        │
 │   │             │        │   UDP:4444     │   │                        │
 │   ├─ telemetry ◄─────────────────────────────── OutGauge (built-in)   │
 │   │             ▼        │                │   │                        │
 │   ├─ fake CAN  plannerd  │                │  companion.py             │
 │   ├─ fake panda │        │                │   ├─ FFB ──► Fanatec DD+  │
 │   ├─ fake DM    ▼        │                │   │  or Xbox controller    │
 │   │         controlsd    │                │   │                        │
 │   │             │        │   UDP:5555     │   │                        │
 │   └─ steer/gas/brake ────────────────────────►└─ steering + pedals    │
 │      (normalized)        │                │                            │
 │                          │                │                            │
 └──────────────────────────┘                └────────────────────────────┘
```

**Key difference from the ACC bridge**: BeamNG has built-in OutGauge telemetry support — no relay script needed. OutGauge is a standard 96-byte UDP protocol that BeamNG sends directly to the bridge machine. However, OutGauge does not include steering angle or lateral/vertical acceleration, so the bridge derives longitudinal acceleration from speed changes (dv/dt).

---

## Hardware Requirements

### Bridge Machine (openpilot host)
- Linux (x86_64 or aarch64)
- Camera: USB webcam, USB HDMI capture card (e.g., Elgato Cam Link), or Jetson CSI camera
- For Jetson deployment: NVIDIA Jetson Orin / Xavier / Nano with JetPack 5.x or 6.x
- Network connection to the game machine

### Game Machine
- Linux (BeamNG runs via Proton/Steam)
- **Fanatec mode** (default): Fanatec Clubsport DD+ wheelbase (or compatible, see [Supported Wheelbases](#supported-wheelbases)) + [hid-fanatecff](https://github.com/gotzl/hid-fanatecff) kernel module
- **Xbox mode** (`--xbox`): No physical wheel needed — uses a virtual Xbox 360 controller via uinput
- BeamNG.drive with OutGauge enabled
- Network connection to the bridge machine (or localhost for local mode)

---

## Network Setup

The bridge and game machines communicate over two UDP channels:

| Channel | Direction | Port | Purpose |
|---|---|---|---|
| BeamNG OutGauge | Game &rarr; Bridge | 4444 | Vehicle state: speed, RPM, gear, throttle, brake |
| Control Commands | Bridge &rarr; Game | 5555 | Steering, throttle, brake, engaged flag |

Both machines must be on the same network. If a firewall is running, open UDP port 4444 inbound on the bridge machine and UDP port 5555 inbound on the game machine.

---

## File Reference

```
tools/beamng_bridge/
├── bridge.py              # Main bridge (runs on openpilot machine)
├── companion.py           # Companion (runs on game machine)
├── beamng_telemetry.py    # OutGauge UDP telemetry receiver
├── fanatec_ffb.py         # Fanatec FFB controller + virtual pedals
├── xbox_controller.py     # Virtual Xbox 360 controller via uinput
├── camera_gst.py          # GStreamer/NVIDIA HW-accelerated camera
├── lib/
│   ├── __init__.py
│   └── can.py             # Fake Honda CAN messages for openpilot
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
| `bridge.py` | Bridge | Captures camera frames, publishes to openpilot via cereal, receives BeamNG telemetry, reads openpilot's control output, sends commands to companion. Supports OpenCV, GStreamer, and screen capture backends. |
| `companion.py` | Game | Receives control commands over UDP. Supports two modes: Fanatec (FFB wheel + virtual pedals) and Xbox (virtual Xbox 360 controller). Auto-disengages on connection loss. |
| `beamng_telemetry.py` | Bridge | Receives and parses OutGauge UDP packets (96 bytes). Derives longitudinal acceleration from speed changes since OutGauge doesn't include acceleration data. Thread-safe with async polling mode. |
| `fanatec_ffb.py` | Game | Finds Fanatec wheel via evdev, implements PD position servo using FF_CONSTANT effects at 200Hz. Creates virtual pedal device via uinput. |
| `xbox_controller.py` | Game | Creates a virtual Xbox 360 controller using Linux uinput with Microsoft vendor/product IDs. Alternative to Fanatec wheel for steering via gamepad input. |
| `camera_gst.py` | Bridge (Jetson) | GStreamer camera capture with NVIDIA hardware acceleration. Supports v4l2 (MJPEG/raw/H.264), CSI, RTSP, and test sources. |
| `lib/can.py` | Bridge | Generates fake Honda Civic CAN messages for openpilot's car interface. |
| `screen_capture.py` | Bridge | Screen capture with three-tier backend hierarchy: NVIDIA NvFBC (GPU-accelerated), GStreamer ximagesrc (GPU resize), mss+OpenCV (CPU fallback). |
| `local_bridge.py` | Bridge | Convenience launcher for single-machine mode. Starts companion as subprocess and bridge with screen capture backend. |
| `tweak.cfg` | Bridge | Steering ratio parameters. Watched for live changes (no restart needed). |

---

## Setup: Game Machine (Companion)

### Companion Prerequisites

```bash
# Python 3.8+ required
pip install evdev numpy

# Verify uinput module is loaded (for virtual pedals / Xbox controller)
sudo modprobe uinput
ls /dev/uinput  # should exist

# Your user needs write access to /dev/uinput
# Either run companion as root, or add a udev rule:
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules
sudo usermod -aG input $USER
# Log out and back in for group change to take effect
```

### BeamNG OutGauge Configuration

BeamNG has built-in OutGauge support that sends vehicle telemetry over UDP. To enable it:

1. Open BeamNG.drive
2. Go to **Options &rarr; Other &rarr; OutGauge**
3. Enable OutGauge
4. Set **IP** to the bridge machine's IP address (or `127.0.0.1` for local mode)
5. Set **Port** to `4444` (matches `--telemetry-port` default on the bridge)

No relay script is needed — BeamNG sends OutGauge packets directly.

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

### BeamNG Controller Settings

#### Fanatec Mode

1. In BeamNG.drive, go to **Options &rarr; Controls &rarr; Bindings**
2. Assign the Fanatec wheel for the steering axis
3. Assign "AC Bridge Virtual Pedals" for throttle (ABS_Z) and brake (ABS_RZ)
4. Set steering linearity and deadzone to minimum

#### Xbox Mode

1. In BeamNG.drive, go to **Options &rarr; Controls**
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
docker build -f Dockerfile.l4t -t beamng-bridge-l4t ../..
docker run -it --rm --runtime nvidia --network host --ipc host \
    --device=/dev/video0 \
    beamng-bridge-l4t \
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
python local_bridge.py --window-title "BeamNG.drive"

# Capture a specific screen region (x,y,w,h)
python local_bridge.py --screen-region 0,0,1920,1080

# Disable FFB for initial testing
python local_bridge.py --no-ffb

# Xbox controller mode (no Fanatec wheel needed)
python local_bridge.py --xbox
python local_bridge.py --xbox --steering-sensitivity 0.7
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
  --companion-port PORT     Internal UDP port for bridge<->companion (default: 5555)
  --telemetry-port PORT     UDP port for BeamNG OutGauge telemetry (default: 4444)

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

**1. Enable OutGauge in BeamNG** (see [BeamNG OutGauge Configuration](#beamng-outgauge-configuration))

**2. Start the companion on the game machine:**

```bash
# Fanatec mode (default)
python companion.py --listen-port 5555

# Or Xbox mode
python companion.py --listen-port 5555 --xbox
```

**3. Start BeamNG.drive, load a map and spawn a vehicle**

**4. Start the bridge on the openpilot machine:**

```bash
# OpenCV (any Linux)
python bridge.py --game-host 192.168.1.100 --camera 0

# GStreamer (Jetson)
python bridge.py --game-host 192.168.1.100 --cam-backend gstreamer --cam-source v4l2-mjpeg
```

**5. Engage openpilot:**

Press `C` on the bridge machine to engage openpilot and set a cruise speed.

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
  --game-host HOST          IP of the game machine running BeamNG.drive

Network:
  --companion-port PORT     UDP port for control commands (default: 5555)
  --telemetry-port PORT     UDP port to receive BeamNG OutGauge (default: 4444)

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

Controller mode:
  --xbox                    Use virtual Xbox 360 controller instead of Fanatec wheel

Xbox options (only with --xbox):
  --steering-sensitivity F  Steering multiplier 0.0-1.0 (default: 1.0)
  --throttle-scale F        Throttle multiplier 0.0-1.0 (default: 1.0)

Fanatec options (default mode):
  --no-ffb                  Disable wheel FFB (pedals only)
  --ffb-strength FLOAT      FFB strength multiplier 0.0-1.0 (default: 1.0)
  --p-gain FLOAT            Position tracking proportional gain (default: 5.0)
  --d-gain FLOAT            Position tracking derivative gain (default: 0.3)
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
```

**Tier 2: GStreamer ximagesrc (GPU resize)**

Falls back to GStreamer's `ximagesrc` for X11 capture, with `nvvideoconvert` for GPU-accelerated resize. Capture is CPU-based but the expensive resize/color-conversion step runs on the GPU.

**Tier 3: mss + OpenCV (CPU fallback)**

Pure CPU fallback using the `mss` library for screen capture and OpenCV for resize/color conversion. Works on any system with a display but uses more CPU than the GPU-accelerated options.

**Backend selection** is automatic. The bridge logs which backend was selected at startup:

```
NvFBC capture initialized (1164x874)           # Tier 1
GStreamer ximagesrc capture initialized         # Tier 2
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
docker build -f tools/beamng_bridge/Dockerfile.l4t -t beamng-bridge-l4t .

# Or with a different DeepStream base version
docker build -f tools/beamng_bridge/Dockerfile.l4t \
    --build-arg DEEPSTREAM_TAG=6.3-samples \
    -t beamng-bridge-l4t .
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
| `IMAGE_TAG` | `beamng-bridge-l4t` | Docker image tag |
| `DEEPSTREAM_TAG` | `7.1-samples-multiarch` | DeepStream base image tag |

### JetPack Version Compatibility

| JetPack | L4T | DeepStream Tag | Notes |
|---|---|---|---|
| 6.x (Orin) | R36.x | `7.1-samples-multiarch` | Default, recommended |
| 5.1.x (Orin/Xavier) | R35.x | `6.3-samples` or `6.4-samples` | Set `DEEPSTREAM_TAG` |

### RTSP Capture (Alternative to Physical Camera)

Instead of pointing a physical camera at the game screen, you can stream the game screen over RTSP for better quality and lower latency.

On the game machine, use OBS Studio with the [obs-rtspserver](https://github.com/iamscottxu/obs-rtspserver) plugin:

1. Install OBS Studio and the RTSP output plugin
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
python bridge.py --game-host 192.168.1.100 --camera 0
```

If TensorRT is not available, modeld falls back to CUDA, then to CPU — no configuration needed.

### Jetson Setup (L4T Container)

The L4T Dockerfile builds modeld with TensorRT support automatically. The DeepStream base image includes TensorRT libraries, and the container installs `onnxruntime-gpu` with TensorRT EP.

```bash
# Build the container (includes modeld + TensorRT)
docker build -f Dockerfile.l4t -t beamng-bridge-l4t ../..

# First run: TensorRT compiles the model (~2-5 min on Orin)
# Subsequent runs: cached engine loads instantly
./run_jetson.sh --game-host 192.168.1.100
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

The `tweak.cfg` file controls how openpilot's steering angle output maps to the controller. The file is watched at runtime; edits take effect immediately without restarting the bridge.

```ini
[steer]
; Steering ratio - higher means less sensitive steering
STEER_RATIO = 14

; Speed (km/h) at which dynamic ratio adjustment starts
STEER_SPEED_OFFSET = 12

; Base maximum steering angle in degrees
BASE_MAX_STEER_ANGLE = 38

; Higher value = less ratio change per unit speed
STEER_SPEED_DENOM = 1.5
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

This value is clipped to `[-1, 1]` and maps to the full wheel rotation range (Fanatec mode) or the full stick range (Xbox mode).

### Tuning Procedure

1. Start with the defaults and engage openpilot on a straight road
2. If the car oscillates (weaving left-right): increase `STEER_RATIO` or increase `BASE_MAX_STEER_ANGLE`
3. If the car doesn't turn enough in corners: decrease `STEER_RATIO` or decrease `BASE_MAX_STEER_ANGLE`
4. If steering is good at low speed but too aggressive at high speed: increase `STEER_SPEED_DENOM`
5. Edit `tweak.cfg` while the bridge is running; changes are applied within 1 second

BeamNG.drive has soft-body physics with realistic tire models. Steering response varies significantly between vehicles — a small city car needs very different tuning than a heavy truck. Start with the defaults on a mid-size sedan.

---

## Fanatec FFB Wheel Control

### How It Works {#ffb-how-it-works}

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

## BeamNG OutGauge Telemetry

### How It Works {#outgauge-how-it-works}

BeamNG sends vehicle telemetry using the OutGauge protocol — a standard 96-byte UDP packet format originally from Live for Speed, also supported by SimHub and other tools. BeamNG sends these packets directly to the configured IP and port (no relay needed).

### OutGauge Fields

The OutGauge packet (little-endian, 96 bytes) contains:

| Offset | Type | Field | Used By Bridge |
|---|---|---|---|
| 0 | uint32 | time (ms) | - |
| 4 | char[4] | car name | - |
| 8 | uint16 | flags | - |
| 10 | uint8 | gear | CAN messages |
| 11 | uint8 | playerid | - |
| 12 | float | speed (m/s) | CAN messages, steering ratio |
| 16 | float | rpm | Diagnostics |
| 20 | float | turbo (bar) | - |
| 24 | float | engine temp (C) | - |
| 28 | float | fuel (0-1) | - |
| 32 | float | oil pressure (bar) | - |
| 36 | float | oil temp (C) | - |
| 40 | uint32 | dash lights | - |
| 44 | uint32 | show lights | - |
| 48 | float | throttle (0-1) | Diagnostics |
| 52 | float | brake (0-1) | Diagnostics |
| 56 | float | clutch (0-1) | - |
| 60 | char[16] | display1 | - |
| 76 | char[16] | display2 | - |
| 92 | int32 | id | - |

### Limitations {#outgauge-limitations}

OutGauge does **not** include:
- **Steering angle**: Reported as 0. openpilot uses its own steering model based on camera input, so this is not a problem.
- **Lateral acceleration**: Not available. Set to 0.
- **Vertical acceleration**: Not available. Set to 0.
- **Longitudinal acceleration**: The bridge derives this from speed changes: `acc_g = (speed - prev_speed) / dt / 9.81`.

This means the IMU simulation for openpilot only has longitudinal g-force data. In practice, openpilot's perception model (modeld) handles lateral dynamics through vision, so the lack of lateral acceleration data does not significantly impact steering performance.

### Testing Telemetry

Test the telemetry receiver independently:

```bash
python beamng_telemetry.py --port 4444
```

Start BeamNG, load a map, spawn a vehicle, and with OutGauge enabled you should see:

```
Listening for BeamNG telemetry on UDP port 4444...
BeamNG telemetry connected (from 192.168.1.100)
speed=65.3km/h steer=0.000 gas=0.78 brake=0.00 rpm=4500 gear=3
```

---

## Internal Architecture

### Data Flow Detail

```
┌─ Camera ──────────────────────────────────────────────────────────────────┐
│ OpenCV or GStreamer capture → crop/resize to 1164x874 RGB                │
│ → cereal PubMaster('roadCameraState') → modeld                          │
└───────────────────────────────────────────────────────────────────────────┘

┌─ BeamNG Telemetry ────────────────────────────────────────────────────────┐
│ OutGauge UDP from BeamNG (speed, RPM, gear)                              │
│ → fake CAN messages (Honda Civic format)                                 │
│ → cereal PubMaster('can') → controlsd                                    │
│                                                                           │
│ Speed-derived acceleration → cereal PubMaster('sensorEvents') → locationd│
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
| BeamNG telemetry | ~async | Receive OutGauge UDP packets |
| Config watcher | event | Watch `tweak.cfg` for changes (via watchdog) |

The companion runs:

| Thread | Rate | Purpose |
|---|---|---|
| Main loop | ~async | Receive UDP, update targets |
| FFB control loop | 200 Hz | Read wheel position, compute PD force, upload effect (Fanatec mode only) |

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

**"Listening for BeamNG telemetry..." but no data received**
- Verify OutGauge is enabled in BeamNG: Options &rarr; Other &rarr; OutGauge
- Check that the OutGauge IP is set to the bridge machine's IP
- Check that the OutGauge port matches (default: 4444)
- Verify you are in a vehicle (OutGauge only sends data when driving)
- Check network connectivity: `ping <BRIDGE_MACHINE_IP>` from the game machine
- Ensure UDP port 4444 is not blocked by a firewall on the bridge machine

**"No Fanatec wheel found"** (Fanatec mode)
- Verify the hid-fanatecff module is loaded: `lsmod | grep fanatec`
- Check the wheel is in PC mode (red LED)
- Verify evdev sees it: `evtest` (look for Fanatec in the list)
- Check USB connection: `lsusb | grep 0eb7`
- If you don't have a Fanatec wheel, use `--xbox` mode instead

**Virtual Xbox controller not detected by BeamNG** (Xbox mode)
- Verify uinput is loaded: `ls /dev/uinput`
- Run companion as root if permission denied: `sudo python companion.py --xbox`
- Verify the device was created: `ls /dev/input/event*` (should show a new device after starting companion)
- In BeamNG, check Options &rarr; Controls for the gamepad

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

**Steering feels different between vehicles in BeamNG**
- BeamNG has soft-body physics; each vehicle has unique steering characteristics
- Adjust `tweak.cfg` per vehicle type (sedans vs trucks vs sports cars)
- The hot-reload feature makes this easy: edit while driving

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
- Use `--window-title "BeamNG.drive"` to target the game window specifically
- Use `--screen-region x,y,w,h` to manually specify the capture region
- Verify the game is not minimized (screen capture cannot capture minimized windows)

**Screen capture: low FPS**
- NvFBC is fastest; if it's not available, check the troubleshooting entry above
- Reduce `--cam-fps` if the system can't sustain the requested rate
- Close other GPU-intensive applications competing for resources

**Low FPS / high latency**
- On x86: switch to GStreamer backend if possible, or reduce camera resolution
- On Jetson: ensure NVIDIA runtime is active (`--runtime nvidia`)
- Reduce `--cam-fps` if the camera can't sustain the requested rate
