from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
import psycopg2
import os
import requests
from dotenv import load_dotenv
import subprocess
import tempfile
import shutil
import glob
from admin_service.auth_routes import router as auth_router

from shared.database import get_db
from shared.models import (
    FieldInstruction,
    CustomerConnection,
    CompanyRegistry,
    IndustryMaster
)
from sync_service.extractor import extract_form_metadata, bool_val, get_conn
from sync_service.generator import auto_generate_field_instructions

load_dotenv()

app = FastAPI(
    title="Axpert Admin Service",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True
)

app.include_router(auth_router)

# ── Pydantic Schemas ──────────────────────────────────────────

class InstructionCreate(BaseModel):
    schema_name: str
    transid:     str
    fieldname:   str
    instruction: str
    created_by:  Optional[str] = "admin"
    level:       Optional[str] = "field"
    ref_name:    Optional[str] = ""

class InstructionResponse(BaseModel):
    id:          int
    schema_name: str
    transid:     str
    fieldname:   str
    instruction: str
    created_by:  str

    class Config:
        from_attributes = True


class ConnectionCreate(BaseModel):
    name:        str
    schema_name: str
    host:        str
    port:        int = 5432
    db_name:     str
    username:    str
    password:    str

class ConnectionResponse(BaseModel):
    id:          int
    name:        str
    schema_name: str
    host:        str
    port:        int
    db_name:     str
    status:      str
    doc_count:   int

    class Config:
        from_attributes = True


# ── Health ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "service": "admin_service",
        "status":  "running ✅"
    }


# ══════════════════════════════════════════════════════════════
# CONNECTIONS
# ══════════════════════════════════════════════════════════════

@app.get("/connections", response_model=list[ConnectionResponse])
async def list_connections():
    """List all customer DB connections"""
    db: Session = next(get_db())
    return db.query(CustomerConnection).all()


@app.post("/connections")
async def create_connection(req: ConnectionCreate):
    """Add a new customer DB connection — sync must be triggered manually"""
    db: Session = next(get_db())

    conn = CustomerConnection(
        name        = req.name,
        schema_name = req.schema_name,
        host        = req.host,
        port        = req.port,
        db_name     = req.db_name,
        username    = req.username,
        password    = req.password,
        status      = "pending",
        doc_count   = 0
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)

    # ✅ FIXED — removed _run_sync() here
    # Sync is triggered manually via the play button in SyncManager
    return {
        "id":          conn.id,
        "name":        conn.name,
        "schema_name": conn.schema_name,
        "host":        conn.host,
        "port":        conn.port,
        "db_name":     conn.db_name,
        "status":      conn.status,
        "doc_count":   conn.doc_count
    }


