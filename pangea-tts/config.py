from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", protected_namespaces=())

    mlserver_base: str = (
        "http://kokoro-tts-predictor.pangea-energy-and-power.svc.cluster.local"
    )
    model_name: str = "kokoro-tts"

    voices_path: str = "/opt/app-root/src/voices.bin"
    placeholder_path: str = "/tmp/kokoro-remote.onnx"

    voice: str = "bf_emma"
    lang: str = "en-gb"
    speed: float = 1.0
    max_chars: int = 800
    http_timeout: float = 30.0

    @property
    def metadata_url(self) -> str:
        return f"{self.mlserver_base}/v2/models/{self.model_name}"

    @property
    def infer_url(self) -> str:
        return f"{self.mlserver_base}/v2/models/{self.model_name}/infer"


settings = Settings()
