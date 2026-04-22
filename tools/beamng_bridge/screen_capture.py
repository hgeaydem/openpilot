"""
Screen capture with NVIDIA acceleration.

Backend hierarchy (auto-selected):
  1. NvFBC (NVIDIA Frame Buffer Capture) - GPU capture + crop + scale + convert
  2. GStreamer ximagesrc + nvvideoconvert - X11 capture with GPU resize
  3. mss + OpenCV - CPU fallback

NvFBC captures the X11 framebuffer directly on the GPU with built-in
hardware scaling and pixel format conversion. It is the same API used by
the truck_sim bridge (tools/truck_sim/cap/nvfbc_cap.cc).

Requires X11 display.
"""
import ctypes
from ctypes import (
    c_uint32, c_uint64, c_uint8, c_char, c_void_p,
    Structure, POINTER, sizeof, byref, CFUNCTYPE, cast,
)
import numpy as np
import time
import subprocess
import os

W, H = 1164, 874

# ============================================================
# NvFBC constants
# ============================================================

NVFBC_VERSION = 7 | (1 << 8)
NVFBC_SUCCESS = 0
NVFBC_CAPTURE_TO_SYS = 0
NVFBC_TRACKING_SCREEN = 2
NVFBC_BUFFER_FORMAT_RGB = 1
NVFBC_TOSYS_GRAB_FLAGS_NOWAIT = 1


def _nvfbc_ver(struct_cls, ver):
    return sizeof(struct_cls) | (ver << 16) | (NVFBC_VERSION << 24)


# ============================================================
# NvFBC structures (must match NvFBC.h layout exactly)
# ============================================================

class NVFBC_BOX(Structure):
    _fields_ = [("x", c_uint32), ("y", c_uint32), ("w", c_uint32), ("h", c_uint32)]


class NVFBC_SIZE(Structure):
    _fields_ = [("w", c_uint32), ("h", c_uint32)]


class NVFBC_FRAME_GRAB_INFO(Structure):
    _fields_ = [
        ("dwWidth", c_uint32),
        ("dwHeight", c_uint32),
        ("dwByteSize", c_uint32),
        ("dwCurrentFrame", c_uint32),
        ("bIsNewFrame", c_uint32),
        ("ulTimestampUs", c_uint64),
        ("dwMissedFrames", c_uint32),
        ("bRequiredPostProcessing", c_uint32),
        ("bDirectCapture", c_uint32),
    ]


_NVFBC_OUTPUT_NAME_LEN = 128
_NVFBC_OUTPUT_MAX = 5


class _NVFBC_RANDR_OUTPUT_INFO(Structure):
    _fields_ = [
        ("dwId", c_uint32),
        ("name", c_char * _NVFBC_OUTPUT_NAME_LEN),
        ("trackedBox", NVFBC_BOX),
    ]


class NVFBC_CREATE_HANDLE_PARAMS(Structure):
    _fields_ = [
        ("dwVersion", c_uint32),
        ("privateData", c_void_p),
        ("privateDataSize", c_uint32),
        ("bExternallyManagedContext", c_uint32),
        ("glxCtx", c_void_p),
        ("glxFBConfig", c_void_p),
    ]


class NVFBC_DESTROY_HANDLE_PARAMS(Structure):
    _fields_ = [("dwVersion", c_uint32)]


class NVFBC_GET_STATUS_PARAMS(Structure):
    _fields_ = [
        ("dwVersion", c_uint32),
        ("bIsCapturePossible", c_uint32),
        ("bCurrentlyCapturing", c_uint32),
        ("bCanCreateNow", c_uint32),
        ("screenSize", NVFBC_SIZE),
        ("bXRandRAvailable", c_uint32),
        ("outputs", _NVFBC_RANDR_OUTPUT_INFO * _NVFBC_OUTPUT_MAX),
        ("dwOutputNum", c_uint32),
        ("dwNvFBCVersion", c_uint32),
        ("bInModeset", c_uint32),
    ]


