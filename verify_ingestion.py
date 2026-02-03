"""
Verification Script for Clinical Data Ingestion
Dumps patient data in a readable format for manual verification against source PDFs.
"""

import os
from datetime import datetime
from typing import List, Dict, Any, Optional
from pymongo import MongoClient
from tabulate import tabulate

# MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "clinical_data_repository")


def format_date(date_val) -> str:
    """Format date for display."""
    if isinstance(date_val, datetime):
        return date_val.strftime("%Y-%m-%d %H:%M")
    return str(date_val) if date_val else "N/A"


def get_code_display(code_obj: Dict) -> str:
    """Extract human-readable code display."""
    if not code_obj:
        return "N/A"
    coding = code_obj.get("coding", [])
    if coding:
        display = coding[0].get("display", "")
        code = coding[0].get("code", "")
        system = coding[0].get("system", "").split("/")[-1]  # Get last part of URL
        if display:
            return f"{display} ({system}: {code})"
        return f"{system}: {code}"
    return code_obj.get("text", "N/A")


def get_value_display(value_obj: Dict) -> str:
    """Extract human-readable value from polymorphic value."""
    if not value_obj:
        return "N/A"
    
    # Check valueQuantity
    if "valueQuantity" in value_obj:
        vq = value_obj["valueQuantity"]
        val = vq.get("value", "")
        unit = vq.get("unit", "")
        return f"{val} {unit}".strip()
    
    # Check valueCodeableConcept
    if "valueCodeableConcept" in value_obj:
        return get_code_display(value_obj["valueCodeableConcept"])
    
    # Check valueString
    if "valueString" in value_obj:
        return value_obj["valueString"]
    
    return str(value_obj)


def get_interpretation_display(interp_list: List[Dict]) -> str:
    """Extract interpretation codes."""
    if not interp_list:
        return ""
    codes = []
    for interp in interp_list:
        for coding in interp.get("coding", []):
            code = coding.get("code", "")
            display = coding.get("display", "")
            if code:
                codes.append(f"{code} ({display})" if display else code)
    return ", ".join(codes) if codes else ""


