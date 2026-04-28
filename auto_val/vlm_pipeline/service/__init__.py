"""HTTP service layer — wraps the core library as a FastAPI app.

Run locally:  python -m vlm_pipeline.service
"""
from .app import create_app

__all__ = ["create_app"]
