# Retired `local` effects archive

The `local` pack shipped 15 personal effects with Astrid Agent. The owner stopped shipping them (Plan 06
Decision 8, owner statement E10, 1 Nov 2026), and PK-12 removed `astrid/packs/local/**` from the tree.
Only the per-user `personal` pack (`~/Astrid/packs/personal`) remains. The final home of these effects is
the owner's decision; until then the bytes live only in Git history.

Parent commit (last commit that contains them): `97014ca3223c4d118e6e191cbda92cb1bd7fda47`

Restore one path:

```bash
git checkout 97014ca3223c4d118e6e191cbda92cb1bd7fda47 -- <path>
```

Restore everything:

```bash
git checkout 97014ca3223c4d118e6e191cbda92cb1bd7fda47 -- astrid/packs/local
```

To use an effect again, copy its `rendering/elements/effects/<id>` folder (and any `shared/` helper it
imports) into your personal pack and declare it in `personal/pack.yaml`, as `astrid` does for effects
made with the `rendering.html_canvas_effect` action.
Change the copied `element.yaml`'s `pack_id: local` to `pack_id: personal`, and copy the element's
`rendering:` declaration (with its `resources`) from the old `astrid/packs/local/pack.yaml`.
`tests/packs/local/test_end_spanning_card_assets.py` does exactly this for `end-spanning-layer`.

Retired with the pack (tests of its bytes): `tests/packs/local/test_personal_element_motion.test.ts`
and `tests/visual-boundary.test.mjs`; restore them from the same parent commit together with the pack.

## Removed paths (87)

