"""Extrae pares ES -> idioma de tienda del catálogo publicado (solo lectura sobre Shopify).

Uso: python -m hub.extract [TIENDA ...]
Guarda los pares en historical_pairs; no toca memoria, glosario ni Shopify.
"""
import re
import sys
import time
from collections import Counter

from fastapi import HTTPException

from .app import shopify_graphql, shopify_stores
from .db import initialize, pool
from .profiles import active_profiles

PAGE_SIZE = 25
# Claves de traducción de Shopify para producto; algunas versiones usan seo_*.
FIELDS = {'title': ('title',), 'body_html': ('body_html',),
          'seo_title': ('meta_title', 'seo_title'), 'seo_description': ('meta_description', 'seo_description')}


def _query(locale):
    tr = f' translations(locale:"{locale}") {{ key value outdated }}' if locale else ''
    return ('query($cursor:String){ products(first:%d,after:$cursor,query:"status:active") { nodes { id handle title descriptionHtml '
            'seo { title description } variants(first:20) { nodes { sku barcode } }%s } pageInfo { hasNextPage endCursor } } }' % (PAGE_SIZE, tr))


def _graphql(store, query, variables):
    for attempt in range(6):
        try:
            return shopify_graphql(store['domain'], store['token'], query, variables)
        except HTTPException as exc:
            if attempt == 5 or 'Throttled' not in str(exc.detail) and 'HTTP 429' not in str(exc.detail):
                raise
            time.sleep(2 ** attempt)


def _base_fields(node):
    base = {'title': node['title'], 'body_html': node['descriptionHtml'],
            'seo_title': (node.get('seo') or {}).get('title'),
            'seo_description': (node.get('seo') or {}).get('description')}
    return {f: (v, False) if (v or '').strip() else None for f, v in base.items()}


def fetch_products(store, translation_locale=None):
    """Todos los productos activos. Con translation_locale usa la traducción y, si falta un campo,
    el campo base (en UK hay productos con el inglés en la traducción y otros directamente en la base)."""
    cursor, out = None, []
    while True:
        data = _graphql(store, _query(translation_locale), {'cursor': cursor})['products']
        for node in data['nodes']:
            fields = _base_fields(node)
            if translation_locale:
                tr = {t['key']: t for t in node.get('translations', [])}
                for field, keys in FIELDS.items():
                    hit = next((tr[k] for k in keys if k in tr and (tr[k].get('value') or '').strip()), None)
                    if hit:
                        fields[field] = (hit['value'], bool(hit.get('outdated')))
            variants = node['variants']['nodes']
            keys = ([('sku', v['sku'].strip()) for v in variants if (v.get('sku') or '').strip()]
                    + [('ean', v['barcode'].strip()) for v in variants if (v.get('barcode') or '').strip()]
                    + [('handle', node['handle'])])
            skus = [k for t, k in keys if t == 'sku']
            out.append({'gid': node['id'], 'keys': keys, 'skus': skus, 'fields': fields})
        if not data['pageInfo']['hasNextPage']:
            return out
        cursor = data['pageInfo']['endCursor']


_ES_WORDS = {'el', 'la', 'los', 'las', 'de', 'del', 'con', 'para', 'y', 'un', 'una', 'que', 'tu', 'su', 'por'}


def looks_spanish(text):
    """Heurística: el texto sigue en español (traducción ausente o sin hacer)."""
    words = re.findall(r"[a-záéíóúñü]+", re.sub('<[^>]+>', ' ', text).lower())
    return len(words) >= 6 and sum(w in _ES_WORDS for w in words) / len(words) > 0.22


def main(only):
    initialize()
    stores = {s['code']: s for s in shopify_stores() if s['token']}
    es = stores['ES']
    # Shopify devuelve el contenido español como campos base de la tienda ES.
    source = fetch_products(es)
    index = {}
    for product in source:
        for key in product['keys']:
            index.setdefault(key, product)
    print(f'ES: {len(source)} productos activos', flush=True)
    for code, store in sorted(stores.items()):
        if code == 'ES' or (only and code not in only):
            continue
        for locale, profile in active_profiles(code).items():
            if not profile['deepl_target']:
                continue
            target = fetch_products(store, locale if profile['role'] == 'translation' else None)
            stats = Counter(products=len(target))
            seen = set()
            with pool.connection() as c:
                for product in target:
                    match = next((index[k] for k in product['keys'] if k in index), None)
                    if not match:
                        stats['sin_correspondencia'] += 1
                        continue
                    # Una fila por producto ES: la clave es su primer SKU (o handle si no tiene).
                    sku = match['skus'][0] if match['skus'] else match['keys'][-1][1]
                    if sku in seen:
                        stats['producto_repetido'] += 1
                        continue
                    seen.add(sku)
                    stats['emparejados'] += 1
                    for field, tgt in product['fields'].items():
                        src = match['fields'][field]
                        if not tgt or not src:
                            stats[f'sin_{field}'] += 1
                            continue
                        same = tgt[0].strip() == src[0].strip()
                        # Destino español (MX): solo interesa lo que difiere de España.
                        if (same if locale.split('-')[0] == 'es' else looks_spanish(tgt[0]) or (len(tgt[0]) > 40 and same)):
                            stats[f'sin_traducir_{field}'] += 1
                            continue
                        c.execute("""INSERT INTO historical_pairs(target_store,target_locale,sku,source_product_gid,target_product_gid,field_key,source_text,target_text,outdated)
                            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT(target_store,target_locale,sku,field_key) DO UPDATE SET
                              source_product_gid=EXCLUDED.source_product_gid,target_product_gid=EXCLUDED.target_product_gid,
                              source_text=EXCLUDED.source_text,target_text=EXCLUDED.target_text,outdated=EXCLUDED.outdated,extracted_at=now()""",
                                  (code, locale, sku, match['gid'], product['gid'], field, src[0], tgt[0], tgt[1]))
                        stats[f'pares_{field}'] += 1
            print(code, locale, profile['role'], dict(stats), flush=True)


if __name__ == '__main__':
    main(set(sys.argv[1:]))
