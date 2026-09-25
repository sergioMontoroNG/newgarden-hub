CREATE TABLE IF NOT EXISTS stores (
 code text PRIMARY KEY, domain text, config jsonb NOT NULL DEFAULT '{}', active boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS store_locales (
 id bigserial PRIMARY KEY, store_code text NOT NULL REFERENCES stores(code), locale text NOT NULL,
 market_id text, strategy text NOT NULL CHECK(strategy IN ('BASE','TRANSLATION')),
 UNIQUE(store_code, locale)
);
CREATE TABLE IF NOT EXISTS global_products (
 product_key text PRIMARY KEY, source jsonb NOT NULL, source_hash text NOT NULL, source_version integer NOT NULL DEFAULT 1,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS store_products (
 product_key text REFERENCES global_products(product_key), store_code text REFERENCES stores(code),
 shopify_product_gid text, current_handle text, mapping_status text NOT NULL DEFAULT 'UNCONFIRMED',
 PRIMARY KEY(product_key, store_code)
);
CREATE TABLE IF NOT EXISTS store_variants (
 id bigserial PRIMARY KEY, product_key text NOT NULL, store_code text NOT NULL, source_variant_gid text,
 target_variant_gid text, sku text, mapping_status text NOT NULL DEFAULT 'UNCONFIRMED'
);
CREATE TABLE IF NOT EXISTS field_rules (
 id bigserial PRIMARY KEY, store_code text NOT NULL, locale text NOT NULL, resource text NOT NULL,
 field text NOT NULL, action text NOT NULL CHECK(action IN ('LOCALIZE','GLOSSARY','COPY','MARKET_MAP','IGNORE')),
 config jsonb NOT NULL DEFAULT '{}', version integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS localization_jobs (
 id bigserial PRIMARY KEY, product_key text REFERENCES global_products(product_key), store_code text,
 locale text, source_version integer NOT NULL, state text NOT NULL DEFAULT 'PENDING', reason text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(product_key,store_code,locale,source_version)
);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON localization_jobs(state,id);
CREATE TABLE IF NOT EXISTS localization_results (
 id bigserial PRIMARY KEY, job_id bigint NOT NULL REFERENCES localization_jobs(id), input jsonb NOT NULL,
 output jsonb, source_hash text NOT NULL, prompt_version text NOT NULL, rules_version text NOT NULL,
 validation jsonb NOT NULL DEFAULT '{}', approved_fields jsonb, approved_by text,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS publication_records (
 id bigserial PRIMARY KEY, job_id bigint NOT NULL REFERENCES localization_jobs(id), result_id bigint NOT NULL REFERENCES localization_results(id),
 fields jsonb NOT NULL, previous_values jsonb NOT NULL DEFAULT '{}', outcomes jsonb NOT NULL DEFAULT '{}',
 shopify_gid text, simulated boolean NOT NULL DEFAULT true, verified_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS process_control (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), state text NOT NULL DEFAULT 'STOPPED',
 last_incident jsonb, resumed_by text, updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO process_control(singleton,state) VALUES(true,'STOPPED') ON CONFLICT DO NOTHING;
INSERT INTO stores(code,active) VALUES ('ES',true),('FR',false),('DE',false),('IT',false),('UK',false),('MX',false) ON CONFLICT DO NOTHING;
INSERT INTO store_locales(store_code,locale,strategy) VALUES ('ES','es-ES','BASE'),('ES','pt-PT','TRANSLATION'),('ES','nl-NL','TRANSLATION'),('FR','fr-FR','BASE'),('DE','de-DE','BASE'),('IT','it-IT','BASE'),('UK','en-GB','BASE'),('MX','es-MX','BASE') ON CONFLICT DO NOTHING;
