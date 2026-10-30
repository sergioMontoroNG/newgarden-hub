"""Perfiles de traducción por tienda y locale de Shopify (tabla translation_profiles)."""
from .db import pool

_COLUMNS = ('store_code', 'shopify_locale', 'role', 'deepl_target', 'formality', 'require_review', 'active')


def _row(row):
    return dict(zip(_COLUMNS, row))


def active_profiles(store_code):
    """Perfiles activos de una tienda, indexados por locale en minúsculas."""
    with pool.connection() as c:
        rows = c.execute(
            f"SELECT {','.join(_COLUMNS)} FROM translation_profiles WHERE store_code=%s AND active",
            (store_code,)).fetchall()
    return {r[1].lower(): _row(r) for r in rows}


def list_active_profiles():
    """Todos los perfiles activos, ordenados por tienda y locale."""
    with pool.connection() as c:
        rows = c.execute(
            f"SELECT {','.join(_COLUMNS)} FROM translation_profiles WHERE active ORDER BY store_code, shopify_locale"
        ).fetchall()
    return [_row(r) for r in rows]


def target_profile(store_code, locale):
    """Perfil activo con destino DeepL para (tienda, locale), o None si no es traducible."""
    profile = active_profiles(store_code).get((locale or '').lower())
    return profile if profile and profile['deepl_target'] else None
