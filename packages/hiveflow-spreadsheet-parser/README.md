# hiveflow-spreadsheet-parser

Give it a spreadsheet, get back every block of structured data in it — profiled, reviewed by a
human, and emitted as one cleaned CSV per approved table.

## Flow

```
propose  ->  <file>.review.yaml  ->  (human edits + approves)  ->  apply  ->  out/<table>.csv + manifest.json
```

1. **`hiveflow-parse propose file.xlsx`** — a Bedrock-hosted Claude agent walks every sheet, finds each
   table-like region (including messy exports: blank rows between records, merged headers/labels, several
   tables per sheet, pivot matrices, title/total banners), profiles it (row counts, cardinality, dtypes,
   detected formats), and writes an editable `file.xlsx.review.yaml`.
2. **Edit the review file** — rename columns, fix `dtype`/`semantic`, drop or reorder columns, adjust
   `a1_range`/`header_rows`, set `include`/`approved`, optionally add free-text `cleanup_instructions`.
3. **`hiveflow-parse apply file.xlsx.review.yaml --out out/`** — runs the cleanup pipeline per approved
   table and writes `out/<table>.csv` plus `out/<file>.manifest.json`.

`hiveflow-parse run file.xlsx --out out/` chains all three (opens `$EDITOR` between steps).

## Requirements

- Amazon Bedrock access with the Claude models enabled in your region; credentials via the standard AWS
  chain (see `.env.example` at the repo root).
- Node.js + the Claude Code CLI on `PATH` (the Claude Agent SDK shells out to it).

## Scope

xlsx only for now, behind a pluggable reader interface (csv / xls / ods / Google Sheets later).
