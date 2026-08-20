---
description: The generated inventory of what Wreath ships and the packages each subsystem can replace.
keywords: batteries included, dependencies, requirements.txt, what does wreath include, do i still install
---

# What you do not have to install

Wreath is wide on purpose. The parts that repeatedly meet in one application
ship in one package, with one version and one set of tests. Each part keeps an
ordinary name so you can find it without learning the brand first.

The table is generated from the repository's subsystem manifest. Every row
links to the guide that owns the idea and names packages a team might otherwise
reach for. Those names are search terms, not insults; they translate vocabulary
you already know into Wreath's surface.

::: capability-map

## Ask the same question in a terminal

```bash
wreath capabilities celery
wreath capabilities redis
wreath capabilities csrf
wreath capabilities wreath.messaging
```

The command reports every match and why it matched. A package name can map to
more than one subsystem because replacing only one use is how duplicate
infrastructure returns. `--json` emits the same result for scripts. No match
exits `1`.

The command opens no application, socket, or database. It answers from the same
checked-in manifest that builds this page, so it also works before a project
exists.

## What stays outside Wreath

One system does not mean every problem. These boundaries are deliberate:

| Need | Use outside Wreath | What Wreath still owns |
|---|---|---|
| image and media processing | Pillow or a media service | object storage and off-request jobs |
| spreadsheets and PDF export | openpyxl, xlsxwriter, WeasyPrint, ReportLab | the data and delivery boundary |
| payments | a provider SDK | signed webhook receipt and outbound calls |
| email or SMS provider APIs | Resend, Postmark, SendGrid, Twilio | message policy, SMTP, DKIM, suppression, retries around your call |
| data science and model inference | NumPy, pandas, scikit-learn, model providers | storage, vector retrieval, jobs, resumable delivery |
| dedicated search products | Elasticsearch, OpenSearch, Meilisearch, Typesense | PostgreSQL full-text, vector, and hybrid retrieval |
| non-PostgreSQL databases | the database's own driver and tools | nothing disguised as a portable ORM adapter |

Wreath's PostgreSQL retrieval is intentionally strong: generated full-text
columns, vector indexes, and rank fusion cover many application-sized search
problems. It does not claim custom analyzer pipelines, typo dictionaries,
faceting, managed reranking, or a sharded search cluster.

Wreath also does not hide unfinished work in this table. Named surfaces that
have not shipped live only in the [roadmap](reference/roadmap.md).

## Why the map can be trusted

The build derives this page and the packaged command index from
`docs/agents/manifest.json`. `wreath-map-lint` fails when a public module has no
owner, an owner has no guide, or the two generated views disagree. The table is
large; its source of truth is not handwritten prose.
