CREATE TABLE media_disk_inventory (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    volume_id uuid NOT NULL
);

CREATE TABLE media_disk_files (
    path text PRIMARY KEY CHECK (path <> ''),
    owner_path text NOT NULL CHECK (owner_path <> ''),
    media_id bigint,
    bytes bigint NOT NULL CHECK (bytes >= 0),
    managed boolean NOT NULL DEFAULT true,
    work_bytes bigint NOT NULL DEFAULT 0 CHECK (work_bytes >= 0),
    expires_at timestamptz,
    work_paths text[] NOT NULL DEFAULT '{}'
);

CREATE INDEX media_disk_files_media_idx ON media_disk_files (media_id);
CREATE INDEX media_disk_files_expiry_idx ON media_disk_files (expires_at)
WHERE expires_at IS NOT NULL;

ALTER TABLE media ADD COLUMN processing_next_attempt_at timestamptz;
ALTER TABLE media_cleanup_tasks
    ADD COLUMN next_attempt_at timestamptz,
    ADD COLUMN quota_owner_path text;

-- используем сохранённые пути, а незавершённые записи берём из реестра
CREATE OR REPLACE FUNCTION queue_media_file_cleanup() RETURNS trigger AS $$
BEGIN
    INSERT INTO media_cleanup_tasks (relative_path)
    SELECT DISTINCT path
    FROM unnest(ARRAY[OLD.path, OLD.display_path, OLD.preview_path, OLD.canonical_path]) AS path
    WHERE path IS NOT NULL AND path <> ''
    ON CONFLICT (relative_path) DO NOTHING;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

-- дополнительные пути остаются в очереди даже после удаления media
CREATE FUNCTION queue_reserved_media_cleanup() RETURNS trigger AS $$
BEGIN
    INSERT INTO media_cleanup_tasks (relative_path, quota_owner_path)
    SELECT DISTINCT path, OLD.path
    FROM (
        SELECT path FROM media_disk_files WHERE media_id = OLD.id
        UNION
        SELECT unnest(work_paths) FROM media_disk_files WHERE media_id = OLD.id
    ) AS paths
    ON CONFLICT (relative_path) DO UPDATE SET
        quota_owner_path = EXCLUDED.quota_owner_path,
        completed_at = NULL;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER media_file_quota_cleanup_on_delete
BEFORE DELETE ON media
FOR EACH ROW EXECUTE FUNCTION queue_reserved_media_cleanup();
