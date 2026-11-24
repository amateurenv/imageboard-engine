-- Название сайта, описание, правила
INSERT INTO site_settings (key, value)
VALUES (
    'site',
    '{"title": "Imageboard Engine", "description": "", "rules": ""}'::jsonb
)
ON CONFLICT (key) DO NOTHING;


-- Расширение группы appearance: логотипы, фоны, CSS-переменные
INSERT INTO site_settings (key, value)
VALUES (
    'appearance',
    '{"active_theme": "dark", "logo_path": null, "favicon_path": null, "background_path": null, "style_vars": {}}'::jsonb
)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
