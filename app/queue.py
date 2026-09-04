from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings

settings = get_settings()


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def enqueue_download(job_id: int) -> None:
    redis = await create_pool(redis_settings())
    try:
        await redis.enqueue_job("process_download", job_id)
    finally:
        await redis.aclose()
