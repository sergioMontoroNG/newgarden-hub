-- Server-side translation campaigns: the SKU list and options are persisted and
-- processed by a background runner, so progress survives closing the browser.
CREATE TABLE IF NOT EXISTS translation_campaigns (
 id bigserial PRIMARY KEY,
 state text NOT NULL DEFAULT 'RESOLVING',
 options jsonb NOT NULL DEFAULT '{}',
 skus text[] NOT NULL DEFAULT '{}',
 resolved boolean NOT NULL DEFAULT false,
 resolved_count integer NOT NULL DEFAULT 0,
 cancel_requested boolean NOT NULL DEFAULT false,
 error text,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS translation_campaign_items (
 id bigserial PRIMARY KEY,
 campaign_id bigint NOT NULL REFERENCES translation_campaigns(id) ON DELETE CASCADE,
 position integer NOT NULL DEFAULT 0,
 skus text[] NOT NULL,
 product_gid text,
 title text NOT NULL DEFAULT '',
 state text NOT NULL DEFAULT 'PENDING',
 batch_id bigint REFERENCES translation_batches(id) ON DELETE SET NULL,
 error text,
 started_at timestamptz,
 finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS translation_campaign_items_queue_idx
 ON translation_campaign_items(campaign_id,state,position);
CREATE INDEX IF NOT EXISTS translation_campaigns_active_idx
 ON translation_campaigns(state,id);