| Path | sha256 | Bytes |
|---|---|---|
| `astrid/packs/local/docs/SKILL.md` | `2362f4107b266a7c5d9e4e85532d1a7f3a52298d47b0f9ee9ce0be8100733542` | 519 |
| `astrid/packs/local/pack.yaml` | `a0021ad3c428dab752d64dcb0ac2394812fa108c22e3710dbd50f844dd4f8c34` | 9837 |
| `astrid/packs/local/rendering/elements/boundary.ts` | `cc8a8700a08cb1991f00bc1f0d231aa7f256227fb86cfe474f530827ae010fe7` | 10025 |
| `astrid/packs/local/rendering/elements/effects/animated-media-transform/component.tsx` | `c410a83f05eb74f21e9f55889a22a243f015c8739d02a8b2567dd89ca73b3f34` | 2835 |
| `astrid/packs/local/rendering/elements/effects/animated-media-transform/element.yaml` | `01f430a9b041de68678385b18be9342c44c1b107ea2db9a667c4ee443402caf3` | 2206 |
| `astrid/packs/local/rendering/elements/effects/animated-media-transform/motion.ts` | `21b2cb3a7e5b5c28de5bba9c2d3121e6d14053d92f909e34f860a7ac1b4803c9` | 3237 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-0.png` | `416aa587a0493d75f901256b00146ec8f34578535a9c67bdf6eb7cdd6fb5d6f1` | 1240266 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-1.png` | `9c348d11c59b973214b2aec0d9886017d70db44ecc0bc5ec44a42f4aacd55223` | 873022 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-2.png` | `2464851c6b826b96cc09b2d83aa79ba2b00ebfb1e4ccb1cf3a16a54242141fa8` | 1597674 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-3.png` | `721367fbd93d819af70b4d9993d271e2c29892557c859a837597f255a524be3d` | 1137594 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-4.png` | `cf1ddd8c30b3445e08344712e291447fd124cd76d9ecbdb7b14aa41433395669` | 991012 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-5.png` | `3bda96e4105793b03b74f9a46fb9766ae510020c45140ca1f4b3e674aa0d3bd0` | 1359873 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-6.png` | `42985be628e46731c7f37faed643b68b2afd80cc7c2d4e366ff1abb70cd3e1ff` | 718353 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/card-7.png` | `4db386bf2c5f92a9823f1f4952995bb6704db0b5de5903ae86bbb66cbfd60bde` | 340612 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/assets/plate.mp4` | `1414594517eb5d53c0e656d4a39bb1e8ed1bf3040f46c6244e549c123d76e785` | 965176 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/component.tsx` | `0318f3e9b3bc78c3d5b800b19ea6078ced83b5b5ff79b85501d74e764f6c0b37` | 5190 |
| `astrid/packs/local/rendering/elements/effects/end-codex-transform/element.yaml` | `331e082f842923d232f5dfd46ca93a584a0a1c28902fd30ffecb176b2e42dc67` | 987 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/base.png` | `a33f523be3960dcb9b9d9064530adf475e26d28531c7c4c4163daa1aa96cac85` | 1038897 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-0.png` | `416aa587a0493d75f901256b00146ec8f34578535a9c67bdf6eb7cdd6fb5d6f1` | 1240266 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-1.png` | `9c348d11c59b973214b2aec0d9886017d70db44ecc0bc5ec44a42f4aacd55223` | 873022 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-2.png` | `2464851c6b826b96cc09b2d83aa79ba2b00ebfb1e4ccb1cf3a16a54242141fa8` | 1597674 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-3.png` | `721367fbd93d819af70b4d9993d271e2c29892557c859a837597f255a524be3d` | 1137594 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-4.png` | `cf1ddd8c30b3445e08344712e291447fd124cd76d9ecbdb7b14aa41433395669` | 991012 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-5.png` | `3bda96e4105793b03b74f9a46fb9766ae510020c45140ca1f4b3e674aa0d3bd0` | 1359873 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-6.png` | `42985be628e46731c7f37faed643b68b2afd80cc7c2d4e366ff1abb70cd3e1ff` | 718353 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/assets/card-7.png` | `4db386bf2c5f92a9823f1f4952995bb6704db0b5de5903ae86bbb66cbfd60bde` | 340612 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/component.tsx` | `5f1ff32f7f7632aba184794d9633d12928192871acad892f218cc33fc95d100f` | 10382 |
| `astrid/packs/local/rendering/elements/effects/end-mid-combined/element.yaml` | `430cc6b1d4b369af4dfe1573bb186cf54be066b6d10561a178cc9c096eadec00` | 1025 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-0.png` | `416aa587a0493d75f901256b00146ec8f34578535a9c67bdf6eb7cdd6fb5d6f1` | 1240266 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-1.png` | `9c348d11c59b973214b2aec0d9886017d70db44ecc0bc5ec44a42f4aacd55223` | 873022 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-2.png` | `2464851c6b826b96cc09b2d83aa79ba2b00ebfb1e4ccb1cf3a16a54242141fa8` | 1597674 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-3.png` | `721367fbd93d819af70b4d9993d271e2c29892557c859a837597f255a524be3d` | 1137594 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-4.png` | `cf1ddd8c30b3445e08344712e291447fd124cd76d9ecbdb7b14aa41433395669` | 991012 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-5.png` | `3bda96e4105793b03b74f9a46fb9766ae510020c45140ca1f4b3e674aa0d3bd0` | 1359873 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-6.png` | `42985be628e46731c7f37faed643b68b2afd80cc7c2d4e366ff1abb70cd3e1ff` | 718353 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/card-7.png` | `4db386bf2c5f92a9823f1f4952995bb6704db0b5de5903ae86bbb66cbfd60bde` | 340612 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/cover16last.png` | `db4afe42896d5d3317c65d4d88c0d0482027f677c5cae802e63500f39e448172` | 361671 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/assets/plate.mp4` | `69fcdccc5b0c18450dffe8edc2a58d767552949c5a0eea208c69835a259e845f` | 651496 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/component.tsx` | `481c181539424cd2b27e12dfbeb8b108ef2da5ecf5871c9f93b3594946db9141` | 7324 |
| `astrid/packs/local/rendering/elements/effects/end-minimax-animate/element.yaml` | `d1a4b1048ac16d97ae5b842740acb2afe3240c90dce0c9ffe6b726d4da45c59b` | 1027 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-0-canonical-mink.png` | `dfc516cf14c88f9da8dae704dd1fd8d060e1e641cfa444d08bc68c495798de2c` | 755246 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-0.png` | `6bd6a895cfe78f3dc2dc4b273e999fb05e7883bfee3faa0494c1d0c546591851` | 507593 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-1.png` | `1233c44cac8400b2467ef8654b1365372f7b8d00dde6e5cd2f14b0259be3061d` | 731235 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-2.png` | `6c21fbd4024a2783d51b8cc777fa6e448d0a32027e34813eb070383ebd78371b` | 562170 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-3.png` | `88cdfffd9b11ec598abc53e00458a07a627c424b5c3f6f65c749849e8ca7e26f` | 1462140 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-4-canonical-mink.png` | `c71d9875085eed4cd731bf30e022055fc78bc091e4cb40a29daf4a1baec8c743` | 1509793 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-4.png` | `8841c0dc9bf431140b9248cff1c35a90e1ef5f94546fcbe163082d93bba2f125` | 1498999 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-5-canonical-mink.png` | `4d551743b190b1a9c1b76f779eeb8d1625db6dd9aa8955f8c39500cb7a3e84a8` | 1619131 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-5.png` | `84e6b79630750bfe142256820cd4599829069adf07bcda10e6dd8f2145e3a97b` | 1582358 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-6.png` | `42985be628e46731c7f37faed643b68b2afd80cc7c2d4e366ff1abb70cd3e1ff` | 718353 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/assets/card-7.png` | `4db386bf2c5f92a9823f1f4952995bb6704db0b5de5903ae86bbb66cbfd60bde` | 340612 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/component.tsx` | `0e850eaa8af9c099105a2d6f2281f53a2ec60bb280fd2e38cf13d530960cfef0` | 16575 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/element.yaml` | `26d1c73f538ecb3d320edd3ab57d6002265c8b5c6afad8a7e763fe22767e4532` | 3848 |
| `astrid/packs/local/rendering/elements/effects/end-spanning-layer/timing.ts` | `32cfbaf31869f9e87848108f9ffbf40ac29205ad663d47d80756c2bef7358fe1` | 3630 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-0.png` | `416aa587a0493d75f901256b00146ec8f34578535a9c67bdf6eb7cdd6fb5d6f1` | 1240266 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-1.png` | `9c348d11c59b973214b2aec0d9886017d70db44ecc0bc5ec44a42f4aacd55223` | 873022 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-2.png` | `2464851c6b826b96cc09b2d83aa79ba2b00ebfb1e4ccb1cf3a16a54242141fa8` | 1597674 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-3.png` | `721367fbd93d819af70b4d9993d271e2c29892557c859a837597f255a524be3d` | 1137594 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-4.png` | `cf1ddd8c30b3445e08344712e291447fd124cd76d9ecbdb7b14aa41433395669` | 991012 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-5.png` | `3bda96e4105793b03b74f9a46fb9766ae510020c45140ca1f4b3e674aa0d3bd0` | 1359873 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-6.png` | `42985be628e46731c7f37faed643b68b2afd80cc7c2d4e366ff1abb70cd3e1ff` | 718353 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/card-7.png` | `4db386bf2c5f92a9823f1f4952995bb6704db0b5de5903ae86bbb66cbfd60bde` | 340612 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/assets/plate.mp4` | `9fbc6e683f446a75370b69e9bf87091250c275d601e3d5c79dfc847f159fb55c` | 527826 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/component.tsx` | `01385f7bd429b0c0d3fc3471d6dd162e45de8117c9539304419a4069a930dfad` | 5408 |
| `astrid/packs/local/rendering/elements/effects/ending-carousel/element.yaml` | `6494d7c7f64f5ed1e4077194ebf671ec3631770766e414450ab9b7f43b6e35ff` | 1124 |
| `astrid/packs/local/rendering/elements/effects/event-card/component.tsx` | `2e8f46e346b44807205880cd8a128e88227dc2b05b1fb8c4dddac5bb277ac4f3` | 4079 |
| `astrid/packs/local/rendering/elements/effects/event-card/element.yaml` | `78b3ab23b731d50e29a64b42fe49dfc4ea98a953f6d8bdc705d6733822f8affa` | 1897 |
| `astrid/packs/local/rendering/elements/effects/frame-overlay/assets/frame.png` | `730113561d7848cea6156c3c0e4f08c1d2db693d7522671cf654f044de528605` | 2949 |
| `astrid/packs/local/rendering/elements/effects/frame-overlay/component.tsx` | `a7f9fd70cf64740c6979ba603d78b960badcd6daee02e3265a9df561810d8256` | 1356 |
| `astrid/packs/local/rendering/elements/effects/frame-overlay/element.yaml` | `efef8f1e414089d11e0349af42c56c186f54f22b54b76e168ea54bea33ed1c62` | 764 |
| `astrid/packs/local/rendering/elements/effects/model-trends/component.tsx` | `498e7818f2c8ad7fbb011bc5e8028b67572b03e8ea073061c0ff8821500f1bc2` | 28239 |
| `astrid/packs/local/rendering/elements/effects/model-trends/element.yaml` | `e4b4d7cfc22ad2479d1f9c56e1bdd18ab1a9711ea47c9bca3a9305d8492b21b9` | 2456 |
| `astrid/packs/local/rendering/elements/effects/neon-orbit-card/component.tsx` | `2e8f46e346b44807205880cd8a128e88227dc2b05b1fb8c4dddac5bb277ac4f3` | 4079 |
| `astrid/packs/local/rendering/elements/effects/neon-orbit-card/element.yaml` | `ce1c7788633135f6302e6ba312b0bc7b7ed80808095a0b3a5b8c67f0e2b415de` | 1867 |
| `astrid/packs/local/rendering/elements/effects/scrolling-guide/component.tsx` | `356324b56de0dc7c63bcda3e4f28a22fe08c924f932fcdedd552b3f56259ad78` | 6490 |
| `astrid/packs/local/rendering/elements/effects/scrolling-guide/element.yaml` | `ca4dc8f57158814a875b6b7718eb5caebe9fe2abb5d0f269ecb7176eecd9212c` | 1528 |
| `astrid/packs/local/rendering/elements/effects/sliding-media/component.tsx` | `56065d85fedb4a74da10e4fc6d134585b15e82614c8b96c5d3147a7d8a45cb2d` | 2696 |
| `astrid/packs/local/rendering/elements/effects/sliding-media/element.yaml` | `02366e3d3ed5da688c9fb2d9015f81f8543554a9ec22a6acfa552bb1416a5986` | 646 |
| `astrid/packs/local/rendering/elements/effects/text-card/component.tsx` | `a717914bb0bcbeead78658bb9f05e9bd37a5545df49142d4e07cd73d27e39686` | 19768 |
| `astrid/packs/local/rendering/elements/effects/text-card/element.yaml` | `080127fb507ee2fcfe1e4a8bddc53d0c5c7b12e16cd922b386bf802907a408d4` | 1802 |
| `astrid/packs/local/rendering/elements/effects/vibe-comfy-asset-overlay/component.tsx` | `3100bf2baec6d395ed7c8cc4a9164df720260616b89de1f71ab751060669c3a2` | 14066 |
| `astrid/packs/local/rendering/elements/effects/vibe-comfy-asset-overlay/element.yaml` | `1c6e78dd2c2fd54218c21a21dc330775db88642a251ab9a775b910dffc1a5ff5` | 771 |
| `astrid/packs/local/rendering/elements/effects/vibe-comfy-bumper/component.tsx` | `3a6856e4d332a5e8c1983c2a3ea65972873950821e99e7a572ec4890e01c94a0` | 10904 |
| `astrid/packs/local/rendering/elements/effects/vibe-comfy-bumper/element.yaml` | `f134e11545c2104db341b6c40c34394797e9b7c2889480017dbc3d8b22f3a958` | 787 |
| `astrid/packs/local/rendering/elements/seam-intent.ts` | `00dcfa82b5378637b7e9da00685e6ea38f077ce729b179491df3691116d4abc2` | 4835 |
| `astrid/packs/local/shared/clip-effects.ts` | `418cb06503e0e330b27f388e7f1bcce72196e3ddd74e22a4af2ea9d2b3617025` | 4504 |
| `astrid/packs/local/shared/readiness-image.tsx` | `353c117886d9dcc0932ae16e25ca4b85e2433919ceaebff7834a96142441dd99` | 5129 |
