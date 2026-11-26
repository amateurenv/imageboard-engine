-- добавляем недостающие ключи appearance без замены выбранных владельцем значений
UPDATE site_settings
SET value = (
    '{
        "active_theme": "dark",
        "logo_path": null,
        "favicon_path": null,
        "background_path": null,
        "style_vars": {}
    }'::jsonb || value
),
    updated_at = now()
WHERE key = 'appearance';
