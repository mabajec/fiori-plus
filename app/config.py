from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://fiori:fiori@localhost:5432/fiori"
    default_admin_email: str = "admin@local"
    default_admin_name: str = "Admin"
    inputs_dir: str = "./inputs"
    # MUST override in production. The dev default is intentionally obvious.
    session_secret: str = "dev-secret-do-not-use-in-production"
    session_max_age_days: int = 7
    session_https_only: bool = False  # set True when behind TLS in production
    totp_issuer: str = "Fiori"


settings = Settings()
