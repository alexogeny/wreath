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

**Reference:** [`wreath.templates`](../reference/templates.md).
