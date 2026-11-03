#!/usr/bin/env python3
"""Search Hugging Face Hub for LoRA adapters by base model."""


from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint
from astrid.pack_api import CredentialsScope
from astrid.core.util.secrets import load_local_api_key_with_source

guard_canonical_entrypoint('training.search_loras')
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from astrid.pack_api import add_choice_arg

HUGGING_FACE_MODELS_API = "https://huggingface.co/api/models"
DEFAULT_DISCOVERY_LIMIT = 1000
PHOTOREAL_TERMS = ("photo", "photography", "photoreal", "photorealistic", "realism", "realistic", "35mm")


def _token_from_env() -> str | None:
    try:
        return CredentialsScope.get_local("huggingface")
    except AstridError:
        try:
            return load_local_api_key_with_source("HUGGING_FACE_HUB_TOKEN")[0]
        except AstridError:
            return None


def _normalize_terms(values: list[str] | tuple[str, ...] | None) -> list[str]:
    terms: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            term = part.strip()
            if term:
                terms.append(term)
    return terms


def _search_command(
    *,
    base_model: str,
    terms: list[str],
    match_mode: str,
    limit: int,
    fetch_limit: int,
) -> str:
    return " ".join(
        [
            f"--base-model {base_model}",
            *[f"--match {term}" for term in terms],
            f"--match-mode {match_mode}",
            f"--limit {limit}",
            f"--fetch-limit {fetch_limit}",
        ]
    )


def _executor_search_command(
    *,
    base_model: str,
    terms: list[str],
    match_mode: str,
    limit: int,
    fetch_limit: int,
    out: str = "/tmp/astrid-search-loras",
) -> str:
    return " ".join(
        [
            "python3 -m astrid.packs.training.actions.search_loras.run",
            f"--base-model {base_model}",
            *[f"--match {term}" for term in terms],
            f"--match-mode {match_mode}",
            f"--limit {limit}",
            f"--fetch-limit {fetch_limit}",
            f"--out {out}",
        ]
    )


def _executor_base_models_command(
    *,
    base_model_match: str,
    fetch_limit: int,
    out: str = "/tmp/astrid-search-loras-base-models",
) -> str:
    return " ".join(
        [
            "python3 -m astrid.packs.training.actions.search_loras.run",
            "--list-base-models",
            f"--base-model-match {base_model_match}",
            f"--fetch-limit {fetch_limit}",
            f"--out {out}",
        ]
    )


def _build_models_url(
    *,
    base_model: str | None,
    query: str | None,
    limit: int,
    sort: str,
    direction: str,
) -> str:
    params: list[tuple[str, str]] = [
        ("filter", "lora"),
        ("sort", sort),
        ("direction", direction),
        ("limit", str(limit)),
        ("full", "true"),
        ("config", "true"),
    ]
    if base_model:
        params.append(("filter", f"base_model:{base_model}"))
    if query:
        params.append(("search", query))
    return f"{HUGGING_FACE_MODELS_API}?{urllib.parse.urlencode(params)}"


def _base_model_tags(tags: list[str]) -> list[str]:
    prefix = "base_model:"
    return [tag for tag in tags if tag.startswith(prefix)]


def _license_tags(tags: list[str]) -> list[str]:
    return [tag for tag in tags if tag.startswith("license:")]


def _safetensors_files(raw: dict[str, Any]) -> list[str]:
    siblings = raw.get("siblings")
    if not isinstance(siblings, list):
        return []
    files: list[str] = []
    for item in siblings:
        if not isinstance(item, dict):
            continue
        filename = item.get("rfilename")
        if isinstance(filename, str) and filename.endswith(".safetensors"):
            files.append(filename)
    return files


def _match_fields(result: dict[str, Any], term: str) -> list[str]:
    needle = term.lower()
    fields = {
        "id": [result.get("id")],
        "author": [result.get("author")],
        "pipeline_tag": [result.get("pipeline_tag")],
        "library_name": [result.get("library_name")],
        "base_model_tags": result.get("base_model_tags", []),
        "license_tags": result.get("license_tags", []),
        "safetensors_files": result.get("safetensors_files", []),
        "tags": result.get("tags", []),
    }
    matched: list[str] = []
    for field, values in fields.items():
        if any(needle in str(value).lower() for value in values if value):
            matched.append(field)
    return matched


