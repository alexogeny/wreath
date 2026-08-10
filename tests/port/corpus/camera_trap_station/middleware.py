"""Cross-cutting HTTP controls assembled as middleware."""

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://rangers.example"],
    allow_methods=["GET", "POST"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
