"""Installer setup choices and their precedence rules."""

from __future__ import annotations

from collections.abc import Mapping


CHOICE_NAMES = ("autostart", "command", "skills", "skill_copies")
RECOMMENDED_CHOICES = {
    "autostart": True,
    "command": True,
    "skills": True,
    "skill_copies": False,
}
# An unversioned selection record predates saved choices. Keep its historical
# effects enabled while preserving the link-first skill installation strategy.
LEGACY_CHOICES = {**RECOMMENDED_CHOICES}


def parse_choice(value: str) -> bool:
    """Parse the public on/off spelling used by installer flags."""
    if value == "on":
        return True
    if value == "off":
        return False
    raise ValueError(f"Expected 'on' or 'off', got {value!r}.")


def _validate_choices(value: Mapping[str, object] | None, *, source: str) -> dict[str, bool]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Saved {source} must be an object.")
    unknown = set(value) - set(CHOICE_NAMES)
    if unknown:
        raise ValueError(f"Unknown {source} choice(s): {', '.join(sorted(map(str, unknown)))}.")
    result: dict[str, bool] = {}
    for name, setting in value.items():
        if type(setting) is not bool:
            raise ValueError(f"{source} choice {name!r} must be true or false.")
        result[name] = setting
    return result


def resolve_choices(*, explicit: Mapping[str, object] | None = None,
                    saved: Mapping[str, object] | None = None,
                    prompt: Mapping[str, object] | None = None,
                    accept_recommended: bool = False,
                    existing_install: bool = False) -> dict[str, bool]:
    """Resolve explicit > saved > prompt/yes > recommended choices.

    Existing installs that have no choice record retain enabled historical
    effects. A saved record may be partial; only its recorded values override
    lower-precedence sources.
    """
    explicit_values = _validate_choices(explicit, source="explicit")
    saved_values = _validate_choices(saved, source="saved")
    prompt_values = _validate_choices(prompt, source="prompt")
    baseline = LEGACY_CHOICES if existing_install and not saved_values else RECOMMENDED_CHOICES
    accepted = RECOMMENDED_CHOICES if accept_recommended else {}
    return {
        name: explicit_values.get(name, saved_values.get(name,
                prompt_values.get(name, accepted.get(name, baseline[name]))))
        for name in CHOICE_NAMES
    }


def unattended_consent_required(*, existing_install: bool, accept_recommended: bool,
                                interactive: bool) -> bool:
    """Whether a fresh noninteractive install must stop for consent."""
    return not existing_install and not accept_recommended and not interactive


def render_plan(choices: Mapping[str, bool]) -> str:
    values = _validate_choices(choices, source="resolved")
    return "Astrid install plan (rerun with --yes to accept):\n" + "\n".join(
        f"  {name.replace('_', ' ')}: {'on' if values[name] else 'off'}"
        for name in CHOICE_NAMES
    )
