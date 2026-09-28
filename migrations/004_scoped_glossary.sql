ALTER TABLE translation_glossary ADD COLUMN IF NOT EXISTS source_store text;
ALTER TABLE translation_glossary ADD COLUMN IF NOT EXISTS target_store text;

ALTER TABLE translation_glossary DROP CONSTRAINT IF EXISTS translation_glossary_source_locale_target_locale_source_term_key;
ALTER TABLE translation_glossary DROP CONSTRAINT IF EXISTS translation_glossary_source_locale_target_locale_source_ter_key;
DROP INDEX IF EXISTS translation_glossary_scoped_term_idx;
CREATE UNIQUE INDEX translation_glossary_scoped_term_idx
 ON translation_glossary(source_store,source_locale,target_store,target_locale,lower(source_term))
 WHERE source_store IS NOT NULL AND target_store IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS translation_glossary_global_term_idx
 ON translation_glossary(source_locale,target_locale,lower(source_term))
 WHERE source_store IS NULL AND target_store IS NULL;
ALTER TABLE translation_glossary ADD CONSTRAINT translation_glossary_store_scope_check
 CHECK ((source_store IS NULL) = (target_store IS NULL));
