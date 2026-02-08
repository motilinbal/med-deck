"""
Pydantic models for medical lab data validation.

This module defines validation models for all document types stored in the labs collection:
- QuantitativeLabModel: For numerical lab test results
- ReferenceRangeModel: For reference/normal ranges
- MicrobiologyModel: For culture and sensitivity reports
- PathologyModel: For histopathology reports
- ImagingModel: For radiology and imaging reports
- PendingIngestion: For staging email data awaiting user approval
- Card: For patient consultation cards
- ChatMessage: For chat messages within cards
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator
from bson.objectid import ObjectId


# =============================================================================
# CHAT MESSAGE TYPES AND ROLES
# =============================================================================

class MessageRole(str, Enum):
    """
    Enumeration of valid message roles in the chat system.
    
    Roles determine how messages are handled, displayed, and processed:
    - USER: Refined medical input from the clinician (visible, LLM context)
    - ASSISTANT: AI Agent response (visible, LLM context)
    - LOG: Debug/System internal messages (hidden, no LLM context)
    - INFO: Transient user-facing status updates (visible, no LLM context)
    - ERROR: Critical failure notifications (visible as alert, no LLM context)
    """
    USER = "user"           # Refined medical input
    ASSISTANT = "assistant" # AI Agent response
    LOG = "log"             # Debug/System internal (hidden)
    INFO = "info"           # Transient user-facing status (e.g., "Searching...")
    ERROR = "error"         # Critical failures


class ChatMessage(BaseModel):
    """
    Model for a single chat message within a Card.
    
    Each message has a unique ID, role, timestamp, and content.
    The chat array is the single source of truth for the conversation history.
    
    Schema:
    {
        "id": "unique_message_id",
        "role": "user" | "assistant" | "log" | "info" | "error",
        "timestamp": "ISO datetime",
        "content": "The message text"
    }
    """
    id: str = Field(
        default_factory=lambda: str(ObjectId()),
        description="Unique identifier for the message"
    )
    role: MessageRole = Field(
        ...,
        description="The role of the message sender"
    )
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp when the message was created"
    )
    content: str = Field(
        ...,
        description="The text content of the message"
    )


class HistoryChunk(BaseModel):
    """
    Embedded model for a single raw history chunk within the Card's chunks array.
    
    This serves as the ledger of raw inputs and links to processed data in the
    history collection.
    
    Schema:
    {
        "text": "The raw, cleaned text chunk from the email (deduplicated)",
        "processed_id": "ObjectId | Null. Reference to the document in 'history' collection",
        "ingested_at": "ISODate. Timestamp of when this chunk was appended"
    }
    """
    text: str = Field(
        ...,
        description="The raw, cleaned text chunk from the email (deduplicated)"
    )
    processed_id: Optional[str] = Field(
        default=None,
        description="MongoDB ObjectId as string referencing the processed document in 'history' collection. Null indicates pending processing."
    )
    ingested_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="UTC timestamp when this chunk was appended to the record"
    )


class Card(BaseModel):
    """
    Model for a patient consultation card.
    
    The card contains the transcript buffer, chat history array,
    and the chunks ledger for raw history inputs.
    
    Schema:
    {
        "_id": "MongoDB ObjectId",
        "serial": int,
        "nickname": "Patient display name",
        "transcript": "Raw audio transcription buffer (ephemeral)",
        "chat": [List of ChatMessage objects],
        "chunks": [List of HistoryChunk objects],
        "processed_note": "Deprecated - kept for migration only"
    }
    """
    id: Optional[str] = Field(default=None, description="MongoDB ObjectId as string")
    serial: int = Field(..., description="Sequential card number")
    nickname: str = Field(default="New Consultation", description="Display name for the card")
    transcript: str = Field(
        default="",
        description="Temporary buffer for raw audio transcription. Cleared after processing."
    )
    chat: List[ChatMessage] = Field(
        default_factory=list,
        description="Chronological list of chat messages (single source of truth)"
    )
    chunks: List[HistoryChunk] = Field(
        default_factory=list,
        description="Ledger of raw history chunks with processing status"
    )
    processed_note: Optional[str] = Field(
        default=None,
        description="DEPRECATED: Kept for migration purposes only. Use chat array instead."
    )


class ProcessedHistoryDocument(BaseModel):
    """
    Model for processed clinical intelligence documents in the 'history' collection.
    
    Each document represents a distinct clinical event (admission, consult, discharge)
    extracted from a raw chunk.
    
    Schema:
    {
        "_id": "ObjectId",
        "card_id": "ObjectId. Reference to the parent card",
        "timestamp": "ISODate. The clinical date extracted from the text",
        "date_estimated": "Boolean. True if specific date was missing and ingestion date was used",
        "title": "String. A generated one-line summary",
        "content": "String. The Scribe-processed narrative in Markdown format",
        "original_chunk_index": "Integer. The index in cards.chunks this document was derived from"
    }
    """
    id: Optional[str] = Field(default=None, description="MongoDB ObjectId as string")
    card_id: str = Field(..., description="ObjectId of the parent card as string")
    timestamp: datetime = Field(..., description="The clinical date extracted from the text (e.g., date of admission)")
    date_estimated: bool = Field(
        default=False,
        description="True if specific date was missing and ingestion date was used"
    )
    title: str = Field(..., description="A generated one-line summary (e.g., 'Internal Medicine Discharge Summary')")
    content: str = Field(..., description="The Scribe-processed narrative in Markdown format")
    original_chunk_index: int = Field(..., description="The index in cards.chunks this document was derived from")
    
    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Validate that card_id is a valid ObjectId string."""
        try:
            ObjectId(v)
            return v
        except Exception as e:
            raise ValueError(f"card_id must be a valid ObjectId string: {e}")


