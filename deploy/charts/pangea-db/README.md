# pangea-db Helm chart

PostgreSQL 15 + pgvector for **Pangea Energy and Power**, packaged to deploy on
OpenShift with **no image build** — it pulls the prebuilt community pgvector image
(`docker.io/pgvector/pgvector:pg15`, PostgreSQL-licensed) and applies the schema
and demo fleet from a ConfigMap the first time the database initialises.

Deployable with **project-level permissions only** — no cluster-admin, no
OperatorHub, no BuildConfig.

## What it deploys

| Resource | Purpose |
|----------|---------|
| `ConfigMap` (`pangea-db-initdb`) | Schema + demo fleet, mounted at `/docker-entrypoint-initdb.d`. Auto-runs on first init. |
| `Secret` (`pangea-db-credentials`) | Database name, user, password (from `values.yaml`). |
| `StatefulSet` (1 replica) | The database + a PVC for `/var/lib/postgresql/data`. |
| `Service` (ClusterIP) | In-cluster endpoint on port 5432. |

## Install

```bash
oc project <your-project>          # or: oc new-project pangea
helm install pangea-db deploy/charts/pangea-db
oc rollout status statefulset/pangea-db
```

That's it — no `oc start-build`. First start applies the schema and demo fleet, so the
pod takes a little longer to become ready the first time.

## Verify

```bash
oc exec statefulset/pangea-db -- \
  psql -U pangea_app -d pangea -c "SELECT status, count(*) FROM turbines GROUP BY status;"
```

You should see 81 operational / 6 maintenance / 3 offline.

## Configuration

Names are fixed to `pangea-db`. Knobs in `values.yaml`:

| Value | Default | Notes |
|-------|---------|-------|
| `image.repository` / `image.tag` | `docker.io/pgvector/pgvector` / `pg15` | Prebuilt Postgres 15 + pgvector. |
| `storageSize` | `10Gi` | Data volume size. |
| `storageClass` | `""` | Empty = cluster default. |
| `credentials.database` | `pangea` | Application database. |
| `credentials.user` | `pangea_app` | Created user; owns the schema (superuser in this image). |
| `credentials.password` | generated | Empty by default: a random 24-character password is generated at install and reused on upgrade. Set it explicitly to choose your own. |

```bash
# Larger volume on a specific StorageClass
helm install pangea-db deploy/charts/pangea-db \
  --set storageSize=50Gi --set storageClass=gp3-csi
```

## OpenShift specifics

The pgvector image is based on the official (Debian) Postgres image, which needs
care under OpenShift's default `restricted-v2` SCC (random UID). The chart handles
this without any special SCC or `fsGroup`:

- **`PGDATA` is a sub-directory** (`/var/lib/postgresql/data/pgdata`) of the mounted
  PVC. The volume root is owned `root:<fsGroup>` and group-writable, so the random
  UID can create and own the `pgdata` sub-dir — which is what `initdb` requires.
- **The socket directory** `/var/run/postgresql` is an `emptyDir`, so it's writable
  regardless of UID.

If the pod fails to initialise with a permissions error on the data directory, the
cause is almost always a StorageClass/CSI driver that doesn't apply `fsGroup`
ownership to the volume — check with your platform team.

## SQL source

The schema and seed SQL live in this chart under `files/init/` and `files/seed/`
and are rendered into the `pangea-db-initdb` ConfigMap at install time. They are
the single source of truth for the database.

## Uninstall

```bash
helm uninstall pangea-db
# The StatefulSet's PVC is retained by design; delete data explicitly:
oc delete pvc -l app.kubernetes.io/name=pangea-db
```
