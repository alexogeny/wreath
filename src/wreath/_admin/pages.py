"""The admin's HTML, as templates compiled once at import.

Server-rendered, and **no JavaScript at all**. That is a deliberate decision
rather than an omission, and it settles two things the plan left open:

* `wreath.policy.security` has no CSP nonce support, so any policy for a
  page carrying inline script has to be permissive. A page with no script needs
  no nonce, so the admin ships `script-src 'none'` -- the strongest policy
  available -- instead of waiting for a nonce mechanism it would then have to
  keep correct.
* Attribute-driven partial updates would need a named-fragment renderer that
  `wreath.templates` does not have. Full-page forms need no such thing, so that
  addition is not made here; see the roadmap.

Every page is composed from one shell at import time, because
`Template.from_string` resolves no includes -- the composition is string
concatenation before compilation, which costs nothing at render time.

The markup is held to `wreath.audit`'s WCAG 2.1 A/AA rules by
`tests/admin/test_admin_accessibility.py`, which runs the auditor over rendered
output rather than over a checklist.
"""

from __future__ import annotations

from ..templates import Template

__all__ = [
    "CONTENT_SECURITY_POLICY",
    "CONFIRM_TEMPLATE",
    "DETAIL_TEMPLATE",
    "FORM_TEMPLATE",
    "INDEX_TEMPLATE",
    "LIST_TEMPLATE",
]

#: The policy the admin's own responses carry. No script, no frame, no object,
#: no base rewrite, and forms may only post back to this origin. `style-src
#: 'unsafe-inline'` is the one relaxation, for the inline stylesheet below; it
#: is not a script vector, and externalising it would make the stylesheet a
#: render-blocking request for a page that otherwise needs none.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)

# Contrast pairs are stated together in each rule so `wreath.audit`'s `contrast`
# check can read them unambiguously. Light: #1a1a1a on #ffffff is 16.1:1,
# #0b4fa8 on #ffffff is 8.6:1. Dark: #ededed on #121212 is 15.6:1, #7cb0ea on
# #121212 is 7.1:1. Borders are #767676 / #8a8a8a, both past the 3:1 that
# `non-text-contrast` wants of a form control's edge.
_STYLE = """\
:root { color-scheme: light dark; }
body { color: #1a1a1a; background: #ffffff;
  font: 16px/1.5 system-ui, sans-serif; margin: 0; }
main { display: block; max-width: 60rem; margin: 0 auto; padding: 1rem; }
a { color: #0b4fa8; background: #ffffff; }
a:focus, button:focus, input:focus, select:focus, textarea:focus {
  outline: 3px solid #0b4fa8; outline-offset: 2px; }
h1 { font-size: 1.6rem; margin: 0 0 1rem; }
h2 { font-size: 1.2rem; margin: 1.5rem 0 0.5rem; }
nav { border-bottom: 1px solid #767676; padding: 0.5rem 1rem; }
nav ul { list-style: none; display: flex; flex-wrap: wrap;
  gap: 1rem; margin: 0; padding: 0; }
table { border-collapse: collapse; width: 100%; }
caption { text-align: left; padding-bottom: 0.5rem; }
th, td { border: 1px solid #767676; padding: 0.4rem 0.6rem; text-align: left; }
th { color: #1a1a1a; background: #f0f0f0; }
dl { display: grid; grid-template-columns: minmax(8rem, 14rem) 1fr; gap: 0.4rem 1rem; }
dt { font-weight: 600; }
dd { margin: 0; overflow-wrap: anywhere; }
.field { margin-bottom: 1rem; }
.field label { display: block; font-weight: 600; margin-bottom: 0.25rem; }
.field input, .field select, .field textarea {
  color: #1a1a1a; background: #ffffff;
  border: 1px solid #767676; border-radius: 3px;
  padding: 0.4rem; width: 100%; max-width: 32rem; font: inherit; }
.field .hint { display: block; font-size: 0.85rem; }
.button { color: #ffffff; background: #0b4fa8; border: 1px solid #0b4fa8;
  border-radius: 3px; padding: 0.45rem 0.9rem; font: inherit; cursor: pointer; }
.button.danger { color: #ffffff; background: #a4262c; border-color: #a4262c; }
.errors { color: #7a1a1f; background: #fdecee; border: 1px solid #a4262c;
  padding: 0.6rem 1rem; }
.withheld { color: #595959; background: #ffffff; font-style: italic; }
.pager { display: flex; gap: 1rem; align-items: center; margin-top: 1rem; }
.actions { display: flex; gap: 0.75rem; align-items: center; margin-top: 1rem; }
@media (prefers-color-scheme: dark) {
  body { color: #ededed; background: #121212; }
  a { color: #7cb0ea; background: #121212; }
  nav { border-bottom-color: #8a8a8a; }
  th, td { border-color: #8a8a8a; }
  th { color: #ededed; background: #1f1f1f; }
  .field input, .field select, .field textarea {
    color: #ededed; background: #1f1f1f; border-color: #8a8a8a; }
  .errors { color: #ffd9dc; background: #3a1114; border-color: #e2818a; }
  .withheld { color: #b8b8b8; background: #121212; }
}
"""

