from app.config import Settings


def test_phase456_media_defaults_are_safe_and_optional():
    settings = Settings(_env_file=None)
    assert settings.telegram_upload_limit_mb == 49
    assert settings.auto_compress_enabled is True
    assert settings.media_compression_attempts == 2
    assert settings.ytdlp_concurrent_fragments == 4
    assert settings.max_file_size_mb >= settings.telegram_upload_limit_mb


def test_phase456_tuning_values_are_clamped():
    settings = Settings(
        _env_file=None,
        ytdlp_concurrent_fragments=999,
        media_compression_attempts=99,
        telegram_upload_limit_mb=0,
    )
    assert settings.ytdlp_concurrent_fragments == 16
    assert settings.media_compression_attempts == 4
    assert settings.telegram_upload_limit_mb == 1