def verify_patient_data(patient_id: str = "Patient/5427704-1"):
    """
    Retrieve and display all data for a specific patient in a readable format.
    
    Args:
        patient_id: The patient identifier to query
    """
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    
    print("=" * 80)
    print(f"PATIENT DATA VERIFICATION REPORT")
    print(f"Patient ID: {patient_id}")
    print(f"Database: {DB_NAME}")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # -------------------------------------------------------------------------
    # Section 1: Diagnostic Reports Summary
    # -------------------------------------------------------------------------
    print("\n" + "-" * 80)
    print("SECTION 1: DIAGNOSTIC REPORTS")
    print("-" * 80)
    
    reports = list(db.diagnostic_reports.find(
        {"subject.reference": patient_id}
    ).sort("effectiveDateTime", -1))
    
    if not reports:
        print("No diagnostic reports found.")
    else:
        print(f"Found {len(reports)} diagnostic report(s):\n")
        
        for idx, report in enumerate(reports, 1):
            print(f"  Report #{idx}")
            print(f"  {'='*60}")
            print(f"  ID:              {report.get('id', report.get('_id', 'N/A'))}")
            print(f"  Status:          {report.get('status', 'N/A')}")
            print(f"  Type:            {get_code_display(report.get('code'))}")
            print(f"  Effective Date:  {format_date(report.get('effectiveDateTime'))}")
            print(f"  Issued:          {format_date(report.get('issued'))}")
            
            # Performers
            performers = report.get("performer", [])
            if performers:
                perf_names = [p.get("display", p.get("reference", "Unknown")) for p in performers]
                print(f"  Performer(s):    {', '.join(perf_names)}")
            
            # Result references
            results = report.get("result", [])
            if results:
                print(f"  Contains:        {len(results)} observation reference(s)")
            
            # Conclusion
            conclusion = report.get("conclusion")
            if conclusion:
                print(f"  Conclusion:      {conclusion}")
            
            print()
    
    # -------------------------------------------------------------------------
    # Section 2: Observations Summary Table
    # -------------------------------------------------------------------------
    print("\n" + "-" * 80)
    print("SECTION 2: OBSERVATIONS SUMMARY")
    print("-" * 80)
    
    observations = list(db.observations.find(
        {"subject.reference": patient_id}
    ).sort("effectiveDateTime", -1))
    
    if not observations:
        print("No observations found.")
    else:
        print(f"Found {len(observations)} observation(s):\n")
        
        # Create summary table
        table_data = []
        for obs in observations:
            row = [
                obs.get("id", str(obs.get("_id", "N/A")))[:20],
                format_date(obs.get("effectiveDateTime")),
                get_code_display(obs.get("code"))[:50],
                get_value_display(obs)[:40],
                get_interpretation_display(obs.get("interpretation", []))[:20],
                "Yes" if obs.get("component") else "No"
            ]
            table_data.append(row)
        
        headers = ["ID", "Date", "Test", "Value", "Interp", "Has Components"]
        print(tabulate(table_data, headers=headers, tablefmt="grid"))
    
    # -------------------------------------------------------------------------
    # Section 3: Detailed Observation Breakdown
    # -------------------------------------------------------------------------
    print("\n" + "-" * 80)
    print("SECTION 3: DETAILED OBSERVATION BREAKDOWN")
    print("-" * 80)
    
    for idx, obs in enumerate(observations, 1):
        print(f"\n  Observation #{idx}")
        print(f"  {'='*60}")
        print(f"  ID:              {obs.get('id', obs.get('_id', 'N/A'))}")
        print(f"  Status:          {obs.get('status', 'N/A')}")
        print(f"  Category:        {', '.join([get_code_display(c) for c in obs.get('category', [])]) or 'N/A'}")
        print(f"  Code:            {get_code_display(obs.get('code'))}")
        print(f"  Date/Time:       {format_date(obs.get('effectiveDateTime'))}")
        print(f"  Issued:          {format_date(obs.get('issued'))}")
        
        # Value details
        print(f"\n  VALUE:")
        if "valueQuantity" in obs:
            vq = obs["valueQuantity"]
            print(f"    Type:          Quantity")
            print(f"    Value:         {vq.get('value')}")
            print(f"    Unit:          {vq.get('unit', 'N/A')}")
            print(f"    System:        {vq.get('system', 'N/A')}")
        elif "valueCodeableConcept" in obs:
            print(f"    Type:          CodeableConcept")
            print(f"    Value:         {get_code_display(obs['valueCodeableConcept'])}")
        elif "valueString" in obs:
            print(f"    Type:          String")
            print(f"    Value:         {obs['valueString'][:100]}{'...' if len(obs.get('valueString', '')) > 100 else ''}")
        else:
            print(f"    Type:          None (check components)")
        
        # Interpretation
        interp = get_interpretation_display(obs.get("interpretation", []))
        if interp:
            print(f"\n  INTERPRETATION:  {interp}")
        
        # Reference Range
        ref_ranges = obs.get("referenceRange", [])
        if ref_ranges:
            print(f"\n  REFERENCE RANGE:")
            for rr in ref_ranges:
                low = rr.get("low", {})
                high = rr.get("high", {})
                low_str = f"{low.get('value', '')} {low.get('unit', '')}".strip() if low else "N/A"
                high_str = f"{high.get('value', '')} {high.get('unit', '')}".strip() if high else "N/A"
                text = rr.get("text", "")
                print(f"    Low: {low_str} | High: {high_str}")
                if text:
                    print(f"    Text: {text}")
        
        # Components (for panels)
        components = obs.get("component", [])
        if components:
            print(f"\n  COMPONENTS ({len(components)}):")
            comp_table = []
            for comp in components:
                comp_row = [
                    get_code_display(comp.get("code"))[:40],
                    get_value_display(comp)[:30],
                    get_interpretation_display(comp.get("interpretation", []))[:15]
                ]
                comp_table.append(comp_row)
            
            comp_headers = ["Test", "Value", "Interp"]
            print("  " + tabulate(comp_table, headers=comp_headers, tablefmt="simple").replace("\n", "\n  "))
        
        # Method
        method = obs.get("method")
        if method:
            print(f"\n  METHOD:          {get_code_display(method)}")
        
        # Specimen
        specimen = obs.get("specimen")
        if specimen:
            print(f"\n  SPECIMEN:        {specimen.get('display', specimen.get('reference', 'N/A'))}")
        
        # Notes
        notes = obs.get("note", [])
        if notes:
            print(f"\n  NOTES:")
            for note in notes:
                text = note.get("text", "")
                if text:
                    print(f"    - {text}")
        
        # Extensions
        extensions = obs.get("extension", [])
        if extensions:
            print(f"\n  EXTENSIONS ({len(extensions)}):")
            for ext in extensions:
                url = ext.get("url", "N/A")
                val = ext.get("valueString") or ext.get("valueCode") or "N/A"
                print(f"    - {url}: {val}")
    
    # -------------------------------------------------------------------------
    # Section 4: Lab Trends (Time Series)
    # -------------------------------------------------------------------------
    print("\n" + "-" * 80)
    print("SECTION 4: LAB VALUE TRENDS")
    print("-" * 80)
    
    # Aggregate to find all unique LOINC codes for this patient
    pipeline = [
        {"$match": {"subject.reference": patient_id}},
        {"$unwind": {"path": "$component", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "root_code": "$code.coding.code",
            "comp_code": "$component.code.coding.code"
        }},
        {"$project": {
            "all_codes": {"$setUnion": [
                {"$ifNull": ["$root_code", []]},
                {"$ifNull": ["$comp_code", []]}
            ]}
        }},
        {"$unwind": "$all_codes"},
        {"$group": {"_id": "$all_codes", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    
    loinc_codes = list(db.observations.aggregate(pipeline))
    
    if loinc_codes:
        print(f"Found {len(loinc_codes)} unique test code(s):\n")
        
        for code_info in loinc_codes[:10]:  # Show top 10
            code = code_info["_id"]
            count = code_info["count"]
            print(f"  - {code}: {count} occurrence(s)")
    else:
        print("No test codes found for trend analysis.")
    
    # -------------------------------------------------------------------------
    # Section 5: Summary Statistics
    # -------------------------------------------------------------------------
    print("\n" + "-" * 80)
    print("SECTION 5: SUMMARY STATISTICS")
    print("-" * 80)
    
    total_reports = db.diagnostic_reports.count_documents({"subject.reference": patient_id})
    total_observations = db.observations.count_documents({"subject.reference": patient_id})
    
    # Count by status
    report_statuses = db.diagnostic_reports.aggregate([
        {"$match": {"subject.reference": patient_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}}
    ])
    
    obs_statuses = db.observations.aggregate([
        {"$match": {"subject.reference": patient_id}},
        {"$group": {"_id": "$status", "count": {"$sum": 1}}}
    ])
    
    print(f"\n  Total Diagnostic Reports:  {total_reports}")
    print(f"  Total Observations:        {total_observations}")
    
    print("\n  Report Status Breakdown:")
    for status in report_statuses:
        print(f"    - {status['_id']}: {status['count']}")
    
    print("\n  Observation Status Breakdown:")
    for status in obs_statuses:
        print(f"    - {status['_id']}: {status['count']}")
    
    # Count observations with components (panels)
    panel_count = db.observations.count_documents({
        "subject.reference": patient_id,
        "component": {"$exists": True, "$ne": []}
    })
    print(f"\n  Observations with Components (Panels): {panel_count}")
    
    # Count abnormal results
    abnormal_count = db.observations.count_documents({
        "subject.reference": patient_id,
        "$or": [
            {"interpretation.coding.code": {"$in": ["H", "L", "A", "HH", "LL", "AA"]}},
            {"component.interpretation.coding.code": {"$in": ["H", "L", "A", "HH", "LL", "AA"]}}
        ]
    })
    print(f"  Abnormal Results: {abnormal_count}")
    
    client.close()
    
    print("\n" + "=" * 80)
    print("END OF VERIFICATION REPORT")
    print("=" * 80)


if __name__ == "__main__":
    import sys
    
    # Allow patient ID to be passed as argument, default to the one from labs.pdf
    patient_id = sys.argv[1] if len(sys.argv) > 1 else "Patient/5427704-1"
    
    verify_patient_data(patient_id)
