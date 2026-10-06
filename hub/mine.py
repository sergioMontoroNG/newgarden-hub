"""Minería de términos sobre historical_pairs: propone candidatos de glosario por tienda.

Uso: python -m hub.mine [TIENDA ...]
Alinea n-gramas ES con n-gramas del idioma destino por co-ocurrencia (coeficiente de Dice)
sobre productos. Solo escribe en glossary_candidates (pendientes de revisión humana).
"""
import json
import re
import sys
from collections import Counter, defaultdict

from .db import initialize, pool

FIELDS = ('title', 'seo_title', 'seo_description')
MIN_SUPPORT, MIN_DICE, MIN_SOURCE_PRODUCTS = 4, 0.72, 4

STOP = {
    'es': 'el la los las un una unos unas de del al y o u e en con sin por para que se su sus tu tus es a lo como más mas muy ya ahora este esta estos estas ideal perfecto perfecta compra comprar cómprala cómprate tuya tuyo descubre disfruta newgarden hogar estilo toque dale protege decora decorar espacio espacios añade crea libre aire uso'.split(),
    'fr': 'le la les un une des de du et ou en avec sans pour par que qui se son sa ses votre vos notre nos à au aux ce cette ces est dans sur plus très acheter découvrez découvre newgarden'.split(),
    'de': 'der die das den dem des ein eine einen einem einer und oder mit ohne für von zu zum zur im in auf ist sie ihr ihre ihren ihrem bei als auch nur sich newgarden kaufen entdecken'.split(),
    'it': 'il lo la i gli le un una uno di del dello della dei degli delle e o con senza per da in su che è il tuo tua tuoi tue al alla nel nella più molto acquista scopri newgarden'.split(),
    'en': 'the a an of and or with without for to in on your you our is it this that these at by from newgarden buy discover'.split(),
}


def tokens(text):
    text = re.sub(r'<[^>]+>', ' ', text or '').lower()
    return re.findall(r"[a-záéíóúñüàâçèêëîïôùûœäößìò][a-záéíóúñüàâçèêëîïôùûœäößìò'\-]*", text)


def ngrams(words, stop, maxn=2):
    out = set()
    for n in range(1, maxn + 1):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in stop or gram[-1] in stop or any(len(w) < 3 for w in gram if w not in stop):
                continue
            if any(len(w) < 3 for w in (gram[0], gram[-1])):
                continue
            out.add(' '.join(gram))
    return out


def mine(store, locale, rows):
    lang = locale.split('-')[0]
    src_stop, tgt_stop = set(STOP['es']), set(STOP.get(lang, ()))
    docs = defaultdict(lambda: ([], []))
    for sku, field, source, target in rows:
        docs[sku][0].extend(tokens(source) + ['|'])
        docs[sku][1].extend(tokens(target) + ['|'])
    src_df, tgt_df, co = Counter(), Counter(), defaultdict(Counter)
    for src_words, tgt_words in docs.values():
        s = {g for g in ngrams(src_words, src_stop | {'|'}) if '|' not in g.split()}
        t = {g for g in ngrams(tgt_words, tgt_stop | {'|'}) if '|' not in g.split()}
        src_df.update(s)
        tgt_df.update(t)
        for sg in s:
            co[sg].update(t)
    results = []
    for sg, df in src_df.items():
        if df < MIN_SOURCE_PRODUCTS:
            continue
        scored = sorted(((2 * c / (df + tgt_df[tg]), c, tg) for tg, c in co[sg].items() if c >= MIN_SUPPORT), reverse=True)
        if not scored or scored[0][0] < MIN_DICE:
            continue
        dice, support, best = scored[0]
        alternatives = [{'term': tg, 'dice': round(d, 2), 'support': c} for d, c, tg in scored[1:4] if d >= MIN_DICE * 0.75]
        results.append({'source_term': sg, 'target_term': best, 'support': support, 'source_products': df,
                        'dice': round(dice, 3), 'alternatives': alternatives,
                        'kind': 'invariant' if sg == best else 'term'})
    # Un n-grama ES contenido en otro con el mismo destino y soporte similar es redundante.
    keep = []
    for r in results:
        redundant = any(o is not r and o['target_term'] == r['target_term'] and r['source_term'] in o['source_term'] and o['source_term'] != r['source_term']
                        and o['support'] >= 0.8 * r['support'] for o in results)
        if not redundant:
            keep.append(r)
    return keep


def main(only):
    initialize()
    with pool.connection() as c:
        groups = c.execute("SELECT DISTINCT target_store,target_locale FROM historical_pairs ORDER BY 1,2").fetchall()
        for store, locale in groups:
            if only and store not in only:
                continue
            rows = c.execute("SELECT sku,field_key,source_text,target_text FROM historical_pairs WHERE target_store=%s AND target_locale=%s AND field_key=ANY(%s)",
                             (store, locale, list(FIELDS))).fetchall()
            found = mine(store, locale, rows)
            c.execute("DELETE FROM glossary_candidates WHERE target_store=%s AND target_locale=%s AND status='pending'", (store, locale))
            for r in found:
                c.execute("""INSERT INTO glossary_candidates(target_store,target_locale,source_term,target_term,kind,support,source_products,dice,alternatives)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(target_store,target_locale,source_term) DO NOTHING""",
                          (store, locale, r['source_term'], r['target_term'], r['kind'], r['support'], r['source_products'], r['dice'], json.dumps(r['alternatives'])))
            print(store, locale, f'{len(found)} candidatos', flush=True)


if __name__ == '__main__':
    main(set(sys.argv[1:]))
