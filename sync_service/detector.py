import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()


def get_conn(schema: str):
    """Connect using meta schema (schema + axdef)"""
    meta_schema = schema + "axdef"
    conn = psycopg2.connect(
        host     = os.getenv("DB_HOST"),
        port     = os.getenv("DB_PORT"),
        database = os.getenv("DB_NAME"),
        user     = os.getenv("DB_USER"),
        password = os.getenv("DB_PASS")
    )
    cur = conn.cursor()
    cur.execute(f"SET search_path TO {meta_schema}")
    cur.close()
    return conn, meta_schema


def detect_modules(schema: str) -> dict:
    """
    Reads axp_vw_menu from meta schema.
    Returns grouped module tree:
    {
      "Payroll": {
        "Attendance Management": {
          "forms":  ["rcatt", "uattm"],
          "iviews": ["ivatreem", "edattd"]
        }
      }
    }

    axp_vw_menu columns:
      menupath, caption, name, pagetype, levelno, parent

    pagetype first char:
      't' → tstruct (form)  → transid = pagetype[1:]
      'i' → iview           → iview   = pagetype[1:]
      ''  → header/group    → skip
    """
    conn, meta_schema = get_conn(schema)
    cur = conn.cursor()

    cur.execute(f"""
        SELECT
            menupath,
            caption,
            name,
            pagetype,
            levelno,
            parent
        FROM {meta_schema}.axp_vw_menu
        WHERE levelno IN (1, 2)
        ORDER BY menupath
    """)

    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cur.close()
    conn.close()

    # ── Build lookup: name → caption for level 1 headers ──
    # e.g. Head531158 → "Attendance Management"
    # e.g. Head775217 → "Payroll"
    header_captions = {}
    for row in rows:
        data = dict(zip(cols, row))
        if not data.get('pagetype'):  # headers have empty pagetype
            header_captions[data['name']] = data['caption']

    # ── Build module tree from level 2 items ──
    tree = {}

    for row in rows:
        data     = dict(zip(cols, row))
        levelno  = data.get('levelno')
        pagetype = data.get('pagetype') or ''

        if levelno != 2:
            continue
        if not pagetype:
            continue

        # Derive type and id from pagetype
        type_char = pagetype[0].lower()  # 't' or 'i'
        item_id   = pagetype[1:]         # actual transid or iview name

        if type_char not in ('t', 'i'):
            continue  # skip web or unknown

        # Get sub_module (direct parent caption)
        parent_name = data.get('parent', '')
        sub_mod     = header_captions.get(parent_name, 'Unknown')

        # Get root_module from menupath
        # menupath = \Payroll\Attendance Management\Record Attendance
        parts    = [p for p in data.get('menupath', '').split('\\') if p]
        root_mod = parts[0] if parts else 'Unknown'

        # Build tree
        if root_mod not in tree:
            tree[root_mod] = {}
        if sub_mod not in tree[root_mod]:
            tree[root_mod][sub_mod] = {
                'forms':  [],
                'iviews': []
            }

        if type_char == 't':
            if item_id not in tree[root_mod][sub_mod]['forms']:
                tree[root_mod][sub_mod]['forms'].append(item_id)
        elif type_char == 'i':
            if item_id not in tree[root_mod][sub_mod]['iviews']:
                tree[root_mod][sub_mod]['iviews'].append(item_id)

    return tree


def detect_practice_chains(schema: str, transids: list) -> list:
    """
    Reads genmap from meta schema to find practice chains.
    Returns list of chains:
    [
      ["rcatt", "uattm"],
      ["fppr", "rattn"]
    ]
    """
    if not transids:
        return []

    conn, meta_schema = get_conn(schema)
    cur = conn.cursor()

    placeholders = ','.join(['%s'] * len(transids))
    cur.execute(f"""
        SELECT
            stransid,
            targettrasid,
            caption,
            onapprove,
            active
        FROM {meta_schema}.v_genmap
        WHERE lower(stransid) IN ({placeholders})
        AND active = 'TRUE'
    """, [t.lower() for t in transids])

    rows = cur.fetchall()
    cur.close()
    conn.close()

    # Build chain map
    chain_map = {}
    for row in rows:
        src    = row[0].lower() if row[0] else ''
        target = row[1].lower() if row[1] else ''
        if src and target:
            if src not in chain_map:
                chain_map[src] = []
            chain_map[src].append(target)

    # Build chains
    chains  = []
    visited = set()

    def build_chain(start):
        chain   = [start]
        current = start
        while current in chain_map:
            next_forms = chain_map[current]
            if not next_forms:
                break
            next_form = next_forms[0]
            if next_form in visited:
                break
            chain.append(next_form)
            visited.add(next_form)
            current = next_form
        return chain

    for transid in [t.lower() for t in transids]:
        if transid not in visited:
            chain = build_chain(transid)
            if len(chain) > 1:
                chains.append(chain)
            visited.add(transid)

    return chains