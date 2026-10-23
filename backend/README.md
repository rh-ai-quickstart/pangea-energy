# Pangea Energy and Power — backend API

Async FastAPI service that sits between the PostgreSQL + pgvector database and the
dashboard UI. **The UI talks only to this API**; this API is the only component
that holds database credentials (read from the `pangea-db-credentials` Secret).

## Endpoints

Read-only except where noted.

| Method & path | Returns |
|---------------|---------|
| `GET /api/health` | Liveness + DB connectivity |
| `GET /api/fleet/status` | Turbine counts by status, live vs rated MW |
| `GET /api/sites` | Site list |
| `GET /api/turbines` | Turbine list (filters: `status`, `site_code`) |
| `GET /api/turbines/{code}` | Single turbine |
| `GET /api/turbines/{code}/readings?hours=24` | Recent sensor readings for charts |
| `GET /api/alerts?status=active` | Alert feed (merged with the live wind cut-out alert) |
| `GET /api/solar/predict` | Predicted vs simulated-actual output per solar site |
| `GET /api/solar/forecast/{site_code}` | Hourly solar forecast detail |
| `GET /api/wave/predict` | Predicted vs actual output per wave farm |
| `GET /api/wave/forecast/{site_code}` | Hourly wave forecast detail |
| `GET /api/wind/status` | Live wind speed per site + cut-out shutdown state |
| `GET /api/operators` | Active roster: shifts, leave, upcoming assignments. `?include_on_leave=false` narrows it to who can actually be given work |
| `GET /api/operators/availability` | Resolved day-by-day rota: shift, window, capacity, booked and free hours, jobs. `?days=` `?code=` |
| `GET /api/shifts` | Clock hours per shift (`day` 07:00-19:00, `night` 19:00-07:00) |
| `GET /api/maintenance` | Work orders |
| **`POST /api/maintenance`** | **Create a work order** — the only write in the API. Rejects (409) an assignment the operative's rota cannot take |
| `POST /api/voice/transcribe` | Audio → text (proxies Whisper on KServe) |
| `POST /api/voice/speak` | Text → WAV (proxies the `pangea-tts` Kokoro gateway) |
| **`POST /api/agent/actions/{id}/confirm`** | **Carry out a proposed write** — goes through `POST /api/maintenance`, so the rota still applies |
| `POST /api/agent/actions/{id}/cancel` | Discard a proposal |
| `GET /api/agent/actions` | Audit trail: what was proposed, and what became of it |
| `GET /api/agent/health` | Assistant LLM reachability |
| `POST /api/agent/chat` | Chat with Pangea (blocking) |
| `POST /api/agent/chat/stream` | Chat with Pangea (SSE token stream) |

Interactive docs at `/docs` once running.

## Layout

```
backend/
├── app/
│   ├── main.py            # app, CORS, lifespan (pool up/down), router wiring
│   ├── config.py          # env-driven settings (DB creds from env/Secret)
│   ├── db.py              # asyncpg connection pool + fetch helpers
│   ├── models.py          # Pydantic v2 response shapes
│   ├── laya.py            # tool-router client + context slicing for the assistant
│   └── routers/           # fleet, turbines, alerts, solar, wave, wind,
│                          # operators, maintenance, voice, agent
├── requirements.txt
└── Containerfile          # UBI9 python-311 base (OpenShift-friendly)
```

The chart is at `deploy/charts/pangea-api/` — Deployment and Service, with
every other chart in the project. There is no Route: the browser reaches the
API through pangea-ui, which proxies /api on the same origin.

## Run locally (against a port-forwarded DB)

```bash
oc port-forward svc/pangea-db 5432:5432 &     # in the pangea namespace

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export POSTGRES_HOST=localhost POSTGRES_PORT=5432 \
       POSTGRES_DB=pangea POSTGRES_USER=pangea_app \
       POSTGRES_PASSWORD=$(oc get secret pangea-db-credentials \
         -o jsonpath='{.data.database-password}' | base64 -d)
uvicorn app.main:app --reload --port 8080

curl localhost:8080/api/fleet/status
```

## Deploy on OpenShift

The DB image is prebuilt, but this API is our own code, so it has to be built
once. Both paths below need **project-level permissions only**.

### 1. Build & push the image

**Option A — OpenShift internal build (no external registry):**

```bash
oc project <your-project>

# Create the build config once, then build from the local backend/ dir.
# The patch is required: this directory has a Containerfile, not a Dockerfile.
oc new-build --name pangea-api --binary --strategy=docker
oc patch bc/pangea-api --type=merge \
  -p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"Containerfile"}}}}'
oc start-build pangea-api --from-dir=. --follow
```

This produces `image-registry.openshift-image-registry.svc:5000/<your-project>/pangea-api:latest`,
which is what the chart pulls — the project name comes from the Helm release
namespace.

