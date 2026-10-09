# Newgarden Content Hub (piloto aislado)

Servicio separado del Compose existente del directorio padre. Cola persistente secuencial, aprobación campo a campo y publicación exclusivamente simulada. El worker por lotes requiere DeepL API (`TRANSLATION_PROVIDER=deepl`); no genera traducciones de muestra. La clave DeepL se configura en el `.env` local y solo se entrega al worker; no se incluye en el repositorio ni en el código del navegador.

## Descubrimiento realizado

- Ubuntu con 79 GB disponibles en el volumen raíz al inspeccionar.
- Docker tenía activos `services-n8n-1` y `services-webp-converter-1`; existía la red `services_n8n_network`. No se alteraron contenedores, redes, configuración ni volúmenes existentes.
- Sin datos de acceso Shopify no se confirmó topología real, locales, permisos ni dominios. `pt-PT` y `nl-NL` en migración son valores iniciales declarativos y deben contrastarse mediante Shopify antes de integrar.

## Requisitos

Docker Engine y Docker Compose v2. Crea este directorio en una ruta con acceso limitado al operador (la propuesta original `/opt/newgarden/content-hub` puede usarse si se despliega allí). `HUB_BIND=127.0.0.1` requiere proxy interno para acceso LAN. Si se publica directamente en LAN, sustituirlo por una IP LAN protegida por firewall. No conectar la red de n8n hasta acordar ese cambio.

```sh
cd /opt/newgarden/content-hub
cp .env.example .env
chmod 600 .env
openssl rand -hex 32   # copiar resultado en POSTGRES_PASSWORD
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8787/health
```

La app es de uso interno y no requiere login. La interfaz está en `/`, el explorador Shopify en `/products` y la configuración de tiendas en `/stores`; Swagger está en `/docs`. En cada tienda, `Consultar locales Shopify` muestra los códigos exactos habilitados, si son principales y si están publicados. El token necesita el scope `read_locales` para esa consulta; se pueden cargar todos los locales publicados y seleccionar los que se van a usar. Cada tienda conserva sus propios códigos, sin conversiones inferidas por país. El explorador busca por SKU, texto/nombre o GID en todas las tiendas activas, lee producto, variantes, imágenes, SEO y metacampos, y compara las traducciones registradas para locales adicionales con los campos actuales de los productos equivalentes en otras tiendas. Lee todas las páginas de metacampos de producto y variante del producto español, ignorando los de Yotpo; las variantes destino se emparejan por SKU (o título si no tienen SKU). Para cada metacampo de origen muestra `Falta el metacampo` si no existe en destino y `Metacampo vacío` si existe sin contenido. Editar el locale principal de una tienda actualiza solo el contenido base del producto o metacampo elegido; editar cualquier locale secundario registra una traducción Shopify, sin cambiar sus equivalentes. El editor de descripción conserva el HTML. La comparación requiere permisos Admin GraphQL de lectura de productos, `read_translations` y `read_locales`; las traducciones requieren `write_translations` y las actualizaciones del contenido base requieren `write_products`. La versión API se controla con `SHOPIFY_API_VERSION` (por defecto `2026-07`). Por defecto, Compose enlaza el servicio a `127.0.0.1`. Para acceso desde la LAN, configura `HUB_BIND` con la IP local del servidor y limita el acceso con el firewall de la red. No expongas el puerto a Internet. `.env` queda excluido de Git. La base de datos y worker no publican puertos; el worker mantiene concurrencia uno con un advisory lock PostgreSQL y no reanuda trabajos al arrancar.

Las migraciones SQL se registran y aplican una sola vez al iniciar la app o el worker. La migración `002` elimina las tiendas independientes NL/PT, sus correspondencias y reglas; cancela los trabajos aún en cola y conserva el historial ya generado y los locales NL/PT de ES.

Para activar DeepL, configura `TRANSLATION_PROVIDER=deepl` y la clave como `DEEPL_AUTH_KEY` en `.env`. DeepL API Free usa automáticamente `api-free.deepl.com` cuando la clave termina en `:fx`; las demás claves usan el endpoint Pro. Luego aplica la configuración recreando el worker: `docker compose up -d --force-recreate worker`. Las llamadas del flujo por lotes salen desde el worker y las del editor desde la API del servidor; la clave no se expone al navegador.

## Glosario y aprendizaje

