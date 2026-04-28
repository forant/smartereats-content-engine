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

Two environment variables:

- `FOOD_DB_PATH` — path to the SQLite OFF DB. Default: `./foodscore.db`.
- `BACKEND_BASE_URL` — base URL of the SmarterEats backend, e.g.
  `http://localhost:8000`. Required.

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

Generation is split into:

- `generate_headline_deterministic(row, fmt, score_a, score_b, halo)`
  — what's wired up today.
- `generate_headline_with_openai(...)` — placeholder, raises
  `NotImplementedError`. Same signature so the dispatcher can swap it
  in without touching callers.
- `generate_headline(...)` — the dispatcher. Currently always calls the
  deterministic generator. To turn on OpenAI later, change one line in
  the dispatcher.

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

## Building input.csv (helper UI)

`input_builder.py` is a local Streamlit app that searches the OFF database
and appends clean rows to `input.csv` — no backend calls, no scoring, no
rendering. One-time setup:

```
pip install -r requirements-builder.txt
```

Then run from the project root:

```
streamlit run input_builder.py
```

Pick `format`, `framing_type`, `purpose`, search OFF for each food, edit
the display name and label, preview the row, click **Append to input.csv**.
The `pair_id` auto-increments. The current `input.csv` is shown as a table
at the bottom with a download button.

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

For each `status=ok` row, a JSON file with the structured inputs needed to
generate a blog post downstream (e.g. via OpenAI). Shape:

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

This script does not call OpenAI — these files are inputs for a separate
blog-generation step.

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
- **Up to 2 bullets describing food_a only**:
  - food_a score `≥ 5` → green-dot bullets from `food_a_what_helps`
  - food_a score `< 5` → red-dot bullets from `food_a_what_hurts`
  - `food_b_what_helps` is intentionally never shown
  - copy is cleaned (`"competitor"` clauses stripped, first letter
    capitalized)
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
