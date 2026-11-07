"""Pull every CRM account into data/crm_accounts.json (standard library only)."""
import json
import os

import crm_api

if __name__ == "__main__":
    rows = crm_api.list_accounts()
    os.makedirs("data", exist_ok=True)
    json.dump(rows, open("data/crm_accounts.json", "w"), indent=2)
    print(f"Fetched {len(rows)} accounts -> data/crm_accounts.json")
