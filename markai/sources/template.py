"""The starter ``sources.yaml`` that ``mark init`` writes.

Kept in sync with ``sources/sources.yaml`` by hand; a test asserts they match.
"""

from __future__ import annotations

SOURCES_TEMPLATE = """# Everything Mark is allowed to learn from. Add entries, save, then run: mark ingest
#
# Three kinds of material: websites, YouTube episodes, and your podcast.
# Nothing else is used. If it is not listed here, Mark does not know it.
#
# Private feed URL with a token in it? Copy this file to sources/sources.local.yaml
# and edit that instead. Mark prefers the local file and git ignores it.

# ---------------------------------------------------------------------------------------
# WEBSITES
# ---------------------------------------------------------------------------------------
websites: []
  # - url: https://example.com/blog/chicago-security-deposits
  #   title: Security deposit rules            # optional; the page title is used otherwise
  #   crawl: false                             # true follows links on the same site
  #   max_pages: 25                            # only used when crawl is true
  #   include_patterns: []                     # only crawl URLs matching these (regex)
  #   exclude_patterns: ["/tag/", "/author/"]  # never crawl URLs matching these (regex)
  #   ignore_robots: false                     # set true only for a site you own
  #   notes: ""

# ---------------------------------------------------------------------------------------
# YOUTUBE
# Mark reads the captions. Videos with captions turned off need a transcript_file.
# ---------------------------------------------------------------------------------------
youtube:
  channel_url: null           # informational only, e.g. https://www.youtube.com/@yourchannel
  channel_name: null          # shown in citations, e.g. Straight Up Chicago Investor
  urls_file: null             # e.g. sources/youtube_urls.txt — one URL per line, easiest for bulk
  episodes: []
    # - url: https://www.youtube.com/watch?v=dQw4w9WgXcQ
    #   title: Screening tenants without getting sued
    #   episode: "212"
    #   published_at: "2023-04-18"      # YouTube does not expose this; fill it in if it matters
    #   transcript_file: null           # path relative to this project, if captions are off

# ---------------------------------------------------------------------------------------
# PODCAST
# Order of preference for each episode, cheapest first:
#   1. A transcript file you drop in data/raw/podcast/transcripts/ (.srt .vtt .txt .json)
#   2. A <podcast:transcript> link in your RSS feed (some hosts add this automatically)
#   3. Local transcription of the audio — slow, roughly one hour of compute per hour of audio,
#      and it needs:  pip install "markai[transcribe]"
# Easiest of all: if the episode is also on YouTube, list it under youtube above instead.
#
# How a transcript file gets matched to an episode:
#   - an explicit transcript_file always wins, or
#   - the filename contains the episode number as a standalone number (212 matches "Ep 212.srt"), or
#   - the filename matches at least 80% of the words in the episode title.
# Run `mark sources match` to see exactly what matched before you ingest.
# ---------------------------------------------------------------------------------------
podcast:
  show_name: null             # e.g. Straight Up Chicago Investor
  rss: null                   # your feed URL, from your podcast host's dashboard
  include_titles: []          # only these episodes; matches part of a title, or an episode number
  max_episodes: 25            # start small; raise it once the first run looks right
  episodes: []
    # - title: Deposits, interest, and the RLTO
    #   episode: "145"
    #   episode_url: https://example.com/episodes/145   # used for the citation link
    #   audio_url: https://example.com/audio/145.mp3
    #   audio_file: null                                 # or a local file you dropped in
    #   transcript_file: data/raw/podcast/transcripts/145.srt
    #   transcript_url: null
    #   published_at: "2021-03-04"
    #   duration_seconds: 3300

# ---------------------------------------------------------------------------------------
# TOOLS Mark can recommend when they fit the question.
# ---------------------------------------------------------------------------------------
tools: []
  # - name: Rental property ROI calculator
  #   description: Cash flow, cap rate and cash-on-cash for a Chicago two-flat or three-flat.
  #   url: https://example.com/roi-calculator
  #   when_to_recommend: When someone is comparing two buildings or sizing a down payment.

# ---------------------------------------------------------------------------------------
# YOUR BUSINESS. This is added to Mark's instructions, so keep it short and factual.
# ---------------------------------------------------------------------------------------
business:
  name: null                  # e.g. GC Realty & Development
  services: null              # one line: what you do for landlords
  contact_url: null           # where to send someone who needs hands-on help
  contact_email: null
  service_area: null          # your definition of Chicagoland, e.g. "Cook, DuPage, Lake, Will"
  never_say: []               # things Mark must never promise or quote
    # - Specific management fee quotes
    # - Recommendations of a named attorney
  extra_instructions: null    # anything else Mark should always keep in mind
"""
