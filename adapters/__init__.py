"""Service adapters: fake (no-dependency) and real LLM/transport implementations.

Selected at runtime by env var via factory.py. Adapters may contain generic
English; only engine/ must stay free of domain literals.
"""
