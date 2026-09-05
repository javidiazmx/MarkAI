# What I need from you

Everything below is what Mark needs to go live. Items 1 to 4 are required. Items 5 to 9 make
him noticeably better. Items 10 to 14 are confirmations I need in writing before this is used
by anyone but you.

You can paste most of it straight into `sources/sources.yaml`. If that is easier as a reply,
send it in any format and I will put it in the file.

---

## Required to go live

### 1. Anthropic API key

- [ ] Create a key at <https://console.anthropic.com/> and turn on billing.
- **How to give it to me:** don't. Run `mark init` and paste it at the prompt. It goes into a
  local `.env` file that is never committed. Never send a key by email or chat.
- Rough cost: a typical question costs a few cents. Mark caps spending at 500 questions a day
  out of the box.

### 2. Website URLs

- [ ] The list of pages you want Mark to read.
- For each one, tell me: **whole site or just this page?** If whole site, which sections to
  skip (blog tag pages, author archives, and store pages are usually noise).
- Example of what to send:
  ```
  https://yoursite.com/blog/chicago-security-deposits   (single page)
  https://yoursite.com/resources/                       (whole section, skip /tag/ and /author/)
  ```

### 3. YouTube episodes

- [ ] Your channel link.
- [ ] The exact episode URLs you want included. Mark reads the captions YouTube already has,
  so this is the cheapest and best source of material.
- **How to get the list fast:** open your uploads playlist in a browser and copy the links, or
  in YouTube Studio go to Content and export the list. Paste one URL per line into a plain text
  file and send it. I will drop it in as `sources/youtube_urls.txt`.
- If a video has captions turned off, Mark reports it by name after ingest and you can send a
  transcript for just that one.

### 4. The podcast

- [ ] **Your RSS feed URL.** Find it in your host's dashboard: Buzzsprout (Settings, then RSS
  Feed), Libsyn (Destinations), Transistor (Show settings), Spotify for Podcasters (Settings,
  Distribution). If your feed URL has a private token in it, tell me and I will keep it out of
  the repository.
- [ ] **Transcripts**, in whichever of these is easiest for you. They are listed cheapest first:
  1. **The episode is also on YouTube.** Then just include it in item 3 and skip the rest. This
     is the best option: free, and citations can link to the exact minute.
  2. **Transcript exports.** Most hosts and editors export `.srt` or `.txt`: Buzzsprout, Libsyn,
     Transistor, Descript, Riverside, Otter. Send them and I will drop them in
     `data/raw/podcast/transcripts/`.
  3. **A transcript tag in your feed.** Some hosts add one automatically. Nothing to do if so.
  4. **Local transcription from the audio.** This works, but it runs at about an hour of
     computer time per hour of audio, so 300 episodes is several days. Mark shows an estimate
     and asks before starting.
- **Filename tip:** name transcript files with the episode number in them (`212.srt`,
  `Ep 212 - Mixed Use.srt`). Mark matches on the number, or on the episode title. Run
  `mark sources match` to see exactly which file went with which episode before ingesting.
- [ ] Which episodes? All of them, the last 25, or a specific list. Starting with 25 is the
  sensible way to check the results before committing to the whole back catalog.

---

## Strongly recommended

### 5. Voyage AI key (better search)

- [ ] Optional key from <https://www.voyageai.com/>. It adds meaning-based search on top of
  keyword search, so Mark finds the right episode even when the landlord uses different words.
  Without it everything still works, just with keyword matching only.

### 6. Tone samples and test questions

- [ ] Three to five episodes that best capture how Mark Ainley actually talks. I will weight
  them when checking that the voice sounds right.
- [ ] Two or three real questions landlords ask you, with the answer you would give. I use
  these to check Mark before you show it to anyone.

### 7. Calculators and tools

- [ ] Any calculators or tools you want Mark to point people to. For each: the link, one line
  on what it does, and when it should come up.
