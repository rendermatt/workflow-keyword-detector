"""Seed data used to pre-populate the database on first run.

`SEED_FEATURES` gives the app one working feature to manage instead of an empty
screen. `DEFAULT_FIT_PROMPT` is the starting prompt the LLM uses to judge whether
a crawled company is a good fit for a feature; it is stored (and editable) in the
`app_settings` table, and the workflow falls back to its own copy if the setting
is ever missing.
"""

SEED_FEATURES = [
    {
        "name": "Workflows",
        "documentation_url": "https://render.com/docs/workflows",
    },
]

# The single global base prompt. It owns the placeholders and standardizes the
# scoring + output format; each feature contributes only the free-text snippet
# that lands at {{FEATURE_INSTRUCTIONS}}.
#
# Placeholders substituted at assessment time (double-brace tokens):
#   {{FEATURE_NAME}}          the feature we're scoring fit for
#   {{FEATURE_DOCUMENTATION}} text fetched from the feature's documentation URL
#   {{FEATURE_INSTRUCTIONS}}  the feature's own "additional guidance" field
#   {{CUSTOMER_DOMAIN}}       the root domain we crawled (the prospect)
#   {{CRAWLED_CONTENT}}       visible text gathered across the prospect's site
DEFAULT_FIT_PROMPT = """\
You are a go-to-market analyst deciding whether a company would be a good fit for \
a specific product feature.

The feature you are evaluating fit for is "{{FEATURE_NAME}}".

Here is the feature's official documentation, which describes what it does and who \
it is for:
<feature_documentation>
{{FEATURE_DOCUMENTATION}}
</feature_documentation>

Additional guidance specific to this feature (may be empty) — when present, weight \
it heavily alongside the documentation:
<feature_specific_guidance>
{{FEATURE_INSTRUCTIONS}}
</feature_specific_guidance>

You are evaluating the company at the domain "{{CUSTOMER_DOMAIN}}". Below is text \
crawled from across their public website — marketing pages, product and docs pages, \
blog posts, engineering and careers pages, and so on:
<crawled_content>
{{CRAWLED_CONTENT}}
</crawled_content>

Based only on the evidence above, assess how strong a fit this company is for the \
"{{FEATURE_NAME}}" feature. A strong fit is a company whose business, technical \
stack, or stated needs align with the problems this feature solves — one that would \
plausibly adopt it and get real value from it.

Return:
- fit_score: an integer from 0 to 100 for overall fit (0 = no fit, 100 = ideal fit).
- tier: one of "strong" (75-100), "promising" (50-74), "weak" (25-49), or
  "unlikely" (0-24), consistent with the score.
- summary: 2-4 sentences explaining your reasoning, grounded in specific evidence
  from their site.
- signals: a list of concrete signals you found — quote or closely paraphrase
  specific things from the crawled content that support your assessment. If the
  content is thin, say what was missing.
- recommendation: one sentence on how a sales or growth team should approach this
  company about the feature.

If the crawled content is empty or uninformative, return a low score and say the \
site could not be meaningfully assessed."""


# The global documentation-distillation prompt. It's identical for every feature
# — the only feature-specific input is the fetched documentation text. Run once
# per crawl (cached until the docs or this prompt change) to turn a docs page
# into the compact brief that {{FEATURE_DOCUMENTATION}} carries in the base
# prompt above.
#
# Placeholders:
#   {{FEATURE_NAME}}    the feature's name
#   {{DOCUMENTATION}}   visible text fetched from the feature's documentation URL
DEFAULT_DISTILL_PROMPT = """\
The text below is documentation for a product feature called "{{FEATURE_NAME}}". \
Distill it into a concise, self-contained brief that another analyst will use to \
judge whether a company is a good fit for this feature — without ever seeing the \
original documentation.

Cover, in this order:
- What the feature does, in 2-3 sentences.
- The kinds of companies, teams, technical stacks, or use cases it is built for.
- Concrete signals on a company's website that would indicate a STRONG fit.
- Signals that would indicate a WEAK or poor fit.

Be specific and compact — aim for under ~400 words. Output the brief as plain text \
with no preamble.

<documentation>
{{DOCUMENTATION}}
</documentation>"""
