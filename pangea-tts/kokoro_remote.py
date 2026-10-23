from __future__ import annotations

import httpx
import numpy as np

_NP_TO_V2 = {
    "float32": "FP32",
    "float16": "FP16",
    "float64": "FP64",
    "int32": "INT32",
    "int64": "INT64",
}
_V2_TO_NP = {
    "FP32": np.float32,
    "FP16": np.float16,
    "FP64": np.float64,
    "INT32": np.int32,
    "INT64": np.int64,
    "BOOL": np.bool_,
}
_V2_TO_ORT = {
    "FP32": "tensor(float)",
    "FP16": "tensor(float16)",
    "FP64": "tensor(double)",
    "INT32": "tensor(int32)",
    "INT64": "tensor(int64)",
}


class _Node:

    __slots__ = ("name", "type", "shape")

    def __init__(self, name: str, type_: str, shape):
        self.name = name
        self.type = type_
        self.shape = shape


class RemoteSession:
    def __init__(self, infer_url: str, metadata: dict, model_path: str,
                 timeout: float = 30.0):
        self.infer_url = infer_url
        self._model_path = model_path
        self._timeout = timeout
        self._inputs = [
            _Node(i["name"], _V2_TO_ORT.get(i.get("datatype"), "tensor(float)"),
                  i.get("shape"))
            for i in metadata.get("inputs", [])
        ]
        self._outputs = [
            _Node(o["name"], _V2_TO_ORT.get(o.get("datatype"), "tensor(float)"),
                  o.get("shape"))
            for o in metadata.get("outputs", [])
        ]

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return self._outputs

    def run(self, output_names, input_feed):
        v2_inputs = []
        for name, arr in input_feed.items():
            arr = np.asarray(arr)
            datatype = _NP_TO_V2.get(arr.dtype.name)
            if datatype is None:
                arr = arr.astype(np.float32)
                datatype = "FP32"
            v2_inputs.append({
                "name": name,
                "shape": list(arr.shape),
                "datatype": datatype,
                "data": arr.ravel().tolist(),
            })

        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(self.infer_url, json={"inputs": v2_inputs})
            resp.raise_for_status()
            body = resp.json()

        by_name = {}
        ordered = []
        for out in body.get("outputs", []):
            np_dt = _V2_TO_NP.get(out.get("datatype"), np.float32)
            arr = np.asarray(out.get("data", []), dtype=np_dt)
            shape = out.get("shape")
            if shape:
                try:
                    arr = arr.reshape(shape)
                except ValueError:
                    pass
            by_name[out.get("name")] = arr
            ordered.append(arr)

        if self._outputs and all(n.name in by_name for n in self._outputs):
            return [by_name[n.name] for n in self._outputs]
        return ordered
