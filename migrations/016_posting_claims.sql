-- claim хранит только hash одноразового секрета, исходный token в базу не попадает
CREATE TABLE posting_claims (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    board_id bigint NOT NULL REFERENCES boards (id) ON DELETE CASCADE,
    account_user_id bigint NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz NULL,
    max_attachments integer NOT NULL CHECK (max_attachments BETWEEN 1 AND 20)
);

CREATE INDEX posting_claims_expiry_idx
ON posting_claims (expires_at) WHERE consumed_at IS NULL;

-- файл до публикации принадлежит ровно одному claim и уже знает целевую доску
ALTER TABLE media
ADD COLUMN board_id bigint NULL REFERENCES boards (id) ON DELETE CASCADE,
ADD COLUMN posting_claim_id bigint NULL REFERENCES posting_claims (id) ON DELETE CASCADE,
ADD COLUMN attachment_position integer NULL CHECK (attachment_position >= 0);

CREATE INDEX media_claim_unattached_idx
ON media (posting_claim_id, id) WHERE post_id IS NULL;

CREATE UNIQUE INDEX media_post_attachment_position_unique_idx
ON media (post_id, attachment_position) WHERE post_id IS NOT NULL;

INSERT INTO site_settings (key, value)
VALUES ('posting_claims', '{"ttl_minutes": 30}'::jsonb)
ON CONFLICT (key) DO NOTHING;
