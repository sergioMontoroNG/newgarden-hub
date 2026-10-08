import hashlib, json, os, html, re, time
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from psycopg.types.json import Jsonb
from .db import pool, initialize
from .quality import assess_translation
from .profiles import active_profiles, target_profile

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
    owner_id:str=Field(min_length=8,max_length=255)
    locale:str=Field(min_length=2,max_length=20)
    key:str=Field(min_length=1,max_length=255)
    value:str=Field(max_length=200000)
    namespace:str|None=Field(default=None,max_length=255)
    metafield_key:str|None=Field(default=None,max_length=255)
    metafield_type:str|None=Field(default=None,max_length=100)
    compare_digest:str|None=Field(default=None,max_length=255)
    source_value:str|None=Field(default=None,max_length=200000)
    source_product_gid:str|None=Field(default=None,max_length=255)
class GlossaryTerm(BaseModel):
    source_store:str=Field(min_length=2,max_length=12)
    source_locale:str=Field(default='es-ES',min_length=2,max_length=20)
    target_store:str=Field(min_length=2,max_length=12)
    target_locale:str=Field(min_length=2,max_length=20)
    source_term:str=Field(min_length=1,max_length=500)
    target_term:str=Field(min_length=1,max_length=500)
    enabled:bool=True
class CandidateDecision(BaseModel):
    target_term:str=Field(min_length=1,max_length=500)
class CellTranslateRequest(BaseModel):
    source_text:str=Field(min_length=1,max_length=120000)
    source_store:str=Field(default='ES',min_length=2,max_length=12)
    source_locale:str=Field(default='es-ES',min_length=2,max_length=20)
    target_store:str=Field(min_length=2,max_length=12)
    target_locale:str=Field(min_length=2,max_length=20)
    html:bool=False
    field_key:str=Field(min_length=1,max_length=255)
    metafield_type:str|None=None
BATCH_BASE_FIELDS={'descriptionHtml','seo.title','seo.description'}
class TranslationBatchRequest(BaseModel):
    sku:str=Field(min_length=1,max_length=120)
    # Optional campaign targeting. Without `fields` the batch keeps its original
    # scope (description, SEO and every metafield). Accepted values: the base keys
    # above, `metafields` (all of them) or `metafield:<namespace>.<key>`.
    fields:list[str]|None=Field(default=None,min_length=1,max_length=100)
    store:str|None=Field(default=None,pattern=r'^[A-Z0-9_-]{2,12}$')
    locale:str|None=Field(default=None,min_length=2,max_length=20)
    # Other SKUs of the same product and whether variant metafields are limited
    # to the variants of `sku` and `also_skus` instead of every variant.
    also_skus:list[str]=Field(default_factory=list,max_length=50)
    variants_of_skus_only:bool=False

    @field_validator('fields')
    @classmethod
    def check_fields(cls,value):
        if value is None: return value
        cleaned=[]
        for item in value:
            item=item.strip()
            if item not in BATCH_BASE_FIELDS and item!='metafields' and not re.fullmatch(r'metafield:[^\s.:]{1,255}\.[^\s:]{1,255}',item):
                raise ValueError(f'Campo no válido: {item}')
            if item not in cleaned: cleaned.append(item)
        return cleaned

def batch_field_selected(key:str,specs:list[str]|None)->bool:
    if specs is None: return key in BATCH_BASE_FIELDS or key.startswith('metafield.')
    if key in specs: return True
    if not key.startswith('metafield.'): return False
    if 'metafields' in specs: return True
    for spec in specs:
        if not spec.startswith('metafield:'): continue
        ns_key=spec[len('metafield:'):]
        if key==f'metafield.product.{ns_key}' or (key.startswith('metafield.variant.') and key.endswith('.'+ns_key)): return True
    return False
class TranslationBatchResolve(BaseModel):
    skus:list[str]=Field(min_length=1,max_length=50)
class TranslationBatchItemIds(BaseModel):
    item_ids:list[int]=Field(min_length=1,max_length=500)
class TranslationBatchItemEdit(BaseModel):
    proposed_text:str=Field(max_length=200000)
class StoreConfig(BaseModel):
    code:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    domain:str=Field(min_length=3,max_length=255)
    token:str=Field(default='',max_length=500)
    locales:list[str]=Field(default_factory=list)

# Shared presentation assets are bundled by Docker's COPY hub ./hub.
from pathlib import Path

STYLE = '<style>' + Path(__file__).with_name('ui.css').read_text(encoding='utf-8') + '</style>'

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

PRODUCT_SELECTION='''id title descriptionHtml handle vendor productType status tags createdAt updatedAt seo { title description } featuredMedia { preview { image { url altText } } } images(first:50) { nodes { id url altText } } variants(first:100) { nodes { id title sku barcode position selectedOptions { name value } } pageInfo { hasNextPage endCursor } } metafields(first:100) { nodes { id namespace key type value compareDigest description } pageInfo { hasNextPage endCursor } }'''

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
    selection=PRODUCT_SELECTION.replace('metafields(first:100) { nodes { id namespace key type value compareDigest description } }',f'metafields(first:100) {{ nodes {{ id namespace key type value compareDigest description {mf_tr} }} }}')
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
                query=f'''query MetafieldPage($id:ID!,$after:String) {{ node(id:$id) {{ ... on {"Product" if field=="metafields" and owner_id==gid else "ProductVariant"} {{ {field}(first:100,after:$after) {{ nodes {{ id namespace key type value compareDigest description {mf_selection()} }} pageInfo {{ hasNextPage endCursor }} }} }} }} }}'''
                owner=shopify_graphql(store['domain'],store['token'],query,{'id':owner_id,'after':page.get('endCursor')}).get('node') or {}
                connection=owner.get(field,{})
                nodes.extend(connection.get('nodes',[])); page=connection.get('pageInfo',{})
                if not page.get('endCursor') and page.get('hasNextPage'): raise HTTPException(502,'Shopify no devolvió el cursor de metacampos')
            return nodes
        data['metafields']['nodes']=collect_connection(gid,'metafields',data.get('metafields',{}))
        variants=data.get('variants',{}).get('nodes',[])
        variant_page=data.get('variants',{}).get('pageInfo',{})
        while variant_page.get('hasNextPage'):
            page_query='''query VariantPage($id:ID!,$after:String) { product(id:$id) { variants(first:100,after:$after) { nodes { id title sku barcode position selectedOptions { name value } } pageInfo { hasNextPage endCursor } } } }'''
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

def is_nl_pt_locale(locale):
    return locale.replace('_','-').split('-')[0].casefold() in ('nl','pt')

def shopify_shop_locales(store):
    query='''query EnabledShopLocales { shopLocales { locale name primary published } }'''
    return shopify_graphql(store['domain'],store['token'],query).get('shopLocales',[])

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
    icons = {
        'home': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
        'products': '<path d="m12 3 9 5v8l-9 5-9-5V8l9-5Z"/><path d="m3 8 9 5 9-5M12 13v8M7.5 5.5l9 5"/>',
        'batch': '<rect x="7" y="7" width="14" height="14" rx="2"/><path d="M17 7V3H3v14h4M11 12h6M11 16h6"/>',
        'review': '<path d="M4 4h16v16H4zM8 9h8M8 13h5"/><path d="m14 17 2 2 4-4"/>',
        'glossary': '<path d="M12 5v16M12 5C8 2 4 3 2 4v15c3-1 6-1 10 2 4-3 7-3 10-2V4c-3-1-6-2-10 1Z"/>',
        'stores': '<path d="M3 10v11h18V10M2 10l2-7h16l2 7M2 10h20M9 21v-7h6v7"/>',
    }
    items = [('home','/','Resumen'),('products','/products','Gestión de productos'),('batch','/products/batch','Traducción en lote'),('review','/translation-batches','Revisión de lotes'),('glossary','/glossary','Glosario'),('stores','/stores','Tiendas')]
    links = ''.join(f'<a class="nav-link {"active" if key==active else ""}" href="{url}" {"aria-current=page" if key==active else ""}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icons[key]}</svg>{label}</a>' for key,url,label in items)
    mark = '<span class="dotmark" aria-hidden="true">' + '<i></i>' * 9 + '</span>'
    body = body.replace('<main class="wrap">', '<main class="wrap" id="main-content" tabindex="-1">', 1)
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#202121"><title>{html.escape(title)} · Newgarden Hub</title>{STYLE}</head><body><a class="skip-link" href="#main-content">Saltar al contenido</a><aside class="sidebar"><a class="brand" href="/" aria-label="Newgarden Content Hub, inicio">{mark}<span><strong translate="no">newgarden</strong><small>CONTENT HUB</small></span></a><nav aria-label="Navegación principal">{links}</nav><div class="sidebar-foot"><p>Contenido global.<br>Control en cada palabra.</p><span>NEWGARDEN / WORKSPACE</span></div></aside><div class="workspace"><header class="workspace-bar"><div class="breadcrumb"><span>Content Hub</span><span aria-hidden="true">/</span>{html.escape(title)}</div><span class="workspace-label">Espacio de trabajo interno</span></header>{body}<footer class="workspace-footer"><span>NEWGARDEN · CONTENT OPERATIONS</span><a href="/docs">Documentación API ↗</a></footer></div></body></html>'''

@app.get('/health')
def health():
    with pool.connection() as c: c.execute('SELECT 1')
    return {'status':'ok'}

@app.get('/',response_class=HTMLResponse)
def home():
    with pool.connection() as c:
        glossary_count=c.execute('SELECT count(*) FROM translation_glossary WHERE enabled').fetchone()[0]
        memory_count=c.execute('SELECT count(*) FROM translation_memory').fetchone()[0]
        store_count=c.execute('SELECT count(*) FROM stores WHERE active').fetchone()[0]
    deepl_ready=bool(os.getenv('DEEPL_AUTH_KEY','').strip())
    provider_status='Configurado' if deepl_ready else 'Falta configurar la clave'
    provider_class='ready' if deepl_ready else 'needs-setup'
    body=f'''<main class="wrap"><section class="overview-intro"><div><span class="section-label">Catálogo / Traducción / Revisión</span><h1>Tu catálogo.<br>En cada idioma.</h1><p class="lede">Un espacio para traducir, comparar y cuidar cada palabra de tu catálogo. Con DeepL y el criterio de tu equipo.</p><div class="actions"><a class="button primary" href="/products">Explorar productos <span aria-hidden="true">↗</span></a><a class="button secondary" href="/products/batch">Traducir en lote</a></div></div><section class="workflow" aria-label="Flujo de traducción"><div class="workflow-head"><span>DE ORIGEN A DESTINO</span><span class="dotmark" aria-hidden="true">{'<i></i>' * 9}</span></div><ol><li><div><strong>Encuentra el producto</strong><small>Consulta tu catálogo en las tiendas conectadas.</small></div></li><li><div><strong>Traduce con contexto</strong><small>Aplica tu glosario y las correcciones guardadas.</small></div></li><li><div><strong>Revisa y guarda</strong><small>Decide qué campos llegan a Shopify.</small></div></li></ol></section></section><section class="overview-section" aria-labelledby="system-title"><div class="section-heading"><h2 id="system-title">Tu espacio de trabajo</h2><span class="muted">Configuración y conocimiento compartido</span></div><div class="system-grid"><article class="system-item"><h3>Motor de traducción</h3><strong class="{provider_class}">DeepL · {provider_status}</strong><p class="muted">La clave permanece en el servidor.</p></article><article class="system-item"><h3>Memoria y glosario</h3><strong>{memory_count} textos · {glossary_count} términos</strong><p class="muted">Correcciones que ayudan en la siguiente traducción.</p></article><article class="system-item"><h3>Catálogo conectado</h3><strong>{store_count} tiendas activas</strong><p class="muted">Conexiones disponibles para consultar el catálogo.</p></article></div></section><div class="resource-links"><a class="resource-link" href="/glossary"><strong>Una voz, en todos los idiomas</strong><span class="muted">Gestiona tus términos preferidos en el glosario.</span><span aria-hidden="true">↗</span></a><a class="resource-link" href="/stores"><strong>Conecta tus mercados</strong><span class="muted">Configura las tiendas y sus idiomas disponibles.</span><span aria-hidden="true">↗</span></a></div></main>'''
    return shell('Resumen',body,'home')

