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
- `markai/web/ledger.py` is the landlord's own record of their buildings; `markai/advisor/log_tool.py`
  is the only way the model touches it.

## Commands

```bash
source .venv/bin/activate        # Windows cmd: .venv\Scripts\activate
ruff check . && ruff format --check .
pytest -q
mark --help                      # or `python -m markai --help`, no PATH needed
```

The owner runs this on Windows. Commands you hand over go in `cmd` form, or through
`python -m markai`, which works with or without the venv active.

## Fixed decisions

| Concern | Decision |
|---|---|
| Model | `claude-opus-5`, adaptive thinking, `output_config.effort`, streaming |
| Fallbacks | `betas=["server-side-fallback-2026-07-01"]` with `fallbacks="default"` |
| Never send | `temperature`, `top_p`, `top_k`, assistant prefill. All 400 on Opus 5 |
| Caching | `cache_control` on the last system block plus top-level; system stays frozen |
| Embeddings | Optional. Voyage when the key is set, otherwise BM25 only |
| YouTube channels | yt-dlp flat listing, cached per channel under `data/raw/youtube/` |
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
- **The legal disclaimer is the platform's, not the model's.** The prompt must never ask for
  a standing disclaimer: `guardrails` decides, appends it once per conversation where the
  surface has no standing notice, and strips one the model wrote anyway.
- **Identity in `markai/web/` is an owner id**, `account:<id>` when signed in and
  `browser:<id>` when not. Nothing stores anything against a bare browser id.
- **Never log an email, a phone, or a question.** `accounts.py` logs a signup id prefix and
  nothing else, and `crm.py` records an error class, never the payload it failed to send.
- **Mining is the only thing that spends.** `mark facts proposals` and `mark facts review`
  make no API call, and `mark facts mine --free` extracts the rule sentences with patterns
  instead of a model: no key, no call, quote and rule the same text.
- **A proposal has to be about renting property.** `is_landlord_business` in
  `facts_miner.py` sorts a city page's deadlines to the back: one unambiguous word, or two
  weak ones, judged on the quoted sentence rather than the label. Nothing is dropped
  without the owner asking (`mark facts drop --off-topic`), what is dropped is kept under
  `dropped` in the proposals file for `mark facts undrop`, and `mark facts why` prints the
  word the verdict turned on. The classifier is a judgement call, so it has to be
  inspectable and reversible.
- **The review queue is ranked by demand, not by confidence.** `group_proposals` takes the
  questions from `mark gaps` and the thumbs-down from `mark feedback` and puts the
  proposals that would have answered one of them first. A fact layer is worth what it
  answers; nobody is going to review nine hundred subjects.
- **`facts_miner` proposes, the owner disposes.** Every mined entry quotes its passage and
  the quote is verified against it; an unverified one is dropped, never shown. Nothing is
  written to `facts.yaml` outside `mark facts review`, and the section key is never written
  twice (YAML keeps the last and silently drops the first).
- **Jay never narrates a gap.** No "that isn't in my materials", no listing the closest
  source that did not fit. `mark gaps` reads `retrieval.coverage`, not the answer's
  wording, and counts `weak` as well as `none`.
- **Thinking is streamed with `display: "summarized"`.** The Opus 5 default is `omitted`,
  which the page renders as a blank bubble for several seconds. Never drop the display.
- **Effort is per question** (`effort_for` in `mark.py`), top-level rather than the
  per-message beta: a mid-conversation change costs one small message-cache rewrite, which
  beats a beta parameter that would 400 every request if its shape is ever wrong.
- **The property log is theirs, and the tool cannot delete from it.** `log_tool` exposes
  add, find, total and close - never delete, which stays a button on their page. Money is
  totalled in `Ledger.totals`, never by the model, and an entry is only ever what they said:
  nothing mined, inferred, or carried over from another owner.
- **The lead form has no password on purpose.** Identity is the device, through an HttpOnly
  cookie. One device never inherits another's conversations, because nothing here proves
  who anyone is.

## Testing conventions

- `Settings(_env_file=None, data_dir=tmp_path, ...)` so the developer's real `.env` is never read.
- The toy corpus in `conftest.py` needs at least six chunks: BM25 gives negative weights on a
  corpus that small, which is why the retriever also has a term-overlap fallback.
- The Anthropic client is faked at the object level in `tests/fakes.py` using real SDK types.
  `respx` cannot intercept the SDK because it runs on `httpx2`.
