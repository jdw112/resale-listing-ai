# Admin Guide

How to create the first admin, review every submission, and tune the intake
pipeline through the Admin Console. Written for internal ops/admin staff.

> Proof-of-concept build. The condition scale and the sufficiency-gate thresholds
> are marked *to confirm with the business* — treat the defaults as starting
> points to tune on real data, not settled policy.

## Admin accounts

There is **no self-service admin signup**. The portal's Sign up only ever creates a
plain `submitter`. Admin is a privileged role, created two ways:

**Bootstrap on startup (recommended for the first admin).** Set
`RESALE_LISTING_AI_ADMIN_EMAIL` and `RESALE_LISTING_AI_ADMIN_PASSWORD` in `.env`. The `app`
container starts with `RESALE_LISTING_AI_BOOTSTRAP_ADMIN=1`, so on boot it creates that
admin — or promotes an existing user with that email to admin (an existing
password is never changed). It's idempotent: a safe no-op on every later start.

**By hand, any time:**

```bash
docker compose exec app python -m resale_listing_ai.admin create-admin you@example.com
# password comes from --password, else $RESALE_LISTING_AI_ADMIN_PASSWORD, else a prompt
```

This creates the user as an admin, or promotes them if they already exist. It never
changes an existing user's password. To promote someone who already signed up as a
submitter, run the same command with their email.

Once you have an admin login, the home screen shows an **Admin Console** card that
submitters don't see.

## The Admin Console

Four tabs: **Providers**, **Gate & Fields**, **Business Config**, and
**Submissions**. The first three edit configuration; every setting has a built-in
default, and your change is stored as an *override* on top of it. **Reset** on any
setting removes your override and returns it to the default — nothing is ever lost.

### Submissions

Every submission in the system (not just your own), newest first. It auto-refreshes
every 10 seconds. You can **filter by status** and page through the list (50 at a
time). Use this to answer "where did submission X get to?" — including items still
processing and items the gate rejected. This is the view to check when a submitter
reports something stuck.

### Gate & Fields — what counts as "enough to accept"

The sufficiency gate decides each submission's outcome. Three levers:

- **Threshold** — the confidence a fact needs before it's trusted (default
  `0.60`). A value below the threshold is treated as "not established."
- **Mandatory fields** — the facts an item *must* have (at confidence ≥ threshold)
  to be accepted. Identification also requires a confident **anchor**: either a UPC
  or a model number.
- **Core fields** — the fields checked when deciding Accepted vs. *Accepted with
  unknowns*.

How the outcome falls out of these:

| Missing mandatory facts | Outcome |
|---|---|
| None | **Accepted** (or *Accepted with unknowns* if a core field is blank) |
| One or two | **Clarification required** — the submitter is asked for exactly those |
| More than two | **Rejected** — no record created |

Raising the threshold makes the gate stricter (more clarifications/rejections,
higher precision). Lowering it accepts more on weaker evidence. Tune this against a
batch of real submissions rather than in the abstract.

### Business Config

- **Oversized (UPS Canada) rules** — the longest-side, length-plus-girth, and
  weight limits behind the `OversizedFlag`. An item over any limit is flagged
  oversized; the flag is *advisory*, not a shipping decision, and reads *Unable to
  Determine* when packaged dimensions are unknown.
- **Categories** and **Condition scale** — edited as JSON. Categories are an *open*
  vocabulary (the pipeline infers a reasonable label; this list keeps them
  consistent, it doesn't constrain them). The condition scale is the list of grades
  a submitter can pick from; it's flagged *to confirm* with the business, so expect
  to revise it.

Edit the JSON in place and save. Invalid JSON or a value that fails validation is
rejected with a message, and the current setting is left unchanged.

### Providers — which vendor backs each pipeline step

Each pipeline "seam" can be pointed at a different vendor without touching code:

| Seam | What it does | Options |
|---|---|---|
| Vision provider | Identify the item from photos | openai, gemini, openrouter, bedrock |
| Copy provider | Draft the listing copy | openai, gemini, openrouter |
| Web search provider | Pricing + image-match with citations | openai_web_search, perplexity_sonar, gemini_search, openrouter_online |
| Image process provider | Background removal / cleanup | photoroom_clipdrop, gemini, bedrock |
| JSON repair provider | Fix malformed model output | claude, openai |

Changing a provider here takes effect on the next submission processed. **The vendor
must have a working API key configured** (in `.env` — see `.env.example`) or its
calls will fail; the console lets you *select* a provider, it doesn't supply the
key. If you're unsure which keys are present, check with whoever runs the
deployment. Provider selection and keys are covered technically in
[`docs/DEVELOPING.md`](../DEVELOPING.md#selectable-providers).

## Offline vs. live processing

If the worker runs with `RESALE_LISTING_AI_STUBS=1`, every provider is replaced by an
offline mock — real photos can't be identified, so real submissions land in
**Rejected** at the gate. That mode is for demos and tests. For the console to
produce real results, the worker must run with live providers and valid keys.

## Day-to-day

- **A submitter says an item is stuck.** Open **Submissions**, filter or find their
  item, and read its state. Still processing, or terminal (rejected / clarification)?
- **Too many rejections.** Check **Gate & Fields**: is the threshold too high for
  the photo quality you're getting? Are the mandatory fields realistic for your
  catalogue?
- **Costs look off, or a vendor is failing.** Switch that seam to another provider
  in **Providers** (confirm its key exists first), and compare.

For the submitter's side of all this, see the [Submitter Guide](submitter-guide.md).
For how the gate, providers, and queues actually work, see the
[Architecture doc](../architecture.md) and [`DEVELOPING.md`](../DEVELOPING.md).
