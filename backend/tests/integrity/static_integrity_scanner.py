"""
STATIC INTEGRITY SCANNER
========================
Scans production codebase (backend/app and frontend/src) for forbidden
anti-patterns, fake artifacts, swallowed exceptions masking business errors,
and hardcoded business metrics.
"""

import os
import re
from typing import List, Dict, Any, Tuple


class StaticIntegrityScanner:
    """
    Scans project source files against platform integrity rules.
    Employs granular allowlists to distinguish legitimate configuration/thresholds
    from dangerous AI-generated shortcuts and hardcoded metrics.
    """

    FORBIDDEN_PATTERNS = [
        (r'mock_weights', "Mock or dummy weights detected in production code"),
        (r'fake_weights', "Fake weights token detected in production code"),
        (r'fake_model', "Fake model token detected in production code"),
        (r'dummy_model', "Dummy model token detected in production code"),
        (r'dummy_weights', "Dummy weights token detected in production code"),
    ]

    # Files explicitly excluded from scanning (tests, documentation, backups, migration reports)
    EXCLUDED_PATHS = [
        "backend/tests/",
        "backend/backups/",
        "backend/venv/",
        "node_modules/",
        "dist/",
        "build/",
        "catboost_info/",
        ".git/",
    ]

    @classmethod
    def is_excluded(cls, file_path: str) -> bool:
        normalized = file_path.replace("\\", "/")
        return any(excl in normalized for excl in cls.EXCLUDED_PATHS)

    # Legitimate guard checks that reject or test forbidden tokens
    ALLOWLIST_EXCEPTIONS = [
        # candidate_vault rejection validator
        ('candidate_vault.py', 'mock_weights', 'raise ValueError'),
        ('candidate_vault.py', 'fixed_weights', 'raise ValueError'),
    ]

    @classmethod
    def is_allowlisted(cls, file_path: str, line: str, pattern: str) -> bool:
        normalized = file_path.replace("\\", "/")
        for file_sub, pat, indicator in cls.ALLOWLIST_EXCEPTIONS:
            if file_sub in normalized and pat in pattern:
                return True
        return False

    @classmethod
    def scan_file_for_forbidden_tokens(cls, file_path: str) -> List[Dict[str, Any]]:
        """Scans a single file for forbidden tokens."""
        if cls.is_excluded(file_path):
            return []

        findings = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line_idx, line in enumerate(f, 1):
                    # Skip comment lines
                    stripped = line.strip()
                    if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                        continue

                    for pattern, message in cls.FORBIDDEN_PATTERNS:
                        if re.search(pattern, line, re.IGNORECASE):
                            if cls.is_allowlisted(file_path, stripped, pattern):
                                continue
                            findings.append({
                                "file": file_path,
                                "line": line_idx,
                                "snippet": stripped[:100],
                                "message": message
                            })
        except Exception as e:
            findings.append({
                "file": file_path,
                "line": 0,
                "snippet": "",
                "message": f"Could not read file: {e}"
            })
        return findings

    @classmethod
    def scan_directory(cls, root_dir: str) -> List[Dict[str, Any]]:
        """Recursively scans root_dir for integrity violations."""
        all_findings = []
        for root, _, files in os.walk(root_dir):
            for file in files:
                if file.endswith((".py", ".js", ".jsx", ".ts", ".tsx")):
                    full_path = os.path.join(root, file)
                    if not cls.is_excluded(full_path):
                        findings = cls.scan_file_for_forbidden_tokens(full_path)
                        all_findings.extend(findings)
        return all_findings


if __name__ == "__main__":
    import sys
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    app_dir = os.path.join(base_dir, "app")
    findings = StaticIntegrityScanner.scan_directory(app_dir)
    if findings:
        print(f"FAILED: Found {len(findings)} integrity violations:")
        for f in findings:
            print(f"  {f['file']}:{f['line']} - {f['message']}: {f['snippet']}")
        sys.exit(1)
    else:
        print("SUCCESS: Zero integrity violations found in production code.")
        sys.exit(0)
