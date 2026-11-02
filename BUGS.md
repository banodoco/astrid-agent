# Pack discovery findings

- **R5, fixed in M3-13 r2 and strengthened in r3:** the discovery cache recursively walked complete pack roots before applying scanner selection. That read hidden bundled `local` assets and scanner-ignored descendants; r2 also removed the project pack tree as an unselected cache input. Cache identity now follows selected discovery roots and visible pack subtrees, while candidate manifests remain in the key so visibility changes invalidate it.
