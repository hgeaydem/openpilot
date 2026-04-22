#!/usr/bin/env python3

import os
import sys
import numpy as np

os.environ["OMP_NUM_THREADS"] = "4"

import onnxruntime as ort

def read(sz):
  dd = []
  gt = 0
  while gt < sz * 4:
    st = os.read(0, sz * 4 - gt)
    assert(len(st) > 0)
    dd.append(st)
    gt += len(st)
  return np.frombuffer(b''.join(dd), dtype=np.float32)

def write(d):
  os.write(1, d.tobytes())

def run_loop(m):
  ishapes = [[1]+ii.shape[1:] for ii in m.get_inputs()]
  keys = [x.name for x in m.get_inputs()]
  print("ready to run onnx model", keys, ishapes, file=sys.stderr)
  while 1:
    inputs = []
    for shp in ishapes:
      ts = np.product(shp)
      #print("reshaping %s with offset %d" % (str(shp), offset), file=sys.stderr)
      inputs.append(read(ts).reshape(shp))
    ret = m.run(None, dict(zip(keys, inputs)))
    #print(ret, file=sys.stderr)
    for r in ret:
      write(r)


if __name__ == "__main__":
  available = ort.get_available_providers()
  print(available, file=sys.stderr)

  provider = None
  provider_options = None

  if 'TensorrtExecutionProvider' in available and 'ONNXCPU' not in os.environ:
    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'models', 'trt_cache')
    os.makedirs(cache_dir, exist_ok=True)
    print(f"OnnxJit is using TensorRT (cache: {cache_dir})", file=sys.stderr)
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    provider = 'TensorrtExecutionProvider'
    provider_options = [{
      'trt_max_workspace_size': str(1 << 30),
      'trt_fp16_enable': 'true',
      'trt_engine_cache_enable': 'true',
      'trt_engine_cache_path': cache_dir,
    }]
  elif 'OpenVINOExecutionProvider' in available and 'ONNXCPU' not in os.environ:
    print("OnnxJit is using openvino", file=sys.stderr)
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    provider = 'OpenVINOExecutionProvider'
  elif 'CUDAExecutionProvider' in available and 'ONNXCPU' not in os.environ:
    print("OnnxJit is using CUDA", file=sys.stderr)
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    provider = 'CUDAExecutionProvider'
  else:
    print("OnnxJit is using CPU", file=sys.stderr)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 8
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    provider = 'CPUExecutionProvider'

  ort_session = ort.InferenceSession(sys.argv[1], options)
  if provider_options:
    ort_session.set_providers([provider], provider_options)
  else:
    ort_session.set_providers([provider], None)
  run_loop(ort_session)
