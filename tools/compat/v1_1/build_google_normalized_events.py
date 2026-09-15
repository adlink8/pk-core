"""Shim: pipeline.build_google_normalized_events"""
from personal_knowledge.application.build_google_normalized_events import *  # noqa: F403
from personal_knowledge.application.build_google_normalized_events import main

if __name__ == "__main__":
    raise SystemExit(main())
