"""Application assembly: settings, middleware, lifespan, and the routers."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic_settings import BaseSettings

from .api import router as llama_router
from .graph import graph_router


class Settings(BaseSettings):
    database_url: str
    weather_base_url: str = "https://weather.invalid"
    request_timeout_s: float = 10.0
    log_level: str = "INFO"


settings = Settings()


@asynccontextmanager
async def lifespan(application: FastAPI):
    await connect_database(settings.database_url)
    yield
    await disconnect_database()


app = FastAPI(title="Highland Herd", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://herd.invalid"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(llama_router)
app.include_router(graph_router, prefix="/graphql")
