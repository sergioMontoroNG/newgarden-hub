"""Isolated UI regression checks. Run: python scripts/check_ui.py.

Requires Playwright and Microsoft Edge (or set UI_BROWSER=chromium after installing
Playwright Chromium). Renders the actual page functions
with fixture data; intercepts every request, so no store or database is contacted.
This verifies presentation and browser interactions, not backend integrations.
"""
import ast
import html
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PAGE_FUNCTIONS = {'shell', 'home', 'stores_page', 'glossary_page', 'product_browser', 'batch_products_page'}
STORES = [dict(code=code, domain=f'newgarden-{code.lower()}.myshopify.com', active=True, has_token=True, locales=['es', 'en']) for code in ['ES', 'FR', 'MX']]
EDIT = dict(store='MX', resource_id='gid://shopify/Product/2', owner_id='gid://shopify/Product/2', locale='en', key='title', value='Outdoor lamp', source_value='Lámpara exterior', mode='translation')
COMPARISON = dict(source=dict(title='Lámpara exterior portátil', sku='NG-1234', id='gid://shopify/Product/1'), columns=[dict(label='ES · es'), dict(label='MX · en')], fields=[dict(key='title', label='Título', values=['Lámpara exterior portátil', 'Outdoor lamp'], edits=[None, EDIT]), dict(key='descriptionHtml', label='Descripción', values=['<p>Una lámpara para disfrutar del exterior.</p>', '<p>An outdoor lamp.</p>'], edits=[None, {**EDIT, 'key': 'body_html', 'value': '<p>An outdoor lamp.</p>'}])], notices=[])
SEARCH = dict(products=[dict(store='ES', store_codes=['ES'], sku='NG-1234', locales={'ES': ['es', 'en']}, candidates=[dict(id='gid://shopify/Product/1', title='Lámpara exterior portátil', handle='lampara-exterior', sku='NG-1234')])], notices=[])


class Pool:
    def connection(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, query): return self
    def fetchone(self): return (3,)


class HTTPException(Exception):
    pass


def page_functions():
    tree = ast.parse((ROOT / 'hub/app.py').read_text(encoding='utf-8'))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in PAGE_FUNCTIONS]
    for node in nodes:
        node.decorator_list = []
    namespace = dict(html=html, json=json, os=os, re=__import__('re'), urlencode=urlencode, pool=Pool(), HTTPException=HTTPException, STYLE='<style>' + (ROOT / 'hub/ui.css').read_text(encoding='utf-8') + '</style>', search_shopify_products=lambda _: SEARCH, compare_shopify_product=lambda _: COMPARISON)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'hub/app.py', 'exec'), namespace)
    return namespace


