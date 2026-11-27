-- единый лимит вложения заменяет старое различие фото и остальных файлов
UPDATE site_settings
SET value = (
    value - 'max_photo_size_mib' - 'max_other_media_size_mib'
) || jsonb_build_object(
    'max_attachment_size_mib', 20,
    'uploads_enabled', true
)
WHERE key = 'media';

-- существующие доски сохраняют более строгий из двух старых лимитов
UPDATE boards
SET media_overrides = CASE
    WHEN media_overrides ? 'max_attachment_size_mib' THEN media_overrides
    WHEN media_overrides ? 'max_photo_size_mib'
      OR media_overrides ? 'max_other_media_size_mib'
    THEN (
        media_overrides - 'max_photo_size_mib' - 'max_other_media_size_mib'
    ) || jsonb_build_object(
        'max_attachment_size_mib', LEAST(
            COALESCE((media_overrides ->> 'max_photo_size_mib')::integer, 20),
            COALESCE((media_overrides ->> 'max_other_media_size_mib')::integer, 20)
        )
    )
    ELSE media_overrides
END;
