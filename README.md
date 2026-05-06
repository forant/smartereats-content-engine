# SmarterEats Content Engine

Batch tool that scores pairs of food products from a CSV by:

1. Looking each barcode up in a local SQLite copy of the Open Food Facts (OFF) data.
2. Mapping the result into the SmarterEats backend `/score` request shape.
3. POSTing to the backend and recording each food's score + interpretation.
4. Generating a deterministic comparison hook for the pair.

The script does **not** call Open Food Facts over the network and does **not**
do fuzzy name search. Barcode is the source of truth.

## Requirements

- Python 3.9+
- The SmarterEats backend running locally
- A local SQLite OFF database (see "Database" below)

Install dependencies:

```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Database

The script reads from a SQLite file with an OFF-shaped schema. It tries
`canonical_products` first and falls back to `products`. Either table must
expose: `code`, a name column, optional `brands`, `nutriments` (JSON string),
`serving_size`, `serving_quantity` (grams), `ingredients_text`.

Default path is `./foodscore.db`. Override with the `FOOD_DB_PATH` env var.

> **Note about the foodscore project layout:** the file
> `foodscore/backend/foodscore.db` holds **app** data (users, food items,
> rewards) — not the OFF product catalog. The OFF catalog lives in
> `foodscore/backend/off_products.db`. Point `FOOD_DB_PATH` at the
> `off_products.db` file (or copy/symlink it as `foodscore.db`):
>
> ```
> export FOOD_DB_PATH="/Users/stellar87/Developer/foodscore/backend/off_products.db"
> ```

## Configuration

Required:

- `BACKEND_BASE_URL` — base URL of the SmarterEats backend, e.g.
  `http://localhost:8000`.

Optional:

- `FOOD_DB_PATH` — path to the SQLite OFF DB. Default: `./foodscore.db`.
- `OPENAI_API_KEY` — enables markdown blog post generation. Without it,
  `main.py` still produces results.csv, blog input JSONs, and PNG cards;
  it just skips the blog-post step with a warning.
- `OPENAI_MODEL` — model name passed to OpenAI chat completions.
  Default: `gpt-4o-mini` (cost-effective).

## Input CSV

`input.csv` in the working directory. Required header:

```
pair_id,food_a_name,food_a_barcode,food_b_name,food_b_barcode,purpose,angle
```

Optional columns (any subset, in any order):

```
food_a_display_name,food_b_display_name,
food_a_label,food_b_label,
framing_type,
format
```

`format` is one of:

- `comparison` (default) — food_a as subject, food_b as baseline
- `exposure` — single-item attack on food_a's halo (food_b is ignored)
- `swap` — food_a is what to swap out, food_b is the recommended pick
- `ranking` — recorded but skipped for now (placeholder for future logic)

### Naming fields

- `food_a_barcode` / `food_b_barcode` — **source of truth for lookup.** Blank
  or `nan` is treated as missing.
- `food_a_name` / `food_b_name` — internal/reference name. Always required.
- `food_a_display_name` / `food_b_display_name` — what appears on cards and
  in the output CSV. Optional. Falls back to `food_*_name`.
- `food_a_label` / `food_b_label` — what appears in hook text (e.g.
  `"This healthy smoothie"`, `"a Snickers"`). Optional. Falls back to
  `food_*_display_name`, then `food_*_name`.

The raw product name from the database is **not** used anywhere in output.

### Other fields

- `purpose` — passed through to the backend (e.g. `snack`, `meal`,
  `pre_workout`). Defaults to `snack` if blank.
- `angle` — free-form, ignored by the script (kept for downstream tooling).
- `framing_type` — selects a hook template. One of:
  - `health_halo` — Food A is perceived as healthier than Food B.
  - `kids_food` — Food A is marketed to kids.
  - `sugar_shock` — call out high-sugar items.
  - blank → default framing.

## Headline rules

Headlines are deterministic for now. The renderer reads `row["headline"]`;
the engine writes `headline` and `headlineMode` to `output/results.csv`.

### Modes (`headlineMode`)

There are four allowed modes. food_a is always the subject; food_b is the
reference baseline (or, for `swap`, the explicit recommendation).

