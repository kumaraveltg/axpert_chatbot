from pydantic import BaseModel
from typing import Optional
from datetime import datetime

# Industry
class IndustryCreate(BaseModel):
    industry:    str
    description: Optional[str] = None

class IndustryResponse(BaseModel):
    id:          int
    industry:    str
    description: Optional[str]
    is_active:   str

    class Config:
        from_attributes = True

# Company
class CompanyCreate(BaseModel):
    schema_name:  str
    company_name: str
    industry_id:  int
    is_reference: Optional[str] = "N"

class CompanyResponse(BaseModel):
    id:           int
    schema_name:  str
    company_name: str
    industry_id:  int
    is_reference: str
    is_active:    str
    created_on:   Optional[datetime]

    class Config:
        from_attributes = True

# Practice
class PracticeCreate(BaseModel):
    industry_id:   int
    module:        str
    practice_name: str
    schema_ref:    str
    transid_chain: str

class PracticeResponse(BaseModel):
    id:            int
    industry_id:   int
    module:        str
    practice_name: str
    schema_ref:    str
    transid_chain: str
    is_active:     str

    class Config:
        from_attributes = True

# Document
class DocumentResponse(BaseModel):
    id:            int
    industry:      str
    module:        str
    practice_name: str
    document:      str
    status:        str
    generated_on:  Optional[datetime]

    class Config:
        from_attributes = True

# Chat
class ChatRequest(BaseModel):
    question:    str
    industry:    str
    module:      Optional[str] = None

class ChatResponse(BaseModel):
    answer:      str
    sources:     list[str]
    practice:    Optional[str]