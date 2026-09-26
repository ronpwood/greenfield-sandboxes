# Plan: Triad spelling

## What we're solving for

A guitarist picks a root and a quality and sees the three notes of that triad, spelled the way a
music theory book would spell them. Wrong-but-plausible: the right pitches with the wrong letter
names (A# where the key wants Bb).

## Requirements

- R1 — choosing a root and a quality shows exactly three note names
- R2 — every triad is spelled with three consecutive letter names (C E G, never C Fb G)
- R3 — the chosen quality is audible: playback sends those three pitches, nothing else

## Expected values

| id | input | expected | derivation |
|---|---|---|---|

## Approach

Add `spellTriad(root, quality)` to `apps/app/theory.ts`; letter arithmetic first, accidental second.

## Left to the builder

How the three notes are laid out on screen.

## Traps

- Spelling from a pitch-class table keyed by semitone loses the letter: 10 semitones is both A# and Bb (V3).

## Team notes

- The fixed suite already imports `theory.ts`; keep the export name stable. — test_designer (test_design)

## Amendments

### A1 — builder (build)
**Targets:** V5 (new)
**Change:** add a diminished row the brief implies but the table omitted:


**Why (first principles):** the brief lists four qualities; the table covered two.
**Ruling:** accepted by reviewer (review_1): the brief names diminished; the derivation is correct.
