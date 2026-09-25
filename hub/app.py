import hashlib, json, os, html, re
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from .db import pool, initialize

def operator():
    # This service is intended to be reachable only on the trusted local network.
    return 'local'

@asynccontextmanager
async def lifespan(app):
    initialize()
    # Recover abandoned RUNNING work: only a complete persisted result can finish it.
    with pool.connection() as c:
        c.execute("UPDATE localization_jobs j SET state=CASE WHEN EXISTS(SELECT 1 FROM localization_results r WHERE r.job_id=j.id AND r.output IS NOT NULL AND r.validation->>'ok'='true') THEN 'READY_FOR_REVIEW' ELSE 'PENDING' END, updated_at=now() WHERE state='RUNNING'")
        c.execute("UPDATE process_control SET state='STOPPED',updated_at=now() WHERE singleton AND state='RUNNING'")
    yield

app=FastAPI(title='Newgarden Content Hub',version='0.1.0',lifespan=lifespan)
class ImportProduct(BaseModel):
    product_key:str=Field(min_length=1,max_length=120)
    source:dict
    mappings:dict[str,dict]=Field(default_factory=dict)
class Enqueue(BaseModel): product_key:str; destinations:list[str]
class Fields(BaseModel): fields:list[str]
class CellTranslation(BaseModel):
    store:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    resource_id:str=Field(min_length=8,max_length=255)
    locale:str=Field(min_length=2,max_length=20)
    key:str=Field(min_length=1,max_length=255)
    value:str=Field(max_length=200000)
class StoreConfig(BaseModel):
    code:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    domain:str=Field(min_length=3,max_length=255)
    token:str=Field(default='',max_length=500)
    locales:list[str]=Field(default_factory=list)

STYLE='''<style>
:root{color-scheme:light;--ink:#20332b;--muted:#68776f;--paper:#f5f3e9;--card:#fffef9;--green:#255b45;--lime:#c9df86;--line:#dce2d4;--orange:#c86b3c}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}header{background:var(--green);color:#fff;padding:22px max(24px,calc((100% - 1120px)/2));display:flex;justify-content:space-between;align-items:center;gap:20px}header strong{font-size:20px;letter-spacing:-.04em}nav{display:flex;gap:8px}nav a{color:#e5f0dc;text-decoration:none;padding:8px 12px;border-radius:999px}nav a:hover,nav a.active{background:#ffffff22;color:white}.wrap{max-width:1120px;margin:42px auto;padding:0 24px}h1{font-size:clamp(30px,5vw,48px);line-height:1.05;letter-spacing:-.055em;margin:0 0 10px}.lede{color:var(--muted);margin:0 0 28px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 8px 24px #20332b08;margin:18px 0}.candidate{padding:14px 0;border-top:1px solid var(--line)}.candidate .button{margin-top:4px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}label{display:block;font-size:13px;font-weight:650;margin:0 0 6px}input,select{width:100%;padding:11px 12px;border:1px solid #ccd6c8;border-radius:10px;background:white;color:var(--ink);font:inherit}input:focus{outline:3px solid #c9df8677;border-color:var(--green)}.field{margin:12px 0}button,.button{border:0;background:var(--green);color:white;padding:11px 16px;border-radius:999px;font-family:inherit;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}button:hover,.button:hover{background:#194431}.secondary{background:#e8eee2;color:var(--green)}.actions{display:flex;gap:10px;align-items:center;margin-top:18px}.muted{color:var(--muted);font-size:13px}.badge{display:inline-block;padding:4px 9px;border-radius:999px;background:#edf2e6;color:var(--green);font-size:12px}.comparison-scroll{overflow:auto;max-height:75vh;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid var(--line);padding:11px 9px;vertical-align:top}.comparison-scroll table{width:max-content;min-width:100%;table-layout:auto;border-collapse:separate;border-spacing:0}.comparison-scroll th,.comparison-scroll td{text-align:left;border-bottom:1px solid var(--line);border-right:1px solid #edf0e8;padding:14px 16px;vertical-align:top;overflow-wrap:anywhere;background:var(--card)}.comparison-scroll thead th{position:sticky;top:0;background:#edf2e6;z-index:2}.comparison-scroll th:first-child,.comparison-scroll td:first-child{position:sticky;left:0;width:210px;min-width:210px;max-width:210px;background:#f7f8f2;z-index:1}.comparison-scroll thead th:first-child{z-index:3;background:#e5ecdc}.comparison-scroll td:not(:first-child),.comparison-scroll th:not(:first-child){width:290px;min-width:290px;max-width:360px}.comparison-scroll td details{margin-top:8px}.comparison-scroll td summary{color:var(--green);font-weight:650;cursor:pointer}.comparison-scroll td button.more{padding:4px 10px;font-size:12px;margin-top:6px}th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}code{background:#edf0e8;padding:2px 5px;border-radius:4px}.status{margin-left:8px;color:var(--green)}@media(max-width:600px){header{align-items:flex-start;flex-direction:column}.wrap{margin-top:28px;padding:0 16px}.card{padding:18px;overflow:auto}}
</style>'''
STYLE=STYLE.replace('</style>','''.cell-edit{width:100%;padding:0;border:0;border-radius:6px;background:transparent;color:inherit;text-align:left;white-space:normal;font:inherit}.cell-edit:hover{background:#edf2e6;color:var(--ink)}.cell-excerpt{display:block}.cell-rest{white-space:pre-wrap}.edit-dialog{width:min(760px,calc(100vw - 32px));border:1px solid var(--line);border-radius:16px;padding:24px;background:var(--card);color:var(--ink);box-shadow:0 20px 80px #14271f55}.edit-dialog::backdrop{background:#14271f88}.edit-dialog textarea{width:100%;min-height:260px;padding:12px;border:1px solid #ccd6c8;border-radius:10px;font:14px/1.5 ui-monospace,monospace}.edit-dialog .html-note{display:none}.edit-dialog.html-mode .html-note{display:block}</style>''')

SHOPIFY_API_VERSION=os.getenv('SHOPIFY_API_VERSION','2026-07')

def shopify_graphql(domain, token, query, variables=None):
    url=f'https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    req=urllib.request.Request(url,data=json.dumps({'query':query,'variables':variables or {}}).encode(),headers={'X-Shopify-Access-Token':token,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=25) as response: data=json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401,403): raise HTTPException(502,'Shopify rechazó el token de una tienda')
        raise HTTPException(502,f'Error HTTP {exc.code} al consultar Shopify')
    except (urllib.error.URLError,TimeoutError): raise HTTPException(502,'No se pudo conectar con Shopify')
    if data.get('errors'): raise HTTPException(502,'Shopify GraphQL: '+str(data['errors'][0].get('message','error'))[:250])
    return data.get('data',{})

