# Data Dictionary — Product & Listing Records

The field-level contract for the enrichment workflow: every column, its type, whether it is required,
its logical group, and its provenance semantics. Reconciles the **Data Contract v1.0** and the two
baseline workbooks with the schemas the code actually generates from.

| | |
|---|---|
| **Authoritative schemas** | [`config/product_schema.json`](../config/product_schema.json) · [`config/listing_schema.json`](../config/listing_schema.json) |
| **Companion** | [Data Contract v1.0](../PRODUCT%20ENRICHMENT%20AGENT/Data_Contract.docx) · PRD v2.2 Appendices A/B |
| **Records** | Product (62 fields, reusable) + Listing (33 fields, per unit), joined by `ProductKey` |
| **Code revision** | `36ded8f` |

The DDL, CSV export columns, and record objects are all generated from these two schema files, so the
store and this dictionary cannot drift apart. Field lists are never hardcoded.

## Value envelope & conventions

Every field is stored as an envelope, so *blank* vs *unknown* vs *verified* is explicit and the gate is a
pure data check:

```json
{ "value": <x> | null, "confidence": 0.0–1.0, "source": "<provenance>" }
```

- **`source`** ∈ `barcode · label_ocr · vision · external_lookup · context · provided · measured · derived`
  (the code envelope, `resale_listing_ai/envelope.py`, additionally uses `generated` for copy/AI-image fields and
  `none` for blanks — the schema files list the eight core sources).
- **`confidence`** 0.0–1.0. Mandatory fields must reach **≥ 0.60** to pass the gate; below that counts as
  *missing*, never filled with a low-confidence guess.
- **Blank / unknown** ⇒ `value:null, confidence:0.0, source:"none"` — never a fabricated placeholder.
- **`ProvidedConditionGrade`** is always `source:"provided"`, confidence 1.0, and **never overwritten**.
- **Copy fields** (titles, bullets, description, keywords) use `source:"generated"` and may draw only on
  verified facts already in the record.
- **Submission status** (not a record field): `Accepted · Accepted with unknowns · Clarification required
  · Rejected`.

Required columns must have stable names, types, and formats (remappable to SharePoint/Power Automate later).
`Y` in the tables below marks a required field.

---

## Product Information (62 fields)

Grain: one per make/model/variant, reusable, keyed by `ProductKey`. One Product backs many Listings.

### Identity
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 1 | ProductKey | string | **Y** | Primary key; reused across units of the same make/model/variant |
| 2 | Brand | string | **Y** | |
| 3 | Manufacturer | string | | |
| 4 | ProductName | string | **Y** | |
| 5 | ProductType | string | | |
| 6 | ProductLineOrSeries | string | | |
| 9 | ModelNumber | string | | |
| 10 | MPN | string | | Manufacturer part number |
| 11 | UPCOrEAN | string | | From barcode decode; blank if not visible |
| 12 | Variant | string | | Variant / edition / generation |

### Classification
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 7 | InternalCategory | string | **Y** | Open vocabulary — one reasonable, reasonably specific inferred label; **not** a fixed platform taxonomy; prefer an existing `categories.json` label |
| 8 | InternalSubcategory | string | | Open vocabulary; paired with InternalCategory |
| 28 | CategorySpecificSpecifications | string | | Category-specific specs |

### Physical & measures
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 13 | Colour | string | | |
| 14 | Finish | string | | |
| 15 | Material | string | | |
| 16–19 | Unpackaged{Length,Width,Height}CM, UnpackagedWeightKG | number | | Product dimensions/weight |
| 20–23 | Packaged{Length,Width,Height}CM, PackagedWeightKG | number | | **Never guess; blank if unknown** |
| 24 | OversizedFlag | enum | | Derived from packaged size/weight vs a UPS Canada baseline; **flag only**, `Yes / No / Unable to Determine` |

### Contents
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 25 | StandardIncludedItems | string | | What a complete new product normally includes |
| 26 | CompatibilityNotes | string | | |
| 27 | InstallationRequirements | string | | |

### Reusable copy (generated from verified facts only)
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 29 | MasterTitle | string | **Y** | Channel-neutral; mandatory per PRD |
| 30 | ShortTitle | string | | |
| 31 | MasterDescription | string | | |
| 32–36 | BulletPoint1…5 | string | | Reusable selling bullets |
| 37 | SearchTerms | string | | |
| 38 | SEOKeywords | string | | |

### Pricing anchor (product-level; not a listing/selling price)
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 39 | PricingAnchorPriceCAD | number | | Product-level market anchor (CAD) |
| 40–41 | PricingAnchorSourceName, PricingAnchorSourceURL | string / url | | Primary source |
| 42–44 | CorroboratingSourcePriceCAD, …Name, …URL | number / string / url | | ≥1 reputable corroboration when available |
| 45–48 | ComparableBrand, ComparableProductName, ComparableProductPriceCAD, ComparableProductURL | | | Used when no exact match exists |
| 49 | PricingConfidence | enum | | Lowered when reputable sources disagree materially (~>10%) |
| 50 | LastPricingCheckDate | date | | |

