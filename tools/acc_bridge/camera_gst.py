#!/usr/bin/env python3
"""
Hardware-accelerated camera capture for NVIDIA Jetson using GStreamer + DeepStream plugins.

Supports multiple input sources:
  - USB camera (MJPEG or raw YUYV/UYVY)
  - HDMI capture card (USB UVC, e.g. Elgato)
  - CSI camera (via nvarguscamerasrc)
  - RTSP network stream (hardware H.264/H.265 decode)
  - Test pattern (for development without a camera)

All sources use NVIDIA hardware acceleration:
  - nvjpegdec / nvv4l2decoder for decode
  - nvvideoconvert for color space conversion and scaling
  - Zero-copy GPU memory (NVMM) where possible

Frames are delivered as RGB numpy arrays at the target resolution (1164x874)
via a callback, ready for publishing to openpilot's cereal messaging.
"""
import sys
import time
import threading
import numpy as np

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

Gst.init(None)

W_TARGET = 1164
H_TARGET = 874


class GstCamera:
    """Hardware-accelerated camera capture via GStreamer NVIDIA plugins."""

    def __init__(self, source='v4l2', device='/dev/video0',
                 width=1920, height=1080, fps=20,
                 target_w=W_TARGET, target_h=H_TARGET,
                 callback=None):
        self.target_w = target_w
        self.target_h = target_h
        self.callback = callback
        self.frame_count = 0
        self._running = False

        pipeline_str = self._build_pipeline(source, device, width, height, fps)
        print(f"GStreamer pipeline:\n  {pipeline_str}")

        self.pipeline = Gst.parse_launch(pipeline_str)

        self.appsink = self.pipeline.get_by_name('sink')
        self.appsink.set_property('emit-signals', True)
        self.appsink.set_property('max-buffers', 1)
        self.appsink.set_property('drop', True)
        self.appsink.set_property('sync', False)
        self.appsink.connect('new-sample', self._on_new_sample)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self._on_error)
        bus.connect('message::warning', self._on_warning)
        bus.connect('message::eos', self._on_eos)

    def _build_pipeline(self, source, device, w, h, fps):
        """Build GStreamer pipeline string based on source type."""

        target_caps = (f'video/x-raw,width={self.target_w},'
                       f'height={self.target_h},format=RGB')

        if source == 'v4l2-mjpeg':
            # USB camera outputting MJPEG (most common for capture cards)
            return (
                f'v4l2src device={device} ! '
                f'image/jpeg,width={w},height={h},framerate={fps}/1 ! '
                f'nvjpegdec ! '
                f'nvvideoconvert ! '
                f'{target_caps} ! '
                f'appsink name=sink'
            )

        elif source == 'v4l2-raw':
            # USB camera outputting raw YUY2/UYVY
            return (
                f'v4l2src device={device} ! '
                f'video/x-raw,width={w},height={h},framerate={fps}/1 ! '
                f'nvvideoconvert ! '
                f'{target_caps} ! '
                f'appsink name=sink'
            )

        elif source == 'v4l2':
            # Auto-negotiate: try MJPEG first, fall back to raw
            return (
                f'v4l2src device={device} ! '
                f'videoconvert ! '
                f'nvvideoconvert ! '
                f'{target_caps} ! '
                f'appsink name=sink'
            )

        elif source == 'v4l2-h264':
            # USB camera or capture card outputting H.264
            return (
                f'v4l2src device={device} ! '
                f'video/x-h264,width={w},height={h},framerate={fps}/1 ! '
                f'h264parse ! '
                f'nvv4l2decoder ! '
                f'nvvideoconvert ! '
                f'{target_caps} ! '
                f'appsink name=sink'
            )

        elif source == 'csi':
            # Jetson CSI camera (via ARGUS)
            return (
                f'nvarguscamerasrc sensor-id=0 ! '
                f'video/x-raw(memory:NVMM),width={w},height={h},'
                f'format=NV12,framerate={fps}/1 ! '
                f'nvvideoconvert ! '
                f'{target_caps} ! '
                f'appsink name=sink'
            )

        elif source == 'rtsp':
            # RTSP network stream (e.g., from game machine running OBS RTSP output)
            return (
                f'rtspsrc location={device} latency=100 ! '
                f'rtph264depay ! '
                f'h264parse ! '
                f'nvv4l2decoder ! '
                f'nvvideoconvert ! '
                f'{target_caps} ! '
                f'appsink name=sink'
            )

        elif source == 'test':
            # Test pattern for development
            return (
                f'videotestsrc pattern=0 ! '
                f'video/x-raw,width={w},height={h},framerate={fps}/1 ! '
                f'nvvideoconvert ! '
                f'{target_caps} ! '
                f'appsink name=sink'
            )

        else:
            raise ValueError(f"Unknown source type: {source}. "
                           f"Use: v4l2, v4l2-mjpeg, v4l2-raw, v4l2-h264, csi, rtsp, test")

    def _on_new_sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        caps = sample.get_caps()

        success, mapinfo = buf.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK

        struct = caps.get_structure(0)
        width = struct.get_int('width')[1]
        height = struct.get_int('height')[1]

        expected_size = width * height * 3  # RGB
        if mapinfo.size >= expected_size:
            frame = np.ndarray(
                (height, width, 3),
                dtype=np.uint8,
                buffer=mapinfo.data[:expected_size]
            )
            if self.callback:
                self.callback(frame.copy())
            self.frame_count += 1

        buf.unmap(mapinfo)
        return Gst.FlowReturn.OK

    def _on_error(self, bus, msg):
        err, debug = msg.parse_error()
        print(f"GStreamer ERROR: {err.message}")
        print(f"  Debug: {debug}")

    def _on_warning(self, bus, msg):
        err, debug = msg.parse_warning()
        print(f"GStreamer WARNING: {err.message}")

    def _on_eos(self, bus, msg):
        print("GStreamer: End of stream")
        self._running = False

    def start(self):
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Failed to start GStreamer pipeline. "
                             "Check camera connection and format support.")
        self._running = True
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()
        print(f"GStreamer camera started (target: {self.target_w}x{self.target_h})")

    def _run_loop(self):
        loop = GLib.MainLoop()
        try:
            loop.run()
        except Exception:
            pass

    def stop(self):
        self._running = False
        self.pipeline.set_state(Gst.State.NULL)

    @property
    def is_running(self):
        return self._running


