ALTER TABLE media
DROP CONSTRAINT media_sticker_format_check,
DROP CONSTRAINT media_ready_sticker_derivatives_check,
ADD CONSTRAINT media_sticker_format_check CHECK (
    media_kind <> 'sticker' OR (
        posting_claim_id IS NOT NULL AND board_id IS NOT NULL
        AND (
            (NOT is_animation AND mime_type = 'image/webp' AND size BETWEEN 1 AND 524288)
            OR (is_animation AND mime_type = 'application/x-tgsticker' AND size BETWEEN 1 AND 65536)
            OR (is_animation AND mime_type = 'video/webm' AND size BETWEEN 1 AND 262144
                AND channels IS NULL AND sample_rate IS NULL)
        )
    )
),
ADD CONSTRAINT media_ready_sticker_derivatives_check CHECK (
    media_kind <> 'sticker' OR processing_state <> 'ready' OR (
        (NOT is_animation AND display_path IS NOT NULL AND display_path = 'display/' || id || '.webp')
        OR (
            is_animation AND display_path IS NULL
            AND preview_path IS NOT NULL AND preview_path = 'previews/' || id || '.webp'
            AND canonical_path IS NOT NULL AND canonical_path = 'stickers/' || id ||
                CASE mime_type WHEN 'video/webm' THEN '.webm' ELSE '.tgs' END
            AND canonical_mime_type IS NOT NULL AND canonical_mime_type = mime_type
            AND duration_seconds IS NOT NULL AND duration_seconds BETWEEN 1 AND 3
        )
    )
);

-- выбираем путь по формату, даже если транзакция не успела записать derivative
CREATE OR REPLACE FUNCTION queue_media_file_cleanup() RETURNS trigger AS $$
BEGIN
    INSERT INTO media_cleanup_tasks (relative_path)
    SELECT DISTINCT path
    FROM unnest(ARRAY[
        OLD.path, OLD.display_path, OLD.preview_path, OLD.canonical_path,
        CASE WHEN OLD.media_kind IN ('video', 'video_note') THEN 'video/' || OLD.id || '.mp4' END,
        CASE WHEN OLD.media_kind IN ('video', 'video_note')
             OR (OLD.media_kind = 'sticker' AND OLD.is_animation)
             THEN 'previews/' || OLD.id || '.webp' END,
        CASE WHEN OLD.media_kind = 'sticker' AND NOT OLD.is_animation
             THEN 'display/' || OLD.id || '.webp' END,
        CASE WHEN OLD.media_kind = 'sticker' AND OLD.is_animation
             THEN 'stickers/' || OLD.id ||
                 CASE OLD.mime_type WHEN 'video/webm' THEN '.webm' ELSE '.tgs' END END
    ]) AS path
    WHERE path IS NOT NULL AND path <> ''
    ON CONFLICT (relative_path) DO NOTHING;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
