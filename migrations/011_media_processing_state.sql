-- состояние хранится в postgresql, чтобы падение redis или worker не потеряло задачу
ALTER TABLE media
ADD COLUMN processing_state text NOT NULL DEFAULT 'queued',
ADD COLUMN processing_attempts integer NOT NULL DEFAULT 0,
ADD COLUMN processing_lease_until timestamptz NULL,
ADD COLUMN last_processing_error text NULL,
ADD CONSTRAINT media_processing_state_valid CHECK (
    processing_state IN ('queued', 'processing', 'ready', 'failed')
),
ADD CONSTRAINT media_processing_attempts_non_negative CHECK (processing_attempts >= 0);

-- worker регулярно выбирает ожидающие задачи и отдельно находит зависшие lease
CREATE INDEX media_processing_queue_idx
ON media (processing_state, created_at, id);

CREATE INDEX media_processing_lease_idx
ON media (processing_lease_until)
WHERE processing_state = 'processing';
