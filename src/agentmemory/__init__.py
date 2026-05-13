"""agentmemory — stub package for SHA-pinning by workspace tools (uv/pip).

The functional installer lives in ``scripts/shared-repo-memory/``; pip-installing
this wheel does not deploy hooks. Run ``scripts/shared-repo-memory/install.py``
to install user assets and ``bootstrap-repo.py --repo-root <path>`` to wire a
target repo.
"""

__version__ = "0.4.4"
