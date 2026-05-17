"""
chat_service/sql_builder.py

Builds SQL from Axpert metadata using extract_form_metadata().
FK convention: child_table.{parent_tablename}id

Query types supported:
  - run_report_sql      → full rows with filters
  - run_count_sql       → COUNT(*) with filters
  - run_grouped_sql     → GROUP BY with COUNT/SUM/AVG
  - run_aggregated_sql  → SUM/AVG/MIN/MAX per group
  - run_ranked_sql      → TOP N by a numeric field
  - run_period_sql      → GROUP BY month/year trend
  - run_analytical_sql  → Window functions (vs avg, % of total)
  - run_tree_sql        → Recursive hierarchy / org chart
"""
import re
from sync_service.extractor import extract_form_metadata, get_runtime_conn, bool_val
from shared.cache import cache, TTL_METADATA, TTL_SQL

 
_DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$|^\d{2}/\d{2}/\d{4}$')

def _is_date_val(v: str) -> bool:
    return bool(_DATE_PATTERN.match(v.strip()))

# ── Metadata with cache ───────────────────────────────────────

def get_form_meta(schema: str, transid: str) -> dict:
    key    = cache.meta_key(schema, transid)
    cached = cache.get(key)
    if cached:
        return cached
    form = extract_form_metadata(schema, transid)
    if form:
        cache.set(key, form, ttl=TTL_METADATA)
    return form


# ── Field classifier ──────────────────────────────────────────

def classify_field(field: dict) -> str:
    mode         = (field.get('modeofentry') or '').lower()
    hidden       = bool_val(field.get('hidden')) == 'Yes'
    src_tb       = field.get('source_table') or ''
    src_f        = field.get('sourcefield')  or ''
    fname        = (field.get('name') or '').lower()
    fromtransid  = field.get('fromtransid') or ''   # ← correct field name

    if hidden:
        return 'hidden'
    if 'image' in fname:
        return 'hidden'
    if mode == 'calculate':
        return 'calculated'
    if mode in ('select from sql', 'select from form') and src_tb and src_f:
        return 'lookup'
    if mode in ('select from sql', 'select from form') and src_tb and not src_f:
        return 'form_lookup'   # ← src_tb exists but sourcefield empty  # ← has fromtransid, no source_table
    if mode == 'fill':
        if src_tb and src_f:
            return 'fill_join'
        return 'fill_skip'
    return 'direct'

