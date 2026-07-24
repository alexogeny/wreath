"""Roost configuration — nested pydantic-settings groups.

Mirrors the real-world pattern of a top-level ``Settings`` composing several
``BaseSettings`` sub-groups (SMS, feature-flags, cloud) as fields.
"""
from pydantic_settings import BaseSettings


class TwilioSettings(BaseSettings):
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_SMS_API_KEY_NAME: str = ""
    TWILIO_MESSAGING_SERVICE_ID: str = ""


class UnleashSettings(BaseSettings):
    UNLEASH_URL: str = "https://flags.tumbleweed.example/api"
    UNLEASH_CLIENT_KEY: str = ""
    UNLEASH_INSTANCE_ID: str = "roost"


class AwsSettings(BaseSettings):
    AWS_DEFAULT_REGION: str = "us-west-2"


class Settings(BaseSettings):
    environment: str = "production"
    broker_url: str = "redis://localhost:6379/0"
    result_backend: str = "redis://localhost:6379/1"
    jwt_secret: str = "change-me"
    jwt_issuer: str = "https://auth.tumbleweed.example/"
    twilio: TwilioSettings = TwilioSettings()
    unleash: UnleashSettings = UnleashSettings()
    aws: AwsSettings = AwsSettings()


settings = Settings()