En `/glossary` cada término se define con tienda e idioma de origen y tienda e idioma de destino. Los locales se consultan en Shopify, el idioma principal publicado se selecciona por defecto y se pueden elegir los demás locales configurados. Las reglas nuevas quedan limitadas a ese par de tiendas y locale; las reglas antiguas sin tienda siguen marcadas como globales hasta eliminarlas o sustituirlas. Al usar `Traducir con DeepL` en una celda, el servicio protege los términos aprobados durante la llamada y los repone en la respuesta; en HTML solo procesa nodos de texto. Al guardar una edición manual exitosa, los pares de texto de hasta 500 caracteres se almacenan como memoria exacta por locale y se devuelven en usos posteriores sin otra llamada DeepL. Las correcciones no se convierten automáticamente en reglas léxicas generales: el operador puede promover expresiones repetidas al glosario.

La API de traducción requiere `DEEPL_AUTH_KEY` en el servidor, no en el navegador, y conserva la revisión humana antes de guardar en Shopify, salvo en las campañas de lotes con publicación directa (véase «Publicación directa y rechazo de lotes»).

En `/products/batch` se puede pegar una lista de hasta 50 SKU para preparar, secuencialmente, propuestas de descripción HTML, título SEO, descripción SEO y metacampos de texto traducibles (producto y variante) para una tienda y locale elegidos. El título principal del producto se excluye siempre de la traducción automática por lotes; puede traducirse en el editor individual, donde una persona inicia y revisa esa acción directamente. Los metacampos de texto enriquecido y listas conservan su estructura; los tipos no textuales no se traducen. El selector consulta los locales publicados directamente en Shopify; el locale no necesita duplicarse antes en la configuración interna de la tienda. Los locales secundarios, como `en` en MX cuando `es` es el principal, se registran como traducciones Shopify. El flujo reutiliza la traducción con DeepL, el glosario y la memoria exacta del explorador. Con la publicación directa desactivada, cada propuesta aparece sin aprobar; hay que revisar y marcar expresamente cada campo y confirmar el guardado, y la preparación no escribe en Shopify. Con la casilla «Traducir y subir directamente a Shopify» (marcada por defecto en esta página) los campos que superan la puerta de calidad se escriben en Shopify durante la preparación; véase la sección siguiente. La interfaz requiere mantener abierta la página mientras procesa la lista; los guardados aprobados se realizan campo a campo y los errores se muestran junto al campo correspondiente. La búsqueda y comparación de un producto en `/products` sigue disponible por separado.

La comparación individual funciona como primera iteración del panel de gestión: muestra el estado por campo y destino (`Al día`, `Desactualizado`, `Falta en destino`, `Cambio en destino` o `Sin referencia`). Al guardar un campo desde el hub, se conserva una huella del origen y del destino. Un cambio posterior en el origen marca la propuesta como desactualizada; un cambio externo en el destino se marca como conflicto y requiere revisión humana. El contenido que ya estaba en Shopify antes de activar este seguimiento aparece como `Sin referencia` hasta guardarlo desde el hub. La cobertura editable de esta iteración es la ya soportada por el editor: título, descripción, handle, SEO, vendor, tipo de producto y metacampos de texto admitidos; ampliar la edición a todos los campos no identificadores y sus mutaciones Shopify es el siguiente tramo del panel. Los IDs se usan solo para relacionar recursos.

Para integrar una pipeline externa hay una API persistente por SKU: `POST /api/translation-batches` con `{"sku":"SKU-123"}` prepara propuestas para todos los locales publicados y devuelve el ID del lote y el resultado de calidad de cada campo. `GET /api/translation-batches/{id}` recupera el lote. Opcionalmente se edita una propuesta con `PATCH /api/translation-batches/{id}/items/{item_id}` y `{"proposed_text":"…"}`. Los campos pendientes pueden aprobarse explícitamente con `POST /api/translation-batches/{id}/approve` y `{"item_ids":[1,2]}`. `POST /api/translation-batches/{id}/apply` escribe en Shopify solo los campos aprobados, incluidos los que pasaron la puerta automática. `POST /api/translation-batches/{id}/reject` rechaza el lote. Errores por producto/local/campo quedan en el lote y no impiden revisar el resto.

