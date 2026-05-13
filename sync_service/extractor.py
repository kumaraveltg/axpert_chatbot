import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()


def get_conn(schema: str, host=None, port=None, db_name=None, username=None, password=None):
    """Derive meta schema from data schema and connect"""
    meta_schema = schema + "axdef"
    conn = psycopg2.connect(
        host     = host     or os.getenv("DB_HOST"),
        port     = port     or os.getenv("DB_PORT"),
        database = db_name  or os.getenv("DB_NAME"),
        user     = username or os.getenv("DB_USER"),
        password = password or os.getenv("DB_PASS")
    )
    cur = conn.cursor()
    cur.execute(f"SET search_path TO {meta_schema}")
    cur.close()
    return conn, meta_schema


def bool_val(val) -> str:
    """Convert t/f or True/False or TRUE/FALSE → Yes/No"""
    if val in (True, 't', 'T', 'Y', 'y', '1', 1, 'TRUE', 'true', 'True'):
        return 'Yes'
    return 'No'

def rows_to_dicts(cur) -> list:
    """Convert cursor result to list of dicts"""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def extract_form_metadata(schema: str, transid: str, host=None, port=None, db_name=None, username=None, password=None) -> dict:
    
    """
    Reads all metadata for one TransID from views.
    schema  = data schema e.g. 'hcaspay'
    transid = form transid e.g. 'ATTN'
    Returns complete form data as dict.
    """
    conn, meta_schema = get_conn(schema, host=host, port=port, db_name=db_name, username=username, password=password)
    cur = conn.cursor()
    tid = transid

    # ── 1. Form header from vw_tstructs ──────────────────────
    cur.execute(f"""
        SELECT
            transid, caption, purpose,
            savecontrol, deletecontrol,
            workflow, attachment, listview,
            trackchanges, layouttype,
            cachedsave, menuposition
        FROM {meta_schema}.vw_tstructs
        WHERE lower(transid) = lower(%s)
    """, (tid,))

    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return {}

    cols = [d[0] for d in cur.description]
    form = dict(zip(cols, row))

    # ── 2. Data Containers from vw_dc ────────────────────────
    cur.execute(f"""
        SELECT
            transid, name, caption,
            tablename, asgrid, allowchange,
            allowempty, adddcrows, deletedcrows,
            popup, purpose, booleandc, defaultstate
        FROM {meta_schema}.vw_dc
        WHERE lower(transid) = lower(%s)
        ORDER BY name
    """, (tid,))

    dc_rows = rows_to_dicts(cur)
    dcs     = []

    for dc in dc_rows:
        dc_name = dc.get('name', '')

        # ── 3. Fields from vw_field_information ──────────────
        cur.execute(f"""
            SELECT
                sno, name, caption,
                datatype, customdatatype,
                datawidth, fdecimal,
                modeofentry, detail,
                hidden, allowempty, readonly,
                savevalue, expression,
                validateexpression,
                sql, listvalues, hint,
                purpose, fromtransid,
                sourcefield, source_table,
                mastertstruct, masterfield,
                refresh, setcarry, displaytotal,
                allowduplicate, onlypositive
            FROM {meta_schema}.vw_field_informations
            WHERE lower(transid) = lower(%s)
            AND lower(dcname) = lower(%s)
            ORDER BY sno
        """, (tid, dc_name))

        fields = rows_to_dicts(cur)
        dc['fields'] = fields
        dcs.append(dc)

    form['datacontainers'] = dcs

    # ── 4. GenMaps from v_genmap ─────────────────────────────
    cur.execute(f"""
        SELECT
            name, caption, targettstruct,
            targettrasid, dcname, basedondc,
            schemaoftarget, onpost, onapprove,
            onreject, purpose, active,
            rowcontrol, groupfield
        FROM {meta_schema}.v_genmap
        WHERE lower(stransid) = lower(%s)
        AND active = 'TRUE'
    """, (tid,))

    form['genmaps'] = rows_to_dicts(cur)

    # ── 5. MDMaps from vw_mdmap ──────────────────────────────
    cur.execute(f"""
        SELECT
            name, caption, mastertransaction,
            masterfield, mastersearchfield,
            detailsearchfield, mastertable, extended
        FROM {meta_schema}.vw_mdmap
        WHERE lower(stransid) = lower(%s)
    """, (tid,))

    form['mdmaps'] = rows_to_dicts(cur)

    # ── 6. FillGrids from v_fillgrid ─────────────────────────
    cur.execute(f"""
        SELECT
            name, caption, targetdc,
            sourcedc, sql_editor_sql,
            multiselect, autoshow,
            executeonsave, purpose,
            mappingdetails, groupfield
        FROM {meta_schema}.v_fillgrid
        WHERE lower(stransid) = lower(%s)
    """, (tid,))

    form['fillgrids'] = rows_to_dicts(cur)

    # ── 7. Toolbar buttons from vw_toolbar_default_btns ──────
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
        AND table_name = 'vw_toolbar_default_btns'
        LIMIT 1
    """, (meta_schema,))

    chk = cur.fetchone()
    if chk:
        cur.execute(f"""
            SELECT *
            FROM {meta_schema}.vw_toolbar_default_btns
            
        """, (tid,))
        form['buttons'] = rows_to_dicts(cur)
    else:
        form['buttons'] = []

    # workflows handled by axpages workflow config — not needed here
    form['workflows'] = []

    cur.close()
    conn.close()
    return form


def debug_form(schema: str, transid: str):
    """
    Quick debug — print what each view returns for a transid.
    Run: python -c "from sync_service.extractor import debug_form; debug_form('hcaspay','ATTN')"
    """
    form = extract_form_metadata(schema, transid)

    print(f"\n{'='*50}")
    print(f"FORM: {form.get('caption')} | TransID: {form.get('transid')}")
    print(f"Workflow: {bool_val(form.get('workflow'))}")
    print(f"Save: {bool_val(form.get('savecontrol'))} | Delete: {bool_val(form.get('deletecontrol'))}")

    print(f"\nDATA CONTAINERS: {len(form.get('datacontainers', []))}")
    for dc in form.get('datacontainers', []):
        grid = "Grid" if bool_val(dc.get('asgrid')) == 'Yes' else "Header"
        print(f"  [{grid}] {dc.get('caption')} | DC: {dc.get('name')} | Table: {dc.get('tablename')}")
        print(f"  Fields: {len(dc.get('fields', []))}")
        for f in dc.get('fields', [])[:3]:
            print(f"    - [{f.get('name')}] {f.get('caption')} | {f.get('modeofentry')} | allowempty={f.get('allowempty')}")
        if len(dc.get('fields', [])) > 3:
            print(f"    ... and {len(dc.get('fields', [])) - 3} more fields")

    print(f"\nGENMAPS  : {len(form.get('genmaps', []))}")
    for g in form.get('genmaps', []):
        print(f"  → {g.get('caption')} | Target: {g.get('targettrasid')} | On: {g.get('onpost')}")

    print(f"\nMDMAPS   : {len(form.get('mdmaps', []))}")
    for m in form.get('mdmaps', []):
        print(f"  → {m.get('caption')} | From: {m.get('mastertransaction')} | {m.get('masterfield')} → {m.get('detailsearchfield')}")

    print(f"\nFILLGRIDS: {len(form.get('fillgrids', []))}")
    for fg in form.get('fillgrids', []):
        print(f"  → {fg.get('caption')} | Target DC: {fg.get('targetdc')}")


    print(f"{'='*50}\n")