def main():
    functions = page_functions()
    writes = []
    terms = [dict(id=1, source_store='ES', source_locale='es', target_store='MX', target_locale='en', source_term='lámpara portátil', target_term='portable lamp')]
    output = Path(tempfile.mkdtemp(prefix='newgarden-ui-'))
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.getenv('UI_BROWSER', 'msedge'))
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request_route):
            request = request_route.request
            url = urlparse(request.url)
            path = url.path
            query = parse_qs(url.query)
            if request.method in {'POST', 'PUT', 'DELETE'}:
                writes.append((path, request.post_data_json if request.post_data else None))
            if path == '/api/stores': data = STORES if request.method == 'GET' else {'ok': True}
            elif path.endswith('/shop-locales'): data = [dict(locale='es', name='Español', primary=True, published=True), dict(locale='en', name='English', primary=False, published=True)]
            elif path == '/api/glossary': data = {'terms': terms} if request.method == 'GET' else {'ok': True}
            elif path.startswith('/api/glossary/'): data = {'ok': True}
            elif path == '/api/shopify/products/search': data = SEARCH
            elif path == '/api/shopify/products/compare': data = COMPARISON
            elif path == '/api/translate-cell': data = {'translation': 'Portable outdoor lamp', 'memory_hit': False, 'glossary_terms': 1}
            elif path == '/api/shopify/translations': data = {'ok': True}
            elif path == '/': data = functions['home']()
            elif path == '/products': data = functions['product_browser'](q=query.get('q', [''])[0], source_id=query.get('source_id', [''])[0])
            elif path == '/products/batch': data = functions['batch_products_page']()
            elif path == '/stores': data = functions['stores_page']()
            elif path == '/glossary': data = functions['glossary_page']()
            else:
                request_route.fulfill(status=404, body='Not found')
                return
            request_route.fulfill(status=200, content_type='text/html; charset=utf-8' if isinstance(data, str) else 'application/json', body=data if isinstance(data, str) else json.dumps(data))

        page.route('**/*', route)
        routes = ['/', '/products', '/products/batch', '/glossary', '/stores', '/products?q=NG-1234&source_id=1']
        for width in [1440, 1024, 768, 390, 320]:
            page.set_viewport_size({'width': width, 'height': 1000})
            for index, path in enumerate(routes):
                page.goto('http://ui.test' + path)
                page.wait_for_load_state('networkidle')
                assert page.locator('h1').count() == 1, path
                assert page.locator('nav [aria-current="page"]').count() == 1, path
                overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth')
                assert not overflow, f'Page overflow: {width} {path}'
                assert page.locator('main').get_attribute('id') == 'main-content'
                if width in [1440, 390]:
                    page.screenshot(path=str(output / f'{width}-{index}.png'), full_page=True)
            print(f'PASS responsive: {width}px', flush=True)

        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.goto('http://ui.test/products')
        page.locator('#term').fill('NG-1234')
        page.get_by_role('button', name='Buscar en tiendas').click()
        page.get_by_role('link', name='Comparar este producto').click()
        page.locator('.cell-edit').first.click()
        page.wait_for_function('document.querySelector("#cell-editor").open')
        page.get_by_role('button', name='Traducir con DeepL', exact=True).click()
        page.wait_for_function('document.querySelector("#edit-value").value === "Portable outdoor lamp"')
        page.screenshot(path=str(output / 'editor.png'))
        page.get_by_role('button', name='Cancelar', exact=True).click()
        page.wait_for_function('!document.querySelector("#cell-editor").open')
        assert not any(path == '/api/shopify/translations' for path, _ in writes)
        page.locator('.cell-edit').first.click()
        page.keyboard.press('Escape')
        page.wait_for_function('!document.querySelector("#cell-editor").open')
        page.locator('.cell-edit').first.click()
        page.locator('#edit-value').fill('Reviewed title')
        with page.expect_navigation(wait_until='networkidle'):
            page.get_by_role('button', name='Guardar en Shopify', exact=True).click()
        page.wait_for_load_state('networkidle')
        assert any(path == '/api/shopify/translations' and body['value'] == 'Reviewed title' for path, body in writes)

        page.goto('http://ui.test/products/batch')
        page.wait_for_load_state('networkidle')
        page.locator('#target-store').select_option('MX')
        page.wait_for_load_state('networkidle')
        page.locator('#target-locale').select_option('en')
        page.locator('#skus').fill('NG-1234')
        before = sum(path == '/api/shopify/translations' for path, _ in writes)
        translation_start = len(writes)
        page.locator('#prepare-batch').click()
        page.wait_for_function('document.querySelectorAll(".batch-approve").length === 1')
        assert page.locator('.batch-approve:checked').count() == 0
        assert not any(path == '/api/translate-cell' and body['field_key'] == 'title' for path, body in writes[translation_start:])
        assert sum(path == '/api/shopify/translations' for path, _ in writes) == before
        page.screenshot(path=str(output / 'batch-proposals.png'), full_page=True)
        page.locator('.batch-approve').first.check()
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#approve-batch').click()
        page.wait_for_function('document.querySelector(".batch-state").textContent === "Guardado"')
        assert sum(path == '/api/shopify/translations' for path, _ in writes) == before + 1

        page.goto('http://ui.test/stores')
        page.wait_for_load_state('networkidle')
        page.locator('[data-code="FR"]').click()
        assert page.locator('#code').input_value() == 'FR'
        page.locator('[data-fetch-locales="FR"]').click()
        page.locator('.use-shop-locales').click()
        assert page.locator('#locales').input_value() == 'es, en'
        page.locator('#store-form button').click()
        page.wait_for_function('document.querySelector("#message").textContent === "Configuración guardada"')

        page.goto('http://ui.test/glossary')
        page.wait_for_load_state('networkidle')
        page.locator('#source-term').fill('luz cálida')
        page.locator('#target-term').fill('warm light')
        page.locator('#save-term').click()
        page.wait_for_function('document.querySelector("#term-status").textContent === "Guardado"')
        assert any(path == '/api/glossary' and body['source_term'] == 'luz cálida' for path, body in writes)

        page.emulate_media(reduced_motion='reduce')
        page.goto('http://ui.test/products?source_id=1')
        page.locator('.cell-edit').first.click()
        assert page.locator('#cell-editor').evaluate('(el) => getComputedStyle(el).transitionDuration') == '0s'
        page.keyboard.press('Escape')
        page.wait_for_function('!document.querySelector("#cell-editor").open')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('.cell-edit').first.click()
        page.wait_for_function('document.querySelector("#cell-editor").classList.contains("is-open")')
        assert page.locator('#cell-editor').evaluate('(el) => el.scrollWidth <= el.clientWidth')
        assert page.locator('#cell-editor').evaluate('(el) => {const r=el.getBoundingClientRect(); return r.top >= 15 && r.bottom <= innerHeight - 15}')
        page.screenshot(path=str(output / 'editor-mobile.png'))
        page.keyboard.press('Escape')
        page.wait_for_function('!document.querySelector("#cell-editor").open')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.goto('http://ui.test/')
        page.keyboard.press('Tab')
        assert page.locator('.skip-link').evaluate('(el) => el === document.activeElement')
        assert not errors, errors
        browser.close()
    print(f'PASS: 30 responsive route checks; search, editor, batch approval, stores, glossary, keyboard and reduced motion. Screenshots: {output}')


if __name__ == '__main__':
    main()
