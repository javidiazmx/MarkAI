# Publishing kit — "How Much Do Property Managers Charge in Chicago?"

Everything needed to ship `docs/chicago-property-management-fees.md`, plus the things
that must be settled first. Read the sequencing warning before anything else.

---

## ⚠️ Sequencing: do the redirects in the same release, or this makes rankings worse

Eleven existing GC posts already compete for this cluster. Publishing a twelfth
without consolidating is the single thing that would make this effort backfire.

**301 into `/blog/chicago-property-management-fees`:**

| Existing URL | Why |
|---|---|
| `/blog/self-managing-your-rentals-saves-you-money--or-does-it` | self-manage cluster |
| `/blog/self-managing-vs-hiring-a-property-manager-in-chicago` | self-manage cluster |
| `/blog/the-pros-and-cons-of-being-a-house-self-manager-in-chicago` | self-manage cluster |
| `/blog/do-i-need-a-rental-property-management-company` | duplicate pair |
| `/blog/should-you-use-a-property-management-company-a-guide-for-chicago-real-estate-investors` | duplicate pair |

**Keep live** (the pillar cites them, so they stay as sources and link up):
`/blog/is-it-cheaper-to-manage-your-own-rental-property` and
`/blog/when-is-property-management-not-worth-the-cost`.

Do not keep all eleven. Anything kept must be retargeted to a genuinely distinct
query and must link up to the pillar.

**Head terms are not this article's job.** `property management Chicago` and
`chicago property management company` are local pack + directories + homepages, and
GC's homepage already ranks page one. A post aimed there would cannibalize a position
GC already owns. Those belong to the Google Business Profile (primary category must be
**Property Management Company**, not Real Estate Agency), review velocity, the service
page, and a verified Chicago address — GC is HQ'd in Roselle, and distance is a top-three
local ranking factor.

---

## Title tag

```
Property Management Fees in Chicago: The Break-Even Math
```
56 characters. Primary keyword in the first four words.

Alternates: `Chicago Property Management Fees: What 7-10% Really Buys` (56) ·
`How Much Do Property Managers Charge in Chicago? 7-10%` (54 — only if the H1 matches).

## Meta description

```
Chicago property management fees run 7-10% of collected rent. See the break-even math against one vacant month - plus when self-managing is the better call.
```
153 characters.

## URL

```
https://www.gcrealtyinc.com/blog/chicago-property-management-fees
```

Evergreen. Never add `-2026` — five posts are being 301'd into this path and it has to
be permanent.

## Primary / secondary keywords

- Primary: `property management fees chicago`
- Exact-question variant (featured-snippet target): `how much do property managers charge in chicago`
- Suburban long-tails, where competition is genuinely thin and conversion is better:
  `property management fees naperville / schaumburg / oak park / elgin`

---

## Structured data

Paste in `<head>`. One `@graph`, so Article, FAQ and the business entity are linked.
FAQ entities below match the article's seven H3s exactly — if the article's FAQ changes,
change this too, or the markup is invalid.

