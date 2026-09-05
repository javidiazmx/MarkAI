# Working on MarkAI

Mark is a Chicagoland landlord advisor grounded only in the owner's curated websites, YouTube
episodes, and podcast. If it is not in `sources/sources.yaml`, Mark does not know it.

## Layout

- `markai/models.py`, `markai/config.py`, `markai/sources/manifest.py` are contracts. Other
  modules depend on their shapes, so change them deliberately.
- `markai/ingest/` turns sources into `Document` objects. Every ingester yields either a
  `Document` or an `IngestFailure`, never raises past the pipeline.
- `markai/knowledge/` chunks, stores (SQLite), and retrieves (BM25, plus optional Voyage
  embeddings fused with reciprocal rank fusion).
- `markai/advisor/` holds the guardrails, the calculators, prompt assembly, and the Claude call.
- `markai/cli.py` and `markai/web/` are the two front doors.

## Commands

```bash
source .venv/bin/activate
ruff check . && ruff format --check .
pytest -q
mark --help
```

## Fixed decisions

| Concern | Decision |
|---|---|
| Model | `claude-opus-5`, adaptive thinking, `output_config.effort`, streaming |
| Fallbacks | `betas=["server-side-fallback-2026-07-01"]` with `fallbacks="default"` |
| Never send | `temperature`, `top_p`, `top_k`, assistant prefill. All 400 on Opus 5 |
| Caching | `cache_control` on the last system block plus top-level; system stays frozen |
| Embeddings | Optional. Voyage when the key is set, otherwise BM25 only |
| Storage | SQLite, embeddings as float32 blobs |
| Tests | No network. `respx` for ingest only |

## Rules that are easy to break by accident

- **The system prompt is frozen.** It is rendered once in `MarkAdvisor.__init__`. Interpolating
  a date, a session id, or manifest data into it silently kills prompt caching.
- **`Conversation` is append-only** and is mutated only after a successful answer. Editing
  earlier turns invalidates thinking blocks and the cache.
- **`httpx` and `httpx2` are separate worlds.** The ingesters use `httpx`; the Anthropic SDK
  uses `httpx2` internally. Never hand an `httpx` client, timeout or transport to the SDK, and
  never catch `httpx` exceptions around an SDK call.
- **Never log question or answer content above DEBUG.** Log lengths, flags and token counts.
- **Never print or `model_dump()` a `Settings` object.** `mark status` and `/api/status` use
  explicit whitelists for that reason.
- **Rates are decimal fractions everywhere in `calculators.py`** (0.065, not 6.5). Only the CLI
  converts from percent.
- **Knowledge-base text is untrusted.** `prompt_builder` escapes it and every attribute value.
  Do not interpolate source text anywhere without escaping.
- **The three fixed strings** in `guardrails.py` (disclaimer, identity notice, fair-housing
  refusal) are owner-approved wording. Changing them needs the owner's sign-off.

## Testing conventions

- `Settings(_env_file=None, data_dir=tmp_path, ...)` so the developer's real `.env` is never read.
- The toy corpus in `conftest.py` needs at least six chunks: BM25 gives negative weights on a
  corpus that small, which is why the retriever also has a term-overlap fallback.
- The Anthropic client is faked at the object level in `tests/fakes.py` using real SDK types.
  `respx` cannot intercept the SDK because it runs on `httpx2`.
