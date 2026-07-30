---
description: Everything Wreath ships in one dependency-free package, and the package you would otherwise install for each of them.
keywords: batteries included, dependencies, requirements.txt, what does wreath include, do i still need, do i still install
---
# What you don't have to install

Wreath is one package with no mandatory runtime dependencies, and it is a wide
one on purpose: the parts of a web application that always end up in the same
`requirements.txt` are here, in the module their name implies, tested together
and released together. The table below is the whole of that surface, with the
packages you would otherwise reach for beside each row so you can find a
capability in the vocabulary you already have.

Nothing here is a comparison. Those package names are good software and most of
them are load-bearing somewhere in this ecosystem; they are in the table because
they are how you would *say* the capability today, not because Wreath is
arguing with them. Every row links to the guide that teaches it — this page
teaches nothing, it just tells you where to look.

::: capability-map

The table is generated from `docs/agents/manifest.json` at build time, and
`uv run wreath-map-lint` fails when a subsystem is missing from it or points at
a page that is not there. That is deliberate: a hand-written map of three dozen
rows is a map that is wrong within a month. The package names in it are also
what the site's own search matches this page on, so if you got here by typing
`celery` into `Ctrl K`, that is why — and the row it belongs to is the link you
wanted.

## What Wreath does not include

A page that claims everything is read as a page that claims nothing, so here is
the other list. None of these are planned, and all of them compose with Wreath
the ordinary way — install the package and call it.

- **Image and media processing.** No thumbnails, no resizing, no EXIF. Reach for
  `pillow`. Wreath will store the bytes for you (`wreath.objects`) and run the
  work off the request path (`wreath.jobs`), but the pixels are yours.
- **Spreadsheet and PDF export.** `openpyxl`, `xlsxwriter`, `weasyprint`,
  `reportlab`. Wreath renders JSON, HTML, and MessagePack; anything with a page
  size is somebody else's job.
- **Payments.** `stripe`, and every other provider SDK. Signed webhooks arrive
  through `wreath.webhooks`, which is the part that is easy to get wrong; the
  billing model is the part that is yours.
- **Transactional email and SMS providers.** `wreath.users` sends its
  verification and password-reset mail over plain SMTP. A provider API —
  `resend`, `sendgrid`, `postmark`, `twilio` — is an ordinary outbound call
  through `wreath.http_client`.
- **Data science and machine learning.** `pandas`, `numpy`, `scikit-learn`, and
  every embedding model. `wreath.series` will compute a chart's buckets in the
  database rather than in a loop, and that is the extent of the arithmetic it
  has opinions about; turning text into a vector is a call you make to a model
  provider, not something a web framework should own. `langchain` and
  `llama-index` are deliberately absent from every row above for the same
  reason: chunking, embedding, and model orchestration are not in this box, and
  a row claiming otherwise would be the first false one on a page whose whole
  argument is that it is generated from what the tree actually contains.
- **A dedicated search engine.** `elasticsearch`, `opensearch-py`,
  `meilisearch`, `typesense`. The retrieval PostgreSQL already does is first
  class here — `Vector` columns with HNSW and IVFFlat indexes
  ([Vector search](guides/vector-search.md)), generated `TsVector` columns with
  GIN ([Full-text search](guides/full-text-search.md)), and a fusion of the two
  by rank ([Hybrid search](guides/hybrid-search.md)) — which is why the ORM row
  above names the vector stores (`chromadb`, `qdrant-client`,
  `pinecone-client`, `weaviate-client`) among what it replaces. Read that row
  narrowly: it is a claim about *storing vectors and ranking by distance*, in
  the database you already run and back up, which for a great many applications
  is the entire requirement. It is not parity with a search product. There are
  no custom analyzers or per-language stemming pipelines, no faceting or
  aggregation layer, no synonym and typo-tolerance dictionaries, no managed
  hybrid re-ranking service, and no sharded cluster for a corpus that has
  outgrown one PostgreSQL. When you need those, install one — and `wreath.jobs`
  is a good place to keep it in step with your tables.
- **Admin UI as a product.** There is no Django-admin equivalent. There is
  enough to build one — generated CRUD, pagination, permissions, templates — and
  a recipe that does: [Build an admin console](cookbook/recipes/build-an-admin-console.md).
- **Non-PostgreSQL databases.** Wreath ships and owns a PostgreSQL driver, and
  the ORM is built for it specifically. There is no MySQL or SQLite backend, and
  no adapter layer that would pretend there could be one.

And a shorter list still: the surfaces Wreath has named but not finished are on
[Reserved and in-progress surfaces](reference/roadmap.md), which is the single
place that answer lives. If something above sounds like it should be on this
page and is not, that page is where to check next.
