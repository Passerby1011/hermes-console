# -*- coding: utf-8 -*-
"""变异测试：把修复回退成「只读根库」，新增用例必须变红（否则断言没牙）。

做法：monkeypatch ``autogroup_ai.profile_scan_targets`` 只返回 (default, <home>/state.db)，
然后跑 tests/test_autogroup_profile_scope.py。期望：至少 4 条失败（主修相关的）。
跑法：<venv>/python.exe dashboard/tests/_mutant_check_profile_scope.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
DASH = TESTS.parent
PLUGIN = DASH.parent
PY = sys.executable

PROBE = DASH / "_mutant_profile_scope_probe.py"
PROBE.write_text(
    "# -*- coding: utf-8 -*-\n"
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, %r)\n"
    "import autogroup_ai as ai\n"
    "_orig = ai.profile_scan_targets\n"
    "def only_root(home, agents=None):\n"
    "    home = Path(home)\n"
    "    t = [('default', home / 'state.db')]\n"
    "    wanted = [str(a).strip() for a in (agents or set()) if str(a).strip()]\n"
    "    if not wanted:\n"
    "        return t\n"
    "    return t if 'default' in wanted else []\n"
    "ai.profile_scan_targets = only_root\n"
    "import pytest\n"
    "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', %r]))\n"
    % (str(DASH), str(TESTS / "test_autogroup_profile_scope.py")),
    encoding="utf-8")

proc = subprocess.run([PY, str(PROBE)], cwd=str(DASH), capture_output=True, text=True,
                      encoding="utf-8", errors="replace",
                      env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "", "SYSTEMROOT": "C:\\Windows"})
tail = proc.stdout.strip().splitlines()[-6:]
print("EXIT =", proc.returncode)
print("\n".join(tail))
failed = proc.returncode != 0
print("MUTANT-KILLED" if failed else "MUTANT-SURVIVED (断言没牙！)")
PROBE.unlink(missing_ok=True)
sys.exit(0 if failed else 1)
