"""Redis queue configuration."""

from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class RedisConfig:
    """Runtime settings for the import queue and worker."""

    url: str = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    queue_name: str = os.getenv("REDIS_QUEUE_NAME", "knowledge_base_import")
    max_retries: int = int(os.getenv("REDIS_MAX_RETRIES", "3"))
    job_timeout_seconds: int = int(os.getenv("REDIS_JOB_TIMEOUT", "3600"))
    visibility_timeout_seconds: int = int(os.getenv("REDIS_VISIBILITY_TIMEOUT", "7200"))
    processing_timeout_seconds: int = int(os.getenv("REDIS_PROCESSING_TIMEOUT", "7200"))


redis_config = RedisConfig()