### Assets & notes
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 51 | ProductSpecificationsSourceURL | url | | |
| 52 | HeroImageURL | url | | **Actual-item marketplace main image** — clearest scrubbed actual-unit photo (former "Tier 1"); eBay/Shopify-ready |
| 53 | ManualURL | url | | EN/FR where available |
| 54 | InstallationGuideURL | url | | Separate from manual; EN/FR where available |
| 55 | ProductFolderURL | url | | |
| 56 | SupportingDocumentFolderURL | url | | |
| 57 | Notes | string | | |

### Additions beyond the 57-field baseline (§8 / FR-9 — documented here)
The schema adds five fields past the 57-field baseline, all supporting the **AI-Generated Website
Hero Image Standard**. `HeroImageURL` (#52) stays the marketplace main image; these describe a *separate*,
unbranded website hero and its audit trail:

| # | Field | Type | Notes |
|---|---|---|---|
| 58 | HeroImageSourceURL | string | **DEPRECATED** — was the retired "Tier-2" manufacturer/retailer hero citation; always blank; column retained to avoid a migration |
| 59 | WebsiteHeroImageURL | url | AI-generated, unbranded, condition-neutral website hero; separate from originals and scrubbed actual-item photos |
| 60 | WebsiteHeroReviewRequired | string | `Yes` when generation could not produce a safe unbranded hero and it needs manual review (Standard §7); else `No`/blank |
| 61 | WebsiteHeroPrompt | string | Exact text-to-image prompt used, persisted for audit/tuning (default on; `RESALE_LISTING_AI_PERSIST_HERO_PROMPT=0` to disable) |
| 62 | WebsiteHeroRejectedURL | url | Storage URL of a QC-rejected AI website hero, saved for manual review; never shown to customers |

> These are exactly the fields the **Photoroom sandbox watermark** exercises: a watermarked website hero
> fails hero-QC, `WebsiteHeroReviewRequired` becomes `Yes`, and `WebsiteHeroRejectedURL` retains the rejected
> image. The marketplace `HeroImageURL` (real scrubbed photo) is unaffected.

---

## Listing Information (33 fields)

Grain: one per physical unit / sellable item. Links to Product via `ProductKey`. **Matches the 33-field
baseline exactly — no additions.**

### Relationship & identity
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 1 | ProductKey | string | **Y** | Foreign key to Product Information |
| 2 | SKU | string | **Y** | Randomized, non-sequential (FNSKU-like); generated, unique |
| 3 | SerialNumber | string | | Extracted only when readable; **never invented**; blank otherwise |

### Condition (provided value never overwritten)
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 4 | ProvidedConditionGrade | string | **Y** | Controlled input from processor; never overwritten |
| 5 | AISuggestedConditionGrade | string | | Evidence-based; **if it differs from provided, flag for review** |
| 6 | ConditionDetails | string | | |
| 7 | VisibleWear | string | | |

### Completeness
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 8 | ActualIncludedItems | string | | |
| 9 | MissingItems | string | | |
| 10 | MissingComponents | string | | |

### Packaging & hardware
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 11 | OriginalPackagingIncluded | enum | | Distinct from box |
| 12 | OriginalBoxIncluded | enum | | **Distinct** from packaging |
| 13 | MissingHardware | enum | | `Yes / No / Unable to Determine` — Unable-to-Determine avoids treating uncertain inspection as complete |

### Function & testing
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 14 | DefectsOrDamage | string | | |
| 15 | FunctionalConcerns | string | | |
| 16 | TestingStatus | string | | |
| 17 | TestsPerformed | string | | |
| 18 | TestResults | string | | |

### Photos (2–3 supporting used; rest reserved)
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 19 | OriginalPhotoFolderURL | url | | **Immutable originals** |
| 20 | ScrubbedPhotoFolderURL | url | | |
| 21–23 | ListingImageURL1–3 | url | | Unit-specific supporting images |
| 24–28 | ListingImageURL4–8 | url | | Reserved capacity; blank under the current 2–3 image rule |
| 29 | ActualProductPhotosAvailable | enum | | |
| 30 | OriginalPackagingPhotosAvailable | enum | | |
| 31 | ProductInformationLabelPhotosAvailable | enum | | |
| 32 | ContextualProductPhotosAvailable | enum | | |

### Notes
| # | Field | Type | Req | Notes |
|---|---|---|---|---|
| 33 | UnitSpecificNotes | string | | |

---

## Schema rules (enforced in code)

**Product** — OversizedFlag derived vs a UPS Canada baseline (Unable-to-Determine when package data unknown);
category fields are open vocabulary; unsupported fields stay blank; additions beyond baseline are allowed when
reliably extractable and valuable (and documented — above).

**Listing** — `ProvidedConditionGrade` is a controlled input, never overwritten (a differing AI grade raises a
review flag); `SerialNumber` extracted only, never invented; `SKU` randomized, non-sequential, unique; only
2–3 `ListingImageURL` columns populated; unsupported fields stay blank.

*Confidence and source travel in JSON columns in Postgres; the CSV export flattens to the `value` only, so a
consumer reads clean columns while the provenance remains queryable in the store.*
