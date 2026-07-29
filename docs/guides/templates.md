# Templates

When you're rendering HTML rather than returning JSON, `wreath.templates` gives
you a small, safe server-side template system. "Safe" is the important word:
values are HTML-escaped by default, so a user's name that happens to contain a
`<script>` tag is rendered as text, not executed. You mark something as trusted
HTML deliberately, never by accident.

## User story: render user content without opening an XSS hole

> *As an API author, I render a comments page. The comment text comes from users,
> so it has to be escaped — but I also loop over a list, and I don't want to
> remember to escape each field by hand.*

```python
comments_template = templates.compile("comments.html")   # compiled at startup

@app.get("/comments")
async def comments(request) -> HTMLResponse:
    rows = await load_comments()
    return HTMLResponse(comments_template.render(rows=rows))
```

The template loops with `{% for %}` and interpolates with `{{ }}`:

```html
{% for c in rows %}<li>{{ c.author }}: {{ c.body }}</li>{% endfor %}
```

Every `{{ }}` is HTML-escaped, so a comment body containing `<script>…</script>`
renders as text, not markup. Escaping is the default; you opt *out* for a value
you trust by wrapping it in `Markup`, never the other way around.

```python
from wreath.templates import TemplateDirectory
from wreath.response import HTMLResponse

templates = TemplateDirectory("templates")
home_template = templates.compile("home.html")   # compile once, at startup

@app.get("/")
async def home(request) -> HTMLResponse:
    return HTMLResponse(home_template.render(title="Wreath"))
```

A template compiles once — into a flat opcode tape, at startup — and rendering
never touches the disk. That split is deliberate: syntax errors surface when
the application boots, not on the first request that happens to hit the page.

Use `escape` and `Markup` when you need to control escaping by hand, and expect
clear, typed errors for syntax and render mistakes rather than a stack trace from
deep inside the engine.

## Private names are not reachable

A lookup resolves by subscript first and then by attribute, so `{{ order.total }}`
works whether `order` is a dict or an object. A dotted path may not name anything
beginning with an underscore, in any position — `{{ u.__init__.__globals__.API_KEY }}`
is a compile error, not a way to read a module global into the page. The language
has no way to *call* anything either: the tape holds text, variables, loops, and
conditionals, and nothing else.

That matters if template source ever reaches you from outside your codebase — a
tenant-supplied email layout, a template stored in a config field. Compiling
untrusted source is still a bad idea and this is not permission to do it, but it
is no longer the difference between a rendered page and your credentials. This is
one of the two paths that put an autonomous agent inside Hugging Face's dataset
pipeline in July 2026; the other was a loader that unpickled remote code, which
is why nothing in wreath deserializes anything but JSON.

**Reference:** [`wreath.templates`](../reference/templates.md).