@app.get('/stores',response_class=HTMLResponse)
def stores_page():
    body='''<main class="wrap"><h1>Tiendas Shopify</h1><p class="lede">Configura las conexiones y los idiomas disponibles para cada tienda.</p><div id="stores" class="grid"><div class="card">Cargando tiendas…</div></div><section class="card"><h2>Añadir tienda</h2><form id="store-form"><div class="grid"><div class="field"><label for="code">Código</label><input id="code" placeholder="FR" pattern="[A-Z0-9_-]{2,12}" required></div><div class="field"><label for="domain">Dominio Shopify</label><input id="domain" placeholder="mi-tienda.myshopify.com" required></div><div class="field"><label for="token">Token de acceso Admin API</label><input id="token" type="password" autocomplete="new-password" placeholder="shpat_…"></div><div class="field"><label for="locales">Locales (separados por coma)</label><input id="locales" placeholder="fr-FR, en-GB"></div></div><div class="actions"><button>Guardar tienda</button><span id="message" class="status" role="status"></span></div></form><p class="muted">Los tokens se guardan en la base de datos de esta instancia. El token vacío conserva el actual al editar.</p></section></main>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadStores(){const r=await fetch('/api/stores');const data=await r.json();document.querySelector('#stores').innerHTML=data.map(s=>`<article class="card"><span class="badge">${s.active?'ACTIVA':'INACTIVA'}</span><h2>${esc(s.code)}</h2><p>${esc(s.domain||'Dominio sin configurar')}</p><p class="muted">Token: ${s.has_token?'•••••••• configurado':'sin configurar'} · Locales configurados: ${esc(s.locales.join(', ')||'ninguno')}</p><button class="secondary" data-code="${esc(s.code)}">Editar configuración</button> <button class="secondary" data-fetch-locales="${esc(s.code)}" ${!s.active||!s.has_token?'disabled':''}>Consultar locales Shopify</button><div class="muted" id="shop-locales-${esc(s.code)}"></div></article>`).join('')||'<div class="card">No hay tiendas configuradas.</div>';document.querySelectorAll('[data-code]').forEach(b=>b.addEventListener('click',()=>editStore(data.find(s=>s.code===b.dataset.code))));document.querySelectorAll('[data-fetch-locales]').forEach(button=>button.addEventListener('click',async()=>{const s=data.find(item=>item.code===button.dataset.fetchLocales),panel=document.querySelector('#shop-locales-'+s.code);panel.textContent='Consultando Shopify…';const response=await fetch('/api/stores/'+encodeURIComponent(s.code)+'/shop-locales'),result=await response.json();if(!response.ok){panel.textContent=result.detail||'No se pudieron consultar los locales';return}const selected=result.filter(item=>item.published);panel.innerHTML='<p><strong>Locales habilitados en Shopify:</strong></p><ul>'+result.map(item=>`<li><code>${esc(item.locale)}</code> · ${esc(item.name)}${item.primary?' · principal':''}${item.published?' · publicado':' · no publicado'}</li>`).join('')+'</ul><button type="button" class="secondary use-shop-locales">Cargar todos los locales publicados</button>';panel.querySelector('.use-shop-locales').addEventListener('click',()=>{editStore(s);document.querySelector('#locales').value=selected.map(item=>item.locale).join(', ')})}))}
function editStore(s){document.querySelector('#code').value=s.code;document.querySelector('#code').readOnly=true;document.querySelector('#domain').value=s.domain||'';document.querySelector('#token').value='';document.querySelector('#locales').value=s.locales.join(', ');document.querySelector('#store-form').scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});document.querySelector('#domain').focus({preventScroll:true})}
document.querySelector('#store-form').addEventListener('submit',async e=>{e.preventDefault();const payload={code:document.querySelector('#code').value.trim().toUpperCase(),domain:document.querySelector('#domain').value.trim(),token:document.querySelector('#token').value,locales:document.querySelector('#locales').value.split(',').map(x=>x.trim()).filter(Boolean)};const r=await fetch('/api/stores',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const m=document.querySelector('#message');if(r.ok){m.textContent='Configuración guardada';document.querySelector('#store-form').reset();document.querySelector('#code').readOnly=false;loadStores()}else{const x=await r.json();m.textContent=x.detail||'No se pudo guardar'}});loadStores();
</script>'''
    return shell('Tiendas',body,'stores')

@app.get('/glossary',response_class=HTMLResponse)
def glossary_page():
    body='''<main class="wrap"><h1>Glosario y aprendizaje</h1><p class="lede">Las reglas se limitan a una tienda origen, su locale y una tienda destino. El locale principal de Shopify queda seleccionado por defecto; puedes elegir otro locale publicado.</p><p><a class="button secondary" href="/glossary/review">Revisar candidatos del catálogo</a></p><section class="card"><h2>Añadir término</h2><form id="term-form"><div class="grid"><div class="field"><label for="source-store">Tienda origen</label><select id="source-store" name="source_store" required></select></div><div class="field"><label for="source-locale">Idioma origen</label><select id="source-locale" name="source_locale" required disabled><option value="">Elige una tienda</option></select></div><div class="field"><label for="target-store">Tienda destino</label><select id="target-store" name="target_store" required></select></div><div class="field"><label for="target-locale">Idioma destino</label><select id="target-locale" name="target_locale" required disabled><option value="">Elige una tienda</option></select></div><div class="field"><label for="source-term">Término origen</label><input id="source-term" name="source_term" required></div><div class="field"><label for="target-term">Traducción preferida</label><input id="target-term" name="target_term" required></div></div><p id="locale-status" class="muted" role="status"></p><button id="save-term">Guardar término</button> <span id="term-status" class="status" role="status"></span></form></section><section class="card"><h2>Términos guardados</h2><div id="terms" tabindex="0" role="region" aria-label="Términos guardados">Cargando…</div></section></main><script>
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));let stores=[];
const sourceStore=document.querySelector('#source-store'),targetStore=document.querySelector('#target-store'),sourceLocale=document.querySelector('#source-locale'),targetLocale=document.querySelector('#target-locale'),localeStatus=document.querySelector('#locale-status');
async function loadStores(){const r=await fetch('/api/stores'),data=await r.json();stores=data.filter(s=>s.active);const options=stores.map(s=>`<option value="${esc(s.code)}">${esc(s.code)} · ${esc(s.domain)}</option>`).join('');sourceStore.innerHTML=options;targetStore.innerHTML=options;await Promise.all([loadLocales(sourceStore.value,sourceLocale),loadLocales(targetStore.value,targetLocale)]);}
async function loadLocales(code,select){select.disabled=true;select.innerHTML='<option value="">Consultando Shopify…</option>';try{const r=await fetch('/api/stores/'+encodeURIComponent(code)+'/shop-locales'),data=await r.json();if(!r.ok)throw new Error(data.detail||'Shopify no devolvió los idiomas');const shopLocales=data.filter(x=>x.published);const configured=stores.find(s=>s.code===code)?.locales||[];const choices=shopLocales.filter(x=>configured.includes(x.locale));if(!choices.length)throw new Error('No hay locales publicados y configurados a la vez. Configúralos en Tiendas.');select.innerHTML=choices.map(x=>`<option value="${esc(x.locale)}">${esc(x.locale)}${x.primary?' · principal':''}</option>`).join('');const primary=choices.find(x=>x.primary);select.value=(primary||choices[0]).locale;select.disabled=false;localeStatus.textContent='Locales publicados en Shopify; el principal está marcado y seleccionado por defecto.';return true}catch(error){select.innerHTML='<option value="">No se pudieron validar los locales</option>';select.disabled=true;localeStatus.textContent=error.message+' · Revisa la conexión y los permisos read_locales en Tiendas.';return false}}
sourceStore.addEventListener('change',()=>loadLocales(sourceStore.value,sourceLocale));targetStore.addEventListener('change',()=>loadLocales(targetStore.value,targetLocale));
async function loadTerms(){const r=await fetch('/api/glossary');let d;try{d=await r.json()}catch{document.querySelector('#terms').textContent='La API devolvió una respuesta no válida';return}if(!r.ok){document.querySelector('#terms').textContent=d.detail||'No se pudo cargar el glosario';return}document.querySelector('#terms').innerHTML=d.terms.length?'<table><tr><th>Tienda origen</th><th>Idioma</th><th>Tienda destino</th><th>Idioma</th><th>Término</th><th>Traducción</th><th>Acciones</th></tr>'+d.terms.map(t=>`<tr><td>${esc(t.source_store||'Todas (regla antigua)')}</td><td>${esc(t.source_locale)}</td><td>${esc(t.target_store||'Todas (regla antigua)')}</td><td>${esc(t.target_locale)}</td><td>${esc(t.source_term)}</td><td>${esc(t.target_term)}</td><td><button class="secondary" onclick="removeTerm(${t.id})">Eliminar</button></td></tr>`).join('')+'</table>':'<p class="muted">Aún no hay términos.</p>'}
async function removeTerm(id){const r=await fetch('/api/glossary/'+id,{method:'DELETE'});if(r.ok)loadTerms()}
document.querySelector('#term-form').addEventListener('submit',async e=>{e.preventDefault();const save=document.querySelector('#save-term');save.disabled=true;const data=Object.fromEntries(new FormData(e.target));const r=await fetch('/api/glossary',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});let result={};try{result=await r.json()}catch{}document.querySelector('#term-status').textContent=r.ok?'Guardado':(result.detail||'No se pudo guardar');save.disabled=false;if(r.ok){document.querySelector('#source-term').value='';document.querySelector('#target-term').value='';loadTerms()}});loadStores();loadTerms();
</script>'''
    return shell('Glosario',body,'glossary')

@app.get('/glossary/review',response_class=HTMLResponse)
def glossary_review_page():
    body="""<main class="wrap"><h1>Revisión de candidatos de glosario</h1><p class="lede">Términos propuestos a partir del catálogo ya traducido. Elige el valor canónico de cada tienda (puedes escribir otro o tomar una alternativa) y apruébalo para que se aplique en las traducciones. Nada se aplica hasta aprobarlo.</p><section class="card"><form id="filters"><div class="grid"><div class="field"><label for="f-store">Tienda destino</label><select id="f-store"></select></div><div class="field"><label for="f-status">Estado</label><select id="f-status"><option value="pending">Pendientes</option><option value="approved">Aprobados</option><option value="rejected">Rechazados</option><option value="">Todos</option></select></div><div class="field"><label for="f-kind">Tipo</label><select id="f-kind"><option value="">Todos</option><option value="term">Traducciones</option><option value="invariant">No traducir (nombres, modelos)</option></select></div><div class="field"><label for="f-conflicts">Inconsistencias</label><select id="f-conflicts"><option value="">Todas</option><option value="1">Solo con alternativas</option></select></div></div></form><p id="summary" class="muted" role="status"></p></section><section class="card"><div id="candidates" tabindex="0" role="region" aria-label="Candidatos de glosario">Cargando…</div></section></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const box=document.querySelector('#candidates'),summary=document.querySelector('#summary'),ids=['#f-store','#f-status','#f-kind','#f-conflicts'];
const badge={pending:'Pendiente',approved:'Aprobado',rejected:'Rechazado'};
async function load(){const q=new URLSearchParams();const v=id=>document.querySelector(id).value;if(v('#f-store'))q.set('store',v('#f-store'));if(v('#f-status'))q.set('status',v('#f-status'));if(v('#f-kind'))q.set('kind',v('#f-kind'));if(v('#f-conflicts'))q.set('conflicts','1');
 let d;try{const r=await fetch('/api/glossary-candidates?'+q);d=await r.json();if(!r.ok)throw new Error(d.detail||'No se pudieron cargar los candidatos')}catch(e){box.textContent=e.message;return}
 const sel=document.querySelector('#f-store');if(sel.dataset.ready!=='1'){sel.innerHTML='<option value="">Todas</option>'+d.stores.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');sel.dataset.ready='1'}
 summary.textContent=d.candidates.length+' candidatos · '+Object.entries(d.counts).map(([k,n])=>(badge[k]||k)+': '+n).join(' · ');
 if(!d.candidates.length){box.innerHTML='<p class="muted">No hay candidatos con estos filtros.</p>';return}
 box.innerHTML='<table><tr><th>Tienda · idioma</th><th>Término ES</th><th>Valor canónico</th><th>Alternativas detectadas</th><th>Evidencia</th><th>Estado</th><th>Acciones</th></tr>'+d.candidates.map(c=>{
  const alts=[{term:c.original_term,support:c.support,dice:c.dice,main:true},...c.alternatives.map(a=>({term:a.term,support:a.support,dice:a.dice}))];
  const locked=c.status!=='pending';
  return `<tr data-id="${c.id}"><td>${esc(c.target_store)} · ${esc(c.target_locale)}</td><td><strong>${esc(c.source_term)}</strong>${c.kind==='invariant'?'<br><span class="badge">No traducir</span>':''}</td><td><input class="canon" value="${esc(c.target_term)}" aria-label="Valor canónico de ${esc(c.source_term)}" ${locked?'disabled':''}>${c.existing&&c.existing!==c.target_term?`<br><small class="muted">En glosario: ${esc(c.existing)}</small>`:''}</td><td>${locked?'':alts.map(a=>`<button type="button" class="secondary pick" data-term="${esc(a.term)}" title="${a.support} productos · afinidad ${a.dice}">${esc(a.term)} <small>(${a.support})</small></button>`).join(' ')}</td><td>${c.support} de ${c.source_products} productos<br><small class="muted">afinidad ${c.dice}</small></td><td><span class="badge">${esc(badge[c.status]||c.status)}</span></td><td class="actions">${c.status==='pending'?'<button type="button" class="approve">Aprobar</button> <button type="button" class="secondary reject">Rechazar</button>':'<button type="button" class="secondary reopen">Reabrir</button>'}<span class="status" role="status"></span></td></tr>`}).join('')+'</table>'}
box.addEventListener('click',async e=>{const row=e.target.closest('tr[data-id]');if(!row)return;const status=row.querySelector('.status'),input=row.querySelector('.canon');
 const pick=e.target.closest('.pick');if(pick){input.value=pick.dataset.term;return}
 const action=['approve','reject','reopen'].find(a=>e.target.classList.contains(a));if(!action)return;
 const button=e.target;button.disabled=true;status.textContent='';
 try{const r=await fetch(`/api/glossary-candidates/${row.dataset.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_term:input.value.trim()||'-'})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'No se pudo guardar');load()}catch(err){status.textContent=err.message;button.disabled=false}});
for(const id of ids)document.querySelector(id).addEventListener('change',load);load();
</script>"""
    return shell('Revisión de glosario',body,'glossary')

