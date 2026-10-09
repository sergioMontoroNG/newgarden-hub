"""Estimate how many characters the catalog would send to a translation engine.

Read-only: it exports the Spanish (ES) catalog with a Shopify bulk operation and
counts the source text of the fields the hub translates. It writes nothing to Shopify
and calls no translation engine (only DeepL's free /v2/usage endpoint, if configured).

Run it inside the app container, which has the database, store tokens and DeepL key:

    docker compose exec -T app python - --help < scripts/estimate_chars.py
    docker compose exec -T app python - < scripts/estimate_chars.py
    docker compose exec -T app python - --limit 200 --json < scripts/estimate_chars.py
"""
import argparse, html, json, re, sys, time, urllib.request
from collections import Counter

from hub.app import shopify_stores, shopify_graphql, shopify_shop_locales
from hub.profiles import active_profiles

TEXT_METAFIELD_TYPES = {'single_line_text_field', 'multi_line_text_field', 'html', 'rich_text_field', 'list.single_line_text_field', 'list.multi_line_text_field'}
BULK_QUERY = '''{ products { edges { node { id title descriptionHtml seo { title description }
  metafields { edges { node { namespace key type value } } }
  variants { edges { node { id sku metafields { edges { node { namespace key type value } } } } } } } } } }'''


def text_len(value, is_html=False):
    """Characters a translation engine would bill: tags excluded for HTML."""
    if not value:
        return 0
    if is_html:
        value = html.unescape(re.sub(r'<[^>]*>', '', value))
    return len(value.strip())


def metafield_chars(mf):
    """(text characters, raw characters) of a text metafield, following how the hub translates each type."""
    kind, value = mf.get('type'), mf.get('value') or ''
    if kind not in TEXT_METAFIELD_TYPES or not value.strip():
        return 0, 0
    if kind == 'html':
        return text_len(value, True), len(value)
    if kind == 'rich_text_field' or kind.startswith('list.'):
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return 0, 0
        total = 0
        def walk(node):
            nonlocal total
            if isinstance(node, str):
                total += len(node.strip())
            elif isinstance(node, dict):
                if node.get('type') == 'text' and isinstance(node.get('value'), str):
                    total += len(node['value'].strip())
                else:
                    for child in node.values():
                        walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
        walk(data)
        return total, total
    return len(value.strip()), len(value)


def is_yotpo(mf):
    return 'yotpo' in (str(mf.get('namespace', '')) + str(mf.get('key', ''))).casefold()


def run_bulk_export():
    store = next((s for s in shopify_stores() if s['code'] == 'ES' and s['token']), None)
    if not store:
        sys.exit('La tienda ES no tiene dominio y token configurados')
    domain_token = (store['domain'], store['token'])
    current = shopify_graphql(*domain_token, 'query { currentBulkOperation(type: QUERY) { id status } }').get('currentBulkOperation')
    if current and current['status'] in ('CREATED', 'RUNNING'):
        sys.exit('Ya hay una exportación masiva en curso en Shopify; espera a que termine y vuelve a intentarlo.')
    started = shopify_graphql(*domain_token, 'mutation($q:String!) { bulkOperationRunQuery(query:$q) { bulkOperation { id status } userErrors { field message } } }', {'q': BULK_QUERY})['bulkOperationRunQuery']
    if started['userErrors']:
        sys.exit('Shopify rechazó la exportación: ' + json.dumps(started['userErrors'], ensure_ascii=False))
    print('Exportando el catálogo de ES con una operación masiva de Shopify (solo lectura)…', file=sys.stderr)
    while True:
        time.sleep(3)
        op = shopify_graphql(*domain_token, 'query { currentBulkOperation(type: QUERY) { id status errorCode objectCount url } }')['currentBulkOperation']
        print(f"  estado: {op['status']} · objetos: {op.get('objectCount')}", file=sys.stderr)
        if op['status'] in ('COMPLETED',):
            return op.get('url')
        if op['status'] in ('FAILED', 'CANCELED', 'EXPIRED'):
            sys.exit(f"La exportación terminó con estado {op['status']} ({op.get('errorCode')})")


def target_pairs():
    """Same rule as the batch creator: published, editable locales with a DeepL profile."""
    pairs = []
    for store in shopify_stores():
        if not store['token']:
            continue
        try:
            locales = shopify_shop_locales(store)
        except Exception as exc:
            print(f"  aviso: {store['code']}: {getattr(exc, 'detail', exc)}", file=sys.stderr)
            continue
        profiles = active_profiles(store['code'])
        for item in locales:
            profile = profiles.get(item['locale'].lower()) or {}
            if item.get('published') and not (store['code'] == 'ES' and item.get('primary')) and profile.get('deepl_target'):
                pairs.append({'store': store['code'], 'locale': item['locale'], 'deepl_target': profile['deepl_target']})
    return pairs