PRODUCT_SELECTION='''id title descriptionHtml handle vendor productType status tags createdAt updatedAt seo { title description } featuredMedia { preview { image { url altText } } } images(first:50) { nodes { id url altText } } variants(first:100) { nodes { id title sku barcode } pageInfo { hasNextPage endCursor } } metafields(first:100) { nodes { id namespace key type value description } pageInfo { hasNextPage endCursor } }'''

def shopify_stores():
    with pool.connection() as c:
        rows=c.execute("SELECT s.code,s.domain,s.config->>'admin_token',COALESCE(array_agg(l.locale) FILTER (WHERE l.locale IS NOT NULL),'{}') FROM stores s LEFT JOIN store_locales l ON l.store_code=s.code WHERE s.active GROUP BY s.code ORDER BY s.code").fetchall()
    return [{'code':r[0],'domain':r[1],'token':r[2] or '', 'locales':r[3]} for r in rows]

def shopify_search(store, term, limit=12):
    if not store['domain'] or not store['token']: return []
    # Run separate Shopify searches: its search grammar does not support arbitrary
    # substring wildcards consistently, and combining filters can hide valid hits.
    safe=term.replace('\\','\\\\').replace('"','\\"')
    q=f'''query ProductSearch($query:String!) {{ products(first:{limit},query:$query) {{ nodes {{ id title handle vendor productType status variants(first:100) {{ nodes {{ sku }} }} }} }} }}'''
    found={}
    # Plain text invokes Shopify's broad product search and covers product names.
    queries=[term]
    # Explicit SKU filter improves exact SKU lookup without constraining text search.
    queries.append(f'sku:"{safe}"')
    numeric=term.removeprefix('gid://shopify/Product/')
    if numeric.isdecimal(): queries.append(f'id:{numeric}')
    errors=[]
    for search in queries:
        try:
            nodes=shopify_graphql(store['domain'],store['token'],q,{'query':search}).get('products',{}).get('nodes',[])
            found.update((node['id'],node) for node in nodes)
        except HTTPException as exc:
            errors.append(exc)
    if not found and errors: raise errors[0]
    return list(found.values())

def shopify_find_by_id(store, identifier):
    raw=identifier.removeprefix('gid://shopify/Product/')
    if not raw.isdecimal(): return None
    gid=f'gid://shopify/Product/{raw}'
    q='''query ProductById($id:ID!) { product(id:$id) { id title handle vendor productType status variants(first:20) { nodes { sku } } } }'''
    return shopify_graphql(store['domain'],store['token'],q,{'id':gid}).get('product')

def shopify_product(store, gid, locales):
    tr=' '.join(f't{n}:translations(locale:{json.dumps(locale)}) {{ key value outdated }}' for n,locale in enumerate(locales))
    mf_tr=' '.join(f'mt{n}:translations(locale:{json.dumps(locale)}) {{ key value outdated }}' for n,locale in enumerate(locales))
    selection=PRODUCT_SELECTION.replace('metafields(first:100) { nodes { id namespace key type value description } }',f'metafields(first:100) {{ nodes {{ id namespace key type value description {mf_tr} }} }}')
    q=f'''query ProductRead($id:ID!) {{ product(id:$id) {{ {selection} {tr} }} }}'''
    translation_error=None
    try: data=shopify_graphql(store['domain'],store['token'],q,{'id':gid}).get('product')
    except HTTPException as exc:
        if not locales: raise
        translation_error=exc.detail
        selection=PRODUCT_SELECTION
        q=f'''query ProductRead($id:ID!) {{ product(id:$id) {{ {selection} }} }}'''
        data=shopify_graphql(store['domain'],store['token'],q,{'id':gid}).get('product')
    if data:
        data['translation_error']=translation_error
        data['translations_by_locale']={locale:data.pop(f't{n}',[]) for n,locale in enumerate(locales)}
        def mf_selection():
            if translation_error: return ''
            return ' '.join(f'mt{n}:translations(locale:{json.dumps(locale)}) {{ key value outdated }}' for n,locale in enumerate(locales))
        def collect_connection(owner_id, field, initial):
            nodes=list(initial.get('nodes',[])); page=initial.get('pageInfo',{})
            while page.get('hasNextPage'):
                query=f'''query MetafieldPage($id:ID!,$after:String) {{ node(id:$id) {{ ... on {"Product" if field=="metafields" and owner_id==gid else "ProductVariant"} {{ {field}(first:100,after:$after) {{ nodes {{ id namespace key type value description {mf_selection()} }} pageInfo {{ hasNextPage endCursor }} }} }} }} }}'''
                owner=shopify_graphql(store['domain'],store['token'],query,{'id':owner_id,'after':page.get('endCursor')}).get('node') or {}
                connection=owner.get(field,{})
                nodes.extend(connection.get('nodes',[])); page=connection.get('pageInfo',{})
                if not page.get('endCursor') and page.get('hasNextPage'): raise HTTPException(502,'Shopify no devolvió el cursor de metacampos')
            return nodes
        data['metafields']['nodes']=collect_connection(gid,'metafields',data.get('metafields',{}))
        variants=data.get('variants',{}).get('nodes',[])
        variant_page=data.get('variants',{}).get('pageInfo',{})
        while variant_page.get('hasNextPage'):
            page_query='''query VariantPage($id:ID!,$after:String) { product(id:$id) { variants(first:100,after:$after) { nodes { id title sku barcode } pageInfo { hasNextPage endCursor } } } }'''
            page_data=shopify_graphql(store['domain'],store['token'],page_query,{'id':gid,'after':variant_page.get('endCursor')}).get('product') or {}
            connection=page_data.get('variants',{}); variants.extend(connection.get('nodes',[])); variant_page=connection.get('pageInfo',{})
            if not variant_page.get('endCursor') and variant_page.get('hasNextPage'): raise HTTPException(502,'Shopify no devolvió el cursor de variantes')
        data['variants']['nodes']=variants
        for variant in variants:
            variant['metafields']={'nodes':collect_connection(variant['id'],'metafields',{'nodes':[],'pageInfo':{'hasNextPage':True,'endCursor':None}})}
        for mf in data.get('metafields',{}).get('nodes',[]):
            mf['translations_by_locale']={locale:mf.pop(f'mt{n}',[]) for n,locale in enumerate(locales)}
        for variant in variants:
            for mf in variant.get('metafields',{}).get('nodes',[]):
                mf['translations_by_locale']={locale:mf.pop(f'mt{n}',[]) for n,locale in enumerate(locales)}
    return data

def shopify_locale_code(locale, store_code=''):
    # Shopify requires the locale tag enabled on that exact shop. Never infer
    # it from a country/store code (for example, es-MX may be es or es-419).
    return locale