- `shock` — small score gap; "wait, these are the same?"
- `exposure` — food_a has a health halo; debunk it
- `dominance` — large score gap; punchy and decisive
- `swap` — explicit "swap food_a for food_b" framing

### Mode selection

```
if format == 'swap':                                      → swap
elif format == 'exposure':                                → exposure
elif abs(score_a - score_b) <= 1:                         → shock
elif food_a has health halo (framing or category):        → exposure
elif abs(score_a - score_b) >= 3:                         → dominance
else:                                                     → shock
```

### Templates (6–8 per mode)

Each mode has 6–8 approved templates (see `HEADLINE_TEMPLATES` in
[main.py](main.py)). For `dominance` there are two internal sub-arrays
keyed by direction (a-wins / a-loses) so templates can be punchy without
contradicting the card visuals — the public `headlineMode` is still
`dominance`.

Hard rules enforced at import time:

- max 12 words per template
- never both `!` and `?` in the same headline
- at most one of `!` or `?`
- no hedging language: `slightly`, `somewhat`, `edges`, `a bit`,
  `kind of`, `not by much`, `relatively`

The validator raises at import if any template breaks these rules.

### Selection within a mode

The template index is `pair_id mod template_count`, so the same pair
always renders the same headline across runs.

### Future: OpenAI

Both the headline and the punchy bullets are split the same way:

- `generate_headline_deterministic(...)` / `punch_up_bullets_deterministic(...)`
  — what's wired up today.
- `generate_headline_with_openai(...)` / `punch_up_bullets_with_openai(...)`
  — placeholders that raise `NotImplementedError`. Same signatures so
  the dispatchers can swap them in without touching callers.
- `generate_headline(...)` / `punch_up_bullets(...)` — the dispatchers.
  Currently always call the deterministic versions. To turn on OpenAI,
  flip one line in each dispatcher.

## Postability filter

Every scored row gets a deterministic `postability_score` (1-10) considering
surprise, belief challenge, clarity, and emotional impact. Only rows with
`postability_score ≥ 7` are:

- written to `output/blog_inputs/`
- rendered as PNG cards
- marked `status=ok`

Lower-scoring rows stay in `output/results.csv` with `status=low_postability`
for QA but produce no card or blog input.

## Content priority

Each row gets a deterministic `content_priority` (0–100, higher = better
content). Used to rank rows for production:

| relation | health_halo | kids_food / sugar_shock | default |
|---|---|---|---|
| `a_loses` | 100 | 90 | 60 |
| `tie` | 95 | 90 | 70 |
| `a_barely_wins` | 85 | 60 | 60 |
| `a_wins` | 40 | 40 | 40 |

## Run

```
export BACKEND_BASE_URL="http://localhost:8000"
export FOOD_DB_PATH="/Users/stellar87/Developer/foodscore/backend/off_products.db"
python main.py
```

## Discovery + cleanup workflow

The system has these responsibilities, kept in separate scripts:

```
discover_candidates.py   → discover and score promising comparisons
discover_evaluations.py  → propose single-food "Is X Healthy?" posts
input_builder.py         → review / clean / approve into input.csv
main.py                  → score, render cards, write blog inputs + .mdx posts
audit_blog_posts.py      → validate staged .mdx (sections, links, dupes)
```

### 1. Discover candidates

`discover_candidates.py` walks the OFF DB for promising foods in a small
set of buckets (smoothie, juice, granola, protein bar, kids yogurt, …),
filters to rows with usable nutrition, scores only the promising ones
against a cached set of reference foods (Snickers, Coca-Cola, Frosted
Flakes, Doritos / Pringles, Skittles), and writes one `pending` row per
kept comparison to `output/discovery_results.csv`.

```
export BACKEND_BASE_URL="http://localhost:8000"
export FOOD_DB_PATH="/Users/.../foodscore/backend/off_products.db"
./venv/bin/python discover_candidates.py
```

Re-running is safe — existing `(candidate_barcode, reference_barcode)`
pairs are skipped, so each run only adds new discoveries. One failed
score doesn't stop the batch.

### 2. Build / clean input.csv (Streamlit)

`input_builder.py` is a local Streamlit app with two modes (selectable
in the sidebar):

```
pip install -r requirements-builder.txt
streamlit run input_builder.py
```

