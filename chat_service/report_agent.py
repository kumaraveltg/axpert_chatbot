"""
=============================================================
chat_service/report_agent.py
=============================================================
"""
import os, json
from groq import Groq
from sync_service.extractor import get_all_transids
from chat_service.sql_builder import (
    run_report_sql, run_count_sql, run_grouped_sql,
    run_aggregated_sql, run_ranked_sql, run_period_sql,
    run_analytical_sql, run_tree_sql, get_filterable_fields,
    build_report_sql, _find_col_by_key
)
from chat_service.report_formatter import format_report
from shared.cache import cache, TTL_MODULES
from dotenv import load_dotenv
load_dotenv()

_groq = Groq(api_key=os.getenv("GROQ_API_KEY"))


# ══════════════════════════════════════════════════════════════
# INTENT DETECTION
# ══════════════════════════════════════════════════════════════

def detect_query_type(question: str) -> str:
    """Use LLM to detect query type — no hardcoded keywords needed."""
    prompt = f"""Classify this HR/Payroll question into exactly one report type.

Report types:
- period     : trends over time (by month, by year, joining trend, monthly salary)
- grouped    : breakdown by a category (by department, by branch, by gender, by designation)
- ranked     : top N, highest, lowest, best, worst performers
- count      : simple total count only (how many, total number of)
- aggregated : salary summary, averages, min/max across groups
- analytical : each row compared to group average, percentage of total
- tree       : org hierarchy, reporting structure, who reports to whom
- report     : general list, show all employees, full details

Question: "{question}"

Reply with ONLY one word."""

    try:
        resp = _llm(prompt, max_tokens=10)
        detected = resp.strip().lower().split()[0]
        valid = {'period','grouped','ranked','count','aggregated','analytical','tree','report'}
        if detected in valid:
            print(f"[report_agent] query_type={detected} (LLM)")
            return detected
    except Exception as e:
        print(f"[report_agent] detect_query_type LLM error: {e}")

    # Last resort fallback only
    return 'report'