@app.get('/api/glossary-candidates')
def list_glossary_candidates(store:str|None=None,status:str|None=None,kind:str|None=None,conflicts:str|None=None,user=Depends(operator)):
    where=[]; params=[]
    if store: where.append('c.target_store=%s'); params.append(store)
    if status: where.append('c.status=%s'); params.append(status)
    if kind: where.append('c.kind=%s'); params.append(kind)
    if conflicts: where.append("jsonb_array_length(c.alternatives)>0")
    clause=('WHERE '+' AND '.join(where)) if where else ''
    with pool.connection() as c:
        rows=c.execute(f"""SELECT c.id,c.target_store,c.target_locale,c.source_term,c.target_term,c.kind,c.support,c.source_products,c.dice,c.alternatives,c.status,
              (SELECT g.target_term FROM translation_glossary g WHERE g.source_store='ES' AND g.target_store=c.target_store AND lower(g.target_locale)=lower(c.target_locale)
                 AND lower(g.source_term)=lower(c.source_term) AND g.enabled LIMIT 1)
            FROM glossary_candidates c {clause}
            ORDER BY (jsonb_array_length(c.alternatives)>0) DESC,c.support DESC,c.id LIMIT 500""",params).fetchall()
        stores=[r[0] for r in c.execute('SELECT DISTINCT target_store FROM glossary_candidates ORDER BY 1').fetchall()]
        counts=dict(c.execute('SELECT status,count(*) FROM glossary_candidates'+(' WHERE target_store=%s' if store else '')+' GROUP BY status',[store] if store else []).fetchall())
    keys=('id','target_store','target_locale','source_term','target_term','kind','support','source_products','dice','alternatives','status','existing')
    out=[dict(zip(keys,r)) for r in rows]
    for item in out: item['original_term']=item['target_term']
    return {'candidates':out,'stores':stores,'counts':counts}

def _candidate_or_404(c,candidate_id):
    row=c.execute('SELECT id,target_store,target_locale,source_term,target_term,status FROM glossary_candidates WHERE id=%s FOR UPDATE',(candidate_id,)).fetchone()
    if not row: raise HTTPException(404,'Candidato no encontrado')
    return row

@app.post('/api/glossary-candidates/{candidate_id}/approve')
def approve_glossary_candidate(candidate_id:int,x:CandidateDecision,user=Depends(operator)):
    term=x.target_term.strip()
    if not term: raise HTTPException(400,'Indica el valor canónico')
    with pool.connection() as c:
        _,store,locale,source_term,_,status=_candidate_or_404(c,candidate_id)
        c.execute("""INSERT INTO translation_glossary(source_store,source_locale,target_store,target_locale,source_term,target_term,enabled,origin)
          VALUES('ES','es-ES',%s,%s,%s,%s,true,'historical_review')
          ON CONFLICT (source_store,source_locale,target_store,target_locale,(lower(source_term)))
          WHERE source_store IS NOT NULL AND target_store IS NOT NULL
          DO UPDATE SET target_term=EXCLUDED.target_term,enabled=true,origin=EXCLUDED.origin,updated_at=now()""",(store,locale,source_term,term))
        c.execute("UPDATE glossary_candidates SET status='approved',target_term=%s WHERE id=%s",(term,candidate_id))
    return {'approved':True}

@app.post('/api/glossary-candidates/{candidate_id}/reject')
def reject_glossary_candidate(candidate_id:int,x:CandidateDecision|None=None,user=Depends(operator)):
    with pool.connection() as c:
        _candidate_or_404(c,candidate_id)
        c.execute("UPDATE glossary_candidates SET status='rejected' WHERE id=%s",(candidate_id,))
    return {'rejected':True}

@app.post('/api/glossary-candidates/{candidate_id}/reopen')
def reopen_glossary_candidate(candidate_id:int,x:CandidateDecision|None=None,user=Depends(operator)):
    with pool.connection() as c:
        _,store,locale,source_term,_,status=_candidate_or_404(c,candidate_id)
        # Solo retira la regla que creó esta revisión; los términos manuales no se tocan.
        if status=='approved':
            c.execute("DELETE FROM translation_glossary WHERE source_store='ES' AND target_store=%s AND target_locale=%s AND lower(source_term)=lower(%s) AND origin='historical_review'",(store,locale,source_term))
        c.execute("UPDATE glossary_candidates SET status='pending' WHERE id=%s",(candidate_id,))
    return {'reopened':True}

@app.get('/api/stores')
def list_stores():
    with pool.connection() as c:
        rows=c.execute("SELECT s.code,s.domain,s.active,COALESCE(s.config->>'admin_token','')<>'',COALESCE(array_agg(l.locale) FILTER (WHERE l.locale IS NOT NULL),'{}') FROM stores s LEFT JOIN store_locales l ON l.store_code=s.code GROUP BY s.code ORDER BY s.code").fetchall()
    return [{'code':r[0],'domain':r[1] or '', 'active':r[2], 'has_token':r[3], 'locales':r[4]} for r in rows]

@app.get('/api/stores/{code}/shop-locales')
def shop_locales(code:str):
    store=next((item for item in shopify_stores() if item['code']==code),None)
    if not store or not store['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales Shopify')
    profiles=active_profiles(code)
    locales=[]
    for item in shopify_shop_locales(store):
        profile=profiles.get(item.get('locale','').lower())
        if profile: locales.append({**item,'role':profile['role'],'deepl_target':profile['deepl_target']})
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
                        states={'CURRENT':'Al día','OUTDATED':'Desactualizado','CONFLICT':'Cambio en destino','MISSING':'Falta en destino','UNTRACKED':'Sin referencia'}
                        state=edit.get('sync_status','UNTRACKED')
                        shown=f'<span class="cell-display">{shown}</span><span class="sync-state sync-{state.lower()}">{states.get(state,state)}</span><button type="button" class="cell-edit" data-edit="{encoded}">Editar celda</button>'
                    cells.append(f'<td>{shown}</td>')
                rows.append(f'<tr><th>{esc(f["label"])}<br><code>{esc(f["key"])}</code></th>{"".join(cells)}</tr>')
            notices=''.join(f'<p class="muted">{esc(n)}</p>' for n in data['notices'])
            detail=f'''<section class="card"><h2>Panel de producto e idiomas</h2><p class="lede">Compara y edita los campos por tienda e idioma. El estado se controla campo a campo: al día, desactualizado, falta en destino, cambio en destino o sin referencia histórica. Los datos anteriores a este panel se marcan sin referencia hasta volver a guardarlos desde aquí.</p><h3>{esc(data['source']['title'])}</h3><p class="muted">SKU: {esc(data['source']['sku'] or '—')} · {esc(data['source']['id'])}</p>{notices}<div class="comparison-scroll" tabindex="0" role="region" aria-label="Comparación de contenido por tienda e idioma"><table><thead><tr><th>Campo</th>{cols}</tr></thead><tbody>{''.join(rows)}</tbody></table></div><dialog class="edit-dialog t-modal" id="cell-editor" aria-labelledby="edit-title"><form method="dialog" id="cell-edit-form"><h2 id="edit-title">Editar contenido</h2><p id="edit-context" class="muted"></p><p class="html-note muted">La descripción se guarda como HTML. Puedes editar etiquetas como &lt;p&gt;, &lt;ul&gt; y &lt;strong&gt;.</p><textarea id="edit-value" aria-label="Contenido"></textarea><div class="actions"><button type="button" class="secondary" id="translate-cell">Traducir con DeepL</button><button type="submit" id="save-cell">Guardar en Shopify</button><button type="button" class="secondary" id="cancel-cell">Cancelar</button><span id="edit-status" class="status" role="status"></span></div></form></dialog></section><script>
const editor=document.querySelector('#cell-editor'),form=document.querySelector('#cell-edit-form'),area=document.querySelector('#edit-value');let editTarget=null;
document.querySelectorAll('.cell-edit').forEach(button=>button.addEventListener('click',()=>{{editTarget=JSON.parse(button.dataset.edit);area.value=editTarget.value||'';document.querySelector('#edit-context').textContent=editTarget.store+' · '+editTarget.locale+' · '+editTarget.key+' · '+(editTarget.mode==='base'?'Contenido base':'Traducción');editor.classList.toggle('html-mode',editTarget.key==='body_html');document.querySelector('#edit-status').textContent='';document.querySelector('#translate-cell').disabled=editTarget.key==='handle'||!editTarget.source_value;openEditor()}}));
document.querySelectorAll('.more').forEach(button=>button.addEventListener('click',()=>{{button.previousElementSibling.textContent=button.dataset.full;button.remove()}}));
let closeTimer,openFrame;
function openEditor() {{clearTimeout(closeTimer);editor.classList.remove('is-closing');editor.showModal();openFrame=requestAnimationFrame(()=>editor.classList.add('is-open'))}}
function closeEditor() {{cancelAnimationFrame(openFrame);editor.classList.remove('is-open');editor.classList.add('is-closing');const duration=matchMedia('(prefers-reduced-motion: reduce)').matches?0:parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--modal-close-dur'))||150;closeTimer=setTimeout(()=>{{editor.close();editor.classList.remove('is-closing')}},duration)}}
editor.addEventListener('cancel',event=>{{event.preventDefault();closeEditor()}});
document.querySelector('#cancel-cell').addEventListener('click',closeEditor);
document.querySelector('#translate-cell').addEventListener('click',async()=>{{if(!editTarget)return;const button=document.querySelector('#translate-cell'),status=document.querySelector('#edit-status');button.disabled=true;status.textContent='Traduciendo…';try{{const response=await fetch('/api/translate-cell',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{source_text:editTarget.source_value||'',source_store:'ES',source_locale:'es-ES',target_store:editTarget.store,target_locale:editTarget.locale,field_key:editTarget.key,metafield_type:editTarget.metafield_type,html:editTarget.key==='body_html'||editTarget.metafield_type==='html'}})}});const result=await response.json();if(!response.ok)throw new Error(result.detail||'Error de traducción');area.value=result.translation;status.textContent=result.memory_hit?'Texto recordado':(result.glossary_terms?`Traducción lista · ${{result.glossary_terms}} término(s) del glosario aplicado(s)`:'Traducción lista para revisar')}}catch(error){{status.textContent=error.message}}finally{{button.disabled=false}}}});
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
    empty = '<section class="empty-state"><h2>Empieza con un producto</h2><p class="muted">Introduce un SKU, nombre o ID. Después selecciona un resultado de ES para comparar los idiomas y editar su contenido.</p></section>' if not term and not source_id else ''
    value=html.escape(term,quote=True)
    body=f'''<main class="wrap"><h1>Gestión de productos</h1><p class="lede">Busca por SKU, nombre, ID o texto en todas las tiendas activas. Abre un producto de España para revisar los campos y su estado por tienda e idioma.</p><section class="card search-panel"><form action="/products" method="get"><div class="field"><label for="term">SKU, nombre o ID de Shopify</label><input id="term" name="q" value="{value}" required placeholder="Ej. NG-1234, lámpara exterior o gid://shopify/Product/…"></div><div class="actions"><button type="submit">Buscar en tiendas</button></div></form></section><section class="batch-shortcut"><div><h2>¿Varios productos a la vez?</h2><p class="muted">Prepara una lista de SKU y revisa cada propuesta antes de guardar.</p></div><a class="button secondary" href="/products/batch">Traducción en lote <span aria-hidden="true">↗</span></a></section>{detail}{results}{empty}</main>'''
    return shell('Explorador de productos',body,'products')

@app.get('/products/batch',response_class=HTMLResponse)
def batch_products_page():
    body='''<main class="wrap"><h1>Campaña de traducción por SKU</h1><p class="lede">Elige varios SKU y, si quieres, solo los campos que te interesan para esta campaña. El servicio prepara las propuestas de un producto cada vez y las deja en <a href="/translation-batches">Revisión de lotes</a>. Nada se escribe en Shopify hasta que alguien revise, apruebe y aplique.</p><section class="card"><form id="batch-form"><div class="field"><label for="skus">SKU (uno por línea o separados por coma)</label><textarea id="skus" required maxlength="20000" placeholder="NG-1234&#10;NG-5678"></textarea></div><div class="grid"><div class="field"><label for="target-store">Tienda destino</label><select id="target-store" disabled><option>Cargando tiendas…</option></select></div><div class="field"><label for="target-locale">Locale destino</label><select id="target-locale" disabled><option value="">Todos los locales</option></select></div></div><fieldset class="field"><legend>Campos a traducir</legend><label><input type="checkbox" class="field-choice" value="descriptionHtml" checked style="width:auto"> Descripción</label><br><label><input type="checkbox" class="field-choice" value="seo.title" checked style="width:auto"> SEO · título</label><br><label><input type="checkbox" class="field-choice" value="seo.description" checked style="width:auto"> SEO · descripción</label><br><label><input type="checkbox" class="field-choice" value="metafields" style="width:auto"> Todos los metacampos (producto y variantes)</label><div id="meta-picker" class="field"><p class="muted" id="meta-status">Cargando metacampos de ES…</p></div><details class="field"><summary>Añadir metacampos a mano</summary><label for="meta-fields">Metacampos adicionales (<code>namespace.key</code>, uno por línea)</label><textarea id="meta-fields" rows="3" maxlength="4000" placeholder="custom.ficha_tecnica"></textarea></details><label><input type="checkbox" id="variant-only" checked style="width:auto"> Metacampos de variante: solo las variantes de los SKU indicados (si no, se traducen las de todas las variantes del producto)</label></fieldset><p id="batch-status" class="muted" role="status" aria-live="polite"></p><div class="actions"><button id="prepare-batch" type="submit">Preparar lotes para revisión</button><a class="button secondary" href="/products">Volver al explorador</a></div><p class="muted">La preparación consulta Shopify y DeepL y guarda los lotes en el hub; no escribe en Shopify. Máximo 50 SKU por ejecución.</p></form></section><section id="batch-results" aria-live="polite"></section></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const storeSelect=document.querySelector('#target-store'),localeSelect=document.querySelector('#target-locale'),status=document.querySelector('#batch-status'),results=document.querySelector('#batch-results');