def probe_v4l2_formats(device='/dev/video0'):
    """Probe a V4L2 device for supported formats to auto-select the best pipeline."""
    import subprocess
    try:
        result = subprocess.run(
            ['v4l2-ctl', '-d', device, '--list-formats-ext'],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout

        has_mjpeg = 'MJPG' in output or 'Motion-JPEG' in output
        has_h264 = 'H.264' in output or 'H264' in output
        has_yuyv = 'YUYV' in output or 'YUY2' in output

        if has_h264:
            return 'v4l2-h264'
        elif has_mjpeg:
            return 'v4l2-mjpeg'
        elif has_yuyv:
            return 'v4l2-raw'
        else:
            return 'v4l2'

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 'v4l2'


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Test GStreamer camera capture')
    parser.add_argument('--source', default='auto',
                        choices=['auto', 'v4l2', 'v4l2-mjpeg', 'v4l2-raw',
                                'v4l2-h264', 'csi', 'rtsp', 'test'])
    parser.add_argument('--device', default='/dev/video0')
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--fps', type=int, default=20)
    args = parser.parse_args()

    source = args.source
    if source == 'auto':
        source = probe_v4l2_formats(args.device)
        print(f"Auto-detected source type: {source}")

    frame_count = [0]
    t_start = [time.time()]

    def on_frame(frame):
        frame_count[0] += 1
        if frame_count[0] % 20 == 0:
            elapsed = time.time() - t_start[0]
            fps = frame_count[0] / elapsed
            print(f"Frame {frame_count[0]}: {frame.shape} @ {fps:.1f} FPS")

    cam = GstCamera(
        source=source,
        device=args.device,
        width=args.width,
        height=args.height,
        fps=args.fps,
        callback=on_frame
    )
    cam.start()

    try:
        while cam.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop()
        elapsed = time.time() - t_start[0]
        print(f"\nTotal: {frame_count[0]} frames in {elapsed:.1f}s "
              f"({frame_count[0]/elapsed:.1f} FPS)")
