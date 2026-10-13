"""Write stackhawk-openapi.json (the API description HawkScan uses) from the running app's own routes."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ifpay.web.app import app  # noqa: E402

spec = app.openapi()
spec["servers"] = [{"url": "/"}]
(ROOT / "stackhawk-openapi.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
print(f"stackhawk-openapi.json: {len(spec['paths'])} paths")
