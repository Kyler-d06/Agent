"""Small mesh smoke test; prints JSON without requiring external services."""
import hashlib
import json
import platform
import sys

text = " ".join(sys.argv[1:]) or "mesh probe"
print(json.dumps({"hostname": platform.node(), "machine": platform.machine(),
                  "characters": len(text), "sha256": hashlib.sha256(text.encode()).hexdigest()}))
