-- opaque key не связан с id, post_id или путём и появляется только после attach ready media
ALTER TABLE media
ADD COLUMN public_key text NULL,
ADD CONSTRAINT media_public_key_format CHECK (
    public_key IS NULL OR public_key ~ '^[A-Za-z0-9_-]{32,128}$'
);

CREATE UNIQUE INDEX media_public_key_unique_idx
ON media (public_key)
WHERE public_key IS NOT NULL;
