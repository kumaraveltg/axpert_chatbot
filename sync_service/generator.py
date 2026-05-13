import os
import psycopg2
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

client = Groq(
    api_key=os.getenv("GROQ_API_KEY")
)


def get_db_conn():
    """Reusable DB connection"""
    return psycopg2.connect(
        host     = os.getenv("DB_HOST"),
        port     = os.getenv("DB_PORT"),
        database = os.getenv("DB_NAME"),
        user     = os.getenv("DB_USER"),
        password = os.getenv("DB_PASS")
    )


def get_field_instructions(schema: str, transid: str) -> dict:
    """
    Fetch admin-added instructions for fields.
    Returns dict: {fieldname: instruction}
    """
    try:
        conn = get_db_conn()
        cur  = conn.cursor()
        cur.execute("""
            SELECT fieldname, instruction
            FROM axpert_chatbot.field_instructions
            WHERE schema_name = %s
            AND lower(transid) = lower(%s)
        """, (schema, transid))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row[0].lower(): row[1] for row in rows}
    except Exception as e:
        print(f"Warning: Could not fetch field instructions: {e}")
        return {}


# ✅ NEW — Auto-generate instructions for fields missing them
def auto_generate_field_instructions(
    schema:       str,
    transid:      str,
    form_caption: str,
    module:       str,
    fields:       list,
    existing:     dict
) -> dict:
    """
    For fields with no admin instruction,
    auto-generate from field metadata using Groq.
    Saves to field_instructions table with created_by='auto'.
    Admin instructions always take priority.
    """

    # Only process visible fields with no existing instruction
    missing = [
        f for f in fields
        if f.get('name')
        and f.get('name', '').lower() not in existing
        and f.get('caption')
        and bool_val(f.get('hidden')) == 'No'
    ]

    if not missing:
        print(f"✅ All fields have instructions: {transid}")
        return existing

    # Batch — max 15 fields per Groq call to save tokens
    batches = [missing[i:i+15] for i in range(0, len(missing), 15)]
    new_instructions = {}

    for batch in batches:
        fields_text = "\n".join([
            f"- {f['name']} | {f.get('caption','')} "
            f"| {f.get('datatype','')} "
            f"| {f.get('modeofentry','')}"
            for f in batch
        ])

        prompt = f"""Module: {module}
Form: {form_caption} (TransID: {transid})

For each field below write ONE sentence:
what it means in business terms and when user fills it.
Include synonyms a user might search for.

Fields:
{fields_text}

Return ONLY in this exact format, one per line:
fieldname: instruction text

Example:
adate: The date when employee was present or absent, used to mark attendance or record working day"""

        try:
            response = client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=400,
                temperature=0.3
            )

            usage = response.usage
            print(f"📊 [Auto-instruct {transid}] "
                  f"in: {usage.prompt_tokens} | "
                  f"out: {usage.completion_tokens} | "
                  f"total: {usage.total_tokens}")

            raw = response.choices[0].message.content
            for line in raw.strip().split("\n"):
                if ":" in line:
                    parts = line.split(":", 1)
                    fname = parts[0].strip().lower()
                    instr = parts[1].strip()
                    if fname and instr:
                        new_instructions[fname] = instr

        except Exception as e:
            print(f"⚠️ Auto-instruct failed for {transid}: {e}")
            continue

    # Save new instructions to DB
    if new_instructions:
        try:
            conn = get_db_conn()
            cur  = conn.cursor()
            for fname, instr in new_instructions.items():
                cur.execute("""
                    INSERT INTO axpert_chatbot.field_instructions
                    (schema_name, transid, fieldname,
                     instruction, created_by,level,ref_name)
                    VALUES (%s, %s, %s, %s, 'auto',field,%s)
                    ON CONFLICT (schema_name, transid, fieldname)
                    DO NOTHING
                """, (schema, transid, fname, instr, fname))
            conn.commit()
            cur.close()
            conn.close()
            print(f"💾 Saved {len(new_instructions)} "
                  f"auto-instructions for {transid}")
        except Exception as e:
            print(f"⚠️ Could not save instructions: {e}")

    # Merge — admin instructions win over auto
    merged = {**new_instructions, **existing}
    return merged


