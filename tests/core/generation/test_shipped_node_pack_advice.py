"""WS-0c: attached advice names the exact shipped lock-only pack and its commit."""
from astrid.core.generation.asset_gaps import attached_asset_readiness


def test_attached_lock_only_pack_is_pinned_outside_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cwd_lock = tmp_path / "custom_nodes.lock"
    cwd_lock.write_text(
        '[nodepacks.Unrelated]\nsource = "local"\npath = "."\nclass_set = ["HunyuanFoleySampler"]\n',
        encoding="utf-8",
    )
    before = cwd_lock.read_bytes()
    repo = "https://github.com/BobRandomNumber/ComfyUI-HunyuanVideo_Foley.git"
    commit = "b94f7aca877ff2be5bc264b057abee3dac682c1b"
    folder = "/srv/ComfyUI/custom_nodes/ComfyUI-HunyuanVideo_Foley"
    result = attached_asset_readiness(
        "foley", nodes=["HunyuanFoleySampler"],
        custom_nodes="/srv/ComfyUI/custom_nodes",
    )
    assert result["status"] == "blocked"
    details = result["issues"][0]["details"]
    assert details["pack"] == "ComfyUI-HunyuanVideo_Foley"
    assert details["repository"] == repo
    assert details["commands"] == [
        ["git", "clone", repo, folder],
        ["git", "-C", folder, "checkout", commit],
    ]
    assert result["fixes"][0]["details"]["commands"] == details["commands"]
    assert cwd_lock.read_bytes() == before
