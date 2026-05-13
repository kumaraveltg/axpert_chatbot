from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, 
    Text, ForeignKey, TIMESTAMP, UniqueConstraint, DateTime
)
from sqlalchemy.orm import (
    relationship, declarative_base
)
from sqlalchemy.sql import func

Base = declarative_base()

class IndustryMaster(Base):
    __tablename__  = "industry_master"
    __table_args__ = {
        "schema": "axpert_chatbot"
    }

    id          = Column(
                    Integer, 
                    primary_key=True
                  )
    industry    = Column(
                    String(50), 
                    unique=True
                  )
    description = Column(String(200))
    is_active   = Column(
                    String(1), 
                    default="Y"
                  )

    companies   = relationship(
                    "CompanyRegistry",
                    back_populates="industry"
                  )
    practices   = relationship(
                    "PracticeMaster",
                    back_populates="industry"
                  )


class CompanyRegistry(Base):
    __tablename__  = "company_registry"
    __table_args__ = {
        "schema": "axpert_chatbot"
    }

    id           = Column(
                     Integer, 
                     primary_key=True
                   )
    schema_name  = Column(
                     String(50), 
                     unique=True
                   )
    company_name = Column(String(200))
    industry_id  = Column(
                     Integer,
                     ForeignKey(
                       "axpert_chatbot"
                       ".industry_master.id"
                     )
                   )
    is_reference = Column(
                     String(1), 
                     default="N"
                   )
    is_active    = Column(
                     String(1), 
                     default="Y"
                   )
    created_on   = Column(
                     TIMESTAMP,
                     server_default=func.now()
                   )

    industry     = relationship(
                     "IndustryMaster",
                     back_populates="companies"
                   )


class PracticeMaster(Base):
    __tablename__  = "practice_master"
    __table_args__ = (
        {
            "schema": "axpert_chatbot"
        },
    )

    id            = Column(
                      Integer, 
                      primary_key=True
                    )
    industry_id   = Column(
                      Integer,
                      ForeignKey(
                        "axpert_chatbot"
                        ".industry_master.id"
                      )
                    )
    module        = Column(String(100))
    practice_name = Column(String(200))
    schema_ref    = Column(String(50))
    transid_chain = Column(String(500))
    is_active     = Column(
                      String(1), 
                      default="Y"
                    )

    industry      = relationship(
                      "IndustryMaster",
                      back_populates="practices"
                    )
    documents     = relationship(
                      "GeneratedDocument",
                      back_populates="practice"
                    )


class GeneratedDocument(Base):
    __tablename__  = "generated_documents"
    __table_args__ = {
        "schema": "axpert_chatbot"
    }

    id            = Column(
                      Integer, 
                      primary_key=True
                    )
    practice_id   = Column(
                      Integer,
                      ForeignKey(
                        "axpert_chatbot"
                        ".practice_master.id"
                      )
                    )
    industry      = Column(String(50))
    module        = Column(String(100))
    practice_name = Column(String(200))
    document      = Column(Text)
    chroma_id     = Column(String(100))
    generated_on  = Column(
                      TIMESTAMP,
                      server_default=func.now()
                    )
    status        = Column(
                      String(20), 
                      default="pending"
                    )

    practice      = relationship(
                      "PracticeMaster",
                      back_populates="documents"
                    )
    
# Add to shared/models.py

class FieldInstruction(Base):
    __tablename__  = "field_instructions"
    __table_args__ = {"schema": "axpert_chatbot"}

    id          = Column(Integer, primary_key=True)
    schema_name = Column(String(50), nullable=False)
    transid     = Column(String(20), nullable=False)
    fieldname   = Column(String(50), nullable=False)
    instruction = Column(Text,       nullable=False)
    level    = Column(String(20),  default="field")
    ref_name = Column(String(200), default="")
    created_by  = Column(String(50), default="admin")
    created_at  = Column(TIMESTAMP,  server_default=func.now())
    updated_at  = Column(TIMESTAMP,  server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            'schema_name', 'transid', 'level', 'ref_name', 
            name='uq_field_instruction'
        ),
        {"schema": "axpert_chatbot"}
    )

class SyncModuleConfig(Base):
    __tablename__ = "sync_module_config"
    __table_args__ = {"schema": "axpert_chatbot"}

    id          = Column(Integer, primary_key=True)
    schema_name = Column(String(100), nullable=False)
    root_module = Column(String(200), nullable=False)
    sub_module  = Column(String(200), nullable=False)
    is_enabled  = Column(String(1), default='Y')
    created_at  = Column(DateTime, default=datetime.now)
    updated_at  = Column(DateTime, default=datetime.now)    


class CustomerConnection(Base):
    __tablename__ = "customer_connections"
    __table_args__ = {"schema": "axpert_chatbot"}

    id          = Column(Integer,     primary_key=True, autoincrement=True)
    name        = Column(String(100), nullable=False)   # "HMS Pharma"
    schema_name = Column(String(50),  nullable=False)   # "hms_pharma"
    host        = Column(String(100), nullable=False)   # "192.168.1.10"
    port        = Column(Integer,     default=5432)
    db_name     = Column(String(100), nullable=False)   # "axpert_db"
    username    = Column(String(100), nullable=False)
    password    = Column(String(255), nullable=False)   # store plain for now
    status      = Column(String(20),  default="pending") # pending/syncing/connected/error
    doc_count   = Column(Integer,     default=0)
    created_at  = Column(DateTime,    default=func.now())
    updated_at  = Column(DateTime,    default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__  = "users"
    __table_args__ = {"schema": "axpert_chatbot"}

    id          = Column(Integer, primary_key=True)
    username    = Column(String, unique=True, nullable=False)
    password    = Column(String, nullable=False)
    role        = Column(String, default="user")
    schema_name = Column(String, nullable=True)
    is_active   = Column(String, default="Y")
    created_at  = Column(DateTime, default=func.now())