def deepl_usage():
    import os
    key = os.getenv('DEEPL_AUTH_KEY', '').strip()
    if not key:
        return None
    host = 'https://api-free.deepl.com' if key.endswith(':fx') else 'https://api.deepl.com'
    try:
        request = urllib.request.Request(host + '/v2/usage', headers={'Authorization': 'DeepL-Auth-Key ' + key})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--limit', type=int, default=0, help='contar solo los primeros N productos (muestra)')
    parser.add_argument('--include-title', action='store_true', help='incluir el título del producto (el lote no lo traduce)')
    parser.add_argument('--skip', default='', help='grupos a excluir, separados por coma: description,seo,product_metafields,variant_metafields')
    parser.add_argument('--rerun-factor', type=float, default=1.2, help='margen por regeneraciones y reintentos (1.2 = +20%%)')
    parser.add_argument('--json', action='store_true', help='salida JSON en vez de tabla')
    args = parser.parse_args()
    skip = {item.strip() for item in args.skip.split(',') if item.strip()}

    pairs = target_pairs()
    url = run_bulk_export()
    groups = {k: {'items': 0, 'chars': 0, 'raw': 0} for k in ('title', 'description', 'seo_title', 'seo_description', 'product_metafields', 'variant_metafields')}
    per_metafield = Counter()
    products = variants = 0
    if url:
        with urllib.request.urlopen(url, timeout=120) as response:
            for line in response:
                row = json.loads(line)
                parent = row.get('__parentId')
                if 'namespace' in row:
                    group = 'variant_metafields' if parent and '/ProductVariant/' in parent else 'product_metafields'
                    if group in skip or is_yotpo(row):
                        continue
                    chars, raw = metafield_chars(row)
                    if chars:
                        groups[group]['items'] += 1; groups[group]['chars'] += chars; groups[group]['raw'] += raw
                        per_metafield[(group, f"{row['namespace']}.{row['key']}")] += chars
                elif '/ProductVariant/' in row.get('id', ''):
                    variants += 1
                elif '/Product/' in row.get('id', ''):
                    if args.limit and products >= args.limit:
                        break
                    products += 1
                    entries = [('description', text_len(row.get('descriptionHtml'), True), len(row.get('descriptionHtml') or '')),
                               ('seo_title', text_len((row.get('seo') or {}).get('title')), len((row.get('seo') or {}).get('title') or '')),
                               ('seo_description', text_len((row.get('seo') or {}).get('description')), len((row.get('seo') or {}).get('description') or ''))]
                    if args.include_title:
                        entries.append(('title', text_len(row.get('title')), len(row.get('title') or '')))
                    for name, chars, raw in entries:
                        if chars and name.split('_')[0] not in skip and name not in skip:
                            groups[name]['items'] += 1; groups[name]['chars'] += chars; groups[name]['raw'] += raw

    per_language = sum(g['chars'] for k, g in groups.items() if k != 'title' or args.include_title)
    raw_per_language = sum(g['raw'] for k, g in groups.items() if k != 'title' or args.include_title)
    n_pairs = len(pairs)
    total = per_language * n_pairs
    report = {
        'products_counted': products, 'variants_seen': variants, 'sample': bool(args.limit),
        'target_pairs': pairs, 'groups': groups,
        'chars_per_target_pair': per_language, 'chars_per_target_pair_with_tags': raw_per_language,
        'initial_load_chars': total, 'initial_load_with_rerun_margin': round(total * args.rerun_factor),
        'monthly_if_catalog_changes': {f'{p}%': round(total * p / 100 * args.rerun_factor) for p in (2, 5, 10)},
        'top_metafields': [{'group': g, 'metafield': name, 'chars': c} for (g, name), c in per_metafield.most_common(10)],
        'deepl_usage': deepl_usage(),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    labels = {'title': 'Título', 'description': 'Descripción (sin etiquetas)', 'seo_title': 'SEO · título', 'seo_description': 'SEO · descripción',
              'product_metafields': 'Metacampos de producto', 'variant_metafields': 'Metacampos de variante'}
    fmt = lambda n: f'{n:,}'.replace(',', '.')
    print(f"\nProductos contados: {fmt(products)}{' (MUESTRA)' if args.limit else ''} · variantes: {fmt(variants)}")
    print(f"Pares tienda·locale destino con DeepL: {n_pairs}  ->  " + ', '.join(f"{p['store']}·{p['locale']}" for p in pairs))
    print(f"\n{'Campo':<32}{'Con texto':>12}{'Caracteres':>14}")
    for key, label in labels.items():
        g = groups[key]
        if key == 'title' and not args.include_title:
            continue
        print(f"{label:<32}{fmt(g['items']):>12}{fmt(g['chars']):>14}")
    print(f"\nCaracteres de origen por par destino: {fmt(per_language)} (con etiquetas HTML contadas: {fmt(raw_per_language)})")
    print(f"Carga inicial (x{n_pairs} pares): {fmt(total)} caracteres")
    print(f"  con margen x{args.rerun_factor} por regeneraciones: {fmt(report['initial_load_with_rerun_margin'])}")
    print('Mantenimiento mensual si cambia ese % del catálogo (hipótesis, con margen): ' + ' · '.join(f'{k}: {fmt(v)}' for k, v in report['monthly_if_catalog_changes'].items()))
    if per_metafield:
        print('\nMetacampos con más texto:')
        for item in report['top_metafields']:
            print(f"  {fmt(item['chars']):>12}  {item['group'].split('_')[0]:<8} {item['metafield']}")
    usage = report['deepl_usage']
    if usage:
        print(f"\nDeepL: {fmt(usage.get('character_count', 0))} de {fmt(usage.get('character_limit', 0))} caracteres usados en el periodo actual")
    print('\nNo descuenta la memoria de traducción ni el glosario; son el techo previo a reutilizaciones.')


main()
