# Jay

Jay is an AI advisor for landlords who own or manage property in Chicagoland. He answers only
from material you give him: your websites, specific YouTube episodes, and your podcast. Nothing
else. When a question falls outside that material he says so instead of guessing, and every
claim he makes points back to the episode or page it came from.

Jay is an AI assistant written in the conversational style of Mark Ainley (Straight Up Chicago
Investor). He is not Mark Ainley, and he is not a lawyer.

## How it works

```
sources/sources.yaml          you list websites, YouTube episodes, and the podcast
sources/facts.yaml            optional: your ordinances with effective dates, and local costs
        │
        ▼  mark ingest        fetches pages, captions and transcripts
data/markai.db                text split into passages, searchable (SQLite)
        │
        ▼  a question         keyword search, plus semantic search if enabled
retrieved passages
        │
        ▼  Claude             the system prompt, your facts, and only those passages
an answer in Mark's voice     grounded in those passages, citations optional
```

## Quick start

```bash
bash setup.sh                 # creates .venv, installs, runs mark init
```

Or by hand, on macOS and Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
mark init                     # asks for your Anthropic API key, writes .env
```

On Windows, in `cmd`:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
mark init
```

`source` is a shell builtin that Windows does not have, and until the venv is active
Windows answers `'mark' is not recognized`. Two ways out, both fine:

```bat
.venv\Scripts\activate     :: then `mark ...` works for the rest of the session
.venv\Scripts\mark ...     :: or call it by path, no activation needed
python -m markai ...         :: or through the module, which never needs the PATH
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
| `mark sources missing URL` | Diffs the site's own sitemap against the knowledge base and names the pages that never made it |
| `mark sources list` | Lists what is currently in the knowledge base |
| `mark sources match` | Shows which transcript file each podcast episode resolved to |
| `mark ingest` | Fetches every source and builds the knowledge base |
| `mark ingest --dry-run` | Shows the plan (including transcription time) and stops |
| `mark ingest --only youtube` | Limits to one kind; repeatable |
| `mark ingest --force` | Re-ingests everything, and re-reads YouTube channels for new uploads |
| `mark ingest --prune` | Deletes stored sources this run did not produce — dropped pages, dead URLs, removed entries |
| `mark status` | What Mark knows, which model, whether the key is set |
| `mark audit` | Checks the knowledge base is usable: silent sources, duplicated text, missing embeddings, and whether real questions find anything. Calls Claude never |
| `mark embed` | Adds semantic search to material already ingested, no re-download |
| `mark gaps` | Where the sources came up short: nothing found, or a thin match |
| `mark gaps --forget-declined` | Clears the rows Jay declined but the sources covered |
| `mark feedback` | What landlords thought of the answers. A thumbs down is a content decision |
| `mark search "deposits"` | Searches the knowledge base directly, without calling Claude |
| `mark search --scores` | Same, showing the keyword and semantic scores behind the coverage verdict |
| `mark episodes "boilers"` | Which episodes cover a topic: number, guest, topics, and a link that lands on the minute |
| `mark episodes --guest "Jane Doe"` | The catalog, filtered to the episodes whose title names someone |
| `mark facts mine` | Reads every indexed source and proposes rules and prices out of them |
| `mark facts review` | Walks the proposals; the ones you accept go into `facts.yaml` |
| `mark facts validate` | Checks `sources/facts.yaml`: every ordinance cites something, the dates make sense |
| `mark facts list` | Every rule and price you maintain, and whether each applies today |
| `mark facts probe "..."` | Which of your facts a real question would put in front of Jay |
| `mark accounts list` | The landlords who filled in the form |
| `mark accounts list --csv leads.csv` | The same, exported. That file holds personal data |
| `mark leads setup` | Asks for the sending mailbox, checks the login, writes `.env` |
| `mark leads test` | Sends one fake lead, to prove the CRM wiring before a real one |
| `mark leads list` | Every lead and whether your CRM has it yet |
| `mark leads send` | Push whatever is waiting, `--retry-all` after fixing a URL |
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
| `MARKAI_EMBEDDING_TOKENS_PER_MINUTE` | `0` | Token budget per minute; 0 lets the run discover it |
| `MARKAI_EMBEDDING_REQUESTS_PER_MINUTE` | `0` | Request budget per minute; 0 lets the run discover it |
| `MARKAI_DATA_DIR` | `data` | Where the knowledge base and downloads live |
| `MARKAI_SOURCES_FILE` | `sources/sources.yaml` | Which manifest to read |
| `MARKAI_SHOW_CITATIONS` | `false` | `[S1]` markers and a source list under the answer |
| `MARKAI_CACHE_TTL` | `1h` | How long the cached system prefix lives: `5m` or `1h` |
| `MARKAI_FAST_MODE` | `false` | Opus 5 fast mode: up to 2.5x output speed at double the token price |
| `MARKAI_TOP_K` | `8` | Passages handed to Mark per question |
| `MARKAI_MIN_RELEVANCE` | `2.0` | Below this, a question counts as uncovered |
| `MARKAI_WEAK_RELEVANCE` | `5.0` | Below this, coverage is reported as weak |
| `MARKAI_YOUTUBE_LANGUAGES` | `en,en-US` | Caption languages to try, in order |
| `MARKAI_CRAWL_DELAY_SECONDS` | `0.5` | Politeness delay between page fetches |
| `MARKAI_YOUTUBE_DELAY_SECONDS` | `2.0` | Pause between caption requests; raise it if YouTube keeps blocking |
| `MARKAI_YOUTUBE_PROXY_URL` | none | Send caption requests through a proxy, for when YouTube has blocked your address |
| `MARKAI_WEBSHARE_USERNAME` | none | Webshare rotating-proxy user; takes precedence over the plain proxy |
| `MARKAI_WEBSHARE_PASSWORD` | none | Webshare password |
| `MARKAI_YOUTUBE_COOKIES_FROM_BROWSER` | none | Read cookies from an installed browser: `firefox`, `edge`, `chrome`… Nothing to install |
| `MARKAI_YOUTUBE_COOKIES_FILE` | none | `cookies.txt` exported from a signed-in browser |
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

**The Voyage free tier works, slowly.** An account with no payment method is capped at 3
requests and 10,000 tokens a minute — and the default batch of 128 passages is about 60,000
tokens, six times over, so retrying it could never succeed. The first time Voyage says so, the
run adopts that budget by itself: batches are cut to fit 10,000 tokens and requests are paced
to stay inside the window. Nothing to configure. Reckon on three to four hours for a few
thousand passages. Progress is saved as it goes, so `mark embed` resumes rather than restarts,
and you can stop it whenever you like.

`MARKAI_EMBEDDING_TOKENS_PER_MINUTE` and `MARKAI_EMBEDDING_REQUESTS_PER_MINUTE` set the budget
by hand if you know your account's limits.

**YouTube channels.** List a whole channel under `youtube.channels` and Mark works out the
video list itself, caching it so re-runs are instant. `mark ingest --force` re-reads the
channel and picks up anything you have published since.

**When YouTube blocks you.** Pacing stops you being blocked; it does nothing once you already
are, because every request from that address fails.

There is a second route built in, and it needs no setup: yt-dlp, already a dependency because
it lists the channels, talks to YouTube over a different client. Whenever the caption library
is blocked, that route is tried before any waiting, and often just works.

If yt-dlp is blocked as well, it then tries cookies from each installed browser in turn —
Firefox, Edge, Chrome, Brave, Vivaldi, Opera, Chromium — and keeps whichever works for the rest
of the run. A browser that is not installed fails instantly, so the sweep is quick, and it runs
once per run rather than once per video. Still nothing to configure; close your browser first,
since some of them lock their cookie store while running.

Name one with `MARKAI_YOUTUBE_COOKIES_FROM_BROWSER` to skip the sweep, or point
`MARKAI_YOUTUBE_COOKIES_FILE` at an exported `cookies.txt` — treat that file as a password, it
is a live session.

When every route fails, the address itself is blocked and no amount of waiting or configuration
changes that. The failure names each route it tried. Move to another network — a phone hotspot
is the quickest test — or set `MARKAI_YOUTUBE_PROXY_URL`, or Webshare credentials for the
rotating pool the caption library recommends.

`mark doctor` shows which route is configured and never prints the credentials.

A large channel takes several sessions. Requests are paced by `MARKAI_YOUTUBE_DELAY_SECONDS`,
and a block is waited out — 30s, then 60s, 120s, 300s — before the video is retried, because
YouTube's throttling lifts on its own. Only when a block survives that ladder twice does the
run stop, and everything already fetched stays cached, so the next run resumes rather than
restarts.

**Only your material.** Rules, deadlines, dollar amounts, market numbers and local practice all
have to come from the sources you supplied. When they don't cover a question Mark says "That's
not covered in my training materials," then names the closest thing he has, or offers to run
the numbers, or flags the gap for you. Reasoning and arithmetic are his own; local facts are not.

**Answers, not footnoted reports.** By default Mark reads across everything retrieved, works
out what it means for the landlord asking, and says it the way it would be said on the phone —
no `[S1]` markers, no source list. The grounding is unchanged: rules, deadlines and dollar
amounts still have to come from the sources, and a question they don't cover still gets "That's
not covered in my training materials." What is hidden is the working, not the requirement.

Set `MARKAI_SHOW_CITATIONS=true` to get the other behaviour: `[S1]` markers that become
footnotes with the episode number, publication date, timestamp and link, YouTube ones pointing
at the exact moment. Useful while checking what Mark is drawing on. Either way, when a source is
more than about two years old and the question is about law or taxes, Mark says so.

**The identity notice appears with the answers that need it**, not permanently at the top of
the page. An answer flagged legal carries it underneath; the header says "AI, not a lawyer" at
all times, because someone should never be unsure whether they are talking to a person.

**Legal questions** end with this sentence, word for word:

> I'm not a lawyer, and this isn't legal advice. You should consult with an Illinois real estate
> attorney to confirm this applies to your situation.

That is enforced in code, not just asked for in the prompt.

**Fair housing.** If a question asks how to screen out a protected class, or how to remove a
tenant without going through the courts, Mark declines that part and redirects to lawful,
uniform criteria and the court process. Also enforced in code.

**Spanish.** Ask in Spanish and Mark answers in Spanish. The guardrails read both languages:
the disclaimer, the fair-housing refusal and the geography notes fire on "¿cómo desalojo a un
inquilino?" exactly as they do on the English question. Accents are folded before matching, so
spelling a word with or without them makes no difference. The three fixed strings stay in their
owner-approved English wording whatever language the answer is in.

**Geography.** Outside Illinois, Mark says it is outside his area. Illinois but outside
Chicagoland, he notes the limitation and shares what his sources cover.

**Podcast episodes without a transcript.** The feed's own show notes are stored as the
episode's text — guests, topics, timestamps, links. That is real material and it is already in
the feed, so a show whose host publishes no transcripts still contributes an episode-level
record with its number, date and link. A one-line teaser is not enough to store and is reported
as a failure instead.

**Sites that publish a sitemap.** Set `from_sitemap: true` on a source and its page list comes
from the site's own index rather than from following links. That is the right mode for a blog:
older posts are typically reachable only through paginated index pages, so a link-following
crawl stops after the first page, while the sitemap is complete by definition.
`include_patterns` narrows it to a section the same way. Verify with `mark sources missing`.

**PDFs.** A PDF linked from a listed site is read like any other page; a lot of housing
material (the RLTO summary, EPA lead-paint pamphlets, HUD forms) is published that way. Scanned
PDFs have no selectable text, so Mark says it needs OCR rather than storing a blank page. Images,
archives and stylesheets are never followed.

**Sites that refuse a bot.** Some council and county sites sit behind a firewall that answers
403 to any user agent it does not recognise as a browser, while their own robots.txt allows the
crawl. Mark introduces itself honestly first; if that exact request is refused with a 403, it
repeats it once presenting as a browser. robots.txt still decides whether a page may be read at
all — a `Disallow` is never retried, a 401 is never retried, and pages behind a login stay
unread.

**Untrusted sources.** Text pulled from web pages and transcripts is escaped and labelled as
reference material. If a page contains something shaped like an instruction, Mark treats it as
data, not as an order.

## Checking the ingest actually worked

Ingest counts say what was stored. They do not say whether a landlord's question finds it —
this project shipped a run that reported 895 pages added while the site's whole blog had been
silently discarded, and another where five identical footers came back for a question about
deposits. `mark audit` looks for that class of problem:

- **A listed source that contributed nothing.** A domain in `sources.yaml` with zero stored
  pages is a silent failure; the audit names it and hands you the `probe` command for it.
- **The same text stored twice**, and **text repeated across a quarter of the store** — a menu
  or footer that survived, diluting every passage.
- **Passages with no embedding**, as a share, so a half-finished `mark embed` is visible.
- **Documents that produced no searchable passages**, and pages under 40 words.
- **Twenty real landlord questions**, in English and Spanish, run through retrieval to see
  whether anything comes back. `--probes` shows each one and its best match.

It never calls Claude. The only paid call is one embedding per probe question when semantic
search is on — fractions of a cent for the whole run. It exits non-zero when something is
broken, so it works in a scheduled check.

## What a question costs

Only one part of a request repeats between questions: the system prompt and the business
block, about 1,800 tokens. That prefix carries the cache breakpoint. Everything else — the
retrieved passages and the question — is different every time.

Which is why the passages are **not** cached on a one-shot question. A cache write costs 1.25×
the input price, and a block that never gets sent again is never read back, so caching it is a
25% surcharge for nothing. The breakpoint goes on only when the same prefix really will be sent
again: inside a tool-call loop, or on the second turn of a conversation.

The prefix TTL is the one judgement call. A write costs 1.25× at `5m` and 2× at `1h`; a read
costs 0.1×. So `5m` pays for itself from the second question within five minutes, `1h` from the
third within an hour — and either one costs *more* than no cache at all if questions are spaced
further apart than that. The default is `1h`, which suits a chat session or a shared browser
page; set `MARKAI_CACHE_TTL=5m` if Mark is only asked the occasional isolated question.

`mark ask` prints reads and writes on the last line, so you can tell those apart. Writes with no
reads, question after question, means the TTL is shorter than the gaps between questions.

`MARKAI_EFFORT` is the other lever, and the bigger one: it trades thinking depth against tokens.
`medium` is the default because it keeps chat quick; `high` is worth it for hard analysis.

## Data and privacy

- Sources, transcripts, audio, embeddings and the database stay in `data/`, which is excluded
  from version control.
- What leaves the machine: your question, the conversation so far, the retrieved passages, and
  tool results, sent to Anthropic. Passage and query text go to Voyage only if you set that key.
- No telemetry, no analytics, no crash reporting.
- Conversations are held in memory and disappear when the process stops. The web page also
  saves its chat list to `data/conversations.db` so a landlord can reopen an earlier
  conversation: question and answer text, grouped by an id the browser keeps. It never
  leaves the machine, and deleting a conversation in the sidebar deletes the row.
  `mark chat` writes nothing.
- Properties a landlord adds in the sidebar go to `data/portfolio.db`, on the same machine
  and under the same owner, and ride along with each of their questions so an answer can be
  about their building. Removing one deletes the row.
- Their property log - expenses, bills, rent in, maintenance, visits, notes - goes to
  `data/ledger.db` under the same owner. It is what they told Jay about their own buildings,
  including amounts and vendors, so it is the most personal thing here after the lead form.
  It never reaches the log file and never leaves the machine except inside their own
  questions. Deleting an entry in the sidebar deletes the row.
- A new lead is queued in `data/leads.db` and POSTed to `MARKAI_CRM_WEBHOOK_URL` if one is
  set. That is the one place any of this leaves the machine on purpose, so whatever you
  point it at is where a landlord's contact details end up.
- The form writes a name, an email, a phone and a neighborhood to `data/accounts.db`. None
  of it reaches the log, and the only place it goes on purpose is your CRM;
  `mark accounts list` is how you read it. It is personal data about other people, so it comes with obligations the rest of this
  does not: say what you will do with it, do only that, and treat the export like your rent
  roll.
- All of these sit under `data/`, which is excluded from version control, and all of them
  are worth knowing about before this page goes in front of anyone but you.

## Mining facts out of the sources

Jay never learns a local fact on his own, and that line does not move. This is a different
thing: the facts are already in the sources, said out loud in a blog post or an episode,
and `mark facts mine` reads them out into proposals for `sources/facts.yaml`.

```bash
mark facts mine --free   # no API call, no cost: proposes the sentences themselves
mark facts mine          # asks Claude to read it, tells you the cost first, resumable
mark facts proposals     # the map: distinct rules, how many sources back each
mark facts review        # decide, with the sentence from your own source in front of you
mark facts validate      # then restart the server
```

**Reviewing is free.** `mark facts proposals` and `mark facts review` make no API call at
all - they read the proposals already on disk. `mark facts mine` is the only one of these
that spends anything, and `--free` is a version of it that spends nothing.

Three things make it safe to run over the whole corpus:

- **Every proposal quotes the sentence it came from, and the quote is checked against the
  passage before you see it.** A paraphrase, a merged sentence, or a number that drifted is
  thrown away and counted, not shown. A rule your sources never stated would arrive wearing
  their name, and you would have no way to tell.
- **Nothing is written while mining.** `facts.yaml` is yours; `mark facts review` is where
  you decide, and accepting appends the rule, its citation, and a link back to the page.
- **Your file keeps its comments.** Entries are inserted as text rather than dumped from a
  parser, and the key is never written twice - YAML keeps the last of two identical keys
  and silently drops the first, which would delete every rule you had written.

It only reads passages with a number and a binding word in them, because the rest is
commentary and reading it costs money for nothing. It tells you the estimated cost before
it starts, and picks up where it left off, so a run you stop is not a run you lose.

**The free path.** `mark facts mine --free` needs no API key and makes no call. It pulls out
the sentences that state a rule outright - a number and a word like "must" or "within",
both in the same sentence - and proposes each one as itself, so the rule and the quote are
the same text and it cannot misquote you. It finds less than the paid run: it will not turn
"about a month and a half" into 45 days, and it skips anything it is unsure about, because a
pattern cannot look at a weak passage and correctly decide there is nothing there. Worth
running first on new material. What it catches costs nothing, and what it misses is still
sitting in the sources for a paid run whenever you want one.

**Reviewing 1300 proposals.** Identical quotes collapse into one decision that says how many
of your sources stated it, and accepting it answers all of them. The queue goes by subject,
with subjects `facts.yaml` already covers at the back and labelled. Filter it and take a
slice in one go:

```bash
mark facts proposals                              # what is in there, by subject
mark facts review --min-sources 3 --accept-all    # the rules three of your sources agree on
mark facts review --kind cost --topic boiler     # or one subject at a time
```

In the one-at-a-time mode, `t` drops a whole subject without deciding it away. A cost
proposal with no number in it is never offered: "$0 to $0" in the fact block is not a
missing answer, it is a wrong one.

## The property log

A landlord's real questions are about their own buildings: did I pay that water bill, when
was somebody last out for the drain, how much have I put into 2145 this year. None of that
is in a blog post, so Jay keeps the record with them.

They mention something in passing - "the plumber came out Tuesday, $340" - and Jay writes it
down and confirms it in one line. Months later they ask about it in plain language and the
answer comes from what they said, not from a guess. Money is added up in code, never in the
model's head. Maintenance starts out open, so "what is still outstanding at the Berwyn
place?" has an answer, and the handoff to a property manager carries the open items with it.

The same log is a panel in the sidebar, under **What's going on**, so nothing is only
knowable by asking: add an entry by hand, tick one done, delete one. Jay can add, search,
total and close. Jay cannot delete - a model quietly dropping a landlord's records is not a
risk worth taking, so that stays a button on their page.

Six kinds cover what people say out loud: expense, bill, rent in, maintenance, visit, note.
Entries live in `data/ledger.db` under the same owner as their conversations, which means an
account when they have signed up and the browser before that.

## How an answer feels

Three things decide whether Jay reads as a person thinking or a machine stalling, and all
three are settings on one request:

- **The reasoning is streamed.** Opus 5 thinks before it writes, and the API's default
  (`display: "omitted"`) means a blank bubble for as long as that takes. With
  `display: "summarized"` the page shows the reasoning as it happens and folds it into
  "Thought for 4s" once the answer starts. It costs nothing: thinking is billed either way.
- **Effort is routed per question.** "How long do I have to return a deposit" is a lookup
  the sources answer outright, and it runs at `low`: faster to the first word and cheaper.
  Anything with money in it, a photo to read, or a request that has to be refused carefully
  runs at `high`. Everything else keeps `MARKAI_EFFORT`. The routing is in `effort_for`,
  and the test file is the specification.
- **The corpus is loaded at startup**, not inside the first question. Building the BM25
  index over every passage takes seconds, and paying that in the first question is the
  worst possible moment: it is the one where somebody decides whether this works.

Jay also remembers. Not a profile it invented: the titles of what this landlord asked
about before, which are their own words and already stored, so it costs nothing and lets
him say "same Berwyn unit as last week?". And the line between what he may reason about and
what he may not is now written out properly: local facts, deadlines, dollar amounts and
ordinances come from the sources or not at all, while how a boiler works, what DSCR means
and how to word a firm message to a tenant are his to answer like the professional he is.
The test in the prompt is "would being wrong about this hurt them in front of a judge, a
tenant, or an inspector".

Under every answer: copy it and rate it. No source list - the podcast is what Jay learned
from, not a reading list to hand back, and an answer that has to show its homework is not
finished. `mark feedback` is where the thumbs go, and a thumbs down is worth more than a
thumbs up: it names a question the sources answered badly.

Jay also does not narrate what he is missing. "That isn't in my materials, and the closest
thing I have is episode 472, which is about porches" is an inventory of the training set,
not an answer. He answers what the sources support, leaves the rest out, and hands over a
next step when there is nothing useful. That moved the gap signal where it belonged:
`mark gaps` now reads the retrieval rather than waiting for Jay to say a magic sentence,
and it lists thin matches as well as empty ones, because a thin match is exactly the near
miss the landlord no longer hears about.

## The lead form

Two questions get answered, then the page asks for a name, an email, a phone and the
neighborhood the rental is in. That form is the lead.
`MARKAI_FREE_QUESTIONS_BEFORE_SIGNUP` moves the line, `MARKAI_ACCOUNT_REQUIRED=false`
removes it. The terminal is never asked.

**There is no password**, and that is a decision rather than an omission. A password does
not verify an email, so it buys no lead quality; what it buys is a second device, and it
costs conversion at the moment somebody decides. Without email delivery it also has no
reset link, so every forgotten one lands in Mark's inbox. So identity here is the device:
filling the form issues an HttpOnly, SameSite=Lax cookie, that browser is remembered, and
the wall does not come back. A landlord on a second device fills the short form again,
which takes fifteen seconds.

That settles something the password version had to be careful about: one device never
inherits another's conversations, because nothing here claims to prove who anyone is. The
questions asked before the form are claimed by the row that form creates, so the sidebar
does not empty itself at the moment somebody commits.

Turn on `MARKAI_COOKIE_SECURE=true` the moment this is behind a domain rather than
`127.0.0.1`, or the cookie will travel over plain http. The access code
(`MARKAI_WEB_ACCESS_CODE`) is still the thing that decides who reaches the page at all.

The free-question count is kept apart from the saved conversations on purpose: deleting a
conversation does not hand back a free question. It is counted when an answer lands, so a
question that failed costs nothing. Someone who clears their browser storage does get
another two.

### Sending the lead to the CRM

**By email**, which is how LeadSimple takes one. Set `MARKAI_LEAD_EMAIL_TO` to the CRM's
inbound address and the `MARKAI_SMTP_*` lines to a mailbox that can send, and every signup
becomes one message:

```
Subject: New lead from Jay: Javier Diaz
Reply-To: javier@example.com