- [ ] Tell me if any of them is an affiliate or paid link. If so the description has to say so.

### 8. Your definition of Chicagoland

- [ ] Which counties and towns count? The default is Cook, DuPage, Lake, Will, Kane, McHenry
  and Kendall.
- [ ] Is Northwest Indiana (Gary, Hammond, Munster) in or out? Right now it is out, and Mark
  will say a question about it is outside his area.

### 9. How Mark should talk about your business

- [ ] Company name and a one-line description of what you do for landlords.
- [ ] Where to send someone who needs hands-on help: a contact page URL or an email address.
- [ ] A "never say" list. Things Mark must not promise or quote. Common ones: specific
  management fee quotes, naming a particular attorney or contractor, guaranteeing an outcome.

---

## Please confirm in writing

These protect you, and I need them before Mark is shown to anyone outside your team.

### 10. Rights to the material

- [ ] You own, or have written permission to use, every website, video and podcast episode you
  list. Mark stores full transcripts of all of it locally so he can quote and cite it.
- Third-party pages (city and county sites, HUD, news) are fine to quote and link, but their
  text is stored on the machine too. Say so if any of them worry you.

### 11. Use of Mark Ainley's name

- [ ] Mark Ainley (or GC Realty on his behalf) agrees to the assistant being called Mark and
  written in his conversational style. Illinois protects a person's name and likeness by
  statute, so this should be a written yes, not an assumption.

### 12. The three fixed sentences

Mark says these word for word. Approve them or send edits. I recommend an Illinois real estate
attorney reads them before this is used by anyone outside your team.

**Legal disclaimer**, appended to every answer that touches a legal question:

> I'm not a lawyer, and this isn't legal advice. You should consult with an Illinois real
> estate attorney to confirm this applies to your situation.

**Identity notice**, shown in the web banner and at the top of every chat session:

> Mark is an AI assistant built in the style of Mark Ainley (Straight Up Chicago Investor). It
> is not Mark Ainley, it is not a lawyer, and its answers are not reviewed by him or by an
> attorney.

**Fair housing refusal**, used when someone asks how to screen out a protected class or remove
a tenant without going to court:

> I can't help with that one. Screening criteria have to be applied the same way to every
> applicant and can't be based on a protected characteristic, and removing a tenant has to go
> through the court process.
>
> What I can help with: lawful, uniform screening criteria (income ratio, credit, rental
> history, applied identically to everyone) and the eviction steps my sources cover.

And one more sentence Mark says whenever a question is outside the material:

> That's not covered in my training materials.

- [ ] Approved as written, or edits attached.

### 13. Who will use it

- [ ] Just you? Your team? Landlords, publicly?

This decides the setup. On your own machine it needs nothing extra. For a team, I add an access
code. Public means HTTPS, a login, spending limits, and a conversation with your attorney about
whether Illinois brokerage advertising rules mean the page needs a sponsoring-broker line.

### 14. What leaves your machine

- [ ] You understand the data flow:
  - Your question, the conversation so far, and the retrieved passages go to Anthropic to
    produce the answer.
  - Passage and query text go to Voyage only if you set that optional key.
  - Nothing else is transmitted. No analytics, no telemetry, no crash reporting.
  - Sources, transcripts, audio and the database stay in the `data/` folder on your machine and
    are excluded from version control.
  - Questions people ask are logged locally, so `mark gaps` can show you what material to add.

---

## Later, whenever you want

- [ ] **Hosting.** Where should this run? Your laptop, an office machine, or a small server.
- [ ] **Branding.** A logo and colors for the browser page.
- [ ] **More material.** Adding a source later is one line in `sources.yaml` and one command.
  Mark's knowledge grows every time you do it.

---

## The short version

If you only send me four things:

1. The Anthropic API key (entered locally, via `mark init`).
2. A text file of YouTube episode URLs.
3. Your podcast RSS URL, plus any transcripts you can export.
4. Your website URLs.

...Mark works. Everything else sharpens him.
