-- скрытие треда обратимо и не уничтожает данные или журнал действий
ALTER TABLE threads ADD COLUMN is_hidden boolean NOT NULL DEFAULT false;
CREATE INDEX threads_public_visibility_idx ON threads (board_id, is_hidden, is_archived);

-- metadata хранит тип актора и контекст действия без секретов и содержимого постов
ALTER TABLE moderation_log ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE moderation_log ADD CONSTRAINT moderation_log_metadata_object
    CHECK (jsonb_typeof(metadata) = 'object');