Name: Javier Diaz
Phone: (312) 555-0134
Email: javier@example.com
```

Three labelled lines and nothing after them. A parser on the far side reads that body, and
anything clever in it is a way to lose a lead, so the neighborhood and the question they
asked stay in `mark leads list` rather than going in the email. If your CRM parses more
than these three, they can be added.

`mark leads setup` asks for all of it, signs in to check the password before saving
anything, and writes it to `.env` itself. Use it rather than editing the file by hand: a
password typed into a prompt that does not echo is one that does not end up in a chat
window or a screenshot.

**Which mailbox sends it.** It does not have to be a mailbox of Jay's, and on Google
Workspace it should not be a new paid seat either:

1. Add `jay@gcrealtyinc.com` as an **alias** on a mailbox you already pay for, and set
   `MARKAI_SMTP_FROM` to the alias while `MARKAI_SMTP_USERNAME` stays the real account.
   Free, and the lead looks like it came from Jay.
2. Or just send as yourself. The CRM reads the body, not the sender, and `Reply-To` is the
   landlord either way.
3. Or use a sending service (Resend, SES, Mailgun) instead of a mailbox. More setup, one
   DNS record, but the credential is an API key scoped to sending that you can revoke
   without touching anybody's email.

On Google Workspace or Gmail, `MARKAI_SMTP_PASSWORD` is an **app password**, generated
under Account → Security → 2-Step Verification → App passwords. The account password will
be refused, and `mark leads setup` says so when it is.

**Or by webhook**, for a CRM with an inbound URL, or a Zapier or Make catch hook. Set
`MARKAI_CRM_WEBHOOK_URL` and it is POSTed a flat JSON lead with everything: name, email,
phone, neighborhood, what they asked about and how many questions they had.
`MARKAI_CRM_WEBHOOK_TOKEN` becomes an `Authorization: Bearer` header;
`MARKAI_CRM_WEBHOOK_HEADER` carries one custom header instead. Email wins if both are set.

Two things it will not do. It will not lose a lead: the lead is written to `data/leads.db`
first and delivered after, retried with backoff up to six times and retried again on the
next start, so an outage or a wrong URL costs time and nothing else. And it will not make a
landlord wait: delivery runs on a worker thread, off the path that answers a question.

```bash
mark leads test              # one obviously fake lead, to prove the wiring
mark leads list              # every lead, and whether the CRM has it
mark leads send              # push what is waiting
mark leads send --retry-all  # after fixing a URL, put the ones that gave up back
```

One person filling the form on their phone and their laptop reaches the CRM once: a
repeat email is recognised and not sent again.
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
re-running is cheap. Mark's knowledge grows every time you add something.

Removing something is the other half, and it needs `--prune`. A page that a later run rejects —
because it turned out to be nothing but site furniture, or the URL now 404s — stays in the
knowledge base until you prune, since the rejection alone does not delete what an earlier run
stored. `--prune` removes everything the run did not produce, restricted to the kinds it
actually covered, so `mark ingest --only website --prune` never touches your podcast episodes.

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
| "YouTube is rate-limiting this machine" | Wait an hour and re-run; already-fetched videos are cached. Raise `MARKAI_YOUTUBE_DELAY_SECONDS` to make it less likely |
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
tests/                no network required
```

## Development

```bash
source .venv/bin/activate        # Windows: .venv\Scripts\activate
ruff check . && ruff format --check .
pytest -q
```

## What this is not

Not legal advice. Not a substitute for an Illinois real estate attorney, an accountant, or your
own judgment about a building. And not Mark Ainley: it is an AI assistant written in his style,
trained only on the material you chose to give it.
