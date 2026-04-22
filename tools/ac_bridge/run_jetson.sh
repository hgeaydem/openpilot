#!/bin/bash
# Launch the AC bridge container on NVIDIA Jetson.
#
# Usage:
#   ./run_jetson.sh --ac-host 192.168.1.100
#   ./run_jetson.sh --ac-host 192.168.1.100 --cam-source v4l2-mjpeg --cam-device /dev/video0
#   ./run_jetson.sh --ac-host 192.168.1.100 --cam-source rtsp --cam-device rtsp://192.168.1.100:8554/game
#
# Environment variables:
#   CAMERA_DEVICE  - camera device path (default: /dev/video0)
#   IMAGE_TAG      - Docker image tag (default: ac-bridge-l4t)
#   DEEPSTREAM_TAG - DeepStream base image tag (default: 7.1-samples-multiarch)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OPENPILOT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

CAMERA_DEVICE="${CAMERA_DEVICE:-/dev/video0}"
IMAGE_TAG="${IMAGE_TAG:-ac-bridge-l4t}"
DEEPSTREAM_TAG="${DEEPSTREAM_TAG:-7.1-samples-multiarch}"

# Build if image doesn't exist
if ! docker image inspect "$IMAGE_TAG" &>/dev/null; then
    echo "Building L4T container (base: deepstream-l4t:${DEEPSTREAM_TAG})..."
    docker build \
        -f "$SCRIPT_DIR/Dockerfile.l4t" \
        --build-arg "DEEPSTREAM_TAG=$DEEPSTREAM_TAG" \
        -t "$IMAGE_TAG" \
        "$OPENPILOT_ROOT"
fi

# Detect available camera devices
CAMERA_MOUNTS=""
for dev in /dev/video*; do
    if [ -e "$dev" ]; then
        CAMERA_MOUNTS="$CAMERA_MOUNTS --device=$dev:$dev:rwm"
    fi
done

echo "Starting AC bridge on Jetson..."
echo "  Camera device: $CAMERA_DEVICE"
echo "  Bridge args: $*"
echo ""

exec docker run -it --rm \
    --runtime nvidia \
    --network host \
    --ipc host \
    $CAMERA_MOUNTS \
    -v /tmp/argus_socket:/tmp/argus_socket \
    -v /tmp/.X11-unix:/tmp/.X11-unix:rw \
    -e DISPLAY="$DISPLAY" \
    -e SIMULATION=1 \
    -e NOSENSOR=1 \
    -v "$SCRIPT_DIR/tweak.cfg:/openpilot/tools/ac_bridge/tweak.cfg" \
    "$IMAGE_TAG" \
    --cam-backend gstreamer \
    "$@"
