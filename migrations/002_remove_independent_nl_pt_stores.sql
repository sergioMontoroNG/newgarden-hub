-- NL and PT remain enabled as locales of ES. Remove only their standalone store records.
UPDATE localization_jobs
SET state='CANCELLED', reason='Tienda independiente NL/PT retirada de la app', updated_at=now()
WHERE store_code IN ('NL','PT') AND state IN ('PENDING','BLOCKED_MAPPING','RUNNING');

DELETE FROM store_variants WHERE store_code IN ('NL','PT');
DELETE FROM store_products WHERE store_code IN ('NL','PT');
DELETE FROM field_rules WHERE store_code IN ('NL','PT');
DELETE FROM store_locales WHERE store_code IN ('NL','PT');
DELETE FROM stores WHERE code IN ('NL','PT');
