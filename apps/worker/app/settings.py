from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    supabase_storage_bucket: str = "easylab"
    database_url: str  # direct connection (port 5432) for asyncpg

    gemini_api_key: str = ""
    gemini_model_pro: str = "gemini-2.5-pro"
    gemini_model_flash: str = "gemini-2.5-flash"
    # Stage C (value extraction) — Flash by default for cost/latency. Separate
    # alias so deployments can swap extraction independently.
    gemini_extraction_model: str = "gemini-2.5-flash"

    internal_shared_secret: str
    fastapi_port: int = 8000

    worker_concurrency: int = 2
    image_concurrency: int = 5
    confidence_threshold: float = 0.7

    # Spend controls (see app/pipeline/spend_controls.py).
    gemini_breaker_threshold: int = 5
    gemini_breaker_cooldown_seconds: int = 300
    global_daily_token_budget: int = 50_000_000
    # Per-workspace daily token cap (sum of tokens_today). Set to 0 to disable.
    workspace_daily_token_budget: int = 2_000_000

    # PaddleOCR runs alongside Gemini Flash in Stage C. Set to false to skip
    # loading the paddle stack at runtime (e.g. CI, local dev without OCR).
    ocr_enabled: bool = True

    # Stage B diagnostics. When true, each processed image gets a JPEG overlay
    # written to Supabase Storage at
    # ``projects/{project_id}/debug/{job_id}/{image_id}.jpg``. The per-field
    # ``stage_b.detector_raw`` events (raw detector box + IoU vs the user's
    # reference_box) are logged regardless of this flag.
    # Off by default — flip on when investigating detection bugs.
    stage_b_debug: bool = False

    log_level: str = "INFO"

    @property
    def detector_version(self) -> str:
        """``model_versions.detector`` for Stage B/C cells (ADR-007 A6).

        Stage B detection is ``app.pipeline.align``; bump ``align-vN`` when its
        algorithm or thresholds change. Embeds the OpenCV build because results
        are only bit-identical within one OpenCV version. Not env-overridable,
        so the provenance can't drift from the code that ran.
        """
        import cv2

        return f"align-v1+opencv{cv2.__version__}"

    class Config:
        env_file = ".env"


settings = Settings()