def product_field_value(product, key):
    if not product: return ''
    if key=='title': return product.get('title','')
    if key=='descriptionHtml': return product.get('descriptionHtml','')
    if key=='handle': return product.get('handle','')
    if key=='vendor': return product.get('vendor','')
    if key=='productType': return product.get('productType','')
    if key=='status': return product.get('status','')
    if key=='tags': return ', '.join(product.get('tags') or [])
    if key=='createdAt': return product.get('createdAt','')
    if key=='updatedAt': return product.get('updatedAt','')
    if key=='seo.title': return (product.get('seo') or {}).get('title','')
    if key=='seo.description': return (product.get('seo') or {}).get('description','')
    variants=product.get('variants',{}).get('nodes',[])
    if key=='variants.sku': return ', '.join(v.get('sku','') for v in variants if v.get('sku'))
    if key=='variants.barcode': return ', '.join(v.get('barcode','') for v in variants if v.get('barcode'))
    if key=='variants.title': return ', '.join(v.get('title','') for v in variants)
    if key=='images.altText': return ', '.join(i.get('altText') or '' for i in product.get('images',{}).get('nodes',[]))
    return ''

def shell(title, body, active=''):
    links=''.join(f'<a class="{"active" if key==active else ""}" href="{url}">{label}</a>' for key,url,label in [('home','/','Resumen'),('products','/products','Productos'),('stores','/stores','Tiendas')])
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} · Newgarden Hub</title>{STYLE}</head><body><header><strong>newgarden <span style="color:var(--lime)">/</span> content hub</strong><nav>{links}</nav></header>{body}</body></html>'''

@app.get('/health')
def health():
    with pool.connection() as c: c.execute('SELECT 1')
    return {'status':'ok'}

@app.get('/',response_class=HTMLResponse)
def home():
    with pool.connection() as c:
        state=c.execute('SELECT state,last_incident FROM process_control').fetchone()
        jobs=c.execute("SELECT j.id,j.product_key,j.store_code,j.locale,j.state,j.reason FROM localization_jobs j ORDER BY j.id DESC LIMIT 100").fetchall()
        prod=c.execute('SELECT product_key,source_version FROM global_products ORDER BY product_key').fetchall()
    rows=''.join(f'<tr><td>{j[0]}</td><td>{html.escape(str(j[1]))}</td><td>{html.escape(str(j[2]))} / {html.escape(str(j[3]))}</td><td><span class="badge">{html.escape(str(j[4]))}</span></td><td>{html.escape(str(j[5] or ""))}</td></tr>' for j in jobs)
    incident=html.escape(json.dumps(state[1],ensure_ascii=False)) if state[1] else 'Ninguna'
    body=f'''<main class="wrap"><h1>Centro de operaciones</h1><p class="lede">Estado de la cola de localización y actividad reciente.</p><section class="grid"><article class="card"><span class="muted">PROCESO</span><h2>{html.escape(state[0])}</h2><span class="muted">Última incidencia: {incident}</span></article><article class="card"><span class="muted">CATÁLOGO</span><h2>{len(prod)} productos</h2><span class="muted">Importa y administra destinos desde la API.</span></article></section><section class="card"><h2>Trabajos recientes</h2><table><tr><th>ID</th><th>Producto</th><th>Destino</th><th>Estado</th><th>Motivo</th></tr>{rows}</table></section><p class="muted">API disponible en <a href="/docs">/docs</a>.</p></main>'''
    return shell('Resumen',body,'home')

@app.get('/stores',response_class=HTMLResponse)
def stores_page():
    body='''<main class="wrap"><h1>Tiendas Shopify</h1><p class="lede">Configura las conexiones y los idiomas disponibles para cada tienda.</p><div id="stores" class="grid"><div class="card">Cargando tiendas…</div></div><section class="card"><h2>Añadir tienda</h2><form id="store-form"><div class="grid"><div class="field"><label for="code">Código</label><input id="code" placeholder="FR" pattern="[A-Z0-9_-]{2,12}" required></div><div class="field"><label for="domain">Dominio Shopify</label><input id="domain" placeholder="mi-tienda.myshopify.com" required></div><div class="field"><label for="token">Token de acceso Admin API</label><input id="token" type="password" autocomplete="new-password" placeholder="shpat_…"></div><div class="field"><label for="locales">Locales (separados por coma)</label><input id="locales" placeholder="fr-FR, en-GB"></div></div><div class="actions"><button>Guardar tienda</button><span id="message" class="status" role="status"></span></div></form><p class="muted">Los tokens se guardan en la base de datos de esta instancia. El token vacío conserva el actual al editar.</p></section></main>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadStores(){const r=await fetch('/api/stores');const data=await r.json();document.querySelector('#stores').innerHTML=data.map(s=>`<article class="card"><span class="badge">${s.active?'ACTIVA':'INACTIVA'}</span><h2>${esc(s.code)}</h2><p>${esc(s.domain||'Dominio sin configurar')}</p><p class="muted">Token: ${s.has_token?'•••••••• configurado':'sin configurar'} · Locales configurados: ${esc(s.locales.join(', ')||'ninguno')}</p><button class="secondary" data-code="${esc(s.code)}">Editar configuración</button> <button class="secondary" data-fetch-locales="${esc(s.code)}" ${!s.active||!s.has_token?'disabled':''}>Consultar locales Shopify</button><div class="muted" id="shop-locales-${esc(s.code)}"></div></article>`).join('')||'<div class="card">No hay tiendas configuradas.</div>';document.querySelectorAll('[data-code]').forEach(b=>b.addEventListener('click',()=>editStore(data.find(s=>s.code===b.dataset.code))));document.querySelectorAll('[data-fetch-locales]').forEach(button=>button.addEventListener('click',async()=>{const s=data.find(item=>item.code===button.dataset.fetchLocales),panel=document.querySelector('#shop-locales-'+s.code);panel.textContent='Consultando Shopify…';const response=await fetch('/api/stores/'+encodeURIComponent(s.code)+'/shop-locales'),result=await response.json();if(!response.ok){panel.textContent=result.detail||'No se pudieron consultar los locales';return}const secondary=result.filter(item=>item.published&&!item.primary);panel.innerHTML='<p><strong>Locales habilitados en Shopify:</strong></p><ul>'+result.map(item=>`<li><code>${esc(item.locale)}</code> · ${esc(item.name)}${item.primary?' · principal':''}${item.published?' · publicado':' · no publicado'}</li>`).join('')+'</ul><button type="button" class="secondary use-shop-locales">Cargar secundarios publicados</button>';panel.querySelector('.use-shop-locales').addEventListener('click',()=>{editStore(s);document.querySelector('#locales').value=secondary.map(item=>item.locale).join(', ')})}))}
