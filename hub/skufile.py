"""Extract a SKU list from an uploaded xlsx, csv or txt file using only the standard library."""
import csv, io, re, zipfile
import xml.etree.ElementTree as ET

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_SKUS = 5000
_NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
       'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
       'pr': 'http://schemas.openxmlformats.org/package/2006/relationships'}
_SKU_HEADERS = {'sku', 'skus', 'código sku', 'codigo sku', 'variant sku', 'sku variante'}
_ALT_HEADERS = {'referencia', 'ref', 'codigo', 'código'}


def _column_index(ref):
    letters = re.match(r'[A-Z]+', ref).group()
    index = 0
    for ch in letters:
        index = index * 26 + ord(ch) - 64
    return index - 1


def _xlsx_rows(data):
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError('El archivo no es un Excel .xlsx válido (los .xls antiguos no se admiten: guárdalo como .xlsx o .csv)')
    for info in archive.infolist():
        if info.file_size > 50 * 1024 * 1024:
            raise ValueError('El Excel descomprimido es demasiado grande')
    names = set(archive.namelist())
    shared = []
    if 'xl/sharedStrings.xml' in names:
        for si in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si', _NS):
            shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % _NS['m'])))
    sheet_path = 'xl/worksheets/sheet1.xml'
    try:
        workbook = ET.fromstring(archive.read('xl/workbook.xml'))
        rid = workbook.find('m:sheets/m:sheet', _NS).get('{%s}id' % _NS['r'])
        for rel in ET.fromstring(archive.read('xl/_rels/workbook.xml.rels')).findall('pr:Relationship', _NS):
            if rel.get('Id') == rid:
                target = rel.get('Target').lstrip('/')
                sheet_path = target if target.startswith('xl/') else 'xl/' + target
    except (KeyError, AttributeError, ET.ParseError):
        pass
    if sheet_path not in names:
        raise ValueError('No se encontró la primera hoja del Excel')
    rows = []
    for row in ET.fromstring(archive.read(sheet_path)).iter('{%s}row' % _NS['m']):
        values = {}
        for cell in row.findall('m:c', _NS):
            kind = cell.get('t')
            if kind == 'inlineStr':
                text = ''.join(t.text or '' for t in cell.iter('{%s}t' % _NS['m']))
            else:
                v = cell.find('m:v', _NS)
                text = '' if v is None or v.text is None else v.text
                if kind == 's' and text.isdigit() and int(text) < len(shared):
                    text = shared[int(text)]
            values[_column_index(cell.get('r'))] = text
        rows.append([values.get(i, '') for i in range(max(values) + 1)] if values else [])
    return rows


def _text_rows(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('latin-1')
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def extract_skus(filename, data):
    """Return unique SKUs: the column headed like "SKU" if there is one, otherwise column A."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('El archivo supera 5 MB')
    if filename.lower().endswith('.xls'):
        raise ValueError('Los .xls antiguos no se admiten: guárdalo como .xlsx o .csv')
    rows = _xlsx_rows(data) if filename.lower().endswith(('.xlsx', '.xlsm')) else _text_rows(data)
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise ValueError('El archivo está vacío')
    column, start = 0, 0
    heads = [str(c).strip().casefold() for c in rows[0]]
    for names in (_SKU_HEADERS, _ALT_HEADERS):
        match = next((i for i, h in enumerate(heads) if h in names), None)
        if match is not None:
            column, start = match, 1
            break
    skus = list(dict.fromkeys(str(r[column]).strip() for r in rows[start:] if len(r) > column and str(r[column]).strip()))
    if not skus:
        raise ValueError('No se encontraron SKU en el archivo')
    if len(skus) > MAX_SKUS:
        raise ValueError(f'El archivo tiene {len(skus)} SKU; el máximo es {MAX_SKUS}')
    return skus