_SHELL = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
%(style)s</style>
</head>
<body>
<nav aria-label="Administered models">
<ul>
<li><a href="{{ home_url }}">Overview</a></li>
{%% for item in models %%}<li><a href="{{ item.url }}">{{ item.label }}</a></li>
{%% endfor %%}</ul>
</nav>
<main>
<h1>{{ heading }}</h1>
%(body)s</main>
</body>
</html>
"""


def _page(body: str) -> Template:
    """Compose one page from the shell and compile it. Import-time only."""
    return Template.from_string(_SHELL % {"style": _STYLE, "body": body}, "admin")


INDEX_TEMPLATE = _page("""\
<p>{{ intro }}</p>
{% if empty %}<p>No models are registered.</p>
{% endif %}{% if models %}<table>
<caption>Registered models</caption>
<thead><tr><th scope="col">Model</th><th scope="col">Table</th></tr></thead>
<tbody>
{% for item in models %}<tr><td><a href="{{ item.url }}">{{ item.label }}</a></td>
<td>{{ item.table }}</td></tr>
{% endfor %}</tbody>
</table>
{% endif %}""")

LIST_TEMPLATE = _page("""\
{% if can_create %}<p class="actions"><a class="button" href="{{ create_url }}">Add {{ model_label }}</a></p>
{% endif %}{% if empty %}<p>No rows to show.</p>
{% endif %}{% if rows %}<table>
<caption>{{ caption }}</caption>
<thead><tr>
{% for column in headers %}<th scope="col">{% if column.sort_url %}<a href="{{ column.sort_url }}">{{ column.label }}</a>{% else %}{{ column.label }}{% endif %}</th>
{% endfor %}<th scope="col">Actions</th></tr></thead>
<tbody>
{% for row in rows %}<tr>
{% for cell in row.cells %}<td>{% if cell.withheld %}<span class="withheld">{{ cell.value }}</span>{% else %}{{ cell.value }}{% endif %}</td>
{% endfor %}<td><a href="{{ row.url }}">View {{ row.label }}</a></td></tr>
{% endfor %}</tbody>
</table>
{% endif %}<p class="pager">
{% if page.has_prev %}<a href="{{ page.prev_url }}">Previous page</a>
{% endif %}<span>{{ page.summary }}</span>
{% if page.has_next %}<a href="{{ page.next_url }}">Next page</a>
{% endif %}</p>""")

DETAIL_TEMPLATE = _page("""\
<dl>
{% for field in fields %}<dt>{{ field.label }}</dt>
<dd>{% if field.withheld %}<span class="withheld">{{ field.value }}</span>{% else %}{{ field.value }}{% endif %}</dd>
{% endfor %}</dl>
<p class="actions"><a href="{{ list_url }}">Back to {{ model_label }} list</a>
{% if can_edit %}<a class="button" href="{{ edit_url }}">Edit this {{ model_label }}</a>
{% endif %}{% if can_delete %}<a class="button danger" href="{{ delete_url }}">Delete this {{ model_label }}</a>
{% endif %}</p>""")

FORM_TEMPLATE = _page("""\
{% if has_errors %}<div class="errors"><h2>The form was not accepted</h2>
<ul>
{% for error in errors %}<li>{{ error.message }}</li>
{% endfor %}</ul></div>
{% endif %}{% if empty %}<p>No fields of this model are writable by you.</p>
{% endif %}<form method="post" action="{{ action }}">
{% for field in fields %}<div class="field">
<label for="{{ field.id }}">{{ field.label }}</label>
{% if field.multiline %}<textarea id="{{ field.id }}" name="{{ field.name }}" rows="4" aria-describedby="{{ field.hint_id }}"{% if field.required %} required{% endif %}>{{ field.value }}</textarea>
{% endif %}{% if field.boolean %}<input type="checkbox" id="{{ field.id }}" name="{{ field.name }}" value="true" aria-describedby="{{ field.hint_id }}"{% if field.checked %} checked{% endif %}>
{% endif %}{% if field.plain %}<input type="{{ field.type }}" id="{{ field.id }}" name="{{ field.name }}" value="{{ field.value }}" aria-describedby="{{ field.hint_id }}"{% if field.required %} required{% endif %}{% if field.numeric %} step="any"{% endif %}>
{% endif %}<span class="hint" id="{{ field.hint_id }}">{{ field.hint }}</span>
</div>
{% endfor %}<p class="actions"><button class="button" type="submit">{{ submit_label }}</button>
<a href="{{ cancel_url }}">Cancel</a></p>
</form>""")

CONFIRM_TEMPLATE = _page("""\
<p>{{ prompt }}</p>
<dl>
{% for field in fields %}<dt>{{ field.label }}</dt>
<dd>{% if field.withheld %}<span class="withheld">{{ field.value }}</span>{% else %}{{ field.value }}{% endif %}</dd>
{% endfor %}</dl>
<form method="post" action="{{ action }}">
<p class="actions"><button class="button danger" type="submit">{{ submit_label }}</button>
<a href="{{ cancel_url }}">Cancel</a></p>
</form>""")
