import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from personal_knowledge.core.project_paths import ROOT, INTEGRATION_DIR, DB_DIR, UNIFIED_DB
print("ROOT          =", ROOT)
print("INTEGRATION   =", INTEGRATION_DIR)
print("DB_DIR        =", DB_DIR)
print("UNIFIED_DB    =", UNIFIED_DB)
