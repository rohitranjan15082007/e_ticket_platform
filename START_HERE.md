# Codex: Start here

This bundle contains the complete project skeleton and source requirements. Application files are placeholders, not a working application.

## Read order
1. PROJECT_RULES.md — original implementation rules, unchanged.
2. SPEC.md — original P2P Payment and Withdrawal Specification v1.2, unchanged. Scope: P2P/payment-withdrawal only.
3. PROJECT_BLUEPRINT.md — whole-platform structure, screens, modules and phases.
4. references/1000137679.pdf — original 11-page requirements.
5. references/*.jpg — four clearer source photographs.
6. VISUAL_MAP.html and PROGRESS.md — file map and implementation tracker.

## Naming clarification
The prior skeleton used SPEC.md for its broad structure document. Here that document is named PROJECT_BLUEPRINT.md. SPEC.md is now the original P2P v1.2, exactly as PROJECT_RULES.md expects. Do not apply the P2P-only specification to unrelated payment methods.

## Implementation instruction
Read the documents first. Identify contradictions or genuinely missing business decisions before implementing affected behavior. Keep the given folder structure. Implement in phases, starting with foundation and financial invariants; run relevant tests and update PROGRESS.md after each phase. Never mark placeholders as implemented. Report completed files, validation evidence and remaining work. Keep secrets out of source control. Do not deploy or activate live payments as part of initial implementation.

The visual map is a static planning snapshot. New files and progress changes must be reflected in FILE_MAP.json and PROGRESS.md. No automatic tracking is installed.
