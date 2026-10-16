// Sprint 5: package-internal type aliases.
//
// These mirror the structure of `tools/remotion/src/types.ts` (which re-exports
// from `types.generated.ts`), but to keep the package self-contained we declare
// the minimal interfaces here. The Banodoco shell at `tools/remotion/` keeps
// the real codegen-driven `types.generated.ts` for its own validation work
// (smoke fixture asserts allowed-arrays match); the package consumers only
// need the runtime shape used by the composition.
export {};
