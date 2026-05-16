from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://fiori:fiori@localhost:5432/fiori"
    default_admin_email: str = "admin@local"
    default_admin_name: str = "Admin"


settings = Settings()