class NVFBC_CREATE_CAPTURE_SESSION_PARAMS(Structure):
    _fields_ = [
        ("dwVersion", c_uint32),
        ("eCaptureType", c_uint32),
        ("eTrackingType", c_uint32),
        ("dwOutputId", c_uint32),
        ("captureBox", NVFBC_BOX),
        ("frameSize", NVFBC_SIZE),
        ("bWithCursor", c_uint32),
        ("bDisableAutoModesetRecovery", c_uint32),
        ("bRoundFrameSize", c_uint32),
        ("dwSamplingRateMs", c_uint32),
        ("bPushModel", c_uint32),
        ("bAllowDirectCapture", c_uint32),
    ]


class NVFBC_DESTROY_CAPTURE_SESSION_PARAMS(Structure):
    _fields_ = [("dwVersion", c_uint32)]


class NVFBC_TOSYS_SETUP_PARAMS(Structure):
    _fields_ = [
        ("dwVersion", c_uint32),
        ("eBufferFormat", c_uint32),
        ("ppBuffer", POINTER(c_void_p)),
        ("bWithDiffMap", c_uint32),
        ("ppDiffMap", POINTER(c_void_p)),
        ("dwDiffMapScalingFactor", c_uint32),
        ("diffMapSize", NVFBC_SIZE),
    ]


class NVFBC_TOSYS_GRAB_FRAME_PARAMS(Structure):
    _fields_ = [
        ("dwVersion", c_uint32),
        ("dwFlags", c_uint32),
        ("pFrameGrabInfo", POINTER(NVFBC_FRAME_GRAB_INFO)),
        ("dwTimeoutMs", c_uint32),
    ]


class NVFBC_API_FUNCTION_LIST(Structure):
    _fields_ = [
        ("dwVersion", c_uint32),
        ("nvFBCGetLastErrorStr", c_void_p),
        ("nvFBCCreateHandle", c_void_p),
        ("nvFBCDestroyHandle", c_void_p),
        ("nvFBCGetStatus", c_void_p),
        ("nvFBCCreateCaptureSession", c_void_p),
        ("nvFBCDestroyCaptureSession", c_void_p),
        ("nvFBCToSysSetUp", c_void_p),
        ("nvFBCToSysGrabFrame", c_void_p),
        ("nvFBCToCudaSetUp", c_void_p),
        ("nvFBCToCudaGrabFrame", c_void_p),
        ("pad1", c_void_p),
        ("pad2", c_void_p),
        ("pad3", c_void_p),
        ("nvFBCBindContext", c_void_p),
        ("nvFBCReleaseContext", c_void_p),
        ("pad4", c_void_p),
        ("pad5", c_void_p),
        ("pad6", c_void_p),
        ("pad7", c_void_p),
        ("nvFBCToGLSetUp", c_void_p),
        ("nvFBCToGLGrabFrame", c_void_p),
    ]


# Function pointer types for NvFBC API calls
_FN_CREATE_HANDLE = CFUNCTYPE(c_uint32, POINTER(c_uint64), POINTER(NVFBC_CREATE_HANDLE_PARAMS))
_FN_DESTROY_HANDLE = CFUNCTYPE(c_uint32, c_uint64, POINTER(NVFBC_DESTROY_HANDLE_PARAMS))
_FN_GET_STATUS = CFUNCTYPE(c_uint32, c_uint64, POINTER(NVFBC_GET_STATUS_PARAMS))
_FN_CREATE_SESSION = CFUNCTYPE(c_uint32, c_uint64, POINTER(NVFBC_CREATE_CAPTURE_SESSION_PARAMS))
_FN_DESTROY_SESSION = CFUNCTYPE(c_uint32, c_uint64, POINTER(NVFBC_DESTROY_CAPTURE_SESSION_PARAMS))
_FN_TOSYS_SETUP = CFUNCTYPE(c_uint32, c_uint64, POINTER(NVFBC_TOSYS_SETUP_PARAMS))
_FN_TOSYS_GRAB = CFUNCTYPE(c_uint32, c_uint64, POINTER(NVFBC_TOSYS_GRAB_FRAME_PARAMS))
_FN_GET_ERROR = CFUNCTYPE(ctypes.c_char_p, c_uint64)


