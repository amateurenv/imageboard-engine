-- path остаётся совместимым путём quarantine, а производные файлы хранятся отдельно
ALTER TABLE media
ADD COLUMN display_path text NULL,
ADD COLUMN preview_path text NULL;

-- удаление записи не удаляет путь сразу: worker повторит безопасную очистку после сбоя диска
CREATE TABLE media_cleanup_tasks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    relative_path text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error text NULL,
    lease_until timestamptz NULL,
    completed_at timestamptz NULL,
    CONSTRAINT media_cleanup_path_not_empty CHECK (char_length(relative_path) > 0)
);

CREATE INDEX media_cleanup_pending_idx
ON media_cleanup_tasks (created_at, id)
WHERE completed_at IS NULL AND lease_until IS NULL;

-- каскадное удаление поста, треда или claim оставляет задачи для каждого известного файла
CREATE OR REPLACE FUNCTION queue_media_file_cleanup() RETURNS trigger AS $$
BEGIN
    INSERT INTO media_cleanup_tasks (relative_path)
    SELECT path
    FROM unnest(ARRAY[OLD.path, OLD.display_path, OLD.preview_path]) AS path
    WHERE path IS NOT NULL AND path <> ''
    ON CONFLICT (relative_path) DO NOTHING;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER media_file_cleanup_on_delete
BEFORE DELETE ON media
FOR EACH ROW EXECUTE FUNCTION queue_media_file_cleanup();
