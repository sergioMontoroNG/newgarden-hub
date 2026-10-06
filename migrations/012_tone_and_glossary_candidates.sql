-- El trato se mantiene por tienda según el contenido publicado: FR (vous) y DE (Sie)
-- son formales; el resto, de tú.
UPDATE translation_profiles SET formality='prefer_more',updated_at=now(),
 notes=COALESCE(notes||' · ','')||'Trato formal (vous/Sie) como el contenido publicado'
 WHERE (store_code,shopify_locale) IN (('FR','fr'),('DE','de'));

-- Candidatos de glosario obtenidos del catálogo (staging). Nada pasa a translation_glossary sin aprobación.
CREATE TABLE IF NOT EXISTS glossary_candidates (
 id bigserial PRIMARY KEY,
 target_store text NOT NULL,
 target_locale text NOT NULL,
 source_term text NOT NULL,
 target_term text NOT NULL,
 kind text NOT NULL CHECK (kind IN ('term','invariant')),
 support integer NOT NULL,
 source_products integer NOT NULL,
 dice real NOT NULL,
 alternatives jsonb NOT NULL DEFAULT '[]',
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(target_store,target_locale,source_term)
);
