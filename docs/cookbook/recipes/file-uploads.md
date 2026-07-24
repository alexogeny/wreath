# Handle file uploads

Accepting a file is just another form of binding. Declare a `File` (or `Form`)
parameter and Wreath parses the multipart body for you, within the size limits
you've configured — so you work with an uploaded file, not a stream you have to
babysit:

```python
from typing import Annotated

from wreath.binding import File
from wreath.request import UploadedFile

@app.post("/upload")
async def upload(request, document: Annotated[UploadedFile, File()]) -> dict:
    return {"name": document.filename, "size": len(document.data)}
```

The uploaded file arrives fully parsed: its bytes are `document.data`, and
`filename`, `content_type`, and the part's headers ride along with it. The body
and multipart limits live on `RequestLimits`, set with `Wreath(limits=...)`,
and anything over them is rejected before your handler runs — so a hostile
10 GB "upload" never gets the chance to fill your disk or your memory.
