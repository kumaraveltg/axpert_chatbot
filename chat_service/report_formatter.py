"""
=============================================================
chat_service/report_formatter.py
=============================================================
"""

def format_report(result: dict, question: str = "") -> dict:
    """
    Shape SQL result for frontend rendering.

    Returns:
    {
      type:     "report",
      title:    str,
      columns:  [{key, label}],
      rows:     [{...}],
      total:    int,
      summary:  [{label, value}],   ← KPI cards
      chart:    {type, x, y},       ← chart config
      cached:   bool
    }
    """
    columns = result.get('columns', [])
    rows    = result.get('rows', [])
    total   = result.get('total', 0)

   # ── KPI summary cards ─────────────────────────────────────
    is_count = any(kw in question.lower() for kw in ['how many', 'count', 'total number'])

    if is_count:
        summary = [{"label": "Total Count", "value": total}]
    else:
        summary = [{"label": "Total Records", "value": total}]
        numeric_cols = _find_numeric_cols(rows, columns)
        for col in numeric_cols[:3]:
            vals = [float(r[col['key']]) for r in rows if r.get(col['key']) is not None]
            if vals:
                summary.append({"label": f"Total {col['label']}", "value": round(sum(vals), 2)})

    # ── Chart config ──────────────────────────────────────────
    is_count = any(kw in question.lower() for kw in ['how many', 'count', 'total number'])
    chart = {} if is_count else _suggest_chart(columns, rows, question)

    # ── Title from question ───────────────────────────────────
    title = _make_title(question, result.get('transid', ''))

    return {
        "type":    "report",
        "title":   title,
        "columns": columns,
        "rows":    rows,
        "total":   total,
        "summary": summary,
        "chart":   chart,
        "cached":  result.get('cached', False),
        "transid": result.get('transid', '')
    }


def _find_numeric_cols(rows: list, columns: list) -> list:
    """Find columns with numeric values."""
    if not rows:
        return []
    numeric = []
    for col in columns:
        k = col['key']
        vals = [r.get(k) for r in rows[:10] if r.get(k) is not None]
        if vals and all(_is_numeric(v) for v in vals):
            numeric.append(col)
    return numeric


def _is_numeric(val) -> bool:
    try:
        float(val)
        return True
    except:
        return False


def _suggest_chart(columns: list, rows: list, question: str) -> dict:
    """
    Auto-suggest chart type based on columns + question.
    Returns chart config for recharts.
    """
    q = question.lower()
    col_keys = [c['key'] for c in columns]

    # Find best x-axis (first text col) and y-axis (first numeric col)
    x_col = None
    y_col = None

    if not rows:
        return {}

    for col in columns:
        k    = col['key']
        vals = [r.get(k) for r in rows[:5] if r.get(k) is not None]
        if not vals:
            continue
        if not x_col and not _is_numeric(vals[0]):
            x_col = col
        if not y_col and _is_numeric(vals[0]):
            y_col = col

    if not x_col or not y_col:
        return {}

    # Choose chart type from question keywords
    if any(w in q for w in ['trend', 'over time', 'monthly', 'yearly']):
        chart_type = 'line'
    elif any(w in q for w in ['breakdown', 'distribution', 'percentage', 'share']):
        chart_type = 'pie'
    else:
        chart_type = 'bar'

    return {
        "type":  chart_type,
        "x":     x_col['key'],
        "xLabel": x_col['label'],
        "y":     y_col['key'],
        "yLabel": y_col['label']
    }

def _make_title(question: str, transid: str) -> str:
    """Generate report title from question."""
    q = question.strip()
    if len(q) < 60:
        return q.capitalize()
    return f"Report: {transid.upper()}"

SKIP_FIELD_PATTERNS = [
    'id', 'aid', 'bid', 'mid', 'sid',
    'code', 'recid', 'rowid', 'sno',
    'dob', 'date', 'freq', 'contactno', 'contact',    
    'phone', 'mobile', 'pincode', 'zip', 'no_of'     
]

def _find_numeric_cols(rows: list, columns: list) -> list:
    """Find numeric columns — skip ID/system fields."""
    if not rows:
        return []
    numeric = []
    for col in columns:
        k   = col['key']
        lbl = col['key'].lower()

        # Skip ID fields
        if any(lbl.endswith(p) for p in SKIP_FIELD_PATTERNS):
            continue
        if lbl in ('sno', 'recid', 'rowid', 'exeord'):
            continue

        vals = [r.get(k) for r in rows[:10] if r.get(k) is not None]
        if vals and all(_is_numeric(v) for v in vals):
            numeric.append(col)
    return numeric

def _suggest_chart(columns, rows, question):
    q = question.lower()
    
    # Find x (text) and y (numeric, non-ID)
    numeric = _find_numeric_cols(rows, columns)
    text_cols = [
        c for c in columns
        if not any(c['key'].lower().endswith(p) 
                   for p in SKIP_FIELD_PATTERNS)
        and not _is_numeric((rows[0].get(c['key']) or ''))
    ]
    
    if not text_cols or not numeric:
        return {}

    x_col = text_cols[0]
    y_col = numeric[0]

    # Only use pie for explicit % / share questions
    if any(w in q for w in ['percentage', 'share', 'proportion', 'pie']):
        chart_type = 'pie'
    elif any(w in q for w in ['trend', 'over time', 'monthly', 'yearly', 'by month']):
        chart_type = 'line'
    else:
        chart_type = 'bar'   # ← default always bar

    return {
        "type": chart_type,
        "x": x_col['key'], "xLabel": x_col['label'],
        "y": y_col['key'], "yLabel": y_col['label']
    }