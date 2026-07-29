# YAML source pointers

One small YAML file per external dataset, API, database, or shared folder.
This is how the hub **federates** data that already lives elsewhere — it
registers a pointer instead of copying the data (the CDH federation model).

## Minimal example

```yaml
id: chirps-rainfall-africa
title: CHIRPS daily rainfall (Africa)
type: remote_dataset          # remote_dataset | api | database | file_share
url: https://data.chc.ucsb.edu/products/CHIRPS-2.0/
```

## Full example

See `examples/chirps_rainfall.yaml` and `examples/aclimate_api.yaml`.

## Fields

| Field | Required | Notes |
|---|---|---|
| `id` | yes | unique, kebab-case |
| `title` | yes | human-readable name |
| `type` | yes | `remote_dataset`, `api`, `database`, `file_share` |
| `url` | yes | URL, connection string, or UNC path |
| `description` | no | what it is and why it matters |
| `format` | no | `geotiff`, `csv`, `json`, `parquet`, ... |
| `access` | no | `open` (default), `internal`, `restricted` |
| `update_frequency` | no | `daily`, `weekly`, `monthly`, `annual`, `static` |
| `owner` | no | `{team, contact}` |
| `license` | no | e.g. `CC-BY-4.0` |
| `hub_role` | no | `federation` (default), `ingest`, `derived`, `reference` |
| `tags` | no | list of keywords |
| `spatial_coverage` / `temporal_coverage` | no | free text |
| `auth_env_var` | no | name of an env var holding the credential — **never put secrets in the YAML itself** |

Any other field is kept as `extra` metadata, so teams can extend the schema
without code changes.

Validate pointers any time with:

```
python -m hub.sources
```
