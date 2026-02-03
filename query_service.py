"""
Clinical Data Query Service
Provides granular access to clinical data with optimized MongoDB aggregation pipelines.
"""

import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Union
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import Collection
from pymongo.database import Database

from models import DiagnosticReport, Observation


class ClinicalDataService:
    """
    Service class for querying clinical data from MongoDB.
    Encapsulates complex aggregation pipelines and returns clean Python objects.
    """
    
    def __init__(self, mongo_uri: str = None, db_name: str = None):
        """
        Initialize the ClinicalDataService.
        
        Args:
            mongo_uri: MongoDB connection URI (defaults to env var or localhost)
            db_name: Database name (defaults to env var or default DB)
        """
        self.mongo_uri = mongo_uri or os.getenv("MONGO_URI", "mongodb://localhost:27017")
        self.db_name = db_name or os.getenv("DB_NAME", "clinical_data_repository")
        self.client: Optional[MongoClient] = None
        self.db: Optional[Database] = None
        
    def connect(self):
        """Establish connection to MongoDB."""
        if not self.client:
            self.client = MongoClient(self.mongo_uri)
            self.db = self.client[self.db_name]
        return self
    
    def close(self):
        """Close MongoDB connection."""
        if self.client:
            self.client.close()
            self.client = None
            self.db = None
    
    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
        return False
    
    # -------------------------------------------------------------------------
    # Patient History Queries
    # -------------------------------------------------------------------------
    
    def get_patient_history(
        self, 
        patient_id: str, 
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 100
    ) -> List[DiagnosticReport]:
        """
        Retrieve all DiagnosticReports for a patient, sorted by date (newest first).
        Uses the compound index: subject.reference: 1, effectiveDateTime: -1
        
        Args:
            patient_id: The patient identifier (e.g., "Patient/123")
            start_date: Optional filter for reports after this date
            end_date: Optional filter for reports before this date
            limit: Maximum number of reports to return
            
        Returns:
            List of DiagnosticReport Pydantic models
        """
        self.connect()
        collection: Collection = self.db.diagnostic_reports
        
        # Build query
        query = {"subject.reference": patient_id}
        
        if start_date or end_date:
            date_filter = {}
            if start_date:
                date_filter["$gte"] = start_date
            if end_date:
                date_filter["$lte"] = end_date
            query["effectiveDateTime"] = date_filter
        
        # Execute query using the compound index
        cursor = collection.find(query).sort(
            [("subject.reference", ASCENDING), ("effectiveDateTime", DESCENDING)]
        ).limit(limit)
        
        # Convert to Pydantic models
        reports = []
        for doc in cursor:
            # Convert ObjectId to string for JSON serialization
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            try:
                report = DiagnosticReport.model_validate(doc)
                reports.append(report)
            except Exception as e:
                # Log error but continue processing other documents
                print(f"Warning: Failed to validate report {doc.get('_id')}: {e}")
        
        return reports
    
    # -------------------------------------------------------------------------
    # Lab Trend Analysis (Time-Series)
    # -------------------------------------------------------------------------
    
    def get_lab_trend(
        self,
        patient_id: str,
        loinc_code: str
    ) -> List[Dict[str, Any]]:
        """
        Retrieve a time-series of values for a specific lab test (by LOINC code).
        Handles both root-level values and component-level values (panels).
        
        Uses an aggregation pipeline to:
        1. Match patient
        2. Unwind component array (preserve root observations)
        3. Match LOINC code at either root or component level
        4. Project datetime and value
        
        Args:
            patient_id: The patient identifier (e.g., "Patient/123")
            loinc_code: The LOINC code to search for (e.g., "2345-7" for Glucose)
            
        Returns:
            List of dictionaries with 'date', 'value', 'unit', and 'source' keys
        """
        self.connect()
        collection: Collection = self.db.observations
        
        pipeline = [
            # Stage 1: Match patient
            {
                "$match": {
                    "subject.reference": patient_id
                }
            },
            # Stage 2: Unwind component array, preserving observations without components
            {
                "$unwind": {
                    "path": "$component",
                    "preserveNullAndEmptyArrays": True
                }
            },
            # Stage 3: Match LOINC code at root OR component level
            {
                "$match": {
                    "$or": [
                        # Root-level observation matches
                        {"code.coding.code": loinc_code},
                        # Component-level observation matches
                        {"component.code.coding.code": loinc_code}
                    ]
                }
            },
            # Stage 4: Project the fields we need
            {
                "$project": {
                    "_id": 0,
                    "date": "$effectiveDateTime",
                    "observation_id": "$id",
                    # Determine the source (root or component)
                    "source": {
                        "$cond": {
                            "if": {"$eq": ["$component.code.coding.code", loinc_code]},
                            "then": "component",
                            "else": "root"
                        }
                    },
                    # Extract value based on where it came from
                    "value": {
                        "$cond": {
                            "if": {"$eq": ["$component.code.coding.code", loinc_code]},
                            "then": {
                                # Value from component
                                "quantity": "$component.valueQuantity.value",
                                "unit": "$component.valueQuantity.unit",
                                "code": "$component.valueQuantity.code",
                                "concept": "$component.valueCodeableConcept",
                                "string": "$component.valueString"
                            },
                            "else": {
                                # Value from root
                                "quantity": "$valueQuantity.value",
                                "unit": "$valueQuantity.unit",
                                "code": "$valueQuantity.code",
                                "concept": "$valueCodeableConcept",
                                "string": "$valueString"
                            }
                        }
                    },
                    # Include interpretation if available
                    "interpretation": {
                        "$cond": {
                            "if": {"$eq": ["$component.code.coding.code", loinc_code]},
                            "then": "$component.interpretation",
                            "else": "$interpretation"
                        }
                    },
                    # Include reference range
                    "reference_range": {
                        "$cond": {
                            "if": {"$eq": ["$component.code.coding.code", loinc_code]},
                            "then": "$component.referenceRange",
                            "else": "$referenceRange"
                        }
                    }
                }
            },
            # Stage 5: Sort by date
            {
                "$sort": {"date": ASCENDING}
            }
        ]
        
        results = list(collection.aggregate(pipeline))
        
        # Post-process to flatten value structure
        processed_results = []
        for result in results:
            processed = {
                "date": result.get("date"),
                "observation_id": result.get("observation_id"),
                "source": result.get("source"),
                "interpretation": result.get("interpretation"),
                "reference_range": result.get("reference_range")
            }
            
            # Extract the actual value based on type
            value_data = result.get("value", {})
            if value_data.get("quantity") is not None:
                processed["value"] = value_data["quantity"]
                processed["value_type"] = "quantity"
                processed["unit"] = value_data.get("unit")
                processed["unit_code"] = value_data.get("code")
            elif value_data.get("concept"):
                processed["value"] = value_data["concept"]
                processed["value_type"] = "codeable_concept"
            elif value_data.get("string"):
                processed["value"] = value_data["string"]
                processed["value_type"] = "string"
            else:
                processed["value"] = None
                processed["value_type"] = "unknown"
            
            processed_results.append(processed)
        
        return processed_results
    
    # -------------------------------------------------------------------------
    # Abnormal Results Detection
    # -------------------------------------------------------------------------
    
    def find_abnormal_results(
        self,
        patient_id: str,
        interpretation_codes: List[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Find observations with abnormal interpretation codes.
        Searches both root-level and component-level interpretations.
        
        Args:
            patient_id: The patient identifier (e.g., "Patient/123")
            interpretation_codes: List of abnormal codes to search for 
                                  (default: ["H", "L", "A", "HH", "LL", "AA"])
            start_date: Optional filter for results after this date
            end_date: Optional filter for results before this date
            limit: Maximum number of results to return
            
        Returns:
            List of abnormal observation results with details
        """
        self.connect()
        collection: Collection = self.db.observations
        
        # Default abnormal codes
        if interpretation_codes is None:
            interpretation_codes = ["H", "L", "A", "HH", "LL", "AA", "HU", "LU"]
        
        # Build base query
        query = {
            "subject.reference": patient_id,
            "$or": [
                # Root-level interpretation
                {"interpretation.coding.code": {"$in": interpretation_codes}},
                # Component-level interpretation
                {"component.interpretation.coding.code": {"$in": interpretation_codes}}
            ]
        }
        
        # Add date filters
        if start_date or end_date:
            date_filter = {}
            if start_date:
                date_filter["$gte"] = start_date
            if end_date:
                date_filter["$lte"] = end_date
            query["effectiveDateTime"] = date_filter
        
        # Execute query
        cursor = collection.find(query).limit(limit)
        
        results = []
        for doc in cursor:
            # Process each document to identify which parts are abnormal
            abnormal_entries = self._extract_abnormal_entries(doc, interpretation_codes)
            results.extend(abnormal_entries)
        
        return results
    
    def _extract_abnormal_entries(
        self, 
        doc: Dict[str, Any], 
        interpretation_codes: List[str]
    ) -> List[Dict[str, Any]]:
        """Helper to extract abnormal entries from an observation document."""
        entries = []
        base_info = {
            "observation_id": doc.get("id") or str(doc.get("_id")),
            "date": doc.get("effectiveDateTime"),
            "patient_id": doc.get("subject", {}).get("reference")
        }
        
        # Check root-level interpretation
        interpretations = doc.get("interpretation", [])
        for interp in interpretations:
            for coding in interp.get("coding", []):
                if coding.get("code") in interpretation_codes:
                    entry = {
                        **base_info,
                        "level": "root",
                        "test_code": doc.get("code", {}).get("coding", [{}])[0].get("code"),
                        "test_name": doc.get("code", {}).get("coding", [{}])[0].get("display"),
                        "interpretation_code": coding.get("code"),
                        "interpretation_display": coding.get("display"),
                        "value": self._extract_value_summary(doc)
                    }
                    entries.append(entry)
        
        # Check component-level interpretations
        components = doc.get("component", [])
        for comp in components:
            comp_interps = comp.get("interpretation", [])
            for interp in comp_interps:
                for coding in interp.get("coding", []):
                    if coding.get("code") in interpretation_codes:
                        entry = {
                            **base_info,
                            "level": "component",
                            "test_code": comp.get("code", {}).get("coding", [{}])[0].get("code"),
                            "test_name": comp.get("code", {}).get("coding", [{}])[0].get("display"),
                            "interpretation_code": coding.get("code"),
                            "interpretation_display": coding.get("display"),
                            "value": self._extract_component_value_summary(comp)
                        }
                        entries.append(entry)
        
        return entries
    
    def _extract_value_summary(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Extract a summary of the observation value."""
        if "valueQuantity" in doc:
            return {
                "type": "quantity",
                "value": doc["valueQuantity"].get("value"),
                "unit": doc["valueQuantity"].get("unit")
            }
        elif "valueCodeableConcept" in doc:
            return {
                "type": "concept",
                "value": doc["valueCodeableConcept"].get("coding", [{}])[0].get("display")
            }
        elif "valueString" in doc:
            return {"type": "string", "value": doc["valueString"]}
        return {"type": "unknown", "value": None}
    
    def _extract_component_value_summary(self, comp: Dict[str, Any]) -> Dict[str, Any]:
        """Extract a summary of the component value."""
        if "valueQuantity" in comp:
            return {
                "type": "quantity",
                "value": comp["valueQuantity"].get("value"),
                "unit": comp["valueQuantity"].get("unit")
            }
        elif "valueCodeableConcept" in comp:
            return {
                "type": "concept",
                "value": comp["valueCodeableConcept"].get("coding", [{}])[0].get("display")
            }
        elif "valueString" in comp:
            return {"type": "string", "value": comp["valueString"]}
        return {"type": "unknown", "value": None}
    
    # -------------------------------------------------------------------------
    # Dynamic Field Queries (Future-Proofing)
    # -------------------------------------------------------------------------
    
    def query_dynamic_fields(
        self,
        field_name: str,
        value: Any,
        value_key: str = "valueString"
    ) -> List[Dict[str, Any]]:
        """
        Query the extension array using the Wildcard Index.
        This enables searching for fields that weren't explicitly defined in the schema.
        
        Args:
            field_name: The extension URL to search for (e.g., "http://example.org/custom-field")
            value: The value to match
            value_key: The key within the extension that contains the value 
                      (default: "valueString", can also be "valueCode", "valueQuantity")
                      
        Returns:
            List of observations containing matching extensions
        """
        self.connect()
        collection: Collection = self.db.observations
        
        # Query using the wildcard index on extension.$**
        query = {
            "extension": {
                "$elemMatch": {
                    "url": field_name,
                    value_key: value
                }
            }
        }
        
        cursor = collection.find(query)
        
        results = []
        for doc in cursor:
            # Extract just the matching extensions
            matching_extensions = [
                ext for ext in doc.get("extension", [])
                if ext.get("url") == field_name and ext.get(value_key) == value
            ]
            
            result = {
                "observation_id": doc.get("id") or str(doc.get("_id")),
                "patient_id": doc.get("subject", {}).get("reference"),
                "date": doc.get("effectiveDateTime"),
                "test_code": doc.get("code", {}).get("coding", [{}])[0].get("code"),
                "test_name": doc.get("code", {}).get("coding", [{}])[0].get("display"),
                "matched_extensions": matching_extensions
            }
            results.append(result)
        
        return results
    
    def find_by_extension_url(
        self,
        url_pattern: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Find all observations that have a specific extension URL.
        Uses the wildcard index for efficient querying.
        
        Args:
            url_pattern: The extension URL to search for (supports partial matching)
            limit: Maximum number of results
            
        Returns:
            List of observations with matching extensions
        """
        self.connect()
        collection: Collection = self.db.observations
        
        # For exact match
        if not url_pattern.startswith("*") and not url_pattern.endswith("*"):
            query = {"extension.url": url_pattern}
        else:
            # For pattern matching (requires text index or regex)
            query = {"extension.url": {"$regex": url_pattern.replace("*", ".*")}}
        
        cursor = collection.find(query).limit(limit)
        
        results = []
        for doc in cursor:
            matching_extensions = [
                ext for ext in doc.get("extension", [])
                if url_pattern.replace("*", "") in ext.get("url", "")
            ]
            
            result = {
                "observation_id": doc.get("id") or str(doc.get("_id")),
                "patient_id": doc.get("subject", {}).get("reference"),
                "date": doc.get("effectiveDateTime"),
                "test_code": doc.get("code", {}).get("coding", [{}])[0].get("code"),
                "extensions": matching_extensions
            }
            results.append(result)
        
        return results
    
    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------
    
    def get_observation_by_id(self, observation_id: str) -> Optional[Observation]:
        """Retrieve a single observation by its ID."""
        self.connect()
        collection: Collection = self.db.observations
        
        doc = collection.find_one({"id": observation_id})
        if doc:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            return Observation.model_validate(doc)
        return None
    
    def get_report_by_id(self, report_id: str) -> Optional[DiagnosticReport]:
        """Retrieve a single diagnostic report by its ID."""
        self.connect()
        collection: Collection = self.db.diagnostic_reports
        
        doc = collection.find_one({"id": report_id})
        if doc:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            return DiagnosticReport.model_validate(doc)
        return None
    
    def search_by_loinc(
        self,
        loinc_code: str,
        patient_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Observation]:
        """
        Search for observations by LOINC code.
        Optionally filter by patient.
        """
        self.connect()
        collection: Collection = self.db.observations
        
        query = {"code.coding.code": loinc_code}
        if patient_id:
            query["subject.reference"] = patient_id
        
        cursor = collection.find(query).limit(limit)
        
        observations = []
        for doc in cursor:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
            try:
                obs = Observation.model_validate(doc)
                observations.append(obs)
            except Exception as e:
                print(f"Warning: Failed to validate observation {doc.get('_id')}: {e}")
        
        return observations

    # -------------------------------------------------------------------------
    # Data Deletion (Administrative)
    # -------------------------------------------------------------------------
    
    def delete_patient_data(self, patient_id: str) -> Dict[str, int]:
        """
        Hard delete all clinical data for a specific patient.
        Removes documents from both diagnostic_reports and observations collections.
        
        WARNING: This permanently deletes data. Use with caution.
        Intended for compliance (Right to be Forgotten) or correcting ingestion errors.
        
        Args:
            patient_id: The patient identifier (e.g., "Patient/123")
            
        Returns:
            Dictionary with deletion counts:
            {
                "reports_deleted": int,
                "observations_deleted": int,
                "success": bool,
                "error": str | None
            }
        """
        self.connect()
        
        result = {
            "reports_deleted": 0,
            "observations_deleted": 0,
            "success": False,
            "error": None
        }
        
        try:
            # Delete from diagnostic_reports collection
            reports_result = self.db.diagnostic_reports.delete_many(
                {"subject.reference": patient_id}
            )
            result["reports_deleted"] = reports_result.deleted_count
            
            # Delete from observations collection
            observations_result = self.db.observations.delete_many(
                {"subject.reference": patient_id}
            )
            result["observations_deleted"] = observations_result.deleted_count
            
            result["success"] = True
            
            print(f"Deleted {result['reports_deleted']} reports and "
                  f"{result['observations_deleted']} observations for {patient_id}")
            
        except Exception as e:
            result["error"] = str(e)
            print(f"Error deleting patient data for {patient_id}: {e}")
        
        return result


# -------------------------------------------------------------------------
# Convenience Functions (Module-level API)
# -------------------------------------------------------------------------

def get_patient_history(patient_id: str, **kwargs) -> List[DiagnosticReport]:
    """Convenience function to get patient history."""
    with ClinicalDataService() as service:
        return service.get_patient_history(patient_id, **kwargs)


def get_lab_trend(patient_id: str, loinc_code: str) -> List[Dict[str, Any]]:
    """Convenience function to get lab trend."""
    with ClinicalDataService() as service:
        return service.get_lab_trend(patient_id, loinc_code)


def find_abnormal_results(patient_id: str, **kwargs) -> List[Dict[str, Any]]:
    """Convenience function to find abnormal results."""
    with ClinicalDataService() as service:
        return service.find_abnormal_results(patient_id, **kwargs)


def query_dynamic_fields(field_name: str, value: Any, **kwargs) -> List[Dict[str, Any]]:
    """Convenience function to query dynamic fields."""
    with ClinicalDataService() as service:
        return service.query_dynamic_fields(field_name, value, **kwargs)


def delete_patient_data(patient_id: str) -> Dict[str, int]:
    """
    Convenience function to delete all data for a patient.
    
    WARNING: This permanently deletes data. Use with caution.
    
    Args:
        patient_id: The patient identifier (e.g., "Patient/123")
        
    Returns:
        Dictionary with deletion counts and status
    """
    with ClinicalDataService() as service:
        return service.delete_patient_data(patient_id)


if __name__ == "__main__":
    # Example usage and testing
    import json
    from datetime import datetime, timedelta
    
    print("Clinical Data Query Service - Test Examples")
    print("=" * 60)
    
    # Example: Get patient history
    print("\n1. Get Patient History (Patient/123):")
    try:
        with ClinicalDataService() as service:
            reports = service.get_patient_history("Patient/123", limit=5)
            print(f"   Found {len(reports)} reports")
            for report in reports[:2]:
                print(f"   - {report.code.coding[0].display if report.code.coding else 'Unknown'} ({report.effectiveDateTime})")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Example: Get lab trend
    print("\n2. Get Lab Trend (Glucose, LOINC 2345-7):")
    try:
        trend = get_lab_trend("Patient/123", "2345-7")
        print(f"   Found {len(trend)} data points")
        for point in trend[:3]:
            print(f"   - {point['date']}: {point['value']} {point.get('unit', '')}")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Example: Find abnormal results
    print("\n3. Find Abnormal Results:")
    try:
        abnormal = find_abnormal_results("Patient/123", limit=5)
        print(f"   Found {len(abnormal)} abnormal results")
        for result in abnormal[:2]:
            print(f"   - {result.get('test_name')}: {result.get('interpretation_code')}")
    except Exception as e:
        print(f"   Error: {e}")
    
    # Example: Query dynamic fields
    print("\n4. Query Dynamic Fields:")
    try:
        dynamic = query_dynamic_fields("http://example.org/custom-field", "test-value")
        print(f"   Found {len(dynamic)} observations with matching extension")
    except Exception as e:
        print(f"   Error: {e}")
    
    print("\n" + "=" * 60)
