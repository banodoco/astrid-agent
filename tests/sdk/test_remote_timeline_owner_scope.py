from __future__ import annotations

import copy

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTimelines
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def test_open_bundle_uses_authenticated_internal_owner_pin():
    source = document_fixture(shared=False)
    canonical = copy.deepcopy(source)
    canonical["parent_revision"]["revision_id"] = "parent-current"
    canonical["timeline_id"] = "film"
    parent = {
        **canonical["parent_revision"],
        "project_id": "project-1",
        "timeline_id": "film",
        "dependency_manifest": canonical["dependency_manifest"],
    }
    shot = {**canonical["shot_revisions"][0], "project_id": "project-1"}
    internal = {**canonical["internal_timeline_revisions"][0], "project_id": "project-1"}
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Transport(RemoteTimelines):
        def open_composition(self, *_args, **_kwargs):
            return DomainResult.success({
                "summary": {
                    "revision_id": "parent-current",
                    "head_revision_id": "parent-current",
                },
                "native_inspection": {
                    "project_id": "project-1",
                    "timeline_id": "film",
                    "head_revision_id": "parent-current",
                },
            })

        def _typed(self, operation, *args, **kwargs):
            calls.append((operation, args))
            if operation == "get_project_parent_composition_revision":
                return DomainResult.success(parent)
            if operation == "get_project_shot_revision":
                return DomainResult.success(shot)
            if operation == "get_project_timeline_revision":
                assert args == ("project-1", "chapter-1", "internal-1")
                return DomainResult.success(internal)
            raise AssertionError(f"unexpected operation: {operation}")

    opened = Transport(object()).open_bundle("project-1", "film")

    assert opened.ok, opened.as_dict()
    internal_calls = [args for operation, args in calls if operation == "get_project_timeline_revision"]
    assert internal_calls == [("project-1", "chapter-1", "internal-1")]
    assert ("project-1", "film", "internal-1") not in internal_calls


@pytest.mark.parametrize("field", ["project_id", "timeline_id", "revision_id"])
def test_open_bundle_refuses_mismatched_parent_response(field):
    source = document_fixture(shared=False)
    canonical = copy.deepcopy(source)
    canonical["parent_revision"]["revision_id"] = "parent-current"
    canonical["timeline_id"] = "film"
    parent = {
        **canonical["parent_revision"],
        "project_id": "project-1",
        "timeline_id": "film",
        "dependency_manifest": canonical["dependency_manifest"],
    }
    parent[field] = "foreign" if field != "revision_id" else "other-parent"

    class Transport(RemoteTimelines):
        def open_composition(self, *_args, **_kwargs):
            return DomainResult.success({
                "summary": {"revision_id": "parent-current", "head_revision_id": "parent-current"},
                "native_inspection": {
                    "project_id": "project-1", "timeline_id": "film", "head_revision_id": "parent-current",
                },
            })

        def _typed(self, operation, *args, **kwargs):
            assert operation == "get_project_parent_composition_revision"
            return DomainResult.success(parent)

    opened = Transport(object()).open_bundle("project-1", "film")

    assert not opened.ok
    assert opened.error.code == "integrity_error"


def test_open_bundle_refuses_mismatched_internal_response():
    source = document_fixture(shared=False)
    canonical = copy.deepcopy(source)
    canonical["parent_revision"]["revision_id"] = "parent-current"
    canonical["timeline_id"] = "film"
    parent = {
        **canonical["parent_revision"],
        "project_id": "project-1",
        "timeline_id": "film",
        "dependency_manifest": canonical["dependency_manifest"],
    }
    shot = {**canonical["shot_revisions"][0], "project_id": "project-1"}
    internal = {**canonical["internal_timeline_revisions"][0], "project_id": "project-1", "timeline_id": "foreign"}

    class Transport(RemoteTimelines):
        def open_composition(self, *_args, **_kwargs):
            return DomainResult.success({
                "summary": {"revision_id": "parent-current", "head_revision_id": "parent-current"},
                "native_inspection": {
                    "project_id": "project-1", "timeline_id": "film", "head_revision_id": "parent-current",
                },
            })

        def _typed(self, operation, *args, **kwargs):
            if operation == "get_project_parent_composition_revision":
                return DomainResult.success(parent)
            if operation == "get_project_shot_revision":
                return DomainResult.success(shot)
            if operation == "get_project_timeline_revision":
                return DomainResult.success(internal)
            raise AssertionError(f"unexpected operation: {operation}")

    opened = Transport(object()).open_bundle("project-1", "film")

    assert not opened.ok
    assert opened.error.code == "integrity_error"


def test_open_bundle_refuses_mismatched_shot_response():
    source = document_fixture(shared=False)
    canonical = copy.deepcopy(source)
    canonical["parent_revision"]["revision_id"] = "parent-current"
    canonical["timeline_id"] = "film"
    parent = {
        **canonical["parent_revision"],
        "project_id": "project-1",
        "timeline_id": "film",
        "dependency_manifest": canonical["dependency_manifest"],
    }
    shot = {**canonical["shot_revisions"][0], "project_id": "foreign"}

    class Transport(RemoteTimelines):
        def open_composition(self, *_args, **_kwargs):
            return DomainResult.success({
                "summary": {"revision_id": "parent-current", "head_revision_id": "parent-current"},
                "native_inspection": {
                    "project_id": "project-1", "timeline_id": "film", "head_revision_id": "parent-current",
                },
            })

        def _typed(self, operation, *args, **kwargs):
            if operation == "get_project_parent_composition_revision":
                return DomainResult.success(parent)
            if operation == "get_project_shot_revision":
                return DomainResult.success(shot)
            raise AssertionError(f"unexpected operation: {operation}")

    opened = Transport(object()).open_bundle("project-1", "film")

    assert not opened.ok
    assert opened.error.code == "integrity_error"
