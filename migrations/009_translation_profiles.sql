-- Perfil de traducción por tienda y locale de Shopify. Fuente única para decidir
-- qué locales se usan, con qué destino/trato de DeepL y si exigen revisión humana.
-- Shopify no se modifica: la app resuelve aquí las diferencias (UK/USA ambos "en", MX con "en" sin uso...).
CREATE TABLE IF NOT EXISTS translation_profiles (
 store_code text NOT NULL REFERENCES stores(code),
 shopify_locale text NOT NULL,
 role text NOT NULL CHECK (role IN ('source','base','translation')),
 deepl_target text,
 formality text CHECK (formality IN ('default','more','less','prefer_more','prefer_less')),
 require_review boolean NOT NULL DEFAULT false,
 active boolean NOT NULL DEFAULT true,
 notes text,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (store_code, shopify_locale)
);

-- Trato de "tú" en todas las tiendas (prefer_less no falla si el idioma no admite formality).
INSERT INTO translation_profiles(store_code,shopify_locale,role,deepl_target,formality,require_review,active,notes)
SELECT v.store_code,v.shopify_locale,v.role,v.deepl_target,v.formality,v.require_review,v.active,v.notes
FROM (VALUES
 ('ES','es-ES','source',NULL,NULL,false,true,'España es la fuente de verdad'),
 ('ES','es','source',NULL,NULL,false,true,'España es la fuente de verdad'),
 ('ES','pt-PT','translation','PT-PT','prefer_less',true,true,'Traducción dentro de ES; sin corpus humano, revisión obligatoria'),
 ('ES','nl','translation','NL','prefer_less',true,true,'Traducción dentro de ES; sin corpus humano, revisión obligatoria'),
 ('FR','fr','base','FR','prefer_less',false,true,NULL),
 ('DE','de','base','DE','prefer_less',false,true,NULL),
 ('IT','it','base','IT','prefer_less',false,true,NULL),
 ('UK','en','translation','EN-GB','prefer_less',false,true,'Todo el contenido vigente está en la traducción en'),
 ('UK','es','base',NULL,NULL,false,false,'Principal publicado abandonado; ignorar'),
 ('USA','en','translation','EN-US','prefer_less',false,true,'Inglés estadounidense, distinto de UK'),
 ('MX','es','base','ES-419','prefer_less',false,true,'Único idioma usado; DeepL no admite ES-MX, se usa ES-419'),
 ('MX','en','translation',NULL,NULL,false,false,'Publicado pero no se usa; ignorar')
) AS v(store_code,shopify_locale,role,deepl_target,formality,require_review,active,notes)
WHERE EXISTS (SELECT 1 FROM stores s WHERE s.code=v.store_code)
ON CONFLICT DO NOTHING;

-- MX no tenía locale configurado, por lo que no entraba en comparaciones ni glosario.
INSERT INTO store_locales(store_code,locale,strategy)
SELECT 'MX','es','BASE' WHERE EXISTS (SELECT 1 FROM stores WHERE code='MX')
ON CONFLICT DO NOTHING;
