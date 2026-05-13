from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = (
    f"postgresql://"
    f"{os.getenv('DB_USER')}:"
    f"{os.getenv('DB_PASS')}@"
    f"{os.getenv('DB_HOST')}:"
    f"{os.getenv('DB_PORT')}/"
    f"{os.getenv('DB_NAME')}"
)

engine = create_engine(
    DATABASE_URL,
    pool_size       = 20,
    max_overflow    = 30,
    pool_timeout    = 60,
    pool_recycle    = 300,
    pool_pre_ping   = True
)

SessionLocal = sessionmaker(
    autocommit = False,
    autoflush  = False,
    bind       = engine
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """
    Creates axpert_chatbot schema
    and all tables automatically
    """
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE SCHEMA IF NOT EXISTS "
            "axpert_chatbot"
        ))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    print("✅ Tables created successfully!")

def get_schema_connection(schema: str):
    """
    Returns connection pointed
    to a specific customer schema
    """
    conn = engine.connect()
    conn.execute(
        text(f"SET search_path TO {schema}")
    )
    return conn

def get_psycopg2_connection(schema: str = None):
    """
    Returns raw psycopg2 connection
    for direct SQL queries
    """
    conn = psycopg2.connect(
        host     = os.getenv("DB_HOST"),
        port     = os.getenv("DB_PORT"),
        database = os.getenv("DB_NAME"),
        user     = os.getenv("DB_USER"),
        password = os.getenv("DB_PASS")
    )
    if schema:
        cur = conn.cursor()
        cur.execute(
            f"SET search_path TO {schema}"
        )
        cur.close()
    return conn