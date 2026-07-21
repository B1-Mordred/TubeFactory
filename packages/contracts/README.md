# Contracts

These JSON Schemas are the provider-neutral boundaries used for schema-constrained editorial output. They reject unknown properties. Domain policy performs the relational checks JSON Schema cannot express, including exact narration offsets, approved claim membership, quotation matching, complete script structure, scene coverage and synthetic-evidence restrictions.

- `script-draft.schema.json`: writer output before deterministic and independent verification.
- `scene-spec.schema.json`: one versioned storyboard scene consumed later by deterministic rendering.
- `comfy-workflow.schema.json`: approved API-format ComfyUI workflow registry entry.
- `voice-request.schema.json` / `voice-response.schema.json`: provider-neutral narration boundary.
- `production-manifest.schema.json`: immutable render, asset, caption, chapter, and source provenance.
