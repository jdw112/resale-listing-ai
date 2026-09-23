# Submitter Guide

How to submit an item through the Acme Resale intake portal, read the result, and
respond when the pipeline needs more information. Written for internal ops staff
doing intake — no technical background assumed.

> This is a proof-of-concept build. Some behaviour described here is deliberately
> simplified; where that affects you, it's called out. The condition list and the
> sufficiency thresholds are still being confirmed with the business, so exact
> wording and cut-offs may change.

## What the portal does

You give it a few photos of one physical item. It identifies the item, prices it,
drafts listing copy, cleans up the photos, and produces two records:

- a **Product** — what the item *is* (brand, model, colour, dimensions, listing
  copy, a price anchor). Shared by every unit of the same make/model.
- a **Listing** — *this specific unit* for sale (its SKU, its condition, its own
  photos).

Every fact in those records comes from real evidence — a barcode, text read off a
label, a priced comparable, a cited search result. The pipeline never guesses a
value to fill a blank. If it can't find something, it leaves it empty or asks you.

## Before you start

- You need an account. Open the portal (ask your admin for the URL — locally it's
  `http://localhost:8000`) and use **Sign up** to create a submitter login, then
  **Log in**. Admin accounts are created separately; signing up always makes a
  plain submitter account.
- Have the item's photos ready. Good photos are the single biggest factor in a
  clean result — see [Taking good photos](#taking-good-photos).

## Submit an item

1. From the home screen choose **Submit an item** (or **Submit** in the nav).
2. **Photos** — add at least one. More angles help; include any label, barcode, or
   rating plate. This is the only required field.
3. **Condition** *(optional)* — pick the grade that matches the unit in hand (New,
   Open Box, Used - Like New, Used - Good, Used - Fair, For Parts). Whatever you
   pick here is **kept exactly as you entered it** and is never overwritten by the
   system — it's your call, not the model's. Leave it blank if you're unsure.
4. **Notes** *(optional)* — anything notable the pipeline should know (a missing
   part, damage, a model number you can read but the photo doesn't show clearly).
5. Choose **Submit item**. You land on the submission's result page.

## Read the result

The result page updates on its own — leave it open, no need to refresh. A
submission moves from **Processing** to one of four outcomes:

| Outcome | What it means | What to do |
|---|---|---|
| **Accepted** | Identified with enough confidence; Product + Listing created, nothing missing. | Done. Review the fields shown. |
| **Accepted with unknowns** | Created, but some non-critical fields couldn't be filled from the evidence. | Usually fine to accept; check the blanks matter to you. |
| **Clarification required** | Close, but one or two key facts are missing. | Answer the prompt (below). |
| **Rejected** | Too little could be identified (more than two key facts missing). No record is created. | Re-shoot with clearer photos and submit again. |

When the outcome is Accepted, the page shows the **Product** and **Listing**
fields that were filled. A rejected item produces no record at all — that's by
design, not an error: the pipeline would rather return nothing than invent details.

## Answering a clarification request

When the outcome is **Clarification required**, the page shows a short form with a
box for each missing fact (for example a model number or UPC it couldn't read).

- Type in whatever you can read directly off the item. Only the facts it actually
  needs are askable — you can't override arbitrary fields.
- You can also **add more photos** here (a clearer shot of the label, say).
- Choose **Submit answer**. The pipeline reprocesses with your input merged in, and
  the page resumes live updates for the new outcome.

Each answer creates a new version in the same thread — your original submission and
every resubmission stay linked, so you can see the history.

## Track your submissions

**My submissions** lists every item you've sent, newest activity first. Each row
shows the current status, the identified item (its Product key) once known, the
Listing SKU, and the pipeline cost for that submission. If an item needed
clarification, a **versions** control expands the full history of that thread.

## Taking good photos

The pipeline reads what's visible — it doesn't assume typical specs. To get a clean
result:

- **Capture the label / rating plate / barcode.** These carry the model number,
  UPC, and printed specs the identification leans on hardest.
- **One item per submission.** A submission is one physical unit. Photograph a
  second unit as its own submission (two identical units correctly share one
  Product but get their own Listing).
- **Multiple angles.** Front, back, and any printed panel beats a single hero shot.
- **In focus and well lit.** Blurry or dark label text is the most common reason a
  fact can't be read, which is what pushes an item into clarification or rejection.

## When something goes wrong

- **"At least one photo is required"** — the submit button stays disabled until
  you've added a photo.
- **Item rejected** — more than two key facts couldn't be established. This almost
  always means the identifying text wasn't readable; re-shoot the label and
  resubmit as a new item.
- **Result stuck on Processing** — it normally takes a moment. If it never
  resolves, tell your admin; they can see the pipeline's state for every submission
  in the Admin Console.

For how an admin reviews submissions and tunes what counts as "enough to accept,"
see the [Admin Guide](admin-guide.md).
