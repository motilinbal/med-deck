"""
Pydantic models for medical lab data validation.

This module defines validation models for all document types stored in the labs collection:
- QuantitativeLabModel: For numerical lab test results
- ReferenceRangeModel: For reference/normal ranges
- MicrobiologyModel: For culture and sensitivity reports
- PathologyModel: For histopathology reports
- ImagingModel: For radiology and imaging reports
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator
from bson.objectid import ObjectId


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
