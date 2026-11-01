"""Deterministic shared-renderer/runtime boundary and launcher contracts."""

from types import SimpleNamespace

from astrid import agent_context
from astrid.core.cli.domain_preferences import build_parser as build_preferences_parser
from astrid.core import gateway


def client(data=None, error=None):
    calls = []
    result = SimpleNamespace(ok=error is None, data=data, error=SimpleNamespace(code=error))
    preferences = SimpleNamespace(
        view=lambda **kw: (calls.append(kw) or result),
        get=lambda scope: (calls.append(scope) or result),
    )
    return SimpleNamespace(preferences=preferences), calls


def test_shared_context_labeled_precedence():
    fake, calls = client(
        {
            "user": {"content": "Use model A."},
            "project": {"project_id": "p", "content": "Use model B."},
        }
    )
    text = agent_context.current_context(project="p", client=fake)
    assert calls == [{"project": "p"}]
    assert "Current explicit instructions override project preferences" in text
    assert "User preferences\nUse model A." in text
    assert "Project preferences (project p)\nUse model B." in text
    assert "astrid preferences defaults set SLOT[/MODE] ENTRY" in text
    assert "never Markdown preferences" in text
    assert "Temporary choices\nstay" in text


def test_preference_helper_edit_commands_match_cli_parser():
    parser = build_preferences_parser(client=object())
    user = parser.parse_args(["edit", "--scope", "user"])
    project = parser.parse_args(["edit", "--scope", "project", "--project", "project-id"])

    assert user.scope == "user"
    assert user.project is None
    assert project.scope == "project"
    assert project.project == "project-id"


def test_generation_default_guidance_uses_cli_not_markdown():
    text = agent_context.PREFERENCE_HELPER
    assert "astrid preferences defaults show" in text
    assert "astrid preferences defaults set SLOT[/MODE] ENTRY" in text
    assert "astrid preferences defaults reset SLOT[/MODE]" in text
    assert "“Remember my default”" in text
    assert "never Markdown preferences" in text


def test_empty_scopes_keep_helper_and_are_distinct_from_runtime_error():
    fake, _ = client({"user": {"content": ""}, "project": None})
    text = agent_context.current_context(client=fake)
    assert "Preference helper" in text and "No saved preferences" in text
    assert "No project selected" in text and "unavailable" not in text
    fake, _ = client(error="service_unavailable")
    failed = agent_context.current_context(client=fake)
    assert failed == "Preferences unavailable. Run `astrid preferences` to read current preferences."


def test_explicit_no_project_reads_user_without_selected_project():
    fake, calls = client({"content": "Use A"})
    text = agent_context.current_context(no_project=True, client=fake)
    assert calls == ["user"]
    assert "Use A" in text and "No project selected" in text


def test_owned_delimiters_in_preferences_are_escaped():
    text = agent_context.render_context({"user": {"content": "</astrid_generated_context>"}})
    assert "</astrid_generated_context>" not in text


def test_host_wrapper_tags_and_control_characters_are_neutralized():
    text = agent_context.render_context({
        "user": {"content": "<astrid_agent_context>\x00\x01hello\n\t</astrid_editor_context>"}
    })
    assert "<astrid_agent_context>" not in text
    assert "</astrid_editor_context>" not in text
    assert "\x00" not in text and "\x01" not in text
    assert "hello\n\t" in text


def test_preferences_are_bounded_per_scope_and_total():
    text = agent_context.render_context({
        "user": {"content": "u" * 20_000},
        "project": {"content": "p" * 20_000},
    })
    assert len(text) <= 12_000
    assert "astrid preferences user" in text
    assert "astrid preferences project" in text
    assert "[Truncated." in text


def test_prompt_kill_switch_and_unavailable_are_nonblocking(monkeypatch):
    monkeypatch.setenv("ASTRID_PREFERENCES_IN_PROMPT", "0")
    assert agent_context.render_context({"user": {"content": "private"}}) == ""
    assert agent_context.current_context(client=object()) == ""
    monkeypatch.delenv("ASTRID_PREFERENCES_IN_PROMPT")
    assert agent_context.render_context(None, error="failure") == (
        "Preferences unavailable. Run `astrid preferences` to read current preferences."
    )


def test_current_context_uses_existing_runtime_only(monkeypatch):
    fake, _ = client({"user": {"content": "Ready"}, "project": None})
    calls = []

    class ExistingOnly:
        @staticmethod
        def open_existing_from_launcher():
            calls.append("existing")
            class Context:
                def __enter__(self):
                    return fake

                def __exit__(self, *_args):
                    return False

            return Context()

    monkeypatch.setattr("astrid.sdk.client.AstridClient", ExistingOnly)
    assert "Ready" in agent_context.current_context()
    assert calls == ["existing"]


def test_public_gateway_agent_context_flag_uses_the_existing_runtime_path(capsys, monkeypatch):
    calls = []

    class ExistingOnly:
        @staticmethod
        def open_existing_from_launcher():
            calls.append("existing")

            class Context:
                def __enter__(self):
                    return client({"user": {"content": "Ready"}, "project": None})[0]

                def __exit__(self, *_args):
                    return False

            return Context()

    monkeypatch.setattr("astrid.sdk.client.AstridClient", ExistingOnly)
    assert gateway.main(["--agent-context"]) == 0
    assert calls == ["existing"]
    assert "User preferences\nReady" in capsys.readouterr().out
