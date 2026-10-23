# granite-4.0-350m modelcar

Modelcar (OCI image carrying the model under `/models`) for IBM
**Granite 4.0 350M** (dense, Apache-2.0) served on the vLLM CPU runtime via
KServe.

## Excluded from git

The model weights and large tokenizer blobs are **not** committed (they exceed
GitHub's size limit and are re-downloadable). Fetch them from Hugging Face
before building:

```bash
# requires: pip install huggingface_hub
hf download ibm-granite/granite-4.0-350m \
  model.safetensors tokenizer.json vocab.json merges.txt \
  --local-dir .
```

The files kept in git (Containerfile + small configs) plus the four fetched
files above are everything the `COPY` in the Containerfile needs.

## Build & push

```bash
podman build --platform linux/amd64 -t quay.io/sara_banderby/pangea-power:granite-350m .
podman push quay.io/sara_banderby/pangea-power:granite-350m
```

Reference from the InferenceService as
`storageUri: oci://quay.io/sara_banderby/pangea-power:granite-350m`.
