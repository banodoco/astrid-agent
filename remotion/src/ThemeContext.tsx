// Sprint 5 back-compat shim — ThemeContext physically lives at
// `@banodoco/timeline-composition/typescript/src/ThemeContext.tsx`.
// Re-export so any pre-Sprint-5 deep relative import resolves.
export * from '@banodoco/timeline-composition/theme-api';
