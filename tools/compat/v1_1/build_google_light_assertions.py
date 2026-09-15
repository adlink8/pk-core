"""Shim: pipeline.build_google_light_assertions"""
from personal_knowledge.application.build_google_light_assertions import *  # noqa: F403
from personal_knowledge.application.build_google_light_assertions import main

if __name__ == "__main__":
    raise SystemExit(main())
