"""
MongoDB Database Setup for Clinical Data Repository
Initializes collections with JSON Schema validation and indexes.
"""

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid, OperationFailure
from typing import Optional


def create_observations_validator() -> dict:
    """
    Creates the $jsonSchema validator for the observations collection.
    Enforces FHIR R4 structure with polymorphic value handling.
    """
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["resourceType", "status", "code", "subject"],
            "properties": {
                "resourceType": {
                    "enum": ["Observation"],
                    "description": "Must be 'Observation'"
                },
                "status": {
                    "enum": ["registered", "preliminary", "final", "amended", "corrected", "cancelled"],
                    "description": "The status of the result value."
                },
                "code": {
                    "bsonType": "object",
                    "required": ["coding"],
                    "properties": {
                        "coding": {
                            "bsonType": "array",
                            "minItems": 1,
                            "items": {
                                "bsonType": "object",
                                "required": ["system", "code"],
                                "properties": {
                                    "system": {"bsonType": "string"},
                                    "code": {"bsonType": "string"},
                                    "display": {"bsonType": "string"},
                                    "version": {"bsonType": "string"}
                                }
                            }
                        },
                        "text": {"bsonType": "string"}
                    }
                },
                "subject": {
                    "bsonType": "object",
                    "required": ["reference"],
                    "properties": {
                        "reference": {"bsonType": "string"},
                        "type": {"bsonType": "string"},
                        "display": {"bsonType": "string"}
                    }
                },
                "effectiveDateTime": {
                    "bsonType": ["date", "string"],
                    "description": "Clinically relevant time for observation"
                },
                "issued": {
                    "bsonType": ["date", "string"],
                    "description": "Date/Time this version was made available"
                },
                "category": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "coding": {"bsonType": "array"},
                            "text": {"bsonType": "string"}
                        }
                    }
                },
                # Polymorphic Value Handling via oneOf
                "valueQuantity": {
                    "bsonType": "object",
                    "required": ["value"],
                    "properties": {
                        "value": {"bsonType": "double"},
                        "unit": {"bsonType": "string"},
                        "system": {"bsonType": "string"},
                        "code": {"bsonType": "string"}
                    }
                },
                "valueCodeableConcept": {
                    "bsonType": "object",
                    "required": ["coding"],
                    "properties": {
                        "coding": {
                            "bsonType": "array",
                            "minItems": 1,
                            "items": {
                                "bsonType": "object",
                                "required": ["system", "code"],
                                "properties": {
                                    "system": {"bsonType": "string"},
                                    "code": {"bsonType": "string"},
                                    "display": {"bsonType": "string"}
                                }
                            }
                        },
                        "text": {"bsonType": "string"}
                    }
                },
                "valueString": {
                    "bsonType": "string",
                    "description": "Narrative value for text-based observations"
                },
                "valueRange": {
                    "bsonType": "object",
                    "properties": {
                        "low": {"bsonType": "object"},
                        "high": {"bsonType": "object"},
                        "text": {"bsonType": "string"}
                    }
                },
                # Component Array for Panels (Antibiograms, BP)
                "component": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["code"],
                        "properties": {
                            "code": {
                                "bsonType": "object",
                                "required": ["coding"],
                                "properties": {
                                    "coding": {
                                        "bsonType": "array",
                                        "minItems": 1,
                                        "items": {
                                            "bsonType": "object",
                                            "required": ["system", "code"],
                                            "properties": {
                                                "system": {"bsonType": "string"},
                                                "code": {"bsonType": "string"},
                                                "display": {"bsonType": "string"}
                                            }
                                        }
                                    }
                                }
                            },
                            "valueQuantity": {"bsonType": "object"},
                            "valueCodeableConcept": {"bsonType": "object"},
                            "valueString": {"bsonType": "string"},
                            "referenceRange": {"bsonType": "array"},
                            "interpretation": {"bsonType": "array"}
                        }
                    }
                },
                # Extension for unknown OCR fields
                "extension": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "required": ["url"],
                        "properties": {
                            "url": {"bsonType": "string"},
                            "valueString": {"bsonType": "string"},
                            "valueCode": {"bsonType": "string"},
                            "valueQuantity": {"bsonType": "object"}
                        }
                    }
                },
                "interpretation": {
                    "bsonType": "array",
                    "items": {"bsonType": "object"}
                },
                "note": {
                    "bsonType": "array",
                    "items": {"bsonType": "object"}
                },
                "method": {
                    "bsonType": "object"
                },
                "specimen": {
                    "bsonType": "object",
                    "properties": {
                        "reference": {"bsonType": "string"},
                        "type": {"bsonType": "string"},
                        "display": {"bsonType": "string"}
                    }
                }
            },
            # Polymorphic value constraint: must have at least one value type OR component array
            "oneOf": [
                {"required": ["valueQuantity"]},
                {"required": ["valueCodeableConcept"]},
                {"required": ["valueString"]},
                {"required": ["valueRange"]},
                {"required": ["component"]},  # Component-only observation (panels)
                {"required": ["dataAbsentReason"]}  # Missing value case with reason
            ]
        }
    }


