import json, os, re, time, urllib.error, urllib.request
from .db import pool, initialize
from psycopg.types.json import Jsonb

LOCK_ID=748392102

def quota_error(stderr: str) -> bool:
    """Conservative classifier; production classification remains unverified until observed."""
    s=stderr.lower()
    return any(re.search(p,s) for p in (r'usage limit',r'quota (?:has been )?(?:exceeded|reached)',r'rate.?limit.*(?:account|usage)',r'out of (?:codex )?credits'))

def _slug(*parts):
    import unicodedata
    s=unicodedata.normalize('NFKD',' '.join(str(x) for x in parts if x)).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+','-',s).strip('-')

def _deterministic_handle(source):
    # URL suggestion is assembled from catalog facts and an operator-approved keyword;
    # model output never controls the handle.
    return _slug(source.get('category'),source.get('model'),source.get('size'),source.get('approved_market_keyword'))[:80]

def generate(product_key,locale,source):
    mode=os.getenv('TRANSLATION_PROVIDER','deepl').lower()
    if mode!='deepl': raise RuntimeError('El flujo por lotes requiere TRANSLATION_PROVIDER=deepl')
    return generate_deepl(product_key,locale,source)

def deepl_translate(values, locale, handle_html=False):
    key=os.getenv('DEEPL_AUTH_KEY','').strip()
    if not key: raise RuntimeError('DEEPL_AUTH: falta configurar DEEPL_AUTH_KEY')
    if not values: return []
    if len(json.dumps(values,ensure_ascii=False).encode())>120*1024:
        raise RuntimeError('Entrada DeepL supera el límite interno de 120 KiB')
    host='https://api-free.deepl.com' if key.endswith(':fx') else 'https://api.deepl.com'
    payload={'text':values,'target_lang':locale.upper(),'preserve_formatting':True}
    if handle_html:
        payload.update({'tag_handling':'html','tag_handling_version':'v2'})
    request=urllib.request.Request(host+'/v2/translate',data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Authorization':'DeepL-Auth-Key '+key,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(request,timeout=int(os.getenv('DEEPL_TIMEOUT_SEC','60'))) as response:
            translations=json.loads(response.read())['translations']
    except urllib.error.HTTPError as exc:
        code=exc.code
        detail=exc.read(1000).decode('utf-8','replace')
        if code==456: prefix='DEEPL_QUOTA:'
        elif code in (401,403): prefix='DEEPL_AUTH:'
        else: prefix='DEEPL_ERROR:'
        raise RuntimeError(f'{prefix} HTTP {code}: {detail}') from exc
    except (urllib.error.URLError,TimeoutError) as exc:
        raise RuntimeError(f'DEEPL_ERROR: fallo de conexión: {type(exc).__name__}') from exc
    if len(translations)!=len(values): raise RuntimeError('DEEPL_ERROR: número de traducciones inesperado')
    return [item['text'] for item in translations]

def generate_deepl(product_key,locale,source):
    # Translate only known user-facing text fields; product identifiers and arbitrary
    # structured metadata are preserved as data and never sent as instructions.
    def scalar(*names):
        for name in names:
            value=source.get(name)
            if isinstance(value,str) and value.strip(): return value
        return ''
    title=scalar('title','name') or product_key
    description=scalar('description_html','description')
    seo_title=scalar('seo_title') or title
    seo_description=scalar('seo_description') or re.sub('<[^>]*>',' ',description).strip()[:320]
    alts=source.get('image_alts') if isinstance(source.get('image_alts'),list) else []
    alts=[value for value in alts if isinstance(value,str) and value.strip()]
    plain_values=[title,seo_title,seo_description,*alts]
    translated=deepl_translate(plain_values,locale)
    translated_description=deepl_translate([description],locale,handle_html=True)[0] if description else ''
    result={'product_key':product_key,'target':locale,'title':translated[0],
      'description_html':translated_description,'seo_title':translated[1],
      'seo_description':translated[2],'suggested_handle':_deterministic_handle(source),
      'image_alts':translated[3:],'metafields':{},'warnings':[
      'Traducción automática de DeepL: requiere revisión humana antes de publicar.']}
    return result

def validate(out,source):
    errors=[]
    needed=('product_key','target','title','description_html','seo_title','seo_description','suggested_handle','image_alts','metafields','warnings')
    if any(k not in out for k in needed): errors.append('Faltan campos requeridos')
    if len(out.get('title',''))>255: errors.append('Título excede 255 caracteres')
    if len(out.get('seo_title',''))>70: errors.append('SEO title excede 70 caracteres')
    if len(out.get('seo_description',''))>320: errors.append('SEO description excede 320 caracteres')
    source_text=json.dumps(source,ensure_ascii=False)
    for number in re.findall(r'\d+(?:[.,]\d+)?\s?(?:mm|cm|m|kg|g|W|V|lm|°C|%)?',source_text,re.I):
        if number and number not in json.dumps(out,ensure_ascii=False): errors.append(f'Cifra/unidad de origen no preservada: {number}')
    return {'ok':not errors,'errors':errors,'provider':os.getenv('TRANSLATION_PROVIDER','deepl'),'quota_detection_validated_in_production':False}

def main():
    initialize()
    print(f"worker ready (provider={os.getenv('TRANSLATION_PROVIDER','deepl')})",flush=True)
    with pool.connection() as conn:
        conn.autocommit=True
        if not conn.execute('SELECT pg_try_advisory_lock(%s)',(LOCK_ID,)).fetchone()[0]:
            print('Another worker owns the advisory lock; exiting',flush=True); return
        while True:
            try:
                with conn.transaction():
                    control=conn.execute('SELECT state FROM process_control WHERE singleton FOR UPDATE').fetchone()[0]
                    if control!='RUNNING':
                        time.sleep(1); continue
                    job=conn.execute("SELECT j.id,j.product_key,j.store_code,j.locale,j.source_version,g.source,g.source_hash,g.source_version FROM localization_jobs j JOIN global_products g USING(product_key) WHERE j.state='PENDING' ORDER BY j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1").fetchone()
                    if not job:
                        conn.execute("UPDATE process_control SET state='STOPPED',updated_at=now() WHERE singleton AND state='RUNNING'")
                        time.sleep(1); continue
                    jid,key,store,locale,jobver,source,source_hash,currentver=job
                    if jobver!=currentver:
                        conn.execute("UPDATE localization_jobs SET state='STALE',reason='La fuente cambió antes del inicio' WHERE id=%s",(jid,)); continue
                    conn.execute("UPDATE localization_jobs SET state='RUNNING',updated_at=now() WHERE id=%s",(jid,))
                completed=conn.execute("SELECT count(*) FROM localization_jobs WHERE state IN ('READY_FOR_REVIEW','APPROVED','PUBLISHED')").fetchone()[0]
                threshold=os.getenv('SIMULATE_QUOTA_AFTER')
                if threshold and completed>=int(threshold):
                    incident={'kind':'SIMULATED_QUOTA','message':'Simulación de cuota agotada; no es una detección de error real','pending_job_id':jid,'completed_jobs':completed}
                    conn.execute("UPDATE localization_jobs SET state='PENDING',reason='Interrupción de cuota simulada' WHERE id=%s",(jid,))
                    conn.execute("UPDATE process_control SET state='STOPPED_QUOTA',last_incident=%s,updated_at=now() WHERE singleton",(Jsonb(incident),))
                    continue
                try:
                    out=generate(key,locale,source); validation=validate(out,source)
                    with conn.transaction():
                        conn.execute('INSERT INTO localization_results(job_id,input,output,source_hash,prompt_version,rules_version,validation) VALUES(%s,%s,%s,%s,%s,%s,%s)',(jid,Jsonb(source),Jsonb(out),source_hash,'deepl-v1','rules-v1',Jsonb(validation)))
                        newstate='READY_FOR_REVIEW' if validation['ok'] else 'QA_FAILED'
                        conn.execute('UPDATE localization_jobs SET state=%s,reason=%s,updated_at=now() WHERE id=%s',(newstate,None if validation['ok'] else '; '.join(validation['errors']),jid))
                except Exception as exc:
                    msg=str(exc)[:500]
                    kind='STOPPED_QUOTA' if quota_error(msg) or 'DEEPL_QUOTA:' in msg else ('STOPPED_AUTH' if 'DEEPL_AUTH:' in msg else 'STOPPED_ERROR')
                    incident={'kind':kind,'message':msg,'pending_job_id':jid}
                    with conn.transaction():
                        conn.execute("UPDATE localization_jobs SET state='PENDING',reason=%s WHERE id=%s",(msg,jid))
                        conn.execute('UPDATE process_control SET state=%s,last_incident=%s,updated_at=now() WHERE singleton',(kind,Jsonb(incident)))
            except Exception as exc:
                print(f'worker loop error: {type(exc).__name__}',flush=True); time.sleep(2)

if __name__=='__main__': main()
