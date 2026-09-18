// Ambient declarations for non-TS assets the bundler understands but tsc does not.
//
// `bun build` resolves `import "./styles.css"` happily; `tsc --noEmit` rejects it with
//   TS2882: Cannot find module or type declarations for side-effect import of './styles.css'
// so without this file the typecheck gate fails code that builds and runs correctly, and
// a build burns one of its three bounded fix loops on a non-defect. Measured 2026-09-18.
//
// A `<link rel="stylesheet">` in index.html sidesteps tsc entirely and is also fine — this
// exists so that BOTH idiomatic ways of adding styles pass the gate, rather than the gate
// silently pushing every app toward one of them.
declare module "*.css";
declare module "*.svg";
declare module "*.png";
declare module "*.webp";
