"""Audit staged .mdx blog posts for the issues most likely to ship broken.

Focused on what we can check without a network call:
  - frontmatter has the required three fields (title, description, date)
  - frontmatter title matches the body H1 (no drift after edits)
  - filename slug matches the post type — comparison and evaluation slugs
    use distinct prefixes
  - required H2 sections are present per format
  - <RelatedPosts slugs="..." /> only references slugs that exist in
    WEBSITE_BLOG_DIR (no 404s)
  - inline /blog/<slug> markdown links resolve to WEBSITE_BLOG_DIR
  - duplicate slugs across staged posts (would silently overwrite at copy)
  - duplicate normalized food topics across evaluation posts
  - forbidden filler / punt phrases ("balanced diet," "moderation," etc.)

Default mode walks output/blog_posts/. Pass paths to audit specific files.
Exits non-zero if any error-level issue is found; prints all findings either
way for a quick triage view.

Usage:
    ./venv/bin/python audit_blog_posts.py
    ./venv/bin/python audit_blog_posts.py output/blog_posts/is-popcorn-healthy.mdx
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional


DEFAULT_DIR = "output/blog_posts"

# Required H2 section names per format. Exposure / swap currently ride on
# the comparison structure with format-aware section bodies — we only check
# headings that are mandatory in every variant.
COMPARISON_REQUIRED_H2: tuple[str, ...] = (
    "Quick Answer",
    "Quick Verdict",
    "Best Choice Based on Your Goal",
    "Better Alternatives",
    "Bottom Line",
)

EVALUATION_REQUIRED_H2: tuple[str, ...] = (
    "Quick Answer",
    "Nutrition Snapshot",
    "Potential Downsides",
    "Healthier or Better Alternatives",
    "Final Verdict",
)

FORBIDDEN_PHRASES: tuple[str, ...] = (
    "everything in moderation",
    "balanced diet",
    "context matters",
    "can be part of a healthy lifestyle",
    "not necessarily bad",
    "in a balanced diet",
    "as part of a balanced",
)

# Forbidden inline filler phrases (case-insensitive substring match).
FORBIDDEN_RE = re.compile(
    r"|".join(re.escape(p) for p in FORBIDDEN_PHRASES),
    re.IGNORECASE,
)

# Capture both the bullet-style and JSX-style related blocks so audits keep
# working through the format change.
JSX_RELATED_RE = re.compile(
    r'<RelatedPosts\b[^>]*\bslugs="([^"]+)"', re.IGNORECASE,
)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(/blog/([a-z0-9-]+)\)")


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _parse_frontmatter(text: str) -> Optional[dict]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None
    out: dict = {}
    for line in m.group(1).splitlines():
        match = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        k, v = match.group(1), match.group(2)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k] = v
    return out


def _h1(text: str) -> Optional[str]:
    """First # heading in the body. Skips frontmatter."""
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
    m = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    return m.group(1).strip() if m else None


def _h2_set(text: str) -> set[str]:
    return {m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE)}


def _is_evaluation_slug(slug: str) -> bool:
    return slug.startswith("is-") and slug.endswith("-healthy") and "-vs-" not in slug \
        or slug.startswith("are-") and slug.endswith("-healthy")


def _is_comparison_slug(slug: str) -> bool:
    return slug.startswith("is-") and "-vs-" in slug