**Manual Search Mode** — search OFF directly, pick Food A / Food B from
rich result cards (per-serving nutrition, missing-field flags, image,
last-updated, completeness score), edit display name + label, preview,
**Append to input.csv**.

**Cleaner Mode** — load `output/discovery_results.csv`, sort the
`pending` rows by `postability_score` descending, and step through them
one at a time. Each row shows the candidate (food_a) and reference
(food_b) context (scores, what_helps, what_hurts, content angle), then
exposes editable `food_*` fields plus `format` / `framing_type` /
`purpose`. **Approve** appends to `input.csv` with an auto-incremented
`pair_id` and marks the discovery row `approved`; **Skip** marks it
`skipped`. Discovery rows already approved or skipped don't reappear in
the queue.

### 3. Render cards (main.py)

Once `input.csv` has the rows you want, run `main.py` as before to score,
write `output/results.csv`, generate the per-row blog inputs JSON, and
render the PNG cards.

### 4. Single-food "Is X Healthy?" posts (evaluations)

Comparison posts answer "X vs Y"; **evaluation** posts answer the more
common search query "Is X healthy?" / "Are X healthy?" with goal-aware
analysis. They reuse the same scoring pipeline, slug machinery, and
internal-linking system.

The `format` column on a row in `input.csv` selects the post type:

```
format=comparison  → "Is X Healthy? (X vs Y)"  body uses comparison template
format=swap        → "Is X Healthy? (X vs Y)"  body recommends Y as the swap
format=exposure    → "Is X Healthy?"           body exposes the halo on X
format=evaluation  → "Is/Are X Healthy?"       single-food, goal-aware
```

`format=evaluation` rows have `food_b_*` empty. The evaluation prompt
covers Quick Answer · Nutrition Snapshot · What Makes X a Good Choice ·
Potential Downsides · How X Fits Different Goals (weight loss / protein /
energy / heart) · Healthier Alternatives · Final Verdict — and frames
"healthy" as goal-dependent, never absolute.

**Discover candidates** from the existing comparison corpus:

```
./venv/bin/python discover_evaluations.py                   # dry-run preview
./venv/bin/python discover_evaluations.py --limit 20        # top 20 candidates
./venv/bin/python discover_evaluations.py --csv output/evaluation_candidates.csv
./venv/bin/python discover_evaluations.py --append-to-input # add directly to input.csv
```

The script walks `input.csv` for unique foods, normalizes (strips
`Original` / `Family Size` / `12 oz` style noise), dedupes, skips foods
whose normalized name is already covered by an evaluation row, and
proposes titles + slugs. Generic single-word categories (`smoothie`,
`yogurt`, `snack`) are filtered by default — pass `--include-vague` to
keep them.

**Generate** evaluation posts the same way as comparison posts — the
flag-based `main.py` flow handles them transparently:

```
./venv/bin/python main.py            # scores + writes blog_inputs + .mdx
./venv/bin/python main.py --blog-only   # regen .mdx from existing blog_inputs
./venv/bin/python main.py --force       # ignore caches; re-score everything
```

Slug shape is `is-popcorn-healthy.mdx` for singular / mass-noun foods and
`are-protein-bars-healthy.mdx` for plural categories. The is/are choice
is deterministic from the last word of the food name (curated plural
endings: `bars`, `chips`, `cookies`, `crackers`, `flakes`, `gummies`, …).
Brand-style names ending in `s` (Doritos, Skittles, Cheez-Its) stay
singular and get `Is`.

### 5. Audit staged posts

Before copying `.mdx` files into the website, run the auditor:

```
export WEBSITE_BLOG_DIR="/path/to/smartereats-website/content/blog"
./venv/bin/python audit_blog_posts.py
```

It walks `output/blog_posts/` (or specific files) and reports:

- frontmatter has all three required fields
- frontmatter `title:` matches the body H1
- required H2 sections per format (auto-detected by section presence)
- `<RelatedPosts slugs="…"/>` only references slugs in `WEBSITE_BLOG_DIR`
- inline `[text](/blog/slug)` links resolve in `WEBSITE_BLOG_DIR`
- no duplicate slugs across staged posts
- no duplicate evaluation topics under different grammar (e.g.
  `is-cheerios-healthy` and `are-cheerios-healthy`)
- no forbidden filler (`balanced diet`, `everything in moderation`, etc.)