@app.put("/connections/{conn_id}", response_model=ConnectionResponse)
async def update_connection(conn_id: int, req: ConnectionCreate):
    """Update an existing connection"""
    db: Session = next(get_db())

    conn = db.query(CustomerConnection).filter_by(id=conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    conn.name        = req.name
    conn.schema_name = req.schema_name
    conn.host        = req.host
    conn.port        = req.port
    conn.db_name     = req.db_name
    conn.username    = req.username
    if req.password:
        conn.password = req.password

    db.commit()
    db.refresh(conn)
    return conn


@app.delete("/connections/{conn_id}")
async def delete_connection(conn_id: int):
    """Delete a customer connection"""
    db: Session = next(get_db())

    conn = db.query(CustomerConnection).filter_by(id=conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    db.delete(conn)
    db.commit()
    return {"message": f"Deleted connection {conn_id}"}


@app.post("/connections/{conn_id}/test")
async def test_connection(conn_id: int):
    """Test if a customer DB is reachable"""
    db: Session = next(get_db())

    conn = db.query(CustomerConnection).filter_by(id=conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    try:
        pg = psycopg2.connect(
            host            = conn.host,
            port            = conn.port,
            dbname          = conn.db_name,
            user            = conn.username,
            password        = conn.password,
            connect_timeout = 5
        )
        pg.close()
        return {"status": "ok", "message": "Connection successful ✅"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/connections/{conn_id}/sync")
async def sync_connection(conn_id: int):
    """Manually trigger sync for a connection"""
    db: Session = next(get_db())

    conn = db.query(CustomerConnection).filter_by(id=conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    conn.status = "syncing"
    db.commit()

    _run_sync(conn_id, db)
    return {"message": f"Sync triggered for {conn.name}"}


def _run_sync(conn_id: int, db):
    conn = db.query(CustomerConnection).filter_by(id=conn_id).first()
    if not conn:
        return
    try:
        requests.post(f"http://127.0.0.1:8005/sync/{conn.schema_name}")
    except Exception as e:
        print(f"[sync] failed: {e}")


# ══════════════════════════════════════════════════════════════
# INSTRUCTIONS CRUD
# ══════════════════════════════════════════════════════════════

@app.post("/instructions", response_model=InstructionResponse)
async def add_instruction(req: InstructionCreate):
    """Add or update a field instruction"""
    db: Session = next(get_db())

    existing = db.query(FieldInstruction).filter_by(
        schema_name = req.schema_name,
        transid     = req.transid.lower(),
        level       = req.level,
        ref_name    = req.ref_name.lower()
    ).first()

    if existing:
        existing.instruction = req.instruction
        existing.created_by  = req.created_by
        existing.updated_at  = func.now()
        db.commit()
        db.refresh(existing)
        return existing

    instr = FieldInstruction(
        schema_name = req.schema_name,
        transid     = req.transid.lower(),
        fieldname   = req.fieldname.lower(),
        instruction = req.instruction,
        created_by  = req.created_by,
        level       = req.level,
        ref_name    = req.ref_name.lower()
    )
    db.add(instr)
    db.commit()
    db.refresh(instr)
    return instr


@app.get("/instructions/{schema_name}/{transid}")
async def get_instructions(schema_name: str, transid: str):
    """Get all instructions for a form"""
    db: Session = next(get_db())

    instrs = db.query(FieldInstruction).filter_by(
        schema_name = schema_name,
        transid     = transid.lower()
    ).all()

    return {
        "schema":       schema_name,
        "transid":      transid,
        "count":        len(instrs),
        "instructions": [
            {
                "fieldname":   i.fieldname,
                "instruction": i.instruction,
                "created_by":  i.created_by
            }
            for i in instrs
        ]
    }


@app.delete("/instructions/{schema_name}/{transid}/{fieldname}")
async def delete_instruction(
    schema_name: str,
    transid:     str,
    fieldname:   str
):
    """Delete a field instruction"""
    db: Session = next(get_db())

    instr = db.query(FieldInstruction).filter_by(
        schema_name = schema_name,
        transid     = transid.lower(),
        fieldname   = fieldname.lower()
    ).first()

    if not instr:
        raise HTTPException(
            status_code = 404,
            detail      = f"Instruction not found for {transid}.{fieldname}"
        )

    db.delete(instr)
    db.commit()
    return {"message": f"Deleted instruction for {fieldname}"}


# ══════════════════════════════════════════════════════════════
# FIELD EXPLORER
# ══════════════════════════════════════════════════════════════

@app.get("/fields/{schema_name}/{transid}")
async def get_fields(schema_name: str, transid: str):
    """List all fields for a form from metadata."""
    db: Session = next(get_db())

    form = extract_form_metadata(schema_name, transid)
    if not form:
        raise HTTPException(
            status_code = 404,
            detail      = f"Form {transid} not found in {schema_name}"
        )

    instrs    = db.query(FieldInstruction).filter_by(
        schema_name = schema_name,
        transid     = transid.lower()
    ).all()
    instr_map = {i.fieldname: i.instruction for i in instrs}

    result = []
    for dc in form.get('datacontainers', []):
        grid = "Grid" if bool_val(dc.get('asgrid')) == 'Yes' else "Header"
        for f in dc.get('fields', []):
            fname   = f.get('name', '')
            hidden  = bool_val(f.get('hidden')) == 'Yes'
            is_mand = bool_val(f.get('allowempty')) == 'No'

            result.append({
                "dcname":           dc.get('name'),
                "dc_type":          grid,
                "fieldname":        fname,
                "caption":          f.get('caption', ''),
                "datatype":         f.get('datatype', ''),
                "modeofentry":      f.get('modeofentry', ''),
                "mandatory":        is_mand,
                "hidden":           hidden,
                "has_instruction":  fname.lower() in instr_map,
                "instruction":      instr_map.get(fname.lower(), None)
            })

    return {
        "schema":                   schema_name,
        "transid":                  transid,
        "caption":                  form.get('caption', ''),
        "total_fields":             len(result),
        "fields_with_instructions": len(instr_map),
        "fields":                   result
    }


@app.get("/forms/{schema_name}")
async def list_forms(schema_name: str):
    """List all synced forms for a schema with instruction coverage stats"""
    db: Session = next(get_db())

    instrs = db.query(FieldInstruction).filter_by(
        schema_name = schema_name
    ).all()

    transid_map = {}
    for i in instrs:
        if i.transid not in transid_map:
            transid_map[i.transid] = 0
        transid_map[i.transid] += 1

    try:
        pg, meta_schema = get_conn(schema_name)
        cur = pg.cursor()
        cur.execute(f"SELECT transid, caption FROM {meta_schema}.vw_tstructs")
        caption_map = {r[0].lower(): r[1] for r in cur.fetchall()}
        cur.close()
        pg.close()
    except Exception:
        caption_map = {}

    return {
        "schema":                  schema_name,
        "forms_with_instructions": len(transid_map),
        "details": [
            {
                "transid":           tid,
                "caption":           caption_map.get(tid.lower(), tid),
                "instruction_count": count
            }
            for tid, count in transid_map.items()
        ]
    }


@app.get("/transids/{schema_name}")
async def list_transids(schema_name: str):
    """Get all transids with captions from Axpert metadata"""
    try:
        pg, meta_schema = get_conn(schema_name)
        cur = pg.cursor()
        cur.execute(f"""
            SELECT DISTINCT
                substring(pagetype from 2) as transid,
                caption
            FROM {meta_schema}.axp_vw_menu
            WHERE pagetype LIKE 't%'
            AND levelno = 2
            AND pagetype IS NOT NULL
            ORDER BY caption
        """)
        rows = cur.fetchall()
        cur.close()
        pg.close()
        return {"transids": [{"transid": r[0], "caption": r[1]} for r in rows]}
    except Exception as e:
        return {"transids": [], "error": str(e)}


@app.get("/company/{schema_name}")
async def get_company(schema_name: str):
    db: Session = next(get_db())
    company = db.query(CompanyRegistry).filter_by(
        schema_name = schema_name,
        is_active   = 'Y'
    ).first()
    if not company:
        return {"industry": "Axpert ERP", "company_name": schema_name}
    industry = db.query(IndustryMaster).filter_by(
        id = company.industry_id
    ).first()
    return {
        "industry":     industry.industry if industry else "Axpert ERP",
        "company_name": company.company_name
    }


@app.get("/instructions/{schema_name}/{transid}/{level}")
async def get_instructions_by_level(schema_name: str, transid: str, level: str):
    db: Session = next(get_db())
    instrs = db.query(FieldInstruction).filter_by(
        schema_name = schema_name,
        transid     = transid.lower(),
        level       = level
    ).all()
    return {
        "schema":  schema_name,
        "transid": transid,
        "level":   level,
        "count":   len(instrs),
        "instructions": [
            {
                "ref_name":    i.ref_name,
                "fieldname":   i.fieldname,
                "instruction": i.instruction,
                "created_by":  i.created_by
            }
            for i in instrs
        ]
    }


@app.get("/level-data/{schema_name}/{transid}/{level}")
async def get_level_data(schema_name: str, transid: str, level: str):
    """Get metadata items for a given level"""
    try:
        pg, meta_schema = get_conn(schema_name)
        cur = pg.cursor()

        if level == 'dc':
            cur.execute(f"""
                SELECT name, caption FROM {meta_schema}.vw_dc
                WHERE lower(transid) = lower(%s)
                ORDER BY name
            """, (transid,))
        elif level == 'form':
            cur.execute(f"""
                SELECT transid as name, caption FROM {meta_schema}.vw_tstructs
                WHERE lower(transid) = lower(%s)
            """, (transid,))
        elif level == 'genmap':
            cur.execute(f"""
                SELECT name, caption FROM {meta_schema}.v_genmap
                WHERE lower(stransid) = lower(%s)
            """, (transid,))
        elif level == 'mdmap':
            cur.execute(f"""
                SELECT name, caption FROM {meta_schema}.vw_mdmap
                WHERE lower(stransid) = lower(%s)
            """, (transid,))
        elif level == 'fillgrid':
            cur.execute(f"""
                SELECT name, caption FROM {meta_schema}.v_fillgrid
                WHERE lower(stransid) = lower(%s)
            """, (transid,))
        else:
            return {"items": []}

        rows = cur.fetchall()
        cur.close()
        pg.close()
        return {"items": [{"name": r[0], "caption": r[1]} for r in rows]}
    except Exception as e:
        return {"items": [], "error": str(e)}


@app.post("/auto-generate/{schema_name}/{transid}/{level}")
async def auto_generate_level(schema_name: str, transid: str, level: str):
    """Auto generate instructions for DC/GenMap/MDMap/FillGrid level"""
    pg, meta_schema = get_conn(schema_name)
    cur = pg.cursor()

    if level == 'dc':
        cur.execute(f"""
            SELECT name, caption, purpose, tablename, allowchange,
                adddcrows, deletedcrows
            FROM {meta_schema}.vw_dc
            WHERE lower(transid) = lower(%s)
        """, (transid,))
        items = [{"name": r[0], "caption": r[1],
                "context": f"Purpose:{r[2] or ''} | Filter:{r[3] or ''} | AllowChange:{r[4] or ''} | AddRows:{r[5] or ''} | DeleteRows:{r[6] or ''}"
                } for r in cur.fetchall()]

    elif level == 'field':
        cur.execute(f"""
            SELECT name, caption, expression, validateexpression,
                sql, listvalues, purpose, datatype, modeofentry
            FROM {meta_schema}.vw_field_informations
            WHERE lower(transid) = lower(%s)
            AND (hidden IS NULL OR hidden NOT IN ('t', 'T', 'Y', 'y', '1', 'True', 'TRUE', 'true'))
        """, (transid,))
        items = [{"name": r[0], "caption": r[1],
                "context": f"Formula:{r[2] or ''} | Validation:{r[3] or ''} | Lookup:{r[4] or ''} | Values:{r[5] or ''} | Purpose:{r[6] or ''} | Type:{r[7] or ''} | Entry:{r[8] or ''}"
                } for r in cur.fetchall()]

    elif level == 'genmap':
        cur.execute(f"""
            SELECT name, caption, targettrasid, onpost, onapprove,
                purpose, rowcontrol, mapping, groupfield,
                dcname, basedondc
            FROM {meta_schema}.v_genmap
            WHERE lower(stransid) = lower(%s)
        """, (transid,))

        def rowcontrol_text(rc):
            if rc is None: return 'all rows'
            rc = str(rc)
            if rc == '0': return 'all grid rows'
            if rc == '1': return 'first grid row only'
            if rc == '2': return 'second grid row only'
            return f'row {rc}'

        items = []
        for r in cur.fetchall():
            name       = r[0]
            caption    = r[1]
            target     = r[2] or ''
            onpost     = r[3] or ''
            onapprove  = r[4] or ''
            purpose    = r[5] or ''
            rowcontrol = r[6]
            mapping    = r[7] or ''
            groupfield = r[8] or ''
            dcname     = r[9] or ''
            basedondc  = r[10] or ''

            rc_text = rowcontrol_text(rowcontrol)

            if groupfield:
                group_text = (
                    f"Groups grid rows by '{groupfield}' — "
                    f"all rows sharing the same {groupfield} value "
                    f"are combined into ONE voucher in {target}"
                )
            else:
                group_text = f"Creates one {target} per {rc_text}"

            context = (
                f"Creates: {target} | "
                f"Trigger: {'On Post' if onpost else ''} {'On Approve' if onapprove else ''} | "
                f"Source DC: {dcname} | Based on DC: {basedondc} | "
                f"Row mapping: {rc_text} | "
                f"Grouping: {group_text} | "
                f"Field mapping: {mapping} | "
                f"Purpose: {purpose}"
            )
            items.append({"name": name, "caption": caption, "context": context})

    elif level == 'mdmap':
        cur.execute(f"""
            SELECT name, caption, mastertransaction, masterfield,
                detailsearchfield, mastersearchfield
            FROM {meta_schema}.vw_mdmap
            WHERE lower(stransid) = lower(%s)
        """, (transid,))
        items = [{"name": r[0], "caption": r[1],
                "context": f"SourceForm:{r[2] or ''} | SourceField:{r[3] or ''} | TargetField:{r[4] or ''} | SearchField:{r[5] or ''}"
                } for r in cur.fetchall()]

    elif level == 'fillgrid':
        cur.execute(f"""
            SELECT name, caption, sql_editor_sql, purpose, targetdc
            FROM {meta_schema}.v_fillgrid
            WHERE lower(stransid) = lower(%s)
        """, (transid,))
        items = [{"name": r[0], "caption": r[1],
                "context": f"SQL:{r[2] or ''} | Purpose:{r[3] or ''} | TargetDC:{r[4] or ''}"
                } for r in cur.fetchall()]

    elif level == 'form':
        cur.execute(f"""
            SELECT transid, caption, purpose, savecontrol,
                deletecontrol, workflow
            FROM {meta_schema}.vw_tstructs
            WHERE lower(transid) = lower(%s)
        """, (transid,))
        items = [{"name": r[0], "caption": r[1],
                "context": f"Purpose:{r[2] or ''} | Save:{r[3] or ''} | Delete:{r[4] or ''} | Workflow:{r[5] or ''}"
                } for r in cur.fetchall()]

    else:
        items = []

    cur.close()
    pg.close()

    if not items:
        return {"count": 0}

    db: Session = next(get_db())
    existing = {
        i.ref_name: i.instruction
        for i in db.query(FieldInstruction).filter_by(
            schema_name = schema_name,
            transid     = transid.lower(),
            level       = level
        ).all()
    }

    fields = [
        {
            "name":        i["name"],
            "caption":     i["caption"],
            "datatype":    level,
            "modeofentry": i.get("context", ""),
            "hidden":      False
        }
        for i in items
    ]

    merged = auto_generate_field_instructions(
        schema       = schema_name,
        transid      = transid,
        form_caption = transid,
        module       = level,
        fields       = fields,
        existing     = existing
    )

    count = 0
    for fname, instr in merged.items():
        if fname not in existing:
            db.add(FieldInstruction(
                schema_name = schema_name,
                transid     = transid.lower(),
                fieldname   = fname,
                instruction = instr,
                created_by  = 'auto',
                level       = level,
                ref_name    = fname
            ))
            count += 1
    db.commit()
    return {"count": count}

@app.get("/core-transactions/{schema_name}")
async def get_core_transactions(schema_name: str):
    """Fetch core transactions from vw_tstructs where iscoretrans = Yes"""
    try:
        pg, meta_schema = get_conn(schema_name)
        cur = pg.cursor()
        cur.execute(f"""
            SELECT transid, caption 
            FROM {meta_schema}.vw_tstructs
            WHERE isacoretrans = 'Yes'
            ORDER BY caption
        """)
        rows = cur.fetchall()
        cur.close()
        pg.close()
        return {"items": [{"transid": r[0], "caption": r[1]} for r in rows]}
    except Exception as e:
        return {"items": [], "error": str(e)}

# ══════════════════════════════════════════════════════════════
# MIGRATION — Local → Cloud
# ══════════════════════════════════════════════════════════════

@app.get("/check-pg-tools")
async def check_pg_tools():
    """Check if pg_dump and pg_restore are available"""
    dump_path    = find_pg_executable("pg_dump")
    restore_path = find_pg_executable("pg_restore")

    # Test by running --version
    results = {}
    for name, path in [("pg_dump", dump_path), ("pg_restore", restore_path)]:
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output = True,
                text    = True,
                timeout = 10
            )
            results[name] = {
                "path":    path,
                "version": result.stdout.strip(),
                "status":  "✅ found"
            }
        except Exception as e:
            results[name] = {
                "path":   path,
                "status": f"❌ not found: {str(e)}"
            }

    return results

def find_pg_executable(exe_name: str) -> str:
    """
    Auto detect pg_dump or pg_restore path.
    1. Check PATH first (Linux/Mac + Windows if configured)
    2. Search common Windows PostgreSQL install paths
    3. Fall back to just the name (hope it's in PATH)
    """
    # Step 1 — check if available in PATH directly
    found = shutil.which(exe_name)
    if found:
        print(f"✅ Found {exe_name} in PATH: {found}")
        return found

    # Step 2 — search Windows PostgreSQL install folders
    windows_patterns = [
        f"C:\\Program Files\\PostgreSQL\\*\\bin\\{exe_name}.exe",
        f"C:\\Program Files (x86)\\PostgreSQL\\*\\bin\\{exe_name}.exe",
        f"D:\\Program Files\\PostgreSQL\\*\\bin\\{exe_name}.exe",
    ]

    for pattern in windows_patterns:
        matches = glob.glob(pattern)
        if matches:
            # Use highest version — sort descending
            matches.sort(reverse=True)
            print(f"✅ Found {exe_name}: {matches[0]}")
            return matches[0]

    # Step 3 — check .env override
    pg_bin = os.getenv("PG_BIN_PATH", "")
    if pg_bin:
        full_path = os.path.join(pg_bin, exe_name)
        print(f"✅ Using PG_BIN_PATH: {full_path}")
        return full_path

    # Step 4 — fallback, let OS find it
    print(f"⚠️ {exe_name} not found — using name only")
    return exe_name

@app.post("/connections/{conn_id}/migrate")
async def migrate_to_cloud(conn_id: int):
    """
    Migrate customer schemas from local Axpert DB to cloud.
    Dumps hcaspay + hcaspayaxdef from local
    Restores to cloud PostgreSQL (Digital Ocean)
    Skips if schema already exists on cloud.
    """
    db: Session = next(get_db())

    conn = db.query(CustomerConnection).filter_by(id=conn_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Cloud credentials from .env
    cloud_host = os.getenv("CLOUD_HOST")
    cloud_port = os.getenv("CLOUD_PORT", "5432")
    cloud_db   = os.getenv("CLOUD_DB",   "postgres")
    cloud_user = os.getenv("CLOUD_USER")
    cloud_pass = os.getenv("CLOUD_PASS")

    if not all([cloud_host, cloud_user, cloud_pass]):
        raise HTTPException(
            status_code = 500,
            detail      = "Cloud credentials missing in .env"
        )

    schema_name = conn.schema_name
    meta_schema = f"{schema_name}axdef"

    results = []

    # Check which schemas already exist on cloud
    try:
        cloud_conn = psycopg2.connect(
            host     = cloud_host,
            port     = cloud_port,
            dbname   = cloud_db,
            user     = cloud_user,
            password = cloud_pass,
            connect_timeout = 10
        )
        cur = cloud_conn.cursor()
        cur.execute("""
            SELECT schema_name 
            FROM information_schema.schemata
            WHERE schema_name = ANY(%s)
        """, ([schema_name, meta_schema],))
        existing_schemas = [r[0] for r in cur.fetchall()]
        cur.close()
        cloud_conn.close()
    except Exception as e:
        raise HTTPException(
            status_code = 500,
            detail      = f"Cannot connect to cloud DB: {str(e)}"
        )

    # Process each schema
    for schema in [schema_name, meta_schema]:

        # Skip if already exists on cloud
        if schema in existing_schemas:
            results.append({
                "schema": schema,
                "status": "⏭ skipped — already exists on cloud"
            })
            continue

        # Set PGPASSWORD env for pg_dump/pg_restore
        env = os.environ.copy()
        env["PGPASSWORD"] = conn.password

        cloud_env = os.environ.copy()
        cloud_env["PGPASSWORD"] = cloud_pass

        try:
            with tempfile.NamedTemporaryFile(
                suffix=".dump", delete=False
            ) as tmp:
                dump_file = tmp.name
            pg_bin = os.getenv("PG_BIN_PATH", "")
            pg_dump_exe    = find_pg_executable("pg_dump")
            pg_restore_exe = find_pg_executable("pg_restore")
                
            # Step 1 — pg_dump from local
            dump_cmd = [
                pg_dump_exe,
                "-h", conn.host,
                "-p", str(conn.port),
                "-U", conn.username,
                "-d", conn.db_name,
                "-n", schema,
                "-F", "c",
                "-f", dump_file
            ]

            dump_result = subprocess.run(
                dump_cmd,
                env     = env,
                capture_output = True,
                text    = True,
                timeout = 300
            )

            if dump_result.returncode != 0:
                results.append({
                    "schema": schema,
                    "status": f"❌ dump failed: {dump_result.stderr[:200]}"
                })
                continue

            print(f"✅ Dumped: {schema} → {dump_file}")

            # Step 1.5 — Create schema on cloud before restore
            try:
                cloud_conn = psycopg2.connect(
                    host     = cloud_host,
                    port     = cloud_port,
                    dbname   = cloud_db,
                    user     = cloud_user,
                    password = cloud_pass,
                    connect_timeout = 10
                )
                cur = cloud_conn.cursor()
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                cloud_conn.commit()
                cur.close()
                cloud_conn.close()
                print(f"✅ Schema created: {schema}")
            except Exception as e:
                results.append({
                    "schema": schema,
                    "status": f"❌ schema create failed: {str(e)}"
                })
                continue

            # Step 2 — pg_restore to cloud
            restore_cmd = [
                pg_restore_exe,
                "-h", cloud_host,
                "-p", str(cloud_port),
                "-U", cloud_user,
                "-d", cloud_db,
                #"-n", schema,
                "--no-owner",
                "--no-privileges",
                "-F", "c",
                dump_file
            ]

            restore_result = subprocess.run(
                restore_cmd,
                env     = cloud_env,
                capture_output = True,
                text    = True,
                timeout = 300
            )

            # pg_restore returns 1 for warnings — check stderr
            if restore_result.returncode > 1:
                results.append({
                    "schema": schema,
                    "status": f"❌ restore failed: {restore_result.stderr[:200]}"
                })
                continue

            print(f"✅ Restored: {schema} → cloud")
            results.append({
                "schema": schema,
                "status": "✅ migrated successfully"
            })

        except subprocess.TimeoutExpired:
            results.append({
                "schema": schema,
                "status": "❌ timeout — schema too large"
            })
        except Exception as e:
            results.append({
                "schema": schema,
                "status": f"❌ error: {str(e)}"
            })
        finally:
            # Clean up temp dump file
            try:
                os.unlink(dump_file)
            except Exception:
                pass

    # Overall status
    all_ok = all("✅" in r["status"] or "⏭" in r["status"] for r in results)

    return {
        "connection": conn.name,
        "schema":     schema_name,
        "status":     "completed" if all_ok else "partial",
        "results":    results
    }