
import json
import urllib.error
import urllib.request

URL = "http://laya-predictor-predictor.pangea-energy-and-power.svc.cluster.local/v1/models/laya-predictor:predict"

# The tool registry, expressed as Laya "choice" criteria (label -> description).
TOOL_CRITERIA = {
    "wind_status": "live wind speed, gusts, or whether turbines are shut down for high wind",
    "solar_forecast": "solar output, cloud cover, irradiance, or panel performance",
    "wave_forecast": "wave height, wave energy, or wave-farm output",
    "active_alerts": "current alarms, warnings, faults, or what needs attention",
    "fleet_totals": "total power generation across the whole fleet right now",
    "none": "greetings, thanks, or anything not about live fleet data",
}

SITE_CRITERIA = {
    "BJF": "Bjornefjall, the Arctic wind farm in northern Norway",
    "any": "no specific site, or the whole fleet",
    # ... add the rest of your site codes/names here for a real test
}

# Try a few representative messages.
MESSAGES = [
    "what's the wind speed right now?",
    "cloud forecast around our solar sites?",
    "is Bjornefjall shut down?",
    "how much are we generating in total?",
    "thanks, that's all",
]


def route(message: str) -> dict:
    payload = {
        "state": message,
        "questions": {
            "tool": {
                "type": "choice",
                "instructions": "Which backend tool should answer this message?",
                "criteria": TOOL_CRITERIA,
            },
            "site": {
                "type": "choice",
                "instructions": "Which site is this message about?",
                "criteria": SITE_CRITERIA,
            },
            "needs_live_data": {
                "type": "noul",
                "instructions": "Does answering this require fetching live fleet data?",
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
        return json.loads(resp.read())


if __name__ == "__main__":
    for msg in MESSAGES:
        print(f"\n>>> {msg}")
        try:
            print(json.dumps(route(msg), indent=2))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            print(f"HTTP {exc.code} {exc.reason}\nbody: {body}")
        except Exception as exc:
            print(f"ERROR: {exc}")
