-- добавляем лимиты публикаций, не перезаписывая уже выбранные значения
UPDATE site_settings
SET value = value || '{"max_threads_per_board_day": 3}'::jsonb
WHERE key = 'thread'
  AND NOT value ? 'max_threads_per_board_day';


UPDATE site_settings
SET value = value || '{"max_posts_per_thread_hour": 10}'::jsonb
WHERE key = 'post'
  AND NOT value ? 'max_posts_per_thread_hour';
