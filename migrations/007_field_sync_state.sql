CREATE TABLE IF NOT EXISTS product_field_sync_state (
 source_product_gid text NOT NULL,
 store_code text NOT NULL,
 locale text NOT NULL,
 resource_id text NOT NULL,
 field_key text NOT NULL,
 source_hash text NOT NULL,
 target_hash text NOT NULL,
 synced_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_product_gid,store_code,locale,resource_id,field_key)
);