class QuantitativeLabModel(BaseModel):
    """
    Model for quantitative lab results (biochemistry, hematology, hormones, etc.).
    
    Schema:
    {
        "_id": "MongoDB unique ID",
        "card_id": "The card_id of the patient",
        "category": "Quantitative",
        "date": "DD/MM/YY format with padding removed",
        "time": "HH:MM format",
        "timestamp": "MongoDB timestamp",
        "material": "Specimen type (e.g., Venous Blood, Urine)",
        "test_name": "Exact test name from source",
        "value": "number or String",
        "operator": "=, >, <, <=, or >=",
        "note": "Optional remarks"
    }
    """
    
    card_id: str = Field(..., description="The card_id of the patient this document belongs to")
    category: Literal["Quantitative"] = Field(default="Quantitative")
    date: str = Field(..., description="DD/MM/YY format with padding removed")
    time: str = Field(..., description="HH:MM format")
    timestamp: datetime = Field(..., description="MongoDB timestamp")
    material: str = Field(..., description="The specimen type (e.g., Venous Blood, Urine, Pleural Fluid)")
    test_name: str = Field(..., description="The exact test name as it appears in the original source")
    value: Union[int, float, str] = Field(..., description="The numeric or string value")
    operator: Literal["=", ">", "<", "<=", ">="] = Field(default="=", description="Comparison operator")
    note: Optional[str] = Field(default=None, description="Remarks related to the sample")
    
    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Validate that card_id is a valid ObjectId string."""
        try:
            ObjectId(v)
            return v
        except Exception as e:
            raise ValueError(f"card_id must be a valid ObjectId string: {e}")
    
    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Validate date is in DD/MM/YY format."""
        if not v:
            raise ValueError("date is required")
        # Basic format check - allows for variable padding (e.g., 5/2/24 or 05/02/24)
        parts = v.split("/")
        if len(parts) != 3:
            raise ValueError("date must be in DD/MM/YY format")
        try:
            day, month, year = parts
            int(day)
            int(month)
            int(year)
        except ValueError:
            raise ValueError("date must be in DD/MM/YY format with numeric values")
        return v
    
    @field_validator("time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time is in HH:MM format."""
        if not v:
            raise ValueError("time is required")
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("time must be in HH:MM format")
        try:
            hour, minute = parts
            hour_int = int(hour)
            minute_int = int(minute)
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError
        except ValueError:
            raise ValueError("time must be in HH:MM format with valid hour (0-23) and minute (0-59)")
        return v


class ReferenceRangeModel(BaseModel):
    """
    Model for reference range documents.
    
    Schema:
    {
        "_id": "MongoDB unique ID",
        "card_id": "The card_id of the patient",
        "category": "Reference",
        "test_name": "Exact test name from source",
        "material": "The specimen type (e.g., Venous Blood, Urine, Pleural Fluid)",
        "low_value": Number or null,
        "high_value": Number or null,
        "units": "e.g., mg/dL, mmol/L"
    }
    """
    
    card_id: str = Field(..., description="The id of the card this reference range is associated with")
    category: Literal["Reference"] = Field(default="Reference")
    test_name: str = Field(..., description="The exact test name as it appears in the original source")
    material: str = Field(..., description="The specimen type (e.g., Venous Blood, Urine, Pleural Fluid)")
    low_value: Optional[Union[int, float]] = Field(default=None, description="Lower bound of normal range")
    high_value: Optional[Union[int, float]] = Field(default=None, description="Upper bound of normal range")
    units: str = Field(..., description="Units of measurement (e.g., mg/dL, mmol/L, g/g)")
    
    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Validate that card_id is a valid ObjectId string."""
        try:
            ObjectId(v)
            return v
        except Exception as e:
            raise ValueError(f"card_id must be a valid ObjectId string: {e}")
    
    @field_validator("test_name")
    @classmethod
    def validate_test_name(cls, v: str) -> str:
        """Ensure test_name is not empty."""
        if not v or not v.strip():
            raise ValueError("test_name is required")
        return v.strip()
    
    @field_validator("material")
    @classmethod
    def validate_material(cls, v: str) -> str:
        """Ensure material is not empty."""
        if not v or not v.strip():
            raise ValueError("material is required")
        return v.strip()
    
    @field_validator("units")
    @classmethod
    def validate_units(cls, v: str) -> str:
        """Ensure units is not empty."""
        if not v or not v.strip():
            raise ValueError("units is required")
        return v.strip()


class CultureOrganism(BaseModel):
    """Model for a single organism in a culture."""
    name: str = Field(..., description="Organism name with quantifier (e.g., 'E. Coli ++')")
    sensitivities: Dict[str, Literal["S", "R", "I"]] = Field(
        default_factory=dict,
        description="Dictionary of antibiotic names to sensitivity (S, R, or I)"
    )


class MicrobiologyModel(BaseModel):
    """
    Model for microbiology culture and sensitivity reports.
    
    Schema:
    {
        "_id": "MongoDB unique ID",
        "card_id": "The card_id of the patient",
        "category": "Microbiology",
        "date": "DD/MM/YY format with padding removed",
        "time": "HH:MM",
        "timestamp": "MongoDB timestamp",
        "material": "Specimen type (e.g., Sputum, Rectal Swab)",
        "gram_stain": "Full description or null",
        "culture": [List of CultureOrganism objects]
    }
    """
    
    card_id: str = Field(..., description="The card_id of the patient this document belongs to")
    category: Literal["Microbiology"] = Field(default="Microbiology")
    date: str = Field(..., description="DD/MM/YY format with padding removed")
    time: str = Field(..., description="HH:MM format")
    timestamp: datetime = Field(..., description="MongoDB timestamp")
    material: str = Field(..., description="Specimen type (e.g., Sputum, Rectal Swab, Pleural Fluid)")
    gram_stain: Optional[str] = Field(default=None, description="Full description of gram stain")
    culture: List[CultureOrganism] = Field(default_factory=list, description="List of cultured organisms")
    
    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Validate that card_id is a valid ObjectId string."""
        try:
            ObjectId(v)
            return v
        except Exception as e:
            raise ValueError(f"card_id must be a valid ObjectId string: {e}")
    
    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Validate date is in DD/MM/YY format."""
        if not v:
            raise ValueError("date is required")
        parts = v.split("/")
        if len(parts) != 3:
            raise ValueError("date must be in DD/MM/YY format")
        try:
            day, month, year = parts
            int(day)
            int(month)
            int(year)
        except ValueError:
            raise ValueError("date must be in DD/MM/YY format with numeric values")
        return v
    
    @field_validator("time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time is in HH:MM format."""
        if not v:
            raise ValueError("time is required")
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("time must be in HH:MM format")
        try:
            hour, minute = parts
            hour_int = int(hour)
            minute_int = int(minute)
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError
        except ValueError:
            raise ValueError("time must be in HH:MM format with valid hour (0-23) and minute (0-59)")
        return v


class PathologyModel(BaseModel):
    """
    Model for pathology (histopathology) reports.
    
    Schema:
    {
        "_id": "MongoDB unique ID",
        "card_id": "The card_id of the patient",
        "category": "Pathology",
        "date": "DD/MM/YY format with padding removed",
        "time": "HH:MM",
        "timestamp": "MongoDB timestamp",
        "specimen": "Anatomical site",
        "clinical_data": "Physician's indication/history",
        "macroscopic": "Full text of gross description",
        "microscopic": "Full text of microscopic findings",
        "diagnosis": "Final pathological diagnosis"
    }
    """
    
    card_id: str = Field(..., description="The card_id of the patient this document belongs to")
    category: Literal["Pathology"] = Field(default="Pathology")
    date: str = Field(..., description="DD/MM/YY format with padding removed")
    time: str = Field(..., description="HH:MM format")
    timestamp: datetime = Field(..., description="MongoDB timestamp")
    specimen: str = Field(..., description="Anatomical site (e.g., Left Pleural Fluid, Stomach Antrum)")
    clinical_data: Optional[str] = Field(default=None, description="Physician's indication/history")
    macroscopic: Optional[str] = Field(default=None, description="Full text of gross description")
    microscopic: Optional[str] = Field(default=None, description="Full text of microscopic findings")
    diagnosis: Optional[str] = Field(default=None, description="Final pathological diagnosis")
    
    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Validate that card_id is a valid ObjectId string."""
        try:
            ObjectId(v)
            return v
        except Exception as e:
            raise ValueError(f"card_id must be a valid ObjectId string: {e}")
    
    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Validate date is in DD/MM/YY format."""
        if not v:
            raise ValueError("date is required")
        parts = v.split("/")
        if len(parts) != 3:
            raise ValueError("date must be in DD/MM/YY format")
        try:
            day, month, year = parts
            int(day)
            int(month)
            int(year)
        except ValueError:
            raise ValueError("date must be in DD/MM/YY format with numeric values")
        return v
    
    @field_validator("time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time is in HH:MM format."""
        if not v:
            raise ValueError("time is required")
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("time must be in HH:MM format")
        try:
            hour, minute = parts
            hour_int = int(hour)
            minute_int = int(minute)
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError
        except ValueError:
            raise ValueError("time must be in HH:MM format with valid hour (0-23) and minute (0-59)")
        return v


class ImagingModel(BaseModel):
    """
    Model for imaging reports (CT, MRI, PET-CT, Ultrasound, etc.).
    
    Schema:
    {
        "_id": "MongoDB unique ID",
        "card_id": "The card_id of the patient",
        "category": "Imaging",
        "date": "DD/MM/YY format with padding removed",
        "time": "HH:MM",
        "timestamp": "MongoDB timestamp",
        "exam_type": "Modality and body part",
        "indication": "Reason for exam",
        "comparison": "Previous studies mentioned",
        "findings": {"Organ_System_Name": "Detailed findings"},
        "summary": "The Impression or Conclusion section"
    }
    """
    
    card_id: str = Field(..., description="The card_id of the patient this document belongs to")
    category: Literal["Imaging"] = Field(default="Imaging")
    date: str = Field(..., description="DD/MM/YY format with padding removed")
    time: str = Field(..., description="HH:MM format")
    timestamp: datetime = Field(..., description="MongoDB timestamp")
    exam_type: str = Field(..., description="Modality and body part (e.g., CT Chest w/ Contrast)")
    indication: Optional[str] = Field(default=None, description="Reason for exam")
    comparison: Optional[str] = Field(default=None, description="Previous studies mentioned")
    findings: Dict[str, str] = Field(default_factory=dict, description="Dynamic keys for organ systems")
    summary: Optional[str] = Field(default=None, description="The Impression or Conclusion section text")
    
    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Validate that card_id is a valid ObjectId string."""
        try:
            ObjectId(v)
            return v
        except Exception as e:
            raise ValueError(f"card_id must be a valid ObjectId string: {e}")
    
    @field_validator("date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Validate date is in DD/MM/YY format."""
        if not v:
            raise ValueError("date is required")
        parts = v.split("/")
        if len(parts) != 3:
            raise ValueError("date must be in DD/MM/YY format")
        try:
            day, month, year = parts
            int(day)
            int(month)
            int(year)
        except ValueError:
            raise ValueError("date must be in DD/MM/YY format with numeric values")
        return v
    
    @field_validator("time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time is in HH:MM format."""
        if not v:
            raise ValueError("time is required")
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError("time must be in HH:MM format")
        try:
            hour, minute = parts
            hour_int = int(hour)
            minute_int = int(minute)
            if not (0 <= hour_int <= 23 and 0 <= minute_int <= 59):
                raise ValueError
        except ValueError:
            raise ValueError("time must be in HH:MM format with valid hour (0-23) and minute (0-59)")
        return v


# Union type for all lab document models
LabDocumentModel = Union[
    QuantitativeLabModel,
    ReferenceRangeModel,
    MicrobiologyModel,
    PathologyModel,
    ImagingModel
]


class PendingIngestion(BaseModel):
    """
    Model for staging email data awaiting user approval.
    
    This model represents a pending ingestion object that holds email content
    (text chunks and PDF attachments) until the user approves or discards it.
    
    Schema:
    {
        "_id": "MongoDB unique ID",
        "card_id": "ObjectId of the patient card",
        "email_uid": "Unique identifier from email server",
        "sender": "Email address of sender (e.g., lab@hospital.com)",
        "received_at": "ISO datetime when email was received",
        "created_new_card": true/false,  // True if "Patient X" triggered creation
        "clean_body_chunks": ["chunk1", "chunk2", ...],  // Text sections
        "has_pdf": true/false,
        "pdf_filename": "lab_results.pdf",  // Optional
        "pdf_data": b"...",  // Binary PDF content, Optional
        "status": "waiting_approval"  // or "processing", "completed"
    }
    """
    
    card_id: str = Field(..., description="The ObjectId of the patient card this ingestion belongs to")
    email_uid: str = Field(..., description="Unique identifier from the email server to prevent re-processing")
    sender: str = Field(..., description="Email address of the sender")
    received_at: datetime = Field(default_factory=datetime.utcnow, description="When the email was received")
    created_new_card: bool = Field(..., description="True if 'Patient X' triggered creation of a new card")
    clean_body_chunks: List[str] = Field(default_factory=list, description="Text chunks after cleaning and splitting")
    has_pdf: bool = Field(default=False, description="Whether a PDF attachment was included")
    pdf_filename: Optional[str] = Field(default=None, description="Original filename of the PDF attachment")
    pdf_data: Optional[bytes] = Field(default=None, description="Binary content of the PDF attachment")
    status: str = Field(default="waiting_approval", description="Current status: waiting_approval, processing, or completed")
    
    @field_validator("card_id")
    @classmethod
    def validate_card_id(cls, v: str) -> str:
        """Validate that card_id is a valid ObjectId string."""
        try:
            ObjectId(v)
            return v
        except Exception as e:
            raise ValueError(f"card_id must be a valid ObjectId string: {e}")
    
    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """Ensure status is one of the allowed values."""
        allowed = {"waiting_approval", "processing", "completed"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v
