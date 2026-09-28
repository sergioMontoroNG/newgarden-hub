CREATE TABLE IF NOT EXISTS translation_batches (
 id bigserial PRIMARY KEY,
 source_sku text NOT NULL,
 source_product_gid text NOT NULL,
 source_title text NOT NULL DEFAULT '',
 state text NOT NULL DEFAULT 'PREPARING',
 error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS translation_batch_items (
 id bigserial PRIMARY KEY,
 batch_id bigint NOT NULL REFERENCES translation_batches(id) ON DELETE CASCADE,
 store_code text NOT NULL,
 locale text NOT NULL,
 field_key text NOT NULL,
 field_label text NOT NULL,
 source_text text NOT NULL DEFAULT '',
 proposed_text text,
 edit_payload jsonb NOT NULL DEFAULT '{}',
 state text NOT NULL DEFAULT 'PENDING_REVIEW',
 error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(batch_id,store_code,locale,field_key)
);

CREATE INDEX IF NOT EXISTS translation_batch_items_review_idx
 ON translation_batch_items(batch_id,state,id);