# ✅ NEW — Generate intent summary from instructions
def generate_intent_summary(
    practice_name: str,
    module:        str,
    sub_module:    str,
    instructions:  dict
) -> str:
    """
    Generate user-friendly summary and common questions
    from field instructions — improves ChromaDB retrieval
    """
    if not instructions:
        return ""

    # Format top 20 instructions only — save tokens
    instr_text = "\n".join([
        f"- {fname}: {instr}"
        for fname, instr
        in list(instructions.items())[:20]
    ])

    prompt = f"""Module: {module}
Sub Module: {sub_module}
Practice: {practice_name}

Admin described these fields:
{instr_text}

Based on these:
1. Write 2 sentences what this practice does in business terms.
2. List 8 questions a non-technical HR user would ask.
3. List 5 action phrases (e.g. "mark present", "record attendance").

Keep under 150 words. Simple language."""

    try:
        response = client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.4
        )

        usage = response.usage
        print(f"📊 [Intent summary] "
              f"in: {usage.prompt_tokens} | "
              f"out: {usage.completion_tokens} | "
              f"total: {usage.total_tokens}")

        return response.choices[0].message.content

    except Exception as e:
        print(f"⚠️ Intent summary failed: {e}")
        return ""


def bool_val(val) -> str:
    """Convert t/f/True/False/TRUE/FALSE/Y/N → Yes/No"""
    if val in (
        False, 'False', 'FALSE', 'false',
        'f', 'F', 'N', 'n', '0', 0, None
    ):
        return 'No'
    if val in (
        True, 't', 'T', 'Y', 'y',
        '1', 1, 'TRUE', 'true', 'True'
    ):
        return 'Yes'
    return 'No'


def filter_form_for_llm(form: dict) -> dict:
    """Remove hidden/system fields before sending to Groq."""
    import copy
    form = copy.deepcopy(form)
    for dc in (form.get('datacontainers') or []):
        dc['fields'] = [
            f for f in (dc.get('fields') or [])
            if (
                bool_val(f.get('allowempty')) == 'No'
                or bool_val(f.get('hidden'))  == 'No'
                or f.get('modeofentry') in (
                    'Accept', 'Select From Sql',
                    'Select From List'
                )
            )
        ]
    return form


def form_to_text(form: dict, instructions: dict = {}) -> str:
    """Convert form metadata → rich readable text"""
    lines   = []
    transid = form.get('transid', '')
    caption = form.get('caption', '')

    lines.append(f"FORM: {caption} (TransID: {transid})")

    if form.get('purpose'):
        lines.append(f"Purpose: {form['purpose']}")

    lines.append(
        f"Workflow Required: {bool_val(form.get('workflow'))}"
    )
    lines.append(
        f"Save: {bool_val(form.get('savecontrol'))} "
        f"| Delete: {bool_val(form.get('deletecontrol'))}"
    )

    for dc in (form.get('datacontainers') or []):
        grid = "Grid" if bool_val(dc.get('asgrid')) == 'Yes' \
               else "Header"
        dc_caption = dc.get('caption') or dc.get('name', '')
        lines.append(
            f"\nSECTION [{grid}]: {dc_caption} "
            f"| DC Name: {dc.get('name', '')} "
            f"| Table: {dc.get('tablename', '')}"
        )

        if dc.get('purpose'):
            lines.append(f"  Purpose: {dc['purpose']}")

        can_add    = bool_val(dc.get('adddcrows'))
        can_delete = bool_val(dc.get('deletedcrows'))
        if grid == "Grid":
            lines.append(
                f"  Grid Options: "
                f"Add Rows={can_add}, "
                f"Delete Rows={can_delete}"
            )

        mandatory = []
        optional  = []

        for f in (dc.get('fields') or []):
            fname    = f.get('name', '')
            caption  = f.get('caption', '') or fname
            dtype    = f.get('datatype', '')
            mode     = f.get('modeofentry', '')
            is_mand  = bool_val(f.get('allowempty')) == 'No'
            readonly = bool_val(f.get('readonly'))  == 'Yes'
            hidden   = bool_val(f.get('hidden'))    == 'Yes'

            fname = fname or caption
            line  = (
                f"  - [{fname}] {caption} "
                f"| Type: {dtype} | Entry: {mode}"
            )

            if is_mand:  line += " | MANDATORY"
            if readonly: line += " | READ-ONLY"
            if hidden:   line += " | HIDDEN"

            if f.get('purpose'):
                line += f"\n      Purpose: {f['purpose']}"
            if f.get('hint'):
                line += f"\n      Hint: {f['hint']}"

            # Admin instruction wins over auto
            if fname.lower() in instructions:
                line += (
                    f"\n      ★ Note: "
                    f"{instructions[fname.lower()]}"
                )

            if f.get('sql'):
                line += f"\n      Lookup: {f['sql'][:60]}..."
            if f.get('fromtransid'):
                line += f"\n      Source: {f['fromtransid']}"
            if f.get('listvalues'):
                line += f"\n      Values: {f['listvalues']}"
            if f.get('expression'):
                line += f"\n      Calc: {f['expression']}"
            if f.get('validateexpression'):
                line += f"\n      Validation: {f['validateexpression']}"

            if is_mand:
                mandatory.append(line)
            else:
                optional.append(line)

        if mandatory:
            lines.append("  MANDATORY FIELDS:")
            lines.extend(mandatory)
        if optional:
            lines.append("  OPTIONAL FIELDS:")
            lines.extend(optional)

    if form.get('genmaps'):
        lines.append("\nAUTO-GENERATION (GenMap):")
        for g in form['genmaps']:
            lines.append(
                f"  - {g.get('caption','')} "
                f"→ Creates: {g.get('targettrasid','')} "
                f"| Trigger: {g.get('onpost','submit')} "
                f"| On Approve: {bool_val(g.get('onapprove'))}"
            )

    if form.get('mdmaps'):
        lines.append("\nAUTO-FILL (MDMap):")
        for m in form['mdmaps']:
            lines.append(
                f"  - {m.get('caption','')} "
                f"| From: {m.get('mastertransaction','')} "
                f"→ Fills: {m.get('detailsearchfield','')}"
            )

    if form.get('fillgrids'):
        lines.append("\nGRID AUTO-FILL:")
        for fg in form['fillgrids']:
            lines.append(
                f"  - {fg.get('caption','')} "
                f"→ DC: {fg.get('targetdc','')}"
            )

    return "\n".join(lines)


