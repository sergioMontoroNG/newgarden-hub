-- USA publica "en" como idioma principal (base), no como traducción; su "es" no está publicado.
UPDATE translation_profiles SET role='base',notes='Inglés estadounidense, idioma principal de la tienda; distinto de UK'
 WHERE store_code='USA' AND shopify_locale='en';
INSERT INTO translation_profiles(store_code,shopify_locale,role,deepl_target,active,notes)
SELECT 'USA','es','translation',NULL,false,'No publicado; ignorar'
WHERE EXISTS (SELECT 1 FROM stores WHERE code='USA')
ON CONFLICT DO NOTHING;

-- Pares ES -> idioma de tienda extraídos del catálogo publicado (solo lectura sobre Shopify).
-- Es un área de staging: nada de aquí llega a memoria o glosario sin revisión.
CREATE TABLE IF NOT EXISTS historical_pairs (
 id bigserial PRIMARY KEY,
 target_store text NOT NULL,
 target_locale text NOT NULL,
 sku text NOT NULL,
 source_product_gid text NOT NULL,
 target_product_gid text NOT NULL,
 field_key text NOT NULL,
 source_text text NOT NULL,
 target_text text NOT NULL,
 outdated boolean NOT NULL DEFAULT false,
 extracted_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(target_store,target_locale,sku,field_key)
);