```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Article",
      "@id": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees#article",
      "isPartOf": { "@id": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees#webpage" },
      "mainEntityOfPage": { "@id": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees#webpage" },
      "headline": "How Much Do Property Managers Charge in Chicago? The Break-Even Math on a 7-10% Fee",
      "description": "Chicago property management fees run 7-10% of collected rent, plus a placement fee of 50-100% of one month's rent. The break-even math against vacancy, the ordinance costs no fee comparison includes, and when self-managing is cheaper.",
      "articleSection": "Property Management",
      "inLanguage": "en-US",
      "datePublished": "FILL IN — ISO 8601, e.g. 2026-10-20T09:00:00-05:00",
      "dateModified": "FILL IN — same as datePublished at launch; update on every substantive edit",
      "image": { "@type": "ImageObject", "url": "FILL IN — absolute URL, 1200x675 minimum", "width": 1200, "height": 675 },
      "author": { "@type": "Person", "name": "Mark Ainley", "jobTitle": "Managing Broker and Co-Founder", "url": "https://www.gcrealtyinc.com/about", "worksFor": { "@id": "https://www.gcrealtyinc.com/#organization" } },
      "publisher": { "@id": "https://www.gcrealtyinc.com/#organization" },
      "about": [
        { "@type": "Thing", "name": "Property management fees" },
        { "@type": "Thing", "name": "Rental property management" },
        { "@type": "Place", "name": "Chicago, Illinois" }
      ],
      "mentions": [
        { "@type": "Legislation", "name": "Chicago Residential Landlord and Tenant Ordinance (CRLTO)" },
        { "@type": "Legislation", "name": "Cook County Residential Tenant Landlord Ordinance (RTLO)" },
        { "@type": "Legislation", "name": "Cook County Just Housing Amendment" },
        { "@type": "Legislation", "name": "Illinois HB 3564" }
      ]
    },
    {
      "@type": "WebPage",
      "@id": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees#webpage",
      "url": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees",
      "name": "Property Management Fees in Chicago: The Break-Even Math",
      "isPartOf": { "@id": "https://www.gcrealtyinc.com/#website" },
      "about": { "@id": "https://www.gcrealtyinc.com/#organization" },
      "breadcrumb": { "@id": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees#breadcrumb" },
      "lastReviewed": "FILL IN — the article footer says October 2026",
      "reviewedBy": { "@id": "https://www.gcrealtyinc.com/#organization" }
    },
    {
      "@type": "BreadcrumbList",
      "@id": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees#breadcrumb",
      "itemListElement": [
        { "@type": "ListItem", "position": 1, "name": "Home", "item": "https://www.gcrealtyinc.com/" },
        { "@type": "ListItem", "position": 2, "name": "Blog", "item": "https://www.gcrealtyinc.com/blog" },
        { "@type": "ListItem", "position": 3, "name": "Property Management Fees in Chicago" }
      ]
    },
    {
      "@type": "FAQPage",
      "@id": "https://www.gcrealtyinc.com/blog/chicago-property-management-fees#faq",
      "mainEntity": [
        { "@type": "Question", "name": "How much do property managers charge in Chicago?",
          "acceptedAnswer": { "@type": "Answer", "text": "7-10% of collected rent for full-service residential management, plus a tenant placement fee of 50-100% of the first month's rent and a fee at each renewal. On a $2,200 rental, roughly $1,848 to $2,640 a year." } },
        { "@type": "Question", "name": "Is the fee based on rent collected or rent due?",
          "acceptedAnswer": { "@type": "Answer", "text": "The Chicagoland benchmark is quoted against rent collected, which means the manager earns when the owner does. Some agreements are written against rent due instead, so ask and get the answer in the contract." } },
        { "@type": "Question", "name": "What does the monthly management fee actually cover?",
          "acceptedAnswer": { "@type": "Answer", "text": "At the 7-8% end of the range: leasing, inspections, rent collection, and compliance including the Cook County RTLO and the crime-free seminar where a village requires one. GC Realty puts that at 30 to 40 hours a year across the full process." } },
        { "@type": "Question", "name": "Do property managers mark up maintenance invoices?",
          "acceptedAnswer": { "@type": "Answer", "text": "Some do, in the range of 15% to 25%, documented in GC Realty's 2025 and 2026 reviews of Chicago companies. It is the most common way a low headline rate becomes an expensive year." } },
        { "@type": "Question", "name": "Do you need a license to be a property manager in Illinois?",
          "acceptedAnswer": { "@type": "Answer", "text": "Yes, to manage for others: a real estate broker's license, or working under someone who holds one. Owners managing their own property are generally exempt. Verify the managing broker's license number with the Illinois Department of Financial and Professional Regulation." } },
        { "@type": "Question", "name": "How much does an eviction cost in Cook County?",
          "acceptedAnswer": { "@type": "Answer", "text": "$287 to $500 to file, plus $800 to $2,100 in attorney fees as of GC Realty's 2024 review. A worst-case wait exceeds $5,000, and attorney fees are not recoverable when a tenant cures by paying unpaid rent plus the landlord's court filing fees." } },
        { "@type": "Question", "name": "When is a property manager not worth the cost?",
          "acceptedAnswer": { "@type": "Answer", "text": "When the owner lives nearby, has one stable long-term tenant, is handy, has the 15 to 25 hours a year, and knows their municipality's deposit, late-fee and notice rules. Then self-managing keeps about $1,800 a year on a $1,500 unit." } }
      ]
    }
  ]
}
</script>
```

---

## 🔴 Settle these before publishing

Each one was raised independently by more than one review pass.

1. **One canonical rate card.** Five different GC rates are live across the site today
   (homepage, Homewood page, leasing FAQ at 5-8%/$125 minimum, the management fee post,
   and the 7-10% market figure). The article deliberately does **not** state a GC rate —
   it says the rate is quoted per property on a call. That ships today and converts
   better. But fix the five contradictory pages anyway: inconsistent entity pricing
   suppresses conversion on every page that already ranks, and it is the cheapest fix
   on this list.