def _normalize_evaluation_topic(slug: str) -> str:
    """Strip 'is-'/'are-' prefix and '-healthy' suffix to get the food
    topic — used to detect duplicate evaluation posts under different
    grammar (e.g. 'is-cheerios-healthy' and 'are-cheerios-healthy')."""
    s = slug
    for prefix in ("is-", "are-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if s.endswith("-healthy"):
        s = s[: -len("-healthy")]
    return s


def _published_slugs(website_blog_dir: Optional[str]) -> set[str]:
    """Mirror main.load_published_blog_index, locally — keeps this script
    importable without circular dependencies."""
    if not website_blog_dir:
        return set()
    p = Path(website_blog_dir)
    if not p.exists() or not p.is_dir():
        return set()
    return {mdx.stem for mdx in p.glob("*.mdx")}


def audit_post(path: Path, published: set[str]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one post. Errors block publishing;
    warnings are advisory."""
    errors: list[str] = []
    warnings: list[str] = []

    text = _read(path)
    if text is None:
        return [f"unreadable file: {path}"], []

    fm = _parse_frontmatter(text)
    if fm is None:
        errors.append("missing or malformed frontmatter")
        return errors, warnings

    for required in ("title", "description", "date"):
        if not (fm.get(required) or "").strip():
            errors.append(f"frontmatter missing `{required}`")
    if "category" in fm or "tags" in fm:
        warnings.append("frontmatter contains fields beyond title/description/date")

    title = fm.get("title", "")
    h1 = _h1(text)
    if title and h1 and title != h1:
        errors.append(f"frontmatter title != body H1 ({title!r} vs {h1!r})")

    slug = path.stem
    is_eval_slug = _is_evaluation_slug(slug)
    is_cmp_slug = _is_comparison_slug(slug)

    # Classify by sections, not slug. Comparison and exposure both produce
    # `is-X-healthy.mdx`-shaped slugs but use different section structures,
    # so we let the body decide what we should be checking.
    h2s = _h2_set(text)
    is_evaluation_post = "Nutrition Snapshot" in h2s and "Final Verdict" in h2s
    expected_h2 = EVALUATION_REQUIRED_H2 if is_evaluation_post else COMPARISON_REQUIRED_H2
    for h in expected_h2:
        if h not in h2s:
            errors.append(f"missing required section: ## {h}")

    # Sanity-check: an evaluation post should have an evaluation-shaped slug.
    if is_evaluation_post and not is_eval_slug:
        warnings.append(
            f"evaluation-format post but slug {slug!r} doesn't match "
            "is-X-healthy / are-X-healthy"
        )

    # Forbidden filler / punt phrases.
    for m in FORBIDDEN_RE.finditer(text):
        warnings.append(f"forbidden phrase {m.group(0)!r} at offset {m.start()}")

    # JSX <RelatedPosts /> slug references must all be published.
    for m in JSX_RELATED_RE.finditer(text):
        slugs = [s.strip() for s in m.group(1).split(",") if s.strip()]
        if not slugs:
            warnings.append("<RelatedPosts /> with empty slugs attribute")
        for s in slugs:
            if published and s not in published:
                errors.append(f"<RelatedPosts /> references unpublished slug {s!r}")

    # Inline markdown /blog/<slug> links — flag any that don't resolve.
    for m in MARKDOWN_LINK_RE.finditer(text):
        s = m.group(1)
        if published and s not in published and s != slug:
            warnings.append(f"inline link to /blog/{s} which isn't in WEBSITE_BLOG_DIR")

    if not is_eval_slug and not is_cmp_slug:
        warnings.append(
            f"slug {slug!r} doesn't match either evaluation "
            "(is-X-healthy / are-X-healthy) or comparison (is-X-healthy-vs-Y) shape"
        )

    return errors, warnings


def audit_directory(paths: list[Path], website_blog_dir: Optional[str]) -> int:
    published = _published_slugs(website_blog_dir)
    if website_blog_dir and not published:
        print(f"WARNING: WEBSITE_BLOG_DIR={website_blog_dir!r} resolved to no "
              ".mdx files; link-resolution checks will skip.", file=sys.stderr)

    seen_slugs: dict[str, Path] = {}
    seen_topics: dict[str, Path] = {}
    total_errors = 0
    total_warnings = 0

    for path in paths:
        slug = path.stem
        errors, warnings = audit_post(path, published)

        # Cross-post checks.
        if slug in seen_slugs:
            errors.append(f"duplicate slug — also at {seen_slugs[slug]}")
        else:
            seen_slugs[slug] = path
        if _is_evaluation_slug(slug):
            topic = _normalize_evaluation_topic(slug)
            if topic in seen_topics:
                errors.append(
                    f"duplicate evaluation topic {topic!r} — also at "
                    f"{seen_topics[topic]}"
                )
            else:
                seen_topics[topic] = path

        if errors or warnings:
            print(f"\n{path}")
            for e in errors:
                print(f"  ERROR: {e}")
            for w in warnings:
                print(f"  warn:  {w}")
        total_errors += len(errors)
        total_warnings += len(warnings)

    print()
    print(f"=== Audit summary ===")
    print(f"Posts checked: {len(paths)}")
    print(f"Errors:        {total_errors}")
    print(f"Warnings:      {total_warnings}")
    return 0 if total_errors == 0 else 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Audit staged blog posts for structure / link / phrase issues.",
    )
    p.add_argument("paths", nargs="*",
                   help=f"specific .mdx files to audit (default: scan {DEFAULT_DIR}/)")
    p.add_argument("--dir", default=DEFAULT_DIR,
                   help=f"directory to scan when no paths are given (default {DEFAULT_DIR})")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.paths:
        paths = [Path(p) for p in args.paths]
    else:
        d = Path(args.dir)
        if not d.is_dir():
            print(f"ERROR: {d} is not a directory", file=sys.stderr)
            return 2
        paths = sorted(d.glob("*.mdx"))
    if not paths:
        print(f"No .mdx files found.")
        return 0
    return audit_directory(paths, os.environ.get("WEBSITE_BLOG_DIR"))


if __name__ == "__main__":
    sys.exit(main())