def extract_query_params(question: str, schema: str, transid: str, query_type: str,columns: list = []) -> dict:
    """
    Use LLM to extract parameters for the detected query type.
    Returns dict with relevant fields (group_field, rank_field, top_n etc.)
    """
    filter_map  = get_filterable_fields(schema, transid)
    fields_text = "\n".join([f"  {k}: {v['caption']}" for k, v in filter_map.items()])

    if query_type == 'grouped':
        col_text = "\n".join([f"  {c['key']}: {c['label']}" for c in columns]) if columns else fields_text
        prompt = (
            f"Available fields:\n{col_text}\n\n"
            f"Question: {question}\n\n"
            f"Which field should we GROUP BY? Return ONLY the field name."
        )
        resp = _llm(prompt, max_tokens=10)
        return {"group_field": resp}

    elif query_type == 'aggregated':
        col_text = "\n".join([f"  {c['key']}: {c['label']}" for c in columns]) if columns else fields_text
        prompt = (
            f"Available fields:\n{col_text}\n\n"
            f"Question: {question}\n\n"
            f"Return JSON with:\n"
            f"  group_field: field to group by\n"
            f"  agg_fields: list of numeric fields to summarize\n"
            f"Example: {{\"group_field\": \"deptname\", \"agg_fields\": [\"basic\", \"grosspay\"]}}\n"
            f"Return ONLY valid JSON."
        )
        resp = _llm(prompt, max_tokens=60)
        try:
            return json.loads(resp.replace("```json","").replace("```","").strip())
        except:
            return {"group_field": None, "agg_fields": []}

    elif query_type == 'ranked':
        col_text = "\n".join([f"  {c['key']}: {c['label']}" for c in columns]) if columns else fields_text
        prompt = (
            f"Available fields:\n{col_text}\n\n"
            f"Question: {question}\n\n"
            f"Return JSON with:\n"
            f"  rank_field: numeric field to rank by\n"
            f"  top_n: number (default 10)\n"
            f"  order: DESC or ASC\n"
            f"  group_field: field to partition by (or null)\n"
            f"Example: {{\"rank_field\": \"basic\", \"top_n\": 10, \"order\": \"DESC\", \"group_field\": null}}\n"
            f"Return ONLY valid JSON."
        )
        resp = _llm(prompt, max_tokens=60)
        try:
            return json.loads(resp.replace("```json","").replace("```","").strip())
        except:
            return {"rank_field": None, "top_n": 10, "order": "DESC", "group_field": None}

    elif query_type == 'period':
        col_text = "\n".join([f"  {c['key']}: {c['label']}" for c in columns]) if columns else fields_text

        # ── Extract only date fields to guide the LLM ──
        date_cols = [c for c in columns if any(
            kw in c['key'].lower() or kw in c['label'].lower()
            for kw in ['date', 'doj', 'dob', 'on', 'month', 'year', 'time']
        )] if columns else []
        date_hint = "\n".join([f"  {c['key']}: {c['label']}" for c in date_cols])

        prompt = (
            f"Available date fields:\n{date_hint}\n\n"
            f"All fields:\n{col_text}\n\n"
            f"Question: {question}\n\n"
            f"Rules:\n"
            f"  - 'joined', 'joining', 'join date', 'doj' → date_field = 'doj'\n"
            f"  - 'birth', 'birthday', 'dob' → date_field = 'dob'\n"
            f"  - Always pick from the Available date fields list above\n"
            f"  - Default to 'doj' for employee trend questions if unclear\n\n"
            f"Return JSON with:\n"
            f"  date_field: date field key to trend by\n"
            f"  period: month or year\n"
            f"  agg_field: numeric field to aggregate (or null for count)\n"
            f"  agg_func: COUNT, SUM, or AVG\n"
            f"Example: {{\"date_field\": \"doj\", \"period\": \"month\", \"agg_field\": null, \"agg_func\": \"COUNT\"}}\n"
            f"Return ONLY valid JSON."
        )
        resp = _llm(prompt, max_tokens=60)
        try:
            result = json.loads(resp.replace("```json", "").replace("```", "").strip())
        except:
            result = {"date_field": None, "period": "month", "agg_field": None, "agg_func": "COUNT"}

        # ── Fallback: infer date_field from question keywords if LLM returned None ──
        if not result.get('date_field'):
            q = question.lower()
            if any(kw in q for kw in ['doj', 'joined', 'joining', 'join date']):
                result['date_field'] = 'doj'
            elif any(kw in q for kw in ['dob', 'birth', 'birthday']):
                result['date_field'] = 'dob'
            else:
                result['date_field'] = 'doj'  # default for employee queries
            print(f"[report_agent] date_field fallback inferred: {result['date_field']}")

        return result

    elif query_type == 'analytical':
        col_text = "\n".join([f"  {c['key']}: {c['label']}" for c in columns]) if columns else fields_text
        prompt = (
            f"Available fields:\n{col_text}\n\n"
            f"Question: {question}\n\n"
            f"Return JSON with:\n"
            f"  measure_field: numeric field to analyze\n"
            f"  partition_field: field to partition/group by (or null)\n"
            f"Example: {{\"measure_field\": \"basic\", \"partition_field\": \"deptname\"}}\n"
            f"Return ONLY valid JSON."
        )
        resp = _llm(prompt, max_tokens=40)
        try:
            return json.loads(resp.replace("```json","").replace("```","").strip())
        except:
            return {"measure_field": None, "partition_field": None}

    elif query_type == 'tree':
        prompt = (
            f"Available fields:\n{fields_text}\n\n"
            f"Question: {question}\n\n"
            f"Return JSON with:\n"
            f"  parent_field: field pointing to parent record\n"
            f"  label_field: field to display as node label\n"
            f"Example: {{\"parent_field\": \"reportingto\", \"label_field\": \"firstname\"}}\n"
            f"Return ONLY valid JSON."
        )
        resp = _llm(prompt, max_tokens=40)
        try:
            return json.loads(resp.replace("```json","").replace("```","").strip())
        except:
            return {"parent_field": None, "label_field": None}

    return {}


