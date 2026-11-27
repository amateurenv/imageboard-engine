ALTER TABLE posting_claims
ADD COLUMN sticker_thread_id bigint REFERENCES threads (id) ON DELETE CASCADE,
ADD CONSTRAINT posting_claims_sticker_check CHECK (
    sticker_thread_id IS NULL OR (account_user_id IS NOT NULL AND max_attachments = 1)
);

CREATE INDEX posting_claims_sticker_thread_idx
ON posting_claims (sticker_thread_id) WHERE sticker_thread_id IS NOT NULL;

ALTER TABLE media
DROP CONSTRAINT media_media_kind_check,
ADD CONSTRAINT media_media_kind_check
    CHECK (media_kind IN ('image', 'audio', 'voice', 'video', 'video_note', 'sticker')),
ADD CONSTRAINT media_sticker_format_check CHECK (
    media_kind <> 'sticker' OR (
        mime_type = 'image/webp' AND NOT is_animation AND size BETWEEN 1 AND 524288
        AND posting_claim_id IS NOT NULL AND board_id IS NOT NULL
    )
),
ADD CONSTRAINT media_ready_sticker_display_check CHECK (
    media_kind <> 'sticker' OR processing_state <> 'ready' OR (
        display_path IS NOT NULL AND display_path = 'display/' || id || '.webp'
    )
);

-- учесть display стикера, если worker остановился после замены файла до commit
CREATE OR REPLACE FUNCTION queue_media_file_cleanup() RETURNS trigger AS $$
BEGIN
    INSERT INTO media_cleanup_tasks (relative_path)
    SELECT DISTINCT path
    FROM unnest(ARRAY[
        OLD.path, OLD.display_path, OLD.preview_path, OLD.canonical_path,
        CASE WHEN OLD.media_kind IN ('video', 'video_note') THEN 'video/' || OLD.id || '.mp4' END,
        CASE WHEN OLD.media_kind IN ('video', 'video_note') THEN 'previews/' || OLD.id || '.webp' END,
        CASE WHEN OLD.media_kind = 'sticker' THEN 'display/' || OLD.id || '.webp' END
    ]) AS path
    WHERE path IS NOT NULL AND path <> ''
    ON CONFLICT (relative_path) DO NOTHING;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
