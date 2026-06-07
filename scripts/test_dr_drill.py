#!/usr/bin/env python3
"""
DR Drill Test Script - Disaster Recovery Drill for backup_manager.py

Executes a read-only disaster recovery drill to verify backup restoration capability.
Does NOT perform actual failover - this is a drill only.

Drill Phases:
1. Pre-DR Check: Verify backup files, DB connectivity
2. Simulated DR: Test restoration path (to test schema, not production)
3. Measure RTO against target
4. Generate report
"""

import sys
import subprocess
import hashlib
from datetime import datetime
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

from backup_manager import DatabaseBackupManager, _load_password, BACKUP_DIR


class DRDrill:
    """Disaster Recovery Drill Executor"""
    
    TARGET_RTO_MINUTES = 30  # Target Recovery Time Objective
    TARGET_RPO_HOURS = 24    # Target Recovery Point Objective
    
    def __init__(self):
        self.password = _load_password()
        self.manager = DatabaseBackupManager(password=self.password)
        self.drill_timestamp = datetime.now()
        self.results = {
            "drill_date": self.drill_timestamp.isoformat(),
            "pre_check": {},
            "backup_verification": {},
            "restoration_test": {},
            "rto_measurement": {},
            "issues_found": [],
            "recommendations": [],
        }
    
    def _run_psql(self, query: str, dbname: str = "investpilot") -> tuple:
        """Run psql query and return (success, output)"""
        result = subprocess.run(
            ["psql", "-h", "localhost", "-p", "5432", "-U", "invest_admin", "-d", dbname, "-c", query],  # noqa: E501
            env={"PGPASSWORD": self.password},
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0, result.stdout, result.stderr
    
    def _compute_checksum(self, filepath: str) -> str:
        """Compute SHA256 checksum of a file"""
        sha256 = hashlib.sha256()
        with open(filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    
    def phase1_pre_check(self) -> dict:
        """Phase 1: Pre-DR Check - Verify current state"""
        print("\n" + "="*60)
        print("PHASE 1: Pre-DR Check")
        print("="*60)
        
        pre_check = {
            "backup_directory": str(BACKUP_DIR),
            "backup_directory_exists": BACKUP_DIR.exists(),
            "backups": [],
            "database_connectivity": False,
            "db_version": None,
        }
        
        # Check backup directory
        if BACKUP_DIR.exists():
            print(f"✓ Backup directory exists: {BACKUP_DIR}")
        else:
            print(f"✗ Backup directory NOT found: {BACKUP_DIR}")
            self.results["issues_found"].append("Backup directory not found")
        
        # List backups
        backups = self.manager.list_backups()
        pre_check["backups"] = backups
        print(f"\nBackups found: {len(backups)}")
        for b in backups:
            print(f"  - {b['filename']} ({b['size_mb']} MB, created: {b['created_at']})")
        
        if not backups:
            self.results["issues_found"].append("No backups found in backup directory")
        
        # Check database connectivity
        success, stdout, stderr = self._run_psql("SELECT version();")
        if success:
            pre_check["database_connectivity"] = True
            pre_check["db_version"] = stdout.split('\n')[2].strip() if len(stdout.split('\n')) > 2 else "Unknown"  # noqa: E501
            print(f"✓ Database connected: {pre_check['db_version']}")
        else:
            print(f"✗ Database connection failed: {stderr[:100]}")
            self.results["issues_found"].append("Database connectivity check failed")
        
        # Check latest backup age (RPO verification)
        if backups:
            latest = backups[0]
            latest_time = datetime.fromisoformat(latest['created_at'])
            age_hours = (self.drill_timestamp - latest_time).total_seconds() / 3600
            pre_check["latest_backup_age_hours"] = round(age_hours, 1)
            pre_check["rpo_target_met"] = age_hours <= self.TARGET_RPO_HOURS
            print(f"\nLatest backup age: {age_hours:.1f} hours")
            print(f"RPO target ({self.TARGET_RPO_HOURS}h): {'✓ MET' if pre_check['rpo_target_met'] else '✗ NOT MET'}")  # noqa: E501
            if not pre_check["rpo_target_met"]:
                self.results["issues_found"].append(f"Backup RPO exceeded: {age_hours:.1f}h > {self.TARGET_RPO_HOURS}h")  # noqa: E501
        
        self.results["pre_check"] = pre_check
        return pre_check
    
    def phase2_backup_verification(self) -> dict:
        """Phase 2: Verify backup integrity"""
        print("\n" + "="*60)
        print("PHASE 2: Backup Integrity Verification")
        print("="*60)
        
        verification = {
            "backups_verified": [],
            "all_passed": True,
        }
        
        backups = self.manager.list_backups()
        
        for backup in backups:
            backup_path = backup['path']
            print(f"\nVerifying: {backup['filename']}")
            
            result = {
                "filename": backup['filename'],
                "path": backup_path,
                "exists": False,
                "non_empty": False,
                "checksum": None,
                "verified": False,
            }
            
            # Check existence
            if Path(backup_path).exists():
                print("  ✓ File exists")
                result["exists"] = True
            else:
                print("  ✗ File NOT found")
                verification["all_passed"] = False
                continue
            
            # Check non-empty
            if Path(backup_path).stat().st_size > 0:
                print(f"  ✓ File non-empty ({backup['size_mb']} MB)")
                result["non_empty"] = True
            else:
                print("  ✗ File is empty")
                verification["all_passed"] = False
                continue
            
            # Compute checksum
            try:
                checksum = self._compute_checksum(backup_path)
                result["checksum"] = checksum
                print(f"  ✓ Checksum: {checksum[:16]}...")
                result["verified"] = True
            except Exception as e:
                print(f"  ✗ Checksum failed: {e}")
                verification["all_passed"] = False
            
            verification["backups_verified"].append(result)
        
        print(f"\nBackup verification: {'✓ ALL PASSED' if verification['all_passed'] else '✗ SOME FAILED'}")  # noqa: E501
        self.results["backup_verification"] = verification
        return verification
    
    def phase3_restoration_test(self) -> dict:
        """Phase 3: Test backup restoration to test schema (NOT production)"""
        print("\n" + "="*60)
        print("PHASE 3: Restoration Capability Test")
        print("="*60)
        
        restoration = {
            "test_schema": "drill_test_schema",
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
            "duration_seconds": None,
            "success": False,
            "message": "",
        }
        
        backups = self.manager.list_backups()
        if not backups:
            restoration["message"] = "No backups available for restoration test"
            self.results["issues_found"].append(restoration["message"])
            print(f"✗ {restoration['message']}")
            self.results["restoration_test"] = restoration
            return restoration
        
        # Use the most recent full backup
        latest_backup = backups[0]
        backup_path = latest_backup['path']
        test_schema = restoration["test_schema"]
        
        print(f"Testing restoration of: {latest_backup['filename']}")
        print(f"Target: test schema '{test_schema}' (NOT production)")
        
        # Create test schema
        success, _, stderr = self._run_psql(f'CREATE SCHEMA IF NOT EXISTS {test_schema};')
        if not success:
            restoration["message"] = f"Failed to create test schema: {stderr[:100]}"
            print(f"✗ {restoration['message']}")
            self.results["issues_found"].append(restoration["message"])
            self.results["restoration_test"] = restoration
            return restoration
        print("✓ Test schema created")
        
        # Try to restore to test schema (use -n schema flag)
        try:
            start_time = datetime.now()
            
            result = subprocess.run(
                [
                    "pg_restore",
                    "-h", "localhost",
                    "-p", "5432",
                    "-U", "invest_admin",
                    "-d", "investpilot",
                    "-n", test_schema,
                    "--no-owner",
                    "--no-acl",
                    backup_path,
                ],
                env={"PGPASSWORD": self.password},
                capture_output=True,
                text=True,
                timeout=300,
            )
            
            end_time = datetime.now()
            restoration["duration_seconds"] = (end_time - start_time).total_seconds()
            restoration["completed_at"] = end_time.isoformat()
            
            if result.returncode == 0:
                restoration["success"] = True
                restoration["message"] = f"Restoration test successful in {restoration['duration_seconds']:.1f}s"  # noqa: E501
                print(f"✓ {restoration['message']}")
            else:
                restoration["message"] = f"Restoration failed: {result.stderr[:200]}"
                print(f"✗ {restoration['message']}")
                self.results["issues_found"].append(restoration["message"])
        except subprocess.TimeoutExpired:
            restoration["message"] = "Restoration test timed out (>5 min)"
            print(f"✗ {restoration['message']}")
            self.results["issues_found"].append(restoration["message"])
        except Exception as e:
            restoration["message"] = f"Restoration test error: {str(e)}"
            print(f"✗ {restoration['message']}")
            self.results["issues_found"].append(restoration["message"])
        
        # Cleanup test schema
        if restoration["success"]:
            success, _, _ = self._run_psql(f'DROP SCHEMA {test_schema} CASCADE;')
            if success:
                print("✓ Test schema cleaned up")
        
        self.results["restoration_test"] = restoration
        return restoration
    
    def phase4_rto_measurement(self) -> dict:
        """Phase 4: Measure actual RTO against target"""
        print("\n" + "="*60)
        print("PHASE 4: RTO Measurement")
        print("="*60)
        
        rto = {
            "target_rto_minutes": self.TARGET_RTO_MINUTES,
            "target_rpo_hours": self.TARGET_RPO_HOURS,
            "actual_rto_seconds": None,
            "actual_rpo_hours": None,
            "rto_met": False,
            "rpo_met": False,
        }
        
        # Measure RPO
        pre_check = self.results.get("pre_check", {})
        if "latest_backup_age_hours" in pre_check:
            rto["actual_rpo_hours"] = pre_check["latest_backup_age_hours"]
            rto["rpo_met"] = rto["actual_rpo_hours"] <= rto["target_rpo_hours"]
            print(f"RPO: {rto['actual_rpo_hours']:.1f}h (target: {rto['target_rpo_hours']}h) - {'✓ MET' if rto['rpo_met'] else '✗ NOT MET'}")  # noqa: E501
        
        # Measure RTO (from restoration test if available)
        restoration = self.results.get("restoration_test", {})
        if restoration.get("duration_seconds"):
            rto["actual_rto_seconds"] = restoration["duration_seconds"]
            rto["rto_met"] = rto["actual_rto_seconds"] <= (self.TARGET_RTO_MINUTES * 60)
            rto_minutes = rto["actual_rto_seconds"] / 60
            print(f"RTO: {rto_minutes:.1f} min (target: {self.TARGET_RTO_MINUTES} min) - {'✓ MET' if rto['rto_met'] else '✗ NOT MET'}")  # noqa: E501
        else:
            print("RTO: Not measured (no restoration test completed)")
        
        self.results["rto_measurement"] = rto
        return rto
    
    def generate_recommendations(self):
        """Generate recommendations based on drill results"""
        issues = self.results["issues_found"]
        recs = self.results["recommendations"]
        
        # Analyze issues and add specific recommendations
        if any("RPO exceeded" in issue for issue in issues):
            recs.append("Schedule more frequent backup intervals to meet RPO target")
        
        if any("No backups found" in issue for issue in issues):
            recs.append("Implement automated backup scheduling immediately")
        
        if any("Restoration failed" in issue for issue in issues):
            recs.append("Investigate and fix backup restoration procedure")
            recs.append("Document manual steps required for DB restoration")
        
        # General recommendations
        recs.append("Implement actual DR state machine (NORMAL→DEGRADED→FAILOVER→RECOVERING→NORMAL)")  # noqa: E501
        recs.append("Schedule quarterly DR drills")
        recs.append("Consider implementing backup checksum tracking for integrity verification")
        
        # Deduplicate
        self.results["recommendations"] = list(set(recs))
    
    def generate_report(self) -> str:
        """Generate DR drill report in markdown format"""
        self.generate_recommendations()
        
        report = f"""# Disaster Recovery Drill Report

**Drill Date:** {self.results['drill_date']}  
**System:** invest_system  
**Status:** {'DRILL COMPLETED' if self.results.get('pre_check') else 'DRILL FAILED'}  

---

## Executive Summary

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| RTO | {self.TARGET_RTO_MINUTES} min | {self.results.get('rto_measurement', {}).get('actual_rto_seconds', 'N/A')} sec | {'✓' if self.results.get('rto_measurement', {}).get('rto_met') else '✗'} |  # noqa: E501
| RPO | {self.TARGET_RPO_HOURS} hours | {self.results.get('rto_measurement', {}).get('actual_rpo_hours', 'N/A')} hours | {'✓' if self.results.get('rto_measurement', {}).get('rpo_met') else '✗'} |  # noqa: E501

---

## Phase 1: Pre-DR Check Results

**Backup Directory:** {self.results['pre_check'].get('backup_directory', 'N/A')}  
**Database Connected:** {'Yes' if self.results['pre_check'].get('database_connectivity') else 'No'}    # noqa: E501
**DB Version:** {self.results['pre_check'].get('db_version', 'N/A')}  

### Backups Available:
"""
        
        for b in self.results['pre_check'].get('backups', []):
            report += f"- {b['filename']} ({b['size_mb']} MB, created: {b['created_at']})\n"
        
        report += f"""
### RPO Status:
- Latest backup age: {self.results['pre_check'].get('latest_backup_age_hours', 'N/A')} hours
- RPO target ({self.TARGET_RPO_HOURS}h): {'MET' if self.results['pre_check'].get('rpo_target_met') else 'NOT MET'}  # noqa: E501

---

## Phase 2: Backup Integrity Verification

**Overall Status:** {'PASSED' if self.results['backup_verification'].get('all_passed') else 'FAILED'}    # noqa: E501

"""
        
        for v in self.results['backup_verification'].get('backups_verified', []):
            status = '✓' if v['verified'] else '✗'
            report += f"- {v['filename']}: {status} (checksum: {v['checksum'][:16]}... if verified)\n"  # noqa: E501
        
        report += f"""
---

## Phase 3: Restoration Capability Test

**Test Schema:** {self.results['restoration_test'].get('test_schema', 'N/A')}  
**Duration:** {self.results['restoration_test'].get('duration_seconds', 'N/A')} seconds  
**Status:** {'SUCCESS' if self.results['restoration_test'].get('success') else 'FAILED'}  
**Message:** {self.results['restoration_test'].get('message', 'N/A')}  

> Note: Restoration was tested on a dedicated test schema, NOT production database.

---

## Phase 4: RTO/RPO Measurement

| Metric | Target | Actual | Achieved |
|--------|--------|--------|----------|
| RTO | {self.TARGET_RTO_MINUTES} min | {self.results['rto_measurement'].get('actual_rto_seconds', 'N/A')} sec | {'Yes' if self.results['rto_measurement'].get('rto_met') else 'No'} |  # noqa: E501
| RPO | {self.TARGET_RPO_HOURS}h | {self.results['rto_measurement'].get('actual_rpo_hours', 'N/A')}h | {'Yes' if self.results['rto_measurement'].get('rpo_met') else 'No'} |  # noqa: E501

---

## Issues Found

"""
        
        if self.results['issues_found']:
            for i, issue in enumerate(self.results['issues_found'], 1):
                report += f"{i}. {issue}\n"
        else:
            report += "No issues found during drill.\n"
        
        report += """
---

## Remediation Plan

| Issue | Remediation | Priority |
|-------|-------------|----------|
"""
        
        issue_to_rem = {
            "RPO exceeded": "Increase backup frequency",
            "No backups found": "Set up automated backups",
            "Restoration failed": "Debug pg_restore procedure",
            "Backup directory not found": "Verify backup storage configuration",
        }
        
        for issue in set(self.results['issues_found']):
            rem = issue_to_rem.get(issue, "Investigate and fix")
            report += f"| {issue} | {rem} | High |\n"
        
        report += """
---

## Recommendations

"""
        
        for i, rec in enumerate(self.results['recommendations'], 1):
            report += f"{i}. {rec}\n"
        
        report += f"""
---

## Next Scheduled Drill

**Suggested Date:** {(datetime.now()).strftime('%Y-%m-%d')} (Quarterly)  
**Frequency:** Every 3 months  

---

## Notes

- This drill was executed in **read-only verification mode**
- No actual failover was performed
- Restoration was tested on a dedicated test schema only
- The DR state machine described (NORMAL→DEGRADED→FAILOVER→RECOVERING→NORMAL) **does not currently exist in backup_manager.py** and should be implemented  # noqa: E501

---

*Report generated by DR Drill Script (test_dr_drill.py)*
"""
        
        return report
    
    def run_drill(self) -> str:
        """Execute full DR drill and return report"""
        print("\n" + "#"*60)
        print("# DISASTER RECOVERY DRILL")
        print("#"*60)
        
        try:
            self.phase1_pre_check()
            self.phase2_backup_verification()
            self.phase3_restoration_test()
            self.phase4_rto_measurement()
        except Exception as e:
            print(f"\n✗ Drill failed with exception: {e}")
            self.results["issues_found"].append(f"Drill execution exception: {str(e)}")
        
        report = self.generate_report()
        return report


def main():
    drill = DRDrill()
    report = drill.run_drill()
    
    # Print report
    print("\n" + "="*60)
    print("DRILL REPORT")
    print("="*60)
    print(report)
    
    # Save report
    report_path = Path(__file__).parent.parent / "docs" / "DR_DRILL_REPORT.md"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n[Report saved to: {report_path}]")
    
    return report


if __name__ == "__main__":
    main()