def _llm(prompt: str, max_tokens: int = 20) -> str:
    """Simple LLM call."""
    resp = _groq.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0
    )
    return resp.choices[0].message.content.strip()


# ══════════════════════════════════════════════════════════════
# TRANSID IDENTIFICATION
# ══════════════════════════════════════════════════════════════

def identify_transid(question: str, schema: str) -> str:
    key    = cache.modules_key(schema)
    cached = cache.get(key)

    if cached:
        transids = cached
    else:
        transids = get_all_transids(schema)
        cache.set(key, transids, ttl=TTL_MODULES)

    if not transids:
        return None

    tid_text = "\n".join([
        f"{t['transid']} = {t['caption']}"
        for t in transids
    ])

    prompt = (
        f"From this list of ERP forms, pick the ONE most relevant transid.\n"
        f"Return ONLY the transid code, nothing else.\n"
        f"Examples: 'employee' → empmu, 'payroll' → epays\n\n"
        f"Forms:\n{tid_text}\n\n"
        f"Question: {question}"
    )

    try:
        tid   = _llm(prompt, max_tokens=10).lower().strip()
        valid = [t['transid'].lower() for t in transids]

        # Exact match first
        if tid in valid:
            return tid

        # Fuzzy fallback — find closest match
        from difflib import get_close_matches
        matches = get_close_matches(tid, valid, n=1, cutoff=0.8)
        if matches:
            print(f"[identify_transid] fuzzy match: {tid} → {matches[0]}")
            return matches[0]

        print(f"[identify_transid] no match found for: {tid}")
        return None

    except Exception as e:
        print(f"[identify_transid] error: {e}")
        return None

# ══════════════════════════════════════════════════════════════
# FILTER EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_filters(question: str, schema: str, transid: str) -> dict:
    filter_map = get_filterable_fields(schema, transid)
    if not filter_map:
        return {}

    fields_text = "\n".join([
        f"  {fname} ({info['caption']}): "
        f"{', '.join(info['values']) if info['values'] else info['type']}"
        for fname, info in filter_map.items()
    ])

    prompt = (
        f"Filterable fields with EXACT allowed values:\n"
        f"{fields_text}\n\n"
        f"Extract filters from question as JSON.\n"
        f"Use ONLY field names and values shown above.\n"
        f"Return ONLY valid JSON. If no filters: {{}}\n\n"
        f"Question: {question}"
    )

    try:
        raw = _llm(prompt, max_tokens=100)
        raw = raw.replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except:
        return {}


# ══════════════════════════════════════════════════════════════
# MAIN HANDLER
# ══════════════════════════════════════════════════════════════

