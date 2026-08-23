# Market research prompt

Paste into a Claude session with web search.

---

I own a production Python poll-and-alert engine: watches web sources (JSON APIs, authed
sessions, HTML scraping), diffs, notifies. 3 monitors live on one free-tier VM. Its edge
is reliability — 17 documented invariants, each preventing a *silently missed* alert. No
multi-tenancy, billing, UI, or SMS yet.

I want $2-10k/mo from it. Solo dev, nights/weekends, strong backend, no audience, no
sales experience. Happy to serve 30 businesses at $200/mo over 700 consumers at $10/mo.

Research two things, with URLs, and separate verified facts from inference.

1. Is consumer campsite/permit cancellation alerting dead for a newcomer? I found ~17
competitors (Campnab $10-30/mo, Outdoor Status, Schnerp, WildPermits, PermitSnag,
FirstCampsite...) and free ones (Campflare claims 10M alerts sent, $0 charged). Verify:
does Recreation.gov now ship its own availability notifications? Any disclosed revenue
in this space? How does Campflare sustain free? Does Recreation.gov's ToS allow
automated access, and has anyone been blocked or C&D'd? Churn — is this one-and-done
demand? Give a verdict.

2. Find better markets for change-monitoring alerts where the buyer is a business (or
has recurring need), a missed alert costs real money, the data source is hard enough to
deter thin competitors, and buyers are reachable without a sales team. Evaluate at
least: golf tee times, building-permit leads for contractors, healthcare license/
exclusion monitoring, city council agenda & RFP monitoring, government appointment
slots, generic monitoring (Visualping/Distill), grant alerts. Add better ones I missed.

For each: who writes the check, incumbent pricing (fetch the pages), customers needed
for $5k/mo, data-source reality (API? auth? how many systems? ToS?), the specific
acquisition channel with evidence it works there, churn shape, and the strongest reason
NOT to do it. Note any evidence of a solo founder earning $2-10k/mo in that space.

Output: verdict on #1, ranked top 3 from #2, kill list, then one recommendation — the
narrowest wedge, first 10 customers, and a 3-week experiment that validates it without
building the product.

Be harsh. If nothing here clears $2k/mo for a solo dev with no audience, say so.