**Option B — build locally and push to a registry (e.g. Quay):**

```bash
podman build -t quay.io/<you>/pangea-api:0.1.0 -f Containerfile .
podman push quay.io/<you>/pangea-api:0.1.0
```

### 2. Install the chart

```bash
# Option A (internal registry) — defaults already point at it:
helm install pangea-api ../deploy/charts/pangea-api

# Option B (external registry):
helm install pangea-api ../deploy/charts/pangea-api \
  --set image.repository=quay.io/<you>/pangea-api --set image.tag=0.1.0

oc rollout status deployment/pangea-api
```

### 3. Verify

```bash
oc exec deployment/pangea-api -- curl -s localhost:8080/api/health

# or from your machine
oc port-forward svc/pangea-api 8080:8080 &
curl localhost:8080/api/fleet/status
```

## Configuration

Everything lives in `templates/deployment.yaml` — Deployment and Service —
with these fixed settings: image
`image-registry.openshift-image-registry.svc:5000/<release namespace>/pangea-api:latest`,
DB host `pangea-db:5432`, credentials from Secret `pangea-db-credentials`
(`database-name/-user/-password`), `CORS_ORIGINS=*` and 1 replica.

When the image moves to a public registry, edit the one `image:` line in
`templates/deployment.yaml`.

### MLflow tracing

Optional, and off by default. `MLFLOW_TRACKING_URI` ships empty because the
tracking server is per-cluster; until it is set the API logs `mlflow tracing
off: MLFLOW_TRACKING_URI is unset` and runs normally.

Find the MLflow route on your cluster:

```bash
oc get route -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,HOST:.spec.host \
  | grep -i mlflow
```

On RHOAI it is usually fronted by the dashboard route with a `/mlflow` path
rather than a route of its own.

Two values have to be set together. `MLFLOW_WORKSPACE` names the workspace on
the tracking server and is sent as a request header; the URI alone is not
enough, and a wrong workspace fails at trace time rather than at startup. It
defaults to the Helm release namespace, which is only right if your MLflow
workspace has the same name as your OpenShift project. List the workspaces you
can see, then set both:

```bash
MLFLOW_HOST=$(oc get route -A -o jsonpath='{range .items[?(@.metadata.name=="rhods-dashboard")]}{.spec.host}{end}')
TOKEN=$(oc create token pangea-mlflow)

curl -sk -H "Authorization: Bearer ${TOKEN}" \
  "https://${MLFLOW_HOST}/mlflow/api/2.0/mlflow/experiments/search?max_results=5"
```

```bash
oc patch configmap pangea-api-config --type merge \
  -p '{"data":{"MLFLOW_TRACKING_URI":"https://'"${MLFLOW_HOST}"'/mlflow",
               "MLFLOW_WORKSPACE":"<your-workspace>"}}'
oc rollout restart deploy/pangea-api
```

Confirm it took — the log line names the resolved URI:

```bash
oc logs deploy/pangea-api | grep 'mlflow tracing'
```

Auth is a bearer token. The `pangea-mlflow` ServiceAccount token is mounted and
used automatically; override it with the `pangea-mlflow-token` Secret if your
tracking server wants different credentials. The token only needs to be
accepted by the tracking server's oauth-proxy — it does not need namespace
admin, even though the chart's RoleBinding currently grants it.

## Model serving (wired)

The API is a proxy to models served on OpenShift AI / KServe; it holds no model
weights and no heavy inference dependencies.

| Model | Endpoint here | Served as |
|-------|---------------|-----------|
| Solar output (ONNX) | `/api/solar/predict` | MLServer, KServe v2 `/v2/models/solar-output/infer` |
| Whisper `tiny` (STT) | `/api/voice/transcribe` | vLLM predictor, OpenAI audio API |
| Kokoro v1.0 (TTS) | `/api/voice/speak` | MLServer ONNX + the `pangea-tts` glue gateway |
| Granite 4.0 350M (assistant) | `/api/agent/chat{,/stream}` | vLLM CPU predictor, OpenAI chat-completions |
| `laya` tool router | used internally by `/api/agent/chat` | KServe `:predict` classifier |

Wave output is computed from a physics model in `wave.py` rather than a served
model. All endpoints degrade to an error/empty response if their model is down.

## Not yet wired

- **RAG over pgvector** — `knowledge_base.embedding` is NULL for every row; no
  embedding model is deployed and there is no search endpoint.
- **Agent tool-calling and writes** — the assistant is grounded by context
  injection only. It cannot create work orders, book operatives, or acknowledge
  alerts; see `assistant-architecture.md` §6 and phase P2.
- **Predictive maintenance** — `ml_predictions` exists and is empty.
