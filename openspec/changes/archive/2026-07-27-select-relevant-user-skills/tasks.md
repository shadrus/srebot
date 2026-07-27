## 1. Backend Selection

- [x] 1.1 Add scope- and trigger-filtered candidate retrieval plus selected-skill rendering.
- [x] 1.2 Add the compact zero-to-three LLM selector prompt, parsing, validation, fail-closed behavior, and token accounting.
- [x] 1.3 Integrate selection into alert, follow-up, and general analyses without changing SODA or alert extraction.
- [x] 1.4 Retry one unparseable length-limited selector response with a larger configurable budget
  and account for both completion attempts.

## 2. Skill Contract Compatibility

- [x] 2.1 Preserve trigger-rule persistence, API handling, resolver matching, and existing values.
- [x] 2.2 Reuse the existing description as required selector guidance without adding a database field.

## 3. Frontend Experience

- [x] 3.1 Preserve trigger-rule editing, payload generation, and catalog display.
- [x] 3.2 Relabel and localize skill description as “When to use” while preserving the existing 255-character contract.

## 4. Verification

- [x] 4.1 Add backend tests for scope filtering, zero-to-three selection, invalid responses, content isolation, integration, and token accounting.
- [x] 4.2 Run backend formatting, linting, schema/tests, bot tests where applicable, and the frontend production build.
- [x] 4.3 Complete the mandatory independent OpenSpec compliance review and resolve all blocking findings.
- [x] 4.4 Add regression coverage for selector truncation and run repository validation.