function editStore(s){document.querySelector('#code').value=s.code;document.querySelector('#code').readOnly=true;document.querySelector('#domain').value=s.domain||'';document.querySelector('#token').value='';document.querySelector('#locales').value=s.locales.join(', ');window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})}
document.querySelector('#store-form').addEventListener('submit',async e=>{e.preventDefault();const payload={code:document.querySelector('#code').value.trim().toUpperCase(),domain:document.querySelector('#domain').value.trim(),token:document.querySelector('#token').value,locales:document.querySelector('#locales').value.split(',').map(x=>x.trim()).filter(Boolean)};const r=await fetch('/api/stores',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const m=document.querySelector('#message');if(r.ok){m.textContent='Configuración guardada';document.querySelector('#store-form').reset();document.querySelector('#code').readOnly=false;loadStores()}else{const x=await r.json();m.textContent=x.detail||'No se pudo guardar'}});loadStores();
</script>'''
    return shell('Tiendas',body,'stores')

@app.get('/api/stores')
def list_stores():
    with pool.connection() as c:
        rows=c.execute("SELECT s.code,s.domain,s.active,COALESCE(s.config->>'admin_token','')<>'',COALESCE(array_agg(l.locale) FILTER (WHERE l.locale IS NOT NULL),'{}') FROM stores s LEFT JOIN store_locales l ON l.store_code=s.code GROUP BY s.code ORDER BY s.code").fetchall()
    return [{'code':r[0],'domain':r[1] or '', 'active':r[2], 'has_token':r[3], 'locales':r[4]} for r in rows]

@app.get('/api/stores/{code}/shop-locales')
def shop_locales(code:str):
    store=next((item for item in shopify_stores() if item['code']==code),None)
    if not store or not store['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales Shopify')
    query='''query EnabledShopLocales { shopLocales { locale name primary published } }'''
    locales=shopify_graphql(store['domain'],store['token'],query).get('shopLocales',[])
    return sorted(locales,key=lambda item:(not item.get('primary',False),item.get('locale','')))

@app.get('/products',response_class=HTMLResponse)
def product_browser(q:str='',source_id:str=''):
    term=q.strip(); detail=''; results=''
    if source_id:
        try:
            data=compare_shopify_product(source_id)
            esc=lambda x:html.escape(str(x if x is not None else ''),quote=True)
            cols=''.join(f'<th>{esc(c["label"])}</th>' for c in data['columns'])
            rows=[]
            for f in data['fields']:
                cells=[]
                for column_index,value in enumerate(f['values']):
                    if value is None or value=='': shown='<span class="muted">—</span>'
                    else:
                        raw=', '.join(map(str,value)) if isinstance(value,list) else str(value)
                        display=html.unescape(re.sub(r'<[^>]*>',' ',raw)).strip() if f['key']=='descriptionHtml' else raw
                        safe=esc(display)
                        shown=safe if len(display)<=220 else f'<span>{esc(display[:220])}…</span><button type="button" class="more" data-full="{esc(display)}">Ver más</button>'
                    edit=f.get('edits',[None]*len(f['values']))[column_index]
                    if edit:
                        encoded=esc(json.dumps(edit,ensure_ascii=False))
                        shown=f'<span class="cell-display">{shown}</span><button type="button" class="cell-edit" data-edit="{encoded}">Editar celda</button>'
                    cells.append(f'<td>{shown}</td>')
                rows.append(f'<tr><th>{esc(f["label"])}<br><code>{esc(f["key"])}</code></th>{"".join(cells)}</tr>')
            notices=''.join(f'<p class="muted">{esc(n)}</p>' for n in data['notices'])
            detail=f'''<section class="card"><h2>Comparación del producto</h2><p class="lede">Selecciona “Editar celda” para guardar una traducción en esa tienda y ese idioma. En descripciones, el editor conserva el HTML.</p><h3>{esc(data['source']['title'])}</h3><p class="muted">SKU: {esc(data['source']['sku'] or '—')} · {esc(data['source']['id'])}</p>{notices}<div class="comparison-scroll"><table><thead><tr><th>Campo</th>{cols}</tr></thead><tbody>{''.join(rows)}</tbody></table></div><dialog class="edit-dialog" id="cell-editor"><form method="dialog" id="cell-edit-form"><h2>Editar traducción</h2><p id="edit-context" class="muted"></p><p class="html-note muted">La descripción se guarda como HTML. Puedes editar etiquetas como &lt;p&gt;, &lt;ul&gt; y &lt;strong&gt;.</p><textarea id="edit-value" aria-label="Traducción"></textarea><div class="actions"><button type="submit" id="save-cell">Guardar en Shopify</button><button type="button" class="secondary" id="cancel-cell">Cancelar</button><span id="edit-status" class="status" role="status"></span></div></form></dialog></section><script>
const editor=document.querySelector('#cell-editor'),form=document.querySelector('#cell-edit-form'),area=document.querySelector('#edit-value');let editTarget=null;
document.querySelectorAll('.cell-edit').forEach(button=>button.addEventListener('click',()=>{{editTarget=JSON.parse(button.dataset.edit);area.value=editTarget.value||'';document.querySelector('#edit-context').textContent=editTarget.store+' · '+editTarget.locale+' · '+editTarget.key;editor.classList.toggle('html-mode',editTarget.key==='body_html');document.querySelector('#edit-status').textContent='';editor.showModal()}}));
document.querySelectorAll('.more').forEach(button=>button.addEventListener('click',()=>{{button.previousElementSibling.textContent=button.dataset.full;button.remove()}}));
document.querySelector('#cancel-cell').addEventListener('click',()=>editor.close());
form.addEventListener('submit',async event=>{{event.preventDefault();if(!editTarget)return;const save=document.querySelector('#save-cell'),status=document.querySelector('#edit-status');save.disabled=true;status.textContent='Guardando…';const response=await fetch('/api/shopify/translations',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{...editTarget,value:area.value}})}});const result=await response.json();save.disabled=false;if(response.ok){{status.textContent='Guardado';location.reload()}}else status.textContent=result.detail||'No se pudo guardar'}});
</script>'''
        except HTTPException as exc:
            detail=f'<section class="card"><h2>No se pudo cargar la comparación</h2><p>{html.escape(str(exc.detail))}</p></section>'
    if term:
        try:
            data=search_shopify_products(term); hits=data['products']
            distinct=', '.join(sorted({code for p in hits for code in p.get('store_codes',[p['store']])}))
            summary=f'{len(hits)} coincidencia(s) en {html.escape(distinct)}' if hits else 'Sin resultados en tiendas activas'
            cards=[]
            for p in hits:
                esc=lambda x:html.escape(str(x if x is not None else ''),quote=True)
                codes=p.get('store_codes',[p['store']]); store_label=' / '.join(codes)
                locale_labels=', '.join(dict.fromkeys(locale for code in codes for locale in p.get('locales',{}).get(code,[])))
                candidates=[]
                for candidate in p['candidates']:
                    if 'ES' in codes:
                        args={'source_id':candidate['id'],'q':term}
                        action=f'<a class="button" href="/products?{urlencode(args)}">Comparar este producto</a>'
                    else: action='<span class="badge">Producto encontrado</span>'
                    candidates.append(f'<div class="candidate"><strong>{esc(candidate["title"])}</strong><p class="muted">ID: {esc(candidate["id"])} · Handle: {esc(candidate["handle"])}</p>{action}</div>')
                title=f'{len(candidates)} productos con este SKU' if len(candidates)>1 else candidates and p['candidates'][0]['title'] or p['sku']
                cards.append(f'<article class="card"><strong>{esc(title)}</strong><p class="muted">SKU: {esc(p["sku"] or "Sin SKU")} · Tienda/mercado: {esc(store_label)} · Locales: {esc(locale_labels or "—")}</p>{"".join(candidates)}</article>')
            errors=''.join(f'<p class="muted">{html.escape(str(n))}</p>' for n in data['notices'])
            results=f'<section class="card"><h2>Coincidencias por tienda</h2><p class="muted">{summary}</p>{errors}{"".join(cards) if cards else "<p>No se encontraron productos.</p>"}</section>'
        except HTTPException as exc:
            results=f'<section class="card"><h2>No se pudo completar la búsqueda</h2><p>{html.escape(str(exc.detail))}</p></section>'
    value=html.escape(term,quote=True)
    body=f'''<main class="wrap"><h1>Explorador de productos</h1><p class="lede">Busca por SKU, nombre, ID o texto en todas las tiendas activas. Selecciona un resultado de ES para abrir la comparación.</p><section class="card"><form action="/products" method="get"><div class="field"><label for="term">SKU, nombre o ID de Shopify</label><input id="term" name="q" value="{value}" required placeholder="Ej. NG-1234, lámpara exterior o gid://shopify/Product/…"></div><div class="actions"><button type="submit">Buscar en tiendas</button></div></form></section>{detail}{results}</main>'''
    return shell('Explorador de productos',body,'products')

@app.get('/api/shopify/products/search')
def search_shopify_products(q:str):
    term=q.strip()
    if len(term)<2: raise HTTPException(400,'Escribe al menos 2 caracteres')
    stores=shopify_stores()
    if not any(s['code']=='ES' and s['token'] for s in stores): raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    # Several configured market codes can point to the same Shopify shop. Query each
    # unique domain/token once, then annotate the matches with every configured code.
    groups={}
    for store in stores:
        if store['domain'] and store['token']:
            groups.setdefault((store['domain'],store['token']),[]).append(store)
    products=[]; notices=[]
    def search_group(group):
        representative=group[0]
        try:
            direct=shopify_find_by_id(representative,term) if term.isdecimal() or term.startswith('gid://shopify/Product/') else None
            hits=shopify_search(representative,term)
            if direct and all(p['id']!=direct['id'] for p in hits): hits.insert(0,direct)
            return group,hits,None
        except HTTPException as exc:
            return group,[],exc.detail
    with ThreadPoolExecutor(max_workers=min(8,len(groups) or 1)) as executor:
        futures=[executor.submit(search_group,group) for group in groups.values()]
        for future in as_completed(futures):
            group,hits,error=future.result()
            if error: notices.append(f'{", ".join(s["code"] for s in group)}: {error}')
            store_codes=[s['code'] for s in group]
            primary='ES' if 'ES' in store_codes else store_codes[0]
            locales={s['code']:s['locales'] for s in group}
            for p in hits:
                candidate={'id':p['id'],'title':p['title'],'handle':p['handle'],'sku':next((v.get('sku') for v in p.get('variants',{}).get('nodes',[] ) if v.get('sku')),'')}
                products.append({'store':primary,'store_codes':store_codes,'locales':locales,'candidate':candidate})
    # A duplicated SKU can match multiple different Shopify products. Group them
    # under the same store/market card and let the operator select the Spanish row.
    grouped={}
    for hit in products:
        candidate=hit.pop('candidate'); sku=candidate['sku'].strip().casefold()
        key=(tuple(hit['store_codes']),sku if sku else candidate['id'])
        if key not in grouped:
            grouped[key]={**hit,'sku':candidate['sku'],'candidates':[]}
        grouped[key]['candidates'].append(candidate)
    result=list(grouped.values())
    result.sort(key=lambda p:(p['store']!='ES',p['store'],p['sku'],p['candidates'][0]['title']))
    return {'products':result,'notices':notices}

@app.get('/api/shopify/products/compare')
def compare_shopify_product(source_id:str):
    stores=shopify_stores(); by_code={s['code']:s for s in stores}; source_store=by_code.get('ES')
    if not source_store or not source_store['token']: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    source_locales=source_store['locales']
    source_lookup_locales=list(dict.fromkeys(shopify_locale_code(locale) for locale in source_locales))
    source=shopify_product(source_store,source_id,source_lookup_locales)
    if not source: raise HTTPException(404,'Producto no encontrado en la tienda ES')
    sku=next((v.get('sku') for v in source.get('variants',{}).get('nodes',[]) if v.get('sku')),'')
    targets=[]; notices=[]
    if source.get('translation_error'): notices.append('Shopify no devolvió las traducciones de los locales configurados en ES; esos valores aparecerán vacíos.')
    with pool.connection() as c:
        mapping_rows=c.execute("SELECT dst.store_code,dst.shopify_product_gid FROM store_products src JOIN store_products dst USING(product_key) WHERE src.store_code='ES' AND src.shopify_product_gid=%s AND dst.store_code<>'ES' AND dst.mapping_status='CONFIRMED'",(source_id,)).fetchall()
    # Prefer mappings tied to this Spanish product. If none exists, try its SKU in each store.
    known={code:gid for code,gid in mapping_rows}
    for locale in source_locales:
        if locale.lower() not in ('es-es','es'):
            resolved=shopify_locale_code(locale)
            label=f'ES · {locale}'+(f' (Shopify {resolved})' if resolved!=locale else '')
            targets.append({'store':'ES','domain':source_store['domain'],'locale':locale,'lookup_locale':resolved,'label':label,'product':source})
    for store in stores:
        if store['code']=='ES': continue
        locales=store['locales']
        if not locales: continue
        def lookup_locale(locale):
            if store['domain']!=source_store['domain']: return locale
            return shopify_locale_code(locale,store['code'])
        locale_map={locale:lookup_locale(locale) for locale in locales}
        query_locales=list(dict.fromkeys(locale_map.values()))
        gid=source_id if store['domain']==source_store['domain'] else known.get(store['code'])
        candidate=None
        if gid and store['token']:
            try: candidate=shopify_product(store,gid,query_locales)
            except HTTPException as exc: notices.append(f'{store["code"]}: no se pudo leer la correspondencia guardada ({exc.detail})')
        if not candidate and sku and store['token']:
            try:
                matches=shopify_search(store,sku,5)
                candidate=next((p for p in matches if any(v.get('sku')==sku for v in p.get('variants',{}).get('nodes',[]))),None)
                if candidate: candidate=shopify_product(store,candidate['id'],query_locales)
            except HTTPException as exc: notices.append(f'{store["code"]}: no se pudo buscar el SKU ({exc.detail})')
        if candidate:
            for locale in locales:
                resolved=locale_map[locale]
                label=f'{store["code"]} · {locale}'+(f' (Shopify {resolved})' if resolved!=locale else '')
                targets.append({'store':store['code'],'domain':store['domain'],'locale':locale,'lookup_locale':resolved,'label':label,'product':candidate,'uses_base_fields':store['domain']!=source_store['domain']})
        if candidate and candidate.get('translation_error'):
            notices.append(f'{store["code"]}: se muestran los campos actuales de la tienda; Shopify no devolvió sus traducciones registradas.')
    # ES, NL, and PT are configured as codes/markets for the same Shopify shop.
    # Keep one column for each actual product+locale, preferring the market label.
    unique_targets={}
    for target in targets:
        key=(target['domain'],(target['product'] or {}).get('id'),target['lookup_locale'])
        prior=unique_targets.get(key)
        if prior is None or (prior['store']=='ES' and target['store']!='ES'):
            unique_targets[key]=target
    targets=list(unique_targets.values())
    def translations(product,locale): return {x['key']:x.get('value','') for x in (product or {}).get('translations_by_locale',{}).get(locale,[])}
    source_variants=source.get('variants',{}).get('nodes',[])
    source_base={'title':source.get('title',''),'descriptionHtml':source.get('descriptionHtml',''),'handle':source.get('handle',''),'vendor':source.get('vendor',''),'productType':source.get('productType',''),'status':source.get('status',''),'tags':', '.join(source.get('tags') or []),'createdAt':source.get('createdAt',''),'updatedAt':source.get('updatedAt',''),'seo.title':(source.get('seo') or {}).get('title',''),'seo.description':(source.get('seo') or {}).get('description',''),'variants.sku':', '.join(v.get('sku','') for v in source_variants if v.get('sku')),'variants.barcode':', '.join(v.get('barcode','') for v in source_variants if v.get('barcode')),'variants.title':', '.join(v.get('title','') for v in source_variants),'images.altText':', '.join(i.get('altText') or '' for i in source.get('images',{}).get('nodes',[]))}
    keymap={'title':'title','descriptionHtml':'body_html','handle':'handle','vendor':'vendor','productType':'product_type','status':'status','tags':'tags','createdAt':'created_at','updatedAt':'updated_at','seo.title':'seo_title','seo.description':'seo_description','variants.sku':'sku','variants.barcode':'barcode','variants.title':'variant_title','images.altText':'image_alt_text'}
    fields=[]
    for key,val in source_base.items():
        values=[val]
        edits=[None]
        for target in targets:
            product=target['product']; tr=translations(product,target['lookup_locale'])
            value=tr.get(keymap[key],'')
            if not value and target.get('uses_base_fields'): value=product_field_value(product,key)
            values.append(value)
            editable={'title','body_html','handle','seo_title','seo_description','vendor','product_type'}
            edits.append({'store':target['store'],'resource_id':product['id'],'locale':target['lookup_locale'],'key':keymap[key],'value':value} if product and keymap.get(key) in editable else None)
        fields.append({'key':key,'label':key.replace('.',' / '),'values':values,'edits':edits})
    # Rows are defined by the Spanish source. A missing key and an empty value
    # are shown distinctly in every destination.
    def is_yotpo(mf):
        return 'yotpo' in str(mf.get('namespace','')).casefold() or 'yotpo' in str(mf.get('key','')).casefold()
    def mf_value_raw(mf, locale=None):
        if mf is None: return None
        localized=next((item for item in mf.get('translations_by_locale',{}).get(locale,[]) if item.get('key')=='value'),None) if locale else None
        return localized.get('value') if localized else mf.get('value')
    def mf_value(mf, product=None, locale=None):
        if mf is None: return 'Falta el metacampo'
        value=mf_value_raw(mf,locale)
        return value if value not in (None,'') else 'Metacampo vacío'
    source_mfs=[item for item in source.get('metafields',{}).get('nodes',[]) if not is_yotpo(item)]
    for mf in sorted(source_mfs,key=lambda item:(item.get('namespace',''),item.get('key',''))):
        namespace,mfkey=mf['namespace'],mf['key']
        key=f'metafield.product.{namespace}.{mfkey}'
        values=[mf_value(mf)]
        edits=[None]
        for target in targets:
            if not target['product']:
                values.append('')
                edits.append(None)
                continue
            dest=next((item for item in (target['product'] or {}).get('metafields',{}).get('nodes',[]) if item['namespace']==namespace and item['key']==mfkey and not is_yotpo(item)),None)
            values.append(mf_value(dest,target['product'],target['lookup_locale']))
            value=mf_value_raw(dest,target['lookup_locale']) if dest else ''
            edits.append({'store':target['store'],'resource_id':dest['id'],'locale':target['lookup_locale'],'key':'value','value':value} if dest and dest.get('type') in {'single_line_text_field','multi_line_text_field','rich_text_field','html'} else None)
        fields.append({'key':key,'label':f'Producto · {namespace} / {mfkey} ({mf.get("type","")})','values':values,'edits':edits})
    for source_variant in source_variants:
        variant_mfs=[item for item in source_variant.get('metafields',{}).get('nodes',[]) if not is_yotpo(item)]
        identity=source_variant.get('sku') or source_variant.get('title') or source_variant['id']
        identity_label=f'SKU {source_variant["sku"]}' if source_variant.get('sku') else source_variant.get('title') or source_variant['id']
        for mf in sorted(variant_mfs,key=lambda item:(item.get('namespace',''),item.get('key',''))):
            namespace,mfkey=mf['namespace'],mf['key']
            key=f'metafield.variant.{identity}.{namespace}.{mfkey}'
            values=[mf_value(mf)]
            edits=[None]
            for target in targets:
                if not target['product']:
                    values.append('')
                    edits.append(None)
                    continue
                dest_variant=next((v for v in (target['product'] or {}).get('variants',{}).get('nodes',[]) if (v.get('sku') or v.get('title') or v['id'])==identity),None)
                dest_mf=next((item for item in (dest_variant or {}).get('metafields',{}).get('nodes',[]) if item['namespace']==namespace and item['key']==mfkey and not is_yotpo(item)),None)
                values.append(mf_value(dest_mf,target['product'],target['lookup_locale']))
                value=mf_value_raw(dest_mf,target['lookup_locale']) if dest_mf else ''
                edits.append({'store':target['store'],'resource_id':dest_mf['id'],'locale':target['lookup_locale'],'key':'value','value':value} if dest_mf and dest_mf.get('type') in {'single_line_text_field','multi_line_text_field','rich_text_field','html'} else None)
            fields.append({'key':key,'label':f'Variante · {identity_label} · {namespace} / {mfkey} ({mf.get("type","")})','values':values,'edits':edits})
    columns=[{'label':'ES · español (base)'}]+[{'label':t['label']} for t in targets]
    if not sku: notices.append('El producto de ES no tiene SKU; no se pudo proponer una correspondencia automática en otras tiendas.')
    for t in targets:
        if not t['product']: notices.append(f'{t["store"]} · {t["locale"]}: no hay producto equivalente localizado por correspondencia o SKU.')
    return {'source':{'id':source['id'],'title':source['title'],'sku':sku},'columns':columns,'fields':fields,'notices':list(dict.fromkeys(notices))}

@app.post('/api/shopify/translations')
def save_shopify_translation(x:CellTranslation,user=Depends(operator)):
    store=next((item for item in shopify_stores() if item['code']==x.store),None)
    if not store or not store['token']: raise HTTPException(404,'Tienda sin configurar')
    allowed_locales={shopify_locale_code(locale,store['code']) for locale in store['locales']}
    if x.locale not in allowed_locales: raise HTTPException(400,'El idioma no está configurado en esa tienda')
    resource_type=x.resource_id.removeprefix('gid://shopify/')
    if resource_type.startswith('Metafield/'):
        if x.key!='value': raise HTTPException(400,'La clave del metacampo no es válida')
    elif resource_type.startswith(('Product/','ProductVariant/')):
        if x.key not in {'title','body_html','handle','seo_title','seo_description','vendor','product_type'}:
            raise HTTPException(400,'El campo no está habilitado para traducción')
    else: raise HTTPException(400,'Tipo de recurso Shopify no admitido')
    resource_query='''query TranslationDigest($id:ID!) { translatableResource(resourceId:$id) { resourceId translatableContent { key value digest } } }'''
    resource=shopify_graphql(store['domain'],store['token'],resource_query,{'id':x.resource_id}).get('translatableResource')
    digest=next((item.get('digest') for item in (resource or {}).get('translatableContent',[]) if item.get('key')==x.key),None)
    if not digest: raise HTTPException(409,'Shopify no ofrece un digest traducible para ese campo; comprueba el tipo de metacampo y los permisos')
    mutation='''mutation RegisterTranslation($id:ID!,$translations:[TranslationInput!]!) { translationsRegister(resourceId:$id,translations:$translations) { userErrors { field message } translations { key value locale } } }'''
    result=shopify_graphql(store['domain'],store['token'],mutation,{'id':x.resource_id,'translations':[{'locale':x.locale,'key':x.key,'value':x.value,'translatableContentDigest':digest}]}).get('translationsRegister') or {}
    errors=result.get('userErrors',[])
    if errors: raise HTTPException(400,'Shopify: '+'; '.join(error.get('message','error') for error in errors)[:500])
    return {'store':x.store,'resource_id':x.resource_id,'locale':x.locale,'key':x.key,'saved':True}

@app.put('/api/stores')
def save_store(x:StoreConfig):
    domain=x.domain.lower().removeprefix('https://').removeprefix('http://').strip('/')
    if not domain.endswith('.myshopify.com'): raise HTTPException(400,'Usa el dominio *.myshopify.com')
    with pool.connection() as c:
        prior=c.execute("SELECT COALESCE(config->>'admin_token','') FROM stores WHERE code=%s",(x.code,)).fetchone()
        token=x.token or (prior[0] if prior else '')
        config={'admin_token':token} if token else {}
        c.execute('INSERT INTO stores(code,domain,config,active) VALUES(%s,%s,%s,true) ON CONFLICT(code) DO UPDATE SET domain=EXCLUDED.domain,config=EXCLUDED.config,active=true',(x.code,domain,Jsonb(config)))
        c.execute('DELETE FROM store_locales WHERE store_code=%s',(x.code,))
        for locale in dict.fromkeys(x.locales):
            c.execute("INSERT INTO store_locales(store_code,locale,strategy) VALUES(%s,%s,'BASE')",(x.code,locale))
    return {'code':x.code,'domain':domain,'active':True,'has_token':bool(token),'locales':list(dict.fromkeys(x.locales))}

@app.post('/api/products/import')
def import_product(x:ImportProduct,user=Depends(operator)):
    raw=json.dumps(x.source,sort_keys=True,ensure_ascii=False); digest=hashlib.sha256(raw.encode()).hexdigest()
    with pool.connection() as c:
        old=c.execute('SELECT source_hash,source_version FROM global_products WHERE product_key=%s FOR UPDATE',(x.product_key,)).fetchone()
        ver=(old[1]+1 if old and old[0]!=digest else old[1] if old else 1)
        c.execute('INSERT INTO global_products(product_key,source,source_hash,source_version) VALUES(%s,%s,%s,%s) ON CONFLICT(product_key) DO UPDATE SET source=EXCLUDED.source,source_hash=EXCLUDED.source_hash,source_version=EXCLUDED.source_version,updated_at=now()', (x.product_key,Jsonb(x.source),digest,ver))
        for store,m in x.mappings.items():
            gid=m.get('shopify_product_gid'); handle=m.get('handle'); status='CONFIRMED' if gid else 'BLOCKED_MAPPING'
            c.execute('INSERT INTO store_products(product_key,store_code,shopify_product_gid,current_handle,mapping_status) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(product_key,store_code) DO UPDATE SET shopify_product_gid=EXCLUDED.shopify_product_gid,current_handle=EXCLUDED.current_handle,mapping_status=EXCLUDED.mapping_status',(x.product_key,store,gid,handle,status))
            if status=='CONFIRMED':
                c.execute("UPDATE localization_jobs SET state='PENDING',reason=NULL WHERE product_key=%s AND store_code=%s AND state='BLOCKED_MAPPING'",(x.product_key,store))
        if old and old[0]!=digest:
            c.execute("UPDATE localization_jobs SET state='STALE',reason='La fuente cambió desde la generación',updated_at=now() WHERE product_key=%s AND state IN ('READY_FOR_REVIEW','APPROVED')",(x.product_key,))
    return {'product_key':x.product_key,'source_version':ver,'source_hash':digest,'mapping_status':'manual confirmation required for unmapped destinations'}

@app.post('/api/jobs/enqueue')
def enqueue(x:Enqueue,user=Depends(operator)):
    with pool.connection() as c:
        p=c.execute('SELECT source_version FROM global_products WHERE product_key=%s',(x.product_key,)).fetchone()
        if not p: raise HTTPException(404,'Producto no registrado')
        added=[]
        for dest in x.destinations:
            bits=dest.split(':',1); store=bits[0]; requested_locale=bits[1] if len(bits)>1 else None
            locale=c.execute('SELECT locale FROM store_locales WHERE store_code=%s AND (%s::text IS NULL OR locale=%s) ORDER BY strategy LIMIT 1',(store,requested_locale,requested_locale)).fetchone()
            if not locale: raise HTTPException(400,f'Destino desconocido: {dest}')
            mp=c.execute('SELECT mapping_status FROM store_products WHERE product_key=%s AND store_code=%s',(x.product_key,store)).fetchone()
            state='PENDING' if store=='ES' or (mp and mp[0]=='CONFIRMED') else 'BLOCKED_MAPPING'
            row=c.execute('INSERT INTO localization_jobs(product_key,store_code,locale,source_version,state,reason) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(product_key,store_code,locale,source_version) DO NOTHING RETURNING id,state',(x.product_key,store,locale[0],p[0],state,None if state=='PENDING' else 'Falta correspondencia aprobada del producto destino')).fetchone()
            if row: added.append({'id':row[0],'state':row[1]})
    return {'created':added}

@app.get('/api/jobs')
def jobs(user=Depends(operator)):
    with pool.connection() as c: rows=c.execute('SELECT id,product_key,store_code,locale,source_version,state,reason FROM localization_jobs ORDER BY id').fetchall()
    return [dict(zip(('id','product_key','store','locale','source_version','state','reason'),r)) for r in rows]

def set_running(user):
    with pool.connection() as c:
        row=c.execute('SELECT state FROM process_control FOR UPDATE').fetchone()
        if row[0]=='RUNNING': raise HTTPException(409,'El worker ya está activo')
        c.execute("UPDATE process_control SET state='RUNNING',last_incident=NULL,resumed_by=%s,updated_at=now() WHERE singleton",(user,))
    return {'state':'RUNNING'}
@app.post('/api/process/start')
def start(user=Depends(operator)): return set_running(user)
@app.post('/api/process/continue')
def resume(user=Depends(operator)): return set_running(user)
@app.post('/api/process/stop')
def stop(user=Depends(operator)):
    with pool.connection() as c: c.execute("UPDATE process_control SET state='STOPPED',resumed_by=%s,updated_at=now() WHERE singleton",(user,))
    return {'state':'STOPPED'}
@app.get('/api/process')
def process(user=Depends(operator)):
    with pool.connection() as c: r=c.execute('SELECT state,last_incident,resumed_by,updated_at FROM process_control').fetchone()
    return dict(zip(('state','last_incident','resumed_by','updated_at'),r))

@app.get('/api/jobs/{job_id}/results')
def results(job_id:int,user=Depends(operator)):
    with pool.connection() as c: rs=c.execute('SELECT id,output,validation,approved_fields,approved_by,created_at FROM localization_results WHERE job_id=%s ORDER BY id',(job_id,)).fetchall()
    return [dict(zip(('id','output','validation','approved_fields','approved_by','created_at'),r)) for r in rs]
@app.get('/api/products/{product_key}')
def product_detail(product_key:str,user=Depends(operator)):
    with pool.connection() as c:
        p=c.execute('SELECT product_key,source,source_hash,source_version,updated_at FROM global_products WHERE product_key=%s',(product_key,)).fetchone()
        if not p: raise HTTPException(404,'Producto no registrado')
        mappings=c.execute('SELECT store_code,shopify_product_gid,current_handle,mapping_status FROM store_products WHERE product_key=%s',(product_key,)).fetchall()
    return {'product_key':p[0],'source':p[1],'source_hash':p[2],'source_version':p[3],'updated_at':p[4],'mappings':[dict(zip(('store','shopify_product_gid','handle','status'),r)) for r in mappings]}
@app.get('/api/jobs/{job_id}/publications')
def publications(job_id:int,user=Depends(operator)):
    with pool.connection() as c: rows=c.execute('SELECT id,fields,outcomes,shopify_gid,simulated,verified_at,created_at FROM publication_records WHERE job_id=%s ORDER BY id',(job_id,)).fetchall()
    return [dict(zip(('id','fields','outcomes','shopify_gid','simulated','verified_at','created_at'),r)) for r in rows]
@app.post('/api/jobs/{job_id}/approve')
def approve(job_id:int,x:Fields,user=Depends(operator)):
    with pool.connection() as c:
        row=c.execute("SELECT r.id,r.output,j.source_version,p.source_version FROM localization_results r JOIN localization_jobs j ON j.id=r.job_id JOIN global_products p ON p.product_key=j.product_key WHERE j.id=%s AND j.state='READY_FOR_REVIEW' ORDER BY r.id DESC LIMIT 1",(job_id,)).fetchone()
        if not row: raise HTTPException(409,'No hay resultado listo para revisar')
        if row[2]!=row[3]: raise HTTPException(409,'Propuesta obsoleta: cambió el origen')
        unknown=set(x.fields)-set(row[1] or {})
        if unknown: raise HTTPException(400,f'Campos no disponibles: {sorted(unknown)}')
        approved={k:row[1][k] for k in x.fields}
        c.execute('UPDATE localization_results SET approved_fields=%s,approved_by=%s WHERE id=%s',(Jsonb(approved),user,row[0]))
        c.execute("UPDATE localization_jobs SET state='APPROVED',updated_at=now() WHERE id=%s",(job_id,))
    return {'job_id':job_id,'approved_fields':approved,'publication':'requires explicit publish call; simulation only'}
@app.post('/api/jobs/{job_id}/publish')
def publish(job_id:int,user=Depends(operator)):
    if os.getenv('SIMULATION','true').lower()!='true': raise HTTPException(409,'Publicación Shopify deshabilitada en este piloto')
    with pool.connection() as c:
        row=c.execute("SELECT r.id,r.approved_fields,j.state,sp.shopify_product_gid FROM localization_results r JOIN localization_jobs j ON j.id=r.job_id LEFT JOIN store_products sp ON sp.product_key=j.product_key AND sp.store_code=j.store_code WHERE j.id=%s ORDER BY r.id DESC LIMIT 1",(job_id,)).fetchone()
        if not row or row[2]!='APPROVED' or not row[1]: raise HTTPException(409,'Aprueba campos concretos antes de simular publicación')
        c.execute('INSERT INTO publication_records(job_id,result_id,fields,shopify_gid,simulated,outcomes) VALUES(%s,%s,%s,%s,true,%s)',(job_id,row[0],Jsonb(row[1]),row[3],Jsonb({k:'SIMULATED_NO_WRITE' for k in row[1]})))
        # A simulated record is not a publication; keep the job approved.
    return {'job_id':job_id,'simulated':True,'shopify_writes':0}
