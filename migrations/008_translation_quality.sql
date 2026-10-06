ALTER TABLE translation_batch_items
 ADD COLUMN IF NOT EXISTS quality jsonb;
