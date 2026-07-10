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

# Placeholders substituted at assessment time (double-brace tokens):
#   {{FEATURE_NAME}}          the feature we're scoring fit for
#   {{FEATURE_DOCUMENTATION}} text fetched from the feature's documentation URL
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