async function fetchJSON(url,options){const r=await fetch(url,options);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Error al procesar la solicitud');return data}
async function loadStores(){try{const data=await fetchJSON('/api/stores'),usable=data.filter(s=>s.active&&s.has_token);if(!usable.length){storeSelect.innerHTML='<option value="">Sin tiendas activas</option>';status.textContent='Activa una tienda y configura su token en Tiendas para habilitarla.';return}storeSelect.innerHTML='<option value="">Todas las tiendas</option>'+usable.map(s=>`<option value="${esc(s.code)}">${esc(s.code)} · ${esc(s.domain||'sin dominio')}</option>`).join('');storeSelect.disabled=false}catch(e){storeSelect.innerHTML='<option value="">No disponible</option>';status.textContent=e.message}}
async function loadLocales(){localeSelect.innerHTML='<option value="">Todos los locales</option>';localeSelect.disabled=true;const code=storeSelect.value;if(!code)return;try{const data=await fetchJSON('/api/stores/'+encodeURIComponent(code)+'/shop-locales');localeSelect.innerHTML='<option value="">Todos los locales</option>'+data.filter(x=>x.published&&x.deepl_target&&!(code==='ES'&&x.primary)).map(x=>`<option value="${esc(x.locale)}">${esc(x.locale)}</option>`).join('');localeSelect.disabled=false}catch(e){status.textContent=e.message}}
storeSelect.addEventListener('change',loadLocales);loadStores();
async function loadMetaDefs(refresh){const box=document.querySelector('#meta-picker');box.innerHTML='<p class="muted" id="meta-status">Cargando metacampos de ES…</p>';try{const defs=await fetchJSON('/api/shopify/metafield-definitions'+(refresh?'?refresh=true':''));box.innerHTML=defs.length?`<label for="meta-search">Buscar metacampo</label><input id="meta-search" type="search" placeholder="Nombre o namespace.key" autocomplete="off"><p class="muted" id="meta-count" role="status" aria-live="polite"></p><div id="meta-list" style="max-height:280px;overflow:auto">${defs.map(d=>`<div class="meta-item" data-search="${esc((d.name+' '+d.value).toLowerCase())}"><label><input type="checkbox" class="meta-choice" value="${esc(d.value)}" style="width:auto"> ${esc(d.name)} <code>${esc(d.value)}</code> <span class="muted">· ${d.owners.map(o=>o==='product'?'producto':'variante').join(' + ')}</span></label></div>`).join('')}</div><button type="button" class="secondary" id="reload-meta">Recargar lista</button>`:'<p class="muted">ES no tiene definiciones de metacampos de texto.</p><button type="button" class="secondary" id="reload-meta">Recargar lista</button>';box.querySelector('#reload-meta').addEventListener('click',()=>loadMetaDefs(true));if(defs.length){const search=box.querySelector('#meta-search'),count=box.querySelector('#meta-count'),update=()=>{const q=search.value.trim().toLowerCase();let shown=0;for(const item of box.querySelectorAll('.meta-item')){const hide=!!q&&!item.dataset.search.includes(q);item.hidden=hide;if(!hide)shown++}count.textContent=shown+' de '+defs.length+' metacampos · '+box.querySelectorAll('.meta-choice:checked').length+' seleccionado(s)'};search.addEventListener('input',update);box.addEventListener('change',update);update()}}catch(e){box.innerHTML='<p class="muted">No se pudo cargar la lista ('+esc(e.message)+'). Puedes añadirlos a mano abajo.</p>'}}
loadMetaDefs(false);
function selectedFields(){const fields=[...document.querySelectorAll('.field-choice:checked')].map(x=>x.value);if(!fields.includes('metafields'))for(const c of document.querySelectorAll('.meta-choice:checked'))fields.push('metafield:'+c.value);if(!fields.includes('metafields'))for(const line of document.querySelector('#meta-fields').value.split(String.fromCharCode(10))){const v=line.trim();if(v)fields.push('metafield:'+v)}return fields}
function addResult(sku,html,ok){const card=document.createElement('section');card.className='card';card.innerHTML=`<h2>SKU ${esc(sku)}</h2>${html}`;results.append(card)}
document.querySelector('#batch-form').addEventListener('submit',async e=>{e.preventDefault();const skus=[...new Set(document.querySelector('#skus').value.replaceAll(',',String.fromCharCode(10)).split(String.fromCharCode(10)).map(x=>x.trim()).filter(Boolean))];if(!skus.length)return;if(skus.length>50){status.textContent='El máximo es 50 SKU por ejecución.';return}const fields=selectedFields();if(!fields.length){status.textContent='Selecciona al menos un campo a traducir.';return}const run=document.querySelector('#prepare-batch');run.disabled=true;results.replaceChildren();const base={fields};if(storeSelect.value)base.store=storeSelect.value;if(localeSelect.value)base.locale=localeSelect.value;base.variants_of_skus_only=document.querySelector('#variant-only').checked;let created=0,groups=[];status.textContent='Resolviendo '+skus.length+' SKU en ES…';try{const resolved=await fetchJSON('/api/translation-batches/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skus})});groups=resolved.groups;resolved.errors.forEach(x=>addResult(x.sku,`<p class="batch-error" role="alert">${esc(x.error)}</p>`))}catch(err){status.textContent=err.message;run.disabled=false;return}for(let i=0;i<groups.length;i++){const g=groups[i];status.textContent='Preparando '+(i+1)+'/'+groups.length+' · '+g.skus.join(', ')+'…';try{const batch=await fetchJSON('/api/translation-batches',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...base,sku:g.skus[0],also_skus:g.skus.slice(1)})});const pending=batch.items.filter(x=>x.state==='PENDING_REVIEW').length,approved=batch.items.filter(x=>x.state==='APPROVED').length,errors=batch.items.filter(x=>x.state==='ERROR').length;created++;addResult(g.skus.join(', '),`<p>${esc(batch.source_title||g.title)}</p>${g.skus.length>1?`<p class="muted">Estos ${g.skus.length} SKU son del mismo producto: se ha creado un solo lote.</p>`:''}<p class="muted">${batch.items.length} campo(s): ${pending} pendientes de revisión, ${approved} aprobados automáticamente, ${errors} con error.</p>${batch.items.filter(x=>x.state==='ERROR').slice(0,3).map(x=>`<p class="batch-error">${esc(x.store)} · ${esc(x.locale)}: ${esc(x.error||'')}</p>`).join('')}<a class="button secondary" href="/translation-batches#${batch.id}">Abrir lote #${batch.id}</a>`)}catch(err){addResult(g.skus.join(', '),`<p class="batch-error" role="alert">${esc(err.message)}</p>`)}}run.disabled=false;status.textContent=created+' lote(s) preparados de '+groups.length+' producto(s). Revísalos en Revisión de lotes.'});
</script>'''
    return shell('Traducción en lote',body,'batch')

@app.get('/translation-batches',response_class=HTMLResponse)
def translation_batches_page():
    body='''<main class="wrap"><h1>Revisión de lotes</h1><p class="lede">Revisa cada propuesta antes de aprobarla. Solo los campos aprobados se podrán aplicar a Shopify.</p><section class="card" aria-labelledby="batch-list-title"><div class="section-heading"><h2 id="batch-list-title">Lotes recientes</h2><button id="refresh-batches" class="secondary" type="button">Actualizar lotes</button></div><p id="batch-list-status" class="muted" role="status" aria-live="polite">Cargando lotes…</p><div id="batch-list"></div></section><section id="batch-detail" aria-label="Detalle del lote"></section></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const list=document.querySelector('#batch-list'),listStatus=document.querySelector('#batch-list-status'),detail=document.querySelector('#batch-detail');let current=null;
async function request(url,options){const r=await fetch(url,options);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||'No se pudo completar la solicitud');return d}
const date=s=>s?new Date(s).toLocaleString('es-ES'):'—';
async function loadBatches(){listStatus.textContent='Cargando lotes…';try{const d=await request('/api/translation-batches');listStatus.textContent=d.length?d.length+' lote(s) recientes.':'No hay lotes persistidos.';list.innerHTML=d.length?'<div class="batch-table-scroll"><table><caption class="sr-only">Lotes de traducción recientes</caption><thead><tr><th scope="col">Lote</th><th scope="col">SKU</th><th scope="col">Producto</th><th scope="col">Estado</th><th scope="col">Actualizado</th><th scope="col">Acción</th></tr></thead><tbody>'+d.map(b=>`<tr><th scope="row">#${b.id}</th><td>${esc(b.sku)}</td><td>${esc(b.source_title||'—')}</td><td>${esc(b.state)}</td><td>${esc(date(b.updated_at))}</td><td><button class="secondary open-batch" data-id="${b.id}" type="button" aria-label="Abrir revisión del lote ${b.id}, SKU ${esc(b.sku)}">Abrir revisión</button></td></tr>`).join('')+'</tbody></table></div>':'';list.querySelectorAll('.open-batch').forEach(b=>b.addEventListener('click',()=>loadDetail(Number(b.dataset.id))))}catch(e){listStatus.textContent=e.message}}
function itemStatus(i){return i.state==='PENDING_REVIEW'?'Pendiente de revisión':i.state==='APPROVED'?(i.quality?.status==='PASS'?'Aprobado automáticamente · pendiente de aplicar':'Aprobado · pendiente de aplicar'):i.state==='SAVED'?'Aplicado en Shopify':i.state==='APPLY_ERROR'?'Error al aplicar':i.state==='ERROR'?'Error de preparación':i.state}
function renderBatch(b){current=b;const pending=b.items.filter(i=>i.state==='PENDING_REVIEW'),approved=b.items.filter(i=>i.state==='APPROVED'),errors=b.items.filter(i=>['ERROR','APPLY_ERROR'].includes(i.state)),stores=[...new Set(b.items.map(i=>i.store))].sort(),locales=[...new Set(b.items.map(i=>i.locale))].sort();detail.innerHTML=`<section class="card batch-detail-card" aria-labelledby="batch-detail-title"><span class="badge">${esc(b.state)}</span><h2 id="batch-detail-title" tabindex="-1">Lote #${b.id} · ${esc(b.sku)}</h2><p>${esc(b.source_title||'')} · Creado ${esc(date(b.created_at))}</p>${b.error?`<p class="batch-error" role="alert">${esc(b.error)}</p>`:''}<div class="batch-summary" aria-label="Resumen del lote"><div><strong>${b.items.length}</strong><span>Campos</span></div><div><strong>${pending.length}</strong><span>Pendientes</span></div><div><strong>${approved.length}</strong><span>Aprobados</span></div><div><strong>${errors.length}</strong><span>Con errores</span></div></div><div class="batch-actions"><button id="approve-selected" type="button" ${pending.length?'':'disabled'}>Aprobar seleccionados (<span id="selected-count">0</span>)</button><button id="apply-approved" type="button" class="secondary" ${approved.length?'':'disabled'}>Aplicar aprobados a Shopify (${approved.length})</button></div><p class="muted">Puedes volver después por los campos pendientes que dejaste sin seleccionar: apruébalos y aplica el nuevo grupo. Aplicar aprobados escribe esos campos en Shopify.</p><p id="batch-action-status" class="status" role="status" aria-live="polite"></p><div class="batch-filters" aria-label="Filtros de campos"><div class="field"><label for="batch-filter-status">Estado</label><select id="batch-filter-status"><option value="">Todos los estados</option><option value="PENDING_REVIEW">Pendientes de revisión</option><option value="APPROVED">Aprobados</option><option value="ERROR">Errores</option><option value="SAVED">Aplicados</option></select></div><div class="field"><label for="batch-filter-store">Tienda</label><select id="batch-filter-store"><option value="">Todas las tiendas</option>${stores.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select></div><div class="field"><label for="batch-filter-locale">Idioma</label><select id="batch-filter-locale"><option value="">Todos los idiomas</option>${locales.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select></div><div class="field"><label for="batch-filter-text">Buscar campo</label><input id="batch-filter-text" type="search" placeholder="Descripción, variante, metacampo…"></div></div><label class="batch-select-visible"><input id="select-visible" type="checkbox" ${pending.length?'':'disabled'}> Seleccionar todos los pendientes visibles</label><div id="batch-items" class="batch-items">${b.items.length?b.items.map(i=>{const pendingItem=i.state==='PENDING_REVIEW',approvedItem=i.state==='APPROVED',proposalId=`batch-proposal-${i.id}`;return `<article class="batch-item ${i.state==='ERROR'||i.state==='APPLY_ERROR'?'has-error':''}" data-state="${esc(i.state)}" data-store="${esc(i.store)}" data-locale="${esc(i.locale)}" data-search="${esc((i.field_label||i.field_key).toLowerCase())}" aria-labelledby="batch-item-title-${i.id}"><div class="batch-item-head"><div><h3 id="batch-item-title-${i.id}">${esc(i.field_label||i.field_key)}</h3><p class="muted">${esc(i.store)} · ${esc(i.locale)}</p></div><span class="badge">${esc(itemStatus(i))}</span></div>${i.quality?`<p class="quality-note">Integridad: ${esc(i.quality.status)} · ${esc(i.quality.score)}/100${i.quality.hard_failures?.length?` · ${esc(i.quality.hard_failures.join('; '))}`:''}</p>`:''}${i.error?`<p class="batch-error" role="note"><strong>Requiere atención:</strong> ${esc(i.error)}</p>`:''}${pendingItem||approvedItem?`<div class="batch-translation-pair"><section><h4>Texto de origen</h4><pre>${esc(i.source_text||'—')}</pre></section>${pendingItem?`<div class="field"><label for="${proposalId}">Propuesta de traducción</label><textarea id="${proposalId}" class="batch-proposal" data-item="${i.id}" rows="${i.field_key==='descriptionHtml'?8:4}">${esc(i.proposed_text||'')}</textarea></div>`:`<section><h4>Propuesta aprobada</h4><pre>${esc(i.proposed_text||'—')}</pre></section>`}</div>${pendingItem?`<div class="batch-item-actions"><button class="secondary save-proposal" data-item="${i.id}" type="button">Guardar cambios</button><label class="batch-approve-label"><input class="approve-check" type="checkbox" value="${i.id}"> Confirmo que revisé esta traducción</label><span class="item-action-status" id="batch-item-status-${i.id}" role="status" aria-live="polite"></span></div>`:`<div class="batch-item-actions"><button class="secondary reopen-approved" data-item="${i.id}" type="button">Devolver a revisión</button><span class="item-action-status" id="batch-item-status-${i.id}" role="status" aria-live="polite"></span></div>`}`:`${i.source_text!=null||i.proposed_text!=null?`<details class="batch-source"><summary>Ver texto y propuesta</summary>${i.source_text!=null?`<h4>Texto de origen</h4><pre>${esc(i.source_text)}</pre>`:''}${i.proposed_text!=null?`<h4>Propuesta</h4><pre>${esc(i.proposed_text)}</pre>`:''}</details>`:''}`}</article>`}).join(''):'<p class="muted">El lote no contiene campos.</p>'}</div></section>`;
const actionStatus=detail.querySelector('#batch-action-status'),itemStatusNode=id=>detail.querySelector(`#batch-item-status-${id}`),items=detail.querySelector('#batch-items');
function applyFilters(){const status=detail.querySelector('#batch-filter-status').value,store=detail.querySelector('#batch-filter-store').value,locale=detail.querySelector('#batch-filter-locale').value,text=detail.querySelector('#batch-filter-text').value.trim().toLowerCase();for(const card of items.querySelectorAll('.batch-item')){card.hidden=!!((status&&(status==='ERROR'? !['ERROR','APPLY_ERROR'].includes(card.dataset.state):card.dataset.state!==status))||(store&&card.dataset.store!==store)||(locale&&card.dataset.locale!==locale)||(text&&!card.dataset.search.includes(text)));if(card.hidden){const checkbox=card.querySelector('.approve-check');if(checkbox)checkbox.checked=false}}const visiblePending=[...items.querySelectorAll('.batch-item:not([hidden]) .approve-check')];detail.querySelector('#select-visible').disabled=!visiblePending.length;const selected=detail.querySelectorAll('.approve-check:checked').length;detail.querySelector('#selected-count').textContent=selected;detail.querySelector('#approve-selected').disabled=!selected;detail.querySelector('#select-visible').checked=visiblePending.length>0&&visiblePending.every(x=>x.checked);const applyButton=detail.querySelector('#apply-approved');applyButton.disabled=!selected&&!approved.length;applyButton.textContent=selected?`Aplicar seleccionados (${selected})`:`Aplicar aprobados a Shopify (${approved.length})`;}
for(const selector of ['#batch-filter-status','#batch-filter-store','#batch-filter-locale','#batch-filter-text'])detail.querySelector(selector).addEventListener('input',applyFilters);detail.querySelector('#select-visible').addEventListener('change',event=>{for(const checkbox of items.querySelectorAll('.batch-item:not([hidden]) .approve-check'))checkbox.checked=event.target.checked;applyFilters()});items.addEventListener('change',event=>{if(event.target.matches('.approve-check'))applyFilters()});applyFilters();
detail.querySelectorAll('.save-proposal').forEach(btn=>btn.addEventListener('click',async()=>{const id=Number(btn.dataset.item),text=detail.querySelector(`.batch-proposal[data-item="${id}"]`).value,status=itemStatusNode(id);btn.disabled=true;status.textContent='Guardando…';try{await request(`/api/translation-batches/${b.id}/items/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposed_text:text})});await loadDetail(b.id);await loadBatches();detail.querySelector(`#batch-proposal-${id}`)?.focus({preventScroll:true});detail.querySelector('#batch-action-status').textContent='Cambios guardados y calidad reevaluada.'}catch(e){status.textContent='No se pudo guardar: '+e.message;btn.disabled=false}}));
detail.querySelectorAll('.reopen-approved').forEach(btn=>btn.addEventListener('click',async()=>{const id=Number(btn.dataset.item),status=itemStatusNode(id);btn.disabled=true;status.textContent='Devolviendo a revisión…';try{await request(`/api/translation-batches/${b.id}/reopen`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:[id]})});await loadDetail(b.id);await loadBatches();detail.querySelector(`#batch-proposal-${id}`)?.focus({preventScroll:true});detail.querySelector('#batch-action-status').textContent='Campo pendiente de revisión. Puedes editarlo o volver a aprobarlo.'}catch(e){status.textContent='No se pudo devolver a revisión: '+e.message;btn.disabled=false}}));
detail.querySelector('#approve-selected')?.addEventListener('click',async()=>{const ids=[...detail.querySelectorAll('.approve-check:checked')].map(x=>Number(x.value));if(!ids.length){actionStatus.textContent='Selecciona al menos una propuesta revisada.';return}const button=detail.querySelector('#approve-selected');button.disabled=true;actionStatus.textContent='Guardando aprobaciones…';try{await request(`/api/translation-batches/${b.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:ids})});await loadDetail(b.id);await loadBatches();detail.querySelector('#batch-action-status').textContent=`${ids.length} campo(s) aprobado(s).`}catch(e){actionStatus.textContent='No se pudieron guardar las aprobaciones: '+e.message;button.disabled=false}});
detail.querySelector('#apply-approved')?.addEventListener('click',async()=>{const ids=[...detail.querySelectorAll('.approve-check:checked')].map(x=>Number(x.value)),selected=ids.length>0;if(!selected&&!approved.length)return;const prompt=selected?`¿Aprobar y aplicar los ${ids.length} campos seleccionados a Shopify?`:`¿Aplicar ${approved.length} campo(s) aprobado(s) a Shopify?`;if(!confirm(prompt+' Esta acción escribe en la tienda.'))return;const button=detail.querySelector('#apply-approved');button.disabled=true;actionStatus.textContent=selected?'Aprobando y aplicando solo los campos seleccionados…':'Aplicando los campos aprobados en Shopify…';try{if(selected)await request(`/api/translation-batches/${b.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:ids})});await request(`/api/translation-batches/${b.id}/apply`,selected?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:ids})}:{method:'POST'});await loadDetail(b.id);await loadBatches();detail.querySelector('#batch-action-status').textContent=selected?`${ids.length} campo(s) aprobado(s) y aplicado(s).`:'Aplicación finalizada. Los campos pendientes siguen disponibles para otra selección.'}catch(e){actionStatus.textContent='No se pudieron aplicar los campos: '+e.message;button.disabled=false}})}
async function loadDetail(id){detail.innerHTML='<section class="card" role="status">Cargando lote…</section>';try{renderBatch(await request('/api/translation-batches/'+id));detail.querySelector('#batch-detail-title')?.focus({preventScroll:true})}catch(e){detail.innerHTML=`<section class="card"><p class="status" role="alert">${esc(e.message)}</p></section>`}}
document.querySelector('#refresh-batches').addEventListener('click',loadBatches);loadBatches();{const m=location.hash.match(/^#([0-9]+)$/);if(m)loadDetail(Number(m[1]))}
</script>'''
    return shell('Revisión de lotes',body,'review')

TEXT_METAFIELD_TYPES={'single_line_text_field','multi_line_text_field','html','rich_text_field','list.single_line_text_field','list.multi_line_text_field'}
_metafield_defs_cache={'at':0.0,'data':None}

@app.get('/api/shopify/metafield-definitions')
def list_metafield_definitions(refresh:bool=False):
    """Text metafield definitions of the ES store (product and variant), for campaign field pickers."""
    if not refresh and _metafield_defs_cache['data'] is not None and time.time()-_metafield_defs_cache['at']<600: return _metafield_defs_cache['data']
    store=next((s for s in shopify_stores() if s['code']=='ES' and s['token']),None)
    if not store: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    query='query Defs($owner:MetafieldOwnerType!,$after:String) { metafieldDefinitions(ownerType:$owner,first:100,after:$after) { nodes { namespace key name type { name } } pageInfo { hasNextPage endCursor } } }'
    found={}
    for owner,label in (('PRODUCT','product'),('PRODUCTVARIANT','variant')):
        after=None
        for _ in range(10):
            page=shopify_graphql(store['domain'],store['token'],query,{'owner':owner,'after':after}).get('metafieldDefinitions') or {}
            for d in page.get('nodes',[]):
                if d['type']['name'] not in TEXT_METAFIELD_TYPES or 'yotpo' in (d['namespace']+d['key']).casefold(): continue
                item=found.setdefault(f"{d['namespace']}.{d['key']}",{'value':f"{d['namespace']}.{d['key']}",'name':d['name'],'type':d['type']['name'],'owners':[]})
                item['owners'].append(label)
            if not page.get('pageInfo',{}).get('hasNextPage'): break
            after=page['pageInfo']['endCursor']
    data=sorted(found.values(),key=lambda x:x['value'])
    _metafield_defs_cache.update(at=time.time(),data=data)
    return data

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
def compare_shopify_product(source_id:str,target_store:str|None=None,target_locale:str|None=None):
    return _compare_shopify_product(source_id,target_store,target_locale)

def _compare_shopify_product(source_id:str,target_store:str|None=None,target_locale:str|None=None,target_locales:dict[str,list[str]]|None=None):
    if (target_store is None)!=(target_locale is None): raise HTTPException(400,'Indica tienda y locale destino juntos')
    stores=shopify_stores(); by_code={s['code']:s for s in stores}; source_store=by_code.get('ES')
    if not source_store or not source_store['token']: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    if target_store is not None:
        selected=by_code.get(target_store)
        if not selected: raise HTTPException(404,'Tienda destino inactiva o sin credenciales Shopify')
        published={item['locale'] for item in shopify_shop_locales(selected) if item.get('published')}
        if target_locale not in published: raise HTTPException(400,'El locale destino no está publicado en Shopify')
    source_locales=list(source_store['locales'])
    if target_store=='ES' and target_locale and target_locale not in source_locales:
        source_locales.append(target_locale)
    source_locales.extend(locale for locale in (target_locales or {}).get('ES',[]) if locale not in source_locales)
    source_lookup_locales=list(dict.fromkeys(shopify_locale_code(locale) for locale in source_locales))
    source=shopify_product(source_store,source_id,source_lookup_locales)
    if not source: raise HTTPException(404,'Producto no encontrado en la tienda ES')
    sku=next((v.get('sku') for v in source.get('variants',{}).get('nodes',[]) if v.get('sku')),'')
    targets=[]; notices=[]
    if source.get('translation_error'): notices.append('Shopify no devolvió las traducciones de los locales configurados en ES; esos valores aparecerán vacíos.')
    primary_locales={}
    for store in stores:
        if not store['token']: continue
        try:
            primary=next((item['locale'] for item in shopify_shop_locales(store) if item.get('primary')),None)
            if primary: primary_locales[store['code']]=primary
        except HTTPException as exc:
            notices.append(f'{store["code"]}: no se pudo identificar el locale principal ({exc.detail}); sus celdas no serán editables hasta configurar read_locales.')
    with pool.connection() as c:
        mapping_rows=c.execute("SELECT dst.store_code,dst.shopify_product_gid FROM store_products src JOIN store_products dst USING(product_key) WHERE src.store_code='ES' AND src.shopify_product_gid=%s AND dst.store_code<>'ES' AND dst.mapping_status='CONFIRMED'",(source_id,)).fetchall()
    # Prefer mappings tied to this Spanish product. If none exists, try its SKU in each store.
    known={code:gid for code,gid in mapping_rows}
    for locale in source_locales:
        if locale!=primary_locales.get('ES') and locale.lower() not in ('es-es','es'):
            resolved=shopify_locale_code(locale)
            label=f'ES · {locale}'+(f' (Shopify {resolved})' if resolved!=locale else '')
            mode='translation' if primary_locales.get('ES') and locale!=primary_locales['ES'] else 'readonly'
            targets.append({'store':'ES','domain':source_store['domain'],'locale':locale,'lookup_locale':resolved,'label':label,'product':source,'mode':mode})
    for store in stores:
        if store['code']=='ES': continue
        locales=list(store['locales'])
        # MX needs its Spanish base column alongside configured secondary
        # locales. Other stores keep only their configured locales in the
        # comparison, even when their primary locale is Spanish.
        primary=primary_locales.get(store['code'])
        if store['code']=='MX' and primary and primary not in locales:
            locales.insert(0,primary)
        locales.extend(locale for locale in (target_locales or {}).get(store['code'],[]) if locale not in locales)
        if store['code']==target_store and target_locale and target_locale not in locales:
            locales.append(target_locale)
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
                primary=primary_locales.get(store['code'])
                mode='base' if primary and locale==primary else 'translation' if primary and locale!=primary else 'readonly'
                targets.append({'store':store['code'],'domain':store['domain'],'locale':locale,'lookup_locale':resolved,'label':label,'product':candidate,'uses_base_fields':store['domain']!=source_store['domain'],'mode':mode})
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
    variant_mapping_errors=[]
    for key,val in source_base.items():
        values=[val]
        edits=[None]
        for target in targets:
            product=target['product']; tr=translations(product,target['lookup_locale'])
            value=product_field_value(product,key) if target['mode']=='base' else tr.get(keymap[key],'')
            if not value and target.get('uses_base_fields') and target['mode']=='translation': value=product_field_value(product,key)
            values.append(value)
            editable={'title','body_html','handle','seo_title','seo_description','vendor','product_type'}
            edits.append({'store':target['store'],'resource_id':product['id'],'owner_id':product['id'],'source_product_gid':source_id,'locale':target['lookup_locale'],'key':keymap[key],'value':value,'source_value':val,'mode':target['mode']} if product and target['mode']!='readonly' and keymap.get(key) in editable else None)
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
            raw_mf_value=mf_value_raw(dest,target['lookup_locale'] if target['mode']=='translation' else None) if dest else None
            values.append(mf_value(dest,target['product'],target['lookup_locale'] if target['mode']=='translation' else None))
            edits.append({'store':target['store'],'resource_id':dest['id'],'owner_id':target['product']['id'],'source_product_gid':source_id,'locale':target['lookup_locale'],'key':'value','value':raw_mf_value,'source_value':mf_value_raw(mf),'namespace':namespace,'metafield_key':mfkey,'metafield_type':dest.get('type'),'compare_digest':dest.get('compareDigest'),'mode':target['mode']} if dest and target['mode']!='readonly' and dest.get('type') in {'single_line_text_field','multi_line_text_field','rich_text_field','html','list.single_line_text_field','list.multi_line_text_field'} else None)
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
                target_variants=(target['product'] or {}).get('variants',{}).get('nodes',[])
                # Match across shops by SKU first, then by the selected option values
                # and position. Variant titles can be translated, so they are not a
                # reliable cross-store identity.
                source_options=tuple((item.get('name',''),item.get('value','')) for item in source_variant.get('selectedOptions',[]))
                dest_variant=next((v for v in target_variants if source_variant.get('sku') and v.get('sku') and v['sku'].strip().casefold()==source_variant['sku'].strip().casefold()),None)
                if dest_variant is None and source_options:
                    dest_variant=next((v for v in target_variants if tuple((item.get('name',''),item.get('value','')) for item in v.get('selectedOptions',[]))==source_options),None)
                if dest_variant is None and source_variant.get('position') is not None:
                    dest_variant=next((v for v in target_variants if v.get('position')==source_variant.get('position')),None)
                if dest_variant is None and target['domain']==source_store['domain']:
                    dest_variant=next((v for v in target_variants if v.get('id')==source_variant.get('id')),None)
                dest_mf=next((item for item in (dest_variant or {}).get('metafields',{}).get('nodes',[]) if item['namespace']==namespace and item['key']==mfkey and not is_yotpo(item)),None)
                if target['mode']!='readonly' and (dest_variant is None or dest_mf is None):
                    reason='No se encontró la variante equivalente en destino' if dest_variant is None else 'La variante de destino no tiene este metacampo'
                    variant_mapping_errors.append({'store':target['store'],'locale':target['locale'],'field_key':key,'field_label':f'Variante · {identity_label} · {namespace} / {mfkey}','error':reason})
                raw_mf_value=mf_value_raw(dest_mf,target['lookup_locale'] if target['mode']=='translation' else None) if dest_mf else None
                values.append(mf_value(dest_mf,target['product'],target['lookup_locale'] if target['mode']=='translation' else None))
                edits.append({'store':target['store'],'resource_id':dest_mf['id'],'owner_id':dest_variant['id'] if dest_variant else '', 'source_product_gid':source_id,'locale':target['lookup_locale'],'key':'value','value':raw_mf_value,'source_value':mf_value_raw(mf),'namespace':namespace,'metafield_key':mfkey,'metafield_type':dest_mf.get('type'),'compare_digest':dest_mf.get('compareDigest'),'mode':target['mode']} if dest_mf and target['mode']!='readonly' and dest_mf.get('type') in {'single_line_text_field','multi_line_text_field','rich_text_field','html','list.single_line_text_field','list.multi_line_text_field'} else None)
            fields.append({'key':key,'label':f'Variante · {identity_label} · {namespace} / {mfkey} ({mf.get("type","")})','values':values,'edits':edits})
    # Keep synchronization state per editable field and destination. Older Shopify
    # values have no baseline and are deliberately reported as unknown until saved
    # through this hub once.
    with pool.connection() as c:
        sync_rows=c.execute('SELECT store_code,locale,resource_id,field_key,source_hash,target_hash FROM product_field_sync_state WHERE source_product_gid=%s',(source_id,)).fetchall()
    sync_state={(row[0],row[1],row[2],row[3]):(row[4],row[5]) for row in sync_rows}
    def value_hash(value): return hashlib.sha256(str(value or '').encode()).hexdigest()
    for field in fields:
        for idx,edit in enumerate(field['edits']):
            if not edit: continue
            source_value=edit.get('source_value') if edit.get('source_value') is not None else (field['values'][0] if field['values'] else '')
            destination_value=edit.get('value') or ''
            baseline=sync_state.get((edit['store'],edit['locale'],edit['resource_id'],edit['key']))
            if not destination_value and source_value:
                status='MISSING'
            elif not baseline:
                status='UNTRACKED'
            elif value_hash(destination_value)!=baseline[1]:
                status='CONFLICT'
            elif value_hash(source_value)!=baseline[0]:
                status='OUTDATED'
            else:
                status='CURRENT'
            edit['sync_status']=status
    columns=[{'label':'ES · español (base)'}]+[{'label':t['label']} for t in targets]
    if not sku: notices.append('El producto de ES no tiene SKU; no se pudo proponer una correspondencia automática en otras tiendas.')
    for t in targets:
        if not t['product']: notices.append(f'{t["store"]} · {t["locale"]}: no hay producto equivalente localizado por correspondencia o SKU.')
    return {'source':{'id':source['id'],'title':source['title'],'sku':sku},'columns':columns,'fields':fields,'notices':list(dict.fromkeys(notices)),'variant_mapping_errors':variant_mapping_errors}

def remember_manual_correction(x:CellTranslation):
    src=(x.source_value or '').strip()
    dst=x.value.strip()
    if src and dst and src!=dst and len(src)<=500 and len(dst)<=500:
        with pool.connection() as c:
            c.execute("INSERT INTO translation_memory(target_store,source_locale,target_locale,source_text,target_text) VALUES(%s,'es-ES',%s,%s,%s) ON CONFLICT(target_store,source_locale,target_locale,source_text) WHERE target_store IS NOT NULL DO UPDATE SET target_text=EXCLUDED.target_text,created_at=now()",(x.store,x.locale,src,dst))

@app.post('/api/translate-cell')
def translate_cell(x:CellTranslateRequest,user=Depends(operator)):
    if x.field_key=='handle': raise HTTPException(400,'El handle requiere adaptación manual antes de guardarse')
    profile=target_profile(x.target_store,x.target_locale)
    if not profile: raise HTTPException(400,f'{x.target_store} · {x.target_locale} no tiene un perfil de traducción activo')
    if x.metafield_type in {'rich_text_field','list.single_line_text_field','list.multi_line_text_field'}:
        try: structured=json.loads(x.source_text)
        except (TypeError,json.JSONDecodeError): raise HTTPException(400,'El metacampo estructurado no contiene JSON válido')
        memory_hits=0; glossary_terms=0; translated_nodes=0
        def translate_leaf(source):
            nonlocal memory_hits,glossary_terms,translated_nodes
            if not source.strip(): return source
            leaf=CellTranslateRequest(source_text=source,source_store=x.source_store,source_locale=x.source_locale,target_store=x.target_store,target_locale=x.target_locale,field_key='metafield_text',html=False)
            result=translate_cell(leaf,user)
            memory_hits+=bool(result.get('memory_hit')); glossary_terms+=int(result.get('glossary_terms',0)); translated_nodes+=1
            return result['translation']
        def walk_text_nodes(node):
            if isinstance(node,dict):
                if node.get('type')=='text' and isinstance(node.get('value'),str):
                    node['value']=translate_leaf(node['value'])
                else:
                    for child in node.values(): walk_text_nodes(child)
            elif isinstance(node,list):
                for child in node: walk_text_nodes(child)
        if x.metafield_type=='rich_text_field':
            if not isinstance(structured,dict) or structured.get('type')!='root': raise HTTPException(400,'Rich text debe tener un nodo raíz Shopify válido')
            walk_text_nodes(structured)
        else:
            if not isinstance(structured,list) or not all(isinstance(item,str) for item in structured): raise HTTPException(400,'La lista del metacampo debe contener textos')
            structured=[translate_leaf(item) for item in structured]
        if not translated_nodes: raise HTTPException(400,'El metacampo no contiene texto traducible')
        return {'translation':json.dumps(structured,ensure_ascii=False,separators=(',',':')),'memory_hit':memory_hits==translated_nodes,'glossary_terms':glossary_terms,'require_review':profile['require_review']}
    if not os.getenv('DEEPL_AUTH_KEY','').strip(): raise HTTPException(503,'DeepL no está configurado en el servidor')
    with pool.connection() as c:
        exact=c.execute("""SELECT target_text FROM translation_memory
            WHERE target_store=%s
              AND split_part(lower(source_locale),'-',1)=split_part(lower(%s),'-',1)
              AND split_part(lower(target_locale),'-',1)=split_part(lower(%s),'-',1)
              AND source_text=%s
            ORDER BY ((lower(source_locale)=lower(%s))::int + (lower(target_locale)=lower(%s))::int) DESC
            LIMIT 1""",(x.target_store,x.source_locale,x.target_locale,x.source_text.strip(),x.source_locale,x.target_locale)).fetchone()
        terms=c.execute("""SELECT source_term,target_term FROM (
              SELECT DISTINCT ON (lower(source_term)) source_term,target_term,
                (((source_store=%s AND target_store=%s)::int * 2)
                  + (lower(source_locale)=lower(%s))::int
                  + (lower(target_locale)=lower(%s))::int) AS specificity
              FROM translation_glossary
              WHERE enabled
                AND ((source_store=%s AND target_store=%s) OR (source_store IS NULL AND target_store IS NULL))
                AND split_part(lower(source_locale),'-',1)=split_part(lower(%s),'-',1)
                AND split_part(lower(target_locale),'-',1)=split_part(lower(%s),'-',1)
              ORDER BY lower(source_term),specificity DESC,updated_at DESC
            ) preferred_terms ORDER BY length(source_term) DESC""",
            (x.source_store,x.target_store,x.source_locale,x.target_locale,x.source_store,x.target_store,x.source_locale,x.target_locale)).fetchall()
    # Keep approved terms intact across DeepL without exposing a glossary or API key
    # to the browser. Replace only text nodes when the value contains HTML.
    import html as html_module
    tokens={}; text=x.source_text; token_index=0
    def preserve_term_case(source, target):
        letters=''.join(char for char in source if char.isalpha())
        if letters and letters.isupper(): return target.upper()
        if letters and letters.islower(): return target.lower()
        if letters and letters[0].isupper() and letters[1:].islower():
            return target[:1].upper()+target[1:].lower()
        return target
    segments=re.split(r'(<[^>]+>)',text) if x.html else [text]
    for i,segment in enumerate(segments):
        if segment.startswith('<') and segment.endswith('>'): continue
        for n,(source_term,target_term) in enumerate(terms):
            pattern=re.compile(r'(?<!\w)'+re.escape(source_term)+r'(?!\w)',re.IGNORECASE)
            def protect(match):
                nonlocal token_index
                marker=f'ZXQGLOSSARY{token_index:06d}QXZ'
                token_index+=1
                replacement=preserve_term_case(match.group(),target_term)
                tokens[marker]=html_module.escape(replacement) if x.html else replacement
                return marker
            segment=pattern.sub(protect,segment)
        segments[i]=segment
    protected=''.join(segments)
    # A saved translation may predate a glossary rule. Reuse memory only when
    # this source text contains no term that the current scoped glossary protects.
    if exact and not tokens: return {'translation':exact[0],'memory_hit':True,'glossary_terms':0,'require_review':profile['require_review']}
    try:
        from .worker import deepl_translate
        translated=deepl_translate([protected],profile['deepl_target'],handle_html=x.html,formality=profile['formality'],source_lang='ES')[0]
    except RuntimeError as exc:
        raise HTTPException(502,str(exc)[:500])
    for marker,target_term in tokens.items(): translated=translated.replace(marker,target_term)
    return {'translation':translated,'memory_hit':False,'glossary_terms':len(tokens),'require_review':profile['require_review']}

@app.get('/api/glossary')
def list_glossary(target_locale:str|None=None,user=Depends(operator)):
    with pool.connection() as c:
        if target_locale:
            rows=c.execute("SELECT id,source_store,source_locale,target_store,target_locale,source_term,target_term,enabled,origin FROM translation_glossary WHERE target_locale=%s ORDER BY source_store,target_store,source_term",(target_locale,)).fetchall()
        else:
            rows=c.execute("SELECT id,source_store,source_locale,target_store,target_locale,source_term,target_term,enabled,origin FROM translation_glossary ORDER BY source_store,target_store,target_locale,source_term").fetchall()
    return {'terms':[dict(zip(('id','source_store','source_locale','target_store','target_locale','source_term','target_term','enabled','origin'),r)) for r in rows]}

@app.post('/api/glossary')
def add_glossary_term(x:GlossaryTerm,user=Depends(operator)):
    active={item['code']:item for item in shopify_stores()}
    if x.source_store not in active or x.target_store not in active: raise HTTPException(400,'Selecciona tiendas activas de la lista')
    if x.source_locale not in active[x.source_store]['locales']: raise HTTPException(400,'El idioma origen no está configurado en la tienda origen')
    if x.target_locale not in active[x.target_store]['locales']: raise HTTPException(400,'El idioma destino no está configurado en la tienda destino')
    with pool.connection() as c:
        row=c.execute("""INSERT INTO translation_glossary(source_store,source_locale,target_store,target_locale,source_term,target_term,enabled)
          VALUES(%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (source_store,source_locale,target_store,target_locale,(lower(source_term)))
          WHERE source_store IS NOT NULL AND target_store IS NOT NULL
          DO UPDATE SET target_term=EXCLUDED.target_term,enabled=EXCLUDED.enabled,updated_at=now()
          RETURNING id""",(x.source_store,x.source_locale,x.target_store,x.target_locale,x.source_term,x.target_term,x.enabled)).fetchone()
    return {'id':row[0],'saved':True}

@app.delete('/api/glossary/{term_id}')
def delete_glossary_term(term_id:int,user=Depends(operator)):
    with pool.connection() as c: c.execute('DELETE FROM translation_glossary WHERE id=%s',(term_id,))
    return {'deleted':True}

@app.post('/api/shopify/translations')
def save_shopify_translation(x:CellTranslation,user=Depends(operator)):
    store=next((item for item in shopify_stores() if item['code']==x.store),None)
    if not store or not store['token']: raise HTTPException(404,'Tienda sin configurar')
    enabled_locales=shopify_shop_locales(store)
    shop_locale=next((item for item in enabled_locales if item.get('locale')==x.locale and item.get('published')),None)
    if not shop_locale: raise HTTPException(400,'El locale no está publicado en Shopify: '+', '.join(item.get('locale','') for item in enabled_locales if item.get('published')))
    if x.store=='ES':
        if shop_locale.get('primary'):
            raise HTTPException(400,'Desde ES solo se guardan traducciones para locales secundarios')
        mode='translation'
    elif shop_locale.get('primary'):
        mode='base'
    else:
        mode='translation'
    resource_type=x.resource_id.removeprefix('gid://shopify/')
    if mode=='base':
        if resource_type.startswith('Metafield/'):
            if x.key!='value' or not x.namespace or not x.owner_id.startswith(('gid://shopify/Product/','gid://shopify/ProductVariant/')) or x.metafield_type not in {'single_line_text_field','multi_line_text_field','rich_text_field','html','list.single_line_text_field','list.multi_line_text_field'}:
                raise HTTPException(400,'Datos de metacampo no válidos para editar el contenido base')
            mutation='''mutation SetBaseMetafield($metafields:[MetafieldsSetInput!]!) { metafieldsSet(metafields:$metafields) { userErrors { field message } } }'''
            if not x.metafield_key: raise HTTPException(400,'Falta la clave Shopify del metacampo')
            variables={'metafields':[{'ownerId':x.owner_id,'namespace':x.namespace,'key':x.metafield_key,'type':x.metafield_type,'value':x.value,'compareDigest':x.compare_digest}]}
            result=shopify_graphql(store['domain'],store['token'],mutation,variables).get('metafieldsSet') or {}
            errors=result.get('userErrors',[])
            if errors: raise HTTPException(400,'Shopify: '+'; '.join(error.get('message','error') for error in errors)[:500])
            remember_manual_correction(x)
            record_field_sync_state(x)
            return {'store':x.store,'resource_id':x.resource_id,'locale':x.locale,'key':x.key,'mode':'base','saved':True}
        if not resource_type.startswith('Product/') or x.owner_id!=x.resource_id:
            raise HTTPException(400,'El recurso de contenido base debe ser el producto de esa tienda')
        fields={'title':('title',None),'body_html':('descriptionHtml',None),'handle':('handle',None),'vendor':('vendor',None),'product_type':('productType',None),'seo_title':('seo.title','title'),'seo_description':('seo.description','description')}
        if x.key not in fields: raise HTTPException(400,'El campo no está habilitado para actualizar el contenido base')
        field,seo_field=fields[x.key]
        product={'id':x.resource_id}
        if seo_field: product['seo']={seo_field:x.value}
        else: product[field]=x.value
        mutation='''mutation UpdateBaseProduct($product:ProductUpdateInput!) { productUpdate(product:$product) { userErrors { field message } } }'''
        result=shopify_graphql(store['domain'],store['token'],mutation,{'product':product}).get('productUpdate') or {}
        errors=result.get('userErrors',[])
        if errors: raise HTTPException(400,'Shopify: '+'; '.join(error.get('message','error') for error in errors)[:500])
        remember_manual_correction(x)
        record_field_sync_state(x)
        return {'store':x.store,'resource_id':x.resource_id,'locale':x.locale,'key':x.key,'mode':'base','saved':True}
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
    remember_manual_correction(x)
    record_field_sync_state(x)
    return {'store':x.store,'resource_id':x.resource_id,'locale':x.locale,'key':x.key,'mode':'translation','saved':True}

def record_field_sync_state(x:CellTranslation):
    source_id=x.source_product_gid or (x.owner_id if x.owner_id.startswith('gid://shopify/Product/') else '')
    if not source_id: return
    digest=lambda value: hashlib.sha256(str(value or '').encode()).hexdigest()
    with pool.connection() as c:
        c.execute('''INSERT INTO product_field_sync_state(source_product_gid,store_code,locale,resource_id,field_key,source_hash,target_hash)
          VALUES(%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(source_product_gid,store_code,locale,resource_id,field_key) DO UPDATE
          SET source_hash=EXCLUDED.source_hash,target_hash=EXCLUDED.target_hash,synced_at=now()''',
          (source_id,x.store,x.locale,x.resource_id,x.key,digest(x.source_value),digest(x.value)))

def translation_batch_payload(batch_id:int):
    with pool.connection() as c:
        batch=c.execute('SELECT id,source_sku,source_product_gid,source_title,state,error,created_at,updated_at FROM translation_batches WHERE id=%s',(batch_id,)).fetchone()
        if not batch: raise HTTPException(404,'Lote de traducción no encontrado')
        rows=c.execute('SELECT id,store_code,locale,field_key,field_label,source_text,proposed_text,edit_payload,state,error,created_at,updated_at,quality FROM translation_batch_items WHERE batch_id=%s ORDER BY id',(batch_id,)).fetchall()
    return {'id':batch[0],'sku':batch[1],'source_product_gid':batch[2],'source_title':batch[3],'state':batch[4],'error':batch[5],'created_at':batch[6],'updated_at':batch[7],
      'items':[dict(zip(('id','store','locale','field_key','field_label','source_text','proposed_text','edit_payload','state','error','created_at','updated_at','quality'),row)) for row in rows]}

def batch_variant_allowed(key:str,skus:list[str]|None)->bool:
    if skus is None or not key.startswith('metafield.variant.'): return True
    rest=key[len('metafield.variant.'):].casefold()
    return any(rest.startswith(s.strip().casefold()+'.') for s in skus)

def resolve_es_product_by_sku(source_store,sku:str):
    hits=shopify_search(source_store,sku,20)
    unique={p['id']:p for p in hits if any((v.get('sku') or '').strip().casefold()==sku.casefold() for v in p.get('variants',{}).get('nodes',[]))}
    if not unique: raise HTTPException(404,'No se encontró un producto ES con ese SKU exacto')
    if len(unique)>1: raise HTTPException(409,'El SKU coincide con varios productos de ES')
    return next(iter(unique.values()))

@app.post('/api/translation-batches/resolve')
def resolve_translation_batch_skus(x:TranslationBatchResolve,user=Depends(operator)):
    """Group SKUs by their ES product so a campaign creates one batch per product."""
    source_store=next((s for s in shopify_stores() if s['code']=='ES' and s['token']),None)
    if not source_store: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    groups={}; errors=[]
    for sku in dict.fromkeys(v.strip() for v in x.skus if v.strip()):
        try: product=resolve_es_product_by_sku(source_store,sku)
        except HTTPException as exc:
            errors.append({'sku':sku,'error':str(exc.detail)}); continue
        group=groups.setdefault(product['id'],{'product_gid':product['id'],'title':product['title'],'skus':[]})
        group['skus'].append(sku)
    return {'groups':list(groups.values()),'errors':errors}

@app.get('/api/translation-batches')
def list_translation_batches(user=Depends(operator)):
    with pool.connection() as c:
        rows=c.execute('SELECT id,source_sku,source_title,state,error,created_at,updated_at FROM translation_batches ORDER BY updated_at DESC,id DESC LIMIT 200').fetchall()
    return [dict(zip(('id','sku','source_title','state','error','created_at','updated_at'),row)) for row in rows]

@app.post('/api/translation-batches')
def create_translation_batch(x:TranslationBatchRequest,user=Depends(operator)):
    sku=x.sku.strip()
    stores=shopify_stores(); source_store=next((s for s in stores if s['code']=='ES' and s['token']),None)
    if not source_store: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    source_product=resolve_es_product_by_sku(source_store,sku)
    allowed_skus=[sku,*[v.strip() for v in x.also_skus if v.strip()]] if x.variants_of_skus_only else None
    label_sku=', '.join(dict.fromkeys([sku,*[v.strip() for v in x.also_skus if v.strip()]]))
    source_locale=next((item['locale'] for item in shopify_shop_locales(source_store) if item.get('primary')),'es-ES')
    targets=[]; target_locales={}
    for store in stores:
        if not store['token']: continue
        try: locales=shopify_shop_locales(store)
        except HTTPException: continue
        profiles=active_profiles(store['code'])
        published=[item['locale'] for item in locales if item.get('published') and not (store['code']=='ES' and item.get('primary')) and (profiles.get(item['locale'].lower()) or {}).get('deepl_target')]
        targets.extend((store['code'],locale) for locale in published)
        if published: target_locales[store['code']]=published
    if x.store: targets=[t for t in targets if t[0]==x.store]
    if x.locale: targets=[t for t in targets if t[1].lower()==x.locale.lower()]
    if not targets: raise HTTPException(409,'No hay locales publicados de destino en las tiendas activas para la selección indicada')
    target_locales={store:[l for l in locales if (store,l) in set(targets)] for store,locales in target_locales.items()}
    target_locales={store:locales for store,locales in target_locales.items() if locales}
    with pool.connection() as c:
        batch_id=c.execute("INSERT INTO translation_batches(source_sku,source_product_gid,source_title,state) VALUES(%s,%s,%s,'PREPARING') RETURNING id",(label_sku,source_product['id'],source_product['title'])).fetchone()[0]
    try:
        comparison=_compare_shopify_product(source_product['id'],target_locales=target_locales)
    except HTTPException as exc:
        with pool.connection() as c:
            for store_code,locale in targets:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,state,error)
                  VALUES(%s,%s,%s,'__product__','Producto','ERROR',%s) ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',
                  (batch_id,store_code,locale,str(exc.detail)[:500]))
            c.execute("UPDATE translation_batches SET state='READY_WITH_ERRORS',error=%s,updated_at=now() WHERE id=%s",(str(exc.detail)[:500],batch_id))
        return translation_batch_payload(batch_id)
    empty_message='Ninguno de los campos seleccionados existe, tiene contenido en origen o tiene equivalente editable en este destino' if x.fields is not None else 'No se encontró un producto equivalente o campos principales editables en el destino'
    for store_code,locale in targets:
        column_index=next((i for i,column in enumerate(comparison['columns']) if i>0 and column['label'].startswith(f'{store_code} · {locale}')),None)
        target_items=0
        for field in comparison['fields']:
            if not batch_field_selected(field['key'],x.fields) or not batch_variant_allowed(field['key'],allowed_skus): continue
            edit_index=column_index
            if edit_index is None or edit_index>=len(field.get('edits',[])) or not field['edits'][edit_index]:
                edit_index=next((i for i,item in enumerate(field.get('edits',[])) if item and item.get('store')==store_code and item.get('locale')==locale),None)
            edit=field.get('edits',[None]*len(field.get('values',[])))[edit_index] if edit_index is not None and edit_index<len(field.get('edits',[])) else None
            source_text=(edit.get('source_value') if field['key'].startswith('metafield.') and edit else field['values'][0] if field.get('values') else '')
            if not edit or not source_text: continue
            try:
                request=CellTranslateRequest(source_text=source_text,source_store='ES',source_locale=source_locale,target_store=store_code,target_locale=locale,field_key=edit['key'],metafield_type=edit.get('metafield_type'),html=field['key']=='descriptionHtml' or edit.get('metafield_type')=='html')
                translated=translate_cell(request,user)
                proposed=translated['translation']; error=None
                quality=assess_translation(source_text,proposed,is_html=request.html)
                state='APPROVED' if quality['status']=='PASS' and not translated.get('require_review') and os.getenv('QUALITY_AUTO_APPROVE','true').lower() in ('1','true','yes','on') else 'PENDING_REVIEW'
            except Exception as exc:
                proposed=None; quality=None; state='ERROR'; error=str(exc.detail if isinstance(exc,HTTPException) else exc)[:500]
            with pool.connection() as c:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,source_text,proposed_text,edit_payload,state,error,quality)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',
                  (batch_id,store_code,locale,field['key'],field['label'],source_text,proposed,Jsonb(edit),state,error,Jsonb(quality) if quality else None))
            target_items+=1
        for missing in comparison.get('variant_mapping_errors',[]):
            if missing['store']!=store_code or missing['locale']!=locale or not batch_field_selected(missing['field_key'],x.fields) or not batch_variant_allowed(missing['field_key'],allowed_skus): continue
            with pool.connection() as c:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,state,error)
                  VALUES(%s,%s,%s,%s,%s,'ERROR',%s) ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',
                  (batch_id,store_code,locale,missing['field_key'],missing['field_label'],missing['error']))
            target_items+=1
        if target_items==0:
            with pool.connection() as c:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,state,error)
                  VALUES(%s,%s,%s,'__product__','Producto','ERROR',%s)
                  ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',(batch_id,store_code,locale,empty_message))
    with pool.connection() as c:
        c.execute("UPDATE translation_batches SET state=CASE WHEN EXISTS(SELECT 1 FROM translation_batch_items WHERE batch_id=%s AND state='PENDING_REVIEW') THEN 'PENDING_REVIEW' WHEN EXISTS(SELECT 1 FROM translation_batch_items WHERE batch_id=%s AND state='APPROVED') THEN 'APPROVED' ELSE 'READY_WITH_ERRORS' END,updated_at=now() WHERE id=%s",(batch_id,batch_id,batch_id))
    return translation_batch_payload(batch_id)

@app.get('/api/translation-batches/{batch_id}')
def get_translation_batch(batch_id:int,user=Depends(operator)):
    return translation_batch_payload(batch_id)

@app.patch('/api/translation-batches/{batch_id}/items/{item_id}')
def edit_translation_batch_item(batch_id:int,item_id:int,x:TranslationBatchItemEdit,user=Depends(operator)):
    with pool.connection() as c:
        current=c.execute("SELECT source_text,field_key,edit_payload FROM translation_batch_items WHERE id=%s AND batch_id=%s AND state='PENDING_REVIEW'",(item_id,batch_id)).fetchone()
        if not current: raise HTTPException(409,'El campo no existe o ya no está pendiente de revisión')
        is_html=current[1]=='descriptionHtml' or (current[2] or {}).get('metafield_type')=='html'
        quality=assess_translation(current[0],x.proposed_text,is_html=is_html)
        c.execute("UPDATE translation_batch_items SET proposed_text=%s,quality=%s,updated_at=now() WHERE id=%s AND batch_id=%s",(x.proposed_text,Jsonb(quality),item_id,batch_id))
    return {'batch_id':batch_id,'item_id':item_id,'updated':True}

@app.post('/api/translation-batches/{batch_id}/approve')
def approve_translation_batch(batch_id:int,x:TranslationBatchItemIds,user=Depends(operator)):
    with pool.connection() as c:
        if not c.execute('SELECT 1 FROM translation_batches WHERE id=%s',(batch_id,)).fetchone(): raise HTTPException(404,'Lote de traducción no encontrado')
        rows=c.execute("UPDATE translation_batch_items SET state='APPROVED',updated_at=now() WHERE batch_id=%s AND id=ANY(%s) AND state='PENDING_REVIEW' RETURNING id",(batch_id,x.item_ids)).fetchall()
        if len(rows)!=len(set(x.item_ids)): raise HTTPException(409,'Uno o más campos no están pendientes para este lote')
        c.execute("""UPDATE translation_batches b SET state=CASE
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='PENDING_REVIEW') THEN 'PENDING_REVIEW'
          ELSE 'APPROVED' END,updated_at=now() WHERE b.id=%s""",(batch_id,))
    return translation_batch_payload(batch_id)

@app.post('/api/translation-batches/{batch_id}/reopen')
def reopen_translation_batch_items(batch_id:int,x:TranslationBatchItemIds,user=Depends(operator)):
    with pool.connection() as c:
        if not c.execute('SELECT 1 FROM translation_batches WHERE id=%s',(batch_id,)).fetchone(): raise HTTPException(404,'Lote de traducción no encontrado')
        rows=c.execute("UPDATE translation_batch_items SET state='PENDING_REVIEW',updated_at=now() WHERE batch_id=%s AND id=ANY(%s) AND state='APPROVED' RETURNING id",(batch_id,x.item_ids)).fetchall()
        if len(rows)!=len(set(x.item_ids)): raise HTTPException(409,'Solo se pueden devolver a revisión campos aprobados que aún no se hayan aplicado')
        c.execute("""UPDATE translation_batches b SET state=CASE
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='PENDING_REVIEW') THEN 'PENDING_REVIEW'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='APPROVED') THEN 'APPROVED'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='SAVED') THEN 'COMPLETED'
          ELSE 'READY_WITH_ERRORS' END,updated_at=now() WHERE b.id=%s""",(batch_id,))
    return translation_batch_payload(batch_id)

@app.post('/api/translation-batches/{batch_id}/apply')
def apply_translation_batch(batch_id:int,x:TranslationBatchItemIds|None=None,user=Depends(operator)):
    with pool.connection() as c:
        if x:
            rows=c.execute("SELECT id,proposed_text,edit_payload FROM translation_batch_items WHERE batch_id=%s AND id=ANY(%s) AND state='APPROVED' ORDER BY id",(batch_id,x.item_ids)).fetchall()
            if len(rows)!=len(set(x.item_ids)): raise HTTPException(409,'Todos los campos seleccionados deben estar aprobados antes de aplicarse')
        else:
            rows=c.execute("SELECT id,proposed_text,edit_payload FROM translation_batch_items WHERE batch_id=%s AND state='APPROVED' ORDER BY id",(batch_id,)).fetchall()
        if not rows:
            if not c.execute('SELECT 1 FROM translation_batches WHERE id=%s',(batch_id,)).fetchone(): raise HTTPException(404,'Lote de traducción no encontrado')
            raise HTTPException(409,'Aprueba al menos un campo antes de aplicarlo')
    for item_id,proposed,payload in rows:
        try:
            save_shopify_translation(CellTranslation(**{**payload,'value':proposed}),user)
            state='SAVED'; error=None
        except (HTTPException,RuntimeError,ValueError) as exc:
            state='APPLY_ERROR'; error=str(exc.detail if isinstance(exc,HTTPException) else exc)[:500]
        with pool.connection() as c:
            c.execute('UPDATE translation_batch_items SET state=%s,error=%s,updated_at=now() WHERE id=%s AND batch_id=%s',(state,error,item_id,batch_id))
    with pool.connection() as c:
        c.execute("""UPDATE translation_batches b SET state=CASE
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='PENDING_REVIEW') THEN 'PENDING_REVIEW'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state IN ('APPROVED','APPLY_ERROR')) THEN 'APPROVED'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='SAVED') THEN 'COMPLETED'
          ELSE 'READY_WITH_ERRORS' END,updated_at=now() WHERE b.id=%s""",(batch_id,))
    return translation_batch_payload(batch_id)

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