def generate_document(
    industry:       str,
    module:         str,
    sub_module:     str,
    practice_name:  str,
    forms_metadata: list,
    schema:         str = ""
) -> str:

    # Step 1 — fetch existing instructions
    all_instructions = {}
    for f in forms_metadata:
        if f and f.get('transid'):
            existing = get_field_instructions(
                schema, f['transid']
            )

            # ✅ Step 2 — auto-generate missing ones
            all_fields = []
            for dc in (f.get('datacontainers') or []):
                all_fields.extend(dc.get('fields') or [])

            merged = auto_generate_field_instructions(
                schema       = schema,
                transid      = f['transid'],
                form_caption = f.get('caption', ''),
                module       = module,
                fields       = all_fields,
                existing     = existing
            )
            all_instructions.update(merged)

    filtered_forms = [
        filter_form_for_llm(f)
        for f in forms_metadata if f
    ]

    all_forms_text = "\n\n========\n\n".join([
        form_to_text(f, all_instructions)
        for f in filtered_forms
    ])

    MAX_CHARS = 3000
    if len(all_forms_text) > MAX_CHARS:
        all_forms_text = (
            all_forms_text[:MAX_CHARS] + "\n[truncated]"
        )

    # ✅ Step 3 — generate intent summary
    intent_summary = generate_intent_summary(
        practice_name = practice_name,
        module        = module,
        sub_module    = sub_module,
        instructions  = all_instructions
    )

    system_prompt = """You are an Axpert ERP expert. 
Use ONLY the metadata given. Never invent anything.
Be concise. Format: OVERVIEW, FORMS, STEPS, FIELD TABLE, MISTAKES."""

    prompt = f"""Industry: {industry} | Module: {module} | Sub: {sub_module}
Practice: {practice_name}

METADATA:
{all_forms_text}

BUSINESS CONTEXT:
{intent_summary}

Write a concise implementation guide."""

    response = client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt}
        ],
        temperature=0.1,
        max_tokens=800
    )

    usage = response.usage
    print(f"📊 [{practice_name}] "
          f"in: {usage.prompt_tokens} | "
          f"out: {usage.completion_tokens} | "
          f"total: {usage.total_tokens}")

    return response.choices[0].message.content