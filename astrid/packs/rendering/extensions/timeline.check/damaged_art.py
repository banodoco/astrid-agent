"""DAMAGED-ART detector for selected sprite assets."""


def run(ctx):
    thresholds = ctx.thresholds
    no_outline_style_below = float(thresholds["no_outline_style_below"])
    damaged_art_below = float(thresholds["damaged_art_below"])
    # The SDK selects these rows from the admitted timeline document and only
    # measures assets already selected by its sprite clips.
    _document = ctx.document
    findings = []
    for selected in ctx.selected_assets():
        integrity = selected.get("outline_integrity")
        try:
            score = float(integrity)
        except (TypeError, ValueError):
            # Missing measurements remain unknown; they are never reported clean.
            continue
        if no_outline_style_below <= score < damaged_art_below:
            findings.append(
                f"{selected['address']}: {selected['asset']} outline integrity {score:.2f} "
                f"(threshold {damaged_art_below:.2f})"
            )
    return findings