def _match_details(result: dict[str, Any], terms: list[str], mode: str) -> dict[str, Any]:
    if not terms:
        return {"matched": True, "terms": [], "score": 0}
    terms_by_field = {term: _match_fields(result, term) for term in terms}
    matched_terms = [term for term, fields in terms_by_field.items() if fields]
    if mode == "all":
        matched = len(matched_terms) == len(terms)
    elif mode == "any":
        matched = bool(matched_terms)
    else:
        raise AstridError(
            "match_mode must be all or any",
            valid_options=["all", "any"],
            recovery_command="set --match-mode to 'all' or 'any'",
        )
    score = sum(len(fields) for fields in terms_by_field.values())
    return {
        "matched": matched,
        "terms": matched_terms,
        "fields": {term: fields for term, fields in terms_by_field.items() if fields},
        "score": score,
    }


def _filter_matches(results: list[dict[str, Any]], terms: list[str], mode: str) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for result in results:
        details = _match_details(result, terms, mode)
        if details["matched"]:
            item = dict(result)
            if terms:
                item["match"] = {
                    "terms": details["terms"],
                    "fields": details["fields"],
                    "score": details["score"],
                }
            matched.append(item)
    if terms:
        matched.sort(key=lambda item: (-item.get("match", {}).get("score", 0), -(item.get("downloads") or 0), item.get("id") or ""))
    return matched


def _guidance_for_lora_search(
    *,
    base_model: str,
    query: str | None,
    match: list[str],
    match_mode: str,
    fallback_used: bool,
    limit: int,
    fetch_limit: int,
    candidate_count: int,
    matched_count: int,
    count: int,
) -> dict[str, Any]:
    messages: list[str] = []
    next_commands: list[str] = []
    next_executor_commands: list[str] = []
    suggested_terms: list[str] = []

    if fallback_used:
        messages.append(
            "Hugging Face text search returned no results; retried broad base-model search and applied the query locally."
        )
    if count == 0:
        messages.append("No LoRA repositories matched the current filters.")
        if "/" not in base_model:
            messages.append(
                "The supplied base model does not look like a full Hugging Face repo id (`owner/name`); run base-model discovery to find the canonical id."
            )
        elif candidate_count == 0:
            messages.append(
                "The base-model filter returned no candidate LoRA repositories; verify the exact Hugging Face base model id with base-model discovery."
            )
        next_commands.append(
            f"--base-model {base_model} --limit {max(limit, 25)} --fetch-limit {max(fetch_limit * 2, 200)}"
        )
        if query and not match:
            next_commands.append(
                f"--base-model {base_model} --match {query} --fetch-limit {max(fetch_limit * 2, 200)}"
            )
        next_commands.append(f"--list-base-models --base-model-match {base_model.split('/')[-1]} --fetch-limit 1000")
        next_executor_commands.append(
            _executor_base_models_command(base_model_match=base_model.split("/")[-1], fetch_limit=1000)
        )
    elif matched_count > count:
        messages.append(
            f"Returned top {count} of {matched_count} local matches; raise --limit to inspect more."
        )
    if match and match_mode == "all" and len(match) > 1 and matched_count == 0:
        messages.append("Multiple --match terms are ANDed by default; use --match-mode any for synonym searches.")
        next_commands.append(
            " ".join(
                [f"--base-model {base_model}", *[f"--match {term}" for term in match], "--match-mode any", f"--fetch-limit {fetch_limit}"]
            )
        )
        next_executor_commands.append(
            _executor_search_command(
                base_model=base_model,
                terms=match,
                match_mode="any",
                limit=max(limit, 25),
                    fetch_limit=fetch_limit,
                    out="/tmp/astrid-search-loras",
                )
            )
    lowered = " ".join([query or "", *match]).lower()
    if any(term in lowered for term in ("photo", "real", "35mm")):
        suggested_terms = [term for term in PHOTOREAL_TERMS if term not in {item.lower() for item in match}]
        if match_mode != "any" and len(match) > 1:
            messages.append("Photoreal intent is usually a synonym search; --match-mode any is recommended.")
        if suggested_terms:
            terms = list(dict.fromkeys([*match, *suggested_terms[:4]]))
            next_commands.append(
                _search_command(
                    base_model=base_model,
                    terms=terms,
                    match_mode="any",
                    limit=max(limit, 25),
                    fetch_limit=max(fetch_limit, 200),
                )
            )
            next_executor_commands.append(
                _executor_search_command(
                    base_model=base_model,
                    terms=terms,
                    match_mode="any",
                    limit=max(limit, 25),
                    fetch_limit=max(fetch_limit, 200),
                )
            )

    return {
        "status": "empty" if count == 0 else "ok",
        "messages": messages,
        "suggested_match_terms": suggested_terms[:8],
        "next_commands": next_commands,
        "next_executor_commands": next_executor_commands,
    }


