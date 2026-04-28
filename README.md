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
framing_type
```

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

## Hook rules

Hooks are deterministic. The relation between scores is one of:

- `tie` — same score
- `a_barely_wins` — A wins by exactly 1
- `a_loses` — A scores below B (any margin)
- `a_wins` — A wins by 2+

| framing | tie | a_barely_wins | a_loses | a_wins |
|---|---|---|---|---|
| `health_halo` | `{A} is basically the same as {B}` | `{A} is barely better than {B}` | `{A} is worse than {B}` | `{A} wins, but it’s not as clean as it looks` |
| `kids_food` | `This kids snack is basically candy` | `This kids snack is barely better than candy` | `This kids snack is basically candy` | `This kids snack actually holds up` |
| `sugar_shock` | `{A} is basically dessert` | `{A} is barely better than dessert` | `{A} is basically dessert` | `{A} beats dessert, but check the sugar` |
| default | `{A} is basically the same as {B}` | `{A} is barely better than {B}` | `{A} is worse than {B}` | `{A} actually beats {B}` |

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

## Output

### `output/results.csv`

```
pair_id,
food_a_name, food_a_display_name, food_a_label, food_a_barcode,
food_a_score, food_a_interpretation, food_a_what_helps,
food_b_name, food_b_display_name, food_b_label, food_b_barcode,
food_b_score, food_b_interpretation, food_b_what_helps,
score_diff, purpose, framing_type, hook, content_priority,
status, error_message
```

`food_*_what_helps` is a `" ; "`-joined list of strings that the backend
returned in `whatHelps`. The card renderer reads this column to draw the
bullet points under the winner and to derive the bottom summary line.

`status` is `ok` or `error`. A failure on one row does not stop the batch —
the row is recorded with `status=error` and a message in `error_message`,
and the script continues. After the run, the script prints totals and the
top 5 rows by `content_priority`.

### `output/cards/*.png`

For every `status=ok` row, the script renders a **1080x1920** vertical
share-card, sorted by `content_priority` descending. Filenames:

```
output/cards/pair_{pair_id_zfill_3}_{a-display-slug}-vs-{b-display-slug}.png
```

Card layout (white bg, dark text, green accent, orange score):

- **Top-left pill** — green capsule with white text. Uses `framing_type` if
  set (`"Health Halo"`, `"Sugar Shock"`, `"Kids Food"`), otherwise the
  capitalized `purpose` (`"Snack"`, `"Breakfast"`, …).
- **Left-aligned headline** — the hook in bold near-black, auto-fit to
  ≤3 lines, max 900px wide.
- **Two side-by-side comparison cards** (440px wide, 40px gap) with rounded
  corners. The higher score is the **winner**:
  - light green fill + green border + green `BEST CHOICE` capsule at the
    top of the card
  - the loser gets a neutral light-gray fill with a subtle border
  - on a tie, neither card is highlighted
- **Score circle** inside each card — soft warm peach fill with the score
  in large bold orange, and a muted `/ 10` directly below.
- **Up to 2 green-bullet reasons** under the winning card, sourced from the
  winner's `food_*_what_helps`. Hidden on tie or when no helps are
  available.
- **Bottom summary line** in centered muted gray. Uses an explicit `summary`
  column from the CSV if present, otherwise distills the winner's first
  one or two `whatHelps` phrases (`"More protein, less processed"`). On a
  tie, falls back to `"Closer than you think."`.
- **Footer** — uses `assets/logo.png` if it exists in the project root,
  otherwise renders bold `SmarterEats` text. Centered near the bottom.

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
