ALTER TABLE media
DROP CONSTRAINT media_media_kind_check,
ADD CONSTRAINT media_media_kind_check
    CHECK (media_kind IN ('image', 'audio', 'voice', 'video', 'video_note')),
ADD CONSTRAINT media_ready_video_derivatives_check
    CHECK (
        media_kind NOT IN ('video', 'video_note') OR processing_state <> 'ready' OR (
            preview_path IS NOT NULL AND preview_path = 'previews/' || id || '.webp'
            AND canonical_path IS NOT NULL AND canonical_path = 'video/' || id || '.mp4'
            AND canonical_mime_type IS NOT NULL AND canonical_mime_type = 'video/mp4'
        )
    );

-- включить конечные пути видео даже при остановке worker между replace и commit
CREATE OR REPLACE FUNCTION queue_media_file_cleanup() RETURNS trigger AS $$
BEGIN
    INSERT INTO media_cleanup_tasks (relative_path)
    SELECT DISTINCT path
    FROM unnest(ARRAY[
        OLD.path, OLD.display_path, OLD.preview_path, OLD.canonical_path,
        CASE WHEN OLD.media_kind IN ('video', 'video_note') THEN 'video/' || OLD.id || '.mp4' END,
        CASE WHEN OLD.media_kind IN ('video', 'video_note') THEN 'previews/' || OLD.id || '.webp' END
    ]) AS path
    WHERE path IS NOT NULL AND path <> ''
    ON CONFLICT (relative_path) DO NOTHING;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