def _guidance_for_base_model_discovery(
    *,
    match: list[str],
    base_model_match: list[str],
    base_models: list[dict[str, Any]],
    candidate_count: int,
    matched_count: int,
    count: int,
    limit: int,
) -> dict[str, Any]:
    messages: list[str] = []
    next_commands: list[str] = []
    next_executor_commands: list[str] = []
    if count == 0:
        messages.append("No base_model tags matched the current discovery filters.")
        next_commands.append(f"--list-base-models --fetch-limit {max(limit * 2, 1000)}")
        next_executor_commands.append(
            " ".join(
                [
                    "python3 -m astrid.packs.training.actions.search_loras.run",
                    "--list-base-models",
                    f"--fetch-limit {max(limit * 2, 1000)}",
                    "--out /tmp/astrid-search-loras-base-models",
                ]
            )
        )
    if match and not base_model_match:
        messages.append(
            "--match filters LoRA repo text; use --base-model-match to filter extracted base model ids directly."
        )
    if candidate_count >= limit:
        messages.append("Discovery scanned the requested limit; raise --fetch-limit for wider coverage.")
        next_commands.append(f"--list-base-models --fetch-limit {limit * 2}")
        if base_model_match:
            next_executor_commands.append(
                _executor_base_models_command(
                    base_model_match=",".join(base_model_match),
                    fetch_limit=limit * 2,
                    out="/tmp/astrid-search-loras-base-models",
                )
            )
    for model in base_models[:5]:
        model_id = model.get("id")
        if isinstance(model_id, str):
            next_commands.append(
                _search_command(
                    base_model=model_id,
                    terms=["photo", "realism", "35mm"],
                    match_mode="any",
                    limit=25,
                    fetch_limit=200,
                )
            )
            next_executor_commands.append(
                _executor_search_command(
                    base_model=model_id,
                    terms=["photo", "realism", "35mm"],
                    match_mode="any",
                    limit=25,
                    fetch_limit=200,
                )
            )
    return {
        "status": "empty" if count == 0 else "ok",
        "messages": messages,
        "next_commands": next_commands,
        "next_executor_commands": next_executor_commands,
    }


def _fetch_models(
    *,
    base_model: str | None,
    query: str | None,
    limit: int,
    sort: str,
    direction: str,
    token: str | None,
    timeout: float,
) -> list[dict[str, Any]]:
    url = _build_models_url(
        base_model=base_model,
        query=query,
        limit=limit,
        sort=sort,
        direction=direction,
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "astrid-search-loras/1.0",
    }
    auth_token = token or _token_from_env()
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AstridError(
            f"Hugging Face model search failed: HTTP {exc.code}: {detail}",
            recovery_command="verify the Hugging Face API is reachable and the base model id is correct, then retry",
        ) from exc
    except urllib.error.URLError as exc:
        raise AstridError(
            f"Hugging Face model search failed: {exc.reason}",
            recovery_command="check network connectivity and Hugging Face API availability, then retry",
        ) from exc

    if not isinstance(raw_data, list):
        raise AstridError(
            "Hugging Face model search returned an unexpected payload",
            recovery_command="verify the Hugging Face API is functioning correctly and retry",
        )
    return [item for item in raw_data if isinstance(item, dict)]


def _normalize_model(raw: dict[str, Any]) -> dict[str, Any]:
    repo_id = raw.get("id") or raw.get("modelId")
    tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
    string_tags = [tag for tag in tags if isinstance(tag, str)]
    return {
        "id": repo_id,
        "url": f"https://huggingface.co/{repo_id}" if isinstance(repo_id, str) else None,
        "author": raw.get("author"),
        "downloads": raw.get("downloads"),
        "likes": raw.get("likes"),
        "gated": raw.get("gated"),
        "private": raw.get("private"),
        "pipeline_tag": raw.get("pipeline_tag"),
        "library_name": raw.get("library_name"),
        "created_at": raw.get("createdAt"),
        "last_modified": raw.get("lastModified"),
        "sha": raw.get("sha"),
        "base_model_tags": _base_model_tags(string_tags),
        "license_tags": _license_tags(string_tags),
        "safetensors_files": _safetensors_files(raw),
        "tags": string_tags,
    }


