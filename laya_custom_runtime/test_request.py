import json
import urllib.request

URL = "http://laya-predictor-predictor.laya.svc.cluster.local/v1/models/laya-predictor:predict"

payload = {
    "state": (
        "Hi, we were billed twice for March. Please refund the duplicate "
        "today or we will cancel our plan."
    ),
    "questions": {
        "department": {
            "type": "choice",
            "instructions": "Which department should handle this request?",
            "criteria": {
                "billing": "invoices, payments, refunds",
                "technical": "bugs, outages, system errors",
                "sales": "pricing, new contracts",
                "other": "everything else",
            },
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgent is this request?",
            "criteria": ["not urgent", "soon", "critical deadline or blocking issue"],
        },
        "churn_risk": {
            "type": "noul",
            "instructions": "Does the user threaten to cancel or leave?",
        },
    },
}

req = urllib.request.Request(
    URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(req, timeout=60) as resp:
    print("HTTP", resp.status)
    print(json.dumps(json.loads(resp.read()), indent=2))
