# Astrid MCP server

Astrid's stdio MCP server is a supported way to use Astrid from clients such
as Claude Code or Codex. Run it from an environment where Astrid is installed:

```sh
python -m astrid.mcp_server
```

Configure your MCP client to start that command as a stdio server. It exposes
the product CLI commands generated from Astrid's argparse parsers, an `invoke`
tool for admitting capability runs, and pack skills as MCP prompts. The skills
are also available through the generated `skills_list` and `skills_show` tools.
Use `skills_show` for `astrid`, then pass a relative `file` such as
`creative-work/SKILL.md` to read a linked guide from that skill's folder.

The VibeComfy pack parser adds `comfy_scan` and `comfy_import_workflow`, which
run `python -m astrid.packs.vibecomfy.local scan` and
`python -m astrid.packs.vibecomfy.local import-workflow`. Workflow import reads
the saved name from a loopback ComfyUI with GET only, refuses redirects and
proxies, and imports the JSON into the selected project. `pack-images` stays
out of MCP because it accepts host file paths.

CLI subprocesses run in the directory where the MCP server was started, so
relative paths refer to that directory. Arguments whose CLI parser type is
`Path` take file text: the server writes that text to an absolute temporary
file and passes its path to the CLI. Other path strings keep their normal local
path meaning.

Timeline renders always run detached. After `invoke` or a render returns a
task ID, poll it with `tasks_show`; the blocking `tasks_follow` command is not
exposed over MCP.

Timeline edits skip the automatic whole-film visual sweep so the stdio server
can return promptly. To run that review separately after an edit, call the
`timelines_check` tool with the timeline `ref`, `project`, and `sweep: true`.
The CLI equivalent is:

```sh
astrid-tools timelines check TIMELINE --project PROJECT --sweep
```

The sweep compares the saved working copy with the published film; it does not
publish timeline changes.
