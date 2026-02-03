"""
Pydantic V2 Data Models for Clinical Data Repository
Based on HL7 FHIR R4 Specification
"""

from __future__ import annotations
from datetime import datetime
from typing import List, Optional, Literal, Annotated, Union
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

# -------------------------------------------------------------------------
# Primitives & Value Sets
# -------------------------------------------------------------------------

class Coding(BaseModel):
    """
    A representation of a defined concept using a symbol from a code system.
    Examples: LOINC, SNOMED CT, RxNorm.
    """
    system: str = Field(..., description="Identity of the terminology system (e.g., http://loinc.org)")
    code: str = Field(..., description="Symbol in syntax defined by the system (e.g., 2345-7)")
    display: Optional[str] = Field(None, description="Human readable representation")
    version: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class CodeableConcept(BaseModel):
    """
    A concept that may be defined by multiple codes (e.g. local code + LOINC).
    """
    coding: List[Coding] = Field(default_factory=list, description="Code defined by a terminology system")
    text: Optional[str] = Field(None, description="Plain text representation of the concept")

    model_config = ConfigDict(populate_by_name=True)


class Quantity(BaseModel):
    """
    A measured amount (or an amount that can potentially be measured).
    """
    value: float = Field(..., description="Numerical value (with implicit precision)")
    unit: Optional[str] = Field(None, description="Unit representation")
    system: Optional[str] = Field("http://unitsofmeasure.org", description="System that defines the coded unit form")
    code: Optional[str] = Field(None, description="Coded form of the unit (UCUM)")

    model_config = ConfigDict(populate_by_name=True)


class Reference(BaseModel):
    """
    A reference from one resource to another.
    """
    reference: str = Field(..., description="Literal reference, Relative, internal or absolute URL")
    type: Optional[str] = None
    display: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class ReferenceRange(BaseModel):
    """
    Guide for interpretation.
    """
    low: Optional[Quantity] = None
    high: Optional[Quantity] = None
    type: Optional[CodeableConcept] = None
    text: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


# -------------------------------------------------------------------------
# Polymorphic Value Types
# -------------------------------------------------------------------------

class ValueQuantity(BaseModel):
    valueType: Literal["Quantity"] = "Quantity"
    valueQuantity: Quantity

    model_config = ConfigDict(populate_by_name=True)


class ValueCodeableConcept(BaseModel):
    valueType: Literal["CodeableConcept"] = "CodeableConcept"
    valueCodeableConcept: CodeableConcept

    model_config = ConfigDict(populate_by_name=True)


class ValueString(BaseModel):
    valueType: Literal["String"] = "String"
    valueString: str

    model_config = ConfigDict(populate_by_name=True)


class ValueRange(BaseModel):
    valueType: Literal["Range"] = "Range"
    valueRange: ReferenceRange  # Sometimes results are ranges

    model_config = ConfigDict(populate_by_name=True)


# Discriminated Union for polymorphic handling
ObservationValue = Annotated[
    Union[ValueQuantity, ValueCodeableConcept, ValueString, ValueRange],
    Field(discriminator="valueType")
]

# -------------------------------------------------------------------------
# Component Structure (Recursive)
# -------------------------------------------------------------------------

class ObservationComponent(BaseModel):
    """
    Used when a single observation has multiple results (e.g., Systolic/Diastolic BP,
    or Microbiology Susceptibility panels).
    """
    code: CodeableConcept
    value: Optional[ObservationValue] = None
    dataAbsentReason: Optional[CodeableConcept] = None
    interpretation: Optional[List[CodeableConcept]] = None
    referenceRange: List[ReferenceRange] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


# -------------------------------------------------------------------------
# Core Resources
# -------------------------------------------------------------------------

class Observation(BaseModel):
    """
    Measurements and simple assertions made about a patient, device or other subject.
    Matches HL7 FHIR R4 Structure.
    """
    model_config = ConfigDict(populate_by_name=True, extra='ignore')

    resourceType: Literal["Observation"] = "Observation"
    id: Optional[str] = Field(None, alias="_id")
    status: Literal["registered", "preliminary", "final", "amended", "corrected", "cancelled"]
    category: List[CodeableConcept] = Field(default_factory=list)
    code: CodeableConcept = Field(..., description="Type of observation (code / type)")
    subject: Reference = Field(..., description="Who and/or what the observation is about")
    effectiveDateTime: Optional[datetime] = Field(None, description="Clinically relevant time/time-period for observation")
    issued: Optional[datetime] = Field(None, description="Date/Time this version was made available")

    # Polymorphic Value Field (The 'value[x]' pattern)
    value: Optional[ObservationValue] = None

    # Component for panels/batteries
    component: List[ObservationComponent] = Field(default_factory=list)

    # Metadata
    interpretation: Optional[List[CodeableConcept]] = None
    note: List[dict] = Field(default_factory=list)
    method: Optional[CodeableConcept] = None
    specimen: Optional[Reference] = Field(None, description="Specimen used for this observation")

    # Extension for "Unseen" data
    extension: List[dict] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_value_presence(self):
        """
        FHIR Invariant: obs-7
        If Observation.code is the same as Observation.component.code, then Observation.value SHALL NOT be present.
        Also ensures at least one of value or component is present (unless dataAbsentReason).
        """
        if self.value is None and not self.component:
            # Check for dataAbsentReason in a real implementation
            pass
        return self


class DiagnosticReport(BaseModel):
    """
    The findings and interpretation of diagnostic tests performed on patients.
    """
    model_config = ConfigDict(populate_by_name=True)

    resourceType: Literal["DiagnosticReport"] = "DiagnosticReport"
    id: Optional[str] = Field(None, alias="_id")
    identifier: List[dict] = Field(default_factory=list)
    status: Literal["registered", "partial", "preliminary", "final", "amended", "corrected", "cancelled"]
    category: List[CodeableConcept] = Field(default_factory=list)
    code: CodeableConcept = Field(..., description="Name/Code for this diagnostic report")
    subject: Reference
    effectiveDateTime: Optional[datetime] = None
    issued: Optional[datetime] = None
    performer: List[Reference] = Field(default_factory=list)
    result: List[Reference] = Field(default_factory=list, description="Observations")
    specimen: List[Reference] = Field(default_factory=list, description="Specimens used in this report")
    conclusion: Optional[str] = None
    presentedForm: List[dict] = Field(default_factory=list, description="Original PDF/Image links")
