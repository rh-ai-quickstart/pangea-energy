from typing import Dict, Optional

import kserve


class LayaPredictor(kserve.Model):
    def __init__(
        self,
        name: str,
        device: Optional[str] = None,
        default: str = "english",
        max_loaded: int = 2,
        preload: bool = True,
    ):
        super().__init__(name)
        self.device = device
        self.default = default
        self.max_loaded = max_loaded
        self.preload = preload
        self.router = None

    def load(self):
        from laya import Router

        print("Loading Laya Router (preload=%s, device=%s)" % (self.preload, self.device))
        self.router = Router(
            device=self.device,
            default=self.default,
            max_loaded=self.max_loaded,
            preload=self.preload,
        )
        self.ready = True
        print("Router loaded successfully")

    def predict(self, payload: Dict, headers: Dict[str, str] = None) -> Dict:
        state = payload["state"]
        questions = payload["questions"]

        overrides = {}
        for key in ("model", "task", "lang", "max_len", "head_max_len"):
            if key in payload:
                overrides[key] = payload[key]

        return self.router.predict(state, questions, **overrides)