def get_form_table(schema: str, fromtransid: str) -> tuple:
    """
    Resolve 'Select From Form' lookup.
    fromtransid = vw_field_informations.fromtransid e.g. 'epaystra'
    Queries vw_tstructs to get tablename, idfield, captionfield.
    Returns (tablename, id_col, display_col)
    """
    try:
        from sync_service.extractor import get_conn
        conn, meta_schema = get_conn(schema)
        cur = conn.cursor()
        cur.execute(f"""
            SELECT tablename, captionfield, idfield
            FROM {meta_schema}.vw_tstructs
            WHERE lower(transid) = lower(%s)
            LIMIT 1
        """, (fromtransid,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            tablename    = row[0].lower()
            captionfield = row[1] or tablename
            idfield      = row[2] or (tablename + 'id')
            return tablename, idfield, captionfield
        return None, None, None
    except Exception as e:
        print(f"[sql_builder] get_form_table error: {e}")
        return None, None, None
    
# ── Extract FK id col from dropdown SQL ───────────────────────

def extract_id_col(sql: str) -> str:
    if not sql:
        return ''
    m = re.search(r'select\s+([\w.]+)', sql, re.IGNORECASE)
    if m:
        col = m.group(1).strip()
        return col.split('.')[-1].lower()
    return ''


# ── Core SQL builder ──────────────────────────────────────────

def build_report_sql(
    schema:  str,
    transid: str,
    filters: dict = {},
    limit:   int  = 500
) -> dict:
    """
    Build full SELECT SQL for a transid.
    Returns {sql, params, columns, main_table, transid,
             alias_map, join_parts, where_parts, from_clause}
    """
    sql_cache_key = cache.sql_key(schema, transid)

    form = get_form_meta(schema, transid)
    if not form:
        raise ValueError(f"Transid '{transid}' not found in {schema}")

    dcs = form.get('datacontainers', [])
    if not dcs:
        raise ValueError(f"No data containers found for {transid}")

    dcs_sorted    = sorted(dcs, key=lambda d: d.get('name', ''))
    select_cols   = []
    join_parts    = []
    columns       = []
    params        = []
    joined_tables = set()
    alias_map     = {}

    # ── Step 1: Assign aliases ────────────────────────────────
    alias_counter = 1
    for dc in dcs_sorted:
        tname = (dc.get('tablename') or '').lower()
        if not tname or tname in alias_map:
            continue
        alias_map[tname] = f"t{alias_counter}"
        alias_counter += 1

    # ── Step 2: DC-to-DC joins ────────────────────────────────
    parent_tname = dcs_sorted[0].get('tablename', '').lower()
    parent_alias = alias_map.get(parent_tname, 't1')
    fk_col       = parent_tname + "id"
    joined_tables.add(parent_tname)

    for dc in dcs_sorted[1:]:
        tname = (dc.get('tablename') or '').lower()
        if not tname or tname in joined_tables:
            continue
        alias = alias_map.get(tname, f"t{alias_counter}")
        join_parts.append(
            f"LEFT JOIN {schema}.{tname} {alias} "
            f"ON {alias}.{fk_col} = {parent_alias}.{fk_col}"
        )
        joined_tables.add(tname)

    # ── Step 3: Fields + lookup joins ────────────────────────
    lookup_idx = alias_counter

    for dc in dcs_sorted:
        dc_tname = (dc.get('tablename') or '').lower()
        dc_alias = alias_map.get(dc_tname, 't1')

        for field in (dc.get('fields') or []):
            ftype   = classify_field(field)
            fname   = (field.get('name') or '').lower()
            caption = field.get('caption') or fname
            src_tb  = (field.get('source_table') or '').lower()
            src_f   = field.get('sourcefield') or ''
            f_sql   = field.get('sql') or ''

            if ftype in ('hidden', 'calculated', 'fill_skip') or not fname:
                continue

            if ftype == 'fill_join':
                lk_alias = alias_map.get(src_tb)
                if lk_alias and src_f:
                    col_key = f"{src_tb}_{src_f}"
                    select_cols.append(f"{lk_alias}.{src_f} AS {col_key}")
                    columns.append({"key": col_key, "label": caption})
                continue
           
            # ── Form lookup — src_tb exists, sourcefield empty ─
            if ftype == 'form_lookup':
                # Convention: id = src_tb + 'id', display = fname
                # e.g. src_tb=epaystra → JOIN ON epaystraid, display=paycat
                id_col = src_tb + 'id'   # epaystraid
                disp_c = fname           # paycat (same name in source table)

                if src_tb not in joined_tables:
                    lk_alias = f"lk{lookup_idx}"
                    join_parts.append(
                        f"LEFT JOIN {schema}.{src_tb} {lk_alias} "
                        f"ON {lk_alias}.{id_col} = {dc_alias}.{fname}"
                    )
                    joined_tables.add(src_tb)
                    alias_map[src_tb] = lk_alias
                    lookup_idx += 1

                lk_alias = alias_map.get(src_tb)
                if not lk_alias:
                    col_expr = f"{dc_alias}.{fname}"
                    col_key  = fname
                else:
                    col_expr = f"{lk_alias}.{disp_c}"
                    col_key  = f"{src_tb}_{disp_c}"

                select_cols.append(f"{col_expr} AS {col_key}")
                columns.append({"key": col_key, "label": caption})
                continue
          

            if ftype == 'lookup' and src_tb and src_f:
                if src_tb not in joined_tables:
                    lk_alias = f"lk{lookup_idx}"
                    id_col   = extract_id_col(f_sql) or fname
                    join_parts.append(
                        f"LEFT JOIN {schema}.{src_tb} {lk_alias} "
                        f"ON {lk_alias}.{id_col} = {dc_alias}.{fname}"
                    )
                    joined_tables.add(src_tb)
                    alias_map[src_tb] = lk_alias
                    lookup_idx += 1

                lk_alias = alias_map.get(src_tb)
                if not lk_alias:
                    continue
                col_expr = f"{lk_alias}.{src_f}"
                col_key  = f"{src_tb}_{src_f}"
            else:
                col_expr = f"{dc_alias}.{fname}"
                col_key  = fname

            select_cols.append(f"{col_expr} AS {col_key}")
            columns.append({"key": col_key, "label": caption})

    if not select_cols:
        raise ValueError(f"No selectable fields found for {transid}")

    # ── Step 4: FROM ──────────────────────────────────────────
    main_tname  = dcs_sorted[0].get('tablename', '').lower()
    from_clause = f"{schema}.{main_tname} {parent_alias}"

    # ── Step 5: WHERE ─────────────────────────────────────────
    where_parts = []

    for key, val in filters.items():
        if val is None or val == '' or key in ('date_from', 'date_to'):
            continue

        if '__' in key:
            fname, op = key.split('__', 1)
        else:
            fname, op = key, 'equals'

        col = _resolve_filter_col(fname, dcs_sorted, alias_map, schema)
        if not col:
            continue

        if op == 'equals':
            where_parts.append(f"LOWER({col}::text) = LOWER(%s)")
            params.append(str(val))
        elif op == 'not_equals':
            where_parts.append(f"LOWER({col}::text) != LOWER(%s)")
            params.append(str(val))
        elif op == 'contains':
            where_parts.append(f"{col}::text ILIKE %s")
            params.append(f"%{val}%")
        elif op == 'starts_with':
            where_parts.append(f"{col}::text ILIKE %s")
            params.append(f"{val}%")
        elif op == 'ends_with':
            where_parts.append(f"{col}::text ILIKE %s")
            params.append(f"%{val}")
        elif op == 'in':
            items        = [v.strip() for v in str(val).split(',')]
            placeholders = ','.join(['%s'] * len(items))
            where_parts.append(f"{col}::text IN ({placeholders})")
            params.extend(items)
        elif op == 'between':
            parts = str(val).split(',')
            if len(parts) == 2:
                v1, v2 = parts[0].strip(), parts[1].strip()
                cast = '::date' if _is_date_val(v1) else '::numeric'
                where_parts.append(f"{col}{cast} BETWEEN %s{cast} AND %s{cast}")
                params.append(v1)
                params.append(v2)
        elif op == 'gt':
            cast = '::date' if _is_date_val(str(val)) else '::numeric'
            where_parts.append(f"{col}{cast} > %s{cast}")
            params.append(str(val))
        elif op == 'lt':
            cast = '::date' if _is_date_val(str(val)) else '::numeric'
            where_parts.append(f"{col}{cast} < %s{cast}")
            params.append(str(val))
        elif op == 'gte':
            cast = '::date' if _is_date_val(str(val)) else '::numeric'
            where_parts.append(f"{col}{cast} >= %s{cast}")
            params.append(str(val))
        elif op == 'lte':
            cast = '::date' if _is_date_val(str(val)) else '::numeric'
            where_parts.append(f"{col}{cast} <= %s{cast}")
            params.append(str(val))

    if filters.get('date_from'):
        where_parts.append(f"{parent_alias}.modifiedon >= %s")
        params.append(filters['date_from'])

    if filters.get('date_to'):
        where_parts.append(f"{parent_alias}.modifiedon <= %s")
        params.append(filters['date_to'])

    # ── Step 6: Assemble ──────────────────────────────────────
    sql = f"SELECT {', '.join(select_cols)}\nFROM {from_clause}"
    if join_parts:
        sql += "\n" + "\n".join(join_parts)
    if where_parts:
        sql += "\nWHERE " + "\nAND ".join(where_parts)
    sql += f"\nLIMIT {limit}"

    cache.set(sql_cache_key, {"columns": columns, "main_table": main_tname}, ttl=TTL_SQL)

    return {
        "sql":         sql,
        "params":      params,
        "columns":     columns,
        "main_table":  main_tname,
        "transid":     transid,
        "alias_map":   alias_map,
        "join_parts":  join_parts,
        "where_parts": where_parts,
        "from_clause": from_clause,
    }


# ── Filter column resolver ────────────────────────────────────

def _resolve_filter_col(key: str, dcs: list, alias_map: dict, schema: str) -> str:
    for dc in dcs:
        tname = (dc.get('tablename') or '').lower()
        alias = alias_map.get(tname, 't1')
        for field in (dc.get('fields') or []):
            if (field.get('name') or '').lower() == key.lower():
                src_tb = (field.get('source_table') or '').lower()
                src_f  = field.get('sourcefield') or ''
                mode   = (field.get('modeofentry') or '').lower()
                if mode in ('select from sql', 'select from form') and src_tb and src_f:
                    lk_alias = alias_map.get(src_tb)
                    if lk_alias:
                        return f"{lk_alias}.{src_f}"
                return f"{alias}.{key}"
    return None


# ── Helper: strip SELECT keep FROM+JOINs+WHERE ───────────────

def _base_from_sql(built: dict) -> str:
    """Extract FROM...WHERE part — no SELECT cols, no LIMIT."""
    sql = re.sub(r'SELECT .+?\nFROM', 'FROM', built['sql'], flags=re.DOTALL)
    sql = re.sub(r'\nLIMIT \d+', '', sql)
    return sql


# ── Helper: find col key by field name ───────────────────────

def _find_col_by_key(key: str, columns: list) -> str:
    if not key:
        return None
    # Exact match
    for col in columns:
        if col['key'].lower() == key.lower():
            return col['key']
    # Key contains the field name e.g. deptname → dept_department
    for col in columns:
        if key.lower() in col['key'].lower():
            return col['key']
    # Field name contains key e.g. searching 'dept' → finds 'dept_department'
    for col in columns:
        if col['key'].lower().startswith(key.lower()[:4]):
            return col['key']
    return None

def _find_col_expr(col_key: str, sql: str) -> str:
    """
    Extract actual SQL expression for a column alias.
    e.g. 'dept_department' → 'lk4.department'
    from 'lk4.department AS dept_department'
    """
    pattern = rf'([\w.]+)\s+AS\s+{re.escape(col_key)}'
    m = re.search(pattern, sql, re.IGNORECASE)
    if m:
        return m.group(1)
    return col_key

# ── Filterable fields ─────────────────────────────────────────

def get_filterable_fields(schema: str, transid: str) -> dict:
    form       = get_form_meta(schema, transid)
    filter_map = {}

    for dc in (form.get('datacontainers') or []):
        for field in (dc.get('fields') or []):
            mode    = (field.get('modeofentry') or '').lower()
            dtype   = (field.get('datatype')    or '').lower()
            hidden  = bool_val(field.get('hidden')) == 'Yes'
            fname   = field.get('name', '')
            caption = field.get('caption', '')
            lvals   = field.get('listvalues', '') or ''
            src_tb  = (field.get('source_table') or '').lower()
            src_f   = field.get('sourcefield') or ''
            f_sql   = field.get('sql') or ''

            if hidden or not fname:
                continue

            if mode == 'select from list' and lvals:
                filter_map[fname] = {
                    "caption": caption,
                    "values":  [v.strip() for v in lvals.split(',')],
                    "type":    "list"
                }

            elif mode == 'select from sql' and not src_tb and not src_f and f_sql:
                # ── Execute SQL to get values ───────────────── 
                values = _fetch_sql_values(f_sql, schema) 
                if values:
                    filter_map[fname] = {
                        "caption": caption,
                        "values":  values,
                        "type":    "list"  # treat same as list since we have values
                    }

            elif 'date' in dtype:
                filter_map[fname] = {
                    "caption": caption,
                    "values":  [],
                    "type":    "date"
                }

            elif mode in ('select from sql', 'select from form') and src_tb:
                values = _fetch_lookup_values(schema, src_tb, src_f)
                filter_map[fname] = {
                    "caption":     caption,
                    "values":      values,
                    "type":        "lookup" if not values else "list",
                    "sourcefield": src_f,
                    "sourcetable": src_tb
                }

    return filter_map


def _fetch_sql_values(sql: str, schema: str) -> list:
    try:
        # ── Skip only truly unsafe patterns ───────────────────
        skip_patterns = [
            r':\w+',        # bind params like :payrollbasedon
            r'\{.*?\}',     # Axpert templates like {dynamicfilter}
        ]
        for pattern in skip_patterns:
            if re.search(pattern, sql, re.IGNORECASE):
                return []

        # ── Add schema prefix to bare table names only ─────────
        # Handles: FROM tablename, FROM tablename alias, JOIN tablename alias
        cleaned = re.sub(
            r'\b(from|join)\s+(?!' + re.escape(schema) + r'\.)([a-zA-Z_]\w*)',
            lambda m: f"{m.group(1)} {schema}.{m.group(2)}",
            sql,
            flags=re.IGNORECASE
        )

        conn, _ = get_runtime_conn(schema)
        cur     = conn.cursor()
        cur.execute(cleaned)
        rows   = cur.fetchall()
        cur.close()
        conn.close()

        return [str(r[0]) for r in rows if r[0] is not None]

    except Exception as e:
        print(f"[_fetch_sql_values] error: {e} | sql: {sql}")
        return []

def _fetch_lookup_values(schema: str, src_tb: str, src_f: str) -> list:
    """
    Fetch distinct values from a lookup table.
    e.g. schema=hcaspay, src_tb=designation, src_f=designation
    """
    if not src_tb or not src_f:
        return []
    try:
        conn, _ = get_runtime_conn(schema)
        cur     = conn.cursor()
        cur.execute(
            f"SELECT DISTINCT {src_f} FROM {schema}.{src_tb} "
            f"WHERE {src_f} IS NOT NULL ORDER BY {src_f} LIMIT 200"
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [str(r[0]) for r in rows if r[0] is not None]
    except Exception as e:
        print(f"[_fetch_lookup_values] error: {e} src_tb={src_tb} src_f={src_f}")
        return []
# ══════════════════════════════════════════════════════════════
# QUERY RUNNERS
# ══════════════════════════════════════════════════════════════

def _exec(schema: str, sql: str, params: list) -> list:
    """Execute SQL and return raw rows."""
    conn, _ = get_runtime_conn(schema)
    cur     = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


# ── 1. Full report ────────────────────────────────────────────

def run_report_sql(
    schema:      str,
    transid:     str,
    filters:     dict = {},
    limit:       int  = 500,
    force_fresh: bool = False
) -> dict:
    """Full rows with filters. Cached 15min unless force_fresh."""
    report_key = cache.report_key(schema, transid, {**filters, "limit": limit})

    if not force_fresh:
        cached = cache.get(report_key)
        if cached:
            cached['cached'] = True
            return cached

    built   = build_report_sql(schema, transid, filters, limit)
    rows    = _exec(schema, built['sql'], built['params'])
    columns = built['columns']

    result = {
        "columns": columns,
        "rows":    [{col['key']: row[i] for i, col in enumerate(columns)} for row in rows],
        "total":   len(rows),
        "cached":  False,
        "transid": transid
    }

    from shared.cache import TTL_REPORT
    cache.set(report_key, result, ttl=TTL_REPORT)
    return result


# ── 2. Count ──────────────────────────────────────────────────

def run_count_sql(schema: str, transid: str, filters: dict = {}) -> dict:
    """COUNT(*) — always fresh, no cache."""
    built     = build_report_sql(schema, transid, filters, limit=9999999)
    base      = _base_from_sql(built)
    count_sql = f"SELECT COUNT(*)\n{base}"
    print(f"[run_count_sql] SQL:\n{count_sql}")
    print(f"[run_count_sql] params: {built['params']}")
    rows      = _exec(schema, count_sql, built['params'])
    print(f"[run_count_sql] result: {rows}")
    return {"count": rows[0][0] if rows else 0}


# ── 3. Grouped ────────────────────────────────────────────────

def run_grouped_sql(
    schema:      str,
    transid:     str,
    filters:     dict = {},
    group_field: str  = None,
    agg_func:    str  = 'COUNT',
    agg_field:   str  = None
) -> dict:
    """
    GROUP BY group_field with COUNT or SUM/AVG/MIN/MAX.
    e.g. employees by department, total salary by branch.
    """
    built      = build_report_sql(schema, transid, filters, limit=9999999)
    base       = _base_from_sql(built)
    group_col  = _find_col_by_key(group_field, built['columns'])

    if not group_col:
            # Fallback — pick first lookup column (has underscore = joined table)
            for col in built['columns']:
                k = col['key'].lower()
                if '_' in k and not any(k.endswith(s) for s in ['id','aid','bid','mid','sid']):
                    group_col = col['key']
                    print(f"[sql_builder] group_field fallback → {group_col}")
                    break
            if not group_col:
                raise ValueError(f"Group field '{group_field}' not found in {transid}")

    if agg_func.upper() in ('SUM', 'AVG', 'MIN', 'MAX') and agg_field:
        agg_col  = _find_col_by_key(agg_field, built['columns'])
        agg_expr = f"{agg_func.upper()}({agg_col}::numeric)"
        val_label = f"{agg_func.title()} {agg_field}"
    else:
        agg_expr  = "COUNT(*)"
        val_label = "Count"

    group_expr = _find_col_expr(group_col, built['sql'])

    sql = f"""
        SELECT {group_expr} AS group_label,
               {agg_expr}  AS agg_value
        {base}
        GROUP BY {group_expr}
        ORDER BY agg_value DESC
    """

    rows = _exec(schema, sql, built['params'])
    return {
        "columns": [
            {"key": "group_label", "label": (group_field or "Group").replace('_',' ').title()},
            {"key": "agg_value",   "label": val_label}
        ],
        "rows":  [{"group_label": r[0], "agg_value": r[1]} for r in rows],
        "total": len(rows),
        "chart": {
            "type": "bar", "x": "group_label", "xLabel": group_field or "Group",
            "y": "agg_value", "yLabel": val_label
        }
    }


# ── 4. Aggregated summary ─────────────────────────────────────

def run_aggregated_sql(
    schema:      str,
    transid:     str,
    filters:     dict = {},
    group_field: str  = None,
    agg_fields:  list = []
) -> dict:
    """
    Multi-aggregate per group: COUNT + SUM + AVG + MIN + MAX.
    e.g. salary summary by department.
    """
    built     = build_report_sql(schema, transid, filters, limit=9999999)
    base      = _base_from_sql(built)
    group_col = _find_col_by_key(group_field, built['columns'])

    if not group_col:
        raise ValueError(f"Group field '{group_field}' not found")

    agg_exprs = ["COUNT(*) AS total_count"]
    out_cols  = [
        {"key": "group_label", "label": (group_field or "Group").title()},
        {"key": "total_count", "label": "Count"}
    ]

    for af in agg_fields:
        ac = _find_col_by_key(af, built['columns'])
        if not ac:
            continue
        label = af.replace('_', ' ').title()
        agg_exprs += [
            f"SUM({ac}::numeric) AS {af}_sum",
            f"ROUND(AVG({ac}::numeric),2) AS {af}_avg",
            f"MAX({ac}::numeric) AS {af}_max",
            f"MIN({ac}::numeric) AS {af}_min",
        ]
        out_cols += [
            {"key": f"{af}_sum", "label": f"Total {label}"},
            {"key": f"{af}_avg", "label": f"Avg {label}"},
            {"key": f"{af}_max", "label": f"Max {label}"},
            {"key": f"{af}_min", "label": f"Min {label}"},
        ]

    group_expr = _find_col_expr(group_col, built['sql'])

    sql = f"""
        SELECT {group_expr} AS group_label,
               {', '.join(agg_exprs)}
        {base}
        GROUP BY {group_expr}
        ORDER BY total_count DESC
    """

    rows = _exec(schema, sql, built['params'])
    keys = ['group_label', 'total_count'] + [
        f"{af}{s}" for af in agg_fields for s in ['_sum','_avg','_max','_min']
    ]

    return {
        "columns": out_cols,
        "rows":    [{keys[i]: row[i] for i in range(len(keys))} for row in rows],
        "total":   len(rows),
        "chart": {
            "type": "bar", "x": "group_label", "xLabel": group_field or "Group",
            "y": "total_count", "yLabel": "Count"
        }
    }


# ── 5. Ranked TOP N ───────────────────────────────────────────

def run_ranked_sql(
    schema:      str,
    transid:     str,
    filters:     dict = {},
    rank_field:  str  = None,
    top_n:       int  = 10,
    order:       str  = 'DESC',
    group_field: str  = None
) -> dict:
    """
    TOP N rows ranked by rank_field.
    Optionally PARTITION BY group_field for rank within group.
    e.g. top 10 highest paid, top 5 per department.
    """
    built    = build_report_sql(schema, transid, filters, limit=9999999)
    base     = _base_from_sql(built)
    rank_col = _find_col_by_key(rank_field, built['columns'])

    if not rank_col:
        # Fallback — pick first numeric column
        for col in built['columns']:
            k = col['key'].lower()
            if not any(k.endswith(s) for s in ['id','aid','bid','mid','sid']):
                # Check if it looks numeric
                if any(kw in k for kw in ['amount','salary','basic','pay',
                                           'gross','net','total','tctc','ctc','amt','cost','price']):
                    rank_col = col['key']
                    print(f"[sql_builder] rank_field fallback → {rank_col}")
                    break
    if not rank_col:
        raise ValueError(f"Rank field '{rank_field}' not found — available: {[c['key'] for c in built['columns']]}")

    if group_field:
        gc           = _find_col_by_key(group_field, built['columns'])
        partition_by = f"PARTITION BY {gc}" if gc else ""
    else:
        partition_by = ""

    select_list = _extract_select_expressions(built['sql'])

    sql = f"""
        SELECT * FROM (
            SELECT {select_list},
                   RANK() OVER ({partition_by} ORDER BY {rank_col}::numeric {order.upper()}) AS rank
            {base}
        ) ranked
        WHERE rank <= {top_n}
        ORDER BY rank
    """

    rows    = _exec(schema, sql, built['params'])
    columns = built['columns'] + [{"key": "rank", "label": "Rank"}]
    keys    = [c['key'] for c in columns]

    return {
        "columns": columns,
        "rows":    [{keys[i]: row[i] for i in range(len(keys))} for row in rows],
        "total":   len(rows),
        "chart":   {}
    }


# ── 6. Period trend ───────────────────────────────────────────

def run_period_sql(
    schema:     str,
    transid:    str,
    filters:    dict = {},
    date_field: str  = None,
    period:     str  = 'month',
    agg_field:  str  = None,
    agg_func:   str  = 'COUNT'
) -> dict:
    """
    Trend over time — GROUP BY month or year.
    e.g. monthly payroll total, employee joins per month.
    """
    built    = build_report_sql(schema, transid, filters, limit=9999999)
    base     = _base_from_sql(built)
    date_col = _find_col_by_key(date_field, built['columns']) if date_field else None

    if not date_col:
        for col in built['columns']:
            if any(kw in col['key'].lower() for kw in ['date', 'doj', 'dob', 'month']):
                date_col = col['key']
                break

    if not date_col:
        raise ValueError("No date field found for period grouping")

    # ✅ Always resolve alias → real SQL expression (e.g. 'doj' → 't1.doj')
    date_expr = _find_col_expr(date_col, built['sql'])

    # ✅ Build trunc_expr using date_expr (real column), not date_col (alias)
    if period == 'year':
        trunc_expr   = f"EXTRACT(YEAR FROM {date_expr}::date)::text"
        period_label = "Year"
    else:
        trunc_expr   = f"TO_CHAR({date_expr}::date, 'YYYY-MM')"
        period_label = "Month"

    # ✅ Aggregation
    if agg_func.upper() in ('SUM', 'AVG') and agg_field:
        agg_col   = _find_col_by_key(agg_field, built['columns'])
        agg_expr  = _find_col_expr(agg_col, built['sql'])
        agg_expr  = f"{agg_func.upper()}({agg_expr}::numeric)"
        val_label = f"{agg_func} {agg_field}"
    else:
        agg_expr  = "COUNT(*)"
        val_label = "Count"

    has_where = 'WHERE' in base.upper()
    connector = "AND" if has_where else "WHERE"

    sql = f"""
        SELECT {trunc_expr} AS period,
               {agg_expr} AS value
        {base}
        {connector} {date_expr} IS NOT NULL
        GROUP BY period
        ORDER BY period
    """

    rows = _exec(schema, sql, built['params'])
    return {
        "columns": [
            {"key": "period", "label": period_label},
            {"key": "value",  "label": val_label}
        ],
        "rows":  [{"period": r[0], "value": r[1]} for r in rows],
        "total": len(rows),
        "chart": {
            "type": "line", "x": "period", "xLabel": period_label,
            "y": "value",   "yLabel": val_label
        }
    }

# ── 7. Analytical (window functions) ─────────────────────────

def run_analytical_sql(
    schema:          str,
    transid:         str,
    filters:         dict = {},
    measure_field:   str  = None,
    partition_field: str  = None
) -> dict:
    """
    Each row + window stats: group avg, % of total, rank in group.
    e.g. each employee salary vs department average.
    """
    built       = build_report_sql(schema, transid, filters, limit=500)
    base        = _base_from_sql(built)
    measure_col = _find_col_by_key(measure_field, built['columns'])

    if not measure_col:
        raise ValueError(f"Measure field '{measure_field}' not found")

    if partition_field:
        pc           = _find_col_by_key(partition_field, built['columns'])
        partition_by = f"PARTITION BY {pc}" if pc else ""
    else:
        partition_by = ""

    select_list = _extract_select_expressions(built['sql'])

    measure_expr   = _find_col_expr(measure_col, built['sql'])
    partition_expr = _find_col_expr(partition_by.replace('PARTITION BY ',''), built['sql']) if partition_by else ''
    partition_by   = f"PARTITION BY {partition_expr}" if partition_expr else ""

    sql = f"""
        SELECT {select_list},
               ROUND(AVG({measure_expr}::numeric) OVER ({partition_by}), 2) AS group_avg,
               ROUND(
                   {measure_expr}::numeric /
                   NULLIF(SUM({measure_expr}::numeric) OVER (), 0) * 100,
               2) AS pct_of_total,
               RANK() OVER ({partition_by} ORDER BY {measure_expr}::numeric DESC) AS rank_in_group
        {base}
        LIMIT 500
    """

    rows    = _exec(schema, sql, built['params'])
    columns = built['columns'] + [
        {"key": "group_avg",     "label": "Group Avg"},
        {"key": "pct_of_total",  "label": "% of Total"},
        {"key": "rank_in_group", "label": "Rank in Group"},
    ]
    keys = [c['key'] for c in columns]

    return {
        "columns": columns,
        "rows":    [{keys[i]: row[i] for i in range(len(keys))} for row in rows],
        "total":   len(rows),
        "chart":   {}
    }


# ── 8. Tree / hierarchy ───────────────────────────────────────

def run_tree_sql(
    schema:       str,
    transid:      str,
    filters:      dict = {},
    parent_field: str  = None,
    id_field:     str  = None,
    label_field:  str  = None,
    max_depth:    int  = 6
) -> dict:
    """
    Recursive CTE for org chart / hierarchy.
    Works on self-referencing tables (reportingto, parentid etc.)
    """
    built      = build_report_sql(schema, transid, filters, limit=9999999)
    main_table = f"{schema}.{built['main_table']}"
    col_keys   = [c['key'] for c in built['columns']]

    # Auto-detect fields
    if not id_field:
        for k in col_keys:
            if k.endswith('id') and built['main_table'] in k:
                id_field = k
                break
        if not id_field:
            id_field = built['main_table'] + 'id'

    if not parent_field:
        for k in col_keys:
            if any(kw in k.lower() for kw in ['reporting','parent','manager','reportsto']):
                parent_field = k
                break

    if not label_field:
        for k in col_keys:
            if any(kw in k.lower() for kw in ['name','firstname','caption','dept','title']):
                label_field = k
                break

    if not parent_field or not label_field:
        raise ValueError("Could not detect parent/label fields for tree query")

    where_clause = ""
    if built.get('where_parts'):
        where_clause = "AND " + " AND ".join(built['where_parts'])

    sql = f"""
        WITH RECURSIVE tree AS (
            SELECT {id_field},
                   {label_field}  AS label,
                   {parent_field} AS parent_id,
                   0              AS depth,
                   {label_field}::text AS path
            FROM {main_table}
            WHERE ({parent_field} IS NULL
                   OR {parent_field}::text = ''
                   OR {parent_field}::text = '0')
            {where_clause}

            UNION ALL

            SELECT c.{id_field},
                   c.{label_field},
                   c.{parent_field},
                   t.depth + 1,
                   t.path || ' > ' || c.{label_field}::text
            FROM {main_table} c
            JOIN tree t ON t.{id_field}::text = c.{parent_field}::text
            WHERE t.depth < {max_depth}
        )
        SELECT {id_field}, label, parent_id, depth, path
        FROM tree
        ORDER BY path
    """

    rows = _exec(schema, sql, built['params'])

    return {
        "columns": [
            {"key": id_field,    "label": "ID"},
            {"key": "label",     "label": (label_field or "Name").replace('_',' ').title()},
            {"key": "parent_id", "label": "Parent"},
            {"key": "depth",     "label": "Level"},
            {"key": "path",      "label": "Full Path"},
        ],
        "rows": [
            {id_field: r[0], "label": r[1], "parent_id": r[2], "depth": r[3], "path": r[4]}
            for r in rows
        ],
        "total": len(rows),
        "chart": {},
        "tree":  True
    }

def _extract_select_expressions(sql: str) -> str:
    """
    Extract full SELECT expression list from built SQL.
    e.g. 'SELECT t1.paycat AS paycat, lk2.branchname AS cm_branch_hdr_branchname...'
    Returns the full expression string between SELECT and FROM.
    """
    m = re.search(r'SELECT\s+(.+?)\s*\nFROM', sql, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1)
    return '*'