# Mark

Mark is an AI advisor for landlords who own or manage property in Chicagoland. He answers only
from material you give him: your websites, specific YouTube episodes, and your podcast. Nothing
else. When a question falls outside that material he says so instead of guessing, and every
claim he makes points back to the episode or page it came from.

Mark is an AI assistant written in the conversational style of Mark Ainley (Straight Up Chicago
Investor). He is not Mark Ainley, and he is not a lawyer.

## How it works

```
sources/sources.yaml          you list websites, YouTube episodes, and the podcast
        │
        ▼  mark ingest        fetches pages, captions and transcripts
data/markai.db                text split into passages, searchable (SQLite)
        │
        ▼  a question         keyword search, plus semantic search if enabled
retrieved passages
        │
        ▼  Claude             Mark's system prompt + only those passages
a cited answer                [S1] markers become footnotes with links and timestamps
```

## Quick start

```bash
bash setup.sh                 # creates .venv, installs, runs mark init
```

Or by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
mark init                     # asks for your Anthropic API key, writes .env
```

Then:

1. Open `sources/sources.yaml` and list your material. See `sources/README.md` for what goes
   where, and `WHAT_I_NEED_FROM_YOU.md` for the full checklist.
2. `mark ingest` — fetches everything and builds the knowledge base.
3. `mark chat` for the terminal, or `mark serve` for a browser page.

## Commands

| Command | What it does |
|---|---|
| `mark init` | Writes `.env` and a starter `sources.yaml`, creates the data folders |
| `mark doctor` | Checks the key, the manifest, the data folder and the knowledge base |
| `mark sources validate` | Confirms `sources.yaml` parses, and warns about tokens in URLs |
| `mark sources validate --check-urls` | Also confirms every listed domain resolves, before you spend an hour ingesting |
| `mark sources probe URL` | Fetches one URL and says exactly what came back, and why it did or did not read |
| `mark sources list` | Lists what is currently in the knowledge base |
| `mark sources match` | Shows which transcript file each podcast episode resolved to |
| `mark ingest` | Fetches every source and builds the knowledge base |
| `mark ingest --dry-run` | Shows the plan (including transcription time) and stops |
| `mark ingest --only youtube` | Limits to one kind; repeatable |
| `mark ingest --force` | Re-ingests everything, and re-reads YouTube channels for new uploads |
| `mark ingest --prune` | Deletes stored sources no longer listed in the manifest |
| `mark status` | What Mark knows, which model, whether the key is set |
| `mark embed` | Adds semantic search to material already ingested, no re-download |
| `mark gaps` | Questions Mark could not answer, so you know what to add |
| `mark search "deposits"` | Searches the knowledge base directly, without calling Claude |
| `mark ask "..."` | One question, one answer, with sources |
| `mark chat` | A conversation in the terminal (`/reset`, `/sources`, `/quit`) |
| `mark serve` | The browser chat page |
| `mark calc mortgage` | Monthly principal and interest |
| `mark calc deal` | Cash flow, cap rate, cash-on-cash, DSCR, the 1% rule |

## Configuration

Everything lives in `.env`. Only the first line is required.

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Required. Your Anthropic key |
| `VOYAGE_API_KEY` | none | Optional. Turns on semantic search |
| `MARKAI_MODEL` | `claude-opus-5` | The Claude model |
| `MARKAI_EFFORT` | `medium` | Reasoning depth: low, medium, high, xhigh, max |
| `MARKAI_MAX_TOKENS` | `16000` | Ceiling per answer |
| `MARKAI_EMBEDDING_MODEL` | `voyage-3.5` | Embedding model, when a Voyage key is set |
| `MARKAI_DATA_DIR` | `data` | Where the knowledge base and downloads live |
| `MARKAI_SOURCES_FILE` | `sources/sources.yaml` | Which manifest to read |
| `MARKAI_TOP_K` | `8` | Passages handed to Mark per question |
| `MARKAI_MIN_RELEVANCE` | `2.0` | Below this, a question counts as uncovered |
| `MARKAI_WEAK_RELEVANCE` | `5.0` | Below this, coverage is reported as weak |
| `MARKAI_YOUTUBE_LANGUAGES` | `en,en-US` | Caption languages to try, in order |
| `MARKAI_CRAWL_DELAY_SECONDS` | `0.5` | Politeness delay between page fetches |
| `MARKAI_MAX_PAGE_BYTES` | `25000000` | How much of a page to read; bigger pages are truncated |
| `MARKAI_TRANSCRIBE_MODEL` | `small` | Whisper model size for podcast audio |
| `MARKAI_WEB_HOST` | `127.0.0.1` | Where the web UI binds |
| `MARKAI_WEB_PORT` | `8000` | Web UI port |
| `MARKAI_WEB_ACCESS_CODE` | none | If set, the web UI requires this code |
| `MARKAI_MAX_QUESTION_CHARS` | `4000` | Longest question the web UI accepts |
| `MARKAI_DAILY_QUESTION_LIMIT` | `500` | Global cap per UTC day, protects your bill |
| `MARKAI_PER_SESSION_QUESTION_LIMIT` | `40` | Cap per conversation |

Relative paths resolve against the project folder, so `mark` works from any directory.

## How Mark behaves

**Adding a Voyage key later is free.** Ingest first, add the key whenever you like: the next
`mark ingest`, or `mark embed`, embeds the passages already on disk. Nothing is downloaded twice.

**YouTube channels.** List a whole channel under `youtube.channels` and Mark works out the
video list itself, caching it so re-runs are instant. `mark ingest --force` re-reads the
channel and picks up anything you have published since.

**Only your material.** Rules, deadlines, dollar amounts, market numbers and local practice all
have to come from the sources you supplied. When they don't cover a question Mark says "That's
not covered in my training materials," then names the closest thing he has, or offers to run
the numbers, or flags the gap for you. Reasoning and arithmetic are his own; local facts are not.

**Citations.** Claims carry `[S1]` markers that become footnotes with the episode number, the
publication date, a timestamp, and a link. YouTube citations link to the exact moment. When a
source is more than about two years old and the question is about law or taxes, Mark says so.

**Legal questions** end with this sentence, word for word:

> I'm not a lawyer, and this isn't legal advice. You should consult with an Illinois real estate
> attorney to confirm this applies to your situation.

That is enforced in code, not just asked for in the prompt.

**Fair housing.** If a question asks how to screen out a protected class, or how to remove a
tenant without going through the courts, Mark declines that part and redirects to lawful,
uniform criteria and the court process. Also enforced in code.

**Geography.** Outside Illinois, Mark says it is outside his area. Illinois but outside
Chicagoland, he notes the limitation and shares what his sources cover.

**PDFs.** A PDF linked from a listed site is read like any other page; a lot of housing
material (the RLTO summary, EPA lead-paint pamphlets, HUD forms) is published that way. Scanned
PDFs have no selectable text, so Mark says it needs OCR rather than storing a blank page. Images,
archives and stylesheets are never followed.

**Untrusted sources.** Text pulled from web pages and transcripts is escaped and labelled as
reference material. If a page contains something shaped like an instruction, Mark treats it as
data, not as an order.

## Data and privacy

- Sources, transcripts, audio, embeddings and the database stay in `data/`, which is excluded
  from version control.
- What leaves the machine: your question, the conversation so far, the retrieved passages, and
  tool results, sent to Anthropic. Passage and query text go to Voyage only if you set that key.
- No telemetry, no analytics, no crash reporting.
- Conversations are held in memory and disappear when the process stops.
- Questions are logged locally (text, coverage, token counts) so `mark gaps` can show you what
  material to add. Answers are not logged. Nothing above DEBUG level records question content.

## Before you share the link with landlords

`mark serve` binds to `127.0.0.1` by default, which means only your machine can reach it. If you
bind anywhere else:

- Set `MARKAI_WEB_ACCESS_CODE`. Without it, `mark serve --host 0.0.0.0` refuses to start unless
  you pass `--yes`, because anyone who can reach the port can spend your API budget.
- Put it behind a reverse proxy with HTTPS and a real login for anything public.
- Check `MARKAI_DAILY_QUESTION_LIMIT` and `MARKAI_PER_SESSION_QUESTION_LIMIT`.
- `MARKAI_EFFORT=medium` is the default because it keeps chat quick and cheap. `high` is better
  for hard analysis and costs more.
- Talk to your attorney about whether Illinois brokerage advertising rules mean the page needs a
  sponsoring-broker line in the banner.

## Adding material later

Add the entry to `sources/sources.yaml` and run `mark ingest`. Unchanged sources are skipped, so
re-running is cheap. Removing an entry does not delete what was stored: use `mark ingest --prune`
for that. Mark's knowledge grows every time you add something.

## Optional extras

```bash
pip install "markai[transcribe]"   # local speech-to-text for podcast audio
```

Voyage AI is not a package install, just a key in `.env`. It adds semantic search on top of
keyword search.

## Troubleshooting

| Problem | What to do |
|---|---|
| "No captions available" for a video | Captions are off. Add a `transcript_file` for that episode, or use the podcast audio |
| "YouTube is rate-limiting this machine" | Wait an hour and re-run, or supply transcript files |
| "ANTHROPIC_API_KEY is not set" | Run `mark init`, or add the key to `.env` |
| Ingest wants to transcribe for hours | Run `mark ingest --dry-run` to see the estimate. Send transcripts or list the YouTube versions instead |
| Answers say "not covered" too often | Check `mark status` for the chunk count, then `mark gaps` to see what is missing |
| A page came back empty | Run `mark sources probe <url>`. It says whether the page is JavaScript-rendered, blocked, or fine |
| A whole site ingested nothing | `mark sources probe` its homepage first, then `mark sources validate --check-urls` |
| Lots of failures and no idea why | Every run writes `data/last-ingest.txt` with each page and its reason |

## Project layout

```
markai/
  config.py           settings from .env
  models.py           the shared data types
  sources/manifest.py the sources.yaml schema
  ingest/             websites, YouTube, podcast, and the pipeline
  knowledge/          chunking, embeddings, SQLite store, retrieval
  advisor/            guardrails, calculators, prompt assembly, the Claude call
  cli.py              the mark command
  web/                FastAPI app and the browser page
prompts/              Mark's system prompt
sources/              your manifest and its documentation
tests/                214 tests, no network required
```

## Development

```bash
source .venv/bin/activate
ruff check . && ruff format --check .
pytest -q
```

## What this is not

Not legal advice. Not a substitute for an Illinois real estate attorney, an accountant, or your
own judgment about a building. And not Mark Ainley: it is an AI assistant written in his style,
trained only on the material you chose to give it.
