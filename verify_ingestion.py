"""
Verification Script for Clinical Data Ingestion (Complete Version)
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
        # Simplified system name for display (e.g. loinc.org -> LOINC)
        sys_url = coding[0].get("system", "")
        if "loinc" in sys_url: system = "LOINC"
        elif "snomed" in sys_url: system = "SNOMED"
        else: system = sys_url.split("/")[-1]
        
        if display:
            return f"{display} ({code})"
        return f"{system}: {code}"
    return code_obj.get("text", "N/A")


def get_value_display(value_obj: Dict) -> str:
    """Extract human-readable value from polymorphic value."""
    if not value_obj:
        return "N/A"
    
    if "valueQuantity" in value_obj:
        vq = value_obj["valueQuantity"]
        val = vq.get("value", "")
        unit = vq.get("unit", "")
        return f"{val} {unit}".strip()
    
    if "valueCodeableConcept" in value_obj:
        return get_code_display(value_obj["valueCodeableConcept"])
    
    if "valueString" in value_obj:
        return value_obj["valueString"]
        
    if "dataAbsentReason" in value_obj:
        return f"[Missing: {get_code_display(value_obj['dataAbsentReason'])}]"
    
    return "N/A"


def get_interpretation_display(interp_list: List[Dict]) -> str:
    """Extract interpretation codes."""
    if not interp_list:
        return ""
    codes = []
    for interp in interp_list:
        for coding in interp.get("coding", []):
            code = coding.get("code", "")
            if code: codes.append(code)
    return ", ".join(codes)


def verify_patient_data(patient_id: str = "Patient/5427704-1"):
    client = MongoClient(MONGO_URI)
    db = client[DB_NAME]
    
    print("=" * 100)
    print(f"PATIENT DATA VERIFICATION REPORT")
    print(f"Patient ID: {patient_id}")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 100)
    
    # -------------------------------------------------------------------------
    # Section 1: Diagnostic Reports Summary
    # -------------------------------------------------------------------------
    print("\n" + "-" * 100)
    print("SECTION 1: DIAGNOSTIC REPORTS")
    print("-" * 100)
    
    reports = list(db.diagnostic_reports.find({"subject.reference": patient_id}).sort("effectiveDateTime", -1))
    
    if not reports:
        print("No diagnostic reports found.")
    else:
        for idx, report in enumerate(reports, 1):
            print(f"  Report #{idx} [ID: {report.get('id', report.get('_id'))}]")
            print(f"  {'='*80}")
            
            # Key Metadata
            data = [
                ["Status", report.get('status', 'N/A')],
                ["Code", get_code_display(report.get('code'))],
                ["Effective Date", format_date(report.get('effectiveDateTime'))],
                ["Issued Date", format_date(report.get('issued'))],
            ]
            
            # NEW: Identifiers (Accession Numbers)
            ids = report.get("identifier", [])
            if ids:
                id_str = ", ".join([f"{i.get('value')} ({i.get('type', {}).get('text', 'ID')})" for i in ids])
                data.append(["Identifiers", id_str])
                
            # NEW: Categories
            cats = report.get("category", [])
            if cats:
                cat_str = ", ".join([get_code_display(c) for c in cats])
                data.append(["Category", cat_str])

            print(tabulate(data, tablefmt="plain"))
            
            # Conclusion / Narrative
            if report.get("conclusion"):
                print(f"\n  Conclusion:\n  {report.get('conclusion')}")
                
            # NEW: PDF Link
            forms = report.get("presentedForm", [])
            if forms:
                print(f"\n  Original Attachments: {len(forms)} file(s)")
            print()
    
    # -------------------------------------------------------------------------
    # Section 2: Observations Summary Table
    # -------------------------------------------------------------------------
    print("\n" + "-" * 100)
    print("SECTION 2: OBSERVATIONS SUMMARY (Latest First)")
    print("-" * 100)
    
    observations = list(db.observations.find({"subject.reference": patient_id}).sort("effectiveDateTime", -1))
    
    if observations:
        table_data = []
        for obs in observations:
            # Check for component vs root value
            is_panel = "Yes" if obs.get("component") else "No"
            val_display = "See Components" if is_panel == "Yes" else get_value_display(obs)
            
            row = [
                format_date(obs.get("effectiveDateTime")),
                get_code_display(obs.get("code"))[:40],
                val_display[:30],
                get_interpretation_display(obs.get("interpretation", [])),
                is_panel
            ]
            table_data.append(row)
        
        print(tabulate(table_data, headers=["Date", "Test", "Value", "Flag", "Panel?"], tablefmt="simple"))
    else:
        print("No observations found.")
    
    # -------------------------------------------------------------------------
    # Section 3: Detailed Observation Breakdown
    # -------------------------------------------------------------------------
    print("\n" + "-" * 100)
    print("SECTION 3: DETAILED OBSERVATION BREAKDOWN")
    print("-" * 100)
    
    for idx, obs in enumerate(observations, 1):
        test_name = get_code_display(obs.get('code'))
        print(f"\n  #{idx} {test_name}")
        print(f"  {'-'*len(str(idx)+test_name+str(2))}")
        
        # Core Info
        print(f"  ID: {obs.get('id', obs.get('_id'))} | Status: {obs.get('status')}")
        
        # Primary Value (if not a panel)
        if not obs.get("component"):
            print(f"  Value: {get_value_display(obs)}")
            
            # Data Absent?
            if "dataAbsentReason" in obs:
                print(f"  Reason Missing: {get_code_display(obs['dataAbsentReason'])}")
                
            # Ref Range
            rrs = obs.get("referenceRange", [])
            for rr in rrs:
                print(f"  Ref Range: {rr.get('text', 'N/A')} (Low: {rr.get('low', {}).get('value','')} - High: {rr.get('high', {}).get('value','')})")
        
        # Notes
        for note in obs.get("note", []):
            print(f"  Note: {note.get('text')}")

        # Components (The heavy lifter for antibiograms)
        components = obs.get("component", [])
        if components:
            print(f"\n  > Panel Results ({len(components)}):")
            comp_table = []
            for c in components:
                # Safe access to reference range
                rrs = c.get("referenceRange", [])
                ref_text = rrs[0].get("text", "") if rrs else ""
                
                comp_table.append([
                    get_code_display(c.get("code"))[:35],
                    get_value_display(c)[:25],
                    get_interpretation_display(c.get("interpretation", [])),
                    ref_text[:20]
                ])
            print(tabulate(comp_table, headers=["   Test", "Value", "Flag", "Ref Range"], tablefmt="plain"))
            
    client.close()

if __name__ == "__main__":
    import sys
    pid = sys.argv[1] if len(sys.argv) > 1 else "Patient/5427704-1"
    verify_patient_data(pid)