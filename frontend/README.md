# Pangea Energy and Power — dashboard UI

First iteration of the frontend: the single-file dashboard (`dashboard-app.html`,
plain HTML/CSS/JS + Chart.js via CDN) served from a **UBI9 nginx** container that
also reverse-proxies `/api` to the `pangea-api` backend.

Because the API is proxied same-origin, the browser never calls the backend
directly — no CORS, and the backend needs no externally reachable Route.

## Live data

A small fetch layer wires these into the otherwise-static dashboard, with
graceful fallback to the demo values if the API is unavailable:

| Element | Endpoint |
|---------|----------|
| Global Fleet counts + Total Output | `GET /api/fleet/status` |
| Active Alerts list | `GET /api/alerts?status=active` |
| Predictive-maintenance vibration trend | `GET /api/turbines/NSO-04/readings?hours=48` |

Fleet + alerts refresh every 30s. Everything else in the dashboard is still
static demo content for this iteration.

## Layout

```
frontend/
├── dashboard-app.html         # the UI (served as index.html)
├── pange.png                  # welcome-screen / logo asset
├── welcome.wav                # spoken greeting on the welcome screen
├── nginx.default.d/
│   └── api-proxy.conf         # /api -> pangea-api:8080
└── Containerfile              # ubi9/nginx-124 base
```

The chart is at `deploy/charts/pangea-ui/` — Deployment, Service and Route in
one file, with every other chart in the project.

## Deploy on OpenShift

Build the image (project-level perms; same pattern as pangea-api):

```bash
cd frontend
oc project <your-project>

oc new-build --name pangea-ui --binary --strategy=docker
oc patch bc/pangea-ui --type=merge \
  -p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Containerfile"}}}}'
oc start-build pangea-ui --from-dir=. --follow
```

Then install the chart:

```bash
helm install pangea-ui ../deploy/charts/pangea-ui
oc rollout status deployment/pangea-ui
oc get route pangea-ui -o jsonpath='https://{.spec.host}{"\n"}'
```

## Welcome greeting

`welcome.wav` starts as the welcome screen and its **Enter** button fade in.
Clicking Enter reveals the dashboard immediately; the greeting keeps playing
over the transition rather than being waited for or cut short.

Browsers only allow that unprompted playback once the site has earned autoplay
permission, so on a first visit it is often refused. When that happens the
greeting is deferred to the first user gesture — a click anywhere, including
the Enter button — so the line is heard either way, just one beat later.

`welcome.wav` is pre-rendered rather than synthesized at load time, so the
splash stays instant and still works when `pangea-tts` is down. Regenerate it
from the same Kokoro voice the assistant uses (`bf_emma`, `en-gb`) whenever the
wording changes:

```bash
cd frontend
UI_HOST=$(oc get route pangea-ui -o jsonpath='{.spec.host}')
curl -sS -X POST "https://$UI_HOST/api/voice/speak" \
  -H 'Content-Type: application/json' \
  -d '{"text": "Welcome to Pangea, Global Automated Operations"}' \
  -o welcome.wav
```

The `Containerfile` copies it unconditionally, so the file must exist before
`oc start-build`. If it is missing at runtime the audio `error` handler fires
and the splash dismisses straight away.

## Guided tour

After Enter, a "Shift handover" prompt slides in bottom-right offering a walk
through the dashboard. The framing is that the user is the **duty supervisor**
taking the watch — the backend's `agent_system_prompt` matches, so the assistant
addresses them as supervisor. Pangea itself is never the supervisor.

Seven steps: intro, left sidebar, map, chat, wake toggle, right sidebar, solar
cards. Skip is available at every step; Escape ends it, arrow keys navigate.
Steps whose target is hidden at the current viewport width are skipped
automatically.

## Notes

- The chart is fully static (no `values.yaml`). To move the image to a public
  registry later, edit the one `image:` line in
  `templates/deployment.yaml`.
- Local preview by opening `dashboard-app.html` in a browser still works; the
  `/api` calls just fail and the static demo values remain.
