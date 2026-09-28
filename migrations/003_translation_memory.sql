CREATE TABLE IF NOT EXISTS translation_glossary (
 id bigserial PRIMARY KEY,
 source_locale text NOT NULL,
 target_locale text NOT NULL,
 source_term text NOT NULL,
 target_term text NOT NULL,
 enabled boolean NOT NULL DEFAULT true,
 origin text NOT NULL DEFAULT 'manual',
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_locale,target_locale,source_term)
);

CREATE TABLE IF NOT EXISTS translation_memory (
 id bigserial PRIMARY KEY,
 source_locale text NOT NULL,
 target_locale text NOT NULL,
 source_text text NOT NULL,
 target_text text NOT NULL,
 origin text NOT NULL DEFAULT 'manual_correction',
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_locale,target_locale,source_text)
);
