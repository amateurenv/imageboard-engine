-- audio не хранит имя, теги или обложку пользователя - только технические данные derivative
ALTER TABLE media
ADD COLUMN media_kind text NOT NULL DEFAULT 'image'
    CHECK (media_kind IN ('image', 'audio', 'voice')),
ADD COLUMN duration_seconds integer NULL CHECK (duration_seconds > 0),
ADD COLUMN channels smallint NULL CHECK (channels > 0),
ADD COLUMN sample_rate integer NULL CHECK (sample_rate > 0),
ADD COLUMN canonical_path text NULL,
ADD COLUMN canonical_mime_type text NULL;

-- новый derivative попадает в прежнюю безопасную cleanup очередь при любом delete media
CREATE OR REPLACE FUNCTION queue_media_file_cleanup() RETURNS trigger AS $$
BEGIN
    INSERT INTO media_cleanup_tasks (relative_path)
    SELECT path
    FROM unnest(ARRAY[OLD.path, OLD.display_path, OLD.preview_path, OLD.canonical_path]) AS path
    WHERE path IS NOT NULL AND path <> ''
    ON CONFLICT (relative_path) DO NOTHING;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
