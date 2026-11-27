-- признак сохраняет только факт анимации для API и не раскрывает исходный файл
ALTER TABLE media
ADD COLUMN is_animation boolean NOT NULL DEFAULT FALSE;