def search_loras(
    *,
    base_model: str,
    query: str | None = None,
    match: list[str] | None = None,
    match_mode: str = "all",
    limit: int = 25,
    fetch_limit: int | None = None,
    sort: str = "downloads",
    direction: str = "-1",
    token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if limit < 1:
        raise AstridError(
            "limit must be at least 1",
            recovery_command="set --limit to a value >= 1",
        )
    if direction not in {"-1", "1"}:
        raise AstridError(
            "direction must be -1 or 1",
            valid_options=["-1", "1"],
            recovery_command="set --direction to -1 or 1",
        )

    if match_mode not in {"all", "any"}:
        raise AstridError(
            "match_mode must be all or any",
            valid_options=["all", "any"],
            recovery_command="set --match-mode to 'all' or 'any'",
        )
    local_terms = _normalize_terms(match)
    effective_fetch_limit = fetch_limit if fetch_limit is not None else (max(limit, 100) if local_terms else limit)
    if effective_fetch_limit < limit:
        raise AstridError(
            "fetch_limit must be greater than or equal to limit",
            recovery_command="set --fetch-limit to a value >= --limit and retry",
        )

    raw_data = _fetch_models(
        base_model=base_model,
        query=query,
        limit=effective_fetch_limit,
        sort=sort,
        direction=direction,
        token=token,
        timeout=timeout,
    )

    candidates = [_normalize_model(item) for item in raw_data]
    fallback_used = False
    effective_terms = local_terms
    if not effective_terms and query and not candidates:
        # Hugging Face API text search can miss tag and filename matches.
        # If it returns nothing, retry broadly and treat --query as a local
        # intent filter so the default path remains useful.
        fallback_used = True
        effective_terms = [query]
        effective_fetch_limit = max(effective_fetch_limit, 100)
        raw_data = _fetch_models(
            base_model=base_model,
            query=None,
            limit=effective_fetch_limit,
            sort=sort,
            direction=direction,
            token=token,
            timeout=timeout,
        )
        candidates = [_normalize_model(item) for item in raw_data]
    matched = _filter_matches(candidates, effective_terms, match_mode)
    results = matched[:limit]
    return {
        "base_model": base_model,
        "query": query,
        "match": effective_terms,
        "match_mode": match_mode,
        "fallback_used": fallback_used,
        "limit": limit,
        "fetch_limit": effective_fetch_limit,
        "sort": sort,
        "direction": direction,
        "candidate_count": len(candidates),
        "matched_count": len(matched),
        "count": len(results),
        "guidance": _guidance_for_lora_search(
            base_model=base_model,
            query=query,
            match=effective_terms,
            match_mode=match_mode,
            fallback_used=fallback_used,
            limit=limit,
            fetch_limit=effective_fetch_limit,
            candidate_count=len(candidates),
            matched_count=len(matched),
            count=len(results),
        ),
        "results": results,
    }


def discover_base_models(
    *,
    query: str | None = None,
    match: list[str] | None = None,
    match_mode: str = "all",
    base_model_match: list[str] | None = None,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    sort: str = "downloads",
    direction: str = "-1",
    token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if limit < 1:
        raise AstridError(
            "limit must be at least 1",
            recovery_command="set --limit to a value >= 1",
        )
    if direction not in {"-1", "1"}:
        raise AstridError(
            "direction must be -1 or 1",
            valid_options=["-1", "1"],
            recovery_command="set --direction to -1 or 1",
        )

    if match_mode not in {"all", "any"}:
        raise AstridError(
            "match_mode must be all or any",
            valid_options=["all", "any"],
            recovery_command="set --match-mode to 'all' or 'any'",
        )
    local_terms = _normalize_terms(match)
    raw_data = _fetch_models(
        base_model=None,
        query=query,
        limit=limit,
        sort=sort,
        direction=direction,
        token=token,
        timeout=timeout,
    )
    candidates = [_normalize_model(item) for item in raw_data]
    matched = _filter_matches(candidates, local_terms, match_mode)
    model_terms = [term.lower() for term in _normalize_terms(base_model_match)]
    counts: dict[str, int] = {}
    for result in matched:
        for tag in result["base_model_tags"]:
            if tag.startswith("base_model:adapter:"):
                continue
            if not tag.startswith("base_model:"):
                continue
            model_id = tag.removeprefix("base_model:")
            if model_terms and not all(term in model_id.lower() for term in model_terms):
                continue
            counts[model_id] = counts.get(model_id, 0) + 1

    base_models = [
        {"id": model_id, "count": count, "url": f"https://huggingface.co/{model_id}"}
        for model_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    ]
    return {
        "query": query,
        "match": local_terms,
        "match_mode": match_mode,
        "base_model_match": base_model_match or [],
        "limit": limit,
        "sort": sort,
        "direction": direction,
        "candidate_count": len(candidates),
        "matched_count": len(matched),
        "count": len(base_models),
        "guidance": _guidance_for_base_model_discovery(
            match=local_terms,
            base_model_match=base_model_match or [],
            base_models=base_models,
            candidate_count=len(candidates),
            matched_count=len(matched),
            count=len(base_models),
            limit=limit,
        ),
        "base_models": base_models,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search Hugging Face Hub for LoRA adapters by base model.")
    add_choice_arg(parser, "--mode", values=("search", "base-models"), default="search", help="Run a LoRA search or discover base-model names. Default search.")
    parser.add_argument("--base-model", help="Base model repo id, for example stabilityai/stable-diffusion-xl-base-1.0.")
    parser.add_argument("--query", help="Optional Hugging Face API text search.")
    parser.add_argument("--match", action="append", default=[], help="Local substring filter across repo id, tags, and safetensors filenames. May be repeated.")
    add_choice_arg(parser, "--match-mode", values=("all", "any"), default="all", help="Whether all --match terms or any --match term must match. Default all.")
    parser.add_argument("--base-model-match", action="append", default=[], help="With --list-base-models, filter extracted base model ids by substring. May be repeated.")
    parser.add_argument("--limit", type=int, help="Maximum result count. Default 25 for search, 1000 for --list-base-models.")
    parser.add_argument("--fetch-limit", type=int, help="How many API results to fetch before applying --match. Defaults to max(limit, 100) when matching.")
    parser.add_argument("--sort", default="downloads", help="Hub sort field. Default downloads.")
    add_choice_arg(parser, "--direction", values=("-1", "1"), default="-1", help="Sort direction: -1 desc, 1 asc.")
    parser.add_argument("--list-base-models", action="store_true", help="Scan LoRA repos and list discovered base_model tags instead of searching one base model.")
    parser.add_argument("--token", help="Optional Hugging Face token. Prefer HF_TOKEN or HUGGING_FACE_HUB_TOKEN.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds.")
    parser.add_argument("--out", type=Path, help="Write JSON response to this file.")
    parser.add_argument("--compact", action="store_true", help="Print compact JSON instead of pretty JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    list_base_models = bool(args.list_base_models or args.mode == "base-models")
    fetch_limit = args.fetch_limit if args.fetch_limit and args.fetch_limit > 0 else None
    limit = args.limit if args.limit and args.limit > 0 else None
    if list_base_models:
        payload = discover_base_models(
            query=args.query,
            match=args.match,
            match_mode=args.match_mode,
            base_model_match=args.base_model_match,
            limit=fetch_limit or limit or DEFAULT_DISCOVERY_LIMIT,
            sort=args.sort,
            direction=args.direction,
            token=args.token,
            timeout=args.timeout,
        )
    else:
        if not args.base_model:
            raise AstridError(
                "--base-model is required unless --list-base-models is set",
                recovery_command="provide --base-model <repo_id> or use --list-base-models",
            )
        payload = search_loras(
            base_model=args.base_model,
            query=args.query,
            match=args.match,
            match_mode=args.match_mode,
            limit=limit or 25,
            fetch_limit=fetch_limit,
            sort=args.sort,
            direction=args.direction,
            token=args.token,
            timeout=args.timeout,
        )

    indent = None if args.compact else 2
    text = json.dumps(payload, indent=indent, sort_keys=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
