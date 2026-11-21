"""All env + model names. Never hardcode model names elsewhere."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    openai_model_brain: str = ""
    openai_model_fast: str = ""
    openai_model_transcribe: str = ""
    openai_model_tts: str = ""

    omnidim_api_key: str = ""
    omnidim_agent_id: str = ""
    omnidim_from_number_id: str = ""
    omnidim_inbound_agent_id: str = ""
    omnidim_base_url: str = "https://backend.omnidim.io/api/v1"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"

    aws_region: str = "us-west-2"
    ddb_table_prefix: str = "chinku_"
    store_backend: str = ""  # "dynamo" or "" (local JSON)
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""  # workshop accounts: temporary credentials
    scheduler_role_arn: str = ""

    public_base_url: str = ""
    smtp_user: str = ""
    smtp_app_password: str = ""
    email_demo_override: bool = True  # deliver every email to SMTP_USER inbox (demo safety)
    demo_reminder_seconds: int = 0  # >0: schedule steps fire after N s (SIMULATED time, for demo)
    user_name: str = "Hanish"
    default_language: str = "English"
    mode: str = "plumber"  # "plumber" = Chinku for Plumbers (job router); "errands" = v1 specs only
    job_id_start: int = 21
    plumber_name: str = "Hanish"  # calls, replies, voice notes (Telugu off for now)
    user_whatsapp: str = ""
    chat_key: str = ""  # secret for the /chat web page
    test_phone: str = ""


@lru_cache
def settings() -> Settings:
    return Settings()
