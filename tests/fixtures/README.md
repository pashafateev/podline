Test fixtures for raw-session scanning.

These fixtures intentionally mirror the real external-drive layout under
`02_recordings/raw/<date>/<session>/`.

Notes
- The `.WAV` files are tiny text placeholders, not real audio.
- Filenames and folder structure match the recorder naming convention.
- File contents differ slightly so future fingerprint tests can assert stable,
  deterministic hashing from file sizes and names.

Included scenarios
- `2024-01-01/010124_013143`: 2 spoken tracks plus `TrLR`
- `2024-01-07/010724_013643`: 4 spoken tracks plus `TrLR`
- `2024-01-14/011424_020000`: unsupported 1-speaker layout
- `2024-01-21/012124_021500`: unsupported 5-spoken-track layout