def create_diagnostic_reports_validator() -> dict:
    """
    Creates the $jsonSchema validator for the diagnostic_reports collection.
    """
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["resourceType", "status", "code", "subject"],
            "properties": {
                "resourceType": {
                    "enum": ["DiagnosticReport"],
                    "description": "Must be 'DiagnosticReport'"
                },
                "status": {
                    "enum": ["registered", "partial", "preliminary", "final", "amended", "corrected", "cancelled"],
                    "description": "The status of the diagnostic report."
                },
                "identifier": {
                    "bsonType": "array",
                    "items": {"bsonType": "object"}
                },
                "category": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "coding": {"bsonType": "array"},
                            "text": {"bsonType": "string"}
                        }
                    }
                },
                "code": {
                    "bsonType": "object",
                    "required": ["coding"],
                    "properties": {
                        "coding": {
                            "bsonType": "array",
                            "minItems": 1,
                            "items": {
                                "bsonType": "object",
                                "required": ["system", "code"],
                                "properties": {
                                    "system": {"bsonType": "string"},
                                    "code": {"bsonType": "string"},
                                    "display": {"bsonType": "string"}
                                }
                            }
                        },
                        "text": {"bsonType": "string"}
                    }
                },
                "subject": {
                    "bsonType": "object",
                    "required": ["reference"],
                    "properties": {
                        "reference": {"bsonType": "string"},
                        "type": {"bsonType": "string"},
                        "display": {"bsonType": "string"}
                    }
                },
                "effectiveDateTime": {
                    "bsonType": ["date", "string"]
                },
                "issued": {
                    "bsonType": ["date", "string"]
                },
                "performer": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "reference": {"bsonType": "string"},
                            "type": {"bsonType": "string"},
                            "display": {"bsonType": "string"}
                        }
                    }
                },
                "result": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "reference": {"bsonType": "string"},
                            "type": {"bsonType": "string"},
                            "display": {"bsonType": "string"}
                        }
                    },
                    "description": "References to Observation resources"
                },
                "specimen": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "reference": {"bsonType": "string"},
                            "type": {"bsonType": "string"},
                            "display": {"bsonType": "string"}
                        }
                    }
                },
                "conclusion": {
                    "bsonType": "string"
                },
                "presentedForm": {
                    "bsonType": "array",
                    "items": {"bsonType": "object"}
                }
            }
        }
    }


def create_patients_validator() -> dict:
    """
    Creates the $jsonSchema validator for the patients collection.
    Basic FHIR Patient resource structure.
    """
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["resourceType"],
            "properties": {
                "resourceType": {
                    "enum": ["Patient"],
                    "description": "Must be 'Patient'"
                },
                "identifier": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "system": {"bsonType": "string"},
                            "value": {"bsonType": "string"},
                            "type": {"bsonType": "object"}
                        }
                    }
                },
                "active": {"bsonType": "bool"},
                "name": {
                    "bsonType": "array",
                    "items": {
                        "bsonType": "object",
                        "properties": {
                            "use": {"bsonType": "string"},
                            "family": {"bsonType": "string"},
                            "given": {"bsonType": "array", "items": {"bsonType": "string"}}
                        }
                    }
                },
                "telecom": {
                    "bsonType": "array",
                    "items": {"bsonType": "object"}
                },
                "gender": {"bsonType": "string"},
                "birthDate": {"bsonType": "string"},
                "deceasedBoolean": {"bsonType": "bool"},
                "deceasedDateTime": {"bsonType": ["date", "string"]},
                "address": {
                    "bsonType": "array",
                    "items": {"bsonType": "object"}
                },
                "maritalStatus": {"bsonType": "object"},
                "extension": {"bsonType": "array"}
            }
        }
    }


