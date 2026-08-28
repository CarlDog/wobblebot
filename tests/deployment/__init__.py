"""Tests that assert properties of the deployment artifacts themselves.

Distinct from ``tests/config/`` (which validates the Pydantic config
schemas an application *loads*): these read ``docker/docker-compose.yml``
and assert what each container is *granted* before any Python runs.
"""
