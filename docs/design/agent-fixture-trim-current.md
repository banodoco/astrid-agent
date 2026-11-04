# Current M2 fixture trim and automatic placement reads

This unit composes approved `db98206f` (automatic placement field reads) and
`540d37b0` (current fixture trim) on Agent integration `df2739f9`.
The intended expanded data comes from `db98206f` for
`recipe_m2_auto_placement.json` and `recipe_m2_shared_auto_placement.json`,
and from `df2739f9` for the other ten fixtures. The narrow commit changes four
files, of which **two** are fixtures; neither is among the ten compact fixtures.
Factoring is applied to the intended expanded data, never to older whole-clip
reads. The auto goldens retain all seven consumed field reads: `at`, `at_ms`,
`track`, `hold`, `from`, `to`, `speed`, including literal absent/null/false states.
Pool, legacy eligibility, membership, order and ownership guards remain intact.

The ten compact fixtures retain 15 golden cases: one each for scalar fields,
clip add/split/trim, occurrence delete/duplicate; two each for occurrence media,
placement and create/unplace; three for shot pool. The two expanded automatic
placement fixtures retain four cases each. There are 23 cases across 12 files.
Current positive equal-state occurrence-delete replay and D07 timing proofs
retain their assertions.

Python conformance tests use
`tests.editing_conformance/fixture_loader.py`. Its explicit record paths restore
only missing `created`, `reads` and `selections` from the same record's command.
Only equal duplicate fields are factored. Named placement/occurrence-media
inputs, occurrence-media verified pins, and create-output/unplace-input are
shared at their known control locations. There is no recursive interpretation
of opaque input/output `records`, `command` or `reads` keys, opaque compression,
or general fixture language. Input and output preservation tests compare the
opaque values and their UTF-8 JSON bytes. Product loaders use ordinary JSON.

The flat handoffs are ordinary expanded JSON at
`/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a/scratchpad/orch/design/handoff-agent-expanded-<stem>.json`.
All 12 exports are byte-identical to their intended expanded sources. App must
consume these exports or equivalent expanded copies, never the private Python
encoding. This is Agent source evidence; actual Runtime/App qualification is
not established here. Runtime schema/provider regeneration and attempt-fence
qualification, plus App original-approval/outbox journeys, remain activation
holds. Existing parent-owned completion, append beyond fixed occurrence extent,
video unplace and legacy migration restrictions are unchanged.

## Counts and hashes

`current` is `df2739f9`; `source` is the intended expanded data above; `after`
is the committed compact fixture (or expanded automatic-placement fixture).
Line counts count an already-flat JSON file as one line. Existing formatting is
retained; the reduction comes from duplicate removal rather than minification.
Exact reconstruction is 10/10 fixtures and 15/15 compact cases. Semantic
comparison of the two auto goldens permits only whole consumed-clip reads to
become the seven field reads; it passes 2/2 fixtures and 8/8 cases. No other
capture, command, child or case data changed.

EditCommand schema is byte-unchanged from `df2739f9`: `astrid/core/timeline/schema/edit-command.schema.json`
(SHA-256 `83a476f894f31dc359514e9c081bf28da2b1f8cd515bb30cce14292823753224`). No schema or dependency pin changes.