async def handle_report(
    question:     str,
    schema:       str,
    client_id:    str,
    filters:      dict = {},
    force_detail: bool = False
) -> dict:
    """
    Full report generation flow.
    Detects query type, identifies transid, extracts filters, runs SQL.
    """
    print(f"[handle_report] ENTRY force_detail={force_detail} filters={filters}")

    def _progress(msg: str):
        cache.publish_report_event(client_id, "progress", {"message": msg})

    try:
        # Step 1 — identify transid
        _progress("Identifying relevant form...")
        transid = identify_transid(question, schema)
        print(f"[debug] identified transid={transid}")  # ← ADD
        if not transid:
            return {"type": "error", "message": "Could not identify relevant data form."}

        _progress(f"Found form: {transid.upper()} — reading metadata...")

        # Step 2 — extract filters from question (if not passed in)
        _progress("Extracting filters...")
        if not filters:
            filters = extract_filters(question, schema, transid)
        print(f"[debug] extracted filters={filters}")   # ← ADD    

        # Step 3 — detect query type
        query_type = detect_query_type(question)
        print(f"[report_agent] query_type={query_type} force_detail={force_detail} question={question}")

        # force_detail overrides count → return full rows
        if force_detail and query_type == 'count':
            query_type = 'report'

        # Invalidate cache when loading fresh detail
        if force_detail:
            cache.invalidate_reports(schema)

        # Step 4 — extract query params (group_field, rank_field etc.)
        q_params = {}
        if query_type not in ('count', 'report'):
            _progress(f"Analysing {query_type} query parameters...")
            built_meta = build_report_sql(schema, transid, {}, limit=1)
            meta_cols  = built_meta['columns']
            q_params   = extract_query_params(question, schema, transid, query_type, meta_cols)
            built = build_report_sql(schema, transid, {}, limit=1)
            print(f"[debug] available columns: {[c['key'] for c in built['columns']]}")
            print(f"[report_agent] q_params={q_params}")

            # Resolve group_field/rank_field to actual column key in columns list
            # e.g. LLM returns 'deptname' but actual col key is 'dept_department'
            for param_key in ('group_field', 'rank_field', 'measure_field',
                            'partition_field', 'date_field', 'agg_field'):
                raw = q_params.get(param_key)
                if raw:
                    built    = build_report_sql(schema, transid, {}, limit=1)
                    resolved = _find_col_by_key(raw, built['columns'])
                    if resolved:
                        q_params[param_key] = resolved
                        print(f"[report_agent] resolved {param_key}: {raw} → {resolved}")
                    else:
                        print(f"[report_agent] WARNING: could not resolve {param_key}: {raw}")

        # Step 5 — run correct query
        _progress("Querying data...")

        if query_type == 'count':
            count_result = run_count_sql(schema, transid, filters)
            return {
                "type":        "count",
                "title":       question.capitalize(),
                "summary":     [{"label": "Total Count", "value": count_result['count']}],
                "transid":     transid,
                "filters":     filters,
                "chart":       {},
                "columns":     [],
                "rows":        [],
                "total":       count_result['count'],
                "filter_meta": get_filterable_fields(schema, transid)
            }

        elif query_type == 'grouped':
            result = run_grouped_sql(
                schema, transid, filters,
                group_field = q_params.get('group_field'),
                agg_func    = q_params.get('agg_func', 'COUNT'),
                agg_field   = q_params.get('agg_field')
            )

        elif query_type == 'aggregated':
            result = run_aggregated_sql(
                schema, transid, filters,
                group_field = q_params.get('group_field'),
                agg_fields  = q_params.get('agg_fields', [])
            )

        elif query_type == 'ranked':
            result = run_ranked_sql(
                schema, transid, filters,
                rank_field  = q_params.get('rank_field'),
                top_n       = int(q_params.get('top_n', 10)),
                order       = q_params.get('order', 'DESC'),
                group_field = q_params.get('group_field')
            )

        elif query_type == 'period':
            result = run_period_sql(
                schema, transid, filters,
                date_field = q_params.get('date_field'),
                period     = q_params.get('period', 'month'),
                agg_field  = q_params.get('agg_field'),
                agg_func   = q_params.get('agg_func', 'COUNT')
            )

        elif query_type == 'analytical':
            result = run_analytical_sql(
                schema, transid, filters,
                measure_field   = q_params.get('measure_field'),
                partition_field = q_params.get('partition_field')
            )

        elif query_type == 'tree':
            result = run_tree_sql(
                schema, transid, filters,
                parent_field = q_params.get('parent_field'),
                label_field  = q_params.get('label_field')
            )

        else:
            # Default full report
            result = run_report_sql(
                schema, transid, filters,
                limit       = 500,
                force_fresh = force_detail
            )

        if not result.get('rows'):
            return {
                "type":    "empty",
                "message": f'No data found for your query.',
                "transid": transid,
                "filters": filters
            }

        # Step 6 — format for frontend
        _progress(f"Formatting {result.get('total', 0)} records...")

        # Attach chart from result if available
        chart = result.pop('chart', None)

        formatted = format_report(result, question)
        formatted['filter_meta'] = get_filterable_fields(schema, transid)

        if chart:
            formatted['chart'] = chart   # use query-specific chart config

        cache.publish_report_event(client_id, "report_done", {
            "transid": transid,
            "total":   result.get('total', 0)
        })

        return formatted

    except Exception as e:
        print(f"[report_agent] Error: {e}")
        import traceback
        traceback.print_exc()
        cache.publish_report_event(client_id, "error", {"message": str(e)})
        return {"type": "error", "message": str(e)}
