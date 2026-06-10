import sys
import time

import config

try:
    age = time.time() - float(config.HEARTBEAT_FILE.read_text())
    sys.exit(0 if age < 90 else 1)
except Exception:
    sys.exit(1)
