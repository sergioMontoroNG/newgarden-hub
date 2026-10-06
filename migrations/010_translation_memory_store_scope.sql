-- La memoria pasa a tener alcance de tienda destino: UK y USA comparten locale "en"
-- y sus correcciones no deben servirse entre sí.
ALTER TABLE translation_memory ADD COLUMN IF NOT EXISTS target_store text;

-- Solo se atribuyen filas cuyo locale identifica una única tienda.
UPDATE translation_memory SET target_store='DE' WHERE target_store IS NULL AND target_locale='de';
UPDATE translation_memory SET target_store='FR' WHERE target_store IS NULL AND target_locale='fr';
UPDATE translation_memory SET target_store='IT' WHERE target_store IS NULL AND target_locale='it';
UPDATE translation_memory SET target_store='ES' WHERE target_store IS NULL AND target_locale IN ('nl','pt-PT');
-- Las filas ambiguas (por ejemplo "en") quedan con target_store NULL y no se reutilizan.

ALTER TABLE translation_memory DROP CONSTRAINT IF EXISTS translation_memory_source_locale_target_locale_source_text_key;
CREATE UNIQUE INDEX IF NOT EXISTS translation_memory_store_text_idx
 ON translation_memory(target_store,source_locale,target_locale,source_text)
 WHERE target_store IS NOT NULL;
