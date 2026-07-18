# Templates

When you're rendering HTML rather than returning JSON, `wreath.templates` gives
you a small, safe server-side template system. "Safe" is the important word:
values are HTML-escaped by default, so a user's name that happens to contain a
`<script>` tag is rendered as text, not executed. You mark something as trusted
HTML deliberately, never by accident.

```python
from wreath.templates import TemplateDirectory
from wreath.response import HTMLResponse

templates = TemplateDirectory("templates")

@app.get("/")
async def home(request) -> HTMLResponse:
    return HTMLResponse(templates.render("home.html", title="Wreath"))
```

Use `escape` and `Markup` when you need to control escaping by hand, and expect
clear, typed errors for syntax and render mistakes rather than a stack trace from
deep inside the engine.

**Reference:** [`wreath.templates`](../reference/templates.md).
