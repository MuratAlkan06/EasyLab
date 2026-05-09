from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    supabase_storage_bucket: str = "easylab"
    database_url: str  # direct connection (port 5432) for asyncpg

    gemini_api_key: str = ""
    gemini_model_pro: str = "gemini-2.5-pro"
    gemini_model_flash: str = "gemini-2.5-flash"
    # Stage B (region detection) — separate alias so the deployment can swap
    # detection independently from template generation if needed.
    gemini_detection_model: str = "gemini-2.5-pro"
    # Stage C (value extraction) — Flash by default for cost/latency. Same
    # alias rationale as detection: deployments can swap independently.
    gemini_extraction_model: str = "gemini-2.5-flash"

    internal_shared_secret: str
    fastapi_port: int = 8000

    worker_concurrency: int = 2
    image_concurrency: int = 5
    confidence_threshold: float = 0.7

    log_level: str = "INFO"

    class Config:
        env_file = ".env"


settings = Settings()
