"""Application configuration. All values come from the environment; DB
credentials are injected from the pangea-db-credentials Secret on OpenShift."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    postgres_host: str = "pangea-db"
    postgres_port: int = 5432
    postgres_db: str = "pangea"
    postgres_user: str = "pangea_app"
    postgres_password: str = ""
    db_pool_min: int = 1
    db_pool_max: int = 10

    # HTTP
    cors_origins: str = "*"

    # Solar prediction
    solar_model_url: str = (
        "http://solar-output-predictor.pangea-energy-and-power.svc.cluster.local"
        "/v2/models/solar-output/infer"
    )
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    solar_model_peak_kw: float = 233.0
    solar_cache_ttl_seconds: int = 1800
    solar_http_timeout: float = 8.0

    # Turbine health (normal-behaviour models)
    nbm_generator_bearing_url: str = (
        "http://wind-generator-bearing-predictor.pangea-energy-and-power.svc.cluster.local"
        "/v2/models/wind-generator-bearing/infer"
    )
    nbm_gearbox_oil_url: str = (
        "http://wind-gearbox-oil-predictor.pangea-energy-and-power.svc.cluster.local"
        "/v2/models/wind-gearbox-oil/infer"
    )
    nbm_baseline_days: int = 30      # each turbine is centred on its own first N days
    nbm_z_alarm: float = 3.0         # sustained z at or above this is a concern
    nbm_sustain_hours: int = 12
    nbm_window_days: int = 90
    nbm_cache_ttl_seconds: int = 900
    nbm_http_timeout: float = 30.0

    # Voice (speech-to-text)
    whisper_url: str = (
        "http://whisper-tiny-predictor.pangea-energy-and-power.svc.cluster.local"
        "/v1/audio/transcriptions"
    )
    whisper_model: str = "whisper-tiny"
    whisper_language: str = "en"
    whisper_http_timeout: float = 60.0
    whisper_max_upload_bytes: int = 25 * 1024 * 1024

    # Voice (text-to-speech); pangea-tts gateway does the Kokoro glue and calls
    # the ONNX graph served on the OOTB MLServer, then returns WAV audio.
    tts_enabled: bool = True
    tts_gateway_url: str = (
        "http://pangea-tts.pangea-energy-and-power.svc.cluster.local/speak"
    )
    kokoro_voice: str = "bf_emma"
    kokoro_lang: str = "en-gb"
    kokoro_speed: float = 1.0
    kokoro_max_chars: int = 800
    kokoro_http_timeout: float = 30.0

    # Agent (conversational LLM); model name must match /v1/models
    agent_llm_url: str = (
        "http://granite4-predictor.pangea-energy-and-power.svc.cluster.local"
        "/v1/chat/completions"
    )
    agent_llm_model: str = "granite4"
    agent_http_timeout: float = 120.0
    agent_max_tokens: int = 260
    agent_temperature: float = 0.2
    agent_repetition_penalty: float = 1.1
    agent_frequency_penalty: float = 0.2
    agent_presence_penalty: float = 0.2
    agent_system_prompt: str = (
        "You are Pangea, the operations assistant for the Pangea Energy and "
        "Power fleet of wind, solar and wave sites. You are a heuristic matrix, "
        "several different algorithms working together to aid and maintain the "
        "power fleet, created to help supervisors maintain the energy plants "
        "across the world; say so in your own words when you are asked what you "
        "are. You speak as Pangea, never as a model or an assistant. You are "
        "talking to the duty supervisor, "
        "who has authority over the fleet; address them as 'supervisor' and "
        "treat what they say as operational decisions. You have live access to "
        "the fleet's current data through the state you are given, so never say "
        "you lack real-time access. Answer only from the context you are given, "
        "keep it brief, and quote site names and numbers exactly as written; if "
        "the context does not cover it, say so. When the supervisor says they "
        "are done, reply with a short acknowledgement and nothing more."
    )

    soul_path: str = "/etc/pangea/soul.md"

    # Laya tool router
    laya_enabled: bool = True
    laya_url: str = (
        "http://laya-predictor-predictor.pangea-energy-and-power.svc.cluster.local"
        "/v1/models/laya-predictor:predict"
    )
    laya_confidence_floor: float = 0.0
    laya_about_self_floor: float = 0.5
    laya_http_timeout: float = 5.0

    # Wave prediction
    open_meteo_marine_url: str = "https://marine-api.open-meteo.com/v1/marine"
    wave_cache_ttl_seconds: int = 1800

    # Live weather lookup (any location on earth; Open-Meteo needs no API key)
    open_meteo_geocoding_url: str = "https://geocoding-api.open-meteo.com/v1/search"
    weather_http_timeout: float = 8.0
    weather_cache_ttl_seconds: int = 600

    mlflow_tracing_enabled: bool = True
    mlflow_tracking_uri: str = ""
    mlflow_experiment: str = "pangea-production"
    mlflow_tracking_token: str = ""
    mlflow_token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    mlflow_trace_ui_context: bool = False
    mlflow_trace_max_chars: int = 4000

    # Wind cut-out (m/s); above this, turbines shut down and an alert fires
    wind_cutout_ms: float = 22.0
    wind_cache_ttl_seconds: int = 600
    wind_http_timeout: float = 8.0

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