2. **Which screening standard is live?** The article publishes both (600 / 2.5× as
   baseline, 700 / 4× on some Chicago units) because both are on GC's own pages.
   Publishing two standards inside the paragraph that argues consistency is the
   fair-housing defense is a real exposure. Pick one, state its scope, update the
   leasing FAQ, then update this article to match.
3. **The $50 application fee cap's effective date.** GC's own material reports it both
   as current Illinois law and as an HB 3564 provision from January 2027. The article
   names the split honestly and recommends ≤$50 either way, which is safe — but get the
   statutory source confirmed and then state it once, cleanly.
4. **Verify these URLs resolve** before publishing (they could not be checked from the
   build environment): `/free-rental-analysis`,
   `/blog/renting-your-property-in-berkeley-rental-market-guide`,
   `/blog/what-are-the-most-common-hidden-fees-in-property-management`,
   `/blog/hb-3564-is-now-law-january-2027-changes-for-all-illinois-landlords`,
   and the Calendly link.
   The research verified `/contact` with NAP *219 E Irving Park Rd, Roselle IL 60172 /
   630-587-7400* — use that as the fallback for any that 404.
5. **Attorney and filing fees are dated 2024.** The article says so in the text. Refresh
   from the Clerk of the Circuit Court before publishing if you want the figures current.
6. **Jay.** The article does not mention it, because there is no public URL yet. Once
   there is one, add a line immediately *above* the Calendly CTA: "Not ready to talk to
   anyone? Ask Jay — our free Chicagoland landlord advisor, which answers only from the
   published material cited on this page, including which ordinance covers your address."
   Low-commitment capture for the segment that will never book a call today.

---

## Internal links to add

**From the article** (all already in place): the Schaumburg and Berkeley guides, hidden
fees, late fees by municipality, HB 3564, when management is not worth the cost, is it
cheaper to self-manage, /about, /free-rental-analysis, the Calendly link.

**To the article**, added by hand in the same release:

- The service page — anchor: *what Chicago property management costs*
- The homepage fee/pricing module — anchor: *property management fees in Chicago*
- `/leasing-faq` and `/owner-faq` — anchor: *how our fees compare to the Chicago market*
- Every `renting-your-property-in-<village>` market guide — anchor:
  *what property managers charge in the Chicago suburbs*. There are dozens of these and
  they are the strongest internal-link asset GC has.
- The two kept self-manage posts — anchor: *the full break-even math on a management fee*

Use descriptive, keyword-bearing anchors under about eight words, and don't point the
same anchor at two URLs.

## Images

- Hero, 1200×675: the fee-versus-vacant-days comparison as a simple bar — "one year of
  management, 25–36 days of rent" against "one vacant month, 30 days". It is the whole
  argument in one image and it is the piece most likely to be screenshotted.
- A second graphic for the three late-fee caps side by side.
- Alt text carrying the keyword naturally, not stuffed.

## Distribution

- **Mailchimp:** subject line *"Your property manager costs 25 days of rent a year"* —
  the break-even frame, not the percentage.
- **Podcast:** this is a natural segment on Straight Up Chicago Investor; the Duke
  Dennis / Bryan Sonn quote in the article is already from an episode.
- **Social:** the three late-fee caps make the single best standalone post —
  three numbers, one metro, and most landlords get at least one wrong.
- Do **not** boost this at head-term audiences. It is a research-stage asset.

## Measurement

Judge it at 30 / 60 / 90 days on three things, not on the head term:

- **30 days** — indexed within a week; redirects resolve with no chains; long-tail
  impressions appear; initial positions in the 20s-40s. A handful of clicks, possibly
  single digits. The honest 30-day win is that eleven competing pages became one.
  Expect a dip-then-recover from the 301s; three to four weeks of wobble is normal and
  is not a reason to undo anything.
- **60 days** — primary keyword into the teens or low 20s; suburban long-tails reaching
  page one or two; featured snippet live possibility on the exact-question query; one
  to three attributable leads.
- **90 days** — top ten for part of the cluster. Top five on the primary keyword is a
  six-to-nine month outcome and depends on links this article does not generate by
  itself. If cluster impressions are up against the pre-launch baseline and long-tails
  are landing in the top twenty, it is working even if the primary keyword is on page two.
- **Day 14 housekeeping:** confirm every redirect resolves cleanly, submit the new URL in
  Search Console, re-submit the sitemap, check for redirect chains.

Measure the head terms against GBP work and the Chicago-address question — never against
this article.
