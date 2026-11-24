-- файлы медиа: оригиналы и превью на диске (media/originals, media/previews)
CREATE TABLE media (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- прикрепление к посту (доступно при создании постов с медиа — Stage 7+)
    post_id bigint NULL REFERENCES posts (id) ON DELETE CASCADE,
    -- путь относительно корня диска, например originals/<uuid>.jpg
    path text NOT NULL,
    size bigint NOT NULL CHECK (size >= 0),
    mime_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT media_path_not_empty CHECK (char_length(path) > 0)
);
CREATE INDEX media_post_idx ON media (post_id);