def init_db(uri: str, db_name: str) -> None:
    """
    Initialize the MongoDB database with collections, validators, and indexes.

    Args:
        uri: MongoDB connection URI (e.g., "mongodb://localhost:27017")
        db_name: Name of the database to initialize
    """
    client = MongoClient(uri)
    db = client[db_name]

    print(f"Initializing database: {db_name}")

    # -------------------------------------------------------------------------
    # Create Collections with Validators
    # -------------------------------------------------------------------------

    # Observations collection with strict FHIR validation
    try:
        db.create_collection(
            "observations",
            validator=create_observations_validator(),
            validationLevel="strict",
            validationAction="error"
        )
        print("✓ Created 'observations' collection with FHIR validator")
    except CollectionInvalid:
        print("⚠ 'observations' collection already exists, skipping creation")
        # Optionally update validator for existing collection
        try:
            db.command({
                "collMod": "observations",
                "validator": create_observations_validator(),
                "validationLevel": "strict",
                "validationAction": "error"
            })
            print("  Updated validator for existing 'observations' collection")
        except OperationFailure as e:
            print(f"  Could not update validator: {e}")

    # Diagnostic Reports collection
    try:
        db.create_collection(
            "diagnostic_reports",
            validator=create_diagnostic_reports_validator(),
            validationLevel="strict",
            validationAction="error"
        )
        print("✓ Created 'diagnostic_reports' collection with FHIR validator")
    except CollectionInvalid:
        print("⚠ 'diagnostic_reports' collection already exists, skipping creation")
        try:
            db.command({
                "collMod": "diagnostic_reports",
                "validator": create_diagnostic_reports_validator(),
                "validationLevel": "strict",
                "validationAction": "error"
            })
            print("  Updated validator for existing 'diagnostic_reports' collection")
        except OperationFailure as e:
            print(f"  Could not update validator: {e}")

    # Patients collection
    try:
        db.create_collection(
            "patients",
            validator=create_patients_validator(),
            validationLevel="strict",
            validationAction="error"
        )
        print("✓ Created 'patients' collection with FHIR validator")
    except CollectionInvalid:
        print("⚠ 'patients' collection already exists, skipping creation")
        try:
            db.command({
                "collMod": "patients",
                "validator": create_patients_validator(),
                "validationLevel": "strict",
                "validationAction": "error"
            })
            print("  Updated validator for existing 'patients' collection")
        except OperationFailure as e:
            print(f"  Could not update validator: {e}")

    # -------------------------------------------------------------------------
    # Create Indexes
    # -------------------------------------------------------------------------

    # Observations indexes
    observations = db["observations"]

    # Compound index: Patient history retrieval (subject + date)
    observations.create_index(
        [("subject.reference", ASCENDING), ("effectiveDateTime", DESCENDING)],
        name="idx_subject_date",
        background=True
    )
    print("✓ Created compound index on observations: subject.reference + effectiveDateTime")

    # Multikey index: Search for specific analytes in panels (e.g., Ampicillin in antibiogram)
    observations.create_index(
        [("component.code.coding.code", ASCENDING)],
        name="idx_component_code",
        background=True
    )
    print("✓ Created multikey index on observations: component.code.coding.code")

    # Index for LOINC code searches on the observation itself
    observations.create_index(
        [("code.coding.code", ASCENDING)],
        name="idx_observation_code",
        background=True
    )
    print("✓ Created index on observations: code.coding.code")

    # Wildcard index: Query any field in extension array (extensibility requirement)
    try:
        observations.create_index(
            {"extension.$**": ASCENDING},
            name="idx_extension_wildcard",
            background=True
        )
        print("✓ Created wildcard index on observations: extension.$**")
    except OperationFailure as e:
        print(f"⚠ Could not create wildcard index (may require MongoDB 4.2+): {e}")

    # Index for status filtering
    observations.create_index(
        [("status", ASCENDING)],
        name="idx_status",
        background=True
    )
    print("✓ Created index on observations: status")

    # Diagnostic Reports indexes
    diagnostic_reports = db["diagnostic_reports"]

    # Compound index: Patient diagnostic history
    diagnostic_reports.create_index(
        [("subject.reference", ASCENDING), ("effectiveDateTime", DESCENDING)],
        name="idx_subject_date",
        background=True
    )
    print("✓ Created compound index on diagnostic_reports: subject.reference + effectiveDateTime")

    # Index for report code searches
    diagnostic_reports.create_index(
        [("code.coding.code", ASCENDING)],
        name="idx_report_code",
        background=True
    )
    print("✓ Created index on diagnostic_reports: code.coding.code")

    # Index for status filtering
    diagnostic_reports.create_index(
        [("status", ASCENDING)],
        name="idx_status",
        background=True
    )
    print("✓ Created index on diagnostic_reports: status")

    # Patients indexes
    patients = db["patients"]

    # Unique index on Medical Record Number (MRN) - assuming first identifier is MRN
    patients.create_index(
        [("identifier.value", ASCENDING), ("identifier.system", ASCENDING)],
        name="idx_identifier",
        unique=True,
        sparse=True,
        background=True
    )
    print("✓ Created unique sparse index on patients: identifier.value + identifier.system")

    # Index for patient name searches
    patients.create_index(
        [("name.family", ASCENDING), ("name.given", ASCENDING)],
        name="idx_name",
        background=True
    )
    print("✓ Created index on patients: name.family + name.given")

    print(f"\n✅ Database '{db_name}' initialization complete!")
    client.close()


if __name__ == "__main__":
    # Example usage
    import os

    # Get MongoDB URI from environment or use default
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    DB_NAME = os.getenv("DB_NAME", "clinical_data_repository")

    init_db(MONGO_URI, DB_NAME)
