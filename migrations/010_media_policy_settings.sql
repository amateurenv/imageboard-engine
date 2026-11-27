-- общие лимиты соответствуют обычному Telegram Bot API для двусторонней синхронизации
INSERT INTO site_settings (key, value)
VALUES (
    'media',
    '{
        "max_attachments_per_post": 10,
        "max_photo_size_mib": 10,
        "max_other_media_size_mib": 20,
        "max_total_size_mib": 200,
        "allow_anonymous_uploads": true
    }'::jsonb
)
ON CONFLICT (key) DO NOTHING;

-- null-подобный пустой объект означает: доска наследует общие лимиты сайта
ALTER TABLE boards
ADD COLUMN media_overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
ADD CONSTRAINT boards_media_overrides_object CHECK (jsonb_typeof(media_overrides) = 'object');