Exits non-zero if any error-level issue is found.

### 6. Tests

```
./venv/bin/python -m unittest discover -s tests
```

Covers grammar / title / slug helpers, comparison-helper backward
compatibility, food-name normalization + dedup, vague-name filtering,
candidate extraction from a synthetic input.csv, and Related-section
slug safety.

## Output

### `output/results.csv`

```
pair_id, format,
food_a_name, food_a_display_name, food_a_label, food_a_barcode,
food_a_score, food_a_interpretation, food_a_what_helps, food_a_what_hurts,
food_b_name, food_b_display_name, food_b_label, food_b_barcode,
food_b_score, food_b_interpretation, food_b_what_helps, food_b_what_hurts,
score_diff, purpose, framing_type,
headline, headlineMode, content_priority,
postability_score, verdict,
status, error_message
```

- `headlineMode` — one of `shock`, `exposure`, `dominance`, `swap`
  (see Headline rules below).
- `postability_score` — 1-10, deterministic. Only rows with
  `postability_score ≥ 7` get rendered as cards and written as blog inputs.
- `verdict` — one-sentence summary of food_a, ready for blog synthesis.
- `status` — `ok` (passed filter) · `low_postability` (filtered) ·
  `skipped` (format=ranking) · `error` (scoring failure).
- `food_*_what_helps` and `food_*_what_hurts` — `" ; "`-joined lists from
  the backend's `whatHelps` / `whatHurts`. The card renderer reads food_a's
  helps or hurts (never food_b's, except in `swap` where food_b's helps
  describe why it's the recommended pick).

`food_*_what_helps` is a `" ; "`-joined list of strings that the backend
returned in `whatHelps`. The card renderer reads this column to draw the
bullet points under the winner and to derive the bottom summary line.

`status` is `ok` or `error`. A failure on one row does not stop the batch —
the row is recorded with `status=error` and a message in `error_message`,
and the script continues. After the run, the script prints totals and the
top 5 rows by `content_priority`.

### `output/blog_inputs/pair_{id}.json`

For each `status=ok` row, a JSON file with the structured inputs the
blog-post generator consumes:

```json
{
  "pair_id": "30",
  "format": "swap",
  "purpose": "breakfast",
  "framing_type": "",
  "headline": "Swap Orange Juice for a whole orange",
  "headlineMode": "swap",
  "verdict": "Avoid OJ — concentrated sugar and stripped of fiber.",
  "postability_score": 9,
  "food_a": {
    "name": "Tropicana Orange Juice",
    "display_name": "Orange Juice",
    "score": 4,
    "interpretation": "...",
    "what_helps": [...],
    "what_hurts": [...]
  },
  "food_b": { ... }   // null for exposure
}
```

### `output/blog_posts/<slug>.mdx`

After the JSON inputs are written, `main.py` calls
`write_blog_posts_from_inputs()`, which (when `OPENAI_API_KEY` is set)
asks the OpenAI chat-completions API to turn each JSON into a
350-500-word markdown blog post staged as `.mdx` for direct copy into
the website's `content/blog/`.

Each post:

- starts with YAML frontmatter — exactly three fields: `title`,
  `description`, `date` (no `category`, `tags`, `slug`, `image`, or
  `draft`)
- uses an SEO title of the form `Is {Food A} Healthy?` (with an optional
  `(Food A vs Food B)` parenthetical for comparison/swap), repeated as
  `# title` in the body
- follows the structure: `## Quick Answer` · `## Quick Verdict` (with
  `food_a.score` and `food_b.score` from the JSON) · `## Why {food_a}
  Falls Short` (3-4 bullets) · `## How {food_b} Compares` (2-3 bullets;
  for exposure-format posts this becomes `## What's Hidden in {food_a}`)
  · `## Best Choice Based on Your Goal` (Weight loss / Energy & satiety
  / Occasional treat) · `## Better Alternatives` (3-5 real foods) · the
  fixed CTA paragraph "Want a faster way to find better swaps?
  SmarterEats lets you compare foods and discover healthier options
  instantly." · `## Bottom Line`
- includes at least one numeric callout (sugar grams, calories, sodium,
  fiber) pulled verbatim from the JSON — never invented
- is instructed to use **only** the facts in the JSON — no invented
  nutrition numbers, no medical claims
- ends with a deterministic `## Related Comparisons` section appended in
  Python after the model call. Candidates come exclusively from the
  `WEBSITE_BLOG_DIR` directory (the live website's `content/blog/`) and
  are rotated by post index. The section is rendered as a single JSX
  component call — `<RelatedPosts slugs="slug1,slug2,slug3" />` — and
  the site looks up each destination's current title at build time, so
  titles never drift when posts are edited later. Skipped automatically
  when `WEBSITE_BLOG_DIR` is unset/invalid or when fewer than 2 other
  published posts exist; referenced slugs always resolve.

Filenames are the URL slug:
`output/blog_posts/is-gatorade-healthy-vs-coca-cola.mdx` (or
`is-nutella-healthy.mdx` for exposure-format posts). The slug is
deterministic from the food display names, not from the headline.

If `OPENAI_API_KEY` is absent, the run prints a warning and skips this
step. One failed API call is logged and the rest of the batch continues.
Card rendering is never blocked by blog generation failures. By default
posts whose `<slug>.mdx` already exists are skipped — pass `--force` to
regenerate everything.

Override the model with `OPENAI_MODEL=...` if you want something other
than the default `gpt-4o-mini`.

### `output/cards/*.png`

For every `status=ok` row, the script renders a **1080x1920** vertical
share-card, sorted by `content_priority` descending. Filenames:

```
output/cards/pair_{pair_id_zfill_3}_{a-display-slug}-vs-{b-display-slug}.png
```

Card layout (light gray canvas, centered white container with soft shadow):

- **White rounded container** with a drop shadow on a `#F7F7F7` canvas, so
  the content reads as a card in a feed rather than floating text.
- **Top-left green pill** with white text — `framing_type` if set,
  otherwise capitalized `purpose`.
- **Left-aligned headline** — the hook in bold near-black, auto-fit to
  ≤3 lines, max 880px wide.
- **food_a hero card** (full width). The verdict is keyed off **food_a's
  absolute score**, not the comparison:
  - score `≥ 7` → green `GOOD CHOICE` pill, light-green card fill
  - score `5–6` → amber `MEH` pill, soft-amber fill
  - score `≤ 4` → red `AVOID` pill, soft-red fill
  - large display name + a big orange score in a warm circle, with
    `/ 10` to the right
- **food_b reference strip** below the hero — small, gray, single line:
  `vs.  {display_name}     {score} / 10`. Never highlighted as a winner
  and never carries a "better choice" pill.
- **Up to 2 punchy bullets** transformed from the backend's
  `whatHelps` / `whatHurts` by [bullets.py](bullets.py):
  - `swap` format → green-dot bullets from food_b's helps (this is the
    one place food_b is praised — that's the point of `swap`)
  - `headlineMode == shock` (close-call comparisons) → muted-gray
    "neutral" bullets that emphasize sameness (`Same sugar problem`,
    `No real upgrade`)
  - food_a score `≥ 7` → green-dot "pick" bullets from food_a's helps
  - otherwise → red-dot "avoid" bullets from food_a's hurts
  - all bullets are ≤ 6 words, single phrase, no hedging language;
    minor vitamins / vague benefits are dropped per spec
- **Logo footer** — `assets/logo.png` at 80px height, centered. Falls back
  to bold `SmarterEats` text if the file is absent.

To use a custom logo, drop a transparent PNG at one of:

```
assets/logo.png       (preferred)
assets/logo.jpg
assets/smartereats.png
logo.png
```

The renderer auto-scales it to a 72px height.

Run the renderer on its own without re-scoring:

```
./venv/bin/python cards.py            # reads output/results.csv
./venv/bin/python cards.py path/to/results.csv path/to/output/dir
```

Pillow is used for rendering with the system Arial Bold/Regular fonts (with
DejaVu Sans fallback).

## Error handling

These are recorded per-row in `error_message` and never abort the batch.
Each error includes the food name for traceability:

- missing or `nan` barcode in the CSV
- barcode not found in the local DB
- nutrition fields missing (`calories`, `proteinGrams`, `totalFatGrams`,
  `carbGrams`) — the `/score` call is skipped when any are missing
- backend request failure (network, non-2xx)
- malformed backend response (non-JSON, missing `score`)