Las propuestas de lote pasan ahora por una puerta determinista de integridad. `quality` en cada campo contiene estado (`PASS`, `REVIEW` o `FAIL`), puntuación, umbral, señales y fallos críticos. Se comprueban texto vacío, preservación de cifras/unidades, variables y marcadores, estructura HTML para descripciones y una relación de longitud configurable. El texto idéntico a la fuente resta puntos, pero solo genera aviso implícito para evitar falsos positivos con marcas y nombres. `QUALITY_MIN_SCORE` (85 por defecto), `QUALITY_MIN_LENGTH_RATIO` (0.35) y `QUALITY_MAX_LENGTH_RATIO` (3.0) son configurables en `.env`. Con `QUALITY_AUTO_APPROVE=true` (por defecto), solo `PASS` se aprueba automáticamente; `REVIEW` y `FAIL` quedan pendientes para revisión. Esto es una puntuación de integridad, no un juicio lingüístico de fidelidad o fluidez. El guardado en Shopify sigue siendo independiente: el integrador debe invocar `/apply` para campos aprobados. Editar una propuesta recalcula `quality` y no la autoaprueba.

## Publicación directa y rechazo de lotes

Por política de administración, las campañas necesitan rendimiento inmediato: la revisión del contenido traducido se hace en Shopify y el hub solo traduce y sube. Esto es una excepción a la aprobación humana previa y se controla por petición.

**Publicación directa (`auto_apply`)**

- `POST /api/translation-batches` acepta `"auto_apply": true`. **Por defecto es `false`**: una integración que no lo indique sigue el flujo de revisión y no escribe en Shopify.
- En `/products/batch` la casilla «Traducir y subir directamente a Shopify» viene marcada y pide confirmación antes de empezar. Al desmarcarla se vuelve al flujo con revisión.
- Con `auto_apply`, cada campo traducido con calidad `PASS` se aprueba y se escribe en Shopify en la misma petición (mismo código que `/apply`). La excepción habitual para `handle` (siempre a revisión) no se aplica en este modo, y `QUALITY_AUTO_APPROVE` y la marca `require_review` del perfil no intervienen: el modo directo lo decide `auto_apply`.
- **Guarda de calidad:** un campo con calidad `REVIEW` o `FAIL`, o marcado para revisión, **no se sube**. Queda `PENDING_REVIEW` y el lote en `PENDING_REVIEW`, visible en «Revisión de lotes» para editarlo, aprobarlo o rechazarlo. Esto sigue siendo una puntuación de integridad, no un juicio lingüístico.
- Un campo cuya escritura en Shopify falla queda en `APPLY_ERROR` con el error; se puede reintentar con `/apply` o descartar. El resto del lote continúa. Un lote donde todo se sube queda `COMPLETED`.
- Los guardados registran la huella de sincronización igual que en el editor, y las traducciones usan `translationsRegister` con el digest vigente.

**Rechazar un lote**

- `POST /api/translation-batches/{id}/reject` (botón «Rechazar lote» en «Revisión de lotes», con confirmación). Pasa a `REJECTED` todos los campos no aplicados (`PENDING_REVIEW`, `APPROVED` y `APPLY_ERROR`). Devuelve `409` si no queda ninguno.
- El lote queda `REJECTED`; si ya tenía campos subidos a Shopify (`SAVED`), queda `COMPLETED`. Rechazar **no revierte** lo ya escrito en Shopify: eso se corrige allí o en el editor.
- Los campos `ERROR` de preparación no cambian. Un campo rechazado ya no se puede editar, aprobar ni aplicar; para reintentar hay que crear otro lote.
- Estados de campo: `PENDING_REVIEW`, `APPROVED`, `SAVED`, `APPLY_ERROR`, `ERROR`, `REJECTED`. Estados de lote: `PREPARING`, `PENDING_REVIEW`, `APPROVED`, `COMPLETED`, `READY_WITH_ERRORS`, `REJECTED`. No requiere migración.

## Operación

```sh
docker compose up -d --build       # arrancar/actualizar imágenes
docker compose stop                # detener sin borrar datos
docker compose start               # arrancar contenedores detenidos; no reanuda cola
docker compose down                # retirar contenedores/red, conserva volumen
docker compose logs --since=1h app worker
docker compose ps
```

Para hacer backup, pasar una ruta de un volumen externo ya montado y administrado por el operador. El script valida que PostgreSQL pueda listar el dump y guarda SHA-256; el almacenamiento externo y su verificación periódica quedan a cargo del operador:

```sh
./scripts/backup.sh /mnt/backup/newgarden/contenthub-$(date +%F-%H%M).dump
sha256sum --check /mnt/backup/newgarden/contenthub-FECHA-HORA.dump.sha256
```

Restauración destructiva del volumen lógico de esta aplicación, tras detenerla y validar backup:

```sh
docker compose stop app worker
./scripts/restore.sh /mnt/backup/newgarden/contenthub-FECHA-HORA.dump
docker compose up -d
```

Actualización: respaldar primero, obtener/revisar cambios del repositorio en esta ruta, luego `docker compose up -d --build` y comprobar `/health`. Mantener dumps fuera del repositorio. Rotación de logs usa el driver por defecto de Docker actualmente; falta acordar límite de retención del host.

## Estado y limitaciones

- Comprobaciones realizadas anteriormente en esta VM son anteriores a la integración actual de DeepL; se verificaron healthcheck, cola y simulación de publicación. Aún hay que realizar una llamada supervisada usando una clave DeepL de prueba para validar la integración en esa cuenta.
- Implementado: gestión web de tiendas/locales/tokens Shopify, importación manual con hash/versionado, mapping explícito, trabajos únicos por producto/tienda/local/version, worker secuencial con DeepL API para título, descripción HTML, SEO y textos alternativos, parada por cuota y errores de proveedor, resultados históricos, chequeos QA básicos, aprobación de campos y registro de publicación simulada.
- Pendiente antes de uso con productos reales: configurar una clave DeepL y validar una generación supervisada; confirmar códigos locales mediante Shopify; automatizar la publicación aprobada desde la cola, auditoría, glosarios remotos DeepL, colisiones de handle, QA ampliado, backup automatizado/verificado y endurecimiento CSRF/TLS/rate limit. El explorador permite traducir producto y metacampos de texto con DeepL antes de guardar; `/glossary` gestiona términos preferidos por par de tiendas y locale y cada guardado manual aprende una memoria exacta reutilizable para el mismo texto.
- No existe fallback API, compras de créditos, cron de reintento ni publicación automática desde la cola del worker. Las campañas de lotes con `auto_apply` sí escriben en Shopify desde la API (véase arriba). Shopify real está desactivado incluso si `SIMULATION=false`.
- Las tareas `BLOCKED_MAPPING` no se ejecutan hasta importar una correspondencia con GID destino.

## Estimación de caracteres a traducir

`scripts/estimate_chars.py` (solo lectura) exporta el catálogo de ES con una operación masiva de Shopify y cuenta los caracteres de origen de los campos que traduce el hub (descripción sin etiquetas, SEO, metacampos de producto y de variante). Multiplica por los pares tienda·locale con perfil DeepL activo y muestra carga inicial y mantenimiento mensual hipotético. No descuenta memoria de traducción ni glosario. Ejecútalo con `docker compose exec -T app python - < scripts/estimate_chars.py`; añade `-- --help` tras `python -` para ver las opciones (`--limit`, `--skip`, `--rerun-factor`, `--json`).

## Desarrollo de la interfaz

El sistema visual compartido está en `hub/ui.css`; `shell()` en `hub/app.py` construye la navegación y el marco de las páginas. El CSS se carga desde el paquete `hub`, que ya se incluye en la imagen Docker. No requiere compilación frontend ni fuentes externas. `PRODUCT.md` recoge el criterio de diseño.

Para comprobar la interfaz de forma aislada, instala Playwright en tu entorno de desarrollo (`python -m pip install playwright`) y ejecuta `python scripts/check_ui.py`. Usa Microsoft Edge por defecto; para Chromium, ejecuta `python -m playwright install chromium` y configura `UI_BROWSER=chromium`. La prueba renderiza las funciones reales de las páginas con datos de prueba e intercepta todas las solicitudes: no contacta con Shopify, DeepL ni PostgreSQL. Comprueba seis vistas a cinco anchuras, búsqueda, edición, aprobación por campo, tiendas, glosario, teclado y movimiento reducido. Guarda capturas en una carpeta temporal indicada al finalizar. No sustituye las pruebas de integración con servicios reales.

## Fuentes consultadas

- DeepL [Translate Text API](https://developers.deepl.com/api-reference/translate/request-translation) y [autenticación y endpoints](https://developers.deepl.com/docs/getting-started/auth).
- Shopify [Manage translated content](https://shopify.dev/docs/apps/build/markets/manage-translated-content), [`translationsRegister`](https://shopify.dev/docs/api/admin-graphql/latest/mutations/translationsregister), [`productUpdate`](https://shopify.dev/docs/api/admin-graphql/latest/mutations/productUpdate) y [`metafieldsSet`](https://shopify.dev/docs/api/admin-graphql/latest/mutations/metafieldsSet): las traducciones necesitan el digest vigente; los cambios de contenido base actualizan únicamente el recurso elegido.
