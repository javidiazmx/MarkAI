# Filling in sources.yaml

This folder holds the only list of things Mark is allowed to learn from. Edit
`sources.yaml`, save it, and run `mark ingest`. Run `mark sources validate` first if you want
to check the file before fetching anything.

## Websites

List each page you want Mark to read. Set `crawl: true` on a section landing page and Mark
will follow links on the same site, up to `max_pages`. Use `exclude_patterns` to keep him out
of tag and author archives.

Mark respects `robots.txt`. If a site you own blocks crawlers, set `ignore_robots: true` on
that entry. Only do that for sites you control or have permission to read.

## YouTube

Mark reads the captions YouTube already has, so no API key is needed. Two ways to list
episodes:

- **A few episodes**: add them under `youtube.episodes`.
- **Many episodes**: put one URL per line in a text file (see `youtube_urls.example.txt`) and
  point `youtube.urls_file` at it. To build that list quickly, open your uploads playlist,
  or export your content list from YouTube Studio, and paste the URLs in.

If a video has captions turned off, ingest reports it and tells you so. Add a
`transcript_file` for that one, or list the same episode under the podcast section instead.

YouTube does not publish an upload date through the endpoint Mark uses, so fill in
`published_at` yourself on episodes where the age of the advice matters. Mark shows the date
in citations and warns when a source is more than about two years old on a legal question.

## Podcast

Ordered cheapest to most expensive:

1. **Also on YouTube?** List it under `youtube` and skip the rest. Free, has timestamps, and
   citations can deep-link to the exact moment.
2. **Transcript exports.** Most hosts (Buzzsprout, Libsyn, Transistor, Spotify for Podcasters)
   and most editors (Descript, Riverside, Otter) can export `.srt` or `.txt`. Drop the files in
   `data/raw/podcast/transcripts/`.
3. **A `<podcast:transcript>` tag in your feed.** Some hosts add this automatically. Mark
   downloads it when it is there.
4. **Local transcription.** Last resort. It needs `pip install "markai[transcribe]"` and runs
   at roughly one hour of compute per hour of audio, so 300 episodes is days of work. `mark
   ingest` shows an estimate and asks before starting.

### How transcript files get matched

For each episode, in order:

1. An explicit `transcript_file:` on the episode always wins.
2. The filename contains the episode number as a standalone number: `Ep 212.srt`, `212.txt`
   and `SUCI-212-mixed-use.vtt` all match episode 212. `2120.srt` does not.
3. The filename matches at least 80% of the words in the episode title.

Run `mark sources match` to see exactly which file each episode resolved to before you ingest.
It is much faster to rename a few files than to debug a bad ingest.

## Where files go

| What | Where |
|---|---|
| Podcast transcripts | `data/raw/podcast/transcripts/` |
| Podcast audio | `data/raw/podcast/audio/` |
| Everything Mark downloads | `data/raw/` |

All of `data/` is git-ignored. Transcripts and audio stay on your machine.

## Private feeds

If your podcast host gives you a feed URL with a token in it, do not commit it. Copy
`sources.yaml` to `sources.local.yaml` and put the real URL there. Mark uses the local file
when it exists, and git ignores it. `mark sources validate` warns you if it spots a token in a
URL.

## Rights

Only list material you own or have permission to use. Mark stores full transcripts of
everything you list, locally, so he can quote and cite it. That is fine for your own podcast,
videos and site. For someone else's material, get the transcript from them, or link to the
page instead of crawling it.

## Changing your mind

Adding an entry and re-running `mark ingest` picks it up. Removing an entry does **not** delete
what was already stored: run `mark ingest --prune` to drop sources that are no longer listed.
Ingest tells you which stored sources are now orphaned either way.
