"""Exact privacy reproducers from the G3 adversarial review."""
import json

import pytest

from astrid.packs.runpod import failure_bundle as fb


def twice(text, **context):
    return fb.Redactor(**context).text(fb.Redactor(**context).text(text))


@pytest.mark.parametrize("prompts,text,absent", [
    (["rose\nblue"], "rose\nblue", ("rose", "blue")),
    (["a violet sunset"], "violet", ("violet",)),
    (["abcdefgh", "efghijkl"], "abcdefghijkl", ("abcd", "ijkl")),
])
def test_finding_1_prompt_fragments_and_overlaps(prompts, text, absent):
    result = twice(text, prompts=prompts)
    assert all(value not in result for value in absent), result


@pytest.mark.parametrize("prompt,text", [("rose", "cm9zZQ=="), ("rose", r"\u0072\u006f\u0073\u0065"),
                                        ("café", "cafe\u0301")])
def test_finding_2_encoded_and_unicode_prompts(prompt, text):
    result = twice(text, prompts=[prompt])
    assert result != text and "rose" not in result and "café" not in result, result


@pytest.mark.parametrize("kind", ["short-secret", "json-credentials"])
def test_finding_4_short_secrets_and_credential_json(kind):
    if kind == "short-secret":
        assert "tulip" not in twice("tulip", secrets=["tulip"])
    else:
        value = {"api_key": "abCD12", "password": "violet meadow password"}
        for _ in range(2):
            value = fb.Redactor().value(value)
        result = json.dumps(value)
        assert "abCD12" not in result and "violet meadow password" not in result


@pytest.mark.parametrize("kind", ["api-prompt", "api-path", "litegraph"])
def test_finding_6_opaque_workflow_nodes_and_edges(kind):
    if kind == "api-prompt":
        graph = {"rose": {"class_type": "/home/alice/Custom", "inputs": {"clip": ["rose", 0]}},
                 "/home/alice/private-note": {"class_type": "ValidClass", "inputs": {}}}
        shaped, _ = fb.structure(graph, fb.Redactor(prompts=["rose"]))
        result = json.dumps(shaped)
        assert "rose" not in result and "/home/alice" not in result
        nodes = shaped["nodes"]
        first = next(iter(nodes))
        assert nodes[first]["inputs"]["clip"] == [first, 0]
    elif kind == "api-path":
        shaped, _ = fb.structure({"/home/alice/private-note": {"class_type": "/home/alice/Custom",
                                                              "inputs": {}}}, fb.Redactor())
        assert "/home/alice" not in json.dumps(shaped)
    else:
        lite, _ = fb.structure({"nodes": [{"id": "rose", "type": "/home/alice/Custom"}],
                                "links": [[1, "rose", 0, "rose", 0, "/home/alice/private-note"]]}, fb.Redactor())
        assert "rose" not in json.dumps(lite) and "/home/alice" not in json.dumps(lite)
        assert lite["links"][0][1] == lite["nodes"][0]["id"] == lite["links"][0][3]


@pytest.mark.parametrize("text", ["[::]:8080", r"https:\/\/alice:violet@host.internal\/private", "[::ffff:192.0.2.1]:8080"])
def test_finding_7_escaped_urls_and_unspecified_ipv6(text):
    result = twice(text)
    assert "::" not in result and "alice" not in result and "host.internal" not in result, result


def test_prompt_match_count_drops_entire_line_even_when_fragments_are_separated():
    assert fb.Redactor(prompts=["rose"]).line("rose " * 8) is None


def test_unicode_lowercase_expansion_preserves_original_match_offsets():
    result = twice("İ" * 4 + "rose", prompts=["rose"])
    assert "rose" not in result


def test_encoded_prompt_inside_url_cannot_split_url_removal():
    result = twice("https://host.internal/cm9zZQ==/private", prompts=["rose"])
    assert result == "<url>"


def test_secret_inside_url_cannot_split_url_removal():
    result = twice("https://alice:tulip@host.internal/private", secrets=["tulip"])
    assert result == "<url>"


def test_huge_lines_are_withheld_before_regex_and_secret_near_matches_observe_abort():
    calls = []
    armed = False

    def check():
        calls.append(None)
        if armed and len(calls) >= 12:
            raise TimeoutError("redaction deadline")

    redactor = fb.Redactor(secrets=["a." * 3999 + "ab"], check=check)
    calls.clear()
    assert redactor.text("." * 1000000) == "<line withheld: processing cap>"
    assert len(calls) == 1
    calls.clear()
    armed = True
    with pytest.raises(TimeoutError, match="redaction deadline"):
        redactor.text("a." * 4000)
    assert len(calls) == 12


def test_controller_redaction_preserves_opaque_edge_identity_with_short_secrets():
    graph = {"a": {"class_type": "Encode", "inputs": {"clip": ["b", 0]}},
             "b": {"class_type": "Decode", "inputs": {}}}
    shaped, _ = fb.structure(graph, fb.Redactor(secrets=["1", "2"]))
    result = json.loads(fb.redact_file("workflow.redacted.json", fb.dump(shaped).encode(),
                                     fb.Redactor(secrets=["1", "2"])))
    first, second = list(result["nodes"])
    assert result["nodes"][first]["inputs"]["clip"] == [second, 0]


@pytest.mark.parametrize('scheme', ['x', 'custom' + 'a' * 64])
def test_urls_with_any_valid_scheme_length_are_removed(scheme):
    result = twice(scheme + '://host.internal/private')
    assert result == '<url>'
