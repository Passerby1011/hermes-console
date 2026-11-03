# -*- coding: utf-8 -*-
"""变异测试 2：把「子行可见性」判定关掉（永远 True），VisibilityParityCase 必须变红。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
DASH = TESTS.parent
PY = sys.executable
PROBE = DASH / "_mutant_child_probe.py"
PROBE.write_text(
    "# -*- coding: utf-8 -*-\n"
    "import sys\n"
    "sys.path.insert(0, %r)\n"
    "import autogroup_ai as ai\n"
    "ai._is_listable_child = lambda row, by_id, cols: True\n"
    "import pytest\n"
    "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', %r]))\n"
    % (str(DASH), str(TESTS / "test_autogroup_profile_scope.py")),
    encoding="utf-8")
proc = subprocess.run([PY, str(PROBE)], cwd=str(DASH), capture_output=True, text=True,
                      encoding="utf-8", errors="replace",
                      env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "", "SYSTEMROOT": "C:\\Windows"})
print("EXIT =", proc.returncode)
print("\n".join(proc.stdout.strip().splitlines()[-5:]))
print("MUTANT-KILLED" if proc.returncode != 0 else "MUTANT-SURVIVED (断言没牙！)")
PROBE.unlink(missing_ok=True)
sys.exit(0 if proc.returncode != 0 else 1)