| fixture stem | cases | current bytes / lines | source bytes / lines | after bytes / lines | source SHA-256 = handoff SHA-256 | after SHA-256 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| recipe_m2_app_clip_scalar_fields | 1 | 161268 / 6317 | 161268 / 6317 | 133227 / 5070 | `8bb75e23c10c9677635d85f33f219b7f1b2d89baf481e3b7ed21f425b76f0c06` | `52dac614ac1b13efda13de9b79ecd3a1713939fd7e6f5f7d07d57ca8ec500348` |
| recipe_m2_clip_add | 1 | 20541 / 906 | 20541 / 906 | 17592 / 770 | `5e9a54e75a0901724dedd3e62c0f37f78840a8edee001f30b92306383800479b` | `cebe45dbf6806c2fb772665ad2c3a416eae172df48d1b0cc48d023cbc80345ff` |
| recipe_m2_clip_split | 1 | 57241 / 2524 | 57241 / 2524 | 45197 / 1951 | `a0e24c1f78d8580f908973745950ed0196c17257831197ec2f75a55abc638f63` | `46bce505cbba3d72f7eb6c060ed9f1cf890e0c50c42a8f50a8edee7d6287e204` |
| recipe_m2_clip_trim | 1 | 52208 / 2275 | 52208 / 2275 | 38282 / 1587 | `4d71a9d184db1b85c220de10842b2dc68e5eb9b48afee180b6c29cf0d5b06152` | `60fcc84d769a2a68b5330288e842ca286f3907510a5dd18bc240c6e314ee612c` |
| recipe_m2_occurrence_delete | 1 | 123486 / 4491 | 123486 / 4491 | 116619 / 4236 | `e66b2464f5d0b779619554aaf2b7ff49e255941e1d3932c5ebe84ad78a0d50ac` | `9a52d55130a4c857e3cb4d8962ea63155c2d3c3b2a6ed035d8babf38666d1e96` |
| recipe_m2_occurrence_duplicate | 1 | 94420 / 3469 | 94420 / 3469 | 88184 / 3230 | `2d0009f60947d4f5f2f8f8f204827315c14319f20da0373789578d0076b39b9f` | `b64b9f9bbed418c5a58455931ad0bdae549ed0ae6e0a6be5c79b30571f19d829` |
| recipe_m2_occurrence_media | 2 | 108547 / 3898 | 108547 / 3898 | 82650 / 2919 | `761153bb5c9de335d212c033bce976315a6846c4bde456b1d32f6aeb35919ad6` | `cf9650d82a3171236dd72a14ca1df2fabee8184edbef63d2020f5eeebcc15d69` |
| recipe_m2_placement | 2 | 79188 / 1 | 79188 / 1 | 58978 / 1 | `7404b665b2918ebf095abc68bd3a20176afb0589b79cf785887c1da50b68d271` | `49bf7e046e79c5d81b3ab183ac11868294a6e5261b727d876f782cf4e4700060` |
| recipe_m2_placement_create_unplace | 2 | 177115 / 6815 | 177115 / 6815 | 124951 / 4742 | `2ee887fd375f9a68682843eb21c69323c2b8bb42d92230e8b50bf7c78791292a` | `e0f01074d0136990b9bc39d882f8015169240cb3167510d870491feeb32c55b0` |
| recipe_m2_shot_pool | 3 | 43819 / 1642 | 43819 / 1642 | 34421 / 1269 | `13f59f8fd0734dff915515efcf2243ceeb1b3f026973793560a6b773ed634078` | `34edbb5e0f99a0b63b8ca8a5c087074ec871b99151df70e58a792ce806c4b484` |
| recipe_m2_auto_placement | 4 | 47994 / 1896 | 53582 / 2166 | 53582 / 2166 | `2d79b76b4c54339460f0f5d1bd45083f83e2784b9dd45b683824362bc2cd1efa` | `2d79b76b4c54339460f0f5d1bd45083f83e2784b9dd45b683824362bc2cd1efa` |
| recipe_m2_shared_auto_placement | 4 | 52232 / 2041 | 64102 / 2605 | 64102 / 2605 | `68934bfe05399636f54122149ac0aab8fce50e6a68c593c3938ec9af35fb7410` | `68934bfe05399636f54122149ac0aab8fce50e6a68c593c3938ec9af35fb7410` |

Ten compact fixtures, current → after: **917,833 → 740,101 bytes** (-177,732) and **32,338 → 25,775 lines** (-6,563).

All twelve changed fixtures, current → after: **1,018,059 → 857,785 bytes** (-160,274) and **36,275 → 30,546 lines** (-5,729).