# ============================================================
# NvFBC capture class
# ============================================================

class NvFBCCapture:
    """GPU-accelerated screen capture via NVIDIA NvFBC (libnvidia-fbc.so.1).

    Captures the X11 framebuffer directly on the GPU. NvFBC handles
    cropping, scaling, and pixel format conversion in hardware.
    """

    def __init__(self, capture_box=None, frame_w=W, frame_h=H):
        self._lib = ctypes.CDLL("libnvidia-fbc.so.1")

        create_instance = self._lib.NvFBCCreateInstance
        create_instance.argtypes = [POINTER(NVFBC_API_FUNCTION_LIST)]
        create_instance.restype = c_uint32

        self._fn = NVFBC_API_FUNCTION_LIST()
        self._fn.dwVersion = NVFBC_VERSION
        status = create_instance(byref(self._fn))
        if status != NVFBC_SUCCESS:
            raise RuntimeError(f"NvFBCCreateInstance failed ({status})")

        self._create_handle = cast(self._fn.nvFBCCreateHandle, _FN_CREATE_HANDLE)
        self._destroy_handle = cast(self._fn.nvFBCDestroyHandle, _FN_DESTROY_HANDLE)
        self._get_status = cast(self._fn.nvFBCGetStatus, _FN_GET_STATUS)
        self._create_session = cast(self._fn.nvFBCCreateCaptureSession, _FN_CREATE_SESSION)
        self._destroy_session = cast(self._fn.nvFBCDestroyCaptureSession, _FN_DESTROY_SESSION)
        self._tosys_setup = cast(self._fn.nvFBCToSysSetUp, _FN_TOSYS_SETUP)
        self._tosys_grab = cast(self._fn.nvFBCToSysGrabFrame, _FN_TOSYS_GRAB)
        self._get_error = cast(self._fn.nvFBCGetLastErrorStr, _FN_GET_ERROR)

        self._handle = c_uint64(0)
        params = NVFBC_CREATE_HANDLE_PARAMS()
        ctypes.memset(byref(params), 0, sizeof(params))
        params.dwVersion = _nvfbc_ver(NVFBC_CREATE_HANDLE_PARAMS, 2)

        status = self._create_handle(byref(self._handle), byref(params))
        if status != NVFBC_SUCCESS:
            raise RuntimeError(f"nvFBCCreateHandle failed ({status})")

        status_params = NVFBC_GET_STATUS_PARAMS()
        ctypes.memset(byref(status_params), 0, sizeof(status_params))
        status_params.dwVersion = _nvfbc_ver(NVFBC_GET_STATUS_PARAMS, 2)
        self._get_status(self._handle.value, byref(status_params))

        if not status_params.bCanCreateNow:
            raise RuntimeError("NvFBC: cannot create capture session on this system")

        screen_w = status_params.screenSize.w
        screen_h = status_params.screenSize.h
        print(f"NvFBC: screen size {screen_w}x{screen_h}")

        if capture_box is None:
            target_aspect = frame_w / frame_h
            cropped_w = int(frame_w / frame_h * screen_h)
            crop_x = max(0, (screen_w - cropped_w) // 2)
            capture_box = NVFBC_BOX(x=crop_x, y=0, w=min(cropped_w, screen_w), h=screen_h)

        cap_params = NVFBC_CREATE_CAPTURE_SESSION_PARAMS()
        ctypes.memset(byref(cap_params), 0, sizeof(cap_params))
        cap_params.dwVersion = _nvfbc_ver(NVFBC_CREATE_CAPTURE_SESSION_PARAMS, 6)
        cap_params.eCaptureType = NVFBC_CAPTURE_TO_SYS
        cap_params.eTrackingType = NVFBC_TRACKING_SCREEN
        cap_params.bWithCursor = 1
        cap_params.frameSize = NVFBC_SIZE(w=frame_w, h=frame_h)
        cap_params.captureBox = capture_box
        cap_params.bPushModel = 1
        cap_params.bAllowDirectCapture = 1

        status = self._create_session(self._handle.value, byref(cap_params))
        if status != NVFBC_SUCCESS:
            err = self._get_error(self._handle.value)
            raise RuntimeError(f"nvFBCCreateCaptureSession failed ({status}): {err}")

        self._buffer_ptr = c_void_p()
        setup = NVFBC_TOSYS_SETUP_PARAMS()
        ctypes.memset(byref(setup), 0, sizeof(setup))
        setup.dwVersion = _nvfbc_ver(NVFBC_TOSYS_SETUP_PARAMS, 3)
        setup.eBufferFormat = NVFBC_BUFFER_FORMAT_RGB
        setup.ppBuffer = ctypes.pointer(self._buffer_ptr)

        status = self._tosys_setup(self._handle.value, byref(setup))
        if status != NVFBC_SUCCESS:
            err = self._get_error(self._handle.value)
            raise RuntimeError(f"nvFBCToSysSetUp failed ({status}): {err}")

        self._frame_info = NVFBC_FRAME_GRAB_INFO()
        self._grab_params = NVFBC_TOSYS_GRAB_FRAME_PARAMS()
        ctypes.memset(byref(self._grab_params), 0, sizeof(self._grab_params))
        self._grab_params.dwVersion = _nvfbc_ver(NVFBC_TOSYS_GRAB_FRAME_PARAMS, 2)
        self._grab_params.dwFlags = NVFBC_TOSYS_GRAB_FLAGS_NOWAIT
        self._grab_params.pFrameGrabInfo = ctypes.pointer(self._frame_info)

        self._frame_w = frame_w
        self._frame_h = frame_h

        print(f"NvFBC: capture session ready "
              f"(box={capture_box.x},{capture_box.y},{capture_box.w},{capture_box.h} "
              f"-> {frame_w}x{frame_h} RGB)")

    def grab(self):
        status = self._tosys_grab(self._handle.value, byref(self._grab_params))
        if status != NVFBC_SUCCESS:
            return None
        if self._buffer_ptr.value is None:
            return None

        info = self._frame_info
        raw = (c_uint8 * info.dwByteSize).from_address(self._buffer_ptr.value)
        return np.frombuffer(raw, dtype=np.uint8).reshape(
            info.dwHeight, info.dwWidth, 3
        ).copy()

    def destroy(self):
        params = NVFBC_DESTROY_CAPTURE_SESSION_PARAMS()
        params.dwVersion = _nvfbc_ver(NVFBC_DESTROY_CAPTURE_SESSION_PARAMS, 1)
        self._destroy_session(self._handle.value, byref(params))

        params2 = NVFBC_DESTROY_HANDLE_PARAMS()
        params2.dwVersion = _nvfbc_ver(NVFBC_DESTROY_HANDLE_PARAMS, 1)
        self._destroy_handle(self._handle.value, byref(params2))

    def __del__(self):
        try:
            self.destroy()
        except Exception:
            pass


# ============================================================
# Window / screen helpers
# ============================================================

WINDOW_RELOCATE_INTERVAL = 5.0


def _find_window_geometry(title_pattern):
    try:
        result = subprocess.run(
            ['xdotool', 'search', '--name', title_pattern],
            capture_output=True, text=True, timeout=5,
        )
        wids = result.stdout.strip().split('\n')
        if not wids or not wids[0]:
            return None

        wid = wids[0]
        geo = subprocess.run(
            ['xdotool', 'getwindowgeometry', '--shell', wid],
            capture_output=True, text=True, timeout=5,
        )
        vals = {}
        for line in geo.stdout.strip().split('\n'):
            if '=' in line:
                k, v = line.split('=', 1)
                vals[k] = int(v)

        return {
            "left": vals.get("X", 0),
            "top": vals.get("Y", 0),
            "width": vals.get("WIDTH", 1920),
            "height": vals.get("HEIGHT", 1080),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None


def _find_window_xid(title_pattern):
    try:
        result = subprocess.run(
            ['xdotool', 'search', '--name', title_pattern],
            capture_output=True, text=True, timeout=5,
        )
        wids = result.stdout.strip().split('\n')
        if wids and wids[0]:
            return int(wids[0])
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return None


def _window_to_nvfbc_box(geo):
    target_aspect = W / H
    win_x, win_y = geo["left"], geo["top"]
    win_w, win_h = geo["width"], geo["height"]
    src_aspect = win_w / win_h

    if src_aspect > target_aspect:
        crop_w = int(win_h * target_aspect)
        crop_x = win_x + (win_w - crop_w) // 2
        return NVFBC_BOX(x=crop_x, y=win_y, w=crop_w, h=win_h)
    else:
        crop_h = int(win_w / target_aspect)
        crop_y = win_y + (win_h - crop_h) // 2
        return NVFBC_BOX(x=win_x, y=crop_y, w=win_w, h=crop_h)


def _parse_region_string(region_str):
    parts = [int(x.strip()) for x in region_str.split(',')]
    if len(parts) != 4:
        raise ValueError(f"Expected x,y,w,h but got: {region_str}")
    return {"left": parts[0], "top": parts[1], "width": parts[2], "height": parts[3]}


# ============================================================
# Backend: NvFBC
# ============================================================

def _start_nvfbc(callback, window_title, region, target_fps):
    capture_box = None
    if window_title:
        geo = _find_window_geometry(window_title)
        if geo:
            capture_box = _window_to_nvfbc_box(geo)
            print(f"NvFBC: targeting window '{window_title}' at "
                  f"{geo['width']}x{geo['height']}+{geo['left']}+{geo['top']}")
    elif region:
        if isinstance(region, str):
            geo = _parse_region_string(region)
        else:
            geo = region
        capture_box = _window_to_nvfbc_box(geo)

    cap = NvFBCCapture(capture_box=capture_box)
    dt = 1.0 / target_fps

    while True:
        t0 = time.time()
        frame = cap.grab()
        if frame is not None:
            callback(frame)
        elapsed = time.time() - t0
        if elapsed < dt:
            time.sleep(dt - elapsed)


# ============================================================
# Backend: GStreamer ximagesrc + NVIDIA
# ============================================================

def _start_gstreamer_screen(callback, window_title, target_fps):
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst, GLib

    Gst.init(None)

    src = "ximagesrc use-damage=0"
    if window_title:
        xid = _find_window_xid(window_title)
        if xid:
            src += f" xid={xid}"
            print(f"GStreamer: targeting window XID {xid}")

    has_nv = Gst.ElementFactory.find('nvvideoconvert') is not None
    if has_nv:
        convert = "videoconvert ! nvvideoconvert"
        print("GStreamer: using nvvideoconvert (GPU resize + colorspace)")
    else:
        convert = "videoconvert ! videoscale"
        print("GStreamer: using CPU videoscale (no NVIDIA plugins found)")

    pipe_str = (
        f"{src} ! video/x-raw,framerate={target_fps}/1 ! "
        f"{convert} ! "
        f"video/x-raw,width={W},height={H},format=RGB ! "
        f"appsink name=sink emit-signals=true max-buffers=1 drop=true"
    )
    print(f"GStreamer pipeline:\n  {pipe_str}")

    pipeline = Gst.parse_launch(pipe_str)
    sink = pipeline.get_by_name("sink")

    def on_new_sample(sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        success, map_info = buf.map(Gst.MapFlags.READ)
        if success:
            frame = np.frombuffer(map_info.data, dtype=np.uint8)
            frame = frame.reshape(H, W, 3).copy()
            buf.unmap(map_info)
            callback(frame)
        return Gst.FlowReturn.OK

    sink.connect("new-sample", on_new_sample)
    pipeline.set_state(Gst.State.PLAYING)

    loop = GLib.MainLoop()
    try:
        loop.run()
    except Exception:
        pass
    finally:
        pipeline.set_state(Gst.State.NULL)


# ============================================================
# Backend: mss (CPU fallback)
# ============================================================

def _start_mss(callback, window_title, region, target_fps):
    import cv2
    try:
        import mss as mss_module
    except ImportError:
        raise ImportError("mss is required for CPU screen capture: pip install mss")

    sct = mss_module.mss()

    monitor = None
    if window_title:
        geo = _find_window_geometry(window_title)
        if geo:
            monitor = geo
            print(f"mss: targeting window '{window_title}' at "
                  f"{geo['width']}x{geo['height']}+{geo['left']}+{geo['top']}")
        else:
            print(f"WARNING: window '{window_title}' not found, capturing primary monitor")

    if monitor is None and region:
        if isinstance(region, str):
            monitor = _parse_region_string(region)
        else:
            monitor = region

    if monitor is None:
        monitor = sct.monitors[1]
        print(f"mss: capturing primary monitor {monitor['width']}x{monitor['height']}")

    dt = 1.0 / target_fps
    target_aspect = W / H
    last_relocate = time.time()

    print(f"mss: screen capture started ({W}x{H} @ {target_fps} FPS)")

    while True:
        t0 = time.time()

        if window_title and (t0 - last_relocate > WINDOW_RELOCATE_INTERVAL):
            updated = _find_window_geometry(window_title)
            if updated:
                monitor = updated
            last_relocate = t0

        img = sct.grab(monitor)
        frame = np.array(img)[:, :, :3]

        src_h, src_w = frame.shape[:2]
        src_aspect = src_w / src_h
        if src_aspect > target_aspect:
            crop_w = int(src_h * target_aspect)
            x_off = (src_w - crop_w) // 2
            frame = frame[:, x_off:x_off + crop_w]
        elif src_aspect < target_aspect:
            crop_h = int(src_w / target_aspect)
            y_off = (src_h - crop_h) // 2
            frame = frame[y_off:y_off + crop_h, :]

        frame = cv2.resize(frame, (W, H))
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        callback(frame)

        elapsed = time.time() - t0
        if elapsed < dt:
            time.sleep(dt - elapsed)


# ============================================================
# Auto-detecting entry point
# ============================================================

def _try_nvfbc():
    try:
        ctypes.CDLL("libnvidia-fbc.so.1")
        return True
    except OSError:
        return False


def _try_gstreamer():
    try:
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        return Gst.ElementFactory.find('ximagesrc') is not None
    except Exception:
        return False


def start_camera_screen(callback, window_title=None, region=None, target_fps=20):
    """Start screen capture with the best available backend.

    Tries NvFBC (GPU), then GStreamer ximagesrc, then mss (CPU).
    """
    if _try_nvfbc():
        print("Screen capture: using NvFBC (GPU-accelerated framebuffer capture)")
        try:
            return _start_nvfbc(callback, window_title, region, target_fps)
        except Exception as e:
            print(f"NvFBC init failed ({e}), trying fallback...")

    if _try_gstreamer():
        print("Screen capture: using GStreamer ximagesrc")
        try:
            return _start_gstreamer_screen(callback, window_title, target_fps)
        except Exception as e:
            print(f"GStreamer screen capture failed ({e}), trying fallback...")

    print("Screen capture: using mss (CPU fallback)")
    return _start_mss(callback, window_title, region, target_fps)
